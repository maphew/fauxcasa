#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pytest",
#   "pillow",
#   "piexif",
#   "av",
# ]
# ///
"""Tests for confirm-archive.py (fauxcasa-ed5.8, M1 gate clause 3 vehicle).

Run:  uv run scripts/test_confirm_archive.py -q

One synthetic picasa-extras corpus (scripts/make-synthetic-library.py
make_extras_library) is generated once per test session and reused by
every test below -- it plays the role of "a real family archive" for a
script whose whole point is to run against one.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import re
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "apps" / "desktop-python"))


def _load(name: str, filename: str):
    """Import a hyphenated-filename script module by path (same technique
    as check-ingest-parity.py's _load_generator)."""
    path = REPO / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ca = _load("confirm_archive", "confirm-archive.py")
msl = _load("make_synthetic_library", "make-synthetic-library.py")

import catalog  # noqa: E402
import picasa_db  # noqa: E402


@pytest.fixture(scope="session")
def corpus(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("confirm-archive-corpus")
    return msl.make_extras_library(root / "corpus")


def _wiring(corpus: Path) -> dict:
    """The corpus's library/contacts/albums/db3 paths, exactly how
    check-ingest-parity.py's run_gate wires scan_library."""
    return {
        "library": corpus / "library",
        "contacts": corpus / "contacts" / "contacts.xml",
        "pal_dir": corpus / "albums",
        "db3_dir": corpus / "db3",
    }


def _snapshot(root: Path) -> list[tuple[str, int, int]]:
    """(relative path, size, mtime_ns) for every file under `root`,
    sorted -- used by the read-only test to prove nothing changed."""
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out.append((str(p.relative_to(root)), st.st_size, st.st_mtime_ns))
    return out


# --------------------------------------------------------------------------
# 1. Green run
# --------------------------------------------------------------------------


def test_green_run_exits_zero_and_reports_pass(corpus, tmp_path, capsys):
    w = _wiring(corpus)
    rc = ca.main([
        str(w["library"]),
        "--contacts", str(w["contacts"]),
        "--pal-dir", str(w["pal_dir"]),
        "--db3", str(w["db3_dir"]),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


# --------------------------------------------------------------------------
# 2. Redaction guarantee
# --------------------------------------------------------------------------


def _sensitive_strings(corpus: Path) -> set[str]:
    """Every real archive string the corpus is known to carry: ini
    caption/keyword values and section names, contacts.xml contact names,
    and folder/file names under library/. Strings shorter than 4 chars are
    dropped by the caller (trivial-collision noise)."""
    out: set[str] = set()
    library = corpus / "library"
    for p in sorted(library.rglob("*")):
        out.add(p.name)
        if p.name.lower() in (".picasa.ini", "picasa.ini") and p.is_file():
            ini = picasa_db.read_picasa_ini(p)
            for sec in ini.sections:
                if sec.name:
                    out.add(sec.name)
                for k, v in sec.items:
                    if k.lower() in ("caption", "keywords"):
                        out.add(v)
    xml_path = corpus / "contacts" / "contacts.xml"
    if xml_path.is_file():
        for m in re.finditer(r'name="([^"]*)"', xml_path.read_text("utf-8")):
            out.add(m.group(1))
    return {s for s in out if len(s) >= 4}


def test_redaction_no_sensitive_string_leaks(corpus, tmp_path, capsys):
    w = _wiring(corpus)
    json_dir = tmp_path / "json-out"
    json_dir.mkdir()
    json_out = json_dir / "report.json"
    rc = ca.main([
        str(w["library"]),
        "--contacts", str(w["contacts"]),
        "--pal-dir", str(w["pal_dir"]),
        "--db3", str(w["db3_dir"]),
        "--json", str(json_out),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    json_text = json_out.read_text("utf-8")

    leaked = []
    for s in _sensitive_strings(corpus):
        if s in out:
            leaked.append(("stdout", s))
        if s in json_text:
            leaked.append(("json", s))
    assert not leaked, f"sensitive strings leaked unredacted: {leaked[:10]}"


# --------------------------------------------------------------------------
# 3. Loss detection
# --------------------------------------------------------------------------


def test_loss_detection_flags_starred_and_photos(corpus):
    w = _wiring(corpus)
    contacts = catalog.load_contacts_xml(w["contacts"])
    cat = catalog.scan_library(
        w["library"], contacts=contacts, pal_dir=w["pal_dir"],
        db3_dir=w["db3_dir"],
    )
    ref = ca.build_reference(
        w["library"].resolve(), w["contacts"], w["pal_dir"], w["db3_dir"])

    # A copy with one starred photo removed. `cat` here is a fresh
    # scan_library() result private to this test (every test re-scans),
    # so this never touches another test's fixture. Removing a list
    # element shifts every later index, so any OTHER photo's
    # stashed_original (an index into this same list) is fixed up too --
    # otherwise compare()'s stashed_originals class could IndexError or
    # silently follow a stale link, independent of what this test asserts.
    victim = next(i for i, p in enumerate(cat.photos) if p.star)
    victim_rel = cat.photos[victim].rel
    mutated_photos = []
    for i, p in enumerate(cat.photos):
        if i == victim:
            continue
        so = p.stashed_original
        if so == victim:
            so = None
        elif so is not None and so > victim:
            so -= 1
        mutated_photos.append(
            p if so == p.stashed_original else dataclasses.replace(p, stashed_original=so)
        )
    mutated_cat = dataclasses.replace(cat, photos=mutated_photos)

    results = ca.compare(ref, mutated_cat)
    by_name = {r.name: r for r in results.rows}

    assert by_name["photos"].verdict == "FAIL"
    assert by_name["starred"].verdict == "FAIL"

    # starred is an aggregate-count class (no per-item ref identity) so it
    # carries no examples at all -- vacuously "only <len= tokens".
    assert by_name["starred"].examples == []
    # photos has computable examples: they must be redacted tokens, never
    # the real rel of the removed photo.
    assert by_name["photos"].examples
    for ex in by_name["photos"].examples:
        assert ex.startswith("<len=")
    assert victim_rel not in by_name["photos"].examples
    joined = " ".join(by_name["photos"].examples)
    assert victim_rel not in joined


# --------------------------------------------------------------------------
# 4. Read-only
# --------------------------------------------------------------------------


def test_read_only_no_file_under_corpus_changes(corpus, tmp_path):
    before = _snapshot(corpus)
    w = _wiring(corpus)
    json_out = tmp_path / "ro-report.json"
    rc = ca.main([
        str(w["library"]),
        "--contacts", str(w["contacts"]),
        "--pal-dir", str(w["pal_dir"]),
        "--db3", str(w["db3_dir"]),
        "--json", str(json_out),
    ])
    after = _snapshot(corpus)
    assert rc == 0
    assert before == after


# --------------------------------------------------------------------------
# 5. json guard
# --------------------------------------------------------------------------


def test_json_inside_library_is_usage_error(corpus, capsys):
    w = _wiring(corpus)
    rc = ca.main([
        str(w["library"]),
        "--json", str(w["library"] / "out.json"),
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert "error" in err.lower()


# --------------------------------------------------------------------------
# 6. Ghost residue still passes (fauxcasa-ed5.8 review blocker)
# --------------------------------------------------------------------------


def test_ghost_residue_still_passes(corpus, tmp_path):
    """A real archive carries clutter catalog.scan_library never reads:
    stale ini sections for deleted/moved files, a description= line that
    happens to live outside the folder's [Picasa] section, and inis in
    photoless folders. None of that should trip a strict FAIL. Exercised
    on a PRIVATE COPY of the corpus so the other tests' pristine session
    fixture is never mutated."""
    ghost_root = tmp_path / "ghost-corpus"
    shutil.copytree(corpus, ghost_root)
    library = ghost_root / "library"

    # 1. A stale section for a deleted/moved file in an existing
    # media-folder ini -- no walked file named "ghost_deleted.jpg" exists,
    # so this must be invisible to every per-photo class.
    beach_ini = library / "2009-07-04 Beach Day" / ".picasa.ini"
    beach_ini.write_text(
        beach_ini.read_text("utf-8")
        + "\r\n[ghost_deleted.jpg]\r\n"
        "star=yes\r\n"
        "caption=Deleted photo ghost caption\r\n"
        "keywords=ghost,deleted\r\n"
        "geotag=1.000000,2.000000\r\n",
        encoding="utf-8",
    )

    # 2. description= living inside an [.album:] section, not [Picasa] --
    # must not count toward folder_descriptions.
    garden_ini = library / "2015-03-15 Garden Project" / ".picasa.ini"
    lines = garden_ini.read_text("utf-8").splitlines()
    header = f"[.album:{msl.ALBUM_DIVERGE}]"
    idx = lines.index(header)
    lines.insert(idx + 1, "description=Ghost album description")
    garden_ini.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

    # 3. A photoless subfolder carrying a [Contacts2] id and an [.album:]
    # definition -- never an ancestor of any media folder, so both must
    # stay outside contacts_ini_present/album_definitions' scope.
    ghost_dir = library / "Ghost Folder No Photos"
    ghost_dir.mkdir()
    (ghost_dir / ".picasa.ini").write_text(
        "[Contacts2]\r\n"
        "abcdefabcdefabcd=Ghost Person;;\r\n"
        "[.album:e5f60718293a4b5c6d7e8f90a1b2c3d4]\r\n"
        "name=Ghost Album\r\n",
        encoding="utf-8",
    )

    rc = ca.main([
        str(library),
        "--contacts", str(ghost_root / "contacts" / "contacts.xml"),
        "--pal-dir", str(ghost_root / "albums"),
        "--db3", str(ghost_root / "db3"),
    ])
    assert rc == 0


# --------------------------------------------------------------------------
# 7. Predicate anchoring
# --------------------------------------------------------------------------


def test_scoped_tallies_match_survey_on_pristine_corpus(corpus):
    """On the PRISTINE corpus every ini section names a real file/folder
    (1:1), so this gate's own scoped tallies (confirm_archive.
    _scoped_ini_survey, restricted to what catalog.scan_library would ever
    read) and picasa_db._survey_ini_tree's tree-wide rollup must agree
    exactly for every class this gate maps onto the survey's vocabulary --
    anchoring the scoped predicates against the survey's so any predicate
    drift between the two fails loudly instead of silently."""
    library = corpus / "library"
    ref = ca.build_reference(
        library.resolve(), corpus / "contacts" / "contacts.xml",
        corpus / "albums", corpus / "db3")
    survey = picasa_db._survey_ini_tree(library)
    survey_feats = survey["features"]
    survey_keys = survey["keys"]

    for name in ("starred", "captioned", "keyworded", "geotagged",
                 "face_tags", "album_memberships"):
        assert ref.feats.get(name, 0) == survey_feats.get(name, 0), name
    # The gate counts edit_keys_other directly (non-rotate recipe key
    # lines); the survey folds rotate= into "edit_keys" -- same identity
    # the parity gate uses, valid while the pristine corpus's rotate
    # values are all well-formed and nonzero.
    assert ref.feats.get("edit_keys_other", 0) == (
        survey_feats.get("edit_keys", 0) - survey_keys.get("rotate", 0))
    for name in ("hidden", "rotate", "p2category", "description"):
        assert ref.keys.get(name, 0) == survey_keys.get(name, 0), name


# --------------------------------------------------------------------------
# 8. Exception redaction
# --------------------------------------------------------------------------


def test_exception_path_never_leaks_archive_strings(corpus, monkeypatch, capsys):
    w = _wiring(corpus)

    def _raise(*_args, **_kwargs):
        raise OSError("C:/SECRETPATH/thumbindex.db")

    monkeypatch.setattr(picasa_db, "read_thumbindex", _raise)

    rc = ca.main([
        str(w["library"]),
        "--contacts", str(w["contacts"]),
        "--pal-dir", str(w["pal_dir"]),
        "--db3", str(w["db3_dir"]),
    ])
    captured = capsys.readouterr()
    assert rc == 3
    assert "SECRETPATH" not in captured.out
    assert "SECRETPATH" not in captured.err


def test_duplicate_section_counts_once(corpus, tmp_path):
    """Real archives grow DUPLICATE sections for the same photo (that is
    why catalog._section_map merges by concatenation at all). The merged
    section then carries duplicate key lines; the tracer reads each
    single-value key FIRST-WINS via .get(), so the reference must too --
    per-LINE tallying would count one captioned/starred photo twice and
    false-FAIL. Regression for the _first() first-wins rule in
    _scoped_ini_survey (a per-line implementation fails this test)."""
    dup_root = tmp_path / "dup-corpus"
    shutil.copytree(corpus, dup_root)
    library = dup_root / "library"
    beach_ini = library / "2009-07-04 Beach Day" / ".picasa.ini"
    # A second section for an EXISTING walked photo, duplicating
    # single-value keys its first section may already carry.
    beach_ini.write_text(
        beach_ini.read_text("utf-8")
        + "\r\n[photo01.jpg]\r\n"
        "star=yes\r\n"
        "caption=Duplicate-section caption\r\n"
        "keywords=dup,section\r\n",
        encoding="utf-8",
    )
    rc = ca.main([
        str(library),
        "--contacts", str(dup_root / "contacts" / "contacts.xml"),
        "--pal-dir", str(dup_root / "albums"),
        "--db3", str(dup_root / "db3"),
    ])
    assert rc == 0


def test_value_predicates_mirror_tracer(corpus, tmp_path):
    """Keys whose VALUES the tracer legitimately drops -- hidden=no,
    rotate=rotate(0), a malformed geotag=, a P2category that is not
    "Hidden Folders", an empty description= line -- must not count on the
    reference side either (second review round: a presence-only reference
    predicate false-FAILed every one of these). Built as a NEW media
    folder + copied photo so no first-wins shadowing from existing
    sections can mask a key."""
    val_root = tmp_path / "value-corpus"
    shutil.copytree(corpus, val_root)
    library = val_root / "library"
    beach = library / "2009-07-04 Beach Day"
    src = next(beach.glob("*.jpg"))
    vdir = library / "Value Predicates"
    vdir.mkdir()
    shutil.copy2(src, vdir / "valcheck.jpg")
    (vdir / ".picasa.ini").write_text(
        "[Picasa]\r\n"
        "P2category=Screensaver\r\n"
        "description=\r\n"
        "[valcheck.jpg]\r\n"
        "hidden=no\r\n"
        "rotate=rotate(0)\r\n"
        "geotag=999.000000,999.000000\r\n",
        encoding="utf-8",
    )
    rc = ca.main([
        str(library),
        "--contacts", str(val_root / "contacts" / "contacts.xml"),
        "--pal-dir", str(val_root / "albums"),
        "--db3", str(val_root / "db3"),
    ])
    assert rc == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"] + sys.argv[1:]))
