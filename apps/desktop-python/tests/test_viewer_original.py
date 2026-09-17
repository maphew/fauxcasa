"""Tests for viewer.py original display and mouse handling.

Split from test_tracer.py (fauxcasa-l09); originally lines 6384-6725 of the monolith."""

from __future__ import annotations

from pathlib import Path
from catalog import (
    scan_library,
)
from tracer_helpers import (
    _big_library,
    _bound_cache,
    _key,
    _mouse,
    _offscreen_app,
    _press,
    _viewer_with_original,
)


# ---------------------------------------------------------------------------
# The two N4 exceptions (fauxcasa-q6l.4/.5): the viewer's explicit fit <-> 1:1
# zoom toggle + pan, and the grid's Ctrl+Alt hover full-screen peek. Bindings
# follow the Picasa shortcut corpus (docs/research/sources/picasaresources/
# keyboard-shortcuts.md): `1` toggles 100% zoom (plus conflict-free
# Ctrl+Alt+0, plus click anchored at the click point); "Hover over a photo
# and use Ctrl-Alt" shows the full-screen preview. All offscreen-safe: mouse
# and key events are constructed and delivered to the widget handlers
# directly; DPR paths force the ratio via monkeypatch.
# ---------------------------------------------------------------------------


def test_viewer_zoom_rect_dpr_and_pan_clamping() -> None:
    """The pure 1:1 geometry: one image pixel per DEVICE pixel (logical size
    = src/dpr, so dpr 2 halves the logical rect), pan clamped so background
    never shows past an edge, and an image smaller than the box centers
    regardless of the pan state."""
    _offscreen_app()
    from viewer import ViewerPage
    zr = ViewerPage._zoom_rect
    from PySide6.QtCore import QRect
    # dpr 1, centered: 2560x1600 in 1280x800 hangs half out on every side
    assert zr(1280, 800, 2560, 1600, 1.0, 0.5, 0.5) == \
        QRect(-640, -400, 2560, 1600)
    # pan clamps: fractional centers past the ends pin the matching edge
    assert zr(1280, 800, 2560, 1600, 1.0, 0.0, 0.0) == \
        QRect(0, 0, 2560, 1600)               # top-left pinned
    assert zr(1280, 800, 2560, 1600, 1.0, 1.0, 1.0) == \
        QRect(-1280, -800, 2560, 1600)        # bottom-right pinned
    assert zr(1280, 800, 2560, 1600, 1.0, -9.0, 99.0) == \
        QRect(0, -800, 2560, 1600)            # wild pans still clamp
    # dpr 2: the SAME source covers half the logical px (native device px)
    assert zr(1280, 800, 2560, 1600, 2.0, 0.5, 0.5) == \
        QRect(0, 0, 1280, 800)
    # smaller than the box: centered, pan has no freedom
    assert zr(1280, 800, 600, 400, 1.0, 0.9, 0.1) == QRect(340, 200, 600, 400)
    # degenerate 1x1 never yields a zero-size rect
    assert zr(1280, 800, 1, 1, 3.0, 0.5, 0.5).width() == 1


def test_viewer_zoom_key_toggles_and_arrows_still_navigate(
        tmp_path: Path) -> None:
    """`1` (Picasa Photo Viewer's 100% toggle) and Ctrl+Alt+0 both toggle
    fit <-> 1:1; PLAIN arrows keep meaning next/prev even while zoomed (the
    triage loop owns them), and the photo change resets the zoom to fit."""
    from PySide6.QtCore import Qt
    v, _ = _viewer_with_original(tmp_path)
    _press(v, Qt.Key.Key_1)
    assert v.zoomed
    _press(v, Qt.Key.Key_1)
    assert not v.zoomed
    _key(v, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier
         | Qt.KeyboardModifier.AltModifier)
    assert v.zoomed
    _press(v, Qt.Key.Key_Right)               # plain arrow: NAVIGATES
    assert v.pos == 1
    assert not v.zoomed                        # ...and the zoom reset to fit
    # a modified 1 (future star-set chords etc.) does NOT toggle
    _key(v, Qt.Key.Key_1, Qt.KeyboardModifier.ControlModifier)
    assert not v.zoomed


def test_viewer_space_requests_star_without_advancing(tmp_path: Path) -> None:
    """Picasa muscle memory: Space changes the current photo's star; Right/J
    remain the ways to advance. Slideshow has its own Space pause scope."""
    from PySide6.QtCore import Qt

    v, _ = _viewer_with_original(tmp_path)
    requested = []
    v.star_toggle_requested.connect(requested.append)
    start_idx = v.current_index()
    _press(v, Qt.Key.Key_Space)
    assert requested == [start_idx]
    assert v.current_index() == start_idx and v.pos == 0


def test_mainwindow_back_action_returns_to_exact_gallery_context(
        library: Path) -> None:
    """The viewer is not a dead end: its toolbar exposes a mouse-clickable
    Gallery action, including the Esc hint, and returns to the viewed tile."""
    from main import MainWindow

    _offscreen_app()
    win = MainWindow(scan_library(library), None,
                     cache_dir=None, build_dir=None)
    display = list(win.grid.display)
    idx = display[-1]
    assert not win.back_action.isVisible()
    win._open_viewer(idx, display, len(display) - 1)
    assert win.pages.currentWidget() is win.viewer
    assert win.back_action.isVisible()
    assert "Esc" in win.back_action.text()
    win.back_action.trigger()
    assert win.pages.currentWidget() is win.pages.widget(0)
    assert not win.back_action.isVisible()
    assert win.grid.current == idx
    win.viewer.quiesce()


def test_space_star_override_persists_without_writing_library(
        library: Path, tmp_path: Path) -> None:
    """Grid Space writes only stars.json in Fauxcasa's cache. The choice
    overlays a fresh source scan, including an explicit zero over Picasa's
    imported star=yes value."""
    from PySide6.QtCore import Qt
    from main import MainWindow
    from starstore import STAR_OVERRIDES_NAME

    _offscreen_app()
    cache_dir = tmp_path / "cache"
    ini = library / "2020-01-01 Trip" / ".picasa.ini"
    ini_before = ini.read_bytes()
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=cache_dir, build_dir=None)
    idx = next(i for i, p in enumerate(cat.photos) if p.name == "a.jpg"
               and p.folder.endswith("Trip"))
    assert cat.photos[idx].star == 1
    win.grid._select(idx)
    _press(win.grid, Qt.Key.Key_Space)
    assert cat.photos[idx].star == 0
    assert (cache_dir / STAR_OVERRIDES_NAME).is_file()
    assert ini.read_bytes() == ini_before

    fresh = scan_library(library)
    assert fresh.photos[idx].star == 1       # source itself remains unchanged
    win2 = MainWindow(fresh, None, cache_dir=cache_dir, build_dir=None)
    assert fresh.photos[idx].star == 0       # machine-local override reapplied
    win2.grid._select(idx)
    _press(win2.grid, Qt.Key.Key_Space)
    assert fresh.photos[idx].star == 1


def test_viewer_zoom_click_anchor_stays_put(tmp_path: Path,
                                            monkeypatch) -> None:
    """Click-to-zoom keeps the clicked image point PUT under the cursor: the
    image pixel under (ax, ay) at fit paints at (ax, ay) at 1:1. A second
    click returns to fit."""
    from PySide6.QtCore import QEvent
    v, orig = _viewer_with_original(tmp_path)
    monkeypatch.setattr(v, "devicePixelRatioF", lambda: 1.0)
    ax, ay = 900.0, 300.0
    _mouse(v, QEvent.Type.MouseButtonPress, ax, ay)
    _mouse(v, QEvent.Type.MouseButtonRelease, ax, ay)
    assert v.zoomed
    fit = v._display_rect(1280, 800, orig.width(), orig.height(), cap=True)
    u_img = (ax - fit.x()) / fit.width() * orig.width()
    v_img = (ay - fit.y()) / fit.height() * orig.height()
    z = v._shown_rect(1280, 800, orig)
    assert z.size().width() == orig.width()      # 1:1 at dpr 1
    assert abs(z.x() + u_img * z.width() / orig.width() - ax) <= 1.0
    assert abs(z.y() + v_img * z.height() / orig.height() - ay) <= 1.0
    _mouse(v, QEvent.Type.MouseButtonPress, ax, ay)
    _mouse(v, QEvent.Type.MouseButtonRelease, ax, ay)
    assert not v.zoomed                          # click toggles back to fit


def test_viewer_chevron_hover_sets_flag_and_repaints(tmp_path: Path) -> None:
    """Mouse move within CHEVRON_MARGIN of an edge (ez2.14), with no button
    held, sets the matching hover flag; moving to the middle clears both.
    A viewport 1280 wide: x=20 is within the left margin, x=1260 within
    the right, x=640 is neither."""
    from PySide6.QtCore import QEvent
    from viewer import CHEVRON_MARGIN
    v, _orig = _viewer_with_original(tmp_path)
    assert not v._hover_prev and not v._hover_next

    _mouse(v, QEvent.Type.MouseMove, 20.0, 400.0)
    assert v._hover_prev and not v._hover_next

    _mouse(v, QEvent.Type.MouseMove, 1280.0 - 20.0, 400.0)
    assert v._hover_next and not v._hover_prev

    _mouse(v, QEvent.Type.MouseMove, 640.0, 400.0)
    assert not v._hover_prev and not v._hover_next
    assert CHEVRON_MARGIN < 640.0   # sanity: the middle is really outside it


def test_viewer_chevron_click_navigates(tmp_path: Path) -> None:
    """A click inside the left/right margin calls the existing prev/next
    step (ez2.14) instead of toggling zoom; a click in the middle keeps
    doing the ordinary click-to-zoom toggle."""
    from PySide6.QtCore import QEvent
    v, _orig = _viewer_with_original(tmp_path)  # 2 photos, showing index 0
    assert v.pos == 0

    _mouse(v, QEvent.Type.MouseButtonPress, 1280.0 - 20.0, 400.0)
    assert v.pos == 1                 # right-margin click -> next
    assert not v.zoomed               # never a zoom toggle

    _mouse(v, QEvent.Type.MouseButtonPress, 20.0, 400.0)
    assert v.pos == 0                 # left-margin click -> prev
    assert not v.zoomed

    # A middle click still does the ordinary click-to-zoom toggle (press +
    # release, matching test_viewer_zoom_click_anchor_stays_put's pattern).
    _mouse(v, QEvent.Type.MouseButtonPress, 640.0, 400.0)
    _mouse(v, QEvent.Type.MouseButtonRelease, 640.0, 400.0)
    assert v.zoomed
    assert v.pos == 0                 # unchanged by the middle click


def test_viewer_chevron_paints_only_when_hovered(tmp_path: Path) -> None:
    """paintEvent draws a chevron only while its hover flag is set — a
    fresh viewer (no hover yet) paints neither, and setting a flag makes
    the grabbed frame differ from the no-hover baseline."""
    v, _orig = _viewer_with_original(tmp_path)
    baseline = v.grab().toImage()

    v._hover_prev = True
    v.update()
    v.repaint()
    with_prev = v.grab().toImage()
    assert with_prev != baseline

    v._hover_prev = False
    v._hover_next = True
    v.update()
    v.repaint()
    with_next = v.grab().toImage()
    assert with_next != baseline


def test_viewer_chevron_leave_event_clears_hover(tmp_path: Path) -> None:
    """The cursor leaving the widget (leaveEvent) hides any shown chevron
    rather than leaving it stuck (ez2.14)."""
    from PySide6.QtCore import QEvent
    v, _orig = _viewer_with_original(tmp_path)
    v._hover_prev = True
    v.leaveEvent(QEvent(QEvent.Type.Leave))
    assert not v._hover_prev and not v._hover_next


def test_viewer_zoom_drag_pans_and_release_does_not_toggle(
        tmp_path: Path, monkeypatch) -> None:
    """While at 1:1 a drag pans (the photo follows the cursor) and its
    release is NOT a click — the zoom stays on; a fling past the edge clamps
    (background never shows, and no dead travel is left to wind back
    through); Ctrl+arrows pan a quarter-viewport without navigating."""
    from PySide6.QtCore import QEvent, Qt
    v, orig = _viewer_with_original(tmp_path)
    monkeypatch.setattr(v, "devicePixelRatioF", lambda: 1.0)
    v.toggle_zoom()                              # center: rect at (-640,-400)
    assert v._shown_rect(1280, 800, orig).x() == -640
    _mouse(v, QEvent.Type.MouseButtonPress, 600, 400)
    _mouse(v, QEvent.Type.MouseMove, 500, 350)   # drag left/up 100/50
    _mouse(v, QEvent.Type.MouseButtonRelease, 500, 350)
    assert v.zoomed                              # a drag never toggles
    z = v._shown_rect(1280, 800, orig)
    assert (z.x(), z.y()) == (-740, -450)        # photo moved with the cursor
    _mouse(v, QEvent.Type.MouseButtonPress, 600, 400)
    _mouse(v, QEvent.Type.MouseMove, 9000, 400)  # fling far right
    _mouse(v, QEvent.Type.MouseButtonRelease, 9000, 400)
    assert v._shown_rect(1280, 800, orig).x() == 0   # clamped at the edge
    before = v._shown_rect(1280, 800, orig).x()
    pos_before = v.pos
    _key(v, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)
    assert v.pos == pos_before                   # pan, not navigation
    assert v._shown_rect(1280, 800, orig).x() == before - 1280 // 4


def test_viewer_zoom_resets_on_photo_change_and_show(tmp_path: Path) -> None:
    """Zoom state is per-photo-shown (Picasa behavior): _step and a fresh
    show_photo both land at fit with a centered pan."""
    v, _ = _viewer_with_original(tmp_path)
    v.toggle_zoom()
    v._pan_by(-300, -200)
    assert v.zoomed and v._zoom_cx != 0.5
    v.show_photo(v.display, 1)
    assert not v.zoomed and v._zoom_cx == 0.5 and v._zoom_cy == 0.5


def test_viewer_zoom_dpr_and_preview_standin(tmp_path: Path,
                                             monkeypatch) -> None:
    """The instance path is devicePixelRatio-correct (a forced dpr 2 halves
    the logical 1:1 rect: native DEVICE pixels, not a 2x blowup), and before
    the original lands the zoomed paint shows the cached preview at ITS own
    pixels — "paint whatever is available" — centered when smaller than the
    viewport, and still paints cleanly (grab)."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256])
    v = ViewerPage(cat, cache)
    v.resize(1280, 800)
    monkeypatch.setattr(v, "devicePixelRatioF", lambda: 2.0)
    v.show_photo(list(range(cache.count)), 0)
    assert v.preview is not None and v.image is None   # original still async
    v.toggle_zoom()
    z = v._shown_rect(1280, 800, v.preview)
    # preview at its own native device px: logical size = preview/2, centered
    assert z.width() == max(1, round(v.preview.width() / 2.0))
    assert z.x() == (1280 - z.width()) // 2
    assert not v.grab().isNull()                       # zoomed paint is clean
    from PySide6.QtGui import QImage
    orig = QImage(2560, 1600, QImage.Format.Format_RGB32)
    orig.fill(0x224466)
    v._on_loaded(v._serial, orig)                      # the original lands...
    assert v.zoomed                                    # ...zoom holds, and
    z2 = v._shown_rect(1280, 800, orig)                # deepens to true 1:1
    assert z2.width() == 1280                          # 2560 px at dpr 2
    v.quiesce()                                        # reap the decode worker
    assert v._decoder is None or not v._decoder.is_alive()
