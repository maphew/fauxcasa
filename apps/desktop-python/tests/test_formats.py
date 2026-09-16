"""Tests for thumbcache.py stills format matrix (TGA, PSD, non-JPEG regression) and HEIC/HEIF decode via pi-heif.

Split from test_tracer.py (fauxcasa-l09); originally lines 12747-13641 of the monolith."""

from __future__ import annotations

import json
import struct
from pathlib import Path
import pytest
import thumbcache
from catalog import (
    ScanFilter,
    load_catalog,
    save_catalog,
    scan_library,
    walk_library,
)
from tracer_helpers import (
    REPO,
    _HEIC_EXIF6,
    _HEIC_FIXTURE,
    _HEIC_ROT6,
    _HEIC_XMP6,
    _assert_heic_quadrants,
    _assert_heic_rotated_quadrants,
    _heic_bytes,
    _load_mtc,
    _make_psd,
    _make_tga,
    _offscreen_app,
    _patch_sandboxed_service,
    _thumb_qimage,
    make_jpeg,
)


# ===========================================================================
# ---- stills format matrix (fauxcasa-v46.4): TGA, PSD Pillow fallback,
# ---- File Types panel, non-JPEG regression fixtures
# ===========================================================================


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


# ---- HEIC/HEIF (fauxcasa-y5b): pi-heif via the same Pillow fallback ------


def test_heic_extensions_in_both_walkers(tmp_path: Path) -> None:
    """.heic/.heif join the stills matrix (fauxcasa-y5b) in BOTH EXTS
    sets, in lockstep, and the walk picks them up case-insensitively —
    the same walk-parity contract TGA/PSD proved in
    test_stills_extensions_in_both_walkers."""
    import catalog

    mtc = _load_mtc()
    assert {".heic", ".heif"} <= catalog.EXTS
    assert catalog.EXTS == mtc.EXTS               # the whole lockstep set

    root = tmp_path / "lib"
    root.mkdir()
    for name in ("a.HEIC", "b.heif", "c.Heic", "d.HEIF"):
        (root / name).write_bytes(b"stub")        # walk checks suffix only
    make_jpeg(root / "e.jpg")
    walked = [p.name for p in walk_library(root)]
    assert sorted(walked) == ["a.HEIC", "b.heif", "c.Heic", "d.HEIF", "e.jpg"]
    script_walk = sorted(p for p in root.rglob("*")
                         if p.suffix.lower() in mtc.EXTS and p.is_file())
    assert [p.name for p in script_walk] == walked


def test_heic_decodes_through_real_indexer(
        tmp_path: Path, monkeypatch) -> None:
    """HEIC through the REAL _index_one path: Qt has no HEIF plugin, so
    the Pillow fallback (pillowload, via pi-heif's opener) must decode
    all four quadrants color-correct at native size — while a truncated,
    undecodable HEIC yields the standard zero-length error tile and never
    sinks the batch. decodefacade.get_service() is patched to a stub
    that CLAIMS sandboxed and answers null for .heic paths specifically
    (_patch_sandboxed_service), so this only stays green because the
    .heic/.heif elif in _index_one intercepts before that stub's
    decode() ever runs — delete the elif and "good.heic" error-tiles."""
    _patch_sandboxed_service(monkeypatch)
    root = tmp_path / "lib"
    root.mkdir()
    (root / "good.heic").write_bytes(_heic_bytes())
    (root / "broken.heic").write_bytes(_heic_bytes(broken=True))
    make_jpeg(root / "ok.jpg")
    cat = scan_library(root)
    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    ent = {rel: e for rel, e in zip(cache.files, cache.entries)}
    _o, length, w, h = ent["good.heic"]
    assert length > 0 and (w, h) == (96, 64)       # never upscaled (< 256px)
    idx = cache.files.index("good.heic")
    _assert_heic_quadrants(
        lambda x, y: _thumb_qimage(cache, idx).pixelColor(x, y))
    assert ent["broken.heic"][1] == 0              # error tile, by design
    assert ent["ok.jpg"][1] > 0                    # neighbors unharmed


def test_viewer_load_original_heic(tmp_path: Path, monkeypatch) -> None:
    """The viewer's full-decode path (shared by the slideshow prefetch):
    the fixture renders via the Pillow/pi-heif fallback, the Picasa
    rotate= turns compose on top exactly like any format, and a
    truncated HEIC returns a null QImage (fail-soft contract, mirrors
    test_viewer_load_original_psd). Same decodefacade stub as the
    indexer test above, for the same reason: proves the viewer's
    .heic/.heif elif intercepts before load_original_oriented would
    ever consult the (stubbed, HEIC-answers-null) sandboxed branch."""
    _offscreen_app()
    _patch_sandboxed_service(monkeypatch)
    from viewer import load_original

    good = tmp_path / "g.heic"
    good.write_bytes(_heic_bytes())
    img = load_original(str(good), 0)
    assert (img.width(), img.height()) == (96, 64)
    _assert_heic_quadrants(img.pixelColor)
    img = load_original(str(good), 1)              # rotate= composes on top
    assert (img.width(), img.height()) == (64, 96)

    broken = tmp_path / "b.heic"
    broken.write_bytes(_heic_bytes(broken=True))
    assert load_original(str(broken), 0).isNull()


def test_heic_container_rotation_decodes_upright_exif_reports_raw_tag(
        tmp_path: Path) -> None:
    """The "Apple shape" (fixtures/heic-smoke/synthetic-rot6.heic.txt):
    a REAL `irot` container transform (angle=3, 270 deg anticlockwise =
    90 deg clockwise) AND a real EXIF Orientation=6 tag, together, on
    the same file -- exactly what an iPhone HEIC carries. This is the
    load-bearing proof for pillowload.py's orientation docstring: HEIC
    uprightness comes from libheif applying the container's irot/imir
    transform DURING DECODE, never from pillow_qimage's exif_transpose
    call (pi-heif's opener resets EXIF Orientation to 1 before
    exif_transpose ever runs, so exif_transpose is always a no-op for
    HEIC) -- yet metareader.read_orientation (exiv2, reading the file's
    raw bytes directly, never through pi-heif) still reports the RAW
    tag, 6, unaffected by pi-heif's in-process neutering. The stored
    (coded-plane) frame is the same 96x64 quadrant layout as
    synthetic.heic; the irot swaps it to a displayed 64x96 with the
    quadrants rotated 270 deg anticlockwise from the stored layout:
    displayed TL = stored BL (blue), TR = stored TL (red), BL = stored
    BR (yellow), BR = stored TR (green)."""
    _offscreen_app()
    from metareader import read_orientation
    from viewer import load_original_oriented

    fixture = REPO / "fixtures" / "heic-smoke" / "synthetic-rot6.heic"
    data = fixture.read_bytes()
    assert read_orientation(data) == 6           # exiv2 reads the raw tag

    path = tmp_path / "rot6.heic"
    path.write_bytes(data)
    img, orientation = load_original_oriented(str(path), rotate=0)
    assert orientation == 6                      # reported, not neutralised
    assert (img.width(), img.height()) == (64, 96)  # the irot ran
    tl, tr = img.pixelColor(5, 5), img.pixelColor(59, 5)
    bl, br = img.pixelColor(5, 91), img.pixelColor(59, 91)
    assert abs(tl.red() - 40) < 30 and abs(tl.blue() - 220) < 30   # blue
    assert abs(tr.red() - 220) < 30 and abs(tr.green() - 40) < 30  # red
    assert abs(bl.red() - 230) < 30 and abs(bl.green() - 210) < 30  # yellow
    assert abs(br.red() - 40) < 30 and abs(br.green() - 200) < 30  # green


# ---- HEIC EXIF-only rotation (fauxcasa-zq9) -------------------------------


def test_heif_container_transform_sniff() -> None:
    """heif_container_transform is the seam that decides whether
    pillow_qimage may apply pi-heif's original_orientation: True for the
    Apple shape (irot bound to the primary item), False for the
    EXIF-only and untagged fixtures (nothing bound rotates), None for
    anything it cannot parse -- and None must NEVER become an apply,
    because "can't tell" plus EXIF would double-rotate a both-present
    file whose ipma we merely failed to read."""
    from pillowload import heif_container_transform

    rot6 = _HEIC_ROT6.read_bytes()
    exif6 = _HEIC_EXIF6.read_bytes()
    plain = _HEIC_FIXTURE.read_bytes()
    assert heif_container_transform(rot6) is True
    assert heif_container_transform(exif6) is False
    assert heif_container_transform(plain) is False
    # The two fixtures differ ONLY in the one EXIF SHORT the generator
    # patched (scripts/make-heic-exif-only-fixture.py): same container.
    assert len(exif6) == len(plain)
    assert sum(a != b for a, b in zip(exif6, plain)) == 1
    # Unparseable -> None, never a raise, never False.
    assert heif_container_transform(b"") is None
    assert heif_container_transform(b"\x00" * 64) is None
    assert heif_container_transform(rot6[:300]) is None    # meta truncated
    assert heif_container_transform(rot6[:40]) is None     # ftyp only
    # A corrupted ipma (primary item's property list gone) on the rot6
    # bytes must read as None, not False: the irot is still in ipco but
    # we can no longer prove it is bound, so the caller must not apply
    # EXIF on top of whatever libheif did.
    idx = rot6.find(b"ipma")
    broken = bytearray(rot6)
    broken[idx - 4:idx] = struct.pack(">I", 8)              # ipma size -> empty
    assert heif_container_transform(bytes(broken)) is None
    # A zero-length irot payload (box size 8) must not read the NEXT
    # box's first byte as its angle: None, not a verdict.
    idx = rot6.find(b"irot")
    broken = bytearray(rot6)
    broken[idx - 4:idx] = struct.pack(">I", 8)
    assert heif_container_transform(bytes(broken)) is None
    # XMP-only fixture: same container shape as synthetic.heic -> False.
    assert heif_container_transform(_HEIC_XMP6.read_bytes()) is False


def test_heic_manual_orientation_decision() -> None:
    """heic_manual_orientation is the ONE value pillow_qimage rotates a
    HEIC by, and it must equal metareader.read_orientation(data) for
    that file or be 1 -- the viewer reports read_orientation as the
    file's orientation and maps stored-frame crop/face rects through it,
    so pixels rotated by anything else would desync from those rects.
      exif6: no transform, EXIF 6            -> 6 (== exiv2)
      rot6:  irot bound, EXIF 6, pi-heif 6   -> 1 (libheif did it)
      xmp6:  no transform, XMP 6, EXIF absent -> 1 (pi-heif says 6; exiv2
             says 1; we side with exiv2, on purpose -- review round 1)
      garbage                                 -> 1 (sniff None)"""
    from metareader import read_orientation
    from pillowload import heic_manual_orientation

    for path, want in ((_HEIC_EXIF6, 6), (_HEIC_ROT6, 1), (_HEIC_XMP6, 1)):
        data = path.read_bytes()
        got = heic_manual_orientation(data, 6)   # pi-heif reports 6 for all
        assert got == want, path.name
        assert got in (1, read_orientation(data)), path.name
    assert heic_manual_orientation(b"", 6) == 1
    assert heic_manual_orientation(b"\x00" * 64, 8) == 1
    # The pi-heif value is a cross-check only: passing None or a
    # disagreeing number changes nothing about the decision.
    assert heic_manual_orientation(_HEIC_EXIF6.read_bytes(), None) == 6
    assert heic_manual_orientation(_HEIC_EXIF6.read_bytes(), 3) == 6


def test_heic_xmp_only_orientation_left_as_stored(
        tmp_path: Path, monkeypatch) -> None:
    """The XMP-only shape (synthetic-xmp6.heic.txt): XMP tiff:Orientation
    =6, NO EXIF, no irot/imir. pi-heif's misc._get_orientation falls
    through to XMP, so info["original_orientation"] is 6 -- but exiv2's
    EXIF-only read_orientation is 1, and that is what the viewer
    reports. Applying pi-heif's 6 would rotate the pixels under a
    reported orientation of 1 (review round 1 should-fix). So every
    path leaves this file AS STORED: 96x64, quadrants in stored
    positions, and the viewer's (pixels, orientation) pair agrees with
    itself. Indexer + viewer + InProcessTransport, same sandboxed stub
    as the y5b tests."""
    _offscreen_app()
    _patch_sandboxed_service(monkeypatch)
    from io import BytesIO
    from PIL import Image
    import decodefacade
    from metareader import read_orientation
    from pillowload import pillow_qimage
    from viewer import load_original, load_original_oriented

    data = _HEIC_XMP6.read_bytes()
    with Image.open(BytesIO(data)) as im:
        assert im.info.get("original_orientation") == 6   # XMP-sourced
        assert im.getexif().get(0x0112) is None            # no EXIF at all
    assert read_orientation(data) == 1
    img = pillow_qimage(data)
    assert (img.width(), img.height()) == (96, 64)
    _assert_heic_quadrants(img.pixelColor)

    root = tmp_path / "lib"
    root.mkdir()
    (root / "xmp6.heic").write_bytes(data)
    cat = scan_library(root)
    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    _o, length, w, h = dict(zip(cache.files, cache.entries))["xmp6.heic"]
    assert length > 0 and (w, h) == (96, 64)
    _assert_heic_quadrants(
        lambda x, y: _thumb_qimage(cache, 0).pixelColor(x, y))
    img, orientation = load_original_oriented(str(root / "xmp6.heic"), rotate=0)
    assert orientation == 1 and (img.width(), img.height()) == (96, 64)
    _assert_heic_quadrants(img.pixelColor)
    _assert_heic_quadrants(load_original(str(root / "xmp6.heic"), 0).pixelColor)
    _assert_heic_quadrants(decodefacade.InProcessTransport().decode(
        str(root / "xmp6.heic"), route="still", edge=0).pixelColor)


def test_heic_exif_only_rotation_decodes_upright(tmp_path: Path) -> None:
    """(a) The EXIF-only shape (synthetic-exif6.heic.txt): a real EXIF
    Orientation=6 and NO irot/imir. libheif has nothing to rotate and
    pi-heif has reset the live tag to 1, so before fauxcasa-zq9 this
    came out 96x64 sideways. pillow_qimage now applies pi-heif's
    info["original_orientation"] because heif_container_transform says
    definitively False -- the result is the SAME 64x96 layout the irot
    fixture produces. Also covers the viewer path: load_original_oriented
    still reports the raw exiv2 tag (6) alongside the upright pixels,
    exactly as for the rot6 fixture, so crop/face math is unchanged."""
    _offscreen_app()
    from metareader import read_orientation
    from pillowload import pillow_qimage
    from viewer import load_original_oriented

    data = _HEIC_EXIF6.read_bytes()
    assert read_orientation(data) == 6
    _assert_heic_rotated_quadrants(pillow_qimage(data))
    thumb = pillow_qimage(data, 32)                    # indexer-style bound
    assert (thumb.width(), thumb.height()) == (21, 32)

    path = tmp_path / "exif6.heic"
    path.write_bytes(data)
    img, orientation = load_original_oriented(str(path), rotate=0)
    assert orientation == 6
    _assert_heic_rotated_quadrants(img)


def test_heic_both_present_not_double_rotated() -> None:
    """(b) The Apple shape (irot AND EXIF=6) must be untouched by the zq9
    apply: pi-heif still reports original_orientation=6 for it, so the
    ONLY thing standing between this file and a 180-degree double turn
    is heif_container_transform returning True. Pin the pixels, not just
    the size (a second 90 would keep 64x96 and only move the colors)."""
    _offscreen_app()
    from io import BytesIO
    from pillowload import pillow_qimage
    from PIL import Image

    data = _HEIC_ROT6.read_bytes()
    with Image.open(BytesIO(data)) as im:
        assert im.info.get("original_orientation") == 6   # the trap is live
        assert im.getexif().get(0x0112) == 1              # neutered, as documented
    _assert_heic_rotated_quadrants(pillow_qimage(data))
    _assert_heic_rotated_quadrants(pillow_qimage(_HEIC_EXIF6.read_bytes()))


def test_heic_orientation_parity_indexer_viewer_facade(
        tmp_path: Path, monkeypatch) -> None:
    """(c) Thumbnail path (thumbcache._index_one), viewer path
    (viewer.load_original) and the facade's InProcessTransport must agree,
    per file AND across the two rotated fixtures: identical dims, and the
    displayed quadrant colors of the EXIF-only file match the irot file
    pixel-for-pixel within lossy tolerance on every path. Same sandboxed
    stub as the y5b tests so the HEIC pre-route is what decoded here."""
    _offscreen_app()
    _patch_sandboxed_service(monkeypatch)
    import decodefacade
    from viewer import load_original

    root = tmp_path / "lib"
    root.mkdir()
    (root / "rot6.heic").write_bytes(_HEIC_ROT6.read_bytes())
    (root / "exif6.heic").write_bytes(_HEIC_EXIF6.read_bytes())
    cat = scan_library(root)
    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    ent = {rel: e for rel, e in zip(cache.files, cache.entries)}
    for name in ("rot6.heic", "exif6.heic"):
        _o, length, w, h = ent[name]
        assert length > 0 and (w, h) == (64, 96), name   # thumb: upright dims
        _assert_heic_rotated_quadrants(
            _thumb_qimage(cache, cache.files.index(name)))
        _assert_heic_rotated_quadrants(load_original(str(root / name), 0))
        # The facade's in-process still route: Qt yields null for HEIC,
        # the pillow_qimage fallback runs -- same orientation contract.
        transport = decodefacade.InProcessTransport()
        _assert_heic_rotated_quadrants(
            transport.decode(str(root / name), route="still", edge=0))


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
    lib_key = str(lib.resolve())
    base = thumbcache.cache_dir_for(
        lib_key, croot, sf.cache_key() + exts_cache_key(set()))
    off = thumbcache.cache_dir_for(
        lib_key, croot, sf.cache_key() + exts_cache_key({".tga"}))
    back = thumbcache.cache_dir_for(
        lib_key, croot, sf.cache_key() + exts_cache_key(set()))
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
    lib_key = str(lib.resolve())
    base_dir = thumbcache.cache_dir_for(
        lib_key, croot, sf.cache_key() + exts_cache_key(set()))
    cat = scan_library(lib)
    thumbcache.build_cache(cat, base_dir)
    save_catalog(cat, base_dir / "catalog.json")

    excluded = {".tga"}
    off_dir = thumbcache.cache_dir_for(
        lib_key, croot, sf.cache_key() + exts_cache_key(excluded))
    assert off_dir != base_dir
    assert not (off_dir / "thumbs.fcache").exists()   # cold, its own dir
    cat_off = scan_library(lib, exts=effective_exts(excluded))
    assert [p.rel for p in cat_off.photos] == ["a.jpg"]
    with pytest.raises(thumbcache.CacheError):
        thumbcache.bind(
            thumbcache.load_cache(base_dir / "thumbs.fcache"), cat_off)

    on_dir = thumbcache.cache_dir_for(
        lib_key, croot, sf.cache_key() + exts_cache_key(set()))
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


def test_pillowload_import_registers_heif_opener() -> None:
    """Importing pillowload registers pi-heif's opener at IMPORT time,
    not lazily from pillow_qimage() (fauxcasa-y5b review): a decode
    worker thread calling register_heif_opener() for the first time
    while ANOTHER worker thread is mid-Image.open() on an unrelated
    photo can KeyError on Pillow's own OPEN dict (populated a moment
    AFTER the format id is appended to the probe order) -- registering
    once, at import, before any worker thread exists, closes that
    window. Skips if pi_heif truly is not installed in this environment
    (mirrors pillowload's own fail-soft contract; this test asserts the
    registration SIDE EFFECT, not that pi-heif is always present)."""
    import importlib

    import pillowload

    importlib.reload(pillowload)  # re-run the module-level call fresh
    try:
        import pi_heif  # noqa: F401
    except ImportError:
        pytest.skip("pi_heif not installed in this environment")
    from PIL import Image

    assert "HEIF" in Image.OPEN
    assert pillowload._heif_registered is True
    assert pillowload._heif_import_failed is False


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


def test_index_one_content_probes_mis_extensioned_still(
        tmp_path: Path) -> None:
    """fauxcasa-tlv regression: _index_one's decode-side QImageReader for
    an ordinary still is now PATH-constructed (QImageReader(str(path)))
    instead of a QBuffer with an explicit suffix-derived format (the
    46b0bab revision reverted here after that hunk was attributed to a
    rare native access violation). A path-constructed reader restores
    EXACT pre-fix behavior: Qt probes the file's CONTENT to pick a
    plugin, so a PNG saved with a .jpg extension still decodes correctly
    (content wins over suffix) rather than failing the (wrong) jpeg
    handler and falling through to the Pillow fallback."""
    from PIL import Image

    lib = tmp_path / "lib"
    lib.mkdir()
    Image.new("RGB", (64, 48), (40, 220, 90)).save(
        lib / "disguised.jpg", "PNG")   # PNG bytes, .jpg extension

    cat = scan_library(lib)
    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    (_o, length, w, h), = cache.entries
    assert length > 0 and (w, h) == (64, 48)     # decoded, not error-tiled
    px = _thumb_qimage(cache, 0).pixelColor(32, 24)
    assert px.green() > 170 and px.red() < 90 and px.blue() < 140
