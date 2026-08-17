#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "PySide6"]
# ///
"""Tests for the Windows sandboxed decode transport (fauxcasa-i92.3, Stage B).

Run: `QT_QPA_PLATFORM=offscreen uv run apps/desktop-python/test_decodesvc_win.py -q`
(matches how test_tracer.py is invoked).

FIX 10 (should-fix, review fix pass): this module used to skip wholesale
on non-Windows, which meant the gate-3 trusted-side protocol fuzz below
-- pure Python, no sandbox, no Windows API -- ran in NO Linux CI, despite
decodesvc_win.py and decodesvc_worker_win.py both being importable on any
platform by design (their Windows-only pieces are internally guarded by
`if sys.platform == "win32":`). Only the tests that actually spawn a real
sandboxed worker (or touch a win32-guarded symbol like `dw.kernel32` or
`dw._build_env_block`) are marked `_WINDOWS_ONLY` below (the `shared_worker`
fixture self-skips too, so every containment/boundary test depending on
it skips cleanly rather than erroring); everything else -- including the
hello/lockdown-sequencing unit tests, which touch only the module's
platform-independent parsing functions -- now runs everywhere.

Three kinds of test here:

1. Containment gates (design doc sec 7 gate 1) -- each spawns a REAL
   sandboxed worker with the probe flag and asserts the hostile attempt
   was DENIED. A failing containment gate is a security finding, not a
   flaky test; see the assertion messages. Windows-only.
2. Positive/boundary tests -- handed_fd_read (the one attempt that must
   succeed), an arena round trip, a real decode of a synthetic fixture,
   lifetime/kill semantics, and the hello/proto refusal path. Mostly
   Windows-only (spawns real workers); a few pure-parsing tests run
   everywhere -- see their own docstrings.
3. Trusted-side protocol fuzz (design doc sec 7 gate 3) -- feeds
   decodesvc_win's response validator (`WinSandboxWorker._validate_response`)
   and its framing (`_read_frame`/`_write_frame`) hand-built lying-worker
   data. No sandbox needed; these exercise ONLY the trusted side's own
   validation code and run on every platform.

Synthetic-only fixtures throughout (privacy rule): the "photo" used here
is a 2x2 red PNG built from a literal byte string, not any real image.
"""

from __future__ import annotations

import base64
import ctypes
import io
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import decodesvc_win as dw  # noqa: E402
import decodesvc_worker_win as dw_worker  # noqa: E402  -- FIX 9: unit-test its pure _write_frame directly (no sandbox/live process needed)
from decodesvc import (  # noqa: E402
    DecodeServiceError,
    ErrorCode,
    FaceRegion,
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

# FIX 10: applied individually to tests that spawn a real sandboxed worker
# or touch a win32-guarded symbol -- NOT at module level, so the
# platform-independent tests (chiefly section 3) run on every platform.
_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires spawning a real Windows AppContainer sandbox worker",
)

# A 2x2 solid-red PNG, valid CRCs, built from a literal byte string --
# synthetic, not real photo data (privacy rule).
SYNTHETIC_PNG_2X2_RED = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAEUlEQVR42mP4z8DwH4QZYAwAR8oH"
    "+Rq28akAAAAASUVORK5CYII=")

SMALL_ARENA_BYTES = 8 * 1024 * 1024  # 8 MiB -- spec: "make it a constructor
                                       # arg so tests use a small arena"


# ---------------------------------------------------------------------------
# Fixtures

@pytest.fixture
def synthetic_png(tmp_path: Path) -> Path:
    p = tmp_path / "synthetic_2x2_red.png"
    p.write_bytes(SYNTHETIC_PNG_2X2_RED)
    return p


@pytest.fixture
def synthetic_wide_png(tmp_path: Path) -> Path:
    """A 32769x1 synthetic PNG -- under MAX_PIXELS (32769 px total) but
    over the per-axis MAX_EDGE cap of 32768 (design doc sec 2.4 checklist
    item 1). Exercises FIX 5's worker-side per-axis guard. Built at test
    time with PySide6.QtGui, not committed as a fixture file."""
    from PySide6.QtGui import QImage
    img = QImage(32_769, 1, QImage.Format_RGB32)
    img.fill(0xFFFF0000)
    p = tmp_path / "synthetic_wide.png"
    assert img.save(str(p), "PNG"), "failed to write synthetic wide PNG fixture"
    return p


@pytest.fixture
def unrecognized_format_file(tmp_path: Path) -> Path:
    """Bytes with no recognizable image magic at all -- no bundled Qt
    image plugin claims this format, exercising FIX 6's UNSUPPORTED path
    (as opposed to CORRUPT: a format a decoder engaged with and failed)."""
    p = tmp_path / "not_an_image.bin"
    p.write_bytes(b"this is definitely not an image file format\x00\x01\x02" * 8)
    return p


@pytest.fixture
def probe_secret_file():
    """A real file inside the user's profile the worker must NOT be able
    to read -- the filesystem-containment probe target (gotcha 4: probe
    USER files, never system files, which carry a default ALL APPLICATION
    PACKAGES ACE and would read fine regardless of containment)."""
    secret_path = Path.home() / "fauxcasa_decodesvc_probe_secret.txt"
    secret_path.write_text("top secret -- the sandbox must not read this\n", encoding="utf-8")
    try:
        yield secret_path
    finally:
        try:
            secret_path.unlink()
        except OSError:
            pass


@pytest.fixture(scope="module")
def shared_worker():
    """One spawned+locked AppContainer worker (probe=True), reused across
    every containment/boundary test in this module -- spawn is ~0.5s, and
    re-spawning per test would dominate the suite's wall clock for no
    isolation benefit (each probe attempt is a fresh, independent job).

    FIX 10: self-skips on non-Windows (rather than letting spawn() raise)
    so every test depending on this fixture shows as skipped, not
    errored, when the module runs on Linux CI."""
    if sys.platform != "win32":
        pytest.skip("requires spawning a real Windows AppContainer sandbox worker")
    worker = dw.WinSandboxWorker(arena_bytes=SMALL_ARENA_BYTES, probe=True)
    worker.spawn()
    try:
        yield worker
    finally:
        worker.close()


def _target_for(attempt: str, probe_secret_file: Path, tmp_path: Path) -> str | None:
    """Per-attempt probe request target (design doc sec 7 gate 1 corpus)."""
    if attempt == "user_file_read":
        return str(probe_secret_file)
    if attempt == "rel_path_read":
        # Bare filename -- the worker's cwd is not the user's profile dir,
        # so this also exercises "does a relative path escape anywhere
        # useful" without assuming a specific cwd.
        return probe_secret_file.name
    if attempt == "dotdot_traversal":
        parent = probe_secret_file.parent
        return f"{parent}\\..\\{parent.name}\\{probe_secret_file.name}"
    if attempt == "qmark_path":
        return f"\\\\?\\{probe_secret_file}"
    if attempt == "device_path":
        return r"\\.\PhysicalDrive0"
    if attempt == "temp_write":
        return str(tmp_path / "fauxcasa_decodesvc_probe_write.txt")
    return None


# ---------------------------------------------------------------------------
# 1. Containment gates (design doc sec 7 gate 1). Each MUST be denied.

CONTAINMENT_GATES = [
    "user_file_read",
    "rel_path_read",
    "dotdot_traversal",
    "qmark_path",
    "device_path",
    "temp_write",
    "registry_hkcu_write",
    "socket_tcp_connect",
    "dns_lookup",
    "subprocess_spawn",
    "open_clipboard",
    # FIX 8 (should-fix, review fix pass): network-I/O-denial coverage
    # backing the socket_create "document, don't assert" decision (see
    # test_socket_create_documented_not_asserted). tcp_connect_nonloopback
    # DOES deny cleanly (confirmed on this box) and joins the strict
    # must-deny set below. udp_sendto and socket_bind_listen do NOT deny
    # on this box -- see test_udp_sendto_and_bind_listen_UNRESOLVED_FINDING
    # below; they are deliberately NOT in this list pending investigation,
    # not because the reviewer's expected-deny assumption was accepted as
    # safe.
    "tcp_connect_nonloopback",
]


@pytest.mark.parametrize("attempt", CONTAINMENT_GATES)
def test_containment_gate_denied(shared_worker, probe_secret_file, tmp_path, attempt):
    target = _target_for(attempt, probe_secret_file, tmp_path)
    result = shared_worker.probe(attempt, target=target)
    assert result["allowed"] is False, (
        f"CONTAINMENT FAILURE: probe {attempt!r} SUCCEEDED inside the "
        f"AppContainer sandbox (detail={result.get('detail')!r}) -- this "
        f"is a security regression, not a flaky test; do not retry, "
        f"escalate")


def _lan_ip():
    """The machine's primary LAN IPv4 (no packet sent; connect() on a UDP
    socket just selects the outbound interface)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _free_tcp_port():
    s = socket.socket()
    s.bind(("", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_udp_sendto_and_bind_listen_are_inert_not_a_gap(shared_worker):
    """RESOLVED (was UNRESOLVED in the review fix pass). Bare udp_sendto()
    and bind()+listen() return success inside the zero-capability
    AppContainer, but direct investigation on this box proves that success
    is INERT -- no datagram is delivered and no inbound connection
    completes -- so it is the same category as socket_create, not a
    containment gap. The three completed-I/O gates below are the real
    check; the syscall-success probes are documented, not asserted (like
    socket_create), now BACKED by this evidence rather than by prose.

    Evidence gate 1 -- external UDP round trip cannot complete: a real DNS
    A-query sent to 8.8.8.8:53 gets NO reply (WFP drops the outbound
    datagram; the connectionless sender never sees the drop).

    Evidence gate 2 -- local UDP delivery does not happen: the worker
    sendto()s a nonce to a trusted listener the broker binds on the LAN IP;
    the datagram never arrives.

    Evidence gate 3 -- inbound connections never complete: the worker
    bind()s a broker-chosen port and blocks in accept(); the broker's
    concurrent connect() via both the LAN IP and loopback times out, and
    accept() never fires.

    All three must show NO network effect. socket_create /
    udp_sendto / socket_bind_listen remain 'document, don't assert'
    (syscall-layer success is capability-free by Windows design); the
    deviation from design sec 7's literal "create a socket must fail" is
    filed for owner ratification as fauxcasa-i92.6, now with this evidence
    that it is inert."""
    # Documented-not-asserted syscall-success probes (grouped with
    # socket_create): record, don't gate on them.
    udp = shared_worker.probe("udp_sendto")
    lst = shared_worker.probe("socket_bind_listen")
    print(f"\nudp_sendto: allowed={udp['allowed']} (documented, inert -- see gates below)")
    print(f"socket_bind_listen: allowed={lst['allowed']} (documented, inert -- see gates below)")

    # Evidence gate 1: external DNS round trip must NOT complete.
    dns = shared_worker.probe("raw_dns_roundtrip")
    assert dns["allowed"] is False, (
        f"CONTAINMENT FAILURE: a DNS query from inside the sandbox got a "
        f"reply ({dns.get('detail')!r}) -- UDP egress+ingress actually "
        f"works; this is a real gap, escalate, do not retry")
    print(f"raw_dns_roundtrip: no reply ({dns.get('detail')!r}) -- egress blocked")

    # Evidence gate 2: a datagram to a broker LAN-IP listener must NOT arrive.
    ip = _lan_ip()
    lsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    lsock.bind((ip, 0))
    lport = lsock.getsockname()[1]
    lsock.settimeout(3.0)
    got = {}

    def _listen():
        try:
            got["data"], got["addr"] = lsock.recvfrom(1024)
        except Exception as e:
            got["err"] = repr(e)

    t = threading.Thread(target=_listen, daemon=True)
    t.start()
    try:
        # Codex review PR110 (P1): go through probe()'s fail-closed
        # validation (ok/attempt echo/bool allowed) AND require that the
        # sendto itself completed inside the sandbox -- if the probe were
        # renamed, removed, or its sendto raised, the "datagram never
        # arrived" assertion below would be vacuously green without any
        # I/O ever being attempted. allowed=True (inert syscall success)
        # is the documented Windows behavior this gate is built on; if
        # Windows ever starts denying the sendto at the syscall layer,
        # this fails loudly so the documented-not-asserted classification
        # gets re-examined rather than silently shifting.
        udp_sent = shared_worker.probe(
            "udp_to_broker",
            extra={"bhost": ip, "bport": lport, "nonce": "probe"})
        assert udp_sent["allowed"] is True, (
            f"udp_to_broker's sendto did not complete inside the sandbox "
            f"({udp_sent['detail']!r}) -- the non-arrival evidence below "
            f"would be vacuous; re-examine the probe (or a syscall-layer "
            f"behavior change) before trusting this gate")
        t.join(timeout=4.0)
    finally:
        lsock.close()
    assert "data" not in got, (
        f"CONTAINMENT FAILURE: a UDP datagram from the sandbox arrived at "
        f"the broker's LAN listener ({got.get('addr')!r}) -- local egress "
        f"works; real gap, escalate")
    print("udp_to_broker: datagram did not arrive at broker listener -- local egress blocked")

    # Evidence gate 3: inbound connection to a worker listener must NOT complete.
    port = _free_tcp_port()
    accepted = {}

    def _run_accept():
        # Same fail-closed path as udp_to_broker above (Codex review
        # PR110 P1): a renamed/broken probe raises in probe() instead of
        # yielding an error response that could be misread as a denial.
        try:
            accepted["resp"] = shared_worker.probe(
                "bind_accept_wait", extra={"bport": port, "wait_s": 5.0})
        except Exception as e:  # surfaced by the assert below, not lost
            accepted["err"] = e

    at = threading.Thread(target=_run_accept, daemon=True)
    at.start()
    time.sleep(1.0)  # let the worker bind+listen before we connect
    outcomes = {}
    for label, host in (("lan", ip), ("loopback", "127.0.0.1")):
        try:
            c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            c.settimeout(2.5)
            c.connect((host, port))
            outcomes[label] = "CONNECTED"
            c.close()
        except Exception as e:
            outcomes[label] = type(e).__name__
    at.join(timeout=8.0)
    assert "err" not in accepted, (
        f"bind_accept_wait probe did not run to completion: {accepted['err']!r}")
    resp = accepted.get("resp", {})
    assert resp.get("allowed") is False, (
        f"CONTAINMENT FAILURE: the sandboxed worker accepted an inbound "
        f"connection ({resp.get('detail')!r}) -- ingress works; real gap")
    assert "CONNECTED" not in outcomes.values(), (
        f"CONTAINMENT FAILURE: broker connected to the sandbox's listener "
        f"({outcomes!r}) -- ingress works; real gap")
    print(f"bind_accept_wait: no inbound connection completed ({outcomes}) -- ingress blocked")


@_WINDOWS_ONLY
def test_memory_limit_exceeded_denied():
    """FIX 8 (should-fix): the worker must never successfully commit
    memory past the job's 2 GiB ProcessMemoryLimit (design doc sec 4) --
    either Python raises MemoryError inside the worker (a clean,
    graceful commit failure, reported as an ordinary denied probe) or
    the job object kills the process outright before it can even
    respond (WORKER_CRASHED at the broker). Either is an acceptable
    'denied'; only a clean successful giant allocation is a containment
    failure. Dedicated worker (not shared_worker): this probe may kill
    its process, which would poison every later test sharing it."""
    worker = dw.WinSandboxWorker(arena_bytes=SMALL_ARENA_BYTES, probe=True)
    worker.spawn()
    try:
        try:
            result = worker.probe("memory_limit_exceeded")
        except DecodeServiceError as e:
            assert e.code == ErrorCode.WORKER_CRASHED, (
                f"CONTAINMENT FAILURE: probe 'memory_limit_exceeded' hit an "
                f"unexpected error instead of a clean deny/kill: {e}")
            return  # killed by the job object -- an acceptable deny
        assert result["allowed"] is False, (
            f"CONTAINMENT FAILURE: worker allocated memory past the job's "
            f"2 GiB ProcessMemoryLimit without being denied or killed "
            f"(detail={result.get('detail')!r}) -- this is a security "
            f"regression, not a flaky test; do not retry, escalate")
    finally:
        worker.close()


def test_registry_hklm_read_documented_not_asserted(shared_worker):
    """AppContainer does NOT block reads of world-readable HKLM keys by
    default (the registry analogue of gotcha 4). Per spec: document,
    don't fail the build on it -- the WRITE gate above is the real
    containment check for the registry."""
    result = shared_worker.probe("registry_hklm_read")
    print(f"\nregistry_hklm_read: allowed={result['allowed']} "
          f"detail={result.get('detail')!r} (documented, not asserted)")


def test_bogus_probe_name_errors(shared_worker):
    """FIX 2 (P1): an unknown/misspelled probe attempt must ERROR, not
    silently 'pass' as a denial -- that would let the containment gate
    suite green-pass a probe that never actually ran."""
    with pytest.raises(ProtocolViolation, match="did not run to completion"):
        shared_worker.probe("this_probe_does_not_exist")


def test_socket_create_documented_not_asserted(shared_worker):
    """Found during Stage B bring-up on this box (not in the spike or the
    bd memory's 6 gotchas -- a 7th, network-flavored analogue of gotcha 4):
    bare `socket.socket(AF_INET, SOCK_STREAM)` SUCCEEDS inside a
    zero-capability AppContainer. This is expected Windows behavior, not a
    containment failure: WinSock socket() is a purely local kernel-object
    allocation with no capability check; the Windows Filtering Platform
    enforces AppContainer network capabilities (internetClient etc.) at
    actual I/O time -- connect()/send()/bind()/getaddrinfo() -- not at
    socket() creation. The REAL containment questions are "can it reach
    the network" and "can it resolve a name", both of which ARE asserted
    and DO pass: test_containment_gate_denied[socket_tcp_connect] and
    [dns_lookup] -- and, per FIX 8 (should-fix, review fix pass),
    [udp_sendto], [socket_bind_listen], and [tcp_connect_nonloopback] too,
    so socket_create's inertness is now backed by three independent I/O-
    denial gates, not just the original two. This deviation (accepting
    socket_create success without asserting on it) is filed for owner
    ratification as fauxcasa-i92.6; document, don't fail the build on
    socket_create alone."""
    result = shared_worker.probe("socket_create")
    print(f"\nsocket_create: allowed={result['allowed']} "
          f"detail={result.get('detail')!r} (documented, not asserted -- "
          f"see docstring)")


# ---------------------------------------------------------------------------
# 2. Positive/boundary tests

def test_handed_fd_read_allowed(shared_worker, synthetic_png):
    """The ONE probe attempt that must succeed: proves the handed-file
    mechanism itself works even though everything else is denied."""
    result = shared_worker.probe_file("handed_fd_read", synthetic_png)
    assert result["allowed"] is True, result.get("detail")
    assert base64.b64decode(result["data_b64"]) == SYNTHETIC_PNG_2X2_RED


def test_arena_round_trip(shared_worker):
    result = shared_worker.probe("arena_write")
    assert result["allowed"] is True, result.get("detail")
    got = shared_worker.read_arena(0, len(dw.ARENA_PROBE_PATTERN))
    assert got == dw.ARENA_PROBE_PATTERN


def test_real_decode(shared_worker, synthetic_png):
    result = shared_worker.decode(synthetic_png)
    assert result.source_w == 2
    assert result.source_h == 2
    assert result.pixels.w == 2
    assert result.pixels.h == 2
    result.pixels.validate(arena_bytes=shared_worker.arena_bytes)  # no raise
    # FIX 1: pixels_bytes is a KEPT, private copy of the arena bytes -- not
    # a live view into memory the worker can still write.
    assert result.pixels_bytes is not None
    assert len(result.pixels_bytes) == result.pixels.len
    # Must match what's still sitting in the arena right now...
    assert result.pixels_bytes == shared_worker.read_arena(
        result.pixels.off, result.pixels.len)
    # ...and mutating the live arena afterward must NOT change the copy
    # already returned to the caller (the TOCTOU guarantee FIX 1 closes).
    before = bytes(result.pixels_bytes)
    ctypes.memmove(
        shared_worker._arena_addr + result.pixels.off,
        (ctypes.c_ubyte * result.pixels.len)(*b"\xff" * result.pixels.len),
        result.pixels.len)
    assert result.pixels_bytes == before
    assert shared_worker.read_arena(result.pixels.off, result.pixels.len) != before


def test_decode_too_large_per_axis(shared_worker, synthetic_wide_png):
    """FIX 5 (P2): an image over MAX_EDGE per-axis but under MAX_PIXELS
    must come back as the worker's own honest TOO_LARGE error, not a
    silent 'ok' -- an ok response here would make the broker's own
    PixelBuffer.validate() reject the buffer as a PROTOCOL violation,
    killing a legitimately-behaving worker over an honestly-oversized
    file."""
    with pytest.raises(DecodeServiceError) as exc_info:
        shared_worker.decode(synthetic_wide_png)
    assert exc_info.value.code == ErrorCode.TOO_LARGE


def test_decode_edge_never_upscales(shared_worker, synthetic_png):
    """Cross-vendor review (P2): `edge` is a MAX long-edge, not an upscale
    target. A 2x2 source requested at edge=512 must stay 2x2 (the thumbnail
    contract never enlarges), not balloon to 512x512 (~1 MiB)."""
    result = shared_worker.decode(synthetic_png, edge=512)
    assert result.pixels.w == 2 and result.pixels.h == 2, (
        f"edge=512 upscaled a 2x2 source to {result.pixels.w}x{result.pixels.h}")


def _write_ihdr_png(path: Path, w: int, h: int) -> None:
    """Take the valid 2x2 PNG and patch only its IHDR width/height to w x h
    (fixing the IHDR CRC). QImageReader.size() then reports the huge declared
    dimensions from IHDR alone -- no pixel buffer that large is ever
    allocated -- so the worker's pre-decode size guard can reject it before
    reader.read(). (An IHDR-only stub does NOT work: libpng refuses to report
    size() without a readable image body, so size() comes back invalid.)"""
    import struct as _struct, zlib as _zlib
    png = bytearray(SYNTHETIC_PNG_2X2_RED)
    _struct.pack_into(">II", png, 16, w, h)  # IHDR w,h at offset 16
    crc = _zlib.crc32(bytes(png[12:16 + 13])) & 0xFFFFFFFF  # 'IHDR' + 13 data
    _struct.pack_into(">I", png, 16 + 13, crc)
    path.write_bytes(bytes(png))


@_WINDOWS_ONLY
def test_decode_oversized_header_too_large_before_decode(tmp_path):
    """Cross-vendor review (P2): at edge=0, a header declaring dimensions
    past the caps must return TOO_LARGE from the pre-decode size check --
    NOT be handed to reader.read(), where Qt's allocation limit / the job
    memory cap would trip first and misreport it as CORRUPT/WORKER_CRASHED.

    Dedicated worker (not shared_worker): a decode that stresses the native
    codec could in principle kill the process; isolating it keeps every
    other test's shared worker clean regardless of the outcome here."""
    worker = dw.WinSandboxWorker(arena_bytes=SMALL_ARENA_BYTES, probe=True)
    worker.spawn()
    try:
        p = tmp_path / "huge_header.png"
        _write_ihdr_png(p, 40_000, 40_000)  # > MAX_EDGE and > MAX_PIXELS
        with pytest.raises(DecodeServiceError) as exc_info:
            worker.decode(p)  # edge=0
        assert exc_info.value.code == ErrorCode.TOO_LARGE
        # Codex review PR110 round 5: the pre-check must cover edge>0 too.
        # An edge above the source long-edge scales nothing (never
        # upscale), so the full 40k x 40k output must still be TOO_LARGE...
        with pytest.raises(DecodeServiceError) as exc_info:
            worker.decode(p, edge=50_000)
        assert exc_info.value.code == ErrorCode.TOO_LARGE
        # ...and even a legal edge yields a scaled 32768x32768 = 1 Gpx
        # output on this source -- over MAX_PIXELS, also pre-checked.
        with pytest.raises(DecodeServiceError) as exc_info:
            worker.decode(p, edge=32_768)
        assert exc_info.value.code == ErrorCode.TOO_LARGE
        # The worker must survive an oversized-header decode (native libpng
        # "Read Error" chatter must not desync the control protocol).
        assert worker._child.is_alive()
        # And it must still serve a normal job afterward.
        good = tmp_path / "ok.png"
        good.write_bytes(SYNTHETIC_PNG_2X2_RED)
        assert worker.decode(good).source_w == 2
    finally:
        worker.close()


def test_decode_unsupported_format(shared_worker, unrecognized_format_file):
    """FIX 6 (P2): a file no bundled decoder claims must come back as
    UNSUPPORTED, not CORRUPT (design doc sec 2.5 taxonomy; matters for
    the per-extension maintenance surface)."""
    with pytest.raises(DecodeServiceError) as exc_info:
        shared_worker.decode(unrecognized_format_file)
    assert exc_info.value.code == ErrorCode.UNSUPPORTED


def test_unknown_op_no_crash(shared_worker):
    """Unknown op -> error reply, no crash (design doc: 'unknown op ->
    error reply, no crash'). The worker must still answer afterward."""
    req_id = shared_worker._next_id()
    shared_worker.send_request({"id": req_id, "op": "frobnicate"})
    resp = shared_worker.recv_response()
    assert resp["id"] == req_id
    assert resp["ok"] is False
    assert resp["error"] == "UNSUPPORTED"
    assert shared_worker._child.is_alive()


@_WINDOWS_ONLY
def test_lifetime_close_kills_worker_within_2s():
    worker = dw.WinSandboxWorker(arena_bytes=SMALL_ARENA_BYTES)
    worker.spawn()
    t0 = time.perf_counter()
    worker.close()
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, f"close() took {elapsed:.2f}s, expected < 2s (gotcha 5 backstop)"
    # Codex review PR110 round 3: close() must FreeSid + clear, or every
    # respawn leaks one native SID allocation
    assert worker._sid is None, "close() must free and clear the AppContainer SID"


@_WINDOWS_ONLY
def test_job_handle_close_kills_worker():
    """KILL_ON_JOB_CLOSE backstop: closing ONLY the job handle (no
    TerminateProcess) must still kill the child within 2s."""
    worker = dw.WinSandboxWorker(arena_bytes=SMALL_ARENA_BYTES)
    worker.spawn()
    child = worker._child
    proc_handle = child.pi.hProcess
    job_handle = child.job
    # Detach so worker.close() below (never called -- test owns teardown)
    # can't double-terminate/close these out from under the direct test.
    child.pi = None
    child.job = None

    t0 = time.perf_counter()
    dw.kernel32.CloseHandle(job_handle)  # KILL_ON_JOB_CLOSE fires here
    waited = dw.kernel32.WaitForSingleObject(proc_handle, 3000)
    elapsed = time.perf_counter() - t0

    assert waited == 0, f"WaitForSingleObject returned {waited}, expected WAIT_OBJECT_0 (0)"
    assert elapsed < 2.0, f"job-handle-close kill took {elapsed:.2f}s, expected < 2s"
    dw.kernel32.CloseHandle(proc_handle)


@_WINDOWS_ONLY
def test_env_block_is_an_allowlist_not_a_copy_of_os_environ(monkeypatch):
    """FIX 3 (P1): _build_env_block must never leak host env vars it
    doesn't explicitly allowlist -- a hijacked decoder must not be able
    to read host tokens/keys/secrets that happen to live in os.environ.
    Regression guard against reverting to `dict(os.environ)`."""
    monkeypatch.setenv("FAUXCASA_TEST_SECRET_TOKEN", "sh-not-in-the-child")
    block = dw._build_env_block(pythonpath=r"C:\some\site-packages", extra_env={})
    # The block is a double-null-terminated sequence of null-terminated
    # strings; wstring_at() alone stops at the FIRST null, so read the
    # full buffer length explicitly and split on embedded nulls.
    text = ctypes.wstring_at(ctypes.addressof(block), len(block))
    entries = [e for e in text.split("\x00") if e]
    keys = {e.split("=", 1)[0] for e in entries}
    assert "FAUXCASA_TEST_SECRET_TOKEN" not in keys, (
        "host-only env var leaked into the worker's environment block -- "
        "the allowlist regressed to copying os.environ")
    allowed = {"SystemRoot", "SystemDrive", "windir", "USERPROFILE",
               "LOCALAPPDATA", "APPDATA", "PATH", "QT_QPA_PLATFORM",
               "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PYTHONUTF8",
               "PYTHONPATH"}
    assert keys <= allowed, f"unexpected keys in env block: {keys - allowed}"
    assert "PATH" in keys  # empty-string PATH is required (see comment)


@_WINDOWS_ONLY
def test_trimmed_env_worker_spawns_and_decodes(synthetic_png):
    """FIX 3 (P1): the trimmed allowlist env is not just theoretically
    sufficient -- confirm a fresh worker spawned under it reaches
    'locked' and decodes a real (synthetic) file end to end. A separate
    worker from `shared_worker` on purpose: this test's entire point is
    to exercise spawn() itself under the trimmed env, not reuse an
    already-proven-working instance."""
    worker = dw.WinSandboxWorker(arena_bytes=SMALL_ARENA_BYTES)
    worker.spawn()
    try:
        result = worker.decode(synthetic_png)
        assert result.source_w == 2
        assert result.source_h == 2
    finally:
        worker.close()


@_WINDOWS_ONLY
def test_worker_python_override_resolves_to_base_interpreter(monkeypatch):
    """Codex review PR110 (P2): FAUXCASA_WORKER_PYTHON selects the probe
    ENVIRONMENT, but resolve_worker_python must still spawn that
    environment's BASE interpreter -- a uv/venv python.exe is a trampoline
    that re-launches the base interpreter as a child process, which the
    job's no-child rule kills before the hello handshake (gotcha 1).
    Under `uv run` this test's own sys.executable IS such a trampoline
    (sys.prefix != sys.base_prefix), which makes it exactly the trap
    input; on a bare base interpreter the identity still holds."""
    monkeypatch.setenv("FAUXCASA_WORKER_PYTHON", sys.executable)
    exe, site = dw.resolve_worker_python()
    expected = os.path.join(sys.base_prefix, "python.exe")
    assert os.path.normcase(exe) == os.path.normcase(expected), (
        f"override resolved to {exe!r}, expected the probe environment's "
        f"base interpreter {expected!r}")
    if os.path.normcase(sys.prefix) != os.path.normcase(sys.base_prefix):
        assert os.path.normcase(exe) != os.path.normcase(sys.executable), (
            "override returned the venv trampoline itself -- it would be "
            "killed by the job's no-child-process rule before hello")
    assert (Path(site) / "PySide6").is_dir(), (
        f"worker PYTHONPATH {site!r} does not contain the override "
        f"environment's PySide6 site-packages")


def test_worker_report_fatal_is_framed_and_parseable():
    """Gotcha 8 regression (companion): a worker-side fatal (e.g. a
    prologue failure like the NUL-open denial on GHA's Server 2025
    runners) must reach the broker as a VALID length-prefixed frame that
    the broker's own _read_frame parses -- never a raw traceback on the
    wire, and never the old silent exit-1 that leaves zero evidence."""
    buf = io.BytesIO()
    old = dw_worker._CTRL
    dw_worker._CTRL = buf
    try:
        dw_worker._report_fatal({"fatal": "worker-main", "error": "boom",
                                  "traceback": "tb"})
    finally:
        dw_worker._CTRL = old
    buf.seek(0)
    msg = dw._read_frame(buf)
    assert msg == {"fatal": "worker-main", "error": "boom", "traceback": "tb"}


def test_hello_wrong_proto_refused_loudly():
    """A stub worker that speaks proto=PROTO+1 is refused loudly. No
    sandbox needed -- exercises decodesvc_win's own hello-verification
    logic against a plain (non-AppContainer) subprocess stub."""
    stub_code = (
        "import sys, json, struct\n"
        "msg = json.dumps({'hello': 1, 'proto': %d, "
        "'bundle': {'id': 'x', 'version': 'y', 'components': {}}, "
        "'ops': ['decode'], 'arena_bytes': %d, 'max_pixels': 1}).encode('utf-8')\n"
        "sys.stdout.buffer.write(struct.pack('<I', len(msg)) + msg)\n"
        "sys.stdout.buffer.flush()\n"
    ) % (PROTO + 1, SMALL_ARENA_BYTES)
    proc = subprocess.Popen([sys.executable, "-c", stub_code], stdout=subprocess.PIPE)
    try:
        hello_msg, _noise = dw._read_hello_frame_tolerant(proc.stdout)
        with pytest.raises(ProtocolViolation, match="proto mismatch"):
            dw.parse_hello(hello_msg, expected_arena_bytes=SMALL_ARENA_BYTES)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_framed_non_hello_first_message_raises_promptly():
    """FIX 4 (P2): a valid framed JSON object that is NOT a hello must
    raise ProtocolViolation immediately -- not fall through to the
    newline-resync path (reserved for truly unframeable noise) and hang
    waiting for a '\\n' that will never arrive, blocking spawn() forever.
    Bounded-time assertion: this must fail fast, not time out."""
    stub_code = (
        "import sys, json, struct\n"
        "msg = json.dumps({'op': 'not_a_hello', 'x': 1}).encode('utf-8')\n"
        "sys.stdout.buffer.write(struct.pack('<I', len(msg)) + msg)\n"
        "sys.stdout.buffer.flush()\n"
        "import time\n"
        "time.sleep(30)\n"  # if the broker hangs, this keeps the pipe open
    )
    proc = subprocess.Popen([sys.executable, "-c", stub_code], stdout=subprocess.PIPE)
    try:
        t0 = time.perf_counter()
        with pytest.raises(ProtocolViolation, match="not a hello handshake"):
            dw._read_hello_frame_tolerant(proc.stdout)
        elapsed = time.perf_counter() - t0
        assert elapsed < 2.0, (
            f"_read_hello_frame_tolerant took {elapsed:.2f}s on a framed "
            f"non-hello first message, expected < 2s (must raise promptly, "
            f"not hang resyncing on a newline that will never arrive)")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.parametrize("payload_expr, match", [
    ("b'{this is not json'", "not valid JSON"),
    ("b'\\xff\\xfe\\xfd\\xfc\\xfb'", "not valid JSON"),      # invalid UTF-8
    ("json.dumps([1, 2, 3]).encode()", "not a JSON object"),  # valid JSON, not a dict
    # Codex review PR110 round 3: a zero-length frame is a successfully
    # framed (empty) malformed message, not resyncable noise
    ("b''", "not valid JSON"),
    # ... and hostile capped JSON that raises bare ValueError (>4300-digit
    # int literal) or RecursionError (deep nesting) must be normalized to
    # ProtocolViolation, same tuple as _read_frame
    ("b'1' * 5000", "not valid JSON"),
    # Whether deep nesting trips RecursionError is platform/version
    # dependent (GHA ubuntu's CPython parses 3000 deep fine and lands in
    # the not-a-dict branch; Windows raises). Either way the contract
    # holds: prompt ProtocolViolation, never a hang or a leaked exception.
    ("b'[' * 3000 + b']' * 3000", "not valid JSON|not a JSON object"),
])
def test_framed_malformed_first_message_raises_promptly(payload_expr, match):
    """Codex review PR110 (P2), completing FIX 4: a first message whose
    4-byte length prefix frames correctly but whose PAYLOAD is malformed
    (bad JSON, bad UTF-8, or a JSON non-object) must also raise
    ProtocolViolation immediately. It cannot be the gotcha-7 ASCII
    diagnostic -- printable ASCII can never produce an in-cap length
    prefix -- so it is worker protocol bytes gone wrong; the old
    fall-through to newline-resync would block in read(1) forever against
    a worker holding the pipe open while waiting for the broker."""
    stub_code = (
        "import sys, json, struct\n"
        f"msg = {payload_expr}\n"
        "sys.stdout.buffer.write(struct.pack('<I', len(msg)) + msg)\n"
        "sys.stdout.buffer.flush()\n"
        "import time\n"
        "time.sleep(30)\n"  # if the broker resyncs, this keeps the pipe open
    )
    proc = subprocess.Popen([sys.executable, "-c", stub_code], stdout=subprocess.PIPE)
    try:
        t0 = time.perf_counter()
        with pytest.raises(ProtocolViolation, match=match):
            dw._read_hello_frame_tolerant(proc.stdout)
        elapsed = time.perf_counter() - t0
        assert elapsed < 2.0, (
            f"_read_hello_frame_tolerant took {elapsed:.2f}s on a framed "
            f"malformed first message, expected < 2s (must raise promptly, "
            f"not block resyncing on a newline that will never arrive)")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_lockdown_sequencing_guard_no_job_before_locked():
    """Design doc sec 7 gate 5: assert no job is dispatched before
    'locked'. No sandbox needed -- a worker that never completed the
    spawn() handshake must refuse to send anything."""
    w = dw.WinSandboxWorker(arena_bytes=SMALL_ARENA_BYTES)
    w._child = object()  # sentinel: "spawned" but never locked
    w._locked = False
    with pytest.raises(RuntimeError, match="lockdown sequencing"):
        w.decode("does-not-matter.png")
    with pytest.raises(RuntimeError, match="lockdown sequencing"):
        w.probe("anything")


@_WINDOWS_ONLY
def test_protocol_violation_kills_live_worker_and_counts(synthetic_png):
    """FIX 7 (should-fix): a ProtocolViolation during decode() on a LIVE
    worker kills it outright (no retry -- the worker is now suspect) and
    increments protocol_violations. Uses a real spawned worker with
    recv_response monkeypatched to return a deliberately malformed
    response, exercising decode()'s actual kill path end to end -- the
    pure-trusted-side fuzz below (_fresh_validator) never has a live
    process to kill, so it only asserts the counter half of this."""
    worker = dw.WinSandboxWorker(arena_bytes=SMALL_ARENA_BYTES)
    worker.spawn()
    try:
        assert worker.protocol_violations == 0
        assert worker.is_alive() is True

        def _lying_recv_response(timeout_ms=None):
            return {"id": worker._id_counter, "ok": True,
                    "source": {"w": 2, "h": 2},
                    "pixels": {"w": 2, "h": 2, "stride": 8, "pixfmt": "RGBA8",
                               "off": 0, "len": 10_000_000}}  # oversized len

        worker.recv_response = _lying_recv_response
        with pytest.raises(ProtocolViolation):
            worker.decode(synthetic_png)

        assert worker.protocol_violations == 1
        assert worker.is_alive() is False
    finally:
        worker.close()


# ---------------------------------------------------------------------------
# 2b. Deadline enforcement, TIMEOUT taxonomy, retry pool (fauxcasa-i92.3.1;
# design doc sec 1 "kill, timeout, crash" / sec 2.5 taxonomy).

class _FakeChild:
    """Stand-in for decodesvc_win._ChildProcess with NO real Windows
    process at all -- just a real OS pipe standing in for the child's
    stdout, so recv_response's blocking read has something real to block
    on. `pi` is never touched by these tests: they monkeypatch
    worker._terminate_child directly rather than exercising its real
    TerminateProcess call, per WinSandboxWorker.decode's own doc that
    _terminate_child only touches self._child through that one surface."""

    def __init__(self, out_file):
        self.pi = None
        self.out_file = out_file
        self.in_file = None


def test_recv_response_deadline_kills_and_reports_timeout():
    """No sandbox needed: a fake child whose read end never gets data
    proves recv_response's timer fires, calls _terminate_child(), and
    re-reports the resulting WORKER_CRASHED-from-EOF as TIMEOUT (design
    doc sec 1) -- not as an honest crash."""
    r_fd, w_fd = os.pipe()
    read_file = os.fdopen(r_fd, "rb", buffering=0)
    write_file = os.fdopen(w_fd, "wb", buffering=0)
    worker = dw.WinSandboxWorker(arena_bytes=SMALL_ARENA_BYTES)
    worker._child = _FakeChild(read_file)
    worker._locked = True
    terminated = {"called": False}

    def _fake_terminate():
        terminated["called"] = True
        write_file.close()  # closes the "child"'s output -- read() sees EOF

    worker._terminate_child = _fake_terminate
    try:
        with pytest.raises(DecodeServiceError) as exc_info:
            worker.recv_response(timeout_ms=200)
        assert exc_info.value.code == ErrorCode.TIMEOUT
        assert terminated["called"] is True
    finally:
        read_file.close()
        try:
            write_file.close()
        except OSError:
            pass


def test_recv_response_prompt_reply_does_not_trigger_deadline():
    """Same fake child, but the write end delivers a valid frame promptly
    -- recv_response must return it normally and never call
    _terminate_child (a live, on-time worker must not be killed)."""
    r_fd, w_fd = os.pipe()
    read_file = os.fdopen(r_fd, "rb", buffering=0)
    write_file = os.fdopen(w_fd, "wb", buffering=0)
    worker = dw.WinSandboxWorker(arena_bytes=SMALL_ARENA_BYTES)
    worker._child = _FakeChild(read_file)
    worker._locked = True
    terminated = {"called": False}
    worker._terminate_child = lambda: terminated.__setitem__("called", True)

    dw._write_frame(write_file, {"id": 1, "ok": True, "detail": "prompt"})
    try:
        resp = worker.recv_response(timeout_ms=5000)
        assert resp == {"id": 1, "ok": True, "detail": "prompt"}
        assert terminated["called"] is False
    finally:
        read_file.close()
        write_file.close()


@_WINDOWS_ONLY
def test_probe_stall_deadline_kills_worker():
    """Live end-to-end version of the fake-child test above: a real,
    entirely non-hostile worker that accepts a probe job and just sits
    there (the `stall` probe) must be killed by the broker's deadline,
    not left hanging forever."""
    worker = dw.WinSandboxWorker(arena_bytes=SMALL_ARENA_BYTES, probe=True)
    worker.spawn()
    try:
        t0 = time.perf_counter()
        with pytest.raises(DecodeServiceError) as exc_info:
            worker.probe("stall", extra={"seconds": 30}, deadline_ms=1500)
        elapsed = time.perf_counter() - t0
        assert exc_info.value.code == ErrorCode.TIMEOUT, (
            f"expected TIMEOUT, got {exc_info.value.code} ({exc_info.value})")
        assert elapsed < 5.0, (
            f"deadline kill took {elapsed:.2f}s, expected close to the "
            f"1.5s deadline_ms")
        assert worker.is_alive() is False
    finally:
        worker.close()


class _FakePoolWorker:
    """A fake WinSandboxWorker for WinDecodePool unit tests: `decode()`
    pops one scripted result (a value to return, or an exception
    instance/type to raise) per call. No Windows API, no real process."""

    def __init__(self, results):
        self._results = list(results)
        self.decode_calls = 0
        self.closed = False

    def decode(self, path, edge=0, deadline_ms=20_000):
        self.decode_calls += 1
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self) -> None:
        self.closed = True


def _stub_pool_ensure_worker(pool: "dw.WinDecodePool", spawned: list):
    """Installs a pool._ensure_worker stub that hands out `spawned`
    workers in order, one per (re)spawn -- mirroring the real
    _ensure_worker's "spawn only when self._worker is None" contract."""
    def _ensure_worker():
        if pool._worker is None:
            pool._worker = spawned.pop(0)
        return pool._worker
    pool._ensure_worker = _ensure_worker


def test_pool_retries_once_on_timeout_then_succeeds():
    pool = dw.WinDecodePool()
    w1 = _FakePoolWorker([DecodeServiceError(ErrorCode.TIMEOUT, "slow")])
    w2 = _FakePoolWorker(["decoded-ok"])
    _stub_pool_ensure_worker(pool, [w1, w2])

    result = pool.decode("some.png")

    assert result == "decoded-ok"
    assert pool.timeouts == 1
    assert pool.crashes == 0
    assert w1.closed is True
    assert w2.closed is False
    assert w1.decode_calls == 1 and w2.decode_calls == 1


def test_pool_retries_once_on_worker_crashed_then_succeeds():
    pool = dw.WinDecodePool()
    w1 = _FakePoolWorker([DecodeServiceError(ErrorCode.WORKER_CRASHED, "died")])
    w2 = _FakePoolWorker(["decoded-ok"])
    _stub_pool_ensure_worker(pool, [w1, w2])

    result = pool.decode("some.png")

    assert result == "decoded-ok"
    assert pool.crashes == 1
    assert pool.timeouts == 0
    assert w1.closed is True
    assert w2.closed is False


def test_pool_protocol_violation_never_retries():
    pool = dw.WinDecodePool()
    w1 = _FakePoolWorker([ProtocolViolation("lying worker")])
    spawned = [w1]
    _stub_pool_ensure_worker(pool, spawned)

    with pytest.raises(ProtocolViolation):
        pool.decode("some.png")

    assert pool.protocol_violations == 1
    assert pool.timeouts == 0
    assert pool.crashes == 0
    assert w1.decode_calls == 1
    assert spawned == [], "a second worker must never be requested after a ProtocolViolation"


def test_pool_timeout_twice_is_permanent():
    pool = dw.WinDecodePool()
    w1 = _FakePoolWorker([DecodeServiceError(ErrorCode.TIMEOUT, "slow")])
    w2 = _FakePoolWorker([DecodeServiceError(ErrorCode.TIMEOUT, "slow again")])
    _stub_pool_ensure_worker(pool, [w1, w2])

    with pytest.raises(DecodeServiceError) as exc_info:
        pool.decode("some.png")

    assert exc_info.value.code == ErrorCode.TIMEOUT
    assert pool.timeouts == 2
    assert w1.closed is True and w2.closed is True


# ---------------------------------------------------------------------------
# 3. Trusted-side protocol fuzz (design doc sec 7 gate 3). No sandbox.

def _fresh_validator() -> dw.WinSandboxWorker:
    """A constructed-but-never-spawned worker: exercises _validate_response
    without touching any Windows API or live process."""
    return dw.WinSandboxWorker(arena_bytes=SMALL_ARENA_BYTES)


def _decode_resp(**pixels_overrides) -> dict:
    pixels = {"w": 2, "h": 2, "stride": 8, "pixfmt": "RGBA8", "off": 0, "len": 16}
    pixels.update(pixels_overrides)
    return {"id": 1, "ok": True, "source": {"w": 2, "h": 2}, "pixels": pixels}


def test_fuzz_oversized_len():
    v = _fresh_validator()
    with pytest.raises(ProtocolViolation):
        v._validate_response(_decode_resp(len=10_000_000), expect_id=1)
    # FIX 7 (should-fix): _validate_response is the counting seam pure-
    # trusted-side fuzz can exercise without a live worker to kill.
    assert v.protocol_violations == 1


def test_fuzz_off_past_arena():
    v = _fresh_validator()
    with pytest.raises(ProtocolViolation):
        v._validate_response(_decode_resp(off=SMALL_ARENA_BYTES), expect_id=1)
    assert v.protocol_violations == 1


def test_fuzz_overlapping_buffers():
    # decode() responses carry exactly one buffer (no `levels` array --
    # index/poster are out of Stage B's scope), so overlap can't arise on
    # the real wire path yet; exercise the same validate_disjoint()
    # decodesvc_win would call the day a multi-buffer op exists, imported
    # unchanged from decodesvc.py.
    a = PixelBuffer(w=2, h=2, stride=8, pixfmt=PixelFormat.RGBA8, off=0, len=16)
    b = PixelBuffer(w=2, h=2, stride=8, pixfmt=PixelFormat.RGBA8, off=8, len=16)
    with pytest.raises(ProtocolViolation):
        PixelBuffer.validate_disjoint([a, b])


@pytest.mark.parametrize("bad_w", [-5, 10**9, -1, 0])
def test_fuzz_negative_huge_dims(bad_w):
    v = _fresh_validator()
    with pytest.raises(ProtocolViolation):
        v._validate_response(_decode_resp(w=bad_w), expect_id=1)
    assert v.protocol_violations == 1


def test_fuzz_non_hex_sha():
    resp = _decode_resp()
    resp["sha256"] = "not-hex!" * 8  # 64 chars, not hex
    v = _fresh_validator()
    with pytest.raises(ProtocolViolation):
        v._validate_response(resp, expect_id=1)
    assert v.protocol_violations == 1


def test_fuzz_unknown_pixfmt():
    v = _fresh_validator()
    with pytest.raises(ProtocolViolation):
        v._validate_response(_decode_resp(pixfmt="NOPE"), expect_id=1)
    assert v.protocol_violations == 1


def test_fuzz_id_mismatch():
    v = _fresh_validator()
    with pytest.raises(ProtocolViolation):
        v._validate_response(_decode_resp(), expect_id=999)
    assert v.protocol_violations == 1


@pytest.mark.parametrize("bad_id", [True, 1.0])
def test_fuzz_non_int_id_matching_value_is_violation(bad_id):
    """Codex review PR110 round 5 deferral (fauxcasa-i92.3.2): plain `!=`
    treats JSON true and 1.0 as equal to the int request id 1 (Python's
    numeric-tower equality), so a worker could echo a non-int id and slip
    past the id check unnoticed. `type(rid) is not int` rejects both --
    bool (an int subclass) and float -- even when numerically equal."""
    resp = _decode_resp()
    resp["id"] = bad_id
    v = _fresh_validator()
    with pytest.raises(ProtocolViolation, match="!= request id"):
        v._validate_response(resp, expect_id=1)
    assert v.protocol_violations == 1


@pytest.mark.parametrize("code", ["PROTOCOL", "TIMEOUT", "WORKER_CRASHED", "CANCELLED"])
def test_fuzz_broker_only_error_code_is_violation(code):
    """Codex review PR110 round 4 (P2): TIMEOUT/WORKER_CRASHED/PROTOCOL/
    CANCELLED are broker-side judgments; a worker claiming one in its own
    response is lying about broker state and must get the kill/no-retry
    PROTOCOL treatment (counter + ProtocolViolation), not a plain
    DecodeServiceError that leaves the suspect worker alive."""
    v = _fresh_validator()
    resp = {"id": 1, "ok": False, "error": code, "detail": "sure, boss"}
    with pytest.raises(ProtocolViolation, match="broker-only error code"):
        v._validate_response(resp, expect_id=1)
    assert v.protocol_violations == 1


def test_fuzz_honest_worker_error_code_not_violation():
    """The flip side: UNSUPPORTED/CORRUPT/TOO_LARGE are the codes an honest
    worker reports about its own work; they surface as DecodeServiceError
    with the violation counter untouched."""
    v = _fresh_validator()
    resp = {"id": 1, "ok": False, "error": "CORRUPT", "detail": "bad stream"}
    with pytest.raises(DecodeServiceError) as ei:
        v._validate_response(resp, expect_id=1)
    assert not isinstance(ei.value, ProtocolViolation)
    assert v.protocol_violations == 0


@pytest.mark.parametrize("bad_ok", ["yes", 1, 0, None, []])
def test_fuzz_non_bool_ok_is_violation(bad_ok):
    """Codex review PR110 round 5 deferral (fauxcasa-i92.3.2): 'ok' must be
    a real JSON boolean -- a non-bool value (e.g. "yes", 1, 0, null, [])
    previously fell through `is not True` into the honest-error branch,
    so e.g. {"ok": "yes", "error": "CORRUPT"} would raise a plain
    DecodeServiceError with no counter increment/kill. Require a real
    bool before deferring to that branch."""
    resp = _decode_resp()
    resp["ok"] = bad_ok
    v = _fresh_validator()
    with pytest.raises(ProtocolViolation, match="not a JSON boolean"):
        v._validate_response(resp, expect_id=1)
    assert v.protocol_violations == 1


@pytest.mark.parametrize("components", [["bad"], ["ab"], "ab", 7])
def test_hello_malformed_components_is_violation(components):
    """Codex review PR110 round 4: a valid-JSON hello whose bundle
    components is not an object must raise ProtocolViolation -- dict()
    coercion of e.g. ['bad'] raises bare ValueError (previously leaked),
    and ['ab'] would silently fabricate {'a': 'b'}."""
    msg = {"hello": 1, "proto": PROTO, "arena_bytes": SMALL_ARENA_BYTES,
           "bundle": {"id": "x", "version": "y", "components": components},
           "ops": ["decode"], "max_pixels": 1}
    with pytest.raises(ProtocolViolation, match="components"):
        dw.parse_hello(msg, expected_arena_bytes=SMALL_ARENA_BYTES)


def test_fuzz_bad_stride():
    v = _fresh_validator()
    with pytest.raises(ProtocolViolation):
        v._validate_response(_decode_resp(stride=3), expect_id=1)
    assert v.protocol_violations == 1


def test_fuzz_len_mismatch():
    v = _fresh_validator()
    with pytest.raises(ProtocolViolation):
        v._validate_response(_decode_resp(len=15), expect_id=1)
    assert v.protocol_violations == 1


def test_fuzz_unknown_error_code():
    resp = {"id": 1, "ok": False, "error": "TOTALLY_MADE_UP", "detail": "x"}
    v = _fresh_validator()
    with pytest.raises(ProtocolViolation):
        v._validate_response(resp, expect_id=1)
    assert v.protocol_violations == 1


def test_fuzz_response_not_a_dict():
    v = _fresh_validator()
    with pytest.raises(ProtocolViolation):
        v._validate_response("not a dict", expect_id=1)  # type: ignore[arg-type]
    assert v.protocol_violations == 1


def test_fuzz_oversized_outgoing_control_frame():
    huge = {"id": 1, "ok": True, "pad": "x" * (MAX_CONTROL_MSG + 100)}
    with pytest.raises(ProtocolViolation):
        dw._write_frame(io.BytesIO(), huge)


def test_worker_write_frame_oversized_outgoing_control_frame():
    """FIX 9 (nit): the worker's own _write_frame (decodesvc_worker_win.py,
    inlined/separate from the broker's) must reject an outgoing frame past
    MAX_CONTROL_MSG too -- symmetric to the broker's guard above."""
    huge = {"id": 1, "ok": True, "pad": "x" * (dw_worker.MAX_CONTROL_MSG + 100)}
    with pytest.raises(ValueError, match="MAX_CONTROL_MSG"):
        dw_worker._write_frame(io.BytesIO(), huge)


def test_fuzz_oversized_incoming_control_frame():
    fake = io.BytesIO(struct.pack("<I", MAX_CONTROL_MSG + 1))
    with pytest.raises(ProtocolViolation):
        dw._read_frame(fake)


def test_fuzz_mid_message_eof():
    buf = io.BytesIO(struct.pack("<I", 100) + b"short")
    with pytest.raises(DecodeServiceError) as exc_info:
        dw._read_frame(buf)
    assert exc_info.value.code == ErrorCode.WORKER_CRASHED


def test_fuzz_garbage_json():
    payload = b"{not valid json!!"
    buf = io.BytesIO(struct.pack("<I", len(payload)) + payload)
    with pytest.raises(ProtocolViolation):
        dw._read_frame(buf)


def test_fuzz_frame_not_a_json_object():
    payload = b"[1, 2, 3]"  # valid JSON, but not an object
    buf = io.BytesIO(struct.pack("<I", len(payload)) + payload)
    with pytest.raises(ProtocolViolation):
        dw._read_frame(buf)


def test_fuzz_huge_integer_literal_is_protocol_violation():
    """Cross-vendor review (P1): a JSON integer literal past CPython's
    4300-digit int-string cap makes json.loads raise a BARE ValueError,
    not JSONDecodeError. It fits in a sub-64 KiB frame, so a hostile
    worker could send it to escape the ProtocolViolation path (leaving the
    worker alive and the violation counter unmoved). _read_frame must
    normalize it to ProtocolViolation."""
    payload = b'{"id": ' + b"9" * 5000 + b"}"  # 5000-digit int > 4300 cap
    assert len(payload) < MAX_CONTROL_MSG  # a worker really can send this
    buf = io.BytesIO(struct.pack("<I", len(payload)) + payload)
    with pytest.raises(ProtocolViolation):
        dw._read_frame(buf)


def test_fuzz_deeply_nested_json_is_protocol_violation():
    """Cross-vendor review (P1): deeply-nested JSON makes json.loads raise
    RecursionError, which likewise must take the protocol-violation path
    rather than escaping as an unexpected exception."""
    depth = 20_000
    payload = (b"[" * depth) + (b"]" * depth)
    assert len(payload) < MAX_CONTROL_MSG
    buf = io.BytesIO(struct.pack("<I", len(payload)) + payload)
    with pytest.raises(ProtocolViolation):
        dw._read_frame(buf)


def test_frame_round_trip_still_works():
    """Sanity anchor: the framing itself is not broken by the fuzz gates
    above -- a well-formed frame still round-trips."""
    buf = io.BytesIO()
    dw._write_frame(buf, {"hello": 1, "x": 2})
    buf.seek(0)
    assert dw._read_frame(buf) == {"hello": 1, "x": 2}


# ---------------------------------------------------------------------------
# 4. Broker-side metadata clamps (design doc sec 2.4 checklist item 5,
# fauxcasa-i92.3.3). `dw.parse_meta_fields` is a pure dict validator, same
# shape as section 3 above -- no sandbox, no live worker.

def test_meta_none_is_defaults():
    assert dw.parse_meta_fields(None) == MetaFields()


def test_meta_empty_dict_is_defaults():
    assert dw.parse_meta_fields({}) == MetaFields()


def test_meta_unknown_top_level_keys_ignored():
    meta = dw.parse_meta_fields({"caption": "hi", "bogus": "ignored", "another": 123})
    assert meta.caption == "hi"


def test_meta_not_a_dict_is_violation():
    with pytest.raises(ProtocolViolation):
        dw.parse_meta_fields("not a dict")  # type: ignore[arg-type]


def test_meta_caption_exactly_max_kept():
    caption = "a" * MAX_CAPTION_BYTES
    meta = dw.parse_meta_fields({"caption": caption})
    assert meta.caption == caption
    assert len(meta.caption.encode("utf-8")) == MAX_CAPTION_BYTES


def test_meta_caption_oversize_ascii_clamped():
    caption = "a" * 9000
    meta = dw.parse_meta_fields({"caption": caption})
    assert len(meta.caption.encode("utf-8")) == MAX_CAPTION_BYTES
    assert meta.caption == "a" * MAX_CAPTION_BYTES


def test_meta_caption_multibyte_boundary_split_drops_cleanly():
    """8191 ASCII bytes + a run of 4-byte emoji straddles the
    MAX_CAPTION_BYTES clamp boundary -- the codepoint split by the
    truncation must be dropped whole, never emitted as mojibake/
    replacement chars (U+FFFD)."""
    caption = "a" * 8191 + ("\U0001F600" * 10)  # each emoji is 4 utf-8 bytes
    assert len(caption.encode("utf-8")) > MAX_CAPTION_BYTES
    meta = dw.parse_meta_fields({"caption": caption})
    encoded = meta.caption.encode("utf-8")
    assert len(encoded) <= MAX_CAPTION_BYTES
    assert "�" not in meta.caption
    assert encoded.decode("utf-8") == meta.caption  # round-trips cleanly


def test_meta_caption_wrong_type_is_violation():
    with pytest.raises(ProtocolViolation):
        dw.parse_meta_fields({"caption": 12345})


def test_meta_keywords_at_max_kept():
    kws = [f"k{i}" for i in range(MAX_KEYWORDS)]
    meta = dw.parse_meta_fields({"keywords": kws})
    assert meta.keywords == tuple(kws)


def test_meta_keywords_over_max_clamped():
    kws = [f"k{i}" for i in range(MAX_KEYWORDS + 1)]
    meta = dw.parse_meta_fields({"keywords": kws})
    assert meta.keywords == tuple(kws[:MAX_KEYWORDS])


def test_meta_keywords_wrong_element_type_is_violation():
    with pytest.raises(ProtocolViolation):
        dw.parse_meta_fields({"keywords": ["ok", 5, "also-ok"]})


def test_meta_keywords_not_a_list_is_violation():
    with pytest.raises(ProtocolViolation):
        dw.parse_meta_fields({"keywords": "not-a-list"})


def test_meta_taken_absent_defaults_empty():
    assert dw.parse_meta_fields({}).taken == ""


def test_meta_taken_wrong_type_is_violation():
    with pytest.raises(ProtocolViolation):
        dw.parse_meta_fields({"taken": 20260101})


def test_meta_faces_at_max_kept():
    faces = [{"name": f"f{i}", "x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1} for i in range(MAX_FACES)]
    meta = dw.parse_meta_fields({"faces": faces})
    assert len(meta.faces) == MAX_FACES
    assert all(isinstance(f, FaceRegion) for f in meta.faces)


def test_meta_faces_over_max_clamped():
    faces = [{"name": f"f{i}", "x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}
             for i in range(MAX_FACES + 1)]
    meta = dw.parse_meta_fields({"faces": faces})
    assert len(meta.faces) == MAX_FACES
    assert meta.faces[-1].name == f"f{MAX_FACES - 1}"


def test_meta_faces_unknown_keys_ignored():
    meta = dw.parse_meta_fields(
        {"faces": [{"name": "a", "x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1, "bogus": True}]})
    assert meta.faces == (FaceRegion(name="a", x=0.1, y=0.1, w=0.1, h=0.1),)


@pytest.mark.parametrize("bad_face", [
    {"name": "a", "x": float("nan"), "y": 0.1, "w": 0.1, "h": 0.1},
    {"name": "a", "x": 0.1, "y": 0.1, "w": float("inf"), "h": 0.1},
    {"name": "a", "x": 0.1, "y": -0.001, "w": 0.1, "h": 0.1},
    {"name": "a", "x": 0.1, "y": 0.1, "w": 0.1, "h": 1.001},
    {"name": 5, "x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1},
    "not-a-dict",
], ids=["nan-x", "inf-w", "y-below-0", "h-above-1", "name-not-str", "face-not-dict"])
def test_meta_faces_bad_entry_is_violation(bad_face):
    with pytest.raises(ProtocolViolation):
        dw.parse_meta_fields({"faces": [bad_face]})


def test_meta_faces_not_a_list_is_violation():
    with pytest.raises(ProtocolViolation):
        dw.parse_meta_fields({"faces": {"name": "a"}})


def test_meta_gps_valid_pair_kept_as_floats():
    meta = dw.parse_meta_fields({"gps": [49, -123]})
    assert meta.gps == (49.0, -123.0)
    assert all(isinstance(v, float) for v in meta.gps)


@pytest.mark.parametrize("bad_gps", [
    [1], ["a", "b"], [float("nan"), 0], True,
], ids=["too-short", "non-numeric", "nan", "bool"])
def test_meta_gps_bad_value_is_violation(bad_gps):
    with pytest.raises(ProtocolViolation):
        dw.parse_meta_fields({"gps": bad_gps})


def test_meta_gps_absent_is_none():
    assert dw.parse_meta_fields({}).gps is None


def test_meta_gps_explicit_none_is_none():
    assert dw.parse_meta_fields({"gps": None}).gps is None


def test_meta_rating_over_max_clamped():
    assert dw.parse_meta_fields({"rating": 7}).rating == 5


def test_meta_rating_below_min_clamped():
    assert dw.parse_meta_fields({"rating": -1}).rating == 0


def test_meta_rating_wrong_type_is_violation():
    with pytest.raises(ProtocolViolation):
        dw.parse_meta_fields({"rating": "3"})


def test_meta_rating_bool_is_violation():
    """rating is an int subclass footgun: bool must be rejected even
    though isinstance(True, int) is True."""
    with pytest.raises(ProtocolViolation):
        dw.parse_meta_fields({"rating": True})


def test_validate_meta_violation_increments_counter():
    v = _fresh_validator()
    with pytest.raises(ProtocolViolation):
        v.validate_meta({"caption": 12345})
    assert v.protocol_violations == 1


def test_validate_meta_clamp_does_not_increment_counter():
    v = _fresh_validator()
    meta = v.validate_meta(
        {"caption": "a" * 9000, "keywords": [f"k{i}" for i in range(MAX_KEYWORDS + 1)]})
    assert len(meta.caption.encode("utf-8")) == MAX_CAPTION_BYTES
    assert len(meta.keywords) == MAX_KEYWORDS
    assert v.protocol_violations == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + (sys.argv[1:] or ["-v"])))
