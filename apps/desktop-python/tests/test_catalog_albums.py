"""Tests for catalog.py Picasa album ingest.

Split from test_tracer.py (fauxcasa-l09); originally lines 8369-8851 of the monolith."""

from __future__ import annotations

import json
import os
from pathlib import Path
from catalog import (
    load_catalog,
    save_catalog,
    scan_library,
)
from tracer_helpers import (
    UID_DEF,
    UID_GHOST,
    UID_PAL,
    _raw_catalog,
    _write_faces_contacts_xml,
    _write_pal,
    make_jpeg,
)


# ---------------------------------------------------------------------------
# M1 ingest completion: import report + placeholder albums (fauxcasa-cam.13)
# and the Picasa2Albums .pal reader + §4 gap-fill merge (fauxcasa-cam.8).
# Synthetic fixtures only (privacy rule): hand-authored inis and .pal XML per
# the forensicir 2007 writeup. Merge rank ASSUMED ini > .pal > db3 pending
# the spec pin (fauxcasa-79b); every departure from a source is an
# ImportReport entry, never a silent resolution (§4).
# ---------------------------------------------------------------------------


def test_read_pal_good_garbage_and_fallbacks(tmp_path: Path) -> None:
    """read_pal parses the documented shape — 32-hex uid, name, the real64
    OLE date converted to the catalog's canonical ISO string, members with
    the [C]\\ volume token stripped to POSIX paths — falls back to the
    file's own name for the uid (Picasa names .pal files by uid), and
    fails soft PER FILE on garbage: bytes that aren't XML, XML that isn't
    a picasa2album, and a file with no usable uid anywhere are each None."""
    from catalog import read_pal, read_pal_dir

    good = _write_pal(tmp_path / "albums", UID_PAL, "Sammy",
                      ["Trip/a.jpg", "Deep/er/b.jpg"])
    pal = read_pal(good)
    assert pal is not None
    assert pal.uid == UID_PAL
    assert pal.name == "Sammy"
    assert pal.date == "2007-07-09T15:07:15"   # OLE 39272.630035
    assert pal.members == ["Trip/a.jpg", "Deep/er/b.jpg"]

    # uid falls back to the file stem when the properties carry none
    stemmed = tmp_path / "albums" / f"{UID_DEF}.pal"
    stemmed.write_text("<picasa2album><files>\n"
                       "<filename>[C]\\x.jpg</filename>\n"
                       "</files></picasa2album>")
    pal = read_pal(stemmed)
    assert pal is not None and pal.uid == UID_DEF
    assert pal.name == UID_DEF[:8] and pal.date is None
    assert pal.members == ["x.jpg"]

    garbage = tmp_path / "albums" / "nothex.pal"
    garbage.write_bytes(b"\x00\x01 not xml at all")
    assert read_pal(garbage) is None
    not_album = tmp_path / "albums" / "other.pal"
    not_album.write_text("<somethingelse><a/></somethingelse>")
    assert read_pal(not_album) is None
    no_uid = tmp_path / "albums" / "badname.pal"   # stem not 32-hex either
    no_uid.write_text("<picasa2album><files/></picasa2album>")
    assert read_pal(no_uid) is None

    # the directory reader keeps the good ones and reports the bad by name
    pals, bad = read_pal_dir(tmp_path / "albums")
    assert {p.uid for p in pals} == {UID_PAL, UID_DEF}
    assert sorted(bad) == ["badname.pal", "nothex.pal", "other.pal"]
    assert read_pal_dir(tmp_path / "no-such-dir") == ([], [])


def test_pal_gap_fill_only_merge(album_library: Path, tmp_path: Path) -> None:
    """§4 merge (rank assumed ini > .pal > db3, fauxcasa-79b): an AGREEING
    .pal changes nothing except filling the ini definition's missing date;
    a .pal-ONLY album materializes like a real album flagged pal-sourced
    (unresolvable members reported, resolvable ones kept); and a .pal that
    IS a placeholder's missing definition fills the name/date gap while
    membership authority stays with the ini's albums= tokens."""
    pal_dir = tmp_path / "albums"
    _write_pal(pal_dir, UID_DEF, "Defined", ["Trip/a.jpg", "Trip/b.jpg"])
    _write_pal(pal_dir, UID_PAL, "Pal Only",
               ["Trip/c.jpg", "Gone/missing.jpg"])
    _write_pal(pal_dir, UID_GHOST, "Ghost Found",
               ["Trip/b.jpg", "Trip/c.jpg"], date=None)

    cat = scan_library(album_library, pal_dir=pal_dir)
    assert list(cat.albums) == [UID_DEF, UID_GHOST, UID_PAL]

    d = cat.albums[UID_DEF]
    assert d.members == [0, 1] and not d.placeholder and not d.pal_sourced
    assert d.name == "Defined"
    assert d.date == "2007-07-09T15:07:15"     # gap-filled: ini had no date

    p = cat.albums[UID_PAL]
    assert p.pal_sourced and not p.placeholder
    assert p.name == "Pal Only" and p.members == [2]

    g = cat.albums[UID_GHOST]
    assert g.pal_sourced and not g.placeholder  # the .pal WAS the definition
    assert g.name == "Ghost Found"
    assert g.members == [1, 2]                  # ini membership, untouched

    kinds = [(e.kind, e.subject) for e in cat.report.entries]
    assert ("pal_member_missing", UID_PAL) in kinds
    assert not any(k == "pal_divergence" for k, _u in kinds)
    assert not any(k == "unknown_album" for k, _u in kinds)  # de-placeholdered
    missing = next(e for e in cat.report.entries
                   if e.kind == "pal_member_missing")
    assert "Gone/missing.jpg" in missing.detail and missing.source == "pal"


def test_pal_divergence_reported_not_membership(
        album_library: Path, tmp_path: Path) -> None:
    """A DIVERGENT .pal for an ini-defined album: the extra member is an
    import-report entry, NOT a membership change, and the ini member the
    .pal lacks is kept (and recorded). An unreadable .pal file is reported
    and skipped without sinking the scan (fail-soft per file)."""
    pal_dir = tmp_path / "albums"
    _write_pal(pal_dir, UID_DEF, "Defined", ["Trip/a.jpg", "Trip/c.jpg"])
    (pal_dir / f"{UID_PAL}.pal").write_bytes(b"\xff\xfe utterly broken")

    cat = scan_library(album_library, pal_dir=pal_dir)
    d = cat.albums[UID_DEF]
    assert d.members == [0, 1]                 # ini wins: c.jpg NOT added
    assert UID_PAL not in cat.albums           # broken file never lands

    div = [e for e in cat.report.entries if e.kind == "pal_divergence"]
    assert len(div) == 1 and div[0].subject == UID_DEF
    assert "Trip/c.jpg" in div[0].detail       # the extra, surfaced
    assert "Trip/b.jpg" in div[0].detail       # the ini member the .pal lacks
    assert "ini wins" in div[0].detail         # the recorded choice
    bad = [e for e in cat.report.entries if e.kind == "pal_unreadable"]
    assert len(bad) == 1 and bad[0].subject == f"{UID_PAL}.pal"


def test_placeholder_album_materializes_and_reports(
        album_library: Path) -> None:
    """§3: an albums= uid with no definition anywhere materializes as a
    placeholder Album — uid, 'Unknown album <uid8>' name, members
    populated, placeholder flag — plus an unknown_album import-report
    entry. Never dropped (the pre-cam.13 code silently skipped these)."""
    cat = scan_library(album_library)
    assert list(cat.albums) == [UID_DEF, UID_GHOST]
    g = cat.albums[UID_GHOST]
    assert g.placeholder and not g.pal_sourced
    assert g.name == f"Unknown album {UID_GHOST[:8]}"
    assert g.members == [1, 2]                 # b.jpg + c.jpg
    assert not cat.albums[UID_DEF].placeholder

    unknown = [e for e in cat.report.entries if e.kind == "unknown_album"]
    assert len(unknown) == 1
    assert unknown[0].subject == UID_GHOST and unknown[0].source == "ini"
    assert "placeholder" in unknown[0].detail


def test_album_redefinition_reported(tmp_path: Path) -> None:
    """Two folders carrying [.album:<uid>] sections that differ in name (or
    other fields) produce an album_redefinition import-report entry naming
    both values and the winner; an identical duplicate does NOT produce one;
    the first definition always wins (fauxcasa-cam.17)."""
    root = tmp_path / "lib"
    make_jpeg(root / "A" / "a.jpg")
    make_jpeg(root / "B" / "b.jpg")
    uid = "aa" * 16
    (root / "A" / ".picasa.ini").write_text(
        f"[.album:{uid}]\r\nname=First Name\r\n"
        f"[a.jpg]\r\nalbums={uid}\r\n")
    (root / "B" / ".picasa.ini").write_text(
        f"[.album:{uid}]\r\nname=Second Name\r\n"
        f"[b.jpg]\r\nalbums={uid}\r\n")
    cat = scan_library(root)

    redef = [e for e in cat.report.entries if e.kind == "album_redefinition"]
    assert len(redef) == 1
    e = redef[0]
    assert e.subject == uid and e.source == "ini"
    assert "First Name" in e.detail and "Second Name" in e.detail
    assert "first" in e.detail.lower()          # winner named
    # first definition wins
    assert cat.albums[uid].name == "First Name"


def test_album_redefinition_no_flood_on_identical(tmp_path: Path) -> None:
    """An identical duplicate definition (same name, date, description)
    does not produce a report entry — only divergence is reported."""
    root = tmp_path / "lib"
    make_jpeg(root / "A" / "a.jpg")
    make_jpeg(root / "B" / "b.jpg")
    uid = "bb" * 16
    ini_block = (f"[.album:{uid}]\r\nname=Same\r\ndate=39272.630035\r\n"
                 f"description=same desc\r\n")
    (root / "A" / ".picasa.ini").write_text(ini_block + f"[a.jpg]\r\nalbums={uid}\r\n")
    (root / "B" / ".picasa.ini").write_text(ini_block + f"[b.jpg]\r\nalbums={uid}\r\n")
    cat = scan_library(root)

    assert not [e for e in cat.report.entries if e.kind == "album_redefinition"]


def test_cross_folder_contact_conflict_reported(tmp_path: Path) -> None:
    """Two sibling folders whose [Contacts2] sections name the same contact
    id differently produce a contact_name_conflict entry (source='ini') with
    both values and the winner named; the first-registered folder wins in the
    flat registry (fauxcasa-cam.17)."""
    root = tmp_path / "lib"
    make_jpeg(root / "Alpha" / "a.jpg")
    make_jpeg(root / "Beta" / "b.jpg")
    cid = "a1b2c3d4e5f6a1b2"
    (root / "Alpha" / ".picasa.ini").write_text(
        f"[Contacts2]\r\n{cid}=Alice Alpha;;\r\n")
    (root / "Beta" / ".picasa.ini").write_text(
        f"[Contacts2]\r\n{cid}=Alice Beta;;\r\n")
    cat = scan_library(root)

    cross = [e for e in cat.report.entries
             if e.kind == "contact_name_conflict" and e.source == "ini"]
    assert len(cross) == 1
    e = cross[0]
    assert e.subject == cid
    assert "Alice Alpha" in e.detail and "Alice Beta" in e.detail
    assert "first" in e.detail.lower()          # winner described
    # the first-registered value is in the registry
    assert cat.contacts[cid] in ("Alice Alpha", "Alice Beta")


def test_cross_folder_contact_conflict_no_duplicate_entry(tmp_path: Path) -> None:
    """A contact conflict between two folders is reported only ONCE even when
    a child of the losing folder re-inherits the losing name — the de-dup
    guard prevents a second entry for the same contact id."""
    root = tmp_path / "lib"
    make_jpeg(root / "Alpha" / "a.jpg")
    make_jpeg(root / "Beta" / "b.jpg")
    make_jpeg(root / "Beta" / "sub" / "c.jpg")
    cid = "c3d4e5f6a1b2c3d4"
    (root / "Alpha" / ".picasa.ini").write_text(
        f"[Contacts2]\r\n{cid}=One;;\r\n")
    (root / "Beta" / ".picasa.ini").write_text(
        f"[Contacts2]\r\n{cid}=Two;;\r\n")
    # Beta/sub inherits "Two" from Beta; no extra ini entry
    cat = scan_library(root)

    cross = [e for e in cat.report.entries
             if e.kind == "contact_name_conflict" and e.source == "ini"]
    assert len(cross) == 1  # only one entry despite three folders


def test_contact_name_conflict_reported(
        faces_library: Path, tmp_path: Path) -> None:
    """The §4 conflict PR #37 resolved silently: contacts.xml renaming a
    [Contacts2] contact is now an import-report entry recording both names
    and the winner. Agreeing ids and xml-only ids produce no entry."""
    from catalog import load_contacts_xml

    contacts = load_contacts_xml(
        _write_faces_contacts_xml(tmp_path / "contacts.xml"))
    cat = scan_library(faces_library, None, contacts)

    # contacts.xml conflict: source="contacts"; cross-folder ini conflict
    # for Ada Ancestor vs Ada Local is source="ini" (tested separately below)
    xml_conflicts = [e for e in cat.report.entries
                     if e.kind == "contact_name_conflict"
                     and e.source == "contacts"]
    assert len(xml_conflicts) == 1             # only Carol has a contacts.xml conflict
    e = xml_conflicts[0]
    assert e.subject == "cccccccccccccccc"
    assert "Carol Ini" in e.detail and "Carol Xml" in e.detail
    assert "contacts.xml wins" in e.detail
    # ...and the resolution itself is unchanged (xml wins, §4)
    assert cat.contacts["cccccccccccccccc"] == "Carol Xml"

    # without contacts.xml: one cross-folder ini conflict (Ada Ancestor vs Ada Local)
    ini_cross = [e for e in scan_library(faces_library).report.entries
                 if e.kind == "contact_name_conflict" and e.source == "ini"]
    assert len(ini_cross) == 1


def test_placeholder_sidebar_marking_and_notes_count(
        album_library: Path) -> None:
    """The sidebar shows a placeholder album visually marked — dimmed,
    italic, '?' suffix — while a real album renders normally; the status
    bar carries the 'N import notes' count with the first entries in the
    tooltip; and clicking the placeholder filters the grid to its members
    exactly like a real album (never dropped, §3)."""
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

    cat = scan_library(album_library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    ghost = item_for(win, "album", UID_GHOST)
    assert ghost is not None
    assert ghost.text(0) == f"Unknown album {UID_GHOST[:8]} ?  (2)"
    assert ghost.font(0).italic()
    assert "placeholder" in ghost.toolTip(0)
    real = item_for(win, "album", UID_DEF)
    assert real.text(0) == "Defined  (2)" and not real.font(0).italic()

    assert win.notes_label.isVisibleTo(win)
    assert win.notes_label.text().strip() == "1 import note"
    assert "unknown_album" in win.notes_label.toolTip()

    win._sidebar_clicked(ghost, 0)             # placeholders filter like albums
    assert [cat.photos[i].rel for i in win.grid.display] == [
        "Trip/b.jpg", "Trip/c.jpg"]

    # a catalog with no notes shows no chrome at all
    clean = scan_library(album_library)
    clean.report.entries.clear()
    win2 = MainWindow(clean, None, cache_dir=None, build_dir=None)
    assert not win2.notes_label.isVisibleTo(win2)


def test_import_report_persistence_and_warm_status(
        album_library: Path, tmp_path: Path) -> None:
    """save_report/load_report round-trip the entries beside catalog.json;
    a missing or corrupt report file degrades to an EMPTY report (fail-soft
    — diagnostics, never a gate); and a warm start that re-attaches the
    persisted report drives the same status-bar count the scan did."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from catalog import REPORT_NAME, load_report, save_report
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    cat = scan_library(album_library)
    assert len(cat.report.entries) == 1
    save_report(cat.report, tmp_path / REPORT_NAME)
    back = load_report(tmp_path / REPORT_NAME)
    assert back.entries == cat.report.entries
    assert back.summary() == "1 import note (unknown_album)"

    assert load_report(tmp_path / "absent.json").entries == []
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    assert load_report(corrupt).entries == []

    # the warm path: persisted catalog (report NOT inside it) + re-attach
    save_catalog(cat, tmp_path / "catalog.json")
    loaded = load_catalog(tmp_path / "catalog.json", album_library)
    assert loaded is not None
    assert loaded.report.entries == []         # empty until re-attached
    loaded.report = load_report(tmp_path / REPORT_NAME)
    win = MainWindow(loaded, None, cache_dir=None, build_dir=None)
    assert win.notes_label.text().strip() == "1 import note"


def test_catalog_v6_roundtrips_album_flags(
        album_library: Path, tmp_path: Path) -> None:
    """From CATALOG_VERSION 6 on: placeholder and pal-sourced albums survive
    the persisted catalog — flags, names, members — and a v5 catalog (which
    silently dropped both classes) is rejected so a warm start cold-rebuilds
    instead of hiding them again. (>= 6: the exact current value is pinned
    by the newest version-gate test.)"""
    import catalog as catmod

    assert catmod.CATALOG_VERSION >= 6
    pal_dir = tmp_path / "albums"
    _write_pal(pal_dir, UID_PAL, "Pal Only", ["Trip/c.jpg"])
    cat = scan_library(album_library, pal_dir=pal_dir)
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    loaded = load_catalog(path, album_library)
    assert loaded is not None
    assert list(loaded.albums) == list(cat.albums)
    for uid, orig in cat.albums.items():
        back = loaded.albums[uid]
        assert back.placeholder == orig.placeholder
        assert back.pal_sourced == orig.pal_sourced
        assert back.members == orig.members and back.name == orig.name
    assert loaded.albums[UID_GHOST].placeholder
    assert loaded.albums[UID_PAL].pal_sourced

    data = _raw_catalog(path)
    data["version"] = 5
    path.write_text(json.dumps(data))  # plain JSON: a version this old never zstd-wrapped
    assert load_catalog(path, album_library) is None


def test_album_tooltip_date_description(tmp_path: Path) -> None:
    """A regular album with date and/or description surfaces those as sidebar
    tooltip lines; placeholder and pal-sourced albums keep their own tooltips
    (fauxcasa-cam.14)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    uid_dated = "aaaabbbbccccdddd" * 2
    uid_nodesc = "0000111122223333" * 2
    (root / "f" / ".picasa.ini").write_text(
        f"[.album:{uid_dated}]\r\n"
        "name=Summer Trip\r\n"
        "date=2022-07-15\r\n"
        "description=Beach holiday\r\n"
        f"token=]album:{uid_dated}\r\n"
        f"[.album:{uid_nodesc}]\r\n"
        "name=No Desc\r\n"
        f"token=]album:{uid_nodesc}\r\n"
        "[a.jpg]\r\n"
        f"albums={uid_dated},{uid_nodesc}\r\n"
    )
    cat = scan_library(root)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    def item_for(kind: str, key: str):
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == (kind, key):
                return it.value()
            it += 1
        return None

    dated = item_for("album", uid_dated)
    assert dated is not None
    tip = dated.toolTip(0)
    assert "2022-07-15" in tip
    assert "Beach holiday" in tip

    no_desc = item_for("album", uid_nodesc)
    assert no_desc is not None
    # No date/description — no tooltip set (empty string)
    assert no_desc.toolTip(0) == ""
