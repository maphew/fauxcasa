"""Tests for main.py/viewer.py fcache v2 loupe and hi-DPI consumer behavior.

Split from test_tracer.py (fauxcasa-l09); originally lines 2263-3135 of the monolith."""

from __future__ import annotations

import json
import sys
from pathlib import Path
import pytest
import thumbcache
from catalog import (
    load_catalog,
    save_catalog,
    scan_library,
)
from tracer_helpers import (
    _big_library,
    _bound_cache,
    _iptc_app13,
    _offscreen_app,
    make_jpeg,
    write_jpeg_meta,
)


# --- the fcache v2 loupe / hi-DPI consumer (fauxcasa-9pp): the viewer paints
# an instant cached preview — the nearest level >= the viewport's device
# pixels, read via best_level()/entry() — while the full original decodes
# off-thread. A v2 cache hands it the >256 (512) level; a v1 cache its 256
# level; no cache or an error tile -> no preview, just the loading text. This
# is the first consumer of the larger levels gtr shipped.


def test_viewer_preview_reads_larger_v2_level(tmp_path: Path) -> None:
    """A large window makes best_level() pick the v2 512 top — the >256 level
    nothing consumed before gtr — and the painted preview IS that level's
    image (its long edge matches entry(idx, level))."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)                          # 600x400 + 400x600 -> real 512
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256, 128])
    viewer = ViewerPage(cat, cache)
    viewer.resize(1280, 800)                     # large window -> min_edge > 512
    min_edge = viewer._preview_min_edge()
    level = cache.best_level(min_edge)
    viewer.show_photo(list(range(cache.count)), 0)
    assert viewer.preview is not None and not viewer.preview.isNull()
    _o, _l, w0, h0 = cache.entry(0, level)       # land.jpg @ 512: 512x341
    assert max(viewer.preview.width(), viewer.preview.height()) == max(w0, h0)
    # the whole point: a window this large clears 256 and reads the >256 level
    assert min_edge >= 512 and level == 0 and max(w0, h0) == 512


def test_viewer_decode_job_that_raises_clears_loading(tmp_path: Path,
                                                      monkeypatch) -> None:
    """A decoder that RAISES (not just returns null) must leave the viewer
    in the 'could not decode' state, not stuck on 'loading…': the persistent
    decode thread survives the exception and a null image is still emitted.
    Before the guard, the exception killed the worker thread silently and
    `loading` stayed True forever — a scripted --open/--screenshot run then
    sat on `viewer.loading` until its timeout (seen on CI's native leg)."""
    import time as _time

    import viewer as viewermod
    from viewer import ViewerPage

    app = _offscreen_app()
    root = tmp_path / "lib"
    _big_library(root)
    cat, cache = _bound_cache(tmp_path, root)
    view = ViewerPage(cat, cache)
    view.resize(400, 300)

    calls = {"n": 0}
    real = viewermod.load_original_oriented

    def boom(path, rotate, crop):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("decoder exploded")
        return real(path, rotate, crop)

    monkeypatch.setattr(viewermod, "load_original_oriented", boom)
    view.show_photo(list(range(cache.count)), 0)
    assert view.loading
    deadline = _time.monotonic() + 5.0
    while view.loading and _time.monotonic() < deadline:
        app.processEvents()
        _time.sleep(0.01)
    assert not view.loading, "loading never cleared after a raising decode"
    assert view.image is None                      # honest failure state
    assert view._decoder is not None and view._decoder.is_alive()

    # The same worker keeps serving: the next photo decodes normally.
    view.show_photo(list(range(cache.count)), 1)
    deadline = _time.monotonic() + 5.0
    while view.loading and _time.monotonic() < deadline:
        app.processEvents()
        _time.sleep(0.01)
    assert not view.loading and view.image is not None
    assert calls["n"] == 2
    view.quiesce()


def test_viewer_preview_v1_falls_back_to_256(tmp_path: Path) -> None:
    """A single-level v1 cache has only 256; the viewer still shows an instant
    256 preview rather than a blank window (graceful, no v2 required)."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)
    cat, cache = _bound_cache(tmp_path, root)    # default -> v1 [256]
    assert cache.levels == [256]
    viewer = ViewerPage(cat, cache)
    viewer.resize(1280, 800)
    viewer.show_photo(list(range(cache.count)), 0)
    assert viewer.preview is not None
    assert max(viewer.preview.width(), viewer.preview.height()) <= 256


def test_viewer_no_preview_without_cache(tmp_path: Path) -> None:
    """No cache yet (cold start before the build lands) -> no preview and no
    crash; the viewer shows its loading text and still loads the original."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)
    cat = scan_library(root)
    viewer = ViewerPage(cat, None)
    viewer.resize(1280, 800)
    viewer.show_photo(list(range(len(cat.photos))), 0)
    assert viewer.preview is None


def test_viewer_error_tile_yields_no_preview(tmp_path: Path) -> None:
    """An error-tile entry (zero-length blob, original undecodable at build)
    yields no preview; a good neighbour in the same cache still previews."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "ok.jpg", 600, 400)
    (root / "f" / "bad.jpg").write_bytes(b"not a jpeg at all")
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256, 128])
    by_rel = {f: i for i, f in enumerate(cache.files)}
    viewer = ViewerPage(cat, cache)
    viewer.resize(1280, 800)
    assert viewer._load_preview(by_rel["f/bad.jpg"], 0) is None   # error tile
    assert viewer._load_preview(by_rel["f/ok.jpg"], 0) is not None


def test_viewer_original_supersedes_preview_then_falls_back(
        tmp_path: Path) -> None:
    """The decoded original replaces (and frees) the preview; but if the
    original FAILS to decode, the cached preview keeps painting rather than
    dropping straight to 'could not decode'. release-0.1 review P2-6: this
    silent-degrade case must also surface a short honest note in the info
    bar (paintEvent never shows "could not decode this file" here, since a
    preview is still on screen) -- otherwise a >64 MP panorama just looks
    permanently blurry with no explanation."""
    _offscreen_app()
    from PySide6.QtGui import QImage
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256])
    viewer = ViewerPage(cat, cache)
    viewer.resize(1280, 800)
    viewer.show_photo(list(range(cache.count)), 0)
    assert viewer.preview is not None
    assert viewer._decode_note is None
    orig = QImage(800, 600, QImage.Format.Format_RGB32)
    orig.fill(0)
    viewer._on_loaded(viewer._serial, orig)         # the real original lands
    assert viewer.image is orig and viewer.preview is None
    assert viewer._decode_note is None
    viewer.show_photo(list(range(cache.count)), 1)  # next photo: preview again
    assert viewer.preview is not None
    assert viewer._decode_note is None              # reset on navigation
    viewer._on_loaded(viewer._serial, QImage())     # original failed to decode
    assert viewer.image is None and viewer.preview is not None
    assert viewer._decode_note, (
        "a null original with a preview showing must set an honest note"
    )
    assert viewer._decode_note in viewer._info_text(
        cat.photos[viewer.current_index()])


def test_viewer_preview_composes_picasa_rotate(tmp_path: Path) -> None:
    """The preview composes the Picasa rotate= quarter-turns on top of the
    EXIF-upright cached thumb, exactly as the grid and the original path do —
    a 90 deg turn makes a landscape thumb paint portrait."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    make_jpeg(root / "land.jpg", 600, 400)          # landscape thumb (w > h)
    (root / ".picasa.ini").write_text(
        "[land.jpg]\r\nrotate=rotate(1)\r\n")       # one quarter-turn CW
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256, 128])
    p = next(p for p in cat.photos if p.rel == "land.jpg")
    assert p.rotate == 1
    _o, _l, w0, h0 = cache.entry(0, 0)
    assert w0 > h0                                   # cached thumb is landscape
    viewer = ViewerPage(cat, cache)
    viewer.resize(1280, 800)
    rotated = viewer._load_preview(0, p.rotate)
    assert rotated is not None and rotated.height() > rotated.width()  # portrait


def test_viewer_display_rect_preview_fills_original_caps() -> None:
    """The pure paint geometry: the preview (cap=False) fills the viewport box;
    a window-sized original (cap=True) lands on the SAME rect, so the hand-off
    is a sharpen-in-place. A small ORIGINAL caps at native (never upscaled),
    while the same small source as a PREVIEW fills the box (the accepted one-off
    pop). Degenerate 1x1 never yields a zero-size rect."""
    _offscreen_app()
    from viewer import ViewerPage
    big = ViewerPage._display_rect(1280, 800, 4000, 3000, cap=True)   # original
    prev = ViewerPage._display_rect(1280, 800, 512, 384, cap=False)   # preview
    assert big == prev                                   # identical 4:3 fit
    assert big.height() == 800 and 0 < big.width() <= 1280   # fills the box
    small_orig = ViewerPage._display_rect(1280, 800, 800, 600, cap=True)
    assert (small_orig.width(), small_orig.height()) == (800, 600)    # native
    small_prev = ViewerPage._display_rect(1280, 800, 800, 600, cap=False)
    assert small_prev.width() > 800 and small_prev.height() > 600     # filled
    assert ViewerPage._display_rect(1280, 800, 1, 1, cap=True).width() == 1


def test_viewer_stale_original_is_dropped(tmp_path: Path) -> None:
    """A late original carrying a superseded serial (user navigated on before it
    decoded) is dropped by the secondary guard in _on_loaded — it must not
    overwrite the current photo's freshly-decoded preview."""
    _offscreen_app()
    from PySide6.QtGui import QImage
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256])
    viewer = ViewerPage(cat, cache)
    viewer.resize(1280, 800)
    viewer.show_photo(list(range(cache.count)), 0)
    stale = viewer._serial
    viewer.show_photo(list(range(cache.count)), 1)   # serial advances; new preview
    p1 = viewer.preview
    assert p1 is not None
    viewer._on_loaded(stale, QImage(800, 600, QImage.Format.Format_RGB32))
    assert viewer.image is None and viewer.preview is p1   # untouched


def test_viewer_preview_min_edge_floors_at_grid_tile(tmp_path: Path) -> None:
    """A tiny window floors min_edge at the grid's 256 px tile, so the preview
    never drops below the grid — best_level picks the 256 level, not the 128."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256, 128])
    viewer = ViewerPage(cat, cache)
    viewer.resize(120, 120)
    assert viewer._preview_min_edge() == 256
    assert cache.best_level(viewer._preview_min_edge()) == 1   # 256, not 128


def test_viewer_preview_dpr_selects_larger_level(
        tmp_path: Path, monkeypatch) -> None:
    """The hi-DPI path: device-pixel-ratio multiplies the logical viewport, so
    the SAME small window selects the 256 level at DPR 1 but the larger 512
    level at DPR 2 — best_level() reads a >256 level only because of the DPR
    scaling, which offscreen (DPR 1.0) alone could never exercise."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256, 128])
    viewer = ViewerPage(cat, cache)
    viewer.resize(200, 200)
    monkeypatch.setattr(viewer, "devicePixelRatioF", lambda: 1.0)
    assert viewer._preview_min_edge() == 256                  # 200 floored to 256
    assert cache.best_level(viewer._preview_min_edge()) == 1  # the 256 level
    monkeypatch.setattr(viewer, "devicePixelRatioF", lambda: 2.0)
    assert viewer._preview_min_edge() == 400                  # 200 * 2 device px
    assert cache.best_level(viewer._preview_min_edge()) == 0  # the >256 512 level


def test_mainwindow_wires_viewer_cache_on_build_and_reconcile(
        tmp_path: Path) -> None:
    """The integration this bead adds: MainWindow hands the viewer the cache
    pair on a cold-build finish (_on_index_finished) and on a reconcile swap
    (reload_data) — the same cache the grid gets, so both consume v2."""
    _offscreen_app()
    import main
    root = tmp_path / "lib"
    _big_library(root)
    cat = scan_library(root)
    win = main.MainWindow(cat, None, cache_dir=None, build_dir=None)
    assert win.viewer.thumbs is None                  # cold start: no cache yet
    built = thumbcache.build_cache(cat, tmp_path / "c", levels=[512, 256])
    win._on_index_finished(built, win.catalog, False)  # cold-build finish
    assert win.viewer.thumbs is not None
    assert win.viewer.thumbs.levels == [512, 256]
    assert win.grid.thumbs is win.viewer.thumbs        # one cache, both consumers
    cat2 = scan_library(root)
    cache2 = thumbcache.load_cache(thumbcache.build_cache(
        cat2, tmp_path / "c2").path)                   # a fresh (v1) cache object
    thumbcache.bind(cache2, cat2)
    win.reload_data(cat2, cache2)                      # reconcile swap
    assert win.viewer.catalog is cat2 and win.viewer.thumbs is cache2


def test_mainwindow_title_is_library_then_product(tmp_path: Path) -> None:
    """The title answers which library is open before naming Fauxcasa."""
    _offscreen_app()
    import main

    root = tmp_path / "Family photos"
    _big_library(root)
    win = main.MainWindow(scan_library(root), None,
                          cache_dir=None, build_dir=None)
    assert win.windowTitle() == "Family photos — Fauxcasa"


def test_main_version_cli(monkeypatch, capsys) -> None:
    """--version is a script-friendly product release identity."""
    import main

    monkeypatch.setattr(sys, "argv", ["fauxcasa", "--version"])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 0
    assert capsys.readouterr().out == "Fauxcasa 0.1.0\n"


def test_index_priority_is_stable_complete_and_filters_bad_indices() -> None:
    """Gallery-first scheduling changes submission order only: duplicates
    and nonsense are ignored and every cache index still appears once."""
    assert thumbcache._index_submission_order(
        7, [4, 1, 4, -1, 99, 3]) == [4, 1, 3, 0, 2, 5, 6]
    assert thumbcache._index_submission_order(4, None) == [0, 1, 2, 3]


def test_index_activity_row_is_prominent_and_determinate(
        library: Path) -> None:
    """Index progress is directly below the toolbar, not discoverable only
    in the bottom status bar, and is removed once the job finishes."""
    import main

    _offscreen_app()
    win = main.MainWindow(scan_library(library), None,
                          cache_dir=None, build_dir=None)
    assert win.activity_row.isHidden()
    win._build_progress(37, 100)
    assert not win.activity_row.isHidden()
    assert win.activity_label.text() == \
        "Indexing thumbnails — 37 of 100 ready (37%)"
    assert win.activity_progress.maximum() == 100
    assert win.activity_progress.value() == 37
    assert win.activity_progress.isTextVisible() is False  # count lives in
                                                            # activity_label
    # No progress_label duplicate (fauxcasa-ez2.6 §7): the activity row
    # already carries the same count/percent while it's visible.
    assert win.progress_label.text() == ""
    win._on_index_finished(None, win.catalog, False)
    assert win.activity_row.isHidden()


def test_status_text_never_raises_the_window_resize_floor(
        library: Path) -> None:
    """fauxcasa-a3m: a long status/activity string must not become the
    window's minimum width.

    QMainWindow enforces its layout's minimum size as a hard resize
    floor, and a plain QLabel with word wrap off reports its full
    sizeHint as its minimumSizeHint. So selecting a photo under a deep
    path used to push the floor past 2700px: the window could be dragged
    bigger but never smaller, and the next relayout (the activity row
    appearing) grew the window to the new floor on its own. ElidingLabel
    decouples the two -- the floor is whatever the real chrome needs and
    stays there no matter how long the text gets."""
    from PySide6.QtWidgets import QApplication
    import main

    _offscreen_app()
    win = main.MainWindow(scan_library(library), None,
                          cache_dir=None, build_dir=None)
    win.resize(700, 600)
    win.show()
    # Baseline with every text-carrying widget already VISIBLE but empty,
    # so the comparison below isolates the TEXT. Showing the activity row
    # does legitimately raise the floor a little (activity_progress has a
    # real 280px minimum), and that structural cost is not what a3m is
    # about; only text-driven growth is.
    win.decode_sandbox_label.setVisible(True)
    win.activity_row.show()
    floor = win.minimumSizeHint().width()

    long_text = (r"C:\Users\Someone\Pictures\2019\Summer Vacation Trip"
                 r"\IMG_20190712_143512_a_very_long_filename.jpg"
                 "   sunset over the bay   keywords: beach, family, "
                 "vacation, sunset, ocean, holiday, coastline") * 2
    win.meta_label.setText(long_text)
    win.counts_label.setText(long_text)
    win.progress_label.setText(long_text)
    win.decode_sandbox_label.setText(long_text)
    win.activity_label.setText(long_text)

    assert win.minimumSizeHint().width() <= floor, \
        "status text widened the window's minimum size"
    # ...and the floor is small enough to actually be a floor, not the
    # 2700px trap this test exists to catch.
    assert win.minimumSizeHint().width() < 700
    # The window must also not have GROWN itself to a new floor. The
    # growth was never immediate -- Qt applied the new minimum on the
    # NEXT layout pass -- so drive one before believing the width.
    win.layout().activate()
    QApplication.processEvents()
    assert win.width() == 700
    # And the floor must not block a deliberate shrink, which is the
    # user-visible symptom the border handles were reporting.
    win.resize(640, 480)
    QApplication.processEvents()
    assert win.width() == 640

    # The text is elided for display only: callers and tests still read
    # the full string back, and the cut part is a hover away.
    assert win.meta_label.text() == long_text
    win.meta_label.grab()          # force a paint -> tooltip refresh
    assert long_text in win.meta_label.toolTip()


def test_eliding_label_leaves_short_text_and_owned_tooltips_alone(
        library: Path) -> None:
    """fauxcasa-a3m: ElidingLabel only intervenes when the text does not
    fit, and it only ever owns a tooltip it installed itself.

    Text that fits paints through QLabel untouched and gets no tooltip
    (an ellipsis-free label with a hover repeating itself is noise). A
    tooltip the OWNER set always wins: decode_sandbox_label shows the
    degrade reason there, and an eliding tooltip overwriting it would
    replace the one piece of diagnosis the banner exists to carry."""
    import main

    _offscreen_app()
    win = main.MainWindow(scan_library(library), None,
                          cache_dir=None, build_dir=None)
    win.resize(900, 600)
    win.show()

    # Sized explicitly rather than trusting the status bar's layout: the
    # contract under test is "the text fits", and a widget's allocated
    # width inside a just-shown window is not settled enough to assert on.
    fits = main.ElidingLabel()
    fits.setText("a.jpg")
    fits.resize(400, 16)
    fits.grab()
    assert fits.toolTip() == ""
    # ...and once it stops fitting, the tooltip appears...
    fits.resize(20, 16)
    fits.grab()
    assert fits.toolTip() == "a.jpg"
    # ...and is withdrawn again when there is room, so a stale hover
    # does not outlive the ellipsis that justified it.
    fits.resize(400, 16)
    fits.grab()
    assert fits.toolTip() == ""

    win.decode_sandbox_label.setToolTip("worker exited: STATUS_ACCESS_DENIED")
    win.decode_sandbox_label.setText(
        "Decoding is not sandboxed on this machine: " + "x" * 400)
    win.decode_sandbox_label.setVisible(True)
    win.decode_sandbox_label.resize(120, 16)
    win.decode_sandbox_label.grab()
    assert win.decode_sandbox_label.toolTip() == \
        "worker exited: STATUS_ACCESS_DENIED"


def test_eliding_label_paints_like_a_qlabel_when_it_elides() -> None:
    """fauxcasa-a3m: the elided branch hand-paints the text, so it has to
    reproduce what QLabel would have done rather than merely put pixels
    somewhere.

    Three properties that a raw painter.drawText() gets wrong and
    QStyle.drawItemText gets right, checked on real rendered pixels
    because a tooltip assertion cannot see any of them: the text is
    actually drawn; a style sheet's color: reaches it (status labels
    inherit one from the activity row); and alignment is honoured, so a
    right-aligned permanent widget like meta_label does not jump to the
    left edge the moment it elides."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    import main

    _offscreen_app()

    def render(label, w=90, h=18):
        label.resize(w, h)
        return label.grab().toImage()

    def hits(img, want: QColor, tol=60):
        """Pixels close to `want`, and the leftmost/rightmost columns
        holding one."""
        cols = [x for x in range(img.width()) for y in range(img.height())
                if (abs(img.pixelColor(x, y).red() - want.red()) < tol
                    and abs(img.pixelColor(x, y).green() - want.green()) < tol
                    and abs(img.pixelColor(x, y).blue() - want.blue()) < tol)]
        return len(cols), (min(cols) if cols else -1), \
            (max(cols) if cols else -1)

    long_text = "a-very-long-status-readout-that-cannot-possibly-fit " * 4

    # 1 + 2: a style sheet colour survives the custom paint.
    red = main.ElidingLabel()
    red.setStyleSheet("color: #d0021b;")
    red.setText(long_text)
    n, _lo, _hi = hits(render(red), QColor("#d0021b"))
    assert n > 0, "elided text was not painted in the style sheet's colour"

    # 3: alignment steers the geometry, measured against the ORACLE this
    # class claims to reproduce -- a real QLabel holding the ALREADY
    # elided string, same font, same box, same flags. Absolute ink
    # columns cannot carry this assertion: ElideRight fits the string to
    # the box, so the leftover gap at the far edge is only whatever the
    # last glyph's metrics leave behind (~1px on Segoe UI, 14px on DejaVu
    # Sans), which is exactly why an absolute "right-aligned ink reaches
    # x >= 85" threshold passed on Windows and failed on Linux.
    #
    # An indent is what makes the two alignments differ by a visible
    # margin on ANY font, because QLabel applies it to the aligned edge
    # only: with AlignLeft the text box starts `INDENT` px in (ink sits
    # right), with AlignRight it ends `INDENT` px early (ink sits left).
    # That also exercises the two pieces of geometry this class
    # reimplements -- _visual_alignment() and _indent().
    from PySide6.QtWidgets import QLabel

    dark = QColor("#000000")
    INDENT = 20

    def ink_pair(align):
        """Ink extents for `long_text` at `align`, ours and QLabel's."""
        flags = align | Qt.AlignmentFlag.AlignVCenter
        ours = main.ElidingLabel()
        ours.setAlignment(flags)
        ours.setIndent(INDENT)
        ours.setText(long_text)
        oracle = QLabel()
        oracle.setTextFormat(Qt.TextFormat.PlainText)
        oracle.setAlignment(flags)
        oracle.setIndent(INDENT)
        # What ElidingLabel's paint path will hand to drawItemText: the
        # box is the 90px widget less the indent it applies to one edge.
        oracle.setText(ours.fontMetrics().elidedText(
            long_text, Qt.TextElideMode.ElideRight, 90 - INDENT))
        return (hits(render(ours), dark, tol=100),
                hits(render(oracle), dark, tol=100))

    (l_n, l_lo, l_hi), (ql_n, ql_lo, ql_hi) = \
        ink_pair(Qt.AlignmentFlag.AlignLeft)
    (r_n, r_lo, r_hi), (qr_n, qr_lo, qr_hi) = \
        ink_pair(Qt.AlignmentFlag.AlignRight)
    assert l_n > 0 and r_n > 0 and ql_n > 0 and qr_n > 0, \
        "elided text was not painted at all"
    # Non-vacuity guard (the lesson of fauxcasa-9pr/47f): if a real
    # QLabel puts both alignments in the same place on this font, the
    # comparisons below would pass for a paint path that ignores
    # alignment entirely, and the test would prove nothing.
    assert abs(ql_lo - qr_lo) > 4 and abs(ql_hi - qr_hi) > 4, (
        "test is vacuous on this font: QLabel itself paints indented "
        f"left- and right-aligned text alike (left {ql_lo}-{ql_hi}, "
        f"right {qr_lo}-{qr_hi})")
    assert abs(l_lo - ql_lo) <= 2 and abs(l_hi - ql_hi) <= 2, (
        "left-aligned elided text is not where QLabel puts it: "
        f"ours {l_lo}-{l_hi}, QLabel {ql_lo}-{ql_hi}")
    assert abs(r_lo - qr_lo) <= 2 and abs(r_hi - qr_hi) <= 2, (
        "right-aligned elided text is not where QLabel puts it: "
        f"ours {r_lo}-{r_hi}, QLabel {qr_lo}-{qr_hi}")

    # 4: a DISABLED label greys out. (A hand-rolled painter.setPen also
    # passes this one, since QStyleOption.initFrom already hands over the
    # disabled colour group -- it is here as a true property of the
    # surface, not as the discriminator. Assertion 5 is that.)
    off = main.ElidingLabel()
    off.setText(long_text)
    off.setEnabled(False)
    on = main.ElidingLabel()
    on.setText(long_text)
    off_img, on_img = render(off), render(on)

    def mean_lightness(img):
        vals = [img.pixelColor(x, y).lightness()
                for x in range(img.width()) for y in range(img.height())]
        return sum(vals) / len(vals)

    assert mean_lightness(off_img) > mean_lightness(on_img) + 2, \
        "disabled elided text was painted as dark as enabled text"

    # 5: RTL uses VISUAL alignment, as QLabel does -- an explicit
    # AlignLeft renders on the right under a right-to-left layout.
    # This is what pins the paint path to QStyle.drawItemText +
    # QStyle.visualAlignment: swapping in a plain painter.drawText with
    # logical alignment leaves the text on the left and fails here,
    # which is the whole reason this test is worth its length.
    # Measured the same way as assertion 3, and for the same reason: an
    # absolute "ink reaches x >= 85" threshold is font-dependent and fails
    # on DejaVu Sans while passing on Segoe UI. The claim here is a
    # relation, not a coordinate -- under RTL an explicit AlignLeft must
    # land exactly where LTR AlignRight lands, and nowhere near where LTR
    # AlignLeft lands. The indent (applied to the VISUAL aligned edge)
    # keeps those two places far apart on any font.
    rtl = main.ElidingLabel()
    rtl.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    rtl.setAlignment(Qt.AlignmentFlag.AlignLeft
                     | Qt.AlignmentFlag.AlignVCenter)
    rtl.setIndent(INDENT)
    rtl.setText(long_text)
    rtl_n, rtl_lo, rtl_hi = hits(render(rtl), dark, tol=100)
    assert rtl_n > 0, "RTL elided text was not painted at all"
    assert abs(rtl_lo - r_lo) <= 2 and abs(rtl_hi - r_hi) <= 2, (
        "RTL label used logical alignment instead of visual: AlignLeft "
        f"under RTL painted at {rtl_lo}-{rtl_hi}, but LTR AlignRight "
        f"paints at {r_lo}-{r_hi}")
    assert abs(rtl_lo - l_lo) > 4, (
        "RTL AlignLeft landed on top of LTR AlignLeft, so this assertion "
        f"cannot tell visual from logical alignment (both {rtl_lo}-"
        f"{rtl_hi})")


def test_eliding_label_tooltip_never_renders_catalog_text_as_markup(
) -> None:
    """fauxcasa-a3m + fauxcasa-6vk finding 7: the auto tooltip carries
    catalog text (captions, keywords, paths), and QToolTip has no
    plain-text mode -- it hands the string to an AutoText QLabel. A
    caption of '<img src=http://…>' must reach the tooltip as escaped
    HTML, not as a fetched external image."""
    from PySide6.QtCore import QSize
    import main

    _offscreen_app()
    label = main.ElidingLabel()
    label.setText('<img src=http://example.invalid/x.png> ' + "y" * 300)
    label.resize(60, 16)
    label.grab()
    tip = label.toolTip()
    assert "&lt;img" in tip or tip.startswith("<html>")
    assert "<img src=" not in tip
    assert isinstance(label.minimumSizeHint(), QSize)


def test_index_finished_status_reads_library_ready(
        library: Path, tmp_path: Path) -> None:
    """fauxcasa-ez2.6 §7: the completion status bar message is a plain
    "Library ready — N photos" verdict, not an indexing-rate number
    nobody but a dev cares about. The machine-readable JSON "indexed"
    event (§7 protocol) keeps rate_per_s untouched — only the human
    status-bar wording changed."""
    import thumbcache
    _offscreen_app()
    from main import MainWindow

    cat = scan_library(library)
    result = thumbcache.build_cache(cat, tmp_path / "c")
    assert result is not None and result.photos == len(cat.photos)

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win._on_index_finished(result, cat, False)
    assert win.statusBar().currentMessage() == \
        f"Library ready — {len(cat.photos):,} photos"


def test_grid_stop_retires_decode_workers() -> None:
    """GridView.stop() retires its decode-worker pool so the daemons don't
    leak and accumulate across a process (fauxcasa-gfz). After stop() no worker
    of this grid is still alive, and stop() is idempotent (closeEvent + the
    autouse teardown may both call it). The immortal pool was what raced the
    main thread on Qt state and crashed a later test on Windows."""
    _offscreen_app()
    from grid import GridView, WORKERS

    g = GridView()
    workers = list(g._workers)
    assert len(workers) == WORKERS and all(t.is_alive() for t in workers)
    g.stop()
    for t in workers:
        assert not t.is_alive()   # joined and exited on the sentinel
    g.stop()                      # idempotent, no error, no re-stop


def test_index_empty_infile_caption_keeps_ini(tmp_path: Path) -> None:
    """An empty/whitespace in-file caption is 'no caption', not "" — it must
    not clobber the ini caption (§4 precedence), and the catalog must still
    round-trip (no '' vs None divergence vs a warm load)."""
    root = tmp_path / "lib"
    # caption present-but-empty in IPTC, plus a real ini caption
    write_jpeg_meta(root / "f" / "p.jpg", iptc=_iptc_app13(caption="   "))
    (root / "f" / ".picasa.ini").write_text("[p.jpg]\r\ncaption=real ini\r\n")
    cat = scan_library(root)
    thumbcache.build_cache(cat, tmp_path / "c")
    p = next(p for p in cat.photos if p.rel == "f/p.jpg")
    assert p.caption == "real ini"  # the empty in-file value did not win

    path = tmp_path / "cat.json"
    save_catalog(cat, path)
    loaded = load_catalog(path, root)
    assert loaded is not None
    lp = next(p for p in loaded.photos if p.rel == "f/p.jpg")
    assert lp.caption == "real ini"  # round-trips; no empty-string leak


def test_bundle_dependency_probe_covers_lazy_runtime_matrix(monkeypatch) -> None:
    """A missing lazy module fails the frozen gate without leaking the
    exception text (which can contain machine-local paths)."""
    import main

    seen = []

    def fake_import(name):
        seen.append(name)
        if name == "exiv2":
            raise ImportError("private machine path must not escape")
        return object()

    monkeypatch.setattr(main.importlib, "import_module", fake_import)
    assert main._bundle_dependency_failures() == {"exiv2": "ImportError"}
    assert seen == list(main.BUNDLE_RUNTIME_MODULES)


def test_bundle_self_check_cli_exits_before_library_resolution(
        monkeypatch, capsys) -> None:
    """The frozen CI probe needs neither a display nor a photo library."""
    import main

    monkeypatch.setattr(main, "_bundle_dependency_failures", lambda: {})
    monkeypatch.setattr(sys, "argv", ["main.py", "--bundle-self-check"])
    assert main.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "event": "bundle-self-check",
        "failures": {},
        "modules": list(main.BUNDLE_RUNTIME_MODULES),
    }


def test_default_cache_root_frozen_vs_checkout(monkeypatch, tmp_path: Path) -> None:
    """main._default_cache_root: REPO-relative in a source checkout; a
    per-user writable dir when frozen (REPO then points inside the
    read-only PyInstaller bundle, so the disposable cache must go
    somewhere writable). XDG_CACHE_HOME wins over the ~/.cache fallback."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    monkeypatch.setattr(main, "FROZEN", False)
    assert main._default_cache_root() == main.REPO / "cache" / "fauxcasa-cache"

    monkeypatch.setattr(main, "FROZEN", True)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert main._default_cache_root() == tmp_path / "xdg" / "fauxcasa"

    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(main.Path, "home",
                        classmethod(lambda cls: tmp_path / "home"))
    assert (main._default_cache_root()
            == tmp_path / "home" / ".cache" / "fauxcasa")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert main._default_cache_root() == tmp_path / "local" / "Fauxcasa" / "cache"

    monkeypatch.delenv("LOCALAPPDATA")
    assert (main._default_cache_root()
            == tmp_path / "home" / ".cache" / "fauxcasa")


def test_bad_sandbox_env_value_shows_messagebox_when_frozen(
        monkeypatch, capsys) -> None:
    """release-0.1 review P2-5: a bad FAUXCASA_DECODE_SANDBOX value already
    prints to stderr and exits 2, but that's invisible in a console-less
    frozen exe -- the app just silently disappears. FROZEN must also pop a
    QMessageBox.critical; a non-frozen (source/dev) run must not, since
    stderr is visible there."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    monkeypatch.setenv("FAUXCASA_DECODE_SANDBOX", "bogus")
    monkeypatch.setattr(sys, "argv", ["main.py"])

    critical_calls = []
    monkeypatch.setattr(
        main.QMessageBox, "critical",
        staticmethod(lambda *a, **k: critical_calls.append((a, k))))

    monkeypatch.setattr(main, "FROZEN", False)
    assert main.main() == 2
    assert critical_calls == [], (
        "a non-frozen run has a visible stderr -- no messagebox needed")
    err = capsys.readouterr().err
    assert "FAUXCASA_DECODE_SANDBOX" in err

    monkeypatch.setattr(main, "FROZEN", True)
    assert main.main() == 2
    assert len(critical_calls) == 1, (
        "a FROZEN run has no visible console -- must show a messagebox "
        "or the app just silently disappears")
    args, _ = critical_calls[0]
    assert "FAUXCASA_DECODE_SANDBOX" in args[-1]


def test_reveal_total_count_and_filter(library: Path, tmp_path: Path) -> None:
    """Folder.total_count counts ALL photos (hidden + stash) for reveal-mode
    UI; it is derived on both scan and load. The grid's default filter shows
    visible-only until reveal is set, then every photo."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from grid import GridView

    app = QApplication.instance() or QApplication([])
    assert app is not None

    cat = scan_library(library)
    assert cat.visible_count == 2  # a.jpg + c.jpg
    trip = cat.folders["2020-01-01 Trip"]
    assert trip.photo_count == 1 and trip.total_count == 2  # a visible, b hidden
    stash = cat.folders["2020-01-01 Trip/.picasaoriginals"]
    assert stash.photo_count == 0 and stash.total_count == 1  # the stashed orig

    # total_count is derived on the warm-load path too (never persisted)
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)
    loaded = load_catalog(path, library)
    assert loaded.folders["2020-01-01 Trip"].total_count == 2
    assert loaded.folders["2020-01-01 Trip/.picasaoriginals"].total_count == 1

    g = GridView()
    g.set_data(cat, None)
    assert len(g.display) == 2  # default view: visible only
    g.reveal = True
    g.set_filter(None, "")
    assert len(g.display) == 4  # reveal: hidden b.jpg + stashed orig appear


def test_mainwindow_reveal_toggle(library: Path) -> None:
    """The 'Show hidden' checkbox flips the grid into reveal mode and back,
    rebuilding the sidebar (the stash folder reappears) and the status
    counts (reveal-mode photo/folder tallies), not just the grid filter."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    def folder_rels(win) -> set:
        rels = set()
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            data = it.value().data(0, Qt.ItemDataRole.UserRole)
            if data and data[0] == "folder":
                rels.add(data[1])
            it += 1
        return rels

    STASH = "2020-01-01 Trip/.picasaoriginals"
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    # default: visible-only grid, sidebar (no stash folder), and counts
    assert not win.grid.reveal and len(win.grid.display) == 2
    assert STASH not in folder_rels(win)
    assert "2 photos · 2 folders" in win.counts_label.text()

    win.reveal_box.setChecked(True)  # fires _toggle_reveal(True)
    assert win.grid.reveal and len(win.grid.display) == 4
    assert STASH in folder_rels(win)  # stash folder revealed in the tree
    assert "4 photos · 3 folders" in win.counts_label.text()

    win.reveal_box.setChecked(False)
    assert not win.grid.reveal and len(win.grid.display) == 2
    assert STASH not in folder_rels(win)
    assert "2 photos · 2 folders" in win.counts_label.text()
