#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "PySide6", "pillow"]
# ///
"""Tests for the tracer's non-GUI layers: catalog scan + metadata,
thumbnail-cache build/load/bind, and walk-rule parity with
scripts/make-thumbcache.py. GUI behavior is exercised separately via
the app's --screenshot harness."""

from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import inmeta
import thumbcache
from catalog import (
    ScanFilter,
    load_catalog,
    reconcile_walk,
    save_catalog,
    scan_library,
    walk_library,
)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolate_qt_per_test():
    """Per-test Qt isolation. Two things otherwise accumulate across the whole
    session and, on Windows offscreen, surface as a flaky native access
    violation in a later paint-heavy test (test_reveal_*'s _toggle_reveal),
    with this test's own decode workers merely parked at jobs.get() in the
    dump — i.e. the crash is cumulative state, not an active worker race
    (fauxcasa-gfz):

    1. Each GridView starts 4 daemon decode threads that block forever on
       jobs.get(). stop() retires them — and must run BEFORE widget deletion,
       so a worker can never emit tile_ready into a half-deleted notifier.
    2. QWidgets created in a test are never destroyed; they pile up as live
       Qt objects. Delete every top-level widget and flush the deferred
       deletions so each test starts from a clean widget tree — the way the
       suite behaved before the loupe tests added this much widget churn."""
    yield
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    from grid import GridView

    for w in app.allWidgets():
        if isinstance(w, GridView):
            w.stop()                       # retire pools before any deletion
    app.processEvents()                    # drain queued tile_ready -> update()
    for w in app.topLevelWidgets():
        w.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # actually free them
    app.processEvents()


def make_jpeg(path: Path, w: int = 64, h: int = 48) -> None:
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(120, 160, 200))
    path.parent.mkdir(parents=True, exist_ok=True)
    assert img.save(str(path), "JPEG", 85)


# ---- synthetic in-file-metadata builders (privacy-safe: we construct the
# APP segments by hand to the documented byte framing; no real Picasa data) --

def _jpeg_bytes(w: int = 64, h: int = 48) -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(120, 160, 200))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    assert img.save(buf, "JPEG", 90)
    return bytes(buf.data())


def _inject(jpeg: bytes, marker: int, payload: bytes) -> bytes:
    """Splice an APPn marker segment in right after SOI (FFD8)."""
    assert jpeg[:2] == b"\xff\xd8"
    seg = bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload
    return jpeg[:2] + seg + jpeg[2:]


def _xmp_app1(caption: str | None = None, keywords: tuple[str, ...] = ()) -> bytes:
    body = ""
    if caption is not None:
        body += ('<dc:description><rdf:Alt>'
                 f'<rdf:li xml:lang="x-default">{caption}</rdf:li>'
                 '</rdf:Alt></dc:description>')
    if keywords:
        lis = "".join(f"<rdf:li>{k}</rdf:li>" for k in keywords)
        body += f"<dc:subject><rdf:Bag>{lis}</rdf:Bag></dc:subject>"
    xml = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<rdf:Description rdf:about="">{body}</rdf:Description>'
        '</rdf:RDF></x:xmpmeta>'
    )
    return b"http://ns.adobe.com/xap/1.0/\x00" + xml.encode("utf-8")


def _iptc_app13(caption: str | None = None,
                keywords: tuple[str, ...] = ()) -> bytes:
    def ds(record: int, dataset: int, value: bytes) -> bytes:
        return bytes([0x1C, record, dataset]) + struct.pack(">H", len(value)) \
            + value

    iim = b""
    if caption is not None:
        iim += ds(2, 120, caption.encode("utf-8"))
    for k in keywords:
        iim += ds(2, 25, k.encode("utf-8"))
    # 8BIM block: id 0x0404 (IPTC-NAA), empty Pascal name padded to even,
    # 4-byte size, then the IIM stream padded to even.
    block = b"8BIM" + struct.pack(">H", 0x0404) + b"\x00\x00" \
        + struct.pack(">I", len(iim)) + iim
    if len(iim) % 2:
        block += b"\x00"
    return b"Photoshop 3.0\x00" + block


def _exif_orientation_app1(orientation: int) -> bytes:
    tiff = b"II" + struct.pack("<H", 42) + struct.pack("<I", 8)
    ifd = (struct.pack("<H", 1)
           + struct.pack("<HHI", 0x0112, 3, 1)  # Orientation, SHORT, count 1
           + struct.pack("<HH", orientation, 0)  # value in low 2 bytes, LE
           + struct.pack("<I", 0))               # next-IFD offset
    return b"Exif\x00\x00" + tiff + ifd


def write_jpeg_meta(path: Path, w: int = 64, h: int = 48, *,
                    xmp: bytes | None = None, iptc: bytes | None = None,
                    exif_orientation: int | None = None) -> None:
    data = _jpeg_bytes(w, h)
    if exif_orientation is not None:
        data = _inject(data, 0xE1, _exif_orientation_app1(exif_orientation))
    if xmp is not None:
        data = _inject(data, 0xE1, xmp)
    if iptc is not None:
        data = _inject(data, 0xED, iptc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


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


def test_scan_filter_ignores_images_outside_dimension_bounds(
        tmp_path: Path) -> None:
    root = tmp_path / "lib"
    make_jpeg(root / "icons" / "tiny.jpg", 32, 32)
    make_jpeg(root / "photos" / "normal.jpg", 640, 480)
    make_jpeg(root / "source" / "huge.jpg", 2400, 1600)

    cat = scan_library(root, ScanFilter(min_width=100, min_height=100,
                                        max_width=2000, max_height=1200))
    assert [p.rel for p in cat.photos] == ["photos/normal.jpg"]
    assert cat.visible_count == 1
    assert list(cat.folders) == ["photos"]


def test_scan_filter_keeps_unreadable_images_for_error_tiles(
        tmp_path: Path) -> None:
    root = tmp_path / "lib"
    bad = root / "broken.jpg"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not actually a jpeg")

    cat = scan_library(root, ScanFilter(min_width=100, min_height=100))
    assert [p.rel for p in cat.photos] == ["broken.jpg"]


def test_filtered_cache_dir_is_separate_from_unfiltered(tmp_path: Path) -> None:
    from thumbcache import cache_dir_for

    root = tmp_path / "lib"
    root.mkdir()
    cache_root = tmp_path / "cache"
    plain = cache_dir_for(root, cache_root)
    filtered = cache_dir_for(
        root, cache_root, ScanFilter(min_width=100, min_height=100).cache_key())
    assert plain != filtered
    assert cache_dir_for(root, cache_root) == plain


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


def test_evict_wantband_over_budget_and_entry_floor(monkeypatch) -> None:
    """The load-bearing _evict invariant the byte-bound test doesn't reach:
    tiles the current paint WANTS are never evicted, even when the want-band
    alone blows past every bound, and the entry cap floors at the want-band
    size. Without these the eviction loop livelocks (re-evicting the very
    tiles the next paint re-requests) or over-evicts the visible set.

    * want-band over CACHE_BYTES: a want-band whose summed bytes EXCEED the
      byte budget must keep ALL of its tiles — _evict terminates with the
      whole set intact (no infinite loop chasing an unreachable budget, no
      eviction of a wanted tile). This is the single case that separates the
      correct impl from a livelock / over-eviction regression.
    * entry-cap floor: entry_cap = max(CACHE_MAX_ENTRIES, len(wanted)+128);
      when len(wanted)+128 is the larger (binding) term — never exercised by
      the other tests — the cache trims to THAT floor, not to the smaller
      CACHE_MAX_ENTRIES, so a large want-band can't be re-evicted to a tiny
      entry cap every frame.
    """
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

    # --- want-band over budget: every tile present is wanted and together
    # they exceed CACHE_BYTES. _evict must keep them all and return; the
    # by-age list excludes wanted tiles, so there is nothing to evict and no
    # spin. A regression that evicts wanted tiles, or loops forever chasing
    # the byte budget, fails here (a livelock would hang the test).
    monkeypatch.setattr(gridmod, "CACHE_BYTES", 3 * one)       # room for 3
    monkeypatch.setattr(gridmod, "CACHE_MAX_ENTRIES", 10_000)  # never binds
    g.tiles.clear()
    g._cache_bytes = 0
    for i in range(6):  # 6 * one > 3 * one = CACHE_BYTES
        img = tile(64)
        g.tiles[i] = [img, i, img.sizeInBytes()]
        g._cache_bytes += img.sizeInBytes()
    g.wanted = frozenset(range(6))  # the whole over-budget set is wanted
    g._evict()
    assert set(g.tiles) == set(range(6))          # nothing evicted
    assert g._cache_bytes == 6 * one              # accounting exact, intact
    assert g._cache_bytes > gridmod.CACHE_BYTES   # really was over budget

    # --- entry-cap floor binds: len(wanted)+128 (133) > CACHE_MAX_ENTRIES
    # (10), so the floor is the cap. Zero-byte error tiles exert no byte
    # pressure, isolating the entry path; eviction trims NON-wanted tiles
    # oldest-first down to the floor while keeping every wanted tile. A
    # regression that dropped the floor would trim to CACHE_MAX_ENTRIES (10)
    # instead, evicting wanted tiles the paint still needs.
    monkeypatch.setattr(gridmod, "CACHE_BYTES", 1 << 30)  # bytes never bind
    monkeypatch.setattr(gridmod, "CACHE_MAX_ENTRIES", 10)
    wanted = set(range(5))
    floor = len(wanted) + 128  # 133 — the binding term
    assert floor > gridmod.CACHE_MAX_ENTRIES
    g.tiles.clear()
    g._cache_bytes = 0
    for i in range(200):  # 5 wanted + 195 non-wanted, all 0-byte error tiles
        g.tiles[i] = [None, i, 0]  # frame_no i (i = newest)
    g.wanted = frozenset(wanted)
    g._evict()
    assert len(g.tiles) == floor   # trimmed to the floor, NOT to 10
    assert wanted <= set(g.tiles)  # every wanted tile survives
    # survivors beyond the want-band are the NEWEST non-wanted tiles
    # (oldest-first eviction): indices 0..4 wanted + 72..199 non-wanted
    assert max(g.tiles) == 199 and min(g.tiles) == 0


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


# ---- in-file metadata reader (inmeta.py) --------------------------------

def test_inmeta_reads_xmp() -> None:
    data = _inject(_jpeg_bytes(), 0xE1,
                   _xmp_app1(caption="a sunset", keywords=("sky", "dusk")))
    m = inmeta.read_jpeg_metadata(data)
    assert m.caption == "a sunset"
    assert m.keywords == ("sky", "dusk")


def test_inmeta_reads_iptc() -> None:
    data = _inject(_jpeg_bytes(), 0xED,
                   _iptc_app13(caption="harbor", keywords=("boat", "water")))
    m = inmeta.read_jpeg_metadata(data)
    assert m.caption == "harbor"
    assert m.keywords == ("boat", "water")


def test_inmeta_xmp_wins_over_iptc() -> None:
    """Picasa writes both; XMP is its primary store, so XMP is authoritative
    per field when the two disagree."""
    data = _jpeg_bytes()
    data = _inject(data, 0xED, _iptc_app13(caption="legacy", keywords=("old",)))
    data = _inject(data, 0xE1, _xmp_app1(caption="current", keywords=("new",)))
    m = inmeta.read_jpeg_metadata(data)
    assert m.caption == "current"
    assert m.keywords == ("new",)


def test_inmeta_field_level_fallback() -> None:
    """A field absent from XMP falls back to IPTC rather than vanishing."""
    data = _jpeg_bytes()
    data = _inject(data, 0xED, _iptc_app13(keywords=("tagged",)))  # keywords only
    data = _inject(data, 0xE1, _xmp_app1(caption="just a caption"))  # caption only
    m = inmeta.read_jpeg_metadata(data)
    assert m.caption == "just a caption"
    assert m.keywords == ("tagged",)


def test_inmeta_utf8() -> None:
    data = _inject(_jpeg_bytes(), 0xED,
                   _iptc_app13(caption="café — naïve", keywords=("Москва",)))
    m = inmeta.read_jpeg_metadata(data)
    assert m.caption == "café — naïve"
    assert m.keywords == ("Москва",)


def test_inmeta_iptc_extended_length() -> None:
    """IPTC datasets can use the extended-length form (octet-count field with
    the high bit set); the reader must follow it instead of misreading the
    length inline."""
    value = "extended".encode("utf-8")
    # 0x1C, record 2, dataset 120, length-field 0x8002 (2 following octets),
    # then the 2-byte big-endian length, then the value.
    ext = (bytes([0x1C, 2, 120]) + struct.pack(">H", 0x8000 | 2)
           + struct.pack(">H", len(value)) + value)
    block = (b"8BIM" + struct.pack(">H", 0x0404) + b"\x00\x00"
             + struct.pack(">I", len(ext)) + ext)
    if len(ext) % 2:
        block += b"\x00"
    app13 = b"Photoshop 3.0\x00" + block
    m = inmeta.read_jpeg_metadata(_inject(_jpeg_bytes(), 0xED, app13))
    assert m.caption == "extended"


def test_inmeta_empty_iptc_caption_normalizes_to_none() -> None:
    """A whitespace/NUL-padded or zero-length IPTC 2:120 means 'no caption'
    (matching the XMP path), so it surfaces as None — never "" — and does not
    by itself produce a non-EMPTY result."""
    ws = _inject(_jpeg_bytes(), 0xED,
                 _iptc_app13(caption="  \x00 ", keywords=("k",)))
    m = inmeta.read_jpeg_metadata(ws)
    assert m.caption is None and m.keywords == ("k",)
    only_empty = _inject(_jpeg_bytes(), 0xED, _iptc_app13(caption=""))
    assert inmeta.read_jpeg_metadata(only_empty) is inmeta.EMPTY


def test_inmeta_empty_for_non_jpeg_and_plain() -> None:
    assert inmeta.read_jpeg_metadata(b"\x89PNG\r\n\x1a\n") is inmeta.EMPTY
    assert inmeta.read_jpeg_metadata(b"") is inmeta.EMPTY
    assert inmeta.read_jpeg_metadata(_jpeg_bytes()) is inmeta.EMPTY  # no APP meta


def test_inmeta_fail_soft_on_garbage() -> None:
    """Truncated/garbled APP segments yield no metadata, never an exception."""
    data = _inject(_jpeg_bytes(), 0xE1, b"http://ns.adobe.com/xap/1.0/\x00<not xml")
    assert inmeta.read_jpeg_metadata(data) is inmeta.EMPTY
    # an EXIF APP1 (not XMP) is ignored by the caption/keyword reader
    data2 = _inject(_jpeg_bytes(), 0xE1, _exif_orientation_app1(6))
    assert inmeta.read_jpeg_metadata(data2) is inmeta.EMPTY


# ---- §4 precedence: in-file metadata overrides the ini for JPEG tier-1 ---

def test_index_infile_caption_overrides_ini(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    write_jpeg_meta(root / "f" / "p.jpg",
                    xmp=_xmp_app1(caption="in-file caption",
                                  keywords=("infile",)))
    # the ini also names a caption/keywords — in-file must win (§4)
    (root / "f" / ".picasa.ini").write_text(
        "[p.jpg]\r\nstar=yes\r\ncaption=ini caption\r\nkeywords=ini\r\n")
    cat = scan_library(root)
    p = next(p for p in cat.photos if p.rel == "f/p.jpg")
    assert p.caption == "ini caption"  # before indexing: ini only
    assert p.star  # ini-only state (star) survives the in-file override

    thumbcache.build_cache(cat, tmp_path / "c")
    assert p.caption == "in-file caption"  # index applied in-file precedence
    assert p.keywords == ("infile",)
    assert p.star  # untouched


def test_index_keeps_ini_caption_when_no_infile(tmp_path: Path) -> None:
    """A JPEG with no in-file caption keeps the ini value (covers migrated
    libraries and non-JPEG formats, where the ini is the only home)."""
    root = tmp_path / "lib"
    write_jpeg_meta(root / "f" / "q.jpg")  # plain JPEG, no APP metadata
    (root / "f" / ".picasa.ini").write_text(
        "[q.jpg]\r\ncaption=ini only\r\nkeywords=a, b\r\n")
    cat = scan_library(root)
    thumbcache.build_cache(cat, tmp_path / "c")
    q = next(p for p in cat.photos if p.rel == "f/q.jpg")
    assert q.caption == "ini only"
    assert q.keywords == ("a", "b")


def test_infile_metadata_survives_catalog_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    write_jpeg_meta(root / "f" / "p.jpg",
                    xmp=_xmp_app1(caption="persisted cap", keywords=("kw1",)))
    cat = scan_library(root)
    thumbcache.build_cache(cat, tmp_path / "c")
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)
    loaded = load_catalog(path, root)
    assert loaded is not None
    p = next(p for p in loaded.photos if p.rel == "f/p.jpg")
    assert p.caption == "persisted cap" and p.keywords == ("kw1",)


# ---- EXIF orientation baked consistently into the thumbnail cache --------

def test_index_bakes_exif_orientation(tmp_path: Path) -> None:
    """orientation=6 (rotate 90 CW) turns a 64x32 landscape into a 32x64
    portrait; the baked thumbnail's stored dims must reflect that, while an
    un-tagged control stays landscape — proving the policy is applied at
    decode, consistently with the viewer's auto-transform."""
    root = tmp_path / "lib"
    write_jpeg_meta(root / "rot.jpg", w=64, h=32, exif_orientation=6)
    write_jpeg_meta(root / "flat.jpg", w=64, h=32)  # no Orientation tag
    cat = scan_library(root)
    result = thumbcache.build_cache(cat, tmp_path / "c")
    cache = thumbcache.load_cache(result.path)
    dims = {rel: (w, h) for rel, (_o, _l, w, h) in
            zip(cache.files, cache.entries)}
    assert dims["rot.jpg"][0] < dims["rot.jpg"][1]   # portrait (rotated)
    assert dims["flat.jpg"][0] > dims["flat.jpg"][1]  # landscape (untouched)


def test_index_orientation_through_scaled_decode(tmp_path: Path) -> None:
    """The PRODUCTION path: real photos exceed THUMB_EDGE, so the indexer
    takes the setScaledSize branch — pre-transform pixel space composed with
    a 90-degree autoTransform. A 600x300 landscape rotated 90 CW must bake to
    a portrait thumb that fits the 256 box AND keeps the 1:2 aspect (catches
    any IgnoreAspectRatio distortion in the scaled-decode math)."""
    root = tmp_path / "lib"
    write_jpeg_meta(root / "big.jpg", w=600, h=300, exif_orientation=6)
    cat = scan_library(root)
    result = thumbcache.build_cache(cat, tmp_path / "c")
    (_o, _l, w, h), = thumbcache.load_cache(result.path).entries
    assert max(w, h) <= thumbcache.THUMB_EDGE  # fits the box
    assert w < h                               # portrait (rotated)
    assert abs((w / h) - 0.5) < 0.05           # aspect preserved, no distortion


def test_make_thumbcache_bakes_orientation_like_indexer(tmp_path: Path) -> None:
    """The standalone builder (scripts/make-thumbcache.py, PIL
    exif_transpose) must apply orientation the same way the in-app indexer
    does — backing the README's "consistent across every path" claim."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mtc", REPO / "scripts" / "make-thumbcache.py")
    mtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtc)

    rot = tmp_path / "rot.jpg"
    write_jpeg_meta(rot, w=600, h=300, exif_orientation=6)
    flat = tmp_path / "flat.jpg"
    write_jpeg_meta(flat, w=600, h=300)
    (_b, rw, rh), = mtc._make_thumb(rot, [256])   # one level -> one record
    (_b, fw, fh), = mtc._make_thumb(flat, [256])
    assert rw < rh   # rotated -> portrait, matching the Qt indexer
    assert fw > fh   # untouched -> landscape


# ---- fcache v2: multi-resolution cache format (fauxcasa-gtr) --------------
#
# v2 stores several long-edge levels per photo, declared in the header
# (largest first); v1 (single 256 level) and v2 share ONE reader keyed on the
# version field, so the shipped v1 benchmark cache keeps loading. The grid
# reads the PRIMARY level (256), so a v2 cache that includes 256 leaves the
# grid and §7 behaviour unchanged and only adds larger levels for a future
# hi-DPI / loupe consumer (the viewer still reads originals, N4).


def _big_library(root: Path) -> None:
    """Sources larger than every test level so each level carries real,
    distinct pixels (a downscale, never an upscale)."""
    make_jpeg(root / "f" / "land.jpg", 600, 400)   # landscape
    make_jpeg(root / "f" / "port.jpg", 400, 600)   # portrait


def test_v1_build_keeps_legacy_header(library: Path, tmp_path: Path) -> None:
    """The single-level default still emits a v1 header (version=1, reserved
    word 0) — the multi-level refactor must not perturb the legacy format the
    shipped benchmark cache depends on."""
    result = thumbcache.build_cache(scan_library(library), tmp_path / "c")
    with open(result.path, "rb") as f:
        hdr = f.read(16)
    assert hdr[:4] == thumbcache.MAGIC
    version, count, reserved = struct.unpack("<III", hdr[4:16])
    assert version == 1 and reserved == 0 and count == 4
    cache = thumbcache.load_cache(result.path)
    assert cache.levels == [256] and cache.primary == 0
    assert cache.level_entries == [cache.entries]  # single level == entries
    # the v1 sidecar gains no "levels" key (byte-stable for old caches)
    meta = json.loads(result.path.with_suffix(".fcache.json").read_text())
    assert "levels" not in meta and meta["thumb_edge"] == 256


def test_v2_multilevel_roundtrip(tmp_path: Path) -> None:
    """A v2 build writes a level table + count*nlevels photo-major index; the
    dual reader recovers the levels, each (photo,level) blob is a real JPEG
    that fits its box, a photo's per-level long edges are non-increasing, the
    primary is 256, and files[] stays one-per-photo (so bind works)."""
    root = tmp_path / "lib"
    _big_library(root)
    cat = scan_library(root)
    result = thumbcache.build_cache(cat, tmp_path / "c", levels=[512, 256, 128])

    with open(result.path, "rb") as f:
        hdr = f.read(16)
        version, count, word3 = struct.unpack("<III", hdr[4:16])
        nlevels = word3 & 0xFFFF
        ltbl = f.read(2 * nlevels)
    assert version == 2 and count == 2 and nlevels == 3
    assert list(struct.unpack("<3H", ltbl)) == [512, 256, 128]

    cache = thumbcache.load_cache(result.path)
    assert cache.levels == [512, 256, 128] and cache.count == 2
    assert cache.primary == 1 and cache.levels[cache.primary] == 256
    assert cache.entries == cache.level_entries[1]  # entries mirrors primary
    assert len(cache.level_entries) == 3
    assert all(len(le) == 2 for le in cache.level_entries)
    assert len(cache.files) == 2  # ONE per photo, not count*nlevels
    thumbcache.bind(cache, cat)   # must not raise
    # sidecar records the levels (informational)
    meta = json.loads(result.path.with_suffix(".fcache.json").read_text())
    assert meta["levels"] == [512, 256, 128] and meta["thumb_edge"] == 256

    with open(result.path, "rb") as f:
        for p in range(cache.count):
            longs = []
            for li, edge in enumerate(cache.levels):
                off, length, w, h = cache.entry(p, li)
                assert length > 0 and 0 < max(w, h) <= edge
                f.seek(off)
                assert f.read(3) == b"\xff\xd8\xff"  # real JPEG SOI
                longs.append(max(w, h))
            assert longs == sorted(longs, reverse=True)  # 512 >= 256 >= 128 caps


def test_best_level_picks_smallest_sufficient(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    _big_library(root)
    cache = thumbcache.load_cache(thumbcache.build_cache(
        scan_library(root), tmp_path / "c", levels=[512, 256, 128]).path)
    assert cache.best_level(512) == 0
    assert cache.best_level(300) == 0     # only 512 is big enough
    assert cache.best_level(256) == 1
    assert cache.best_level(130) == 1
    assert cache.best_level(128) == 2
    assert cache.best_level(1) == 2       # cheapest sufficient
    assert cache.best_level(99999) == 0   # nothing big enough -> largest


def test_dual_version_both_load_and_bind(library: Path, tmp_path: Path) -> None:
    """A v1 and a v2 cache from the same library both load and bind; the
    levels differ but files[] (one per photo, walk order) is identical."""
    cat1 = scan_library(library)
    v1 = thumbcache.build_cache(cat1, tmp_path / "v1")
    cat2 = scan_library(library)
    v2 = thumbcache.build_cache(cat2, tmp_path / "v2", levels=[512, 256])
    c1 = thumbcache.load_cache(v1.path)
    c2 = thumbcache.load_cache(v2.path)
    assert c1.levels == [256] and c2.levels == [512, 256]
    assert c1.files == c2.files            # identical one-per-photo walk
    thumbcache.bind(c1, cat1)
    thumbcache.bind(c2, cat2)


def test_v2_levels_normalized_largest_first(tmp_path: Path) -> None:
    """Levels are de-duped and sorted largest-first regardless of input order,
    so the on-disk table, the primary, and bind are deterministic."""
    root = tmp_path / "lib"
    _big_library(root)
    cache = thumbcache.load_cache(thumbcache.build_cache(
        scan_library(root), tmp_path / "c",
        levels=[128, 256, 128, 512]).path)  # unsorted + duplicate
    assert cache.levels == [512, 256, 128]


def test_v1_layout_reserved_for_default_256_only() -> None:
    """The v1 layout (no level table; the reader hard-codes [256]) must be
    chosen ONLY for the default single 256 px set. A lone non-256 level has to
    go to v2 or it gets silently mislabeled 256. The predicate is mirrored in
    both builders, so assert both modules agree."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mtc", REPO / "scripts" / "make-thumbcache.py")
    mtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtc)
    for mod in (thumbcache, mtc):
        assert mod._is_v1([256]) is True       # the legacy default stays v1
        assert mod._is_v1([512]) is False      # a lone non-256 level -> v2
        assert mod._is_v1([128]) is False
        assert mod._is_v1([512, 256]) is False
        assert mod._is_v1([512, 256, 128]) is False


def test_v2_single_nondefault_level_not_mislabeled(tmp_path: Path) -> None:
    """build_cache(levels=[512]) — a single NON-256 level — must emit v2 with a
    level table so the read-back level is 512, not the v1-hardcoded 256, and the
    sidecar's thumb_edge agrees with the header. Guards the regression where any
    single level wrote a v1 header and silently became 256."""
    root = tmp_path / "lib"
    _big_library(root)            # 600x400 / 400x600 -> a real 512 px downscale
    result = thumbcache.build_cache(scan_library(root), tmp_path / "c",
                                    levels=[512])
    with open(result.path, "rb") as f:
        hdr = f.read(16)
        version, count, word3 = struct.unpack("<III", hdr[4:16])
        nlevels = word3 & 0xFFFF
        ltbl = f.read(2 * nlevels)
    assert version == 2 and count == 2 and nlevels == 1
    assert list(struct.unpack("<1H", ltbl)) == [512]

    cache = thumbcache.load_cache(result.path)
    assert cache.levels == [512] and cache.primary == 0
    assert cache.levels[cache.primary] == 512        # NOT mislabeled 256
    assert cache.entries == cache.level_entries[0]
    assert len(cache.files) == 2                      # one record per photo
    meta = json.loads(result.path.with_suffix(".fcache.json").read_text())
    assert meta["thumb_edge"] == 512                  # sidecar agrees with header
    assert "levels" not in meta                       # single level: no list key


def test_load_rejects_unsupported_version_and_truncated_v2(tmp_path: Path) -> None:
    bad_ver = tmp_path / "v3.fcache"
    bad_ver.write_bytes(thumbcache.MAGIC + struct.pack("<III", 3, 0, 0))
    with pytest.raises(thumbcache.CacheError, match="unsupported"):
        thumbcache.load_cache(bad_ver)
    # a v2 header promising 3 levels with no level-table bytes following
    trunc = tmp_path / "trunc.fcache"
    trunc.write_bytes(thumbcache.MAGIC + struct.pack("<IIHH", 2, 1, 3, 0))
    with pytest.raises(thumbcache.CacheError, match="level table"):
        thumbcache.load_cache(trunc)


def test_v2_canonical_builder_thumbs_match_inapp(tmp_path: Path) -> None:
    """scripts/make-thumbcache.py and the in-app builder produce the SAME v2
    levels and per-level geometry for the same input. JPEG blob BYTES differ
    between PIL and Qt (as they do in v1), so we assert the level set, the
    fit-the-box invariant, the orientation, and the aspect ratio agree — the
    structure, not the encoder output."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mtc", REPO / "scripts" / "make-thumbcache.py")
    mtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtc)
    assert mtc._normalize_levels([128, 512, 256]) == [512, 256, 128]
    assert mtc._primary_level([512, 256, 128]) == 1  # mirrors thumbcache

    root = tmp_path / "lib"
    _big_library(root)
    levels = [512, 256, 128]
    inapp = thumbcache.load_cache(thumbcache.build_cache(
        scan_library(root), tmp_path / "c", levels=levels).path)
    files = sorted(root.rglob("*.jpg"))
    assert [f.name for f in files] == ["land.jpg", "port.jpg"]
    for p, f in enumerate(files):
        canon = mtc._make_thumb(f, levels)   # PIL builder, direct (no pool)
        assert len(canon) == len(levels)
        for li, edge in enumerate(levels):
            cb, cw, ch = canon[li]
            assert cb[:3] == b"\xff\xd8\xff" and 0 < max(cw, ch) <= edge
            _o, _l, iw, ih = inapp.entry(p, li)
            assert (cw < ch) == (iw < ih)            # same orientation
            assert abs((cw / ch) - (iw / ih)) < 0.03  # aspect agrees


def test_canonical_builder_corrupt_source_is_error_tile(tmp_path: Path) -> None:
    """A corrupt source in the CLI builder yields a zero-length blob at every
    level — the same error tile the in-app builder emits — so one bad file can't
    abort the whole batch build by raising out of _make_thumb."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mtc", REPO / "scripts" / "make-thumbcache.py")
    mtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtc)
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not a jpeg at all")
    out = mtc._make_thumb(bad, [512, 256, 128])
    assert out == [(b"", 0, 0)] * 3


def test_v2_error_tile_is_zero_length_at_every_level(tmp_path: Path) -> None:
    """A corrupt source yields a zero-length blob for EVERY level (the error
    tile the grid/load path keys on), while a good photo's levels are all
    non-zero."""
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "ok.jpg", 600, 400)
    (root / "f" / "bad.jpg").write_bytes(b"not a jpeg at all")
    cat = scan_library(root)
    cache = thumbcache.load_cache(thumbcache.build_cache(
        cat, tmp_path / "c", levels=[512, 256, 128]).path)
    by_rel = {f: i for i, f in enumerate(cache.files)}
    bad, ok = by_rel["f/bad.jpg"], by_rel["f/ok.jpg"]
    for li in range(len(cache.levels)):
        assert cache.entry(bad, li)[1] == 0   # zero-length = error tile
        assert cache.entry(ok, li)[1] > 0


def test_v2_never_upscales_small_source(tmp_path: Path) -> None:
    """A source smaller than the largest level is NOT upscaled: the top level
    holds the photo's native dims, and the per-level long edges are
    non-increasing (contract item 5)."""
    root = tmp_path / "lib"
    make_jpeg(root / "small.jpg", 200, 150)  # < 512 and < 256
    cache = thumbcache.load_cache(thumbcache.build_cache(
        scan_library(root), tmp_path / "c", levels=[512, 256, 128]).path)
    _o, _l, w0, h0 = cache.entry(0, 0)        # top (512) level
    assert (w0, h0) == (200, 150)             # native — never upscaled
    assert max(cache.entry(0, 2)[2:]) <= 128  # 128 level downscaled to fit
    longs = [max(cache.entry(0, li)[2:]) for li in range(3)]
    assert longs == sorted(longs, reverse=True)


def test_normalize_levels_caps_at_64() -> None:
    """A level set the v2 reader would reject (>64 levels, nlevels is a u16
    the reader bounds to 64) fails fast at build time; 64 is accepted."""
    assert len(thumbcache._normalize_levels(list(range(1, 65)))) == 64
    with pytest.raises(ValueError, match="64 levels"):
        thumbcache._normalize_levels(list(range(1, 66)))


def test_sidecar_only_rewrites_v1_and_v2(tmp_path: Path) -> None:
    """make-thumbcache --sidecar-only regenerates the sidecar from a fresh
    walk + the cache HEADER (authoritative on a rewrite): a v1 cache gets no
    'levels' key, a v2 cache gets its real level set even when --levels says
    otherwise. The rewrite path uses no process pool, so build the cache
    in-app (threads) and drive the script's main() directly."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mtc", REPO / "scripts" / "make-thumbcache.py")
    mtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtc)
    assert mtc._parse_levels("recommended") == list(mtc.RECOMMENDED_LEVELS)

    lib = tmp_path / "lib"
    _big_library(lib)

    v1 = thumbcache.build_cache(scan_library(lib), tmp_path / "c1").path
    assert mtc.main(["--library", str(lib), "--out", str(v1),
                     "--sidecar-only"]) == 0
    m1 = json.loads(v1.with_suffix(".fcache.json").read_text())
    assert "levels" not in m1 and m1["thumb_edge"] == 256 and len(m1["files"]) == 2

    v2 = thumbcache.build_cache(scan_library(lib), tmp_path / "c2",
                                levels=[512, 256, 128]).path
    # a DIFFERENT --levels proves the cache header, not the flag, wins
    assert mtc.main(["--library", str(lib), "--out", str(v2),
                     "--sidecar-only", "--levels", "256"]) == 0
    m2 = json.loads(v2.with_suffix(".fcache.json").read_text())
    assert m2["levels"] == [512, 256, 128] and m2["thumb_edge"] == 256


def test_v2_cache_drives_grid_consumer(tmp_path: Path) -> None:
    """The --thumbs adopt path (load_cache -> bind -> grid.set_data) takes a
    v2 cache unchanged: the grid reads entries[idx] = the PRIMARY 256 level
    (never the 512 top), so grid / z1e need no v2 awareness."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from grid import GridView

    app = QApplication.instance() or QApplication([])
    assert app is not None
    root = tmp_path / "lib"
    _big_library(root)
    cat = scan_library(root)
    cache = thumbcache.load_cache(thumbcache.build_cache(
        cat, tmp_path / "c", levels=[512, 256, 128]).path)
    thumbcache.bind(cache, cat)   # the adopt bind
    grid = GridView()
    grid.set_data(cat, cache)     # consumer takes the v2 cache
    for idx in range(cache.count):
        off, length, w, h = cache.entries[idx]
        assert length > 0 and max(w, h) <= 256   # primary 256, not the 512 top


# --- the fcache v2 loupe / hi-DPI consumer (fauxcasa-9pp): the viewer paints
# an instant cached preview — the nearest level >= the viewport's device
# pixels, read via best_level()/entry() — while the full original decodes
# off-thread. A v2 cache hands it the >256 (512) level; a v1 cache its 256
# level; no cache or an error tile -> no preview, just the loading text. This
# is the first consumer of the larger levels gtr shipped.


def _offscreen_app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _bound_cache(tmp_path: Path, root: Path, levels=None):
    """Build + load + bind a cache for `root`; returns (catalog, cache)."""
    cat = scan_library(root)
    built = thumbcache.build_cache(cat, tmp_path / "c", levels=levels)
    cache = thumbcache.load_cache(built.path)
    thumbcache.bind(cache, cat)
    return cat, cache


def test_viewer_preview_reads_larger_v2_level(tmp_path: Path) -> None:
    """A large window makes best_level() pick the v2 512 top — the >256 level
    nothing consumed before gtr — and the painted preview IS that level's
    image (its long edge matches entry(idx, level))."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)                          # 600x400 + 400x600 -> real 512
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256, 128])
    viewer = ViewerPage(cat, cache)
    viewer.resize(1280, 800)                     # large window -> min_edge > 512
    min_edge = viewer._preview_min_edge()
    level = cache.best_level(min_edge)
    viewer.show_photo(list(range(cache.count)), 0)
    assert viewer.preview is not None and not viewer.preview.isNull()
    _o, _l, w0, h0 = cache.entry(0, level)       # land.jpg @ 512: 512x341
    assert max(viewer.preview.width(), viewer.preview.height()) == max(w0, h0)
    # the whole point: a window this large clears 256 and reads the >256 level
    assert min_edge >= 512 and level == 0 and max(w0, h0) == 512


def test_viewer_preview_v1_falls_back_to_256(tmp_path: Path) -> None:
    """A single-level v1 cache has only 256; the viewer still shows an instant
    256 preview rather than a blank window (graceful, no v2 required)."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)
    cat, cache = _bound_cache(tmp_path, root)    # default -> v1 [256]
    assert cache.levels == [256]
    viewer = ViewerPage(cat, cache)
    viewer.resize(1280, 800)
    viewer.show_photo(list(range(cache.count)), 0)
    assert viewer.preview is not None
    assert max(viewer.preview.width(), viewer.preview.height()) <= 256


def test_viewer_no_preview_without_cache(tmp_path: Path) -> None:
    """No cache yet (cold start before the build lands) -> no preview and no
    crash; the viewer shows its loading text and still loads the original."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)
    cat = scan_library(root)
    viewer = ViewerPage(cat, None)
    viewer.resize(1280, 800)
    viewer.show_photo(list(range(len(cat.photos))), 0)
    assert viewer.preview is None


def test_viewer_error_tile_yields_no_preview(tmp_path: Path) -> None:
    """An error-tile entry (zero-length blob, original undecodable at build)
    yields no preview; a good neighbour in the same cache still previews."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "ok.jpg", 600, 400)
    (root / "f" / "bad.jpg").write_bytes(b"not a jpeg at all")
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256, 128])
    by_rel = {f: i for i, f in enumerate(cache.files)}
    viewer = ViewerPage(cat, cache)
    viewer.resize(1280, 800)
    assert viewer._load_preview(by_rel["f/bad.jpg"], 0) is None   # error tile
    assert viewer._load_preview(by_rel["f/ok.jpg"], 0) is not None


def test_viewer_original_supersedes_preview_then_falls_back(
        tmp_path: Path) -> None:
    """The decoded original replaces (and frees) the preview; but if the
    original FAILS to decode, the cached preview keeps painting rather than
    dropping straight to 'could not decode'."""
    _offscreen_app()
    from PySide6.QtGui import QImage
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256])
    viewer = ViewerPage(cat, cache)
    viewer.resize(1280, 800)
    viewer.show_photo(list(range(cache.count)), 0)
    assert viewer.preview is not None
    orig = QImage(800, 600, QImage.Format.Format_RGB32)
    orig.fill(0)
    viewer._on_loaded(viewer._serial, orig)         # the real original lands
    assert viewer.image is orig and viewer.preview is None
    viewer.show_photo(list(range(cache.count)), 1)  # next photo: preview again
    assert viewer.preview is not None
    viewer._on_loaded(viewer._serial, QImage())     # original failed to decode
    assert viewer.image is None and viewer.preview is not None


def test_viewer_preview_composes_picasa_rotate(tmp_path: Path) -> None:
    """The preview composes the Picasa rotate= quarter-turns on top of the
    EXIF-upright cached thumb, exactly as the grid and the original path do —
    a 90 deg turn makes a landscape thumb paint portrait."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    make_jpeg(root / "land.jpg", 600, 400)          # landscape thumb (w > h)
    (root / ".picasa.ini").write_text(
        "[land.jpg]\r\nrotate=rotate(1)\r\n")       # one quarter-turn CW
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256, 128])
    p = next(p for p in cat.photos if p.rel == "land.jpg")
    assert p.rotate == 1
    _o, _l, w0, h0 = cache.entry(0, 0)
    assert w0 > h0                                   # cached thumb is landscape
    viewer = ViewerPage(cat, cache)
    viewer.resize(1280, 800)
    rotated = viewer._load_preview(0, p.rotate)
    assert rotated is not None and rotated.height() > rotated.width()  # portrait


def test_viewer_display_rect_preview_fills_original_caps() -> None:
    """The pure paint geometry: the preview (cap=False) fills the viewport box;
    a window-sized original (cap=True) lands on the SAME rect, so the hand-off
    is a sharpen-in-place. A small ORIGINAL caps at native (never upscaled),
    while the same small source as a PREVIEW fills the box (the accepted one-off
    pop). Degenerate 1x1 never yields a zero-size rect."""
    _offscreen_app()
    from viewer import ViewerPage
    big = ViewerPage._display_rect(1280, 800, 4000, 3000, cap=True)   # original
    prev = ViewerPage._display_rect(1280, 800, 512, 384, cap=False)   # preview
    assert big == prev                                   # identical 4:3 fit
    assert big.height() == 800 and 0 < big.width() <= 1280   # fills the box
    small_orig = ViewerPage._display_rect(1280, 800, 800, 600, cap=True)
    assert (small_orig.width(), small_orig.height()) == (800, 600)    # native
    small_prev = ViewerPage._display_rect(1280, 800, 800, 600, cap=False)
    assert small_prev.width() > 800 and small_prev.height() > 600     # filled
    assert ViewerPage._display_rect(1280, 800, 1, 1, cap=True).width() == 1


def test_viewer_stale_original_is_dropped(tmp_path: Path) -> None:
    """A late original carrying a superseded serial (user navigated on before it
    decoded) is dropped by the secondary guard in _on_loaded — it must not
    overwrite the current photo's freshly-decoded preview."""
    _offscreen_app()
    from PySide6.QtGui import QImage
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256])
    viewer = ViewerPage(cat, cache)
    viewer.resize(1280, 800)
    viewer.show_photo(list(range(cache.count)), 0)
    stale = viewer._serial
    viewer.show_photo(list(range(cache.count)), 1)   # serial advances; new preview
    p1 = viewer.preview
    assert p1 is not None
    viewer._on_loaded(stale, QImage(800, 600, QImage.Format.Format_RGB32))
    assert viewer.image is None and viewer.preview is p1   # untouched


def test_viewer_preview_min_edge_floors_at_grid_tile(tmp_path: Path) -> None:
    """A tiny window floors min_edge at the grid's 256 px tile, so the preview
    never drops below the grid — best_level picks the 256 level, not the 128."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256, 128])
    viewer = ViewerPage(cat, cache)
    viewer.resize(120, 120)
    assert viewer._preview_min_edge() == 256
    assert cache.best_level(viewer._preview_min_edge()) == 1   # 256, not 128


def test_viewer_preview_dpr_selects_larger_level(
        tmp_path: Path, monkeypatch) -> None:
    """The hi-DPI path: device-pixel-ratio multiplies the logical viewport, so
    the SAME small window selects the 256 level at DPR 1 but the larger 512
    level at DPR 2 — best_level() reads a >256 level only because of the DPR
    scaling, which offscreen (DPR 1.0) alone could never exercise."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256, 128])
    viewer = ViewerPage(cat, cache)
    viewer.resize(200, 200)
    monkeypatch.setattr(viewer, "devicePixelRatioF", lambda: 1.0)
    assert viewer._preview_min_edge() == 256                  # 200 floored to 256
    assert cache.best_level(viewer._preview_min_edge()) == 1  # the 256 level
    monkeypatch.setattr(viewer, "devicePixelRatioF", lambda: 2.0)
    assert viewer._preview_min_edge() == 400                  # 200 * 2 device px
    assert cache.best_level(viewer._preview_min_edge()) == 0  # the >256 512 level


def test_mainwindow_wires_viewer_cache_on_build_and_reconcile(
        tmp_path: Path) -> None:
    """The integration this bead adds: MainWindow hands the viewer the cache
    pair on a cold-build finish (_on_index_finished) and on a reconcile swap
    (reload_data) — the same cache the grid gets, so both consume v2."""
    _offscreen_app()
    import main
    root = tmp_path / "lib"
    _big_library(root)
    cat = scan_library(root)
    win = main.MainWindow(cat, None, cache_dir=None, build_dir=None)
    assert win.viewer.thumbs is None                  # cold start: no cache yet
    built = thumbcache.build_cache(cat, tmp_path / "c", levels=[512, 256])
    win._on_index_finished(built, win.catalog, False)  # cold-build finish
    assert win.viewer.thumbs is not None
    assert win.viewer.thumbs.levels == [512, 256]
    assert win.grid.thumbs is win.viewer.thumbs        # one cache, both consumers
    cat2 = scan_library(root)
    cache2 = thumbcache.load_cache(thumbcache.build_cache(
        cat2, tmp_path / "c2").path)                   # a fresh (v1) cache object
    thumbcache.bind(cache2, cat2)
    win.reload_data(cat2, cache2)                      # reconcile swap
    assert win.viewer.catalog is cat2 and win.viewer.thumbs is cache2


def test_grid_stop_retires_decode_workers() -> None:
    """GridView.stop() retires its decode-worker pool so the daemons don't
    leak and accumulate across a process (fauxcasa-gfz). After stop() no worker
    of this grid is still alive, and stop() is idempotent (closeEvent + the
    autouse teardown may both call it). The immortal pool was what raced the
    main thread on Qt state and crashed a later test on Windows."""
    _offscreen_app()
    from grid import GridView, WORKERS

    g = GridView()
    workers = list(g._workers)
    assert len(workers) == WORKERS and all(t.is_alive() for t in workers)
    g.stop()
    for t in workers:
        assert not t.is_alive()   # joined and exited on the sentinel
    g.stop()                      # idempotent, no error, no re-stop


def test_index_empty_infile_caption_keeps_ini(tmp_path: Path) -> None:
    """An empty/whitespace in-file caption is 'no caption', not "" — it must
    not clobber the ini caption (§4 precedence), and the catalog must still
    round-trip (no '' vs None divergence vs a warm load)."""
    root = tmp_path / "lib"
    # caption present-but-empty in IPTC, plus a real ini caption
    write_jpeg_meta(root / "f" / "p.jpg", iptc=_iptc_app13(caption="   "))
    (root / "f" / ".picasa.ini").write_text("[p.jpg]\r\ncaption=real ini\r\n")
    cat = scan_library(root)
    thumbcache.build_cache(cat, tmp_path / "c")
    p = next(p for p in cat.photos if p.rel == "f/p.jpg")
    assert p.caption == "real ini"  # the empty in-file value did not win

    path = tmp_path / "cat.json"
    save_catalog(cat, path)
    loaded = load_catalog(path, root)
    assert loaded is not None
    lp = next(p for p in loaded.photos if p.rel == "f/p.jpg")
    assert lp.caption == "real ini"  # round-trips; no empty-string leak


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
    rebuilding the sidebar (the stash folder reappears) and the status
    counts (reveal-mode photo/folder tallies), not just the grid filter."""
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

    STASH = "2020-01-01 Trip/.picasaoriginals"
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    # default: visible-only grid, sidebar (no stash folder), and counts
    assert not win.grid.reveal and len(win.grid.display) == 2
    assert STASH not in folder_rels(win)
    assert "2 photos · 2 folders" in win.counts_label.text()

    win.reveal_box.setChecked(True)  # fires _toggle_reveal(True)
    assert win.grid.reveal and len(win.grid.display) == 4
    assert STASH in folder_rels(win)  # stash folder revealed in the tree
    assert "4 photos · 3 folders" in win.counts_label.text()

    win.reveal_box.setChecked(False)
    assert not win.grid.reveal and len(win.grid.display) == 2
    assert STASH not in folder_rels(win)
    assert "2 photos · 2 folders" in win.counts_label.text()


@pytest.fixture()
def reveal_library(tmp_path: Path) -> Path:
    """A library whose one folder holds a visible starred photo and a
    hidden=yes STARRED photo, so reveal mode changes BOTH a rendered
    per-folder count (1 -> 2) and the Starred tally (1 -> 2) — exercising
    _build_sidebar's fcount() and its (visible or reveal) star branch."""
    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "shown.jpg")
    make_jpeg(root / "Trip" / "secret.jpg")
    (root / "Trip" / ".picasa.ini").write_text(
        "[shown.jpg]\r\nstar=yes\r\n"
        "[secret.jpg]\r\nstar=yes\r\nhidden=yes\r\n"
    )
    return root


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

@pytest.fixture()
def folder_hidden_library(tmp_path: Path) -> Path:
    """A normal folder tagged with the sibling category (P2category=Folders
    on Disk) plus a folder placed in Picasa's built-in 'Hidden Folders'
    collection ([Picasa] P2category=Hidden Folders), which hides the WHOLE
    folder — every photo under it, mirroring per-photo hidden=yes."""
    root = tmp_path / "lib"
    make_jpeg(root / "2020 Trip" / "a.jpg")
    (root / "2020 Trip" / ".picasa.ini").write_text(
        "[Picasa]\r\nname=2020 Trip\r\nP2category=Folders on Disk\r\n")
    make_jpeg(root / "2021 Secret" / "s1.jpg")
    make_jpeg(root / "2021 Secret" / "s2.jpg")
    (root / "2021 Secret" / ".picasa.ini").write_text(
        "[Picasa]\r\nname=2021 Secret\r\nP2category=Hidden Folders\r\n")
    return root


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
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
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
                        lambda root, cr: ("prog", [str(root), str(cr)]))

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
                        lambda root, cr: ("prog", [str(root), str(cr)]))
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
    main_py = Path(__file__).resolve().parent / "main.py"
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
    tracer_dir = Path(__file__).resolve().parent
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

    log_path = applog.setup(tmp_path / "cr")
    assert log_path == tmp_path / "cr" / "fauxcasa-tracer.log"

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
    main_py = Path(__file__).resolve().parent / "main.py"
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

    # Human diagnostics: in the log file beside the per-library caches.
    log_path = cache_root / "fauxcasa-tracer.log"
    assert log_path.is_file()
    log_text = log_path.read_text()
    assert "photos," in log_text and "folders," in log_text   # startup line
    # The machine protocol must NOT have been rerouted into the log.
    assert "READY" not in log_text and '"event": "ready"' not in log_text


# ---- search upgrades: multi-word AND, -term negation, folder names --------
# (fauxcasa-q6l.6) §5: instant search over filenames, captions, keywords and
# folder names, with '-term' negation. Positive terms AND together (each may
# match a different field of the same photo); any '-term' hit excludes the
# photo; a lone '-' (a negation still being typed) is ignored. People-name
# search joins the same haystack once faces land (see the haystack() parts
# list in main.MainWindow._search_changed).

@pytest.fixture()
def search_library(tmp_path: Path) -> Path:
    """Distinct vocabulary per field so each test proves WHICH field matched:
    'beach'/'city'/'osaka' appear only in folder names, 'ocean'/'sand'/'neon'
    only in keywords, 'golden'/'stalls' only in captions, and
    'sunset'/'dunes'/'market'/'street' only in filenames. Osaka is nested
    under 2021 City to exercise rel-path-segment matching."""
    root = tmp_path / "lib"
    make_jpeg(root / "2020 Beach Trip" / "sunset.jpg")
    make_jpeg(root / "2020 Beach Trip" / "dunes.jpg")
    make_jpeg(root / "2021 City" / "market.jpg")
    make_jpeg(root / "2021 City" / "Osaka" / "street.jpg")
    (root / "2020 Beach Trip" / ".picasa.ini").write_text(
        "[sunset.jpg]\r\ncaption=Golden hour\r\nkeywords=sun, ocean\r\n"
        "[dunes.jpg]\r\nkeywords=sand\r\n")
    (root / "2021 City" / ".picasa.ini").write_text(
        "[market.jpg]\r\ncaption=night stalls\r\nkeywords=food, neon\r\n")
    return root


def _search_win(library_root: Path):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None
    return MainWindow(scan_library(library_root), None,
                      cache_dir=None, build_dir=None)


def _hits(win) -> set:
    return {win.catalog.photos[i].name for i in win.grid.display}


def test_search_multi_word_and(search_library: Path) -> None:
    """Multiple terms AND together — each must match somewhere on the same
    photo, fields may differ per term — instead of the old single-substring
    reading where 'sunset ocean' had to appear verbatim, space included."""
    win = _search_win(search_library)

    win.search.setText("sunset ocean")         # filename AND keyword
    assert _hits(win) == {"sunset.jpg"}
    win.search.setText("beach golden")         # folder AND caption
    assert _hits(win) == {"sunset.jpg"}
    assert "Search" in win.counts_label.text()
    win.search.setText("sunset neon")          # terms hit different photos
    assert _hits(win) == set()


def test_search_negation(search_library: Path) -> None:
    """'-term' excludes any photo it matches, whatever the field; a
    negation-only query stands alone (Picasa's all-photos hack was exactly
    a match-nothing negation search)."""
    win = _search_win(search_library)

    win.search.setText("beach -dunes")         # folder hits minus a filename
    assert _hits(win) == {"sunset.jpg"}
    win.search.setText("beach -ocean")         # ...minus a keyword hit
    assert _hits(win) == {"dunes.jpg"}
    win.search.setText("-city")                # negation-only: the rest
    assert _hits(win) == {"sunset.jpg", "dunes.jpg"}
    win.search.setText("-nosuchterm")          # excludes nothing -> all
    assert _hits(win) == {"sunset.jpg", "dunes.jpg", "market.jpg",
                          "street.jpg"}


def test_search_folder_name(search_library: Path) -> None:
    """A term matching a folder's display title or any rel-path segment
    pulls that folder's photos into the flat result set — a nested folder's
    photos are also reached through their parent's segment."""
    win = _search_win(search_library)

    win.search.setText("beach")
    assert _hits(win) == {"sunset.jpg", "dunes.jpg"}
    win.search.setText("osaka")                # the nested folder's own name
    assert _hits(win) == {"street.jpg"}
    win.search.setText("city")                 # parent segment: nested too
    assert _hits(win) == {"market.jpg", "street.jpg"}


def test_search_negated_folder(search_library: Path) -> None:
    win = _search_win(search_library)

    win.search.setText("-beach")
    assert _hits(win) == {"market.jpg", "street.jpg"}
    win.search.setText(".jpg -city")           # everything minus a subtree
    assert _hits(win) == {"sunset.jpg", "dunes.jpg"}
    win.search.setText("sun -beach")           # positive vetoed by folder
    assert _hits(win) == set()


def test_search_case_insensitive(search_library: Path) -> None:
    """Both positive and negative terms match case-insensitively against
    every field (filename, caption, keyword, folder)."""
    win = _search_win(search_library)

    win.search.setText("BEACH Golden")
    assert _hits(win) == {"sunset.jpg"}
    win.search.setText("OcEaN")
    assert _hits(win) == {"sunset.jpg"}
    win.search.setText("beach -DUNES")
    assert _hits(win) == {"sunset.jpg"}
    win.search.setText("OSAKA")
    assert _hits(win) == {"street.jpg"}


def test_search_degenerate_queries(search_library: Path) -> None:
    """Empty/whitespace queries and a lone '-' (a negation still being
    typed) fall back to the unfiltered All-photos view instead of blanking
    the grid; a trailing lone '-' inside a real query is simply ignored."""
    win = _search_win(search_library)
    all_names = {"sunset.jpg", "dunes.jpg", "market.jpg", "street.jpg"}

    for q in ("", "   ", "-", " - ", "- -"):
        win.search.setText("beach")            # a real filter first...
        win.search.setText(q)                  # ...then the degenerate query
        assert _hits(win) == all_names, repr(q)
        assert "All photos" in win.counts_label.text(), repr(q)

    win.search.setText("beach -")              # half-typed negation: ignored
    assert _hits(win) == {"sunset.jpg", "dunes.jpg"}
    assert "Search" in win.counts_label.text()
def test_grid_decodes_dpr_scaled_v2_level(tmp_path: Path) -> None:
    """fauxcasa-q7m: the grid's decode worker reads the v2 level chosen by the
    DPR-scaled native edge, then caps the tile to that edge. At native 256
    (devicePixelRatio 1) it reads the 256 level — the legacy primary, a no-op;
    at native 512 (a 2x display) it reads the 512 level so a hi-DPI tile is
    sharp. The cap holds the tile at exactly the device footprint."""
    import queue as _queue
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from grid import GridView

    root = tmp_path / "lib"
    _big_library(root)               # land.jpg 600x400 (idx 0), port.jpg (idx 1)
    cat = scan_library(root)
    v2 = thumbcache.load_cache(thumbcache.build_cache(
        cat, tmp_path / "c", levels=[512, 256, 128]).path)
    assert v2.levels == [512, 256, 128]

    app = QApplication.instance() or QApplication([])
    assert app is not None
    g = GridView()
    g.set_data(cat, v2)

    def decode(idx: int, native: int):
        g._tile_native = native
        g.generation += 1
        g.wanted = frozenset({idx})
        with g.pending_lock:
            g.pending.discard(idx)
        g.tiles.pop(idx, None)
        g._request(idx)              # a daemon worker decodes onto g.done
        for _ in range(200):         # bounded wait (~10s worst case)
            try:
                gen, di, img = g.done.get(timeout=0.05)
            except _queue.Empty:
                continue
            if gen == g.generation and di == idx:
                return img
        raise AssertionError("decode did not complete")

    # idx 0 == land.jpg 600x400: 512 level caps the long edge to 512x341, the
    # 256 level to 256x171. The long edge equals the chosen level -> proves
    # which level the worker read. rotate=0, so dims are not transposed.
    big = decode(0, 512)
    assert big is not None and max(big.width(), big.height()) == 512
    small = decode(0, 256)
    assert small is not None and max(small.width(), small.height()) == 256


def test_grid_v1_cache_falls_back_to_only_level(tmp_path: Path) -> None:
    """fauxcasa-q7m: a v1 cache has only the 256 level, so even a hi-DPI native
    edge (512) reads it via best_level's largest-available fallback — the grid
    never asks a v1 cache for a level it doesn't have, it just stays soft."""
    import queue as _queue
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from grid import GridView

    root = tmp_path / "lib"
    _big_library(root)
    cat = scan_library(root)
    v1 = thumbcache.load_cache(thumbcache.build_cache(cat, tmp_path / "c").path)
    assert v1.levels == [256]

    app = QApplication.instance() or QApplication([])
    g = GridView()
    g.set_data(cat, v1)
    g._tile_native = 512             # pretend a 2x display
    g.generation += 1
    g.wanted = frozenset({0})
    with g.pending_lock:
        g.pending.discard(0)
    g.tiles.pop(0, None)
    g._request(0)
    img = None
    for _ in range(200):
        try:
            gen, di, im = g.done.get(timeout=0.05)
        except _queue.Empty:
            continue
        if gen == g.generation and di == 0:
            img = im
            break
    assert img is not None and max(img.width(), img.height()) == 256


def test_refresh_tile_native_dpr_and_invalidation(monkeypatch) -> None:
    """fauxcasa-q7m: _refresh_tile_native scales the native edge by
    devicePixelRatio, floors at TILE_NATIVE, and invalidates (forcing a
    re-decode at the new level) ONLY when the edge changes — a steady ratio
    costs an int compare, moving to a 2x monitor drops stale 256 tiles."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import grid as gridmod
    from grid import GridView

    app = QApplication.instance() or QApplication([])
    assert app is not None
    g = GridView()

    monkeypatch.setattr(g, "devicePixelRatioF", lambda: 1.0)
    g._tile_native = 0               # force the first refresh to set it
    g._refresh_tile_native()
    assert g._tile_native == gridmod.TILE_NATIVE          # 256, the v1/dpr1 base

    # steady DPR -> no invalidation: a seeded tile and generation survive
    g.tiles[7] = [None, 0, 0]
    gen = g.generation
    g._refresh_tile_native()
    assert g._tile_native == gridmod.TILE_NATIVE
    assert g.generation == gen and 7 in g.tiles

    # move to a 2x display -> native 512, tiles invalidated for re-decode
    monkeypatch.setattr(g, "devicePixelRatioF", lambda: 2.0)
    g._refresh_tile_native()
    assert g._tile_native == 2 * gridmod.TILE_NATIVE      # 512
    assert g.generation == gen + 1 and 7 not in g.tiles

    # fractional ratio rounds; sub-1 (rare) stays floored at TILE_NATIVE
    monkeypatch.setattr(g, "devicePixelRatioF", lambda: 1.5)
    g._refresh_tile_native()
    assert g._tile_native == round(gridmod.TILE_NATIVE * 1.5)   # 384
    monkeypatch.setattr(g, "devicePixelRatioF", lambda: 0.5)
    g._refresh_tile_native()
    assert g._tile_native == gridmod.TILE_NATIVE          # never below the base


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# Grid multi-select (fauxcasa-q6l.1): selection set + current/anchor model,
# Ctrl/Shift click, Shift+arrow extension, Ctrl+A, Esc, signal payloads,
# multi-rect paint, and the selection-vs-filter/zoom behavior. First
# selection/keyboard coverage in the suite: mouse/key events are constructed
# directly and delivered to the widget handlers — offscreen-safe, no window
# activation or QTest focus dependence.
# ---------------------------------------------------------------------------


def _selection_grid(tmp_path: Path):
    """A shown offscreen GridView over a synthetic two-folder library
    (6 + 3 photos), no thumb cache (selection never needs decoded tiles),
    sized to exactly 2 columns so row geometry is deterministic. Display
    order is p00..p08 (sorted rel paths)."""
    _offscreen_app()
    from grid import GridView

    root = tmp_path / "lib"
    k = 0
    for fi, count in ((0, 6), (1, 3)):
        for _ in range(count):
            make_jpeg(root / f"f{fi}" / f"p{k:02d}.jpg")
            k += 1
    cat = scan_library(root)
    g = GridView()
    g.resize(400, 640)   # viewport ~398 -> (398-8)//168 = 2 columns
    g.show()             # hidden widgets keep a stale default viewport size
    g.set_data(cat, None)
    assert len(g.display) == 9 and g.cols == 2
    return g


def _click(g, idx: int, modifiers=None, double: bool = False) -> None:
    """Deliver a left-button press (or double-click) at the center of the
    tile for catalog index idx, in viewport coordinates."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    gi, n = g.loc[idx]
    r = g._item_rect(g.groups[gi], n)
    pos = QPointF(r.center().x(),
                  r.center().y() - g.verticalScrollBar().value())
    mods = (modifiers if modifiers is not None
            else Qt.KeyboardModifier.NoModifier)
    kind = (QEvent.Type.MouseButtonDblClick if double
            else QEvent.Type.MouseButtonPress)
    ev = QMouseEvent(kind, pos, pos, pos, Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton, mods)
    (g.mouseDoubleClickEvent if double else g.mousePressEvent)(ev)


def _key(g, key, modifiers=None) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    mods = (modifiers if modifiers is not None
            else Qt.KeyboardModifier.NoModifier)
    g.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods))


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


