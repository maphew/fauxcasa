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
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import inmeta
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
    grid.tiles[0] = [object(), 1]
    grid.pending.add(5)
    gen_before = grid.generation
    grid.set_data(cat, None)  # the reconcile-style swap
    assert grid.tiles == {} and grid.pending == set()
    assert grid.generation > gen_before  # stale queued decodes are dropped


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
    _b, rw, rh = mtc._make_thumb(rot)
    _b, fw, fh = mtc._make_thumb(flat)
    assert rw < rh   # rotated -> portrait, matching the Qt indexer
    assert fw > fh   # untouched -> landscape


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
