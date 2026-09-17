"""Windows sandboxed decode transport (fauxcasa-i92.3).

Broker-side production home for the machinery proven by
docs/research/spikes/appcontainer-spawn-spike.py (see bd memory
win-appcontainer-decode-sandbox for the 6 load-bearing gotchas this
module encodes). `DecodePoolSet` (below) is owned by
`decodefacade.WinSandboxTransport`, which `decodefacade.DecodeService`
uses for route="still" decodes whenever the sandbox is up (see the
design doc, docs/design/decode-service.md sections 2 and 4); thumbcache.py
also imports `DecodePoolSet` directly for its batch-arena sizing.

Wire shape (design doc sec 2, 2.1-2.3), summarized for this file's
worker (decodesvc_worker_win.py):

    worker -> broker (first frame):
        {"hello": 1, "proto": 1, "bundle": {...}, "ops": [...],
         "arena_bytes": N, "max_pixels": N}
    broker -> worker (arena handoff, before any job):
        {"op": "attach_arena", "handle": N}
    worker -> broker (ack):
        {"op": "attach_arena", "ok": true|false, "error": "..."}
    worker -> broker (phase 2 complete):
        {"locked": true, "is_appcontainer": true|false}
    broker -> worker (decode job):
        {"id": N, "op": "decode", "edge": 0, "pixfmt": "RGBA8", "handle": N}
    worker -> broker (decode result):
        {"id": N, "ok": true, "source": {"w":.., "h":..},
         "pixels": {"w":.., "h":.., "stride":.., "pixfmt":"RGBA8",
                    "off":.., "len":..}}
        {"id": N, "ok": false, "error": "CORRUPT", "detail": "..."}
    broker -> worker (probe job, test-only, requires
        FAUXCASA_DECODESVC_PROBE=1 in the worker's environment):
        {"id": N, "op": "probe", "attempt": "user_file_read",
         "target": "...", "handle": N}
    worker -> broker (probe result):
        {"id": N, "op": "probe", "attempt": "...", "allowed": bool,
         "detail": "...", "data_b64": "..."}
    (test-only `stall` probe attempt: sleeps `seconds` (capped 300s), then
        replies normally like any other probe -- a live, non-hostile
        worker that never answers within a job's deadline, exercising the
        broker's kill-on-timeout path, fauxcasa-i92.3.1, design doc sec 1.)

Every control-channel message is length-prefixed JSON: a 4-byte
little-endian length, then UTF-8 JSON, hard-capped at MAX_CONTROL_MSG
(design doc sec 2 item 1). Pixels never travel on the control channel;
they live in a broker-created anonymous shared-memory arena, duplicated
into the worker at spawn and mapped read-write there, read-write on the
broker side too (v1 simplification, design doc sec 2 footnote 1) --
`read_arena()` always COPIES out (design doc sec 2.4 checklist item 6).

ctypes + stdlib only -- no pywin32. This module is importable on any
platform (the dataclass-level pieces: frame codec, response validation,
hello/locked parsing) so the trusted-side protocol-fuzz tests can run
everywhere; `WinSandboxWorker.spawn()` is the one entry point that
requires Windows and raises RuntimeError otherwise.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import math
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_log = logging.getLogger("fauxcasa.decodesvc_win")

from decodesvc import (
    ARENA_BYTES,
    BundleInfo,
    DecodeResult,
    DecodeServiceError,
    ErrorCode,
    FaceRegion,
    Hello,
    MAX_CAPTION_BYTES,
    MAX_CONTROL_MSG,
    MAX_FACES,
    MAX_KEYWORDS,
    MetaFields,
    PixelBuffer,
    PixelFormat,
    PROTO,
    ProtocolViolation,
)

PROFILE_NAME = "fauxcasa.decode.worker"
ARENA_DEFAULT_BYTES = ARENA_BYTES
DEFAULT_MEM_LIMIT_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB, design doc sec 4
# fauxcasa-i92.3.4: bounds spawn()'s entire handshake (hello read incl. the
# resync loop, attach_arena ack, locked) the same way recv_response's timer
# bounds a job -- a worker that spawns but never writes a byte would
# otherwise hang the broker forever, a strictly easier attack than
# stalling an in-progress job (today's three blocking reads there are all
# unbounded). Measured healthy handshake on this box is ~100ms; 30s bounds
# the hang while leaving slow CI runners enormous headroom.
DEFAULT_SPAWN_DEADLINE_MS = 30_000

# The only error codes an honest worker ever reports about ITS OWN work
# (decodesvc_worker_win.py emits exactly these). The rest -- TIMEOUT,
# WORKER_CRASHED, PROTOCOL, CANCELLED -- are broker-side judgments about
# the worker; a worker claiming one in a response is lying about the
# broker's own state and gets the kill/no-retry PROTOCOL treatment.
WORKER_ERROR_CODES = frozenset(
    {ErrorCode.UNSUPPORTED, ErrorCode.CORRUPT, ErrorCode.TOO_LARGE})

# A fixed, independently-recomputable byte pattern used by the
# `arena_write` probe attempt (decodesvc_worker_win.py recomputes the
# identical formula rather than importing this module, to keep the
# sandboxed worker's import surface minimal).
ARENA_PROBE_PATTERN = bytes((i * 7 + 11) % 256 for i in range(256))


# ---------------------------------------------------------------------------
# Length-prefixed JSON framing (design doc sec 2 item 1). Pure functions on
# any binary file-like object -- no Windows API needed, so the trusted-side
# protocol-fuzz tests exercise these on every platform.

def _read_exact(fh: Any, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = fh.read(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _read_frame(fh: Any) -> dict:
    """Read one length-prefixed JSON frame. A closed/EOF pipe raises
    DecodeServiceError(WORKER_CRASHED) -- from the broker's point of view
    that IS what happened. An oversized or malformed frame raises
    ProtocolViolation (evidence of compromise, design doc sec 1)."""
    header = _read_exact(fh, 4)
    if header is None:
        raise DecodeServiceError(
            ErrorCode.WORKER_CRASHED, "control pipe closed while reading frame length")
    (length,) = struct.unpack("<I", header)
    if length > MAX_CONTROL_MSG:
        raise ProtocolViolation(
            f"incoming control frame {length} bytes exceeds MAX_CONTROL_MSG {MAX_CONTROL_MSG}")
    payload = _read_exact(fh, length)
    if payload is None:
        raise DecodeServiceError(
            ErrorCode.WORKER_CRASHED, "control pipe closed mid-frame")
    return _decode_frame_payload(payload)


def _decode_frame_payload(payload: bytes) -> dict:
    """Parse one already-framed control payload; shared by the strict
    _read_frame and the hello-tolerant reader's strict retry."""
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        # ValueError covers json.JSONDecodeError (a subclass) AND the
        # int-string conversion cap (a >4300-digit integer literal raises
        # bare ValueError, not JSONDecodeError); RecursionError covers a
        # deeply-nested object. A hostile worker can fit any of these in a
        # sub-64 KiB frame, so all must take the protocol-violation path
        # (kill + counter), not escape as an unexpected exception.
        raise ProtocolViolation(f"malformed JSON control frame: {e!r}") from e
    if not isinstance(obj, dict):
        raise ProtocolViolation(f"control frame is not a JSON object: {type(obj).__name__}")
    return obj


def _drain_pipe(fh: Any, limit: int) -> bytes:
    """Read whatever is left on `fh` up to `limit` bytes or EOF. Only safe
    to call once the writer is known to be gone (the worker process has
    exited and the broker closed its own copy of the write end at spawn),
    otherwise this would block like any pipe read. Never raises."""
    buf = bytearray()
    try:
        while len(buf) < limit:
            chunk = fh.read(min(4096, limit - len(buf)))
            if not chunk:
                break
            buf += chunk
    except OSError:
        pass
    return bytes(buf)


def _write_frame(fh: Any, obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    if len(data) > MAX_CONTROL_MSG:
        raise ProtocolViolation(
            f"outgoing control frame {len(data)} bytes exceeds MAX_CONTROL_MSG {MAX_CONTROL_MSG}")
    try:
        fh.write(struct.pack("<I", len(data)) + data)
        fh.flush()
    except OSError as e:
        raise DecodeServiceError(
            ErrorCode.WORKER_CRASHED, f"control pipe write failed: {e}") from e


HELLO_RESYNC_MAX_NOISE = 4096  # bytes of leading noise tolerated (see below)


def _read_hello_frame_tolerant(fh: Any) -> tuple[dict, int]:
    """Like _read_frame, but tolerates leading non-frame bytes before the
    worker's first real frame, up to HELLO_RESYNC_MAX_NOISE bytes. Returns
    (hello_dict, noise_bytes_discarded).

    Gotcha 7 (found during Stage B bring-up, this box): a uv-managed base
    CPython interpreter, when spawned inside the AppContainer, prints one
    non-fatal diagnostic line to stdout BEFORE any Python code runs --
    'Failed to find real location of <path>\\n' -- because the
    AppContainer token lacks traverse rights on ancestor directories of
    the interpreter's own path (no default "bypass traverse checking"
    privilege), so the interpreter's own path-canonicalization probe
    fails; it then falls back to the given path and continues completely
    normally (hello, decode, and every probe all work). This is
    broker-controlled noise from OUR OWN spawn (never attacker
    data -- it happens before the worker has touched anything), so
    resyncing past it here does not weaken the protocol's strictness
    against a compromised worker. EVERY read after this one (attach_arena
    ack, locked, every job response) uses the strict _read_frame with
    zero tolerance.

    Deliberately NOT a byte-by-byte sliding resync: a naive "shift the
    4-byte candidate window by one and retry" scanner has a real failure
    mode here -- a window straddling the boundary between ASCII noise
    text and the true binary length prefix can itself look like a small
    "plausible" length (e.g. [last_ascii_byte, 0xd1, 0x00, 0x00] decodes
    to a value < MAX_CONTROL_MSG purely by byte-pattern coincidence),
    triggering a read for a length that will never arrive (the worker is
    waiting on OUR next message) -- an apparent hang that only resolves
    on EOF. Bisected and confirmed on this box before this fix. Instead:
    try the strict framing exactly once (which can only be fooled by
    noise if the very first 4 bytes happen to already be a valid frame,
    impossible for ASCII diagnostic text -- see inline comment below);
    on failure to frame at all (length prefix over the cap -- the only
    way ASCII noise presents), discard through the next newline (bounded)
    and retry the strict framing exactly once more. Two bounded attempts,
    no scanning; a payload that FRAMES but doesn't parse as a hello is a
    protocol violation, never noise (see inline comment).

    fauxcasa-yfq: every exception raised here carries the raw bytes
    consumed so far as `exc.leading_bytes`, so spawn() can reassemble the
    worker's real startup output (these bytes plus whatever is still in
    the pipe once the worker has exited) instead of reporting the second
    noise line's first four bytes as a frame length. The ONE-line
    tolerance itself is deliberately unchanged: a worker that is still
    alive after two unframeable lines is not a known-good case, and
    widening the pre-hello tolerance for it would loosen the parser for
    every spawn to explain a failure that spawn() can explain better by
    looking at the exit code."""
    header = _read_exact(fh, 4)
    if header is None:
        exc = DecodeServiceError(
            ErrorCode.WORKER_CRASHED, "control pipe closed before any bytes (worker died before hello)")
        exc.leading_bytes = b""
        raise exc
    (length,) = struct.unpack("<I", header)
    consumed = bytearray(header)
    # Any 4 bytes drawn entirely from printable ASCII text (as this box's
    # known diagnostic line is) decode, as a little-endian u32, to a value
    # with its top byte in [0x20, 0x7E] -- always > MAX_CONTROL_MSG. So this
    # branch is only ever taken for a genuine (small) frame OR a coincidence
    # too improbable to design around; either way we validate the parse.
    # length == 0 is a successfully framed (empty) message, not ASCII noise:
    # it must take the strict branch below and fail parse as a protocol
    # violation, never fall through to newline-resync (which would block).
    if length <= MAX_CONTROL_MSG:
        payload = _read_exact(fh, length)
        if payload is None:
            exc = DecodeServiceError(
                ErrorCode.WORKER_CRASHED, "control pipe closed mid-frame while reading the first frame")
            exc.leading_bytes = bytes(consumed)
            raise exc
        consumed += payload
        # FIX 4 (P2) + Codex review PR110 (P2): ANYTHING that framed
        # successfully is worker protocol bytes, never gotcha-7 noise --
        # the ASCII diagnostic can't produce an in-cap length prefix (see
        # comment above this branch). A framed payload that is malformed
        # JSON, a non-object, or a non-hello object is a real (if wrong)
        # first message from a worker that has already consumed our
        # attention and may now be blocked waiting for attach_arena.
        # Falling through to newline-resync here would discard real
        # protocol bytes and then block in fh.read(1) waiting for a '\n'
        # that may never arrive, hanging spawn() forever. Raise promptly
        # instead; the resync below is reserved for the known unframeable
        # ASCII interpreter diagnostic.
        try:
            obj = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as e:
            # Same tuple as _read_frame: hostile capped JSON can raise bare
            # ValueError (huge int literal) or RecursionError (deep nesting).
            exc = ProtocolViolation(
                f"first framed control message was not valid JSON: {e!r}")
            exc.leading_bytes = bytes(consumed)
            raise exc from e
        if not isinstance(obj, dict):
            exc = ProtocolViolation(
                f"first framed control message was not a JSON object: "
                f"{type(obj).__name__}")
            exc.leading_bytes = bytes(consumed)
            raise exc
        if obj.get("hello") != 1:
            exc = ProtocolViolation(
                f"first framed control message was not a hello handshake: {obj!r}")
            exc.leading_bytes = bytes(consumed)
            raise exc
        return obj, 0

    noise = bytes(consumed)
    if b"\n" not in noise:
        extra = bytearray()
        while b"\n" not in extra:
            if len(noise) + len(extra) >= HELLO_RESYNC_MAX_NOISE:
                exc = ProtocolViolation(
                    f"first {len(noise) + len(extra)} bytes of worker stdout were not "
                    f"a valid hello frame and contained no newline to resync past: "
                    f"{bytes(noise + extra)!r}")
                exc.leading_bytes = noise + bytes(extra)
                exc.unframed = True
                raise exc
            b = fh.read(1)
            if not b:
                exc = DecodeServiceError(
                    ErrorCode.WORKER_CRASHED,
                    f"control pipe closed while discarding a leading diagnostic "
                    f"line (gotcha 7); {len(noise) + len(extra)} noise bytes so "
                    f"far: {bytes(noise + extra)!r}")
                exc.leading_bytes = noise + bytes(extra)
                exc.unframed = True
                raise exc
            extra += b
        noise = noise + bytes(extra)

    # Strict retry -- no further tolerance. Inlined rather than calling
    # _read_frame so the 4 header bytes stay attached to any exception as
    # leading_bytes: when they are really the first 4 characters of a
    # SECOND diagnostic line (fauxcasa-yfq: "python.exe: can't open file
    # ..." -> b'pyth'), spawn() needs them to show the line whole.
    #
    # `exc.unframed = True` marks the exceptions where the worker never
    # produced a single well-formed length prefix -- the only ones spawn()
    # may re-code as WORKER_CRASHED after a non-zero exit. Anything that
    # FRAMED (a plausible length followed by a bogus or non-hello body)
    # is protocol bytes and keeps its PROTOCOL code even if the worker
    # then exits: that is the evidence-of-compromise case (review round 1).
    header2 = _read_exact(fh, 4)
    if header2 is None:
        exc = DecodeServiceError(
            ErrorCode.WORKER_CRASHED,
            "control pipe closed after a leading diagnostic line, before any frame "
            "(worker died before hello)")
        exc.leading_bytes = noise
        exc.unframed = True
        raise exc
    (length2,) = struct.unpack("<I", header2)
    if length2 > MAX_CONTROL_MSG:
        exc = ProtocolViolation(
            f"incoming control frame {length2} bytes exceeds MAX_CONTROL_MSG "
            f"{MAX_CONTROL_MSG} (after discarding {len(noise)} leading noise bytes)")
        exc.leading_bytes = noise + header2
        exc.unframed = True
        raise exc
    payload2 = _read_exact(fh, length2)
    if payload2 is None:
        exc = DecodeServiceError(
            ErrorCode.WORKER_CRASHED, "control pipe closed mid-frame")
        exc.leading_bytes = noise + header2
        raise exc
    try:
        hello_msg = _decode_frame_payload(payload2)
    except ProtocolViolation as e:
        e.leading_bytes = noise + header2 + payload2
        raise
    if hello_msg.get("hello") != 1:
        exc = ProtocolViolation(
            f"expected hello handshake after discarding {len(noise)} leading "
            f"noise bytes, got {hello_msg!r}")
        exc.leading_bytes = noise + header2 + payload2
        raise exc
    return hello_msg, len(noise)


# fauxcasa-yfq: how much of a dead worker's remaining stdout/stderr spawn()
# reads into the error message. Startup diagnostics are a few lines; the
# cap only guards against a pathological worker that wrote a lot before
# dying.
PREHELLO_OUTPUT_LIMIT = 64 * 1024

_PREHELLO_PERMISSION_MARKERS = ("permission denied", "errno 13", "access is denied",
                                "can't open file", "cannot open file")

# fauxcasa-ayh: the actionable fix for a worker PYTHONPATH (PySide6 site-
# packages) that lives on a volume refusing the AppContainer per-SID grant
# (ReFS/Dev Drive, typically because UV_CACHE_DIR points there) -- ONE
# constant so describe_prehello_death's post-mortem message and
# decodefacade's pre-first-spawn warning (preflight_worker_grants) never
# drift apart.
UNGRANTABLE_PYTHONPATH_HINT = (
    "put the uv cache / venv on a grantable volume (e.g. unset UV_CACHE_DIR "
    "so it defaults to %LOCALAPPDATA%\\uv) or point FAUXCASA_WORKER_PYTHON "
    "at an interpreter whose site-packages is on one")


def describe_prehello_death(output: bytes, exit_code: int | None, worker_args: list[str],
                            grant_errors: dict[str, str],
                            pythonpath: str | None = None) -> str:
    """Turn a worker's pre-hello startup output plus its exit code into the
    human-readable reason spawn() reports (fauxcasa-yfq). Pure function so
    the tests can feed it a fake worker's output on any platform.

    `output` is everything the worker wrote before dying (the bytes the
    hello reader consumed plus whatever _drain_pipe recovered);
    `grant_errors` is spawn()'s {path: error} map of best-effort ACL
    grants that failed. When the output shows the interpreter could not
    open the worker script (the observed symptom on a volume whose ACL
    refuses the per-SID grant -- ReFS/Dev Drive, or a volume the user
    does not own), the message names the failed grant as the cause rather
    than leaving the reader to connect the two."""
    text = output.decode("utf-8", "replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    parts: list[str] = []
    if exit_code is None:
        parts.append("worker died before its hello frame")
    else:
        parts.append(f"worker exited before its hello frame (exit code {exit_code:#010x})")
    if lines:
        shown = [ln if len(ln) <= 300 else ln[:300] + "..." for ln in lines[:8]]
        joined = " | ".join(shown)
        if len(lines) > len(shown):
            joined += f" | ... ({len(lines) - len(shown)} more lines)"
        parts.append(f"its startup output was: {joined}")
    else:
        parts.append("it wrote nothing")
    lowered = text.lower()
    permission_hit = any(m in lowered for m in _PREHELLO_PERMISSION_MARKERS)
    if permission_hit or grant_errors:
        script = next((a for a in worker_args if a.lower().endswith(".py")), None)
        if permission_hit and script:
            parts.append(f"the AppContainer could not read the worker script {script!r}")
        if grant_errors:
            failed = "; ".join(f"{p!r}: {err}" for p, err in grant_errors.items())
            parts.append(
                "the AppContainer read+execute ACL grant failed on: " + failed
                + " -- the volume holding that path refuses the per-SID grant "
                "(typical of a ReFS/Dev Drive or a volume this user does not "
                "own), so the sandbox cannot read anything there; see "
                "docs/decode-threat-model.md 'Sandbox degraded on a Dev Drive'")
            if pythonpath and pythonpath in grant_errors:
                parts.append(
                    "that path is the worker PYTHONPATH (PySide6 lives there), "
                    "which cannot be staged like the worker script; "
                    + UNGRANTABLE_PYTHONPATH_HINT)
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Handshake parsing (design doc sec 2.1, sec 4). Pure dict validators --
# cross-platform, no live worker needed (the "hello/proto" test feeds these
# a plain python -c stub's output).

def parse_hello(msg: dict, expected_arena_bytes: int) -> Hello:
    """Verify + parse the worker's first frame. proto must match exactly
    (else refuse, loud, N7) and arena_bytes must equal what the broker
    allocated -- the broker never trusts hello claims for enforcement."""
    if not isinstance(msg, dict) or msg.get("hello") != 1:
        raise ProtocolViolation(f"expected hello handshake, got {msg!r}")
    proto = msg.get("proto")
    if proto != PROTO:
        raise ProtocolViolation(
            f"proto mismatch: worker reports {proto!r}, broker requires exactly {PROTO}")
    arena_bytes = msg.get("arena_bytes")
    if arena_bytes != expected_arena_bytes:
        raise ProtocolViolation(
            f"arena_bytes mismatch: worker reports {arena_bytes!r}, "
            f"broker allocated {expected_arena_bytes}")
    bundle = msg.get("bundle")
    if not isinstance(bundle, dict):
        raise ProtocolViolation(f"missing/invalid bundle in hello: {bundle!r}")
    components = bundle.get("components", {})
    if not isinstance(components, dict):
        # dict() coercion of a hostile non-dict either raises bare
        # ValueError (e.g. ["bad"]) or silently fabricates a mapping
        # (e.g. ["ab"] -> {"a": "b"}); require a JSON object outright.
        raise ProtocolViolation(
            f"malformed bundle components in hello: {components!r}")
    try:
        bundle_info = BundleInfo(
            id=bundle["id"], version=bundle["version"],
            components=dict(components))
    except (KeyError, TypeError, ValueError) as e:
        raise ProtocolViolation(f"malformed bundle in hello: {e}") from e
    ops = msg.get("ops")
    if not isinstance(ops, list) or not all(isinstance(o, str) for o in ops):
        raise ProtocolViolation(f"malformed ops in hello: {ops!r}")
    max_pixels = msg.get("max_pixels")
    if not isinstance(max_pixels, int) or max_pixels <= 0:
        raise ProtocolViolation(f"malformed max_pixels in hello: {max_pixels!r}")
    return Hello(proto=proto, bundle=bundle_info, ops=tuple(ops),
                 arena_bytes=arena_bytes, max_pixels=max_pixels)


def parse_locked(msg: dict) -> bool:
    """Verify the phase-2 lockdown report; returns the worker's
    self-reported IsAppContainer bool (design doc sec 4: 'locked' mostly
    asserts state on Windows, since the parent applies authority
    stripping at spawn)."""
    if not isinstance(msg, dict) or msg.get("locked") is not True:
        raise ProtocolViolation(f"expected lockdown confirmation, got {msg!r}")
    is_appc = msg.get("is_appcontainer")
    if not isinstance(is_appc, bool):
        raise ProtocolViolation(f"locked message missing is_appcontainer bool: {msg!r}")
    return is_appc


# ---------------------------------------------------------------------------
# In-file metadata parsing (design doc sec 2.4 checklist item 5,
# fauxcasa-i92.3.3), landed ahead of the index/poster ops that will carry
# MetaFields in a real response. Pure dict validator -- cross-platform, no
# live worker needed, same shape as parse_hello/parse_locked above.

# FIX 1 (P1, reviewer fix pass (fauxcasa-i92.3.x)): a broker-chosen
# per-item byte bound for taken/keywords/face names. Design doc sec 2.4
# checklist item 5 only names the caption byte cap (MAX_CAPTION_BYTES) and
# the keyword/face COUNT caps (MAX_KEYWORDS/MAX_FACES); it says nothing
# about a per-item byte size for the smaller string fields. Rather than
# lean on MAX_CONTROL_MSG incidentally bounding them (true today, but an
# accident of the control-frame cap, not a designed clamp for this layer),
# this keeps the clamp layer self-contained with its own explicit bound.
MAX_META_ITEM_BYTES = 1024


def _checked_str(value: object, field: str, *, max_bytes: int | None = None) -> str:
    """FIX 1 (P1, reviewer fix pass (fauxcasa-i92.3.x)): closes an
    exception surface in parse_meta_fields -- a lone UTF-16 surrogate
    (e.g. JSON "\\ud800bad", legal JSON, illegal Unicode) passes the
    isinstance(str) guard but makes .encode("utf-8") raise
    UnicodeEncodeError, which used to escape parse_meta_fields uncaught,
    defeating the ProtocolViolation counter/kill contract for that call
    entirely. Same clamp-vs-reject split as the rest of this module: not a
    string, or not UTF-8-encodable, is a TYPE claim (reject); too many
    bytes is a SIZE claim (clamp -- truncate the ENCODED bytes first, then
    decode with errors='ignore', same technique as the caption clamp, so a
    codepoint split at the boundary is dropped whole, never mojibake)."""
    if not isinstance(value, str):
        raise ProtocolViolation(f"{field} not a string: {value!r}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as e:
        raise ProtocolViolation(f"{field} not UTF-8-encodable: {e}") from e
    if max_bytes is not None and len(encoded) > max_bytes:
        value = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return value


def _checked_finite(value: object, field: str) -> float:
    """Codex review (feat/i92.3-pool-hardening): OverflowError companion to
    _checked_str's surrogate fix above, for the two numeric-shaped meta
    fields (gps coordinates, face x/y/w/h). A JSON-valid huge integer
    literal (e.g. 10**400) passes the isinstance(v, (int, float)) check,
    then float(value) -- or math.isfinite(value) directly -- raises
    OverflowError ("int too large to convert to float"), which used to
    escape parse_meta_fields uncaught: an unexpected exception, not a
    ProtocolViolation, defeating the counter/kill contract exactly like
    the surrogate-string bug this module's FIX 1 closed. Same clamp-vs-
    reject split as _checked_str: not a real number (a bool included) is a
    TYPE claim (reject); a magnitude too large to represent as a float, or
    a non-finite result, is a FINITENESS claim (reject -- there is no sane
    clamp for an unbounded integer literal). Callers apply any additional
    field-specific range check to the returned float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolViolation(f"{field} is not a real number: {value!r}")
    try:
        result = float(value)
    except OverflowError as e:
        raise ProtocolViolation(f"{field} magnitude too large: {e}") from e
    if not math.isfinite(result):
        raise ProtocolViolation(f"{field} is not finite: {value!r}")
    return result


def parse_meta_fields(meta: object, *,
                       max_caption_bytes: int = MAX_CAPTION_BYTES,
                       max_keywords: int = MAX_KEYWORDS,
                       max_faces: int = MAX_FACES) -> MetaFields:
    """The clamp-vs-reject rule: a claim about SIZE (caption bytes, keyword/
    face count) is CLAMPED, because a worker can legitimately produce too
    much of a real thing, but a claim about TYPE, SHAPE, or FINITENESS is
    REJECTED as a ProtocolViolation, because that can only be a lie --
    unknown top-level keys (and unknown keys inside a face dict) are
    ignored for additive evolution. This is the seam the future index/
    poster response validation MUST route through, exactly as
    `_validate_response_checks` is for PixelBuffer today."""
    if meta is None:
        return MetaFields()
    if not isinstance(meta, dict):
        raise ProtocolViolation(f"meta is not a JSON object: {type(meta).__name__}")

    # FIX 4 (P2, reviewer fix pass (fauxcasa-i92.3.x)): MetaFields is an
    # ALL-optional contract (see the dataclass docstring) -- our own i92.4
    # exiv2 wrapper will naturally emit an explicit JSON null for a field
    # it found no value for, not merely omit the key. Before this, only
    # `gps` treated null the same as "field omitted"; every other field
    # raised ProtocolViolation on an explicit null, which would make our
    # own honest worker trip the compromise counter on ordinary,
    # unremarkable files. Normalize null -> omitted for every optional
    # field, once, up front: a false positive on that counter is worse
    # than a clamp we could have applied to an omitted default instead.
    meta = {k: v for k, v in meta.items()
            if not (v is None and k in ("caption", "keywords", "taken",
                                          "gps", "rating", "faces"))}

    caption = _checked_str(meta.get("caption", ""), "meta.caption",
                            max_bytes=max_caption_bytes)

    keywords_raw = meta.get("keywords", [])
    if not isinstance(keywords_raw, list):
        raise ProtocolViolation(f"meta.keywords is not a list of strings: {keywords_raw!r}")
    # Validate/clamp EVERY keyword's type+bytes before the count clamp
    # below -- same "validate everything, even entries a count clamp will
    # drop" principle the faces loop documents further down.
    checked_keywords = [
        _checked_str(k, "meta.keyword", max_bytes=MAX_META_ITEM_BYTES)
        for k in keywords_raw
    ]
    keywords = tuple(checked_keywords[:max_keywords])

    taken = _checked_str(meta.get("taken", ""), "meta.taken",
                          max_bytes=MAX_META_ITEM_BYTES)

    gps_raw = meta.get("gps")
    gps: tuple[float, float] | None = None
    if gps_raw is not None:
        if not isinstance(gps_raw, (list, tuple)) or len(gps_raw) != 2:
            raise ProtocolViolation(f"meta.gps is not a 2-element list/tuple: {gps_raw!r}")
        lat_raw, lon_raw = gps_raw
        # Codex review (feat/i92.3-pool-hardening): routes through
        # _checked_finite so a huge integer literal (10**400) is a
        # ProtocolViolation, not an uncaught OverflowError -- see that
        # helper's docstring.
        lat = _checked_finite(lat_raw, "meta.gps element")
        lon = _checked_finite(lon_raw, "meta.gps element")
        gps = (lat, lon)

    rating_raw = meta.get("rating", 0)
    if not isinstance(rating_raw, int) or isinstance(rating_raw, bool):
        raise ProtocolViolation(f"meta.rating is not an int: {rating_raw!r}")
    rating = max(0, min(5, rating_raw))

    faces_raw = meta.get("faces", [])
    if not isinstance(faces_raw, list):
        raise ProtocolViolation(f"meta.faces is not a list: {faces_raw!r}")
    # Validate EVERY entry (even ones a count clamp below will drop) --
    # geometry claims are shape claims, same severity as PixelBuffer dims
    # (design doc sec 2.4 item 4/6); clamping a lie away rather than
    # rejecting it would silently let a hostile worker hide bad data past
    # the visible cutoff.
    faces: list[FaceRegion] = []
    for f in faces_raw:
        if not isinstance(f, dict):
            raise ProtocolViolation(f"meta.faces entry is not an object: {f!r}")
        # FIX 1: routes through the same surrogate-safe check as caption/
        # taken/keywords, clamped to MAX_META_ITEM_BYTES.
        # An unnamed face region (name omitted or explicit JSON null) is
        # the honest common case -- detected-but-unidentified faces in
        # Picasa's own model and MWG/exiv2 region records carry geometry
        # with no name, and design sec 2.4 item 5 imposes no name
        # requirement. FIX 4's null->omitted normalization above only
        # covers the six top-level keys, so mirror it here: None -> ""
        # exactly, keeping any non-None non-str (0, [], ...) a type lie.
        name_raw = f.get("name")
        name = ("" if name_raw is None else
                _checked_str(name_raw, "face.name", max_bytes=MAX_META_ITEM_BYTES))
        coords: dict[str, float] = {}
        for key in ("x", "y", "w", "h"):
            v = f.get(key)
            # Codex review (feat/i92.3-pool-hardening): routes through
            # _checked_finite so a huge integer literal (10**400) is a
            # ProtocolViolation, not an uncaught OverflowError -- see that
            # helper's docstring. The [0,1] range check applies to the
            # already-finite float it returns.
            fv = _checked_finite(v, f"face.{key}")
            if not (0.0 <= fv <= 1.0):
                raise ProtocolViolation(f"face.{key} out of range [0,1]: {v!r}")
            coords[key] = fv
        faces.append(FaceRegion(name=name, x=coords["x"], y=coords["y"],
                                 w=coords["w"], h=coords["h"]))
    faces_clamped = tuple(faces[:max_faces])

    return MetaFields(caption=caption, keywords=keywords, taken=taken,
                       gps=gps, rating=rating, faces=faces_clamped)


# ---------------------------------------------------------------------------
# Windows-only machinery (ctypes structs, prototypes, AppContainer/job/ACL
# helpers). Guarded so this module stays importable on any platform --
# the trusted-side validation/framing code above needs no Windows API.

if sys.platform == "win32":
    from ctypes import wintypes

    LPVOID = ctypes.c_void_p
    SIZE_T = ctypes.c_size_t
    ULONG_PTR = ctypes.c_size_t

    CREATE_SUSPENDED = 0x00000004
    DETACHED_PROCESS = 0x00000008
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    STARTF_USESTDHANDLES = 0x00000100
    HANDLE_FLAG_INHERIT = 0x00000001

    PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
    PROC_THREAD_ATTRIBUTE_MITIGATION_POLICY = 0x00020007
    PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
    PROC_THREAD_ATTRIBUTE_CHILD_PROCESS_POLICY = 0x0002000E
    PROCESS_CREATION_CHILD_PROCESS_RESTRICTED = 0x01

    MITIGATION_DEP_ENABLE = 0x01
    MITIGATION_FORCE_RELOCATE_ALWAYS_ON = 0x01 << 8
    MITIGATION_HEAP_TERMINATE_ALWAYS_ON = 0x01 << 12
    MITIGATION_FULL = (
        MITIGATION_DEP_ENABLE
        | MITIGATION_FORCE_RELOCATE_ALWAYS_ON
        | MITIGATION_HEAP_TERMINATE_ALWAYS_ON
    )

    JobObjectBasicUIRestrictions = 4
    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_UILIMIT_ALL = 0x000000FF

    SE_FILE_OBJECT = 1
    DACL_SECURITY_INFORMATION = 0x00000004
    GENERIC_READ = 0x80000000
    GENERIC_EXECUTE = 0x20000000
    GENERIC_ALL = 0x10000000
    GRANT_ACCESS = 1
    REVOKE_ACCESS = 4
    NO_MULTIPLE_TRUSTEE = 0
    TRUSTEE_IS_SID = 0
    TRUSTEE_IS_GROUP = 2
    OBJECT_INHERIT_ACE = 0x1
    CONTAINER_INHERIT_ACE = 0x2
    SUB_CONTAINERS_AND_OBJECTS_INHERIT = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
    NO_INHERITANCE = 0x0
    SECURITY_DESCRIPTOR_REVISION = 1
    ERROR_INSUFFICIENT_BUFFER = 122

    ERROR_ALREADY_EXISTS = 183
    HRESULT_ALREADY_EXISTS = 0x800700B7

    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    DUPLICATE_SAME_ACCESS = 0x00000002

    TOKEN_QUERY = 0x0008
    TokenIsAppContainer = 29

    PAGE_READWRITE = 0x04
    FILE_MAP_ALL_ACCESS = 0x000F001F
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1)

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", LPVOID),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", LPVOID)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class SECURITY_CAPABILITIES(ctypes.Structure):
        _fields_ = [
            ("AppContainerSid", LPVOID),
            ("Capabilities", LPVOID),
            ("CapabilityCount", wintypes.DWORD),
            ("Reserved", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", SIZE_T),
            ("MaximumWorkingSetSize", SIZE_T),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ULONG_PTR),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", SIZE_T),
            ("JobMemoryLimit", SIZE_T),
            ("PeakProcessMemoryUsed", SIZE_T),
            ("PeakJobMemoryUsed", SIZE_T),
        ]

    class JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
        _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]

    class TRUSTEE_W(ctypes.Structure):
        _fields_ = [
            ("pMultipleTrustee", LPVOID),
            ("MultipleTrusteeOperation", ctypes.c_int),
            ("TrusteeForm", ctypes.c_int),
            ("TrusteeType", ctypes.c_int),
            ("ptstrName", LPVOID),
        ]

    class EXPLICIT_ACCESS_W(ctypes.Structure):
        _fields_ = [
            ("grfAccessPermissions", wintypes.DWORD),
            ("grfAccessMode", ctypes.c_int),
            ("grfInheritance", wintypes.DWORD),
            ("Trustee", TRUSTEE_W),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD]
    kernel32.CreatePipe.restype = wintypes.BOOL

    kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel32.SetHandleInformation.restype = wintypes.BOOL

    kernel32.InitializeProcThreadAttributeList.argtypes = [
        LPVOID, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SIZE_T)]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL

    kernel32.UpdateProcThreadAttribute.argtypes = [
        LPVOID, wintypes.DWORD, ctypes.c_size_t, LPVOID, SIZE_T, LPVOID, ctypes.POINTER(SIZE_T)]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL

    kernel32.DeleteProcThreadAttributeList.argtypes = [LPVOID]
    kernel32.DeleteProcThreadAttributeList.restype = None

    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(SECURITY_ATTRIBUTES),
        ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.BOOL, wintypes.DWORD,
        LPVOID, wintypes.LPCWSTR, LPVOID, ctypes.POINTER(PROCESS_INFORMATION)]
    kernel32.CreateProcessW.restype = wintypes.BOOL

    kernel32.CreateJobObjectW.argtypes = [ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE

    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, LPVOID, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL

    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD

    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL

    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD

    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    kernel32.DuplicateHandle.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.DuplicateHandle.restype = wintypes.BOOL

    kernel32.CreateFileMappingW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR]
    kernel32.CreateFileMappingW.restype = wintypes.HANDLE

    kernel32.MapViewOfFile.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
    kernel32.MapViewOfFile.restype = ctypes.c_void_p

    kernel32.UnmapViewOfFile.argtypes = [LPVOID]
    kernel32.UnmapViewOfFile.restype = wintypes.BOOL

    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES),
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE

    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(LPVOID), ctypes.POINTER(LPVOID),
        ctypes.POINTER(LPVOID), ctypes.POINTER(LPVOID), ctypes.POINTER(LPVOID)]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD

    advapi32.SetEntriesInAclW.argtypes = [
        wintypes.ULONG, ctypes.POINTER(EXPLICIT_ACCESS_W), LPVOID, ctypes.POINTER(LPVOID)]
    advapi32.SetEntriesInAclW.restype = wintypes.DWORD

    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, LPVOID, LPVOID, LPVOID, LPVOID]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD

    advapi32.ConvertSidToStringSidW.argtypes = [LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    advapi32.FreeSid.argtypes = [LPVOID]
    advapi32.FreeSid.restype = LPVOID

    kernel32.LocalFree.argtypes = [LPVOID]
    kernel32.LocalFree.restype = LPVOID

    userenv.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, LPVOID, wintypes.DWORD,
        ctypes.POINTER(LPVOID)]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long

    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(LPVOID)]
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long

    user32.GetProcessWindowStation.restype = wintypes.HANDLE
    user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user32.GetThreadDesktop.restype = wintypes.HANDLE
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    user32.GetUserObjectSecurity.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    user32.GetUserObjectSecurity.restype = wintypes.BOOL

    user32.SetUserObjectSecurity.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), LPVOID]
    user32.SetUserObjectSecurity.restype = wintypes.BOOL

    advapi32.GetSecurityDescriptorDacl.argtypes = [
        LPVOID, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(LPVOID), ctypes.POINTER(wintypes.BOOL)]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL

    advapi32.InitializeSecurityDescriptor.argtypes = [LPVOID, wintypes.DWORD]
    advapi32.InitializeSecurityDescriptor.restype = wintypes.BOOL

    advapi32.SetSecurityDescriptorDacl.argtypes = [LPVOID, wintypes.BOOL, LPVOID, wintypes.BOOL]
    advapi32.SetSecurityDescriptorDacl.restype = wintypes.BOOL

    # -- AppContainer profile -------------------------------------------

    def create_or_derive_profile(name: str) -> LPVOID:
        """Idempotent: create the profile, else derive its SID (gotcha 5:
        ERROR_ALREADY_EXISTS is HRESULT 0x800700B7)."""
        sid = LPVOID()
        hr = userenv.CreateAppContainerProfile(
            name, name, "fauxcasa decode worker sandbox", None, 0, ctypes.byref(sid))
        hr &= 0xFFFFFFFF
        if hr == 0:
            return sid
        if hr == HRESULT_ALREADY_EXISTS:
            hr2 = userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
            hr2 &= 0xFFFFFFFF
            if hr2 != 0:
                raise OSError(f"DeriveAppContainerSid failed HRESULT=0x{hr2:08X}")
            return sid
        raise OSError(f"CreateAppContainerProfile failed HRESULT=0x{hr:08X}")

    # fauxcasa-ez2.9 Stage 1 rework (P1 finding "Profile/SID race"):
    # CreateAppContainerProfile/DeriveAppContainerSidFromAppContainerName
    # are NOT safe to call concurrently -- DecodePoolSet.warm() spawns all
    # N members in parallel threads (design), and every one of them used
    # to call create_or_derive_profile() itself, reliably racing the
    # userenv profile-store APIs (observed: 0x80070020 SHARING_VIOLATION,
    # 0x80070005 ACCESS_DENIED, 0x800703FA ERROR_KEY_DELETED, and a
    # "successful" warm that silently spawned fewer members than
    # requested). Resolve the SID for a given profile name EXACTLY ONCE
    # per process -- a module-level cache guarded by a lock -- and hand
    # every spawn() the same cached SID object; the lock is only ever
    # held around the (cheap, once cached) profile API call, never around
    # spawn() itself.
    _profile_sid_cache: dict[str, LPVOID] = {}
    _profile_sid_lock = threading.Lock()

    def get_cached_profile_sid(name: str) -> LPVOID:
        """Resolve `name`'s AppContainer SID once per process and cache
        it; every caller reuses the same SID rather than re-entering the
        racy userenv profile APIs."""
        sid = _profile_sid_cache.get(name)
        if sid is not None:
            return sid
        with _profile_sid_lock:
            sid = _profile_sid_cache.get(name)
            if sid is not None:
                return sid
            sid = create_or_derive_profile(name)
            _profile_sid_cache[name] = sid
            return sid

    def sid_to_string(sid: LPVOID) -> str:
        ptr = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(ptr)):
            return "<unknown>"
        s = ptr.value
        kernel32.LocalFree(ptr)
        return s

    # -- ACL grant / revoke ----------------------------------------------

    def _set_file_dacl(path: str, sid: LPVOID, mode: int,
                        inherit: int = SUB_CONTAINERS_AND_OBJECTS_INHERIT) -> None:
        p_owner = LPVOID()
        p_group = LPVOID()
        p_dacl = LPVOID()
        p_sacl = LPVOID()
        p_sd = LPVOID()
        err = advapi32.GetNamedSecurityInfoW(
            path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
            ctypes.byref(p_owner), ctypes.byref(p_group),
            ctypes.byref(p_dacl), ctypes.byref(p_sacl), ctypes.byref(p_sd))
        if err != 0:
            raise OSError(f"GetNamedSecurityInfoW({path}) err={err}")
        try:
            ea = EXPLICIT_ACCESS_W()
            ea.grfAccessPermissions = GENERIC_READ | GENERIC_EXECUTE
            ea.grfAccessMode = mode
            # P2 finding "frozen grant scope": callers now pass
            # NO_INHERITANCE for a single FILE grant (the frozen exe
            # itself) -- only a directory-recursive grant (sys._MEIPASS)
            # uses the inheritable default.
            ea.grfInheritance = inherit
            ea.Trustee.pMultipleTrustee = None
            ea.Trustee.MultipleTrusteeOperation = NO_MULTIPLE_TRUSTEE
            ea.Trustee.TrusteeForm = TRUSTEE_IS_SID
            ea.Trustee.TrusteeType = TRUSTEE_IS_GROUP
            ea.Trustee.ptstrName = sid
            new_dacl = LPVOID()
            err = advapi32.SetEntriesInAclW(1, ctypes.byref(ea), p_dacl, ctypes.byref(new_dacl))
            if err != 0:
                raise OSError(f"SetEntriesInAclW({path}) err={err}")
            try:
                err = advapi32.SetNamedSecurityInfoW(
                    path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None, new_dacl, None)
                if err != 0:
                    raise OSError(f"SetNamedSecurityInfoW({path}) err={err}")
            finally:
                if new_dacl:
                    kernel32.LocalFree(new_dacl)
        finally:
            if p_sd:
                kernel32.LocalFree(p_sd)

    def _grant_user_object(handle, sid: LPVOID, inherit: int) -> None:
        si = wintypes.DWORD(DACL_SECURITY_INFORMATION)
        needed = wintypes.DWORD(0)
        ok = user32.GetUserObjectSecurity(handle, ctypes.byref(si), None, 0, ctypes.byref(needed))
        err = ctypes.get_last_error()
        if ok or err != ERROR_INSUFFICIENT_BUFFER:
            raise OSError(f"GetUserObjectSecurity(size) ok={ok} err={err}")
        buf = (ctypes.c_byte * needed.value)()
        if not user32.GetUserObjectSecurity(handle, ctypes.byref(si), buf, needed.value,
                                             ctypes.byref(needed)):
            raise OSError(f"GetUserObjectSecurity err={ctypes.get_last_error()}")

        present = wintypes.BOOL(0)
        defaulted = wintypes.BOOL(0)
        old_dacl = LPVOID()
        if not advapi32.GetSecurityDescriptorDacl(
                ctypes.cast(buf, LPVOID), ctypes.byref(present),
                ctypes.byref(old_dacl), ctypes.byref(defaulted)):
            raise OSError(f"GetSecurityDescriptorDacl err={ctypes.get_last_error()}")

        ea = EXPLICIT_ACCESS_W()
        ea.grfAccessPermissions = GENERIC_ALL
        ea.grfAccessMode = GRANT_ACCESS
        ea.grfInheritance = inherit
        ea.Trustee.pMultipleTrustee = None
        ea.Trustee.MultipleTrusteeOperation = NO_MULTIPLE_TRUSTEE
        ea.Trustee.TrusteeForm = TRUSTEE_IS_SID
        ea.Trustee.TrusteeType = TRUSTEE_IS_GROUP
        ea.Trustee.ptstrName = sid
        new_dacl = LPVOID()
        rc = advapi32.SetEntriesInAclW(
            1, ctypes.byref(ea), old_dacl if present.value else None, ctypes.byref(new_dacl))
        if rc != 0:
            raise OSError(f"SetEntriesInAclW(userobj) err={rc}")
        try:
            new_sd = (ctypes.c_byte * 64)()
            new_sd_p = ctypes.cast(new_sd, LPVOID)
            if not advapi32.InitializeSecurityDescriptor(new_sd_p, SECURITY_DESCRIPTOR_REVISION):
                raise OSError(f"InitializeSD err={ctypes.get_last_error()}")
            if not advapi32.SetSecurityDescriptorDacl(new_sd_p, True, new_dacl, False):
                raise OSError(f"SetSDDacl err={ctypes.get_last_error()}")
            si2 = wintypes.DWORD(DACL_SECURITY_INFORMATION)
            if not user32.SetUserObjectSecurity(handle, ctypes.byref(si2), new_sd_p):
                raise OSError(f"SetUserObjectSecurity err={ctypes.get_last_error()}")
        finally:
            if new_dacl:
                kernel32.LocalFree(new_dacl)

    def grant_winsta_desktop(sid: LPVOID) -> dict:
        """Grant the AppContainer SID access to the broker's window
        station and desktop (gotcha 3 -- mandatory, else the child dies
        0xC0000142 at user32 init)."""
        out = {}
        winsta = user32.GetProcessWindowStation()
        desktop = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        try:
            _grant_user_object(winsta, sid, CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE)
            out["window_station"] = "granted"
        except OSError as e:
            out["window_station"] = str(e)
        try:
            _grant_user_object(desktop, sid, NO_INHERITANCE)
            out["desktop"] = "granted"
        except OSError as e:
            out["desktop"] = str(e)
        return out

    def grant_read_execute(path: str, sid: LPVOID,
                            inherit: int = SUB_CONTAINERS_AND_OBJECTS_INHERIT) -> None:
        _set_file_dacl(path, sid, GRANT_ACCESS, inherit=inherit)

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    def _guid_from_str(guid_str: str) -> "_GUID":
        b = uuid.UUID(guid_str).bytes  # big-endian; matches GUID's wire layout
        g = _GUID()
        g.Data1 = int.from_bytes(b[0:4], "big")
        g.Data2 = int.from_bytes(b[4:6], "big")
        g.Data3 = int.from_bytes(b[6:8], "big")
        g.Data4 = (ctypes.c_ubyte * 8)(*b[8:16])
        return g

    # Re-review residual (item 4): FOLDERID_* GUIDs (shlobj_core.h) for the
    # known folders is_known_user_folder() must refuse a recursive grant
    # on. SHGetKnownFolderPath (unlike a %USERPROFILE%\\<name> guess)
    # correctly resolves OneDrive-redirected and localized folder names.
    KNOWN_FOLDER_GUIDS: dict[str, str] = {
        "Profile": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
        "Desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
        "Documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
        "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
        "Pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    }
    # %USERPROFILE%\\<suffix> fallback used only when the API call for that
    # specific folder fails; "" means the profile root itself.
    _KNOWN_FOLDER_FALLBACK_SUFFIX: dict[str, str] = {
        "Profile": "", "Desktop": "Desktop", "Documents": "Documents",
        "Downloads": "Downloads", "Pictures": "Pictures",
    }

    _shell32 = ctypes.windll.shell32
    _ole32 = ctypes.windll.ole32
    _shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p)]
    _shell32.SHGetKnownFolderPath.restype = ctypes.c_long  # HRESULT
    _ole32.CoTaskMemFree.argtypes = [LPVOID]
    _ole32.CoTaskMemFree.restype = None

    def _sh_get_known_folder_path(name: str) -> str | None:
        """SHGetKnownFolderPath for KNOWN_FOLDER_GUIDS[name]. Returns None
        on any failure (unsupported OS, restricted account, ...) -- a thin
        seam so tests can monkeypatch per-folder results (e.g. a
        OneDrive-redirected Desktop) without touching the real registry/
        shell state."""
        guid = _guid_from_str(KNOWN_FOLDER_GUIDS[name])
        out = ctypes.c_wchar_p()
        try:
            hr = _shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
        except OSError:
            return None
        if hr != 0 or not out.value:
            return None
        path = out.value
        try:
            _ole32.CoTaskMemFree(out)
        except Exception:
            pass
        return path

    def _known_user_folder_targets() -> list[str]:
        """Normalized (case-folded) realpaths of the user's known folders
        -- SHGetKnownFolderPath per folder, falling back to the
        %USERPROFILE%\\<name> heuristic ONLY for a folder whose API call
        failed."""
        home = os.environ.get("USERPROFILE")
        out: list[str] = []
        for name in KNOWN_FOLDER_GUIDS:
            path = _sh_get_known_folder_path(name)
            if not path and home:
                suffix = _KNOWN_FOLDER_FALLBACK_SUFFIX[name]
                path = os.path.join(home, suffix) if suffix else home
            if not path:
                continue
            try:
                out.append(os.path.normcase(os.path.realpath(path)))
            except OSError:
                pass
        return out

    def is_known_user_folder(path: str) -> bool:
        """True when `path` resolves to the user's profile root or one of
        its Desktop/Downloads/Documents/Pictures folders -- including
        OneDrive-redirected and localized names, via SHGetKnownFolderPath
        (P2 finding "frozen grant scope"; re-review residual item 4):
        granting a recursive, inheritable RX ACL there would defeat the
        "user files are denied" property the threat model relies on -- a
        portable exe dropped in Downloads (or the profile root itself)
        must never widen the AppContainer's read to the rest of the
        user's files. Compared case-insensitively on normalized real
        paths so a differently-cased or symlinked path still matches."""
        try:
            target = os.path.normcase(os.path.realpath(path))
        except OSError:
            return False
        return target in _known_user_folder_targets()

    def revoke(path: str, sid: LPVOID) -> None:
        try:
            _set_file_dacl(path, sid, REVOKE_ACCESS)
        except OSError:
            pass

    # fauxcasa-ez2.9 (Stage 1, P1 finding "spawn ~720ms": grant_read_execute
    # on base_dir + site-packages measured 264ms + 119ms EVERY spawn) and
    # (P1 finding "ACL grant failure is fatal even where access already
    # exists"): grant_read_execute_once() makes the grant idempotent per
    # (container SID, directory) two ways -- an in-process set (fast path
    # for repeated spawns in one session) and an on-disk marker file (fast
    # path across broker restarts within the same profile) -- and makes a
    # failed grant BEST-EFFORT: the hello handshake (a worker that hellos
    # can read its own runtime) is the actual readability proof, not the
    # SetNamedSecurityInfoW return code, so a failure here is logged at
    # info and swallowed rather than raised. Marker writes are themselves
    # best-effort (a read-only target dir just means paying the ACL cost
    # again next spawn, not a functional failure).
    _acl_granted_this_process: set[tuple[str, str]] = set()
    # P2 finding "grant_read_execute_once is not thread-safe": guards
    # BOTH _acl_granted_this_process and the marker-file check+write below
    # -- warm() spawns all N pool members in parallel threads, and without
    # this lock every one of them misses the in-process cache and issues
    # its own concurrent SetNamedSecurityInfoW on the same directory (the
    # optimisation this set exists for silently doesn't apply in the one
    # case it was built for).
    _acl_grant_lock = threading.Lock()
    # fauxcasa-yfq: paths whose grant failure has already been logged at
    # WARNING this process (every later failure for the same path is INFO
    # -- the pool re-tries the grant on each spawn, ~9 per warm()).
    _acl_grant_warned: set[str] = set()
    # ... and (sid, path) grants that failed this process, with the error
    # detail, so the pool's N spawns per warm() pay the OS call once.
    _acl_grant_failed_this_process: dict[tuple[str, str], str] = {}
    # ... and worker scripts whose staging has been announced (same idea).
    _staging_logged: set[str] = set()

    ACL_MARKER_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 days

    def fauxcasa_cache_root() -> Path:
        """%LOCALAPPDATA%\\Fauxcasa\\cache (falling back to TEMP, then the
        home dir, when LOCALAPPDATA is unset) -- the same root catalog.py/
        db3rescue.py use. Shared by the ACL-marker store and the staged
        worker-script copy (fauxcasa-yfq): both must live on a volume
        where the user can set ACLs, which the profile drive is.

        The TEMP / home fallbacks exist for the marker store, where a
        misplaced file costs one repeated ACL call. Staging puts EXECUTED
        code under this root, so stage_worker_script() refuses to run
        unless LOCALAPPDATA itself resolved (cache_root_is_localappdata)
        -- a world-writable TEMP is not a place to launch the worker
        from (review round 1, item 12)."""
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or str(Path.home())
        return Path(base) / "Fauxcasa" / "cache"

    # -- Worker-script staging (fauxcasa-yfq) ---------------------------------
    #
    # A source checkout on a volume that refuses the per-SID ACL grant
    # (observed: an A: ReFS drive whose root ACL is Authenticated Users:
    # Modify with no WRITE_DAC -- SetNamedSecurityInfoW and icacls both
    # fail err=5 even on a fresh directory the user owns) leaves the
    # AppContainer unable to open decodesvc_worker_win.py at all: the
    # interpreter prints "can't open file ...: [Errno 13] Permission
    # denied" and exits 2 before hello, and the sandbox degrades to
    # in-process on every run. The frozen bundle never hits this because
    # its payload sits under Program Files or LOCALAPPDATA, both grantable
    # (or already readable by ALL APPLICATION PACKAGES). Staging mirrors
    # that: copy the worker script into the cache root -- which IS
    # grantable, the ACL markers already live there -- and launch from
    # the copy.
    #
    # Only the one file is copied. decodesvc_worker_win.py is deliberately
    # self-contained (its module docstring: "this file's import list IS
    # the sandboxed process's attack surface budget" -- stdlib, ctypes and
    # PySide6 only, no repo sibling imports), so there is no import
    # closure to chase. PySide6 itself comes from the worker PYTHONPATH,
    # which is granted separately; if THAT is on an ungrantable volume
    # too (a uv cache relocated onto the same drive) staging cannot help
    # -- copying a few hundred MB of Qt per spawn is not a fallback -- and
    # spawn() says so in its error instead (see describe_prehello_death).
    #
    # The copy is content-hashed: <cache>/sandbox-worker/<sha256[:16]>/
    # decodesvc_worker_win.py, re-verified byte-for-byte before reuse, so
    # an edited source is never run from a stale copy. Old hash dirs are
    # pruned once they are a day old (a concurrent broker may still be
    # launching from a younger one). The copy is launched ONLY when the
    # staging root's own grant succeeds; if that grant fails too, or the
    # copy cannot be written, spawn stays on the source path (see the
    # comment at the call site in spawn()).
    WORKER_STAGING_DIRNAME = "sandbox-worker"
    WORKER_STAGING_MAX_AGE_SECONDS = 24 * 3600

    def cache_root_is_localappdata() -> bool:
        """True when fauxcasa_cache_root() resolved from LOCALAPPDATA rather
        than the TEMP/home fallbacks -- the precondition for staging."""
        return bool(os.environ.get("LOCALAPPDATA"))

    def worker_staging_root() -> Path:
        return fauxcasa_cache_root() / WORKER_STAGING_DIRNAME

    def _prune_worker_staging(root: Path, keep: Path) -> None:
        cutoff = time.time() - WORKER_STAGING_MAX_AGE_SECONDS
        try:
            for entry in root.iterdir():
                if entry == keep or not entry.is_dir():
                    continue
                try:
                    if entry.stat().st_mtime >= cutoff:
                        continue
                    for child in entry.iterdir():
                        child.unlink(missing_ok=True)
                    entry.rmdir()
                except OSError:
                    continue
        except OSError:
            pass

    def stage_worker_script(source: str) -> str:
        """Copy `source` (the worker script) into the content-hashed
        staging dir under the cache root and return the copy's path.
        Idempotent: an existing copy with identical bytes is reused; any
        other content lands in a different hash dir. Raises OSError when
        the source is unreadable or the cache root is not writable -- the
        caller falls back to launching from the source path. Refuses
        (OSError) when the cache root did not resolve from LOCALAPPDATA:
        executed code does not go under a TEMP/home fallback."""
        if not cache_root_is_localappdata():
            raise OSError("LOCALAPPDATA is unset; refusing to stage executed code "
                          "under the TEMP/home cache-root fallback")
        src = Path(source)
        data = src.read_bytes()
        digest = hashlib.sha256(data).hexdigest()[:16]
        root = worker_staging_root()
        dest_dir = root / digest
        dest = dest_dir / src.name
        try:
            if dest.read_bytes() == data:
                # Touch on reuse so the day-old prune means "unused for
                # a day", not "written a day ago".
                try:
                    os.utime(dest_dir, None)
                except OSError:
                    pass
                _prune_worker_staging(root, dest_dir)
                return str(dest)
        except OSError:
            pass
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a concurrent broker never launches a
        # half-written copy; os.replace is atomic within one volume.
        tmp = dest_dir / f".{src.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_bytes(data)
        try:
            os.replace(tmp, dest)
        except OSError:
            tmp.unlink(missing_ok=True)
            # Lost a race with another broker staging the same content;
            # the winner's copy is byte-identical by construction.
            if dest.read_bytes() != data:
                raise
        _prune_worker_staging(root, dest_dir)
        return str(dest)

    _acl_marker_root_pruned = False

    def _prune_acl_markers(root: Path, sid_str: str) -> None:
        """Re-review residual (item 5): drop markers older than 30 days,
        or not matching the CURRENT profile SID -- the profile SID
        changes across a profile re-creation, and stale markers
        (particularly from an old scheme, e.g. one that embedded a
        directory mtime, see _acl_marker_path below) would otherwise
        accumulate forever, one-per-mtime-change, since dropping mtime
        from the key means the self-heal invalidation is now the ONLY
        thing that removes a marker mid-session. Best-effort, run once
        per process the first time the marker root is opened."""
        try:
            cutoff = time.time() - ACL_MARKER_MAX_AGE_SECONDS
            for entry in root.iterdir():
                if not entry.name.startswith("acl-"):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
                    continue
                # Markers from the OLD (sid|directory|mtime) key scheme
                # (or any marker for a different/stale SID) can't be
                # matched by content cheaply -- but they are also never
                # looked up again under the new (sid, directory)-only
                # scheme (different digest), so they are pure disk
                # litter. Prune anything not freshly written by an
                # in-process grant this run (best-effort: read its
                # recorded SID if present, else age it out above).
                try:
                    recorded_sid = entry.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if recorded_sid and recorded_sid != sid_str and sid_str:
                    entry.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort: a locked/unreadable marker root is not fatal

    def _acl_marker_root(sid_str: str = "") -> Path:
        """Marker storage root -- %LOCALAPPDATA%\\Fauxcasa\\cache\\acl-markers
        (P3 finding: the old per-directory marker lived inside the
        interpreter/site-packages/repo trees it was granting access to --
        user-writable AND, for the repo case, a one-file sandbox
        off-switch any same-user process could plant). Falls back to TEMP
        or the home dir when LOCALAPPDATA is unset, matching the
        catalog.py/db3rescue.py LOCALAPPDATA pattern. Prunes markers
        older than 30 days (or for a stale SID) once per process, the
        first time the root is opened (re-review residual item 5)."""
        global _acl_marker_root_pruned
        root = fauxcasa_cache_root() / "acl-markers"
        root.mkdir(parents=True, exist_ok=True)
        if not _acl_marker_root_pruned:
            _acl_marker_root_pruned = True
            _prune_acl_markers(root, sid_str)
        return root

    def _acl_marker_path(directory: str, sid_str: str) -> Path:
        # Re-review residual (item 5): keyed on (SID, NORMALIZED directory
        # path) ONLY -- dropping the directory mtime that used to be part
        # of this key. The mtime churns on every __pycache__ write inside
        # a granted directory (observed: 13 orphaned marker files after
        # one day of normal use, each holding a grant that was never
        # invalidated, just abandoned under a new digest), so it defeated
        # the marker's own purpose (skip the OS call on repeat spawns).
        # A directory whose ACL genuinely changed underneath a stale
        # marker (icacls /reset, install move/repair, profile
        # re-creation) is caught by the EXISTING self-heal path instead:
        # invalidate_acl_grant() is called on a pre-hello spawn death
        # (worker can't read its own runtime -> the grant clearly isn't
        # actually in effect), which deletes the marker and forces a
        # real re-grant on the next spawn.
        try:
            norm_dir = os.path.normcase(os.path.realpath(directory))
        except OSError:
            norm_dir = directory
        key = f"{sid_str}|{norm_dir}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return _acl_marker_root(sid_str) / f"acl-{digest}"

    def invalidate_acl_grant(path: str, sid: LPVOID) -> None:
        """Drop the in-process + on-disk cache entries for (sid, path) so
        the next grant_read_execute_once() call re-issues the real grant
        (P2 finding "self-heal for stale markers": a spawn failure at
        loader init, e.g. 0xC0000142, is exactly the symptom of a stale
        marker withholding a grant that is genuinely needed -- the prior
        behavior required manually deleting a hidden marker file)."""
        sid_str = sid_to_string(sid)
        with _acl_grant_lock:
            _acl_granted_this_process.discard((sid_str, path))
            _acl_grant_failed_this_process.pop((sid_str, path), None)
            try:
                _acl_marker_path(path, sid_str).unlink(missing_ok=True)
            except OSError:
                pass

    def grant_read_execute_once(path: str, sid: LPVOID,
                                 inherit: int = SUB_CONTAINERS_AND_OBJECTS_INHERIT) -> str | None:
        """Grant read+execute for `sid` on `path`, skipping the OS call
        entirely once already granted this process or a prior process
        (marker file present). `inherit` is NO_INHERITANCE for a single
        FILE grant target (the frozen exe itself, P2 finding) or the
        recursive default for a directory. Returns None on success/skip,
        or a string error detail on a (non-fatal) grant failure."""
        sid_str = sid_to_string(sid)
        key = (sid_str, path)
        with _acl_grant_lock:
            if key in _acl_granted_this_process:
                return None
            # fauxcasa-yfq: a grant that already failed this process fails
            # again the same way (the volume's ACL is not going to change
            # between two pool spawns), so return the remembered detail
            # instead of paying another SetNamedSecurityInfoW round-trip
            # and another log line per pool member. invalidate_acl_grant()
            # (the pre-hello-death self-heal) clears this too, so a fixed
            # ACL is retried on the next spawn after a failure.
            cached_failure = _acl_grant_failed_this_process.get(key)
            if cached_failure is not None:
                return cached_failure
            marker = _acl_marker_path(path, sid_str)
            try:
                if marker.exists():
                    _acl_granted_this_process.add(key)
                    return None
            except OSError:
                pass
            try:
                grant_read_execute(path, sid, inherit=inherit)
            except OSError as e:
                # Best-effort (P1 finding): do NOT raise -- the hello
                # handshake below is the real readability proof, and a
                # Program-Files-class install where ALL APPLICATION
                # PACKAGES already has RX must still be able to spawn.
                #
                # fauxcasa-yfq: but say so at WARNING, once per path per
                # process. At INFO this was invisible while the sandbox
                # silently degraded on every run from a ReFS/Dev Drive
                # checkout (the volume refuses the per-SID grant, err=5,
                # and unlike Program Files nothing else grants the
                # container read there).
                if path not in _acl_grant_warned:
                    _acl_grant_warned.add(path)
                    _log.warning(
                        "AppContainer read+execute ACL grant failed on %r for %s: %s "
                        "-- the sandbox worker will not be able to read this path "
                        "unless the volume already grants ALL APPLICATION PACKAGES; "
                        "a spawn that dies before hello will name this as the cause",
                        path, sid_str, e)
                else:
                    _log.info("ACL grant best-effort failure on %r for %r: %s", path, sid_str, e)
                _acl_grant_failed_this_process[key] = str(e)
                return str(e)
            _acl_granted_this_process.add(key)
            try:
                # Record the SID this marker was granted for -- read back
                # by _prune_acl_markers() to drop markers for a stale/
                # different SID (item 5).
                marker.write_text(f"{sid_str}\n", encoding="utf-8")
            except OSError:
                pass  # non-fatal: next spawn just re-grants (cheap once cached in-process)
            return None

    # -- Job object --------------------------------------------------------

    def make_job(mem_limit_bytes: int) -> wintypes.HANDLE:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_PROCESS_MEMORY)
        info.BasicLimitInformation.ActiveProcessLimit = 1
        info.ProcessMemoryLimit = mem_limit_bytes
        if not kernel32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        ui = JOBOBJECT_BASIC_UI_RESTRICTIONS()
        ui.UIRestrictionsClass = JOB_OBJECT_UILIMIT_ALL
        if not kernel32.SetInformationJobObject(
                job, JobObjectBasicUIRestrictions, ctypes.byref(ui), ctypes.sizeof(ui)):
            raise ctypes.WinError(ctypes.get_last_error())
        return job

    # -- Pipes + environment ------------------------------------------------

    def make_pipe_pair():
        """Returns (child_stdin_read, broker_stdin_write, broker_stdout_read,
        child_stdout_write). Child ends are inheritable; broker ends are not."""
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(sa)
        sa.lpSecurityDescriptor = None
        sa.bInheritHandle = True
        stdin_r = wintypes.HANDLE()
        stdin_w = wintypes.HANDLE()
        if not kernel32.CreatePipe(ctypes.byref(stdin_r), ctypes.byref(stdin_w), ctypes.byref(sa), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        stdout_r = wintypes.HANDLE()
        stdout_w = wintypes.HANDLE()
        if not kernel32.CreatePipe(ctypes.byref(stdout_r), ctypes.byref(stdout_w), ctypes.byref(sa), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        kernel32.SetHandleInformation(stdin_w, HANDLE_FLAG_INHERIT, 0)
        kernel32.SetHandleInformation(stdout_r, HANDLE_FLAG_INHERIT, 0)
        return stdin_r, stdin_w, stdout_r, stdout_w

    # FIX 3 (P1, review fix pass): the worker environment is an explicit
    # allowlist, not a copy of the broker's own os.environ -- a hijacked
    # decoder must not be able to read host tokens/keys/secrets that
    # happen to live in the broker's environment. Every name kept here is
    # empirically load-bearing -- verified by trimming to exactly this set
    # and spawning a real AppContainer worker end to end (hello -> attach_arena
    # -> locked -> decode all succeed; see the module's containment/decode
    # tests, which all run under this trimmed env). What each key is for:
    #   SystemRoot, SystemDrive, windir -- Windows DLL/loader path
    #     resolution AND (confirmed by direct testing) the AppContainer
    #     machinery itself: CreateProcessW with a SECURITY_CAPABILITIES
    #     attribute fails outright (WinError 203,
    #     ERROR_ENVVAR_NOT_FOUND) if these are missing -- the OS needs
    #     them to set up the container's virtualized profile paths.
    #   USERPROFILE, LOCALAPPDATA, APPDATA -- same AppContainer-profile-
    #     setup requirement as above (confirmed by the same WinError 203
    #     reproduction/fix); dropping any of the six above breaks spawn
    #     itself, before the worker ever runs a line of Python.
    #   PATH -- deliberately forced to the EMPTY string, not copied from
    #     the host: PySide6/__init__.py unconditionally does
    #     `os.environ['PATH']` at import time (a KeyError, not a graceful
    #     default, if PATH is absent) and then appends its own package dir
    #     to it. An empty string satisfies that without handing the worker
    #     the host's real PATH (which could name arbitrary host tool
    #     directories) -- Windows' default DLL search order already checks
    #     the application directory and System32 before consulting PATH,
    #     so this costs nothing for the worker's own DLL resolution.
    # PATHEXT and TEMP/TMP were tested and are NOT needed: the worker never
    # execs anything by extension lookup (no child processes -- design doc
    # sec 4) and never writes to a temp dir (arena + control pipe only), so
    # both are deliberately omitted (see design doc sec 4 filesystem row).
    _ENV_PASSTHROUGH_KEYS = ("SystemRoot", "SystemDrive", "windir",
                              "USERPROFILE", "LOCALAPPDATA", "APPDATA")

    def _build_env_block(pythonpath: str | None, extra_env: dict[str, str]) -> ctypes.Array:
        env: dict[str, str] = {}
        for key in _ENV_PASSTHROUGH_KEYS:
            val = os.environ.get(key)
            if val:
                env[key] = val
        env["PATH"] = ""  # see comment above -- required key, empty value
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONUTF8"] = "1"
        if pythonpath:
            env["PYTHONPATH"] = pythonpath
        # The worker's own test-only/config flags (FAUXCASA_DECODESVC_*)
        # only -- nothing else from the caller passes through.
        for k, v in extra_env.items():
            if k.startswith("FAUXCASA_DECODESVC_"):
                env[k] = v
        parts = [f"{k}={v}" for k, v in env.items()]
        block = "\x00".join(parts) + "\x00\x00"
        return ctypes.create_unicode_buffer(block)

    def _duplicate_into_child(source_handle, child_process_handle) -> int:
        target = wintypes.HANDLE()
        cur_proc = kernel32.GetCurrentProcess()
        ok = kernel32.DuplicateHandle(
            cur_proc, source_handle, child_process_handle, ctypes.byref(target),
            0, False, DUPLICATE_SAME_ACCESS)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(target.value or 0)

    # fauxcasa-ez2.9 (Stage 1, P1 finding "spawn ~720ms not 31ms"): the
    # interpreter probe is a ~110ms subprocess round-trip that resolve_
    # worker_python() previously paid on EVERY spawn(); it depends only on
    # sys.executable and FAUXCASA_WORKER_PYTHON, both fixed for the life of
    # the process, so a module-level cache keyed on the env override makes
    # every spawn after the first pay nothing for resolution. Cleared only
    # by the env changing (different key) -- there is no cross-process
    # invalidation because the resolved paths are themselves per-process
    # facts (this interpreter's own base_prefix/PySide6 site-packages).
    _resolve_cache: dict[str | None, tuple[str, str | None]] = {}

    def resolve_worker_python() -> tuple[str, str | None]:
        """Return (worker_python_exe, worker_pythonpath). Resolved by
        probing the CURRENT interpreter (must have PySide6 importable --
        declared as a PEP 723 dependency of the caller's uv env): its
        sys.base_prefix, NEVER sys.executable itself (gotcha 1 -- a uv
        venv python.exe is a trampoline that re-launches the base
        interpreter as a forbidden child process).

        FAUXCASA_WORKER_PYTHON overrides which ENVIRONMENT is probed (its
        PySide6 site-packages become the worker's PYTHONPATH), but the
        spawned exe is still always that environment's base interpreter:
        gotcha 1 applies to an override venv/uv interpreter exactly as it
        does to sys.executable (Codex review PR110 P2 -- returning the
        trampoline directly would die under the job's no-child rule
        before ever reaching the hello handshake).

        Frozen bundle branch (fauxcasa-ez2.9 Stage 1, P0 finding): a
        PyInstaller onedir's sys.executable IS the app -- there is no
        python.exe to probe with `-c`, and there never will be one in
        _internal/ (only python3xx.dll). When frozen, the worker is the
        SAME exe re-invoked as `<exe> --decode-worker` (main.py dispatches
        that flag to decodesvc_worker_win.main() before its own argparse,
        mirroring the existing videostream `--worker` re-entry pattern);
        no PYTHONPATH is needed (everything is already on the bundle's own
        import path), so this returns (sys.executable, None) with NO
        subprocess probe at all -- onefile is out of scope (its bootloader
        spawns a child, which ActiveProcessLimit=1/child-restricted block,
        docs/design/decode-service.md sec "Frozen bundle" note)."""
        if getattr(sys, "frozen", False):
            return sys.executable, None
        env_py = os.environ.get("FAUXCASA_WORKER_PYTHON")
        cached = _resolve_cache.get(env_py)
        if cached is not None:
            return cached
        probe_interp = env_py or sys.executable
        code = (
            "import sys, json, os\n"
            "d = {'base_prefix': sys.base_prefix}\n"
            "try:\n"
            "    import PySide6\n"
            "    d['site'] = os.path.dirname(os.path.dirname(PySide6.__file__))\n"
            "except Exception as e:\n"
            "    d['err'] = repr(e)\n"
            "print(json.dumps(d))\n"
        )
        out = subprocess.run([probe_interp, "-c", code], capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"worker-python probe failed (rc={out.returncode}): {out.stderr}")
        dirs = json.loads(out.stdout.strip().splitlines()[-1])
        if "site" not in dirs:
            raise RuntimeError(f"PySide6 not importable from {probe_interp}: {dirs.get('err')}")
        worker_python = os.path.join(dirs["base_prefix"], "python.exe")
        result = (worker_python, dirs["site"])
        _resolve_cache[env_py] = result
        return result

    def worker_grant_targets(worker_python: str,
                              worker_pythonpath: str | None) -> list[tuple[str, int]]:
        """Compute spawn()'s AppContainer read+execute grant targets
        (fauxcasa-ayh) for either layout `resolve_worker_python()` can
        return: frozen (a PyInstaller onedir re-invoking itself as
        `--decode-worker`) or source (a python.exe base interpreter plus
        the worker script's own dir). Directories get the recursive
        inherit flags; the frozen exe FILE itself gets NO_INHERITANCE (it
        cannot widen read to sibling files). Duplicates are collapsed,
        preserving first-seen order, and a None `worker_pythonpath` (the
        frozen case) is skipped.

        Pure computation -- callers still own the known-user-folder
        refusal (spawn()'s frozen branch) and the actual grant call
        (grant_read_execute_once), so this costs nothing extra to call
        before the first spawn (preflight_worker_grants, below)."""
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", None)
            exe_path = str(Path(worker_python).resolve())
            targets: list[tuple[str, int]] = []
            if meipass:
                targets.append((meipass, SUB_CONTAINERS_AND_OBJECTS_INHERIT))
            targets.append((exe_path, NO_INHERITANCE))
            return targets
        worker_script = str(Path(__file__).resolve().with_name("decodesvc_worker_win.py"))
        worker_dir = str(Path(worker_script).parent)
        base_dir = str(Path(worker_python).parent)
        targets = []
        for d in (base_dir, worker_pythonpath, worker_dir):
            if d and d not in [t for t, _ in targets]:
                targets.append((d, SUB_CONTAINERS_AND_OBJECTS_INHERIT))
        return targets

    def check_grant_targets_safe(targets: list[tuple[str, int]]) -> None:
        """Raise the same RuntimeError spawn() has always raised (P2
        finding "frozen grant scope") for the FIRST target in `targets`
        that would receive a RECURSIVE (SUB_CONTAINERS_AND_OBJECTS_
        INHERIT) grant and sits inside a known user folder (profile
        root/Desktop/Downloads/Documents/Pictures) -- widening the
        AppContainer's read to the rest of the user's files is never
        acceptable. A NO_INHERITANCE target (the frozen exe's own FILE)
        is exempt: it cannot widen read to sibling files. No-op when
        every target is safe.

        Shared by BOTH callers (fauxcasa-ayh review finding 1 -- the
        blocker): spawn() calls this on its frozen-branch grant_targets
        exactly where it raised before (test_spawn_refuses_frozen_grant_
        on_known_user_folder must still see `called == []`, i.e. the
        refusal fires before any grant_read_execute_once call), and
        preflight_worker_grants() (below) calls this PER TARGET so it
        can report a refusal as a failure and keep checking the rest,
        instead of the bug this closes: preflight silently issuing the
        recursive grant (and writing its on-disk marker) before spawn()
        ever got a chance to refuse."""
        for target_path, target_inherit in targets:
            if (target_inherit == SUB_CONTAINERS_AND_OBJECTS_INHERIT
                    and is_known_user_folder(target_path)):
                raise RuntimeError(
                    f"refusing to grant AppContainer read+execute: "
                    f"{target_path!r} is a known user folder (profile "
                    "root/Desktop/Downloads/Documents/Pictures) -- "
                    "move the install elsewhere and retry")

    def preflight_worker_grants(profile_name: str = PROFILE_NAME) -> list[tuple[str, str]]:
        """Resolve the worker layout and issue every AppContainer
        read+execute grant spawn() would issue -- BEFORE the first spawn
        attempt (fauxcasa-ayh), so a ReFS/Dev Drive uv cache that refuses
        the per-SID grant is reported as a warning naming the actionable
        fix rather than only surfacing later as a ModuleNotFoundError:
        PySide6 pre-hello worker death (describe_prehello_death).

        Returns a list of (path, error_detail) for targets whose grant
        failed; empty when every target granted (or was already
        cached/skippable -- an ALL APPLICATION PACKAGES-granted volume,
        e.g. Program Files, reports no failures here even though this
        function still issues the same best-effort, non-raising calls
        grant_read_execute_once always has). Grant failures are
        non-fatal here exactly as they are in spawn(); resolve_worker_
        python() errors (e.g. PySide6 not importable) DO propagate --
        that is a real misconfiguration, not a grantability question.

        fauxcasa-ayh review finding 1 (blocker): a target that spawn()
        would REFUSE to grant (check_grant_targets_safe -- a recursive
        target inside a known user folder) is reported as a failure
        here too, WITHOUT ever calling grant_read_execute_once for it --
        this must never be the thing that widens the AppContainer's
        read to the rest of the user's files just because it ran before
        spawn()."""
        worker_python, worker_pythonpath = resolve_worker_python()
        sid = get_cached_profile_sid(profile_name)
        failures: list[tuple[str, str]] = []
        for target_path, target_inherit in worker_grant_targets(worker_python, worker_pythonpath):
            try:
                check_grant_targets_safe([(target_path, target_inherit)])
            except RuntimeError:
                failures.append((target_path, "refused: known user folder"))
                continue
            err = grant_read_execute_once(target_path, sid, inherit=target_inherit)
            if err is not None:
                failures.append((target_path, err))
        return failures

    def blocking_worker_grant_failures() -> list[tuple[str, str]]:
        """preflight_worker_grants(), filtered to the failures that would
        actually BLOCK a real spawn (fauxcasa-ayh review finding 2).

        When NOT frozen, drop a failure on the worker SCRIPT's own
        directory ALONE: spawn() already recovers from that one by
        staging a content-hashed copy under the cache root
        (stage_worker_script/worker_staging_root, fauxcasa-yfq) --
        unlike the interpreter base dir or the worker PYTHONPATH, which
        cannot be staged. But (review finding 7) that drop is only safe
        when the staging root ITSELF grants: if %LOCALAPPDATA% is also
        ungrantable, staging cannot save the worker either, and the
        worker-dir failure is real and must stay reported.

        The probe mirrors spawn()'s preconditions IN ORDER (review
        finding 9): stage_worker_script() refuses (OSError) when the
        cache root did not resolve from LOCALAPPDATA, and spawn() then
        falls back to the refused source path -- so with LOCALAPPDATA
        unset the drop is never justified, however grantable the
        TEMP/home fallback directory happens to be. Checking that FIRST
        also keeps this proxy from issuing a recursive grant spawn()
        itself would never issue (spawn never grants a staging root it
        refuses to stage into).

        When frozen, nothing is dropped: sys._MEIPASS is not a separate
        "worker script dir" that spawn() can substitute a staged copy
        for -- it IS the payload directory being granted, so a failure
        on it is unconditionally blocking."""
        failures = preflight_worker_grants()
        if getattr(sys, "frozen", False) or not failures:
            return failures
        try:
            source_worker_dir = str(Path(__file__).resolve().parent)
        except OSError:
            return failures
        if not any(p == source_worker_dir for p, _ in failures):
            return failures
        if not cache_root_is_localappdata():
            # Staging is unavailable at all (stage_worker_script raises,
            # spawn falls back to the source path) -- the worker-dir
            # failure is real, and probing the TEMP/home fallback root
            # would answer a question spawn never asks.
            return failures
        sid = get_cached_profile_sid(PROFILE_NAME)
        staging_err = grant_read_execute_once(
            str(worker_staging_root()), sid, inherit=SUB_CONTAINERS_AND_OBJECTS_INHERIT)
        if staging_err is not None:
            # Staging root itself refuses the grant too -- the worker-dir
            # failure is real; keep it in the list.
            return failures
        return [(p, e) for p, e in failures if p != source_worker_dir]

    def is_appcontainer(token_handle=None) -> bool | str:
        """Query TokenIsAppContainer on the current process's token (used
        broker-side only for diagnostics; the worker does its own copy of
        this in decodesvc_worker_win.py to keep its import surface
        separate)."""
        try:
            proc = kernel32.GetCurrentProcess()
            tok = wintypes.HANDLE()
            advapi32.OpenProcessToken.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
            advapi32.OpenProcessToken.restype = wintypes.BOOL
            if not advapi32.OpenProcessToken(proc, TOKEN_QUERY, ctypes.byref(tok)):
                return f"err:OpenProcessToken {ctypes.get_last_error()}"
            advapi32.GetTokenInformation.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD)]
            advapi32.GetTokenInformation.restype = wintypes.BOOL
            val = wintypes.DWORD(0)
            rl = wintypes.DWORD(0)
            ok = advapi32.GetTokenInformation(
                tok, TokenIsAppContainer, ctypes.byref(val), 4, ctypes.byref(rl))
            kernel32.CloseHandle(tok)
            if not ok:
                return f"err:GetTokenInformation {ctypes.get_last_error()}"
            return bool(val.value)
        except Exception as e:  # pragma: no cover - diagnostic path
            return f"err:{e!r}"

    # -- Child process wrapper -----------------------------------------------

    class _ChildProcess:
        """One live spawned worker process. gotcha 5: TerminateProcess
        BEFORE closing the read pipe/handles on teardown, else a blocked
        ReadFile can hang close()."""

        __slots__ = ("pi", "job", "in_file", "out_file")

        def __init__(self, pi, job, in_file, out_file):
            self.pi = pi
            self.job = job
            self.in_file = in_file
            self.out_file = out_file

        def close(self) -> None:
            if self.pi is not None and self.pi.hProcess:
                kernel32.TerminateProcess(self.pi.hProcess, 1)
                kernel32.WaitForSingleObject(self.pi.hProcess, 2000)
                kernel32.CloseHandle(self.pi.hProcess)
                kernel32.CloseHandle(self.pi.hThread)
                self.pi = None
            if self.job:
                kernel32.CloseHandle(self.job)  # KILL_ON_JOB_CLOSE backstop
                self.job = None
            try:
                self.in_file.close()
            except OSError:
                pass
            try:
                self.out_file.close()
            except OSError:
                pass

        def exit_code(self) -> int:
            if self.pi is None:
                return -1
            code = wintypes.DWORD()
            kernel32.GetExitCodeProcess(self.pi.hProcess, ctypes.byref(code))
            return code.value

        def is_alive(self) -> bool:
            if self.pi is None:
                return False
            return kernel32.WaitForSingleObject(self.pi.hProcess, 0) == WAIT_TIMEOUT

    def _spawn_appcontainer(worker_python: str, worker_args: list[str], sid: LPVOID,
                             pythonpath: str | None, extra_env: dict[str, str],
                             mem_limit_bytes: int) -> "_ChildProcess":
        """Full-lockdown spawn: AppContainer SID + child-process-restricted
        policy + full mitigation policy + handle-list restricted to
        exactly the control pipe ends, nested in a KILL_ON_JOB_CLOSE job
        with ActiveProcessLimit=1. DETACHED_PROCESS not CREATE_NO_WINDOW
        (gotcha 2 -- the latter spawns a forbidden conhost.exe child
        under child-process-restricted policy)."""
        stdin_r, stdin_w, stdout_r, stdout_w = make_pipe_pair()

        # Gotcha 8 (found via GHA CI bisect, 2026-08-16): opening the NUL
        # device from INSIDE the AppContainer is denied on some hosts
        # (GitHub's windows-latest / Server 2025 runners; allowed on this
        # Win11 dev box), and the worker needs a NUL fd to point fd 1/2
        # away from the control pipe before any hostile decode runs. The
        # broker is unsandboxed, so it opens NUL and hands the worker an
        # inheritable write-only handle (value passed in the env). The only
        # capability this grants the sandbox is writing to nowhere.
        nul_sa = SECURITY_ATTRIBUTES()
        nul_sa.nLength = ctypes.sizeof(nul_sa)
        nul_sa.lpSecurityDescriptor = None
        nul_sa.bInheritHandle = True
        nul_h = kernel32.CreateFileW(
            "NUL", GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
            ctypes.byref(nul_sa), OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        if not nul_h or nul_h == INVALID_HANDLE_VALUE.value:
            err = ctypes.get_last_error()
            for h in (stdin_r, stdin_w, stdout_r, stdout_w):
                kernel32.CloseHandle(h)
            raise OSError(f"CreateFileW(NUL) failed err={err}")

        extra_env = {**extra_env, "FAUXCASA_DECODESVC_NUL_HANDLE": str(int(nul_h))}
        env_block = _build_env_block(pythonpath, extra_env)
        # Frozen dispatch: worker_args == ["--decode-worker"] (no script
        # path -- the exe re-invokes itself, main.py dispatches the flag
        # before argparse). Source dispatch: worker_args == [worker_script].
        cmdline = " ".join(f'"{a}"' for a in [worker_python, *worker_args])
        cmd_buf = ctypes.create_unicode_buffer(cmdline)

        job = make_job(mem_limit_bytes)

        sec_cap = SECURITY_CAPABILITIES()
        sec_cap.AppContainerSid = sid
        sec_cap.Capabilities = None
        sec_cap.CapabilityCount = 0
        sec_cap.Reserved = 0

        child_policy = ctypes.c_uint32(PROCESS_CREATION_CHILD_PROCESS_RESTRICTED)
        handle_arr = (wintypes.HANDLE * 3)(stdin_r, stdout_w, nul_h)
        mitigation = ctypes.c_uint64(MITIGATION_FULL)

        attrs = [
            (PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, ctypes.byref(sec_cap), ctypes.sizeof(sec_cap)),
            (PROC_THREAD_ATTRIBUTE_CHILD_PROCESS_POLICY, ctypes.byref(child_policy), ctypes.sizeof(child_policy)),
            (PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handle_arr, ctypes.sizeof(handle_arr)),
            (PROC_THREAD_ATTRIBUTE_MITIGATION_POLICY, ctypes.byref(mitigation), ctypes.sizeof(mitigation)),
        ]

        size = SIZE_T(0)
        kernel32.InitializeProcThreadAttributeList(None, len(attrs), 0, ctypes.byref(size))
        attr_buf = (ctypes.c_byte * size.value)()
        attr_list = ctypes.cast(attr_buf, LPVOID)
        if not kernel32.InitializeProcThreadAttributeList(attr_list, len(attrs), 0, ctypes.byref(size)):
            kernel32.CloseHandle(stdin_r)
            kernel32.CloseHandle(stdin_w)
            kernel32.CloseHandle(stdout_r)
            kernel32.CloseHandle(stdout_w)
            kernel32.CloseHandle(nul_h)
            kernel32.CloseHandle(job)
            raise ctypes.WinError(ctypes.get_last_error())

        pi = PROCESS_INFORMATION()
        try:
            for attr_id, ptr, sz in attrs:
                if not kernel32.UpdateProcThreadAttribute(attr_list, 0, attr_id, ptr, sz, None, None):
                    raise ctypes.WinError(ctypes.get_last_error())

            si = STARTUPINFOEXW()
            si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
            si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            si.StartupInfo.hStdInput = stdin_r
            si.StartupInfo.hStdOutput = stdout_w
            si.StartupInfo.hStdError = stdout_w
            si.lpAttributeList = attr_list

            flags = (DETACHED_PROCESS | CREATE_UNICODE_ENVIRONMENT
                     | EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED)
            ok = kernel32.CreateProcessW(
                None, cmd_buf, None, None, True, flags,
                env_block, None, ctypes.byref(si.StartupInfo), ctypes.byref(pi))
            err = ctypes.get_last_error()
        finally:
            kernel32.DeleteProcThreadAttributeList(attr_list)

        kernel32.CloseHandle(stdin_r)
        kernel32.CloseHandle(stdout_w)
        kernel32.CloseHandle(nul_h)  # child holds its own inherited copy

        if not ok:
            kernel32.CloseHandle(stdin_w)
            kernel32.CloseHandle(stdout_r)
            kernel32.CloseHandle(job)
            raise ctypes.WinError(err)

        if not kernel32.AssignProcessToJobObject(job, pi.hProcess):
            err2 = ctypes.get_last_error()
            kernel32.TerminateProcess(pi.hProcess, 1)
            kernel32.CloseHandle(pi.hProcess)
            kernel32.CloseHandle(pi.hThread)
            kernel32.CloseHandle(job)
            kernel32.CloseHandle(stdin_w)
            kernel32.CloseHandle(stdout_r)
            raise ctypes.WinError(err2)
        # gotcha 5: resume only AFTER job assignment -- the child must
        # never execute a single instruction outside the job.
        kernel32.ResumeThread(pi.hThread)

        import msvcrt
        out_fd = msvcrt.open_osfhandle(stdout_r.value, os.O_RDONLY)
        out_file = os.fdopen(out_fd, "rb", buffering=0)
        in_fd = msvcrt.open_osfhandle(stdin_w.value, 0)
        in_file = os.fdopen(in_fd, "wb", buffering=0)

        return _ChildProcess(pi, job, in_file, out_file)


# ---------------------------------------------------------------------------
# FIX 2 (P2, reviewer fix pass (fauxcasa-i92.3.x)): closes a race between
# recv_response's deadline timer and a successful read completing.
# timer.cancel() in the old code was a no-op once the timer callback had
# already started running, so a response that arrived right as the
# deadline expired could still see the worker killed AFTER recv_response
# had already decided to return that response -- handing a trusted caller
# a "successful" result from a worker whose process was mid-TerminateProcess.
# The old code also tracked "did the deadline fire" on an INSTANCE
# attribute (self._deadline_hit), which persists across jobs: a timer left
# running past its own recv_response call (already fixed by cancel(), but
# only when cancel() actually beats the callback) could otherwise poison
# the next job's read. This class is deliberately tiny and dependency-free
# (just a lock) so it is unit-testable without any real threading.Timer.

class _DeadlineGuard:
    """Serializes "the deadline fired" against "the read finished" so
    exactly one of them wins. `terminate_fn` is called AT MOST ONCE, and
    only if on_deadline() reaches the lock before finish() does."""

    def __init__(self, terminate_fn) -> None:
        self._terminate_fn = terminate_fn
        self._lock = threading.Lock()
        self._hit = False
        self._done = False

    def on_deadline(self) -> bool:
        """Timer callback. Returns True iff this call actually fired the
        kill (i.e. finish() had not already run); False means the read
        already completed and this callback is a harmless straggler."""
        with self._lock:
            if self._done:
                return False
            self._hit = True
            self._terminate_fn()
            return True

    def finish(self) -> bool:
        """Called once the read has returned (with a result OR an
        exception). Returns whether the deadline had already fired by
        then. After this returns, a late on_deadline() call is guaranteed
        to be a no-op."""
        with self._lock:
            self._done = True
            return self._hit


# ---------------------------------------------------------------------------
# WinSandboxWorker: importable on any platform (construction touches no
# Windows API); spawn() is the one entry point that requires Windows.

class WinSandboxWorker:
    """One live sandboxed decode worker (design doc sec 1, 2, 4). Owns the
    AppContainer profile grant, the job object, the control pipes, and
    the response arena for exactly one worker process."""

    def __init__(self, arena_bytes: int = ARENA_DEFAULT_BYTES, probe: bool = False,
                 profile_name: str = PROFILE_NAME,
                 mem_limit_bytes: int = DEFAULT_MEM_LIMIT_BYTES,
                 spawn_deadline_ms: int | None = DEFAULT_SPAWN_DEADLINE_MS) -> None:
        self.arena_bytes = arena_bytes
        self.profile_name = profile_name
        self.mem_limit_bytes = mem_limit_bytes
        # fauxcasa-i92.3.4: bounds the ENTIRE spawn() handshake below;
        # None disables it (unbounded, prior behavior).
        self.spawn_deadline_ms = spawn_deadline_ms
        self._probe_enabled = probe
        self._child = None
        self._sid = None
        self._locked = False
        self._id_counter = 0
        self.hello: Hello | None = None
        self.hello_noise_bytes = 0  # see _read_hello_frame_tolerant (gotcha 7)
        # fauxcasa-yfq: what spawn() actually launched (None when frozen)
        # and whether it came from the staged copy under the cache root.
        self.worker_script_path: str | None = None
        self.worker_script_staged = False
        self._arena_handle = None
        self._arena_addr: int | None = None
        self._winsta_grant: dict | None = None
        # fauxcasa-ez2.9 Stage 1: best-effort ACL grant failures from the
        # most recent spawn() (dir -> error detail string), diagnostic only
        # -- spawn() no longer raises on these (P1 finding).
        self.grant_errors: dict[str, str] = {}
        # FIX 7 (should-fix, review fix pass): per-session protocol-
        # violation counter (design doc sec 1/sec 7 gate 3). Full
        # counter-assertion contract is fauxcasa-i92.3.2; this is just the
        # cheap, correct minimum -- increment it, kill the worker, no
        # retry (the pool, not this transport, owns retry/respawn).
        self.protocol_violations = 0

    # -- lifecycle ---------------------------------------------------------

    def spawn(self) -> Hello:
        if sys.platform != "win32":
            raise RuntimeError(
                "WinSandboxWorker.spawn() requires Windows (this process is "
                f"{sys.platform!r}); construction/validation is fine cross-platform, "
                "spawning a real sandboxed worker is not")
        if self._child is not None:
            raise RuntimeError("already spawned; call close() first")

        worker_python, worker_pythonpath = resolve_worker_python()
        frozen = getattr(sys, "frozen", False)
        # Source-dispatch only; stay None when frozen so the staging and
        # blame-list checks below never depend on short-circuit ordering.
        worker_script: str | None = None
        worker_dir: str | None = None
        if frozen:
            # fauxcasa-ez2.9 Stage 1 (P0 finding): the worker is THIS SAME
            # exe re-invoked as `<exe> --decode-worker` -- main.py dispatches
            # that flag to decodesvc_worker_win.main() before its own
            # argparse (mirrors the existing videostream `--worker` pattern).
            # No worker-script path, no PYTHONPATH.
            #
            # P2 finding "frozen grant scope": ACL grant targets are
            # sys._MEIPASS (the extracted onedir _internal payload,
            # RECURSIVE grant) and the exe FILE ITSELF (NO_INHERITANCE --
            # a single-file grant that cannot widen read to sibling
            # files). Refuse to grant -- fail startup with a clear reason
            # instead of silently widening access -- when a target that
            # actually receives a RECURSIVE grant is a known user folder.
            #
            # Re-review residual (item 4): the refusal now applies ONLY
            # to the actual grant targets, not to the exe's directory in
            # general -- checking exe_dir unconditionally (as this used
            # to) refused a perfectly safe layout, e.g. a onedir build's
            # exe sitting in Downloads next to its OWN `_internal`
            # subfolder (that subfolder, not Downloads itself, is what
            # gets the recursive grant; Downloads' other files are never
            # touched). exe_dir only matters when it IS the recursive
            # grant target, i.e. sys._MEIPASS == exe_dir (an "extract in
            # place" layout with no distinct payload subfolder) -- and
            # that case is already covered by checking meipass itself.
            worker_args = ["--decode-worker"]
            # fauxcasa-ayh review finding 1: the refusal itself now lives
            # in check_grant_targets_safe (shared with preflight_worker_
            # grants) -- called on the SAME grant_targets this branch
            # always used (meipass RECURSIVE + the exe FILE NO_INHERITANCE,
            # of which only the former is ever checked), so this raises
            # in exactly the same place, before any grant is issued.
            grant_targets = worker_grant_targets(worker_python, worker_pythonpath)
            check_grant_targets_safe(grant_targets)
        else:
            worker_script = str(Path(__file__).resolve().with_name("decodesvc_worker_win.py"))
            worker_dir = str(Path(worker_script).parent)
            worker_args = [worker_script]
            grant_targets = worker_grant_targets(worker_python, worker_pythonpath)

        # P1 finding "Profile/SID race": resolve the SID exactly once per
        # process (module-level cache + lock) -- never call the userenv
        # profile API concurrently, which every parallel warm() spawn used
        # to do.
        sid = get_cached_profile_sid(self.profile_name)
        self._sid = sid

        # Everything from here to a live child runs under one cleanup
        # guard: close() is idempotent, _child is still None, and any
        # failure (grant/winsta RuntimeError, arena WinError, spawn
        # OSError) must free the per-spawn SID -- and the arena, once
        # created -- or a retried spawn() overwrites and leaks them
        # (Codex review PR110 rounds 4+5).
        try:
            # fauxcasa-ez2.9 Stage 1: best-effort, one-time-per-(SID,dir)
            # grants (see grant_read_execute_once) -- NOT raised on failure.
            # The hello handshake below is the actual readability proof; a
            # Program-Files-class install where ALL APPLICATION PACKAGES
            # already has RX must still spawn even if WRITE_DAC is denied.
            self.grant_errors = {}
            self.worker_script_staged = False
            self.worker_script_path = None
            for target_path, target_inherit in grant_targets:
                err = grant_read_execute_once(target_path, sid, inherit=target_inherit)
                if err is not None:
                    self.grant_errors[target_path] = err

            # fauxcasa-yfq: the worker-script dir refused the grant (a
            # ReFS/Dev Drive checkout) -- launch from a content-hashed
            # copy under the cache root instead, which the user CAN grant.
            # See the staging comment block above stage_worker_script for
            # why only this one file moves and what it cannot fix.
            #
            # Staging is eager (on the grant failure itself, not after a
            # first pre-hello death) but only takes effect when the
            # staging root's OWN grant succeeds, so it can only ever swap
            # a directory that refused the grant for one that just
            # accepted it. The Program-Files-class case the best-effort
            # rule exists for -- ALL APPLICATION PACKAGES already has RX on
            # the source, WRITE_DAC denied -- therefore either launches a
            # copy the container was just granted (fine) or, if the cache
            # root refused too, stays on the readable source. Launching
            # the copy after a refused staging grant was wrong: a non-None
            # error means SetNamedSecurityInfoW failed just now, and the
            # copy under LOCALAPPDATA has no AAP ACE to fall back on
            # (review round 1). A reactive variant (stage only on the
            # retry after a permission-flavoured death) would need
            # cross-spawn state and cost every ReFS user one failed spawn
            # per process for no extra safety once this rule holds.
            if worker_dir is not None and worker_dir in self.grant_errors:
                try:
                    staged = stage_worker_script(worker_script)
                    staging_root = str(worker_staging_root())
                    staging_err = grant_read_execute_once(
                        staging_root, sid, inherit=SUB_CONTAINERS_AND_OBJECTS_INHERIT)
                    if staging_err is not None:
                        self.grant_errors[staging_root] = staging_err
                        refused_key = f"{worker_script}|staging-refused"
                        if refused_key not in _staging_logged:
                            _staging_logged.add(refused_key)
                            _log.warning(
                                "worker script staged to %r but %r refused the ACL grant too "
                                "(%s); launching from the source path -- the hello handshake "
                                "decides", staged, staging_root, staging_err)
                    else:
                        grant_targets.append((staging_root, SUB_CONTAINERS_AND_OBJECTS_INHERIT))
                        worker_args = [staged]
                        self.worker_script_staged = True
                        if worker_script not in _staging_logged:
                            _staging_logged.add(worker_script)
                            _log.warning(
                                "worker script dir %r refused the AppContainer ACL grant; "
                                "launching the sandbox worker from a content-hashed copy at %r",
                                worker_dir, staged)
                except OSError as e:
                    refused_key = f"{worker_script}|staging-failed"
                    if refused_key not in _staging_logged:
                        _staging_logged.add(refused_key)
                        _log.warning("could not stage the worker script under %r (%s); "
                                     "launching from the source path",
                                     str(worker_staging_root()), e)
            self.worker_script_path = worker_args[0] if not frozen else None

            winsta_result = grant_winsta_desktop(sid)
            self._winsta_grant = winsta_result
            if winsta_result.get("window_station") != "granted" or winsta_result.get("desktop") != "granted":
                raise RuntimeError(
                    "window station/desktop grant failed (gotcha 3, mandatory for "
                    f"AppContainer loader init): {winsta_result}")

            arena_handle = self._create_arena()

            extra_env = {"FAUXCASA_DECODESVC_ARENA_BYTES": str(self.arena_bytes)}
            if self._probe_enabled:
                extra_env["FAUXCASA_DECODESVC_PROBE"] = "1"

            child = _spawn_appcontainer(worker_python, worker_args, sid,
                                         worker_pythonpath, extra_env, self.mem_limit_bytes)
        except Exception:
            self.close()
            raise
        self._child = child

        # fauxcasa-i92.3.4: one _DeadlineGuard/timer bounds the ENTIRE
        # handshake below (hello read incl. its resync loop, the
        # attach_arena ack, the locked frame) -- mirrors recv_response's
        # per-job deadline pattern (FIX 2, reviewer fix pass
        # (fauxcasa-i92.3.x)) via the same `self._deadline_guard(...)`
        # factory seam. A worker that spawns but never writes a byte is a
        # strictly easier attack than stalling an in-progress job, and
        # the three blocking reads below were, before this fix, all
        # unbounded. spawn_deadline_ms=None (constructor arg) disables
        # this entirely, same as recv_response's timeout_ms=None.
        timer = None
        try:
            # The arena duplication runs BEFORE the timer is armed (Codex
            # cross-vendor review, P1): DuplicateHandle is a fast local
            # syscall on OUR OWN handles that the worker cannot stall, but
            # a deadline firing mid-call TerminateProcess()es its target
            # and can make it raise a raw PermissionError instead of the
            # WORKER_CRASHED-from-EOF the conversion below expects --
            # escaping both the TIMEOUT re-report here and the pool's
            # timeout accounting/retry. The deadline exists to bound the
            # blocking pipe reads, and every one of those comes after.
            dup_arena = _duplicate_into_child(arena_handle, child.pi.hProcess)

            # Guard/timer setup sits INSIDE this try (opus review): a
            # timer.start() that raises (thread exhaustion) must reach the
            # same close() as every other spawn failure, or the per-spawn
            # SID/arena leak fixes (Codex review PR110 rounds 4+5) regress
            # for exactly this path.
            guard = self._deadline_guard(self._terminate_child)
            if self.spawn_deadline_ms is not None:
                timer = threading.Timer(self.spawn_deadline_ms / 1000, guard.on_deadline)
                timer.daemon = True
                timer.start()

            try:
                try:
                    hello_msg, noise = _read_hello_frame_tolerant(child.out_file)
                except DecodeServiceError as e:
                    # A worker that dies before hello almost always died in
                    # loader/startup; the NTSTATUS exit code (0xC0000142 user32
                    # init, 0xC0000135 DLL not found, 0xC0000022 access denied,
                    # ...) plus the exe we actually spawned is the difference
                    # between a debuggable CI failure and a shrug. `e.code` is
                    # preserved unchanged (e.g. WORKER_CRASHED), so an
                    # enriched error here still converts to TIMEOUT correctly
                    # below if the deadline is what actually killed the child.
                    waited = kernel32.WaitForSingleObject(child.pi.hProcess, 2000)
                    exited = waited == WAIT_OBJECT_0
                    exit_code = wintypes.DWORD(0)
                    kernel32.GetExitCodeProcess(child.pi.hProcess, ctypes.byref(exit_code))
                    # P2 finding "self-heal for stale markers": a worker
                    # that dies before hello is exactly the symptom of a
                    # stale ACL marker withholding a grant it actually
                    # needs (loader init, e.g. 0xC0000142). Drop the
                    # marker + in-process entry for every grant target now
                    # so the pool's existing WORKER_CRASHED retry (which
                    # calls spawn() again) re-issues the real grant instead
                    # of skipping it a second time.
                    for target_path, _target_inherit in grant_targets:
                        invalidate_acl_grant(target_path, sid)
                    # fauxcasa-yfq: a worker that has ALREADY EXITED never
                    # reached the protocol -- whatever it wrote is the
                    # interpreter's own startup diagnostics, not frames.
                    # Reassemble them (bytes the hello reader consumed +
                    # the rest of the pipe, safe to drain now that the
                    # writer is gone) and report the real cause. When the
                    # reader never saw a well-formed length prefix
                    # (`e.unframed`) AND the exit was non-zero, the error
                    # is re-coded WORKER_CRASHED: PROTOCOL means "evidence
                    # of compromise" (kill, count, never retry), and an
                    # interpreter that could not open its script is
                    # neither compromised nor worth counting as such;
                    # WORKER_CRASHED is what every other pre-hello death
                    # (0xC0000142 etc.) already reports. A FRAMED bogus or
                    # non-hello first message keeps PROTOCOL whatever the
                    # exit code -- that is protocol bytes gone wrong, the
                    # very case PROTOCOL exists for (review round 1).
                    leading = getattr(e, "leading_bytes", b"")
                    unframed = getattr(e, "unframed", False)
                    if exited:
                        tail = _drain_pipe(child.out_file, PREHELLO_OUTPUT_LIMIT)
                        # A worker-dir grant failure that staging worked
                        # around is no longer a cause; leave it out of
                        # the blame list (self.grant_errors keeps it).
                        unresolved = {
                            p: err for p, err in self.grant_errors.items()
                            if not (self.worker_script_staged and p == worker_dir)}
                        reason = describe_prehello_death(
                            leading + tail, exit_code.value, worker_args, unresolved,
                            pythonpath=worker_pythonpath)
                        code = (ErrorCode.WORKER_CRASHED
                                if unframed and exit_code.value != 0 else e.code)
                        # The reader's own detail usually repeats the
                        # output already shown above; keep a short tail
                        # of it for correlation, not the whole thing.
                        reader_detail = e.detail if len(e.detail) <= 200 else e.detail[:200] + "..."
                        msg = (f"{reason} [hello reader: {reader_detail}; exe {worker_python!r}; "
                               f"pythonpath {worker_pythonpath!r}]")
                    else:
                        code = e.code
                        msg = (f"{e} [worker still running after 2s; last exit code query "
                               f"{exit_code.value:#010x}; exe {worker_python!r}; "
                               f"pythonpath {worker_pythonpath!r}]")
                    # Keep the TYPE in step with the code: a PROTOCOL error
                    # must stay a ProtocolViolation so WinDecodePool.decode's
                    # `except ProtocolViolation` (count, kill, no retry)
                    # catches it rather than the generic branch.
                    if code is ErrorCode.PROTOCOL:
                        raise ProtocolViolation(msg) from e
                    raise DecodeServiceError(code, msg) from e
                self.hello_noise_bytes = noise
                self.hello = parse_hello(hello_msg, self.arena_bytes)

                _write_frame(child.in_file, {"op": "attach_arena", "handle": dup_arena})
                attach_resp = _read_frame(child.out_file)
                if (not isinstance(attach_resp, dict) or attach_resp.get("op") != "attach_arena"
                        or attach_resp.get("ok") is not True):
                    raise ProtocolViolation(f"attach_arena failed: {attach_resp!r}")

                locked_msg = _read_frame(child.out_file)
                is_appc = parse_locked(locked_msg)
                if not is_appc:
                    raise RuntimeError(
                        "worker reports is_appcontainer=False after phase 2 -- refusing "
                        "to trust lockdown; this is a security finding, not a retry case")
                self._locked = True
            except Exception as e:
                # fauxcasa-i92.3.4: the deadline firing while one of the
                # reads above blocks kills the child (guard.on_deadline()
                # -> _terminate_child()), which closes its end of the
                # pipe -- the blocked read then raises
                # DecodeServiceError(WORKER_CRASHED) from EOF, same as any
                # other unexpected child death. guard.finish() is the
                # single point that atomically decides whether the
                # deadline or the handshake "won" (same race
                # recv_response's guard closes); if the deadline won AND
                # the error is our own WORKER_CRASHED-from-EOF, re-report
                # it as TIMEOUT so the caller sees OUR kill, not an honest
                # crash. ProtocolViolation IS a DecodeServiceError
                # subclass but its code is always PROTOCOL, never
                # WORKER_CRASHED, so this check naturally leaves it -- and
                # every other exception -- to propagate unchanged even if
                # the timer fired late: evidence of compromise (or an
                # unrelated failure), not a timing artifact.
                hit = guard.finish()
                if (isinstance(e, DecodeServiceError)
                        and e.code == ErrorCode.WORKER_CRASHED and hit):
                    raise DecodeServiceError(
                        ErrorCode.TIMEOUT,
                        f"spawn handshake deadline exceeded "
                        f"(spawn_deadline_ms={self.spawn_deadline_ms}); "
                        f"worker killed") from e
                raise
            else:
                hit = guard.finish()
                if hit:
                    # The handshake completed, but the deadline fired
                    # first (or concurrently) -- the worker is already
                    # killed or dying. Never return a hello from a corpse
                    # (mirrors recv_response's identical comment).
                    self.close()
                    raise DecodeServiceError(
                        ErrorCode.TIMEOUT,
                        f"spawn handshake deadline exceeded "
                        f"(spawn_deadline_ms={self.spawn_deadline_ms}); "
                        f"worker killed")
        except Exception:
            self.close()
            raise
        finally:
            # guard.finish() (above, in both the except and else branches)
            # always runs BEFORE this cancel() -- once finish() has
            # recorded "done", a timer callback firing later is a
            # guaranteed no-op, so a straggler thread here can never race
            # a later job (same ordering argument as recv_response's
            # finally comment).
            if timer is not None:
                timer.cancel()

        return self.hello

    def close(self) -> None:
        """Safe to call twice. Deliberately does NOT delete the
        AppContainer profile or revoke its ACL grants: the profile is
        reused across broker sessions (design doc sec 4)."""
        if self._child is not None:
            self._child.close()
            self._child = None
        self._locked = False
        # fauxcasa-i92.3.4 (opus review): a worker we killed or refused to
        # trust must not keep advertising its handshake -- anything that
        # logs or inspects a failed spawn would otherwise see a hello from
        # a corpse. _require_ready() already blocks jobs either way; this
        # just clears the residue.
        self.hello = None
        self.hello_noise_bytes = 0
        if self._arena_addr:
            kernel32.UnmapViewOfFile(ctypes.c_void_p(self._arena_addr))
            self._arena_addr = None
        if self._arena_handle:
            kernel32.CloseHandle(self._arena_handle)
            self._arena_handle = None
        # P1 finding "Profile/SID race" fix: the SID now comes from the
        # process-wide get_cached_profile_sid() cache and is SHARED across
        # every WinSandboxWorker for this profile_name -- deliberately NOT
        # FreeSid()'d here (that would free memory every other cached
        # worker/future spawn still points at). The reusable AppContainer
        # *profile* itself was always left untouched; the cached SID now
        # simply lives for the lifetime of the process too.
        self._sid = None

    def kill(self) -> None:
        """Hard-stop path (design doc sec 1: 'never a polite request to
        possibly-owned code'). Identical to close() for this transport --
        there is no separate soft-kill state; TerminateProcess always
        runs before the pipes are closed (gotcha 5)."""
        self.close()

    def _terminate_child(self) -> None:
        """fauxcasa-i92.3.1: the deadline-timer hard-kill hook, called
        from recv_response's background threading.Timer when a job's
        timeout_ms expires (design doc sec 1: 'never a polite request').
        Kept as its own small method -- touching self._child only through
        this one surface -- so a test can inject a fake self._child and
        monkeypatch just this call to verify the timer fired, without a
        real Windows process to kill."""
        if self._child is not None and self.is_alive():
            kernel32.TerminateProcess(self._child.pi.hProcess, 1)

    def __del__(self) -> None:  # pragma: no cover - best-effort GC safety net
        try:
            self.close()
        except Exception:
            pass

    def is_alive(self) -> bool:
        """True iff a child process is spawned and still running. False
        after close()/kill(), or if spawn() was never called."""
        return self._child is not None and self._child.is_alive()

    # -- arena ---------------------------------------------------------------

    def _create_arena(self):
        size_high = (self.arena_bytes >> 32) & 0xFFFFFFFF
        size_low = self.arena_bytes & 0xFFFFFFFF
        h = kernel32.CreateFileMappingW(
            INVALID_HANDLE_VALUE, None, PAGE_READWRITE, size_high, size_low, None)
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())
        addr = kernel32.MapViewOfFile(h, FILE_MAP_ALL_ACCESS, 0, 0, self.arena_bytes)
        if not addr:
            err = ctypes.get_last_error()
            kernel32.CloseHandle(h)
            raise ctypes.WinError(err)
        self._arena_handle = h
        self._arena_addr = addr
        return h

    def read_arena(self, off: int, length: int) -> bytes:
        """COPIES `length` bytes out of the arena at `off` (design doc
        sec 2.4 checklist item 6 -- no trusted-side reference into memory
        a worker can still write)."""
        if self._arena_addr is None:
            raise RuntimeError("arena not mapped (spawn() not called or already closed)")
        if off < 0 or length < 0 or off + length > self.arena_bytes:
            raise ProtocolViolation(
                f"read_arena[{off}:{off + length}) outside arena of {self.arena_bytes} bytes")
        buf = ctypes.create_string_buffer(length)
        ctypes.memmove(buf, self._arena_addr + off, length)
        return buf.raw

    # -- control channel -------------------------------------------------

    def send_request(self, msg: dict) -> None:
        if self._child is None:
            raise RuntimeError("worker not spawned")
        _write_frame(self._child.in_file, msg)

    def _deadline_guard(self, terminate_fn) -> "_DeadlineGuard":
        """Factory seam (FIX 2, reviewer fix pass (fauxcasa-i92.3.x)): the
        one place recv_response constructs its guard, so a test can wrap
        or replace it (e.g. to invoke on_deadline() deterministically,
        without a real timer) without reaching into recv_response's own
        locals."""
        return _DeadlineGuard(terminate_fn)

    def recv_response(self, timeout_ms: int | None = None) -> dict:
        """fauxcasa-i92.3.1: with timeout_ms=None, unchanged behavior --
        block on the read with no deadline. With timeout_ms set, arm a
        background threading.Timer that hard-kills the child (never a
        polite request, design doc sec 1) if the read is still blocked
        when it fires; killing the child closes its end of the pipe, so
        the blocked read returns EOF and _read_frame raises
        DecodeServiceError(WORKER_CRASHED) -- which we re-report as
        TIMEOUT here, since the crash was OUR kill, not the worker dying
        on its own. A ProtocolViolation (malformed/oversized frame)
        propagates unchanged even if the timer fired late -- it is
        evidence of compromise, not a timing artifact.

        FIX 2 (P2, reviewer fix pass (fauxcasa-i92.3.x)): a `_DeadlineGuard`
        (see its class docstring) replaces the old instance-level
        `_deadline_hit` flag and closes the race where the timer fires
        just as the read completes successfully -- `guard.finish()` is the
        single point that decides, atomically, whether the deadline or the
        read "won"; if the deadline won even though the read returned a
        well-formed response, that response is from a worker we are in the
        middle of killing and must never be handed to a trusted caller."""
        if self._child is None:
            raise RuntimeError("worker not spawned")
        if timeout_ms is None:
            return _read_frame(self._child.out_file)

        guard = self._deadline_guard(self._terminate_child)
        timer = threading.Timer(timeout_ms / 1000, guard.on_deadline)
        timer.daemon = True
        timer.start()
        try:
            resp = _read_frame(self._child.out_file)
        except DecodeServiceError as e:
            hit = guard.finish()
            if e.code == ErrorCode.WORKER_CRASHED and hit:
                raise DecodeServiceError(
                    ErrorCode.TIMEOUT,
                    f"deadline exceeded (timeout_ms={timeout_ms}); worker killed") from e
            raise
        else:
            hit = guard.finish()
            if hit:
                # The read succeeded, but the deadline fired first (or
                # concurrently) -- the worker is already killed or dying.
                # Never return a result from a corpse.
                self.kill()
                raise DecodeServiceError(
                    ErrorCode.TIMEOUT,
                    "deadline expired as the response arrived; worker killed")
            return resp
        finally:
            # guard.finish() (above, in both branches) always runs BEFORE
            # this cancel() -- once finish() has recorded "done", a timer
            # callback that fires later is a guaranteed no-op, so a
            # straggler thread here can never race a subsequent job.
            timer.cancel()

    def _require_ready(self) -> None:
        if self._child is None:
            raise RuntimeError("worker not spawned; call spawn() first")
        if not self._locked:
            raise RuntimeError(
                "lockdown sequencing violation: worker has not reported "
                "'locked' yet -- refusing to dispatch a job (design doc sec 4/7 gate 5)")

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    # -- trusted-side response validation (the fuzz-test seam) ---------------

    def _validate_response(self, resp: dict, expect_id: int | None = None) -> tuple[dict, PixelBuffer]:
        """The checklist (design doc sec 2.4) applied to a `decode`-shaped
        response, BEFORE any arena memory is touched. Raises
        ProtocolViolation or DecodeServiceError; never needs a live
        worker, arena mapping, or Windows -- the seam the trusted-side
        protocol-fuzz tests call directly with hand-built dicts.

        FIX 7 (should-fix, review fix pass): any ProtocolViolation raised
        below increments self.protocol_violations (design doc sec 7 gate
        3) before propagating -- this is the seam pure-trusted-side fuzz
        tests call WITHOUT a live worker, so counting here (rather than
        only in decode()'s kill path, which needs a real child to kill)
        is what lets those tests assert the counter directly. decode()
        additionally kills the worker on catching this from a live job."""
        try:
            return self._validate_response_checks(resp, expect_id)
        except ProtocolViolation:
            self.protocol_violations += 1
            raise

    def _validate_response_checks(self, resp: dict, expect_id: int | None = None) -> tuple[dict, PixelBuffer]:
        if not isinstance(resp, dict):
            raise ProtocolViolation(f"response is not a JSON object: {type(resp).__name__}")
        if expect_id is not None:
            # Codex review PR110 round 5 deferral (fauxcasa-i92.3.2): plain
            # `!=` accepts JSON true for 1 and 1.0 for an int expect_id
            # (Python numeric-tower equality) -- require an exact int type
            # first. `type(rid) is not int` deliberately rejects bool too
            # (bool is an int subclass) since a JSON `true`/`false` id is
            # not a legitimate response id.
            rid = resp.get("id")
            if type(rid) is not int or rid != expect_id:
                raise ProtocolViolation(f"response id {rid!r} != request id {expect_id}")
        if "sha256" in resp:
            sha = resp["sha256"]
            if not (isinstance(sha, str) and len(sha) == 64
                    and all(c in "0123456789abcdef" for c in sha)):
                raise ProtocolViolation(f"sha256 not 64 lowercase hex chars: {sha!r}")
        # Codex review PR110 round 5 deferral (fauxcasa-i92.3.2): `is not
        # True` alone lets ANY non-True value ("yes", 1, null, ...) fall
        # through into the honest-error branch below, where e.g.
        # {"ok": "yes", "error": "CORRUPT"} would raise a plain
        # DecodeServiceError with no counter increment/kill -- a worker
        # returning a non-boolean "ok" is protocol-malformed, not an
        # honest error. Require a real JSON boolean before deferring to
        # the honest-error branch.
        ok = resp.get("ok")
        if type(ok) is not bool:
            raise ProtocolViolation(f"'ok' field is not a JSON boolean: {ok!r}")
        if ok is not True:
            code_name = resp.get("error")
            try:
                code = ErrorCode(code_name)
            except (ValueError, TypeError) as e:
                raise ProtocolViolation(f"unknown/missing error code: {code_name!r}") from e
            if code not in WORKER_ERROR_CODES:
                raise ProtocolViolation(
                    f"broker-only error code in worker response: {code_name!r}")
            raise DecodeServiceError(code, str(resp.get("detail", "")))
        source = resp.get("source")
        if not isinstance(source, dict):
            raise ProtocolViolation(f"missing/invalid 'source' field: {source!r}")
        sw, sh = source.get("w"), source.get("h")
        if not (isinstance(sw, int) and isinstance(sh, int) and not isinstance(sw, bool)
                and not isinstance(sh, bool) and sw > 0 and sh > 0):
            raise ProtocolViolation(f"invalid source dims: {source!r}")
        pixels = resp.get("pixels")
        if not isinstance(pixels, dict):
            raise ProtocolViolation(f"missing/invalid 'pixels' field: {pixels!r}")
        try:
            pixfmt = PixelFormat(pixels.get("pixfmt"))
        except ValueError as e:
            raise ProtocolViolation(f"unknown pixfmt: {pixels.get('pixfmt')!r}") from e
        for key in ("w", "h", "stride", "off", "len"):
            v = pixels.get(key)
            if not isinstance(v, int) or isinstance(v, bool):
                raise ProtocolViolation(f"pixels.{key} missing/non-int: {v!r}")
        buf = PixelBuffer(w=pixels["w"], h=pixels["h"], stride=pixels["stride"],
                           pixfmt=pixfmt, off=pixels["off"], len=pixels["len"])
        buf.validate(arena_bytes=self.arena_bytes)
        PixelBuffer.validate_disjoint([buf])
        return source, buf

    def validate_meta(self, meta: object) -> MetaFields:
        """fauxcasa-i92.3.3: broker-side entry point for the checklist item
        5 metadata clamps (design doc sec 2.4), landed ahead of the index/
        poster ops that will actually carry MetaFields in a response.
        Mirrors _validate_response's counting seam exactly -- a
        ProtocolViolation from parse_meta_fields increments
        self.protocol_violations before propagating (the i92.3.2 counter
        contract), so this keeps working the day a real index() job calls
        it; the caller still owns the kill, same as _validate_response."""
        try:
            return parse_meta_fields(meta)
        except ProtocolViolation:
            self.protocol_violations += 1
            raise

    # -- jobs ------------------------------------------------------------

    def _open_and_duplicate_file(self, path: Path) -> int:
        h = kernel32.CreateFileW(
            str(path), GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL, None)
        if not h or h == INVALID_HANDLE_VALUE.value:
            # A CreateFileW failure here is a caller/file problem (bad
            # path, permissions, ...) -- it has nothing to do with the
            # worker's health and must stay an ordinary OSError, never
            # reclassified below (Codex review (feat/i92.3-pool-hardening),
            # explicit caveat).
            raise OSError(f"CreateFileW({path}) failed err={ctypes.get_last_error()}")
        try:
            try:
                return _duplicate_into_child(h, self._child.pi.hProcess)
            except OSError as e:
                # Codex review (feat/i92.3-pool-hardening): DuplicateHandle
                # targets the WORKER's process handle, not this file --
                # a raw OSError here (not DecodeServiceError(WORKER_CRASHED))
                # means the target process handle is dead, i.e. the worker's
                # process died while idle (OOM-killed by the job object,
                # crashed between jobs) and this is the first thing to touch
                # it since. Before this fix that OSError escaped uncaught,
                # outside the closed WORKER_CRASHED/TIMEOUT/PROTOCOL/
                # CANCELLED taxonomy (module docstring), so the pool's
                # retry-on-crash branch never triggered and the dead worker
                # stayed cached. Only reclassify when the worker is
                # confirmed not alive; any other DuplicateHandle failure
                # (a genuinely unexpected OS error with a live worker)
                # re-raises unchanged rather than being misdiagnosed as a
                # worker crash it isn't.
                if self.is_alive() is False:
                    raise DecodeServiceError(
                        ErrorCode.WORKER_CRASHED,
                        f"worker died before job dispatch: {e}") from e
                raise
        finally:
            kernel32.CloseHandle(h)

    def decode(self, path: Path, edge: int = 0, deadline_ms: int = 20_000) -> DecodeResult:
        """deadline_ms defaults to 20s (design doc sec 1's still default).
        fauxcasa-i92.3.1: on DecodeServiceError(TIMEOUT) this worker is
        killed and the error re-raised -- this transport does not retry;
        that policy belongs one layer up in WinDecodePool."""
        self._require_ready()
        req_id = self._next_id()
        dup_handle = self._open_and_duplicate_file(Path(path))
        # FIX 9 (contract drift, reviewer fix pass (fauxcasa-i92.3.x)):
        # design doc sec 2.2 lists deadline_ms as a common informational
        # request field; the worker's decode handler already tolerates
        # unknown/extra request keys (additive evolution), so this is a
        # pure addition -- purely informational today (the broker, not the
        # worker, enforces the deadline via recv_response's timer).
        request = {"id": req_id, "op": "decode", "edge": int(edge),
                   "pixfmt": PixelFormat.RGBA8.value, "handle": dup_handle,
                   "deadline_ms": int(deadline_ms)}
        try:
            self.send_request(request)
            resp = self.recv_response(timeout_ms=deadline_ms)
        except ProtocolViolation:
            # FIX 7 (should-fix, review fix pass): a malformed/oversized
            # incoming frame is evidence of compromise (design doc sec 1)
            # detected below _validate_response's seam (framing, not
            # shape), so it isn't counted there -- count it here.
            self.protocol_violations += 1
            self.kill()
            raise
        except DecodeServiceError as e:
            # fauxcasa-i92.3.1: TIMEOUT is this worker's own kill (design
            # doc sec 1); close it out here too, same as the
            # ProtocolViolation branch above. Any other DecodeServiceError
            # (e.g. an honest WORKER_CRASHED) is left alone -- the pool
            # owns retry/respawn decisions, not this transport.
            if e.code == ErrorCode.TIMEOUT:
                self.kill()
            raise
        try:
            source, buf = self._validate_response(resp, expect_id=req_id)
        except ProtocolViolation:
            # _validate_response already incremented protocol_violations
            # (its own seam, shared with the trusted-side fuzz tests);
            # here we just do the kill -- no retry, the worker is suspect.
            # (An honest CORRUPT/UNSUPPORTED/TOO_LARGE result is a plain
            # DecodeServiceError, not ProtocolViolation, and does not land
            # here -- the worker behaved correctly.)
            self.kill()
            raise
        # Checklist item 6: copy the bytes out of the arena now, before any
        # later job can overwrite this worker's single-slot arena, and KEEP
        # the copy -- no trusted consumer may re-read arena memory the
        # worker can still write (TOCTOU). The (off, len) descriptor in
        # `buf` stays for shape only; `pixels_bytes` is the real payload.
        pixels_bytes = self.read_arena(buf.off, buf.len)
        return DecodeResult(pixels=buf, source_w=source["w"], source_h=source["h"],
                             pixels_bytes=pixels_bytes)

    def probe(self, name: str, target: str | None = None, handle: int | None = None,
              extra: dict | None = None, deadline_ms: int | None = None) -> dict:
        """Send a probe job (test-only; the worker only honors it when
        spawned with probe=True, i.e. FAUXCASA_DECODESVC_PROBE=1 in its
        environment). Returns {attempt, allowed, detail}. `extra` carries
        probe-specific request fields (e.g. udp_to_broker's bhost/bport/
        nonce, bind_accept_wait's bport/wait_s) so those completed-I/O
        gates go through this same fail-closed validation instead of raw
        send_request/recv_response (Codex review PR110 P1); the fixed
        id/op/attempt keys always win over `extra`.

        deadline_ms (fauxcasa-i92.3.1): threaded straight to recv_response,
        same TIMEOUT->kill semantics as decode() -- needed by the live
        `stall` probe test, which spawns a worker that accepts a job and
        never answers, to exercise the broker's deadline path end to end.

        FIX 2 (P1, review fix pass): fail-closed. A denial (`allowed:
        False`) is accepted ONLY when the worker's response is well-formed
        proof that the requested probe actually ran: `ok` is True,
        `attempt` echoes the requested name, and `allowed` is a real bool.
        Anything else -- an unknown attempt, PROBE_ENABLED=0, a worker-side
        error, a missing/malformed field -- must NOT be silently treated
        as a denial; that would let a misspelled or removed probe
        silently "pass" the containment gate without ever having run.
        Raise ProtocolViolation instead, loudly, so the calling test
        ERRORS rather than green-passing. This is the integrity of the
        whole gate suite (design doc sec 7 gate 1).

        fauxcasa-i92.3.4: the counting/kill policy for those
        ProtocolViolations, ratified. Two categories:

        - Worker-LIE (count self.protocol_violations + kill(), mirroring
          decode()'s FIX 7 treatment): a transport-level ProtocolViolation
          out of recv_response (malformed/oversized frame -- closes the
          "pre-existing gap this task does not widen" noted at FIX 6
          above); a non-dict response; an id mismatch; a non-bool `ok`
          (new check below -- see its own comment); an `attempt` echo
          mismatch or a missing/non-bool `allowed`, both only reachable
          once `ok` is True, i.e. the worker CLAIMS the probe ran. All of
          these are the worker (or a malformed transport) asserting
          something false about a probe attempt -- evidence of
          compromise, same standing as any other ProtocolViolation.
        - Harness-error (raise only, no count, worker stays alive): an
          honest JSON `ok: false` -- the worker truthfully reports the
          probe did NOT run (unknown probe name, PROBE_ENABLED=0, an
          exception _handle_probe caught worker-side). Counting these
          against the escape-gate counter would poison it with harness
          mistakes that are not compromise events; fail-closed semantics
          are preserved regardless -- this is still not treated as a
          denial, it just doesn't kill a perfectly healthy worker."""
        self._require_ready()
        if not self._probe_enabled:
            raise RuntimeError(
                "worker was not spawned with probe=True; probe() is test-only")
        req_id = self._next_id()
        request: dict[str, Any] = {**(extra or {}),
                                   "id": req_id, "op": "probe", "attempt": name}
        if target is not None:
            request["target"] = target
        if handle is not None:
            request["handle"] = handle
        self.send_request(request)

        def _worker_lie(message: str) -> ProtocolViolation:
            # fauxcasa-i92.3.4: the shared count+kill seam for every
            # worker-lie branch below -- see the policy paragraph above.
            # RETURNS the exception (call sites say `raise _worker_lie(...)`)
            # rather than raising it here, so the control flow stays
            # visible to readers and type checkers (opus review).
            self.protocol_violations += 1
            self.kill()
            return ProtocolViolation(message)

        try:
            resp = self.recv_response(timeout_ms=deadline_ms)
        except ProtocolViolation:
            # fauxcasa-i92.3.4: closes the transport-level gap FIX 6 left
            # open -- a malformed/oversized frame is worker-lie evidence,
            # same as decode()'s own FIX 7 branch, so it must be counted
            # and killed too. Re-raises the SAME exception (not a new one
            # via _worker_lie) so its original message is preserved
            # unchanged, mirroring decode()'s FIX 7 branch exactly.
            # ProtocolViolation is a DecodeServiceError subclass, so this
            # must be caught BEFORE the generic except below, or it would
            # fall into the TIMEOUT-only branch there uncounted.
            self.protocol_violations += 1
            self.kill()
            raise
        except DecodeServiceError as e:
            # fauxcasa-i92.3.1: same TIMEOUT->kill semantics as decode();
            # any other DecodeServiceError propagates exactly as it did
            # before deadline_ms existed.
            if e.code == ErrorCode.TIMEOUT:
                self.kill()
            raise
        if not isinstance(resp, dict):
            raise _worker_lie(f"probe response is not a JSON object: {resp!r}")
        # FIX 6 (P3, reviewer fix pass (fauxcasa-i92.3.x)): same type-strict
        # id guard as _validate_response_checks -- plain `!=` accepts JSON
        # true for 1 and 1.0 for an int req_id (Python numeric-tower
        # equality).
        rid = resp.get("id")
        if type(rid) is not int or rid != req_id:
            raise _worker_lie(f"probe response id {rid!r} != request id {req_id}")
        # fauxcasa-i92.3.4: type-strict `ok` check, mirroring
        # _validate_response_checks's identical fix (Codex review PR110
        # round 5 deferral) -- plain `is not True` alone lets a malformed,
        # non-boolean `ok` ("yes", 1, null, ...) fall into the honest
        # ok=False branch below unnoticed. A non-bool `ok` is protocol-
        # malformed, a worker lie about its own response shape, not an
        # honest report -- count + kill.
        ok = resp.get("ok")
        if type(ok) is not bool:
            raise _worker_lie(f"probe {name!r} response 'ok' field is not a JSON boolean: {ok!r}")
        if ok is not True:
            # Harness-error: an honest `ok: false` -- the worker truthfully
            # reports the probe did not run (unknown attempt,
            # PROBE_ENABLED=0, a worker-side exception). Fail-closed (still
            # raised, never treated as a denial) but NOT evidence of
            # compromise -- no counter bump, no kill (see the policy
            # paragraph above).
            raise ProtocolViolation(
                f"probe {name!r} did not run to completion (ok=False, "
                f"error={resp.get('error')!r}, detail={resp.get('detail')!r}) -- "
                f"a broken or unknown probe must never be treated as a denial")
        # Everything below is only reachable with ok=True -- the worker is
        # now CLAIMING it ran the requested probe, so any inconsistency
        # here is the worker lying about that claim.
        if resp.get("attempt") != name:
            raise _worker_lie(f"probe response attempt {resp.get('attempt')!r} != requested {name!r}")
        allowed = resp.get("allowed")
        if not isinstance(allowed, bool):
            raise _worker_lie(f"probe {name!r} response missing/non-bool 'allowed': {allowed!r}")
        return {"attempt": name, "allowed": allowed,
                "detail": resp.get("detail"), "data_b64": resp.get("data_b64")}

    def probe_file(self, name: str, path: Path) -> dict:
        """Convenience for probe attempts needing a per-job handed file
        handle (currently only handed_fd_read)."""
        self._require_ready()
        dup = self._open_and_duplicate_file(Path(path))
        return self.probe(name, handle=dup)


# ---------------------------------------------------------------------------
# WinDecodePool: the minimal single-slot pool that owns the design-sec-1
# retry policy WinSandboxWorker itself deliberately defers (fauxcasa-i92.3.1).
# Importable on any platform -- only _ensure_worker()'s spawn() call needs
# Windows, same split as WinSandboxWorker above.

class WinDecodePool:
    """One live worker at a time, with the TIMEOUT/WORKER_CRASHED
    retry-once-then-permanent policy from design doc sec 1/2.5:
    ProtocolViolation is evidence of compromise and never retries; TIMEOUT
    and WORKER_CRASHED each get exactly one retry on a fresh worker before
    becoming permanent; the honest worker-reported codes (CORRUPT/
    UNSUPPORTED/TOO_LARGE) are permanent for the file and leave the worker
    alive and reusable.

    protocol_violations/timeouts/crashes are per-SESSION counters that
    live on the POOL, not the transport: a WinSandboxWorker's own
    protocol_violations counter resets to 0 every time a fresh worker is
    spawned, which is the wrong home for a session-wide tally (the full
    counter-assertion contract is fauxcasa-i92.3.2; this pool is where
    that contract's counters actually need to live to survive a respawn).

    Codex review (feat/i92.3-pool-hardening): this is a single-slot pool
    -- one live worker, one control pipe -- so it is NOT safe for two
    threads to call decode() concurrently without help: they would either
    share one worker's pipe mid-job (one caller's response is consumed by
    the other, producing a spurious id-mismatch ProtocolViolation that
    kills a perfectly healthy worker) or both observe self._worker as
    None and double-spawn, leaking a worker. `_job_lock` fixes this the
    minimal way available to a single-slot pool: whole-job serialization,
    the entire decode() retry loop as one critical section. Concurrency
    LANES (e.g. an interactive job preempting a queued batch job) are
    explicitly NOT this class's job -- that belongs to the future
    multi-worker DecodeService this class is a stepping stone toward
    (module docstring, design doc sec 2/4).

    fauxcasa-i92.3.4: ratified deviation from design doc sec 1, which
    describes the broker respawning a dead slot "in the background (~92-
    400 ms, off the hot path)". `_ensure_worker` above respawns lazily
    and SYNCHRONOUSLY, inline in the retrying `decode()` call -- a retry
    pays the full spawn latency on the hot path. Deliberate for this
    minimal single-slot pool: there is exactly one slot and, with no
    concurrent lane to protect from a stalled respawn, no hot path worth
    the complexity of a background respawn yet. Background respawn lands
    alongside the future multi-worker DecodeService this class is a
    stepping stone toward, not before."""

    def __init__(self, arena_bytes: int = ARENA_DEFAULT_BYTES, probe: bool = False,
                 profile_name: str = PROFILE_NAME,
                 mem_limit_bytes: int = DEFAULT_MEM_LIMIT_BYTES,
                 spawn_deadline_ms: int | None = DEFAULT_SPAWN_DEADLINE_MS) -> None:
        self.arena_bytes = arena_bytes
        self.probe = probe
        self.profile_name = profile_name
        self.mem_limit_bytes = mem_limit_bytes
        # fauxcasa-i92.3.4: threaded straight to each WinSandboxWorker this
        # pool constructs (_ensure_worker below). A spawn-deadline TIMEOUT
        # then flows through decode()'s existing except taxonomy below
        # (counted in self.timeouts, one retry via FIX 3's routing) with
        # no new pool code needed for it.
        self.spawn_deadline_ms = spawn_deadline_ms
        self._worker: WinSandboxWorker | None = None
        # Codex review (feat/i92.3-pool-hardening): guards the ENTIRE
        # decode() retry loop (and close(), which must not race a live
        # job's respawn) -- see the class docstring's "not safe for
        # concurrent decode()" paragraph.
        self._job_lock = threading.Lock()
        # Per-SESSION counters (design doc sec 1/2.5) -- survive respawns.
        self.protocol_violations = 0
        self.timeouts = 0
        self.crashes = 0
        # fauxcasa-ez2.9 Stage 2 review P2-6: a monotonically increasing
        # count of SUCCESSFUL decode() calls -- the only counter this
        # class exposed before this (protocol_violations/timeouts/
        # crashes) counts FAILURES, so a test asserting "the sandbox
        # started" could never tell a real routed decode from a deleted
        # routing branch that never called decode() at all. Never reset
        # (mirrors the other per-session counters).
        self.jobs = 0

    def _ensure_worker(self) -> WinSandboxWorker:
        if (self._worker is not None and self._worker._child is not None
                and not self._worker.is_alive()):
            # Codex review (feat/i92.3-pool-hardening): the cached worker
            # was spawned (it has a child process) but is no longer alive
            # -- it died while idle, between jobs (OOM-killed by the job
            # object, crashed on its own). Gated on `_child is not None` so
            # a constructed-but-never-spawned worker (is_alive() is also
            # False for that, but for an entirely different reason) is
            # never mistaken for a dead one. Deliberately NOT counted in
            # self.crashes: that counter counts failed JOBS per the
            # taxonomy in this class's docstring, and this worker never
            # failed a job -- it was found dead and silently replaced
            # before ever being handed one, which costs the caller nothing.
            self._worker.close()
            self._worker = None
        if self._worker is None:
            worker = WinSandboxWorker(
                arena_bytes=self.arena_bytes, probe=self.probe,
                profile_name=self.profile_name, mem_limit_bytes=self.mem_limit_bytes,
                spawn_deadline_ms=self.spawn_deadline_ms)
            worker.spawn()
            self._worker = worker
        return self._worker

    def decode(self, path: Path, edge: int = 0, deadline_ms: int = 20_000) -> DecodeResult:
        # Codex review (feat/i92.3-pool-hardening): whole-job
        # serialization -- see the class docstring. Every branch below
        # (return, retry, raise) stays inside this lock for the entire
        # method body.
        with self._job_lock:
            for attempt in range(2):
                worker = None
                try:
                    # FIX 3 (P2, reviewer fix pass (fauxcasa-i92.3.x)): spawn
                    # now happens INSIDE the try. Previously `_ensure_worker()`
                    # sat outside it, so a DecodeServiceError raised from
                    # spawn() itself (e.g. the worker died in loader init,
                    # WORKER_CRASHED) propagated with no counter increment and
                    # no retry -- bypassing the exact taxonomy this method
                    # exists to enforce. Routing spawn failures through the
                    # same except clauses as decode failures gives them the
                    # same counting/retry/kill treatment.
                    worker = self._ensure_worker()
                    result = worker.decode(path, edge=edge, deadline_ms=deadline_ms)
                    self.jobs += 1
                    return result
                except ProtocolViolation:
                    # Evidence of compromise (design doc sec 1/2.5): NO retry.
                    # worker.decode() already killed the worker (and a
                    # ProtocolViolation from spawn() -- e.g. a hello violation
                    # -- already closed itself internally, leaving `worker`
                    # None here); FIX 5 closes it again defensively (close() is
                    # idempotent) rather than relying invisibly on the callee's
                    # own kill path, then drop our reference so the next call
                    # spawns fresh rather than reusing a dead one.
                    self.protocol_violations += 1
                    if worker is not None:
                        worker.close()
                    self._worker = None
                    raise
                except DecodeServiceError as e:
                    if e.code not in (ErrorCode.TIMEOUT, ErrorCode.WORKER_CRASHED):
                        # Honest CORRUPT/UNSUPPORTED/TOO_LARGE: permanent for
                        # this file; the worker behaved correctly and stays
                        # alive and reusable.
                        raise
                    if e.code is ErrorCode.TIMEOUT:
                        self.timeouts += 1
                    else:
                        self.crashes += 1
                    # `worker` is None here when THIS DecodeServiceError came
                    # from _ensure_worker()/spawn() rather than worker.decode()
                    # -- spawn() already tore itself down on failure, so there
                    # is nothing left to close.
                    if worker is not None:
                        worker.close()
                    self._worker = None
                    if attempt == 1:
                        # Second TIMEOUT/WORKER_CRASHED in a row: permanent.
                        raise
                    # else: loop around -- _ensure_worker() spawns a fresh
                    # worker for the one allowed retry (design doc sec 1/2.5).
            raise AssertionError("unreachable: the loop above always returns or raises")

    def close(self) -> None:
        """Safe to call twice. Codex review (feat/i92.3-pool-hardening):
        takes _job_lock too -- an idempotent close() of the current worker
        must not race a live job's respawn (see the class docstring)."""
        with self._job_lock:
            if self._worker is not None:
                self._worker.close()
                self._worker = None


# ---------------------------------------------------------------------------
# DecodePoolSet: the multi-worker lease pool (fauxcasa-ez2.9 Stage 1, P1
# finding "WinDecodePool is single-slot..."). Composes N independent
# WinDecodePool instances for BATCH work (small 8 MiB arenas -- index-time
# decodes only need a 512px level, design doc sec 6) plus one reserved
# INTERACTIVE instance (a full 256 MiB arena, for the viewer/slideshow's
# full-resolution decode) -- see design doc sec 1 "plus one reserved
# interactive worker" and the lens finding recommending exactly this
# composition instead of widening WinDecodePool itself (which stays a
# single-slot class with its existing 12 tests untouched).

class DecodePoolSet:
    """Lease N batch WinDecodePool instances + 1 interactive one via
    queue.LifoQueue, keyed by `lane`. A batch lease (`lane="batch"`) can
    never take the interactive instance, and (P2 finding "arena lanes")
    an interactive lease (`lane="interactive"`) can never borrow a batch
    instance either -- batch members carry an 8 MiB arena
    (BATCH_ARENA_BYTES), far too small for the interactive lane's
    always-full-resolution (edge=0) decodes, so cross-lane borrowing used
    to fail those decodes TOO_LARGE whenever the reserved worker was
    busy. An interactive lease blocks/waits for the reserved instance
    instead. Spawn is LAZY: no worker process exists until the first
    lease() call actually
    needs one (inside `_ensure_worker`, called from the leasing thread) or
    until `warm()` is called explicitly to pre-spawn everything in
    parallel. Tolerates fewer batch workers than requested -- a spawn
    failure on one batch member (e.g. ERROR_COMMITMENT_LIMIT on an 8 MiB
    arena, unlikely, or any other spawn error) during warm() just drops
    that member from the pool rather than failing the whole set; an
    interactive-instance warm failure is NOT swallowed (see warm())."""

    BATCH_ARENA_BYTES = 8 * 2**20       # 8 MiB (design doc sec 6)
    INTERACTIVE_ARENA_BYTES = ARENA_DEFAULT_BYTES  # 256 MiB

    def __init__(self, n_batch: int, probe: bool = False,
                 profile_name: str = PROFILE_NAME,
                 mem_limit_bytes: int = DEFAULT_MEM_LIMIT_BYTES,
                 spawn_deadline_ms: int | None = DEFAULT_SPAWN_DEADLINE_MS) -> None:
        n_batch = max(1, n_batch)
        self._lock = threading.Lock()
        self._batch: list[WinDecodePool] = [
            WinDecodePool(arena_bytes=self.BATCH_ARENA_BYTES, probe=probe,
                          profile_name=profile_name, mem_limit_bytes=mem_limit_bytes,
                          spawn_deadline_ms=spawn_deadline_ms)
            for _ in range(n_batch)
        ]
        self._interactive = WinDecodePool(
            arena_bytes=self.INTERACTIVE_ARENA_BYTES, probe=probe,
            profile_name=profile_name, mem_limit_bytes=mem_limit_bytes,
            spawn_deadline_ms=spawn_deadline_ms)
        self._batch_free: "queue.LifoQueue[WinDecodePool]" = queue.LifoQueue()
        for p in self._batch:
            self._batch_free.put(p)
        self._interactive_free: "queue.LifoQueue[WinDecodePool]" = queue.LifoQueue()
        self._interactive_free.put(self._interactive)
        self._closed = False
        # P3 finding "release() doesn't validate membership": a double
        # release (or releasing a member warm() already dropped) used to
        # silently re-add a duplicate/dead pool to the free queue --
        # _job_lock prevented actual corruption but hid the bug. Tracked
        # by id() (WinDecodePool has no __eq__/__hash__ override, but id()
        # is unambiguous and avoids relying on that).
        self._in_use: set[int] = set()

    @property
    def batch_size(self) -> int:
        """Current batch pool member count (may shrink after warm())."""
        with self._lock:
            return len(self._batch)

    def warm(self) -> int:
        """Spawn every pool member's first worker in parallel threads
        (module helper for the "spawn N workers in parallel" perf item --
        module-level resolve() caching + one-time ACL grants make every
        spawn AFTER the first cheap). A batch member that fails to spawn
        is dropped from the pool (tolerate fewer workers than requested);
        the interactive member failing is re-raised -- the facade (not
        this class) decides what a failed interactive spawn means for
        session state (degraded). Returns the surviving batch size."""
        members = list(self._batch) + [self._interactive]
        errors: dict[int, BaseException] = {}

        def _warm_one(i: int, pool: WinDecodePool) -> None:
            try:
                with pool._job_lock:
                    pool._ensure_worker()
            except BaseException as e:  # noqa: BLE001 -- best-effort warm, collected below
                errors[i] = e

        threads = [threading.Thread(target=_warm_one, args=(i, p), daemon=True)
                   for i, p in enumerate(members)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        interactive_idx = len(members) - 1
        if interactive_idx in errors:
            raise errors[interactive_idx]

        with self._lock:
            surviving = [p for i, p in enumerate(self._batch) if i not in errors]
            if len(surviving) != len(self._batch):
                _log.info("DecodePoolSet.warm: %d/%d batch workers spawned "
                          "(dropped: %s)", len(surviving), len(self._batch),
                          {i: str(e) for i, e in errors.items() if i != interactive_idx})
                self._batch = surviving
                # P3 finding "warm() reassigns self._batch_free under the
                # lock while lease() reads it unlocked": a lease() call
                # already blocked on the OLD queue object would never wake
                # if warm() replaced self._batch_free outright. Drain and
                # refill the EXISTING queue in place instead.
                surviving_set = set(surviving)
                drained = []
                try:
                    while True:
                        drained.append(self._batch_free.get_nowait())
                except queue.Empty:
                    pass
                for p in drained:
                    if p in surviving_set:
                        self._batch_free.put(p)
            return len(self._batch)

    def lease(self, lane: str = "batch", timeout: float | None = None) -> WinDecodePool:
        if lane not in ("batch", "interactive"):
            raise ValueError(f"unknown lane {lane!r}")
        if lane == "batch":
            pool = self._batch_free.get(timeout=timeout)
        else:
            # interactive: ONLY the reserved instance (P2 finding "arena
            # lanes"). This lane is always edge=0/full-resolution
            # (decodefacade routes edge==0 here); a batch member's arena
            # is only BATCH_ARENA_BYTES (8 MiB), far too small for a
            # full-res decode -- borrowing across lanes used to make an
            # interactive decode fail TOO_LARGE, intermittently and
            # non-deterministically, whenever the reserved worker was
            # busy. Never borrow; block/wait for the reserved instance.
            pool = self._interactive_free.get(timeout=timeout)
        with self._lock:
            self._in_use.add(id(pool))
        return pool

    def release(self, pool: WinDecodePool) -> None:
        # P3 finding "release() doesn't validate membership": a double
        # release or a release of a member warm() already dropped from
        # the pool must not silently re-add a duplicate/dead pool to the
        # free queue -- log loudly and ignore instead.
        with self._lock:
            if id(pool) not in self._in_use:
                _log.error("DecodePoolSet.release: %r is not a currently "
                           "leased member (double release, or a member "
                           "warm() already dropped) -- ignoring", pool)
                return
            self._in_use.discard(id(pool))
        if pool is self._interactive:
            self._interactive_free.put(pool)
        else:
            self._batch_free.put(pool)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            batch = list(self._batch)
        for p in batch:
            p.close()
        self._interactive.close()


def spawn_worker(arena_bytes: int = ARENA_DEFAULT_BYTES, probe: bool = False,
                  profile_name: str = PROFILE_NAME,
                  mem_limit_bytes: int = DEFAULT_MEM_LIMIT_BYTES,
                  spawn_deadline_ms: int | None = DEFAULT_SPAWN_DEADLINE_MS) -> WinSandboxWorker:
    """Convenience: construct + spawn in one call (mainly for tests)."""
    worker = WinSandboxWorker(arena_bytes=arena_bytes, probe=probe,
                               profile_name=profile_name, mem_limit_bytes=mem_limit_bytes,
                               spawn_deadline_ms=spawn_deadline_ms)
    worker.spawn()
    return worker
