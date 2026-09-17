"""Tests for library.py multi-root core (library.json, root-id minting, resolve_open_path, .fauxcasa exclusion) plus catalog/cache/offline-tolerance plumbing.

Split from test_tracer.py (fauxcasa-l09); originally lines 14365-16908 of the monolith."""

from __future__ import annotations

import json
import os
import re
import struct
import sys
from pathlib import Path
import pytest
import library as libmod
import thumbcache
import volumes as volmod
from catalog import (
    load_catalog,
    reconcile_walk,
    save_catalog,
    scan_library,
    walk_library,
)
from tracer_helpers import (
    REPO,
    _bound_cache,
    _load_mtc,
    _marked_stored_image,
    _offscreen_app,
    _quadrant_jpeg,
    _raw_catalog,
    _raw_photo_rows,
    _spin,
    _write_faces_contacts_xml,
    _write_raw_catalog,
    make_jpeg,
)


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


def test_thumb_tiny_crop_of_large_source_decodes_correctly(
        tmp_path: Path, monkeypatch) -> None:
    """fauxcasa-7aj.1 (PR #58 decode-cost nit): when the crop's kept region
    ALREADY fits the top-level box (no downscale needed), _index_one bounds
    the decode with QImageReader.setClipRect (an ROI decode) instead of
    reading the whole source at full resolution and cropping after —
    exercised here on a 1024px source cropped down to a 128x128 corner (well
    under THUMB_EDGE=256). This pins CORRECTNESS of that ROI path: the wrong
    quadrant, an off-by-one clip rect, or a double-crop (clip_applied not
    suppressing the later crop_qimage_upright call) would all fail the
    pixel/dimension assertions below.

    hi2 item 8: correctness alone doesn't prove the ROI *path* actually
    ran — a correct-looking thumb could in principle come from the
    fallback full-decode-then-crop route if setClipRect silently became a
    no-op. A spy on QImageReader.setClipRect (patched at the class level,
    since thumbcache imports QImageReader locally) closes that gap by
    asserting the clip call fires exactly once with the expected pixel
    box, in addition to the pixel/dimension pins below."""
    _offscreen_app()
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QImageReader

    clip_calls: list[QRect] = []
    orig_set_clip_rect = QImageReader.setClipRect

    def spy_set_clip_rect(self, rect, *a, **kw):
        clip_calls.append(QRect(rect))
        return orig_set_clip_rect(self, rect, *a, **kw)

    monkeypatch.setattr(QImageReader, "setClipRect", spy_set_clip_rect)

    root = tmp_path / "lib"
    _quadrant_jpeg(root / "f" / "cropped.jpg", edge=1024)
    # fraction rect (0.5, 0.0, 0.625, 0.125) -> pixel box (512, 0, 128, 128)
    # on the 1024px source: a 128x128 corner entirely inside the TR (green)
    # quadrant (x in [512,1024), y in [0,512)), and already <= THUMB_EDGE.
    (root / "f" / ".picasa.ini").write_text(
        "[cropped.jpg]\r\ncrop=rect64(80000000a0002000)\r\n")
    cat, cache = _bound_cache(tmp_path, root)
    ci = next(i for i, p in enumerate(cat.photos) if p.name == "cropped.jpg")
    assert cat.photos[ci].crop == (0.5, 0.0, 0.625, 0.125)
    offset, length, w, h = cache.entries[ci]
    assert length > 0
    # THE spy assertion (hi2 item 8): the ROI decode path actually fired,
    # with exactly the pixel box the crop maps to — not just a
    # correct-looking output that could have come from a fallback route.
    assert clip_calls == [QRect(512, 0, 128, 128)]
    with open(cache.path, "rb") as f:
        f.seek(offset)
        from PySide6.QtGui import QImage
        img = QImage.fromData(f.read(length), "JPEG")
    assert not img.isNull()
    # Never upscaled: the crop box was already 128x128, under THUMB_EDGE.
    assert (img.width(), img.height()) == (128, 128) == (w, h)
    for fx, fy in ((0.1, 0.1), (0.5, 0.5), (0.9, 0.9)):
        c = img.pixelColor(int(fx * img.width()), int(fy * img.height()))
        assert c.green() > 150 and c.red() < 90 and c.blue() < 90, (fx, fy)


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
    # _index_one's first arg is the ALREADY-RESOLVED absolute path
    # (multiroot .b, design §6 — Catalog.abs() is the choke point), not
    # the library root: callers resolve it before dispatch.
    _idx, blobs, *_rest, primary = thumbcache._index_one(
        cat.abs(cat.photos[0]), cat.photos[0], 0, [thumbcache.THUMB_EDGE])
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
    """The M1 ingest-parity gate (spec §9 clause 2) end-to-end: zero
    ingest LOSS across every ingested class, always (the gate FAILS on
    loss/excess/ratchet regardless of expected-missing count — see
    check-ingest-parity.py's run_gate). fauxcasa-cam.20's db3 caption
    gap-fill returns the table to fully ingested. Runs the real script in
    its own uv env (exactly CI's tests.yml job); skips when uv is absent."""
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
    assert "PASS: zero ingest loss across all" in proc.stdout
    assert "fully ingested (0 expected-missing)" in proc.stdout


# ===========================================================================
# ---- multi-root library model core (fauxcasa-ed5.7.1): library.json,
# ---- root-id minting, resolve_open_path, .fauxcasa walk exclusion
# ===========================================================================


def test_mint_root_id_shape_and_uniqueness() -> None:
    """mint_root_id returns 8-char lowercase hex; 200 mints are all unique
    and never path-derived. mint_library_id returns a parseable uuid4."""
    import uuid

    ids = [libmod.mint_root_id() for _ in range(200)]
    for rid in ids:
        assert re.fullmatch(r"[0-9a-f]{8}", rid), f"bad shape: {rid!r}"
    assert len(set(ids)) == 200, "collision among 200 mints"

    lid1 = libmod.mint_library_id()
    lid2 = libmod.mint_library_id()
    uuid.UUID(lid1)   # must parse without raising
    uuid.UUID(lid2)
    assert lid1 != lid2


def test_library_json_roundtrip(tmp_path: Path) -> None:
    """save_library + load_library is a lossless roundtrip; atomic replace
    works on a second write; no .tmp files left behind."""
    home = tmp_path / "home"
    root_a = tmp_path / "photos"
    root_b = tmp_path / "scans"
    root_a.mkdir()
    root_b.mkdir()

    id_a = libmod.mint_root_id()
    id_b = libmod.mint_root_id()
    library_id = libmod.mint_library_id()

    cfg = libmod.LibraryConfig(
        library_id=library_id,
        name="Test Library",
        roots=[
            libmod.LibraryRoot(id=id_a, path=root_a, label="Photos"),
            libmod.LibraryRoot(id=id_b, path=root_b, label="Scans"),
        ],
        home=home,
    )

    libmod.save_library(cfg)

    json_path = home / ".fauxcasa" / "library.json"
    assert json_path.is_file()

    raw = json.loads(json_path.read_text(encoding="utf-8"))
    assert raw["format"] == 1
    assert raw["library_id"] == library_id
    assert raw["roots"][0]["path"] == root_a.as_posix()
    assert raw["roots"][1]["path"] == root_b.as_posix()

    loaded = libmod.load_library(home)
    assert loaded is not None
    assert loaded.library_id == cfg.library_id
    assert loaded.name == cfg.name
    assert not loaded.is_legacy
    assert len(loaded.roots) == 2
    assert loaded.roots[0].id == id_a
    assert loaded.roots[0].label == "Photos"
    assert loaded.roots[1].id == id_b
    assert loaded.roots[1].label == "Scans"

    # Second save: atomic replace over existing file still works
    libmod.save_library(cfg)
    loaded2 = libmod.load_library(home)
    assert loaded2 is not None
    assert loaded2.library_id == library_id

    # No leftover .tmp files
    tmp_files = list((home / ".fauxcasa").glob("*.tmp"))
    assert tmp_files == [], f"leftover tmp files: {tmp_files}"


def test_load_library_fail_soft(tmp_path: Path) -> None:
    """load_library returns None for all invalid inputs."""
    home = tmp_path / "home"
    fauxcasa_dir = home / ".fauxcasa"
    fauxcasa_dir.mkdir(parents=True)

    def write_json(data) -> None:
        (fauxcasa_dir / "library.json").write_text(
            json.dumps(data), encoding="utf-8")

    # Missing .fauxcasa/ entirely
    assert libmod.load_library(tmp_path / "nonexistent") is None

    # Garbage text
    (fauxcasa_dir / "library.json").write_text("not json!!", encoding="utf-8")
    assert libmod.load_library(home) is None

    # Wrong format version
    write_json({"format": 2, "library_id": "abc", "roots": []})
    assert libmod.load_library(home) is None

    # Empty roots list
    write_json({"format": 1, "library_id": "test-id", "name": "", "roots": []})
    assert libmod.load_library(home) is None

    # Duplicate root ids
    ra = tmp_path / "a"
    rb = tmp_path / "b"
    ra.mkdir(); rb.mkdir()
    write_json({
        "format": 1, "library_id": "test-id", "name": "",
        "roots": [
            {"id": "a1b2c3d4", "path": ra.as_posix(),
             "volume_uuid": None, "vol_rel": None, "label": ""},
            {"id": "a1b2c3d4", "path": rb.as_posix(),
             "volume_uuid": None, "vol_rel": None, "label": ""},
        ]
    })
    assert libmod.load_library(home) is None

    # Root with reserved id ""
    write_json({
        "format": 1, "library_id": "test-id", "name": "",
        "roots": [
            {"id": "", "path": ra.as_posix(),
             "volume_uuid": None, "vol_rel": None, "label": ""},
        ]
    })
    assert libmod.load_library(home) is None

    # Nested roots (tmp/a and tmp/a/sub)
    ra_sub = ra / "sub"
    ra_sub.mkdir()
    write_json({
        "format": 1, "library_id": "test-id", "name": "",
        "roots": [
            {"id": "a1b2c3d4", "path": ra.as_posix(),
             "volume_uuid": None, "vol_rel": None, "label": ""},
            {"id": "5e6f7a8b", "path": ra_sub.as_posix(),
             "volume_uuid": None, "vol_rel": None, "label": ""},
        ]
    })
    assert libmod.load_library(home) is None


def test_resolve_open_path(tmp_path: Path) -> None:
    """resolve_open_path: explicit cfg for a valid library.json, legacy
    fallback for a bare dir or a corrupt library.json."""
    # (a) dir with a valid library.json -> explicit config
    home_a = tmp_path / "home_a"
    root_a = tmp_path / "root_a"
    root_a.mkdir()
    lid = libmod.mint_library_id()
    cfg = libmod.LibraryConfig(
        library_id=lid, name="Test Library",
        roots=[libmod.LibraryRoot(
            id=libmod.mint_root_id(), path=root_a, label="Root A")],
        home=home_a,
    )
    libmod.save_library(cfg)
    result = libmod.resolve_open_path(home_a)
    assert not result.is_legacy
    assert result.library_id == lid

    # (b) bare dir -> degenerate legacy
    bare = tmp_path / "bare"
    bare.mkdir()
    result_b = libmod.resolve_open_path(bare)
    assert result_b.is_legacy
    assert result_b.home is None
    assert len(result_b.roots) == 1
    assert result_b.roots[0].id == libmod.LEGACY_ROOT_ID
    assert result_b.roots[0].path == bare.resolve()

    # (c) dir with corrupt library.json -> legacy fallback
    corrupt = tmp_path / "corrupt"
    corrupt_fauxcasa = corrupt / ".fauxcasa"
    corrupt_fauxcasa.mkdir(parents=True)
    (corrupt_fauxcasa / "library.json").write_text("{ bad json", encoding="utf-8")
    result_c = libmod.resolve_open_path(corrupt)
    assert result_c.is_legacy


def test_root_marker_roundtrip(tmp_path: Path) -> None:
    """write_root_marker / read_root_marker: valid id round-trips; absent
    and garbage content return None; nonexistent parent returns False."""
    d = tmp_path / "root"
    d.mkdir()

    # Successful write + read
    assert libmod.write_root_marker(d, "a1b2c3d4") is True
    assert libmod.read_root_marker(d) == "a1b2c3d4"

    # Absent marker -> None
    d2 = tmp_path / "nomarker"
    d2.mkdir()
    assert libmod.read_root_marker(d2) is None

    # Garbage content -> None
    d3 = tmp_path / "bad"
    d3.mkdir()
    (d3 / libmod.ROOT_MARKER).write_text("not an id", encoding="utf-8")
    assert libmod.read_root_marker(d3) is None

    # Write to a nonexistent parent dir -> False, no exception
    nodir = tmp_path / "does_not_exist" / "also_not_here"
    result = libmod.write_root_marker(nodir, "a1b2c3d4")
    assert result is False


def test_walk_excludes_fauxcasa_dir_and_marker(tmp_path: Path) -> None:
    """walk_library excludes .fauxcasa/ content and .fauxcasa-root at any
    depth; .picasaoriginals (dot-stash) is still walked; scan_library
    photo count agrees."""
    root = tmp_path / "root"

    # Two normal jpegs
    make_jpeg(root / "a.jpg")
    make_jpeg(root / "sub" / "b.jpg")

    # .fauxcasa library state — extension-bearing file (proved real behavior)
    (root / ".fauxcasa").mkdir()
    (root / ".fauxcasa" / "library.json").write_text("{}", encoding="utf-8")
    (root / ".fauxcasa" / "thumbs").mkdir()
    make_jpeg(root / ".fauxcasa" / "thumbs" / "leak.jpg")

    # Nested .fauxcasa at a sub-level
    (root / "f" / ".fauxcasa").mkdir(parents=True)
    make_jpeg(root / "f" / ".fauxcasa" / "deep.jpg")

    # The root marker file
    (root / libmod.ROOT_MARKER).write_text("a1b2c3d4", encoding="utf-8")

    # .picasaoriginals dot-stash — should still be walked
    make_jpeg(root / "f" / ".picasaoriginals" / "orig.jpg")

    result = walk_library(root)
    result_rels = [p.relative_to(root).as_posix() for p in result]

    assert "a.jpg" in result_rels
    assert "sub/b.jpg" in result_rels
    assert "f/.picasaoriginals/orig.jpg" in result_rels

    # Nothing under any .fauxcasa dir
    for rel in result_rels:
        assert ".fauxcasa" not in Path(rel).parts, (
            f"found .fauxcasa path in walk: {rel!r}")

    # The marker file is excluded
    assert libmod.ROOT_MARKER not in result_rels

    # Exactly the three expected files
    assert len(result) == 3, f"expected 3 files, got {result_rels!r}"

    # scan_library agrees on photo count
    cat = scan_library(root)
    assert len(cat.photos) == 3


def test_fauxcasa_exclusion_parity_with_make_thumbcache(tmp_path: Path) -> None:
    """Both walk twins exclude .fauxcasa/ and .fauxcasa-root identically;
    constant drift between library.py and the script is caught here."""
    root = tmp_path / "root"

    make_jpeg(root / "a.jpg")
    make_jpeg(root / "sub" / "b.jpg")
    make_jpeg(root / "f" / ".picasaoriginals" / "orig.jpg")

    (root / ".fauxcasa").mkdir()
    make_jpeg(root / ".fauxcasa" / "leak.jpg")
    (root / ".fauxcasa" / "sub").mkdir()
    make_jpeg(root / ".fauxcasa" / "sub" / "deep.jpg")
    (root / libmod.ROOT_MARKER).write_text("a1b2c3d4", encoding="utf-8")

    mtc = _load_mtc()

    # Constant drift guard (the twin-invariant)
    assert mtc.LIBRARY_DIR == libmod.LIBRARY_DIR
    assert mtc.ROOT_MARKER == libmod.ROOT_MARKER

    catalog_result = walk_library(root)
    script_result = mtc.walk_library(root, mtc.EXTS)
    assert catalog_result == script_result


def test_nested_root_rejected(tmp_path: Path) -> None:
    """add_root raises ValueError for descendant, ancestor, and duplicate
    roots; a sibling root succeeds and round-trips through save/load."""
    home = tmp_path / "home"
    a = tmp_path / "a"
    a.mkdir()
    a_sub = a / "sub"
    a_sub.mkdir()
    b = tmp_path / "b"
    b.mkdir()

    lid = libmod.mint_library_id()
    cfg = libmod.LibraryConfig(
        library_id=lid, name="Test Library",
        roots=[libmod.LibraryRoot(
            id="a1b2c3d4", path=a, label="A")],
        home=home,
    )

    # Descendant of existing root
    with pytest.raises(ValueError):
        libmod.add_root(cfg, a_sub)

    # Ancestor of existing root (tmp_path contains a)
    with pytest.raises(ValueError):
        libmod.add_root(cfg, tmp_path)

    # Exact duplicate
    with pytest.raises(ValueError):
        libmod.add_root(cfg, a)

    # Sibling root succeeds
    new_root = libmod.add_root(cfg, b)
    assert re.fullmatch(r"[0-9a-f]{8}", new_root.id)
    assert new_root.id != "a1b2c3d4"
    assert new_root.label == "b"
    assert len(cfg.roots) == 2

    # Round-trip through save/load
    libmod.save_library(cfg)
    loaded = libmod.load_library(home)
    assert loaded is not None
    assert len(loaded.roots) == 2
    assert loaded.roots[1].id == new_root.id


def test_add_root_rejects_home_swallow(tmp_path: Path) -> None:
    """add_root raises ValueError when the candidate would contain the
    library-home; a different sibling root succeeds."""
    h_home = tmp_path / "h" / "home"
    a = tmp_path / "a"
    a.mkdir()
    (tmp_path / "h").mkdir()

    lid = libmod.mint_library_id()
    cfg = libmod.LibraryConfig(
        library_id=lid, name="Test Library",
        roots=[libmod.LibraryRoot(
            id="a1b2c3d4", path=a, label="A")],
        home=h_home,
    )

    # tmp/"h" contains home (h/home); adding it would swallow the home
    with pytest.raises(ValueError):
        libmod.add_root(cfg, tmp_path / "h")

    # A genuinely separate root succeeds
    c = tmp_path / "c"
    c.mkdir()
    new_root = libmod.add_root(cfg, c)
    assert new_root.id != "a1b2c3d4"


def test_add_root_on_legacy_raises(tmp_path: Path) -> None:
    """add_root raises ValueError on a legacy config (promotion is bead .d)."""
    d = tmp_path / "photos"
    d.mkdir()
    cfg = libmod.legacy_config(d)
    assert cfg.is_legacy
    with pytest.raises(ValueError):
        libmod.add_root(cfg, tmp_path / "other")


def test_save_library_on_legacy_raises(tmp_path: Path) -> None:
    """save_library refuses a legacy config (no home; never written to disk)."""
    d = tmp_path / "photos"
    d.mkdir()
    with pytest.raises(ValueError):
        libmod.save_library(libmod.legacy_config(d))


def test_linux_reveal_does_not_block_key_handler(tmp_path: Path,
                                                 monkeypatch) -> None:
    """q6l.21 regression pin: the Linux reveal call must return before the
    D-Bus deadline elapses. The OLD synchronous subprocess.run path would
    hold the key handler for up to _DBUS_TIMEOUT (3 s) if dbus-send hung.
    This test runs a genuinely slow probe (sys.executable sleeping 30 s),
    patches _DBUS_TIMEOUT down to 0.2 s, and asserts that:
      1. reveal_in_file_manager returns immediately (well under 1 s).
      2. The fallback (xdg-open folder) fires LATER via the QTimer deadline
         kill, not inline in the key handler.
      3. The QProcess is reaped (_pending is empty) after settling, so no
         live QProcess leaks into subsequent tests.

    Uses real QProcess/QTimer against sys.executable (not dbus-send), so
    the test is deterministic on both Windows and ubuntu CI legs with no
    session bus needed. Synthetic tmp_path data only."""
    import time

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])

    import locate

    target = tmp_path / "d" / "p.jpg"
    target.parent.mkdir()
    target.write_bytes(b"x")

    # Replace the dbus-send command with a probe that sleeps for 30 s —
    # long enough to be "hung" relative to the 0.2 s deadline we set below.
    monkeypatch.setattr(locate, "_dbus_show_items_cmd",
                        lambda p: [sys.executable, "-c",
                                   "import time; time.sleep(30)"])
    # Shrink the deadline so the test completes in ~0.2 s, not 3 s.
    monkeypatch.setattr(locate, "_DBUS_TIMEOUT", 0.2)
    popens: list[object] = []
    monkeypatch.setattr(locate.subprocess, "Popen",
                        lambda cmd, *a, **k: popens.append(cmd))

    t0 = time.monotonic()
    assert locate.reveal_in_file_manager(target, platform="linux")
    # Key assertion: the call returns IMMEDIATELY, not after _DBUS_TIMEOUT.
    # The old sync path would have held the key handler for the full timeout.
    assert time.monotonic() - t0 < 1.0
    assert popens == []                              # fallback resolves later, not inline

    # Pump the event loop until the QTimer deadline fires, kills the probe,
    # and the settle path launches xdg-open.
    assert _spin(app, lambda: popens)
    assert popens == [["xdg-open", str(target.parent)]]

    # Probe must be fully reaped — no live QProcess leaks into the next test.
    assert _spin(app, lambda: not locate._pending)


# ---- multiroot catalog plumbing (fauxcasa-ed5.7.2, bead .b) ---------------


def test_walk_roots_tags_and_orders_by_root(tmp_path: Path) -> None:
    """walk_roots (design §4): each root's frozen walk_library order,
    independently, chained in cfg.roots list order, every hit tagged with
    that root's id."""
    from catalog import walk_roots

    root_a, root_b = tmp_path / "a", tmp_path / "b"
    make_jpeg(root_a / "z.jpg")
    make_jpeg(root_a / "a.jpg")
    make_jpeg(root_b / "m.jpg")
    id_a, id_b = "aaaaaaaa", "bbbbbbbb"
    cfg = libmod.LibraryConfig(
        library_id=libmod.mint_library_id(), name="",
        roots=[libmod.LibraryRoot(id=id_a, path=root_a),
               libmod.LibraryRoot(id=id_b, path=root_b)],
        home=tmp_path,
    )
    hits = walk_roots(cfg)
    assert [rid for rid, _ in hits] == [id_a, id_a, id_b]
    # per-root order matches walk_library's own frozen (sorted) order
    assert [p.name for rid, p in hits if rid == id_a] == ["a.jpg", "z.jpg"]
    assert [p.name for rid, p in hits if rid == id_b] == ["m.jpg"]


def test_multiroot_two_root_save_load_roundtrip_v13(tmp_path: Path) -> None:
    """A 2-root explicit-library catalog round-trips (design §5/§6): the
    header carries library_id + a roots snapshot, roots[0]'s photo needs
    no "R" (absent means roots[0]), the second root's photo carries an
    explicit "R" (fauxcasa-ed5.5: now at the GROUP level, since grouping
    is by (root_id, folder) — see _group_photo_rows), and
    Catalog.roots/library_id survive the reload.
    (== 15: the newest version-gate test pins the exact value — bumped
    from 14 by fauxcasa-cam.5's XMP-faces merge, a value-completeness
    bump, not a schema change.)"""
    import catalog as catmod
    from catalog import Catalog, Folder, Photo

    assert catmod.CATALOG_VERSION == 15
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    id_a, id_b = "aaaaaaaa", "bbbbbbbb"
    lib_id = libmod.mint_library_id()

    cat = Catalog(
        root=root_a,
        photos=[
            Photo(rel="1.jpg", folder="", name="1.jpg", root_id=id_a),
            Photo(rel="2.jpg", folder="", name="2.jpg", root_id=id_b),
        ],
        folders={
            "": Folder(rel="", title="a", photo_count=1, total_count=1,
                      root_id=id_a),
            id_b: Folder(rel="", title="b", photo_count=1, total_count=1,
                         root_id=id_b),
        },
        albums={},
        roots=[libmod.LibraryRoot(id=id_a, path=root_a),
               libmod.LibraryRoot(id=id_b, path=root_b)],
        library_id=lib_id,
    )
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    raw = _raw_catalog(path)
    assert raw["library_id"] == lib_id
    assert raw["roots"] == [{"id": id_a, "path": str(root_a)},
                            {"id": id_b, "path": str(root_b)}]
    assert "library" not in raw
    rows = {r["r"]: r for r in _raw_photo_rows(raw)}
    assert "R" not in rows["1.jpg"]            # roots[0]: absent means first
    assert rows["2.jpg"]["R"] == id_b
    # the "R" convention is now hoisted to the GROUP, not the row: each
    # photo is alone in its own root's "" folder, so there are 2 groups,
    # and only the second root's group carries "R".
    groups = raw["photos"]
    assert sum("R" in g for g in groups) == 1
    assert next(g for g in groups if "R" in g)["R"] == id_b

    cfg = libmod.LibraryConfig(library_id=lib_id, name="",
                               roots=cat.roots, home=tmp_path)
    loaded = load_catalog(path, cfg)
    assert loaded is not None
    assert loaded.library_id == lib_id
    assert [r.id for r in loaded.roots] == [id_a, id_b]
    p1 = next(p for p in loaded.photos if p.rel == "1.jpg")
    p2 = next(p for p in loaded.photos if p.rel == "2.jpg")
    assert p1.root_id == id_a and p2.root_id == id_b

    # Wrong library_id -> rejected (closes the old "library field never
    # checked" gap for explicit libraries, design §5).
    wrong_cfg = libmod.LibraryConfig(library_id=libmod.mint_library_id(),
                                     name="", roots=cat.roots, home=tmp_path)
    assert load_catalog(path, wrong_cfg) is None


def test_group_photo_rows_preserves_order_when_folder_split_by_subfolder(
        tmp_path: Path) -> None:
    """Regression: walk_library sorts by full path, so a folder's own
    files are NOT always adjacent — a subfolder whose name sorts between
    two of the parent's own files splits them (e.g. "2020/apple.jpg",
    "2020/summer/b.jpg", "2020/zoo.jpg": "2020"'s two files are not
    adjacent). _group_photo_rows must reproduce this exact flattened
    order on save+load (run-based grouping, not dict-insertion-order
    grouping keyed on (root_id, folder), which would silently move
    "2020/zoo.jpg" next to "2020/apple.jpg" and corrupt Album.members'
    index-based membership)."""
    from catalog import Album, Catalog, Folder, Photo

    rels = ["2020/apple.jpg", "2020/summer/b.jpg", "2020/zoo.jpg"]
    photos = [Photo(rel=r, folder=r.rpartition("/")[0],
                    name=r.rpartition("/")[2]) for r in rels]
    folders = {
        "2020": Folder(rel="2020", title="2020", photo_count=2,
                       total_count=2),
        "2020/summer": Folder(rel="2020/summer", title="summer",
                              photo_count=1, total_count=1),
    }
    # An album member pointing at the row that a dict-keyed grouping
    # would silently displace ("2020/summer/b.jpg" is index 1).
    uid = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    albums = {uid: Album(uid=uid, name="Split", members=[1])}
    cat = Catalog(root=tmp_path, photos=photos, folders=folders,
                 albums=albums)

    path = tmp_path / "catalog.json"
    save_catalog(cat, path)
    loaded = load_catalog(path, tmp_path)
    assert loaded is not None
    assert [p.rel for p in loaded.photos] == rels
    member_rel = loaded.photos[loaded.albums[uid].members[0]].rel
    assert member_rel == "2020/summer/b.jpg"


def test_photo_rows_byte_identical_with_and_without_roots_header(
        tmp_path: Path) -> None:
    """Promotion invariant (design §5/§8): a legacy catalog's photo rows
    are byte-identical whether or not Catalog.roots happens to be
    populated, as long as every photo's root_id is "" (roots[0] either
    way) — promotion rewrites ONLY the header, never a photo row."""
    from catalog import Catalog, Folder, Photo

    photos = [Photo(rel="a.jpg", folder="", name="a.jpg", star=2,
                    caption="hi", sha256="deadbeef", size=10, mtime=100)]
    folders = {"": Folder(rel="", title="x", photo_count=1, total_count=1)}

    bare = Catalog(root=tmp_path, photos=photos, folders=folders, albums={})
    with_root = Catalog(root=tmp_path, photos=photos, folders=folders,
                        albums={},
                        roots=[libmod.LibraryRoot(id="", path=tmp_path)])

    p1, p2 = tmp_path / "bare.json", tmp_path / "with_root.json"
    save_catalog(bare, p1)
    save_catalog(with_root, p2)
    # BYTES, not parsed dicts: dict equality would pass a key-order change
    # that still breaks the promotion invariant's byte-identity claim.
    assert p1.read_bytes() == p2.read_bytes()
    raw1 = _raw_catalog(p1)
    assert "R" not in raw1["photos"][0]         # group level, not per-row
    assert "R" not in _raw_photo_rows(raw1)[0]
    assert raw1["library"] == str(tmp_path)
    # And the row serialization itself: a roots[0] photo in an EXPLICIT
    # multi-root catalog must emit the same row bytes as the legacy row,
    # while a non-first-root photo gains exactly the "R" key.
    from catalog import _photo_to_row
    legacy_row = json.dumps(_photo_to_row(photos[0], ""))
    photos[0].root_id = "aaaaaaaa"
    first_root_row = json.dumps(_photo_to_row(photos[0], "aaaaaaaa"))
    nonfirst_row = _photo_to_row(photos[0], "bbbbbbbb")
    photos[0].root_id = ""
    assert first_root_row == legacy_row
    assert nonfirst_row.pop("R") == "aaaaaaaa"
    assert json.dumps(nonfirst_row) == legacy_row


def test_reconcile_duplicate_rel_across_roots_disambiguated(
        tmp_path: Path) -> None:
    """Two roots each have "img.jpg" — reconcile_walk's (root_id, rel) key
    (design §6) means walking one root never sees the other root's
    same-named photo as added/removed, and each root's drift is tracked
    independently."""
    from catalog import Catalog, Photo

    root_a, root_b = tmp_path / "a", tmp_path / "b"
    make_jpeg(root_a / "img.jpg")
    make_jpeg(root_b / "img.jpg")
    id_a, id_b = "aaaaaaaa", "bbbbbbbb"

    def sig(p: Path) -> tuple[int, int]:
        st = p.stat()
        return st.st_size, int(st.st_mtime)

    sa, sb = sig(root_a / "img.jpg"), sig(root_b / "img.jpg")
    photos = [
        Photo(rel="img.jpg", folder="", name="img.jpg", root_id=id_a,
              size=sa[0], mtime=sa[1]),
        Photo(rel="img.jpg", folder="", name="img.jpg", root_id=id_b,
              size=sb[0], mtime=sb[1]),
    ]
    cat = Catalog(root=root_a, photos=photos, folders={}, albums={},
                  roots=[libmod.LibraryRoot(id=id_a, path=root_a),
                         libmod.LibraryRoot(id=id_b, path=root_b)])

    drift_a = reconcile_walk(cat, root_a, root_id=id_a)
    drift_b = reconcile_walk(cat, root_b, root_id=id_b)
    assert drift_a is not None and not drift_a.changed
    assert drift_b is not None and not drift_b.changed

    # Delete root_b's copy only: root_a's reconcile must still see NO
    # drift, root_b's must see exactly one removal — never double-counted
    # or cross-attributed thanks to the (root_id, rel) key.
    (root_b / "img.jpg").unlink()
    drift_a2 = reconcile_walk(cat, root_a, root_id=id_a)
    drift_b2 = reconcile_walk(cat, root_b, root_id=id_b)
    assert drift_a2 is not None and not drift_a2.changed
    assert drift_b2 is not None
    assert drift_b2.removed == 1 and drift_a2.removed == 0


def test_catalog_abs_none_for_unknown_root(tmp_path: Path) -> None:
    """Catalog.abs() is the single choke point for absolute-path
    composition (design §6): a known root resolves normally; a photo
    whose root_id isn't in Catalog.roots at all (offline/unresolvable)
    returns None instead of raising or guessing at a path."""
    from catalog import Catalog, Photo

    root_a = tmp_path / "a"
    photo_known = Photo(rel="x.jpg", folder="", name="x.jpg",
                        root_id="aaaaaaaa")
    photo_unknown = Photo(rel="y.jpg", folder="", name="y.jpg",
                          root_id="ffffffff")
    cat = Catalog(root=root_a, photos=[photo_known, photo_unknown],
                  folders={}, albums={},
                  roots=[libmod.LibraryRoot(id="aaaaaaaa", path=root_a)])

    assert cat.abs(photo_known) == root_a / "x.jpg"
    assert cat.abs(photo_unknown) is None


def test_folder_key_prefix_convention_roundtrip(tmp_path: Path) -> None:
    """Folder identity in the persisted "folders"/"hidden_folders" maps
    (design §5): roots[0]'s folder keys are bare rel; every other root's
    folder is prefixed "<root_id>/<rel>" so same-named folders across
    roots (e.g. a "2019" folder on two drives) don't collide. A root's
    OWN top-level folder (rel == "") collapses to the bare root_id, no
    trailing slash."""
    from catalog import Catalog, Folder, Photo

    id_a, id_b = "aaaaaaaa", "bbbbbbbb"
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    photos = [
        Photo(rel="2019/x.jpg", folder="2019", name="x.jpg", root_id=id_a),
        Photo(rel="2019/y.jpg", folder="2019", name="y.jpg", root_id=id_b),
        Photo(rel="z.jpg", folder="", name="z.jpg", root_id=id_b),
    ]
    folders = {
        "2019": Folder(rel="2019", title="2019", description="root A 2019",
                       photo_count=1, total_count=1, root_id=id_a),
        f"{id_b}/2019": Folder(rel="2019", title="2019",
                              description="root B 2019", photo_count=1,
                              total_count=1, root_id=id_b,
                              folder_hidden=True),
        id_b: Folder(rel="", title="b", photo_count=1, total_count=1,
                     root_id=id_b),
    }
    cat = Catalog(root=root_a, photos=photos, folders=folders, albums={},
                  roots=[libmod.LibraryRoot(id=id_a, path=root_a),
                         libmod.LibraryRoot(id=id_b, path=root_b)],
                  library_id=libmod.mint_library_id())
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)
    raw = _raw_catalog(path)
    assert raw["folders"] == {"2019": "root A 2019",
                              f"{id_b}/2019": "root B 2019"}
    assert raw["hidden_folders"] == [f"{id_b}/2019"]

    cfg = libmod.LibraryConfig(library_id=cat.library_id, name="",
                               roots=cat.roots, home=tmp_path)
    loaded = load_catalog(path, cfg)
    assert loaded is not None
    assert set(loaded.folders) == {"2019", f"{id_b}/2019", id_b}
    assert loaded.folders["2019"].root_id == id_a
    assert loaded.folders[f"{id_b}/2019"].root_id == id_b
    assert loaded.folders[f"{id_b}/2019"].folder_hidden
    y = next(p for p in loaded.photos if p.rel == "2019/y.jpg")
    assert not y.visible   # folder_hidden forces invisibility


def test_old_version_compat_single_root_vs_multi_root(
        library: Path, tmp_path: Path) -> None:
    """Compat rule (design §5): a PRE_MULTIROOT_VERSION (v11 — the last
    format before root_id/roots plumbing landed) file still loads when
    the library passed to load_catalog has exactly one root — every row
    is then implicitly roots[0]. The SAME file is rejected outright (cold
    walk) when the library has more than one root: a pre-multiroot file
    predates root disambiguation and cannot express it."""
    import catalog as catmod

    cat = scan_library(library)
    thumbcache.build_cache(cat, tmp_path / "c")
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    data = _raw_catalog(path)
    assert data["version"] == catmod.CATALOG_VERSION
    # A genuine v11 fixture needs FLAT rows (ungroup the v13 shape back) —
    # v11 predates grouping entirely, same as it predates "R" (single root
    # here, so no "R" survives either way).
    data["photos"] = _raw_photo_rows(data)
    data["version"] = catmod.PRE_MULTIROOT_VERSION
    path.write_text(json.dumps(data))  # plain JSON: v11 never zstd-wrapped

    single_root_cfg = libmod.legacy_config(library)
    loaded = load_catalog(path, single_root_cfg)
    assert loaded is not None
    assert all(p.root_id == "" for p in loaded.photos)

    other = tmp_path / "other-root"
    other.mkdir()
    multi_cfg = libmod.LibraryConfig(
        library_id=libmod.mint_library_id(), name="",
        roots=[libmod.LibraryRoot(id="aaaaaaaa", path=library),
               libmod.LibraryRoot(id="bbbbbbbb", path=other)],
        home=tmp_path,
    )
    assert load_catalog(path, multi_cfg) is None


def test_plain_json_v13_file_rejected(tmp_path: Path) -> None:
    """A v13 file is ALWAYS the zstd-wrapped grouped shape (fauxcasa-
    ed5.5); a plain-JSON file claiming CATALOG_VERSION is a structural
    impossibility (never produced by save_catalog) and must cold-walk,
    not be trusted just because "version" matches."""
    import catalog as catmod

    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"version": catmod.CATALOG_VERSION,
                                "library": str(tmp_path), "photos": []}))
    assert load_catalog(path, tmp_path) is None


def test_zstd_wrapped_v11_file_rejected(tmp_path: Path) -> None:
    """The v11 compat carve-out only ever applied to plain JSON (v11
    predates zstd entirely) — a zstd-wrapped file claiming
    PRE_MULTIROOT_VERSION is likewise a structural impossibility and
    must cold-walk."""
    import catalog as catmod

    path = tmp_path / "catalog.json"
    _write_raw_catalog(path, {"version": catmod.PRE_MULTIROOT_VERSION,
                              "library": str(tmp_path), "photos": []})
    assert load_catalog(path, tmp_path) is None


def test_v13_catalog_rejected_after_v14_bump(
        library: Path, tmp_path: Path) -> None:
    """fauxcasa-cam.14 step 4 bumped CATALOG_VERSION 13->14 (new ini_sigs/
    contacts_sig fields): a genuine pre-bump v13 file — the PREVIOUS
    current format, zstd-wrapped, with neither field present — is now
    rejected outright and cold-rebuilds, per the module's existing
    'no migration path, the catalog is a regenerable cache' posture (same
    treatment every prior version bump gave its immediate predecessor)."""
    import catalog as catmod

    cat = scan_library(library)
    thumbcache.build_cache(cat, tmp_path / "c")
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    data = _raw_catalog(path)
    assert data["version"] == catmod.CATALOG_VERSION == 15
    data.pop("ini_sigs", None)      # a real v13 file never had these
    data.pop("contacts_sig", None)
    data["version"] = 13
    _write_raw_catalog(path, data)  # still zstd-wrapped: v13 always was

    assert load_catalog(path, library) is None


def test_v14_catalog_rejected_after_v15_bump(
        library: Path, tmp_path: Path) -> None:
    """fauxcasa-cam.5 bumped CATALOG_VERSION 14->15: a genuine pre-bump v14
    file is STRUCTURALLY identical (the bump is value-completeness — a v14
    catalog's Photo.faces may lack XMP-only faces/names, not a schema
    change — see the CATALOG_VERSION comment), so there is no field to
    strip here; only the version number itself distinguishes it, and that
    alone is enough to reject and cold-rebuild, same posture as every
    prior bump."""
    import catalog as catmod

    cat = scan_library(library)
    thumbcache.build_cache(cat, tmp_path / "c")
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    data = _raw_catalog(path)
    assert data["version"] == catmod.CATALOG_VERSION == 15
    data["version"] = 14
    _write_raw_catalog(path, data)  # still zstd-wrapped: v14 always was

    assert load_catalog(path, library) is None


# ---- multiroot cache binding (fauxcasa-ed5.7.3, bead .c) -------------------


def test_fcache_name_legacy_and_explicit() -> None:
    """The implicit legacy root (id "") keeps the unsuffixed 'thumbs.fcache'
    name — no rename, no rewrite (design §3); every explicit root gets its
    own 'thumbs-<root_id>.fcache'."""
    assert thumbcache.fcache_name("") == "thumbs.fcache"
    assert thumbcache.fcache_name("a1b2c3d4") == "thumbs-a1b2c3d4.fcache"


def test_open_shared_read_returns_a_readable_fd(tmp_path: Path) -> None:
    """thumbcache.open_shared_read (fauxcasa-ez2.12 finding 2), platform-
    neutral: it returns an int fd whose seek+read behaves exactly like a
    plain os.open() fd — the read path is unchanged, only the sharing
    mode differs."""
    target = tmp_path / "probe.bin"
    target.write_bytes(b"hello fcache")
    fd = thumbcache.open_shared_read(target)
    try:
        assert isinstance(fd, int) and fd >= 0
        os.lseek(fd, 6, 0)
        assert os.read(fd, 6) == b"fcache"
    finally:
        os.close(fd)


@pytest.mark.skipif(not sys.platform.startswith("win"),
                     reason="FILE_SHARE_DELETE is a Windows-only sharing "
                            "violation to reproduce")
def test_open_shared_read_tolerates_concurrent_replace(
        tmp_path: Path) -> None:
    """thumbcache.open_shared_read + _replace_fcache (fauxcasa-ez2.12
    finding 2): on Windows, plain os.open() lacks FILE_SHARE_DELETE, so a
    reconcile rebuild's tmp.replace(out) can raise PermissionError while
    the grid's worker fd is open. open_shared_read fixes the missing
    FILE_SHARE_DELETE half of that — but on-box verification found
    os.replace() (MoveFileEx/MOVEFILE_REPLACE_EXISTING) can still raise
    PermissionError against an open destination even WITH
    FILE_SHARE_DELETE; _replace_fcache's ReplaceFileW retry is the half
    that actually lands the rebuild. Exercise the real pair together: a
    handle from open_shared_read must let _replace_fcache succeed, and the
    already-open fd must keep serving the OLD bytes (an open Windows
    handle pins its data even after the name is replaced)."""
    target = tmp_path / "thumbs.fcache"
    target.write_bytes(b"OLD BYTES...")
    replacement = tmp_path / "thumbs.fcache.tmp"
    replacement.write_bytes(b"NEW BYTES!!!")

    fd = thumbcache.open_shared_read(target)
    try:
        thumbcache._replace_fcache(replacement, target)   # must not raise
        os.lseek(fd, 0, 0)
        assert os.read(fd, len(b"OLD BYTES...")) == b"OLD BYTES..."
    finally:
        os.close(fd)
    assert target.read_bytes() == b"NEW BYTES!!!"


def test_build_cache_legacy_root_id_writes_unsuffixed_name(
        tmp_path: Path) -> None:
    """The default root_id (LEGACY_ROOT_ID) writes the unsuffixed
    'thumbs.fcache' — exactly the pre-multiroot name — and binds via both
    the plain 2-arg bind() call (today's single-library callers) and the
    equivalent explicit root_id="" call (design §3/§4)."""
    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    cat = scan_library(root)
    result = thumbcache.build_cache(cat, tmp_path / "c")
    assert result.path.name == "thumbs.fcache"
    cache = thumbcache.load_cache(result.path)
    thumbcache.bind(cache, cat)               # today's call, unchanged
    thumbcache.bind(cache, cat, root_id="")   # equivalent per-root call


def test_build_cache_per_root_two_explicit_roots(tmp_path: Path) -> None:
    """Two explicit roots each get their OWN thumbs-<root_id>.fcache (+
    typed sidecar) — build_cache(root_id=...) builds only that root's
    slice, never touching the other root's file (design §3), and bind()
    with a root_id compares only that root's slice, so binding one root's
    cache against the other root fails loudly instead of silently
    misaligning tiles."""
    from catalog import Catalog, Photo

    root_a, root_b = tmp_path / "a", tmp_path / "b"
    make_jpeg(root_a / "1.jpg")
    make_jpeg(root_b / "2.jpg")
    id_a, id_b = "aaaaaaaa", "bbbbbbbb"
    lib_id = libmod.mint_library_id()

    cat = Catalog(
        root=root_a,
        photos=[
            Photo(rel="1.jpg", folder="", name="1.jpg", root_id=id_a),
            Photo(rel="2.jpg", folder="", name="2.jpg", root_id=id_b),
        ],
        folders={}, albums={},
        roots=[libmod.LibraryRoot(id=id_a, path=root_a),
               libmod.LibraryRoot(id=id_b, path=root_b)],
        library_id=lib_id,
    )
    cache_dir = tmp_path / "c"
    result_a = thumbcache.build_cache(cat, cache_dir, root_id=id_a)
    result_b = thumbcache.build_cache(cat, cache_dir, root_id=id_b)

    assert result_a.path.name == f"thumbs-{id_a}.fcache"
    assert result_b.path.name == f"thumbs-{id_b}.fcache"
    assert result_a.path != result_b.path
    assert result_a.photos == 1 and result_b.photos == 1

    cache_a = thumbcache.load_cache(result_a.path)
    cache_b = thumbcache.load_cache(result_b.path)
    assert cache_a.files == ["1.jpg"]
    assert cache_b.files == ["2.jpg"]
    assert cache_a.library_id == lib_id and cache_b.library_id == lib_id
    assert cache_a.sidecar_version == 2 and cache_b.sidecar_version == 2
    meta_a = json.loads(result_a.path.with_suffix(".fcache.json").read_text())
    assert meta_a["root_id"] == id_a and "library" not in meta_a

    thumbcache.bind(cache_a, cat, root_id=id_a)
    thumbcache.bind(cache_b, cat, root_id=id_b)
    with pytest.raises(thumbcache.CacheError):
        thumbcache.bind(cache_a, cat, root_id=id_b)  # wrong root: mismatch


def test_parse_sidecar_entry_typed_shapes() -> None:
    """Typed sidecar 'files' entries (sidecar_version 2, design §5): a
    plain string means a root-relative path belonging to THIS root
    (returned with a None root id — the string form carries none of its
    own); a two-element [root_id, rel] array is the reserved cross-root
    shape (unused in M1) — parsed, not delimiter-hacked. Anything else is
    malformed."""
    assert thumbcache.parse_sidecar_entry("a/b.jpg") == (None, "a/b.jpg")
    assert (thumbcache.parse_sidecar_entry(["aaaaaaaa", "a/b.jpg"])
            == ("aaaaaaaa", "a/b.jpg"))
    with pytest.raises(thumbcache.CacheError):
        thumbcache.parse_sidecar_entry(123)
    with pytest.raises(thumbcache.CacheError):
        thumbcache.parse_sidecar_entry(["only-one"])
    with pytest.raises(thumbcache.CacheError):
        thumbcache.parse_sidecar_entry([1, "not-a-string-id"])


def test_load_cache_accepts_mixed_typed_sidecar_entries(
        tmp_path: Path) -> None:
    """load_cache parses a sidecar_version 2 'files' array that mixes
    plain strings and the reserved 2-element cross-root shape (unused in
    M1) without raising, taking the rel from either shape."""
    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    make_jpeg(root / "b.jpg")
    cat = scan_library(root)
    result = thumbcache.build_cache(cat, tmp_path / "c")
    sidecar_path = result.path.with_suffix(".fcache.json")
    data = json.loads(sidecar_path.read_text())
    data["sidecar_version"] = 2
    data["files"] = [data["files"][0], ["ffffffff", data["files"][1]]]
    sidecar_path.write_text(json.dumps(data))

    cache = thumbcache.load_cache(result.path)
    assert cache.files == ["a.jpg", "b.jpg"]
    assert cache.sidecar_version == 2


def test_legacy_sidecar_shape_has_no_new_keys(tmp_path: Path) -> None:
    """A legacy (implicit-root) sidecar carries none of the new keys —
    'sidecar_version', 'library_id' — and every 'files' entry stays a
    plain string; this is the shape the shipped benchmark cache's sidecar
    has, and the new reader accepts it exactly as-is."""
    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    cat = scan_library(root)
    result = thumbcache.build_cache(cat, tmp_path / "c")
    raw = json.loads(result.path.with_suffix(".fcache.json").read_text())
    assert "sidecar_version" not in raw and "library_id" not in raw
    assert "library" in raw and all(isinstance(f, str) for f in raw["files"])

    cache = thumbcache.load_cache(result.path)
    assert cache.sidecar_version == 1 and cache.library_id == ""


def test_frozen_v1_legacy_rebuild_is_byte_identical(tmp_path: Path) -> None:
    """FROZEN-V1 byte-identity regression (bead .c acceptance criterion):
    a synthetic v1-style fixture built with the CURRENT (pre-multiroot)
    writer semantics — the exact dict/key-order thumbcache.build_cache
    wrote before this bead — is (a) read successfully by the new reader
    with zero rewrite, and (b) reproduced BYTE-FOR-BYTE (both the packed
    .fcache and the .fcache.json sidecar) by a legacy-mode rebuild through
    the new code (default root_id, no library_id). This is the portable,
    privacy-safe stand-in for diffing against the real (gitignored,
    machine-local) 100k benchmark cache at
    A:/dev/fauxcasa/cache/benchmark-thumbs.fcache, which was spot-checked
    by hand to still load through thumbcache.load_cache unchanged."""
    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg", 40, 30)
    make_jpeg(root / "sub" / "b.jpg", 50, 20)
    cat = scan_library(root)
    assert len(cat.photos) == 2
    assert cat.library_id == ""  # legacy: the branch under test

    out_dir = tmp_path / "c"
    result = thumbcache.build_cache(cat, out_dir)
    assert result.path.name == "thumbs.fcache"

    # The pre-multiroot dict literal, key order and all (thumbcache.py's
    # build_cache before fauxcasa-ed5.7.3): {count, library, thumb_edge,
    # files} for a single-level (v1) build — no "levels" key.
    expected_sidecar = {
        "count": len(cat.photos),
        "library": str(cat.root),
        "thumb_edge": thumbcache.THUMB_EDGE,
        "files": [p.rel for p in cat.photos],
    }
    # Path.write_text (both the real writer and this expected-fixture path)
    # applies platform newline translation on Windows (\n -> \r\n); compare
    # via the SAME write_text/read_bytes round trip on both sides so the
    # byte-identity assertion is about JSON content/key-order, not an
    # incidental platform newline mismatch.
    expected_path = tmp_path / "expected.fcache.json"
    expected_path.write_text(json.dumps(expected_sidecar, indent=1))
    expected_bytes = expected_path.read_bytes()
    actual_bytes = result.path.with_suffix(".fcache.json").read_bytes()
    assert actual_bytes == expected_bytes

    # The packed .fcache header is the frozen v1 layout regardless of this
    # bead's changes (this bead never touched _write_fcache).
    hdr = result.path.read_bytes()[:16]
    assert hdr == thumbcache.MAGIC + struct.pack(
        "<III", 1, len(cat.photos), 0)

    # New reader, zero rewrite: reads straight through, binds normally.
    cache = thumbcache.load_cache(result.path)
    assert cache.files == [p.rel for p in cat.photos]
    assert cache.library == str(cat.root)
    assert cache.library_id == "" and cache.sidecar_version == 1
    thumbcache.bind(cache, cat)


def test_cache_dir_for_legacy_digest_is_pinned() -> None:
    """cache_dir_for's legacy key formula — sha256(str(path.resolve()))[:16]
    — is UNCHANGED by the multiroot signature change (design §7: the
    function now takes the key string directly, callers own the
    str(path.resolve()) choice). This literal digest was computed
    independently (sha256 of the fixed key below, truncated to 16 hex
    chars) BEFORE any of this bead's edits, so a formula or truncation
    regression fails loudly — every existing single-root library's cache
    dir depends on this staying fixed forever."""
    key = r"A:\some\fixed\library\path"
    assert thumbcache.cache_dir_for(key, Path("cr")) == \
        Path("cr") / "b997252540498434"


def test_cache_dir_for_path_derivation_end_to_end(tmp_path: Path) -> None:
    """The full legacy derivation main.py performs — Path -> str(resolve())
    -> cache_dir_for — must equal sha256(str(path.resolve()))[:16] exactly
    (the OLD cache_dir_for(path) formula). The pinned-literal test above
    guards the hash+truncation; this one guards the path-key derivation
    that moved from cache_dir_for into the caller, so a future drift there
    (extra normalization, os.fspath, casefolding) fails on a real Path."""
    import hashlib

    root = tmp_path / "lib"
    root.mkdir()
    expected = hashlib.sha256(
        str(root.resolve()).encode()).hexdigest()[:16]
    got = thumbcache.cache_dir_for(str(root.resolve()), tmp_path / "cr")
    assert got == tmp_path / "cr" / expected


def test_cache_dir_for_library_id_distinct_from_legacy_and_variant(
        tmp_path: Path) -> None:
    """An explicit library's key (library_id, a uuid) never collides with
    an implicit legacy library's key (a resolved path string) even if one
    happened to look like the other, and a variant (ScanFilter/File Types
    cache_key) always derives its own dir, deterministically (design §7)."""
    croot = tmp_path / "cr"
    legacy_key = str((tmp_path / "lib").resolve())
    lib_id = libmod.mint_library_id()

    d_legacy = thumbcache.cache_dir_for(legacy_key, croot)
    d_explicit = thumbcache.cache_dir_for(lib_id, croot)
    assert d_legacy != d_explicit

    d_variant = thumbcache.cache_dir_for(lib_id, croot, "v1")
    assert d_variant != d_explicit
    assert thumbcache.cache_dir_for(lib_id, croot, "v1") == d_variant


def test_make_thumbcache_library_home_two_roots(tmp_path: Path) -> None:
    """--library-home mode (multiroot .c, design §11 — named --library-home
    here rather than the design doc's --library to avoid colliding with
    this script's existing single-directory --library flag) reads
    library.json and builds one thumbs-<root_id>.fcache (+ typed sidecar)
    per watched root."""
    # A ProcessPoolExecutor build (--library-home is NOT --sidecar-only, so
    # it always builds through the pool) can't run through a module loaded
    # via importlib under an alias ("mtc"): Windows spawn workers can't
    # re-import a module by that fake name (see test_main_bad_library_exits_2
    # for the same real-process pattern used elsewhere in this file for
    # exactly this reason).
    import os
    import subprocess

    script = REPO / "scripts" / "make-thumbcache.py"
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    make_jpeg(root_a / "1.jpg")
    make_jpeg(root_b / "2.jpg")
    home = tmp_path / "home"
    id_a, id_b = "aaaaaaaa", "bbbbbbbb"
    lib_id = libmod.mint_library_id()
    cfg = libmod.LibraryConfig(
        library_id=lib_id, name="Test",
        roots=[libmod.LibraryRoot(id=id_a, path=root_a),
               libmod.LibraryRoot(id=id_b, path=root_b)],
        home=home,
    )
    libmod.save_library(cfg)

    out_dir = tmp_path / "cache-out"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run(
        [sys.executable, str(script), "--library-home", str(home),
         "--out-dir", str(out_dir), "--jobs", "1"],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)

    fa = out_dir / f"thumbs-{id_a}.fcache"
    fb = out_dir / f"thumbs-{id_b}.fcache"
    assert fa.is_file() and fb.is_file()
    meta_a = json.loads(fa.with_suffix(".fcache.json").read_text())
    meta_b = json.loads(fb.with_suffix(".fcache.json").read_text())
    assert meta_a["library_id"] == lib_id and meta_a["root_id"] == id_a
    assert meta_b["library_id"] == lib_id and meta_b["root_id"] == id_b
    assert meta_a["files"] == ["1.jpg"]
    assert meta_b["files"] == ["2.jpg"]
    assert meta_a["sidecar_version"] == 2 and meta_b["sidecar_version"] == 2

    # in-app reader loads either per-root cache directly
    cache_a = thumbcache.load_cache(fa)
    assert cache_a.files == ["1.jpg"] and cache_a.library_id == lib_id


def test_make_thumbcache_library_home_default_out_dir_formula(
        tmp_path: Path, monkeypatch) -> None:
    """--library-home with no --out-dir derives the SAME digest dir as
    thumbcache.cache_dir_for(library_id, cache_root) with no variant
    (design §7), so a script-built cache lands exactly where the app's
    warm-start path looks for it. Checked as a pure formula (no process
    pool involved) — see the sibling smoke test's docstring for why a real
    --library-home build must go through a subprocess."""
    mtc = _load_mtc()
    monkeypatch.setattr(mtc, "CACHE", tmp_path / "cache")

    lib_id = libmod.mint_library_id()
    expected = thumbcache.cache_dir_for(lib_id, tmp_path / "cache")
    assert mtc._default_out_dir(lib_id) == expected


# ---- multiroot offline tolerance (fauxcasa-ed5.7.5, bead .e) --------------


# ---- multiroot volume UUID resolution (fauxcasa-ed5.7.6, bead .f) ---------


def test_volume_probe_dispatch_and_linux_uuid_mapping(
        tmp_path: Path, monkeypatch) -> None:
    """Linux UUID lookup maps the longest containing mount through the
    by-uuid identity table in both directions; unsupported hosts remain
    fail-soft rather than guessing a filesystem identity."""
    mount = tmp_path / "mnt"
    mount.mkdir()
    device = tmp_path / "device"
    device.write_bytes(b"")
    monkeypatch.setattr(volmod.sys, "platform", "linux")
    monkeypatch.setattr(
        volmod, "_linux_mounts", lambda: [(mount, str(device))])
    monkeypatch.setattr(
        volmod, "_linux_uuid_devices",
        lambda: {"1111-AAAA": device.resolve()})

    assert volmod.volume_uuid_for(mount) == "1111-AAAA"
    assert volmod.mount_for_uuid("1111-aaaa") == mount

    monkeypatch.setattr(volmod.sys, "platform", "unsupported-test-os")
    assert volmod.volume_uuid_for(mount) is None
    assert volmod.mount_for_uuid("1111-AAAA") is None


def test_volume_probe_dispatch_windows_and_macos(
        tmp_path: Path, monkeypatch) -> None:
    """Platform branches are testable without touching real volumes or
    launching diskutil; null results propagate unchanged."""
    marker = "\\\\?\\Volume{test}\\"
    monkeypatch.setattr(volmod.sys, "platform", "win32")
    monkeypatch.setattr(volmod, "_windows_volume_uuid", lambda _p: marker)
    monkeypatch.setattr(
        volmod, "_windows_mount_for_uuid", lambda _u: tmp_path)
    assert volmod.volume_uuid_for(tmp_path) == marker
    assert volmod.mount_for_uuid(marker) == tmp_path

    monkeypatch.setattr(volmod.sys, "platform", "darwin")
    monkeypatch.setattr(
        volmod, "_diskutil_info",
        lambda arg: ({"VolumeUUID": "MAC-UUID"} if arg == str(tmp_path)
                     else {"MountPoint": str(tmp_path)}))
    assert volmod.volume_uuid_for(tmp_path) == "MAC-UUID"
    assert volmod.mount_for_uuid("MAC-UUID") == tmp_path


def test_root_resolution_order_path_then_home_then_uuid(
        tmp_path: Path, monkeypatch) -> None:
    """Each earlier resolution signal prevents later probes from winning."""
    home = tmp_path / "home"
    home_candidate = home / "photos"
    home_candidate.mkdir(parents=True)
    last_known = tmp_path / "last-known"
    last_known.mkdir()
    uuid_mount = tmp_path / "uuid-mount"
    (uuid_mount / "archive").mkdir(parents=True)

    calls: list[str] = []
    monkeypatch.setattr(
        libmod.volumes, "volume_uuid_for",
        lambda _p: calls.append("path-uuid") or "VOL-A")
    monkeypatch.setattr(
        libmod.volumes, "mount_for_uuid",
        lambda _u: calls.append("uuid-mount") or uuid_mount)
    root = libmod.LibraryRoot(
        id="aaaaaaaa", path=last_known, volume_uuid="vol-a",
        vol_rel="archive", home_rel="photos")
    assert libmod.bind_root_location(root, home) == last_known.resolve()
    assert calls == ["path-uuid"]

    # A mismatched volume at the last-known path must not be trusted; the
    # home-relative moved-library candidate wins before UUID enumeration.
    calls.clear()
    monkeypatch.setattr(
        libmod.volumes, "volume_uuid_for",
        lambda _p: calls.append("path-uuid") or "DIFFERENT")
    root.path = last_known
    assert libmod.bind_root_location(root, home) == home_candidate.resolve()
    assert root.path == home_candidate.resolve()
    assert calls == ["path-uuid", "path-uuid", "uuid-mount"]

    # Without a home-relative signal, UUID + vol_rel is the final recovery.
    calls.clear()
    root.path = tmp_path / "missing"
    root.home_rel = None
    root.vol_rel = "archive"
    assert libmod.bind_root_location(root, home) == (uuid_mount / "archive").resolve()
    assert calls == ["uuid-mount"]


def test_uuid_remount_self_heals_library_json(
        tmp_path: Path, monkeypatch) -> None:
    """A drive-letter/mountpoint change rewrites only the last-known root
    path and survives a fresh read of library.json."""
    home = tmp_path / "home"
    new_mount = tmp_path / "remounted"
    new_root = new_mount / "Photos"
    new_root.mkdir(parents=True)
    root = libmod.LibraryRoot(
        id="aaaaaaaa", path=tmp_path / "old-mount" / "Photos",
        volume_uuid="VOL-1", vol_rel="Photos", label="Photos")
    cfg = libmod.LibraryConfig(
        library_id=libmod.mint_library_id(), name="Test", roots=[root],
        home=home)
    libmod.save_library(cfg)

    monkeypatch.setattr(libmod.volumes, "volume_uuid_for", lambda _p: None)
    monkeypatch.setattr(
        libmod.volumes, "mount_for_uuid",
        lambda value: new_mount if value == "VOL-1" else None)
    assert libmod.resolve_root_locations(cfg)
    assert cfg.roots[0].path == new_root.resolve()
    raw = json.loads(libmod.library_json_path(home).read_text(encoding="utf-8"))
    assert raw["roots"][0]["path"] == new_root.resolve().as_posix()

    loaded = libmod.load_library(home)
    assert loaded is not None
    assert loaded.roots[0].path == new_root.resolve()


def test_uuid_remount_clears_stale_home_rel(
        tmp_path: Path, monkeypatch) -> None:
    """A UUID recovery that moves the root off the library home must also
    re-derive home_rel: persisting the stale hint would let an unrelated
    directory that later appears at home/<old home_rel> outrank the UUID
    binding on the next open (home_rel is probed before UUID)."""
    home = tmp_path / "home"
    home.mkdir()
    new_mount = tmp_path / "remounted"
    new_root = new_mount / "Photos"
    new_root.mkdir(parents=True)
    root = libmod.LibraryRoot(
        id="aaaaaaaa", path=home / "Photos", volume_uuid="VOL-1",
        vol_rel="Photos", home_rel="Photos", label="Photos")
    cfg = libmod.LibraryConfig(
        library_id=libmod.mint_library_id(), name="Test", roots=[root],
        home=home)

    monkeypatch.setattr(libmod.volumes, "volume_uuid_for", lambda _p: None)
    monkeypatch.setattr(
        libmod.volumes, "mount_for_uuid",
        lambda value: new_mount if value == "VOL-1" else None)
    assert libmod.resolve_root_locations(cfg)
    assert cfg.roots[0].path == new_root.resolve()
    assert cfg.roots[0].home_rel is None
    raw = json.loads(libmod.library_json_path(home).read_text(encoding="utf-8"))
    assert raw["roots"][0]["home_rel"] is None

    # An unrelated directory appearing at the old home-relative spot must
    # not capture the root on the next resolve.
    (home / "Photos").mkdir()
    libmod.resolve_root_locations(cfg)
    assert cfg.roots[0].path == new_root.resolve()


def test_probe_cache_dedupes_one_open_but_not_the_next_pass(
        tmp_path: Path, monkeypatch) -> None:
    """fauxcasa-t0a: one volumes.ProbeCache scoped to an open dedupes the
    volume probes resolve_root_locations and refresh_offline_ids would
    otherwise EACH pay per UUID-bound root (2N serial subprocess launches
    on macOS) — while a fresh pass without the cache probes again, so a
    remount between passes is always re-observed (never a
    skip-when-path-matches shortcut)."""
    photos = tmp_path / "photos"
    photos.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    calls: list[str] = []
    monkeypatch.setattr(volmod, "volume_uuid_for",
                        lambda p: calls.append(str(p)) or "VOL-1")
    monkeypatch.setattr(volmod, "mount_for_uuid", lambda _u: None)
    root = libmod.LibraryRoot(id="aaaaaaaa", path=photos,
                              volume_uuid="VOL-1", vol_rel="photos")
    cfg = libmod.LibraryConfig(library_id=libmod.mint_library_id(),
                               name="T", roots=[root], home=home)

    probes = volmod.ProbeCache()
    libmod.resolve_root_locations(cfg, persist=False, probes=probes)
    assert len(calls) == 1              # healthy-path branch: one probe

    from catalog import Catalog
    cat = Catalog(root=photos, photos=[], folders={}, albums={},
                  roots=[root], library_id=cfg.library_id)
    cat.refresh_offline_ids(probes)     # the open's shared cache
    assert len(calls) == 1              # no re-probe within the open
    assert root.id not in cat.offline_ids

    cat.refresh_offline_ids()           # next pass, no cache: fresh probe
    assert len(calls) == 2


def test_uuid_resolution_rejects_overlapping_candidates(
        tmp_path: Path, monkeypatch) -> None:
    """Corrupt/stale bindings cannot self-heal two roots onto one tree."""
    home = tmp_path / "home"
    mount = tmp_path / "mount"
    (mount / "Photos").mkdir(parents=True)
    old_a = tmp_path / "gone-a"
    old_b = tmp_path / "gone-b"
    roots = [
        libmod.LibraryRoot(id="aaaaaaaa", path=old_a,
                           volume_uuid="VOL", vol_rel="Photos"),
        libmod.LibraryRoot(id="bbbbbbbb", path=old_b,
                           volume_uuid="VOL", vol_rel="Photos"),
    ]
    cfg = libmod.LibraryConfig(
        library_id=libmod.mint_library_id(), name="Test", roots=roots,
        home=home)
    libmod.save_library(cfg)
    monkeypatch.setattr(libmod.volumes, "mount_for_uuid", lambda _u: mount)

    assert not libmod.resolve_root_locations(cfg)
    assert [root.path for root in cfg.roots] == [old_a, old_b]
    raw = json.loads(libmod.library_json_path(home).read_text(encoding="utf-8"))
    assert [Path(root["path"]) for root in raw["roots"]] == [old_a, old_b]


def test_null_uuid_and_cross_platform_resolution_degrade_offline(
        tmp_path: Path, monkeypatch) -> None:
    """Null UUIDs use viable earlier signals but never invent a remount;
    a persisted UUID that cannot be probed on this host is conservatively
    offline unless home-relative or UUID enumeration can prove a location."""
    home = tmp_path / "home"
    home.mkdir()
    online = tmp_path / "online"
    online.mkdir()
    mount_calls: list[str] = []
    monkeypatch.setattr(libmod.volumes, "volume_uuid_for", lambda _p: None)
    monkeypatch.setattr(
        libmod.volumes, "mount_for_uuid",
        lambda value: mount_calls.append(value) or None)

    null_uuid = libmod.LibraryRoot(id="aaaaaaaa", path=online)
    assert libmod.bind_root_location(null_uuid, home) == online.resolve()
    assert mount_calls == []

    null_missing = libmod.LibraryRoot(
        id="bbbbbbbb", path=tmp_path / "gone", volume_uuid=None,
        vol_rel="Photos")
    assert libmod.bind_root_location(null_missing, home) is None
    assert null_missing.path == tmp_path / "gone"
    assert mount_calls == []

    known_but_unprobeable = libmod.LibraryRoot(
        id="cccccccc", path=online, volume_uuid="FOREIGN-UUID",
        vol_rel=None)
    assert libmod.bind_root_location(known_but_unprobeable, home) is None
    assert mount_calls == []


def test_transient_uuid_probe_glitch_preserves_stored_binding(
        tmp_path: Path, monkeypatch) -> None:
    """An UNMOVED root whose UUID probe transiently returns None keeps its
    stored binding: re-capturing (None, None) there would let the caller's
    self-heal persist the erasure, silently discarding the UUID remount
    net. A genuinely MOVED root still re-captures through the same probe
    outage -- home_rel's copy-outranks-UUID semantics -- clearing the now
    possibly-stale binding."""
    home = tmp_path / "home"
    photos = home / "photos"
    photos.mkdir(parents=True)
    monkeypatch.setattr(libmod.volumes, "volume_uuid_for", lambda _p: None)
    monkeypatch.setattr(libmod.volumes, "mount_for_uuid", lambda _u: None)
    root = libmod.LibraryRoot(
        id="aaaaaaaa", path=photos, volume_uuid="VOL-1", vol_rel="photos",
        home_rel="photos")
    assert libmod.bind_root_location(root, home) == photos.resolve()
    assert root.volume_uuid == "VOL-1"
    assert root.vol_rel == "photos"

    moved = home / "moved"
    moved.mkdir()
    root.home_rel = "moved"
    assert libmod.bind_root_location(root, home) == moved.resolve()
    assert root.path == moved.resolve()
    assert root.volume_uuid is None and root.vol_rel is None


def test_load_library_drops_unsafe_recovery_hints(
        tmp_path: Path, monkeypatch) -> None:
    """A tampered/escaping vol_rel or home_rel is an optional hint, not
    load-bearing structure: the root loads with the bad hint dropped
    instead of the whole library collapsing to the implicit legacy
    fallback (which would hide every root id/label)."""
    monkeypatch.setattr(libmod.volumes, "volume_uuid_for", lambda _p: None)
    monkeypatch.setattr(libmod.volumes, "mount_for_uuid", lambda _u: None)
    home = tmp_path / "home"
    home.mkdir()
    root_dir = tmp_path / "photos"
    root_dir.mkdir()
    payload = {
        "format": libmod.LIBRARY_FORMAT,
        "library_id": libmod.mint_library_id(),
        "name": "Test",
        "roots": [{
            "id": "aaaaaaaa",
            "path": root_dir.as_posix(),
            "volume_uuid": "VOL-1",
            "vol_rel": "../../escape",
            "home_rel": (tmp_path / "absolute").as_posix(),
            "label": "Photos",
        }],
    }
    libmod.library_json_path(home).parent.mkdir(parents=True)
    libmod.library_json_path(home).write_text(
        json.dumps(payload), encoding="utf-8")
    cfg = libmod.load_library(home)
    assert cfg is not None and not cfg.is_legacy
    assert cfg.roots[0].id == "aaaaaaaa"
    assert cfg.roots[0].label == "Photos"
    assert cfg.roots[0].vol_rel is None
    assert cfg.roots[0].home_rel is None
    assert cfg.roots[0].volume_uuid == "VOL-1"  # non-path hint kept


def test_linux_uuid_lookup_prefers_longest_containing_mount(
        tmp_path: Path, monkeypatch) -> None:
    """Overlapping mounts resolve to the NESTED mountpoint's identity --
    pins _linux_mounts' longest-first ordering (via its injectable
    /proc/mounts path, octal space escapes included) and
    _containing_mount's first-match selection, which a single-mount mock
    satisfies trivially."""
    nested = tmp_path / "mnt"
    nested.mkdir()
    parent_dev = tmp_path / "parent-dev"
    nested_dev = tmp_path / "nested-dev"
    parent_dev.write_bytes(b"")
    nested_dev.write_bytes(b"")

    def _mount_field(p: Path) -> str:
        return str(p).replace(" ", "\\040")

    mounts_file = tmp_path / "proc-mounts"
    mounts_file.write_text(
        f"{_mount_field(parent_dev)} {_mount_field(tmp_path)} ext4 rw 0 0\n"
        f"{_mount_field(nested_dev)} {_mount_field(nested)} ext4 rw 0 0\n",
        encoding="utf-8")
    mounts = volmod._linux_mounts(mounts_file)
    assert mounts[0][0] == nested  # longest mountpoint first, not file order

    monkeypatch.setattr(volmod.sys, "platform", "linux")
    monkeypatch.setattr(volmod, "_linux_mounts", lambda: mounts)
    monkeypatch.setattr(volmod, "_linux_uuid_devices", lambda: {
        "PARENT-UUID": parent_dev.resolve(),
        "NESTED-UUID": nested_dev.resolve(),
    })
    assert volmod.volume_uuid_for(nested) == "NESTED-UUID"
    assert volmod.volume_uuid_for(tmp_path) == "PARENT-UUID"


def test_home_rel_persists_and_recovers_moved_root(
        tmp_path: Path, monkeypatch) -> None:
    """save_library writes the additive home_rel for an under-home root,
    and a fresh load self-heals through it once the last-known path is
    gone -- the round trip the PR's 'persists home_rel additively' claim
    rests on."""
    monkeypatch.setattr(libmod.volumes, "volume_uuid_for", lambda _p: None)
    monkeypatch.setattr(libmod.volumes, "mount_for_uuid", lambda _u: None)
    home = tmp_path / "home"
    photos = home / "sub" / "photos"
    photos.mkdir(parents=True)
    cfg = libmod.LibraryConfig(
        library_id=libmod.mint_library_id(), name="Test",
        roots=[libmod.LibraryRoot(id="aaaaaaaa", path=photos,
                                  label="Photos")],
        home=home)
    libmod.save_library(cfg)
    raw = json.loads(
        libmod.library_json_path(home).read_text(encoding="utf-8"))
    assert raw["roots"][0]["home_rel"] == "sub/photos"

    raw["roots"][0]["path"] = (tmp_path / "gone").as_posix()
    libmod.library_json_path(home).write_text(
        json.dumps(raw), encoding="utf-8")
    loaded = libmod.load_library(home)
    assert loaded is not None
    assert loaded.roots[0].path == photos.resolve()


def test_diskutil_probe_survives_truncated_plist(monkeypatch) -> None:
    """diskutil exiting 0 with truncated XML must degrade to None through
    the fail-soft contract, not raise ExpatError (which plistlib's expat
    backend raises and which is NOT a ValueError)."""
    class _Proc:
        returncode = 0
        stdout = b"<?xml version='1.0'?><plist><dict>"

    monkeypatch.setattr(
        volmod.subprocess, "run", lambda *_a, **_k: _Proc())
    assert volmod._diskutil_info("disk1") is None


def test_root_is_online_resolved_path_check(
        tmp_path: Path, monkeypatch) -> None:
    """library.root_is_online (design §8, bead .e): a resolved existing
    directory is online; a missing path (unplugged drive) is offline; a
    path that resolves to a FILE, not a directory, is offline too — the
    check is is_dir(), not merely exists(). LibraryRoot.path is stored
    UNRESOLVED (last-known-path semantics), so a relative path pointed at
    a real directory must also read as online once resolved."""
    online_dir = tmp_path / "a"
    online_dir.mkdir()
    missing_dir = tmp_path / "gone"
    a_file = tmp_path / "not-a-dir.txt"
    a_file.write_text("x")

    assert libmod.root_is_online(libmod.LibraryRoot(id="x", path=online_dir))
    assert not libmod.root_is_online(
        libmod.LibraryRoot(id="x", path=missing_dir))
    assert not libmod.root_is_online(libmod.LibraryRoot(id="x", path=a_file))

    bound = libmod.LibraryRoot(
        id="x", path=online_dir, volume_uuid="BOUND-UUID")
    monkeypatch.setattr(
        libmod.volumes, "volume_uuid_for", lambda _p: "bound-uuid")
    assert libmod.root_is_online(bound)
    monkeypatch.setattr(
        libmod.volumes, "volume_uuid_for", lambda _p: "OTHER-UUID")
    assert not libmod.root_is_online(bound)  # reused mount path, wrong volume
    monkeypatch.setattr(libmod.volumes, "volume_uuid_for", lambda _p: None)
    assert not libmod.root_is_online(bound)  # unprobeable is conservatively offline


def test_catalog_offline_ids_refresh_and_abs_none_for_offline(
        tmp_path: Path) -> None:
    """Catalog.refresh_offline_ids/online_roots/offline_roots/abs (design
    §8, bead .e): both roots online -> abs() resolves both photos
    normally; root B's directory removed -> refresh_offline_ids() puts
    its id in offline_ids, online_roots()/offline_roots() split
    accordingly, and abs() now returns None for root B's photo (extending
    bead .b's unknown-root None case to the offline case) while root A's
    photo still resolves — and the photo ROW itself stays in
    `catalog.photos` untouched (design §8: unplugging a drive does not
    lose data)."""
    import shutil

    from catalog import Catalog, Photo

    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    id_a, id_b = "aaaaaaaa", "bbbbbbbb"
    photo_a = Photo(rel="1.jpg", folder="", name="1.jpg", root_id=id_a)
    photo_b = Photo(rel="2.jpg", folder="", name="2.jpg", root_id=id_b)
    cat = Catalog(root=root_a, photos=[photo_a, photo_b], folders={},
                 albums={},
                 roots=[libmod.LibraryRoot(id=id_a, path=root_a, label="A"),
                        libmod.LibraryRoot(id=id_b, path=root_b, label="B")])

    cat.refresh_offline_ids()
    assert cat.offline_ids == set()
    assert [r.id for r in cat.online_roots()] == [id_a, id_b]
    assert cat.offline_roots() == []
    assert cat.abs(photo_a) == root_a / "1.jpg"
    assert cat.abs(photo_b) == root_b / "2.jpg"

    shutil.rmtree(root_b)
    cat.refresh_offline_ids()
    assert cat.offline_ids == {id_b}
    assert [r.id for r in cat.online_roots()] == [id_a]
    assert [r.id for r in cat.offline_roots()] == [id_b]
    assert cat.abs(photo_a) == root_a / "1.jpg"      # online root: unaffected
    assert cat.abs(photo_b) is None                   # offline: placeholder
    assert photo_b in cat.photos                      # row never dropped


def test_reconcile_never_removes_offline_root_entries(tmp_path: Path) -> None:
    """THE regression test the bead names (design §8's load-bearing rule,
    §13 item 3): unplug root B — delete its directory outright, a whole
    drive vanishing, not a single file — run the per-root reconcile loop
    (main._reconcile_online_roots), and assert ZERO catalog entries are
    ever counted as removed for root B, root B is reported offline, and
    drift on the still-online root A is detected normally and is
    unaffected by root B's absence."""
    import shutil

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main
    from catalog import Catalog, Photo

    root_a, root_b = tmp_path / "a", tmp_path / "b"
    make_jpeg(root_a / "1.jpg")
    make_jpeg(root_a / "2.jpg")
    make_jpeg(root_b / "3.jpg")
    id_a, id_b = "aaaaaaaa", "bbbbbbbb"

    def sig(p: Path) -> tuple[int, int]:
        st = p.stat()
        return st.st_size, int(st.st_mtime)

    s1, s2, s3 = (sig(root_a / "1.jpg"), sig(root_a / "2.jpg"),
                 sig(root_b / "3.jpg"))
    photos = [
        Photo(rel="1.jpg", folder="", name="1.jpg", root_id=id_a,
             size=s1[0], mtime=s1[1]),
        Photo(rel="2.jpg", folder="", name="2.jpg", root_id=id_a,
             size=s2[0], mtime=s2[1]),
        Photo(rel="3.jpg", folder="", name="3.jpg", root_id=id_b,
             size=s3[0], mtime=s3[1]),
    ]
    cat = Catalog(root=root_a, photos=photos, folders={}, albums={},
                 roots=[libmod.LibraryRoot(id=id_a, path=root_a, label="A"),
                        libmod.LibraryRoot(id=id_b, path=root_b, label="B")])

    # Baseline: nothing changed yet, both roots online.
    drift, offline = main._reconcile_online_roots(cat, None, None, None)
    assert drift is not None and not drift.changed
    assert offline == []

    # Unplug root B outright, and produce REAL drift on root A so the
    # still-online root's reconcile is proven unaffected by root B's
    # absence, not just silent because nothing else happened either.
    shutil.rmtree(root_b)
    make_jpeg(root_a / "4.jpg")

    drift2, offline2 = main._reconcile_online_roots(cat, None, None, None)
    assert drift2 is not None
    # THE assertion: root B's now-missing "3.jpg" is NEVER counted as
    # removed — its root is offline, so reconcile_walk is never even
    # invoked for it.
    assert drift2.removed == 0
    assert drift2.added == 1           # root A's "4.jpg", detected normally
    assert drift2.modified == 0
    assert offline2 == ["B"]

    # The catalog itself still carries root B's photo row untouched.
    assert any(p.rel == "3.jpg" and p.root_id == id_b for p in cat.photos)


def test_reconcile_online_roots_detects_contacts_xml_drift_once(
        tmp_path: Path) -> None:
    """The one machine-local contacts.xml is checked ONCE by
    main._reconcile_online_roots (fauxcasa-cam.14 step 4), not inside the
    per-root reconcile_walk loop it calls: editing contacts.xml between
    scans is invisible to a bare reconcile_walk call (it has no
    contacts_path to check — that is explicitly not its job) on EITHER of
    a 2-root library's roots, but IS caught by the multiroot helper —
    resolving the PR-37 rider that editing contacts.xml never refreshed a
    warm start."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main
    from catalog import Catalog, Photo, stat_sig

    root_a, root_b = tmp_path / "a", tmp_path / "b"
    make_jpeg(root_a / "1.jpg")
    make_jpeg(root_b / "2.jpg")
    id_a, id_b = "aaaaaaaa", "bbbbbbbb"

    contacts_path = _write_faces_contacts_xml(tmp_path / "contacts.xml")

    def sig(p: Path) -> tuple[int, int]:
        st = p.stat()
        return st.st_size, int(st.st_mtime)

    s1, s2 = sig(root_a / "1.jpg"), sig(root_b / "2.jpg")
    photos = [
        Photo(rel="1.jpg", folder="", name="1.jpg", root_id=id_a,
             size=s1[0], mtime=s1[1]),
        Photo(rel="2.jpg", folder="", name="2.jpg", root_id=id_b,
             size=s2[0], mtime=s2[1]),
    ]
    cat = Catalog(root=root_a, photos=photos, folders={}, albums={},
                 contacts_sig=stat_sig(contacts_path),
                 roots=[libmod.LibraryRoot(id=id_a, path=root_a),
                        libmod.LibraryRoot(id=id_b, path=root_b)])

    # Baseline: clean on both individual roots and via the multiroot
    # helper.
    assert not reconcile_walk(cat, root_a, root_id=id_a).changed
    assert not reconcile_walk(cat, root_b, root_id=id_b).changed
    drift, _ = main._reconcile_online_roots(cat, None, None, None,
                                            contacts_path)
    assert drift is not None and not drift.changed

    # Edit contacts.xml externally (rename one contact).
    contacts_path.write_text(
        contacts_path.read_text().replace("Carol Xml", "Carol Renamed"))

    # A bare reconcile_walk call — no contacts_path parameter exists on
    # it at all — cannot see the edit, on EITHER root.
    assert not reconcile_walk(cat, root_a, root_id=id_a).ini_changed
    assert not reconcile_walk(cat, root_b, root_id=id_b).ini_changed

    # The multiroot helper DOES see it — exactly once, not doubled by
    # having two online roots.
    drift2, _ = main._reconcile_online_roots(cat, None, None, None,
                                             contacts_path)
    assert drift2 is not None
    assert drift2.ini_changed and drift2.changed
    assert drift2.added == 0 and drift2.removed == 0 and drift2.modified == 0


def test_reconcile_rebuild_reloads_contacts_xml_for_sig_pairing(
        tmp_path: Path) -> None:
    """Codex review finding 4 (fauxcasa-cam.14 step 4): the reconcile
    REBUILD path must reload contacts.xml from disk — not reuse
    self.contacts, the name map loaded once at startup — so the persisted
    contacts_sig and the resolved face names always describe the SAME
    snapshot. Before this fix, an edit landing between startup and
    reconcile would persist the NEW (post-edit) signature paired with the
    OLD (stale, startup) names, and a later warm start would trust that
    stale content forever since the signature claims nothing changed."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import time as _time

    from catalog import load_contacts_xml, stat_sig
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    cid = "cccccccccccccccc"
    (root / ".picasa.ini").write_text(
        f"[Contacts2]\r\n{cid}=Ini Name;;\r\n"
        f"[a.jpg]\r\nfaces=rect64(1234),{cid}\r\n")

    contacts_path = tmp_path / "contacts.xml"
    contacts_path.write_text(
        '<contacts>\n'
        f' <contact id="{cid}" name="Startup Name" '
        'modified_time="2026-01-01T00:00:00-07:00" local_contact="1"/>\n'
        '</contacts>\n')
    startup_contacts = load_contacts_xml(contacts_path)
    assert startup_contacts[cid] == "Startup Name"

    cat = scan_library(root, None, startup_contacts)
    face = next(p for p in cat.photos if p.rel == "a.jpg").faces[0]
    assert face[2] == "Startup Name"          # contacts.xml wins over ini
    cat.contacts_sig = stat_sig(contacts_path)

    cache_dir = tmp_path / "cachedir"
    win = MainWindow(cat, None, cache_dir=cache_dir, build_dir=None,
                     contacts=startup_contacts, contacts_path=contacts_path)

    # A real external edit AFTER startup — a rename in contacts.xml. The
    # new name is a different LENGTH than "Startup Name" (size, not just
    # mtime, must differ — int(st_mtime) truncates to whole seconds, and
    # two writes inside the same test can easily land in the same second).
    contacts_path.write_text(
        '<contacts>\n'
        f' <contact id="{cid}" name="Renamed To Somebody Else" '
        'modified_time="2026-01-02T00:00:00-07:00" local_contact="1"/>\n'
        '</contacts>\n')
    edited_sig = stat_sig(contacts_path)
    assert edited_sig != cat.contacts_sig      # baseline: a real edit happened

    win._start_reconcile()
    assert win._reconcile_thread is not None
    deadline = _time.time() + 20
    while win._reconcile_thread.is_alive() and _time.time() < deadline:
        app.processEvents()
        _time.sleep(0.01)
    assert not win._reconcile_thread.is_alive()
    app.processEvents()  # deliver the queued bridge.finished signal

    disk = load_catalog(cache_dir / "catalog.json", root)
    assert disk is not None
    assert disk.contacts_sig == edited_sig
    # THE regression: the persisted signature must be paired with the
    # NAMES it actually describes, never the stale startup map.
    disk_face = next(p for p in disk.photos if p.rel == "a.jpg").faces[0]
    assert disk_face[2] == "Renamed To Somebody Else"
    win.shutdown()


def test_reconcile_rebuild_contacts_sig_captured_before_read(
        tmp_path: Path) -> None:
    """Codex review round-2 finding 2 (fauxcasa-cam.14 step 4): the
    reconcile rebuild branch must stat contacts.xml BEFORE calling
    load_contacts_xml, not after — same guiding principle as
    catalog._read_folder_ini's round-2 pre-read stat fix (round-1 shipped
    this branch stat-AFTER-read). Simulated by mutating contacts.xml from
    inside a monkeypatched main.load_contacts_xml (a rewrite landing
    DURING that read): the persisted contacts_sig must be the
    PRE-mutation signature, so a subsequent reconcile correctly detects
    drift against the file's actual (post-mutation) state instead of
    trusting a signature that already looks current."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import time as _time
    import unittest.mock as mock

    import main as mainmod
    from catalog import load_contacts_xml as real_load_contacts_xml
    from catalog import stat_sig
    from main import MainWindow
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None

    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")

    contacts_path = tmp_path / "contacts.xml"
    contacts_path.write_text("<contacts>\n</contacts>\n")
    before_sig = stat_sig(contacts_path)

    cat = scan_library(root)
    cat.contacts_sig = before_sig
    cache_dir = tmp_path / "cachedir"
    win = MainWindow(cat, None, cache_dir=cache_dir, build_dir=None,
                     contacts_path=contacts_path)

    # A genuine photo drift is the simplest reliable trigger onto the
    # rebuild branch — the race under test happens once that branch
    # actually calls load_contacts_xml below, regardless of what tripped
    # drift detection in the first place.
    make_jpeg(root / "b.jpg")

    def racing_load(path):
        result = real_load_contacts_xml(path)
        # A rewrite landing DURING the read — appends text so size
        # changes (not just mtime, which truncates to whole seconds).
        Path(path).write_text(
            Path(path).read_text() + "<!-- edited mid-read -->\n")
        return result

    with mock.patch.object(mainmod, "load_contacts_xml",
                           side_effect=racing_load):
        win._start_reconcile()
        assert win._reconcile_thread is not None
        deadline = _time.time() + 20
        while win._reconcile_thread.is_alive() and _time.time() < deadline:
            app.processEvents()
            _time.sleep(0.01)
        assert not win._reconcile_thread.is_alive()
        app.processEvents()

    disk = load_catalog(cache_dir / "catalog.json", root)
    assert disk is not None
    # THE fix: the persisted signature is the PRE-mutation one, not the
    # file's current (post-mutation, mid-read-edited) state.
    assert disk.contacts_sig == before_sig
    assert disk.contacts_sig != stat_sig(contacts_path)
    win.shutdown()


def test_read_folder_ini_signature_captured_before_parse_time(
        tmp_path: Path) -> None:
    """Codex review round-2 finding 1 (fauxcasa-cam.14 step 4): a
    freshness signature must never be NEWER than the content it
    describes — _read_folder_ini now stats the winning path BEFORE
    calling read_picasa_ini, correcting the prior (round-1) stat-AFTER-
    read version, which paired NEW content with a signature that already
    read as current whenever a rewrite landed DURING the parse.
    Simulated by mutating the ini file from inside a monkeypatched
    read_picasa_ini (a rewrite landing exactly during the "read"): the
    signature _read_folder_ini returns must be the PRE-mutation one,
    stat'd before the mock ever ran — never the file's post-mutation
    state — so a subsequent reconcile (which re-stats fresh) correctly
    sees a MISMATCH against that stored signature and flags drift,
    self-healing with one rebuild instead of silently persisting stale
    content forever (the round-1 stat-after-read version would instead
    store the post-mutation signature and never flag drift again)."""
    import unittest.mock as mock

    import catalog as catmod
    import picasa_db

    folder = tmp_path / "lib"
    folder.mkdir()
    ini_path = folder / ".picasa.ini"
    ini_path.write_text("[Picasa]\r\nname=Before\r\n")
    before_sig = catmod.stat_sig(ini_path)

    real_read = picasa_db.read_picasa_ini

    def racing_read(path):
        result = real_read(path)                     # parses "Before"
        ini_path.write_text("[Picasa]\r\nname=After\r\n\r\n")  # the race
        return result

    with mock.patch("picasa_db.read_picasa_ini", side_effect=racing_read):
        result = catmod._read_folder_ini(folder)

    assert result is not None
    ini, sig = result
    assert ini.section("Picasa").get("name") == "Before"    # parsed content
    # THE fix: the signature is the PRE-mutation one, not the file's
    # current (post-mutation) state.
    assert sig == before_sig
    after_sig = catmod.stat_sig(ini_path)
    assert sig != after_sig  # size differs: "Before" vs "After\r\n\r\n"

    # A subsequent reconcile WOULD flag this as drift (self-heals with
    # one rebuild) rather than silently trusting the stale "Before" name
    # forever — reconcile's fresh probe reads the file's CURRENT state,
    # which no longer matches the stored (pre-mutation) signature.
    assert catmod._folder_ini_sig(folder) == after_sig != sig


@pytest.mark.skipif(sys.platform.startswith("win"),
                    reason="chmod 000 does not reliably block reads on Windows")
def test_ini_variant_selection_agrees_when_first_candidate_unreadable(
        tmp_path: Path) -> None:
    """Codex review round-2 finding 3 (fauxcasa-cam.14 step 4): an
    exists-but-UNREADABLE first INI_NAMES candidate (a real-world
    permission hiccup) must not make the scan side (_read_folder_ini,
    which falls through past it to a later readable name) and the
    reconcile side (_folder_ini_sig) pick DIFFERENT variants — a plain
    stat-only probe on the reconcile side would happily "succeed" on the
    unreadable file (stat needs only directory permission, not file-read
    permission) and report ITS signature, which would never match what
    the scan actually indexed from the SECOND file — every reconcile
    would then report drift forever, never converging. Both paths now
    share _select_ini_variant, so they agree exactly. Skipped when
    running as root: root ignores file-mode permission bits, so chmod
    000 would not actually block the read and the test would be
    meaningless (root is also skipped implicitly by the OSError below
    never being raised, but an explicit skip is clearer)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("running as root: chmod 000 does not block reads")

    import catalog as catmod

    folder = tmp_path / "lib"
    folder.mkdir()
    unreadable = folder / ".picasa.ini"   # first INI_NAMES candidate
    unreadable.write_text("[Picasa]\r\nname=Unreadable\r\n")
    readable = folder / "Picasa.ini"      # second INI_NAMES candidate
    readable.write_text("[Picasa]\r\nname=Readable\r\n")
    unreadable.chmod(0o000)
    try:
        with open(unreadable, "rb"):
            pytest.skip("unreadable file was still readable in this "
                       "environment (e.g. running as an unaffected "
                       "privileged/container user) — chmod 000 didn't "
                       "actually block the read here")
    except OSError:
        pass  # confirmed: this environment DOES enforce the permission

    try:
        # The scan side falls through to the readable second candidate.
        result = catmod._read_folder_ini(folder)
        assert result is not None
        ini, sig = result
        assert ini.path == readable
        assert ini.section("Picasa").get("name") == "Readable"
        assert sig == catmod.stat_sig(readable)

        # The reconcile side's independent probe must pick the SAME
        # (second, readable) variant — never the unreadable first one —
        # so a reconcile right after this "scan" sees no drift.
        assert catmod._folder_ini_sig(folder) == sig
    finally:
        unreadable.chmod(0o644)  # restore so tmp_path cleanup can remove it


def test_offline_root_labels_empty_for_single_root_library(
        tmp_path: Path) -> None:
    """_offline_root_labels (design §12, bead .e) is a no-op for a
    single-root (today's real, legacy) library even if offline_ids
    happens to be non-empty by construction — a single-root library going
    offline has no in-app badge target (main.py never opens an explicit
    multi-root library yet, bead .d/.g). A genuine >1-root library with an
    offline root DOES produce a label, falling back path.name when no
    explicit `label` was set."""
    import main
    from catalog import Catalog, Photo

    root = tmp_path / "solo"
    root.mkdir()
    single = Catalog(
        root=root, photos=[Photo(rel="1.jpg", folder="", name="1.jpg")],
        folders={}, albums={},
        roots=[libmod.LibraryRoot(id="aaaaaaaa", path=root)])
    single.offline_ids = {"aaaaaaaa"}      # contrived — still len(roots) == 1
    assert main._offline_root_labels(single) == []

    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    cat = Catalog(root=root_a, photos=[], folders={}, albums={},
                 roots=[libmod.LibraryRoot(id="aaaaaaaa", path=root_a),
                        libmod.LibraryRoot(id="bbbbbbbb", path=root_b)])
    cat.refresh_offline_ids()   # root_b was never created -> offline
    assert main._offline_root_labels(cat) == ["b"]     # falls back to path.name


def test_single_root_offline_message_explains_and_suppresses_badge(
        tmp_path: Path) -> None:
    """fauxcasa-hi2 items 4/5: the owner's 2026-09-14 decision. A
    single-root library whose one root is offline has no badge target
    (item 4 stays suppressed — asserted again here via
    _offline_root_labels), but it must NOT leave the user looking at an
    empty grid with no explanation (item 5) — _single_root_offline_message
    is that explanation, and _reconcile_online_roots must emit the SAME
    empty (no-badge) label list _offline_root_labels does, since fauxcasa-hi2
    item 4 was filed on the two disagreeing."""
    import main
    from catalog import Catalog, Photo

    root = tmp_path / "solo"
    root.mkdir()
    single = Catalog(
        root=root, photos=[Photo(rel="1.jpg", folder="", name="1.jpg")],
        folders={}, albums={},
        roots=[libmod.LibraryRoot(id="aaaaaaaa", path=root,
                                  label="Vacation Drive")])
    single.offline_ids = {"aaaaaaaa"}      # contrived — still len(roots) == 1

    # No badge (item 4, unchanged) ...
    assert main._offline_root_labels(single) == []
    # ... but a real, non-technical explanation (item 5).
    msg = main._single_root_offline_message(single)
    assert msg is not None
    assert "Vacation Drive" in msg
    for banned in ("root", "catalog", "reconcile", "offline_ids"):
        assert banned not in msg.lower()
    assert "isn't connected" in msg
    assert "pick up where it left off" in msg
    # One sentence: exactly one terminal period, at the end.
    assert msg.count(".") == 1 and msg.endswith(".")

    # A library with no offline root at all has nothing to explain.
    online = Catalog(
        root=root, photos=[], folders={}, albums={},
        roots=[libmod.LibraryRoot(id="aaaaaaaa", path=root)])
    online.refresh_offline_ids()
    assert main._single_root_offline_message(online) is None

    # A genuine multi-root library keeps using the badge surface instead
    # — _single_root_offline_message declines so callers fall back to
    # _offline_root_labels.
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    multi = Catalog(
        root=root_a, photos=[], folders={}, albums={},
        roots=[libmod.LibraryRoot(id="aaaaaaaa", path=root_a),
               libmod.LibraryRoot(id="bbbbbbbb", path=root_b)])
    multi.refresh_offline_ids()   # root_b was never created -> offline
    assert main._single_root_offline_message(multi) is None
    assert main._offline_root_labels(multi) == ["b"]   # badge still works


def test_single_root_offline_message_elides_long_drive_name() -> None:
    """The drive name is elided in the middle (like ElidingLabel's own
    choice for path-shaped text) rather than left to make the status-bar
    sentence unreasonably long."""
    import main
    from catalog import Catalog, Photo
    from pathlib import Path as _P

    long_label = "This Is A Very Long External Hard Drive Volume Label"
    assert len(long_label) > main._OFFLINE_DRIVE_NAME_MAX_CHARS
    root = _P("solo")
    single = Catalog(
        root=root, photos=[Photo(rel="1.jpg", folder="", name="1.jpg")],
        folders={}, albums={},
        roots=[libmod.LibraryRoot(id="aaaaaaaa", path=root,
                                  label=long_label)])
    single.offline_ids = {"aaaaaaaa"}

    msg = main._single_root_offline_message(single)
    assert msg is not None
    assert long_label not in msg          # too long — must be shortened
    assert "…" in msg                # middle-ellipsis marker present
    assert long_label[:5] in msg          # start survives
    assert long_label[-5:] in msg         # end survives


def test_reconcile_online_roots_suppresses_single_root_offline_label(
        tmp_path: Path) -> None:
    """_reconcile_online_roots (fauxcasa-hi2 item 4) must agree with
    _offline_root_labels: a single-root library's offline root produces NO
    label from either — the inconsistency the bead was filed about. A
    genuine multi-root library still gets its offline labels from the
    reconcile path, unchanged."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import main
    from catalog import Catalog, Photo

    root = tmp_path / "solo"
    make_jpeg(root / "1.jpg")
    id_only = "aaaaaaaa"
    photo = Photo(rel="1.jpg", folder="", name="1.jpg", root_id=id_only)
    single = Catalog(
        root=root, photos=[photo], folders={}, albums={},
        roots=[libmod.LibraryRoot(id=id_only, path=root)])

    # Simulate an unplugged single root: delete the directory outright so
    # refresh_offline_ids() (called inside _reconcile_online_roots) marks
    # it offline for real, not by contrived offline_ids construction.
    import shutil
    shutil.rmtree(root)

    drift, labels = main._reconcile_online_roots(single, None, None, None)
    assert drift is not None
    assert labels == []   # single-root: no badge label, matching item 4

    # Multi-root control: the offline root DOES still get a label here.
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    make_jpeg(root_a / "1.jpg")
    cat = Catalog(
        root=root_a, photos=[], folders={}, albums={},
        roots=[libmod.LibraryRoot(id="aaaaaaaa", path=root_a),
               libmod.LibraryRoot(id="bbbbbbbb", path=root_b, label="B")])
    drift2, labels2 = main._reconcile_online_roots(cat, None, None, None)
    assert drift2 is not None
    assert labels2 == ["B"]


def test_sidebar_shows_offline_root_badge(tmp_path: Path) -> None:
    """_build_sidebar (design §12, bead .e): a >1-root library with an
    offline root gets a greyed 'B (offline)' root node under Folders. Bead
    .g keeps that node enabled so any cached descendant folders remain
    browseable, but it is not itself selectable when the root has no direct
    photos. An online-only library shows no badge at all."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
    from main import MainWindow
    from catalog import Catalog, Photo

    app = QApplication.instance() or QApplication([])
    assert app is not None

    def badge_texts(win) -> list[str]:
        out = []
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            item = it.value()
            if item.text(0).endswith("(offline)"):
                out.append(item.text(0))
            it += 1
        return out

    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()   # root_b deliberately never created -> offline
    id_a, id_b = "aaaaaaaa", "bbbbbbbb"
    cat = Catalog(root=root_a, photos=[], folders={}, albums={},
                 roots=[libmod.LibraryRoot(id=id_a, path=root_a, label="A"),
                        libmod.LibraryRoot(id=id_b, path=root_b, label="B")])
    cat.refresh_offline_ids()
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    texts = badge_texts(win)
    assert texts == ["B (offline)"]
    it = QTreeWidgetItemIterator(win.tree)
    badge = None
    while it.value():
        if it.value().text(0) == "B (offline)":
            badge = it.value()
            break
        it += 1
    assert badge is not None
    assert not bool(badge.flags() & Qt.ItemFlag.ItemIsSelectable)
    assert bool(badge.flags() & Qt.ItemFlag.ItemIsEnabled)
    assert badge.font(0).italic()

    # An online-only (single-root) library shows no offline badge.
    solo = Catalog(root=root_a, photos=[], folders={}, albums={},
                   roots=[libmod.LibraryRoot(id=id_a, path=root_a)])
    win2 = MainWindow(solo, None, cache_dir=None, build_dir=None)
    assert badge_texts(win2) == []
