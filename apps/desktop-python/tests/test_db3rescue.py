"""Tests for db3rescue.py Picasa db3 rescue/parsing.

Split from test_tracer.py (fauxcasa-l09); originally lines 8854-9171 of the monolith."""

from __future__ import annotations

from pathlib import Path
import thumbcache
from catalog import (
    scan_library,
)
from tracer_helpers import (
    CID_DB3,
    _db3_machine_path,
    _make_caption_db3,
    _make_person_db3,
    _write_pmp,
    _write_thumbindex,
    _xmp_app1,
    make_jpeg,
    write_jpeg_meta,
)


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


def test_db3_caption_gap_fill_precedence_and_reporting(tmp_path: Path) -> None:
    """cam.20: db3 fills only empty captions and reports every populated
    non-gap outcome. Ini and later in-file captions remain authoritative.
    """
    root = tmp_path / "lib"
    folder = root / "Trip"
    make_jpeg(folder / "gap.jpg")
    make_jpeg(folder / "same.jpg")
    make_jpeg(folder / "conflict.jpg")
    write_jpeg_meta(folder / "infile.jpg",
                    xmp=_xmp_app1(caption="in-file wins"))
    (folder / ".picasa.ini").write_text(
        "[same.jpg]\r\ncaption=same caption\r\n"
        "[conflict.jpg]\r\ncaption=ini wins\r\n")
    db3 = _make_caption_db3(
        tmp_path / "db3", _db3_machine_path(folder),
        ["gap.jpg", "same.jpg", "conflict.jpg", "infile.jpg"],
        ["rescued caption", "same caption", "db3 loses", "db3 interim"],
    )

    cat = scan_library(root, db3_dir=db3)
    by_name = {p.name: p for p in cat.photos}
    assert by_name["gap.jpg"].caption == "rescued caption"
    assert by_name["same.jpg"].caption == "same caption"
    assert by_name["conflict.jpg"].caption == "ini wins"
    assert by_name["infile.jpg"].caption == "db3 interim"

    entries = {(e.kind, e.subject): e for e in cat.report.entries}
    assert ("db3_caption_rescued", "Trip/gap.jpg") in entries
    assert ("db3_caption_rescued", "Trip/infile.jpg") in entries
    assert ("db3_caption_redundant", "Trip/same.jpg") in entries
    assert ("db3_caption_conflict", "Trip/conflict.jpg") in entries
    assert "higher-rank" in entries[
        ("db3_caption_conflict", "Trip/conflict.jpg")].detail

    # The indexer's established §4 merge runs after db3 rescue and replaces
    # the interim gap-fill with non-empty in-file XMP/IPTC metadata.
    thumbcache.build_cache(cat, tmp_path / "cache")
    assert by_name["infile.jpg"].caption == "in-file wins"
    assert by_name["gap.jpg"].caption == "rescued caption"


def test_db3_caption_fail_soft_unreadable_and_unjoined(tmp_path: Path) -> None:
    """Caption rescue never sinks a scan: broken join bytes and caption rows
    beyond thumbindex become import-report diagnostics, not exceptions.
    """
    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "a.jpg")

    broken = tmp_path / "broken-db3"
    _write_pmp(broken / "imagedata_caption.pmp", "string", ["", "caption"])
    (broken / "thumbindex.db").write_bytes(b"not a thumbindex")
    cat = scan_library(root, db3_dir=broken)
    assert cat.photos[0].caption is None
    unreadable = [e for e in cat.report.entries
                  if e.kind == "db3_unreadable"]
    assert len(unreadable) == 1 and unreadable[0].subject == "thumbindex.db"

    short = tmp_path / "short-db3"
    _write_thumbindex(short / "thumbindex.db", [
        (_db3_machine_path(root / "Trip"), 0x01, None),
        ("a.jpg", 0x02, 0),
    ])
    _write_pmp(short / "imagedata_caption.pmp", "string",
               ["", "joined", "past end"])
    cat = scan_library(root, db3_dir=short)
    assert cat.photos[0].caption == "joined"
    misses = [e for e in cat.report.entries
              if e.kind == "db3_path_unresolved"]
    assert len(misses) == 1 and misses[0].subject == "imagedata row 2"

    # imagedata carries a caption but thumbindex.db is absent altogether
    # (distinct fail-soft branch from the corrupt-bytes case above).
    absent = tmp_path / "absent-db3"
    _write_pmp(absent / "imagedata_caption.pmp", "string", ["", "caption"])
    cat = scan_library(root, db3_dir=absent)
    assert cat.photos[0].caption is None
    unreadable = [e for e in cat.report.entries
                  if e.kind == "db3_unreadable"]
    assert len(unreadable) == 1 and unreadable[0].subject == "thumbindex.db"

    # a caption row's thumbindex path joins to a real machine path, but no
    # photo in this library — the deleted-photo case; reported subject is
    # the untranslated abs machine path (folder_abs + name), not a row index.
    ghost_folder = _db3_machine_path(root / "Trip")
    ghost = tmp_path / "ghost-db3"
    _make_caption_db3(ghost, ghost_folder, ["a.jpg", "ghost.jpg"],
                      ["", "ghost caption"])
    cat = scan_library(root, db3_dir=ghost)
    assert cat.photos[0].caption is None      # a.jpg's own db3 caption was ""
    unresolved = [e for e in cat.report.entries
                  if e.kind == "db3_path_unresolved"]
    assert len(unresolved) == 1
    assert unresolved[0].subject == ghost_folder + "ghost.jpg"


def test_db3_unreadable_thumbindex_reported_once(db3_library: Path,
                                                 tmp_path: Path) -> None:
    """A single corrupt thumbindex.db must produce exactly ONE
    db3_unreadable entry even when both the caption rescue and the
    person/face rescue independently read it — caption rescue runs first
    and bails, the face-row section must not add a second identical note.
    """
    db3 = _make_person_db3(
        tmp_path / "db3", _db3_machine_path(db3_library / "Trip"),
        ["a.jpg", "b.jpg"], face_parent=1)
    _write_pmp(db3 / "imagedata_caption.pmp", "string", ["", "caption"])
    (db3 / "thumbindex.db").write_bytes(b"not a thumbindex")

    cat = scan_library(db3_library, db3_dir=db3)
    unreadable = [e for e in cat.report.entries
                  if e.kind == "db3_unreadable"]
    assert len(unreadable) == 1 and unreadable[0].subject == "thumbindex.db"
