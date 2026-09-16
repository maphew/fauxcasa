"""Tests for inmeta.py in-file metadata reader (ExtendedXMP, precedence, import report).

Split from test_tracer.py (fauxcasa-l09); originally lines 1220-1639 of the monolith."""

from __future__ import annotations

import struct
from pathlib import Path
import inmeta
import thumbcache
from catalog import (
    load_catalog,
    save_catalog,
    scan_library,
)
from tracer_helpers import (
    WHITEHORSE,
    _EXT_GUID,
    _exif_orientation_app1,
    _ext_app1,
    _face_region_xml,
    _inject,
    _iptc_app13,
    _jpeg_bytes,
    _jpeg_with_segments,
    _main_xmp_with_note,
    _meta_jpeg,
    _xmp_app1,
    write_jpeg_meta,
)


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


# ---- ExtendedXMP reassembly (fauxcasa-cam.5): a main XMP packet over the
# ~64 KB single-APP1-segment ceiling spills into extension APP1 segments,
# tied to the main packet by a shared GUID (xmpNote:HasExtendedXMP on the
# main side, a per-chunk header on the extension side). ---------------------


def test_inmeta_extended_xmp_reassembles_out_of_order_chunks() -> None:
    """Three chunks arriving in the FILE in a scrambled order (2, 0, 1)
    still reassemble correctly — reassembly sorts by the declared offset,
    not file/segment order."""
    body = b"<x:xmpmeta>" + b"Y" * 300 + b"</x:xmpmeta>"
    c0, c1, c2 = body[:100], body[100:200], body[200:]
    jpeg = _jpeg_with_segments(
        _main_xmp_with_note(_EXT_GUID),
        _ext_app1(_EXT_GUID, len(body), 200, c2),
        _ext_app1(_EXT_GUID, len(body), 0, c0),
        _ext_app1(_EXT_GUID, len(body), 100, c1),
    )
    assert inmeta.extended_xmp(jpeg) == body


def test_inmeta_extended_xmp_wrong_guid_chunk_ignored() -> None:
    """A chunk stamped with a DIFFERENT GUID (a leftover from some other
    packet/file merge) is ignored, not spliced into the result."""
    body = b"<x:xmpmeta>" + b"Z" * 100 + b"</x:xmpmeta>"
    other_guid = b"CD" * 16
    jpeg = _jpeg_with_segments(
        _main_xmp_with_note(_EXT_GUID),
        _ext_app1(other_guid, 9999, 0, b"not part of this packet at all!"),
        _ext_app1(_EXT_GUID, len(body), 0, body),
    )
    assert inmeta.extended_xmp(jpeg) == body


def test_inmeta_extended_xmp_missing_chunk_is_none() -> None:
    """A gap (a chunk never arrived) -> None, not a truncated best-effort
    result — a hole in the middle of an XMP packet is not valid XML
    anyway, so failing closed is the only safe behavior."""
    body = b"<x:xmpmeta>" + b"W" * 300 + b"</x:xmpmeta>"
    c0, _c1, c2 = body[:100], body[100:200], body[200:]
    jpeg = _jpeg_with_segments(
        _main_xmp_with_note(_EXT_GUID),
        _ext_app1(_EXT_GUID, len(body), 0, c0),
        _ext_app1(_EXT_GUID, len(body), 200, c2),  # chunk at offset 100 missing
    )
    assert inmeta.extended_xmp(jpeg) is None


def test_inmeta_extended_xmp_absent_is_none() -> None:
    """No HasExtendedXMP note and no extension segments at all: None, not
    an exception — the common case (most JPEGs have no ExtendedXMP)."""
    assert inmeta.extended_xmp(_jpeg_bytes()) is None
    assert inmeta.extended_xmp(b"garbage" * 1000) is None


def test_inmeta_extended_xmp_rejects_over_max_bytes() -> None:
    """A chunk header declaring a total length over
    _MAX_EXTENDED_XMP_BYTES is rejected outright (None) — a hostile
    length field must not drive an allocation anywhere near that size."""
    jpeg = _jpeg_with_segments(
        _main_xmp_with_note(_EXT_GUID),
        _ext_app1(_EXT_GUID, inmeta._MAX_EXTENDED_XMP_BYTES + 1, 0, b"x"))
    assert inmeta.extended_xmp(jpeg) is None


def test_inmeta_extended_xmp_inconsistent_total_length_is_none() -> None:
    """Two chunks (same GUID) that DISAGREE on the packet's declared total
    length is corrupt/mixed input — rejected, not resolved by picking
    either value."""
    jpeg = _jpeg_with_segments(
        _main_xmp_with_note(_EXT_GUID),
        _ext_app1(_EXT_GUID, 100, 0, b"a" * 50),
        _ext_app1(_EXT_GUID, 200, 50, b"b" * 50))
    assert inmeta.extended_xmp(jpeg) is None


def test_inmeta_extended_xmp_overlapping_chunks_is_none() -> None:
    """Two chunks whose declared offsets overlap (the second starts before
    the first ends) fail the contiguous-tiling check — a gap and an
    overlap are both 'doesn't fit the shape', not a best-effort splice."""
    body = b"<x:xmpmeta>" + b"Q" * 100 + b"</x:xmpmeta>"
    jpeg = _jpeg_with_segments(
        _main_xmp_with_note(_EXT_GUID),
        _ext_app1(_EXT_GUID, len(body), 0, body[:60]),
        _ext_app1(_EXT_GUID, len(body), 50, body[50:]))  # overlaps [50,60)
    assert inmeta.extended_xmp(jpeg) is None


def test_read_jpeg_metadata_fills_caption_from_extended_xmp() -> None:
    """A caption too long for the main packet spills into ExtendedXMP;
    read_jpeg_metadata reassembles it and fills the caption the main
    packet's dc:description omitted (main-packet field wins when both
    carry one — proved by the keywords side of this same fixture)."""
    long_caption = "A" * 5000
    ext_xml = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<rdf:Description rdf:about="">'
        f'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">'
        f'{long_caption}</rdf:li></rdf:Alt></dc:description>'
        # a keyword ALSO in the extension: must NOT override the main
        # packet's own (different) keyword below.
        '<dc:subject><rdf:Bag><rdf:li>from-extension</rdf:li></rdf:Bag>'
        '</dc:subject>'
        '</rdf:Description></rdf:RDF></x:xmpmeta>'
    ).encode("utf-8")
    jpeg = _jpeg_with_segments(
        _main_xmp_with_note(_EXT_GUID),
        _ext_app1(_EXT_GUID, len(ext_xml), 0, ext_xml),
    )
    jpeg = _inject(jpeg, 0xE1, _xmp_app1(keywords=("from-main",)))
    m = inmeta.read_jpeg_metadata(jpeg)
    assert m.caption == long_caption
    assert m.keywords == ("from-main",)  # main packet wins per field


def test_extended_xmp_end_to_end_faces_and_caption(tmp_path: Path) -> None:
    """The acceptance case: a >64 KB packet, faces living ONLY in the
    extension, read end-to-end through read_photo_meta (the indexer's
    actual entry point) — proving inmeta.extended_xmp +
    metareader.read_file_meta's extended_xmp fallback compose correctly,
    not just each in isolation."""
    import metareader
    import thumbcache
    from catalog import Photo

    big_padding = "P" * 70000  # forces the main packet over the JPEG
    # APP1 ceiling if it were inline; here it's what's split into ext.
    ext_xml = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<rdf:Description rdf:about="">'
        f'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">'
        f'Family at the beach {big_padding}</rdf:li></rdf:Alt>'
        '</dc:description>'
        f'{_face_region_xml("Ada Test", 0.4, 0.35, 0.2, 0.3)}'
        '</rdf:Description></rdf:RDF></x:xmpmeta>'
    ).encode("utf-8")
    assert len(ext_xml) > 65536
    chunk_size = 60000
    chunks = [ext_xml[i:i + chunk_size]
             for i in range(0, len(ext_xml), chunk_size)]
    ext_segments = [_ext_app1(_EXT_GUID, len(ext_xml), i * chunk_size, c)
                    for i, c in enumerate(chunks)]
    jpeg = _jpeg_with_segments(_main_xmp_with_note(_EXT_GUID), *ext_segments)

    path = tmp_path / "big.jpg"
    path.write_bytes(jpeg)
    photo = Photo(rel="big.jpg", folder="", name="big.jpg")
    data, _size, _mtime, _sha, meta, fmeta = thumbcache.read_photo_meta(
        path, photo)
    assert data == jpeg
    assert meta.caption is not None and meta.caption.startswith(
        "Family at the beach")
    assert len(fmeta.faces) == 1
    assert fmeta.faces[0][1] == "Ada Test"


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


# ---- import-report entries for in-file overrides (fauxcasa-cam.17) --------


def test_infile_override_reported(tmp_path: Path) -> None:
    """When an in-file value replaces a DIFFERENT non-empty ini value, an
    infile_override entry is added to the catalog's import report (§4).
    Covers caption and keywords here (see test_infile_override_geotag_reported
    for geotag); date_taken has no ini key so it is always a gap-fill and
    never produces an entry, and star is deliberately excluded (machine-local
    star overlays make the ini-original value unobservable at this point —
    see apply_photo_meta)."""
    root = tmp_path / "lib"
    write_jpeg_meta(root / "f" / "p.jpg",
                    xmp=_xmp_app1(caption="file cap", keywords=("fkw",)))
    (root / "f" / ".picasa.ini").write_text(
        "[p.jpg]\r\ncaption=ini cap\r\nkeywords=ikw\r\n")
    cat = scan_library(root)
    thumbcache.build_cache(cat, tmp_path / "c")

    entries = [e for e in cat.report.entries if e.kind == "infile_override"]
    # both caption and keywords were overridden
    subjects = {e.subject for e in entries}
    assert "f/p.jpg" in subjects
    kinds_detail = {e.subject: e.detail for e in entries}
    cap_e = next(e for e in entries if "caption" in e.detail)
    assert 'ini "ini cap"' in cap_e.detail and 'in-file "file cap"' in cap_e.detail
    assert cap_e.source == "file"
    kw_e = next(e for e in entries if "keywords" in e.detail)
    assert '"ikw"' in kw_e.detail and '"fkw"' in kw_e.detail


def test_infile_override_geotag_reported(tmp_path: Path) -> None:
    """A geotag override (ini geotag= beaten by in-file EXIF GPS) produces an
    infile_override entry. star is deliberately NOT reported here even
    though XMP Rating=3 beats ini star=yes: machine-local star overlays
    (apply_star_overrides) are applied before build_cache runs, so
    photo.star may already be an overlay value rather than the ini-original
    — a report entry would misattribute the overlay to "ini" (see
    apply_photo_meta)."""
    root = tmp_path / "mlib"
    _meta_jpeg(root / "f" / "a.jpg", gps=WHITEHORSE, rating=3)
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\nstar=yes\r\ngeotag=-33.856800,151.215300\r\n")
    cat = scan_library(root)
    thumbcache.build_cache(cat, tmp_path / "c")

    overrides = {e.detail.split(":")[0]: e
                 for e in cat.report.entries if e.kind == "infile_override"}
    assert "geotag" in overrides
    assert "star" not in overrides
    a = next(p for p in cat.photos if p.rel == "f/a.jpg")
    assert a.star == 3  # in-file rating still WINS on the photo itself


def test_infile_override_no_flood(tmp_path: Path) -> None:
    """Gap-fills — ini had no value, in-file supplies one — must NOT add
    a report entry; only a genuine replacement of a different non-empty ini
    value counts as an override (fauxcasa-cam.17 no-flood rule)."""
    root = tmp_path / "lib"
    # p: has in-file caption but NO ini caption/keywords -> gap-fill, no entry
    write_jpeg_meta(root / "f" / "p.jpg",
                    xmp=_xmp_app1(caption="file only"))
    # q: in-file caption MATCHES the ini caption -> same value, no entry
    write_jpeg_meta(root / "f" / "q.jpg",
                    xmp=_xmp_app1(caption="same"))
    (root / "f" / ".picasa.ini").write_text(
        "[q.jpg]\r\ncaption=same\r\n")
    cat = scan_library(root)
    thumbcache.build_cache(cat, tmp_path / "c")

    overrides = [e for e in cat.report.entries if e.kind == "infile_override"]
    assert not overrides, f"unexpected override entries: {overrides}"
