"""Tests for inspector.py InspectorPanel.

Split from test_tracer.py (fauxcasa-l09); originally lines 17752-18014 of the monolith."""

from __future__ import annotations

from pathlib import Path
from catalog import (
    scan_library,
)
from tracer_helpers import (
    _inspector_text,
    _key,
    _offscreen_app,
    _press,
    make_jpeg,
)


# ---------------------------------------------------------------------------
# Metadata inspector panel (fauxcasa-q6l.25): InspectorPanel (inspector.py)
# is a pure catalog-only display widget, reachable from BOTH the grid and
# the viewer through one instance living in a splitter around main.py's
# `pages` stack — not inside either page — plus the app.info keymap action
# (bare I, key_only, dispatched per-surface AFTER each surface's exact
# Ctrl-chord checks so Ctrl+I keeps meaning grid.invert in the grid). Tests
# below cover the toggle wiring/dispatch order, populate/omit rules, viewer
# navigation updates, multi-select, and the album/people resolution main.py
# does on the panel's behalf (the panel itself never touches the catalog's
# album map or looks a UID up).
# ---------------------------------------------------------------------------


def test_inspector_hidden_by_default_and_toggles(library: Path) -> None:
    """Info starts unchecked/hidden; the toolbar action, bare I in the
    grid, and bare I in the viewer all toggle it — but Ctrl+I in the grid
    still inverts the selection and leaves the panel alone (the key_only
    dispatch-order contract, keymap.py / grid.py)."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow

    win = MainWindow(scan_library(library), None,
                     cache_dir=None, build_dir=None)
    assert not win.info_action.isChecked()
    assert win.inspector.isHidden()

    win.info_action.trigger()
    assert win.info_action.isChecked()
    assert not win.inspector.isHidden()

    win.info_action.trigger()
    assert not win.info_action.isChecked()
    assert win.inspector.isHidden()

    _press(win.grid, Qt.Key.Key_I)                       # bare I: toggles on
    assert win.info_action.isChecked()
    assert not win.inspector.isHidden()

    display = list(win.grid.display)
    win.grid._select(display[0])
    _key(win.grid, Qt.Key.Key_I, Qt.KeyboardModifier.ControlModifier)
    assert win.info_action.isChecked()                    # unchanged
    assert win.grid.selection == set(display) - {display[0]}  # inverted

    win._open_viewer(display[0], display, 0)
    _press(win.viewer, Qt.Key.Key_I)                      # bare I: toggles off
    assert not win.info_action.isChecked()
    assert win.inspector.isHidden()
    win.viewer.quiesce()


def test_inspector_grid_selection_populates(library: Path) -> None:
    """Selecting a.jpg with the panel visible shows its name, caption,
    keywords (plain comma text — NOT the status bar's #hashtag spelling),
    and a star glyph."""
    _offscreen_app()
    from main import MainWindow

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.info_action.setChecked(True)
    a = next(p for p in cat.photos if p.rel.endswith("Trip/a.jpg"))
    win.grid._select(cat.photos.index(a))

    text = _inspector_text(win.inspector)
    assert "a.jpg" in text
    assert "the beach" in text
    assert "sun, sand" in text
    assert "★" in text


def test_inspector_missing_metadata_omits_rows(library: Path) -> None:
    """c.jpg has no caption/keywords/date — those rows are OMITTED
    entirely (no dashes, no crash), while the name still shows. c.jpg
    (not the hidden b.jpg) so the test exercises a photo the UI can
    actually reach without Show hidden."""
    _offscreen_app()
    from main import MainWindow

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.info_action.setChecked(True)
    c = next(p for p in cat.photos if p.rel.endswith("Picnic/c.jpg"))
    win.grid._select(cat.photos.index(c))

    text = _inspector_text(win.inspector)
    assert "c.jpg" in text
    assert "Caption" not in text
    assert "Keywords" not in text
    assert "Taken" not in text


def test_inspector_viewer_navigation_updates(library: Path) -> None:
    """Opening the viewer and stepping to the next photo refreshes the
    panel with the new photo's name — the same photo_shown path that
    already drives the status bar (fauxcasa-q6l.1), no new plumbing."""
    _offscreen_app()
    from main import MainWindow

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.info_action.setChecked(True)
    display = list(win.grid.display)
    assert len(display) >= 2               # a.jpg, c.jpg (b.jpg is hidden)

    win._open_viewer(display[0], display, 0)
    assert cat.photos[display[0]].name in _inspector_text(win.inspector)

    win.viewer._step(1)
    assert cat.photos[display[1]].name in _inspector_text(win.inspector)
    win.viewer.quiesce()


def test_inspector_multi_select_then_escape(library: Path) -> None:
    """Multi-select shows the aggregate count; Esc collapsing back to one
    item repopulates the single-photo rows (spec §5 dual mode, mirrored
    from the status label's existing behavior)."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.info_action.setChecked(True)
    display = list(win.grid.display)
    win.grid._select(display[0])

    _key(win.grid, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert f"{len(display)} photos selected" in _inspector_text(win.inspector)

    _key(win.grid, Qt.Key.Key_Escape)
    assert cat.photos[win.grid.current].name in _inspector_text(win.inspector)


def test_inspector_album_and_people_rows(library: Path) -> None:
    """Albums resolve UID -> display name (main.py's job, not the
    panel's); People joins named faces and appends a '+N unnamed' tail
    for faces with no name at all (fauxcasa-cam.3/.4 face data)."""
    _offscreen_app()
    from main import MainWindow

    cat = scan_library(library)
    a = next(p for p in cat.photos if p.rel.endswith("Trip/a.jpg"))
    # library's a.jpg already carries albums=deadbeef... ("Best Of"); add
    # a named + an unnamed face directly on the catalog (house style —
    # mutate in-test like test_status_readout_date_coords_and_star_count).
    a.faces = (
        ((0.1, 0.1, 0.3, 0.3), "aaaaaaaaaaaaaaaa", "Alice"),
        ((0.5, 0.5, 0.7, 0.7), "bbbbbbbbbbbbbbbb", None),
    )
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.info_action.setChecked(True)
    win.grid._select(cat.photos.index(a))

    text = _inspector_text(win.inspector)
    assert "Best Of" in text
    assert "Alice, +1 unnamed" in text


def test_inspector_search_typing_does_not_toggle(library: Path) -> None:
    """Typing 'i' in the search box must TYPE, not toggle the panel —
    the reason info_action deliberately has no window-level QAction
    shortcut (main.py). QTest.keyClick goes through the real key/shortcut
    event pipeline, unlike the _press/_key helpers, which call
    keyPressEvent directly and cannot catch a setShortcut regression."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from main import MainWindow

    win = MainWindow(scan_library(library), None,
                     cache_dir=None, build_dir=None)
    assert not win.info_action.isChecked()
    QTest.keyClick(win.search, Qt.Key.Key_I)
    assert not win.info_action.isChecked()
    assert win.search.text() == "i"


def test_inspector_hidden_skips_populate_refreshes_on_show(
        library: Path) -> None:
    """While hidden the panel is NOT populated on selection (cheapness:
    selection churn must cost nothing when the panel is off); toggling it
    on refreshes once from the current selection (spec: "skip populate
    work but refresh once on show")."""
    _offscreen_app()
    from main import MainWindow

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    a = next(p for p in cat.photos if p.rel.endswith("Trip/a.jpg"))
    win.grid._select(cat.photos.index(a))
    assert win.inspector._form.rowCount() == 0     # nothing populated
    win.info_action.setChecked(True)               # show -> one refresh
    assert "a.jpg" in _inspector_text(win.inspector)


def test_inspector_bulk_star_keeps_multi_state(library: Path) -> None:
    """Space over a multi-selection stars the photos but must LEAVE the
    status/inspector in "N photos selected" — the grid's selection is
    still live, so flipping to a single-photo readout would lie about
    what a follow-up action applies to (review finding)."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.info_action.setChecked(True)
    display = list(win.grid.display)
    win.grid._select(display[0])
    _key(win.grid, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert f"{len(display)} photos selected" in _inspector_text(win.inspector)

    _key(win.grid, Qt.Key.Key_Space)               # bulk star toggle
    assert all(cat.photos[i].star for i in display)
    assert f"{len(display)} photos selected" in _inspector_text(win.inspector)
    assert f"{len(display)} photos selected" in win.meta_label.text()


def test_inspector_rederives_after_catalog_reload(
        library: Path, tmp_path: Path) -> None:
    """reload_data swaps the catalog; the grid may keep the same current
    index WITHOUT emitting, so the panel must be re-derived from the new
    catalog rather than keep rendering the old Photo object (review
    finding). The new library's photo lives at the library ROOT, which
    also pins the empty-Folder-row omission (folder == "")."""
    _offscreen_app()
    from main import MainWindow

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.info_action.setChecked(True)
    a = next(p for p in cat.photos if p.rel.endswith("Trip/a.jpg"))
    win.grid._select(cat.photos.index(a))
    assert "the beach" in _inspector_text(win.inspector)

    # a.jpg is catalog index 1 (walk order: .picasaoriginals/a, a, b, c).
    # lib2 must keep index 1 valid — same current index surviving the
    # swap WITHOUT a photo_selected emit is exactly the stale hazard —
    # so give it two root-level photos: y.jpg(0), z.jpg(1).
    root2 = tmp_path / "lib2"
    make_jpeg(root2 / "y.jpg")
    make_jpeg(root2 / "z.jpg")
    win.reload_data(scan_library(root2), None)

    text = _inspector_text(win.inspector)
    assert "the beach" not in text
    assert "z.jpg" in text
    assert "Folder" not in text                    # root-level: row omitted
