"""Tests for main.py tray/sidebar behavior.

Split from test_tracer.py (fauxcasa-l09); originally lines 9812-10254 of the monolith."""

from __future__ import annotations

from pathlib import Path
import thumbcache
from catalog import (
    scan_library,
)
from tracer_helpers import (
    _key,
    _offscreen_app,
    _sidebar_click,
    _tray_window,
    make_jpeg,
)


# ---------------------------------------------------------------------------
# Selection tray (fauxcasa-q6l.2): persistent CROSS-FOLDER Hold/Clear +
# typed readout (spec §5). Decisions under test (tray.py module doc):
# identity by REL PATH (survives reconcile index remaps), HOLD ORDER
# (insertion order — a tray is a deliberately assembled staging set),
# and the never-silent vanish note (N7). MainWindow.selection_context()
# is the M2 output-action attachment point.
# ---------------------------------------------------------------------------


def test_tray_hold_appends_in_display_order_no_duplicates(
        tmp_path: Path) -> None:
    """Ctrl+H holds the CURRENT multi-selection: one Hold appends its
    members in display order; a later Hold appends only the new rels —
    an already-held photo keeps its original slot (hold order, the
    q6l.1 handoff's ordering decision); an empty selection is a no-op."""
    from PySide6.QtCore import Qt

    win = _tray_window(tmp_path)
    g = win.grid
    d = g.display
    CTRL = Qt.KeyboardModifier.ControlModifier
    _key(g, Qt.Key.Key_H, CTRL)                  # nothing selected: no-op
    assert win.tray.held == []
    g._set_selection({d[2], d[0]}, d[2], d[0])   # unordered set, cross-folder
    _key(g, Qt.Key.Key_H, CTRL)
    assert win.tray.held == ["f0/a.jpg", "f1/c.jpg"]   # display order
    g._set_selection({d[3], d[0]}, d[3], d[3])   # d0 already held
    _key(g, Qt.Key.Key_H, CTRL)
    assert win.tray.held == ["f0/a.jpg", "f1/c.jpg", "f1/d.jpg"]
    assert win.tray.held_indices() == [d[0], d[2], d[3]]
    assert win.tray.readout.text() == "3 photos held"
    # The Hold BUTTON is the same path as the key
    g._set_selection({d[1]}, d[1], d[1])
    win.tray.hold_btn.click()
    assert win.tray.held[-1] == "f0/b.jpg" and len(win.tray.held) == 4


def test_tray_persists_across_views_and_search(tmp_path: Path) -> None:
    """The held set is CROSS-FOLDER and survives every view change by
    construction (rel identity, owned above the grid): search, album,
    starred, folder navigation — the grid's per-view selection collapses
    while the tray never moves."""
    win = _tray_window(tmp_path)
    g = win.grid
    d = list(g.display)
    g._set_selection({d[0], d[3]}, d[3], d[0])   # one from each folder
    win._hold_selection()
    held = list(win.tray.held)
    assert held == ["f0/a.jpg", "f1/d.jpg"]

    win.search.setText("c")                      # search view
    assert g.selection == set() or g.selection == {g.current}
    assert win.tray.held == held
    _sidebar_click(win, "album", "cafecafecafecafecafecafecafecafe")
    assert win.tray.held == held
    _sidebar_click(win, "folder", "f1")
    assert win.tray.held == held
    _sidebar_click(win, "all", "")
    assert win.tray.held == held
    assert win.tray.held_indices() == [d[0], d[3]]
    # ...and holding FROM a filtered view appends across the boundary
    win.search.setText("b")
    assert [win.catalog.photos[i].rel for i in g.display] == ["f0/b.jpg"]
    g._set_selection(set(g.display), g.display[0], g.display[0])
    win._hold_selection()
    assert win.tray.held == held + ["f0/b.jpg"]


def test_tray_reload_data_remaps_indices_by_rel(tmp_path: Path) -> None:
    """The subtle part: a reconcile swap REMAPS catalog indices. Held
    photos survive by identity — after a file is ADDED ahead of them in
    walk order, the same rels resolve to shifted indices."""
    win = _tray_window(tmp_path)
    root = win.catalog.root
    d = list(win.grid.display)
    win.grid._set_selection({d[2], d[3]}, d[3], d[2])
    win._hold_selection()
    assert win.tray.held_indices() == [2, 3]

    make_jpeg(root / "f0" / "0-new.jpg")         # sorts ahead of everything
    fresh = scan_library(root)
    built = thumbcache.build_cache(fresh, tmp_path / "c2")
    cache = thumbcache.load_cache(built.path)
    thumbcache.bind(cache, fresh)
    win.reload_data(fresh, cache)

    assert win.tray.held == ["f1/c.jpg", "f1/d.jpg"]   # identity intact
    assert win.tray.held_indices() == [3, 4]           # indices remapped
    assert win.tray.vanished == 0
    assert win.tray.readout.text() == "2 photos held"


def test_tray_vanished_note_is_never_silent(tmp_path: Path) -> None:
    """Held photos missing from the swapped catalog are dropped from the
    set but surfaced as a count note (N7) — also when the WHOLE held set
    vanished — and the note clears on the next tray action."""
    win = _tray_window(tmp_path)
    root = win.catalog.root
    d = list(win.grid.display)
    win.grid._set_selection({d[0], d[2]}, d[2], d[0])
    win._hold_selection()

    (root / "f1" / "c.jpg").unlink()
    fresh = scan_library(root)
    built = thumbcache.build_cache(fresh, tmp_path / "c2")
    cache = thumbcache.load_cache(built.path)
    thumbcache.bind(cache, fresh)
    win.reload_data(fresh, cache)

    assert win.tray.held == ["f0/a.jpg"] and win.tray.vanished == 1
    assert win.tray.readout.text() == \
        "1 photo held — 1 held photo no longer in the library"
    # the whole set vanishing must still leave the note
    (root / "f0" / "a.jpg").unlink()
    fresh2 = scan_library(root)
    built2 = thumbcache.build_cache(fresh2, tmp_path / "c3")
    cache2 = thumbcache.load_cache(built2.path)
    thumbcache.bind(cache2, fresh2)
    win.reload_data(fresh2, cache2)
    assert win.tray.held == [] and win.tray.vanished == 2
    assert win.tray.readout.text() == \
        "2 held photos no longer in the library"
    # the next tray action acknowledges the note
    win.grid._select(win.grid.display[0])
    win._hold_selection()
    assert win.tray.vanished == 0
    assert win.tray.readout.text() == "1 photo held"


def test_tray_click_navigates_grid_with_view_fallback(
        tmp_path: Path) -> None:
    """Clicking a held thumb selects + scrolls the grid to that photo —
    from the viewer page, and from a view whose filter hides it (falls
    back to the All-photos view; a held photo is cross-folder by
    design)."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent

    win = _tray_window(tmp_path)
    g = win.grid
    d = list(g.display)
    g._set_selection({d[3]}, d[3], d[3])
    win._hold_selection()

    win.search.setText("a")                      # filters f1/d.jpg away
    assert d[3] not in g.display_pos
    win.pages.setCurrentWidget(win.viewer)       # navigate leaves the viewer
    # left-click the first tray thumb through the real bar hit test
    win.tray.bar.resize(300, 56)
    pos = QPointF(float(6 + 22), 28.0)           # center of cell 0
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos, pos, pos,
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    win.tray.bar.mousePressEvent(ev)

    assert win.pages.currentWidget() is win.pages.widget(0)
    assert win.search.text() == ""               # fell back to All photos
    assert g.current == d[3] and g.selection == {d[3]}
    assert d[3] in g.display_pos
    # in a view that already shows it, the view is kept as-is
    win.search.setText("c")
    win._tray_navigate("f1/d.jpg")               # not shown -> falls back
    win.search.setText("d")
    win._tray_navigate("f1/d.jpg")               # shown -> search survives
    assert win.search.text() == "d" and g.current == d[3]


def test_tray_navigate_hidden_photo_auto_reveals(library: Path) -> None:
    """Navigating to a held photo that is hidden auto-reveals rather than
    silently falling back to All photos (q6l.18, N7 compliance).
    reveal_box is set so UI state stays consistent; the photo is selected;
    a status message names the action."""
    _offscreen_app()
    from main import MainWindow

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    g = win.grid

    # library fixture: "2020-01-01 Trip/b.jpg" carries hidden=yes
    hidden_rel = "2020-01-01 Trip/b.jpg"
    idx = next(i for i, p in enumerate(cat.photos) if p.rel == hidden_rel)
    assert not cat.photos[idx].visible            # confirm fixture
    assert not win.grid.reveal                    # reveal starts off
    assert idx not in g.display_pos              # absent without reveal

    win.tray.hold([hidden_rel])                   # hold the hidden photo
    win._tray_navigate(hidden_rel)

    assert win.grid.reveal                        # auto-revealed via reveal_box
    assert win.reveal_box.isChecked()             # checkbox UI in sync
    assert idx in g.display_pos                  # photo now in grid
    assert g.current == idx                       # photo selected
    assert "revealed" in win.statusBar().currentMessage().lower()


def test_tray_navigate_hidden_reveals_but_still_missing(
        monkeypatch, library: Path) -> None:
    """When reveal is toggled ON during tray navigation but the photo is
    still absent after the fallback, the status message names BOTH facts:
    reveal was toggled AND the photo was not found — reveal is never a
    silent side effect (q6l.18, N7).

    The edge case is simulated by patching set_filter to a no-op so
    display_pos is never updated, keeping the photo absent throughout."""
    _offscreen_app()
    from main import MainWindow

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    g = win.grid

    hidden_rel = "2020-01-01 Trip/b.jpg"
    idx = next(i for i, p in enumerate(cat.photos) if p.rel == hidden_rel)
    assert not cat.photos[idx].visible   # confirm fixture
    assert not win.grid.reveal           # reveal starts off
    assert idx not in g.display_pos      # absent without reveal

    win.tray.hold([hidden_rel])

    # Patch set_filter so display_pos is never updated — simulates the
    # edge case where reveal toggled but the photo is still not shown.
    monkeypatch.setattr(g, "set_filter", lambda *a, **k: None)

    win._tray_navigate(hidden_rel)

    # reveal flag was set as a side effect of the navigation attempt
    assert win.grid.reveal
    msg = win.statusBar().currentMessage()
    # message must name the reveal action AND acknowledge the failure
    assert "revealed" in msg.lower()
    assert "not visible" in msg.lower()


def test_tray_clear_and_per_item_remove(tmp_path: Path) -> None:
    """Clear empties the tray (button enablement follows); middle-click
    on a thumb removes exactly that item, keeping the rest in order."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    win = _tray_window(tmp_path)
    g = win.grid
    d = list(g.display)
    assert not win.tray.clear_btn.isEnabled()
    g._set_selection({d[0], d[1], d[2]}, d[2], d[0])
    win._hold_selection()
    assert win.tray.clear_btn.isEnabled()

    win.tray.bar.resize(300, 56)
    cell = 44 + 6                                # tray.THUMB + tray.PAD
    pos = QPointF(float(6 + cell + 22), 28.0)    # center of cell 1
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos, pos, pos,
                     Qt.MouseButton.MiddleButton,
                     Qt.MouseButton.MiddleButton,
                     Qt.KeyboardModifier.NoModifier)
    win.tray.bar.mousePressEvent(ev)
    assert win.tray.held == ["f0/a.jpg", "f1/c.jpg"]
    win.tray.remove("nope/never-held.jpg")       # unknown rel: no-op
    assert win.tray.held == ["f0/a.jpg", "f1/c.jpg"]

    g._select(-1)                                # so the view readout shows
    win.tray.clear_btn.click()
    assert win.tray.held == [] and not win.tray.clear_btn.isEnabled()
    assert win.tray.readout.text() == "4 photos"  # back to the view readout


def test_tray_typed_readout_strings(tmp_path: Path) -> None:
    """The spec's type-aware phrasing, driven by sidebar type + grid
    selection + tray state: folder/album views, N selected, N held —
    singular forms included. Lives in the tray strip; the status-bar
    dual mode is a separate surface (q6l.1) and stays as-is."""
    win = _tray_window(tmp_path)
    g = win.grid
    d = list(g.display)
    assert win.tray.readout.text() == "4 photos"          # All photos view
    _sidebar_click(win, "folder", "f0")
    assert win.tray.readout.text() == "Folder selected — 2 photos"
    _sidebar_click(win, "album", "cafecafecafecafecafecafecafecafe")
    assert win.tray.readout.text() == "Album selected — 1 photo"
    _sidebar_click(win, "all", "")
    win.search.setText("c")
    assert win.tray.readout.text() == "Search — 1 photo"
    win.search.setText("")
    g._set_selection({d[0]}, d[0], d[0])
    assert win.tray.readout.text() == "1 photo selected"
    g._set_selection({d[0], d[1], d[3]}, d[3], d[0])
    assert win.tray.readout.text() == "3 photos selected"
    win._hold_selection()                       # held wins over selection
    assert win.tray.readout.text() == "3 photos held"
    # the status bar's dual mode is untouched by the tray readout
    assert win.meta_label.text() == "3 photos selected  "


def test_tray_ctrl_h_from_grid_and_viewer(tmp_path: Path) -> None:
    """Ctrl+H (Picasa muscle memory) holds from BOTH surfaces: the grid
    holds its selection set, the viewer holds the photo on screen."""
    from PySide6.QtCore import Qt

    win = _tray_window(tmp_path)
    g = win.grid
    d = list(g.display)
    CTRL = Qt.KeyboardModifier.ControlModifier
    g._set_selection({d[1]}, d[1], d[1])
    _key(g, Qt.Key.Key_H, CTRL)
    assert win.tray.held == ["f0/b.jpg"]

    win._open_viewer(d[2], list(d), 2)           # viewer on f1/c.jpg
    _key(win.viewer, Qt.Key.Key_H, CTRL)
    assert win.tray.held == ["f0/b.jpg", "f1/c.jpg"]
    _key(win.viewer, Qt.Key.Key_Right)           # -> f1/d.jpg
    _key(win.viewer, Qt.Key.Key_H, CTRL)
    assert win.tray.held == ["f0/b.jpg", "f1/c.jpg", "f1/d.jpg"]


def test_selection_context_is_the_m2_hook(tmp_path: Path) -> None:
    """selection_context() — what a future output action reads: kind
    follows the precedence (held > photos > view type), indices carry
    the input's own order (HOLD order for held, display order for a
    selection), and the held rels ride along regardless of kind."""
    win = _tray_window(tmp_path)
    g = win.grid
    d = list(g.display)

    ctx = win.selection_context()
    assert ctx.kind == "all" and ctx.indices == tuple(d) and ctx.held == ()
    _sidebar_click(win, "folder", "f1")
    assert win.selection_context().kind == "folder"
    win.search.setText("c")
    assert win.selection_context().kind == "search"
    win.search.setText("")

    g._set_selection({d[2], d[0]}, d[2], d[0])
    ctx = win.selection_context()
    assert ctx.kind == "photos" and ctx.indices == (d[0], d[2])

    win._hold_from_viewer(d[3])                  # hold order: d3 first
    win._hold_from_viewer(d[0])
    ctx = win.selection_context()
    assert ctx.kind == "held"
    assert ctx.indices == (d[3], d[0])           # HOLD order, not display
    assert ctx.held == ("f1/d.jpg", "f0/a.jpg")


def test_tray_thumbs_render_from_fcache(tmp_path: Path) -> None:
    """Held thumbs decode synchronously from the cache pair (no new
    decode threads — the memo dict fills on paint); with no cache yet
    the paint is a placeholder and nothing is memoized, so pixels
    upgrade in place when the cold build lands (set_thumbs)."""
    win = _tray_window(tmp_path, with_cache=True)
    g = win.grid
    d = list(g.display)
    g._set_selection({d[0], d[2]}, d[2], d[0])
    win._hold_selection()
    win.tray.bar.resize(300, 56)
    assert not win.tray.bar.grab().isNull()      # paints through paintEvent
    imgs = [win.tray.thumb_image(r) for r in win.tray.held]
    assert all(i is not None and not i.isNull() for i in imgs)
    assert all(i.width() <= 44 and i.height() <= 44 for i in imgs)

    # no-cache window: placeholder paint, no memoization, then upgrade
    win2 = _tray_window(tmp_path / "w2")
    d2 = list(win2.grid.display)
    win2.grid._set_selection({d2[0]}, d2[0], d2[0])
    win2._hold_selection()
    win2.tray.bar.resize(300, 56)
    assert not win2.tray.bar.grab().isNull()
    assert win2.tray.thumb_image("f0/a.jpg") is None
    assert win2.tray._thumb_imgs == {}
    built = thumbcache.build_cache(scan_library(win2.catalog.root),
                                   tmp_path / "w2" / "c")
    cache = thumbcache.load_cache(built.path)
    thumbcache.bind(cache, win2.catalog)
    win2.tray.set_thumbs(cache)
    assert win2.tray.thumb_image("f0/a.jpg") is not None


def test_tray_overflow_paints_plus_n_tail(tmp_path: Path) -> None:
    """More held photos than the bar fits: the tail collapses to a '+N'
    cell (the model, not the pixels, is what actions read); hit testing
    maps only painted thumbs."""
    win = _tray_window(tmp_path)
    g = win.grid
    d = list(g.display)
    g._set_selection(set(d), d[0], d[0])
    win._hold_selection()
    bar = win.tray.bar
    # Pin the size: grab()/render() activate the parent layout, which
    # would otherwise re-widen the bar past the overflow under test.
    bar.setFixedSize(3 * (44 + 6) + 6, 44 + 2 * 6)   # three whole cells
    assert bar._slots() == 3
    assert bar._shown() == (2, 2)                # 2 thumbs + '+2' tail
    assert bar.item_at(6 + 22) == 0              # first painted thumb
    assert bar.item_at(6 + 2 * (44 + 6) + 22) == -1   # the '+N' cell
    assert bar.item_at(2) == -1                  # left gutter
    assert not bar.grab().isNull()               # paints the '+N' branch
    assert bar._shown() == (2, 2)                # geometry held through paint
