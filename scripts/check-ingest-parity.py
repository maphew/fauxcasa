#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pillow",
#   "piexif",
# ]
# ///
"""M1 ingest-loss gate (fauxcasa-ed5.1): survey/tracer cross-check.

Spec §9, M1 gate clause 2: "`picasa_db.py survey` cross-check shows zero
ingest loss on synthetic corpora." This harness generates (or takes) a
synthetic picasa-extras corpus (scripts/make-synthetic-library.py
--picasa-extras), runs BOTH readers over the same tree --

- the survey rollup (scripts/picasa_db.py `_survey_ini_tree`), plus this
  harness's own filesystem counts for what the ini survey cannot see
  (photo files, .pal albums, contacts.xml), plus the generator's
  manifest.json ground truth;
- the tracer's `catalog.scan_library` (apps/desktop-python/catalog.py,
  public API only -- this script never modifies the tracer)

-- and diffs feature counts per ingest class. Designed to land GREEN and
RATCHET: classes the tracer does not yet ingest are marked
expected-missing with the owning bead id, and the harness FAILS on

(a) LOSS/mismatch in a class marked ingested,
(b) a class the tracer now ingests but the table still marks
    expected-missing (forces the table to ratchet forward),
(c) a class the corpus no longer exercises (reference count 0), and
(d) generator/survey disagreement (manifest vs survey rollup), so a bug
    in either reference side fails loudly instead of masking loss.

Usage::

    uv run scripts/check-ingest-parity.py            # temp corpus, auto-clean
    uv run scripts/check-ingest-parity.py --corpus cache/synthetic-library-extras
    uv run scripts/check-ingest-parity.py --keep     # print + keep temp corpus

Exit code 0 = zero ingest loss (per the table below); 1 = gate failure.

NOTE: Python here is research/gate tooling per the repo script conventions;
it does not decide the application implementation language.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "apps" / "desktop-python"))

import picasa_db  # noqa: E402
from catalog import Catalog, Photo, load_contacts_xml, scan_library  # noqa: E402

# Image extensions the tracer walks (catalog.EXTS is the tracer's rule;
# imported, not copied, so the photo count follows the tracer's walk).
from catalog import EXTS  # noqa: E402


def _load_generator():
    """Import make-synthetic-library.py (hyphenated name) as a module."""
    path = REPO / "scripts" / "make-synthetic-library.py"
    spec = importlib.util.spec_from_file_location("make_synthetic_library", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # required before exec (dataclasses etc.)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# Reference side: survey rollup + filesystem counts + manifest ground truth
# --------------------------------------------------------------------------


@dataclass
class Reference:
    survey: dict[str, Any]        # picasa_db._survey_ini_tree(library)
    photos_fs: int                # image files under library/ (tracer walk rule)
    folders_fs: int               # dirs containing >=1 image file
    pal_files: int                # albums/*.pal
    contacts_xml: int             # <contact> entries in contacts/contacts.xml
    manifest: dict[str, Any]      # generator ground truth


def build_reference(corpus: Path) -> Reference:
    library = corpus / "library"
    survey = picasa_db._survey_ini_tree(library)
    images = [
        p for p in library.rglob("*")
        if p.suffix.lower() in EXTS and p.is_file()
    ]
    contacts_xml = 0
    cx = corpus / "contacts" / "contacts.xml"
    if cx.is_file():
        contacts_xml = len(re.findall(r"<contact\b", cx.read_text("utf-8")))
    manifest = json.loads((corpus / "manifest.json").read_text("utf-8"))
    return Reference(
        survey=survey,
        photos_fs=len(images),
        folders_fs=len({p.parent for p in images}),
        pal_files=len(list((corpus / "albums").glob("*.pal"))),
        contacts_xml=contacts_xml,
        manifest=manifest,
    )


# --------------------------------------------------------------------------
# Tracer side: counts from the public Catalog, with RATCHET PROBES.
#
# A probe returns None while the tracer has no representation for the
# class ("expected-missing" holds) and an int once it does. Probes look
# for dataclass fields by candidate names and for behavioral evidence
# (e.g. a .pal-only album uid appearing in catalog.albums), so the gate
# flips to "ratchet forward" the moment fauxcasa-cam.* ingest work lands
# -- even in a parallel branch -- rather than silently staying green.
# --------------------------------------------------------------------------


def _photo_field(name_candidates: tuple[str, ...]) -> str | None:
    for n in name_candidates:
        if n in Photo.__dataclass_fields__:
            return n
    return None


def _catalog_field(name_candidates: tuple[str, ...]) -> str | None:
    for n in name_candidates:
        if n in Catalog.__dataclass_fields__:
            return n
    return None


def _count_photo_values(cat: Catalog, field: str) -> int:
    n = 0
    for p in cat.photos:
        v = getattr(p, field, None)
        if v is None or v == () or v == "" or v == 0 or v is False:
            continue
        n += len(v) if isinstance(v, (tuple, list)) else 1
    return n


def probe_pal(cat: Catalog, ref: Reference) -> int | None:
    """Behavioral: the corpus ships one album that exists ONLY as a .pal
    file; if its uid shows up in catalog.albums, .pal ingest has landed."""
    pal_only = ref.manifest["uids"]["pal_only"]
    if pal_only in cat.albums:
        return sum(1 for u in ref.manifest["uids"]["pal_files"]
                   if u in cat.albums)
    return None


def probe_placeholder_albums(cat: Catalog, ref: Reference) -> int | None:
    """Behavioral: albums= tokens naming a uid with no definition anywhere
    (the placeholder-album case, fauxcasa-cam.13). Ingested when the
    unknown uid materializes as a catalog album."""
    unknown = ref.manifest["uids"]["unknown"]
    return 1 if unknown in cat.albums else None


def probe_edit_keys(cat: Catalog, ref: Reference) -> int | None:
    f = _photo_field(("filters", "edits", "edit_stack", "crop"))
    return _count_photo_values(cat, f) if f else None


# --------------------------------------------------------------------------
# The expectation table -- THE ratchet artifact.
#
# To ratchet: when ingest work lands and the harness fails with "now
# ingested", flip `owner=None` -> the class moves to the ingested block
# (set its tracer fn to the real count and drop the probe).
# --------------------------------------------------------------------------


@dataclass
class ParityClass:
    name: str
    reference: Callable[[Reference], int]  # ground-truth count
    tracer: Callable[[Catalog, Reference], int | None]  # None = no ingest yet
    owner: str | None = None  # owning bead id => expected-missing
    manifest_key: str | None = None  # cross-check key in manifest["expected"]

    @property
    def ingested(self) -> bool:
        return self.owner is None


def _feat(key: str) -> Callable[[Reference], int]:
    return lambda ref: ref.survey["features"].get(key, 0)


def _key(key: str) -> Callable[[Reference], int]:
    return lambda ref: ref.survey["keys"].get(key, 0)


CLASSES: list[ParityClass] = [
    # ---- ingested today: zero loss REQUIRED --------------------------------
    ParityClass(
        "photos", lambda r: r.photos_fs,
        lambda c, r: len(c.photos), manifest_key="photos"),
    ParityClass(
        "folders", lambda r: r.folders_fs,
        lambda c, r: len(c.folders), manifest_key="folders"),
    ParityClass(
        "starred", _feat("starred"),
        lambda c, r: sum(1 for p in c.photos if p.star),
        manifest_key="starred"),
    ParityClass(
        "captioned", _feat("captioned"),
        lambda c, r: sum(1 for p in c.photos if p.caption),
        manifest_key="captioned"),
    ParityClass(
        "keyworded", _feat("keyworded"),
        lambda c, r: sum(1 for p in c.photos if p.keywords),
        manifest_key="keyworded"),
    ParityClass(
        "hidden_photos", _key("hidden"),
        lambda c, r: sum(1 for p in c.photos if p.hidden),
        manifest_key="hidden_photos"),
    ParityClass(
        "hidden_folders", _key("p2category"),
        lambda c, r: sum(1 for f in c.folders.values() if f.folder_hidden),
        manifest_key="hidden_folders"),
    ParityClass(
        "rotated", _key("rotate"),
        lambda c, r: sum(1 for p in c.photos if p.rotate),
        manifest_key="rotated"),
    ParityClass(
        "album_definitions",
        lambda r: r.survey["sections"].get("album", 0),
        # only ini-defined albums count here; .pal/placeholder uids have
        # their own classes below (their arrival must ratchet, not skew this)
        lambda c, r: sum(1 for u in c.albums
                         if u in r.manifest["uids"]["ini_defined"]),
        manifest_key="album_definitions"),
    ParityClass(
        "album_memberships", _feat("album_memberships"),
        lambda c, r: sum(len(p.albums) for p in c.photos),
        manifest_key="album_memberships"),
    ParityClass(
        "folder_descriptions", _key("description"),
        lambda c, r: sum(1 for f in c.folders.values() if f.description),
        manifest_key="folder_descriptions"),
    # ---- faces/contacts: ingested by PR #37 (fauxcasa-cam.1/.2) ------------
    ParityClass(
        "face_tags", _feat("face_tags"),
        lambda c, r: sum(len(p.faces) for p in c.photos),
        manifest_key="face_tags"),
    ParityClass(
        # "named" means named AFTER the §4 merge (contacts.xml ∪ [Contacts2]),
        # which the ini-only survey cannot express (its face_tags_named counts
        # non-sentinel ids, i.e. nameable — a different notion). The manifest
        # ground truth is the reference; the two unnamed faces are the
        # ffffffffffffffff suggestion and the orphaned id.
        "face_tags_named",
        lambda r: r.manifest["expected"]["face_tags_named"],
        lambda c, r: sum(1 for p in c.photos
                         for _rect, _cid, name in p.faces if name),
        manifest_key="face_tags_named"),
    ParityClass(
        # every [Contacts2] id survives into the merged registry
        "contacts_ini", _feat("contacts"),
        lambda c, r: sum(1 for i in r.manifest["contacts_ini_ids"]
                         if i in c.contacts),
        manifest_key="contacts_ini"),
    ParityClass(
        # every contacts.xml id survives into the merged registry
        "contacts_xml", lambda r: r.contacts_xml,
        lambda c, r: sum(1 for i in r.manifest["contacts_xml_ids"]
                         if i in c.contacts),
        manifest_key="contacts_xml"),
    ParityClass(
        # the merged registry as a whole: ini ∪ xml, no dupes, no strays
        "contacts_registry",
        lambda r: r.manifest["expected"]["contacts_registry"],
        lambda c, r: len(c.contacts),
        manifest_key="contacts_registry"),
    # ---- geotags: ingested by fauxcasa-cam.9/.10/.11 PR --------------------
    ParityClass(
        # ini geotag= keys at scan; in-file EXIF GPS overrides at index.
        # This gate runs scan_library only (no indexer) and the extras
        # corpus's geotags are ini-only (no EXIF GPS), so the scan-level
        # count matches the survey's geotag= key count exactly.
        "geotagged", _feat("geotagged"),
        lambda c, r: sum(1 for p in c.photos if p.geotag is not None),
        manifest_key="geotagged"),
    # ---- expected-missing: owned, probed, ratchets forward -----------------
    ParityClass(
        "pal_albums", lambda r: r.pal_files, probe_pal,
        owner="fauxcasa-cam.8", manifest_key="pal_albums"),
    ParityClass(
        "placeholder_albums",
        lambda r: r.manifest["expected"]["placeholder_album_uids"],
        probe_placeholder_albums,
        owner="fauxcasa-cam.13", manifest_key="placeholder_album_uids"),
    ParityClass(
        "edit_keys_other",
        # survey's edit_keys folds in rotate=, which IS ingested; subtract it
        lambda r: (r.survey["features"].get("edit_keys", 0)
                   - r.survey["keys"].get("rotate", 0)),
        probe_edit_keys,
        owner="fauxcasa-cam.15", manifest_key="edit_keys_other"),
]


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def run_gate(corpus: Path) -> int:
    ref = build_reference(corpus)
    # The gate scans the way the app does: contacts.xml threaded in when the
    # corpus ships one (fauxcasa-cam.2's --contacts discovery, made explicit).
    cx = corpus / "contacts" / "contacts.xml"
    contacts = load_contacts_xml(cx) if cx.is_file() else {}
    cat = scan_library(corpus / "library", contacts=contacts)
    expected = ref.manifest.get("expected", {})

    failures: list[str] = []
    rows: list[tuple[str, str, str, str, str]] = []
    for cls in CLASSES:
        ref_n = cls.reference(ref)
        tr = cls.tracer(cat, ref)
        status = "ingested" if cls.ingested else f"expected-missing ({cls.owner})"
        verdict = "ok"

        # (c) the corpus must exercise every class in the table
        if ref_n <= 0:
            verdict = "FAIL corpus"
            failures.append(
                f"{cls.name}: corpus does not exercise this class "
                f"(reference count {ref_n}) -- fix the generator or drop the row")
        # (d) generator vs survey/reference reader agreement
        if cls.manifest_key is not None and cls.manifest_key in expected \
                and ref_n != expected[cls.manifest_key]:
            verdict = "FAIL reference"
            failures.append(
                f"{cls.name}: reference readers disagree -- survey/fs say "
                f"{ref_n}, manifest ground truth says "
                f"{expected[cls.manifest_key]}")
        if cls.ingested:
            # (a) loss (or excess -- both are parity failures) in an
            # ingested class
            if tr is None:
                verdict = "FAIL loss"
                failures.append(
                    f"{cls.name}: marked ingested but the tracer produced "
                    f"no value")
            elif tr != ref_n:
                verdict = "FAIL loss" if tr < ref_n else "FAIL excess"
                failures.append(
                    f"{cls.name}: ingest {'loss' if tr < ref_n else 'excess'}"
                    f" -- reference {ref_n}, tracer {tr}")
        else:
            # (b) the ratchet: ingest landed, table must move forward
            if tr is not None:
                verdict = "FAIL ratchet"
                failures.append(
                    f"{cls.name}: the tracer now ingests this class "
                    f"(count {tr}) but the table still marks it "
                    f"expected-missing under {cls.owner} -- move it to the "
                    f"ingested block (and close/annotate the bead)")
        rows.append((cls.name, str(ref_n), "-" if tr is None else str(tr),
                     status, verdict))

    w = [max(len(r[i]) for r in rows + [("class", "ref", "tracer",
                                         "status", "verdict")])
         for i in range(5)]
    header = ("class", "reference", "tracer", "status", "verdict")
    w = [max(w[i], len(header[i])) for i in range(5)]
    print(f"ingest-parity gate over {corpus}")
    print("  ".join(h.ljust(w[i]) for i, h in enumerate(header)))
    print("  ".join("-" * w[i] for i in range(5)))
    for r in rows:
        print("  ".join(r[i].ljust(w[i]) for i in range(5)))

    if failures:
        print(f"\nFAIL: {len(failures)} problem(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    n_ing = sum(1 for c in CLASSES if c.ingested)
    print(f"\nPASS: zero ingest loss across {n_ing} ingested classes; "
          f"{len(CLASSES) - n_ing} expected-missing classes are owned and "
          f"probed for ratchet")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--corpus", default=None, metavar="DIR",
        help="existing picasa-extras corpus root (contains library/ + "
        "manifest.json); default: generate a fresh one in a temp dir")
    ap.add_argument(
        "--keep", action="store_true",
        help="generate into cache/synthetic-library-extras and keep it "
        "instead of a temp dir")
    args = ap.parse_args(argv)

    if args.corpus is not None:
        corpus = Path(args.corpus)
        if not (corpus / "manifest.json").is_file():
            print(f"error: {corpus} is not a picasa-extras corpus "
                  f"(no manifest.json)", file=sys.stderr)
            return 2
        return run_gate(corpus)

    gen = _load_generator()
    if args.keep:
        return run_gate(gen.make_extras_library())
    with tempfile.TemporaryDirectory(prefix="fauxcasa-parity-") as td:
        return run_gate(gen.make_extras_library(Path(td) / "corpus"))


if __name__ == "__main__":
    sys.exit(main())
