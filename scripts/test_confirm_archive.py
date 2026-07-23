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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"] + sys.argv[1:]))
