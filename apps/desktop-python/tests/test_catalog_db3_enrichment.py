"""Tests for main.py/catalog.py root-aware db3/.pal enrichment.

Split from test_tracer.py (fauxcasa-l09); originally lines 9174-9809 of the monolith."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
import thumbcache
from catalog import (
    load_catalog,
    save_catalog,
    scan_library,
)
from tracer_helpers import (
    CID_DB3,
    PERSON_DB3,
    _db3_machine_path,
    _make_person_db3,
    _offscreen_app,
    _raw_catalog,
    _two_root_library_cfg,
    _write_pal,
    _write_pmp,
    make_jpeg,
)


# ---- root-aware db3/.pal enrichment (fauxcasa-cam.21) --------------------


def test_multiroot_db3_enrichment_is_root_qualified(tmp_path: Path) -> None:
    """cam.21 acceptance, db3 half: with two roots carrying the same rel,
    a db3 whose rows live under root A gap-fills ONLY root A's photo, the
    other root's twin stays untouched, no cross-root false
    db3_path_unresolved noise appears, and person diagnostics are emitted
    once — not once per root as the old per-root invocation did."""
    import main as mainmod

    cfg, root_a, _root_b = _two_root_library_cfg(tmp_path)
    db3 = _make_person_db3(tmp_path / "db3",
                           _db3_machine_path(root_a / "Trip"), ["same.jpg"])
    _write_pmp(db3 / "imagedata_caption.pmp", "string",
               ["", "root-a caption"])

    cat = mainmod._scan_library_config(cfg, None, {}, None, None, db3)
    by_root = {(p.root_id, p.rel): p for p in cat.photos}
    assert by_root[("aaaaaaaa", "Trip/same.jpg")].caption == "root-a caption"
    assert by_root[("bbbbbbbb", "Trip/same.jpg")].caption is None
    kinds = [e.kind for e in cat.report.entries]
    assert kinds.count("db3_caption_rescued") == 1
    assert kinds.count("db3_person_rescued") == 1
    assert "db3_path_unresolved" not in kinds
    assert "db3_path_ambiguous" not in kinds
    assert CID_DB3 in cat.db3_contacts
    assert cat.contacts[CID_DB3] == PERSON_DB3


def test_multiroot_pal_membership_is_root_qualified(tmp_path: Path) -> None:
    """cam.21 acceptance, .pal half: a member whose volume-stripped path
    translates onto root A attaches only there; a bare-rel member both
    roots could claim attaches NOWHERE and is reported ambiguous; and the
    unknown_album class is emitted once globally after the .pal pass, not
    prematurely per root."""
    import main as mainmod

    cfg, root_a, root_b = _two_root_library_cfg(tmp_path)
    uid_full, uid_bare, uid_gone = "a" * 32, "b" * 32, "c" * 32
    for r in (root_a, root_b):
        (r / "Trip" / ".picasa.ini").write_text(
            f"[same.jpg]\r\nalbums={uid_gone}\r\n")
    pal_dir = tmp_path / "pals"
    # The volume-stripped spelling of root_a's photo: drop only a drive
    # token (Windows) — on POSIX every component is real path material.
    parts = [c for c in str(root_a).replace("\\", "/").split("/") if c]
    if re.fullmatch(r"[A-Za-z]:", parts[0]):
        parts = parts[1:]
    a_member = "/".join(parts + ["Trip", "same.jpg"])
    _write_pal(pal_dir, uid_full, "Root A only", [a_member])
    _write_pal(pal_dir, uid_bare, "Ambiguous", ["Trip/same.jpg"])

    cat = mainmod._scan_library_config(cfg, None, {}, pal_dir, None, None)
    idx = {(p.root_id, p.rel): i for i, p in enumerate(cat.photos)}
    full = cat.albums[uid_full]
    assert full.pal_sourced
    assert full.members == [idx[("aaaaaaaa", "Trip/same.jpg")]]
    assert cat.albums[uid_bare].members == []
    kinds = [e.kind for e in cat.report.entries]
    assert kinds.count("pal_member_ambiguous") == 1
    assert "pal_member_missing" not in kinds
    assert "pal_divergence" not in kinds
    unknown = [e for e in cat.report.entries if e.kind == "unknown_album"]
    assert len(unknown) == 1 and unknown[0].subject == uid_gone


def test_db3_join_multiroot_drive_tie_break_and_ambiguity() -> None:
    """_make_join's multiroot resolution: translate_db3_path is drive-
    insensitive (§8), so sibling roots differing only by drive both match
    a stripped path — the row's own drive token must break the tie, and a
    third drive that matches both is ambiguous, never guessed. (Not
    constructible through real directories on one volume, hence the
    direct unit test.)"""
    from catalog import Photo
    from db3rescue import _make_join

    pa = Photo(rel="x.jpg", folder="", name="x.jpg", media="image")
    pa.root_id = "aaaaaaaa"
    pb = Photo(rel="x.jpg", folder="", name="x.jpg", media="image")
    pb.root_id = "bbbbbbbb"
    join = _make_join([("aaaaaaaa", Path("D:/photos")),
                       ("bbbbbbbb", Path("E:/photos"))], [pa, pb])
    assert join("D:\\photos\\x.jpg") == (pa, None)
    assert join("E:\\photos\\x.jpg") == (pb, None)
    assert join("Q:\\photos\\x.jpg") == (None, "ambiguous")
    assert join("Q:\\elsewhere\\x.jpg") == (None, "unresolved")
    assert join("") == (None, "unresolved")

    # An exact-rel match outranks a casefold-only twin in another root
    # even when the drive token matches neither (P3-c).
    pc = Photo(rel="Trip/Photo.jpg", folder="Trip", name="Photo.jpg",
               media="image")
    pc.root_id = "aaaaaaaa"
    pd = Photo(rel="Trip/photo.jpg", folder="Trip", name="photo.jpg",
               media="image")
    pd.root_id = "bbbbbbbb"
    join2 = _make_join([("aaaaaaaa", Path("D:/photos")),
                        ("bbbbbbbb", Path("E:/photos"))], [pc, pd])
    assert join2("Q:\\photos\\Trip\\Photo.jpg") == (pc, None)
    assert join2("Q:\\photos\\Trip\\photo.jpg") == (pd, None)


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
    (>= 11: the exact current value is pinned by the newest version-gate
    test, test_multiroot_two_root_save_load_roundtrip_v13.)"""
    import catalog as catmod

    assert catmod.CATALOG_VERSION >= 11
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

    data = _raw_catalog(path)
    data["version"] = 10                   # pre-db3-rescue format
    path.write_text(json.dumps(data))  # plain JSON: a version this old never zstd-wrapped
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


def test_db3_people_sidebar_not_flagged_when_name_shared_with_ini_contact(
        db3_library: Path, tmp_path: Path) -> None:
    """fauxcasa-7aj.2: the db3-rescued tooltip flag is keyed by CONTACT ID,
    not display name. b.jpg here names a SECOND, ini-only contact id that
    happens to share PERSON_DB3's display name with the db3-rescued
    contact on a.jpg — the display name is therefore known via a non-db3
    source too, so it must NOT be flagged (the old name-keyed check
    (`{cat.contacts[c] for c in db3_contacts}` matched by display name)
    would have wrongly flagged this ini contact as db3-rescued)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    cid_ini = "cccccccccccccccc"
    (db3_library / "Trip" / ".picasa.ini").write_text(
        "[Contacts2]\r\n"
        f"{cid_ini}={PERSON_DB3};;\r\n"
        "[a.jpg]\r\n"
        f"faces=rect64(6800600097ff9fff),{CID_DB3}\r\n"
        "[b.jpg]\r\n"
        f"faces=rect64(1234),{cid_ini}\r\n")
    db3 = _make_person_db3(
        tmp_path / "db3", _db3_machine_path(db3_library / "Trip"),
        ["a.jpg", "b.jpg"], face_parent=1)
    cat = scan_library(db3_library, db3_dir=db3)
    assert cat.db3_contacts == {CID_DB3}
    assert cat.contacts[CID_DB3] == cat.contacts[cid_ini] == PERSON_DB3
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    def item_for(kind: str, key: str):
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == (kind, key):
                return it.value()
            it += 1
        return None

    shared = item_for("person", PERSON_DB3)
    assert shared is not None and shared.text(0).endswith("(2)")
    assert shared.toolTip(0) == ""      # NOT flagged: also ini-named


# ---------------------------------------------------------------------------
# db3/.pal rescue gating (fauxcasa-ez2.13): --db3/--pal-dir default to
# THIS machine's Picasa2 AppData regardless of which library is opened, so
# without a gate, opening any folder on a machine that once ran Picasa
# floods the import report with db3_path_unresolved noise about some
# other library. The rescue now only auto-runs when the opened root
# overlaps one of Picasa's own watched roots (registry); an explicit
# --db3/--pal-dir always forces it on.
# ---------------------------------------------------------------------------


def test_paths_related_normalizes_case_and_containment(tmp_path: Path) -> None:
    import main as mainmod

    root = tmp_path / "Lib" / "Sub"
    root.mkdir(parents=True)
    watched = tmp_path / "lib"                       # same tree, diff case
    assert mainmod._paths_related(root, watched)
    assert mainmod._paths_related(watched, root)
    assert mainmod._paths_related(root, root)         # equal
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()
    assert not mainmod._paths_related(root, unrelated)


def test_db3_rescue_enabled_explicit_flag_always_wins(
        tmp_path: Path, monkeypatch) -> None:
    """An explicit --db3/--pal-dir forces the rescue on even when the
    registry says nothing about this root (or raises)."""
    import main as mainmod

    def boom():
        raise RuntimeError("no registry on this box")

    monkeypatch.setattr(mainmod.library, "picasa_watched_from_registry", boom)
    enabled, reason = mainmod._db3_rescue_enabled(tmp_path, explicit=True)
    assert enabled
    assert "explicit" in reason


def test_db3_rescue_enabled_gates_on_watched_roots(
        tmp_path: Path, monkeypatch) -> None:
    import main as mainmod

    watched_root = tmp_path / "Watched" / "Trip"
    watched_root.mkdir(parents=True)
    other_root = tmp_path / "unrelated"
    other_root.mkdir()

    monkeypatch.setattr(
        mainmod.library, "picasa_watched_from_registry",
        lambda: [tmp_path / "watched"])              # case-diff on purpose

    enabled, reason = mainmod._db3_rescue_enabled(watched_root, explicit=False)
    assert enabled
    assert "overlaps" in reason

    enabled, reason = mainmod._db3_rescue_enabled(other_root, explicit=False)
    assert not enabled
    assert "does not overlap" in reason


def test_db3_rescue_disabled_when_registry_absent(
        tmp_path: Path, monkeypatch) -> None:
    import main as mainmod

    def boom():
        raise RuntimeError("Picasa watched-folders registry value not found")

    monkeypatch.setattr(mainmod.library, "picasa_watched_from_registry", boom)
    enabled, reason = mainmod._db3_rescue_enabled(tmp_path, explicit=False)
    assert not enabled
    assert "registry" in reason


def test_import_report_status_count_excludes_and_collapses_db3_unresolved() -> None:
    """catalog.ImportReport: status_count() drops every db3_path_unresolved
    entry (machine residue, not a library conflict); grouped_entries()
    still surfaces them, collapsed into ONE row per source with a count
    instead of N rows, while every other kind stays one row per entry."""
    from catalog import ImportReport

    report = ImportReport()
    for i in range(5):
        report.add("db3", "db3_path_unresolved", f"path{i}.jpg", "unjoined")
    report.add("ini", "unknown_album", "UID1", "referenced but undefined")
    report.add("contacts", "contact_name_conflict", "cid1", "name diverges")

    assert report.status_count() == 2               # the 5 unresolved excluded

    rows = report.grouped_entries()
    unresolved_rows = [r for r in rows if r[1] == "db3_path_unresolved"]
    assert len(unresolved_rows) == 1                 # collapsed to one row
    source, kind, count, examples = unresolved_rows[0]
    assert source == "db3" and count == 5
    assert examples == [f"path{i}.jpg" for i in range(5)]
    other_rows = [r for r in rows if r[1] != "db3_path_unresolved"]
    assert len(other_rows) == 2                      # untouched, one each
    assert all(r[2] == 1 for r in other_rows)


def test_import_report_grouped_entries_caps_examples_at_20() -> None:
    from catalog import ImportReport

    report = ImportReport()
    for i in range(25):
        report.add("db3", "db3_path_unresolved", f"path{i}.jpg", "unjoined")
    rows = report.grouped_entries()
    assert len(rows) == 1
    _source, _kind, count, examples = rows[0]
    assert count == 25
    assert len(examples) == 20


def test_import_notes_dialog_lists_grouped_entries(library: Path) -> None:
    """The status-bar button's dialog (fauxcasa-ez2.13) lists (kind,
    count, examples) rows via ImportReport.grouped_entries() and offers a
    'Reveal report file' button."""
    _offscreen_app()
    from main import MainWindow
    from catalog import ReportEntry

    cat = scan_library(library)
    for i in range(3):
        cat.report.entries.append(ReportEntry(
            "db3", "db3_path_unresolved", f"p{i}.jpg", "unjoined"))
    cat.report.entries.append(ReportEntry(
        "ini", "unknown_album", "UID1", "referenced but undefined"))
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    captured = {}

    def fake_exec(dlg):
        from PySide6.QtWidgets import QTableWidget, QPushButton
        table = dlg.findChildren(QTableWidget)[0]
        captured["rows"] = [
            (table.item(r, 0).text(), table.item(r, 1).text(),
             table.item(r, 2).text())
            for r in range(table.rowCount())]
        captured["reveal"] = [b for b in dlg.findChildren(QPushButton)
                              if b.text() == "Reveal report file"]
        return 0

    import PySide6.QtWidgets as qtw
    orig = qtw.QDialog.exec
    qtw.QDialog.exec = lambda self: fake_exec(self)
    try:
        win._show_import_notes_dialog()
    finally:
        qtw.QDialog.exec = orig

    rows = captured["rows"]
    assert ("[db3] db3_path_unresolved", "3", "p0.jpg, p1.jpg, p2.jpg") in rows
    assert ("[ini] unknown_album", "1", "UID1") in rows
    assert len(captured["reveal"]) == 1


def test_db3_rescue_skipped_for_unrelated_root_shows_zero_import_notes(
        tmp_path: Path, monkeypatch) -> None:
    """End-to-end (fauxcasa-ez2.13): opening a non-Picasa root with a db3
    dir present, on a machine whose Picasa watched roots don't overlap
    it, runs no rescue at all — the import report stays empty rather than
    filling with db3_path_unresolved noise about some other library."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import main as mainmod
    from catalog import REPORT_NAME, load_report

    app = QApplication.instance() or QApplication([])
    assert app is not None

    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    db3 = _make_person_db3(tmp_path / "db3", "Q:\\somewhere\\else",
                           ["a.jpg"], face_parent=1)

    monkeypatch.setattr(mainmod, "default_db3_dir", lambda: db3)
    monkeypatch.setattr(mainmod, "default_pal_dir", lambda: None)
    monkeypatch.setattr(
        mainmod.library, "picasa_watched_from_registry",
        lambda: [tmp_path / "totally-unrelated-library"])

    cache_root = tmp_path / "cr"
    monkeypatch.setattr(sys, "argv", [
        "fauxcasa-tracer", str(root), "--cache-root", str(cache_root),
        "--quit-after-ready", "--finish-build", "--timeout", "30"])
    rc = mainmod.main()
    assert rc == 0

    cache_dir = thumbcache.cache_dir_for(str(root.resolve()), cache_root, "")
    report = load_report(cache_dir / REPORT_NAME)
    assert report.entries == []


def test_explicit_db3_flag_forces_rescue_for_unrelated_root(
        tmp_path: Path, monkeypatch) -> None:
    """An explicit --db3 always forces the rescue on (the
    PicasaStarter-relocation case), even for a root the registry's
    watched folders say nothing about."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import main as mainmod
    from catalog import REPORT_NAME, load_report

    app = QApplication.instance() or QApplication([])
    assert app is not None

    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    db3 = _make_person_db3(tmp_path / "db3", "Q:\\somewhere\\else",
                           ["a.jpg"], face_parent=1)

    monkeypatch.setattr(
        mainmod.library, "picasa_watched_from_registry",
        lambda: [tmp_path / "totally-unrelated-library"])

    cache_root = tmp_path / "cr"
    monkeypatch.setattr(sys, "argv", [
        "fauxcasa-tracer", str(root), "--cache-root", str(cache_root),
        "--db3", str(db3),
        "--quit-after-ready", "--finish-build", "--timeout", "30"])
    rc = mainmod.main()
    assert rc == 0

    cache_dir = thumbcache.cache_dir_for(str(root.resolve()), cache_root, "")
    report = load_report(cache_dir / REPORT_NAME)
    assert any(e.kind == "db3_path_unresolved" for e in report.entries)
