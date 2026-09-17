"""Tests for cropmap.py edit-recipe ingest and crop-applied display.

Split from test_tracer.py (fauxcasa-l09); originally lines 14186-14346 of the monolith."""

from __future__ import annotations

import json
from pathlib import Path
from catalog import (
    load_catalog,
    save_catalog,
    scan_library,
)
from tracer_helpers import (
    _FIXTURE_004_CROP,
    _FIXTURE_004_RECT,
    _raw_catalog,
    make_jpeg,
)


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

    assert CATALOG_VERSION >= 10  # exact value pinned by the v13 test
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
    data = _raw_catalog(path)
    data["version"] = 9                          # pre-edit-recipe format
    path.write_text(json.dumps(data))  # plain JSON: a version this old never zstd-wrapped
    assert load_catalog(path, root) is None
