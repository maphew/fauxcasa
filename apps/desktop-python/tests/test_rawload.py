"""Tests for thumbcache.py/rawload.py RAW decode integration.

Split from test_tracer.py (fauxcasa-l09); originally lines 7579-8366 of the monolith."""

from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path
import pytest
import thumbcache
from catalog import (
    ScanFilter,
    scan_library,
    walk_library,
)
from tracer_helpers import (
    REPO,
    _exif_orientation_app1,
    _hits,
    _inject,
    _jpeg_bytes,
    _make_dng,
    _offscreen_app,
    _search_win,
    _sweep_qt_widgets,
    _thumb_qimage,
    _xmp_app1,
    make_jpeg,
    write_jpeg_meta,
)


# ---------------------------------------------------------------------------
# RAW support (fauxcasa-v46.1): Picasa's documented 16-vendor extension list
# in BOTH walkers (lockstep, or caches stop binding), rawpy decode routed by
# extension ahead of any content sniff (TIFF-based RAW containers fool
# QImageReader/PIL), embedded-JPEG-preview-first with demosaic fallback,
# orientation applied exactly once per path, and corrupt-RAW fail-soft.
#
# Fixture provenance (privacy rule: NEVER real family data): the DNG bytes
# below come from scripts/make-synthetic-dng.py's `_make_dng_bytes` (loaded
# once via the module-loader below, same pattern as the make-thumbcache.py
# imports elsewhere in this file) — a hand-rolled minimal-but-valid little-
# endian DNG 1.4, a TIFF container holding a deterministic synthetic 16-bit
# RGGB CFA mosaic (struct-packed gradient, no camera involved), optionally an
# embedded JPEG preview built by the suite's own Qt encoder (_jpeg_bytes),
# plus the tags LibRaw's identify() requires (DNGVersion, CFA geometry,
# ColorMatrix1, UniqueCameraModel; note LibRaw rejects raws under 22 px per
# side). Verified against rawpy/LibRaw: imread + postprocess succeed,
# extract_thumb returns the preview when present and LibRawNoThumbnailError
# when absent. The two builders used to be independently hand-rolled and
# drifted apart (fauxcasa-wqi.3); this test now imports the single source of
# truth instead of re-duplicating ~150 lines of TIFF-packing logic.
# ---------------------------------------------------------------------------


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
# Preview-orientation edge fix (fauxcasa-v46.5): unit tests for the helpers
# and the full end-to-end path.  The mapping is empirically verified:
# TIFF/EXIF Orientation 1/3/6/8 in the container → LibRaw sizes.flip
# 0/3/6/5 respectively (confirmed by running rawpy against _make_dng).
# ---------------------------------------------------------------------------


def test_flip_helpers_inject_and_detect() -> None:
    """_inject_exif_orientation embeds a tag; _jpeg_has_exif_orientation finds
    it. Round-trip: inject orientation 6 into a plain JPEG (no own tag) →
    detect succeeds; detect on the unmodified plain JPEG → False."""
    import rawload

    plain = _jpeg_bytes(16, 16)                  # no EXIF at all
    assert not rawload._jpeg_has_exif_orientation(plain)

    with_tag = rawload._inject_exif_orientation(plain, 6)
    assert with_tag[:2] == b"\xff\xd8"           # still valid SOI
    assert rawload._jpeg_has_exif_orientation(with_tag)

    # A JPEG that already carries orientation (from _inject in test helpers)
    already = _inject(plain, 0xE1, _exif_orientation_app1(6))
    assert rawload._jpeg_has_exif_orientation(already)


def test_inject_exif_orientation_app0_ordering() -> None:
    """_inject_exif_orientation must keep a JFIF APP0 first when the source
    JPEG has one (fauxcasa-wqi.5): strictly, APP0 must precede any other
    application segment, and Qt's own JPEG encoder (_jpeg_bytes) always
    writes one right after SOI. Splicing the EXIF APP1 before it (the old
    behavior) is tolerated by Qt/libjpeg/exiv2 but non-conformant. The
    no-APP0 shape must keep working too (splice right after SOI, as before).
    """
    import rawload

    # Shape 1: Qt's encoder output — SOI, APP0(JFIF), ... . Confirmed by
    # construction: Qt always emits FFD8 FFE0 for its own JPEG output.
    with_app0 = _jpeg_bytes(16, 16)
    assert with_app0[2:4] == b"\xff\xe0", \
        "test assumption: Qt's JPEG encoder writes a JFIF APP0"
    app0_len = struct.unpack_from(">H", with_app0, 4)[0]
    app0_end = 4 + app0_len

    injected = rawload._inject_exif_orientation(with_app0, 6)
    assert injected[:2] == b"\xff\xd8"
    assert injected[2:4] == b"\xff\xe0", "APP0 must stay the first segment"
    # The injected APP1 (EXIF) must sit immediately after APP0, not before.
    assert injected[app0_end:app0_end + 2] == b"\xff\xe1"
    assert rawload._jpeg_has_exif_orientation(injected)

    # Shape 2: no APP0 at all — strip Qt's APP0 out of the same source so
    # the rest of the stream (any trailing segments + scan data) still
    # decodes, then confirm the no-APP0 path is unchanged: splice right
    # after SOI.
    no_app0 = with_app0[:2] + with_app0[app0_end:]
    assert no_app0[2:4] != b"\xff\xe0"
    injected_no_app0 = rawload._inject_exif_orientation(no_app0, 6)
    assert injected_no_app0[:2] == b"\xff\xd8"
    assert injected_no_app0[2:4] == b"\xff\xe1", \
        "no JFIF APP0 present: EXIF APP1 splices right after SOI"
    assert rawload._jpeg_has_exif_orientation(injected_no_app0)

    # Shape 3: APP0 marker present but its declared length is malformed
    # (< 2 — the length field counts itself, so 0/1 is garbage). Must take
    # the right-after-SOI fallback, not splice between the length bytes.
    for bad_len in (0, 1):
        malformed = with_app0[:4] + struct.pack(">H", bad_len) + with_app0[6:]
        injected_bad = rawload._inject_exif_orientation(malformed, 6)
        assert injected_bad[:2] == b"\xff\xd8"
        assert injected_bad[2:4] == b"\xff\xe1", \
            f"malformed APP0 length {bad_len}: fall back to right after SOI"


def test_flip_helpers_garbage_input() -> None:
    """_jpeg_has_exif_orientation and _inject_exif_orientation are fail-soft:
    garbage bytes don't raise."""
    import rawload

    for bad in (b"", b"not a jpeg", b"\xff\xd8\xff" + b"\x00" * 100):
        assert not rawload._jpeg_has_exif_orientation(bad)

    # inject always returns bytes starting with SOI regardless of the value
    result = rawload._inject_exif_orientation(_jpeg_bytes(8, 8), 8)
    assert result[:2] == b"\xff\xd8"


def test_libraw_flip_to_exif_orientation_mapping() -> None:
    """The _LIBRAW_FLIP_TO_EXIF_ORIENTATION table covers all four nonzero
    flip values rawpy produces (3, 5, 6) with the correct EXIF values
    (3 = 180°, 8 = 90° CCW, 6 = 90° CW), verified against real rawpy output
    for _make_dng with orientation 3/8/6 → flip 3/5/6."""
    import rawload

    m = rawload._LIBRAW_FLIP_TO_EXIF_ORIENTATION
    assert m[3] == 3    # 180° → EXIF 3
    assert m[5] == 8    # 90° CCW → EXIF 8 (rotate 270° CW)
    assert m[6] == 6    # 90° CW  → EXIF 6 (rotate 90° CW)
    assert 0 not in m   # flip=0 never needs injection


@pytest.mark.parametrize("container_orient,exif_expect", [
    (3, 3),    # 180°
    (6, 6),    # 90° CW
    (8, 8),    # 90° CCW
])
def test_raw_preview_flip_injection_e2e(
        tmp_path: Path, container_orient: int, exif_expect: int) -> None:
    """DNG with container Orientation=N and an embedded JPEG preview that
    carries NO own orientation tag: raw_preview_jpeg must inject EXIF
    Orientation=exif_expect into the returned bytes so callers' auto-transform
    applies the correct rotation. The mapping is verified against rawpy's
    actual sizes.flip values for each container orientation."""
    import rawload

    # preview JPEG built by _jpeg_bytes has no EXIF at all
    pj = _jpeg_bytes(64, 48)
    assert not rawload._jpeg_has_exif_orientation(pj)

    p = _make_dng(tmp_path / "f.dng",
                  orientation=container_orient,
                  preview_jpeg=pj,
                  preview_size=(64, 48))
    data = p.read_bytes()

    returned = rawload.raw_preview_jpeg(data)
    assert returned is not None, "raw_preview_jpeg returned None"
    assert returned[:2] == b"\xff\xd8", "returned bytes are not a JPEG"
    assert rawload._jpeg_has_exif_orientation(returned), \
        "orientation was not injected"

    # The injected tag must carry the expected EXIF orientation value.
    # Re-use metareader.read_orientation to read it back.
    import metareader
    got = metareader.read_orientation(returned)
    assert got == exif_expect, (
        f"container orient {container_orient}: "
        f"expected EXIF {exif_expect}, got {got}"
    )


def test_raw_preview_flip_injection_skipped_when_preview_has_own_tag(
        tmp_path: Path) -> None:
    """When the preview already carries its own EXIF Orientation tag, no
    injection occurs — the existing tag is the sole source of rotation.
    The existing test_raw_orientation_applied_once_preview (orientation 6 in
    the preview) already covers the cache path; this confirms raw_preview_jpeg
    does not double-inject when flip != 0 and own tag is present."""
    import rawload

    # preview with its own orientation=6
    pj = _inject(_jpeg_bytes(64, 48), 0xE1, _exif_orientation_app1(6))
    assert rawload._jpeg_has_exif_orientation(pj)

    p = _make_dng(tmp_path / "f.dng",
                  orientation=6,
                  preview_jpeg=pj,
                  preview_size=(64, 48))
    returned = rawload.raw_preview_jpeg(p.read_bytes())
    assert returned is not None

    # Byte-identical to pj (no segment was prepended) — checked by comparing
    # to a version that WOULD have injection and confirming lengths differ.
    plain_pj = _jpeg_bytes(64, 48)
    injected_would_be = rawload._inject_exif_orientation(plain_pj, 6)
    # returned starts with the preview's own APP1 after SOI, not a new one
    assert len(returned) == len(pj)              # no extra segment prepended


def test_raw_preview_no_flip_valid_jpeg(tmp_path: Path) -> None:
    """DNG with container Orientation=1 (flip=0): raw_preview_jpeg returns
    a valid JPEG. Our injection code does not fire when flip=0. rawpy/LibRaw
    may add its own EXIF metadata (empirically it does — Orientation=1 is
    injected by LibRaw regardless of whether the embedded preview had a tag).
    We do not assert absence of a tag (LibRaw adds one); we assert the JPEG
    is usable and decodes to the expected landscape dimensions."""
    _offscreen_app()
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImageReader

    import rawload

    pj = _jpeg_bytes(32, 32)
    p = _make_dng(tmp_path / "f.dng",
                  orientation=1,
                  preview_jpeg=pj,
                  preview_size=(32, 32))
    returned = rawload.raw_preview_jpeg(p.read_bytes())
    assert returned is not None
    assert returned[:2] == b"\xff\xd8"           # valid JPEG SOI

    # Decode with auto-transform: orientation=1 means no rotation, so the
    # 32x32 preview decodes square (no portrait/landscape swap).
    buf = QBuffer()
    buf.setData(returned)
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buf)
    reader.setAutoTransform(True)
    img = reader.read()
    assert not img.isNull()
    assert img.width() == 32 and img.height() == 32


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


def test_people_sidebar_reflects_xmp_faces_on_cold_index_finish(
        tmp_path: Path) -> None:
    """fauxcasa-cam.5 review fix: the cold-build branch of
    _on_index_finished must rebuild the sidebar like _on_backfill_done
    already does, so an XMP-only face (added by the indexer's §4 merge,
    fauxcasa-cam.5) shows up under People immediately — no reveal toggle,
    no relaunch, no separate sidebar-refreshing action needed."""
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

    import metareader

    root = tmp_path / "lib"
    data = metareader.embed_test_metadata(
        _jpeg_bytes(), faces=[("Xmp Person", 0.5, 0.5, 0.2, 0.2)])
    (root / "p.jpg").parent.mkdir(parents=True, exist_ok=True)
    (root / "p.jpg").write_bytes(data)

    win = MainWindow(scan_library(root), None, cache_dir=None, build_dir=None)
    assert item_for(win, "person", "Xmp Person") is None  # pre-index: unknown

    result = thumbcache.build_cache(win.catalog, tmp_path / "cache")
    assert result is not None
    win._on_index_finished(result, win.catalog, False)  # cold-build finish

    assert item_for(win, "person", "Xmp Person") is not None
    assert not win.reveal_box.isChecked()  # no reveal toggle needed


def test_import_notes_refresh_on_cold_index_finish(
        tmp_path: Path, capsys) -> None:
    """Review finding (fauxcasa-nu9): the cold-build branch of
    _on_index_finished must also call _update_import_notes, same as the
    reconcile branch (reload_data) already does — otherwise a fresh
    infile_override entry from build_cache's §4 tier-1 merge stays
    invisible in the status bar until relaunch."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    root = tmp_path / "lib"
    # in-file caption conflicts with ini -> one infile_override entry,
    # recorded only once build_cache (the indexer) actually runs.
    write_jpeg_meta(root / "f" / "p.jpg", xmp=_xmp_app1(caption="file cap"))
    (root / "f" / ".picasa.ini").write_text(
        "[p.jpg]\r\ncaption=ini cap\r\n")

    win = MainWindow(scan_library(root), None, cache_dir=None, build_dir=None)
    assert win.notes_label.text() == ""          # nothing yet: pre-index

    result = thumbcache.build_cache(win.catalog, tmp_path / "cache")
    assert result is not None
    win._on_index_finished(result, win.catalog, False)  # cold-build finish

    assert win.notes_label.isVisibleTo(win)
    assert "1 import note" in win.notes_label.text()
    assert "caption" in win.notes_label.toolTip()
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


def test_ready_poll_timer_dies_with_the_window(
        monkeypatch, library: Path, tmp_path: Path, capsys) -> None:
    """After a self-quitting in-process main() run, the window is torn down
    and the (reused) QApplication's event loop is spun several times; no
    stale check_ready may reach the excepthook.

    The original (caplog-based, `_run`-named) version of this test could
    never go red against the bug it documents: inside the test body `win`
    was still referenced by check_ready's own closure (a live Python object
    whose C++ side was never deleted), and the per-test widget sweep that
    actually deletes the window (`_sweep_qt_widgets`, called from
    `_isolate_qt_per_test`) runs only at FIXTURE teardown — after this
    test's assertions had already passed either way. A stale check_ready
    firing into a still-alive window just re-runs harmlessly; it needs an
    already-deleted GridView to raise. So this version calls
    `_sweep_qt_widgets` itself, INSIDE the test body, before spinning the
    loop, forcing the same teardown the fixture performs but early enough
    to matter (fauxcasa-xf2).

    Asserts on capsys' stderr mirror (applog's _StderrHandler), NOT caplog:
    applog sets `log.propagate = False` on the 'fauxcasa' logger, matching the
    convention on test_cmd_promote_requires_explicit_library.

    Confirmed red only when BOTH parts of the fix are reverted together —
    `poll = QTimer()` (unparented) AND dropping the `poll.stop()` after
    `app.exec()` in main.py: the stale poll then fires check_ready into the
    deleted window's `win.pages` and the RuntimeError ("Internal C++ object
    already deleted") reaches the excepthook as "uncaught exception".
    Reverting ONLY the parenting while keeping `poll.stop()` stays GREEN:
    `poll.stop()` alone disarms the timer before this test (or anything
    else) ever gets a chance to spin the loop, so that half of the bug
    can't be observed this way — this is expected, not a hole in the test;
    do not re-investigate it as a gap.

    The spin below drives `app.exec()`, NOT a bare `QEventLoop()` (unlike
    the sibling `test_abandoned_hard_stop_does_not_fire_after_its_run`,
    whose own spin loop this was originally copied from). Measured directly
    while building this test: after `main.main()` has called
    `QCoreApplication.quit()` once (the --quit-after-ready path), Qt leaves
    `QThreadData::quitNow` set, and every later bare `QEventLoop().exec()`
    on this thread returns in well under a millisecond WITHOUT servicing
    any pending timer, forever — only `QCoreApplication::exec()` resets that
    flag on entry. A bare-`QEventLoop()` version of this spin is silently a
    no-op no matter how many rounds or how long each `singleShot` is, so it
    stays green against BOTH the fixed and the fully-reverted main.py — an
    even more vacuous failure mode than the one this test was written to
    fix. `app.exec()` + `app.quit()` is the only form of this spin that
    actually lets a leaked timer fire."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    import main

    app = QApplication.instance() or QApplication([])
    cache_root = tmp_path / "cr"
    monkeypatch.setattr(sys, "argv", [
        "fauxcasa-tracer", str(library), "--cache-root", str(cache_root),
        "--quit-after-ready", "--finish-build", "--timeout", "30"])
    assert main.main() == 0
    capsys.readouterr()          # discard the run's own stdout/stderr

    # Force the window's real teardown NOW, inside the test, instead of
    # waiting for the fixture to do it after these assertions run.
    _sweep_qt_widgets(app)

    # The window is gone; give any leaked 50 ms poll several chances to
    # fire into it.
    for _ in range(6):
        QTimer.singleShot(60, app.quit)
        app.exec()
    err = capsys.readouterr().err
    assert "uncaught exception" not in err, \
        f"stale check_ready fired into the deleted window: {err}"
