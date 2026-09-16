"""Tests for main.py reveal mode, the Hidden Folders category, the bench_scroll occlusion gate, and file-log diagnostics.

Split from test_tracer.py (fauxcasa-l09); originally lines 3154-4607 of the monolith."""

from __future__ import annotations

import json
import sys
from pathlib import Path
import pytest
import library as libmod
import thumbcache
from catalog import (
    ScanFilter,
    load_catalog,
    save_catalog,
    scan_library,
)
from tracer_helpers import (
    APP_DIR,
    _offscreen_app,
    make_jpeg,
)


def test_reveal_sidebar_counts_and_starred(reveal_library: Path) -> None:
    """Reveal mode updates the rendered per-folder count TEXT in the sidebar
    AND the Starred tally (the hidden-starred path), and a Show-hidden toggle
    PRESERVES the active (non-All) view rather than resetting to All photos
    (fauxcasa-f5k / fauxcasa-x1l)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    def item_for(win, kind: str, key: str):
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == (kind, key):
                return it.value()
            it += 1
        return None

    cat = scan_library(reveal_library)
    trip = cat.folders["Trip"]
    assert trip.photo_count == 1 and trip.total_count == 2  # shown + hidden
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    # default (visible-only): the folder's rendered count is its 1 visible
    # photo, and the Starred node counts only the visible starred photo.
    folder_item = item_for(win, "folder", "Trip")
    assert folder_item is not None and folder_item.text(0).endswith("(1)")
    assert "(1)" in item_for(win, "starred", "").text(0)

    # Drive an explicit, non-All view: select Starred.
    win._sidebar_clicked(item_for(win, "starred", ""), 0)
    assert win.grid.filter_label == "Starred" and len(win.grid.display) == 1

    # Reveal: the per-folder count TEXT now includes the hidden photo (2),
    # the Starred node text reflects the hidden starred secret.jpg (2), and
    # the x1l preserved-context behaviour keeps us on Starred — NOT All — now
    # showing both starred photos.
    win.reveal_box.setChecked(True)
    assert item_for(win, "folder", "Trip").text(0).endswith("(2)")
    assert "(2)" in item_for(win, "starred", "").text(0)
    assert win.grid.filter_label == "Starred"        # not reset to All
    assert len(win.grid.display) == 2
    assert "Starred: 2 photos" in win.counts_label.text()
    # the sidebar highlight followed the preserved view
    assert win.tree.currentItem() is item_for(win, "starred", "")

    # Toggle back off: still on Starred, hidden photo gone, count TEXT 1 again.
    win.reveal_box.setChecked(False)
    assert win.grid.filter_label == "Starred" and len(win.grid.display) == 1
    assert item_for(win, "folder", "Trip").text(0).endswith("(1)")
    assert "(1)" in item_for(win, "starred", "").text(0)
    assert "Starred: 1 photos" in win.counts_label.text()


def test_sidebar_rebuild_survives_repeated_toggles(reveal_library: Path) -> None:
    """Regression for fauxcasa-gfz: rebuilding the sidebar while a tree item is
    current must not corrupt Qt state. The old clear()-in-place rebuild
    intermittently access-violated on the real Windows Qt platform (a single
    window, a handful of rebuilds was enough); _rebuild_sidebar() swaps in a
    fresh tree and deleteLater()s the old one instead. Loop the Show-hidden
    toggle (each toggle = one rebuild with Starred as the current item) far past
    the pre-fix crash threshold; a revert segfaults the whole run here."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    def item_for(win, kind: str, key: str):
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == (kind, key):
                return it.value()
            it += 1
        return None

    cat = scan_library(reveal_library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win._sidebar_clicked(item_for(win, "starred", ""), 0)  # a live current item

    for _ in range(30):
        win.reveal_box.setChecked(True)
        win.reveal_box.setChecked(False)
        app.processEvents()  # let the deferred old-tree deletions run

    # Still coherent after all the swaps: Starred view preserved, counts sane.
    assert win.grid.filter_label == "Starred"
    assert item_for(win, "starred", "") is not None
    assert win.tree.currentItem() is item_for(win, "starred", "")


def test_empty_text_search_no_match(library: Path) -> None:
    """A search with zero hits sets the grid's painted placeholder to the
    query-specific wording (fauxcasa-ez2.6 empty states); a match clears it
    again rather than leaving stale copy under real tiles."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    win.search.setText("nonesuch")
    assert win.grid.display == []
    assert win.grid.empty_text == 'No photos match "nonesuch"'

    win.search.setText("beach")
    assert win.grid.display
    assert win.grid.empty_text == ""


def test_empty_text_folder_with_no_photos(tmp_path: Path) -> None:
    """The "folder" kind gets the gentler folder-specific wording rather
    than the terminal "empty library" one, distinguished purely by the
    active sidebar selection — folders with zero photos never actually
    appear as sidebar items (see _build_sidebar's fcount(folder) > 0
    filter), so this drives _apply_view("folder", ...) directly the same
    way _sidebar_clicked would, against a bare empty catalog, to pin the
    wording _update_empty_text picks for that kind."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItem
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    root = tmp_path / "empty-lib"
    root.mkdir()
    cat = scan_library(root)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    item = QTreeWidgetItem(win.tree, ["Empty Folder"])
    item.setData(0, Qt.ItemDataRole.UserRole, ("folder", "Empty Folder"))
    win.tree.setCurrentItem(item)
    win._apply_view("folder", "Empty Folder")
    assert win.grid.display == []
    assert win.grid.empty_text == "This folder has no photos"


def test_empty_text_empty_library(tmp_path: Path) -> None:
    """A library with no photos at all names the root and points at
    Library… — distinct copy from a merely-empty search or folder."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    root = tmp_path / "empty-lib"
    root.mkdir()
    cat = scan_library(root)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    assert win.grid.display == []
    assert win.grid.empty_text == (
        f"No photos found under {cat.root.name} — use Library… "
        "to pick another folder")


def test_empty_text_cold_scan_pending_preempts_empty_library(
        tmp_path: Path) -> None:
    """While a deferred cold scan is in flight the grid is genuinely empty
    (the placeholder catalog), but the library isn't confirmed empty yet —
    the scanning wording must win over the terminal "no photos" one, and
    landing the walk clears it back to real content."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow
    from catalog import Catalog

    app = QApplication.instance() or QApplication([])
    assert app is not None

    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    cfg = libmod.legacy_config(root)
    empty = Catalog(root=cfg.roots[0].path, photos=[], folders={},
                    albums={}, roots=list(cfg.roots),
                    library_id=cfg.library_id)
    cache_dir = tmp_path / "cachedir"
    win = MainWindow(empty, None, cache_dir=cache_dir, build_dir=None,
                     cfg=cfg)
    assert win.grid.empty_text.startswith("No photos found under")

    win._start_cold_scan(cache_dir)
    assert win.grid.empty_text == f"Scanning {empty.root.name}…"

    import time as _time
    deadline = _time.time() + 5.0
    while win._cold_scan_pending and _time.time() < deadline:
        app.processEvents()
        _time.sleep(0.01)
    app.processEvents()
    assert not win._cold_scan_pending
    assert win.grid.empty_text == ""       # the real, non-empty catalog landed
    win.shutdown()


def test_reveal_preserves_search_view(library: Path) -> None:
    """A Show-hidden toggle while a search is active keeps the search view
    and recomputes it for the new reveal state, instead of clearing the box
    and snapping to All photos (fauxcasa-x1l)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    # Search by extension: matches every photo's filename, but the default
    # (visible-only) view excludes the hidden b.jpg and the stashed original.
    win.search.setText(".jpg")
    assert "Search" in win.counts_label.text()
    visible_hits = len(win.grid.display)
    assert visible_hits == 2  # a.jpg + c.jpg

    win.reveal_box.setChecked(True)
    assert win.search.text() == ".jpg"               # search box preserved
    assert "Search" in win.counts_label.text()       # not reset to All
    assert len(win.grid.display) == 4                 # hidden + stash now match


# ---- folder-level "Hidden Folders" category (fauxcasa-r42) ----------------
#
# Synthetic fixtures only (privacy): hand-authored .picasa.ini files + tiny
# Qt-generated JPEGs. The on-disk format (folder [Picasa] P2category=Hidden
# Folders, sibling of Folders on Disk) is DOCUMENTED — committed fixture 025
# carries the Folders-on-Disk block; docs/research/wine-oracle.md lists
# "Hidden Folders" in the catdata categories — but NOT yet captured in a
# hide-folder oracle differential (a future 032-hide-folder fixture).


def test_scan_folder_hidden_category(folder_hidden_library: Path) -> None:
    """A folder in the Hidden Folders category marks Folder.folder_hidden and
    forces all its photos invisible (counting toward total_count, not
    photo_count) — while the sibling Folders-on-Disk category does NOT."""
    cat = scan_library(folder_hidden_library)
    normal = cat.folders["2020 Trip"]
    secret = cat.folders["2021 Secret"]

    # the sibling category is a normal, visible folder
    assert not normal.folder_hidden
    assert normal.photo_count == 1 and normal.total_count == 1

    # the Hidden Folders category hides the whole folder
    assert secret.folder_hidden
    assert secret.photo_count == 0 and secret.total_count == 2

    for p in cat.photos:
        assert p.visible == (p.folder != "2021 Secret")
    assert cat.visible_count == 1  # only 2020 Trip/a.jpg


def test_folder_hidden_matcher_is_defensive() -> None:
    """_is_folder_hidden is trimmed + case-insensitive on the exact value,
    never false-positives on the sibling categories, and tolerates an absent
    P2category or [Picasa] section."""
    import catalog
    from picasa_db import IniSection

    def mk(v: str) -> IniSection:
        return IniSection(name="Picasa", items=[("P2category", v)])

    assert catalog._is_folder_hidden(mk("Hidden Folders"))
    assert catalog._is_folder_hidden(mk("  hidden folders  "))  # trim + case
    assert catalog._is_folder_hidden(mk("HIDDEN FOLDERS"))
    assert not catalog._is_folder_hidden(mk("Folders on Disk"))  # sibling
    assert not catalog._is_folder_hidden(mk("Exported Pictures"))
    assert not catalog._is_folder_hidden(mk(""))
    assert not catalog._is_folder_hidden(IniSection(name="Picasa"))  # no key
    assert not catalog._is_folder_hidden(None)  # no [Picasa] section at all


def test_folder_hidden_legacy_category_key() -> None:
    """Legacy Picasa 2 used category= (not P2category=); _is_folder_hidden
    accepts both so pre-3.x folders are hidden correctly (fauxcasa-cam.14)."""
    import catalog
    from picasa_db import IniSection

    def mk_legacy(v: str) -> IniSection:
        return IniSection(name="Picasa", items=[("category", v)])

    assert catalog._is_folder_hidden(mk_legacy("Hidden Folders"))
    assert catalog._is_folder_hidden(mk_legacy("hidden folders"))
    assert not catalog._is_folder_hidden(mk_legacy("Folders on Disk"))
    assert not catalog._is_folder_hidden(mk_legacy(""))


def test_scan_folder_hidden_legacy_category(tmp_path: Path) -> None:
    """A folder with [Picasa] category=Hidden Folders (legacy spelling) is
    also hidden — same result as P2category= (fauxcasa-cam.14)."""
    root = tmp_path / "lib"
    make_jpeg(root / "visible" / "a.jpg")
    (root / "visible" / ".picasa.ini").write_text(
        "[Picasa]\r\nP2category=Folders on Disk\r\n")
    make_jpeg(root / "hidden" / "b.jpg")
    (root / "hidden" / ".picasa.ini").write_text(
        "[Picasa]\r\ncategory=Hidden Folders\r\n")
    cat = scan_library(root)
    assert not cat.folders["visible"].folder_hidden
    assert cat.folders["hidden"].folder_hidden
    assert cat.visible_count == 1


def test_folder_hidden_survives_catalog_roundtrip(
        folder_hidden_library: Path, tmp_path: Path) -> None:
    """folder_hidden + the derived per-photo visibility round-trip through the
    persisted catalog (the warm-load path never re-reads inis, so membership
    is stored in `hidden_folders` and re-derived on load)."""
    cat = scan_library(folder_hidden_library)
    thumbcache.build_cache(cat, tmp_path / "c")  # fills signals
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    loaded = load_catalog(path, folder_hidden_library)
    assert loaded is not None
    assert loaded.folders["2021 Secret"].folder_hidden
    assert not loaded.folders["2020 Trip"].folder_hidden
    assert loaded.folders["2021 Secret"].photo_count == 0
    assert loaded.folders["2021 Secret"].total_count == 2
    assert loaded.visible_count == 1
    for p in loaded.photos:
        assert p.visible == (p.folder != "2021 Secret")


def test_mainwindow_reveal_folder_hidden(folder_hidden_library: Path) -> None:
    """A folder in the 'Hidden Folders' category is absent from the normal
    sidebar and excluded from the grid and status counts; the Show-hidden
    toggle surfaces it and its photos, with reveal-mode per-folder and status
    counts including them — the same mechanism that surfaces stash folders."""
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

    def folder_item(win, rel: str):
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == ("folder", rel):
                return it.value()
            it += 1
        return None

    SECRET = "2021 Secret"
    cat = scan_library(folder_hidden_library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    # default: only the normal folder's one visible photo; the hidden folder
    # is gone from the tree and the status tally counts neither it nor its
    # photos.
    assert not win.grid.reveal and len(win.grid.display) == 1
    assert SECRET not in folder_rels(win)
    assert "1 photos · 1 folders" in win.counts_label.text()

    win.reveal_box.setChecked(True)  # fires _toggle_reveal(True)
    assert win.grid.reveal and len(win.grid.display) == 3  # 1 + 2 hidden
    assert SECRET in folder_rels(win)                      # surfaced in tree
    assert folder_item(win, SECRET).text(0).endswith("(2)")  # reveal count
    assert "3 photos · 2 folders" in win.counts_label.text()

    win.reveal_box.setChecked(False)
    assert not win.grid.reveal and len(win.grid.display) == 1
    assert SECRET not in folder_rels(win)
    assert "1 photos · 1 folders" in win.counts_label.text()


def test_pump_decoded_byte_accounting() -> None:
    """_pump_decoded keeps _cache_bytes exact when an index is re-decoded
    in place (the live-build re-feed path): the previous tile's bytes are
    subtracted exactly once. Only the product feed->done->_pump path hits
    this branch, so drive it directly here."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication
    from grid import GridView

    app = QApplication.instance() or QApplication([])
    assert app is not None
    g = GridView()

    def tile(px: int) -> QImage:
        im = QImage(px, px, QImage.Format.Format_RGB32)
        im.fill(QColor(1, 2, 3))
        return im

    big, small = tile(128), tile(64)
    gen = g.generation

    g.done.put((gen, 7, big))           # first decode of idx 7
    g._pump_decoded()
    assert g.tiles[7][2] == big.sizeInBytes()
    assert g._cache_bytes == big.sizeInBytes()

    g.done.put((gen, 7, small))         # re-decode same idx, smaller image
    g._pump_decoded()
    assert g.tiles[7][2] == small.sizeInBytes()
    assert g._cache_bytes == small.sizeInBytes()  # NOT big + small
    assert g._cache_bytes == sum(t[2] for t in g.tiles.values())

    g.done.put((gen, 7, None))          # re-decode to an error tile: 0 bytes
    g._pump_decoded()
    assert g.tiles[7][0] is None and g.tiles[7][2] == 0
    assert g._cache_bytes == 0

    # a stale-generation result is dropped and must not touch the counter
    g.done.put((gen - 1, 9, big))
    g._pump_decoded()
    assert 9 not in g.tiles and g._cache_bytes == 0


def test_default_library_frozen_vs_checkout(monkeypatch) -> None:
    """main._default_library: the bundled synthetic library in a source
    checkout; None when frozen (REPO points inside the read-only bundle, so
    there is nothing to default to — recall/prompt takes over)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    monkeypatch.setattr(main, "FROZEN", False)
    assert main._default_library() == main.REPO / "cache" / "synthetic-library"
    monkeypatch.setattr(main, "FROZEN", True)
    assert main._default_library() is None


def test_remember_library_roundtrip(tmp_path: Path) -> None:
    """The remembered-library config survives a round trip, ignores a
    library that no longer exists (so the app re-prompts), and tolerates a
    missing or corrupt config file."""
    import os
    import shutil
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    cache_root = tmp_path / "cr"
    assert main._remembered_library(cache_root) is None  # nothing stored yet

    lib = tmp_path / "lib"
    lib.mkdir()
    main._remember_library(cache_root, lib)
    assert main._remembered_library(cache_root) == lib

    shutil.rmtree(lib)  # a vanished library is ignored, not returned
    assert main._remembered_library(cache_root) is None

    main._config_path(cache_root).write_text("{ not json")  # corrupt
    assert main._remembered_library(cache_root) is None
    main._config_path(cache_root).write_text(json.dumps({"other": 1}))  # no key
    assert main._remembered_library(cache_root) is None


def test_remembered_library_ignores_filesystem_root(tmp_path: Path, capsys) -> None:
    """A frozen first-run mistake can persist '/' as the last library. Treat
    that as no remembered library so the next launch re-prompts instead of
    scanning the whole filesystem before any main window exists."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    cache_root = tmp_path / "cr"
    main._config_path(cache_root).parent.mkdir(parents=True)
    main._config_path(cache_root).write_text(json.dumps({"library": str(Path("/"))}))

    assert main._remembered_library(cache_root) is None
    assert "ignoring remembered filesystem root" in capsys.readouterr().err


def test_resolve_library_order(monkeypatch, tmp_path: Path) -> None:
    """_resolve_library precedence: explicit arg → checkout default →
    (frozen) remembered → prompt; a frozen explicit arg is remembered."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    cache_root = tmp_path / "cr"
    explicit = tmp_path / "explicit"
    explicit.mkdir()

    # explicit arg wins and is NOT remembered in a source checkout
    monkeypatch.setattr(main, "FROZEN", False)
    assert main._resolve_library(str(explicit), cache_root) == explicit.resolve()
    assert main._remembered_library(cache_root) is None

    # an explicit but missing path is an error (None), never a silent prompt
    monkeypatch.setattr(main, "_prompt_for_library",
                        lambda cr: pytest.fail("explicit arg must not prompt"))
    assert main._resolve_library(str(tmp_path / "nope"), cache_root) is None

    # no arg in a checkout → the built-in default (patched to a real dir)
    fake_default = tmp_path / "synthetic"
    fake_default.mkdir()
    monkeypatch.setattr(main, "_default_library", lambda: fake_default)
    assert main._resolve_library(None, cache_root) == fake_default.resolve()

    # frozen: an explicit arg is remembered for the next double-click
    monkeypatch.setattr(main, "FROZEN", True)
    monkeypatch.setattr(main, "_default_library", lambda: None)
    assert main._resolve_library(str(explicit), cache_root) == explicit.resolve()
    assert main._remembered_library(cache_root) == explicit.resolve()

    # frozen, no arg → recall the remembered library WITHOUT prompting
    assert main._resolve_library(None, cache_root) == explicit.resolve()


def test_resolve_library_rejects_filesystem_root(
        monkeypatch, tmp_path: Path, capsys) -> None:
    """An explicit filesystem root must fail before scan_library() can walk
    the OS tree. Frozen mode must not remember the bad choice."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    monkeypatch.setattr(main, "FROZEN", True)
    cache_root = tmp_path / "cr"

    assert main._resolve_library(str(Path("/")), cache_root) is None
    err = capsys.readouterr().err
    assert "refusing to scan filesystem root" in err
    assert main._remembered_library(cache_root) is None


def test_restart_command_source_vs_frozen(monkeypatch, tmp_path: Path) -> None:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    library = tmp_path / "lib"
    cache_root = tmp_path / "cr"

    monkeypatch.setattr(main, "FROZEN", True)
    monkeypatch.setattr(main.sys, "executable", "/tmp/fauxcasa-tracer")
    assert main._restart_command(library, cache_root) == (
        "/tmp/fauxcasa-tracer",
        [str(library), "--cache-root", str(cache_root)],
    )

    monkeypatch.setattr(main, "FROZEN", False)
    assert main._restart_command(library, cache_root) == (
        "/tmp/fauxcasa-tracer",
        [str(main.APP_DIR / "main.py"), str(library),
         "--cache-root", str(cache_root)],
    )


def test_restart_command_scan_filter_flags(monkeypatch, tmp_path: Path) -> None:
    """--min-image-size and --max-image-size are forwarded when set, absent
    when the filter is inactive — fauxcasa-q6l.19."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    library = tmp_path / "lib"
    cache_root = tmp_path / "cr"
    monkeypatch.setattr(main, "FROZEN", True)
    monkeypatch.setattr(main.sys, "executable", "/usr/bin/fauxcasa")

    # No filter — neither flag appears.
    _prog, args = main._restart_command(library, cache_root, scan_filter=None)
    assert "--min-image-size" not in args
    assert "--max-image-size" not in args

    # Inactive filter (all zeros) — same: no flags.
    _prog, args = main._restart_command(
        library, cache_root, scan_filter=ScanFilter())
    assert "--min-image-size" not in args
    assert "--max-image-size" not in args

    # Active min filter only.
    sf = ScanFilter(min_width=100, min_height=75)
    _prog, args = main._restart_command(library, cache_root, scan_filter=sf)
    idx = args.index("--min-image-size")
    assert args[idx + 1] == "100x75"
    assert "--max-image-size" not in args

    # Active max filter only.
    sf = ScanFilter(max_width=8000, max_height=6000)
    _prog, args = main._restart_command(library, cache_root, scan_filter=sf)
    assert "--min-image-size" not in args
    idx = args.index("--max-image-size")
    assert args[idx + 1] == "8000x6000"

    # Both min and max set.
    sf = ScanFilter(min_width=100, min_height=75,
                    max_width=8000, max_height=6000)
    _prog, args = main._restart_command(library, cache_root, scan_filter=sf)
    assert args[args.index("--min-image-size") + 1] == "100x75"
    assert args[args.index("--max-image-size") + 1] == "8000x6000"


def test_restart_command_thumbs_flag(monkeypatch, tmp_path: Path) -> None:
    """--thumbs is forwarded only when supplied — fauxcasa-q6l.19."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    library = tmp_path / "lib"
    cache_root = tmp_path / "cr"
    thumbs_path = tmp_path / "bench.fcache"
    monkeypatch.setattr(main, "FROZEN", True)
    monkeypatch.setattr(main.sys, "executable", "/usr/bin/fauxcasa")

    # thumbs=None — flag absent.
    _prog, args = main._restart_command(library, cache_root, thumbs=None)
    assert "--thumbs" not in args

    # thumbs supplied — flag present with the correct path.
    _prog, args = main._restart_command(library, cache_root, thumbs=thumbs_path)
    idx = args.index("--thumbs")
    assert args[idx + 1] == str(thumbs_path)


def test_restart_command_open_drops_thumbs(monkeypatch, tmp_path: Path) -> None:
    """_change_library (Open...) does NOT carry --thumbs to a different
    library — the adopted cache is specific to the original library.
    fauxcasa-q6l.19."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication, QFileDialog
    import main

    app = QApplication.instance() or QApplication([])
    assert app is not None

    current = tmp_path / "Current"
    chosen = tmp_path / "Chosen"
    make_jpeg(current / "a.jpg")
    make_jpeg(chosen / "b.jpg")
    cache_root = tmp_path / "cr"
    thumbs_path = tmp_path / "bench.fcache"

    cat = scan_library(current)
    sf = ScanFilter(min_width=50, min_height=50)
    win = main.MainWindow(cat, None, cache_root / "cache", None,
                          scan_filter=sf, cache_root=cache_root,
                          thumbs_path=thumbs_path)

    captured: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(chosen)))
    monkeypatch.setattr(QProcess, "startDetached",
                        staticmethod(lambda prog, argv:
                                     captured.append((prog, argv)) or True))

    win._change_library()

    assert captured, "expected startDetached to be called"
    _prog, argv = captured[0]
    # Scan-size constraint preserved.
    assert "--min-image-size" in argv
    # --thumbs must NOT appear for a different-library relaunch.
    assert "--thumbs" not in argv


def test_restart_command_file_types_drops_thumbs(
        monkeypatch, tmp_path: Path) -> None:
    """_show_file_types must NOT carry --thumbs: a File-Types change alters
    the effective walk (exts), so an adopted cache no longer matches the new
    file set — bind() would raise CacheError and the relaunched process exits
    2 with no UI.  The relaunch must cold-rebuild instead.
    fauxcasa-q6l.19."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication
    from filetypes import FileTypesDialog, save_excluded_exts
    import main

    app = QApplication.instance() or QApplication([])
    assert app is not None

    lib = tmp_path / "lib"
    make_jpeg(lib / "a.jpg")
    cache_root = tmp_path / "cr"
    thumbs_path = tmp_path / "bench.fcache"

    cat = scan_library(lib)
    sf = ScanFilter(min_width=50, min_height=50)
    win = main.MainWindow(cat, None, cache_root / "cache", None,
                          scan_filter=sf, cache_root=cache_root,
                          thumbs_path=thumbs_path)

    captured: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(QProcess, "startDetached",
                        staticmethod(lambda prog, argv:
                                     captured.append((prog, argv)) or True))
    # Simulate user toggling a file type (different from current exclusions).
    monkeypatch.setattr(FileTypesDialog, "exec_", lambda self: True)
    monkeypatch.setattr(FileTypesDialog, "excluded", lambda self: {".tga"})

    win._show_file_types()

    assert captured, "expected startDetached to be called"
    _prog, argv = captured[0]
    # Scan-size constraint preserved.
    assert "--min-image-size" in argv
    # --thumbs must NOT appear: the extension change alters the file set,
    # so an adopted cache would fail bind() — the relaunch cold-rebuilds.
    assert "--thumbs" not in argv


def test_resolve_library_frozen_first_run(monkeypatch, tmp_path: Path) -> None:
    """Frozen, no library and nothing remembered: a chosen folder is used;
    a cancelled/headless picker yields None (graceful), not a crash."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    cache_root = tmp_path / "cr"  # empty: nothing remembered
    monkeypatch.setattr(main, "FROZEN", True)
    monkeypatch.setattr(main, "_default_library", lambda: None)

    picked = tmp_path / "picked"
    picked.mkdir()
    monkeypatch.setattr(main, "_prompt_for_library", lambda cr: picked)
    assert main._resolve_library(None, cache_root) == picked

    monkeypatch.setattr(main, "_prompt_for_library", lambda cr: None)
    assert main._resolve_library(None, cache_root) is None


def test_prompt_for_library_headless_returns_none(tmp_path: Path) -> None:
    """Under an offscreen/headless platform there is no one to answer a
    modal folder dialog — the picker must bail with None, never block."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    assert main._prompt_for_library(tmp_path) is None


def test_gui_unavailable_decided_before_qapplication(monkeypatch) -> None:
    """main._gui_unavailable (fauxcasa-7e5 fix 1) decides headlessness from
    the ENVIRONMENT ALONE — it must NOT construct a QApplication, because on
    Linux with the default xcb plugin and no DISPLAY/WAYLAND, QApplication([])
    aborts the process (exit 134) before any in-process guard can run. A
    headless Qt platform (offscreen/minimal/vnc) is unavailable; otherwise on
    Linux a real display (DISPLAY or WAYLAND_DISPLAY) is required, while a
    non-Linux desktop is assumed to have one."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    for plat in ("offscreen", "minimal", "vnc", "offscreen:somearg"):
        monkeypatch.setenv("QT_QPA_PLATFORM", plat)
        assert main._gui_unavailable() is True

    # No headless platform forced: fall through to the per-OS display check.
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.setattr(main.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert main._gui_unavailable() is True             # Linux, no display
    monkeypatch.setenv("DISPLAY", ":0")
    assert main._gui_unavailable() is False            # X11 display present
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert main._gui_unavailable() is False            # Wayland present
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(main.sys, "platform", "darwin")
    assert main._gui_unavailable() is False            # non-Linux: assume GUI


def test_remembered_library_ignores_non_object_json(tmp_path: Path) -> None:
    """main._remembered_library (fauxcasa-7e5 fix 2): a valid-but-non-object
    config ('null', '42', '[]', a bare string/bool) has no .get and must be
    treated as 'nothing remembered' (None) — NOT raise an AttributeError past
    the (OSError, ValueError) catch and crash the launch with a traceback."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    cache_root = tmp_path / "cr"
    cfg = main._config_path(cache_root)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    for blob in ("null", "42", "[]", '"just a path-shaped string"', "true"):
        cfg.write_text(blob)
        assert main._remembered_library(cache_root) is None


def test_remember_library_atomic_leaves_no_temp(tmp_path: Path) -> None:
    """fauxcasa-7e5 fix 3: the config is written via a temp sibling + atomic
    os.replace, so after a successful remember only config.json exists — never
    a half-written '.tmp' a concurrent frozen instance could read as torn."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    cache_root = tmp_path / "cr"
    lib = tmp_path / "lib"
    lib.mkdir()
    main._remember_library(cache_root, lib)
    assert sorted(p.name for p in cache_root.iterdir()) == ["config.json"]
    assert main._remembered_library(cache_root) == lib


def test_remember_library_preserves_filetypes_exclusions(tmp_path: Path) -> None:
    """main._remember_library (fauxcasa-ez2.12 finding 1) used to overwrite
    the WHOLE cache-root config.json with just {"library": ...}, wiping
    every library's File Types exclusions that filetypes.save_excluded_exts
    stores in the same file (filetypes.py's preserved-keys contract).
    Saving exclusions and then remembering a library must leave the
    exclusions readable afterward."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import filetypes
    import main

    cache_root = tmp_path / "cr"
    lib = tmp_path / "lib"
    lib.mkdir()

    excluded = {".bmp"}
    assert filetypes.save_excluded_exts(cache_root, lib, excluded)
    assert filetypes.load_excluded_exts(cache_root, lib) == excluded

    main._remember_library(cache_root, lib)

    assert main._remembered_library(cache_root) == lib
    assert filetypes.load_excluded_exts(cache_root, lib) == excluded


def test_remember_library_oserror_is_soft(tmp_path: Path, capsys) -> None:
    """_remember_library (fauxcasa-7e5) is best-effort: an unwritable cache
    root — here its parent is a regular file, so mkdir raises NotADirectoryError
    (an OSError) — must NOT abort the launch. It reports on stderr, returns
    cleanly, and leaves nothing behind."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    cache_root = blocker / "cr"      # mkdir(parents=True) -> NotADirectoryError

    lib = tmp_path / "lib"
    lib.mkdir()
    main._remember_library(cache_root, lib)            # must not raise
    assert "could not remember library choice" in capsys.readouterr().err
    assert not cache_root.exists()


def test_resolve_library_distinguishes_not_a_dir_from_missing(
        monkeypatch, tmp_path: Path, capsys) -> None:
    """_resolve_library (fauxcasa-7e5 fix 4): an explicit path that EXISTS but
    is a regular file gets a clear 'not a folder' message; a path that simply
    isn't there keeps 'library not found'. Both still resolve to None (exit 2),
    but the wording no longer misleads a user who pointed at a file."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    monkeypatch.setattr(main, "FROZEN", False)
    cache_root = tmp_path / "cr"

    missing = tmp_path / "nope"
    assert main._resolve_library(str(missing), cache_root) is None
    assert "library not found" in capsys.readouterr().err

    a_file = tmp_path / "afile.jpg"
    a_file.write_text("not a directory")
    assert main._resolve_library(str(a_file), cache_root) is None
    err = capsys.readouterr().err
    assert "not a folder" in err and "library not found" not in err


def test_prompt_for_library_picker_success(monkeypatch, tmp_path: Path) -> None:
    """_prompt_for_library happy path (fauxcasa-62b): with a GUI available and
    the user choosing a folder, it returns the resolved choice AND remembers it
    for the next no-arg launch. Both headless guards — the env pre-check and the
    offscreen platformName backstop — are bypassed so the picker body runs; a
    cancelled (empty) picker still yields None."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox
    import main

    app = QApplication.instance() or QApplication([])
    assert app is not None

    chosen = tmp_path / "MyPhotos"
    chosen.mkdir()
    cache_root = tmp_path / "cr"
    warnings: list[str] = []

    monkeypatch.setattr(main, "_gui_unavailable", lambda: False)
    monkeypatch.setattr(QApplication, "platformName", lambda self: "xcb")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(chosen)))
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda _parent, _title, msg: warnings.append(msg)),
    )
    # Drive the WelcomeDialog programmatically (never a real exec()) —
    # "Choose a folder…" clicked, exercising the folder-picker fallback
    # path this test is actually about.
    def _fake_welcome_exec(self):
        self.choice = "folder"
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(main.WelcomeDialog, "exec", _fake_welcome_exec)

    got = main._prompt_for_library(cache_root)
    assert got == chosen.resolve()
    # the choice is persisted so the next double-click reopens it
    assert main._remembered_library(cache_root) == chosen.resolve()
    assert warnings == []

    # a cancelled picker (empty string) returns None
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: ""))
    assert main._prompt_for_library(tmp_path / "cr2") is None

    # choosing the filesystem root warns and loops back to the picker; the
    # eventual real folder is what gets persisted.
    choices = iter([str(Path("/")), str(chosen)])
    cache_root_3 = tmp_path / "cr3"
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: next(choices)))
    assert main._prompt_for_library(cache_root_3) == chosen.resolve()
    assert main._remembered_library(cache_root_3) == chosen.resolve()
    assert len(warnings) == 1
    assert "not the filesystem root" in warnings[0]


def test_welcome_dialog_hides_picasa_button_when_zero_watched(
        monkeypatch) -> None:
    """WelcomeDialog (fauxcasa-ez2.14) shows only 'Choose a folder…' + Cancel
    when the registry read returns 0 existing watched folders — the
    Picasa-import button is a dead end otherwise and must not appear."""
    _offscreen_app()
    import main

    monkeypatch.setattr(main, "_existing_picasa_watched_count", lambda: 0)
    dlg = main.WelcomeDialog(main._existing_picasa_watched_count())
    assert dlg.picasa_button is None
    assert dlg.folder_button is not None
    assert dlg.windowTitle() == main.APP_NAME


def test_welcome_dialog_shows_picasa_button_with_count(monkeypatch) -> None:
    """With N >= 1 existing watched folders, the button text carries the
    count, and clicking it sets .choice = 'picasa' + accepts (drives the
    dialog programmatically, never a real exec(), per ez2.14's test note)."""
    _offscreen_app()
    from PySide6.QtWidgets import QDialog
    import main

    dlg = main.WelcomeDialog(3)
    assert dlg.picasa_button is not None
    assert dlg.picasa_button.text() == "Use Picasa's watched folders (3 found)"

    dlg.picasa_button.click()
    assert dlg.choice == "picasa"
    assert dlg.result() == QDialog.DialogCode.Accepted


def test_welcome_dialog_choose_folder_sets_choice() -> None:
    """Clicking 'Choose a folder…' sets .choice = 'folder' + accepts."""
    _offscreen_app()
    from PySide6.QtWidgets import QDialog
    import main

    dlg = main.WelcomeDialog(0)
    dlg.folder_button.click()
    assert dlg.choice == "folder"
    assert dlg.result() == QDialog.DialogCode.Accepted


def test_welcome_dialog_cancel_exits_as_today(monkeypatch, tmp_path: Path) -> None:
    """Cancel in the WelcomeDialog still yields None from _prompt_for_library
    (ez2.14 preserves the existing cancel contract)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QDialog
    import main

    app = QApplication.instance() or QApplication([])
    assert app is not None
    monkeypatch.setattr(main, "_gui_unavailable", lambda: False)
    monkeypatch.setattr(QApplication, "platformName", lambda self: "xcb")

    def _fake_cancel_exec(self):
        self.reject()
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(main.WelcomeDialog, "exec", _fake_cancel_exec)

    assert main._prompt_for_library(tmp_path / "cr") is None


def test_existing_picasa_watched_count_filters_missing_dirs(
        monkeypatch, tmp_path: Path) -> None:
    """_existing_picasa_watched_count (ez2.14) counts only registry entries
    that still exist as directories on disk, and fails soft to 0 when the
    registry read raises (no key, non-Windows, empty value)."""
    import main
    import library

    real = tmp_path / "RealFolder"
    real.mkdir()
    missing = tmp_path / "GoneFolder"

    monkeypatch.setattr(library, "picasa_watched_from_registry",
                        lambda: [real, missing])
    assert main._existing_picasa_watched_count() == 1

    def _raise():
        raise RuntimeError("no registry entry")
    monkeypatch.setattr(library, "picasa_watched_from_registry", _raise)
    assert main._existing_picasa_watched_count() == 0


def test_import_picasa_watched_for_welcome_creates_multiroot_home(
        monkeypatch, tmp_path: Path) -> None:
    """The WelcomeDialog's 'picasa' choice (fauxcasa-ez2.14) runs the same
    import_picasa_watched path --import-picasa-watched registry uses,
    entirely inside cache_root (never touching the watched folders
    themselves), and returns the new library-home for _prompt_for_library
    to remember and open."""
    import main
    import library

    root_a = tmp_path / "Watched" / "A"
    root_b = tmp_path / "Watched" / "B"
    make_jpeg(root_a / "a.jpg")
    make_jpeg(root_b / "b.jpg")
    cache_root = tmp_path / "cr"

    monkeypatch.setattr(library, "picasa_watched_from_registry",
                        lambda: [root_a, root_b])

    home = main._import_picasa_watched_for_welcome(cache_root)
    assert home is not None
    assert home == (cache_root / "picasa-watched-library").resolve()
    cfg = library.resolve_open_path(home)
    assert not cfg.is_legacy
    assert len(cfg.roots) == 2
    assert {r.path.resolve() for r in cfg.roots} == {
        root_a.resolve(), root_b.resolve()}
    # Neither watched folder itself was touched — only cache_root grew.
    assert not (root_a / ".fauxcasa").exists()
    assert not (root_b / ".fauxcasa").exists()


def test_import_picasa_watched_for_welcome_no_usable_folders_returns_none(
        monkeypatch, tmp_path: Path) -> None:
    """A registry list with nothing usable (all missing/nested) fails soft
    to None — the WelcomeDialog's caller falls back to the folder picker,
    it never crashes first-run."""
    import main
    import library

    monkeypatch.setattr(library, "picasa_watched_from_registry",
                        lambda: [tmp_path / "does-not-exist"])
    assert main._import_picasa_watched_for_welcome(tmp_path / "cr") is None


def test_prompt_for_library_picasa_choice_end_to_end(
        monkeypatch, tmp_path: Path) -> None:
    """_prompt_for_library with the WelcomeDialog's 'picasa' choice (driven
    programmatically) imports the watched folders, remembers the new
    library-home, and returns it — the end-to-end path the welcome button
    triggers in the real app."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QDialog
    import main
    import library

    app = QApplication.instance() or QApplication([])
    assert app is not None
    monkeypatch.setattr(main, "_gui_unavailable", lambda: False)
    monkeypatch.setattr(QApplication, "platformName", lambda self: "xcb")

    watched = tmp_path / "Watched"
    make_jpeg(watched / "p.jpg")
    monkeypatch.setattr(library, "picasa_watched_from_registry",
                        lambda: [watched])

    def _fake_picasa_exec(self):
        self.choice = "picasa"
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(main.WelcomeDialog, "exec", _fake_picasa_exec)

    cache_root = tmp_path / "cr"
    got = main._prompt_for_library(cache_root)
    expected_home = (cache_root / "picasa-watched-library").resolve()
    assert got == expected_home
    assert main._remembered_library(cache_root) == expected_home


def test_mainwindow_open_action_relaunches_with_selected_library(
        monkeypatch, tmp_path: Path) -> None:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication, QFileDialog
    import main

    app = QApplication.instance() or QApplication([])
    assert app is not None

    current = tmp_path / "Current"
    chosen = tmp_path / "Chosen"
    make_jpeg(current / "a.jpg")
    make_jpeg(chosen / "b.jpg")
    cache_root = tmp_path / "cr"
    cat = scan_library(current)
    win = main.MainWindow(cat, None, cache_root / "old-cache", None,
                          cache_root=cache_root)

    started: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(chosen)))
    monkeypatch.setattr(
        QProcess,
        "startDetached",
        staticmethod(lambda program, args: started.append((program, args))
                     or True),
    )
    monkeypatch.setattr(main, "_restart_command",
                        lambda root, cr, **_kw: ("prog", [str(root), str(cr)]))

    win._change_library()

    assert started == [("prog", [str(chosen.resolve()), str(cache_root)])]
    assert main._remembered_library(cache_root) == chosen.resolve()


def test_mainwindow_open_action_warns_when_relaunch_fails(
        monkeypatch, tmp_path: Path) -> None:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
    import main

    app = QApplication.instance() or QApplication([])
    assert app is not None

    current = tmp_path / "Current"
    chosen = tmp_path / "Chosen"
    make_jpeg(current / "a.jpg")
    make_jpeg(chosen / "b.jpg")
    cache_root = tmp_path / "cr"
    cat = scan_library(current)
    win = main.MainWindow(cat, None, cache_root / "old-cache", None,
                          cache_root=cache_root)

    warnings: list[str] = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(chosen)))
    monkeypatch.setattr(
        QProcess,
        "startDetached",
        staticmethod(lambda _program, _args: (False, 0)),
    )
    monkeypatch.setattr(main, "_restart_command",
                        lambda root, cr, **_kw: ("prog", [str(root), str(cr)]))
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda _parent, _title, msg: warnings.append(msg)),
    )

    win._change_library()

    assert warnings == [f"Could not open the selected folder:\n"
                        f"{chosen.resolve()}"]
    assert main._remembered_library(cache_root) is None


def test_main_bad_library_exits_2(tmp_path: Path) -> None:
    """End-to-end main() via subprocess (fauxcasa-62b): an explicit but
    nonexistent library exits 2 with a friendly message and NO traceback.
    Running the real process WITHOUT --cache-root also exercises the
    cache-root defaulting branch and the argparse wiring that the unit-level
    _resolve_library tests skip."""
    import os
    import subprocess
    main_py = APP_DIR / "main.py"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    missing = tmp_path / "no-such-library-here"
    proc = subprocess.run([sys.executable, str(main_py), str(missing)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert "library not found" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_image_size_arg_parser() -> None:
    import argparse
    import main

    assert main._parse_image_size_arg("100x200") == (100, 200)
    assert main._parse_image_size_arg("100X200") == (100, 200)
    assert main._parse_image_size_arg("100,200") == (100, 200)
    with pytest.raises(argparse.ArgumentTypeError):
        main._parse_image_size_arg("100")
    with pytest.raises(argparse.ArgumentTypeError):
        main._parse_image_size_arg("0x100")


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="exercises the Linux no-DISPLAY pre-construction guard")
def test_frozen_noarg_headless_exits_2_without_aborting(tmp_path: Path) -> None:
    """Regression for fauxcasa-7e5 fix 1 (fauxcasa-62b): a FROZEN no-arg launch
    on Linux with DISPLAY, WAYLAND_DISPLAY and QT_QPA_PLATFORM ALL UNSET must
    reach the friendly 'no library selected' exit 2 — NOT abort (exit 134)
    inside QApplication([]) under the default xcb plugin. The pre-construction
    _gui_unavailable guard returns before any QApplication is built. This is
    the unit-speed twin of the frozen-bundle CI leg in bundle.yml."""
    import os
    import subprocess
    tracer_dir = APP_DIR
    cache_root = tmp_path / "cr"
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(tracer_dir)!r})\n"
        "import main\n"
        "main.FROZEN = True\n"            # simulate a PyInstaller bundle
        f"sys.argv = ['fauxcasa-tracer', '--cache-root', {str(cache_root)!r}]\n"
        "sys.exit(main.main())\n"
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM")}
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert "no library selected" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_main_reuses_existing_qapplication(
        monkeypatch, library: Path, tmp_path: Path) -> None:
    """main() must consume a QApplication that already exists in the process —
    as a frozen first-run picker leaves behind (main.py: `QApplication.instance()
    or QApplication([])`) — rather than construct a second one, which Qt forbids.
    Drive a full, self-quitting run on a tiny library and confirm the very same
    app instance carried through and the run succeeded (exit 0)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import main

    app = QApplication.instance() or QApplication([])   # pre-existing app
    cache_root = tmp_path / "cr"
    monkeypatch.setattr(sys, "argv", [
        "fauxcasa-tracer", str(library), "--cache-root", str(cache_root),
        "--quit-after-ready", "--finish-build", "--timeout", "30"])
    rc = main.main()
    assert rc == 0
    assert QApplication.instance() is app               # reused, never recreated


def test_pcts_nearest_rank() -> None:
    """bench_scroll.pcts uses a nearest-rank LOWER index so a sub-1.0
    quantile never overshoots to the max (the documented int(n*q)==n trap
    that would report p100 as p99 at n=100)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import bench_scroll as bs

    z = bs.pcts([])
    assert z["n"] == 0 and z["p99"] == 0.0 and z["max"] == 0.0

    r = bs.pcts([float(i) for i in range(1, 101)])  # 1..100, already sorted
    assert r["n"] == 100
    assert r["max"] == 100.0           # s[-1]
    assert r["min"] == 1.0             # s[0]
    assert r["p50"] == 50.0            # nearest-rank lower index
    assert r["p99"] < 100.0            # the trap: must NOT collapse onto max


# ---------- bench_scroll: occlusion_clean platform gate (fauxcasa-ed5.10) ---


def test_occlusion_clean_timeout_frames_ignored_on_windows() -> None:
    """On Windows a paint-bound run produces ~100 ms intervals that alias with
    the Wayland frame-callback-timeout signature.  timeout_frames must NOT
    disqualify occlusion_clean on win32."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import bench_scroll as bs

    # Simulate: visible, no fill stalls, but 25 timeout-band intervals.
    assert bs._occlusion_clean(0, 0, 25, platform="win32") is True
    assert bs._occlusion_clean(0, 0, 25, platform="cygwin") is True


def test_occlusion_clean_timeout_frames_disqualify_on_linux() -> None:
    """On Linux the ~100 ms cluster is the Wayland compositor occlusion
    signature; timeout_frames > 0 must disqualify occlusion_clean."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import bench_scroll as bs

    assert bs._occlusion_clean(0, 0, 1, platform="linux") is False
    assert bs._occlusion_clean(0, 0, 25, platform="linux2") is False


def test_occlusion_clean_other_tells_still_apply_on_all_platforms() -> None:
    """not_visible_ticks and fill_timeouts disqualify occlusion_clean
    regardless of platform."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import bench_scroll as bs

    for plat in ("win32", "linux", "darwin"):
        assert bs._occlusion_clean(1, 0, 0, platform=plat) is False  # not_visible
        assert bs._occlusion_clean(0, 1, 0, platform=plat) is False  # fill_timeout
        assert bs._occlusion_clean(0, 0, 0, platform=plat) is True   # all clean


# ---------- diagnostics: log file survives console=False (fauxcasa-pqw) ----


def test_applog_writes_logfile_and_mirrors_stderr(tmp_path: Path, capsys) -> None:
    """applog.setup() returns a log path and fans a record out to BOTH the
    rotating log file (the only survivor in a console=False windowed build)
    and the per-emit stderr mirror (so a console — and pytest's capsys —
    still sees it). The file format carries the level; the stderr mirror is
    bare, matching the app's old print() UX."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import applog

    log_path = applog.setup(tmp_path / "cr", "test-slug")
    assert log_path == tmp_path / "cr" / "test-slug.log"

    applog.log.warning("marker-7f3 happened")
    assert "marker-7f3 happened" in capsys.readouterr().err   # stderr mirror
    text = log_path.read_text()
    assert "marker-7f3 happened" in text and "WARNING" in text  # file + level


def test_applog_stderr_mirror_noops_when_stream_is_none(
        monkeypatch, tmp_path: Path) -> None:
    """A windowed PyInstaller build has sys.stderr == None; the mirror must
    skip silently rather than raise (which would turn a benign warning into a
    crash). The log file still records it."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import applog

    log_path = applog.setup(tmp_path / "cr")
    monkeypatch.setattr(sys, "stderr", None)
    applog.log.error("survives-none-stderr")        # must not raise
    monkeypatch.undo()
    assert "survives-none-stderr" in log_path.read_text()


def test_applog_excepthook_logs_traceback(tmp_path: Path) -> None:
    """The installed sys.excepthook routes an uncaught exception's full
    traceback to the log file — the only record of a crash when there is no
    console. Headless, _show_fatal_dialog is a no-op (offscreen guard), so
    nothing blocks."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import applog

    log_path = applog.setup(tmp_path / "cr")
    try:
        raise ValueError("boom-marker-c2")
    except ValueError:
        sys.excepthook(*sys.exc_info())          # invoke the installed hook
    text = log_path.read_text()
    assert "boom-marker-c2" in text
    assert "ValueError" in text and "Traceback" in text


def test_main_run_logs_and_keeps_stdout_protocol(
        library: Path, tmp_path: Path) -> None:
    """End-to-end via subprocess: a real run writes the always-on startup
    status line to the log file (so a console=False build keeps a diagnostic
    record) WHILE the §7 machine protocol (READY + the ready JSON) stays on
    real stdout and is NOT diverted into the log. Pins both halves of
    fauxcasa-pqw at once."""
    import os
    import subprocess
    main_py = APP_DIR / "main.py"
    cache_root = tmp_path / "cr"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run(
        [sys.executable, str(main_py), str(library),
         "--cache-root", str(cache_root),
         "--quit-after-ready", "--finish-build", "--timeout", "30"],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 0, (proc.returncode, proc.stderr)

    # §7 machine protocol: on stdout, unchanged.
    assert "READY" in proc.stdout
    assert '"event": "ready"' in proc.stdout
    ready_line = next(
        line for line in proc.stdout.splitlines()
        if '"event": "ready"' in line)
    ready_json = json.loads(ready_line)
    assert ready_json["version"] == "0.1.0"   # rel-0.1 identity (__version__)

    # Human diagnostics: in the log file beside the per-library caches.
    log_path = cache_root / "fauxcasa.log"
    assert log_path.is_file()
    log_text = log_path.read_text()
    assert "photos," in log_text and "folders," in log_text   # startup line
    # The machine protocol must NOT have been rerouted into the log.
    assert "READY" not in log_text and '"event": "ready"' not in log_text
