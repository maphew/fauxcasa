#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "PySide6"]
# ///
"""Tests for the tracer's non-GUI layers: catalog scan + metadata,
thumbnail-cache build/load/bind, and walk-rule parity with
scripts/make-thumbcache.py. GUI behavior is exercised separately via
the app's --screenshot harness."""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import thumbcache
from catalog import (
    load_catalog,
    reconcile_walk,
    save_catalog,
    scan_library,
    walk_library,
)

REPO = Path(__file__).resolve().parent.parent


def make_jpeg(path: Path, w: int = 64, h: int = 48) -> None:
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(120, 160, 200))
    path.parent.mkdir(parents=True, exist_ok=True)
    assert img.save(str(path), "JPEG", 85)


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    make_jpeg(root / "2020-01-01 Trip" / "a.jpg")
    make_jpeg(root / "2020-01-01 Trip" / "b.jpg", 48, 64)
    make_jpeg(root / "2020-01-01 Trip" / ".picasaoriginals" / "a.jpg")
    make_jpeg(root / "2021-05-05 Picnic" / "c.jpg")
    (root / "2020-01-01 Trip" / ".picasa.ini").write_text(
        "[Picasa]\r\nname=Trip!\r\ndescription=fun\r\n"
        "[a.jpg]\r\nstar=yes\r\ncaption=the beach\r\n"
        "keywords=sun, sand\r\nrotate=rotate(1)\r\n"
        "albums=deadbeefdeadbeefdeadbeefdeadbeef\r\n"
        "[.album:deadbeefdeadbeefdeadbeefdeadbeef]\r\n"
        "name=Best Of\r\ntoken=deadbeefdeadbeefdeadbeefdeadbeef\r\n"
        "[b.jpg]\r\nhidden=yes\r\n"
    )
    return root


def test_scan_metadata(library: Path) -> None:
    cat = scan_library(library)
    # walk order is sorted rel path: .picasaoriginals/a, a, b, c
    rels = [p.rel for p in cat.photos]
    assert rels == sorted(rels)
    assert len(cat.photos) == 4

    a = next(p for p in cat.photos if p.rel.endswith("Trip/a.jpg"))
    assert a.star and a.caption == "the beach"
    assert a.keywords == ("sun", "sand")
    assert a.rotate == 1
    assert a.albums == ("deadbeefdeadbeefdeadbeefdeadbeef",)
    assert a.visible

    b = next(p for p in cat.photos if p.rel.endswith("b.jpg"))
    assert b.hidden and not b.visible

    stashed = next(p for p in cat.photos if ".picasaoriginals" in p.rel)
    assert not stashed.visible

    assert cat.visible_count == 2  # a.jpg + c.jpg
    # folder title is the ON-DISK name (N1) — ini name= goes stale
    assert cat.folders["2020-01-01 Trip"].title == "2020-01-01 Trip"
    assert cat.folders["2020-01-01 Trip"].description == "fun"
    assert cat.folders["2020-01-01 Trip"].photo_count == 1

    album = cat.albums["deadbeefdeadbeefdeadbeefdeadbeef"]
    assert album.name == "Best Of"
    assert album.members == [rels.index("2020-01-01 Trip/a.jpg")]


def test_walk_rule_parity_with_make_thumbcache(library: Path) -> None:
    """catalog order must equal make-thumbcache entry order or caches
    stop binding — compare against the script's own walk."""
    import catalog
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mtc", REPO / "scripts" / "make-thumbcache.py")
    mtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtc)
    assert mtc.EXTS == catalog.EXTS  # the usual drift vector
    script_walk = sorted(
        p for p in library.rglob("*")
        if p.suffix.lower() in mtc.EXTS and p.is_file()
    )
    assert walk_library(library) == script_walk


def test_component_sort_order(tmp_path: Path) -> None:
    """The entry-order rule is path-COMPONENT order: '2020' sorts before
    '2020-01 Trip' even though the joined strings sort the other way
    ('-' < '/'). The shipped benchmark cache uses this order."""
    root = tmp_path / "lib"
    make_jpeg(root / "2020" / "x.jpg")
    make_jpeg(root / "2020-01 Trip" / "x.jpg")
    rels = [p.rel for p in scan_library(root).photos]
    assert rels == ["2020/x.jpg", "2020-01 Trip/x.jpg"]
    assert rels != sorted(rels)  # string sort would invert them


def test_rel_paths_match_relative_to(tmp_path: Path) -> None:
    """rel_paths() must equal relative_to().as_posix() exactly —
    including for true roots ('/', 'D:\\', UNC shares), whose str()
    keeps a trailing separator that breaks naive prefix slicing."""
    from pathlib import PurePosixPath, PureWindowsPath

    from catalog import rel_paths

    # normal nested root (fast path)
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    files = [root / "f" / "a.jpg"]
    assert rel_paths(root, files) == ["f/a.jpg"]

    # filesystem root: trailing-separator prefix
    proot = PurePosixPath("/")
    pfiles = [PurePosixPath("/a/b.jpg"), PurePosixPath("/c.jpg")]
    assert rel_paths(proot, pfiles) == ["a/b.jpg", "c.jpg"]

    # Windows drive root: native separator differs from POSIX (on a
    # POSIX host this also exercises the parity-probe fallback)
    wroot = PureWindowsPath("P:/")
    wfiles = [PureWindowsPath("P:/photos/a.jpg")]
    assert rel_paths(wroot, wfiles) == ["photos/a.jpg"]

    # UNC share root
    uroot = PureWindowsPath("//nas/photos")
    ufiles = [PureWindowsPath("//nas/photos/DCIM/x.jpg")]
    assert rel_paths(uroot, ufiles) == ["DCIM/x.jpg"]


def test_duplicate_sections_and_flag_normalization(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "Originals" / "a.jpg")  # legacy stash
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\ncaption=first\r\n"
        "[A.JPG]\r\nstar=Yes \r\nkeywords=dup\r\n"  # dup section, odd case
    )
    cat = scan_library(root)
    a = next(p for p in cat.photos if p.rel == "f/a.jpg")
    assert a.caption == "first"  # first occurrence wins
    assert a.star  # merged from the duplicate section, value normalized
    assert a.keywords == ("dup",)
    legacy = next(p for p in cat.photos if "Originals" in p.rel)
    assert not legacy.visible


def test_build_fills_signals_and_binds(library: Path, tmp_path: Path) -> None:
    cat = scan_library(library)
    cache_dir = tmp_path / "cache"
    result = thumbcache.build_cache(cat, cache_dir)
    assert result is not None
    assert result.photos == 4 and result.rate > 0  # throughput measured

    cache = thumbcache.load_cache(result.path)
    assert cache.count == 4
    thumbcache.bind(cache, cat)  # must not raise

    # the indexer filled identity + staleness signals into the catalog
    for p in cat.photos:
        assert p.size >= 0 and p.mtime >= 0
        assert p.sha256 is not None and len(p.sha256) == 64

    # blobs are real JPEGs with recorded dims
    with open(result.path, "rb") as f:
        for off, length, w, h in cache.entries:
            assert length > 0 and w > 0 and h > 0
            f.seek(off)
            assert f.read(3) == b"\xff\xd8\xff"

    # library drift => bind refuses an out-of-date fcache
    make_jpeg(library / "2021-05-05 Picnic" / "d.jpg")
    cat2 = scan_library(library)
    with pytest.raises(thumbcache.CacheError):
        thumbcache.bind(cache, cat2)


def test_persistent_catalog_roundtrip(library: Path, tmp_path: Path) -> None:
    """A loaded catalog is indistinguishable from a freshly walked one,
    so a warm start can skip the walk."""
    cat = scan_library(library)
    thumbcache.build_cache(cat, tmp_path / "c")  # fills signals
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    loaded = load_catalog(path, library)
    assert loaded is not None
    assert [p.rel for p in loaded.photos] == [p.rel for p in cat.photos]
    assert loaded.visible_count == cat.visible_count
    assert set(loaded.folders) == set(cat.folders)
    assert set(loaded.albums) == set(cat.albums)
    a = next(p for p in loaded.photos if p.rel.endswith("Trip/a.jpg"))
    assert a.star and a.caption == "the beach" and a.rotate == 1
    assert a.sha256 is not None  # signals survive the round trip
    # derived fields recomputed, not stored
    assert a.folder == "2020-01-01 Trip" and a.visible
    b = next(p for p in loaded.photos if p.rel.endswith("b.jpg"))
    assert b.hidden and not b.visible
    # folder description persisted; title derived from the on-disk name
    assert loaded.folders["2020-01-01 Trip"].description == "fun"
    assert loaded.folders["2020-01-01 Trip"].title == "2020-01-01 Trip"
    assert loaded.albums["deadbeefdeadbeefdeadbeefdeadbeef"].name == "Best Of"


def test_load_catalog_rejects_foreign_format(tmp_path: Path) -> None:
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps({"library": "x", "files": []}))  # old signals-only
    assert load_catalog(p, tmp_path) is None
    p.write_text(json.dumps([1, 2, 3]))  # not even an object
    assert load_catalog(p, tmp_path) is None
    p.write_text("{ not json")
    assert load_catalog(p, tmp_path) is None
    assert load_catalog(tmp_path / "missing.json", tmp_path) is None


def test_reconcile_detects_drift(library: Path, tmp_path: Path) -> None:
    cat = scan_library(library)
    thumbcache.build_cache(cat, tmp_path / "c")  # fills size/mtime signals

    assert not reconcile_walk(cat, library).changed  # nothing changed yet

    make_jpeg(library / "2021-05-05 Picnic" / "new.jpg")  # add one
    (library / "2020-01-01 Trip" / "a.jpg").unlink()       # remove one
    drift = reconcile_walk(cat, library)
    assert drift.changed and drift.added == 1 and drift.removed == 1


def test_reconcile_walk_cancels(library: Path) -> None:
    import threading
    cat = scan_library(library)
    ev = threading.Event()
    ev.set()  # already cancelled
    assert reconcile_walk(cat, library, cancel=ev) is None


def test_set_data_invalidates_tiles() -> None:
    """The reconcile swap goes through grid.set_data; it must invalidate
    the decoded-tile cache or a rebuilt catalog paints stale thumbnails
    keyed by old indices (the major review finding)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from catalog import Catalog, Folder, Photo
    from grid import GridView

    app = QApplication.instance() or QApplication([])
    assert app is not None
    grid = GridView()
    cat = Catalog(root=Path("/x"),
                  photos=[Photo(rel="a.jpg", folder="", name="a.jpg")],
                  folders={"": Folder(rel="", title="x", photo_count=1)},
                  albums={})
    grid.set_data(cat, None)
    # simulate a populated tile cache + an in-flight decode generation
    grid.tiles[0] = [object(), 1, 999]
    grid._cache_bytes = 999
    grid.pending.add(5)
    gen_before = grid.generation
    grid.set_data(cat, None)  # the reconcile-style swap
    assert grid.tiles == {} and grid.pending == set()
    assert grid._cache_bytes == 0  # byte accounting resets with the cache
    assert grid.generation > gen_before  # stale queued decodes are dropped


def test_evict_bounds_by_bytes(monkeypatch) -> None:
    """Decoded-tile eviction is bounded by summed BYTES (oldest-first),
    never drops a tile the current paint wants, and an entry backstop
    bounds zero-byte (error) tiles that exert no byte pressure."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication
    import grid as gridmod
    from grid import GridView

    app = QApplication.instance() or QApplication([])
    assert app is not None
    g = GridView()

    def tile(px: int) -> QImage:
        im = QImage(px, px, QImage.Format.Format_RGB32)
        im.fill(QColor(10, 20, 30))
        return im

    one = tile(64).sizeInBytes()  # 64*64*4 = 16384 B
    assert one > 0

    def fill(n: int) -> None:
        g.tiles.clear()
        g._cache_bytes = 0
        for i in range(n):
            img = tile(64)
            g.tiles[i] = [img, i, img.sizeInBytes()]  # frame_no i (i = newest)
            g._cache_bytes += img.sizeInBytes()

    # byte budget binds: room for 3 tiles, 6 present, none wanted ->
    # the three NEWEST survive, accounting stays exact
    monkeypatch.setattr(gridmod, "CACHE_BYTES", 3 * one)
    monkeypatch.setattr(gridmod, "CACHE_MAX_ENTRIES", 10_000)
    fill(6)
    g.wanted = frozenset()
    g._evict()
    assert set(g.tiles) == {3, 4, 5}
    assert g._cache_bytes == 3 * one == sum(g.tiles[i][2] for i in g.tiles)

    # wanted tiles are never evicted, even the oldest, even over budget
    fill(6)
    g.wanted = frozenset({0, 1})
    g._evict()
    assert {0, 1} <= set(g.tiles)
    assert g._cache_bytes <= 3 * one

    # entry backstop: zero-byte error tiles exert no byte pressure but an
    # all-error library still must not grow the dict without bound
    monkeypatch.setattr(gridmod, "CACHE_MAX_ENTRIES", 150)
    g.tiles.clear()
    g._cache_bytes = 0
    for i in range(300):
        g.tiles[i] = [None, i, 0]
    g.wanted = frozenset()
    g._evict()
    assert len(g.tiles) <= 150


def test_load_catalog_survives_malformed_rows(library: Path,
                                               tmp_path: Path) -> None:
    cat = scan_library(library)
    thumbcache.build_cache(cat, tmp_path / "c")
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)
    data = json.loads(path.read_text())
    data["photos"][0] = {"no_rel_key": 1}  # version 1, but a broken row
    path.write_text(json.dumps(data))
    assert load_catalog(path, library) is None  # -> caller cold-walks


def test_load_rejects_old_sidecar(library: Path, tmp_path: Path) -> None:
    result = thumbcache.build_cache(cat := scan_library(library), tmp_path / "c")
    sidecar = result.path.with_suffix(".fcache.json")
    meta = json.loads(sidecar.read_text())
    del meta["files"]
    sidecar.write_text(json.dumps(meta))
    with pytest.raises(thumbcache.CacheError, match="files"):
        thumbcache.load_cache(result.path)
    assert cat  # silence linters


def test_load_rejects_corrupt(tmp_path: Path) -> None:
    bad = tmp_path / "bad.fcache"
    bad.write_bytes(b"NOPE" + b"\x00" * 12)
    with pytest.raises(thumbcache.CacheError):
        thumbcache.load_cache(bad)
    short = tmp_path / "short.fcache"
    short.write_bytes(thumbcache.MAGIC + struct.pack("<III", 1, 99, 0))
    with pytest.raises(thumbcache.CacheError, match="truncated"):
        thumbcache.load_cache(short)


def test_unreadable_image_gets_error_entry(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "ok.jpg")
    (root / "f" / "corrupt.jpg").write_bytes(b"not a jpeg at all")
    cat = scan_library(root)
    result = thumbcache.build_cache(cat, tmp_path / "c")
    cache = thumbcache.load_cache(result.path)
    by_rel = dict(zip(cache.files, cache.entries))
    assert by_rel["f/corrupt.jpg"][1] == 0  # zero-length = error tile
    assert by_rel["f/ok.jpg"][1] > 0


def test_default_cache_root_frozen_vs_checkout(monkeypatch, tmp_path: Path) -> None:
    """main._default_cache_root: REPO-relative in a source checkout; a
    per-user writable dir when frozen (REPO then points inside the
    read-only PyInstaller bundle, so the disposable cache must go
    somewhere writable). XDG_CACHE_HOME wins over the ~/.cache fallback."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main

    monkeypatch.setattr(main, "FROZEN", False)
    assert main._default_cache_root() == main.REPO / "cache" / "tracer-cache"

    monkeypatch.setattr(main, "FROZEN", True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert main._default_cache_root() == tmp_path / "xdg" / "fauxcasa-tracer"

    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(main.Path, "home",
                        classmethod(lambda cls: tmp_path / "home"))
    assert (main._default_cache_root()
            == tmp_path / "home" / ".cache" / "fauxcasa-tracer")


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
    rebuilding the sidebar and the All-photos view each way."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    assert not win.grid.reveal and len(win.grid.display) == 2
    win.reveal_box.setChecked(True)  # fires _toggle_reveal(True)
    assert win.grid.reveal and len(win.grid.display) == 4
    win.reveal_box.setChecked(False)
    assert not win.grid.reveal and len(win.grid.display) == 2


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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
