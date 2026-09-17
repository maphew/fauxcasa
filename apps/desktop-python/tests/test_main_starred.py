"""Tests for main.py starred view and star-threshold filtering.

Split from test_tracer.py (fauxcasa-l09); originally lines 19823-20402 of the monolith."""

from __future__ import annotations

import json
from pathlib import Path
from catalog import (
    scan_library,
)
from tracer_helpers import (
    _dated_starred_library,
    _header_click,
    _key,
    _offscreen_app,
    _sidebar_click,
    _sidebar_text,
    _star_all_with_dates,
    make_jpeg,
)


def test_starred_view_is_date_grouped_newest_first(tmp_path: Path) -> None:
    """The Starred collection groups by month, newest month first, with a
    genuinely dateless photo sinking into a trailing 'Undated' group (spec
    §5, fauxcasa-q6l.20 clause b)."""
    _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    _dated_starred_library(root)
    cat = scan_library(root)
    by_name = _star_all_with_dates(cat)

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    _sidebar_click(win, "starred", "")

    assert [g.title for g in win.grid.groups] == [
        "September 2026", "August 2026", "July 2026", "Undated"]
    assert [g.folder for g in win.grid.groups] == [
        "2026-09", "2026-08", "2026-07", "undated"]
    assert win.grid.groups[0].items == [by_name["sep.jpg"]]
    assert win.grid.groups[1].items == [by_name["aug.jpg"]]
    assert win.grid.groups[2].items == [by_name["jul.jpg"]]
    assert win.grid.groups[-1].items == [by_name["nodate.jpg"]]


def test_starred_view_jump_next_folder_steps_month_groups(
        tmp_path: Path) -> None:
    """jump_next_folder/jump_prev_folder — Picasa's folder-boundary jump
    buttons — step between the Starred collection's MONTH groups exactly
    as they step between folders in the default view (pure group-index/y
    logic, agnostic to what a group's key means). Enough photos per month
    to force real scroll overflow (_jump_grid's own convention above)."""
    _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    months = [(0, "2026-09-05T10:00:00"), (1, "2026-08-20T10:00:00"),
              (2, "2026-07-01T10:00:00")]
    for mi, _stamp in months:
        for k in range(8):
            make_jpeg(root / "f" / f"m{mi}p{k:02d}.jpg")
    cat = scan_library(root)
    for mi, stamp in months:
        for k in range(8):
            i = next(j for j, p in enumerate(cat.photos)
                     if p.name == f"m{mi}p{k:02d}.jpg")
            cat.photos[i].date_taken = stamp
            cat.photos[i].star = 1

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.resize(400, 300)
    _sidebar_click(win, "starred", "")
    g = win.grid
    assert len(g.groups) == 3
    assert g.content_h > g.viewport().height()   # real overflow, like _jump_grid

    sb = g.verticalScrollBar()
    assert sb.value() == 0
    g.jump_next_folder()
    assert sb.value() == g.groups[1].y
    g.jump_next_folder()
    assert sb.value() == g.groups[2].y
    g.jump_prev_folder()                 # exactly at a group top: step back
    assert sb.value() == g.groups[1].y


def test_starred_view_header_play_emits_group_items(tmp_path: Path) -> None:
    """Clicking the header play glyph on a MONTH group in the Starred
    collection emits play_group with that group's own key, and
    MainWindow._play_group starts the slideshow over exactly that group's
    items (fauxcasa-q6l.16's per-group play, extended to date groups)."""
    _offscreen_app()
    from grid import HEADER_H, PAD
    from main import MainWindow

    root = tmp_path / "lib"
    _dated_starred_library(root)
    cat = scan_library(root)
    by_name = _star_all_with_dates(cat)

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.resize(500, 400)
    win.show()
    _sidebar_click(win, "starred", "")
    g = win.grid

    emitted: list[str] = []
    g.play_group.connect(emitted.append)
    w = g.viewport().width()
    top = g.verticalScrollBar().value()
    glyph0 = g._header_glyph_rect(g.groups[0].y - top, w)
    _header_click(g, glyph0.center().x(), glyph0.center().y())
    assert emitted == ["2026-09"]

    win._play_group("2026-09")
    assert win._slideshow is not None
    assert win._slideshow.display == [by_name["sep.jpg"]]


# ---------------------------------------------------------------------------
# Scoped star views (fauxcasa-q6l.20), clause (a): the star-threshold
# predicate (>= N) composes with any view via one choke point,
# MainWindow._scope_indices, driven by the View > Stars radio menu.
# ---------------------------------------------------------------------------


def test_star_min_persistence_roundtrip(tmp_path: Path) -> None:
    """save/load round-trip: 1-5 survive, 0/out-of-range/non-int read as
    0 (Any) on both sides — view prefs are a convenience, never a gate."""
    from main import load_star_min, save_star_min

    save_star_min(tmp_path, 3)
    assert load_star_min(tmp_path) == 3
    cfg = tmp_path / "config.json"
    assert json.loads(cfg.read_text())["star_min"] == 3

    save_star_min(tmp_path, 0)                 # default: stored as absent
    assert "star_min" not in json.loads(cfg.read_text())
    assert load_star_min(tmp_path) == 0

    save_star_min(None, 4)                     # no state dir: no-op
    assert load_star_min(None) == 0
    assert load_star_min(tmp_path / "nowhere") == 0     # missing file
    cfg.write_text('{"star_min": 9}')                    # out of range
    assert load_star_min(tmp_path) == 0
    cfg.write_text('{"star_min": true}')                 # bool, not int
    assert load_star_min(tmp_path) == 0
    cfg.write_text("{not json")                           # garbage
    assert load_star_min(tmp_path) == 0


def test_star_min_persists_across_mainwindow_instances(tmp_path: Path) -> None:
    """Setting the threshold via the View > Stars menu persists it, and a
    fresh MainWindow over the same state dir starts with it ALREADY
    APPLIED to the very first paint (fauxcasa-q6l.20 review finding 1) —
    not just remembered as a number the menu agrees with while the grid
    still shows everything."""
    _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "low.jpg")
    make_jpeg(root / "f" / "high.jpg")
    cat = scan_library(root)
    by_name = {p.name: i for i, p in enumerate(cat.photos)}
    cat.photos[by_name["low.jpg"]].star = 1
    cat.photos[by_name["high.jpg"]].star = 3
    state_dir = tmp_path / "state"
    win = MainWindow(cat, None, cache_dir=None, build_dir=None,
                     state_dir=state_dir)
    win.star_actions[3].trigger()               # "3 stars or more"
    assert win._star_min == 3

    cat2 = scan_library(root)
    by_name2 = {p.name: i for i, p in enumerate(cat2.photos)}
    cat2.photos[by_name2["low.jpg"]].star = 1
    cat2.photos[by_name2["high.jpg"]].star = 3
    win2 = MainWindow(cat2, None, cache_dir=None, build_dir=None,
                      state_dir=state_dir)
    assert win2._star_min == 3
    assert win2.star_actions[3].isChecked()
    assert not win2.star_actions[0].isChecked()
    assert win2.grid.display == [by_name2["high.jpg"]]   # already filtered
    assert "≥3★" in win2.counts_label.text()


def test_star_min_filters_folder_view_keeps_grouping_and_sort(
        tmp_path: Path) -> None:
    """Threshold >=3 in the default folder view shows only qualifying
    photos, still grouped by folder with the folder's remembered sort
    mode applied — set_filter(None) alone can no longer do both at once,
    hence set_filter's `default_sort` escape hatch."""
    _offscreen_app()
    from grid import SORT_SIZE
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "big.jpg", 256, 192)
    make_jpeg(root / "f" / "medium.jpg", 128, 96)
    make_jpeg(root / "f" / "small.jpg", 64, 48)
    make_jpeg(root / "f" / "tiny.jpg", 16, 12)
    cat = scan_library(root)
    by_name = {p.name: i for i, p in enumerate(cat.photos)}
    cat.photos[by_name["big.jpg"]].star = 1
    cat.photos[by_name["medium.jpg"]].star = 3
    cat.photos[by_name["small.jpg"]].star = 5
    cat.photos[by_name["tiny.jpg"]].star = 0
    # Photo.size is normally filled by the thumbcache bind/backfill pass,
    # not a bare scan_library — set it directly (like date_taken above)
    # so SORT_SIZE has something real to sort on.
    for name, size in (("tiny.jpg", 1), ("small.jpg", 2),
                       ("medium.jpg", 3), ("big.jpg", 4)):
        cat.photos[by_name[name]].size = size

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.grid.sort_modes["f"] = SORT_SIZE
    win._set_star_min(3)

    assert len(win.grid.groups) == 1 and win.grid.groups[0].folder == "f"
    assert win.grid.display == [
        by_name["small.jpg"], by_name["medium.jpg"]]   # size-ascending
    assert "≥3★" in win.counts_label.text()

    win._set_star_min(0)                        # "Any" restores everything
    assert win.grid.display == [
        by_name["tiny.jpg"], by_name["small.jpg"],
        by_name["medium.jpg"], by_name["big.jpg"]]
    assert "★" not in win.counts_label.text()


def test_star_min_composes_with_search(tmp_path: Path) -> None:
    """A star threshold ANDs with the active search term."""
    _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "beach_low.jpg")
    make_jpeg(root / "f" / "beach_high.jpg")
    make_jpeg(root / "f" / "hill_high.jpg")
    cat = scan_library(root)
    by_name = {p.name: i for i, p in enumerate(cat.photos)}
    cat.photos[by_name["beach_low.jpg"]].star = 1
    cat.photos[by_name["beach_high.jpg"]].star = 4
    cat.photos[by_name["hill_high.jpg"]].star = 4

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win._set_star_min(3)
    win.search.setText("beach")
    assert win.grid.display == [by_name["beach_high.jpg"]]
    assert "≥3★" in win.counts_label.text()

    win._set_star_min(0)
    assert sorted(win.grid.display) == sorted([
        by_name["beach_low.jpg"], by_name["beach_high.jpg"]])


def test_star_min_composes_with_album_view(tmp_path: Path) -> None:
    """A star threshold ANDs with an album's membership."""
    _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "b.jpg")
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\nstar=yes\r\nalbums=cafecafecafecafecafecafecafecafe\r\n"
        "[b.jpg]\r\nalbums=cafecafecafecafecafecafecafecafe\r\n"
        "[.album:cafecafecafecafecafecafecafecafe]\r\nname=Best\r\n"
        "token=cafecafecafecafecafecafecafecafe\r\n"
    )
    cat = scan_library(root)
    by_name = {p.name: i for i, p in enumerate(cat.photos)}

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win._set_star_min(1)
    _sidebar_click(win, "album", "cafecafecafecafecafecafecafecafe")
    assert win.grid.display == [by_name["a.jpg"]]
    assert "≥1★" in win.counts_label.text()


def test_star_min_starred_view_is_max_of_threshold_and_one(
        tmp_path: Path) -> None:
    """The Starred collection under threshold N shows star >= max(1, N):
    a threshold of 1 (or 0/Any) still excludes unstarred photos, and a
    higher threshold tightens further."""
    _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "one.jpg")
    make_jpeg(root / "f" / "three.jpg")
    make_jpeg(root / "f" / "unstarred.jpg")
    cat = scan_library(root)
    by_name = {p.name: i for i, p in enumerate(cat.photos)}
    cat.photos[by_name["one.jpg"]].star = 1
    cat.photos[by_name["three.jpg"]].star = 3

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win._set_star_min(1)
    _sidebar_click(win, "starred", "")
    assert sorted(win.grid.display) == sorted([
        by_name["one.jpg"], by_name["three.jpg"]])

    win._set_star_min(3)
    assert win.grid.display == [by_name["three.jpg"]]
    assert "≥3★" in win.counts_label.text()


def test_star_menu_actions_reflect_current_threshold(tmp_path: Path) -> None:
    """The View > Stars radio group stays in sync: exactly one action
    checked, matching _star_min, after a programmatic change."""
    _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    cat = scan_library(root)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    assert win.star_actions[0].isChecked()
    assert [a.isChecked() for a in win.star_actions].count(True) == 1

    win._set_star_min(2)
    assert win.star_actions[2].isChecked()
    assert [a.isChecked() for a in win.star_actions].count(True) == 1


# ---------------------------------------------------------------------------
# Scoped star views (fauxcasa-q6l.20), clause (c): bulk-unstar is Ctrl+A
# (select-all-in-scope, shipped) plus one gesture — Shift+Space, or the
# View > Clear Star(s) menu item — that zeroes stars on the whole selection
# in one starstore save.
# ---------------------------------------------------------------------------


def test_shift_space_clears_stars_on_whole_selection_in_starred_view(
        tmp_path: Path) -> None:
    """Ctrl+A then Shift+Space over a MIXED-star selection in the default
    view: unconditional zero on every selected photo, never Space's
    toggle semantics (which would normalize a mixed selection to ALL-
    starred instead) — every cleared photo's zero lands in stars.json,
    and the Starred sidebar count drops to 0."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    import starstore
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "b.jpg")
    make_jpeg(root / "f" / "c.jpg")   # unstarred: makes the selection MIXED
    cat = scan_library(root)
    by_name = {p.name: i for i, p in enumerate(cat.photos)}
    cat.photos[by_name["a.jpg"]].star = 3
    cat.photos[by_name["b.jpg"]].star = 5
    # c.jpg stays star=0

    state_dir = tmp_path / "state"
    win = MainWindow(cat, None, cache_dir=None, build_dir=None,
                     state_dir=state_dir)
    _key(win.grid, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert win.grid.selection == {
        by_name["a.jpg"], by_name["b.jpg"], by_name["c.jpg"]}
    _key(win.grid, Qt.Key.Key_Space, Qt.KeyboardModifier.ShiftModifier)

    # Star_toggle's mixed-selection rule would have set target=1 (normalize
    # to starred) here; clear must zero all three regardless.
    assert cat.photos[by_name["a.jpg"]].star == 0
    assert cat.photos[by_name["b.jpg"]].star == 0
    assert cat.photos[by_name["c.jpg"]].star == 0
    assert _sidebar_text(win, "starred", "") == "Starred  (0)"
    overrides = starstore.load_star_overrides(state_dir)
    assert overrides[("", "f/a.jpg")] == 0
    assert overrides[("", "f/b.jpg")] == 0
    assert overrides[("", "f/c.jpg")] == 0

    _sidebar_click(win, "starred", "")
    assert win.grid.display == []


def test_shift_space_in_search_scope_clears_only_the_searched_photos(
        tmp_path: Path) -> None:
    """The same gesture in a search scope only clears what Ctrl+A actually
    selected there — the search results, a MIXED-star pair so toggle's
    normalize-to-starred behavior would visibly diverge from clear —
    leaving an equally-starred photo outside the search term untouched."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "beach_sun.jpg")
    make_jpeg(root / "f" / "beach_shade.jpg")
    make_jpeg(root / "f" / "hill.jpg")
    cat = scan_library(root)
    by_name = {p.name: i for i, p in enumerate(cat.photos)}
    cat.photos[by_name["beach_sun.jpg"]].star = 4
    # beach_shade.jpg stays star=0: mixed search-result selection
    cat.photos[by_name["hill.jpg"]].star = 4

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.search.setText("beach")
    assert sorted(win.grid.display) == sorted([
        by_name["beach_sun.jpg"], by_name["beach_shade.jpg"]])

    _key(win.grid, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    _key(win.grid, Qt.Key.Key_Space, Qt.KeyboardModifier.ShiftModifier)

    # Toggle's mixed-selection rule would have STARRED beach_shade.jpg
    # (target=1); clear must zero the whole mixed pair instead.
    assert cat.photos[by_name["beach_sun.jpg"]].star == 0
    assert cat.photos[by_name["beach_shade.jpg"]].star == 0
    assert cat.photos[by_name["hill.jpg"]].star == 4   # outside the search


def test_shift_space_in_viewer_clears_only_the_current_photo(
        tmp_path: Path) -> None:
    """The viewer half of the gesture (fauxcasa-q6l.20 clause c): Shift+
    Space is unconditional CLEAR, not star_toggle's add/remove — on an
    unstarred current photo, toggle would STAR it (target=1) but clear
    must leave it at 0 — and the rest of the (frozen) display list is
    untouched either way."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "b.jpg")
    cat = scan_library(root)
    by_name = {p.name: i for i, p in enumerate(cat.photos)}
    # a.jpg stays star=0 — the discriminating case: star_toggle would SET
    # it to 1 (nothing selected is "already starred"), clear must not.
    cat.photos[by_name["b.jpg"]].star = 3

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    display = [by_name["a.jpg"], by_name["b.jpg"]]
    win._open_viewer(by_name["a.jpg"], display, 0)

    _key(win.viewer, Qt.Key.Key_Space, Qt.KeyboardModifier.ShiftModifier)

    assert cat.photos[by_name["a.jpg"]].star == 0   # stayed cleared
    assert cat.photos[by_name["b.jpg"]].star == 3   # untouched

    win._open_viewer(by_name["b.jpg"], display, 1)
    _key(win.viewer, Qt.Key.Key_Space, Qt.KeyboardModifier.ShiftModifier)
    assert cat.photos[by_name["b.jpg"]].star == 0   # the ordinary case too
    win.viewer.quiesce()


def test_menu_clear_stars_dispatches_grid_or_viewer_by_current_page(
        tmp_path: Path) -> None:
    """View > Clear Star(s) is the mouse-only equivalent of Shift+Space:
    it clears the grid selection when the grid is showing, and just the
    current photo when the viewer is showing."""
    _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "b.jpg")
    cat = scan_library(root)
    by_name = {p.name: i for i, p in enumerate(cat.photos)}
    cat.photos[by_name["a.jpg"]].star = 1
    cat.photos[by_name["b.jpg"]].star = 1

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.grid._select(by_name["a.jpg"])
    win.star_clear_action.trigger()
    assert cat.photos[by_name["a.jpg"]].star == 0
    assert cat.photos[by_name["b.jpg"]].star == 1

    display = [by_name["a.jpg"], by_name["b.jpg"]]
    win._open_viewer(by_name["b.jpg"], display, 1)
    win.star_clear_action.trigger()
    assert cat.photos[by_name["b.jpg"]].star == 0
    win.viewer.quiesce()


# ---------------------------------------------------------------------------
# Scoped star views (fauxcasa-q6l.20), post-review fixes: the persisted
# threshold must apply at startup and survive a reconcile swap, and the
# month grouper must never raise on an unbounded date_taken year.
# ---------------------------------------------------------------------------


def test_reload_data_keeps_star_threshold_applied(tmp_path: Path) -> None:
    """A reconcile-swap reload (fauxcasa-q6l.20 review finding 3) must not
    silently drop an active star threshold back to the unfiltered default
    — reload_data's own set_data call re-applies the plain default view,
    so the fix must redo the threshold pass afterward."""
    _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "low.jpg")
    make_jpeg(root / "f" / "high.jpg")
    cat = scan_library(root)
    by_name = {p.name: i for i, p in enumerate(cat.photos)}
    cat.photos[by_name["low.jpg"]].star = 1
    cat.photos[by_name["high.jpg"]].star = 3

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win._set_star_min(3)
    assert win.grid.display == [by_name["high.jpg"]]

    cat2 = scan_library(root)
    by_name2 = {p.name: i for i, p in enumerate(cat2.photos)}
    cat2.photos[by_name2["low.jpg"]].star = 1
    cat2.photos[by_name2["high.jpg"]].star = 3
    win.reload_data(cat2, None)

    assert win._star_min == 3
    assert win.grid.display == [by_name2["high.jpg"]]
    assert "≥3★" in win.counts_label.text()


def test_starred_grouper_never_raises_on_unbounded_date_taken_year(
        tmp_path: Path) -> None:
    """date_taken's year is UNBOUNDED (§6 footgun 16) — an all-zero EXIF
    placeholder ("0000-05-01T...") groups sensibly by month, and a
    malformed/non-4-digit year ("12345-06-07T...") sinks to Undated —
    neither ever raises out of set_filter's grouper (fauxcasa-q6l.20
    review finding 2, was a bare datetime.strptime)."""
    _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "zero_year.jpg")
    make_jpeg(root / "f" / "huge_year.jpg")
    make_jpeg(root / "f" / "normal.jpg")
    cat = scan_library(root)
    by_name = {p.name: i for i, p in enumerate(cat.photos)}
    cat.photos[by_name["zero_year.jpg"]].date_taken = "0000-05-01T10:00:00"
    cat.photos[by_name["huge_year.jpg"]].date_taken = "12345-06-07T00:00:00"
    cat.photos[by_name["normal.jpg"]].date_taken = "2026-01-01T00:00:00"
    for i in by_name.values():
        cat.photos[i].star = 1

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    _sidebar_click(win, "starred", "")   # must not raise

    groups = {g.folder: g for g in win.grid.groups}
    assert groups["0000-05"].title == "May 0000"
    assert groups["0000-05"].items == [by_name["zero_year.jpg"]]
    assert groups["undated"].items == [by_name["huge_year.jpg"]]
    assert groups["2026-01"].items == [by_name["normal.jpg"]]


def test_resync_widens_to_thresholded_folder_view_not_just_starred(
        tmp_path: Path) -> None:
    """fauxcasa-q6l.20 review finding 4: a >=3-star FOLDER view (not the
    Starred collection) must also resync after a bulk clear-stars from
    inside it — the display empties and the status label's count drops
    to 0, instead of leaving cleared tiles on screen with a stale
    '>=3★: 2 photos' readout."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "b.jpg")
    cat = scan_library(root)
    by_name = {p.name: i for i, p in enumerate(cat.photos)}
    cat.photos[by_name["a.jpg"]].star = 3
    cat.photos[by_name["b.jpg"]].star = 4

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win._set_star_min(3)
    assert sorted(win.grid.display) == sorted([
        by_name["a.jpg"], by_name["b.jpg"]])

    _key(win.grid, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    _key(win.grid, Qt.Key.Key_Space, Qt.KeyboardModifier.ShiftModifier)

    assert win.grid.display == []
    assert "0 photos" in win.counts_label.text()
    assert "≥3★" in win.counts_label.text()


def test_recently_updated_empty_message_blames_threshold_not_backfill(
        tmp_path: Path) -> None:
    """fauxcasa-q6l.20 review finding 7: an empty Recently Updated under
    an active star threshold must not claim the backfill is still
    running when that isn't why it's empty."""
    _offscreen_app()
    from catalog import BACKFILL_NOT_STARTED
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    cat = scan_library(root)
    cat.photos[0].star = 1
    cat.backfill_state = BACKFILL_NOT_STARTED   # would normally explain 0

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win._set_star_min(3)   # nothing qualifies -> Recently Updated is empty
    _sidebar_click(win, "recent", "")

    assert win.grid.display == []
    msg = win.statusBar().currentMessage()
    assert "star threshold" in msg
    assert "backfill" not in msg
