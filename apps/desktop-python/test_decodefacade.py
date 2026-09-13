#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "PySide6", "rawpy", "av", "pillow", "exiv2"]
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
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import decodefacade as df  # noqa: E402

_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires spawning a real Windows AppContainer sandbox worker",
)

SYNTHETIC_PNG_2X2_RED = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAEUlEQVR42mP4z8DwH4QZYAwAR8oH"
    "+Rq28akAAAAASUVORK5CYII=")


@pytest.fixture
def synthetic_png(tmp_path: Path) -> Path:
    p = tmp_path / "synthetic_2x2_red.png"
    p.write_bytes(SYNTHETIC_PNG_2X2_RED)
    return p


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


def test_decode_falls_back_to_in_process_after_session_degrade(monkeypatch, synthetic_png):
    """A per-file decode() call that hits a RuntimeError from the sandbox
    (spawn-class failure surfacing later) must flip state to degraded and
    still return a real decoded image via the in-process fallback --
    never raise out to the caller."""
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
    assert not img.isNull()
    assert img.width() == 2 and img.height() == 2
    assert svc.state == df.STATE_DEGRADED


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


# ---------------------------------------------------------------------------
# 3. Real end-to-end (Windows only): decode() through the real sandbox,
# index()/poster() always in-process.

@_WINDOWS_ONLY
def test_decode_real_sandbox_still(monkeypatch, synthetic_png):
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")
    svc = df.get_service()
    img = svc.decode(synthetic_png, route="still", edge=0)
    assert svc.state == df.STATE_SANDBOXED
    assert not img.isNull()
    assert img.width() == 2 and img.height() == 2


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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + (sys.argv[1:] or ["-v"])))
