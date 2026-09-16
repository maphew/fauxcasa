"""Tests for main.py multi-root sidebar UI.

Split from test_tracer.py (fauxcasa-l09); originally lines 17394-17749 of the monolith."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
import pytest
import library as libmod
import thumbcache
from tracer_helpers import (
    APP_DIR,
    _multiroot_ui_catalog,
    _raw_catalog,
    _raw_photo_rows,
    make_jpeg,
)


# ---- multiroot sidebar/cache UI (fauxcasa-ed5.7.7, bead .g) ---------------


def test_multiroot_sidebar_and_grid_use_root_qualified_folder_identity(
        tmp_path: Path) -> None:
    """Duplicate folder rels render under distinct manifest-ordered roots,
    and the grid/sort/scroll key is the same catalog-qualified identity."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None
    cat = _multiroot_ui_catalog(tmp_path)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    folders = next(win.tree.topLevelItem(i)
                   for i in range(win.tree.topLevelItemCount())
                   if win.tree.topLevelItem(i).text(0) == "Folders")
    assert [folders.child(i).text(0) for i in range(folders.childCount())] == \
        ["Primary", "Archive"]
    assert folders.child(0).child(0).text(0) == "A 2019  (1)"
    assert folders.child(1).child(0).text(0) == "B 2019  (1)"
    assert folders.child(0).child(0).data(0, Qt.ItemDataRole.UserRole) == \
        ("folder", "2019")
    assert folders.child(1).child(0).data(0, Qt.ItemDataRole.UserRole) == \
        ("folder", "bbbbbbbb/2019")
    assert [g.folder for g in win.grid.groups] == \
        ["2019", "bbbbbbbb/2019"]
    assert [g.title for g in win.grid.groups] == ["A 2019", "B 2019"]

    # No hidden duplicate folder item elsewhere in the tree.
    data = []
    it = QTreeWidgetItemIterator(win.tree)
    while it.value():
        value = it.value().data(0, Qt.ItemDataRole.UserRole)
        if value and value[0] == "folder":
            data.append(value)
        it += 1
    assert data == [("folder", "2019"),
                    ("folder", "bbbbbbbb/2019")]


def test_single_root_sidebar_and_identity_are_exact_passthrough(
        tmp_path: Path) -> None:
    """A promoted one-root catalog gains no visual root level and keeps the
    historical bare folder/tray keys despite its non-empty root id."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow
    from catalog import Catalog, Folder, Photo

    app = QApplication.instance() or QApplication([])
    assert app is not None
    root = tmp_path / "root"
    root.mkdir()
    photo = Photo(rel="2019/x.jpg", folder="2019", name="x.jpg",
                  root_id="aaaaaaaa")
    cat = Catalog(
        root=root, photos=[photo],
        folders={"2019": Folder(rel="2019", title="2019", photo_count=1,
                                total_count=1, root_id="aaaaaaaa")},
        albums={}, roots=[libmod.LibraryRoot(id="aaaaaaaa", path=root,
                                             label="Must not appear")],
        library_id=libmod.mint_library_id())
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    folders = next(win.tree.topLevelItem(i)
                   for i in range(win.tree.topLevelItemCount())
                   if win.tree.topLevelItem(i).text(0) == "Folders")
    assert folders.childCount() == 1
    assert folders.child(0).text(0) == "2019  (1)"
    assert win.grid.groups[0].folder == "2019"
    assert win.tray.photo_key(photo) == "2019/x.jpg"


def test_multiroot_tray_and_composite_cache_disambiguate_duplicate_rels(
        tmp_path: Path) -> None:
    """Held identity and cache lookup both retain root ownership; catalog
    order maps to each root cache's independent local entry order."""
    from grid import CompositeThumbCache
    from tray import SelectionTray

    cat = _multiroot_ui_catalog(tmp_path)
    tray = SelectionTray(cat, None)
    keys = [tray.photo_key(p) for p in cat.photos]
    assert keys == [("aaaaaaaa", "2019/same.jpg"),
                    ("bbbbbbbb", "2019/same.jpg")]
    assert tray.hold(keys) == 2
    assert tray.held_indices() == [0, 1]

    class FakeCache:
        def __init__(self, path: Path, levels=(256,)):
            self.path = path
            self.calls = []
            self.levels = list(levels)

        def best_level(self, _edge):
            return 256

        def entry(self, idx, level=None):
            self.calls.append((idx, level))
            return (idx * 100, 10, 64, 48)

    cache_a = FakeCache(tmp_path / "thumbs-aaaaaaaa.fcache")
    cache_b = FakeCache(tmp_path / "thumbs-bbbbbbbb.fcache")
    composite = CompositeThumbCache(
        cat, {"aaaaaaaa": cache_a, "bbbbbbbb": cache_b})
    assert composite.count == 2
    assert composite.entry(0, 256) == (0, 10, 64, 48)
    assert composite.path == cache_a.path
    assert composite.entry(1, 256) == (0, 10, 64, 48)
    assert composite.path == cache_b.path
    assert cache_a.calls == [(0, 256)] and cache_b.calls == [(0, 256)]


def test_composite_cache_rejects_mismatched_root_levels(
        tmp_path: Path) -> None:
    """A v1 single-level cache alongside a multi-level cache would IndexError
    once a photo routed through the smaller root's shorter levels list —
    __init__ rejects the mismatch up front instead, naming the offending
    root."""
    from grid import CompositeThumbCache

    cat = _multiroot_ui_catalog(tmp_path)

    class FakeCache:
        def __init__(self, path: Path, levels):
            self.path = path
            self.levels = list(levels)

        def best_level(self, _edge):
            return 0

        def entry(self, idx, level=None):
            return (idx * 100, 10, 64, 48)

    cache_a = FakeCache(tmp_path / "thumbs-aaaaaaaa.fcache", [256])
    cache_b = FakeCache(tmp_path / "thumbs-bbbbbbbb.fcache", [512, 256, 128])
    with pytest.raises(ValueError, match="bbbbbbbb"):
        CompositeThumbCache(cat, {"aaaaaaaa": cache_a, "bbbbbbbb": cache_b})


def test_composite_cache_local_index_increments_per_root(
        tmp_path: Path) -> None:
    """A root's SECOND photo must get local cache index 1, not 0 — a catalog
    with exactly one photo per root (as in the disambiguation test above)
    can't exercise the `next_local[root_id] = local + 1` increment, since
    every local index there is 0."""
    from catalog import Catalog, Folder, Photo
    from grid import CompositeThumbCache

    root_a, root_b = tmp_path / "root-a", tmp_path / "root-b"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)
    id_a, id_b = "aaaaaaaa", "bbbbbbbb"
    photos = [
        Photo(rel="2019/one.jpg", folder="2019", name="one.jpg",
              root_id=id_a),
        Photo(rel="2019/same.jpg", folder="2019", name="same.jpg",
              root_id=id_b),
        Photo(rel="2019/two.jpg", folder="2019", name="two.jpg",
              root_id=id_a),
    ]
    folders = {
        "2019": Folder(rel="2019", title="A 2019", photo_count=2,
                       total_count=2, root_id=id_a),
        f"{id_b}/2019": Folder(rel="2019", title="B 2019", photo_count=1,
                               total_count=1, root_id=id_b),
    }
    roots = [libmod.LibraryRoot(id=id_a, path=root_a, label="Primary"),
             libmod.LibraryRoot(id=id_b, path=root_b, label="Archive")]
    cat = Catalog(root=root_a, photos=photos, folders=folders, albums={},
                  roots=roots, library_id=libmod.mint_library_id())

    class FakeCache:
        def __init__(self, path: Path):
            self.path = path
            self.calls = []
            self.levels = [256]

        def best_level(self, _edge):
            return 256

        def entry(self, idx, level=None):
            self.calls.append((idx, level))
            return (idx * 100, 10, 64, 48)

    cache_a = FakeCache(tmp_path / "thumbs-aaaaaaaa.fcache")
    cache_b = FakeCache(tmp_path / "thumbs-bbbbbbbb.fcache")
    composite = CompositeThumbCache(
        cat, {"aaaaaaaa": cache_a, "bbbbbbbb": cache_b})
    assert composite.count == 3
    assert composite.entry(0, 256) == (0, 10, 64, 48)  # rootA local index 0
    # Photo 2 (global index 2) is rootA's SECOND photo -> local index 1.
    assert composite.entry(2, 256) == (100, 10, 64, 48)
    assert cache_a.calls == [(0, 256), (1, 256)]
    assert cache_b.calls == []


def test_multiroot_search_index_uses_root_qualified_folder_key(
        tmp_path: Path) -> None:
    """_rebuild_search_index must route each photo's folder haystack through
    folder_key(cat, p.root_id, p.folder) — a regression to bare p.folder
    would silently give later roots' photos the FIRST root's folder title
    (both rels are "2019" in the fixture, but the titles differ)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None
    cat = _multiroot_ui_catalog(tmp_path)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    assert len(win._search_pairs) == len(cat.photos) == 2
    hay_a = win._search_pairs[0][1]
    hay_b = win._search_pairs[1][1]
    assert "a 2019" in hay_a and "b 2019" not in hay_a
    assert "b 2019" in hay_b and "a 2019" not in hay_b


def test_main_two_root_cold_build_then_warm_open(tmp_path: Path) -> None:
    """Real subprocess startup scans both roots, builds/binds one cache per
    root, then reopens the same explicit library through the warm path."""
    import subprocess

    root_a, root_b = tmp_path / "a", tmp_path / "b"
    make_jpeg(root_a / "same.jpg")
    make_jpeg(root_b / "same.jpg")
    home = tmp_path / "home"
    home.mkdir()
    cfg = libmod.LibraryConfig(
        library_id=libmod.mint_library_id(), name="Two roots",
        roots=[libmod.LibraryRoot(id="aaaaaaaa", path=root_a, label="A"),
               libmod.LibraryRoot(id="bbbbbbbb", path=root_b, label="B")],
        home=home)
    libmod.save_library(cfg)
    cache_root = tmp_path / "cache"
    main_py = APP_DIR / "main.py"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")

    def run() -> tuple[dict, subprocess.CompletedProcess]:
        proc = subprocess.run(
            [sys.executable, str(main_py), str(home),
             "--cache-root", str(cache_root), "--quit-after-ready",
             "--finish-build", "--timeout", "60"],
            capture_output=True, text=True, env=env, timeout=90)
        events = [json.loads(line) for line in proc.stdout.splitlines()
                  if line.startswith("{")]
        ready = next((e for e in events if e.get("event") == "ready"), None)
        assert ready is not None, (proc.returncode, proc.stdout, proc.stderr)
        return ready, proc

    cold, proc = run()
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert cold["photos"] == 2 and not cold["warm"]
    cache_dir = thumbcache.cache_dir_for(cfg.library_id, cache_root)
    assert (cache_dir / "thumbs-aaaaaaaa.fcache").is_file()
    assert (cache_dir / "thumbs-bbbbbbbb.fcache").is_file()

    warm, proc = run()
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert warm["photos"] == 2 and warm["warm"]


def test_main_missing_cache_keeps_offline_root_slice(tmp_path: Path) -> None:
    """A missing per-root fcache must not discard the persisted catalog
    (PR #81 review finding): an offline root walks as empty, so falling
    back to a cold rescan would persist a catalog without that root's
    photos. With root B offline and root A's cache deleted, the open must
    keep both catalog slices, re-index only A, and leave B's on-disk
    fcache byte-identical (never overwritten with error blobs)."""
    import subprocess

    root_a, root_b = tmp_path / "a", tmp_path / "b"
    make_jpeg(root_a / "one.jpg")
    make_jpeg(root_b / "two.jpg")
    home = tmp_path / "home"
    home.mkdir()
    cfg = libmod.LibraryConfig(
        library_id=libmod.mint_library_id(), name="Two roots",
        roots=[libmod.LibraryRoot(id="aaaaaaaa", path=root_a, label="A"),
               libmod.LibraryRoot(id="bbbbbbbb", path=root_b, label="B")],
        home=home)
    libmod.save_library(cfg)
    cache_root = tmp_path / "cache"
    main_py = APP_DIR / "main.py"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")

    def run() -> tuple[dict, subprocess.CompletedProcess]:
        proc = subprocess.run(
            [sys.executable, str(main_py), str(home),
             "--cache-root", str(cache_root), "--quit-after-ready",
             "--finish-build", "--timeout", "60"],
            capture_output=True, text=True, env=env, timeout=90)
        events = [json.loads(line) for line in proc.stdout.splitlines()
                  if line.startswith("{")]
        ready = next((e for e in events if e.get("event") == "ready"), None)
        assert ready is not None, (proc.returncode, proc.stdout, proc.stderr)
        return ready, proc

    cold, proc = run()
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert cold["photos"] == 2 and not cold["warm"]
    cache_dir = thumbcache.cache_dir_for(cfg.library_id, cache_root)
    a_cache = cache_dir / "thumbs-aaaaaaaa.fcache"
    b_cache = cache_dir / "thumbs-bbbbbbbb.fcache"
    b_bytes = b_cache.read_bytes()

    a_cache.unlink()                      # incomplete caches ...
    root_b.rename(tmp_path / "b-moved")   # ... while root B is offline

    again, proc = run()
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert again["photos"] == 2 and not again["warm"]   # slice preserved
    assert a_cache.is_file()                            # A re-indexed
    assert b_cache.read_bytes() == b_bytes              # B never rewritten
    cat = _raw_catalog(cache_dir / "catalog.json")
    assert len(_raw_photo_rows(cat)) == 2

    # Same open with B's cache also gone: the composite serves an
    # all-error placeholder for B — the catalog still keeps B's photos
    # and the build must not fabricate an fcache for an offline root.
    b_cache.unlink()
    third, proc = run()
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert third["photos"] == 2 and not third["warm"]
    assert not b_cache.exists()
    cat = _raw_catalog(cache_dir / "catalog.json")
    assert len(_raw_photo_rows(cat)) == 2
