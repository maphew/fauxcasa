#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
# No dependencies on purpose: this script only READS an existing archive
# (stdlib + repo modules; videoload's `import av` is lazy and unreachable
# without a scan_filter). Keeps `uv run` on the owner's machine free of
# binary wheels. test_confirm_archive.py declares pillow/piexif/av itself
# because IT generates a synthetic corpus.
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
(FAIL); 2 = usage error; 3 = an unexpected exception was raised while
reading the archive (the exception TYPE is reported, never its message or
traceback -- those can embed the archive's own paths/strings).
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

import picasa_db
from catalog import (
    EXTS,
    INI_NAMES,
    LIBRARY_DIR,
    ROOT_MARKER,
    Catalog,
    _flag,
    _harvest_contacts2,
    _is_folder_hidden,
    _parse_ini_geotag,
    load_contacts_xml,
    read_pal_dir,
    scan_library,
)

# db3 rescue plumbing (fauxcasa-cam.6): the same machine-path translator
# scan_library's own rescue_people uses, reused here so the reference-side
# db3 join follows the exact same rule as the tracer.
from db3rescue import translate_db3_path
from videoload import VIDEO_EXTS

_redact = picasa_db._redact_str

DEFAULT_MAX_DETAIL = 10


# --------------------------------------------------------------------------
# --picasa-home derivation
# --------------------------------------------------------------------------


def derive_picasa_home(home: Path) -> tuple[Path | None, Path | None, Path | None]:
    """(contacts.xml, Picasa2Albums dir, db3 dir) under a copied Picasa
    app-data area; `home` plays the role of %LocalAppData%\\Google.
    Real Picasa lays out Picasa2\\{contacts\\contacts.xml,db3} with
    Picasa2Albums a SIBLING of Picasa2\\ (observed empirically:
    docs/research/picasastarter-notes.md, wine-oracle.md). The sibling
    location is probed first; Picasa2\\Picasa2Albums second, tolerating a
    copy shaped after catalog.default_pal_dir's (divergent, fauxcasa-1t6)
    path. Each path is returned only if it exists on disk -- a copied
    app-data area legitimately carries a subset."""
    contacts = home / "Picasa2" / "contacts" / "contacts.xml"
    db3_dir = home / "Picasa2" / "db3"
    pal_dir = None
    for cand in (home / "Picasa2Albums", home / "Picasa2" / "Picasa2Albums"):
        if cand.is_dir():
            pal_dir = cand
            break
    return (
        contacts if contacts.is_file() else None,
        pal_dir,
        db3_dir if db3_dir.is_dir() else None,
    )


# --------------------------------------------------------------------------
# Reference side: survey rollup + independent filesystem walks
# --------------------------------------------------------------------------


@dataclass
class Reference:
    # Scoped ini tallies (fauxcasa-ed5.8 review fix): NOT picasa_db.
    # _survey_ini_tree's tree-wide rollup -- that counts every section in
    # every .picasa.ini under `library`, including ghost sections for
    # deleted/moved files, description=/p2category= lines that happen to
    # live in a non-[Picasa] section, and inis in folders with no media at
    # all. catalog.scan_library never reads any of that: it only opens the
    # ini catalog._read_folder_ini would pick for a MEDIA folder (folders_fs
    # below) and only merges sections whose name matches a WALKED file in
    # that same folder. `feats`/`keys` are this gate's OWN independent
    # rebuild of that narrower scope (see _scoped_ini_survey) -- same
    # feature/key vocabulary as _survey_ini_tree, same predicates
    # (predicate-anchored against it in test_confirm_archive.py on the
    # pristine corpus), but counted only where the tracer would ever look.
    feats: dict[str, int]
    keys: dict[str, int]
    photos_fs: frozenset[str]           # media file rels (tracer walk rule, EXTS)
    videos_fs: frozenset[str]           # the VIDEO_EXTS subset (fauxcasa-v46.2)
    folders_fs: frozenset[str]          # folder rels with >=1 media file
    stashed_fs: frozenset[str]          # stash-file rels whose sibling exists
    contacts_ini_ids: frozenset[str]    # [Contacts2] ids, media folders + ancestors
    album_def_uids: frozenset[str]      # [.album:<uid>] uids, media folders only
    contacts_xml_ids: frozenset[str] | None       # None = --contacts not given
    pal_uids: frozenset[str] | None               # None = --pal-dir not given
    # db3 (None sentinels below distinguish "no --db3 given" from "given but
    # the reference reader found nothing to index" -- see build_reference)
    db3_video_dims: dict[str, tuple[int, int]] | None
    db3_video_filetype: dict[str, int] | None
    db3_caption_precedence: dict[str, tuple[str, str]] | None
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
    nroot = len(library.parts)
    return [
        p for p in library.rglob("*")
        if p.suffix.lower() in EXTS and p.is_file()
        # walk_library's own exclusions, mirrored (fauxcasa-ed5.7.1): a
        # .fauxcasa/ library-state dir or .fauxcasa-root marker inside the
        # copied library must be invisible to BOTH sides.
        and LIBRARY_DIR not in p.parts[nroot:]
        and p.name != ROOT_MARKER
    ]


def _folder_ini_path(folder: Path) -> Path | None:
    """The ini file catalog._read_folder_ini would pick for `folder`: the
    first existing name in catalog.INI_NAMES (.picasa.ini, Picasa.ini,
    picasa.ini), in that order. Mirrors the tracer's own precedence (not a
    reuse of its private function) so this reads the SAME sidecar the
    tracer would load for this folder, never a tree-wide union.

    NOTE: comparison is by filesystem lookup (`p.is_file()`), so on a
    case-insensitive filesystem (Windows -- Picasa's own platform) a
    folder carrying only e.g. `PICASA.INI` still resolves via the
    `.picasa.ini` probe; on a case-sensitive filesystem this can diverge
    from catalog.INI_NAMES's exact-name selection, exactly like the
    tracer's own probe would."""
    for name in INI_NAMES:
        p = folder / name
        if p.is_file():
            return p
    return None


def _read_ini_soft(path: Path) -> picasa_db.PicasaIni | None:
    """Fail-soft per file: an unreadable ini degrades to absent, never an
    exception -- matches picasa_db._survey_ini_tree's read_errors handling
    and catalog._read_folder_ini's own per-name fallback."""
    try:
        return picasa_db.read_picasa_ini(path)
    except OSError:
        return None


def _merge_sections(ini: picasa_db.PicasaIni) -> dict[str, picasa_db.IniSection]:
    """name.lower() -> section, duplicate sections concatenated in order.
    Mirrors catalog._section_map's merge rule (IniSection.get is
    first-key-wins, and concatenating preserves that across duplicates) --
    written independently here, not imported, since it's simple enough
    that reusing the tracer's own private helper would blur the
    independent-readers doctrine this whole gate rests on.

    NOTE: keyed by name.lower() -- targets Windows' case-insensitive
    filesystem (Picasa's own platform), matching catalog._section_map
    exactly; on a case-sensitive filesystem section-name casing could in
    principle diverge from a file lookup, same caveat as
    _folder_ini_path above."""
    out: dict[str, picasa_db.IniSection] = {}
    for sec in ini.sections:
        key = sec.name.lower()
        prev = out.get(key)
        if prev is None:
            out[key] = sec
        else:
            merged = picasa_db.IniSection(name=prev.name, items=list(prev.items))
            merged.items.extend(sec.items)
            out[key] = merged
    return out


def _scoped_ini_survey(
    library: Path,
    folders_fs: frozenset[str],
    photos_by_folder: dict[str, frozenset[str]],
) -> tuple[dict[str, int], dict[str, int], frozenset[str], frozenset[str]]:
    """The reference-side survey rebuild, SCOPED to exactly what
    catalog.scan_library would ever read (fauxcasa-ed5.8 review fix for
    the aggregate-class false-FAIL blocker) -- unlike picasa_db.
    _survey_ini_tree, which tallies every section in every .picasa.ini
    under `library` regardless of whether any code path ever looks at it.

    Returns (feats, keys, contacts_ini_ids, album_def_uids):

    - feats/keys: _survey_ini_tree's feature/key VOCABULARY (plus this
      gate's own "edit_keys_other"), but each key judged by the TRACER's
      predicate -- _flag for star/hidden, `get("caption") or None`,
      non-empty keyword tokens, parse_rotate % 4 truthiness,
      _parse_ini_geotag is not None, _is_folder_hidden for p2category,
      parse_faces rect count with fail-soft [] -- imported/mirrored from
      catalog.py so a value the tracer legitimately drops (hidden=no,
      rotate=rotate(0), a malformed geotag=, a P2category that isn't
      "Hidden Folders", an empty description= line) can never false-FAIL
      a strict class (second review round, fauxcasa-ed5.8). Tallied ONLY
      from:
        * per-photo keys: sections in a MEDIA folder's ini (folders_fs)
          whose name.lower() matches a WALKED photo's filename in that
          same folder -- mirrors catalog.scan_library's
          `secmap.get(name.lower())` per-file lookup exactly. A stale
          section for a deleted/renamed file (no walked file of that
          name) is invisible here, same as it is to the tracer.
        * folder-level keys (description/p2category): the folder-scope
          `[Picasa]` section only, media folders only -- mirrors
          `psec = secmap.get("picasa")` in scan_library. A description=
          line that happens to live in some OTHER section (e.g. an
          [.album:] block) is not folder_descriptions/hidden_folders
          material to the tracer and isn't counted here either.
      The predicate-anchoring test compares these tallies against
      _survey_ini_tree on the PRISTINE synthetic corpus, where every
      section names a real file and every value is well-formed, so both
      scope and predicates coincide there by construction.
    - contacts_ini_ids: [Contacts2] ids from MEDIA folders' inis AND
      their ANCESTOR CHAIN up to and including the library root ("") --
      exactly the ini set scan_library's contacts-registry loop iterates,
      because that loop runs AFTER folder_contacts has loaded every
      ancestor ini into ini_by_folder. A photoless SIBLING branch (never
      an ancestor of any media folder) stays correctly excluded.
    - album_def_uids: `[.album:<uid>]` definitions from MEDIA folders'
      inis ONLY. scan_library's album-definition loop runs EARLIER than
      the contacts loop -- over ini_by_folder as it stands right after
      the walk, i.e. media folders plus only whatever ancestors faces=
      harvesting happened to pull in -- so an ancestor-only [.album:]
      block is NOT reliably ingested (whether it is flips on an unrelated
      faces= line in the subtree). Scoping the reference to media folders
      keeps it a subset of what the tracer always ingests: real Picasa
      writes [.album:] blocks into member (media) folders, and a strict
      presence row can only false-FAIL on over-inclusion, never on
      under-inclusion.

    Every ini read is fail-soft per file (_read_ini_soft): unreadable or
    absent sidecars degrade to "no ini for this folder", never an
    exception.
    """
    feats: dict[str, int] = {}
    keys: dict[str, int] = {}

    def bump(d: dict[str, int], k: str, n: int = 1) -> None:
        d[k] = d.get(k, 0) + n

    ini_cache: dict[str, tuple[picasa_db.PicasaIni, dict] | None] = {}

    def get_ini(folder_rel: str) -> tuple[picasa_db.PicasaIni, dict] | None:
        if folder_rel in ini_cache:
            return ini_cache[folder_rel]
        folder = library / folder_rel if folder_rel else library
        path = _folder_ini_path(folder)
        ini = _read_ini_soft(path) if path is not None else None
        entry = (ini, _merge_sections(ini)) if ini is not None else None
        ini_cache[folder_rel] = entry
        return entry

    # ---- contacts_ini_ids: media folders + ancestors ---------------------
    # ---- album_def_uids: media folders ONLY (see docstring) --------------
    # Contacts: scan_library's [Contacts2] registry loop runs after
    # folder_contacts() has loaded every ancestor ini, so ancestors count.
    # Albums: scan_library's album-definition loop runs BEFORE that, over
    # media-folder inis (plus only faces=-triggered ancestors), so the
    # reference stays a subset -- media folders only -- to never
    # false-FAIL a strict presence row on an ancestor-only block.
    contacts_ids: dict[str, str] = {}
    album_uids: set[str] = set()
    visited: set[str] = set()
    for folder_rel in folders_fs:
        cur = folder_rel
        while cur not in visited:
            visited.add(cur)
            entry = get_ini(cur)
            if entry is not None:
                ini, secmap = entry
                sec = secmap.get("contacts2")
                if sec is not None:
                    _harvest_contacts2({"contacts2": sec}, contacts_ids)
                if cur in folders_fs:
                    for s in ini.sections:
                        if s.name.lower().startswith(".album:"):
                            uid = s.name.split(":", 1)[1].strip().lower()
                            if uid:
                                album_uids.add(uid)
            if not cur:
                break
            cur = cur.rsplit("/", 1)[0] if "/" in cur else ""

    # ---- folder-level/per-photo feats+keys ------------------------------
    for folder_rel, photo_names in photos_by_folder.items():
        entry = get_ini(folder_rel)
        if entry is None:
            continue
        _ini, secmap = entry

        # folder-level keys, via the tracer's OWN predicates so a value
        # the tracer legitimately drops (a P2category other than "Hidden
        # Folders", an empty description= line) can never false-FAIL here.
        # Lookup is IniSection.get -- case-insensitive FIRST-WINS, so a
        # duplicated key line in a merged duplicate section counts once,
        # exactly like the tracer's read.
        psec = secmap.get("picasa")
        if psec is not None:
            if (psec.get("description") or None) is not None:
                bump(keys, "description")     # mirrors Folder.description
            if _is_folder_hidden(psec):
                bump(keys, "p2category")      # mirrors Folder.folder_hidden
        # per-photo keys: sections matching a WALKED photo in this folder,
        # each key judged by the exact predicate scan_library applies when
        # it sets the Photo field compare() later counts. Only the
        # edit-recipe keys tally per LINE, because Photo.edits itself
        # preserves every key line raw, dupes kept (fauxcasa-cam.15).
        for photo_name in photo_names:
            sec = secmap.get(photo_name.lower())
            if sec is None:
                continue
            if _flag(sec, "star"):            # p.star = 1 if _flag(...)
                bump(feats, "starred")
            if sec.get("caption"):            # p.caption = get(...) or None
                bump(feats, "captioned")
            kw = sec.get("keywords")
            if kw and any(k.strip() for k in kw.split(",")):
                bump(feats, "keyworded")      # p.keywords = non-empty tokens
            if _flag(sec, "hidden"):          # p.hidden = _flag(...)
                bump(keys, "hidden")
            rot = sec.get("rotate")
            if rot:                           # p.rotate = parse_rotate % 4,
                try:                          # ValueError fail-soft -> 0
                    if picasa_db.parse_rotate(rot) % 4:
                        bump(keys, "rotate")
                except ValueError:
                    pass
            gt = sec.get("geotag")
            if gt and _parse_ini_geotag(gt) is not None:
                bump(feats, "geotagged")      # p.geotag = parse or None
            fv = sec.get("faces")
            if fv:
                try:                          # fail-soft per line, like
                    faces = picasa_db.parse_faces(fv)  # scan_library's own
                except ValueError:                     # except -> []
                    faces = []
                bump(feats, "face_tags", len(faces))
            al = sec.get("albums")
            if al:
                bump(feats, "album_memberships",
                     sum(1 for t in al.split(",") if t.strip()))
            for k, _v in sec.items:
                # k.lower(), no strip: Photo.edits filters `k.lower() in
                # EDIT_RECIPE_KEYS` and compare() narrows the same way.
                if k.lower() in _EDIT_KEYS_OTHER:
                    bump(feats, "edit_keys_other")

    return feats, keys, frozenset(contacts_ids), frozenset(album_uids)


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
        folder = library / folder_rel if folder_rel else library
        # The sibling ini via the SAME name-precedence catalog._read_folder_ini
        # uses (fauxcasa-ed5.8 review fix): a hardcoded ".picasa.ini" here
        # read every caption in a Picasa-2-era archive (Picasa.ini) as
        # "divergent" -- there was never an ini to compare against.
        if folder not in ini_cache:
            path = _folder_ini_path(folder)
            ini_cache[folder] = _read_ini_soft(path) if path is not None else None
        ini = ini_cache[folder]
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
    photos_by_folder: dict[str, set[str]] = {fr: set() for fr in folders_fs}
    for p in images:
        photos_by_folder[_folder_rel(p, library)].add(p.name)
    photos_by_folder_frozen = {
        fr: frozenset(names) for fr, names in photos_by_folder.items()
    }
    feats, keys, contacts_ini_ids, album_def_uids = _scoped_ini_survey(
        library, folders_fs, photos_by_folder_frozen)

    contacts_xml_ids = (
        frozenset(load_contacts_xml(contacts_path)) if contacts_path else None
    )
    pal_uids = None
    if pal_dir is not None:
        pals, _bad = read_pal_dir(pal_dir)
        pal_uids = frozenset(p.uid for p in pals)

    db3_video_dims: dict[str, tuple[int, int]] | None = None
    db3_video_filetype: dict[str, int] | None = None
    db3_caption_precedence: dict[str, tuple[str, str]] | None = None
    db3_rows_unjoinable: int | None = None
    if db3_dir is not None:
        (db3_video_dims, db3_video_filetype, db3_caption_precedence,
         db3_rows_unjoinable) = _build_db3_reference(db3_dir, library)

    return Reference(
        feats=feats,
        keys=keys,
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
        db3_caption_precedence=db3_caption_precedence,
        db3_rows_unjoinable=db3_rows_unjoinable,
    )


# --------------------------------------------------------------------------
# Tracer side + comparison
# --------------------------------------------------------------------------


# The survey's per-file edit-key vocabulary (picasa_db._INI_EDIT_KEYS)
# minus rotate=, which is ingested/counted as its own class -- copied
# verbatim from check-ingest-parity.py's _EDIT_KEYS_OTHER (fauxcasa-ed5.1).
_EDIT_KEYS_OTHER = frozenset(
    ["filters", "crop", "crop64", "flipped", "redo", "textactive"])


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
    feats = ref.feats
    keys = ref.keys

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
    # db3 caption gap-fill (fauxcasa-cam.20): scan_library's rescue fills
    # EMPTY captions from db3, so the tracer legitimately exceeds the ini
    # survey by exactly the joinable gap rows -- mirror check-ingest-
    # parity.py's _captioned_reference. A gap row counts only when its rel
    # names a WALKED photo (disk spelling, or the rescue's own casefold
    # fallback); a stale db3 row for a deleted file fills nothing and must
    # not inflate the reference.
    db3_caption_gaps = 0
    if ref.db3_caption_precedence:
        photos_fold = {r.casefold() for r in ref.photos_fs}
        db3_caption_gaps = sum(
            1 for rel, (_db3c, ini_c) in ref.db3_caption_precedence.items()
            if not ini_c
            and (rel in ref.photos_fs or rel.casefold() in photos_fold))
    rows.append(_strict_count_row(
        "captioned", feats.get("captioned", 0) + db3_caption_gaps,
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
        feats.get("edit_keys_other", 0),
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
        # translate_db3_path returns the DB3's own path spelling; its
        # contract has the caller fall back to a casefolded catalog lookup
        # for the rare disk-vs-db3 case mismatch (a case-renamed folder on
        # a case-insensitive volume). Mirror rescue_people's two-index
        # join exactly, or such a video false-FAILs here while the
        # shipping importer joins it fine.
        by_fold = {p.rel.casefold(): p for p in cat.photos}

        def _lookup(rel: str):
            return by_rel.get(rel) or by_fold.get(rel.casefold())

        dims_ok = frozenset(
            rel for rel, dims in ref.db3_video_dims.items()
            if (p := _lookup(rel)) is not None and p.dims == dims
        )
        rows.append(_strict_presence_row(
            "db3_video_dims", frozenset(ref.db3_video_dims), dims_ok))
        ft_ok = frozenset(
            rel for rel, ftype in ref.db3_video_filetype.items()
            if (p := _lookup(rel)) is not None and p.media == "video"
            and ftype in _DB3_VIDEO_FILETYPE_CODES
        )
        rows.append(_strict_presence_row(
            "db3_video_filetype", frozenset(ref.db3_video_filetype), ft_ok))

    # ---- advisory tier (never affects exit code) --------------------------
    if ref.db3_caption_precedence is None:
        rows.append(ClassResult("db3_caption_precedence", "advisory", None, None,
                                 "skipped (source absent)"))
        rows.append(ClassResult("db3_rows_unjoinable", "advisory", None, None,
                                 "skipped (source absent)"))
    else:
        n = len(ref.db3_caption_precedence)
        rows.append(ClassResult("db3_caption_precedence", "advisory", n, None,
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
                    "must NOT be inside LIBRARY, --picasa-home, or any "
                    "resolved --contacts/--pal-dir/--db3 source")
    ap.add_argument("--max-detail", type=int, default=DEFAULT_MAX_DETAIL,
                    metavar="N",
                    help="cap of redacted example rels printed per "
                    f"mismatching class (default: {DEFAULT_MAX_DETAIL})")
    args = ap.parse_args(argv)

    try:
        return _run(args)
    except Exception as e:  # noqa: BLE001 -- the blanket catch IS the redaction barrier
        # Never print str(e) or a traceback: an exception's message
        # commonly embeds the archive's own path (an OSError's filename,
        # a malformed-ini parse position, ...) -- the same discipline
        # picasa_db.cmd_survey applies to its own OS-level errors ("the
        # exception class name only"). Exit 3 is reserved for this path
        # (see the module docstring's exit-code list).
        print(f"error: {type(e).__name__} "
              "(details withheld: redaction contract)", file=sys.stderr)
        return 3


def _run(args: argparse.Namespace) -> int:
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
    # actually reads from -- library, --picasa-home, AND every resolved
    # source that survived the explicit-override logic above (contacts.
    # xml's PARENT dir, pal_dir, db3_dir), not just the first two. Checked
    # before any archive I/O happens.
    if args.json is not None:
        out = Path(args.json).resolve()
        roots = [library]
        if picasa_home is not None:
            roots.append(picasa_home)
        if contacts_path is not None:
            roots.append(contacts_path.parent.resolve())
        if pal_dir is not None:
            roots.append(pal_dir.resolve())
        if db3_dir is not None:
            roots.append(db3_dir.resolve())
        for root in roots:
            if out == root or root in out.parents:
                print("error: --json OUT must not be inside LIBRARY, "
                      "--picasa-home, or any resolved --contacts/"
                      "--pal-dir/--db3 source", file=sys.stderr)
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
