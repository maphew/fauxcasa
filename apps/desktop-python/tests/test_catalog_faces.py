"""Tests for catalog.py faces/people read-only ingest and the People surface.

Split from test_tracer.py (fauxcasa-l09); originally lines 5866-6155 of the monolith."""

from __future__ import annotations

from pathlib import Path
from catalog import (
    load_catalog,
    save_catalog,
    scan_library,
)
from tracer_helpers import (
    _write_faces_contacts_xml,
)


# ---- faces/people read-only ingest + People surface -----------------------
# (fauxcasa-cam.1/.2/.3) Synthetic fixtures only (privacy rule): hand-authored
# inis + a hand-authored contacts.xml matching the oracle-014 grammar + tiny
# Qt-generated JPEGs — no real Picasa data.


def test_load_contacts_xml_defensive(tmp_path: Path) -> None:
    """load_contacts_xml: ids are zero-padded to 16 to join faces= /
    [Contacts2]; a nameless or bad-id entry skips that entry only; a
    missing or malformed FILE yields {} (fail-soft, never a gate)."""
    from catalog import load_contacts_xml

    p = tmp_path / "contacts.xml"
    p.write_text(
        '<contacts>'
        '<contact id="ca5c88ca60f42c0b" name="Pat One" local_contact="1"/>'
        '<contact id="632e71e2ffd6c6d" name="Pat Short"/>'
        '<contact id="ffffffffffffffff" name=""/>'
        '<contact name="No Id"/>'
        '<contact id="not-hex-at-all" name="Bad Id"/>'
        '</contacts>')
    assert load_contacts_xml(p) == {
        "ca5c88ca60f42c0b": "Pat One",
        "0632e71e2ffd6c6d": "Pat Short",   # short id joined padded
    }
    assert load_contacts_xml(tmp_path / "absent.xml") == {}
    garbage = tmp_path / "garbage.xml"
    garbage.write_bytes(b"\x00\x01 not xml at all")
    assert load_contacts_xml(garbage) == {}


def test_default_contacts_xml_discovery(monkeypatch, tmp_path: Path) -> None:
    """default_contacts_xml finds %LocalAppData%\\Google\\Picasa2\\contacts\\
    contacts.xml when it exists, and returns None (not a phantom path) when
    the env var or the file is absent."""
    from catalog import default_contacts_xml

    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert default_contacts_xml() is None
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_contacts_xml() is None   # dir exists, file absent
    p = tmp_path / "Google" / "Picasa2" / "contacts" / "contacts.xml"
    p.parent.mkdir(parents=True)
    p.write_text("<contacts/>")
    assert default_contacts_xml() == p


def test_default_pal_dir_discovery(monkeypatch, tmp_path: Path) -> None:
    """default_pal_dir finds %LocalAppData%\\Google\\Picasa2Albums — real
    Picasa's layout, a sibling of Picasa2\\ (fauxcasa-1t6) — preferring it
    over the legacy Picasa2\\Picasa2Albums fallback, and returns None (not a
    phantom path) when the env var or both directories are absent."""
    from catalog import default_pal_dir

    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert default_pal_dir() is None
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_pal_dir() is None        # Google/ absent entirely
    legacy = tmp_path / "Google" / "Picasa2" / "Picasa2Albums"
    legacy.mkdir(parents=True)
    assert default_pal_dir() == legacy      # fallback still honoured
    sibling = tmp_path / "Google" / "Picasa2Albums"
    sibling.mkdir()
    assert default_pal_dir() == sibling     # real layout wins over fallback
    legacy.rmdir()
    assert default_pal_dir() == sibling     # real layout alone, no fallback


def test_scan_ingests_faces_regions_and_names(faces_library: Path) -> None:
    """faces= regions land on Photo.faces as (rect, padded id, name):
    rects decode per parse_rect64, short ids join [Contacts2] zero-padded,
    the nearest [Contacts2] definition wins over an ancestor's, an
    ancestor's table names faces in folders whose own ini has none
    (downward inheritance — including a photo-less root), and
    UNKNOWN_CONTACT / orphan ids stay unnamed (None). Legacy [Contacts]
    web-id values must never surface as person names."""
    import picasa_db

    cat = scan_library(faces_library)
    a = next(p for p in cat.photos if p.rel == "Trip/a.jpg")
    assert [cid for _r, cid, _n in a.faces] == [
        "0632e71e2ffd6c6d",       # zero-padded from the 15-char faces= id
        "aaaaaaaaaaaaaaa1",
        picasa_db.UNKNOWN_CONTACT,
    ]
    assert [n for _r, _c, n in a.faces] == ["Bob Short", "Ada Local", None]
    assert a.faces[0][0] == picasa_db.parse_rect64("4a8e8e6b")
    assert a.faces[1][0] == picasa_db.parse_rect64("3f845bcb59418507")

    # hidden photos still carry their faces (reveal mode needs them)
    b = next(p for p in cat.photos if p.rel == "Trip/b.jpg")
    assert not b.visible
    assert [n for _r, _c, n in b.faces] == ["Carol Ini", None, None]
    assert "someone_lh,4af3" not in [n for _r, _c, n in b.faces]

    # downward inheritance: Picnic's ini has no [Contacts2]; the ROOT ini
    # (a folder the walk never visits — it holds no photos) names the face.
    c = next(p for p in cat.photos if p.rel == "Picnic/c.jpg")
    assert c.faces == (
        (picasa_db.parse_rect64("2222"), "aaaaaaaaaaaaaaa1", "Ada Ancestor"),
    )

    # the flat registry carries the harvested names
    assert cat.contacts["0632e71e2ffd6c6d"] == "Bob Short"
    assert cat.contacts["cccccccccccccccc"] == "Carol Ini"
    assert "dddddddddddddddd" not in cat.contacts  # [Contacts] has no names
    assert picasa_db.UNKNOWN_CONTACT not in cat.contacts


def test_contacts_xml_wins_name_conflicts(
        faces_library: Path, tmp_path: Path) -> None:
    """spec par.4 precedence: contacts.xml beats [Contacts2] on a name conflict,
    and it is the ONLY namer of a legacy-[Contacts] id; ini-only names
    survive untouched."""
    from catalog import load_contacts_xml

    contacts = load_contacts_xml(
        _write_faces_contacts_xml(tmp_path / "contacts.xml"))
    cat = scan_library(faces_library, None, contacts)

    b = next(p for p in cat.photos if p.rel == "Trip/b.jpg")
    assert [n for _r, _c, n in b.faces] == [
        "Carol Xml",     # xml wins over the ini's 'Carol Ini'
        None,            # orphan: no source names it
        "Dave Legacy",   # nameable only via contacts.xml
    ]
    a = next(p for p in cat.photos if p.rel == "Trip/a.jpg")
    assert a.faces[0][2] == "Bob Short"   # ini-only name unaffected

    assert cat.contacts["cccccccccccccccc"] == "Carol Xml"
    assert cat.contacts["dddddddddddddddd"] == "Dave Legacy"


def test_faces_catalog_roundtrip(faces_library: Path, tmp_path: Path) -> None:
    """Faces + the contact registry survive the persisted catalog
    (CATALOG_VERSION 4): a warm load reproduces every Photo.faces tuple —
    rect fractions are n/65536, exact in JSON — and Catalog.contacts."""
    from catalog import load_contacts_xml

    contacts = load_contacts_xml(
        _write_faces_contacts_xml(tmp_path / "contacts.xml"))
    cat = scan_library(faces_library, None, contacts)
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    loaded = load_catalog(path, faces_library)
    assert loaded is not None
    assert [p.faces for p in loaded.photos] == [p.faces for p in cat.photos]
    assert loaded.contacts == cat.contacts
    assert any(p.faces for p in loaded.photos)  # the fixture isn't vacuous


def test_people_sidebar_filters_and_unnamed(faces_library: Path) -> None:
    """The People sidebar section lists named people with live photo counts
    (visible set only until reveal), clicking one filters the grid exactly
    like an album, and the explicit 'Unnamed faces' affordance surfaces
    photos with suggested/unresolved regions (N7 spirit)."""
    import os
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

    cat = scan_library(faces_library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    # named people with counts; hidden b.jpg's Carol is NOT listed yet
    assert item_for(win, "person", "Ada Local").text(0).endswith("(1)")
    assert item_for(win, "person", "Ada Ancestor").text(0).endswith("(1)")
    assert item_for(win, "person", "Bob Short").text(0).endswith("(1)")
    assert item_for(win, "person", "Carol Ini") is None

    # clicking a person filters the grid like an album does
    win._sidebar_clicked(item_for(win, "person", "Ada Ancestor"), 0)
    assert win.grid.filter_label == "Ada Ancestor"
    assert [cat.photos[i].rel for i in win.grid.display] == ["Picnic/c.jpg"]
    assert "Person “Ada Ancestor”: 1 photos" in win.counts_label.text()

    # unnamed-faces affordance: only visible a.jpg (its UNKNOWN face) so far
    unnamed = item_for(win, "unnamed", "")
    assert unnamed is not None and unnamed.text(0).endswith("(1)")
    win._sidebar_clicked(unnamed, 0)
    assert win.grid.filter_label == "Unnamed faces"
    assert [cat.photos[i].rel for i in win.grid.display] == ["Trip/a.jpg"]

    # live counts: reveal surfaces hidden b.jpg — Carol appears, the
    # unnamed tally grows, and the preserved Unnamed view is recomputed
    win.reveal_box.setChecked(True)
    assert item_for(win, "person", "Carol Ini").text(0).endswith("(1)")
    assert item_for(win, "unnamed", "").text(0).endswith("(2)")
    assert win.grid.filter_label == "Unnamed faces"
    assert sorted(cat.photos[i].rel for i in win.grid.display) == [
        "Trip/a.jpg", "Trip/b.jpg"]

    win.reveal_box.setChecked(False)
    assert item_for(win, "person", "Carol Ini") is None
    assert item_for(win, "unnamed", "").text(0).endswith("(1)")


def test_search_matches_person_names(faces_library: Path) -> None:
    """Person names join the search predicate (spec par.5): a name fragment
    finds every visible photo with a matching named face; hidden photos'
    faces stay out until reveal; no false hits."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    cat = scan_library(faces_library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    win.search.setText("ada")            # Ada Local (a.jpg) + Ada Ancestor (c)
    assert sorted(cat.photos[i].rel for i in win.grid.display) == [
        "Picnic/c.jpg", "Trip/a.jpg"]
    win.search.setText("bob short")
    assert [cat.photos[i].rel for i in win.grid.display] == ["Trip/a.jpg"]
    win.search.setText("carol")          # only on hidden b.jpg
    assert win.grid.display == []
    win.reveal_box.setChecked(True)      # reveal recomputes the search view
    assert [cat.photos[i].rel for i in win.grid.display] == ["Trip/b.jpg"]
    win.reveal_box.setChecked(False)
    win.search.setText("nobody-here")
    assert win.grid.display == []
