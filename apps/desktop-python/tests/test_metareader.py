"""Tests for metareader.py EXIF/XMP/IPTC metadata reading.

Split from test_tracer.py (fauxcasa-l09); originally lines 6906-7324 of the monolith."""

from __future__ import annotations

from pathlib import Path
import pytest
import metareader
from catalog import (
    scan_library,
)
from tracer_helpers import (
    SYDNEY,
    WHITEHORSE,
    _jpeg_bytes,
    _meta_jpeg,
    _tiff_bytes,
    make_jpeg,
)


# In-file capture date / GPS / XMP Rating via metareader — the exiv2
# bytes-mode seam (fauxcasa-cam.9/.10/.11). Fixtures are synthesized through
# metareader's OWN test-support writer (embed_test_metadata) so every exiv2
# call in the repo stays inside that one module (the library-swap seam);
# pixels are flat synthetic fills, metadata invented — privacy-safe.
# ---------------------------------------------------------------------------


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


def test_metareader_parse_rating_rejects_non_finite() -> None:
    """metareader._parse_rating (fauxcasa-ez2.12 finding 3): a writer that
    emits a non-finite Rating string ('inf', '-inf', or '1e999' — which
    Python's float() parses to inf) used to raise OverflowError out of
    int(float(...)), breaking read_file_meta's never-raises contract and
    aborting the whole index build. Must return None instead, same as any
    other unparsable value."""
    assert metareader._parse_rating("inf") is None
    assert metareader._parse_rating("-inf") is None
    assert metareader._parse_rating("1e999") is None


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


# ---------------------------------------------------------------------------
# faces-in-XMP + caption/keywords via metareader (fauxcasa-cam.5): mwg-rs
# RegionInfo and dc:description/dc:subject, the library-neutral fields that
# feed non-JPEG containers and the ini-face geometry merge (thumbcache).
# ---------------------------------------------------------------------------


def test_metareader_reads_faces_xmp() -> None:
    """mwg-rs RegionList: one named face, one unnamed (suggested) face —
    center+dims normalized XMP -> (left, top, right, bottom) STORED-pixel
    fractions, the same rect frame as ini faces= (picasa-ini-format.md)."""
    data = metareader.embed_test_metadata(
        _jpeg_bytes(),
        faces=[("Ada Test", 0.40, 0.35, 0.20, 0.30),
               (None, 0.70, 0.60, 0.15, 0.25)])
    fm = metareader.read_file_meta(data)
    assert len(fm.faces) == 2
    rect1, name1 = fm.faces[0]
    assert name1 == "Ada Test"
    assert rect1 == pytest.approx((0.30, 0.20, 0.50, 0.50))
    rect2, name2 = fm.faces[1]
    assert name2 is None
    assert rect2 == pytest.approx((0.625, 0.475, 0.775, 0.725))


def test_embed_test_metadata_faces_is_idempotent() -> None:
    """fauxcasa-za8 regression: embed_test_metadata(faces=...) called TWICE
    on the same bytes must yield the same faces the second time, not an
    empty RegionList. Before the fix, xmp.add()'s second Bag left the
    RegionList ambiguous and exiv2 serialized it as empty on write --
    reproduced live by scripts/make-synthetic-library.py's generator
    re-run wiping its own faces-in-XMP fixture. Also proves a THIRD call
    (and a stale applied_dims from the first call not leaking into a
    second call that omits it) stay clean."""
    faces = [("Ada Test", 0.40, 0.35, 0.20, 0.30),
             (None, 0.70, 0.60, 0.15, 0.25)]
    once = metareader.embed_test_metadata(
        _jpeg_bytes(), faces=faces, applied_dims=(64, 48))
    fm_once = metareader.read_file_meta(once)
    assert len(fm_once.faces) == 2

    twice = metareader.embed_test_metadata(once, faces=faces)
    fm_twice = metareader.read_file_meta(twice)
    assert fm_twice.faces == fm_once.faces

    thrice = metareader.embed_test_metadata(twice, faces=faces)
    assert metareader.read_file_meta(thrice).faces == fm_once.faces


def test_metareader_reads_caption_keywords_faces_tiff() -> None:
    """A TIFF carrier (fauxcasa-cam.5: RAW/TIFF containers) round-trips
    caption, keywords, and a face region through the same exiv2 seam."""
    data = metareader.embed_test_metadata(
        _tiff_bytes(), caption="Family at the beach",
        keywords=["beach", "family"],
        faces=[("Bob Example", 0.5, 0.5, 0.2, 0.2)])
    fm = metareader.read_file_meta(data)
    assert fm.caption == "Family at the beach"
    assert fm.keywords == ("beach", "family")
    assert len(fm.faces) == 1
    assert fm.faces[0][1] == "Bob Example"


def test_metareader_faces_garbage_packet_is_empty() -> None:
    """Hostile/non-image bytes: faces (and caption/keywords) fail soft to
    the all-empty FileMeta, same contract as date/gps/rating."""
    assert metareader.read_file_meta(b"garbage" * 1000) == metareader.EMPTY
    assert metareader.read_file_meta(b"") == metareader.EMPTY


def test_metareader_faces_capped_at_max_faces() -> None:
    """A packet carrying MAX_FACES+50 valid Face regions yields only
    MAX_FACES — the DoS guard (decodesvc.MAX_FACES, shared with the future
    decode sandbox — fauxcasa-cam.5 review fix) so a hostile packet cannot
    balloon the catalog."""
    from decodesvc import MAX_FACES

    faces = [(f"Person {i}", 0.1, 0.1, 0.02, 0.02)
             for i in range(MAX_FACES + 50)]
    data = metareader.embed_test_metadata(_jpeg_bytes(), faces=faces)
    fm = metareader.read_file_meta(data)
    assert len(fm.faces) == MAX_FACES


def test_metareader_faces_pixel_unit_skipped() -> None:
    """stArea:unit != 'normalized' (e.g. legacy pixel-unit regions) is
    skipped outright rather than misread as a 0..1 fraction."""
    import exiv2  # noqa: PLC0415 (test-only direct use, to plant a unit
    # this module's own writer never emits — embed_test_metadata always
    # writes normalized, so proving the reader's unit filter needs a
    # hand-planted non-normalized value)

    data = metareader.embed_test_metadata(
        _jpeg_bytes(), faces=[("Px Person", 100, 100, 50, 50)])
    img = exiv2.ImageFactory.open(data)
    img.readMetadata()
    img.xmpData()["Xmp.mwg-rs.Regions/mwg-rs:RegionList[1]/mwg-rs:Area/"
                   "stArea:unit"] = "pixel"
    img.writeMetadata()
    out = bytes(img.io().mmap())
    assert metareader.read_file_meta(out).faces == ()


def test_metareader_faces_type_pet_skipped_missing_type_defaults_face() -> None:
    """mwg-rs:Type == 'Pet' (or any non-Face type) is skipped; a region
    with NO Type property at all defaults to Face per MWG (both proved in
    one packet, alongside a real Face region, so the filter can't just be
    accidentally accepting everything)."""
    import exiv2  # noqa: PLC0415 (test-only: plant a Type embed_test_metadata
    # itself never writes anything but "Face")

    data = metareader.embed_test_metadata(
        _jpeg_bytes(),
        faces=[("Fido", 0.2, 0.2, 0.1, 0.1),      # region 1: will become Pet
               ("Ada", 0.5, 0.5, 0.1, 0.1),        # region 2: Type removed
               ("Bob", 0.8, 0.8, 0.1, 0.1)])       # region 3: stays Face
    img = exiv2.ImageFactory.open(data)
    img.readMetadata()
    xmp = img.xmpData()
    xmp["Xmp.mwg-rs.Regions/mwg-rs:RegionList[1]/mwg-rs:Type"] = "Pet"
    del xmp["Xmp.mwg-rs.Regions/mwg-rs:RegionList[2]/mwg-rs:Type"]
    img.writeMetadata()
    out = bytes(img.io().mmap())
    fm = metareader.read_file_meta(out)
    assert [n for _r, n in fm.faces] == ["Ada", "Bob"]


def test_metareader_faces_name_only_region_does_not_hide_next(
        ) -> None:
    """A region with mwg-rs:Name but no Area at all (malformed/incomplete)
    is skipped, not treated as 'the RegionList ends here' — a valid
    region 2 right after it must still be read (review fix: `continue`,
    not `break`, on a missing stArea:x)."""
    import exiv2

    data = metareader.embed_test_metadata(
        _jpeg_bytes(), faces=[("Valid Two", 0.5, 0.5, 0.1, 0.1)])
    img = exiv2.ImageFactory.open(data)
    img.readMetadata()
    xmp = img.xmpData()
    # Insert a Name-only region 1, shifting the valid region to index 2 by
    # re-writing both entries from scratch (indices are 1-based, in-order).
    base = exiv2.XmpTextValue()
    base.setXmpArrayType(exiv2.XmpValue.XmpArrayType.xaBag)
    xmp2 = img.xmpData()
    del xmp2["Xmp.mwg-rs.Regions/mwg-rs:RegionList[1]/mwg-rs:Name"]
    del xmp2["Xmp.mwg-rs.Regions/mwg-rs:RegionList[1]/mwg-rs:Type"]
    del xmp2["Xmp.mwg-rs.Regions/mwg-rs:RegionList[1]/mwg-rs:Area/stArea:x"]
    del xmp2["Xmp.mwg-rs.Regions/mwg-rs:RegionList[1]/mwg-rs:Area/stArea:y"]
    del xmp2["Xmp.mwg-rs.Regions/mwg-rs:RegionList[1]/mwg-rs:Area/stArea:w"]
    del xmp2["Xmp.mwg-rs.Regions/mwg-rs:RegionList[1]/mwg-rs:Area/stArea:h"]
    del xmp2["Xmp.mwg-rs.Regions/mwg-rs:RegionList[1]/mwg-rs:Area/stArea:unit"]
    xmp2["Xmp.mwg-rs.Regions/mwg-rs:RegionList[1]/mwg-rs:Name"] = "Name Only"
    xmp2["Xmp.mwg-rs.Regions/mwg-rs:RegionList[2]/mwg-rs:Name"] = "Valid Two"
    xmp2["Xmp.mwg-rs.Regions/mwg-rs:RegionList[2]/mwg-rs:Area/stArea:x"] = "0.5"
    xmp2["Xmp.mwg-rs.Regions/mwg-rs:RegionList[2]/mwg-rs:Area/stArea:y"] = "0.5"
    xmp2["Xmp.mwg-rs.Regions/mwg-rs:RegionList[2]/mwg-rs:Area/stArea:w"] = "0.1"
    xmp2["Xmp.mwg-rs.Regions/mwg-rs:RegionList[2]/mwg-rs:Area/stArea:h"] = "0.1"
    xmp2["Xmp.mwg-rs.Regions/mwg-rs:RegionList[2]/mwg-rs:Area/stArea:unit"] = \
        "normalized"
    img.writeMetadata()
    out = bytes(img.io().mmap())
    fm = metareader.read_file_meta(out)
    assert [n for _r, n in fm.faces] == ["Valid Two"]


def test_metareader_caption_takes_first_alternative_of_two_languages() -> None:
    """exiv2's LangAlt toString() for TWO alternatives joins them as
    'lang="x-default" <text>, lang="fr-FR" <text>' (confirmed empirically
    against python-exiv2 0.19.2) — only the first (x-default) text must
    surface, never the second language's text tacked on after a comma."""
    import exiv2

    data = metareader.embed_test_metadata(_jpeg_bytes())
    img = exiv2.ImageFactory.open(data)
    img.readMetadata()
    xmp = img.xmpData()
    xmp["Xmp.dc.description"] = 'lang="x-default" Default text'
    xmp["Xmp.dc.description"] = 'lang="fr-FR" Texte francais'
    img.writeMetadata()
    out = bytes(img.io().mmap())
    fm = metareader.read_file_meta(out)
    assert fm.caption == "Default text"


def test_metareader_caption_truncated_to_max_caption_bytes() -> None:
    """A caption longer than MAX_CAPTION_BYTES is cut, on a UTF-8
    boundary, never raised or dropped entirely (fauxcasa-cam.5 review fix:
    ExtendedXMP removed the natural size bound a caption used to have)."""
    from decodesvc import MAX_CAPTION_BYTES

    long_caption = "é" * (MAX_CAPTION_BYTES)  # each char is 2 UTF-8 bytes
    data = metareader.embed_test_metadata(_jpeg_bytes(), caption=long_caption)
    fm = metareader.read_file_meta(data)
    assert fm.caption is not None
    assert len(fm.caption.encode("utf-8")) <= MAX_CAPTION_BYTES
    assert long_caption.startswith(fm.caption)  # a clean prefix, no mangling


def test_metareader_keywords_capped_at_max_keywords() -> None:
    from decodesvc import MAX_KEYWORDS

    kws = [f"kw{i}" for i in range(MAX_KEYWORDS + 10)]
    data = metareader.embed_test_metadata(_jpeg_bytes(), keywords=kws)
    fm = metareader.read_file_meta(data)
    assert len(fm.keywords) == MAX_KEYWORDS


def test_metareader_extended_xmp_garbage_keeps_main_packet_fields() -> None:
    """A garbage `extended_xmp` argument (the TIFF-shell parse fails)
    degrades to the main packet's OWN fields — never an exception, and
    never a wipe of what the main packet already carried."""
    data = metareader.embed_test_metadata(
        _jpeg_bytes(), caption="from main packet", rating=4)
    fm = metareader.read_file_meta(data, extended_xmp=b"not xml at all!!")
    assert fm.caption == "from main packet"
    assert fm.rating == 4


def test_metareader_faces_orientation_frame_transpose() -> None:
    """mwg-rs:AppliedToDimensions equal to the STORED dims TRANSPOSED
    means the writer expressed the region in the DISPLAY frame; the
    region is rotated back into the stored frame by the inverse
    orientation. A round-trip through the SAME orientation
    (cropmap.map_fraction_rect) must reproduce the display-frame rect the
    writer meant — i.e. the overlay position is unaffected by which frame
    the writer chose."""
    from cropmap import map_fraction_rect

    display_rect_meant = (0.40, 0.15, 0.60, 0.35)
    dl, dt, dr, db = display_rect_meant
    cx, cy = (dl + dr) / 2, (dt + db) / 2
    w, h = dr - dl, db - dt
    data = metareader.embed_test_metadata(
        _jpeg_bytes(64, 48),  # stored 64x48 landscape
        orientation=6,        # display: 48x64 portrait
        faces=[("Ada", cx, cy, w, h)],
        applied_dims=(48, 64))  # TRANSPOSED vs stored (64, 48)
    fm = metareader.read_file_meta(data)
    assert len(fm.faces) == 1
    stored_rect, name = fm.faces[0]
    assert name == "Ada"
    back = map_fraction_rect(stored_rect, 6, 0)
    assert back == pytest.approx(display_rect_meant, abs=1e-6)


def test_metareader_faces_orientation_frame_no_transpose_when_matching(
        ) -> None:
    """AppliedToDimensions equal to the stored dims AS-IS (no transpose)
    leaves the region exactly as computed — the common/expected case."""
    data = metareader.embed_test_metadata(
        _jpeg_bytes(64, 48), orientation=6,
        faces=[("Ada", 0.5, 0.25, 0.2, 0.2)],
        applied_dims=(64, 48))  # matches stored, no transpose
    fm = metareader.read_file_meta(data)
    assert fm.faces[0][0] == pytest.approx((0.4, 0.15, 0.6, 0.35))


def test_metareader_faces_orientation_frame_absent_keeps_as_is() -> None:
    """No AppliedToDimensions at all: the pre-guard behavior (region used
    exactly as computed) — the guard must not require the property."""
    data = metareader.embed_test_metadata(
        _jpeg_bytes(64, 48), orientation=6,
        faces=[("Ada", 0.5, 0.25, 0.2, 0.2)])
    fm = metareader.read_file_meta(data)
    assert fm.faces[0][0] == pytest.approx((0.4, 0.15, 0.6, 0.35))


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
