#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "PySide6", "rawpy", "av", "pillow", "pi-heif", "exiv2"]
# ///
"""Tests for decodefacade.py (fauxcasa-ez2.9 Stage 1): env-based
transport selection, the sandboxed/in-process/degraded state machine,
error-taxonomy mapping, and --require-sandbox's fail-loud contract.

Run: `QT_QPA_PLATFORM=offscreen uv run apps/desktop-python/test_decodefacade.py -q`

Synthetic-only fixtures (privacy rule): a 2x2 red PNG built from a
literal byte string, not any real image.
"""

from __future__ import annotations

import base64
import queue
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import decodefacade as df  # noqa: E402

_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires spawning a real Windows AppContainer sandbox worker",
)


def _assert_sandbox_grantable() -> None:
    """fauxcasa-ayh: the 3 real-sandbox tests below spawn an actual
    AppContainer worker; on a dev box whose UV_CACHE_DIR points at a
    ReFS/Dev Drive volume, the worker PYTHONPATH grant fails and the
    worker dies pre-hello, degrading these tests to a mysterious
    STATE_DEGRADED assertion failure. Call this FIRST so that box fails
    LOUD and explicit instead, naming the ungrantable path(s) and the
    fix -- rather than silently skipping (this is exactly the real-
    sandbox contract these tests exist to exercise).

    A failure on the worker SCRIPT's own directory alone is excluded:
    spawn() already recovers from that one by staging a content-hashed
    copy under the cache root (fauxcasa-yfq) -- unlike the interpreter
    base dir or the PYTHONPATH, neither of which can be staged -- so it
    is not actually blocking (verified: a real spawn still reaches hello
    on a checkout whose own directory sits on a ReFS/Dev Drive volume
    that refuses the grant, the exact situation on this repo's own dev
    box worktrees)."""
    import decodesvc_win as dw

    failures = dw.preflight_worker_grants()
    source_worker_dir = str(Path(dw.__file__).resolve().parent)
    failures = [(p, e) for p, e in failures if p != source_worker_dir]
    if failures:
        detail = "; ".join(f"{p!r}: {err}" for p, err in failures)
        pytest.fail(
            "real-sandbox test cannot run: AppContainer read+execute grant "
            f"failed before spawn on {detail}; {dw.UNGRANTABLE_PYTHONPATH_HINT}")

SYNTHETIC_PNG_2X2_RED = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAEUlEQVR42mP4z8DwH4QZYAwAR8oH"
    "+Rq28akAAAAASUVORK5CYII=")


@pytest.fixture
def synthetic_png(tmp_path: Path) -> Path:
    p = tmp_path / "synthetic_2x2_red.png"
    p.write_bytes(SYNTHETIC_PNG_2X2_RED)
    return p


def _make_oriented_jpeg(path: Path, w: int = 64, h: int = 32,
                        orientation: int = 6) -> Path:
    """A synthetic JPEG carrying an EXIF Orientation tag -- same
    construction as test_sandbox_e2e.py's _make_jpeg (not imported: this
    file stays a self-contained script). Used for the P2-2 orientation-
    parity test: both DecodeService transports must return the SAME
    display-upright dims for an orientation-6 source."""
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(120, 160, 200))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    assert img.save(buf, "JPEG", 90)
    data = bytes(buf.data())
    assert data[:2] == b"\xff\xd8"
    tiff = b"II" + struct.pack("<H", 42) + struct.pack("<I", 8)
    ifd = (struct.pack("<H", 1)
           + struct.pack("<HHI", 0x0112, 3, 1)
           + struct.pack("<HH", orientation, 0)
           + struct.pack("<I", 0))
    payload = b"Exif\x00\x00" + tiff + ifd
    seg = bytes([0xFF, 0xE1]) + struct.pack(">H", len(payload) + 2) + payload
    data = data[:2] + seg + data[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture(autouse=True)
def _reset_facade_singleton():
    """Every test gets a fresh DecodeService (ensure_started() only ever
    runs once per instance by design) and any sandbox it spawned is
    closed on teardown."""
    df.reset_service()
    yield
    df.reset_service()


# ---------------------------------------------------------------------------
# 1. sandbox_mode() env selection

def test_sandbox_mode_defaults_off_under_pytest(monkeypatch):
    monkeypatch.delenv("FAUXCASA_DECODE_SANDBOX", raising=False)
    assert df.under_pytest(), "this test IS running under pytest"
    assert df.sandbox_mode() == "0"


def test_sandbox_mode_explicit_env_overrides_default(monkeypatch):
    for val in ("0", "1", "require"):
        monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", val)
        assert df.sandbox_mode() == val


def test_sandbox_mode_rejects_garbage(monkeypatch):
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "yes")
    with pytest.raises(ValueError):
        df.sandbox_mode()


# ---------------------------------------------------------------------------
# 2. State machine: in-process (env=0), degraded (spawn failure), require

def test_ensure_started_env_zero_is_in_process(monkeypatch):
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "0")
    svc = df.get_service()
    svc.ensure_started()
    assert svc.state == df.STATE_IN_PROCESS
    assert "FAUXCASA_DECODE_SANDBOX=0" in svc.reason


def test_ensure_started_is_idempotent(monkeypatch):
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "0")
    svc = df.get_service()
    svc.ensure_started()
    state1 = svc.state
    svc.state = "mutated-to-prove-no-rerun"
    svc.ensure_started()
    assert svc.state == "mutated-to-prove-no-rerun", (
        "a second ensure_started() call must be a no-op")


def test_ensure_started_degrades_on_spawn_failure(monkeypatch):
    """P1 finding: monkeypatch spawn to raise -> state degraded, reason
    set, decode() still works (falls back to in-process)."""
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")

    class _BoomTransport:
        def __init__(self, n_batch):
            pass

        def start(self):
            raise RuntimeError("simulated spawn failure")

    monkeypatch.setattr(df, "WinSandboxTransport", _BoomTransport)
    if sys.platform != "win32":
        pytest.skip("degrade path is only reached when sys.platform == 'win32'")
    svc = df.get_service()
    svc.ensure_started()
    assert svc.state == df.STATE_DEGRADED
    assert "simulated spawn failure" in svc.reason


def test_ensure_started_warns_on_ungrantable_pythonpath(monkeypatch, capsys):
    """fauxcasa-ayh: ensure_started() must check the worker PYTHONPATH's
    grantability BEFORE the first spawn attempt -- a ReFS/Dev Drive
    UV_CACHE_DIR that refuses the AppContainer per-SID grant needs to
    warn with the actionable fix here, once, rather than leaving the
    reader to connect a much later pre-hello ModuleNotFoundError:
    PySide6 worker death back to this cause. The grant is still best-
    effort (spawn() -> start() below can succeed anyway on an ALL
    APPLICATION PACKAGES-granted volume), so the warning must NOT
    degrade the session by itself."""
    import applog  # noqa: F401 -- side effect: attaches the stderr mirror handler
    if sys.platform != "win32":
        pytest.skip("sandbox path is only reached when sys.platform == 'win32'")
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")

    import decodesvc_win as dw

    fake_path = "X:/fake/site-packages"
    monkeypatch.setattr(dw, "preflight_worker_grants",
                         lambda *a, **k: [(fake_path, "err=5")])
    monkeypatch.setattr(df.WinSandboxTransport, "start", lambda self: None)

    svc = df.get_service()
    svc.ensure_started()

    assert len(svc.warnings) == 1
    assert fake_path in svc.warnings[0]
    assert "UV_CACHE_DIR" in svc.warnings[0]
    assert "UV_CACHE_DIR" in capsys.readouterr().err
    assert svc.state != df.STATE_DEGRADED


@_WINDOWS_ONLY
def test_winsandboxtransport_start_raises_when_batch_pool_empty(monkeypatch):
    """fauxcasa-ez2.9 Stage 2 review P2-3: warm() tolerates individual
    batch-member spawn failures and can legitimately return 0 while the
    interactive member still succeeds -- WinSandboxTransport.start() must
    not silently accept that. Left unchecked, `state` becomes sandboxed
    with an empty batch pool: every index thread would then block the
    full 30s lease() timeout on the "batch" lane before getting a
    per-file null, with no session degrade (N7 "never silent")."""
    from thumbcache import INDEX_WORKERS

    sandbox = df.WinSandboxTransport(n_batch=INDEX_WORKERS)
    monkeypatch.setattr(sandbox._pool_set, "warm", lambda: 0)
    try:
        with pytest.raises(RuntimeError, match="no batch decode workers"):
            sandbox.start()
    finally:
        sandbox.close()


@_WINDOWS_ONLY
def test_ensure_started_degrades_when_batch_pool_fully_empty(monkeypatch):
    """Same as test_winsandboxtransport_start_raises_when_batch_pool_empty
    but through the production call path (ensure_started()): the session
    must degrade honestly (state=degraded, reason set) rather than
    reporting sandboxed with a dead batch lane."""
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")

    class _ZeroBatchTransport(df.WinSandboxTransport):
        def start(self):
            self._pool_set.warm = lambda: 0
            super().start()

    monkeypatch.setattr(df, "WinSandboxTransport", _ZeroBatchTransport)
    svc = df.get_service()
    svc.ensure_started()
    assert svc.state == df.STATE_DEGRADED
    assert "no batch decode workers" in svc.reason


def test_ensure_started_require_mode_raises(monkeypatch):
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "require")

    class _BoomTransport:
        def __init__(self, n_batch):
            pass

        def start(self):
            raise RuntimeError("simulated spawn failure")

    monkeypatch.setattr(df, "WinSandboxTransport", _BoomTransport)
    if sys.platform != "win32":
        pytest.skip("require-mode spawn attempt is only reached on win32")
    svc = df.get_service()
    with pytest.raises(df.DecodeSandboxRequiredError):
        svc.ensure_started()


def test_decode_returns_null_for_crashed_file_then_in_process_for_next(
        monkeypatch, synthetic_png, tmp_path):
    """P1 finding "facade escape": a RuntimeError from the sandbox
    (spawn-class failure -- e.g. a malicious file that crashed/killed the
    sandbox worker) must flip state to degraded and return a NULL QImage
    for the file that triggered it -- that file must NEVER fall through
    to in-process decode. Only a SUBSEQUENT call, with a different path,
    may use the in-process fallback."""
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")

    class _RaisingSandbox:
        def __init__(self, n_batch):
            pass

        def start(self):
            pass  # "succeeds" at startup

        def decode(self, path, route="still", edge=0):
            raise RuntimeError("simulated mid-session spawn failure")

        def close(self):
            pass

    monkeypatch.setattr(df, "WinSandboxTransport", _RaisingSandbox)
    if sys.platform != "win32":
        pytest.skip("sandboxed decode path only reached on win32")
    svc = df.get_service()
    img = svc.decode(synthetic_png, route="still", edge=0)
    assert img.isNull(), (
        "the file that crashed/killed the sandbox worker must never be "
        "decoded in-process")
    assert svc.state == df.STATE_DEGRADED

    other_png = tmp_path / "other.png"
    other_png.write_bytes(SYNTHETIC_PNG_2X2_RED)
    img2 = svc.decode(other_png, route="still", edge=0)
    assert not img2.isNull(), (
        "a later call with a DIFFERENT path must fall back to in-process")
    assert img2.width() == 2 and img2.height() == 2


def test_decode_oserror_degrades_to_null(monkeypatch, synthetic_png):
    """P2 finding: OSError (create_or_derive_profile/CreateProcess
    failures) must NOT escape the "null QImage on ANY failure" contract --
    it maps to the same session-level degrade + null-for-this-file
    behaviour as RuntimeError."""
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")

    class _RaisingSandbox:
        def __init__(self, n_batch):
            pass

        def start(self):
            pass

        def decode(self, path, route="still", edge=0):
            raise OSError("simulated CreateProcess/profile failure")

        def close(self):
            pass

    monkeypatch.setattr(df, "WinSandboxTransport", _RaisingSandbox)
    if sys.platform != "win32":
        pytest.skip("sandboxed decode path only reached on win32")
    svc = df.get_service()
    img = svc.decode(synthetic_png, route="still", edge=0)
    assert img.isNull()
    assert svc.state == df.STATE_DEGRADED


def test_decode_queue_empty_is_per_file_null_not_session_degrade(
        monkeypatch, synthetic_png):
    """Re-review residual (item 1): a queue.Empty from a contended
    INTERACTIVE lease (one reserved worker, timeout 30s) is NOT a
    spawn-class failure -- the sandbox is fine, just busy. It must map to
    a PER-FILE null image only; the session must stay 'sandboxed' so the
    next call still tries the sandbox. Simulated here with two
    "concurrent" interactive decodes where the second's lease call raises
    queue.Empty (as WinDecodePoolSet.lease(timeout=30) does when the
    reserved interactive worker stays checked out)."""
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")

    calls = []

    class _ContendedSandbox:
        def __init__(self, n_batch):
            pass

        def start(self):
            pass

        def decode(self, path, route="still", edge=0):
            calls.append(path)
            if len(calls) == 1:
                from PySide6.QtGui import QImage
                return QImage(2, 2, QImage.Format.Format_RGBA8888)
            raise queue.Empty()  # second concurrent interactive lease times out

        def close(self):
            pass

    monkeypatch.setattr(df, "WinSandboxTransport", _ContendedSandbox)
    if sys.platform != "win32":
        pytest.skip("sandboxed decode path only reached on win32")
    svc = df.get_service()

    img1 = svc.decode(synthetic_png, route="still", edge=0)
    assert not img1.isNull()
    assert svc.state == df.STATE_SANDBOXED

    img2 = svc.decode(synthetic_png, route="still", edge=0)
    assert img2.isNull(), "the timed-out call gets a null image"
    assert svc.state == df.STATE_SANDBOXED, (
        "a lease timeout must not degrade the session")
    assert svc.reason == ""

    # the sandbox is still in use -- a third call goes through it again.
    img3 = svc.decode(synthetic_png, route="still", edge=0)
    assert len(calls) == 3


def test_ensure_started_closes_transport_on_start_failure(monkeypatch):
    """P3 finding: a warm()/start() failure must close() the transport --
    otherwise any worker processes it spawned before the failing member
    leak for the rest of the session."""
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")
    closed = []

    class _BoomTransport:
        def __init__(self, n_batch):
            pass

        def start(self):
            raise RuntimeError("simulated spawn failure")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(df, "WinSandboxTransport", _BoomTransport)
    if sys.platform != "win32":
        pytest.skip("degrade path is only reached when sys.platform == 'win32'")
    svc = df.get_service()
    svc.ensure_started()
    assert svc.state == df.STATE_DEGRADED
    assert closed == [True], "a failed start() must still close() the transport"


def test_decode_protocol_violation_never_falls_back_in_process(monkeypatch, synthetic_png):
    """ProtocolViolation is evidence of compromise (design doc sec 1/2.5):
    the facade must return null and log.error, and must NEVER re-decode
    that file in-process -- confirmed here by making the in-process
    transport raise if it is ever called."""
    from decodesvc import ProtocolViolation

    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")

    class _LyingSandbox:
        def __init__(self, n_batch):
            pass

        def start(self):
            pass

        def decode(self, path, route="still", edge=0):
            raise ProtocolViolation("simulated hostile response")

        def close(self):
            pass

    monkeypatch.setattr(df, "WinSandboxTransport", _LyingSandbox)
    if sys.platform != "win32":
        pytest.skip("sandboxed decode path only reached on win32")
    svc = df.get_service()

    def _must_not_be_called(*a, **k):
        raise AssertionError("in-process decode must NEVER run after a ProtocolViolation")

    monkeypatch.setattr(svc._in_process, "decode", _must_not_be_called)
    img = svc.decode(synthetic_png, route="still", edge=0)
    assert img.isNull()
    assert svc.state == df.STATE_SANDBOXED, (
        "a ProtocolViolation on one file must not degrade the whole session")


def test_decode_non_still_route_falls_back_without_touching_sandbox(monkeypatch, tmp_path):
    """Stage 1 scope: only route='still' is sandboxed; any other route
    (raw/tiff16/video) must go straight to InProcess without ever
    touching the sandbox transport (proven by a sandbox stub that raises
    if decode() is called with the wrong route)."""
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")

    class _StrictSandbox:
        def __init__(self, n_batch):
            pass

        def start(self):
            pass

        def decode(self, path, route="still", edge=0):
            if route != "still":
                raise df.NotSandboxed(f"route {route!r} not sandboxed")
            raise AssertionError("should not be reached in this test")

        def close(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(df, "WinSandboxTransport", _StrictSandbox)
    if sys.platform != "win32":
        pytest.skip("sandboxed decode path only reached on win32")
    svc = df.get_service()
    missing = tmp_path / "does-not-exist.raw"
    img = svc.decode(missing, route="raw", edge=0)
    assert img.isNull()  # OSError on open -> null, no crash


def test_decode_concurrent_degrade_never_raises_attributeerror(monkeypatch, synthetic_png):
    """Re-review residual (item 2): decode() must read self._sandbox ONCE
    into a local under the lock. Simulated here with a transport whose
    decode() degrades the service (mutating svc._sandbox to None) from
    "another thread" mid-call -- i.e. the mutation happens INSIDE the
    stub's decode(), after decode() has already read the (now-stale)
    local. A second call must still see a consistent local and never
    raise AttributeError out of decode()."""
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")

    class _DegradingMidCallSandbox:
        def __init__(self, n_batch):
            pass

        def start(self):
            pass

        def decode(self, path, route="still", edge=0):
            # Simulate a concurrent thread's degrade branch mutating
            # svc._sandbox to None WHILE this call is in flight -- the
            # local `sandbox` this call captured under the lock must
            # still be used for the remainder of this call.
            svc._sandbox = None
            raise RuntimeError("simulated concurrent spawn-class failure")

        def close(self):
            pass

    monkeypatch.setattr(df, "WinSandboxTransport", _DegradingMidCallSandbox)
    if sys.platform != "win32":
        pytest.skip("sandboxed decode path only reached on win32")
    svc = df.get_service()

    # decode() must not raise AttributeError even though svc._sandbox is
    # concurrently nulled out from inside the stub's decode() call.
    img = svc.decode(synthetic_png, route="still", edge=0)
    assert img.isNull()
    assert svc.state == df.STATE_DEGRADED

    # a later call, with self._sandbox already None, must not raise
    # either -- it falls through to in-process.
    img2 = svc.decode(synthetic_png, route="still", edge=0)
    assert not img2.isNull()


def test_decode_require_mode_mid_session_failure_never_falls_back(
        monkeypatch, synthetic_png):
    """Re-review residual (item 3): under FAUXCASA_DECODE_SANDBOX=require,
    a MID-SESSION spawn-class failure must set _require_failed, return
    null for THIS and every LATER "still" call, log.error once, and set
    state='degraded' reason='require: sandbox lost mid-session' -- "still"
    must NEVER fall back to in-process (proven with an in-process stub
    that raises if ever called with route="still").

    release-0.1 review P1-2: the fail-closed latch must only blank
    "still" -- the only route the sandbox actually serves -- not
    raw/video/tiff16/psd, which are documented in-process-by-design
    regardless of sandbox state and must keep working normally even
    after a "still"-route require-mode failure."""
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "require")

    class _BoomAfterStartSandbox:
        def __init__(self, n_batch):
            pass

        def start(self):
            pass  # startup succeeds

        def decode(self, path, route="still", edge=0):
            raise RuntimeError("simulated mid-session spawn failure")

        def close(self):
            pass

    monkeypatch.setattr(df, "WinSandboxTransport", _BoomAfterStartSandbox)
    if sys.platform != "win32":
        pytest.skip("sandboxed decode path only reached on win32")
    svc = df.get_service()

    orig_in_process_decode = svc._in_process.decode
    non_still_calls = []

    def _still_must_not_be_called(path, route="still", edge=0):
        if route == "still":
            raise AssertionError(
                "in-process decode must NEVER run for route='still' "
                "under require mode after a mid-session sandbox loss")
        non_still_calls.append(route)
        return orig_in_process_decode(path, route=route, edge=edge)

    monkeypatch.setattr(svc._in_process, "decode", _still_must_not_be_called)

    img1 = svc.decode(synthetic_png, route="still", edge=0)
    assert img1.isNull()
    assert svc.state == df.STATE_DEGRADED
    assert svc.reason == "require: sandbox lost mid-session"
    assert svc._require_failed is True

    # every later "still" call also returns null, forever, without ever
    # touching in-process (the monkeypatched stub above would raise).
    img2 = svc.decode(synthetic_png, route="still", edge=0)
    assert img2.isNull()

    # a non-"still" route is NOT covered by the latch: it must keep
    # reaching its documented in-process path (proven by the spy above
    # recording the call) instead of the latch short-circuiting it to a
    # null blank before in-process is ever touched (the synthetic_png
    # bytes are not a real RAW file, so the decode result itself is
    # incidental -- what matters is that in-process was reached at all).
    svc.decode(synthetic_png, route="raw", edge=0)
    assert non_still_calls == ["raw"], (
        "route='raw' must reach InProcessTransport.decode even after a "
        "require-mode 'still' failure latched _require_failed -- the "
        "latch must only blank 'still' (release-0.1 review P1-2)")


# ---------------------------------------------------------------------------
# 3. Real end-to-end (Windows only): decode() through the real sandbox,
# index()/poster() always in-process.

@_WINDOWS_ONLY
def test_decode_real_sandbox_still(monkeypatch, synthetic_png):
    _assert_sandbox_grantable()
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")
    svc = df.get_service()
    img = svc.decode(synthetic_png, route="still", edge=0)
    assert svc.state == df.STATE_SANDBOXED
    assert not img.isNull()
    assert img.width() == 2 and img.height() == 2


def test_decode_in_process_applies_orientation(monkeypatch, tmp_path):
    """fauxcasa-ez2.9 Stage 2 review P2-2: InProcessTransport's
    QImage.fromData branch must apply EXIF orientation itself --
    DecodeService.decode() is documented as ALWAYS display-upright, and
    both wired call sites (thumbcache._index_one, viewer.
    load_original_oriented) skip their own manual apply_orientation()
    step on the sandboxed branch, trusting the facade to have already
    oriented the image on every transport."""
    p = _make_oriented_jpeg(tmp_path / "rotated.jpg", w=64, h=32, orientation=6)
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "0")
    svc = df.get_service()
    img = svc.decode(str(p), route="still", edge=0)
    assert not img.isNull()
    assert (img.width(), img.height()) == (32, 64), (
        "orientation=6 on a 64x32 source must decode display-upright (32x64)")


@_WINDOWS_ONLY
def test_decode_orientation_parity_sandboxed_vs_in_process(monkeypatch, tmp_path):
    """fauxcasa-ez2.9 Stage 2 review P2-2: both transports must agree on
    orientation. Before the fix, InProcessTransport returned 64x32
    (sideways) for this file while WinSandboxTransport returned 32x64
    (upright) -- a concurrent mid-session degrade could hand a caller a
    sideways image with no way to tell."""
    _assert_sandbox_grantable()
    p = _make_oriented_jpeg(tmp_path / "rotated.jpg", w=64, h=32, orientation=6)

    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "0")
    df.reset_service()
    img0 = df.get_service().decode(str(p), route="still", edge=0)

    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")
    df.reset_service()
    svc1 = df.get_service()
    img1 = svc1.decode(str(p), route="still", edge=0)
    assert svc1.state == df.STATE_SANDBOXED, f"sandbox failed: {svc1.reason}"

    assert not img0.isNull() and not img1.isNull()
    assert (img0.width(), img0.height()) == (32, 64)
    assert (img1.width(), img1.height()) == (32, 64)


@_WINDOWS_ONLY
def test_decode_real_sandbox_warms_full_pool_three_times_in_a_row(monkeypatch):
    """P1 finding "Profile/SID race": DecodePoolSet(n_batch=INDEX_WORKERS)
    .warm() used to race CreateAppContainerProfile across every one of
    its parallel spawn threads (observed 0x80070020 SHARING_VIOLATION,
    0x80070005 ACCESS_DENIED, 0x800703FA ERROR_KEY_DELETED, and a
    "successful" warm that silently spawned fewer members than
    requested). This is the actual PRODUCTION path -- ensure_started()
    -> WinSandboxTransport(n_batch=INDEX_WORKERS).start() ->
    DecodePoolSet.warm() -- which the 180-test decodesvc_win suite never
    exercised (it spawns serially or at most 3 in parallel). Three
    consecutive warms through the real facade must each reach the full
    min(8, cpu_count())+1 member count and state == sandboxed."""
    _assert_sandbox_grantable()
    from thumbcache import INDEX_WORKERS

    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")
    expected = INDEX_WORKERS + 1  # batch members + the reserved interactive
    for trial in range(3):
        df.reset_service()
        svc = df.get_service()
        svc.ensure_started()
        assert svc.state == df.STATE_SANDBOXED, (
            f"trial {trial}: state={svc.state!r} reason={svc.reason!r}")
        pool_set = svc._sandbox._pool_set
        surviving = pool_set.batch_size + 1  # +1 for the reserved interactive
        assert surviving == expected, (
            f"trial {trial}: surviving={surviving}, expected {expected}")
    df.reset_service()


def test_index_and_poster_are_always_in_process(monkeypatch, synthetic_png):
    """Stage 1 scope: index()/poster() never touch the sandbox transport
    even when it is available (no sandboxed 'index'/'poster' op exists
    yet) -- proven by a sandbox stub that raises if decode() is called."""
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")

    class _MustNotBeCalled:
        def __init__(self, n_batch):
            pass

        def start(self):
            pass

        def decode(self, *a, **k):
            raise AssertionError("index()/poster() must not touch the sandbox in Stage 1")

        def close(self):
            pass

    monkeypatch.setattr(df, "WinSandboxTransport", _MustNotBeCalled)
    svc = df.get_service()
    img = svc.index(synthetic_png, top=512)
    assert not img.isNull()
    assert img.width() == 2 and img.height() == 2


def test_winsandboxtransport_decode_returns_null_qimage_for_missing_pixels_bytes():
    """decodesvc.DecodeResult.pixels_bytes is Optional (defaults to None
    for back-compat construction) -- WinSandboxTransport.decode() must
    guard that instead of handing None to QImage(), which would raise.
    Uses a fake pool/transport so this does not depend on a real sandbox
    worker or win32."""
    from decodesvc import DecodeResult, PixelBuffer, PixelFormat

    class _FakePool:
        def decode(self, path, edge=0):
            return DecodeResult(
                pixels=PixelBuffer(w=2, h=2, stride=8,
                                    pixfmt=PixelFormat.RGBA8, off=0, len=0),
                source_w=2, source_h=2, pixels_bytes=None)

    class _FakePoolSet:
        def lease(self, lane, timeout=30):
            return _FakePool()

        def release(self, pool):
            pass

    transport = object.__new__(df.WinSandboxTransport)
    transport._dw = None
    transport._pool_set = _FakePoolSet()
    img = transport.decode("does-not-matter.jpg", route="still", edge=0)
    assert img.isNull(), "pixels_bytes=None must yield a null QImage, not a crash"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + (sys.argv[1:] or ["-v"])))
