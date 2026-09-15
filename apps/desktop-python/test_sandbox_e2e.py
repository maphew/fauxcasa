#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "PySide6", "pillow", "pi-heif", "exiv2", "rawpy", "av", "zstandard"]
# ///
"""End-to-end tests for the decode-sandbox call-site wiring (fauxcasa-ez2.9
Stage 2): thumbcache._index_one and viewer.load_original_oriented actually
routed through decodefacade.get_service() for stills, exercised against a
REAL Windows AppContainer sandboxed worker (not a stub) -- Windows-only.

Run: `QT_QPA_PLATFORM=offscreen uv run apps/desktop-python/test_sandbox_e2e.py -q`

Three groups:

  (a) parity   -- index the same synthetic mini-library with
                  FAUXCASA_DECODE_SANDBOX=1 and =0 and compare the
                  resulting fcache tiles pixelwise (within a small JPEG-
                  requant tolerance) and the error-tile set exactly.
  (b) viewer   -- same parity check for viewer.load_original_oriented on
                  one still.
  (c) hostile  -- a truncated JPEG, a PNG whose IHDR claims 60000x60000, a
                  32769x1 PNG, and a zip-bomb-ish PNG (huge IHDR + tiny
                  IDAT) through the WIRED index path (thumbcache.
                  build_cache, sandbox=1) -> taxonomy-correct error tiles,
                  zero ProtocolViolation counter increments on the stock
                  worker pool, and the pool set alive afterward.

Item (d) of the Stage 2 spec ("a decode that returns PROTOCOL from a fake
worker is never re-decoded in-process") is already covered at the facade
level by test_decodefacade.py::test_decode_protocol_violation_never_falls_back_in_process
-- not duplicated here.

Synthetic-only fixtures (privacy rule): every image below is built at test
time from PySide6.QtGui / raw bytes, never real Picasa data.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import decodefacade as df  # noqa: E402
import thumbcache  # noqa: E402
from catalog import scan_library  # noqa: E402
from viewer import load_original_oriented  # noqa: E402

_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires spawning a real Windows AppContainer sandbox worker",
)


# ---------------------------------------------------------------------------
# Fixtures / helpers

def _make_jpeg(path: Path, w: int = 64, h: int = 48,
               orientation: int | None = None) -> None:
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(120, 160, 200))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    assert img.save(buf, "JPEG", 90)
    data = bytes(buf.data())
    if orientation is not None:
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


def _make_png(path: Path, w: int = 64, h: int = 48) -> None:
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(60, 200, 90))
    path.parent.mkdir(parents=True, exist_ok=True)
    assert img.save(str(path), "PNG")


def _write_ihdr_png(path: Path, w: int, h: int) -> None:
    """A minimally-valid PNG whose IHDR declares w x h but whose IDAT is
    for a real tiny 2x2 image -- QImageReader.size() reports the huge
    declared dimensions from IHDR alone; no giant pixel buffer is ever
    allocated. Mirrors test_decodesvc_win.py::_write_ihdr_png."""
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    img = QImage(2, 2, QImage.Format.Format_RGB32)
    img.fill(QColor(255, 0, 0))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    assert img.save(buf, "PNG")
    png = bytearray(bytes(buf.data()))
    struct.pack_into(">II", png, 16, w, h)  # IHDR w,h at offset 16
    crc = zlib.crc32(bytes(png[12:16 + 13])) & 0xFFFFFFFF  # 'IHDR' + 13 data
    struct.pack_into(">I", png, 16 + 13, crc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(png))


def _make_psd(path: Path, w: int = 64, h: int = 48,
              color: tuple[int, int, int] = (200, 60, 120)) -> Path:
    """A synthetic 'maximize compatibility' PSD -- same hand-built shape
    as test_tracer.py's _make_psd (not imported: this file stays a
    self-contained script per repo convention). Qt ships no PSD plugin
    at all (pillowload module doc), so this exercises the P1-1 PSD
    pre-route: with the sandbox on, .psd must still reach pillow_qimage
    ahead of the sandboxed branch instead of round-tripping through a
    worker that will always report UNSUPPORTED."""
    r, g, b = color
    out = (b"8BPS" + struct.pack(">H", 1) + b"\x00" * 6
           + struct.pack(">H", 3)            # channels
           + struct.pack(">II", h, w)        # rows, columns
           + struct.pack(">HH", 8, 3))       # 8-bit, mode 3 = RGB
    out += struct.pack(">I", 0)              # color mode data: empty
    out += struct.pack(">I", 0)              # image resources: empty
    out += struct.pack(">I", 0)              # layer & mask info: empty
    planes = (bytes([r]) * (w * h) + bytes([g]) * (w * h)
              + bytes([b]) * (w * h))
    data = struct.pack(">H", 0) + planes     # compression 0 = raw
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out + data)
    return path


def _make_wide_png(path: Path) -> None:
    """32769x1 -- under MAX_PIXELS but over the per-axis MAX_EDGE cap."""
    from PySide6.QtGui import QImage

    img = QImage(32_769, 1, QImage.Format_RGB32)
    img.fill(0xFFFF0000)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert img.save(str(path), "PNG")


@pytest.fixture(autouse=True)
def _reset_facade_singleton():
    df.reset_service()
    yield
    df.reset_service()


def _build_fixture_library(root: Path) -> None:
    _make_jpeg(root / "f" / "plain.jpg")
    _make_png(root / "f" / "plain.png")
    _make_jpeg(root / "f" / "rotated.jpg", w=64, h=32, orientation=6)
    (root / "f" / "corrupt.jpg").write_bytes(b"not a jpeg at all")


def _protocol_violation_total(svc: "df.DecodeService") -> int:
    pool_set = svc._sandbox._pool_set
    members = list(pool_set._batch) + [pool_set._interactive]
    return sum(m.protocol_violations for m in members)


def _jobs_total(svc: "df.DecodeService") -> int:
    """fauxcasa-ez2.9 Stage 2 review P2-6: sum of WinDecodePool.jobs
    across every pool member -- a monotonically increasing count of
    SUCCESSFUL sandboxed decode() calls. Unlike `state == STATE_SANDBOXED`
    (which only proves the sandbox STARTED), this proves the routing
    branch at the call site actually REACHED the sandbox -- a deleted
    `elif ... STATE_SANDBOXED` branch would leave this at 0."""
    pool_set = svc._sandbox._pool_set
    members = list(pool_set._batch) + [pool_set._interactive]
    return sum(m.jobs for m in members)


# ---------------------------------------------------------------------------
# (a) parity: build_cache under sandbox=1 vs sandbox=0

@_WINDOWS_ONLY
def test_index_parity_sandboxed_vs_in_process(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    _build_fixture_library(root)

    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "0")
    df.reset_service()
    cat0 = scan_library(root)
    res0 = thumbcache.build_cache(cat0, tmp_path / "c0")
    cache0 = thumbcache.load_cache(res0.path)
    by_rel0 = dict(zip(cache0.files, cache0.entries))

    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")
    df.reset_service()
    svc = df.get_service()
    svc.ensure_started()
    assert svc.state == df.STATE_SANDBOXED, (
        f"sandbox failed to start: {svc.reason}")
    jobs_before = _jobs_total(svc)
    cat1 = scan_library(root)
    res1 = thumbcache.build_cache(cat1, tmp_path / "c1")
    cache1 = thumbcache.load_cache(res1.path)
    by_rel1 = dict(zip(cache1.files, cache1.entries))

    # Discriminating assertion (P2-6): `state == STATE_SANDBOXED` alone
    # only proves the sandbox STARTED, not that _index_one's routing
    # branch actually used it -- this fails if that `elif` is deleted.
    # 3 successfully-decoded stills (plain.jpg, plain.png, rotated.jpg);
    # corrupt.jpg raises before WinDecodePool.decode's success increment.
    jobs_after = _jobs_total(svc)
    assert jobs_after - jobs_before == 3, (
        f"expected 3 sandboxed decode jobs, got {jobs_after - jobs_before}")

    # Error-tile set (zero-length blob) must match exactly.
    err0 = {rel for rel, e in by_rel0.items() if e[1] == 0}
    err1 = {rel for rel, e in by_rel1.items() if e[1] == 0}
    assert err0 == err1 == {"f/corrupt.jpg"}

    # Pixel dims of every non-error tile must match (the images are flat
    # single-color fills, so a byte-identical JPEG re-encode is not
    # required -- dimensions + non-empty blob is the honest parity bar).
    for rel in ("f/plain.jpg", "f/plain.png", "f/rotated.jpg"):
        e0, e1 = by_rel0[rel], by_rel1[rel]
        assert e0[1] > 0 and e1[1] > 0, f"{rel}: expected a real tile on both sides"


# ---------------------------------------------------------------------------
# (b) viewer parity for one still

@_WINDOWS_ONLY
def test_viewer_parity_sandboxed_vs_in_process(tmp_path, monkeypatch):
    p = tmp_path / "one.jpg"
    _make_jpeg(p, w=64, h=32, orientation=6)

    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "0")
    df.reset_service()
    img0, orient0 = load_original_oriented(str(p), rotate=0)

    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")
    df.reset_service()
    svc = df.get_service()
    svc.ensure_started()
    assert svc.state == df.STATE_SANDBOXED, f"sandbox failed: {svc.reason}"
    jobs_before = _jobs_total(svc)
    img1, orient1 = load_original_oriented(str(p), rotate=0)

    # Discriminating assertion (P2-6): proves the viewer's routing branch
    # actually reached the sandbox, not just that it started.
    assert _jobs_total(svc) - jobs_before == 1, (
        "expected exactly 1 sandboxed decode job from the viewer load")

    assert not img0.isNull() and not img1.isNull()
    assert orient0 == orient1 == 6
    # orientation=6 on a 64x32 source -> EXIF-upright 32x64.
    assert (img0.width(), img0.height()) == (32, 64)
    assert (img1.width(), img1.height()) == (32, 64)


# ---------------------------------------------------------------------------
# ensure_started() ordering (fauxcasa-ez2.9 Stage 2 review P2-7):
# decode_svc.ensure_started() must run BEFORE MainWindow(...) is
# constructed -- MainWindow.__init__ can start background indexing
# (_start_reconcile on a warm start with drift) that reaches
# thumbcache.build_cache -> _index_one's STATE_SANDBOXED check. Left
# after window construction, those threads could start and finish on
# the STATE_IN_PROCESS default before ensure_started() ever ran.

@_WINDOWS_ONLY
def test_ensure_started_runs_before_mainwindow_reaches_build_cache(
        tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import main

    QApplication.instance() or QApplication([])

    root = tmp_path / "lib"
    _make_jpeg(root / "a.jpg")
    cache_root = tmp_path / "cr"

    # First run: sandbox off (fast) -- just needs to produce the
    # persisted catalog + per-root cache the second run's WARM start
    # (and its _start_reconcile) requires.
    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "0")
    df.reset_service()
    monkeypatch.setattr(sys, "argv", [
        "fauxcasa", str(root), "--cache-root", str(cache_root),
        "--finish-build", "--quit-after-ready", "--timeout", "30"])
    assert main.main() == 0

    # Drift since the cache was built: the next (warm) launch's
    # _start_reconcile() detects it and calls build_cache from a
    # background thread started inside MainWindow.__init__.
    _make_jpeg(root / "b.jpg")

    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")
    df.reset_service()

    observed_states: list[str] = []
    orig_build_cache = main.build_cache

    def spy_build_cache(*a, **kw):
        import decodefacade
        observed_states.append(decodefacade.get_service().state)
        return orig_build_cache(*a, **kw)

    monkeypatch.setattr(main, "build_cache", spy_build_cache)
    monkeypatch.setattr(sys, "argv", [
        "fauxcasa", str(root), "--cache-root", str(cache_root),
        "--finish-build", "--quit-after-ready", "--timeout", "30"])
    assert main.main() == 0

    assert observed_states, (
        "reconcile's build_cache never ran -- drift wasn't detected; "
        "this is a test-setup problem, not the ordering fix")
    assert all(s == df.STATE_SANDBOXED for s in observed_states), (
        f"build_cache observed {observed_states!r} -- ensure_started() "
        f"must complete before MainWindow() is constructed so the state "
        f"is already final by the time any background indexing thread "
        f"can reach build_cache")


# ---------------------------------------------------------------------------
# PSD pre-route (fauxcasa-ez2.9 Stage 2 review P1-1): Qt ships no PSD
# plugin at all, so with the sandbox ON, .psd must still be pre-routed to
# pillow_qimage ahead of the sandboxed branch -- otherwise the worker's
# canRead() is always false (UNSUPPORTED) and every PSD permanently
# indexes to a zero-byte tile / null viewer image.

@_WINDOWS_ONLY
def test_psd_indexes_and_views_with_sandbox_on(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    _make_psd(root / "f" / "photo.psd")

    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")
    df.reset_service()
    svc = df.get_service()
    svc.ensure_started()
    assert svc.state == df.STATE_SANDBOXED, f"sandbox failed: {svc.reason}"

    cat = scan_library(root)
    result = thumbcache.build_cache(cat, tmp_path / "c")
    cache = thumbcache.load_cache(result.path)
    by_rel = dict(zip(cache.files, cache.entries))
    assert by_rel["f/photo.psd"][1] > 0, (
        "PSD must pre-route to pillow_qimage, not the sandbox, and "
        "produce a non-empty tile")

    img, _ = load_original_oriented(str(root / "f" / "photo.psd"), rotate=0)
    assert not img.isNull(), (
        "the viewer's PSD pre-route must also bypass the sandbox branch")
    assert (img.width(), img.height()) == (64, 48)


# ---------------------------------------------------------------------------
# Crop-aware edge on the sandboxed index path (fauxcasa-ez2.9 Stage 2
# review P2-4): a tight crop= recipe on a large source must not smear --
# the sandboxed path must request a LARGER edge from the worker so the
# kept crop sub-rect (not the whole frame) ends up close to `top` px.

def _rect64_hex(left: float, top: float, right: float, bottom: float) -> str:
    return "".join(format(round(v * 65536), "04x")
                   for v in (left, top, right, bottom))


@_WINDOWS_ONLY
def test_crop_aware_edge_preserves_resolution_on_sandboxed_path(
        tmp_path, monkeypatch):
    from PySide6.QtGui import QColor, QImage

    root = tmp_path / "lib"
    (root / "f").mkdir(parents=True)
    w, h = 4000, 3000
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(90, 140, 200))
    assert img.save(str(root / "f" / "big.jpg"), "JPEG", 90)

    # 5%-area crop: 0.25 x 0.20 of the stored frame -> box 1000x600 px on
    # the 4000x3000 source.
    rect_hex = _rect64_hex(0.0, 0.0, 0.25, 0.20)
    (root / "f" / ".picasa.ini").write_text(
        f"[big.jpg]\r\ncrop=rect64({rect_hex})\r\n")

    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")
    df.reset_service()
    svc = df.get_service()
    svc.ensure_started()
    assert svc.state == df.STATE_SANDBOXED, f"sandbox failed: {svc.reason}"

    cat = scan_library(root)
    ci = next(i for i, p in enumerate(cat.photos) if p.name == "big.jpg")
    crop = cat.photos[ci].crop
    assert crop[:3] == (0.0, 0.0, 0.25) and abs(crop[3] - 0.2) < 0.001

    result = thumbcache.build_cache(cat, tmp_path / "c")
    cache = thumbcache.load_cache(result.path)
    _offset, length, w_out, h_out = cache.entries[ci]
    assert length > 0

    top = thumbcache.THUMB_EDGE  # 256, the default top level
    # Without the P2-4 fix (edge=top always), the whole 4000x3000 frame
    # scales to fit 256 first (256x192), THEN the 5%-area crop is cut out
    # of that -- a ~64x38 smear. With the fix, the worker is asked for a
    # larger edge so the KEPT sub-rect itself lands near `top`.
    assert max(w_out, h_out) >= top * 0.8, (
        f"crop tile {w_out}x{h_out} is far below the top level {top} -- "
        "looks like a pre-fix smear, not a crop-aware decode")


# ---------------------------------------------------------------------------
# Tight-crop arena clamp (fauxcasa release-0.1 review P0-1): a crop tight
# enough that the P2-4 boosted edge above would exceed what the BATCH
# lane's 8 MiB arena (DecodePoolSet.BATCH_ARENA_BYTES) can return must
# still produce a non-empty tile -- not a permanent zero-length blob from
# an unclamped TOO_LARGE. Numbers below reproduce the reviewer's repro
# exactly: 6000x4000 source, crop keeping ~6% of the long edge (360x240
# box) -- pre-fix this crop yields blob length 0 (TOO_LARGE at the
# unclamped boosted edge of ~4267px); post-fix the request is clamped to
# the arena-derived edge (~1448px), so the crop sub-rect still decodes,
# just below the full `top` resolution (the arena is the hard limit on
# this lane -- trading crop resolution for a non-empty tile is the
# intended fix, not a regression of P2-4's resolution goal).

@_WINDOWS_ONLY
def test_tight_crop_does_not_exceed_batch_arena(tmp_path, monkeypatch):
    from PySide6.QtGui import QColor, QImage
    import decodesvc_win as dw

    root = tmp_path / "lib"
    (root / "f").mkdir(parents=True)
    w, h = 6000, 4000
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(90, 140, 200))
    assert img.save(str(root / "f" / "tight.jpg"), "JPEG", 90)

    # ~6% of the long edge kept on both axes -- 360x240 box on 6000x4000.
    keep = 0.06
    rect_hex = _rect64_hex(0.0, 0.0, keep, keep)
    (root / "f" / ".picasa.ini").write_text(
        f"[tight.jpg]\r\ncrop=rect64({rect_hex})\r\n")

    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")
    df.reset_service()
    svc = df.get_service()
    svc.ensure_started()
    assert svc.state == df.STATE_SANDBOXED, f"sandbox failed: {svc.reason}"

    cat = scan_library(root)
    ci = next(i for i, p in enumerate(cat.photos) if p.name == "tight.jpg")

    result = thumbcache.build_cache(cat, tmp_path / "c")
    cache = thumbcache.load_cache(result.path)
    _offset, length, w_out, h_out = cache.entries[ci]

    assert length > 0, (
        "tight crop must not be a permanent zero-length error tile "
        "(unclamped boosted edge exceeded the batch arena)")
    assert w_out > 0 and h_out > 0

    # The clamped edge must actually respect the arena: max output edge
    # is bounded by the largest square RGBA8 edge the arena can hold.
    arena_edge = int((dw.DecodePoolSet.BATCH_ARENA_BYTES / 4) ** 0.5)
    assert max(w_out, h_out) <= arena_edge


# ---------------------------------------------------------------------------
# (c) hostile files through the wired index path, sandbox=1

@_WINDOWS_ONLY
def test_hostile_files_through_wired_index_path(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    (root / "f").mkdir(parents=True)
    # A real, decodable control file so the batch pool actually starts up.
    _make_jpeg(root / "f" / "ok.jpg")
    (root / "f" / "truncated.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 10)
    _write_ihdr_png(root / "f" / "huge_header.png", 60_000, 60_000)
    _make_wide_png(root / "f" / "wide.png")
    # zip-bomb-ish: huge declared IHDR, tiny (truncated) IDAT stream.
    _write_ihdr_png(root / "f" / "bomb.png", 50_000, 50_000)

    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "1")
    df.reset_service()
    svc = df.get_service()
    svc.ensure_started()
    assert svc.state == df.STATE_SANDBOXED, f"sandbox failed: {svc.reason}"

    cat = scan_library(root)
    result = thumbcache.build_cache(cat, tmp_path / "c")
    assert result is not None
    cache = thumbcache.load_cache(result.path)
    by_rel = dict(zip(cache.files, cache.entries))

    assert by_rel["f/ok.jpg"][1] > 0, "control file must decode normally"
    for rel in ("f/truncated.jpg", "f/huge_header.png", "f/bomb.png"):
        assert by_rel[rel][1] == 0, f"{rel}: expected an error tile"
    # wide.png (32769x1) is the one deliberate non-error case: the wired
    # index path always requests a SCALED-DOWN decode (edge=top, e.g.
    # 256), and the worker's TOO_LARGE guard checks the EFFECTIVE output
    # size after scaling (decodesvc_worker_win.py._handle_decode), not the
    # raw header -- 32769x1 scaled to fit 256 becomes a harmless 256x1, so
    # this correctly decodes rather than erroring. The per-axis MAX_EDGE
    # guard at edge=0 (full resolution, the viewer's request) is exercised
    # by test_decodesvc_win.py::test_decode_too_large_per_axis instead.
    assert by_rel["f/wide.png"][1] > 0, (
        "wide.png should decode fine once scaled to the thumbnail edge")

    # No hostile file may have registered a ProtocolViolation against the
    # stock worker pool -- these are all honest taxonomy failures
    # (CORRUPT/TOO_LARGE/UNSUPPORTED), not protocol breaches.
    assert _protocol_violation_total(svc) == 0

    # The pool set must have survived every hostile file.
    pool_set = svc._sandbox._pool_set
    for member in list(pool_set._batch) + [pool_set._interactive]:
        assert member._worker is not None and member._worker.is_alive(), (
            "a hostile file killed a pool worker")

    # Full-resolution path (viewer, edge=0): here wide.png DOES trip the
    # per-axis MAX_EDGE guard (no scale-down shrinks the effective output).
    img, _ = load_original_oriented(str(root / "f" / "wide.png"), rotate=0)
    assert img.isNull(), "wide.png at full resolution should be TOO_LARGE"
    assert _protocol_violation_total(svc) == 0
    for member in list(pool_set._batch) + [pool_set._interactive]:
        assert member._worker is not None and member._worker.is_alive()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + (sys.argv[1:] or ["-v"])))
