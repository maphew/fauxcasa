"""Tests for grid.py multi-select (selection set, current/anchor, keyboard/mouse).

Split from test_tracer.py (fauxcasa-l09); originally lines 5495-5863 of the monolith."""

from __future__ import annotations

from pathlib import Path
from catalog import (
    scan_library,
)
from tracer_helpers import (
    _click,
    _key,
    _selection_grid,
)


# ---------------------------------------------------------------------------
# Grid multi-select (fauxcasa-q6l.1): selection set + current/anchor model,
# Ctrl/Shift click, Shift+arrow extension, Ctrl+A, Esc, signal payloads,
# multi-rect paint, and the selection-vs-filter/zoom behavior. First
# selection/keyboard coverage in the suite: mouse/key events are constructed
# directly and delivered to the widget handlers — offscreen-safe, no window
# activation or QTest focus dependence.
# ---------------------------------------------------------------------------


def test_grid_click_selects_single(tmp_path: Path) -> None:
    """Plain click: the set collapses to the clicked tile, which becomes
    both current and anchor; a plain background click clears everything."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    g = _selection_grid(tmp_path)
    d = g.display
    _click(g, d[0])
    assert g.selection == {d[0]} and g.current == d[0] and g.anchor == d[0]
    _click(g, d[2])   # a second plain click REPLACES, never accumulates
    assert g.selection == {d[2]} and g.current == d[2] and g.anchor == d[2]
    # plain click on the header band (no photo there) clears
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(5.0, 5.0),
                     QPointF(5.0, 5.0), QPointF(5.0, 5.0),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    g.mousePressEvent(ev)
    assert g.selection == set() and g.current == -1


def test_grid_ctrl_click_toggles(tmp_path: Path) -> None:
    """Ctrl+click toggles membership without touching the rest; toggling a
    tile OUT keeps it current (focus without selection); a Ctrl-modified
    background click never clears an assembled selection; a modified
    double-click is selection assembly, not a viewer open."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    g = _selection_grid(tmp_path)
    d = g.display
    CTRL = Qt.KeyboardModifier.ControlModifier
    _click(g, d[0])
    _click(g, d[3], CTRL)
    _click(g, d[6], CTRL)   # across the group seam
    assert g.selection == {d[0], d[3], d[6]} and g.current == d[6]
    _click(g, d[3], CTRL)   # toggle one back OFF
    assert g.selection == {d[0], d[6]}
    assert g.current == d[3]  # still current: keyboard focus stays visible
    # Ctrl+click on the header band is a no-op, not a clear
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(5.0, 5.0),
                     QPointF(5.0, 5.0), QPointF(5.0, 5.0),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     CTRL)
    g.mousePressEvent(ev)
    assert g.selection == {d[0], d[6]}
    # a Ctrl double-click neither activates nor collapses the set
    acts = []
    g.photo_activated.connect(lambda idx, disp, pos: acts.append(idx))
    _click(g, d[0], CTRL, double=True)
    assert acts == [] and g.selection == {d[0], d[6]}


def test_grid_shift_click_range(tmp_path: Path) -> None:
    """Shift+click selects the anchor..hit range in display order (spanning
    group seams), replaces on re-range (both directions), and Ctrl+Shift
    ADDS the range to the existing set."""
    from PySide6.QtCore import Qt

    g = _selection_grid(tmp_path)
    d = g.display
    CTRL = Qt.KeyboardModifier.ControlModifier
    SHIFT = Qt.KeyboardModifier.ShiftModifier
    _click(g, d[1])
    _click(g, d[4], SHIFT)
    assert g.selection == set(d[1:5])
    assert g.current == d[4] and g.anchor == d[1]   # anchor holds
    _click(g, d[7], SHIFT)   # re-range from the SAME anchor, across groups
    assert g.selection == set(d[1:8]) and g.anchor == d[1]
    _click(g, d[0], SHIFT)   # reverse direction from the same anchor
    assert g.selection == {d[0], d[1]} and g.current == d[0]
    # Ctrl+Shift adds a disjoint range without clearing the set
    _click(g, d[6], CTRL)                  # scatter + move the anchor
    _click(g, d[8], CTRL | SHIFT)
    assert g.selection == {d[0], d[1], d[6], d[7], d[8]}
    # Shift+click with no usable anchor degrades to a plain select
    g._select(-1)
    _click(g, d[5], SHIFT)
    assert g.selection == {d[5]} and g.anchor == d[5]


def test_grid_shift_arrow_extends(tmp_path: Path) -> None:
    """Shift+arrows walk the current end of the anchor..current range:
    extend, shrink back, extend by a visual row (Down), and an unmodified
    arrow collapses back to single-select."""
    from PySide6.QtCore import Qt

    g = _selection_grid(tmp_path)
    d = g.display
    SHIFT = Qt.KeyboardModifier.ShiftModifier
    _click(g, d[2])
    _key(g, Qt.Key.Key_Right, SHIFT)
    assert g.selection == {d[2], d[3]} and g.current == d[3]
    assert g.anchor == d[2]
    _key(g, Qt.Key.Key_Right, SHIFT)
    assert g.selection == set(d[2:5]) and g.current == d[4]
    _key(g, Qt.Key.Key_Left, SHIFT)    # shrink back toward the anchor
    assert g.selection == {d[2], d[3]} and g.current == d[3]
    _key(g, Qt.Key.Key_Down, SHIFT)    # one visual row down (2 cols): d3->d5
    assert g.selection == set(d[2:6]) and g.current == d[5]
    _key(g, Qt.Key.Key_Right)          # no Shift: collapse to single
    assert g.selection == {d[6]} and g.current == d[6] and g.anchor == d[6]


def test_grid_select_all_and_escape(tmp_path: Path) -> None:
    """Ctrl+A (QKeySequence.SelectAll — Cmd+A on macOS for free) selects
    the whole display set; Esc collapses to the current item only; Esc
    with no current item stays cleanly empty."""
    from PySide6.QtCore import Qt

    g = _selection_grid(tmp_path)
    d = g.display
    CTRL = Qt.KeyboardModifier.ControlModifier
    _key(g, Qt.Key.Key_A, CTRL)   # nothing selected yet: current -> first
    assert g.selection == set(d) and g.current == d[0]
    _key(g, Qt.Key.Key_Escape)
    assert g.selection == {d[0]} and g.current == d[0]
    _click(g, d[3])
    _key(g, Qt.Key.Key_A, CTRL)   # current/anchor keep their place
    assert g.selection == set(d) and g.current == d[3] and g.anchor == d[3]
    _key(g, Qt.Key.Key_Escape)
    assert g.selection == {d[3]}
    g._select(-1)
    _key(g, Qt.Key.Key_Escape)    # no current: clears to empty, no crash
    assert g.selection == set() and g.current == -1


def test_grid_deselect_ctrl_d(tmp_path: Path) -> None:
    """Ctrl+D (grid.deselect) clears the entire selection set while keeping
    the current item as keyboard focus (deselected but still current).
    Distinct from Esc (grid.clear) which collapses to {current}."""
    from PySide6.QtCore import Qt

    g = _selection_grid(tmp_path)
    d = g.display
    CTRL = Qt.KeyboardModifier.ControlModifier
    SHIFT = Qt.KeyboardModifier.ShiftModifier
    _click(g, d[1])
    _click(g, d[4], SHIFT)
    assert len(g.selection) > 1   # range selection assembled
    _key(g, Qt.Key.Key_D, CTRL)
    assert g.selection == set()   # all deselected
    assert g.current == d[4]      # keyboard focus preserved
    # Ctrl+D with no current item is a clean no-op (no crash)
    g._select(-1)
    _key(g, Qt.Key.Key_D, CTRL)
    assert g.selection == set() and g.current == -1


def test_grid_invert_selection_ctrl_i(tmp_path: Path) -> None:
    """Ctrl+I (grid.invert) flips selection membership over the current
    view: unselected photos become selected and vice versa. The current
    item stays as keyboard focus regardless of its new membership state."""
    from PySide6.QtCore import Qt

    g = _selection_grid(tmp_path)
    d = g.display
    CTRL = Qt.KeyboardModifier.ControlModifier
    SHIFT = Qt.KeyboardModifier.ShiftModifier
    _click(g, d[0])
    _click(g, d[2], SHIFT)         # d[0], d[1], d[2] selected
    assert g.selection == set(d[:3])
    _key(g, Qt.Key.Key_I, CTRL)
    assert g.selection == set(d[3:])    # the other 6 photos are now selected
    assert g.current == d[2]            # current preserved
    # Invert of the full set yields empty
    _key(g, Qt.Key.Key_A, CTRL)        # select all first
    _key(g, Qt.Key.Key_I, CTRL)
    assert g.selection == set()


def test_grid_home_end_navigation(tmp_path: Path) -> None:
    """Home (grid.first) jumps to the first photo; End (grid.last) to the
    last. Both are key_only so they ride the same Shift-extension path as
    arrows: Shift+Home selects anchor..first, Shift+End anchor..last."""
    from PySide6.QtCore import Qt

    g = _selection_grid(tmp_path)
    d = g.display
    SHIFT = Qt.KeyboardModifier.ShiftModifier
    _click(g, d[4])                    # somewhere in the middle
    _key(g, Qt.Key.Key_Home)
    assert g.current == d[0] and g.selection == {d[0]}
    _key(g, Qt.Key.Key_End)
    assert g.current == d[-1] and g.selection == {d[-1]}
    # Shift+Home from d[-1] extends the selection to anchor..d[0]
    _key(g, Qt.Key.Key_Home, SHIFT)
    assert g.current == d[0] and g.anchor == d[-1]
    assert g.selection == set(d)


def test_grid_selection_signal_payloads(tmp_path: Path) -> None:
    """selection_changed carries a set COPY of catalog indices and fires
    only when the set changes; photo_selected keeps tracking the current
    item; a pure no-op click re-emits nothing; Enter activates the
    CURRENT item of a multi-selection."""
    from PySide6.QtCore import Qt

    g = _selection_grid(tmp_path)
    d = g.display
    CTRL = Qt.KeyboardModifier.ControlModifier
    got_sel: list = []
    got_cur: list = []
    acts: list = []
    g.selection_changed.connect(got_sel.append)
    g.photo_selected.connect(got_cur.append)
    g.photo_activated.connect(lambda idx, disp, pos: acts.append((idx, pos)))

    _click(g, d[0])
    assert got_sel[-1] == {d[0]} and got_cur[-1] == d[0]
    n_sel, n_cur = len(got_sel), len(got_cur)
    _click(g, d[0])                      # no-op: same single selection
    assert len(got_sel) == n_sel and len(got_cur) == n_cur
    _click(g, d[2], CTRL)
    assert got_sel[-1] == {d[0], d[2]} and got_cur[-1] == d[2]
    got_sel[-1].clear()                  # receivers get a copy, not the model
    assert g.selection == {d[0], d[2]}
    _key(g, Qt.Key.Key_Return)           # Enter opens the CURRENT item
    assert acts[-1] == (d[2], 2)
    g._select(-1)                        # clearing emits -1 / empty set
    assert got_cur[-1] == -1 and got_sel[-1] == set()


def test_grid_multiselect_paint_offscreen(tmp_path: Path) -> None:
    """Painting a multi-selection offscreen must not crash: selected rects,
    the stronger current border, the dashed focus-only cue (current toggled
    out of the set), and a selection that extends beyond the viewport."""
    from PySide6.QtCore import Qt

    g = _selection_grid(tmp_path)
    d = g.display
    CTRL = Qt.KeyboardModifier.ControlModifier
    _click(g, d[0])
    _click(g, d[3], Qt.KeyboardModifier.ShiftModifier)
    frames = g.frame_no
    assert not g.grab().isNull()         # renders through paintEvent
    assert g.frame_no > frames
    _click(g, d[3], CTRL)                # current now OUTSIDE the set
    assert g.current == d[3] and d[3] not in g.selection
    g.grab()                             # exercises the focus-cue branch
    g._set_selection(set(d), d[-1], d[0])  # spans past the viewport bottom
    frames = g.frame_no
    g.grab()
    assert g.frame_no > frames


def test_grid_selection_survives_zoom_and_relayout(tmp_path: Path) -> None:
    """Zoom and resize are pure relayouts: the selection set, current item,
    and anchor all survive them untouched."""
    from PySide6.QtCore import Qt

    g = _selection_grid(tmp_path)
    d = g.display
    _click(g, d[1])
    _click(g, d[4], Qt.KeyboardModifier.ShiftModifier)
    sel = set(g.selection)
    cols = g.cols
    g.set_zoom(96)                       # row heights change ~2x
    assert g.selection == sel and g.current == d[4] and g.anchor == d[1]
    g.resize(560, 640)                   # wider window: column count changes
    assert g.cols != cols                # ((558-8)//104 = 5 at zoom 96)
    assert g.selection == sel and g.current == d[4] and g.anchor == d[1]


def test_grid_set_filter_selection_policy(tmp_path: Path) -> None:
    """set_filter collapses the selection to the current item if the new
    view still shows it, else clears entirely (documented policy: the
    grid's selection is per-view; cross-view persistence is the q6l.2
    tray's job)."""
    from PySide6.QtCore import Qt

    g = _selection_grid(tmp_path)
    d = g.display
    _click(g, d[1])
    _click(g, d[4], Qt.KeyboardModifier.ShiftModifier)   # {d1..d4}, cur d4
    g.set_filter(list(d[3:6]), "subset")   # current d4 still shown
    assert g.selection == {d[4]} and g.current == d[4]
    g._set_selection(set(d[3:6]), d[3], d[3])
    g.set_filter([d[0], d[1]], "elsewhere")   # current d3 filtered away
    assert g.selection == set() and g.current == -1 and g.anchor == -1


def test_mainwindow_selection_status_label(library: Path) -> None:
    """The status label's dual mode over the set-valued signal: exactly one
    selected shows that photo's metadata line, several show the aggregate
    'N photos selected', none clears — driven through the real Ctrl+A /
    Esc / clear paths."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    assert len(win.grid.display) == 2      # a.jpg + c.jpg visible

    win.grid._select(win.grid.display[0])  # the starred, captioned a.jpg
    text = win.meta_label.text()
    assert "a.jpg" in text and "★" in text and "the beach" in text
    _key(win.grid, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert win.meta_label.text() == "2 photos selected  "
    _key(win.grid, Qt.Key.Key_Escape)      # collapse to current -> metadata
    assert "a.jpg" in win.meta_label.text()
    win.grid._select(-1)
    assert win.meta_label.text() == ""
