#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pillow",
#   "piexif",
#   "av",
# ]
# ///
"""M1 gate clause 3 vehicle (fauxcasa-ed5.8): owner-runnable survey/ingest
cross-check against a REAL Picasa family archive.

Spec §9, M1 gate clause 3: "owner confirms the same on the family archive"
(docs/product-spec.md ~line 878). scripts/check-ingest-parity.py proves
zero ingest loss on SYNTHETIC corpora against a generator manifest; a real
archive has no manifest, so this is its manifest-free sibling -- every
comparison class computes BOTH the reference (survey/filesystem) side and
the tracer (apps/desktop-python/catalog.scan_library) side independently
from disk, then diffs counts. It never writes anywhere: scan_library and
every reference-side reader used here are pure reads (verified by
skimming catalog.py/db3rescue.py -- the only writes in catalog.py are in
save_catalog/save_report, never called from scan_library's call graph).

PRIVACY CONTRACT -- always redacted, no opt-out: every string that
originates from the archive (rel paths, uids, contact ids, the LIBRARY
path itself) is rendered through picasa_db._redact_str -- `<len=N
sha1=8hex>` -- before it is ever printed or written to --json. Dates are
never printed. This is safe to run against a real family archive and to
paste the output into an issue or share with a maintainer.

Usage::

    uv run scripts/confirm-archive.py D:\\Photos
    uv run scripts/confirm-archive.py D:\\Photos --picasa-home D:\\PicasaAppData\\Google
    uv run scripts/confirm-archive.py D:\\Photos --contacts C:\\...\\contacts.xml \\
        --pal-dir C:\\...\\Picasa2Albums --db3 C:\\...\\db3 --json out\\report.json

Exit codes: 0 = every strict class matches (PASS); 1 = strict mismatch
(FAIL); 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "apps" / "desktop-python"))

import picasa_db  # noqa: E402
from catalog import (  # noqa: E402
    Catalog,
    EXTS,
    _harvest_contacts2,
    load_contacts_xml,
    read_pal_dir,
    scan_library,
)
# db3 rescue plumbing (fauxcasa-cam.6): the same machine-path translator
# scan_library's own rescue_people uses, reused here so the reference-side
# db3 join follows the exact same rule as the tracer.
from db3rescue import translate_db3_path  # noqa: E402
from videoload import VIDEO_EXTS  # noqa: E402

_redact = picasa_db._redact_str

DEFAULT_MAX_DETAIL = 10


# --------------------------------------------------------------------------
# --picasa-home derivation
# --------------------------------------------------------------------------


def derive_picasa_home(home: Path) -> tuple[Path | None, Path | None, Path | None]:
    """(contacts.xml, Picasa2Albums dir, db3 dir) under a copied Picasa
    app-data area, mirroring apps/desktop-python/catalog.default_contacts_xml
    / default_pal_dir and db3rescue.default_db3_dir -- those default to
    %LocalAppData%\\Google\\Picasa2\\{contacts\\contacts.xml,db3} and
    %LocalAppData%\\Google\\Picasa2Albums; `home` here plays the role of
    %LocalAppData%\\Google. Each path is returned only if it exists on
    disk -- a copied app-data area legitimately carries a subset."""
    contacts = home / "Picasa2" / "contacts" / "contacts.xml"
    pal_dir = home / "Picasa2Albums"
    db3_dir = home / "Picasa2" / "db3"
    return (
        contacts if contacts.is_file() else None,
        pal_dir if pal_dir.is_dir() else None,
        db3_dir if db3_dir.is_dir() else None,
    )


# --------------------------------------------------------------------------
# Reference side: survey rollup + independent filesystem walks
# --------------------------------------------------------------------------


@dataclass
class Reference:
    survey: dict[str, Any]              # picasa_db._survey_ini_tree(library)
    photos_fs: frozenset[str]           # media file rels (tracer walk rule, EXTS)
    videos_fs: frozenset[str]           # the VIDEO_EXTS subset (fauxcasa-v46.2)
    folders_fs: frozenset[str]          # folder rels with >=1 media file
    stashed_fs: frozenset[str]          # stash-file rels whose sibling exists
    contacts_ini_ids: frozenset[str]    # ids in any [Contacts2] section, tree-wide
    album_def_uids: frozenset[str]      # uids carrying an [.album:<uid>] section
    contacts_xml_ids: frozenset[str] | None       # None = --contacts not given
    pal_uids: frozenset[str] | None               # None = --pal-dir not given
    # db3 (None sentinels below distinguish "no --db3 given" from "given but
    # the reference reader found nothing to index" -- see build_reference)
    db3_video_dims: dict[str, tuple[int, int]] | None
    db3_video_filetype: dict[str, int] | None
    db3_caption_diverge: dict[str, tuple[str, str]] | None
    db3_rows_unjoinable: int | None


def _rel(p: Path, library: Path) -> str:
    return p.relative_to(library).as_posix()


def _folder_rel(p: Path, library: Path) -> str:
    r = p.parent.relative_to(library).as_posix()
    return "" if r == "." else r


def _fs_media(library: Path) -> list[Path]:
    """Independent filesystem walk: every EXTS-suffixed file under
    `library`, exactly the class-1 reference rule (mirrors
    check-ingest-parity.py's build_reference; this gate does NOT reuse
    catalog.walk_library so a bug shared by both walks can't hide loss)."""
    return [
        p for p in library.rglob("*")
        if p.suffix.lower() in EXTS and p.is_file()
    ]


def _scan_ini_tree(library: Path) -> tuple[frozenset[str], frozenset[str]]:
    """One independent walk of every .picasa.ini/Picasa.ini under `library`
    (same file-discovery rule as picasa_db._survey_ini_tree), collecting:

    - contacts_ini_ids: every [Contacts2] id, normalized exactly like the
      tracer's catalog._harvest_contacts2 (hex-id regex, zero-padded to 16,
      non-empty display name required) so the SET of ids that could ever
      enter Catalog.contacts matches on both sides. Values are allowed to
      overwrite across files (last-walked wins) -- only key membership is
      used, so that divergence from the tracer's true nearest-ini-wins
      precedence never matters here.
    - album_def_uids: the uid of every `[.album:<uid>]` section, parsed
      exactly like catalog.scan_library's own album-definition loop
      (`sec.name.split(":", 1)[1].strip().lower()`).

    Fail-soft per file (OSError skips that file only), matching
    _survey_ini_tree's own read_errors handling.
    """
    contacts_ids: dict[str, str] = {}
    album_uids: set[str] = set()
    for p in sorted(library.rglob("*")):
        if p.name.lower() not in (".picasa.ini", "picasa.ini") or not p.is_file():
            continue
        try:
            ini = picasa_db.read_picasa_ini(p)
        except OSError:
            continue
        sec = ini.section("Contacts2")
        if sec is not None:
            _harvest_contacts2({"contacts2": sec}, contacts_ids)
        for s in ini.sections:
            if s.name.lower().startswith(".album:"):
                uid = s.name.split(":", 1)[1].strip().lower()
                if uid:
                    album_uids.add(uid)
    return frozenset(contacts_ids), frozenset(album_uids)


# imagedata.filetype codes for a db3-indexed video clip (picasa_db.
# THUMBINDEX_FILE_TYPES) -- copied from check-ingest-parity.py
# (fauxcasa-ed5.1/.11): thumbindex ftype mirrors imagedata.filetype exactly
# on real (non-face-crop) rows per docs/research/picasa-db3-validated.md.
_DB3_VIDEO_FILETYPE_CODES = frozenset(
    code for code, name in picasa_db.THUMBINDEX_FILE_TYPES.items()
    if name in ("avi", "mpeg4?")
)


def _build_db3_reference(db3_dir: Path, library: Path) -> tuple[
    dict[str, tuple[int, int]], dict[str, int],
    dict[str, tuple[str, str]], int,
]:
    """Adapted from check-ingest-parity.py's _db3_reference (fauxcasa-ed5.1/
    .11): independently rebuilds the db3-indexed rel-path ground truth from
    db3_dir's .pmp columns + thumbindex.db (picasa_db's validated readers),
    joined via db3rescue.translate_db3_path exactly like the rescue
    importer joins. Extended here (fauxcasa-ed5.8) with a 4th return value,
    the count of thumbindex FILE rows translate_db3_path could not join to
    this library -- an advisory-tier signal real archives are expected to
    trip (a machine-local db3 commonly indexes paths outside any one
    watched-folder root) but a synthetic corpus normally does not."""
    dims: dict[str, tuple[int, int]] = {}
    filetypes: dict[str, int] = {}
    diverge: dict[str, tuple[str, str]] = {}
    unjoinable = 0
    ti_path = db3_dir / "thumbindex.db"
    if not ti_path.is_file():
        return dims, filetypes, diverge, unjoinable
    # scan_library resolves its root before db3rescue.translate_db3_path
    # ever sees it ("root = root.resolve()") -- resolve here too so this
    # independent reference-side join can't drift from the tracer's.
    library = library.resolve()

    entries = picasa_db.read_thumbindex(ti_path, strict=False)
    full_paths = picasa_db.thumbindex_full_paths(entries)
    table = picasa_db.read_table(db3_dir, "imagedata", strict=False)
    width_col = table.columns.get("width")
    height_col = table.columns.get("height")
    filetype_col = table.columns.get("filetype")
    caption_col = table.columns.get("caption")
    ini_cache: dict[Path, picasa_db.PicasaIni | None] = {}

    for entry, abs_path in zip(entries, full_paths):
        if not entry.name or entry.is_folder:
            continue  # folders, face-crop rows, and deleted slots carry no file
        rel = translate_db3_path(abs_path, library)
        if rel is None:
            unjoinable += 1
            continue
        ftype = filetype_col.get(entry.index) if filetype_col else None
        if ftype in _DB3_VIDEO_FILETYPE_CODES:
            filetypes[rel] = ftype
            w = width_col.get(entry.index) if width_col else None
            h = height_col.get(entry.index) if height_col else None
            if w is not None and h is not None:
                dims[rel] = (w, h)
        db3_caption = caption_col.get(entry.index) if caption_col else None
        if not db3_caption:
            continue
        folder_rel, _, name = rel.rpartition("/")
        ini_path = (library / folder_rel if folder_rel else library) \
            / ".picasa.ini"
        if ini_path not in ini_cache:
            ini_cache[ini_path] = (
                picasa_db.read_picasa_ini(ini_path)
                if ini_path.is_file() else None
            )
        ini = ini_cache[ini_path]
        ini_caption = ""
        if ini is not None:
            sec = ini.section(name)
            if sec is not None:
                ini_caption = sec.get("caption") or ""
        if db3_caption != ini_caption:
            diverge[rel] = (db3_caption, ini_caption)
    return dims, filetypes, diverge, unjoinable


def build_reference(
    library: Path,
    contacts_path: Path | None,
    pal_dir: Path | None,
    db3_dir: Path | None,
) -> Reference:
    """Build the reference side purely from disk -- never touches a
    Catalog. `library` must already be resolved (main() does this once,
    shared with the tracer's own scan_library(root.resolve()))."""
    survey = picasa_db._survey_ini_tree(library)
    images = _fs_media(library)
    photos_fs = frozenset(_rel(p, library) for p in images)
    videos_fs = frozenset(
        _rel(p, library) for p in images if p.suffix.lower() in VIDEO_EXTS
    )
    folders_fs = frozenset(_folder_rel(p, library) for p in images)
    stashed_fs = frozenset(
        _rel(p, library) for p in images
        if (p.parent.name.lower() == ".picasaoriginals"
            or p.parent.name == "Originals")
        and (p.parent.parent / p.name).is_file()
    )
    contacts_ini_ids, album_def_uids = _scan_ini_tree(library)

    contacts_xml_ids = (
        frozenset(load_contacts_xml(contacts_path)) if contacts_path else None
    )
    pal_uids = None
    if pal_dir is not None:
        pals, _bad = read_pal_dir(pal_dir)
        pal_uids = frozenset(p.uid for p in pals)

    db3_video_dims: dict[str, tuple[int, int]] | None = None
    db3_video_filetype: dict[str, int] | None = None
    db3_caption_diverge: dict[str, tuple[str, str]] | None = None
    db3_rows_unjoinable: int | None = None
    if db3_dir is not None:
        (db3_video_dims, db3_video_filetype, db3_caption_diverge,
         db3_rows_unjoinable) = _build_db3_reference(db3_dir, library)

    return Reference(
        survey=survey,
        photos_fs=photos_fs,
        videos_fs=videos_fs,
        folders_fs=folders_fs,
        stashed_fs=stashed_fs,
        contacts_ini_ids=contacts_ini_ids,
        album_def_uids=album_def_uids,
        contacts_xml_ids=contacts_xml_ids,
        pal_uids=pal_uids,
        db3_video_dims=db3_video_dims,
        db3_video_filetype=db3_video_filetype,
        db3_caption_diverge=db3_caption_diverge,
        db3_rows_unjoinable=db3_rows_unjoinable,
    )


# --------------------------------------------------------------------------
# Tracer side + comparison
# --------------------------------------------------------------------------


# The survey's per-file edit-key vocabulary (picasa_db._INI_EDIT_KEYS)
# minus rotate=, which is ingested/counted as its own class -- copied
# verbatim from check-ingest-parity.py's _EDIT_KEYS_OTHER (fauxcasa-ed5.1).
_EDIT_KEYS_OTHER = frozenset(
    "filters crop crop64 flipped redo textactive".split())


def _photos_by_rel(c: Catalog) -> dict[str, Any]:
    return {p.rel: p for p in c.photos}


@dataclass
class ClassResult:
    name: str
    tier: str            # "strict" | "advisory"
    ref_count: int | None       # None = skipped (source absent)
    tracer_count: int | None
    verdict: str          # "ok" | "FAIL" | "warn" | "skipped (source absent)"
    # Already-redacted `<len=N sha1=..>` tokens -- never raw archive
    # strings. Populated only "where computable" (spec): the aggregate
    # ini-survey classes (starred/captioned/.../edit_keys_other) have no
    # per-item identity on the reference side, so they carry none.
    examples: list[str] = field(default_factory=list)


@dataclass
class Results:
    rows: list[ClassResult]
    # "source:kind" -> count, from Catalog.report.entries (ImportReport).
    # source/kind are fixed code vocabulary, never user data -- safe
    # unredacted per the redaction contract (only subject/detail are
    # archive strings, and neither is ever surfaced here).
    report_histogram: dict[str, int]


def _strict_fs_row(name: str, ref_set: frozenset, tracer_set: frozenset) -> ClassResult:
    """A strict-tier row whose reference AND tracer sides are each an
    independently-computed set of the SAME identity tokens (rel paths) --
    the fs-walk-vs-catalog classes (photos/folders/videos/
    stashed_originals). ref_count/tracer_count are the true set sizes (not
    an intersection), so both loss (ref-only) and excess (tracer-only)
    show up in the counts; mismatches carry the symmetric difference as
    redacted examples."""
    ref_n, tr_n = len(ref_set), len(tracer_set)
    verdict = "ok" if ref_n == tr_n else "FAIL"
    examples: list[str] = []
    if verdict == "FAIL":
        examples = [_redact(x) for x in sorted(ref_set ^ tracer_set)]
    return ClassResult(name, "strict", ref_n, tr_n, verdict, examples)


def _strict_presence_row(name: str, ref_set: frozenset | None,
                         matched: frozenset) -> ClassResult:
    """A strict-tier row that checks PRESENCE of every reference-side id/
    uid in the tracer's catalog (contacts/albums classes): `matched` is
    the subset of `ref_set` the caller found in the catalog, so it is
    always <= ref_set by construction (no "excess" concept for a presence
    check) -- ref_count is len(ref_set), tracer_count is len(matched), and
    a mismatch's examples are the ref-side ids/uids with no match."""
    if ref_set is None:
        return ClassResult(name, "strict", None, None, "skipped (source absent)")
    ref_n, tr_n = len(ref_set), len(matched)
    verdict = "ok" if ref_n == tr_n else "FAIL"
    examples: list[str] = []
    if verdict == "FAIL":
        examples = [_redact(x) for x in sorted(ref_set - matched)]
    return ClassResult(name, "strict", ref_n, tr_n, verdict, examples)


def _strict_count_row(name: str, ref_n: int | None, tr_n: int) -> ClassResult:
    """A strict-tier row where only the reference side's aggregate COUNT is
    available (the ini-survey rollup carries no per-item identity) -- no
    example rels are computable, per the spec's 'where computable'."""
    if ref_n is None:
        return ClassResult(name, "strict", None, None, "skipped (source absent)")
    verdict = "ok" if ref_n == tr_n else "FAIL"
    return ClassResult(name, "strict", ref_n, tr_n, verdict)


def compare(ref: Reference, cat: Catalog) -> Results:
    rows: list[ClassResult] = []
    by_rel = _photos_by_rel(cat)
    feats = ref.survey.get("features", {})
    keys = ref.survey.get("keys", {})

    # ---- fs-vs-catalog identity classes (examples computable) -----------
    rows.append(_strict_fs_row(
        "photos", ref.photos_fs, frozenset(p.rel for p in cat.photos)))
    rows.append(_strict_fs_row(
        "folders", ref.folders_fs, frozenset(cat.folders)))
    rows.append(_strict_fs_row(
        "videos", ref.videos_fs,
        frozenset(p.rel for p in cat.photos if p.media == "video")))
    stash_tracer = frozenset(
        cat.photos[p.stashed_original].rel
        for p in cat.photos if p.stashed_original is not None
    )
    rows.append(_strict_fs_row("stashed_originals", ref.stashed_fs, stash_tracer))

    # ---- ini-survey aggregate classes (no per-item identity) ------------
    rows.append(_strict_count_row(
        "starred", feats.get("starred", 0),
        sum(1 for p in cat.photos if p.star)))
    rows.append(_strict_count_row(
        "captioned", feats.get("captioned", 0),
        sum(1 for p in cat.photos if p.caption)))
    rows.append(_strict_count_row(
        "keyworded", feats.get("keyworded", 0),
        sum(1 for p in cat.photos if p.keywords)))
    rows.append(_strict_count_row(
        "hidden_photos", keys.get("hidden", 0),
        sum(1 for p in cat.photos if p.hidden)))
    rows.append(_strict_count_row(
        "hidden_folders", keys.get("p2category", 0),
        sum(1 for f in cat.folders.values() if f.folder_hidden)))
    rows.append(_strict_count_row(
        "rotated", keys.get("rotate", 0),
        sum(1 for p in cat.photos if p.rotate)))
    rows.append(_strict_count_row(
        "folder_descriptions", keys.get("description", 0),
        sum(1 for f in cat.folders.values() if f.description)))
    rows.append(_strict_count_row(
        "album_memberships", feats.get("album_memberships", 0),
        sum(len(p.albums) for p in cat.photos)))
    rows.append(_strict_count_row(
        "geotagged", feats.get("geotagged", 0),
        sum(1 for p in cat.photos if p.geotag is not None)))
    rows.append(_strict_count_row(
        "face_tags", feats.get("face_tags", 0),
        sum(len(p.faces) for p in cat.photos)))
    rows.append(_strict_count_row(
        "edit_keys_other",
        feats.get("edit_keys", 0) - keys.get("rotate", 0),
        sum(1 for p in cat.photos for k, _v in p.edits
            if k.lower() in _EDIT_KEYS_OTHER)))

    # ---- contacts / albums (ids/uids; examples computable) ---------------
    if ref.contacts_xml_ids is None:
        rows.append(_strict_presence_row("contacts_xml_present", None, frozenset()))
    else:
        matched = frozenset(
            i for i in ref.contacts_xml_ids if i in cat.contacts)
        rows.append(_strict_presence_row(
            "contacts_xml_present", ref.contacts_xml_ids, matched))
    matched_ini = frozenset(
        i for i in ref.contacts_ini_ids if i in cat.contacts)
    rows.append(_strict_presence_row(
        "contacts_ini_present", ref.contacts_ini_ids, matched_ini))
    matched_albums = frozenset(
        u for u in ref.album_def_uids
        if u in cat.albums and not cat.albums[u].placeholder)
    rows.append(_strict_presence_row(
        "album_definitions", ref.album_def_uids, matched_albums))
    if ref.pal_uids is None:
        rows.append(_strict_presence_row("pal_albums_present", None, frozenset()))
    else:
        matched_pal = frozenset(u for u in ref.pal_uids if u in cat.albums)
        rows.append(_strict_presence_row(
            "pal_albums_present", ref.pal_uids, matched_pal))

    # ---- db3 (strict) ------------------------------------------------------
    if ref.db3_video_dims is None:
        rows.append(_strict_presence_row("db3_video_dims", None, frozenset()))
        rows.append(_strict_presence_row("db3_video_filetype", None, frozenset()))
    else:
        dims_ok = frozenset(
            rel for rel, dims in ref.db3_video_dims.items()
            if (p := by_rel.get(rel)) is not None and p.dims == dims
        )
        rows.append(_strict_presence_row(
            "db3_video_dims", frozenset(ref.db3_video_dims), dims_ok))
        ft_ok = frozenset(
            rel for rel, ftype in ref.db3_video_filetype.items()
            if (p := by_rel.get(rel)) is not None and p.media == "video"
            and ftype in _DB3_VIDEO_FILETYPE_CODES
        )
        rows.append(_strict_presence_row(
            "db3_video_filetype", frozenset(ref.db3_video_filetype), ft_ok))

    # ---- advisory tier (never affects exit code) --------------------------
    if ref.db3_caption_diverge is None:
        rows.append(ClassResult("db3_caption_diverge", "advisory", None, None,
                                 "skipped (source absent)"))
        rows.append(ClassResult("db3_rows_unjoinable", "advisory", None, None,
                                 "skipped (source absent)"))
    else:
        n = len(ref.db3_caption_diverge)
        rows.append(ClassResult("db3_caption_diverge", "advisory", n, None,
                                 "warn" if n else "ok"))
        u = ref.db3_rows_unjoinable or 0
        rows.append(ClassResult("db3_rows_unjoinable", "advisory", u, None,
                                 "warn" if u else "ok"))

    histogram: dict[str, int] = {}
    for e in cat.report.entries:
        key = f"{e.source}:{e.kind}"
        histogram[key] = histogram.get(key, 0) + 1

    return Results(rows=rows, report_histogram=histogram)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _fmt(n: int | None) -> str:
    return "-" if n is None else str(n)


def _cap(items: list[str], n: int) -> list[str]:
    return items[:n] if n >= 0 else items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("library", metavar="LIBRARY",
                    help="root of the (read-only copy of the) watched-folder "
                    "photo tree")
    ap.add_argument("--picasa-home", metavar="DIR", default=None,
                    help="a copied Picasa app-data area; derives --contacts/"
                    "--pal-dir/--db3 from it (only paths that exist), unless "
                    "overridden explicitly")
    ap.add_argument("--contacts", metavar="PATH", default=None,
                    help="Picasa contacts.xml (overrides --picasa-home)")
    ap.add_argument("--pal-dir", metavar="PATH", default=None,
                    help="Picasa2Albums directory of .pal files (overrides "
                    "--picasa-home)")
    ap.add_argument("--db3", metavar="PATH", default=None,
                    help="Picasa db3 directory (overrides --picasa-home)")
    ap.add_argument("--json", metavar="OUT", default=None,
                    help="also write the redacted machine-readable result; "
                    "must NOT be inside LIBRARY or --picasa-home")
    ap.add_argument("--max-detail", type=int, default=DEFAULT_MAX_DETAIL,
                    metavar="N",
                    help="cap of redacted example rels printed per "
                    f"mismatching class (default: {DEFAULT_MAX_DETAIL})")
    args = ap.parse_args(argv)

    library = Path(args.library)
    if not library.is_dir():
        print("error: LIBRARY is not a directory", file=sys.stderr)
        return 2
    library = library.resolve()

    picasa_home = Path(args.picasa_home).resolve() if args.picasa_home else None
    if picasa_home is not None and not picasa_home.is_dir():
        print("error: --picasa-home is not a directory", file=sys.stderr)
        return 2

    contacts_path = pal_dir = db3_dir = None
    if picasa_home is not None:
        contacts_path, pal_dir, db3_dir = derive_picasa_home(picasa_home)
    # An explicit flag REPLACES (not merely adds to) whatever --picasa-home
    # derived, mirroring main.py's --pal-dir/--db3 precedence: a bad
    # explicit path drops that source entirely rather than silently
    # resurrecting the derived default.
    if args.contacts is not None:
        contacts_path = Path(args.contacts)
        if not contacts_path.is_file():
            print("warning: --contacts does not exist; ignored",
                  file=sys.stderr)
            contacts_path = None
    if args.pal_dir is not None:
        pal_dir = Path(args.pal_dir)
        if not pal_dir.is_dir():
            print("warning: --pal-dir does not exist; ignored",
                  file=sys.stderr)
            pal_dir = None
    if args.db3 is not None:
        db3_dir = Path(args.db3)
        if not db3_dir.is_dir():
            print("warning: --db3 does not exist; ignored", file=sys.stderr)
            db3_dir = None

    # Read-only guarantee: --json must not land under anything this script
    # reads from. Checked before any archive I/O happens.
    if args.json is not None:
        out = Path(args.json).resolve()
        roots = [library] + ([picasa_home] if picasa_home is not None else [])
        for root in roots:
            if out == root or root in out.parents:
                print("error: --json OUT must not be inside LIBRARY or "
                      "--picasa-home", file=sys.stderr)
                return 2

    # scan_library is a pure read (verified by skimming catalog.py/
    # db3rescue.py: the only writes in catalog.py live in save_catalog/
    # save_report, neither reachable from scan_library's call graph, and
    # db3rescue.py performs no writes at all) -- safe against a read-only
    # copy of the archive.
    contacts_map = load_contacts_xml(contacts_path) if contacts_path else {}
    cat = scan_library(library, contacts=contacts_map,
                       pal_dir=pal_dir, db3_dir=db3_dir)
    ref = build_reference(library, contacts_path, pal_dir, db3_dir)
    results = compare(ref, cat)

    lib_token = _redact(str(library))
    print(f"library: {lib_token}")
    print("confirm-archive gate (M1 clause 3) over the library above")

    header = ("class", "reference", "tracer", "tier", "verdict")
    table_rows = [
        (r.name, _fmt(r.ref_count), _fmt(r.tracer_count), r.tier, r.verdict)
        for r in results.rows
    ]
    w = [max(len(x[i]) for x in table_rows + [header]) for i in range(5)]
    print("  ".join(h.ljust(w[i]) for i, h in enumerate(header)))
    print("  ".join("-" * w[i] for i in range(5)))
    for r in table_rows:
        print("  ".join(r[i].ljust(w[i]) for i in range(5)))

    if results.report_histogram:
        print("\nadvisory: import_report histogram (source:kind x count)")
        for key in sorted(results.report_histogram):
            print(f"  {key} x{results.report_histogram[key]}")
    else:
        print("\nadvisory: import_report is empty")

    failures = [r for r in results.rows
                if r.tier == "strict" and r.verdict == "FAIL"]
    if failures:
        print(f"\nFAIL: {len(failures)} problem(s)")
        for r in failures:
            print(f"  - {r.name}: reference {r.ref_count}, "
                  f"tracer {r.tracer_count}")
            for ex in _cap(r.examples, args.max_detail):
                print(f"      example: {ex}")
    else:
        print("\nPASS: every strict class matches between the survey/"
              "filesystem reference and the tracer catalog")

    if args.json is not None:
        payload = {
            "library": lib_token,
            "pass": not failures,
            "rows": [
                {
                    "name": r.name,
                    "tier": r.tier,
                    "reference": r.ref_count,
                    "tracer": r.tracer_count,
                    "verdict": r.verdict,
                    "examples": _cap(r.examples, args.max_detail),
                }
                for r in results.rows
            ],
            "report_histogram": results.report_histogram,
        }
        Path(args.json).write_text(
            json.dumps(payload, indent=1), encoding="utf-8")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
