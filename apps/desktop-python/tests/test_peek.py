"""Tests for peek.py grid peek preview.

Split from test_tracer.py (fauxcasa-l09); originally lines 6766-6905 of the monolith."""

from __future__ import annotations

from pathlib import Path
from tracer_helpers import (
    _big_library,
    _bound_cache,
    _click,
    _key,
    _key_up,
    _offscreen_app,
    _peek_move,
    _peek_probes,
    _selection_grid,
)


def test_grid_peek_hover_trigger_retarget_and_mouse_out(
        tmp_path: Path) -> None:
    """The Ctrl+Alt hover trigger: a bare hover (or half the chord) never
    fires; the full chord over a photo requests the peek; hovering to a NEW
    photo re-points it (a fresh request, no release between); hovering off
    any photo — or leaving the grid — releases."""
    from PySide6.QtCore import QEvent, Qt
    from grid import PEEK_MODS
    g = _selection_grid(tmp_path)
    req, rel = _peek_probes(g)
    first, second = g.display[0], g.display[1]
    _peek_move(g, first, Qt.KeyboardModifier.NoModifier)   # bare hover
    _peek_move(g, first, Qt.KeyboardModifier.ControlModifier)  # half a chord
    assert req == [] and g._peek_idx == -1
    _peek_move(g, first, PEEK_MODS)
    assert req == [first] and g._peek_idx == first
    _peek_move(g, first, PEEK_MODS)                        # same tile: no spam
    assert req == [first]
    _peek_move(g, second, PEEK_MODS)                       # retarget re-emits
    assert req == [first, second] and rel == []
    _peek_move(g, None, PEEK_MODS)                         # off any photo
    assert rel == [True] and g._peek_idx == -1
    _peek_move(g, second, PEEK_MODS)                       # back on: re-fires
    assert req[-1] == second
    g.leaveEvent(QEvent(QEvent.Type.Leave))                # left the grid
    assert rel == [True, True] and g._hover is None


def test_grid_peek_key_chord_triggers_without_a_move(tmp_path: Path) -> None:
    """Picasa's actual gesture: park the cursor on a photo, THEN press
    Ctrl+Alt — the completed chord triggers from the remembered hover
    position with no mouse move; either modifier's release dismisses."""
    from PySide6.QtCore import Qt
    g = _selection_grid(tmp_path)
    req, rel = _peek_probes(g)
    first = g.display[0]
    _peek_move(g, first, Qt.KeyboardModifier.NoModifier)   # park the cursor
    _key(g, Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier)
    assert req == []                                       # half the chord
    _key(g, Qt.Key.Key_Alt, Qt.KeyboardModifier.ControlModifier
         | Qt.KeyboardModifier.AltModifier)
    assert req == [first] and g._peek_idx == first         # chord completed
    _key_up(g, Qt.Key.Key_Control, Qt.KeyboardModifier.AltModifier)
    assert rel == [True] and g._peek_idx == -1             # chord broken


def test_grid_peek_esc_and_click_dismiss_and_rearm(tmp_path: Path) -> None:
    """Esc dismisses the peek WITHOUT touching the selection (the normal
    selection-collapse Esc is only consumed by an active peek), a click
    dismisses it while still doing its selection work, and both hold the
    peek dismissed until the chord drops and re-triggers."""
    from PySide6.QtCore import Qt
    from grid import PEEK_MODS
    g = _selection_grid(tmp_path)
    req, rel = _peek_probes(g)
    first, second = g.display[0], g.display[1]
    g._select(first)
    _peek_move(g, second, PEEK_MODS)
    assert g._peek_idx == second
    _key(g, Qt.Key.Key_Escape, PEEK_MODS)                  # Esc: peek only
    assert rel == [True] and g._peek_idx == -1
    assert g.selection == {first} and g.current == first   # selection intact
    _peek_move(g, second, PEEK_MODS)                       # chord still held:
    assert g._peek_idx == -1 and len(req) == 1             # suppressed
    _peek_move(g, second, Qt.KeyboardModifier.NoModifier)  # chord drops...
    _peek_move(g, second, PEEK_MODS)                       # ...re-arms
    assert req == [second, second] and g._peek_idx == second
    # a click dismisses AND still does its (Ctrl-toggle) selection work
    _click(g, second, modifiers=PEEK_MODS)
    assert g._peek_idx == -1 and rel == [True, True]
    assert g.selection == {first, second}                  # Ctrl+click added
    _peek_move(g, first, PEEK_MODS)                        # still suppressed
    assert g._peek_idx == -1


def test_mainwindow_peek_lifecycle_reuses_surface_and_cache(
        tmp_path: Path) -> None:
    """MainWindow's peek surface: lazily created on the first request, then
    REUSED (one instance, one persistent decode worker — the slideshow
    lifecycle discipline); it shares the grid's cache pair so the cached
    preview paints instantly; it shows at fit; and it is frameless,
    full-screen on the target screen, input-transparent, and can never take
    focus from the grid (flags + WA_ShowWithoutActivating + NoFocus)."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    import main
    from peek import PeekPage
    root = tmp_path / "lib"
    _big_library(root)
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256])
    win = main.MainWindow(cat, cache, cache_dir=None, build_dir=None)
    assert win._peek_page is None                          # lazy until used
    win.grid.peek_requested.emit(0)
    page = win._peek_page
    assert isinstance(page, PeekPage) and page.isVisible()
    assert page.current_index() == 0 and not page.zoomed   # shown at fit
    assert page.thumbs is win.grid.thumbs                  # shared cache pair
    assert page.preview is not None                        # instant preview
    flags = page.windowFlags()
    for f in (Qt.WindowType.FramelessWindowHint,
              Qt.WindowType.WindowStaysOnTopHint,
              Qt.WindowType.WindowTransparentForInput,
              Qt.WindowType.WindowDoesNotAcceptFocus):
        assert flags & f, f
    assert page.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert page.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert page.geometry() == PeekPage._target_screen().geometry()
    win.grid.peek_released.emit()
    assert page.isHidden()
    win.grid.peek_requested.emit(1)                        # reuse, re-pointed
    assert win._peek_page is page and page.isVisible()
    assert page.current_index() == 1
    worker = page._decoder                                 # ONE worker...
    win.grid.peek_released.emit()
    win.grid.peek_requested.emit(0)
    assert page._decoder is worker                         # ...every peek
    page.quiesce()                                         # no thread leak
    assert worker is None or not worker.is_alive()


def test_peek_target_screen_falls_back_to_primary(monkeypatch) -> None:
    """Multi-monitor placement: the peek goes to the screen containing the
    cursor; when screenAt can't resolve one (headless, or a cursor parked
    between screens) it falls back to the primary rather than nowhere."""
    _offscreen_app()
    import peek
    from PySide6.QtGui import QGuiApplication
    primary = QGuiApplication.primaryScreen()

    class _NoHit:  # QGuiApplication stand-in: cursor over no known screen
        @staticmethod
        def screenAt(_pos):
            return None

        @staticmethod
        def primaryScreen():
            return primary

    monkeypatch.setattr(peek, "QGuiApplication", _NoHit)
    assert peek.PeekPage._target_screen() is primary
