"""Windows sandboxed decode transport (fauxcasa-i92.3, Stage B).

Broker-side production home for the machinery proven by
docs/research/spikes/appcontainer-spawn-spike.py (see bd memory
win-appcontainer-decode-sandbox for the 6 load-bearing gotchas this
module encodes). Structured as a class the future pooled DecodeService
will own; nothing in the app imports this yet (see the design doc,
docs/design/decode-service.md sections 2 and 4).

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
import json
import math
import os
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

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
    protocol violation, never noise (see inline comment)."""
    header = _read_exact(fh, 4)
    if header is None:
        raise DecodeServiceError(
            ErrorCode.WORKER_CRASHED, "control pipe closed before any bytes (worker died before hello)")
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
            raise DecodeServiceError(
                ErrorCode.WORKER_CRASHED, "control pipe closed mid-frame while reading the first frame")
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
            raise ProtocolViolation(
                f"first framed control message was not valid JSON: {e!r}") from e
        if not isinstance(obj, dict):
            raise ProtocolViolation(
                f"first framed control message was not a JSON object: "
                f"{type(obj).__name__}")
        if obj.get("hello") != 1:
            raise ProtocolViolation(
                f"first framed control message was not a hello handshake: {obj!r}")
        return obj, 0

    noise = bytes(consumed)
    if b"\n" not in noise:
        extra = bytearray()
        while b"\n" not in extra:
            if len(noise) + len(extra) >= HELLO_RESYNC_MAX_NOISE:
                raise ProtocolViolation(
                    f"first {len(noise) + len(extra)} bytes of worker stdout were not "
                    f"a valid hello frame and contained no newline to resync past: "
                    f"{bytes(noise + extra)!r}")
            b = fh.read(1)
            if not b:
                raise DecodeServiceError(
                    ErrorCode.WORKER_CRASHED,
                    f"control pipe closed while discarding a leading diagnostic "
                    f"line (gotcha 7); {len(noise) + len(extra)} noise bytes so "
                    f"far: {bytes(noise + extra)!r}")
            extra += b
        noise = noise + bytes(extra)

    hello_msg = _read_frame(fh)  # strict retry -- no further tolerance
    if hello_msg.get("hello") != 1:
        raise ProtocolViolation(
            f"expected hello handshake after discarding {len(noise)} leading "
            f"noise bytes, got {hello_msg!r}")
    return hello_msg, len(noise)


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
        lat, lon = gps_raw
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (lat, lon)):
            raise ProtocolViolation(f"meta.gps element is not a real number: {gps_raw!r}")
        if not (math.isfinite(lat) and math.isfinite(lon)):
            raise ProtocolViolation(f"meta.gps element is not finite: {gps_raw!r}")
        gps = (float(lat), float(lon))

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
        name = _checked_str(f.get("name"), "face.name", max_bytes=MAX_META_ITEM_BYTES)
        coords: dict[str, float] = {}
        for key in ("x", "y", "w", "h"):
            v = f.get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ProtocolViolation(f"face.{key} is not a real number: {v!r}")
            if not math.isfinite(v) or not (0.0 <= v <= 1.0):
                raise ProtocolViolation(f"face.{key} not finite/in [0,1]: {v!r}")
            coords[key] = float(v)
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

    def sid_to_string(sid: LPVOID) -> str:
        ptr = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(ptr)):
            return "<unknown>"
        s = ptr.value
        kernel32.LocalFree(ptr)
        return s

    # -- ACL grant / revoke ----------------------------------------------

    def _set_file_dacl(path: str, sid: LPVOID, mode: int) -> None:
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
            ea.grfInheritance = SUB_CONTAINERS_AND_OBJECTS_INHERIT
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

    def grant_read_execute(path: str, sid: LPVOID) -> None:
        _set_file_dacl(path, sid, GRANT_ACCESS)

    def revoke(path: str, sid: LPVOID) -> None:
        try:
            _set_file_dacl(path, sid, REVOKE_ACCESS)
        except OSError:
            pass

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

    def resolve_worker_python() -> tuple[str, str]:
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
        before ever reaching the hello handshake)."""
        env_py = os.environ.get("FAUXCASA_WORKER_PYTHON")
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
        return worker_python, dirs["site"]

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

    def _spawn_appcontainer(worker_python: str, worker_script: str, sid: LPVOID,
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
        cmdline = f'"{worker_python}" "{worker_script}"'
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
                 mem_limit_bytes: int = DEFAULT_MEM_LIMIT_BYTES) -> None:
        self.arena_bytes = arena_bytes
        self.profile_name = profile_name
        self.mem_limit_bytes = mem_limit_bytes
        self._probe_enabled = probe
        self._child = None
        self._sid = None
        self._locked = False
        self._id_counter = 0
        self.hello: Hello | None = None
        self.hello_noise_bytes = 0  # see _read_hello_frame_tolerant (gotcha 7)
        self._arena_handle = None
        self._arena_addr: int | None = None
        self._winsta_grant: dict | None = None
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
        worker_script = str(Path(__file__).resolve().with_name("decodesvc_worker_win.py"))
        worker_dir = str(Path(worker_script).parent)
        base_dir = str(Path(worker_python).parent)

        sid = create_or_derive_profile(self.profile_name)
        self._sid = sid

        # Everything from here to a live child runs under one cleanup
        # guard: close() is idempotent, _child is still None, and any
        # failure (grant/winsta RuntimeError, arena WinError, spawn
        # OSError) must free the per-spawn SID -- and the arena, once
        # created -- or a retried spawn() overwrites and leaks them
        # (Codex review PR110 rounds 4+5).
        try:
            grant_targets = []
            for d in (base_dir, worker_pythonpath, worker_dir):
                if d and d not in grant_targets:
                    grant_targets.append(d)
            grant_errors = {}
            for d in grant_targets:
                try:
                    grant_read_execute(d, sid)
                except OSError as e:
                    grant_errors[d] = str(e)
            if grant_errors:
                raise RuntimeError(
                    f"ACL grant(s) failed, refusing to spawn a worker that cannot "
                    f"read its own runtime (gotcha 3): {grant_errors}")

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

            child = _spawn_appcontainer(worker_python, worker_script, sid,
                                         worker_pythonpath, extra_env, self.mem_limit_bytes)
        except Exception:
            self.close()
            raise
        self._child = child

        try:
            dup_arena = _duplicate_into_child(arena_handle, child.pi.hProcess)

            try:
                hello_msg, noise = _read_hello_frame_tolerant(child.out_file)
            except DecodeServiceError as e:
                # A worker that dies before hello almost always died in
                # loader/startup; the NTSTATUS exit code (0xC0000142 user32
                # init, 0xC0000135 DLL not found, 0xC0000022 access denied,
                # ...) plus the exe we actually spawned is the difference
                # between a debuggable CI failure and a shrug.
                kernel32.WaitForSingleObject(child.pi.hProcess, 2000)
                exit_code = wintypes.DWORD(0)
                kernel32.GetExitCodeProcess(child.pi.hProcess, ctypes.byref(exit_code))
                raise DecodeServiceError(
                    e.code,
                    f"{e} [worker exit code {exit_code.value:#010x}; "
                    f"exe {worker_python!r}; pythonpath {worker_pythonpath!r}]") from e
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
        except Exception:
            self.close()
            raise

        return self.hello

    def close(self) -> None:
        """Safe to call twice. Deliberately does NOT delete the
        AppContainer profile or revoke its ACL grants: the profile is
        reused across broker sessions (design doc sec 4)."""
        if self._child is not None:
            self._child.close()
            self._child = None
        self._locked = False
        if self._arena_addr:
            kernel32.UnmapViewOfFile(ctypes.c_void_p(self._arena_addr))
            self._arena_addr = None
        if self._arena_handle:
            kernel32.CloseHandle(self._arena_handle)
            self._arena_handle = None
        # The SID from create_or_derive_profile is a per-spawn native
        # allocation (MSDN: caller must FreeSid); the reusable AppContainer
        # *profile* is untouched. Only ever set inside spawn() (Windows-only),
        # so the advapi32 call is unreachable off-Windows.
        if self._sid is not None:
            advapi32.FreeSid(self._sid)
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
            raise OSError(f"CreateFileW({path}) failed err={ctypes.get_last_error()}")
        try:
            return _duplicate_into_child(h, self._child.pi.hProcess)
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
        whole gate suite (design doc sec 7 gate 1)."""
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
        try:
            resp = self.recv_response(timeout_ms=deadline_ms)
        except DecodeServiceError as e:
            # fauxcasa-i92.3.1: same TIMEOUT->kill semantics as decode();
            # any other DecodeServiceError (including ProtocolViolation,
            # a pre-existing gap this task does not widen) propagates
            # exactly as it did before deadline_ms existed.
            if e.code == ErrorCode.TIMEOUT:
                self.kill()
            raise
        if not isinstance(resp, dict):
            raise ProtocolViolation(f"probe response is not a JSON object: {resp!r}")
        # FIX 6 (P3, reviewer fix pass (fauxcasa-i92.3.x)): same type-strict
        # id guard as _validate_response_checks -- plain `!=` accepts JSON
        # true for 1 and 1.0 for an int req_id (Python numeric-tower
        # equality). Deliberately no counter/kill semantics here (unlike
        # _validate_response_checks): probe() PV raises also cover
        # harness-side mistakes like an unknown probe name, so blanket
        # counting would poison the escape-gate counter -- a follow-up
        # bead will decide that policy.
        rid = resp.get("id")
        if type(rid) is not int or rid != req_id:
            raise ProtocolViolation(f"probe response id {rid!r} != request id {req_id}")
        if resp.get("ok") is not True:
            raise ProtocolViolation(
                f"probe {name!r} did not run to completion (ok={resp.get('ok')!r}, "
                f"error={resp.get('error')!r}, detail={resp.get('detail')!r}) -- "
                f"a broken or unknown probe must never be treated as a denial")
        if resp.get("attempt") != name:
            raise ProtocolViolation(
                f"probe response attempt {resp.get('attempt')!r} != requested {name!r}")
        allowed = resp.get("allowed")
        if not isinstance(allowed, bool):
            raise ProtocolViolation(
                f"probe {name!r} response missing/non-bool 'allowed': {allowed!r}")
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
    that contract's counters actually need to live to survive a respawn)."""

    def __init__(self, arena_bytes: int = ARENA_DEFAULT_BYTES, probe: bool = False,
                 profile_name: str = PROFILE_NAME,
                 mem_limit_bytes: int = DEFAULT_MEM_LIMIT_BYTES) -> None:
        self.arena_bytes = arena_bytes
        self.probe = probe
        self.profile_name = profile_name
        self.mem_limit_bytes = mem_limit_bytes
        self._worker: WinSandboxWorker | None = None
        # Per-SESSION counters (design doc sec 1/2.5) -- survive respawns.
        self.protocol_violations = 0
        self.timeouts = 0
        self.crashes = 0

    def _ensure_worker(self) -> WinSandboxWorker:
        if self._worker is None:
            worker = WinSandboxWorker(
                arena_bytes=self.arena_bytes, probe=self.probe,
                profile_name=self.profile_name, mem_limit_bytes=self.mem_limit_bytes)
            worker.spawn()
            self._worker = worker
        return self._worker

    def decode(self, path: Path, edge: int = 0, deadline_ms: int = 20_000) -> DecodeResult:
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
                return worker.decode(path, edge=edge, deadline_ms=deadline_ms)
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
        """Safe to call twice."""
        if self._worker is not None:
            self._worker.close()
            self._worker = None


def spawn_worker(arena_bytes: int = ARENA_DEFAULT_BYTES, probe: bool = False,
                  profile_name: str = PROFILE_NAME,
                  mem_limit_bytes: int = DEFAULT_MEM_LIMIT_BYTES) -> WinSandboxWorker:
    """Convenience: construct + spawn in one call (mainly for tests)."""
    worker = WinSandboxWorker(arena_bytes=arena_bytes, probe=probe,
                               profile_name=profile_name, mem_limit_bytes=mem_limit_bytes)
    worker.spawn()
    return worker
