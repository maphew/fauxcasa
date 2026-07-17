#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "PySide6", "pillow", "exiv2", "rawpy", "av"]
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
    save_catalog_retrying,
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
       suite behaved before the loupe tests added this much widget churn.

    3. Same discipline for ViewerPage (and its SlideshowPage subclass):
       quiesce() ages out and joins any in-flight original-decode /
       prefetch thread. The LAST navigation's loader still holds a VALID
       serial when the test ends, so without this it can emit into the
       widget deletion below — the same gfz access-violation family, seen
       on Windows once the slideshow tests added rapid-navigation churn."""
    yield
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    from grid import GridView
    from viewer import ViewerPage

    for w in app.allWidgets():
        if isinstance(w, GridView):
            w.stop()                       # retire pools before any deletion
        elif isinstance(w, ViewerPage):
            w.quiesce()                    # reap decode/prefetch workers
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


def test_save_catalog_retrying_succeeds_after_transient_errors(
        library: Path, tmp_path: Path) -> None:
    """save_catalog_retrying retries on OSError and returns on first success."""
    import unittest.mock as mock

    cat = scan_library(library)
    path = tmp_path / "catalog.json"
    call_count = 0

    original = __import__("catalog").save_catalog

    def flaky_save(catalog, p):
        nonlocal call_count
        call_count += 1
        if call_count < 3:  # fail twice, succeed on third
            raise OSError("transient sharing violation")
        original(catalog, p)

    with mock.patch("catalog.save_catalog", side_effect=flaky_save):
        # backoff=0 avoids real sleeps in tests
        save_catalog_retrying(cat, path, attempts=5, backoff=0)

    assert call_count == 3
    assert path.exists()
    assert load_catalog(path, library) is not None


def test_save_catalog_retrying_raises_after_all_attempts_exhausted(
        library: Path, tmp_path: Path) -> None:
    """save_catalog_retrying raises OSError when every attempt fails."""
    import unittest.mock as mock

    cat = scan_library(library)
    path = tmp_path / "catalog.json"
    sentinel = OSError("always broken")

    with mock.patch("catalog.save_catalog", side_effect=sentinel):
        with pytest.raises(OSError, match="always broken"):
            save_catalog_retrying(cat, path, attempts=3, backoff=0)

    assert not path.exists()  # nothing written on total failure


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


# ---- M1 slideshow: play button + full-screen timed loop (fauxcasa-q6l.3) --
# SlideshowPage rides ViewerPage's rendering (instant preview + async
# original) and adds the playback loop: timer advance with wrap-around,
# Space pause/resume, manual nav that keeps playing, Esc exit, and a
# dwell-time prefetch of the next original. MainWindow's ▶ Play action
# plays the CURRENT display set full-screen and Esc returns to exactly
# the prior grid/viewer state. All offscreen-safe: timers are driven with
# short test delays through processEvents, never wall-clock sleeps alone.


def _spin(app, cond, timeout_s: float = 8.0) -> bool:
    """Pump the event loop until cond() (timers fire through
    processEvents) or the deadline passes; returns the final cond()."""
    import time
    deadline = time.monotonic() + timeout_s
    while not cond() and time.monotonic() < deadline:
        app.processEvents()
    return cond()


def _pump(app, seconds: float) -> None:
    """Pump the event loop for a fixed interval (to show something does
    NOT happen, e.g. no advance while paused)."""
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()


def _press(widget, key) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    widget.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))


def _show_library(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        make_jpeg(root / "show" / name)
    return root


def test_slideshow_starts_fullscreen_and_plays(tmp_path: Path) -> None:
    """start() goes full-screen over the given display set at the given
    position, playing (timer running, not paused), with the transient
    control hint up."""
    app = _offscreen_app()
    from slideshow import SLIDE_DELAY_MS, SlideshowPage
    cat = scan_library(_show_library(tmp_path))
    show = SlideshowPage(cat, None, delay_ms=60_000)   # no advance in-test
    assert show._timer.interval() == 60_000            # test delay honored
    assert SlideshowPage(cat, None)._timer.interval() == SLIDE_DELAY_MS
    display = list(range(len(cat.photos)))
    show.start(display, 1)
    assert show.isFullScreen()
    assert show.display == display and show.pos == 1
    assert show._timer.isActive() and not show.paused
    assert show._hint_visible
    _ = app


def test_slideshow_advances_on_timer_and_wraps(tmp_path: Path) -> None:
    """The dwell timer steps through the display set in order and WRAPS at
    the end (the corpus documents no Picasa end-of-show behavior, so the
    loop is the chosen convention — slideshow.py module docstring)."""
    app = _offscreen_app()
    from slideshow import SlideshowPage
    cat = scan_library(_show_library(tmp_path))
    show = SlideshowPage(cat, None, delay_ms=20)
    display = list(range(len(cat.photos)))
    seen: list[int] = []
    show.photo_shown.connect(seen.append)
    show.start(display, 0)
    assert _spin(app, lambda: len(seen) >= 5), seen
    # initial show + 4 timer advances: a b c -> wrap -> a b
    assert seen[:5] == [display[0], display[1], display[2],
                        display[0], display[1]]


def test_slideshow_space_pauses_and_resumes(tmp_path: Path) -> None:
    """Space stops the dwell timer (no advance while paused); a second
    Space resumes and the show advances again."""
    app = _offscreen_app()
    from PySide6.QtCore import Qt
    from slideshow import SlideshowPage
    cat = scan_library(_show_library(tmp_path))
    show = SlideshowPage(cat, None, delay_ms=20)
    seen: list[int] = []
    show.photo_shown.connect(seen.append)
    show.start(list(range(len(cat.photos))), 0)
    _press(show, Qt.Key.Key_Space)
    assert show.paused and not show._timer.isActive()
    shown_while_paused = len(seen)
    _pump(app, 0.15)                     # several delays' worth of time
    assert len(seen) == shown_while_paused    # ...and no advance happened
    _press(show, Qt.Key.Key_Space)
    assert not show.paused and show._timer.isActive()
    assert _spin(app, lambda: len(seen) > shown_while_paused)


def test_slideshow_manual_nav_wraps_and_keeps_playing(tmp_path: Path) -> None:
    """Left/Right and J/K navigate with wrap-around WITHOUT stopping
    playback (the dwell restarts); while paused they navigate but stay
    paused."""
    app = _offscreen_app()
    from PySide6.QtCore import Qt
    from slideshow import SlideshowPage
    cat = scan_library(_show_library(tmp_path))
    show = SlideshowPage(cat, None, delay_ms=60_000)   # manual nav only
    show.start(list(range(len(cat.photos))), 0)
    last = len(cat.photos) - 1
    _press(show, Qt.Key.Key_Right)
    assert show.pos == 1 and show._timer.isActive() and not show.paused
    _press(show, Qt.Key.Key_J)                         # J = forward, as viewer
    assert show.pos == 2
    _press(show, Qt.Key.Key_Right)                     # wrap forward
    assert show.pos == 0
    _press(show, Qt.Key.Key_Left)                      # wrap backward
    assert show.pos == last
    _press(show, Qt.Key.Key_K)                         # K = back, as viewer
    assert show.pos == last - 1
    assert show._timer.isActive() and not show.paused  # still playing
    _press(show, Qt.Key.Key_Space)                     # pause...
    _press(show, Qt.Key.Key_Right)                     # ...nav while paused
    assert show.pos == last and show.paused
    assert not show._timer.isActive()                  # stays paused
    _ = app


def test_slideshow_esc_exits_and_stops(tmp_path: Path) -> None:
    """Esc stops the timer, hides the surface, and emits closed with the
    current catalog index (the ViewerPage closed contract)."""
    app = _offscreen_app()
    from PySide6.QtCore import Qt
    from slideshow import SlideshowPage
    cat = scan_library(_show_library(tmp_path))
    show = SlideshowPage(cat, None, delay_ms=60_000)
    closed: list[int] = []
    show.closed.connect(closed.append)
    display = list(range(len(cat.photos)))
    show.start(display, 2)
    _press(show, Qt.Key.Key_Escape)
    assert closed == [display[2]]
    assert not show._timer.isActive()
    assert show.isHidden()
    _ = app


def test_slideshow_prefetch_makes_advance_a_pure_swap(tmp_path: Path) -> None:
    """During the dwell the NEXT photo's original decodes off-thread; the
    advance then swaps it in instantly — image present, no loading state,
    no preview flash — instead of starting a fresh async load."""
    app = _offscreen_app()
    from PySide6.QtCore import Qt
    from slideshow import SlideshowPage
    cat = scan_library(_show_library(tmp_path))
    show = SlideshowPage(cat, None, delay_ms=60_000)   # advance manually
    display = list(range(len(cat.photos)))
    show.start(display, 0)

    def prefetched_next() -> bool:
        with show._prefetch_lock:
            got = show._prefetched
        return got is not None and got[0] == display[1] and got[1] is not None

    assert _spin(app, prefetched_next)
    _press(show, Qt.Key.Key_Right)
    # The swap is synchronous: the original is up BEFORE any event pumping.
    assert show.pos == 1
    assert show.image is not None and not show.image.isNull()
    assert not show.loading and show.preview is None


def test_slideshow_prefetch_failure_falls_back_to_async(
        tmp_path: Path) -> None:
    """An undecodable next photo is remembered as a FAILED prefetch (None);
    the advance then takes the normal ViewerPage async path and degrades to
    the viewer's could-not-decode state — never a crash, never a stall."""
    app = _offscreen_app()
    from PySide6.QtCore import Qt
    from slideshow import SlideshowPage
    root = tmp_path / "lib"
    make_jpeg(root / "show" / "a.jpg")
    (root / "show" / "bad.jpg").write_bytes(b"not a jpeg at all")
    make_jpeg(root / "show" / "c.jpg")
    cat = scan_library(root)
    by_rel = {p.rel: i for i, p in enumerate(cat.photos)}
    display = [by_rel["show/a.jpg"], by_rel["show/bad.jpg"],
               by_rel["show/c.jpg"]]
    show = SlideshowPage(cat, None, delay_ms=60_000)
    show.start(display, 0)
    assert _spin(app, lambda: show._prefetched is not None)
    assert show._prefetched == (display[1], None)      # tried, failed
    _press(show, Qt.Key.Key_Right)
    assert show.pos == 1 and show.image is None        # async path taken
    assert _spin(app, lambda: not show.loading)        # decode fails soft
    assert show.image is None                          # "could not decode"


def test_mainwindow_play_action_plays_current_view_and_esc_restores(
        library: Path) -> None:
    """The toolbar ▶ Play action starts a full-screen slideshow over the
    grid's CURRENT display set from the selected photo; Esc tears the
    surface down and the browser beneath is exactly as it was."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    display_before = list(win.grid.display)
    win.grid._select(display_before[1])                # current photo
    win.play_action.trigger()
    show = win._slideshow
    assert show is not None and show.isFullScreen()
    assert show.display == display_before              # the current view...
    assert show.pos == 1                               # ...from the selection
    assert show._timer.isActive()
    _press(show, Qt.Key.Key_Escape)
    assert show.isHidden() and not show._timer.isActive()    # stopped
    assert win.pages.currentWidget() is win.pages.widget(0)   # still browser
    assert list(win.grid.display) == display_before    # view untouched
    assert win.grid.current == display_before[1]       # selection untouched


def test_mainwindow_play_from_search_album_and_starred_views(
        library: Path) -> None:
    """Play acts on whatever the grid is showing: a search result set, an
    album's members, and the Starred set — not always All photos — and an
    EMPTY view is a no-op (no blank show, no crash)."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    def play_and_close() -> list[int]:
        win.play_action.trigger()
        assert win._slideshow is not None and win._slideshow.isFullScreen()
        shown = list(win._slideshow.display)
        _press(win._slideshow, Qt.Key.Key_Escape)
        assert win._slideshow.isHidden()
        return shown

    win.search.setText("beach")                        # a.jpg's caption
    filtered = list(win.grid.display)
    assert len(filtered) == 1
    assert play_and_close() == filtered
    assert win.search.text() == "beach"                # search state survives

    win.search.clear()
    uid = "deadbeefdeadbeefdeadbeefdeadbeef"
    win._apply_view("album", uid)                      # album view
    members = list(cat.albums[uid].members)
    assert list(win.grid.display) == members
    assert play_and_close() == members

    win._apply_view("starred", "")                     # starred view
    starred = list(win.grid.display)
    assert len(starred) == 1
    assert play_and_close() == starred

    win.search.setText("no-such-photo-anywhere")       # empty display set
    assert win.grid.display == []
    win.play_action.trigger()                          # no-op: nothing to show
    assert win._slideshow is None or win._slideshow.isHidden()


def test_mainwindow_play_from_viewer_returns_to_viewer(
        library: Path) -> None:
    """Play while the single-photo viewer is up starts at the viewer's
    photo, and Esc returns to the viewer page (the exact prior state), not
    the grid."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    display = list(win.grid.display)
    win._open_viewer(display[1], display, 1)
    assert win.pages.currentWidget() is win.viewer
    win.play_action.trigger()
    show = win._slideshow
    assert show is not None and show.pos == 1          # the viewer's photo
    _press(show, Qt.Key.Key_Escape)
    assert show.isHidden()
    assert win.pages.currentWidget() is win.viewer     # back to the viewer
    assert win.viewer.current_index() == display[1]    # on the same photo


def test_mainwindow_slideshow_surface_reused_and_repointed(
        library: Path, tmp_path: Path) -> None:
    """The slideshow surface is ONE lasting instance (never deleted
    mid-run — its decode threads must not race a widget teardown,
    fauxcasa-gfz): a later Play reuses it, re-pointed at the catalog/cache
    a reconcile swapped in; and a reconcile landing DURING a show closes
    it (its display indices belong to the old catalog, as with the viewer
    page)."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.play_action.trigger()
    first = win._slideshow
    assert first is not None and first.catalog is cat
    _press(first, Qt.Key.Key_Escape)

    cat2 = scan_library(library)                       # the reconcile swap
    cache2 = thumbcache.load_cache(
        thumbcache.build_cache(cat2, tmp_path / "c2").path)
    thumbcache.bind(cache2, cat2)
    win.reload_data(cat2, cache2)
    win.play_action.trigger()
    assert win._slideshow is first                     # reused, not recreated
    assert first.catalog is cat2 and first.thumbs is cache2   # re-pointed
    assert first.isFullScreen()

    cat3 = scan_library(library)                       # reconcile mid-show...
    win.reload_data(cat3, cache2)
    assert first.isHidden()                            # ...closes the show
    assert not first._timer.isActive()
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
    # Forward CLI args so `uv run test_tracer.py -k X -x` selects tests
    # instead of silently running the whole suite (fauxcasa-q6l.17 — this
    # cost four separate sessions a bisect detour before being fixed).
    # Default -v only when the caller passed nothing.
    sys.exit(pytest.main([__file__] + (sys.argv[1:] or ["-v"])))


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


# ---- faces/people read-only ingest + People surface -----------------------
# (fauxcasa-cam.1/.2/.3) Synthetic fixtures only (privacy rule): hand-authored
# inis + a hand-authored contacts.xml matching the oracle-014 grammar + tiny
# Qt-generated JPEGs — no real Picasa data.

@pytest.fixture()
def faces_library(tmp_path: Path) -> Path:
    """Faces fixture exercising the whole ingest matrix: a photo-less ROOT
    ini whose [Contacts2] flows downward; a Trip ini that re-names the
    ancestor's contact (nearest definition wins), defines a zero-padded
    short id and a name that conflicts with contacts.xml, and carries a
    legacy [Contacts] entry (web ids, never display names); an UNKNOWN
    (unconfirmed-suggestion) face; an orphan id nobody names; and a hidden
    photo so People counts prove they are visible-set-aware."""
    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "a.jpg")
    make_jpeg(root / "Trip" / "b.jpg")
    make_jpeg(root / "Picnic" / "c.jpg")
    (root / ".picasa.ini").write_text(
        "[Contacts2]\r\naaaaaaaaaaaaaaa1=Ada Ancestor;;\r\n")
    (root / "Trip" / ".picasa.ini").write_text(
        "[Contacts2]\r\n"
        "0632e71e2ffd6c6d=Bob Short;;\r\n"
        "cccccccccccccccc=Carol Ini;;\r\n"
        "aaaaaaaaaaaaaaa1=Ada Local;;\r\n"   # overrides the root's name here
        "[Contacts]\r\n"
        "dddddddddddddddd=someone_lh,4af3\r\n"
        "[a.jpg]\r\n"
        # short (%llx-stripped) id + inherited-and-overridden id + an
        # unconfirmed suggestion (UNKNOWN_CONTACT)
        "faces=rect64(4a8e8e6b),632e71e2ffd6c6d;"
        "rect64(3f845bcb59418507),aaaaaaaaaaaaaaa1;"
        "rect64(ff),ffffffffffffffff\r\n"
        "[b.jpg]\r\n"
        "hidden=yes\r\n"
        # xml-conflict id + an orphan nobody names + the legacy-[Contacts] id
        "faces=rect64(1234),cccccccccccccccc;"
        "rect64(5678),9999999999999999;"
        "rect64(9abc),dddddddddddddddd\r\n"
    )
    (root / "Picnic" / ".picasa.ini").write_text(
        "[c.jpg]\r\nfaces=rect64(2222),aaaaaaaaaaaaaaa1\r\n")
    return root


def _write_faces_contacts_xml(path: Path) -> Path:
    """A synthetic machine-local contacts.xml (oracle-014 grammar): names
    the ini-conflict contact (xml must win) and the legacy-[Contacts]-only
    contact (nameable ONLY via contacts.xml)."""
    path.write_text(
        '<contacts>\n'
        ' <contact id="cccccccccccccccc" name="Carol Xml" '
        'modified_time="2026-01-01T00:00:00-07:00" local_contact="1"/>\n'
        ' <contact id="dddddddddddddddd" name="Dave Legacy" '
        'modified_time="2026-01-01T00:00:00-07:00" local_contact="1"/>\n'
        '</contacts>\n')
    return path


def test_load_contacts_xml_defensive(tmp_path: Path) -> None:
    """load_contacts_xml: ids are zero-padded to 16 to join faces= /
    [Contacts2]; a nameless or bad-id entry skips that entry only; a
    missing or malformed FILE yields {} (fail-soft, never a gate)."""
    from catalog import load_contacts_xml

    p = tmp_path / "contacts.xml"
    p.write_text(
        '<contacts>'
        '<contact id="ca5c88ca60f42c0b" name="Pat One" local_contact="1"/>'
        '<contact id="632e71e2ffd6c6d" name="Pat Short"/>'
        '<contact id="ffffffffffffffff" name=""/>'
        '<contact name="No Id"/>'
        '<contact id="not-hex-at-all" name="Bad Id"/>'
        '</contacts>')
    assert load_contacts_xml(p) == {
        "ca5c88ca60f42c0b": "Pat One",
        "0632e71e2ffd6c6d": "Pat Short",   # short id joined padded
    }
    assert load_contacts_xml(tmp_path / "absent.xml") == {}
    garbage = tmp_path / "garbage.xml"
    garbage.write_bytes(b"\x00\x01 not xml at all")
    assert load_contacts_xml(garbage) == {}


def test_default_contacts_xml_discovery(monkeypatch, tmp_path: Path) -> None:
    """default_contacts_xml finds %LocalAppData%\\Google\\Picasa2\\contacts\\
    contacts.xml when it exists, and returns None (not a phantom path) when
    the env var or the file is absent."""
    from catalog import default_contacts_xml

    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert default_contacts_xml() is None
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_contacts_xml() is None   # dir exists, file absent
    p = tmp_path / "Google" / "Picasa2" / "contacts" / "contacts.xml"
    p.parent.mkdir(parents=True)
    p.write_text("<contacts/>")
    assert default_contacts_xml() == p


def test_scan_ingests_faces_regions_and_names(faces_library: Path) -> None:
    """faces= regions land on Photo.faces as (rect, padded id, name):
    rects decode per parse_rect64, short ids join [Contacts2] zero-padded,
    the nearest [Contacts2] definition wins over an ancestor's, an
    ancestor's table names faces in folders whose own ini has none
    (downward inheritance — including a photo-less root), and
    UNKNOWN_CONTACT / orphan ids stay unnamed (None). Legacy [Contacts]
    web-id values must never surface as person names."""
    import picasa_db

    cat = scan_library(faces_library)
    a = next(p for p in cat.photos if p.rel == "Trip/a.jpg")
    assert [cid for _r, cid, _n in a.faces] == [
        "0632e71e2ffd6c6d",       # zero-padded from the 15-char faces= id
        "aaaaaaaaaaaaaaa1",
        picasa_db.UNKNOWN_CONTACT,
    ]
    assert [n for _r, _c, n in a.faces] == ["Bob Short", "Ada Local", None]
    assert a.faces[0][0] == picasa_db.parse_rect64("4a8e8e6b")
    assert a.faces[1][0] == picasa_db.parse_rect64("3f845bcb59418507")

    # hidden photos still carry their faces (reveal mode needs them)
    b = next(p for p in cat.photos if p.rel == "Trip/b.jpg")
    assert not b.visible
    assert [n for _r, _c, n in b.faces] == ["Carol Ini", None, None]
    assert "someone_lh,4af3" not in [n for _r, _c, n in b.faces]

    # downward inheritance: Picnic's ini has no [Contacts2]; the ROOT ini
    # (a folder the walk never visits — it holds no photos) names the face.
    c = next(p for p in cat.photos if p.rel == "Picnic/c.jpg")
    assert c.faces == (
        (picasa_db.parse_rect64("2222"), "aaaaaaaaaaaaaaa1", "Ada Ancestor"),
    )

    # the flat registry carries the harvested names
    assert cat.contacts["0632e71e2ffd6c6d"] == "Bob Short"
    assert cat.contacts["cccccccccccccccc"] == "Carol Ini"
    assert "dddddddddddddddd" not in cat.contacts  # [Contacts] has no names
    assert picasa_db.UNKNOWN_CONTACT not in cat.contacts


def test_contacts_xml_wins_name_conflicts(
        faces_library: Path, tmp_path: Path) -> None:
    """spec par.4 precedence: contacts.xml beats [Contacts2] on a name conflict,
    and it is the ONLY namer of a legacy-[Contacts] id; ini-only names
    survive untouched."""
    from catalog import load_contacts_xml

    contacts = load_contacts_xml(
        _write_faces_contacts_xml(tmp_path / "contacts.xml"))
    cat = scan_library(faces_library, None, contacts)

    b = next(p for p in cat.photos if p.rel == "Trip/b.jpg")
    assert [n for _r, _c, n in b.faces] == [
        "Carol Xml",     # xml wins over the ini's 'Carol Ini'
        None,            # orphan: no source names it
        "Dave Legacy",   # nameable only via contacts.xml
    ]
    a = next(p for p in cat.photos if p.rel == "Trip/a.jpg")
    assert a.faces[0][2] == "Bob Short"   # ini-only name unaffected

    assert cat.contacts["cccccccccccccccc"] == "Carol Xml"
    assert cat.contacts["dddddddddddddddd"] == "Dave Legacy"


def test_faces_catalog_roundtrip(faces_library: Path, tmp_path: Path) -> None:
    """Faces + the contact registry survive the persisted catalog
    (CATALOG_VERSION 4): a warm load reproduces every Photo.faces tuple —
    rect fractions are n/65536, exact in JSON — and Catalog.contacts."""
    from catalog import load_contacts_xml

    contacts = load_contacts_xml(
        _write_faces_contacts_xml(tmp_path / "contacts.xml"))
    cat = scan_library(faces_library, None, contacts)
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    loaded = load_catalog(path, faces_library)
    assert loaded is not None
    assert [p.faces for p in loaded.photos] == [p.faces for p in cat.photos]
    assert loaded.contacts == cat.contacts
    assert any(p.faces for p in loaded.photos)  # the fixture isn't vacuous


def test_people_sidebar_filters_and_unnamed(faces_library: Path) -> None:
    """The People sidebar section lists named people with live photo counts
    (visible set only until reveal), clicking one filters the grid exactly
    like an album, and the explicit 'Unnamed faces' affordance surfaces
    photos with suggested/unresolved regions (N7 spirit)."""
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

    cat = scan_library(faces_library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    # named people with counts; hidden b.jpg's Carol is NOT listed yet
    assert item_for(win, "person", "Ada Local").text(0).endswith("(1)")
    assert item_for(win, "person", "Ada Ancestor").text(0).endswith("(1)")
    assert item_for(win, "person", "Bob Short").text(0).endswith("(1)")
    assert item_for(win, "person", "Carol Ini") is None

    # clicking a person filters the grid like an album does
    win._sidebar_clicked(item_for(win, "person", "Ada Ancestor"), 0)
    assert win.grid.filter_label == "Ada Ancestor"
    assert [cat.photos[i].rel for i in win.grid.display] == ["Picnic/c.jpg"]
    assert "Person “Ada Ancestor”: 1 photos" in win.counts_label.text()

    # unnamed-faces affordance: only visible a.jpg (its UNKNOWN face) so far
    unnamed = item_for(win, "unnamed", "")
    assert unnamed is not None and unnamed.text(0).endswith("(1)")
    win._sidebar_clicked(unnamed, 0)
    assert win.grid.filter_label == "Unnamed faces"
    assert [cat.photos[i].rel for i in win.grid.display] == ["Trip/a.jpg"]

    # live counts: reveal surfaces hidden b.jpg — Carol appears, the
    # unnamed tally grows, and the preserved Unnamed view is recomputed
    win.reveal_box.setChecked(True)
    assert item_for(win, "person", "Carol Ini").text(0).endswith("(1)")
    assert item_for(win, "unnamed", "").text(0).endswith("(2)")
    assert win.grid.filter_label == "Unnamed faces"
    assert sorted(cat.photos[i].rel for i in win.grid.display) == [
        "Trip/a.jpg", "Trip/b.jpg"]

    win.reveal_box.setChecked(False)
    assert item_for(win, "person", "Carol Ini") is None
    assert item_for(win, "unnamed", "").text(0).endswith("(1)")


def test_search_matches_person_names(faces_library: Path) -> None:
    """Person names join the search predicate (spec par.5): a name fragment
    finds every visible photo with a matching named face; hidden photos'
    faces stay out until reveal; no false hits."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    cat = scan_library(faces_library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    win.search.setText("ada")            # Ada Local (a.jpg) + Ada Ancestor (c)
    assert sorted(cat.photos[i].rel for i in win.grid.display) == [
        "Picnic/c.jpg", "Trip/a.jpg"]
    win.search.setText("bob short")
    assert [cat.photos[i].rel for i in win.grid.display] == ["Trip/a.jpg"]
    win.search.setText("carol")          # only on hidden b.jpg
    assert win.grid.display == []
    win.reveal_box.setChecked(True)      # reveal recomputes the search view
    assert [cat.photos[i].rel for i in win.grid.display] == ["Trip/b.jpg"]
    win.reveal_box.setChecked(False)
    win.search.setText("nobody-here")
    assert win.grid.display == []


# ---------------------------------------------------------------------------
# M1 browse: the Recently Updated auto-collection (fauxcasa-q6l.7) and the
# jump-to-folder/end buttons beside the grid scrollbar (fauxcasa-q6l.9).
# Recency = FILE MTIME (the read-only app's honest proxy for "updated");
# semantics live on main.recent_indices. Jump stepping uses the grid's
# group y-offsets: prev from mid-group snaps to the CURRENT group's top
# first (Picasa behavior), then to the prior group.
# ---------------------------------------------------------------------------


def _days_ago(n: float) -> float:
    import time
    return time.time() - n * 86400


def test_recent_indices_mtime_window_from_disk(tmp_path: Path) -> None:
    """End to end: os.utime sets controlled file mtimes, the indexer
    (build_cache) captures them into Photo.mtime, and recent_indices
    selects exactly the photos modified within the RECENT_DAYS window —
    hidden ones only under reveal."""
    from main import recent_indices

    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "fresh.jpg")
    make_jpeg(root / "Trip" / "stale.jpg")
    make_jpeg(root / "Picnic" / "fresh2.jpg")
    make_jpeg(root / "Picnic" / "secret.jpg")
    (root / "Picnic" / ".picasa.ini").write_text(
        "[secret.jpg]\r\nhidden=yes\r\n")
    os.utime(root / "Trip" / "fresh.jpg", (_days_ago(1),) * 2)
    os.utime(root / "Trip" / "stale.jpg", (_days_ago(90),) * 2)
    os.utime(root / "Picnic" / "fresh2.jpg", (_days_ago(2),) * 2)
    os.utime(root / "Picnic" / "secret.jpg", (_days_ago(3),) * 2)

    cat = scan_library(root)
    assert all(p.mtime < 0 for p in cat.photos)   # unindexed: no signal yet
    assert recent_indices(cat, reveal=False) == []  # ...and honestly empty
    assert thumbcache.build_cache(cat, tmp_path / "cache") is not None

    rels = lambda idxs: sorted(cat.photos[i].rel for i in idxs)  # noqa: E731
    assert rels(recent_indices(cat, reveal=False)) == [
        "Picnic/fresh2.jpg", "Trip/fresh.jpg"]
    assert rels(recent_indices(cat, reveal=True)) == [
        "Picnic/fresh2.jpg", "Picnic/secret.jpg", "Trip/fresh.jpg"]


def test_recent_indices_empty_window_falls_back_to_most_recent_k(
        monkeypatch, tmp_path: Path) -> None:
    """A library untouched for months still gets a useful collection: with
    nothing inside the window, the K most recently modified photos stand
    in (catalog order), never photos without an mtime signal."""
    import main as main_mod
    from main import recent_indices

    root = tmp_path / "lib"
    for n in range(4):
        make_jpeg(root / "Old" / f"p{n}.jpg")
    cat = scan_library(root)
    # Direct signal injection (the disk->mtime path is covered above):
    # all far older than RECENT_DAYS, distinct, newest NOT in index order.
    for p, days in zip(cat.photos, (90, 40, 70, 60)):
        p.mtime = int(_days_ago(days))

    monkeypatch.setattr(main_mod, "RECENT_FALLBACK_K", 2)
    got = recent_indices(cat, reveal=False)
    assert got == sorted(got)                       # catalog order
    assert sorted(cat.photos[i].rel for i in got) == [
        "Old/p1.jpg", "Old/p3.jpg"]                 # the 2 newest by mtime
    # An entirely unindexed catalog (mtime -1 everywhere, e.g. adopt-mode)
    # yields an honest empty set even through the fallback.
    for p in cat.photos:
        p.mtime = -1
    assert recent_indices(cat, reveal=False) == []


def test_recent_sidebar_item_click_filters_and_counts(tmp_path: Path) -> None:
    """The sidebar's Recently Updated item carries a live count and clicking
    it filters the grid via set_filter, like Starred; reveal recomputes the
    view in place (x1l) and _refresh_recent_count updates the label once a
    cold build fills mtimes in."""
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

    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "fresh.jpg")
    make_jpeg(root / "Trip" / "stale.jpg")
    make_jpeg(root / "Trip" / "secret.jpg")
    (root / "Trip" / ".picasa.ini").write_text(
        "[secret.jpg]\r\nhidden=yes\r\n")
    cat = scan_library(root)
    by_rel = {p.rel: p for p in cat.photos}
    by_rel["Trip/fresh.jpg"].mtime = int(_days_ago(1))
    by_rel["Trip/stale.jpg"].mtime = int(_days_ago(90))
    by_rel["Trip/secret.jpg"].mtime = int(_days_ago(2))

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    item = item_for(win, "recent", "")
    assert item is not None and item.text(0).endswith("(1)")

    win._sidebar_clicked(item, 0)
    assert win.grid.filter_label == "Recently Updated"
    assert [cat.photos[i].rel for i in win.grid.display] == ["Trip/fresh.jpg"]

    # Reveal keeps the active view and recomputes it: the hidden-but-recent
    # photo joins; the rebuilt sidebar's count text follows.
    win.reveal_box.setChecked(True)
    assert win.grid.filter_label == "Recently Updated"
    assert sorted(cat.photos[i].rel for i in win.grid.display) == [
        "Trip/fresh.jpg", "Trip/secret.jpg"]
    assert item_for(win, "recent", "").text(0).endswith("(2)")
    win.reveal_box.setChecked(False)
    assert [cat.photos[i].rel for i in win.grid.display] == ["Trip/fresh.jpg"]

    # Cold-build label refresh: a catalog indexed AFTER the sidebar was
    # built (counts read as-of-construction) gets its count fixed in place —
    # setText on the live item, never a clear()+repopulate (fauxcasa-gfz).
    by_rel["Trip/stale.jpg"].mtime = int(_days_ago(0.5))
    win._refresh_recent_count()
    assert item_for(win, "recent", "").text(0).endswith("(2)")


def _jump_grid(tmp_path: Path):
    """A shown offscreen GridView over a 3-folder library (8 photos each),
    2 columns, viewport far shorter than the content so every jump has
    room to move. Returns the grid; group tops are read from g.groups."""
    _offscreen_app()
    from grid import GridView

    root = tmp_path / "lib"
    k = 0
    for fi in range(3):
        for _ in range(8):
            make_jpeg(root / f"f{fi}" / f"p{k:02d}.jpg")
            k += 1
    cat = scan_library(root)
    g = GridView()
    g.resize(400, 300)
    g.show()
    g.set_data(cat, None)
    assert len(g.groups) == 3 and g.content_h > g.viewport().height()
    assert g.verticalScrollBar().maximum() > g.groups[2].y
    return g


def test_grid_current_group_index_bisect(tmp_path: Path) -> None:
    g = _jump_grid(tmp_path)
    sb = g.verticalScrollBar()
    tops = [grp.y for grp in g.groups]
    assert tops[0] == 0 and tops[0] < tops[1] < tops[2]
    for gi, top in enumerate(tops):
        sb.setValue(top)                      # exactly at a header
        assert g.current_group_index() == gi
        sb.setValue(top + 5)                  # a bit into the group
        assert g.current_group_index() == gi
    sb.setValue(tops[1] - 1)                  # last row of the group before
    assert g.current_group_index() == 0


def test_grid_jump_folder_stepping_across_boundaries(tmp_path: Path) -> None:
    """next walks group top -> group top; prev from MID-group first snaps
    to the current group's own top (Picasa behavior), THEN to the prior
    group; both are no-ops at their respective ends."""
    g = _jump_grid(tmp_path)
    sb = g.verticalScrollBar()
    tops = [grp.y for grp in g.groups]

    sb.setValue(0)
    g.jump_next_folder()
    assert sb.value() == tops[1]
    g.jump_next_folder()
    assert sb.value() == tops[2]
    g.jump_next_folder()                      # inside the last group: no-op
    assert sb.value() == tops[2]

    sb.setValue(tops[2] + 40)                 # mid-group...
    g.jump_prev_folder()
    assert sb.value() == tops[2]              # ...its own top first
    g.jump_prev_folder()
    assert sb.value() == tops[1]              # then the prior group
    g.jump_prev_folder()
    assert sb.value() == tops[0] == 0
    g.jump_prev_folder()                      # top of the first group: no-op
    assert sb.value() == 0


def test_grid_jump_top_end_and_buttons(tmp_path: Path) -> None:
    """jump_to_end/top hit the scrollbar's extremes, and the scrollbar-side
    button cluster drives the same primitives (clicked wiring)."""
    g = _jump_grid(tmp_path)
    sb = g.verticalScrollBar()
    tops = [grp.y for grp in g.groups]

    g.jump_to_end()
    assert sb.value() == sb.maximum() > 0
    g.jump_to_top()
    assert sb.value() == 0

    g.btn_end.click()
    assert sb.value() == sb.maximum()
    g.btn_prev_folder.click()                 # end is mid-last-group here
    assert sb.value() == tops[2]
    g.btn_next_folder.click()                 # no next group: no-op
    assert sb.value() == tops[2]
    g.btn_top.click()
    assert sb.value() == 0
    g.btn_next_folder.click()
    assert sb.value() == tops[1]
    # The cluster must never steal the grid's keyboard focus (triage keys).
    from PySide6.QtCore import Qt as _Qt
    for b in (g.btn_top, g.btn_prev_folder, g.btn_next_folder, g.btn_end):
        assert b.focusPolicy() == _Qt.FocusPolicy.NoFocus


# ---------------------------------------------------------------------------
# The two N4 exceptions (fauxcasa-q6l.4/.5): the viewer's explicit fit <-> 1:1
# zoom toggle + pan, and the grid's Ctrl+Alt hover full-screen peek. Bindings
# follow the Picasa shortcut corpus (docs/research/sources/picasaresources/
# keyboard-shortcuts.md): `1` toggles 100% zoom (plus conflict-free
# Ctrl+Alt+0, plus click anchored at the click point); "Hover over a photo
# and use Ctrl-Alt" shows the full-screen preview. All offscreen-safe: mouse
# and key events are constructed and delivered to the widget handlers
# directly; DPR paths force the ratio via monkeypatch.
# ---------------------------------------------------------------------------


def _viewer_with_original(tmp_path: Path, w: int = 2560, h: int = 1600):
    """A 1280x800 ViewerPage showing photo 0 with a decoded `w`x`h` original
    already landed (via _on_loaded, no thread) — the common zoom case."""
    _offscreen_app()
    from PySide6.QtGui import QImage
    from viewer import ViewerPage
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "b.jpg")
    cat = scan_library(root)
    v = ViewerPage(cat, None)
    v.resize(1280, 800)
    v.show_photo(list(range(len(cat.photos))), 0)
    orig = QImage(w, h, QImage.Format.Format_RGB32)
    orig.fill(0x336699)
    v._on_loaded(v._serial, orig)
    assert v.image is orig and not v.zoomed
    return v, orig


def _mouse(widget, kind, x: float, y: float,
           button=None, modifiers=None) -> None:
    """Deliver a synthetic left-button mouse event to the widget handlers
    (press/move/release), offscreen-safe."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    pos = QPointF(x, y)
    btn = button if button is not None else Qt.MouseButton.LeftButton
    mods = (modifiers if modifiers is not None
            else Qt.KeyboardModifier.NoModifier)
    ev = QMouseEvent(kind, pos, pos, pos, btn, btn, mods)
    if kind == QEvent.Type.MouseButtonPress:
        widget.mousePressEvent(ev)
    elif kind == QEvent.Type.MouseMove:
        widget.mouseMoveEvent(ev)
    else:
        widget.mouseReleaseEvent(ev)


def test_viewer_zoom_rect_dpr_and_pan_clamping() -> None:
    """The pure 1:1 geometry: one image pixel per DEVICE pixel (logical size
    = src/dpr, so dpr 2 halves the logical rect), pan clamped so background
    never shows past an edge, and an image smaller than the box centers
    regardless of the pan state."""
    _offscreen_app()
    from viewer import ViewerPage
    zr = ViewerPage._zoom_rect
    from PySide6.QtCore import QRect
    # dpr 1, centered: 2560x1600 in 1280x800 hangs half out on every side
    assert zr(1280, 800, 2560, 1600, 1.0, 0.5, 0.5) == \
        QRect(-640, -400, 2560, 1600)
    # pan clamps: fractional centers past the ends pin the matching edge
    assert zr(1280, 800, 2560, 1600, 1.0, 0.0, 0.0) == \
        QRect(0, 0, 2560, 1600)               # top-left pinned
    assert zr(1280, 800, 2560, 1600, 1.0, 1.0, 1.0) == \
        QRect(-1280, -800, 2560, 1600)        # bottom-right pinned
    assert zr(1280, 800, 2560, 1600, 1.0, -9.0, 99.0) == \
        QRect(0, -800, 2560, 1600)            # wild pans still clamp
    # dpr 2: the SAME source covers half the logical px (native device px)
    assert zr(1280, 800, 2560, 1600, 2.0, 0.5, 0.5) == \
        QRect(0, 0, 1280, 800)
    # smaller than the box: centered, pan has no freedom
    assert zr(1280, 800, 600, 400, 1.0, 0.9, 0.1) == QRect(340, 200, 600, 400)
    # degenerate 1x1 never yields a zero-size rect
    assert zr(1280, 800, 1, 1, 3.0, 0.5, 0.5).width() == 1


def test_viewer_zoom_key_toggles_and_arrows_still_navigate(
        tmp_path: Path) -> None:
    """`1` (Picasa Photo Viewer's 100% toggle) and Ctrl+Alt+0 both toggle
    fit <-> 1:1; PLAIN arrows keep meaning next/prev even while zoomed (the
    triage loop owns them), and the photo change resets the zoom to fit."""
    from PySide6.QtCore import Qt
    v, _ = _viewer_with_original(tmp_path)
    _press(v, Qt.Key.Key_1)
    assert v.zoomed
    _press(v, Qt.Key.Key_1)
    assert not v.zoomed
    _key(v, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier
         | Qt.KeyboardModifier.AltModifier)
    assert v.zoomed
    _press(v, Qt.Key.Key_Right)               # plain arrow: NAVIGATES
    assert v.pos == 1
    assert not v.zoomed                        # ...and the zoom reset to fit
    # a modified 1 (future star-set chords etc.) does NOT toggle
    _key(v, Qt.Key.Key_1, Qt.KeyboardModifier.ControlModifier)
    assert not v.zoomed


def test_viewer_zoom_click_anchor_stays_put(tmp_path: Path,
                                            monkeypatch) -> None:
    """Click-to-zoom keeps the clicked image point PUT under the cursor: the
    image pixel under (ax, ay) at fit paints at (ax, ay) at 1:1. A second
    click returns to fit."""
    from PySide6.QtCore import QEvent
    v, orig = _viewer_with_original(tmp_path)
    monkeypatch.setattr(v, "devicePixelRatioF", lambda: 1.0)
    ax, ay = 900.0, 300.0
    _mouse(v, QEvent.Type.MouseButtonPress, ax, ay)
    _mouse(v, QEvent.Type.MouseButtonRelease, ax, ay)
    assert v.zoomed
    fit = v._display_rect(1280, 800, orig.width(), orig.height(), cap=True)
    u_img = (ax - fit.x()) / fit.width() * orig.width()
    v_img = (ay - fit.y()) / fit.height() * orig.height()
    z = v._shown_rect(1280, 800, orig)
    assert z.size().width() == orig.width()      # 1:1 at dpr 1
    assert abs(z.x() + u_img * z.width() / orig.width() - ax) <= 1.0
    assert abs(z.y() + v_img * z.height() / orig.height() - ay) <= 1.0
    _mouse(v, QEvent.Type.MouseButtonPress, ax, ay)
    _mouse(v, QEvent.Type.MouseButtonRelease, ax, ay)
    assert not v.zoomed                          # click toggles back to fit


def test_viewer_zoom_drag_pans_and_release_does_not_toggle(
        tmp_path: Path, monkeypatch) -> None:
    """While at 1:1 a drag pans (the photo follows the cursor) and its
    release is NOT a click — the zoom stays on; a fling past the edge clamps
    (background never shows, and no dead travel is left to wind back
    through); Ctrl+arrows pan a quarter-viewport without navigating."""
    from PySide6.QtCore import QEvent, Qt
    v, orig = _viewer_with_original(tmp_path)
    monkeypatch.setattr(v, "devicePixelRatioF", lambda: 1.0)
    v.toggle_zoom()                              # center: rect at (-640,-400)
    assert v._shown_rect(1280, 800, orig).x() == -640
    _mouse(v, QEvent.Type.MouseButtonPress, 600, 400)
    _mouse(v, QEvent.Type.MouseMove, 500, 350)   # drag left/up 100/50
    _mouse(v, QEvent.Type.MouseButtonRelease, 500, 350)
    assert v.zoomed                              # a drag never toggles
    z = v._shown_rect(1280, 800, orig)
    assert (z.x(), z.y()) == (-740, -450)        # photo moved with the cursor
    _mouse(v, QEvent.Type.MouseButtonPress, 600, 400)
    _mouse(v, QEvent.Type.MouseMove, 9000, 400)  # fling far right
    _mouse(v, QEvent.Type.MouseButtonRelease, 9000, 400)
    assert v._shown_rect(1280, 800, orig).x() == 0   # clamped at the edge
    before = v._shown_rect(1280, 800, orig).x()
    pos_before = v.pos
    _key(v, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)
    assert v.pos == pos_before                   # pan, not navigation
    assert v._shown_rect(1280, 800, orig).x() == before - 1280 // 4


def test_viewer_zoom_resets_on_photo_change_and_show(tmp_path: Path) -> None:
    """Zoom state is per-photo-shown (Picasa behavior): _step and a fresh
    show_photo both land at fit with a centered pan."""
    v, _ = _viewer_with_original(tmp_path)
    v.toggle_zoom()
    v._pan_by(-300, -200)
    assert v.zoomed and v._zoom_cx != 0.5
    v.show_photo(v.display, 1)
    assert not v.zoomed and v._zoom_cx == 0.5 and v._zoom_cy == 0.5


def test_viewer_zoom_dpr_and_preview_standin(tmp_path: Path,
                                             monkeypatch) -> None:
    """The instance path is devicePixelRatio-correct (a forced dpr 2 halves
    the logical 1:1 rect: native DEVICE pixels, not a 2x blowup), and before
    the original lands the zoomed paint shows the cached preview at ITS own
    pixels — "paint whatever is available" — centered when smaller than the
    viewport, and still paints cleanly (grab)."""
    _offscreen_app()
    from viewer import ViewerPage
    root = tmp_path / "lib"
    _big_library(root)
    cat, cache = _bound_cache(tmp_path, root, levels=[512, 256])
    v = ViewerPage(cat, cache)
    v.resize(1280, 800)
    monkeypatch.setattr(v, "devicePixelRatioF", lambda: 2.0)
    v.show_photo(list(range(cache.count)), 0)
    assert v.preview is not None and v.image is None   # original still async
    v.toggle_zoom()
    z = v._shown_rect(1280, 800, v.preview)
    # preview at its own native device px: logical size = preview/2, centered
    assert z.width() == max(1, round(v.preview.width() / 2.0))
    assert z.x() == (1280 - z.width()) // 2
    assert not v.grab().isNull()                       # zoomed paint is clean
    from PySide6.QtGui import QImage
    orig = QImage(2560, 1600, QImage.Format.Format_RGB32)
    orig.fill(0x224466)
    v._on_loaded(v._serial, orig)                      # the original lands...
    assert v.zoomed                                    # ...zoom holds, and
    z2 = v._shown_rect(1280, 800, orig)                # deepens to true 1:1
    assert z2.width() == 1280                          # 2560 px at dpr 2
    v.quiesce()                                        # reap the decode worker
    assert v._decoder is None or not v._decoder.is_alive()


# -- the hover peek trigger state machine in the grid (fauxcasa-q6l.5) and
# the frameless full-screen surface MainWindow drives from it (peek.py) --


def _peek_move(g, idx: int | None, mods) -> None:
    """Deliver a button-free mouse move at the center of `idx`'s tile
    (or the top-left header/padding band for idx=None: no photo there)."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    if idx is None:
        pos = QPointF(2.0, 2.0)
    else:
        gi, n = g.loc[idx]
        r = g._item_rect(g.groups[gi], n)
        pos = QPointF(r.center().x(),
                      r.center().y() - g.verticalScrollBar().value())
    ev = QMouseEvent(QEvent.Type.MouseMove, pos, pos, pos,
                     Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, mods)
    g.mouseMoveEvent(ev)


def _key_up(g, key, modifiers=None) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    mods = (modifiers if modifiers is not None
            else Qt.KeyboardModifier.NoModifier)
    g.keyReleaseEvent(QKeyEvent(QEvent.Type.KeyRelease, key, mods))


def _peek_probes(g):
    """(requested, released) recorders wired to the grid's peek signals."""
    req: list[int] = []
    rel: list[bool] = []
    g.peek_requested.connect(req.append)
    g.peek_released.connect(lambda: rel.append(True))
    return req, rel


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
# In-file capture date / GPS / XMP Rating via metareader — the exiv2
# bytes-mode seam (fauxcasa-cam.9/.10/.11). Fixtures are synthesized through
# metareader's OWN test-support writer (embed_test_metadata) so every exiv2
# call in the repo stays inside that one module (the library-swap seam);
# pixels are flat synthetic fills, metadata invented — privacy-safe.
# ---------------------------------------------------------------------------

import metareader  # noqa: E402

# Whitehorse YT — the western longitude exercises the hemisphere sign, and
# both coordinates convert to EXIF d/m/s rationals exactly (spike truth).
WHITEHORSE = (60.72125, -135.05685)
SYDNEY = (-33.8568, 151.2153)  # southern lat: the other sign branch


def _meta_jpeg(path: Path | None = None, **meta) -> bytes:
    """JPEG bytes carrying exactly the given in-file metadata; also written
    to `path` when given (embed_test_metadata raises on failure — a broken
    fixture must fail loudly)."""
    data = metareader.embed_test_metadata(_jpeg_bytes(), **meta)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return data


def test_metareader_reads_date_gps_rating() -> None:
    """The three fields off one JPEG's bytes — and the date is pre-1903
    (footgun 16: scanned photos predate Picasa's UI floor; no year floor
    here, ever)."""
    fm = metareader.read_file_meta(_meta_jpeg(
        date_time_original="1899:03:02 14:00:00",
        gps=WHITEHORSE, rating=3))
    assert fm.date_taken == "1899-03-02T14:00:00"
    assert fm.gps == pytest.approx(WHITEHORSE)
    assert fm.rating == 3
    # southern/eastern signs too
    fm2 = metareader.read_file_meta(_meta_jpeg(gps=SYDNEY))
    assert fm2.gps == pytest.approx(SYDNEY)


def test_metareader_datetime_fallback_and_garbage() -> None:
    """DateTimeOriginal wins; Exif.Image.DateTime is the fallback; the
    all-zeros camera placeholder and free-text garbage read as None."""
    both = _meta_jpeg(date_time_original="2009:07:04 13:00:00",
                      date_time="2020:01:01 00:00:00")
    assert metareader.read_file_meta(both).date_taken == "2009-07-04T13:00:00"
    only_fallback = _meta_jpeg(date_time="2020:01:01 08:30:59")
    assert metareader.read_file_meta(only_fallback).date_taken == \
        "2020-01-01T08:30:59"
    zeros = _meta_jpeg(date_time_original="0000:00:00 00:00:00")
    assert metareader.read_file_meta(zeros).date_taken is None
    junk = _meta_jpeg(date_time_original="not a date")
    assert metareader.read_file_meta(junk).date_taken is None
    absent = _meta_jpeg(rating=1)  # no date fields at all
    assert metareader.read_file_meta(absent).date_taken is None


def test_metareader_rating_clamps_to_0_5() -> None:
    """xmp:Rating -> int clamped into the §3 star model: out-of-range
    values clamp (7 -> 5; XMP's -1 'rejected' -> 0 until the M2
    reverse-star work owns it); an explicit 0 is 0, not None; absent is
    None (no Rating in the packet at all)."""
    assert metareader.read_file_meta(_meta_jpeg(rating=7)).rating == 5
    assert metareader.read_file_meta(_meta_jpeg(rating=-1)).rating == 0
    assert metareader.read_file_meta(_meta_jpeg(rating=0)).rating == 0
    assert metareader.read_file_meta(_meta_jpeg(rating="3.0")).rating == 3
    assert metareader.read_file_meta(_jpeg_bytes()).rating is None


def test_metareader_fail_soft_on_garbage_bytes() -> None:
    """The fail-soft contract: hostile/degenerate bytes yield all-None,
    never an exception (one corrupt photo must not abort an index)."""
    empty = metareader.FileMeta()
    assert metareader.read_file_meta(b"") == empty
    assert metareader.read_file_meta(b"garbage" * 1000) == empty
    good = _meta_jpeg(date_time_original="2009:07:04 13:00:00", rating=4)
    assert metareader.read_file_meta(good[:40]) == empty  # truncated
    assert metareader.read_file_meta(good) != empty  # sanity: intact reads


def test_metareader_non_jpeg_carriers() -> None:
    """metareader is bytes-in, container-sniffing: PNG and WebP carriers
    (two of the §4 non-JPEG homes) read the same fields back."""
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    img = QImage(40, 30, QImage.Format.Format_RGB32)
    img.fill(QColor(50, 90, 130))
    for fmt in ("PNG", "WEBP"):
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        if not img.save(buf, fmt):
            continue  # a Qt build without this plugin: skip the carrier
        data = metareader.embed_test_metadata(
            bytes(buf.data()),
            date_time_original="2015:03:15 09:30:00", rating=2)
        fm = metareader.read_file_meta(data)
        assert fm.date_taken == "2015-03-15T09:30:00", fmt
        assert fm.rating == 2, fmt


def test_scan_ini_geotag_and_star_count(tmp_path: Path) -> None:
    """scan_library fills geotag from the ini geotag=lat,lon key (the
    non-EXIF source) fail-soft per line, and star=yes imports as exactly
    1 star (§3: legacy star=yes -> 1)."""
    root = tmp_path / "lib"
    for name in ("a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg"):
        make_jpeg(root / "f" / name)
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\ngeotag=-33.856800,151.215300\r\nstar=yes\r\n"
        "[b.jpg]\r\ngeotag=not,numbers\r\n"      # garbage floats
        "[c.jpg]\r\ngeotag=1,2,3\r\n"            # wrong arity
        "[d.jpg]\r\ngeotag=95.0,10.0\r\n"        # out of range
        "[e.jpg]\r\ngeotag=\r\n"                 # empty value
    )
    cat = scan_library(root)
    by = {p.name: p for p in cat.photos}
    assert by["a.jpg"].geotag == pytest.approx((-33.8568, 151.2153))
    assert by["a.jpg"].star == 1
    assert by["b.jpg"].geotag is None
    assert by["c.jpg"].geotag is None
    assert by["d.jpg"].geotag is None
    assert by["e.jpg"].geotag is None
    assert by["b.jpg"].star == 0


@pytest.fixture()
def metadata_library(tmp_path: Path) -> Path:
    """One folder, four precedence cases (§4 tier-1 / §3 star authority):
      a.jpg  in-file EXIF GPS + Rating 3 + date; ini geotag= + star=yes
             -> in-file wins everything
      b.jpg  no in-file metadata; ini geotag= + star=yes
             -> ini fallback holds after indexing
      c.jpg  in-file Rating 2 only; no ini
             -> Rating alone sets the count
      d.jpg  in-file Rating 0; ini star=yes
             -> ini stays authoritative for zero-vs-nonzero (still 1)"""
    root = tmp_path / "mlib"
    _meta_jpeg(root / "f" / "a.jpg",
               date_time_original="1899:03:02 14:00:00",
               gps=WHITEHORSE, rating=3)
    make_jpeg(root / "f" / "b.jpg")
    _meta_jpeg(root / "f" / "c.jpg", rating=2)
    _meta_jpeg(root / "f" / "d.jpg", rating=0)
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\nstar=yes\r\ngeotag=-33.856800,151.215300\r\n"
        "[b.jpg]\r\nstar=yes\r\ngeotag=-33.856800,151.215300\r\n"
        "[d.jpg]\r\nstar=yes\r\n"
    )
    return root


def test_index_precedence_infile_beats_ini(
        metadata_library: Path, tmp_path: Path) -> None:
    cat = scan_library(metadata_library)
    by = {p.name: p for p in cat.photos}
    # scan-level state before the index: ini only, in-file not read yet
    assert by["a.jpg"].geotag == pytest.approx(SYDNEY)
    assert by["a.jpg"].star == 1 and by["a.jpg"].date_taken is None
    assert by["c.jpg"].star == 0

    assert thumbcache.build_cache(cat, tmp_path / "c") is not None
    # a: in-file EXIF GPS beats ini geotag=; Rating 3 beats bare star=yes
    assert by["a.jpg"].geotag == pytest.approx(WHITEHORSE)
    assert by["a.jpg"].star == 3
    assert by["a.jpg"].date_taken == "1899-03-02T14:00:00"  # footgun 16
    # b: no in-file values -> the ini fallback survives the index pass
    assert by["b.jpg"].geotag == pytest.approx(SYDNEY)
    assert by["b.jpg"].star == 1 and by["b.jpg"].date_taken is None
    # c: Rating alone sets the count
    assert by["c.jpg"].star == 2
    # d: an explicit Rating 0 does NOT unstar an ini-starred photo (§3:
    # ini star= is authoritative for zero-vs-nonzero)
    assert by["d.jpg"].star == 1


def test_metadata_catalog_roundtrip_and_version_gate(
        metadata_library: Path, tmp_path: Path) -> None:
    """date_taken / geotag / star count survive save_catalog/load_catalog
    (the warm-load path), and a pre-v5 catalog is rejected so a warm start
    can never silently drop the new fields."""
    import catalog as catmod

    cat = scan_library(metadata_library)
    assert thumbcache.build_cache(cat, tmp_path / "c") is not None
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    loaded = load_catalog(path, metadata_library)
    assert loaded is not None
    for orig, back in zip(cat.photos, loaded.photos):
        assert back.star == orig.star and isinstance(back.star, int)
        assert back.date_taken == orig.date_taken
        assert back.geotag == orig.geotag  # exact: rounded before persist
    a = next(p for p in loaded.photos if p.name == "a.jpg")
    assert a.star == 3 and a.geotag == pytest.approx(WHITEHORSE)
    assert a.date_taken == "1899-03-02T14:00:00"

    # the version gate: a v4 (pre-metadata) catalog cold-rebuilds
    assert catmod.CATALOG_VERSION >= 5   # exact value pinned by the v7 test
    data = json.loads(path.read_text())
    data["version"] = 4
    path.write_text(json.dumps(data))
    assert load_catalog(path, metadata_library) is None


def test_grid_geotag_badge_paint_smoke(tmp_path: Path) -> None:
    """The geotag corner badge paints without incident alongside the star
    badge and selection chrome (offscreen render through paintEvent), in
    a corner of its own (bottom-right vs the star's top-right)."""
    from PySide6.QtGui import QImage

    from grid import GEO_TEAL, STAR_GOLD, _pin_polygon

    g = _selection_grid(tmp_path)
    cat = g.catalog
    d = g.display
    cat.photos[d[0]].geotag = WHITEHORSE            # pin only
    cat.photos[d[1]].geotag = SYDNEY                # pin + star together
    cat.photos[d[1]].star = 4
    _click(g, d[1])                                 # selection chrome on top
    shot = g.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
    assert not shot.isNull()

    # both badges actually hit the viewport: their colors appear in the shot
    found = {"geo": False, "star": False}
    for y in range(0, shot.height(), 2):
        for x in range(0, shot.width(), 2):
            c = shot.pixelColor(x, y)
            if (abs(c.red() - GEO_TEAL.red()) < 30
                    and abs(c.green() - GEO_TEAL.green()) < 30
                    and abs(c.blue() - GEO_TEAL.blue()) < 30):
                found["geo"] = True
            elif (abs(c.red() - STAR_GOLD.red()) < 30
                    and abs(c.green() - STAR_GOLD.green()) < 30
                    and abs(c.blue() - STAR_GOLD.blue()) < 30):
                found["star"] = True
        if all(found.values()):
            break
    assert found["geo"] and found["star"]

    # shape sanity: a closed teardrop — the tip plus a 13-point head arc
    poly = _pin_polygon(10.0, 10.0, 8.0)
    assert poly.size() == 14
    assert poly.at(0).y() > poly.at(7).y()  # tip below the head's top arc


def test_status_readout_date_coords_and_star_count(library: Path) -> None:
    """Single-photo status-bar mode (§5 dual mode) reads out capture date,
    coordinates (§3 geotag v1 display), and the star COUNT (one ★ per
    star); the Starred view still treats any count >= 1 as starred
    (backward-compatible truthiness)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None
    cat = scan_library(library)
    a = next(p for p in cat.photos if p.rel.endswith("Trip/a.jpg"))
    a.star = 3
    a.date_taken = "1899-03-02T14:00:00"
    a.geotag = WHITEHORSE
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    idx = cat.photos.index(a)
    win.grid._select(idx)
    text = win.meta_label.text()
    assert "★★★" in text and "★★★★" not in text   # exactly three
    assert "1899-03-02 14:00:00" in text            # unbounded year, displayed
    assert "60.72125, -135.05685" in text           # signed decimal readout
    assert "the beach" in text                      # caption still present

    # Starred view: count >= 1 keeps every existing truthy consumer working
    win._apply_view("starred", "")
    assert idx in win.grid.display
    assert "Starred: 1 photos" in win.counts_label.text()


def test_viewer_info_line_paints_metadata(library: Path) -> None:
    """The viewer's info bar composes star count + date + coordinates
    without incident (offscreen paint smoke; the text path is the shared
    catalog.format_* formatting the status bar test asserts on)."""
    _offscreen_app()
    from viewer import ViewerPage

    cat = scan_library(library)
    a = next(p for p in cat.photos if p.rel.endswith("Trip/a.jpg"))
    a.star = 5
    a.date_taken = "2009-07-04T13:00:00"
    a.geotag = SYDNEY
    v = ViewerPage(cat, None)
    v.resize(320, 240)
    v.show()
    v.show_photo([cat.photos.index(a)], 0)
    assert not v.grab().isNull()   # paints the bar with all fields present
    v.quiesce()


# ---------------------------------------------------------------------------
# RAW support (fauxcasa-v46.1): Picasa's documented 16-vendor extension list
# in BOTH walkers (lockstep, or caches stop binding), rawpy decode routed by
# extension ahead of any content sniff (TIFF-based RAW containers fool
# QImageReader/PIL), embedded-JPEG-preview-first with demosaic fallback,
# orientation applied exactly once per path, and corrupt-RAW fail-soft.
#
# Fixture provenance (privacy rule: NEVER real family data): _make_dng below
# hand-rolls a minimal-but-valid little-endian DNG 1.4 from scratch — a TIFF
# container holding a deterministic synthetic 16-bit RGGB CFA mosaic
# (struct-packed gradient, no camera involved), optionally an embedded JPEG
# preview built by the suite's own Qt encoder (_jpeg_bytes), plus the tags
# LibRaw's identify() requires (DNGVersion, CFA geometry, ColorMatrix1,
# UniqueCameraModel; note LibRaw rejects raws under 22 px per side). Verified
# against rawpy/LibRaw: imread + postprocess succeed, extract_thumb returns
# the preview when present and LibRawNoThumbnailError when absent.
# ---------------------------------------------------------------------------


def _dng_ifd(entries: list, ifd_off: int) -> bytes:
    """Serialize one TIFF IFD at ifd_off: sorted 12-byte entries, values
    <= 4 bytes inline, larger payloads appended after the table (word-
    aligned). entries: (tag, type, count, payload_bytes)."""
    entries = sorted(entries, key=lambda e: e[0])
    data_off = ifd_off + 2 + 12 * len(entries) + 4
    table = struct.pack("<H", len(entries))
    data = b""
    for tag, typ, count, payload in entries:
        if len(payload) <= 4:
            table += struct.pack("<HHI", tag, typ, count) \
                + payload.ljust(4, b"\0")
        else:
            if (data_off + len(data)) % 2:
                data += b"\0"
            table += struct.pack("<HHII", tag, typ, count,
                                 data_off + len(data))
            data += payload
    return table + struct.pack("<I", 0) + data


def _dng_ifd_size(entries: list) -> int:
    return 2 + 12 * len(entries) + 4 + sum(
        len(p) + (len(p) % 2) for _t, _y, _c, p in entries if len(p) > 4)


def _make_dng(path: Path, w: int = 32, h: int = 24, orientation: int = 1,
              preview_jpeg: bytes | None = None,
              preview_size: tuple[int, int] = (0, 0),
              truncate: bool = False) -> Path:
    """A tiny synthetic DNG (see the section comment for provenance). With
    `preview_jpeg`, IFD0 is a JPEG-compressed preview (the layout real
    cameras use) and the CFA raw lives in a SubIFD; without, the raw IS
    IFD0 and the file carries no thumbnail at all (forces the demosaic
    fallback). `orientation` writes TIFF tag 274 so LibRaw bakes the flip
    during postprocess. `truncate` chops half the CFA strip off the end —
    a structurally-valid header whose pixel read fails (fail-soft test)."""
    _SHORT, _LONG, _BYTE, _ASCII, _SRAT = 3, 4, 1, 2, 10
    strip = struct.pack(f"<{w * h}H", *(((x * 89 + y * 71) % 4096)
                                        for y in range(h) for x in range(w)))
    cam = b"Fauxcasa Synthetic\0"
    cm = b"".join(struct.pack("<ii", v, 10000) for v in
                  (10000, 0, 0, 0, 10000, 0, 0, 0, 10000))  # identity XYZ

    def E(tag, typ, fmt, *vals):
        return (tag, typ, len(vals) if len(vals) > 1 else 1,
                struct.pack(fmt, *vals))

    raw_entries = [
        E(254, _LONG, "<I", 0),            # NewSubfileType: the raw image
        E(256, _LONG, "<I", w), E(257, _LONG, "<I", h),
        E(258, _SHORT, "<H", 16),          # 16-bit samples
        E(259, _SHORT, "<H", 1),           # uncompressed
        E(262, _SHORT, "<H", 32803),       # PhotometricInterpretation: CFA
        E(277, _SHORT, "<H", 1),           # 1 sample/px
        E(278, _LONG, "<I", h),            # RowsPerStrip
        E(279, _LONG, "<I", len(strip)),   # StripByteCounts
        E(284, _SHORT, "<H", 1),
        E(33421, _SHORT, "<HH", 2, 2),     # CFARepeatPatternDim
        (33422, _BYTE, 4, bytes([0, 1, 1, 2])),  # CFAPattern: RGGB
        E(50714, _SHORT, "<H", 0),         # BlackLevel
        E(50717, _LONG, "<I", 4095),       # WhiteLevel
    ]
    shared = [
        (50706, _BYTE, 4, bytes([1, 4, 0, 0])),      # DNGVersion 1.4
        (50708, _ASCII, len(cam), cam),              # UniqueCameraModel
        (50721, _SRAT, 9, cm),                       # ColorMatrix1
        E(50778, _SHORT, "<H", 21),                  # CalibrationIlluminant1
        E(274, _SHORT, "<H", orientation),           # Orientation
    ]

    if preview_jpeg is None:
        ifd0 = raw_entries + shared + [E(273, _LONG, "<I", 0)]
        strip_off = 8 + _dng_ifd_size(ifd0)
        ifd0[-1] = E(273, _LONG, "<I", strip_off)    # StripOffsets -> raw
        out = struct.pack("<2sHI", b"II", 42, 8) + _dng_ifd(ifd0, 8)
        assert len(out) == strip_off
        out += strip
    else:
        pw, ph = preview_size
        ifd0 = [
            E(254, _LONG, "<I", 1),        # reduced-resolution preview
            E(256, _LONG, "<I", pw), E(257, _LONG, "<I", ph),
            (258, _SHORT, 3, struct.pack("<HHH", 8, 8, 8)),
            E(259, _SHORT, "<H", 7),       # JPEG-compressed strip
            E(262, _SHORT, "<H", 6),       # YCbCr
            E(277, _SHORT, "<H", 3),
            E(278, _LONG, "<I", ph),
            E(279, _LONG, "<I", len(preview_jpeg)),
            E(273, _LONG, "<I", 0),        # -> preview jpeg (patched below)
            E(330, _LONG, "<I", 0),        # SubIFDs -> raw (patched below)
        ] + shared
        raw_ifd = raw_entries + [E(273, _LONG, "<I", 0)]
        sub_off = 8 + _dng_ifd_size(ifd0)
        jpeg_off = sub_off + _dng_ifd_size(raw_ifd)
        strip_off = jpeg_off + len(preview_jpeg)
        ifd0 = [E(273, _LONG, "<I", jpeg_off) if e[0] == 273
                else E(330, _LONG, "<I", sub_off) if e[0] == 330
                else e for e in ifd0]
        raw_ifd[-1] = E(273, _LONG, "<I", strip_off)
        out = struct.pack("<2sHI", b"II", 42, 8) + _dng_ifd(ifd0, 8)
        assert len(out) == sub_off
        out += _dng_ifd(raw_ifd, sub_off)
        assert len(out) == jpeg_off
        out += preview_jpeg + strip
    if truncate:
        out = out[:len(out) - len(strip) // 2]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out)
    return path


def _thumb_qimage(cache, idx: int):
    """Decode cache entry idx's primary-level blob, or None on error tile."""
    from PySide6.QtGui import QImage

    offset, length, _w, _h = cache.entries[idx]
    if length <= 0:
        return None
    with open(cache.path, "rb") as f:
        f.seek(offset)
        img = QImage.fromData(f.read(length), "JPEG")
    return None if img.isNull() else img


def test_raw_extensions_in_both_walkers(tmp_path: Path) -> None:
    """Picasa's documented RAW list (files-supported-by-picasa3.md: 18
    extensions, 16 vendors) is in BOTH EXTS sets, in lockstep, and both
    walks pick RAW files up case-insensitively — the walk-parity contract
    that keeps caches binding."""
    import importlib.util

    import catalog
    import rawload

    spec = importlib.util.spec_from_file_location(
        "mtc", REPO / "scripts" / "make-thumbcache.py")
    mtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtc)

    documented = {".dng", ".crw", ".cr2", ".raw", ".raf", ".3fr", ".dcr",
                  ".kdc", ".mrw", ".nef", ".nrw", ".orf", ".rw2", ".pef",
                  ".x3f", ".arw", ".srf", ".sr2"}
    assert rawload.RAW_EXTS == documented
    assert mtc.RAW_EXTS == rawload.RAW_EXTS   # the script's mirror
    assert catalog.EXTS == mtc.EXTS           # the whole lockstep set
    assert documented <= catalog.EXTS

    root = tmp_path / "lib"
    root.mkdir()
    for name in ("a.NEF", "b.dng", "c.Cr2", "d.ARW"):
        (root / name).write_bytes(b"stub")    # walk checks suffix only
    make_jpeg(root / "e.jpg")
    walked = [p.name for p in walk_library(root)]
    assert sorted(walked) == ["a.NEF", "b.dng", "c.Cr2", "d.ARW", "e.jpg"]
    script_walk = sorted(p for p in root.rglob("*")
                         if p.suffix.lower() in mtc.EXTS and p.is_file())
    assert [p.name for p in script_walk] == walked


def test_raw_thumb_via_embedded_preview(tmp_path: Path) -> None:
    """A DNG with an embedded JPEG preview thumbs through extract_thumb, NOT
    demosaic: the cached thumb has the preview's dimensions (64x48; the
    half-size demosaic of the 32x24 raw would be 16x12) and the preview's
    uniform color (the synthetic CFA gradient could never decode to it)."""
    root = tmp_path / "lib"
    root.mkdir()
    _make_dng(root / "p.dng", preview_jpeg=_jpeg_bytes(64, 48),
              preview_size=(64, 48))
    cat = scan_library(root)
    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    (_o, length, w, h), = cache.entries
    assert length > 0 and (w, h) == (64, 48)
    img = _thumb_qimage(cache, 0)
    px = img.pixelColor(32, 24)
    # _jpeg_bytes fills (120, 160, 200); allow JPEG q80 drift
    assert abs(px.red() - 120) < 30 and abs(px.green() - 160) < 30 \
        and abs(px.blue() - 200) < 30


def test_raw_thumb_demosaic_fallback(tmp_path: Path) -> None:
    """A DNG with NO embedded preview falls back to rawpy postprocess
    (half_size=True): the 32x24 raw demosaics to a 16x12 thumb — a real
    decode, not an error tile."""
    root = tmp_path / "lib"
    root.mkdir()
    _make_dng(root / "n.dng")                  # no preview IFD at all
    cat = scan_library(root)
    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    (_o, length, w, h), = cache.entries
    assert length > 0 and (w, h) == (16, 12)   # half of 32x24
    assert _thumb_qimage(cache, 0) is not None


def test_raw_orientation_applied_once_demosaic(tmp_path: Path) -> None:
    """Orientation=6 on a landscape 32x24 raw with no preview: LibRaw bakes
    the flip during postprocess, and the indexer must NOT transform again —
    the thumb comes out portrait (12x16). A double application would be a
    180-degree turn, landing back at landscape."""
    root = tmp_path / "lib"
    root.mkdir()
    _make_dng(root / "r.dng", orientation=6)
    cat = scan_library(root)
    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    (_o, length, w, h), = cache.entries
    assert length > 0 and w < h and (w, h) == (12, 16)


def test_raw_orientation_applied_once_preview(tmp_path: Path) -> None:
    """An embedded preview carrying its OWN EXIF Orientation=6 tag (LibRaw
    passes EXIF'd previews through byte-preserving): the ordinary JPEG
    auto-transform applies it exactly once, so the 64x48 landscape preview
    thumbs portrait (48x64). Twice would be 180 degrees — landscape again."""
    root = tmp_path / "lib"
    root.mkdir()
    pj = _inject(_jpeg_bytes(64, 48), 0xE1, _exif_orientation_app1(6))
    _make_dng(root / "pr.dng", preview_jpeg=pj, preview_size=(64, 48))
    cat = scan_library(root)
    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    (_o, length, w, h), = cache.entries
    assert length > 0 and (w, h) == (48, 64)   # portrait: applied once


def test_raw_corrupt_fails_soft(tmp_path: Path) -> None:
    """Corrupt RAWs — a truncated CFA strip and outright garbage bytes —
    yield the existing zero-length error tile and never abort the build;
    the good neighbors still index."""
    root = tmp_path / "lib"
    root.mkdir()
    _make_dng(root / "trunc.dng", truncate=True)
    (root / "garbage.nef").write_bytes(b"\x00\x01 not a raw file" * 64)
    make_jpeg(root / "ok.jpg")
    cat = scan_library(root)
    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    lengths = {rel: length for rel, (_o, length, _w, _h) in
               zip(cache.files, cache.entries)}
    assert lengths["trunc.dng"] == 0           # error tile
    assert lengths["garbage.nef"] == 0         # error tile
    assert lengths["ok.jpg"] > 0               # neighbors unharmed


def test_viewer_load_original_raw(tmp_path: Path) -> None:
    """viewer.load_original (the seam the slideshow prefetch shares): the
    embedded preview decodes for responsiveness (proven by dimensions AND
    the preview's color), a preview-less DNG demosaics at full size, the
    Picasa rotate= turns compose on top exactly like any format, and a
    corrupt RAW returns a null QImage (the viewer's fail-soft contract)."""
    _offscreen_app()
    from viewer import load_original

    prev = _make_dng(tmp_path / "p.dng", preview_jpeg=_jpeg_bytes(64, 48),
                     preview_size=(64, 48))
    img = load_original(str(prev), 0)
    assert (img.width(), img.height()) == (64, 48)
    px = img.pixelColor(32, 24)
    assert abs(px.red() - 120) < 30 and abs(px.blue() - 200) < 30

    noprev = _make_dng(tmp_path / "n.dng")
    img = load_original(str(noprev), 0)        # full demosaic: native 32x24
    assert (img.width(), img.height()) == (32, 24)
    img = load_original(str(noprev), 1)        # rotate= composes on top
    assert (img.width(), img.height()) == (24, 32)

    bad = tmp_path / "bad.dng"
    bad.write_bytes(b"garbage" * 100)
    assert load_original(str(bad), 0).isNull()


def test_make_thumbcache_raw_paths(tmp_path: Path) -> None:
    """The standalone PIL builder mirrors the same routing: preview-first
    (its own EXIF applied once by exif_transpose), demosaic fallback (flip
    baked by LibRaw, NOT transposed again), error tile on corrupt bytes."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mtc", REPO / "scripts" / "make-thumbcache.py")
    mtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtc)

    prev = _make_dng(tmp_path / "p.dng", preview_jpeg=_jpeg_bytes(64, 48),
                     preview_size=(64, 48))
    (blob, w, h), = mtc._make_thumb(prev, [256])
    assert blob and (w, h) == (64, 48)         # the preview, not 16x12

    rot_prev = _make_dng(
        tmp_path / "rp.dng",
        preview_jpeg=_inject(_jpeg_bytes(64, 48),
                             0xE1, _exif_orientation_app1(6)),
        preview_size=(64, 48))
    (blob, w, h), = mtc._make_thumb(rot_prev, [256])
    assert blob and (w, h) == (48, 64)         # preview EXIF applied once

    noprev = _make_dng(tmp_path / "n.dng", orientation=6)
    (blob, w, h), = mtc._make_thumb(noprev, [256])
    assert blob and (w, h) == (12, 16)         # LibRaw flip only, once

    bad = tmp_path / "bad.arw"
    bad.write_bytes(b"not a raw")
    assert mtc._make_thumb(bad, [256]) == [(b"", 0, 0)]


def test_pre_raw_cache_stops_binding_on_exts_change(tmp_path: Path) -> None:
    """The upgrade path: a cache whose walk never saw RAW files (built by a
    pre-v46 binary, or before the RAW arrived) must fail bind() the moment
    the new walk includes them — the count mismatch that makes main() fall
    back to a cold rescan + rebuild instead of showing misbound tiles."""
    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    make_jpeg(root / "b.jpg")
    cat = scan_library(root)
    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    thumbcache.bind(cache, cat)                # sanity: binds pre-change

    _make_dng(root / "new.dng")                # RAW joins the walk
    fresh = scan_library(root)
    with pytest.raises(thumbcache.CacheError,
                       match="does not match the library walk"):
        thumbcache.bind(cache, fresh)


def test_scan_filter_never_drops_raw(tmp_path: Path) -> None:
    """The size scan-filter judges RAW dimensions as unknowable (a
    QImageReader sniff of the TIFF container would report the tiny embedded
    preview's dims — a wrong answer) and therefore always KEEPS RAW files,
    while still filtering ordinary images."""
    root = tmp_path / "lib"
    root.mkdir()
    make_jpeg(root / "small.jpg", 64, 48)
    _make_dng(root / "m.dng")
    files = [p.name for p in
             walk_library(root, ScanFilter(min_width=1000))]
    assert files == ["m.dng"]                  # jpeg filtered, RAW kept


# ---------------------------------------------------------------------------
# §7 performance gates (fauxcasa-ed5.4/.3): the --search-probe latency
# harness and the per-catalog search-haystack index behind it. The probe
# emits one machine-readable {"event":"search","query","ms","hits"} line
# per comma-separated query (scripts/perf-canary.py parses these in CI);
# the haystack list is a MainWindow-owned parallel structure — NOT Photo
# fields — rebuilt on reload_data and on cold-index finish, because
# build_cache merges in-file captions/keywords into photos in place.
# ---------------------------------------------------------------------------


def test_search_probe_emits_wellformed_events(search_library: Path,
                                              capsys) -> None:
    """run_search_probe drives each query through the real search box (the
    same setText -> _search_changed path a keystroke takes), prints one JSON
    line per query with the documented keys, skips blank segments, and
    leaves the box empty. Repeated identical queries re-fire (the probe
    clears the box between queries)."""
    from main import run_search_probe

    win = _search_win(search_library)
    events = run_search_probe(
        win, "beach, beach -dunes ,nosuchterm,-city, ,beach")
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert [json.loads(ln) for ln in lines] == events

    assert [e["query"] for e in events] == [
        "beach", "beach -dunes", "nosuchterm", "-city", "beach"]
    by_q = {e["query"]: e for e in events}
    assert by_q["beach"]["hits"] == 2           # sunset.jpg + dunes.jpg
    assert by_q["beach -dunes"]["hits"] == 1    # negation applies
    assert by_q["nosuchterm"]["hits"] == 0
    assert by_q["-city"]["hits"] == 2           # negation-only query
    for e in events:
        assert e["event"] == "search"
        assert isinstance(e["ms"], float) and e["ms"] >= 0.0
        assert isinstance(e["hits"], int)
    assert win.search.text() == ""              # box left clean
    assert _hits(win) == {"sunset.jpg", "dunes.jpg", "market.jpg",
                          "street.jpg"}         # ...and the filter reset


def test_search_changed_records_latency_and_hits(search_library: Path
                                                 ) -> None:
    """Every _search_changed run — including the empty-query reset path —
    records last_search_ms/last_search_hits for the probe to read."""
    win = _search_win(search_library)

    win.search.setText("beach")
    assert win.last_search_hits == 2
    assert win.last_search_ms >= 0.0
    win.search.setText("")                      # reset path records too
    assert win.last_search_hits == 4            # the unfiltered view
    assert win.last_search_ms >= 0.0


def test_search_haystack_rebuilt_on_reload_data(search_library: Path,
                                                tmp_path: Path) -> None:
    """reload_data (the reconcile swap) rebuilds the haystack index for the
    NEW catalog: photos and metadata that only exist in the swapped-in
    library are searchable, vanished ones are not."""
    win = _search_win(search_library)
    win.search.setText("sunset")
    assert _hits(win) == {"sunset.jpg"}
    n_pairs = len(win._search_pairs)
    assert n_pairs == len(win.catalog.photos)

    other = tmp_path / "other-lib"
    make_jpeg(other / "2022 Aurora" / "borealis.jpg")
    (other / "2022 Aurora" / ".picasa.ini").write_text(
        "[borealis.jpg]\r\ncaption=green curtain\r\nkeywords=night\r\n")
    win.reload_data(scan_library(other), None)

    assert len(win._search_pairs) == 1          # parallel to the new catalog
    win.search.setText("curtain")               # new caption is indexed
    assert _hits(win) == {"borealis.jpg"}
    win.search.setText("sunset")                # the old library is gone
    assert _hits(win) == set()


def test_search_haystack_rebuilt_on_cold_index_finish(
        search_library: Path, tmp_path: Path, capsys) -> None:
    """The cold-build finish path re-indexes: build_cache merges in-file
    captions/keywords into the SAME Photo objects in place, so
    _on_index_finished must rebuild the haystacks — an in-place caption
    change is invisible to the stale index (that staleness is exactly why
    the sync point exists) and searchable after."""
    win = _search_win(search_library)
    result = thumbcache.build_cache(win.catalog, tmp_path / "cache")
    assert result is not None

    # In-place mutation, as the indexer does. The prebuilt index is stale
    # by design until a sync point runs:
    dunes = next(p for p in win.catalog.photos if p.name == "dunes.jpg")
    dunes.caption = "windswept ripples"
    win.search.setText("windswept")
    assert _hits(win) == set()                  # stale: not re-indexed yet

    win._on_index_finished(result, win.catalog, False)  # cold-build finish
    win.search.setText("")                      # identical text would not
    win.search.setText("windswept")             # re-fire textChanged
    assert _hits(win) == {"dunes.jpg"}          # fresh haystacks
    capsys.readouterr()                         # swallow the indexed event


def test_search_haystack_visible_subset_and_reveal(tmp_path: Path) -> None:
    """The precomputed visible-only pair list serves off-reveal searches
    (hidden photos excluded); reveal searches scan the full list. Semantics
    identical to the per-keystroke scan it replaced."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "shown.jpg")
    make_jpeg(root / "Trip" / "secret.jpg")
    (root / "Trip" / ".picasa.ini").write_text(
        "[secret.jpg]\r\nhidden=yes\r\n")
    app = QApplication.instance() or QApplication([])
    assert app is not None
    win = MainWindow(scan_library(root), None, cache_dir=None, build_dir=None)

    assert len(win._search_pairs) == 2
    assert len(win._search_pairs_vis) == 1      # the hidden photo is out

    win.search.setText("trip")                  # folder term, off-reveal
    assert _hits(win) == {"shown.jpg"}
    win.reveal_box.setChecked(True)             # reveal re-runs the search
    assert _hits(win) == {"shown.jpg", "secret.jpg"}
    win.reveal_box.setChecked(False)
    assert _hits(win) == {"shown.jpg"}




# ---------------------------------------------------------------------------
# READY-poll timer lifecycle (fauxcasa-q6l.15): an in-process main() run must
# not leave its 50 ms check_ready poll alive — an orphan QTimer outlived
# main() and fired into a deleted GridView during a LATER test's
# processEvents (RuntimeError noise through the excepthook; independently
# rediscovered by three implementation sessions before being fixed).
# ---------------------------------------------------------------------------


def test_ready_poll_timer_dies_with_the_run(
        monkeypatch, library: Path, tmp_path: Path, caplog) -> None:
    """After a self-quitting in-process main() run, spinning the (reused)
    QApplication's event loop must fire no stale check_ready — no CRITICAL
    'uncaught exception' may reach the log."""
    import logging
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    import main

    app = QApplication.instance() or QApplication([])
    cache_root = tmp_path / "cr"
    monkeypatch.setattr(sys, "argv", [
        "fauxcasa-tracer", str(library), "--cache-root", str(cache_root),
        "--quit-after-ready", "--finish-build", "--timeout", "30"])
    assert main.main() == 0

    # The window is gone; give any leaked 50 ms poll several chances to fire.
    with caplog.at_level(logging.CRITICAL, logger="fauxcasa"):
        for _ in range(6):
            loop = QEventLoop()
            QTimer.singleShot(60, loop.quit)
            loop.exec()
            QCoreApplication.processEvents()
    stale = [r for r in caplog.records if "uncaught exception" in r.message]
    assert stale == [], f"stale check_ready fired: {stale}"


# ---------------------------------------------------------------------------
# M1 ingest completion: import report + placeholder albums (fauxcasa-cam.13)
# and the Picasa2Albums .pal reader + §4 gap-fill merge (fauxcasa-cam.8).
# Synthetic fixtures only (privacy rule): hand-authored inis and .pal XML per
# the forensicir 2007 writeup. Merge rank ASSUMED ini > .pal > db3 pending
# the spec pin (fauxcasa-79b); every departure from a source is an
# ImportReport entry, never a silent resolution (§4).
# ---------------------------------------------------------------------------

# 32-hex album uids: one ini-defined, one referenced-but-never-defined
# (the placeholder case), one that exists only as a .pal file.
UID_DEF = "1111222233334444555566667777888a"
UID_GHOST = "2222333344445555666677778888999b"
UID_PAL = "3333444455556666777788889999aaab"


@pytest.fixture()
def album_library(tmp_path: Path) -> Path:
    """Three photos in one folder: an ini-defined album holding a+b, and a
    GHOST uid referenced from b+c with no [.album:] definition anywhere."""
    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "a.jpg")
    make_jpeg(root / "Trip" / "b.jpg")
    make_jpeg(root / "Trip" / "c.jpg")
    (root / "Trip" / ".picasa.ini").write_text(
        f"[.album:{UID_DEF}]\r\n"
        "name=Defined\r\n"
        f"token=]album:{UID_DEF}\r\n"
        "[a.jpg]\r\n"
        f"albums={UID_DEF}\r\n"
        "[b.jpg]\r\n"
        f"albums={UID_DEF},{UID_GHOST}\r\n"
        "[c.jpg]\r\n"
        f"albums={UID_GHOST}\r\n")
    return root


def _write_pal(pal_dir: Path, uid: str, name: str, members: list[str],
               date: str | None = "39272.630035") -> Path:
    """One .pal file in the forensicir-documented picasa2album shape (the
    same XML the synthetic-corpus generator writes): property elements,
    then the <files> volume-token member list nested in the name property."""
    files = "\n".join(
        f" <filename>[C]\\{m.replace('/', chr(92))}</filename>"
        for m in members)
    date_prop = (f'<property name="date" type="real64" value="{date}"/>\n'
                 if date is not None else "")
    pal_dir.mkdir(parents=True, exist_ok=True)
    p = pal_dir / f"{uid}.pal"
    p.write_text(
        "<picasa2album>\n"
        f"<dbid>0164eaeacdd4046f5c1e44522fe44527</dbid>\n"
        f"<albumid>{uid}</albumid>\n"
        f'<property name="uid" type="string" value="{uid}"/>\n'
        f'<property name="category" type="num" value="0"/>\n'
        f"{date_prop}"
        f'<property name="token" type="string" value="]album:{uid}"/>\n'
        f'<property name="name" type="string" value="{name}">\n'
        "<files>\n"
        f"{files}\n"
        "</files>\n"
        "</property>\n"
        "</picasa2album>\n",
        encoding="utf-8")
    return p


def test_read_pal_good_garbage_and_fallbacks(tmp_path: Path) -> None:
    """read_pal parses the documented shape — 32-hex uid, name, the real64
    OLE date converted to the catalog's canonical ISO string, members with
    the [C]\\ volume token stripped to POSIX paths — falls back to the
    file's own name for the uid (Picasa names .pal files by uid), and
    fails soft PER FILE on garbage: bytes that aren't XML, XML that isn't
    a picasa2album, and a file with no usable uid anywhere are each None."""
    from catalog import read_pal, read_pal_dir

    good = _write_pal(tmp_path / "albums", UID_PAL, "Sammy",
                      ["Trip/a.jpg", "Deep/er/b.jpg"])
    pal = read_pal(good)
    assert pal is not None
    assert pal.uid == UID_PAL
    assert pal.name == "Sammy"
    assert pal.date == "2007-07-09T15:07:15"   # OLE 39272.630035
    assert pal.members == ["Trip/a.jpg", "Deep/er/b.jpg"]

    # uid falls back to the file stem when the properties carry none
    stemmed = tmp_path / "albums" / f"{UID_DEF}.pal"
    stemmed.write_text("<picasa2album><files>\n"
                       "<filename>[C]\\x.jpg</filename>\n"
                       "</files></picasa2album>")
    pal = read_pal(stemmed)
    assert pal is not None and pal.uid == UID_DEF
    assert pal.name == UID_DEF[:8] and pal.date is None
    assert pal.members == ["x.jpg"]

    garbage = tmp_path / "albums" / "nothex.pal"
    garbage.write_bytes(b"\x00\x01 not xml at all")
    assert read_pal(garbage) is None
    not_album = tmp_path / "albums" / "other.pal"
    not_album.write_text("<somethingelse><a/></somethingelse>")
    assert read_pal(not_album) is None
    no_uid = tmp_path / "albums" / "badname.pal"   # stem not 32-hex either
    no_uid.write_text("<picasa2album><files/></picasa2album>")
    assert read_pal(no_uid) is None

    # the directory reader keeps the good ones and reports the bad by name
    pals, bad = read_pal_dir(tmp_path / "albums")
    assert {p.uid for p in pals} == {UID_PAL, UID_DEF}
    assert sorted(bad) == ["badname.pal", "nothex.pal", "other.pal"]
    assert read_pal_dir(tmp_path / "no-such-dir") == ([], [])


def test_pal_gap_fill_only_merge(album_library: Path, tmp_path: Path) -> None:
    """§4 merge (rank assumed ini > .pal > db3, fauxcasa-79b): an AGREEING
    .pal changes nothing except filling the ini definition's missing date;
    a .pal-ONLY album materializes like a real album flagged pal-sourced
    (unresolvable members reported, resolvable ones kept); and a .pal that
    IS a placeholder's missing definition fills the name/date gap while
    membership authority stays with the ini's albums= tokens."""
    pal_dir = tmp_path / "albums"
    _write_pal(pal_dir, UID_DEF, "Defined", ["Trip/a.jpg", "Trip/b.jpg"])
    _write_pal(pal_dir, UID_PAL, "Pal Only",
               ["Trip/c.jpg", "Gone/missing.jpg"])
    _write_pal(pal_dir, UID_GHOST, "Ghost Found",
               ["Trip/b.jpg", "Trip/c.jpg"], date=None)

    cat = scan_library(album_library, pal_dir=pal_dir)
    assert list(cat.albums) == [UID_DEF, UID_GHOST, UID_PAL]

    d = cat.albums[UID_DEF]
    assert d.members == [0, 1] and not d.placeholder and not d.pal_sourced
    assert d.name == "Defined"
    assert d.date == "2007-07-09T15:07:15"     # gap-filled: ini had no date

    p = cat.albums[UID_PAL]
    assert p.pal_sourced and not p.placeholder
    assert p.name == "Pal Only" and p.members == [2]

    g = cat.albums[UID_GHOST]
    assert g.pal_sourced and not g.placeholder  # the .pal WAS the definition
    assert g.name == "Ghost Found"
    assert g.members == [1, 2]                  # ini membership, untouched

    kinds = [(e.kind, e.subject) for e in cat.report.entries]
    assert ("pal_member_missing", UID_PAL) in kinds
    assert not any(k == "pal_divergence" for k, _u in kinds)
    assert not any(k == "unknown_album" for k, _u in kinds)  # de-placeholdered
    missing = next(e for e in cat.report.entries
                   if e.kind == "pal_member_missing")
    assert "Gone/missing.jpg" in missing.detail and missing.source == "pal"


def test_pal_divergence_reported_not_membership(
        album_library: Path, tmp_path: Path) -> None:
    """A DIVERGENT .pal for an ini-defined album: the extra member is an
    import-report entry, NOT a membership change, and the ini member the
    .pal lacks is kept (and recorded). An unreadable .pal file is reported
    and skipped without sinking the scan (fail-soft per file)."""
    pal_dir = tmp_path / "albums"
    _write_pal(pal_dir, UID_DEF, "Defined", ["Trip/a.jpg", "Trip/c.jpg"])
    (pal_dir / f"{UID_PAL}.pal").write_bytes(b"\xff\xfe utterly broken")

    cat = scan_library(album_library, pal_dir=pal_dir)
    d = cat.albums[UID_DEF]
    assert d.members == [0, 1]                 # ini wins: c.jpg NOT added
    assert UID_PAL not in cat.albums           # broken file never lands

    div = [e for e in cat.report.entries if e.kind == "pal_divergence"]
    assert len(div) == 1 and div[0].subject == UID_DEF
    assert "Trip/c.jpg" in div[0].detail       # the extra, surfaced
    assert "Trip/b.jpg" in div[0].detail       # the ini member the .pal lacks
    assert "ini wins" in div[0].detail         # the recorded choice
    bad = [e for e in cat.report.entries if e.kind == "pal_unreadable"]
    assert len(bad) == 1 and bad[0].subject == f"{UID_PAL}.pal"


def test_placeholder_album_materializes_and_reports(
        album_library: Path) -> None:
    """§3: an albums= uid with no definition anywhere materializes as a
    placeholder Album — uid, 'Unknown album <uid8>' name, members
    populated, placeholder flag — plus an unknown_album import-report
    entry. Never dropped (the pre-cam.13 code silently skipped these)."""
    cat = scan_library(album_library)
    assert list(cat.albums) == [UID_DEF, UID_GHOST]
    g = cat.albums[UID_GHOST]
    assert g.placeholder and not g.pal_sourced
    assert g.name == f"Unknown album {UID_GHOST[:8]}"
    assert g.members == [1, 2]                 # b.jpg + c.jpg
    assert not cat.albums[UID_DEF].placeholder

    unknown = [e for e in cat.report.entries if e.kind == "unknown_album"]
    assert len(unknown) == 1
    assert unknown[0].subject == UID_GHOST and unknown[0].source == "ini"
    assert "placeholder" in unknown[0].detail


def test_contact_name_conflict_reported(
        faces_library: Path, tmp_path: Path) -> None:
    """The §4 conflict PR #37 resolved silently: contacts.xml renaming a
    [Contacts2] contact is now an import-report entry recording both names
    and the winner. Agreeing ids and xml-only ids produce no entry."""
    from catalog import load_contacts_xml

    contacts = load_contacts_xml(
        _write_faces_contacts_xml(tmp_path / "contacts.xml"))
    cat = scan_library(faces_library, None, contacts)

    conflicts = [e for e in cat.report.entries
                 if e.kind == "contact_name_conflict"]
    assert len(conflicts) == 1                 # only Carol truly conflicts
    e = conflicts[0]
    assert e.subject == "cccccccccccccccc" and e.source == "contacts"
    assert "Carol Ini" in e.detail and "Carol Xml" in e.detail
    assert "contacts.xml wins" in e.detail
    # ...and the resolution itself is unchanged (xml wins, §4)
    assert cat.contacts["cccccccccccccccc"] == "Carol Xml"

    assert not scan_library(faces_library).report.entries  # no xml, no notes


def test_placeholder_sidebar_marking_and_notes_count(
        album_library: Path) -> None:
    """The sidebar shows a placeholder album visually marked — dimmed,
    italic, '?' suffix — while a real album renders normally; the status
    bar carries the 'N import notes' count with the first entries in the
    tooltip; and clicking the placeholder filters the grid to its members
    exactly like a real album (never dropped, §3)."""
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

    cat = scan_library(album_library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    ghost = item_for(win, "album", UID_GHOST)
    assert ghost is not None
    assert ghost.text(0) == f"Unknown album {UID_GHOST[:8]} ?  (2)"
    assert ghost.font(0).italic()
    assert "placeholder" in ghost.toolTip(0)
    real = item_for(win, "album", UID_DEF)
    assert real.text(0) == "Defined  (2)" and not real.font(0).italic()

    assert win.notes_label.isVisibleTo(win)
    assert win.notes_label.text().strip() == "1 import note"
    assert "unknown_album" in win.notes_label.toolTip()

    win._sidebar_clicked(ghost, 0)             # placeholders filter like albums
    assert [cat.photos[i].rel for i in win.grid.display] == [
        "Trip/b.jpg", "Trip/c.jpg"]

    # a catalog with no notes shows no chrome at all
    clean = scan_library(album_library)
    clean.report.entries.clear()
    win2 = MainWindow(clean, None, cache_dir=None, build_dir=None)
    assert not win2.notes_label.isVisibleTo(win2)


def test_import_report_persistence_and_warm_status(
        album_library: Path, tmp_path: Path) -> None:
    """save_report/load_report round-trip the entries beside catalog.json;
    a missing or corrupt report file degrades to an EMPTY report (fail-soft
    — diagnostics, never a gate); and a warm start that re-attaches the
    persisted report drives the same status-bar count the scan did."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from catalog import REPORT_NAME, load_report, save_report
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    cat = scan_library(album_library)
    assert len(cat.report.entries) == 1
    save_report(cat.report, tmp_path / REPORT_NAME)
    back = load_report(tmp_path / REPORT_NAME)
    assert back.entries == cat.report.entries
    assert back.summary() == "1 import note (unknown_album)"

    assert load_report(tmp_path / "absent.json").entries == []
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    assert load_report(corrupt).entries == []

    # the warm path: persisted catalog (report NOT inside it) + re-attach
    save_catalog(cat, tmp_path / "catalog.json")
    loaded = load_catalog(tmp_path / "catalog.json", album_library)
    assert loaded is not None
    assert loaded.report.entries == []         # empty until re-attached
    loaded.report = load_report(tmp_path / REPORT_NAME)
    win = MainWindow(loaded, None, cache_dir=None, build_dir=None)
    assert win.notes_label.text().strip() == "1 import note"


def test_catalog_v6_roundtrips_album_flags(
        album_library: Path, tmp_path: Path) -> None:
    """From CATALOG_VERSION 6 on: placeholder and pal-sourced albums survive
    the persisted catalog — flags, names, members — and a v5 catalog (which
    silently dropped both classes) is rejected so a warm start cold-rebuilds
    instead of hiding them again. (>= 6: the exact current value is pinned
    by the newest version-gate test.)"""
    import catalog as catmod

    assert catmod.CATALOG_VERSION >= 6
    pal_dir = tmp_path / "albums"
    _write_pal(pal_dir, UID_PAL, "Pal Only", ["Trip/c.jpg"])
    cat = scan_library(album_library, pal_dir=pal_dir)
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    loaded = load_catalog(path, album_library)
    assert loaded is not None
    assert list(loaded.albums) == list(cat.albums)
    for uid, orig in cat.albums.items():
        back = loaded.albums[uid]
        assert back.placeholder == orig.placeholder
        assert back.pal_sourced == orig.pal_sourced
        assert back.members == orig.members and back.name == orig.name
    assert loaded.albums[UID_GHOST].placeholder
    assert loaded.albums[UID_PAL].pal_sourced

    data = json.loads(path.read_text())
    data["version"] = 5
    path.write_text(json.dumps(data))
    assert load_catalog(path, album_library) is None


# ---------------------------------------------------------------------------
# db3 rescue import (fauxcasa-cam.6/.7): the §4 plumbing (locate db3,
# translate machine paths, thumbindex/pmp joins) + the class-4 rescue
# (person albums -> contacts-only face names). Synthetic fixtures only
# (privacy rule): the MINIMAL writer below emits the byte formats documented
# in docs/research/picasa-db3-validated.md — TESTS ONLY, never product code
# — and the validated picasa_db readers are the arbiter (round-trip test).
# Rescue classes 1-3 (ignored faces, manual sort, video overrides) have no
# pinned byte format yet (fauxcasa-ed5.9) and stay out of scope here.
# ---------------------------------------------------------------------------

CID_DB3 = "ca5c88ca60f42c0b"           # oracle-014's contact-id shape
PERSON_DB3 = "Synthetic Person 1200"

_PMP_TYPE_CODES = {"string": 0x0, "uint32": 0x1, "uint8": 0x3, "uint64": 0x4}
_PMP_PACK = {0x1: "<I", 0x3: "<B", 0x4: "<Q"}


def _write_pmp(path: Path, type_name: str, values: list) -> Path:
    """One .pmp column file to the documented layout (constants written
    literally from picasa-db3-validated.md, NOT taken from the reader, so
    the round-trip test proves the parser against independent bytes):
    u32 magic 0x3fcccccd | u16 type | u16 0x1332 | u32 2 | u16 type |
    u16 0x1332 | u32 count | payload (NUL-terminated strings or packed
    little-endian fixed-width values)."""
    code = _PMP_TYPE_CODES[type_name]
    out = bytearray(struct.pack("<IHHIHHI", 0x3FCCCCCD, code, 0x1332, 2,
                                code, 0x1332, len(values)))
    for v in values:
        if code == 0x0:
            out += v.encode("utf-8") + b"\x00"
        else:
            out += struct.pack(_PMP_PACK[code], v)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def _write_thumbindex(path: Path, entries: list) -> Path:
    """thumbindex.db to the documented layout: u32 magic 0x40466666 |
    u32 count | per entry NUL-terminated name + <u64 taken, u64 mtime,
    u32 size, u8 ftype, u32 flags, u8 valid, u32 parent>. `entries` is
    (name, ftype, parent-index-or-None); timestamps/sizes stay 0 (the
    oracle's face-crop records carry 0 there too) and valid is 1."""
    out = bytearray(struct.pack("<II", 0x40466666, len(entries)))
    for name, ftype, parent in entries:
        out += name.encode("utf-8") + b"\x00"
        out += struct.pack("<QQIBIBI", 0, 0, 0, ftype, 0, 1,
                           0xFFFFFFFF if parent is None else parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def _db3_machine_path(folder: Path, drive: str = "Q:") -> str:
    """The db3 spelling of `folder`: absolute, backslashes, trailing
    separator (folder thumbindex entries carry one), and a DIFFERENT
    drive letter than the live one — §8 says drive letters are
    translated, so every join in these tests must survive the swap."""
    parts = [c for c in str(folder).replace("\\", "/").split("/") if c]
    if len(parts[0]) == 2 and parts[0][1] == ":":
        parts = parts[1:]
    return drive + "\\" + "\\".join(parts) + "\\"


def _make_person_db3(db3: Path, folder_abs: str, photos: list[str],
                     contact_id: str = CID_DB3,
                     person_name: str = PERSON_DB3,
                     face_parent: int | None = None) -> Path:
    """A minimal synthetic db3 in the oracle-014 shape: thumbindex row 0
    is the folder (absolute machine path), rows 1..n the photos, plus —
    when face_parent (a photo's thumbindex row) is given — a virtual
    face-crop record whose imagedata row is filetype 1001 joined via
    personalbumid to a category-8 person album. albumdata carries the
    stock no-contact-id people bucket too, which the rescue must skip."""
    if not folder_abs.endswith(("\\", "/")):
        folder_abs += "\\"
    ti = [(folder_abs, 0x01, None)]
    for name in photos:
        ti.append((name, 0x02, 0))
    filetype = [1] + [2] * len(photos)
    personalbumid = [0] * (1 + len(photos))
    if face_parent is not None:
        ti.append(("", 0xE9, face_parent))    # ftype = low byte of 0x3e9
        filetype.append(1001)                 # the virtual face-crop row
        personalbumid.append(2)               # -> the person-album row
    _write_thumbindex(db3 / "thumbindex.db", ti)
    # albumdata rows: [0] the watched folder (category 2), [1] the stock
    # people bucket (category 8, NO contact id — skipped by the rescue),
    # [2] the person album (category 8, name + albumcontactids).
    _write_pmp(db3 / "albumdata_category.pmp", "uint32", [2, 8, 8])
    _write_pmp(db3 / "albumdata_name.pmp", "string",
               [folder_abs, "Unnamed people", person_name])
    _write_pmp(db3 / "albumdata_albumcontactids.pmp", "uint64",
               [0, 0, int(contact_id, 16)])
    _write_pmp(db3 / "albumdata_token.pmp", "string",
               ["", "]unknownface", "]facealbum:2"])
    _write_pmp(db3 / "imagedata_filetype.pmp", "uint32", filetype)
    _write_pmp(db3 / "imagedata_personalbumid.pmp", "uint32", personalbumid)
    return db3


@pytest.fixture()
def db3_library(tmp_path: Path) -> Path:
    """One folder, two photos: a.jpg carries an ini faces= region whose
    contact id NO ini/contacts.xml source names — the exact gap the db3
    person-album rescue exists to fill; b.jpg carries no faces= at all."""
    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "a.jpg")
    make_jpeg(root / "Trip" / "b.jpg")
    (root / "Trip" / ".picasa.ini").write_text(
        "[a.jpg]\r\n"
        f"faces=rect64(6800600097ff9fff),{CID_DB3}\r\n")
    return root


def test_db3_writer_roundtrip_via_validated_parsers(tmp_path: Path) -> None:
    """The synthetic writer's bytes read back through the VALIDATED
    parsers under strict mode: pmp string/uint32/uint64 columns (values,
    filename-derived table/column, exact-fit payload) and thumbindex
    (folder/file/face-crop discrimination, parent linkage, full-path
    join) — so every db3 test below runs on oracle-shaped bytes."""
    import picasa_db

    db3 = tmp_path / "db3"
    col = picasa_db.read_pmp(
        _write_pmp(db3 / "albumdata_name.pmp", "string", ["a", "", "sí"]))
    assert (col.table, col.column) == ("albumdata", "name")
    assert col.values == ["a", "", "sí"] and col.count == 3

    col = picasa_db.read_pmp(
        _write_pmp(db3 / "imagedata_filetype.pmp", "uint32", [1, 2, 1001]))
    assert col.values == [1, 2, 1001] and col.type_name == "uint32"

    col = picasa_db.read_pmp(
        _write_pmp(db3 / "albumdata_albumcontactids.pmp", "uint64",
                   [0, int(CID_DB3, 16)]))
    assert col.values == [0, 0xCA5C88CA60F42C0B]

    entries = picasa_db.read_thumbindex(_write_thumbindex(
        db3 / "thumbindex.db", [
            ("C:\\lib\\Trip\\", 0x01, None),
            ("a.jpg", 0x02, 0),
            ("", 0xE9, 1),
        ]))
    assert [e.is_folder for e in entries] == [True, False, False]
    assert entries[1].parent == 0 and entries[1].ftype_name == "jpeg"
    assert entries[2].is_facecrop and entries[2].parent == 1
    assert picasa_db.thumbindex_full_paths(entries) == [
        "C:\\lib\\Trip\\", "C:\\lib\\Trip\\a.jpg", ""]


def test_translate_db3_path_drives_case_and_misses(tmp_path: Path) -> None:
    """§8 path translation: drive letters compare stripped (a library
    moved from C: to Q: still joins), separators normalize, components
    match case-insensitively while the returned key keeps the db3
    spelling, the root itself is "", and a path outside the library —
    or shorter than it — is None (import-report material, never an
    error)."""
    from db3rescue import translate_db3_path

    root = tmp_path / "lib"
    trip = _db3_machine_path(root / "Trip")        # Q:-drive spelling
    assert translate_db3_path(trip + "a.jpg", root) == "Trip/a.jpg"
    assert translate_db3_path(trip, root) == "Trip"
    assert translate_db3_path(_db3_machine_path(root), root) == ""
    assert translate_db3_path(trip.upper() + "A.JPG", root) == "TRIP/A.JPG"
    assert translate_db3_path("Q:\\elsewhere\\Trip\\a.jpg", root) is None
    assert translate_db3_path("Q:\\", root) is None


def test_default_db3_dir_discovery(monkeypatch, tmp_path: Path) -> None:
    """default_db3_dir finds %LocalAppData%\\Google\\Picasa2\\db3 when it
    exists and returns None (not a phantom path) when the env var or the
    directory is absent — same fail-soft shape as contacts/.pal."""
    from db3rescue import default_db3_dir

    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert default_db3_dir() is None
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_db3_dir() is None               # dir absent
    d = tmp_path / "Google" / "Picasa2" / "db3"
    d.mkdir(parents=True)
    assert default_db3_dir() == d


def test_db3_person_album_rescue_end_to_end(db3_library: Path,
                                            tmp_path: Path) -> None:
    """The class-4 rescue end to end: a db3 person album (category 8,
    name + albumcontactids) names a contact id the ini faces= carries
    but nothing else names — the name fills the gap (§4), resolves the
    face, joins the registry source-flagged, and the rescue is an
    import-report entry; the agreeing virtual face row produces NO
    residue note. Without the db3 the same scan leaves the gap."""
    import picasa_db

    db3 = _make_person_db3(
        tmp_path / "db3", _db3_machine_path(db3_library / "Trip"),
        ["a.jpg", "b.jpg"], face_parent=1)         # the face is on a.jpg
    cat = scan_library(db3_library, db3_dir=db3)

    a = next(p for p in cat.photos if p.rel == "Trip/a.jpg")
    assert a.faces == ((picasa_db.parse_rect64("6800600097ff9fff"),
                        CID_DB3, PERSON_DB3),)
    assert cat.contacts[CID_DB3] == PERSON_DB3
    assert cat.db3_contacts == {CID_DB3}
    kinds = [(e.kind, e.subject) for e in cat.report.entries]
    assert ("db3_person_rescued", CID_DB3) in kinds
    assert not any(k == "db3_face_residue" for k, _s in kinds)
    assert not any(k == "db3_path_unresolved" for k, _s in kinds)
    rescued = next(e for e in cat.report.entries
                   if e.kind == "db3_person_rescued")
    assert rescued.source == "db3" and PERSON_DB3 in rescued.detail

    plain = scan_library(db3_library)              # no db3: the gap shows
    ap = next(p for p in plain.photos if p.rel == "Trip/a.jpg")
    assert ap.faces[0][2] is None and CID_DB3 not in plain.contacts


def test_db3_gap_fill_only_never_renames(db3_library: Path,
                                         tmp_path: Path) -> None:
    """§4 rank ini/contacts.xml > db3: a name either source already
    provides NEVER changes — a divergent db3 person album is an
    import-report entry recording both names, not a rename — and an
    agreeing db3 produces no notes and no source flag at all."""
    ini = db3_library / "Trip" / ".picasa.ini"
    db3 = _make_person_db3(
        tmp_path / "db3", _db3_machine_path(db3_library / "Trip"),
        ["a.jpg", "b.jpg"], face_parent=1)

    # [Contacts2] already names the id: kept verbatim, conflict reported
    ini.write_text("[Contacts2]\r\n"
                   f"{CID_DB3}=Ini Name;;\r\n"
                   "[a.jpg]\r\n"
                   f"faces=rect64(6800600097ff9fff),{CID_DB3}\r\n")
    cat = scan_library(db3_library, db3_dir=db3)
    a = next(p for p in cat.photos if p.rel == "Trip/a.jpg")
    assert a.faces[0][2] == "Ini Name"
    assert cat.contacts[CID_DB3] == "Ini Name"
    assert cat.db3_contacts == set()
    conf = [e for e in cat.report.entries if e.kind == "db3_name_conflict"]
    assert len(conf) == 1 and conf[0].subject == CID_DB3
    assert "Ini Name" in conf[0].detail and PERSON_DB3 in conf[0].detail
    assert not any(e.kind == "db3_person_rescued"
                   for e in cat.report.entries)

    # contacts.xml names it: same rule (xml outranks db3 too)
    ini.write_text("[a.jpg]\r\n"
                   f"faces=rect64(6800600097ff9fff),{CID_DB3}\r\n")
    cat = scan_library(db3_library, contacts={CID_DB3: "Xml Name"},
                       db3_dir=db3)
    a = next(p for p in cat.photos if p.rel == "Trip/a.jpg")
    assert a.faces[0][2] == "Xml Name"
    assert cat.db3_contacts == set()
    assert any(e.kind == "db3_name_conflict" for e in cat.report.entries)

    # agreement is not a conflict: same name everywhere -> no db3 notes
    ini.write_text("[Contacts2]\r\n"
                   f"{CID_DB3}={PERSON_DB3};;\r\n"
                   "[a.jpg]\r\n"
                   f"faces=rect64(6800600097ff9fff),{CID_DB3}\r\n")
    cat = scan_library(db3_library, db3_dir=db3)
    assert cat.db3_contacts == set()
    assert not any(e.kind.startswith("db3_") for e in cat.report.entries)


def test_db3_untag_not_resurrected_fixture_026(tmp_path: Path) -> None:
    """The fixture-026 shape, exactly: Reset Faces removed the ini
    faces= line but left the [Contacts2] line, the db3 person album, and
    the virtual face row behind. An absent ini faces= is an
    AUTHORITATIVE UNTAG — the db3 residue must not resurrect the face
    (it becomes an import-report entry instead). The pure-db3 variant
    (no [Contacts2] residue either) still rescues the NAME — people
    albums exist only in db3 — but the person ends with zero tagged
    photos: no resurrect through the back door."""
    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "b.jpg")
    ini = root / "Trip" / ".picasa.ini"
    ini.write_text("[Contacts2]\r\n"
                   f"{CID_DB3}={PERSON_DB3};;\r\n"   # 026: line REMAINS
                   "[b.jpg]\r\n"
                   "backuphash=22344\r\n")           # rewritten, no faces=
    db3 = _make_person_db3(
        tmp_path / "db3", _db3_machine_path(root / "Trip"),
        ["b.jpg"], face_parent=1)

    cat = scan_library(root, db3_dir=db3)
    b = next(p for p in cat.photos if p.rel == "Trip/b.jpg")
    assert b.faces == ()                             # NOT resurrected
    assert cat.contacts[CID_DB3] == PERSON_DB3       # named by the ini...
    assert cat.db3_contacts == set()                 # ...not by the rescue
    res = [e for e in cat.report.entries if e.kind == "db3_face_residue"]
    assert len(res) == 1 and res[0].subject == "Trip/b.jpg"
    assert "026" in res[0].detail and "not resurrected" in res[0].detail

    ini.write_text("[b.jpg]\r\nbackuphash=1\r\n")    # pure-db3 residue
    cat = scan_library(root, db3_dir=db3)
    b = next(p for p in cat.photos if p.rel == "Trip/b.jpg")
    assert b.faces == ()
    assert cat.contacts[CID_DB3] == PERSON_DB3       # name rescued...
    assert cat.db3_contacts == {CID_DB3}             # ...and source-flagged
    assert not any(p.faces for p in cat.photos)      # zero tagged photos
    assert any(e.kind == "db3_face_residue" for e in cat.report.entries)


def test_db3_unresolvable_paths_reported_not_fatal(db3_library: Path,
                                                   tmp_path: Path) -> None:
    """A db3 whose face row sits on a photo OUTSIDE this library (a
    watched folder we don't browse, or a moved tree): the path fails to
    translate and becomes an import-report entry — never an error — and
    the name rescue itself still lands (it needs no path)."""
    db3 = _make_person_db3(tmp_path / "db3", "Q:\\somewhere\\else",
                           ["a.jpg", "b.jpg"], face_parent=1)
    cat = scan_library(db3_library, db3_dir=db3)

    a = next(p for p in cat.photos if p.rel == "Trip/a.jpg")
    assert a.faces[0][2] == PERSON_DB3               # rescue still lands
    assert cat.db3_contacts == {CID_DB3}
    bad = [e for e in cat.report.entries if e.kind == "db3_path_unresolved"]
    assert len(bad) == 1
    assert bad[0].subject == "Q:\\somewhere\\else\\a.jpg"
    assert "cannot join" in bad[0].detail and bad[0].source == "db3"


def test_db3_fail_soft_absent_and_corrupt(db3_library: Path,
                                          tmp_path: Path) -> None:
    """Fail-soft plumbing: an empty db3 dir (a fresh install has no
    albumdata) rescues nothing silently; corrupt pmp columns degrade to
    no rescue without sinking the scan; a corrupt thumbindex still lets
    the NAME rescue land and surfaces the degraded face join as an
    import note instead of an exception."""
    empty = tmp_path / "empty-db3"
    empty.mkdir()
    cat = scan_library(db3_library, db3_dir=empty)
    assert cat.db3_contacts == set() and not cat.report.entries

    broken = tmp_path / "broken-db3"
    broken.mkdir()
    (broken / "albumdata_category.pmp").write_bytes(b"\x00\x01 garbage")
    (broken / "albumdata_name.pmp").write_bytes(b"junk")
    (broken / "albumdata_albumcontactids.pmp").write_bytes(b"junk")
    cat = scan_library(db3_library, db3_dir=broken)
    assert cat.db3_contacts == set() and not cat.report.entries

    half = _make_person_db3(
        tmp_path / "half-db3", _db3_machine_path(db3_library / "Trip"),
        ["a.jpg", "b.jpg"], face_parent=1)
    (half / "thumbindex.db").write_bytes(b"\xde\xad\xbe\xef corrupt")
    cat = scan_library(db3_library, db3_dir=half)
    assert cat.contacts[CID_DB3] == PERSON_DB3       # names still rescued
    assert cat.db3_contacts == {CID_DB3}
    assert any(e.kind == "db3_unreadable" for e in cat.report.entries)


def test_db3_catalog_roundtrip_v11(db3_library: Path,
                                   tmp_path: Path) -> None:
    """From CATALOG_VERSION 11 on: rescued names on faces/registry and the
    db3_contacts source flag survive the persisted catalog, and a v10
    catalog (scanned before the rescue existed) is rejected so a warm
    start cold-rebuilds instead of silently un-naming rescued people.
    (== 11: the newest version-gate test pins the exact value.)"""
    import catalog as catmod

    assert catmod.CATALOG_VERSION == 11
    db3 = _make_person_db3(
        tmp_path / "db3", _db3_machine_path(db3_library / "Trip"),
        ["a.jpg", "b.jpg"], face_parent=1)
    cat = scan_library(db3_library, db3_dir=db3)
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    loaded = load_catalog(path, db3_library)
    assert loaded is not None
    assert loaded.db3_contacts == {CID_DB3}
    assert [p.faces for p in loaded.photos] == [p.faces for p in cat.photos]
    assert loaded.contacts == cat.contacts

    data = json.loads(path.read_text())
    data["version"] = 10                   # pre-db3-rescue format
    path.write_text(json.dumps(data))
    assert load_catalog(path, db3_library) is None


def test_db3_people_sidebar_source_flag(db3_library: Path,
                                        tmp_path: Path) -> None:
    """db3-rescued people join the People sidebar like any named person
    — live counts, click-to-filter — with their provenance flagged in
    the tooltip; an ini-named person alongside shows the flag is
    per-person, not global."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    (db3_library / "Trip" / ".picasa.ini").write_text(
        "[Contacts2]\r\n"
        "bbbbbbbbbbbbbbb2=Ini Bob;;\r\n"
        "[a.jpg]\r\n"
        f"faces=rect64(6800600097ff9fff),{CID_DB3}\r\n"
        "[b.jpg]\r\n"
        "faces=rect64(1234),bbbbbbbbbbbbbbb2\r\n")
    db3 = _make_person_db3(
        tmp_path / "db3", _db3_machine_path(db3_library / "Trip"),
        ["a.jpg", "b.jpg"], face_parent=1)
    cat = scan_library(db3_library, db3_dir=db3)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    def item_for(kind: str, key: str):
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == (kind, key):
                return it.value()
            it += 1
        return None

    rescued = item_for("person", PERSON_DB3)
    assert rescued is not None and rescued.text(0).endswith("(1)")
    assert "db3" in rescued.toolTip(0)
    named = item_for("person", "Ini Bob")
    assert named is not None and named.toolTip(0) == ""

    win._sidebar_clicked(rescued, 0)     # counts/filters live like any person
    assert [cat.photos[i].rel for i in win.grid.display] == ["Trip/a.jpg"]


# ---------------------------------------------------------------------------
# Selection tray (fauxcasa-q6l.2): persistent CROSS-FOLDER Hold/Clear +
# typed readout (spec §5). Decisions under test (tray.py module doc):
# identity by REL PATH (survives reconcile index remaps), HOLD ORDER
# (insertion order — a tray is a deliberately assembled staging set),
# and the never-silent vanish note (N7). MainWindow.selection_context()
# is the M2 output-action attachment point.
# ---------------------------------------------------------------------------


def _tray_window(tmp_path: Path, with_cache: bool = False):
    """A MainWindow over a synthetic two-folder library (2 + 2 photos,
    one single-member album), optionally with a built+bound fcache so
    tray thumbs have real pixels. Display order (sorted rels) is
    f0/a.jpg, f0/b.jpg, f1/c.jpg, f1/d.jpg -> catalog indices 0..3."""
    _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f0" / "a.jpg")
    make_jpeg(root / "f0" / "b.jpg")
    make_jpeg(root / "f1" / "c.jpg")
    make_jpeg(root / "f1" / "d.jpg")
    (root / "f0" / ".picasa.ini").write_text(
        "[a.jpg]\r\nalbums=cafecafecafecafecafecafecafecafe\r\n"
        "[.album:cafecafecafecafecafecafecafecafe]\r\nname=Best\r\n"
    )
    cat = scan_library(root)
    thumbs = None
    if with_cache:
        built = thumbcache.build_cache(cat, tmp_path / "c")
        thumbs = thumbcache.load_cache(built.path)
        thumbcache.bind(thumbs, cat)
    win = MainWindow(cat, thumbs, cache_dir=None, build_dir=None)
    assert [cat.photos[i].rel for i in win.grid.display] == [
        "f0/a.jpg", "f0/b.jpg", "f1/c.jpg", "f1/d.jpg"]
    return win


def _sidebar_click(win, kind: str, key: str) -> None:
    """Click the sidebar item carrying (kind, key) through the real
    itemClicked signal, so both connected slots (_sidebar_clicked and
    the tray-readout refresh) run in connection order."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItemIterator

    it = QTreeWidgetItemIterator(win.tree)
    while it.value():
        if it.value().data(0, Qt.ItemDataRole.UserRole) == (kind, key):
            win.tree.itemClicked.emit(it.value(), 0)
            return
        it += 1
    raise AssertionError(f"sidebar item {(kind, key)} not found")


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


# ---------------------------------------------------------------------------
# Video support (fauxcasa-v46.2): Picasa's documented video extension list in
# BOTH walkers (lockstep, or caches stop binding), PyAV poster-frame decode
# routed by extension ahead of any content sniff (per the merged decode-
# service design §3c: PyAV in-process, never an ffmpeg subprocess), ini
# attachment to video files (star/caption/albums/geotag + width=/height= dim
# seeds), corrupt-video fail-soft, media kind through the catalog round-trip,
# the grid's play badge, and the viewer's honest playback-pending note.
# Playback itself is fauxcasa-v46.3 (gated on the §3c sandbox-valve ruling).
#
# Fixture provenance (privacy rule: NEVER real family data): _make_clip
# encodes a tiny solid-color mpeg4 clip from scratch with PyAV — the same
# engine the poster seam decodes with — so every video fixture is synthetic
# and generated in-test.
# ---------------------------------------------------------------------------


def _make_clip(path: Path, color=(200, 60, 40), w: int = 64, h: int = 48,
               nframes: int = 8, rate: int = 8) -> Path:
    """A tiny real video: solid-`color` frames, mpeg4, in whatever
    container the extension names (.mp4 muxes moov-at-end by default —
    exactly the shape that defeats pipe input and needs seekable reads).
    8 frames at 8 fps = 1 s; pass nframes=2 for a sub-second clip that
    forces the poster's seek-past-the-end fallback."""
    import av
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=rate)
        stream.width, stream.height = w, h
        stream.pix_fmt = "yuv420p"
        img = Image.new("RGB", (w, h), color)
        for _ in range(nframes):
            for pkt in stream.encode(av.VideoFrame.from_image(img)):
                container.mux(pkt)
        for pkt in stream.encode():   # flush the encoder
            container.mux(pkt)
    return path


def test_video_extensions_in_both_walkers(tmp_path: Path) -> None:
    """Picasa's documented video list (files-supported-by-picasa3.md "For
    playback in Picasa": 17 extensions, plus .mpeg as the four-letter
    alias of .mpg — the .jpeg/.jpg precedent) is in BOTH EXTS sets, in
    lockstep; audio-only .wma/.mp3 stay excluded; both walks pick video
    files up case-insensitively; and the size scan-filter always KEEPS
    videos (their dims are unknowable without a decode)."""
    import importlib.util

    import catalog
    import videoload

    spec = importlib.util.spec_from_file_location(
        "mtc", REPO / "scripts" / "make-thumbcache.py")
    mtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtc)

    documented = {".mpg", ".mpeg", ".mod", ".mmv", ".tod", ".wmv", ".asf",
                  ".avi", ".divx", ".mov", ".m4v", ".3gp", ".3g2", ".mp4",
                  ".m2t", ".m2ts", ".mts", ".mkv"}
    assert videoload.VIDEO_EXTS == documented
    assert mtc.VIDEO_EXTS == videoload.VIDEO_EXTS  # the script's mirror
    assert catalog.EXTS == mtc.EXTS                # the whole lockstep set
    assert documented <= catalog.EXTS
    assert not (catalog.EXTS & {".wma", ".mp3"})   # audio is not walked

    root = tmp_path / "lib"
    root.mkdir()
    for name in ("a.MP4", "b.avi", "c.MoV", "d.m2ts"):
        (root / name).write_bytes(b"stub")         # walk checks suffix only
    make_jpeg(root / "e.jpg")
    walked = [p.name for p in walk_library(root)]
    assert sorted(walked) == ["a.MP4", "b.avi", "c.MoV", "d.m2ts", "e.jpg"]
    script_walk = sorted(p for p in root.rglob("*")
                         if p.suffix.lower() in mtc.EXTS and p.is_file())
    assert [p.name for p in script_walk] == walked

    # the size filter judges video dims unknowable and keeps every video
    # (QImageReader must never sniff video bytes — videoload module doc)
    kept = {p.name for p in walk_library(root, ScanFilter(min_width=10000))}
    assert kept == {"a.MP4", "b.avi", "c.MoV", "d.m2ts"}


def test_video_poster_thumb_media_kind_and_duration(tmp_path: Path) -> None:
    """A real clip indexes to a poster-frame thumbnail via PyAV: the cached
    thumb has the clip's dimensions (64x48 < 256: never upscaled) and its
    solid frame color; a sub-second clip exercises the seek-past-the-end
    fallback to the first decodable frame; media kind is set by extension;
    sha256/size/mtime identity fills as usual; and the videoload seam's
    poster_frame returns the packed fixed-shape RGB buffer plus a sane
    probe_duration."""
    import videoload

    root = tmp_path / "lib"
    _make_clip(root / "clip.mp4", color=(200, 60, 40))            # 1 s
    _make_clip(root / "short.avi", color=(40, 60, 200), nframes=2)  # 0.25 s
    make_jpeg(root / "still.jpg")
    cat = scan_library(root)
    assert {p.rel: p.media for p in cat.photos} == {
        "clip.mp4": "video", "short.avi": "video", "still.jpg": "image"}

    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    by = dict(zip(cache.files, cache.entries))
    _o, length, w, h = by["clip.mp4"]
    assert length > 0 and (w, h) == (64, 48)
    img = _thumb_qimage(cache, cache.files.index("clip.mp4"))
    px = img.pixelColor(32, 24)
    # solid (200, 60, 40) through yuv420p + JPEG q80; allow codec drift
    assert abs(px.red() - 200) < 40 and px.red() > px.blue()

    _o, length, w, h = by["short.avi"]                 # fallback path
    assert length > 0 and (w, h) == (64, 48)
    px = _thumb_qimage(cache, cache.files.index("short.avi")) \
        .pixelColor(32, 24)
    assert px.blue() > px.red()                        # the blue clip

    for p in cat.photos:                               # N6 identity as usual
        assert p.sha256 and p.size > 0 and p.mtime > 0

    # the seam's raw shape: packed RGB888, exactly w*3*h bytes (the fixed-
    # shape pixel contract the sandboxed decode service will validate)
    data = (root / "clip.mp4").read_bytes()
    buf, w, h = videoload.poster_frame(data)
    assert (w, h) == (64, 48) and len(buf) == w * 3 * h
    dur = videoload.probe_duration(data)
    assert dur is not None and 0.5 <= dur <= 2.0
    assert videoload.poster_frame(b"not a video") is None
    assert videoload.probe_duration(b"not a video") is None


def test_video_corrupt_is_error_tile(tmp_path: Path) -> None:
    """Corrupt videos — outright garbage bytes under two video extensions —
    yield the existing zero-length error tile and never abort the build;
    the good neighbors (a still AND a decodable clip) still index."""
    root = tmp_path / "lib"
    root.mkdir()
    (root / "garbage.mp4").write_bytes(b"\x00\x01 not a video" * 64)
    (root / "noise.wmv").write_bytes(b"\xff\xd8 also not one" * 64)
    _make_clip(root / "ok.avi")
    make_jpeg(root / "ok.jpg")
    cat = scan_library(root)
    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    lengths = {rel: length for rel, (_o, length, _w, _h) in
               zip(cache.files, cache.entries)}
    assert lengths["garbage.mp4"] == 0         # error tile
    assert lengths["noise.wmv"] == 0           # error tile
    assert lengths["ok.avi"] > 0               # neighbors unharmed
    assert lengths["ok.jpg"] > 0


def test_video_ini_attachment_and_dim_seed(tmp_path: Path) -> None:
    """ini sections for video files flow through the existing parse:
    star/caption/albums/geotag attach by filename exactly like a photo's,
    width=/height= seed Photo.dims (malformed values fail soft to None) —
    and the indexer's SKIPPED in-file metadata pass (exiv2 video support
    is patchy) leaves the ini values in force after a build."""
    root = tmp_path / "lib"
    _make_clip(root / "clip00.avi")
    _make_clip(root / "clip01.mp4")
    make_jpeg(root / "p.jpg")
    uid = "d4e5f60718293a4b5c6d7e8f90a1b2c3"
    (root / ".picasa.ini").write_text(
        f"[.album:{uid}]\r\nname=Movies\r\n"
        "[clip00.avi]\r\nstar=yes\r\ncaption=First swim\r\n"
        f"albums={uid}\r\ngeotag=48.858844,2.294351\r\n"
        "width=640\r\nheight=480\r\n"
        "[clip01.mp4]\r\nwidth=banana\r\nheight=480\r\n")
    cat = scan_library(root)
    a = next(p for p in cat.photos if p.rel == "clip00.avi")
    assert a.media == "video" and a.star == 1
    assert a.caption == "First swim"
    assert a.albums == (uid,)
    assert a.geotag == pytest.approx((48.858844, 2.294351))
    assert a.dims == (640, 480)
    assert cat.albums[uid].members == [cat.photos.index(a)]
    b = next(p for p in cat.photos if p.rel == "clip01.mp4")
    assert b.dims is None                      # malformed width= fails soft

    assert thumbcache.build_cache(cat, tmp_path / "c") is not None
    assert a.caption == "First swim" and a.star == 1  # ini stays in force


def test_video_catalog_roundtrip_media_and_dims(tmp_path: Path) -> None:
    """media kind and ini-seeded dims survive save_catalog/load_catalog:
    dims persist (`wh` rows — the warm path never re-reads inis) while
    media is DERIVED from the extension on load, never stored; and a
    pre-v6 catalog (walked without videos) is rejected so a warm start
    can never silently hide every video in the library."""
    import catalog as catmod

    root = tmp_path / "lib"
    _make_clip(root / "c.mp4")
    make_jpeg(root / "p.jpg")
    (root / ".picasa.ini").write_text("[c.mp4]\r\nwidth=64\r\nheight=48\r\n")
    cat = scan_library(root)
    assert thumbcache.build_cache(cat, tmp_path / "cc") is not None
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    loaded = load_catalog(path, root)
    assert loaded is not None
    v = next(p for p in loaded.photos if p.rel == "c.mp4")
    assert v.media == "video" and v.dims == (64, 48)
    assert v.sha256 == next(p for p in cat.photos
                            if p.rel == "c.mp4").sha256
    s = next(p for p in loaded.photos if p.rel == "p.jpg")
    assert s.media == "image" and s.dims is None
    rows = json.loads(path.read_text())["photos"]
    assert all("media" not in r for r in rows)  # derived, never persisted

    data = json.loads(path.read_text())
    data["version"] = catmod.CATALOG_VERSION - 1
    path.write_text(json.dumps(data))
    assert load_catalog(path, root) is None     # pre-video: cold-rebuild


def test_grid_video_play_badge_paint_smoke(tmp_path: Path) -> None:
    """The video play badge paints in its OWN corner (bottom-left; star
    owns top-right, geotag pin bottom-right) without incident alongside
    both other badges: the badge-center pixel of a video tile is the play
    glyph's white, the same spot on a non-video neighbor is not."""
    from PySide6.QtGui import QImage

    from grid import _play_polygon

    g = _selection_grid(tmp_path)
    cat = g.catalog
    d = g.display
    cat.photos[d[0]].media = "video"
    cat.photos[d[0]].star = 2                       # all three corners at once
    cat.photos[d[0]].geotag = (60.72125, -135.05685)
    shot = g.viewport().grab().toImage().convertToFormat(
        QImage.Format.Format_RGB32)
    assert not shot.isNull()

    s = max(7.0, g.tile / 14.0)

    def badge_px(n: int):
        r = g._item_rect(g.groups[0], n)            # scroll is 0: same coords
        c = shot.pixelColor(int(r.x() + s + 2), int(r.bottom() - s - 2))
        return (c.red(), c.green(), c.blue())

    assert all(abs(v - 235) < 25 for v in badge_px(0))      # the play glyph
    assert not all(abs(v - 235) < 25 for v in badge_px(1))  # plain neighbor

    # shape sanity: a right-pointing triangle — apex at the vertical center
    poly = _play_polygon(10.0, 10.0, 8.0)
    assert poly.size() == 3
    assert poly.at(1).x() > poly.at(0).x() and poly.at(1).y() == 10.0


def test_viewer_video_poster_and_pending_note(tmp_path: Path) -> None:
    """viewer.load_original routes video by extension to the poster seam:
    a clip's poster decodes at native size (proven by dimensions AND the
    frame color), the Picasa rotate= turns compose on top exactly like any
    format, a corrupt video returns a null QImage (fail-soft), and the
    viewer's info line carries the honest M1 placeholder — the poster is
    shown, playback is named as pending v46.3, never attempted."""
    _offscreen_app()
    from viewer import ViewerPage, load_original

    root = tmp_path / "lib"
    clip = _make_clip(root / "clip.mp4", color=(200, 60, 40))
    make_jpeg(root / "p.jpg")
    img = load_original(str(clip), 0)
    assert (img.width(), img.height()) == (64, 48)
    px = img.pixelColor(32, 24)
    assert abs(px.red() - 200) < 40 and px.red() > px.blue()
    img = load_original(str(clip), 1)          # rotate= composes on top
    assert (img.width(), img.height()) == (48, 64)

    bad = root / "bad.avi"
    bad.write_bytes(b"garbage" * 100)
    assert load_original(str(bad), 0).isNull()

    cat = scan_library(root)
    v = ViewerPage(cat, None)
    v.resize(320, 240)
    v.show()
    idx = next(i for i, p in enumerate(cat.photos) if p.rel == "clip.mp4")
    v.show_photo([idx], 0)
    assert "video — playback pending (v46.3)" in v._info_text(cat.photos[idx])
    still = next(p for p in cat.photos if p.rel == "p.jpg")
    assert "playback pending" not in v._info_text(still)
    assert not v.grab().isNull()               # the note paints w/o incident
    v.quiesce()


def test_make_thumbcache_video_paths(tmp_path: Path) -> None:
    """The standalone PIL builder mirrors the same routing (in PyAV+PIL
    terms): a real clip thumbs to its poster frame at native size with the
    frame's color; corrupt video bytes are the error tile."""
    import importlib.util
    import io

    from PIL import Image

    spec = importlib.util.spec_from_file_location(
        "mtc", REPO / "scripts" / "make-thumbcache.py")
    mtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtc)

    clip = _make_clip(tmp_path / "c.mp4", color=(200, 60, 40))
    (blob, w, h), = mtc._make_thumb(clip, [256])
    assert blob and (w, h) == (64, 48)
    px = Image.open(io.BytesIO(blob)).getpixel((32, 24))
    assert abs(px[0] - 200) < 40 and px[0] > px[2]

    bad = tmp_path / "bad.wmv"
    bad.write_bytes(b"not a movie")
    assert mtc._make_thumb(bad, [256]) == [(b"", 0, 0)]


# ---------------------------------------------------------------------------
# Adopt-mode backfill (fauxcasa-cam.12): --thumbs binds a prebuilt fcache
# without running the indexer, so the catalog starts with no identity
# signals (N6 — reconcile blind to in-place edits), ini-only metadata (§4
# tier-1 never applied), and no mtimes (Recently Updated honestly 0).
# thumbcache.backfill_catalog is the read side of the indexer without the
# thumbnail work — read_photo_meta + apply_photo_meta, the SAME factored
# functions build_cache runs — applied in catalog order behind a bounded
# two-reader window so the persisted cursor is a contiguous frontier and a
# killed pass resumes exactly where it left off.
# ---------------------------------------------------------------------------

from catalog import (  # noqa: E402
    BACKFILL_COMPLETE,
    BACKFILL_IN_PROGRESS,
    BACKFILL_NOT_STARTED,
)


def _adopted_catalog(root: Path, tmp_path: Path):
    """main()'s --thumbs flow in miniature: build a prebuilt fcache from
    one walk, bind a FRESH scan (ini-only, signal-less) to it, mark the
    catalog NOT_STARTED and persist it — returns (catalog, catalog_path)
    ready for backfill_catalog."""
    built = thumbcache.build_cache(scan_library(root), tmp_path / "prebuilt")
    assert built is not None
    cat = scan_library(root)
    cache = thumbcache.load_cache(built.path)
    thumbcache.bind(cache, cat)
    cat.backfill_state = BACKFILL_NOT_STARTED
    cat_path = tmp_path / "catalog.json"
    save_catalog(cat, cat_path)
    return cat, cat_path


def test_backfill_matches_indexer_read_side(tmp_path: Path) -> None:
    """The core parity claim: a backfilled adopt-mode catalog is
    indistinguishable from an indexed one across every read-side field —
    identity signals AND the §4 tier-1 precedence merge (in-file caption/
    keywords beat ini, ini survives where the file carries none, EXIF GPS
    beats geotag=, XMP Rating beats bare star=yes) — and the merged result
    persists for the next warm start."""
    root = tmp_path / "lib"
    # a: ini caption/keywords + in-file XMP -> in-file wins
    write_jpeg_meta(root / "f" / "a.jpg",
                    xmp=_xmp_app1("in-file cap", ("ifkw",)))
    # b: ini caption only -> survives the pass untouched
    make_jpeg(root / "f" / "b.jpg")
    # c: in-file EXIF date + GPS + XMP Rating over ini star=yes/geotag=
    _meta_jpeg(root / "f" / "c.jpg",
               date_time_original="1899:03:02 14:00:00",
               gps=WHITEHORSE, rating=3)
    # d: nothing anywhere (signals only)
    make_jpeg(root / "f" / "d.jpg")
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\ncaption=ini cap a\r\nkeywords=inikw\r\n"
        "[b.jpg]\r\ncaption=ini cap b\r\n"
        "[c.jpg]\r\nstar=yes\r\ngeotag=-33.856800,151.215300\r\n")

    ref = scan_library(root)                     # reference: the indexer
    assert thumbcache.build_cache(ref, tmp_path / "ref") is not None

    cat, cat_path = _adopted_catalog(root, tmp_path)
    a = next(p for p in cat.photos if p.name == "a.jpg")
    assert a.caption == "ini cap a"              # the gap: ini tier showing
    assert a.sha256 is None and a.mtime < 0 and a.size < 0

    result = thumbcache.backfill_catalog(cat, cat_path)
    assert result is not None
    assert result.photos == len(cat.photos) and result.workers == 2
    assert cat.backfill_state == BACKFILL_COMPLETE

    for got, want in zip(cat.photos, ref.photos):
        assert got.rel == want.rel
        assert (got.size, got.mtime, got.sha256) == \
            (want.size, want.mtime, want.sha256)
        assert got.caption == want.caption
        assert got.keywords == want.keywords
        assert got.date_taken == want.date_taken
        assert got.geotag == want.geotag
        assert got.star == want.star
    by = {p.name: p for p in cat.photos}
    assert by["a.jpg"].caption == "in-file cap"          # tier-1 applied
    assert by["a.jpg"].keywords == ("ifkw",)
    assert by["b.jpg"].caption == "ini cap b"            # ini fallback kept
    assert by["c.jpg"].star == 3                          # Rating over star=yes
    assert by["c.jpg"].geotag == pytest.approx(WHITEHORSE)  # GPS over geotag=
    assert by["c.jpg"].date_taken == "1899-03-02T14:00:00"  # no year floor
    assert len(by["d.jpg"].sha256) == 64 and by["d.jpg"].mtime >= 0

    # the merged result is durable: the next launch warm-loads it complete
    loaded = load_catalog(cat_path, root)
    assert loaded is not None
    assert loaded.backfill_state == BACKFILL_COMPLETE
    assert next(p for p in loaded.photos
                if p.name == "a.jpg").caption == "in-file cap"
    # ...and a complete catalog's file shape carries no backfill key
    assert "backfill" not in json.loads(cat_path.read_text())


def test_backfill_interrupt_resume_and_periodic_persist(
        tmp_path: Path, monkeypatch) -> None:
    """Kill-safety: the pass persists every persist_every photos AND on
    cancel, recording IN_PROGRESS + a contiguous cursor; a relaunch loads
    that catalog and resumes from the cursor, never re-reading the photos
    already applied."""
    import threading

    root = tmp_path / "lib"
    for n in range(6):
        make_jpeg(root / "f" / f"p{n}.jpg")
    cat, cat_path = _adopted_catalog(root, tmp_path)

    saves: list[int] = []
    real_save = thumbcache.save_catalog
    real_save_retrying = thumbcache.save_catalog_retrying

    def counting_save(c, p):
        saves.append(c.backfill_cursor)
        real_save(c, p)

    def counting_save_retrying(c, p, attempts=5, backoff=0.1):
        # terminal (must=True) saves now go through save_catalog_retrying
        saves.append(c.backfill_cursor)
        real_save_retrying(c, p, attempts=attempts, backoff=backoff)

    monkeypatch.setattr(thumbcache, "save_catalog", counting_save)
    monkeypatch.setattr(thumbcache, "save_catalog_retrying", counting_save_retrying)

    stop = threading.Event()
    assert thumbcache.backfill_catalog(
        cat, cat_path, cancel=stop, persist_every=2,
        progress=lambda done, total: stop.set() if done >= 3 else None,
    ) is None                                    # cancelled mid-pass
    # results apply IN ORDER, so the checkpoints are deterministic: the
    # periodic save at 2, then the cancel checkpoint at 3
    assert saves == [2, 3]

    disk = load_catalog(cat_path, root)          # what a relaunch loads
    assert disk is not None
    assert disk.backfill_state == BACKFILL_IN_PROGRESS
    assert disk.backfill_cursor == 3
    assert all(p.sha256 and p.mtime >= 0 for p in disk.photos[:3])
    assert all(p.sha256 is None and p.mtime < 0 for p in disk.photos[3:])

    read: list[str] = []
    real_read = thumbcache.read_photo_meta

    def recording_read(r, photo):
        read.append(photo.rel)
        return real_read(r, photo)

    monkeypatch.setattr(thumbcache, "read_photo_meta", recording_read)
    result = thumbcache.backfill_catalog(disk, cat_path)
    assert result is not None and result.photos == 3   # the tail only
    assert sorted(read) == ["f/p3.jpg", "f/p4.jpg", "f/p5.jpg"]
    assert disk.backfill_state == BACKFILL_COMPLETE
    assert all(p.sha256 for p in disk.photos)
    again = load_catalog(cat_path, root)
    assert again is not None and again.backfill_state == BACKFILL_COMPLETE


def test_backfill_survives_transient_checkpoint_failure(
        tmp_path: Path, monkeypatch) -> None:
    """Windows regression (caught live at photo 96,500 of the 100k
    benchmark run): os.replace onto a catalog.json some reader momentarily
    holds open without FILE_SHARE_DELETE (antivirus, the search indexer,
    any tool peeking at the file) raises a transient PermissionError. A
    PERIODIC checkpoint failing must not abort the multi-minute pass — it
    retries at the next photo — and the terminal save still lands."""
    root = tmp_path / "lib"
    for n in range(6):
        make_jpeg(root / "f" / f"p{n}.jpg")
    cat, cat_path = _adopted_catalog(root, tmp_path)

    calls = [0]
    real_save = thumbcache.save_catalog

    def flaky_save(c, p):
        calls[0] += 1
        if calls[0] == 1:                        # first periodic checkpoint
            raise PermissionError(5, "Access is denied")
        real_save(c, p)

    monkeypatch.setattr(thumbcache, "save_catalog", flaky_save)
    result = thumbcache.backfill_catalog(cat, cat_path, persist_every=2)
    assert result is not None and result.photos == 6   # pass not aborted
    assert calls[0] >= 2                         # ...and saves resumed
    disk = load_catalog(cat_path, root)
    assert disk is not None and disk.backfill_state == BACKFILL_COMPLETE
    assert all(p.sha256 for p in disk.photos)


def test_backfill_worker_cap_two(tmp_path: Path, monkeypatch) -> None:
    """Rate limiting is structural: BACKFILL_WORKERS is 2 (deliberately far
    below INDEX_WORKERS) and the pass never has more than that many reads
    in flight — a bounded submission window, not the indexer's
    fire-everything pool."""
    import threading
    import time as _time

    assert thumbcache.BACKFILL_WORKERS == 2
    root = tmp_path / "lib"
    for n in range(10):
        make_jpeg(root / "f" / f"p{n}.jpg")
    cat, cat_path = _adopted_catalog(root, tmp_path)

    lock = threading.Lock()
    active, peak = [0], [0]
    real_read = thumbcache.read_photo_meta

    def tracking_read(r, photo):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            _time.sleep(0.01)                    # widen the overlap window
            return real_read(r, photo)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(thumbcache, "read_photo_meta", tracking_read)
    assert thumbcache.backfill_catalog(cat, cat_path) is not None
    assert 1 <= peak[0] <= thumbcache.BACKFILL_WORKERS


def test_backfill_pause_parks_readers(tmp_path: Path, monkeypatch) -> None:
    """The low-priority hook: a set pause event parks the pass before any
    read is submitted (and between photos); clearing it lets the pass run
    to completion."""
    import threading
    import time as _time

    root = tmp_path / "lib"
    for n in range(4):
        make_jpeg(root / "f" / f"p{n}.jpg")
    cat, cat_path = _adopted_catalog(root, tmp_path)

    reads: list[str] = []
    real_read = thumbcache.read_photo_meta

    def recording_read(r, photo):
        reads.append(photo.rel)
        return real_read(r, photo)

    monkeypatch.setattr(thumbcache, "read_photo_meta", recording_read)
    pause = threading.Event()
    pause.set()                                  # paused before the start
    out: list = []
    t = threading.Thread(
        target=lambda: out.append(
            thumbcache.backfill_catalog(cat, cat_path, pause=pause)),
        daemon=True)
    t.start()
    _time.sleep(0.3)
    assert reads == [] and t.is_alive()          # parked: nothing read yet
    pause.clear()
    t.join(timeout=15)
    assert not t.is_alive()
    assert out and out[0] is not None and out[0].photos == 4
    assert len(reads) == 4
    assert cat.backfill_state == BACKFILL_COMPLETE


def test_backfill_flips_recently_updated_from_zero(tmp_path: Path) -> None:
    """The PR #41 rider: adopt-mode mtimes are -1 so Recently Updated is
    honestly empty; the backfill fills REAL file mtimes and the collection
    populates through the exact same recent_indices seam."""
    from main import recent_indices

    root = tmp_path / "lib"
    make_jpeg(root / "T" / "fresh.jpg")
    make_jpeg(root / "T" / "stale.jpg")
    os.utime(root / "T" / "fresh.jpg", (_days_ago(1),) * 2)
    os.utime(root / "T" / "stale.jpg", (_days_ago(90),) * 2)
    cat, cat_path = _adopted_catalog(root, tmp_path)

    assert recent_indices(cat, reveal=False) == []     # honest pre-backfill 0
    assert thumbcache.backfill_catalog(cat, cat_path) is not None
    got = [cat.photos[i].rel for i in recent_indices(cat, reveal=False)]
    assert got == ["T/fresh.jpg"]                      # window, not fallback


def test_backfill_state_roundtrip_and_version_gate(tmp_path: Path) -> None:
    """backfill_state/cursor persistence: NOT_STARTED and IN_PROGRESS(cursor)
    round-trip (cursor clamped against hand-edits), COMPLETE writes no key at
    all (an indexer-built catalog's file shape is unchanged), garbage in the
    key degrades to a cold walk, and the v7 version gate rejects a v6 catalog
    (which cannot say whether an adopt catalog was ever backfilled)."""
    import catalog as catmod

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "b.jpg")
    cat = scan_library(root)
    path = tmp_path / "catalog.json"

    cat.backfill_state = BACKFILL_NOT_STARTED
    save_catalog(cat, path)
    loaded = load_catalog(path, root)
    assert loaded is not None
    assert loaded.backfill_state == BACKFILL_NOT_STARTED
    assert loaded.backfill_cursor == 0

    cat.backfill_state = BACKFILL_IN_PROGRESS
    cat.backfill_cursor = 1
    save_catalog(cat, path)
    loaded = load_catalog(path, root)
    assert loaded is not None
    assert loaded.backfill_state == BACKFILL_IN_PROGRESS
    assert loaded.backfill_cursor == 1
    data = json.loads(path.read_text())
    data["backfill"]["cursor"] = 99              # hand-edited overshoot
    path.write_text(json.dumps(data))
    assert load_catalog(path, root).backfill_cursor == 2   # clamped to count

    cat.backfill_state = BACKFILL_COMPLETE
    save_catalog(cat, path)
    assert "backfill" not in json.loads(path.read_text())
    loaded = load_catalog(path, root)
    assert loaded is not None
    assert loaded.backfill_state == BACKFILL_COMPLETE

    data = json.loads(path.read_text())
    data["backfill"] = {"state": "banana"}       # unknown state string
    path.write_text(json.dumps(data))
    assert load_catalog(path, root) is None
    data["backfill"] = "not-an-object"
    path.write_text(json.dumps(data))
    assert load_catalog(path, root) is None

    # v9 (TGA/PSD stills, fauxcasa-v46.4) through v10 (edit recipes,
    # fauxcasa-cam.15): the exact current value is pinned by the newest
    # version-gate test (test_db3_catalog_roundtrip_v11).
    assert catmod.CATALOG_VERSION >= 9
    cat.backfill_state = BACKFILL_COMPLETE
    save_catalog(cat, path)
    data = json.loads(path.read_text())
    data["version"] = 6                          # pre-backfill-state format
    path.write_text(json.dumps(data))
    assert load_catalog(path, root) is None


def test_recent_empty_state_hint_while_backfill_pending(
        tmp_path: Path) -> None:
    """§1 modes-not-modals honesty (the PR #41 rider): while an adopt-mode
    catalog's backfill has not yet filled mtimes, the sidebar's Recently
    Updated says WHY it is empty ('indexing metadata…', not a bare 0) and
    clicking it explains the empty view in the status bar; a COMPLETE
    catalog's empty collection is a bare, final 0 again. cache_dir=None
    keeps the pass itself from starting, so the label logic is tested
    deterministically."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    def item_for(win, kind, key):
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == (kind, key):
                return it.value()
            it += 1
        return None

    root = tmp_path / "lib"
    make_jpeg(root / "T" / "a.jpg")
    cat = scan_library(root)
    cat.backfill_state = BACKFILL_NOT_STARTED
    win = MainWindow(cat, None, cache_dir=None, build_dir=None, adopt=True)
    assert win._backfill_thread is None          # nowhere to persist into

    item = item_for(win, "recent", "")
    assert "indexing metadata" in item.text(0)   # the hint, not (0)
    win._sidebar_clicked(item, 0)
    assert "backfill" in win.statusBar().currentMessage()

    cat.backfill_state = BACKFILL_COMPLETE
    win.statusBar().clearMessage()
    win._refresh_recent_count()
    assert item_for(win, "recent", "").text(0).endswith("(0)")
    win._apply_view("recent", "")
    assert win.statusBar().currentMessage() == ""


def test_mainwindow_adopt_backfill_end_to_end(tmp_path: Path) -> None:
    """The wiring: an adopt-mode MainWindow starts the backfill thread
    (NOT reconcile — a warm start's reconcile is deferred behind it), the
    pass runs against cache_dir/catalog.json, and on completion the sidebar
    refreshes (Recently Updated flips from the indexing hint to a real
    count), the catalog persists COMPLETE with full signals, and the
    deferred reconcile then starts."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import time as _time
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    def item_for(win, kind, key):
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == (kind, key):
                return it.value()
            it += 1
        return None

    root = tmp_path / "lib"
    make_jpeg(root / "T" / "fresh.jpg")
    make_jpeg(root / "T" / "old.jpg")
    os.utime(root / "T" / "fresh.jpg", (_days_ago(1),) * 2)
    os.utime(root / "T" / "old.jpg", (_days_ago(90),) * 2)

    built = thumbcache.build_cache(scan_library(root), tmp_path / "prebuilt")
    cat = scan_library(root)
    cache = thumbcache.load_cache(built.path)
    thumbcache.bind(cache, cat)
    cat.backfill_state = BACKFILL_NOT_STARTED
    cache_dir = tmp_path / "cachedir"
    save_catalog(cat, cache_dir / "catalog.json")  # what main()'s adopt saves

    win = MainWindow(cat, cache, cache_dir=cache_dir, build_dir=None,
                     warm=True, adopt=True)
    assert win._backfill_thread is not None      # backfill, not reconcile
    assert win._reconcile_thread is None
    assert win._reconcile_after_backfill
    assert "indexing metadata" in item_for(win, "recent", "").text(0)

    deadline = _time.time() + 20
    while win._backfill_thread.is_alive() and _time.time() < deadline:
        app.processEvents()
        _time.sleep(0.01)
    assert not win._backfill_thread.is_alive()
    app.processEvents()                          # deliver backfill_done

    assert cat.backfill_state == BACKFILL_COMPLETE
    assert item_for(win, "recent", "").text(0).endswith("(1)")
    disk = load_catalog(cache_dir / "catalog.json", root)
    assert disk is not None
    assert disk.backfill_state == BACKFILL_COMPLETE
    assert all(p.sha256 and p.mtime >= 0 for p in disk.photos)
    assert win._reconcile_thread is not None     # the deferred reconcile ran
    win.shutdown()


# ---------------------------------------------------------------------------
# Face-region overlay (fauxcasa-cam.4): faces= rect64 fractions are relative
# to the STORED pixels (picasa-ini-format.md "faces=": rotate= does NOT
# transform them; EXIF orientation handling is the consumer's job), while
# every display path shows EXIF-upright + rotate= composed — so the overlay
# maps stored-frame rects through that SAME composed transform, then through
# the live _shown_rect (fit / panned 1:1). Verification strategy: the 8x4
# orientation x rotate matrix is checked against Qt's OWN pixel transforms
# (mirrored()/rotate() on a marked synthetic image — an independent
# reference, not a re-derivation of the mapping algebra), and end-to-end
# cases go through REAL EXIF Orientation bytes + the actual
# load_original_oriented decode. Orientation is read at VIEW time from the
# original's bytes (metareader.read_orientation, the exiv2 seam) — no
# catalog schema change. Fixtures are synthetic (privacy rule).
# ---------------------------------------------------------------------------

# Asymmetric on BOTH axes (margins differ left/right and top/bottom), so
# every mirror, turn, or axis-swap mix-up moves the patch and fails a probe.
_FACE_STORED_RECT = (0.25, 0.5, 0.5, 0.75)


def _marked_stored_image(w: int = 96, h: int = 64):
    """Gray stored-frame image with a red block filling exactly the
    _FACE_STORED_RECT fractions."""
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(96, 96, 96))
    left, top, right, bottom = _FACE_STORED_RECT
    for y in range(int(top * h), int(bottom * h)):
        for x in range(int(left * w), int(right * w)):
            img.setPixelColor(x, y, QColor(255, 0, 0))
    return img


def _qt_display_transform(img, orientation: int):
    """Qt's own pixel-level EXIF display transform — the independent
    reference: QTransform mirror/rotate combos straight from the EXIF 274
    definitions (2 mirror-H; 3 rotate 180; 4 mirror-V; 5 mirror-H + rotate
    270 CW = transpose; 6 rotate 90 CW; 7 mirror-H + rotate 90 CW =
    transverse; 8 rotate 270 CW), NOT map_face_fraction's algebra."""
    from PySide6.QtGui import QTransform

    rot90 = QTransform().rotate(90)
    mir_h = QTransform().scale(-1, 1)   # not QImage.mirrored: that bool
    mir_v = QTransform().scale(1, -1)   # overload is deprecated in PySide6
    if orientation == 2:
        return img.transformed(mir_h)
    if orientation == 3:
        return img.transformed(QTransform().rotate(180))
    if orientation == 4:
        return img.transformed(mir_v)
    if orientation == 5:
        return img.transformed(rot90).transformed(mir_h)
    if orientation == 6:
        return img.transformed(rot90)
    if orientation == 7:
        return img.transformed(rot90).transformed(mir_v)
    if orientation == 8:
        return img.transformed(QTransform().rotate(270))
    return img


@pytest.mark.parametrize("orientation", range(1, 9))
@pytest.mark.parametrize("rotate", range(4))
def test_face_map_matrix_orientation_x_rotate(orientation: int,
                                              rotate: int) -> None:
    """All 32 EXIF-orientation x rotate= compositions: the mapped rect's
    center must land ON the red patch in the actually-transformed image,
    and a probe just past each mapped edge must land OFF it — pinning all
    four edges against Qt's own pixel transforms."""
    _offscreen_app()
    from PySide6.QtGui import QTransform

    from viewer import map_face_fraction

    disp = _qt_display_transform(_marked_stored_image(), orientation)
    if rotate:
        disp = disp.transformed(QTransform().rotate(90 * rotate))
    left, top, right, bottom = map_face_fraction(
        _FACE_STORED_RECT, orientation, rotate)
    assert 0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0
    w, h = disp.width(), disp.height()
    cx, cy = (left + right) / 2 * w, (top + bottom) / 2 * h

    def red(x: float, y: float) -> bool:
        c = disp.pixelColor(int(x), int(y))
        return c.red() > 180 and c.green() < 80 and c.blue() < 80

    assert red(cx, cy)                       # center ON the patch
    pad = 4                                  # min patch-to-edge margin is 16
    assert not red(left * w - pad, cy)       # each mapped edge is pinned:
    assert not red(right * w + pad, cy)      # just outside must be OFF
    assert not red(cx, top * h - pad)
    assert not red(cx, bottom * h + pad)


def test_face_overlay_end_to_end_exif_bytes(tmp_path: Path) -> None:
    """Full-pipeline probes with REAL EXIF Orientation bytes: exiv2 writes
    tag 274, load_original_oriented decodes (autoTransform + rotate=,
    bytes read once) and reports the stored value, and face_widget_rect
    at the image's own 1:1 rect lands on the marked patch — confirming
    Qt's autoTransform composition IS the transform the pure math models,
    on real files. Cases cover a plain turn, a mirror, and a mirror-turn
    composed with rotate=."""
    _offscreen_app()
    from PySide6.QtCore import QBuffer, QIODevice, QRect

    import metareader
    from viewer import face_widget_rect, load_original_oriented

    for orientation, rotate in ((6, 0), (2, 0), (5, 1)):
        img = _marked_stored_image()
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        assert img.save(buf, "JPEG", 95)
        data = metareader.embed_test_metadata(
            bytes(buf.data()), orientation=orientation)
        p = tmp_path / f"o{orientation}r{rotate}.jpg"
        p.write_bytes(data)
        shown, got = load_original_oriented(str(p), rotate)
        assert not shown.isNull() and got == orientation
        wr = face_widget_rect(_FACE_STORED_RECT, got, rotate,
                              QRect(0, 0, shown.width(), shown.height()))
        c = shown.pixelColor(int(wr.center().x()), int(wr.center().y()))
        assert c.red() > 150 and c.green() < 100 and c.blue() < 100


def test_metareader_read_orientation() -> None:
    """read_orientation: each stored value 1..8 round-trips from real EXIF
    bytes; an absent tag, out-of-range values, and garbage/empty bytes all
    fail soft to 1 (never an exception) — the wrong-but-bounded contract a
    paint path needs."""
    import metareader

    base = _jpeg_bytes()
    assert metareader.read_orientation(base) == 1        # no tag at all
    for o in range(1, 9):
        assert metareader.read_orientation(
            metareader.embed_test_metadata(base, orientation=o)) == o
    for bad in (0, 9):
        assert metareader.read_orientation(
            metareader.embed_test_metadata(base, orientation=bad)) == 1
    assert metareader.read_orientation(b"") == 1
    assert metareader.read_orientation(b"\xff\xd8 not really a jpeg") == 1


def _face_viewer(tmp_path: Path):
    """A 1280x800 viewer over a 2-photo library where photo 0 carries two
    faces= tags (one named via [Contacts2], one an unconfirmed
    ffffffffffffffff suggestion) and photo 1 carries none; photo 0's
    original is landed via _on_loaded after staling the async decode job
    (serial bump), so the landed orientation can never be overwritten by
    the worker mid-test."""
    _offscreen_app()
    from PySide6.QtGui import QImage

    from viewer import ViewerPage

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "b.jpg")
    (root / "f" / ".picasa.ini").write_text(
        "[Contacts2]\r\nabcdef0123456789=Pat Named;;\r\n"
        "[a.jpg]\r\n"
        "faces=rect64(3f845bcb59418507),abcdef0123456789;"
        "rect64(ff),ffffffffffffffff\r\n")
    cat = scan_library(root)
    assert cat.photos[0].faces and not cat.photos[1].faces
    v = ViewerPage(cat, None)
    v.resize(1280, 800)
    v.show_photo([0, 1], 0)
    v._serial += 1                       # stale the async decode job
    orig = QImage(2560, 1600, QImage.Format.Format_RGB32)
    orig.fill(0x336699)
    v._on_loaded(v._serial, orig, 1)
    assert v.image is orig
    return v, orig


def test_viewer_face_toggle_only_with_faces(tmp_path: Path) -> None:
    """F toggles the overlay on a face-bearing photo — rects for BOTH tags,
    the named one carrying its name and the unconfirmed one None (dashed +
    "Unnamed" in paint) — and the overlaid paint is clean offscreen. On a
    photo with NO faces the same key is a no-op and no rects are produced
    (no dead mode switch); the toggle itself is session-sticky across
    navigation."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    v, _ = _face_viewer(tmp_path)
    assert not v.faces_visible and v._face_rects() == []
    _press(v, Qt.Key.Key_F)
    assert v.faces_visible
    rects = v._face_rects()
    assert len(rects) == 2
    assert {name for _r, name in rects} == {"Pat Named", None}
    assert not v.grab().isNull()         # overlay paint is clean (offscreen)
    _press(v, Qt.Key.Key_F)
    assert not v.faces_visible and v._face_rects() == []
    _press(v, Qt.Key.Key_F)              # back on, then navigate away
    v.show_photo([0, 1], 1)              # the no-faces photo
    v._serial += 1
    orig = QImage(800, 600, QImage.Format.Format_RGB32)
    orig.fill(0x111111)
    v._on_loaded(v._serial, orig, 1)
    assert v.faces_visible               # sticky across navigation...
    assert v._face_rects() == []         # ...but nothing to draw here
    _press(v, Qt.Key.Key_F)
    assert v.faces_visible               # F on a faceless photo: no-op


def test_viewer_face_rects_wait_for_original(tmp_path: Path) -> None:
    """The overlay waits for the ORIGINAL: the stored orientation rides in
    with the decode, so while only the preview stand-in is up _face_rects
    is empty (a mis-mapped box is worse than a fraction-of-a-second wait),
    and the orientation delivered with the image is adopted and applied to
    the mapping."""
    _offscreen_app()
    from PySide6.QtGui import QImage

    from viewer import ViewerPage, face_widget_rect

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\nfaces=rect64(4a8e8e6b),ffffffffffffffff\r\n")
    cat = scan_library(root)
    v = ViewerPage(cat, None)
    v.resize(1280, 800)
    v.show_photo([0], 0)
    v._serial += 1                       # keep the async decode out of it
    v.faces_visible = True
    assert v.image is None
    assert v._face_rects() == []         # orientation unknown: no boxes yet
    orig = QImage(1600, 2400, QImage.Format.Format_RGB32)
    orig.fill(0x445566)
    v._on_loaded(v._serial, orig, 6)     # a 90-CW-stored original lands
    assert v._orientation == 6
    (rect, _name), = v._face_rects()
    shown = v._shown_rect(1280, 800, orig)
    assert rect == face_widget_rect(cat.photos[0].faces[0][0], 6, 0, shown)
    # the doc's worked example: a stored top-LEFT face reads top-RIGHT
    # once a 90-CW-stored (orientation 6) photo is displayed upright
    assert rect.right() == pytest.approx(shown.x() + shown.width())
    assert rect.top() == pytest.approx(shown.y())


def test_viewer_face_rects_track_zoom_and_pan(tmp_path: Path,
                                              monkeypatch) -> None:
    """The widget-space face rect is exactly face_widget_rect over the LIVE
    _shown_rect: at fit, at 1:1 (the box scales with the zoom), and after a
    pan the box moves by exactly the shown-rect delta — the overlay never
    drifts off its image pixels."""
    from viewer import face_widget_rect

    v, orig = _face_viewer(tmp_path)
    monkeypatch.setattr(v, "devicePixelRatioF", lambda: 1.0)
    v.faces_visible = True
    stored = v.catalog.photos[0].faces[0][0]
    fit = v._shown_rect(1280, 800, orig)
    at_fit = v._face_rects()[0][0]
    assert at_fit == face_widget_rect(stored, 1, 0, fit)
    v.toggle_zoom()
    z1 = v._shown_rect(1280, 800, orig)
    r1 = v._face_rects()[0][0]
    assert r1 == face_widget_rect(stored, 1, 0, z1)
    assert r1.width() > at_fit.width()          # the box scales with zoom
    v._pan_by(-120, -80)
    z2 = v._shown_rect(1280, 800, orig)
    r2 = v._face_rects()[0][0]
    assert (z2.x() - z1.x(), z2.y() - z1.y()) == (-120, -80)
    assert r2.x() - r1.x() == pytest.approx(-120)
    assert r2.y() - r1.y() == pytest.approx(-80)


def test_face_overlay_hidden_on_peek_and_slideshow(tmp_path: Path) -> None:
    """Peek and slideshow are glance surfaces: face_overlay_allowed is
    False there, toggle_faces cannot enable it, and even a forced
    faces_visible produces no rects — the overlay is viewer-only by
    policy (viewer.py module doc). No show_photo here on purpose: state
    is set directly so no decode worker ever spawns for a policy test."""
    _offscreen_app()
    from PySide6.QtGui import QImage

    from peek import PeekPage
    from slideshow import SlideshowPage

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\nfaces=rect64(4a8e8e6b),ffffffffffffffff\r\n")
    cat = scan_library(root)
    orig = QImage(400, 300, QImage.Format.Format_RGB32)
    orig.fill(0x222222)
    for cls in (PeekPage, SlideshowPage):
        s = cls(cat)
        s.resize(640, 480)
        s.display, s.pos = [0], 0
        s.image = orig
        assert not s.face_overlay_allowed
        s.toggle_faces()
        assert not s.faces_visible           # F could never switch it on
        s.faces_visible = True               # even forced...
        assert s._face_rects() == []         # ...the paint gate holds


# ---------------------------------------------------------------------------
# Per-folder sort modes: date / name / size (fauxcasa-q6l.11).
# Manual mode (display of Picasa's persisted db3 order) is blocked on the
# missing oracle fixture and deliberately untested/unimplemented here.
# ---------------------------------------------------------------------------

def _photo(name: str, folder: str = "f", **kw):
    from catalog import Photo
    return Photo(rel=f"{folder}/{name}", folder=folder, name=name, **kw)


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


# ===========================================================================
# ---- stills format matrix (fauxcasa-v46.4): TGA, PSD Pillow fallback,
# ---- File Types panel, non-JPEG regression fixtures
# ===========================================================================


def _load_mtc():
    """scripts/make-thumbcache.py as a module (hyphenated file name)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mtc", REPO / "scripts" / "make-thumbcache.py")
    mtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtc)
    return mtc


def _make_tga(path: Path, w: int = 64, h: int = 48,
              color: tuple[int, int, int] = (40, 200, 90)) -> Path:
    """A synthetic TGA via Pillow (Qt's qtga plugin reads, PIL writes)."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), color).save(path, "TGA")
    return path


def test_stills_extensions_in_both_walkers(tmp_path: Path) -> None:
    """The full §5 stills matrix (JPEG, PNG, TIFF, GIF, BMP, PSD, TGA,
    WebP) is in BOTH EXTS sets, in lockstep, and both walks pick the
    v46.4 additions (TGA, PSD) up case-insensitively — the walk-parity
    contract that keeps caches binding."""
    import catalog

    mtc = _load_mtc()
    stills = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
              ".webp", ".tga", ".psd"}
    assert stills <= catalog.EXTS
    assert catalog.EXTS == mtc.EXTS           # the whole lockstep set

    root = tmp_path / "lib"
    root.mkdir()
    for name in ("a.TGA", "b.psd", "c.Tga", "d.PSD"):
        (root / name).write_bytes(b"stub")    # walk checks suffix only
    make_jpeg(root / "e.jpg")
    walked = [p.name for p in walk_library(root)]
    assert sorted(walked) == ["a.TGA", "b.psd", "c.Tga", "d.PSD", "e.jpg"]
    script_walk = sorted(p for p in root.rglob("*")
                         if p.suffix.lower() in mtc.EXTS and p.is_file())
    assert [p.name for p in script_walk] == walked


def test_tga_thumbs_through_real_indexer(tmp_path: Path) -> None:
    """A TGA rides the ordinary QImageReader decode (the pinned PySide6
    build ships qtga): the REAL _index_one path produces a color-correct
    thumb, not an error tile — and the standalone PIL builder reads the
    same file natively (cache-content parity)."""
    root = tmp_path / "lib"
    root.mkdir()
    _make_tga(root / "t.tga")
    cat = scan_library(root)
    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    (_o, length, w, h), = cache.entries
    assert length > 0 and (w, h) == (64, 48)
    px = _thumb_qimage(cache, 0).pixelColor(32, 24)
    assert abs(px.red() - 40) < 30 and abs(px.green() - 200) < 30 \
        and abs(px.blue() - 90) < 30

    (blob, w, h), = _load_mtc()._make_thumb(root / "t.tga", [256])
    assert blob and (w, h) == (64, 48)        # PIL reads TGA natively


def _make_psd(path: Path, w: int = 64, h: int = 48,
              color: tuple[int, int, int] = (200, 60, 120),
              truncate: bool = False) -> Path:
    """A synthetic 'maximize compatibility' PSD: hand-built to the
    documented framing (Pillow READS PSD but cannot write it) — 8BPS v1
    header, empty color-mode/resources/layer sections, then the raw-
    compression flattened composite as planar RGB. Privacy-safe like the
    hand-built APP segments above. `truncate=True` cuts the composite
    planes short: the synthetic stand-in for a PSD saved WITHOUT
    maximize-compatibility (no usable composite -> legitimately an
    error tile, never a crash)."""
    r, g, b = color
    out = (b"8BPS" + struct.pack(">H", 1) + b"\x00" * 6
           + struct.pack(">H", 3)            # channels
           + struct.pack(">II", h, w)        # rows, columns
           + struct.pack(">HH", 8, 3))       # 8-bit, mode 3 = RGB
    out += struct.pack(">I", 0)              # color mode data: empty
    out += struct.pack(">I", 0)              # image resources: empty
    out += struct.pack(">I", 0)              # layer & mask info: empty
    planes = (bytes([r]) * (w * h) + bytes([g]) * (w * h)
              + bytes([b]) * (w * h))
    data = struct.pack(">H", 0) + planes     # compression 0 = raw
    if truncate:
        data = data[: 2 + (w * h) // 2]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out + data)
    return path


def test_psd_composite_decodes_no_composite_error_tiles(tmp_path: Path) -> None:
    """PSD through the REAL _index_one path: Qt has no PSD plugin, so the
    Pillow fallback (pillowload) must decode the flattened composite —
    color-correct, right dims — while a PSD whose composite is unusable
    yields the standard zero-length error tile and never sinks the batch.
    The standalone PIL builder agrees on both (cache-content parity)."""
    root = tmp_path / "lib"
    root.mkdir()
    _make_psd(root / "good.psd")
    _make_psd(root / "nocomposite.psd", truncate=True)
    make_jpeg(root / "ok.jpg")
    cat = scan_library(root)
    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    ent = {rel: e for rel, e in zip(cache.files, cache.entries)}
    _o, length, w, h = ent["good.psd"]
    assert length > 0 and (w, h) == (64, 48)
    idx = cache.files.index("good.psd")
    px = _thumb_qimage(cache, idx).pixelColor(32, 24)
    assert abs(px.red() - 200) < 30 and abs(px.green() - 60) < 30 \
        and abs(px.blue() - 120) < 30
    assert ent["nocomposite.psd"][1] == 0     # error tile, by design
    assert ent["ok.jpg"][1] > 0               # neighbors unharmed

    mtc = _load_mtc()
    (blob, w, h), = mtc._make_thumb(root / "good.psd", [256])
    assert blob and (w, h) == (64, 48)        # PIL reads the composite
    assert mtc._make_thumb(root / "nocomposite.psd", [256]) == [(b"", 0, 0)]


def test_viewer_load_original_psd(tmp_path: Path) -> None:
    """The viewer's full-decode path (shared by the slideshow prefetch):
    a composite-bearing PSD renders via the Pillow fallback, the Picasa
    rotate= turns compose on top exactly like any format, and a PSD with
    no usable composite returns a null QImage (the viewer paints its
    honest 'could not decode this file' text — fail-soft contract)."""
    _offscreen_app()
    from viewer import load_original

    good = _make_psd(tmp_path / "g.psd")
    img = load_original(str(good), 0)
    assert (img.width(), img.height()) == (64, 48)
    px = img.pixelColor(32, 24)
    assert abs(px.red() - 200) < 30 and abs(px.blue() - 120) < 30
    img = load_original(str(good), 1)         # rotate= composes on top
    assert (img.width(), img.height()) == (48, 64)

    broken = _make_psd(tmp_path / "b.psd", truncate=True)
    assert load_original(str(broken), 0).isNull()


def test_file_types_cache_key_and_walk_seam(tmp_path: Path) -> None:
    """The File Types choice folds into cache identity exactly like
    ScanFilter.cache_key: the default choice keys "" (existing cache dirs
    keep binding), an exclusion keys a deterministic string, and toggling
    an extension off and back on derives the SAME cache dir. The walk
    seam honors the effective set; unknown names subtract nothing."""
    from filetypes import effective_exts, exts_cache_key

    assert exts_cache_key(set()) == ""
    assert exts_cache_key({".xyz"}) == ""        # unknown: not a real choice
    key = exts_cache_key({".tga", ".psd"})
    assert key == "exts:no=.psd,.tga"            # deterministic, sorted
    assert exts_cache_key({".psd", ".tga"}) == key
    import catalog as catmod
    assert effective_exts({".xyz"}) == frozenset(catmod.EXTS)
    assert ".tga" not in effective_exts({".tga"})

    lib = tmp_path / "lib"
    lib.mkdir()
    sf = ScanFilter()
    croot = tmp_path / "cr"
    base = thumbcache.cache_dir_for(
        lib, croot, sf.cache_key() + exts_cache_key(set()))
    off = thumbcache.cache_dir_for(
        lib, croot, sf.cache_key() + exts_cache_key({".tga"}))
    back = thumbcache.cache_dir_for(
        lib, croot, sf.cache_key() + exts_cache_key(set()))
    assert off != base                           # a changed set: its own dir
    assert back == base                          # off-and-on: the SAME dir

    make_jpeg(lib / "a.jpg")
    _make_tga(lib / "b.tga")
    assert [p.name for p in walk_library(lib)] == ["a.jpg", "b.tga"]
    assert [p.name for p in
            walk_library(lib, exts=effective_exts({".tga"}))] == ["a.jpg"]


def test_file_types_toggle_returns_to_same_warm_cache(tmp_path: Path) -> None:
    """End-to-end warm coherence (the CRITICAL property): build the
    DEFAULT cache once; excluding an extension derives a different (cold)
    cache dir whose walk the default cache must NOT bind (cache-order
    parity would silently misalign tiles); re-enabling derives the
    ORIGINAL dir again, where the persisted catalog still loads and the
    original cache still binds — the user gets their warm start back."""
    from filetypes import effective_exts, exts_cache_key

    lib = tmp_path / "lib"
    make_jpeg(lib / "a.jpg")
    _make_tga(lib / "b.tga")
    croot = tmp_path / "cr"
    sf = ScanFilter()
    base_dir = thumbcache.cache_dir_for(
        lib, croot, sf.cache_key() + exts_cache_key(set()))
    cat = scan_library(lib)
    thumbcache.build_cache(cat, base_dir)
    save_catalog(cat, base_dir / "catalog.json")

    excluded = {".tga"}
    off_dir = thumbcache.cache_dir_for(
        lib, croot, sf.cache_key() + exts_cache_key(excluded))
    assert off_dir != base_dir
    assert not (off_dir / "thumbs.fcache").exists()   # cold, its own dir
    cat_off = scan_library(lib, exts=effective_exts(excluded))
    assert [p.rel for p in cat_off.photos] == ["a.jpg"]
    with pytest.raises(thumbcache.CacheError):
        thumbcache.bind(
            thumbcache.load_cache(base_dir / "thumbs.fcache"), cat_off)

    on_dir = thumbcache.cache_dir_for(
        lib, croot, sf.cache_key() + exts_cache_key(set()))
    assert on_dir == base_dir                    # back to the SAME dir...
    loaded = load_catalog(on_dir / "catalog.json", lib)
    assert loaded is not None                    # ...whose catalog loads...
    cache = thumbcache.load_cache(on_dir / "thumbs.fcache")
    thumbcache.bind(cache, loaded)               # ...and whose cache binds


def test_file_types_config_roundtrip(tmp_path: Path) -> None:
    """Per-library persistence in config.json: save/load round-trips,
    other libraries' entries and the remembered-library key are
    preserved, clearing an exclusion removes the entry (default = no
    entry at all), and a garbage file, unknown names, or undotted case
    variants all fail soft / normalize."""
    from filetypes import load_excluded_exts, save_excluded_exts

    croot = tmp_path / "cr"
    croot.mkdir()
    lib_a = tmp_path / "A"
    lib_a.mkdir()
    lib_b = tmp_path / "B"
    lib_b.mkdir()
    cfg = croot / "config.json"
    cfg.write_text(json.dumps({"library": "keepme"}))

    assert load_excluded_exts(croot, lib_a) == set()
    assert save_excluded_exts(croot, lib_a, {".psd", ".tga"})
    assert save_excluded_exts(croot, lib_b, {".gif"})
    assert load_excluded_exts(croot, lib_a) == {".psd", ".tga"}
    assert load_excluded_exts(croot, lib_b) == {".gif"}
    data = json.loads(cfg.read_text())
    assert data["library"] == "keepme"           # other keys preserved

    assert save_excluded_exts(croot, lib_a, set())   # back to default
    data = json.loads(cfg.read_text())
    assert str(lib_a.resolve()) not in data.get("exclude_exts", {})
    assert load_excluded_exts(croot, lib_b) == {".gif"}  # B untouched

    cfg.write_text("not json at all")
    assert load_excluded_exts(croot, lib_b) == set()     # fail-soft
    assert save_excluded_exts(croot, lib_b, {".bmp"})    # rebuilds the file
    assert load_excluded_exts(croot, lib_b) == {".bmp"}

    cfg.write_text(json.dumps(
        {"exclude_exts": {str(lib_b.resolve()): [".nope", "TGA", 7]}}))
    assert load_excluded_exts(croot, lib_b) == {".tga"}  # normalized+filtered


def test_file_types_dialog_checkboxes() -> None:
    """The panel lists every supported extension exactly once (grouped
    Stills / RAW / Video in EXTS lockstep by construction), reflects the
    persisted exclusions, and edits round-trip through excluded() — the
    set the accept handler persists and folds into the cache key."""
    _offscreen_app()
    import catalog as catmod
    from filetypes import STILL_EXTS, FileTypesDialog
    from rawload import RAW_EXTS
    from videoload import VIDEO_EXTS

    assert STILL_EXTS | RAW_EXTS | VIDEO_EXTS == frozenset(catmod.EXTS)
    dlg = FileTypesDialog({".psd"})
    assert set(dlg.boxes) == set(catmod.EXTS)    # one box per extension
    assert not dlg.boxes[".psd"].isChecked()     # persisted exclusion shown
    assert dlg.boxes[".jpg"].isChecked()
    dlg.boxes[".tga"].setChecked(False)
    dlg.boxes[".psd"].setChecked(True)
    assert dlg.excluded() == {".tga"}


def test_make_thumbcache_exclude_exts(tmp_path: Path) -> None:
    """--exclude-exts mirrors the panel for adopt-mode parity, driven
    through the pool-free --sidecar-only path: the excluded walk matches
    a cache built over the same effective set (count + files agree), the
    same invocation WITHOUT the flag sees the extra file and refuses, and
    an unsupported name is a hard usage error (exit 2), never a silent
    no-op."""
    from filetypes import effective_exts

    mtc = _load_mtc()
    lib = tmp_path / "lib"
    make_jpeg(lib / "a.jpg")
    _make_tga(lib / "b.tga")
    cat = scan_library(lib, exts=effective_exts({".tga"}))
    out = thumbcache.build_cache(cat, tmp_path / "c").path

    assert mtc.main(["--library", str(lib), "--out", str(out),
                     "--sidecar-only", "--exclude-exts", ".tga"]) == 0
    meta = json.loads(out.with_suffix(".fcache.json").read_text())
    assert meta["files"] == ["a.jpg"]            # the excluded walk
    thumbcache.bind(thumbcache.load_cache(out), cat)  # adopt-mode parity

    # without the flag the fresh walk sees b.tga too: count mismatch
    assert mtc.main(["--library", str(lib), "--out", str(out),
                     "--sidecar-only"]) == 2
    assert mtc.main(["--library", str(lib), "--out", str(out),
                     "--exclude-exts", ".bogus"]) == 2


def test_tiff_is_16bit_header_sniff(tmp_path: Path) -> None:
    """tiff_is_16bit: pure TIFF header sniff, four boundary cases
    (fauxcasa-v46.7):
      16-bit grayscale TIFF  -> True
      8-bit grayscale TIFF   -> False
      non-TIFF bytes         -> False
      truncated bytes        -> False, never raises
    """
    from PIL import Image
    from pillowload import tiff_is_16bit

    g16 = tmp_path / "g16.tif"
    Image.new("I;16", (4, 4), 40000).save(g16, "TIFF")
    assert tiff_is_16bit(g16.read_bytes()) is True

    g8 = tmp_path / "g8.tif"
    Image.new("L", (4, 4), 128).save(g8, "TIFF")
    assert tiff_is_16bit(g8.read_bytes()) is False

    assert tiff_is_16bit(b"not a tiff") is False  # non-TIFF magic
    assert tiff_is_16bit(b"II") is False           # truncated before magic
    assert tiff_is_16bit(b"") is False             # empty

    # Truncated: valid II header but cut before IFD content
    full = g16.read_bytes()
    assert tiff_is_16bit(full[:8]) is False        # header only, no IFD


def test_nonjpeg_regression_matrix(tmp_path: Path) -> None:
    """The §5 stills-matrix regression sweep (fauxcasa-v46.4): before
    this, 5 of the 6 claimed formats were 'done' only by construction —
    the corpus was all Qt-generated baseline JPEG. One library, one REAL
    build_cache/_index_one pass, every fixture generated in-test
    (synthetic per the privacy rule); each asserts the right PIXELS —
    catching CMYK channel inversion and wrong-GIF-frame bugs, not just
    non-null — or the intended error tile:

      cmyk.jpg    Adobe CMYK JPEG        -> decodes RED (never inverted)
      prog.jpg    progressive JPEG       -> decodes
      gray16.tif  16-bit grayscale TIFF  -> decodes mid-gray (not clipped)
      anim.gif    2-frame animated GIF   -> the FIRST frame is the thumb
      t.tga       TGA                    -> decodes
      good.psd    PSD with composite     -> decodes (Pillow fallback)
      nocomp.psd  PSD, unusable composite-> error tile, by design

    (Verified against the pinned PySide6 build: Qt decodes most of these;
    16-bit TIFF and PSD go through the Pillow route. If a Qt upgrade ever
    drops one of the Qt-decoded formats, the fallback rescues it and this
    matrix still pins the pixels.)"""
    from PIL import Image

    lib = tmp_path / "lib"
    lib.mkdir()
    Image.new("CMYK", (64, 48), (0, 255, 255, 0)).save(
        lib / "cmyk.jpg", "JPEG", quality=95)
    Image.new("RGB", (64, 48), (20, 60, 220)).save(
        lib / "prog.jpg", "JPEG", quality=95, progressive=True)
    Image.new("I;16", (64, 48), 40000).save(lib / "gray16.tif", "TIFF")
    first = Image.new("RGB", (64, 48), (30, 200, 40))
    second = Image.new("RGB", (64, 48), (220, 30, 200))
    first.save(lib / "anim.gif", "GIF", save_all=True,
               append_images=[second], duration=200, loop=0)
    _make_tga(lib / "t.tga")
    _make_psd(lib / "good.psd")
    _make_psd(lib / "nocomp.psd", truncate=True)

    cat = scan_library(lib)
    assert len(cat.photos) == 7                  # every fixture walked
    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    ent = {rel: (i, e)
           for i, (rel, e) in enumerate(zip(cache.files, cache.entries))}

    def color_at(rel: str):
        i, (_o, length, w, h) = ent[rel]
        assert length > 0, f"{rel} error-tiled"
        assert (w, h) == (64, 48), rel
        return _thumb_qimage(cache, i).pixelColor(32, 24)

    px = color_at("cmyk.jpg")                    # CMYK red, NOT cyan
    assert px.red() > 200 and px.green() < 60 and px.blue() < 60
    px = color_at("prog.jpg")
    assert px.blue() > 170 and px.red() < 80
    px = color_at("gray16.tif")                  # 40000/65535 ~ 156 gray
    assert abs(px.red() - 156) < 30 and abs(px.red() - px.blue()) < 10
    px = color_at("anim.gif")                    # FIRST frame green...
    assert px.green() > 150 and px.red() < 90    # ...never frame-2 magenta
    px = color_at("t.tga")
    assert abs(px.green() - 200) < 30
    px = color_at("good.psd")
    assert abs(px.red() - 200) < 30 and abs(px.blue() - 120) < 30
    assert ent["nocomp.psd"][1][1] == 0          # the ONE intended error tile


# ---------------------------------------------------------------------------
# Keymap layer (fauxcasa-q6l.8): keymap.py is the single action->QKeySequence
# default-scheme table (the spec's Picasa-compatible scheme) that grid /
# viewer / slideshow / main look bindings up from. Tests here cover the table
# integrity check (duplicate chords within a surface scope + the M2 digit/X
# reservations — the check that would have caught bare '1' colliding with a
# future star-set key), the NEW bindings the bead owes (J/K in the GRID,
# Ctrl+Enter reveal-in-file-manager from grid and viewer), and the per-
# platform launcher commands behind locate.reveal_in_file_manager (Windows
# verified for real during development; macOS/Linux structurally). Existing
# key tests above pin that the refactor changed only the lookup mechanism.
# ---------------------------------------------------------------------------


def test_keymap_default_scheme_has_no_conflicts() -> None:
    """The shipped scheme is collision-free: no duplicate chords within a
    surface scope, and nothing squats on an M2-reserved key (digits 0-5
    star-set, X reject) beyond the one grandfathered tenant — bare '1' on
    viewer.zoom_toggle, the PR #45 arbitration."""
    import keymap

    assert keymap.conflicts() == []


def test_keymap_conflict_checker_is_real() -> None:
    """The integrity check catches what it exists for. A future binding
    claiming bare '1' (the M2 star-set) fails BOTH ways: reserved-key
    violation and duplicate chord against the grandfathered zoom toggle —
    exactly the collision the '1' zoom binding would have shipped into a
    digit scheme unnoticed. X (M2 reject) and plain same-scope duplicates
    are caught too; cross-scope reuse (viewer Space vs slideshow Space)
    is not a conflict."""
    import keymap

    scheme = dict(keymap.DEFAULT_SCHEME)
    scheme["viewer.star_1"] = keymap.Binding(("1",))
    found = keymap.conflicts(scheme)
    assert ("viewer.star_1", "RESERVED") in [f[:2] for f in found]
    assert any({a, b} == {"viewer.star_1", "viewer.zoom_toggle"}
               for a, b, _ in found)

    scheme = dict(keymap.DEFAULT_SCHEME)
    scheme["grid.reject"] = keymap.Binding(("X",))
    assert any(f[:2] == ("grid.reject", "RESERVED")
               for f in keymap.conflicts(scheme))

    scheme = dict(keymap.DEFAULT_SCHEME)
    scheme["grid.flip"] = keymap.Binding(("Ctrl+H",))     # dupes grid.hold
    assert any({a, b} == {"grid.flip", "grid.hold"}
               for a, b, _ in keymap.conflicts(scheme))

    # an "app." global chord conflicts INTO every surface scope
    scheme = dict(keymap.DEFAULT_SCHEME)
    scheme["app.flip"] = keymap.Binding(("Ctrl+H",))
    assert any({a, b} >= {"app.flip"} and "hold" in a + b
               for a, b, _ in keymap.conflicts(scheme))


def test_keymap_platform_correctness_rides_qt() -> None:
    """Ctrl/Cmd correctness (spec §8) comes from Qt, not per-OS tables:
    grid.select_all defers to StandardKey.SelectAll (Cmd+A on macOS from
    Qt's own binding list), and app.play resolves F11 + Ctrl+4 (the
    Picasa slideshow chord added in ed5.12) for QAction.setShortcuts."""
    import keymap
    from PySide6.QtGui import QKeySequence

    assert (keymap.DEFAULT_SCHEME["grid.select_all"].standard
            == QKeySequence.StandardKey.SelectAll)
    play_strs = [s.toString() for s in keymap.shortcuts("app.play")]
    assert "F11" in play_strs
    assert "Ctrl+4" in play_strs


def test_play_tooltip_derives_from_keymap(library: Path) -> None:
    """The ▶ Play tooltip is built from keymap.shortcuts, not a hard-coded
    string — every chord in the scheme appears in the tooltip text, so
    adding or removing a chord keeps the UI self-consistent (ed5.12)."""
    import keymap
    _offscreen_app()
    from main import MainWindow
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    tip = win.play_action.toolTip()
    for seq in keymap.shortcuts("app.play"):
        assert seq.toString() in tip, (
            f"{seq.toString()!r} missing from play tooltip: {tip!r}")


def test_grid_jk_navigate_next_prev(tmp_path: Path) -> None:
    """J/K step the grid's CURRENT item forward/back exactly like
    Right/Left (Picasa's viewer J/K, extended to the grid per q6l.8):
    plain taps collapse the selection to the target, and Shift extends
    from the anchor because they ride the same nav path as the arrows."""
    from PySide6.QtCore import Qt

    g = _selection_grid(tmp_path)
    d = g.display
    _click(g, d[0])
    _key(g, Qt.Key.Key_J)
    assert g.current == d[1] and g.selection == {d[1]}
    _key(g, Qt.Key.Key_J)
    assert g.current == d[2]
    _key(g, Qt.Key.Key_K)
    assert g.current == d[1] and g.selection == {d[1]}
    _key(g, Qt.Key.Key_J, Qt.KeyboardModifier.ShiftModifier)
    assert g.selection == {d[1], d[2]}          # Shift+J extends, as arrows
    assert g.current == d[2] and g.anchor == d[1]
    _key(g, Qt.Key.Key_K)                       # plain key: collapse again
    assert g.selection == {d[1]}


def test_grid_ctrl_enter_reveals_current(tmp_path: Path,
                                         monkeypatch) -> None:
    """Ctrl+Enter (Picasa: Locate on Disk) reveals the CURRENT item in the
    OS file manager and never activates it — plain Enter still opens the
    viewer — and with no current item the chord is a swallowed no-op. Both
    the main-row Return and keypad Enter spellings fire."""
    from PySide6.QtCore import Qt

    import grid as gridmod

    g = _selection_grid(tmp_path)
    d = g.display
    calls: list[Path] = []
    monkeypatch.setattr(gridmod, "reveal_in_file_manager",
                        lambda p: calls.append(Path(p)) or True)
    opened: list[int] = []
    g.photo_activated.connect(lambda idx, _d, _p: opened.append(idx))
    _key(g, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert calls == [] and opened == []         # no current item: no-op
    _click(g, d[1])
    _key(g, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert opened == []                         # reveal, never activate
    assert calls == [g.catalog.root / g.catalog.photos[d[1]].rel]
    _key(g, Qt.Key.Key_Enter, Qt.KeyboardModifier.ControlModifier
         | Qt.KeyboardModifier.KeypadModifier)  # keypad Enter spelling
    assert len(calls) == 2
    _key(g, Qt.Key.Key_Return)                  # plain Enter still opens
    assert opened == [d[1]]


def test_viewer_ctrl_enter_reveals_shown_photo(tmp_path: Path,
                                               monkeypatch) -> None:
    """Ctrl+Enter in the viewer reveals the photo ON SCREEN — the same
    keymap action, the same one launcher function as the grid."""
    from PySide6.QtCore import Qt

    import viewer as viewermod

    v, _ = _viewer_with_original(tmp_path)
    calls: list[Path] = []
    monkeypatch.setattr(viewermod, "reveal_in_file_manager",
                        lambda p: calls.append(Path(p)) or True)
    _key(v, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    idx = v.current_index()
    assert idx >= 0
    assert calls == [v.catalog.root / v.catalog.photos[idx].rel]
    _key(v, Qt.Key.Key_Right)                   # nav still navigates
    _key(v, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert calls[-1] == v.catalog.root / v.catalog.photos[
        v.current_index()].rel


def test_reveal_in_file_manager_per_platform(tmp_path: Path,
                                             monkeypatch) -> None:
    """The exact launcher per platform. Windows: ONE command-line string,
    explorer /select,"<native path>" (list argv would re-quote and break
    explorer's comma parsing) — this exact form was verified for real on
    Windows (explorer opens with the file selected, checked via the Shell
    COM automation API). macOS: open -R argv. Linux: FileManager1
    ShowItems over dbus-send, falling back to xdg-open of the CONTAINING
    FOLDER when the call fails or dbus-send is missing; a launcher OSError
    reports False instead of raising into a key handler."""
    import locate

    target = tmp_path / "sub dir" / "photo one.jpg"    # spaces on purpose
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    popens: list[object] = []
    monkeypatch.setattr(locate.subprocess, "Popen",
                        lambda cmd, *a, **k: popens.append(cmd))
    assert locate.reveal_in_file_manager(target, platform="win32")
    assert popens == [f'explorer /select,"{os.path.normpath(target)}"']

    popens.clear()
    assert locate.reveal_in_file_manager(target, platform="darwin")
    assert popens == [["open", "-R", str(target)]]

    # Linux, happy path: ShowItems answers -> no fallback launcher at all
    popens.clear()
    runs: list[list[str]] = []

    class _Ok:
        returncode = 0

    monkeypatch.setattr(locate.subprocess, "run",
                        lambda cmd, **k: runs.append(cmd) or _Ok())
    assert locate.reveal_in_file_manager(target, platform="linux")
    assert popens == []
    (cmd,) = runs
    assert cmd[0] == "dbus-send"
    assert "--dest=org.freedesktop.FileManager1" in cmd
    assert cmd[-2] == f"array:string:{target.absolute().as_uri()}"
    assert cmd[-1] == "string:"                 # empty startup id

    # Linux, no FileManager1 answer: open the containing folder instead
    class _Fail:
        returncode = 1

    monkeypatch.setattr(locate.subprocess, "run", lambda cmd, **k: _Fail())
    assert locate.reveal_in_file_manager(target, platform="linux")
    assert popens == [["xdg-open", str(target.parent)]]

    # Linux, dbus-send binary missing entirely: the same folder fallback
    popens.clear()

    def _missing(cmd, **k):
        raise FileNotFoundError("dbus-send")

    monkeypatch.setattr(locate.subprocess, "run", _missing)
    assert locate.reveal_in_file_manager(target, platform="linux")
    assert popens == [["xdg-open", str(target.parent)]]

    # launcher failure -> False, never an exception into the key handler
    def _boom(cmd, *a, **k):
        raise OSError("no explorer")

    monkeypatch.setattr(locate.subprocess, "Popen", _boom)
    assert not locate.reveal_in_file_manager(target, platform="win32")


# ---------------------------------------------------------------------------
# Edit-recipe ingest + crop-applied display (fauxcasa-cam.15): §4 "ini wins
# for edit recipes", and M1 "see your library again" means a cropped photo
# shows CROPPED — Picasa itself rendered unsaved recipes applied (oracle
# fixture 004: the crop lives in the ini ALONE — crop=rect64(..) plus its
# filters= crop64 twin — JPEG untouched, no db3 change). Ingest: the crop
# rect is resolved for display (crop= wins over the chain; conflicts land on
# the import report) and every recipe key/value is preserved RAW on
# Photo.edits (N3 losslessness; full recipe rendering is M3). Display: the
# crop bakes into thumbnails at index time — like the EXIF bake, and UNLIKE
# rotate=, because a crop changes WHICH pixels are shown — and applies to
# the viewer's decoded original, composing crop -> EXIF orientation ->
# rotate= (rect64 fractions are STORED-frame: the format doc pins that
# rotate= does not transform crop coords; cropmap.py holds the derivation).
# Faces on a cropped photo rebase through the crop first — they still
# reference stored pixels. Fixtures are synthetic (privacy rule).
# ---------------------------------------------------------------------------

# Oracle fixture 004's exact value: rect64(dc3369dc570a51e), zero-stripped
# 15-hex — the padded split is 0dc3 369d c570 a51e.
_FIXTURE_004_CROP = "rect64(dc3369dc570a51e)"
_FIXTURE_004_RECT = (0x0DC3 / 65536, 0x369D / 65536,
                     0xC570 / 65536, 0xA51E / 65536)


def test_crop_ingest_fixture_004_shape(tmp_path: Path) -> None:
    """The observed unsaved-crop ini shape (oracle fixture 004): crop= and
    the agreeing filters= crop64 twin parse to the exact rect64 fractions,
    both raw lines are preserved in ini order on Photo.edits (backuphash=
    is NOT a recipe key), has_edits is on, and the agreeing pair raises NO
    crop_conflict import note — Picasa always writes both."""
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "photo03.jpg")
    (root / "f" / ".picasa.ini").write_text(
        "[photo03.jpg]\r\n"
        "backuphash=64082\r\n"
        f"crop={_FIXTURE_004_CROP}\r\n"
        "filters=crop64=1,dc3369dc570a51e;\r\n")
    cat = scan_library(root)
    p = cat.photos[0]
    assert p.crop == _FIXTURE_004_RECT
    assert p.edits == (("crop", _FIXTURE_004_CROP),
                       ("filters", "crop64=1,dc3369dc570a51e;"))
    assert p.has_edits
    assert not [e for e in cat.report.entries if e.kind == "crop_conflict"]


def test_crop_source_precedence(tmp_path: Path) -> None:
    """_resolve_crop's source ladder: a filters=-only chain fills in (its
    LAST crop64 wins — the chain is ordered history and a re-crop
    appends); a disagreeing crop= WINS and surfaces a crop_conflict import
    note (§4: never silently resolved); a malformed crop= falls through to
    the chain; a standalone gist-era crop64= key is the last resort; a
    degenerate rect is no crop at all — but the raw junk is still
    preserved and still reads as edited (honest marker)."""
    root = tmp_path / "lib"
    for name in "abcde":
        make_jpeg(root / "f" / f"{name}.jpg")
    (root / "f" / ".picasa.ini").write_text(
        # chain only; two crop64 ops -> the LAST one wins
        "[a.jpg]\r\n"
        "filters=crop64=1,10001000;crop64=1,400080008000c000;\r\n"
        # crop= disagrees with the chain -> crop= wins + import note
        "[b.jpg]\r\n"
        "crop=rect64(40004000c000c000)\r\n"
        "filters=crop64=1,10001000;\r\n"
        # malformed crop= -> the chain fills the gap, no conflict
        "[c.jpg]\r\n"
        "crop=rect64(not-hex)\r\n"
        "filters=crop64=1,40004000c000c000;\r\n"
        # standalone crop64= key (gist-era), op-param shape
        "[d.jpg]\r\n"
        "crop64=1,40004000c000c000\r\n"
        # degenerate rect (right < left, bottom < top): unusable
        "[e.jpg]\r\n"
        "crop=rect64(c000c00040004000)\r\n")
    cat = scan_library(root)
    by = {p.name: p for p in cat.photos}
    assert by["a.jpg"].crop == (0.25, 0.5, 0.5, 0.75)          # last crop64
    assert by["b.jpg"].crop == (0.25, 0.25, 0.75, 0.75)        # crop= wins
    assert by["c.jpg"].crop == (0.25, 0.25, 0.75, 0.75)        # chain fills
    assert by["d.jpg"].crop == (0.25, 0.25, 0.75, 0.75)        # bare crop64=
    assert by["e.jpg"].crop is None
    assert by["e.jpg"].edits and by["e.jpg"].has_edits         # raw survives
    conflicts = [e for e in cat.report.entries if e.kind == "crop_conflict"]
    assert len(conflicts) == 1 and conflicts[0].subject == "f/b.jpg"


def test_edit_recipe_raw_preservation_and_marker(tmp_path: Path) -> None:
    """N3 losslessness: every recipe key/value survives verbatim, in ini
    order, DUPLICATES kept (crashed-mid-write files have them) — and
    rotate= is excluded (parsed field of its own, composes live). The
    has_edits marker: redo= alone counts (recipe state present even if
    every op is undone), text=/textactive=1 count, but textactive=0 alone
    does NOT (Picasa's overlay-off record — fixture 005 writes it into the
    post-bake stash ini), nor does rotate= alone."""
    root = tmp_path / "lib"
    for name in ("a", "b", "c", "d"):
        make_jpeg(root / "f" / f"{name}.jpg")
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\n"
        "rotate=rotate(1)\r\n"
        "redo=unsharp=1,0.5;\r\n"
        "textactive=1\r\n"
        "text=1;10;20;hello;Arial;0.1;0.2;0.3;0.4;v1,ffffffff;;\r\n"
        "flipped=1\r\n"
        "redo=unsharp=1,0.7;\r\n"          # duplicate key, kept in order
        "[b.jpg]\r\n"
        "textactive=0\r\n"                  # overlay-off alone: NOT edited
        "[c.jpg]\r\n"
        "rotate=rotate(2)\r\n"              # rotate alone: NOT a recipe
        "[d.jpg]\r\n"
        "redo=unsharp=1,0.5;\r\n")          # undone ops still carry state
    cat = scan_library(root)
    by = {p.name: p for p in cat.photos}
    assert by["a.jpg"].edits == (
        ("redo", "unsharp=1,0.5;"),
        ("textactive", "1"),
        ("text", "1;10;20;hello;Arial;0.1;0.2;0.3;0.4;v1,ffffffff;;"),
        ("flipped", "1"),
        ("redo", "unsharp=1,0.7;"),
    )
    assert by["a.jpg"].rotate == 1 and by["a.jpg"].has_edits
    assert by["b.jpg"].edits == (("textactive", "0"),)
    assert not by["b.jpg"].has_edits
    assert by["c.jpg"].edits == () and not by["c.jpg"].has_edits
    assert by["d.jpg"].has_edits


def test_catalog_v10_crop_roundtrip_and_version_gate(tmp_path: Path) -> None:
    """CATALOG_VERSION is 10 (edit recipes + the crop-baked thumbs it
    implies; v9 was TGA/PSD), the crop rect and raw recipe strings
    round-trip exactly through the persisted catalog (n/65536 fractions
    are exact in JSON; has_edits re-derives), and a pre-v10 file is
    rejected -> the caller cold-rebuilds (nothing in the size/mtime drift
    check could notice an ini-only interpretation change)."""
    from catalog import CATALOG_VERSION

    assert CATALOG_VERSION >= 10  # exact value pinned by the v11 test
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "b.jpg")
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\n"
        f"crop={_FIXTURE_004_CROP}\r\n"
        "filters=crop64=1,dc3369dc570a51e;\r\n"
        "redo=unsharp=1,0.5;\r\n")
    cat = scan_library(root)
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)
    loaded = load_catalog(path, root)
    assert loaded is not None
    a, b = loaded.photos[0], loaded.photos[1]
    assert a.crop == _FIXTURE_004_RECT
    assert a.edits == cat.photos[0].edits
    assert a.has_edits and not b.has_edits
    assert b.crop is None and b.edits == ()
    data = json.loads(path.read_text())
    data["version"] = 9                          # pre-edit-recipe format
    path.write_text(json.dumps(data))
    assert load_catalog(path, root) is None


def _quadrant_jpeg(path: Path, edge: int = 1024) -> None:
    """A 4-quadrant marker image: TL red, TR green, BL blue, BR white."""
    from PySide6.QtGui import QColor, QImage, QPainter

    img = QImage(edge, edge, QImage.Format.Format_RGB32)
    half = edge // 2
    p = QPainter(img)
    p.fillRect(0, 0, half, half, QColor(255, 0, 0))
    p.fillRect(half, 0, half, half, QColor(0, 200, 0))
    p.fillRect(0, half, half, half, QColor(0, 0, 255))
    p.fillRect(half, half, half, half, QColor(255, 255, 255))
    p.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    assert img.save(str(path), "JPEG", 95)


def test_thumb_bakes_crop_before_downscale(tmp_path: Path) -> None:
    """The indexer bakes the crop into the cached thumbnail, and it crops
    BEFORE the downscale in resolution terms: cropping a 1024px source to
    its 512px top-right quadrant must yield a 256px thumb (the scaled-
    decode target is computed for the SUB-RECT) — the naive decode-to-256-
    then-crop order would yield 128px, so the dimension assertion pins the
    order. Pixel probes confirm WHICH quadrant survived. The uncropped
    sibling behaves exactly as before."""
    _offscreen_app()
    from PySide6.QtGui import QImage

    root = tmp_path / "lib"
    _quadrant_jpeg(root / "f" / "cropped.jpg")
    _quadrant_jpeg(root / "f" / "plain.jpg")
    # top-right quadrant: (0.5, 0.0, ~1.0, 0.5) — 1.0 is not encodable in
    # a u16 fraction, so Picasa writes 0xffff (0.999985), as here
    (root / "f" / ".picasa.ini").write_text(
        "[cropped.jpg]\r\ncrop=rect64(80000000ffff8000)\r\n")
    cat, cache = _bound_cache(tmp_path, root)
    by = {p.name: i for i, p in enumerate(cat.photos)}

    def thumb(idx: int) -> QImage:
        offset, length, _w, _h = cache.entries[idx]
        assert length > 0
        with open(cache.path, "rb") as f:
            f.seek(offset)
            img = QImage.fromData(f.read(length), "JPEG")
        assert not img.isNull()
        return img

    ci = by["cropped.jpg"]
    img = thumb(ci)
    assert (img.width(), img.height()) == (256, 256)   # NOT 128: see doc
    assert cache.entries[ci][2:] == (256, 256)
    for fx, fy in ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75)):
        c = img.pixelColor(int(fx * img.width()), int(fy * img.height()))
        assert c.green() > 150 and c.red() < 90 and c.blue() < 90, (fx, fy)
    plain = thumb(by["plain.jpg"])
    assert (plain.width(), plain.height()) == (256, 256)
    tl = plain.pixelColor(64, 64)
    assert tl.red() > 150 and tl.green() < 90          # TL still red


def test_thumb_crop_composes_with_exif_orientation(tmp_path: Path) -> None:
    """crop= coordinates are STORED-frame while the baked thumb is
    EXIF-upright, so the bake must map the rect through the orientation
    tag: on a 90-CW-stored (orientation 6) photo, cropping exactly the
    marked stored rect yields a thumb of the region's SWAPPED dims that is
    all marker — an unmapped rect would crop gray. Uses _index_one
    directly (the bake seam)."""
    _offscreen_app()
    from PySide6.QtCore import QBuffer, QIODevice

    import metareader

    root = tmp_path / "lib"
    img = _marked_stored_image()          # 96x64, red at (.25,.5,.5,.75)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    assert img.save(buf, "JPEG", 95)
    (root / "f").mkdir(parents=True)
    (root / "f" / "a.jpg").write_bytes(
        metareader.embed_test_metadata(bytes(buf.data()), orientation=6))
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\ncrop=rect64(400080008000c000)\r\n")  # == the red patch
    cat = scan_library(root)
    assert cat.photos[0].crop == (0.25, 0.5, 0.5, 0.75)
    _idx, blobs, *_rest, primary = thumbcache._index_one(
        root, cat.photos[0], 0, [thumbcache.THUMB_EDGE])
    # stored region 24x16 -> orientation 6 swaps -> 16x24 upright, < 256
    # so never upscaled
    assert (primary.width(), primary.height()) == (16, 24)
    assert blobs[0][1:] == (16, 24)
    c = primary.pixelColor(8, 12)
    assert c.red() > 150 and c.green() < 100 and c.blue() < 100


def test_viewer_crop_exif_rotate_composition_order(tmp_path: Path) -> None:
    """The viewer's decode composes crop -> EXIF orientation -> rotate=,
    against REAL EXIF bytes: with orientation 6 and a stored-frame crop
    whose top-left quadrant is the red patch, the patch must land top-
    RIGHT at rotate=0 (one 90 CW) and bottom-right at rotate=1 (180
    total), with the dims transformed to match. Wrong order — cropping
    the upright pixels with the UNMAPPED stored rect, or cropping after
    rotate= — moves the patch and fails the probes."""
    _offscreen_app()
    from PySide6.QtCore import QBuffer, QIODevice

    import metareader
    from viewer import load_original_oriented

    img = _marked_stored_image()          # 96x64, red at (.25,.5,.5,.75)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    assert img.save(buf, "JPEG", 95)
    p = tmp_path / "a.jpg"
    p.write_bytes(
        metareader.embed_test_metadata(bytes(buf.data()), orientation=6))
    # stored-frame crop (0.25, 0.5, 0.75, 1.0): a 48x32 region whose
    # LEFT-TOP quadrant is exactly the red patch
    crop = (0.25, 0.5, 0.75, 1.0)

    def red(shown, fx: float, fy: float) -> bool:
        c = shown.pixelColor(int(fx * shown.width()),
                             int(fy * shown.height()))
        return c.red() > 150 and c.green() < 100 and c.blue() < 100

    shown, got = load_original_oriented(str(p), 0, crop)
    assert got == 6 and not shown.isNull()
    assert (shown.width(), shown.height()) == (32, 48)  # 90 CW swaps
    assert red(shown, 0.75, 0.25) and not red(shown, 0.25, 0.75)

    shown, got = load_original_oriented(str(p), 1, crop)
    assert got == 6
    assert (shown.width(), shown.height()) == (48, 32)  # 180 total
    assert red(shown, 0.75, 0.75) and not red(shown, 0.25, 0.25)


def test_face_rect_rebase_through_crop() -> None:
    """Faces on a cropped photo still reference STORED pixels, so the
    overlay rebases each rect into the crop sub-rect FIRST, then applies
    the same EXIF x rotate mapping as the pixels. Pure-math contract:
    identity (face == crop fills the frame), clamping (a straddling face
    shows its visible part), rejection (a cropped-out face has no
    on-screen pixels), degenerate crops, and the composition with the
    orientation map afterwards."""
    from cropmap import map_fraction_rect, rebase_fraction_rect

    crop = (0.25, 0.25, 0.75, 0.75)
    assert rebase_fraction_rect(crop, crop) == (0.0, 0.0, 1.0, 1.0)
    got = rebase_fraction_rect((0.4, 0.4, 0.6, 0.6), (0.5, 0.25, 1.0, 0.75))
    assert got == pytest.approx((0.0, 0.3, 0.2, 0.7))
    assert rebase_fraction_rect((0.0, 0.0, 0.1, 0.1), (0.5, 0.5, 1.0, 1.0)) \
        is None
    assert rebase_fraction_rect((0.4, 0.4, 0.6, 0.6), (0.5, 0.5, 0.5, 1.0)) \
        is None                                        # degenerate crop
    # composed: a face filling the crop fills the displayed frame under
    # ANY orientation x rotate (the mapping of (0,0,1,1) is (0,0,1,1))
    for orientation in range(1, 9):
        for rotate in range(4):
            assert map_fraction_rect(
                rebase_fraction_rect(crop, crop), orientation, rotate
            ) == (0.0, 0.0, 1.0, 1.0)


def test_viewer_face_rects_on_cropped_photo(tmp_path: Path) -> None:
    """End-to-end overlay-on-crop: a face tag coinciding with the crop
    fills the shown rect exactly, and a face the crop cut out produces NO
    box (its pixels are not on screen) — the mapping goes rebase -> EXIF x
    rotate -> _shown_rect, all through the real viewer plumbing."""
    _offscreen_app()
    from PySide6.QtGui import QImage

    from viewer import ViewerPage, face_widget_rect

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    (root / "f" / ".picasa.ini").write_text(
        "[Contacts2]\r\nabcdef0123456789=Pat Named;;\r\n"
        "[a.jpg]\r\n"
        "crop=rect64(40004000c000c000)\r\n"
        "faces=rect64(40004000c000c000),abcdef0123456789;"
        "rect64(10001000),ffffffffffffffff\r\n")
    cat = scan_library(root)
    p = cat.photos[0]
    assert p.crop == (0.25, 0.25, 0.75, 0.75) and len(p.faces) == 2
    v = ViewerPage(cat, None)
    v.resize(1280, 800)
    v.show_photo([0], 0)
    v._serial += 1                        # stale the async decode job
    orig = QImage(640, 480, QImage.Format.Format_RGB32)  # cropped decode
    orig.fill(0x336699)
    v._on_loaded(v._serial, orig, 1)
    v.faces_visible = True
    rects = v._face_rects()
    # the (0,0,.0625,.0625) face lies wholly outside the crop: dropped
    assert len(rects) == 1 and rects[0][1] == "Pat Named"
    shown = v._shown_rect(1280, 800, orig)
    assert rects[0][0] == face_widget_rect((0.0, 0.0, 1.0, 1.0), 1, 0, shown)


def test_viewer_info_bar_edited_chip(tmp_path: Path) -> None:
    """The honest M1 'edited' cue: a photo carrying a recipe shows the
    chip in the viewer info bar; a plain photo (and a textactive=0-only
    one) does not. The unsaved-vs-baked state cue is M3 — presence only."""
    _offscreen_app()
    from viewer import ViewerPage

    root = tmp_path / "lib"
    for name in ("a", "b", "c"):
        make_jpeg(root / "f" / f"{name}.jpg")
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\nfilters=tilt=1,0.280632,0.000000;\r\n"
        "[c.jpg]\r\ntextactive=0\r\n")
    cat = scan_library(root)
    by = {p.name: p for p in cat.photos}
    v = ViewerPage(cat, None)
    v.display, v.pos = [0, 1, 2], 0
    assert "edited" in v._info_text(by["a.jpg"])
    assert "edited" not in v._info_text(by["b.jpg"])
    assert "edited" not in v._info_text(by["c.jpg"])


def test_ingest_parity_gate_passes() -> None:
    """The M1 ingest-parity gate (spec §9 clause 2) end-to-end: with
    edit-recipe ingest landed (fauxcasa-cam.15) the expectation table has
    ZERO expected-missing classes, so the gate must PASS fully ingested —
    zero loss across every class it tracks. Runs the real script in its
    own uv env (exactly CI's tests.yml job); skips when uv is absent."""
    import shutil
    import subprocess

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not on PATH")
    proc = subprocess.run(
        [uv, "run", str(REPO / "scripts" / "check-ingest-parity.py")],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "fully ingested (0 expected-missing)" in proc.stdout
