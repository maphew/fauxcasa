"""pytest conftest for apps/desktop-python/tests/ (fauxcasa-l09).

sys.path bootstrap plus every named + autouse fixture from the
pre-split test_tracer.py, in original order."""

from __future__ import annotations

import sys

from tracer_helpers import APP_DIR

sys.path.insert(0, str(APP_DIR))

import os
from pathlib import Path
import pytest
from tracer_helpers import (
    CID_DB3,
    UID_DEF,
    UID_GHOST,
    VIEW_SPEC_ALBUM_UID,
    WHITEHORSE,
    _meta_jpeg,
    _sweep_qt_widgets,
    make_jpeg,
)


@pytest.fixture(scope="session", autouse=True)
def _pin_decode_sandbox_off():
    """fauxcasa-ez2.9 Stage 1: decodefacade.sandbox_mode() already
    defaults to "0" whenever "pytest" is in sys.modules, but this pins
    FAUXCASA_DECODE_SANDBOX=0 explicitly (belt-and-suspenders, per the
    lens plan) for the whole session -- this suite calls build_cache
    ~86 times and load_original ~29 times; once Stage 2 wires those call
    sites onto decodefacade, spawning a real AppContainer worker per call
    would blow tracer.yml's 15-minute timeout and cannot run at all on
    Linux CI. Restores whatever was there before (normally unset) on
    teardown."""
    prev = os.environ.get("FAUXCASA_DECODE_SANDBOX")
    os.environ["FAUXCASA_DECODE_SANDBOX"] = "0"
    yield
    if prev is None:
        os.environ.pop("FAUXCASA_DECODE_SANDBOX", None)
    else:
        os.environ["FAUXCASA_DECODE_SANDBOX"] = prev


@pytest.fixture(autouse=True)
def _isolate_qt_per_test():
    """Per-test Qt isolation: run ``_sweep_qt_widgets`` (see its docstring
    for why) after every test so accumulated decode threads and undeleted
    widgets from the whole session don't surface as a flaky native access
    violation in a later paint-heavy test (test_reveal_*'s _toggle_reveal) —
    the crash is cumulative state, not an active worker race (fauxcasa-gfz)."""
    yield
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    _sweep_qt_widgets(app)


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    make_jpeg(root / "2020-01-01 Trip" / "a.jpg")
    make_jpeg(root / "2020-01-01 Trip" / "b.jpg", 48, 64)
    make_jpeg(root / "2020-01-01 Trip" / ".picasaoriginals" / "a.jpg")
    make_jpeg(root / "2021-05-05 Picnic" / "c.jpg")
    (root / "2020-01-01 Trip" / ".picasa.ini").write_text(
        "[Picasa]\r\nname=Trip!\r\ndescription=fun\r\n"
        "[a.jpg]\r\nstar=yes\r\ncaption=the beach\r\n"
        "keywords=sun, sand\r\nrotate=rotate(1)\r\n"
        "albums=deadbeefdeadbeefdeadbeefdeadbeef\r\n"
        "[.album:deadbeefdeadbeefdeadbeefdeadbeef]\r\n"
        "name=Best Of\r\ntoken=deadbeefdeadbeefdeadbeefdeadbeef\r\n"
        "[b.jpg]\r\nhidden=yes\r\n"
    )
    return root


@pytest.fixture()
def reveal_library(tmp_path: Path) -> Path:
    """A library whose one folder holds a visible starred photo and a
    hidden=yes STARRED photo, so reveal mode changes BOTH a rendered
    per-folder count (1 -> 2) and the Starred tally (1 -> 2) — exercising
    _build_sidebar's fcount() and its (visible or reveal) star branch."""
    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "shown.jpg")
    make_jpeg(root / "Trip" / "secret.jpg")
    (root / "Trip" / ".picasa.ini").write_text(
        "[shown.jpg]\r\nstar=yes\r\n"
        "[secret.jpg]\r\nstar=yes\r\nhidden=yes\r\n"
    )
    return root


@pytest.fixture()
def folder_hidden_library(tmp_path: Path) -> Path:
    """A normal folder tagged with the sibling category (P2category=Folders
    on Disk) plus a folder placed in Picasa's built-in 'Hidden Folders'
    collection ([Picasa] P2category=Hidden Folders), which hides the WHOLE
    folder — every photo under it, mirroring per-photo hidden=yes."""
    root = tmp_path / "lib"
    make_jpeg(root / "2020 Trip" / "a.jpg")
    (root / "2020 Trip" / ".picasa.ini").write_text(
        "[Picasa]\r\nname=2020 Trip\r\nP2category=Folders on Disk\r\n")
    make_jpeg(root / "2021 Secret" / "s1.jpg")
    make_jpeg(root / "2021 Secret" / "s2.jpg")
    (root / "2021 Secret" / ".picasa.ini").write_text(
        "[Picasa]\r\nname=2021 Secret\r\nP2category=Hidden Folders\r\n")
    return root


@pytest.fixture()
def search_library(tmp_path: Path) -> Path:
    """Distinct vocabulary per field so each test proves WHICH field matched:
    'beach'/'city'/'osaka' appear only in folder names, 'ocean'/'sand'/'neon'
    only in keywords, 'golden'/'stalls' only in captions, and
    'sunset'/'dunes'/'market'/'street' only in filenames. Osaka is nested
    under 2021 City to exercise rel-path-segment matching."""
    root = tmp_path / "lib"
    make_jpeg(root / "2020 Beach Trip" / "sunset.jpg")
    make_jpeg(root / "2020 Beach Trip" / "dunes.jpg")
    make_jpeg(root / "2021 City" / "market.jpg")
    make_jpeg(root / "2021 City" / "Osaka" / "street.jpg")
    (root / "2020 Beach Trip" / ".picasa.ini").write_text(
        "[sunset.jpg]\r\ncaption=Golden hour\r\nkeywords=sun, ocean\r\n"
        "[dunes.jpg]\r\nkeywords=sand\r\n")
    (root / "2021 City" / ".picasa.ini").write_text(
        "[market.jpg]\r\ncaption=night stalls\r\nkeywords=food, neon\r\n")
    return root


@pytest.fixture()
def faces_library(tmp_path: Path) -> Path:
    """Faces fixture exercising the whole ingest matrix: a photo-less ROOT
    ini whose [Contacts2] flows downward; a Trip ini that re-names the
    ancestor's contact (nearest definition wins), defines a zero-padded
    short id and a name that conflicts with contacts.xml, and carries a
    legacy [Contacts] entry (web ids, never display names); an UNKNOWN
    (unconfirmed-suggestion) face; an orphan id nobody names; and a hidden
    photo so People counts prove they are visible-set-aware."""
    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "a.jpg")
    make_jpeg(root / "Trip" / "b.jpg")
    make_jpeg(root / "Picnic" / "c.jpg")
    (root / ".picasa.ini").write_text(
        "[Contacts2]\r\naaaaaaaaaaaaaaa1=Ada Ancestor;;\r\n")
    (root / "Trip" / ".picasa.ini").write_text(
        "[Contacts2]\r\n"
        "0632e71e2ffd6c6d=Bob Short;;\r\n"
        "cccccccccccccccc=Carol Ini;;\r\n"
        "aaaaaaaaaaaaaaa1=Ada Local;;\r\n"   # overrides the root's name here
        "[Contacts]\r\n"
        "dddddddddddddddd=someone_lh,4af3\r\n"
        "[a.jpg]\r\n"
        # short (%llx-stripped) id + inherited-and-overridden id + an
        # unconfirmed suggestion (UNKNOWN_CONTACT)
        "faces=rect64(4a8e8e6b),632e71e2ffd6c6d;"
        "rect64(3f845bcb59418507),aaaaaaaaaaaaaaa1;"
        "rect64(ff),ffffffffffffffff\r\n"
        "[b.jpg]\r\n"
        "hidden=yes\r\n"
        # xml-conflict id + an orphan nobody names + the legacy-[Contacts] id
        "faces=rect64(1234),cccccccccccccccc;"
        "rect64(5678),9999999999999999;"
        "rect64(9abc),dddddddddddddddd\r\n"
    )
    (root / "Picnic" / ".picasa.ini").write_text(
        "[c.jpg]\r\nfaces=rect64(2222),aaaaaaaaaaaaaaa1\r\n")
    return root


@pytest.fixture()
def metadata_library(tmp_path: Path) -> Path:
    """One folder, four precedence cases (§4 tier-1 / §3 star authority):
      a.jpg  in-file EXIF GPS + Rating 3 + date; ini geotag= + star=yes
             -> in-file wins everything
      b.jpg  no in-file metadata; ini geotag= + star=yes
             -> ini fallback holds after indexing
      c.jpg  in-file Rating 2 only; no ini
             -> Rating alone sets the count
      d.jpg  in-file Rating 0; ini star=yes
             -> ini stays authoritative for zero-vs-nonzero (still 1)"""
    root = tmp_path / "mlib"
    _meta_jpeg(root / "f" / "a.jpg",
               date_time_original="1899:03:02 14:00:00",
               gps=WHITEHORSE, rating=3)
    make_jpeg(root / "f" / "b.jpg")
    _meta_jpeg(root / "f" / "c.jpg", rating=2)
    _meta_jpeg(root / "f" / "d.jpg", rating=0)
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\nstar=yes\r\ngeotag=-33.856800,151.215300\r\n"
        "[b.jpg]\r\nstar=yes\r\ngeotag=-33.856800,151.215300\r\n"
        "[d.jpg]\r\nstar=yes\r\n"
    )
    return root


@pytest.fixture()
def album_library(tmp_path: Path) -> Path:
    """Three photos in one folder: an ini-defined album holding a+b, and a
    GHOST uid referenced from b+c with no [.album:] definition anywhere."""
    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "a.jpg")
    make_jpeg(root / "Trip" / "b.jpg")
    make_jpeg(root / "Trip" / "c.jpg")
    (root / "Trip" / ".picasa.ini").write_text(
        f"[.album:{UID_DEF}]\r\n"
        "name=Defined\r\n"
        f"token=]album:{UID_DEF}\r\n"
        "[a.jpg]\r\n"
        f"albums={UID_DEF}\r\n"
        "[b.jpg]\r\n"
        f"albums={UID_DEF},{UID_GHOST}\r\n"
        "[c.jpg]\r\n"
        f"albums={UID_GHOST}\r\n")
    return root


@pytest.fixture()
def db3_library(tmp_path: Path) -> Path:
    """One folder, two photos: a.jpg carries an ini faces= region whose
    contact id NO ini/contacts.xml source names — the exact gap the db3
    person-album rescue exists to fill; b.jpg carries no faces= at all."""
    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "a.jpg")
    make_jpeg(root / "Trip" / "b.jpg")
    (root / "Trip" / ".picasa.ini").write_text(
        "[a.jpg]\r\n"
        f"faces=rect64(6800600097ff9fff),{CID_DB3}\r\n")
    return root


@pytest.fixture()
def view_spec_library(tmp_path: Path) -> Path:
    """Covers every --view SPEC kind with a DISTINCT result set each, so a
    test can tell "matched the right kind" from "matched by accident":
    a.jpg is the lone album member AND the lone named-person photo; b.jpg
    carries only an unnamed (UNKNOWN_CONTACT) face; d.jpg is the lone
    starred photo; Control/c.jpg has none of the above (a plain control).
    Synthetic .picasa.ini only — no real Picasa data."""
    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "a.jpg")
    make_jpeg(root / "Trip" / "b.jpg")
    make_jpeg(root / "Trip" / "d.jpg")
    make_jpeg(root / "Control" / "c.jpg")
    (root / "Trip" / ".picasa.ini").write_text(
        "[Contacts2]\r\naaaaaaaaaaaaaaa1=Ada Example;;\r\n"
        f"[.album:{VIEW_SPEC_ALBUM_UID}]\r\n"
        f"name=Best Of\r\ntoken={VIEW_SPEC_ALBUM_UID}\r\n"
        "[a.jpg]\r\n"
        f"albums={VIEW_SPEC_ALBUM_UID}\r\n"
        "faces=rect64(4a8e8e6b),aaaaaaaaaaaaaaa1\r\n"
        "[b.jpg]\r\nfaces=rect64(ff),ffffffffffffffff\r\n"
        "[d.jpg]\r\nstar=yes\r\n"
    )
    return root
