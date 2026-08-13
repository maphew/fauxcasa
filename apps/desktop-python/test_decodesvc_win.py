#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "PySide6"]
# ///
"""Tests for the Windows sandboxed decode transport (fauxcasa-i92.3, Stage B).

Run: `QT_QPA_PLATFORM=offscreen uv run apps/desktop-python/test_decodesvc_win.py -q`
(matches how test_tracer.py is invoked). The whole module skips on
non-Windows -- this is the Windows half of i92.3; the Linux half is a
separate module.

Three kinds of test here:

1. Containment gates (design doc sec 7 gate 1) -- each spawns a REAL
   sandboxed worker with the probe flag and asserts the hostile attempt
   was DENIED. A failing containment gate is a security finding, not a
   flaky test; see the assertion messages.
2. Positive/boundary tests -- handed_fd_read (the one attempt that must
   succeed), an arena round trip, a real decode of a synthetic fixture,
   lifetime/kill semantics, and the hello/proto refusal path.
3. Trusted-side protocol fuzz (design doc sec 7 gate 3) -- feeds
   decodesvc_win's response validator (`WinSandboxWorker._validate_response`)
   and its framing (`_read_frame`/`_write_frame`) hand-built lying-worker
   data. No sandbox needed; these exercise ONLY the broker's own
   validation code and run on every platform (they just happen to live in
   a module that otherwise skips on non-Windows, per the Stage B spec).

Synthetic-only fixtures throughout (privacy rule): the "photo" used here
is a 2x2 red PNG built from a literal byte string, not any real image.
"""

from __future__ import annotations

import base64
import ctypes
import io
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip(
        "Windows-only sandbox (fauxcasa-i92.3 Stage B); the trusted-side "
        "fuzz tests below are individually platform-independent by design "
        "(no Windows API touched) but this module skips wholesale here per "
        "the Stage B spec -- a Linux module owns the Linux half.",
        allow_module_level=True,
    )

sys.path.insert(0, str(Path(__file__).resolve().parent))

import decodesvc_win as dw  # noqa: E402
from decodesvc import (  # noqa: E402
    DecodeServiceError,
    ErrorCode,
    MAX_CONTROL_MSG,
    PixelBuffer,
    PixelFormat,
    PROTO,
    ProtocolViolation,
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
    isolation benefit (each probe attempt is a fresh, independent job)."""
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
    [dns_lookup]. Document, don't fail the build on socket_create alone."""
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


def test_lifetime_close_kills_worker_within_2s():
    worker = dw.WinSandboxWorker(arena_bytes=SMALL_ARENA_BYTES)
    worker.spawn()
    t0 = time.perf_counter()
    worker.close()
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, f"close() took {elapsed:.2f}s, expected < 2s (gotcha 5 backstop)"


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

        def _lying_recv_response():
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


def test_frame_round_trip_still_works():
    """Sanity anchor: the framing itself is not broken by the fuzz gates
    above -- a well-formed frame still round-trips."""
    buf = io.BytesIO()
    dw._write_frame(buf, {"hello": 1, "x": 2})
    buf.seek(0)
    assert dw._read_frame(buf) == {"hello": 1, "x": 2}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + (sys.argv[1:] or ["-v"])))
