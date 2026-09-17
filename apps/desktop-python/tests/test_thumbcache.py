"""Tests for thumbcache.py XMP/EXIF metadata merge and the fcache v2 multi-resolution format.

Split from test_tracer.py (fauxcasa-l09); originally lines 1642-2260 of the monolith."""

from __future__ import annotations

import json
import struct
from pathlib import Path
import pytest
import inmeta
import thumbcache
from catalog import (
    scan_library,
)
from tracer_helpers import (
    REPO,
    _big_library,
    _jpeg_bytes,
    _rect64,
    _tiff_bytes,
    make_jpeg,
    write_jpeg_meta,
)


# ---- fauxcasa-cam.5: XMP faces + non-JPEG caption/keywords merge into the
# catalog at index time (thumbcache.apply_photo_meta) -----------------------


def test_apply_photo_meta_merges_xmp_faces_by_geometry(tmp_path: Path) -> None:
    """One photo: an ini faces= region overlapping a NAMED XMP region (IoU
    >= 0.5) keeps the ini rect + contact id but takes the XMP display name
    (in-file wins tier-1); a second ini region with nothing overlapping it
    stays untouched; a THIRD, non-overlapping XMP region is ADDED with an
    'xmp:<name>' contact id. Both the name-fill and the add are GAP-FILLS
    (the ini face was unnamed; the added face has no ini counterpart at
    all) — fauxcasa-cam.17's no-flood rule means NEITHER is reported (see
    test_apply_photo_meta_faces_replacement_reported for the case that
    IS)."""
    import metareader
    import picasa_db

    root = tmp_path / "lib"
    matched_rect = (0.40, 0.40, 0.60, 0.60)
    untouched_rect = (0.80, 0.80, 0.90, 0.90)
    data = metareader.embed_test_metadata(
        _jpeg_bytes(),
        faces=[("Xmp Match", 0.50, 0.50, 0.20, 0.20),   # center of matched_rect
               ("Xmp New", 0.10, 0.10, 0.05, 0.05)])     # nowhere near either
    (root / "p.jpg").parent.mkdir(parents=True, exist_ok=True)
    (root / "p.jpg").write_bytes(data)
    (root / ".picasa.ini").write_text(
        "[p.jpg]\r\n"
        f"faces=rect64({_rect64(*matched_rect)}),cccccccccccccccc;"
        f"rect64({_rect64(*untouched_rect)}),{picasa_db.UNKNOWN_CONTACT}\r\n")
    cat = scan_library(root)
    thumbcache.build_cache(cat, tmp_path / "c")

    p = next(ph for ph in cat.photos if ph.rel == "p.jpg")
    assert len(p.faces) == 3
    rect0, cid0, name0 = p.faces[0]
    assert rect0 == pytest.approx(matched_rect, abs=1e-4)  # rect64 quantization
    assert (cid0, name0) == ("cccccccccccccccc", "Xmp Match")
    rect1, cid1, name1 = p.faces[1]
    assert rect1 == pytest.approx(untouched_rect, abs=1e-4)
    assert (cid1, name1) == (picasa_db.UNKNOWN_CONTACT, None)
    rect2, cid2, name2 = p.faces[2]
    assert rect2 == pytest.approx((0.075, 0.075, 0.125, 0.125))
    assert (cid2, name2) == ("xmp:Xmp New", "Xmp New")
    assert not [e for e in cat.report.entries
               if e.kind == "infile_override" and e.detail.startswith("faces:")]


def test_apply_photo_meta_faces_replacement_reported(tmp_path: Path) -> None:
    """The ONE case faces DO get reported: an ini face that already has a
    resolvable display name (via [Contacts2]) is renamed by a DIFFERENT
    XMP name — a genuine conflict, not a gap-fill — producing exactly one
    infile_override entry shaped like the other fields' ('ini "X" ->
    in-file "Y"')."""
    import metareader

    root = tmp_path / "lib"
    rect = (0.40, 0.40, 0.60, 0.60)
    data = metareader.embed_test_metadata(
        _jpeg_bytes(), faces=[("Robert", 0.50, 0.50, 0.20, 0.20)])
    (root / "p.jpg").parent.mkdir(parents=True, exist_ok=True)
    (root / "p.jpg").write_bytes(data)
    (root / ".picasa.ini").write_text(
        f"[p.jpg]\r\nfaces=rect64({_rect64(*rect)}),cccccccccccccccc\r\n"
        "[Contacts2]\r\ncccccccccccccccc=Bob;;\r\n")
    cat = scan_library(root)
    thumbcache.build_cache(cat, tmp_path / "c")

    p = next(ph for ph in cat.photos if ph.rel == "p.jpg")
    assert len(p.faces) == 1
    _rect, cid, name = p.faces[0]
    assert (cid, name) == ("cccccccccccccccc", "Robert")
    entries = [e for e in cat.report.entries
              if e.kind == "infile_override" and e.detail.startswith("faces:")]
    assert len(entries) == 1
    assert 'ini "Bob" -> in-file "Robert"' in entries[0].detail


def test_infile_override_no_flood_many_xmp_only_faces(tmp_path: Path) -> None:
    """fauxcasa-cam.17 no-flood rule, the scenario that motivated it:
    Picasa's 'write faces to XMP' option strips faces= from the ini
    entirely once it's on, so EVERY faced photo becomes a pure XMP gap-
    fill (no ini match at all) — five such photos must produce ZERO
    infile_override entries, not five (which, at real-library scale,
    would be the "100k entries" flood the rule exists to prevent)."""
    import metareader

    root = tmp_path / "lib"
    for i in range(5):
        data = metareader.embed_test_metadata(
            _jpeg_bytes(), faces=[(f"Person {i}", 0.5, 0.5, 0.2, 0.2)])
        (root / f"p{i}.jpg").parent.mkdir(parents=True, exist_ok=True)
        (root / f"p{i}.jpg").write_bytes(data)
    cat = scan_library(root)
    thumbcache.build_cache(cat, tmp_path / "c")

    for i in range(5):
        p = next(ph for ph in cat.photos if ph.rel == f"p{i}.jpg")
        assert len(p.faces) == 1 and p.faces[0][2] == f"Person {i}"
    assert not [e for e in cat.report.entries
               if e.kind == "infile_override" and e.detail.startswith("faces:")]


def test_merge_xmp_faces_iou_boundary_at_0_5() -> None:
    """The IoU >= 0.5 threshold is a hard boundary: two equal-size squares
    whose overlap gives IoU just under 0.5 do NOT merge (the XMP face is
    ADDED instead); just at/over 0.5, they DO (computed via the exact
    two-equal-squares IoU formula (s-d)/(s+d), not hardcoded floats, so
    the test pins the threshold itself rather than one lucky fixture)."""
    from catalog import Photo
    import thumbcache as tc

    s = 0.2

    def rects_for_iou(target_iou: float):
        d = s * (1.0 - target_iou) / (1.0 + target_iou)
        ini = (0.4 - s / 2, 0.4 - s / 2, 0.4 + s / 2, 0.4 + s / 2)
        xmp = (0.4 + d - s / 2, 0.4 - s / 2, 0.4 + d + s / 2, 0.4 + s / 2)
        return ini, xmp

    ini_rect, xmp_rect_under = rects_for_iou(0.499)
    assert tc._face_iou(ini_rect, xmp_rect_under) < 0.5
    p_under = Photo(rel="p.jpg", folder="", name="p.jpg",
                    faces=((ini_rect, "cccccccccccccccc", None),))
    tc._merge_xmp_faces(p_under, [(xmp_rect_under, "Xmp Under")], None)
    assert len(p_under.faces) == 2  # no match found: ADDED

    ini_rect2, xmp_rect_over = rects_for_iou(0.501)
    assert tc._face_iou(ini_rect2, xmp_rect_over) >= 0.5
    p_over = Photo(rel="p.jpg", folder="", name="p.jpg",
                   faces=((ini_rect2, "cccccccccccccccc", None),))
    tc._merge_xmp_faces(p_over, [(xmp_rect_over, "Xmp Over")], None)
    assert len(p_over.faces) == 1  # matched: MERGED
    assert p_over.faces[0][2] == "Xmp Over"


def test_apply_photo_meta_faces_idempotent_no_duplicate_report() -> None:
    """Calling apply_photo_meta twice with the SAME fmeta (e.g. a re-index
    of an unchanged file) renames the face once, reports it once, and the
    second call is a no-op — never a duplicate infile_override entry nor
    a second (spurious) rename."""
    from catalog import ImportReport, Photo
    import metareader
    import thumbcache as tc

    rect = (0.4, 0.4, 0.6, 0.6)
    photo = Photo(rel="p.jpg", folder="", name="p.jpg",
                 faces=((rect, "cccccccccccccccc", "Bob"),))
    fmeta = metareader.FileMeta(faces=((rect, "Robert"),))
    report = ImportReport()

    tc.apply_photo_meta(photo, 100, 0, "sha1", inmeta.EMPTY, fmeta, report)
    assert photo.faces[0][2] == "Robert"
    assert len(report.entries) == 1

    tc.apply_photo_meta(photo, 100, 0, "sha1", inmeta.EMPTY, fmeta, report)
    assert photo.faces[0][2] == "Robert"
    assert len(report.entries) == 1  # unchanged: no duplicate entry


def test_apply_photo_meta_xmp_face_noop_when_name_unchanged(
        tmp_path: Path) -> None:
    """A geometric match that changes nothing (XMP face is unnamed, or its
    name already matches the ini's) does not rewrite photo.faces or add a
    report entry — matching apply_photo_meta's other §4 fields, which only
    report a REPLACEMENT, never a same-value touch."""
    import metareader

    root = tmp_path / "lib"
    rect = (0.40, 0.40, 0.60, 0.60)
    data = metareader.embed_test_metadata(
        _jpeg_bytes(), faces=[(None, 0.50, 0.50, 0.20, 0.20)])
    (root / "p.jpg").parent.mkdir(parents=True, exist_ok=True)
    (root / "p.jpg").write_bytes(data)
    (root / ".picasa.ini").write_text(
        f"[p.jpg]\r\nfaces=rect64({_rect64(*rect)}),cccccccccccccccc\r\n")
    cat = scan_library(root)
    original_faces = cat.photos[0].faces
    thumbcache.build_cache(cat, tmp_path / "c")

    p = next(ph for ph in cat.photos if ph.rel == "p.jpg")
    assert p.faces == original_faces  # unchanged: (rect, cid, None) already
    assert not [e for e in cat.report.entries
               if e.kind == "infile_override" and e.detail.startswith("faces:")]


def test_xmp_added_face_visible_in_people_sidebar(tmp_path: Path) -> None:
    """A non-overlapping XMP-only face (no ini match) is ADDED with an
    'xmp:<name>' id and shows up in the People sidebar under that name —
    the sidebar groups purely by Photo.faces' display name, not by id
    shape, so no people-registry change was needed for this to work."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    import metareader
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    root = tmp_path / "lib"
    data = metareader.embed_test_metadata(
        _jpeg_bytes(), faces=[("Xmp Person", 0.5, 0.5, 0.2, 0.2)])
    (root / "p.jpg").parent.mkdir(parents=True, exist_ok=True)
    (root / "p.jpg").write_bytes(data)
    cat = scan_library(root)
    thumbcache.build_cache(cat, tmp_path / "c")

    p = next(ph for ph in cat.photos if ph.rel == "p.jpg")
    assert p.faces == (((0.4, 0.4, 0.6, 0.6), "xmp:Xmp Person", "Xmp Person"),)

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    people, _unnamed, _name_cids = win._people_counts()
    assert people.get("Xmp Person") == 1


def test_metareader_caption_keywords_fallback_for_tiff(tmp_path: Path) -> None:
    """inmeta is JPEG-only, so a TIFF's caption/keywords come from
    metareader's own dc:description/dc:subject read (fmeta) — same §4
    truthy-wins-over-ini rule, reported the same way as the JPEG path."""
    import metareader

    root = tmp_path / "lib"
    data = metareader.embed_test_metadata(
        _tiff_bytes(), caption="file tiff cap", keywords=["filekw"])
    (root / "p.tif").parent.mkdir(parents=True, exist_ok=True)
    (root / "p.tif").write_bytes(data)
    (root / ".picasa.ini").write_text(
        "[p.tif]\r\ncaption=ini tiff cap\r\nkeywords=inikw\r\n")
    cat = scan_library(root)
    thumbcache.build_cache(cat, tmp_path / "c")

    p = next(ph for ph in cat.photos if ph.rel == "p.tif")
    assert p.caption == "file tiff cap"
    assert p.keywords == ("filekw",)
    entries = {e.detail.split(":")[0]: e for e in cat.report.entries
              if e.kind == "infile_override"}
    assert "caption" in entries and "keywords" in entries


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
