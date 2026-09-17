"""Tests for main.py/grid.py integration.

Split from test_tracer.py (fauxcasa-l09); originally lines 12063-12744 of the monolith."""

from __future__ import annotations

import json
import os
from pathlib import Path
import library as libmod
from catalog import (
    scan_library,
)
from tracer_helpers import (
    _bound_cache,
    _offscreen_app,
    _photo,
    _show_library,
    make_jpeg,
)


# ---------------------------------------------------------------------------
# Per-folder sort modes: date / name / size (fauxcasa-q6l.11).
# Manual mode (display of Picasa's persisted db3 order) is blocked on the
# missing oracle fixture and deliberately untested/unimplemented here.
# ---------------------------------------------------------------------------


def test_sort_modes_persistence_roundtrip(tmp_path: Path) -> None:
    """save/load round-trip: non-default modes survive, default-mode and
    unknown-mode entries are dropped on BOTH sides, and every degraded
    input (no cache dir, missing file, garbage, non-object JSON) reads as
    {} — view prefs are a convenience, never a gate."""
    from main import load_sort_modes, save_sort_modes

    save_sort_modes(tmp_path, {"a": "date", "b": "name",  # name = default
                               "c": "size", "d": "bogus"})
    assert load_sort_modes(tmp_path) == {"a": "date", "c": "size"}
    cfg = tmp_path / "config.json"
    assert cfg.is_file()
    doc = json.loads(cfg.read_text())
    assert doc["sort_modes"] == {"a": "date", "c": "size"}

    save_sort_modes(None, {"a": "date"})           # no cache dir: no-op
    assert load_sort_modes(None) == {}
    assert load_sort_modes(tmp_path / "nowhere") == {}   # missing file
    cfg.write_text("{not json")                          # garbage
    assert load_sort_modes(tmp_path) == {}
    cfg.write_text("42")                                 # non-object doc
    assert load_sort_modes(tmp_path) == {}
    cfg.write_text('{"sort_modes": {"a": "date", "b": 3, "c": "up"}}')
    assert load_sort_modes(tmp_path) == {"a": "date"}    # bad values drop


def test_window_geometry_persistence_roundtrip(tmp_path: Path) -> None:
    """save/load round-trip for the persisted window geometry
    (fauxcasa-ez2.6 §6): a real QByteArray survives base64 in
    config.json, merged alongside sort_modes, and every degraded input
    (no state dir, missing file, garbage, non-string value, empty
    string) reads as None — view prefs are a convenience, never a gate."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QByteArray
    from PySide6.QtWidgets import QApplication
    from main import (
        load_sort_modes,
        load_window_geometry,
        save_sort_modes,
        save_window_geometry,
    )

    QApplication.instance() or QApplication([])
    blob = QByteArray(b"\x00\x01\xffnot-real-geometry-but-real-bytes")
    save_window_geometry(tmp_path, blob)
    loaded = load_window_geometry(tmp_path)
    assert loaded is not None and bytes(loaded) == bytes(blob)

    # Merges into the existing doc: an already-persisted sort mode survives.
    save_sort_modes(tmp_path, {"a": "date"})
    save_window_geometry(tmp_path, blob)
    assert load_sort_modes(tmp_path) == {"a": "date"}
    assert bytes(load_window_geometry(tmp_path)) == bytes(blob)

    assert load_window_geometry(None) is None                    # no dir
    assert load_window_geometry(tmp_path / "nowhere") is None    # missing
    cfg = tmp_path / "config.json"
    cfg.write_text("{not json")
    assert load_window_geometry(tmp_path) is None                # garbage
    cfg.write_text('{"window_geometry": 42}')
    assert load_window_geometry(tmp_path) is None                # non-str
    cfg.write_text('{"window_geometry": ""}')
    assert load_window_geometry(tmp_path) is None                # empty


def test_default_window_size_scales_to_small_screen(monkeypatch) -> None:
    """_default_window_size scales the 1280x800 v1 baseline down to
    min(1280, 0.9*avail.width) x min(800, 0.9*avail.height) instead of
    spilling off a small display (fauxcasa-ez2.6 §6); a roomy screen
    still gets the plain 1280x800 baseline unscaled."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QApplication
    import main

    app = QApplication.instance() or QApplication([])
    screen = app.primaryScreen()
    assert screen is not None, "offscreen platform still reports a screen"

    monkeypatch.setattr(
        type(screen), "availableGeometry",
        lambda self: QRect(0, 0, 1000, 700))
    assert main._default_window_size() == (900, 630)

    monkeypatch.setattr(
        type(screen), "availableGeometry",
        lambda self: QRect(0, 0, 3840, 2160))
    assert main._default_window_size() == (1280, 800)


def test_mainwindow_restores_persisted_geometry_and_saves_on_close(
        tmp_path: Path) -> None:
    """MainWindow.__init__ restores a persisted window rect when it's
    still on-screen, and closeEvent persists whatever's current — an
    end-to-end round trip through two constructions (fauxcasa-ez2.6 §6)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow, load_window_geometry

    QApplication.instance() or QApplication([])
    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    cat = scan_library(root)
    state_dir = tmp_path / "state"

    # A modest size, well within the tiny virtual screen the offscreen
    # platform reports (Qt's own restoreGeometry clamps to the available
    # screen rect, same as a real WM would for an oversized saved rect).
    win1 = MainWindow(cat, None, cache_dir=None, build_dir=None,
                      state_dir=state_dir)
    win1.resize(500, 400)
    win1.close()   # closeEvent persists the current geometry
    assert load_window_geometry(state_dir) is not None

    win2 = MainWindow(cat, None, cache_dir=None, build_dir=None,
                      state_dir=state_dir)
    assert win2.size().width() == 500 and win2.size().height() == 400


def test_sort_folder_items_date_mixed_and_pre1903() -> None:
    """Date mode over the three data classes at once: canonical date_taken
    strings (an UNBOUNDED pre-1903 year included — §6 footgun 16) sort
    lexically-chronologically, an mtime-only photo interleaves at its
    local wall-clock time, dateless+unindexed photos sink to the end in
    name order, and a date_taken tie breaks by name (stability over the
    name-ordered input)."""
    from datetime import datetime

    from grid import SORT_DATE, sort_folder_items

    mt = int(datetime(2010, 1, 2, 3, 4, 5).timestamp())  # local, DST-safe
    photos = [
        _photo("a.jpg"),                                       # sinks
        _photo("b.jpg"),                                       # sinks
        _photo("c.jpg", mtime=mt),                             # 2010 via mtime
        _photo("d.jpg", date_taken="1899-06-01T00:00:00"),     # pre-1903
        _photo("e.jpg", date_taken="2020-05-01T10:00:00"),
        _photo("f.jpg", date_taken="2020-05-01T10:00:00"),     # tie with e
    ]
    items = list(range(len(photos)))                # name/walk order
    got = sort_folder_items(items, photos, SORT_DATE)
    assert got == [3, 2, 4, 5, 0, 1]
    assert items == list(range(len(photos)))        # input never mutated


def test_sort_folder_items_size_unindexed_sinks_and_ties() -> None:
    """Size mode: ascending Photo.size, unindexed (-1) photos sink to the
    end, and both equal sizes and the sunk tail keep name order."""
    from grid import SORT_SIZE, sort_folder_items

    photos = [
        _photo("a.jpg", size=-1),
        _photo("b.jpg", size=500),
        _photo("c.jpg", size=100),
        _photo("d.jpg", size=100),   # tie with c: name order holds
        _photo("e.jpg", size=-1),
    ]
    got = sort_folder_items(list(range(5)), photos, SORT_SIZE)
    assert got == [2, 3, 1, 0, 4]


def test_sort_default_name_is_walk_order(tmp_path: Path) -> None:
    """The honest-default claim: within one folder the walk already yields
    filename order, so SORT_NAME (the default) is the identity permutation
    and a modeless grid displays exactly catalog order."""
    _offscreen_app()
    from grid import DEFAULT_SORT_MODE, SORT_NAME, GridView, sort_folder_items

    assert DEFAULT_SORT_MODE == SORT_NAME
    root = tmp_path / "lib"
    for name in ("zed.jpg", "mid.jpg", "abc.jpg"):   # created out of order
        make_jpeg(root / "f" / name)
    cat = scan_library(root)
    assert [p.name for p in cat.photos] == ["abc.jpg", "mid.jpg", "zed.jpg"]
    items = list(range(len(cat.photos)))
    assert sort_folder_items(items, cat.photos, SORT_NAME) == items
    grid = GridView()
    grid.set_data(cat, None)
    assert grid.display == items                     # pre-q6l.11 behavior


def test_grid_sort_is_display_permutation_index_parity(tmp_path: Path) -> None:
    """The invariant that keeps the fcache binding safe: a sort mode only
    PERMUTES the default view's display list. Same index set as unsorted,
    catalog order untouched, loc/display_pos consistent, cache entries
    still keyed by catalog index (identical before/after), other folders'
    groups untouched — and an explicit display set (album/search/starred)
    ignores the mode entirely."""
    _offscreen_app()
    from grid import GridView

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "big.jpg", 256, 192)      # name order: big < small
    make_jpeg(root / "f" / "small.jpg", 16, 12)      # size order: small < big
    make_jpeg(root / "g" / "one.jpg")
    cat, cache = _bound_cache(tmp_path, root)
    assert cat.photos[0].name == "big.jpg" and cat.photos[0].size > 0
    assert cat.photos[0].size > cat.photos[1].size   # bytes follow pixels

    grid = GridView()
    grid.set_data(cat, cache)
    baseline = list(grid.display)
    assert baseline == [0, 1, 2]
    entries_before = [cache.entry(i) for i in range(3)]   # primary level

    grid.sort_modes = {"f": "size"}
    grid.set_filter(None, "")
    assert grid.display == [1, 0, 2]                 # f re-sorted, g untouched
    assert sorted(grid.display) == sorted(baseline)  # pure permutation
    assert [p.name for p in cat.photos] == ["big.jpg", "small.jpg", "one.jpg"]
    assert [cache.entry(i) for i in range(3)] == entries_before
    for pos, idx in enumerate(grid.display):         # display maps consistent
        assert grid.display_pos[idx] == pos
        gi, n = grid.loc[idx]
        assert grid.groups[gi].items[n] == idx

    grid.set_filter([0, 1], "album-ish")             # explicit set: given
    assert grid.display == [0, 1]                    # order wins, mode ignored


def test_folder_sort_context_menu_wiring(tmp_path: Path) -> None:
    """The sidebar folder context menu end to end: three checkable actions
    (current mode checked, name by default), triggering one applies the
    sort to the live view immediately and persists it to the per-library
    config.json, the rebuilt menu shows the new checkmark, and a second
    window over the same cache dir wakes up with the mode already applied
    (the persistence round-trip through the real load path)."""
    _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        make_jpeg(root / "f" / name)
    cache_dir = tmp_path / "cachedir"
    cache_dir.mkdir()

    def dated_catalog():
        cat = scan_library(root)
        for p, day in zip(cat.photos, ("03", "02", "01")):
            p.date_taken = f"2021-06-{day}T12:00:00"  # reverse of name order
        return cat

    win = MainWindow(dated_catalog(), None, cache_dir=cache_dir,
                     build_dir=None)
    assert win.grid.display == [0, 1, 2]
    menu = win._folder_sort_menu("f")
    acts = {a.data(): a for a in menu.actions() if a.isCheckable()}
    assert set(acts) == {"name", "date", "size"}
    assert acts["name"].isChecked()                  # default, honestly named

    acts["date"].trigger()
    assert win.grid.sort_modes == {"f": "date"}
    assert win.grid.display == [2, 1, 0]             # applied immediately
    doc = json.loads((cache_dir / "config.json").read_text())
    assert doc["sort_modes"] == {"f": "date"}
    remenu = {a.data(): a for a in win._folder_sort_menu("f").actions()
              if a.isCheckable()}
    assert remenu["date"].isChecked() and not remenu["name"].isChecked()

    # While a search view is up the mode change is stored, not applied —
    # the search set keeps its own order (folder-scoped feature).
    win.search.setText(".jpg")
    search_display = list(win.grid.display)
    win._set_folder_sort("f", "size")
    assert win.grid.display == search_display
    win._set_folder_sort("f", "date")                # restore for the reopen
    win.search.setText("")

    # Back to name: the default is dropped from the dict and the file.
    win._set_folder_sort("f", "name")
    assert win.grid.sort_modes == {} and win.grid.display == [0, 1, 2]
    assert json.loads(
        (cache_dir / "config.json").read_text())["sort_modes"] == {}

    win._set_folder_sort("f", "date")
    win2 = MainWindow(dated_catalog(), None, cache_dir=cache_dir,
                      build_dir=None)
    assert win2.grid.sort_modes == {"f": "date"}
    assert win2.grid.display == [2, 1, 0]            # sorted from first paint


def test_folder_view_flat_listing(tmp_path: Path) -> None:
    """Flat mode (fauxcasa-q6l.10): all non-empty folders appear as direct
    children of the Folders root (not nested), ordered alphabetically by leaf
    name (case-insensitive), each carrying the full on-disk path as tooltip."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItemIterator
    from main import MainWindow
    from catalog import scan_library

    root = tmp_path / "lib"
    make_jpeg(root / "Animals" / "cat.jpg")
    make_jpeg(root / "Animals" / "Dog" / "dog.jpg")
    make_jpeg(root / "Zebra" / "z.jpg")
    make_jpeg(root / "apples" / "a.jpg")   # lowercase leaf: sorts between Animals and Zebra

    cat = scan_library(root)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    # Default state: tree mode (flat toggle is off).
    assert not win._flat_check.isChecked()

    # Switch to flat mode.
    win._flat_check.setChecked(True)

    # Collect all ("folder", rel) items from the rebuilt sidebar.
    folder_items = []
    it = QTreeWidgetItemIterator(win.tree)
    while it.value():
        d = it.value().data(0, Qt.ItemDataRole.UserRole)
        if d is not None and d[0] == "folder":
            folder_items.append((d[1], it.value()))
        it += 1

    rels = [r for r, _ in folder_items]

    # All non-empty folders are present.
    expected_rels = set(cat.folders.keys())
    assert set(rels) == expected_rels, f"missing: {expected_rels - set(rels)}"

    # Items are sorted by leaf name, case-insensitive.
    leaf_names = [r.rsplit("/", 1)[-1].lower() for r in rels]
    assert leaf_names == sorted(leaf_names), f"not alphabetical: {leaf_names}"

    # Each item's tooltip is the full on-disk path.
    for rel, item in folder_items:
        expected_tip = str(cat.root / rel)
        assert item.toolTip(0) == expected_tip, (
            f"wrong tooltip for {rel!r}: {item.toolTip(0)!r}")

    # All items are direct children of the Folders root (no nesting).
    it2 = QTreeWidgetItemIterator(win.tree)
    while it2.value():
        d = it2.value().data(0, Qt.ItemDataRole.UserRole)
        if d is not None and d[0] == "folder":
            parent_data = it2.value().parent().data(
                0, Qt.ItemDataRole.UserRole)
            # Parent is the unselectable root — it carries ("folders_root",
            # "") (ez2.14: lets the right-click menu find it), not a
            # ("folder", rel) tuple of its own.
            assert parent_data == ("folders_root", ""), (
                f"folder item {d[1]!r} has parent with data {parent_data!r}")
        it2 += 1


def test_folder_view_flat_root_rel_photos(tmp_path: Path) -> None:
    """Photos directly in the library root (rel == "") must not render an
    unlabeled flat item: the leaf label falls back to the root's on-disk
    folder name, and tree mode's stand-in root node carries the path
    tooltip (codex cross-review finding, fauxcasa-q6l.10)."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItemIterator
    from main import MainWindow
    from catalog import scan_library

    root = tmp_path / "lib"
    make_jpeg(root / "rootpic.jpg")          # photo directly in the root
    make_jpeg(root / "Animals" / "cat.jpg")

    cat = scan_library(root)
    assert "" in cat.folders  # precondition: root folder is a real entry
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win._flat_check.setChecked(True)

    def folder_items():
        found = {}
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            d = it.value().data(0, Qt.ItemDataRole.UserRole)
            if d is not None and d[0] == "folder":
                found[d[1]] = it.value()
            it += 1
        return found

    flat = folder_items()
    root_item = flat[""]
    assert root_item.text(0).startswith("lib  ("), root_item.text(0)
    assert root_item.toolTip(0) == str(cat.root)

    # Tree mode: the stand-in root node (the "Folders" header, which carries
    # ("folders_root", "") item data — ez2.14 — not a ("folder", rel) tuple
    # of its own) keeps the path-on-demand tooltip.
    win._flat_check.setChecked(False)
    headers = [win.tree.topLevelItem(i)
               for i in range(win.tree.topLevelItemCount())]
    folders_header = [
        h for h in headers
        if h.data(0, Qt.ItemDataRole.UserRole) == ("folders_root", "")]
    assert folders_header, "Folders header not found"
    assert folders_header[0].toolTip(0) == str(cat.root)


def test_multiroot_flat_folder_listing(tmp_path: Path) -> None:
    """Flat mode across a genuine (>1-root) multiroot library (fauxcasa-
    q6l.10): items are alphabetical by leaf name across ALL roots, keyed by
    the root-qualified folder_key (not bare rel — two roots' same-named
    folders must stay distinct), tooltip is the OWNING root's on-disk path,
    and an offline root's folders stay listed (browseable from cache) but
    styled italic/dim rather than skipped."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
    from main import MainWindow
    from catalog import Catalog, Folder
    from grid import folder_key

    app = QApplication.instance() or QApplication([])
    assert app is not None

    root_a, root_b = tmp_path / "root-a", tmp_path / "root-b"
    root_a.mkdir(parents=True)
    # root_b deliberately never created -> offline (mirrors
    # test_sidebar_shows_offline_root_badge's fixture pattern).
    id_a, id_b = "aaaaaaaa", "bbbbbbbb"
    folders = {
        "Zebra": Folder(rel="Zebra", title="Zebra", photo_count=1,
                        total_count=1, root_id=id_a),
        "apples": Folder(rel="apples", title="apples", photo_count=1,
                         total_count=1, root_id=id_a),
        f"{id_b}/Mango": Folder(rel="Mango", title="Mango", photo_count=1,
                                total_count=1, root_id=id_b),
    }
    roots = [libmod.LibraryRoot(id=id_a, path=root_a, label="A"),
             libmod.LibraryRoot(id=id_b, path=root_b, label="B")]
    cat = Catalog(root=root_a, photos=[], folders=folders, albums={},
                 roots=roots, library_id=libmod.mint_library_id())
    cat.refresh_offline_ids()
    assert cat.offline_ids == {id_b}

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win._flat_check.setChecked(True)

    folder_items = []
    it = QTreeWidgetItemIterator(win.tree)
    while it.value():
        d = it.value().data(0, Qt.ItemDataRole.UserRole)
        if d is not None and d[0] == "folder":
            folder_items.append((d[1], it.value()))
        it += 1

    # Keyed by folder_key (root-qualified), not bare rel.
    keys = [k for k, _ in folder_items]
    assert keys == [folder_key(cat, id_a, "apples"),
                    folder_key(cat, id_b, "Mango"),
                    folder_key(cat, id_a, "Zebra")]
    assert set(keys) == set(folders)   # matches the folders dict's own keys

    by_key = dict(folder_items)
    # Tooltip carries the OWNING root's on-disk path, not root_a's for both.
    assert by_key[folder_key(cat, id_a, "apples")].toolTip(0) == \
        str(root_a / "apples")
    assert by_key[folder_key(cat, id_a, "Zebra")].toolTip(0) == \
        str(root_a / "Zebra")
    assert by_key[folder_key(cat, id_b, "Mango")].toolTip(0) == \
        str(root_b / "Mango")

    # The offline root's folder item is still listed (not skipped)...
    mango_item = by_key[folder_key(cat, id_b, "Mango")]
    assert mango_item.text(0) == "Mango  (1)"
    # ...but styled the same italic/dim cue the tree mode gives its root
    # header, since flat mode has no header row to carry the cue instead.
    assert mango_item.font(0).italic()
    # The online root's items are not styled offline.
    assert not by_key[folder_key(cat, id_a, "Zebra")].font(0).italic()


def test_folder_tooltip_description(tmp_path: Path) -> None:
    """Folder sidebar tooltips append the .picasa.ini [Picasa] description=
    (if present) after the on-disk path, on its own line, in all four
    sidebar shapes the flat/tree x single/multi-root toggle produces
    (fauxcasa-cam.14, rebuilt against the q6l.10 flat/tree sidebar rework).
    A folder with no description keeps the bare path-only tooltip."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItemIterator
    from main import MainWindow
    from catalog import scan_library, Catalog, Folder

    def folder_tooltip(win, rel: str) -> str | None:
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == ("folder", rel):
                return it.value().toolTip(0)
            it += 1
        return None

    # --- single-root: tree mode and flat mode ---------------------------
    root = tmp_path / "lib"
    make_jpeg(root / "trip" / "a.jpg")
    (root / "trip" / ".picasa.ini").write_text(
        "[Picasa]\r\ndescription=Summer 2020\r\n")
    make_jpeg(root / "plain" / "b.jpg")  # no ini, no description

    cat = scan_library(root)
    assert cat.folders["trip"].description == "Summer 2020"
    assert cat.folders["plain"].description is None

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    # Tree mode (default)
    tip = folder_tooltip(win, "trip")
    assert tip is not None
    assert str(root / "trip") in tip and "Summer 2020" in tip
    assert folder_tooltip(win, "plain") == str(root / "plain")

    # Flat mode
    win._flat_check.setChecked(True)
    tip_flat = folder_tooltip(win, "trip")
    assert tip_flat is not None
    assert str(root / "trip") in tip_flat and "Summer 2020" in tip_flat
    assert folder_tooltip(win, "plain") == str(root / "plain")

    # --- multiroot: tree mode and flat mode -----------------------------
    root_a, root_b = tmp_path / "root-a", tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    id_a, id_b = "aaaaaaaa", "bbbbbbbb"
    folders = {
        "2019": Folder(rel="2019", title="A 2019", description="Alpha album",
                       photo_count=1, total_count=1, root_id=id_a),
        f"{id_b}/2019": Folder(rel="2019", title="B 2019",
                               photo_count=1, total_count=1, root_id=id_b),
    }
    roots = [libmod.LibraryRoot(id=id_a, path=root_a, label="Primary"),
             libmod.LibraryRoot(id=id_b, path=root_b, label="Archive")]
    mcat = Catalog(root=root_a, photos=[], folders=folders, albums={},
                   roots=roots, library_id=libmod.mint_library_id())

    mwin = MainWindow(mcat, None, cache_dir=None, build_dir=None)

    # Tree mode (default): tooltip uses the OWNING root's manifest path.
    tip_a = folder_tooltip(mwin, "2019")
    assert tip_a is not None
    assert str(root_a / "2019") in tip_a and "Alpha album" in tip_a
    assert folder_tooltip(mwin, f"{id_b}/2019") == str(root_b / "2019")

    # Flat mode
    mwin._flat_check.setChecked(True)
    tip_a_flat = folder_tooltip(mwin, "2019")
    assert tip_a_flat is not None
    assert str(root_a / "2019") in tip_a_flat and "Alpha album" in tip_a_flat
    assert folder_tooltip(mwin, f"{id_b}/2019") == str(root_b / "2019")


def test_folder_view_toggle_persistence(tmp_path: Path) -> None:
    """Flat/tree choice persists to the per-library config.json (q6l.10).
    save_folder_view / load_folder_view round-trip; default (tree) is absent
    rather than False; the toggle merges with sort_modes in the same doc."""
    from main import load_folder_view, save_folder_view, save_sort_modes

    # Default: tree mode (no key in file yet).
    assert load_folder_view(None) is False
    assert load_folder_view(tmp_path / "nowhere") is False

    # Persist flat=True; default (tree=False) is absent, not stored.
    save_folder_view(tmp_path, True)
    assert load_folder_view(tmp_path) is True
    doc = json.loads((tmp_path / "config.json").read_text())
    assert doc.get("folder_view_flat") is True

    # Switching back to tree removes the key.
    save_folder_view(tmp_path, False)
    assert load_folder_view(tmp_path) is False
    doc2 = json.loads((tmp_path / "config.json").read_text())
    assert "folder_view_flat" not in doc2

    # save_folder_view merges with sort_modes — neither key clobbers the other.
    save_sort_modes(tmp_path, {"x": "date"})
    save_folder_view(tmp_path, True)
    merged = json.loads((tmp_path / "config.json").read_text())
    assert merged["sort_modes"] == {"x": "date"}
    assert merged["folder_view_flat"] is True

    save_folder_view(tmp_path, False)
    merged2 = json.loads((tmp_path / "config.json").read_text())
    assert merged2["sort_modes"] == {"x": "date"}   # sort_modes untouched
    assert "folder_view_flat" not in merged2

    # Garbage / non-object inputs are tolerated.
    (tmp_path / "config.json").write_text("{not json")
    assert load_folder_view(tmp_path) is False
    (tmp_path / "config.json").write_text("null")
    assert load_folder_view(tmp_path) is False
    (tmp_path / "config.json").write_text('{"folder_view_flat": "yes"}')
    assert load_folder_view(tmp_path) is False   # non-bool value ignored


def test_folder_view_toggle_wires_and_persists_in_window(
        tmp_path: Path) -> None:
    """The flat-check in a MainWindow switches the sidebar view and persists
    the choice to the per-library config.json; a second window over the same
    cache dir wakes up in flat mode."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItemIterator
    from main import MainWindow
    from catalog import scan_library

    root = tmp_path / "lib"
    make_jpeg(root / "Bees" / "b.jpg")
    make_jpeg(root / "Ants" / "a.jpg")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    cat = scan_library(root)
    win = MainWindow(cat, None, cache_dir=cache_dir, build_dir=None)

    # Tree mode by default: "Ants" and "Bees" appear nested under Folders.
    assert not win._flat_check.isChecked()

    def folder_items(win2):
        items = []
        it = QTreeWidgetItemIterator(win2.tree)
        while it.value():
            d = it.value().data(0, Qt.ItemDataRole.UserRole)
            if d is not None and d[0] == "folder":
                items.append((d[1], it.value()))
            it += 1
        return items

    tree_items = folder_items(win)
    assert {r for r, _ in tree_items} == {"Ants", "Bees"}

    # Toggle to flat — persists.
    win._flat_check.setChecked(True)
    assert (cache_dir / "config.json").is_file()
    cfg = json.loads((cache_dir / "config.json").read_text())
    assert cfg.get("folder_view_flat") is True

    flat_items = folder_items(win)
    leaf_names = [r.rsplit("/", 1)[-1] for r, _ in flat_items]
    assert sorted(leaf_names, key=str.lower) == leaf_names   # alphabetical

    # Clicking a flat-mode folder item still selects the grid view correctly:
    # folder clicks show all photos (no filter) and scroll to the folder;
    # verify it doesn't crash and the grid has the correct item count.
    _, first_item = flat_items[0]
    first_item.setSelected(True)
    win._sidebar_clicked(first_item, 0)
    # Folder click keeps all photos visible (no filter set).
    assert win.grid.filter_label == ""
    assert len(win.grid.display) == len(cat.photos)

    # A second window over the same cache dir starts in flat mode.
    cat2 = scan_library(root)
    win2 = MainWindow(cat2, None, cache_dir=cache_dir, build_dir=None)
    assert win2._flat_check.isChecked()


def test_slideshow_follows_folder_sort_order(tmp_path: Path) -> None:
    """The slideshow (and viewer activation) consume grid.display, so a
    folder's sort mode carries through with no code of its own: Play on a
    date-sorted folder view starts the show over the SORTED display list."""
    _offscreen_app()
    from main import MainWindow

    cat = scan_library(_show_library(tmp_path))
    for p, day in zip(cat.photos, ("03", "02", "01")):
        p.date_taken = f"2021-06-{day}T12:00:00"     # reverse of name order
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win._set_folder_sort("show", "date")
    assert win.grid.display == [2, 1, 0]
    win._start_slideshow()
    assert win._slideshow is not None
    assert win._slideshow.display == [2, 1, 0]
    win._slideshow._exit()
