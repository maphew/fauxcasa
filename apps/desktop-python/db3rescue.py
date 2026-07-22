"""db3 rescue import for the tracer (fauxcasa-cam.6/.7/.20).

Spec §4 reads the db3 catalog as the *rescue importer*: db3 fills gaps
ONLY — anything the ini or contacts.xml already provides never changes,
and every departure from a source lands on the ImportReport, never a
silent resolution. Of the four DB-only rescue classes, only class 4
(contacts-only face names / people albums) has a pinned byte format
(oracle fixtures 014/026, `docs/research/picasa-db3-validated.md`);
classes 1-3 (ignored faces, manual sort, video overrides) await oracle
fixtures (fauxcasa-ed5.9) and are deliberately not read here.

The plumbing (cam.6): locate the machine-local db3 directory (--db3 flag
or the %LocalAppData%\\Google\\Picasa2\\db3 default; fail-soft absent),
read thumbindex + the needed pmp columns through scripts/picasa_db.py
(the project's validated readers — never reimplemented), and translate
db3's absolute machine paths onto library-relative catalog keys. Drive
letters are translated, never stored (§8): translation compares path
components case-insensitively with the drive prefix stripped from both
sides, so a library that moved from `C:` to `D:` (or into a Wine `Z:`
prefix) still joins. An unresolvable path is an import-report entry,
never an error.

Caption rescue (cam.20): imagedata.caption joins through thumbindex and
fills only a Photo.caption gap. An ini caption already parsed by the walk
is never replaced; equal and divergent non-gap outcomes are both surfaced
in the ImportReport. The indexer subsequently applies in-file XMP/IPTC
captions over the walked catalog, preserving §4's in-file > ini > db3 rank.

The class-4 rescue (cam.7): albumdata category-8 rows are person albums
— name + albumcontactids (a uint64 whose %016x form is byte-equal to the
ini [Contacts2] key and the contacts.xml contact id). People albums
exist ONLY in db3, so a person the ini/contacts.xml never named is
rescued from here: the name fills the registry gap (source-flagged via
the returned id set) and resolves already-parsed ini faces= regions
carrying that id. Virtual imagedata rows (filetype 1001) join
personalbumid -> person album and, via their thumbindex entry's parent
linkage, the photo the face was tagged on — used ONLY to surface
residue: oracle fixture 026 (Reset Faces) proves an absent ini faces=
line is an authoritative untag that leaves stale db3 rows behind, so a
db3 face with no matching ini entry is REPORTED, never resurrected.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts"))

import picasa_db  # noqa: E402

# imagedata.filetype of a virtual face-crop row (fixture 014: 1001 =
# 0x3e9; its thumbindex entry's ftype byte is the low byte, 0xe9).
FACE_FILETYPE = 1001
# albumdata.category of a people album (picasa-db3-validated.md: the
# stock ]unknownface row and per-person ]facealbum:<row> rows).
PERSON_ALBUM_CATEGORY = 8

_DRIVE_RE = re.compile(r"^[A-Za-z]:$")


def default_db3_dir() -> Path | None:
    """The machine-local Picasa 3.9 db3 directory, if this machine has
    one: %LocalAppData%\\Google\\Picasa2\\db3 (same discovery shape as
    default_contacts_xml / default_pal_dir in catalog.py; PicasaStarter
    relocations are the --db3 flag's job). None when the env var or the
    directory is absent — discovery is best-effort by design."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    p = Path(base) / "Google" / "Picasa2" / "db3"
    return p if p.is_dir() else None


def translate_db3_path(abs_path: str, root: Path) -> str | None:
    """One db3 absolute machine path -> a library-relative POSIX key,
    or None when the path does not sit under the library root.

    §8: drive letters in imported data are translated, never stored —
    the drive prefix is stripped from BOTH sides before comparing, so
    `Z:\\photos\\Trip\\a.jpg` joins a library rooted at `D:\\photos`.
    Components compare case-insensitively (db3 is written by Windows
    Picasa); the returned key keeps the db3 spelling, and the caller
    falls back to a casefolded catalog lookup for the rare mismatch.
    The root itself translates to ""."""
    p_parts = [c for c in abs_path.replace("\\", "/").split("/") if c]
    r_parts = [c for c in str(root).replace("\\", "/").split("/") if c]
    if p_parts and _DRIVE_RE.fullmatch(p_parts[0]):
        p_parts = p_parts[1:]
    if r_parts and _DRIVE_RE.fullmatch(r_parts[0]):
        r_parts = r_parts[1:]
    if len(p_parts) < len(r_parts):
        return None
    for pc, rc in zip(p_parts, r_parts):
        if pc.casefold() != rc.casefold():
            return None
    return "/".join(p_parts[len(r_parts):])


def _col(db3_dir: Path, table: str, column: str) -> "picasa_db.PmpColumn | None":
    """One pmp column, tolerantly — None when the file is absent or
    defective (fail-soft: a missing or corrupt db3 must degrade the
    rescue, never sink the scan)."""
    p = db3_dir / f"{table}_{column}.pmp"
    if not p.is_file():
        return None
    try:
        return picasa_db.read_pmp(p, strict=False)
    except (picasa_db.PmpError, OSError):
        return None


def _rescue_captions(db3_dir: Path, root: Path, photos: list, report) -> None:
    """Gap-fill Photo.caption from imagedata_caption.pmp, fail-soft.

    thumbindex row number is the imagedata row number. Every populated db3
    caption therefore has one of four visible outcomes: rescued into an empty
    Photo.caption, retained as a redundant lower-rank value, rejected as a
    divergent lower-rank value, or reported as unjoinable. Empty db3 values
    carry no information and are ignored.

    At scan time Photo.caption already carries the ini value. The existing
    indexer later replaces it with non-empty in-file XMP/IPTC metadata, so the
    complete §4 rank remains in-file > ini > db3 without a second parser here.
    """
    caption_col = _col(db3_dir, "imagedata", "caption")
    if caption_col is None:
        return
    captions = [
        (row, value) for row, value in enumerate(caption_col.values)
        if isinstance(value, str) and value
    ]
    if not captions:
        return

    ti_path = db3_dir / "thumbindex.db"
    if not ti_path.is_file():
        report.add("db3", "db3_unreadable", "thumbindex.db",
                   "imagedata carries caption data but thumbindex.db is "
                   "absent — db3 captions cannot be joined to photos this "
                   "run (fail-soft)")
        return
    try:
        entries = picasa_db.read_thumbindex(ti_path, strict=False)
        full_paths = picasa_db.thumbindex_full_paths(entries)
    except (picasa_db.ThumbIndexError, OSError):
        report.add("db3", "db3_unreadable", "thumbindex.db",
                   "thumbindex.db is unreadable — db3 captions cannot be "
                   "joined to photos this run (fail-soft)")
        return

    by_rel = {p.rel: p for p in photos}
    by_fold = {p.rel.casefold(): p for p in photos}
    for row, db3_caption in captions:
        abs_path = full_paths[row] if row < len(full_paths) else ""
        rel = translate_db3_path(abs_path, root) if abs_path else None
        photo = None
        if rel is not None:
            photo = by_rel.get(rel) or by_fold.get(rel.casefold())
        if photo is None:
            subject = abs_path or f"imagedata row {row}"
            report.add("db3", "db3_path_unresolved", subject,
                       "db3 carries a caption on a row that does not join "
                       "to a photo in this library — skipped (fail-soft)")
            continue

        if photo.caption is None or photo.caption == "":
            photo.caption = db3_caption
            report.add("db3", "db3_caption_rescued", photo.rel,
                       f"db3 caption filled the empty caption on {photo.rel} "
                       "(§4 gap-fill)")
        elif photo.caption == db3_caption:
            report.add("db3", "db3_caption_redundant", photo.rel,
                       f"db3 repeats the existing caption on {photo.rel} — "
                       "the higher-rank ini/in-file value is kept (§4)")
        else:
            report.add("db3", "db3_caption_conflict", photo.rel,
                       f"db3 caption diverges from the existing caption on "
                       f"{photo.rel} — the higher-rank ini/in-file value is "
                       "kept (§4; db3 fills gaps only)")


def rescue_people(db3_dir: Path, root: Path, photos: list,
                  registry: dict[str, str], report) -> set[str]:
    """The class-4 rescue, §4 gap-fill only. Mutates `registry` (adds
    names for contact ids no ini/contacts.xml source named) and the
    Photo.faces name slots that those rescued ids resolve; returns the
    set of contact ids whose names came from db3 (the source flag the
    People sidebar and the persisted catalog carry).

    Every non-gap outcome is an ImportReport entry, never applied:
    - db3_name_conflict: db3 names a contact the registry already names
      differently — the ini/contacts.xml name is kept (§4 rank);
    - db3_face_residue: a db3 virtual face row targets a photo whose ini
      has no matching faces= entry — absent ini faces= is an
      authoritative untag (oracle fixture 026), not a gap; the face is
      NOT resurrected;
    - db3_path_unresolved: a face row's parent photo path does not
      translate onto this library (or translates to no catalog photo).

    Fail-soft throughout: missing/corrupt db3 files just shrink what can
    be rescued (a fresh db3 legitimately has no albumdata at all)."""
    db3_dir = Path(db3_dir)
    rescued: set[str] = set()

    # cam.20 shares the established db3 invocation rather than widening
    # catalog.py's public surface: caption rescue is independent of whether
    # this db3 also happens to carry the person-album columns below.
    _rescue_captions(db3_dir, root, photos, report)

    cat_col = _col(db3_dir, "albumdata", "category")
    name_col = _col(db3_dir, "albumdata", "name")
    ids_col = _col(db3_dir, "albumdata", "albumcontactids")
    if cat_col is None or name_col is None or ids_col is None:
        return rescued  # no person albums recorded: nothing to rescue

    # album row -> (contact id, db3 name). The stock ]unknownface bucket
    # is category 8 with NO contact id — skipped (its faces are the
    # ini's ffffffffffffffff suggestions, which stay unnamed by design).
    persons: dict[int, tuple[str, str]] = {}
    for row, category in enumerate(cat_col.values):
        if category != PERSON_ALBUM_CATEGORY:
            continue
        cid_val = ids_col.get(row) or 0
        name = (name_col.get(row) or "").strip()
        if not cid_val or not name:
            continue
        cid = f"{cid_val:016x}"
        if cid == picasa_db.UNKNOWN_CONTACT:
            continue
        persons[row] = (cid, name)

    for row in sorted(persons):
        cid, name = persons[row]
        known = registry.get(cid)
        if known is None:
            registry[cid] = name
            rescued.add(cid)
            report.add("db3", "db3_person_rescued", cid,
                       f"db3 person album (row {row}) names contact {cid} "
                       f"“{name}” — no ini [Contacts2] or contacts.xml "
                       f"source; name rescued from db3 (§4 gap-fill)")
        elif known != name:
            report.add("db3", "db3_name_conflict", cid,
                       f"db3 person album names contact {cid} “{name}” but "
                       f"the ini/contacts.xml registry says “{known}” — db3 "
                       f"fills gaps only, the registry name is kept (§4)")

    # Resolve the name gap on already-parsed ini faces= regions: only
    # faces whose id was rescued above — an id the registry knew stays
    # exactly as the walk resolved it (including the None a non-ancestor
    # [Contacts2] definition leaves under downward inheritance).
    if rescued:
        for p in photos:
            if p.faces and any(n is None and cid in rescued
                               for _rect, cid, n in p.faces):
                p.faces = tuple(
                    (rect, cid,
                     registry[cid] if n is None and cid in rescued else n)
                    for rect, cid, n in p.faces)

    # Virtual face rows -> parent photos, for RESIDUE surfacing only
    # (never membership: the ini faces= line is the authority both ways).
    ft_col = _col(db3_dir, "imagedata", "filetype")
    pa_col = _col(db3_dir, "imagedata", "personalbumid")
    ti_path = db3_dir / "thumbindex.db"
    entries = None
    if ft_col is not None and pa_col is not None and ti_path.is_file():
        try:
            entries = picasa_db.read_thumbindex(ti_path, strict=False)
        except (picasa_db.ThumbIndexError, OSError):
            report.add("db3", "db3_unreadable", "thumbindex.db",
                       "thumbindex.db is unreadable — db3 face rows cannot "
                       "be joined to photos this run (names above still "
                       "rescued; fail-soft)")
    if entries is None:
        return rescued

    by_rel = {p.rel: p for p in photos}
    by_fold = {p.rel.casefold(): p for p in photos}
    unresolved_seen: set[str] = set()  # one note per path, not per face
    for row, ftype in enumerate(ft_col.values):
        if ftype != FACE_FILETYPE:
            continue
        person = persons.get(pa_col.get(row) or 0)
        if person is None:
            continue  # unknown-bucket face or an album row we skipped
        cid, db3_name = person
        shown = registry.get(cid, db3_name)
        # record number in thumbindex == record number in imagedata; the
        # face-crop entry's parent is the photo, the photo's parent the
        # folder entry carrying the absolute machine path.
        photo_e = None
        if row < len(entries) and entries[row].parent is not None \
                and 0 <= entries[row].parent < len(entries):
            photo_e = entries[entries[row].parent]
        folder_e = None
        if photo_e is not None and photo_e.parent is not None \
                and 0 <= photo_e.parent < len(entries):
            folder_e = entries[photo_e.parent]
        if photo_e is None or folder_e is None or not photo_e.name:
            report.add("db3", "db3_path_unresolved", f"imagedata row {row}",
                       f"db3 face of “{shown}” has no resolvable thumbindex "
                       f"parent linkage — skipped (fail-soft)")
            continue
        abs_path = folder_e.name + photo_e.name  # folder keeps its sep
        rel = translate_db3_path(abs_path, root)
        photo = None
        if rel is not None:
            photo = by_rel.get(rel) or by_fold.get(rel.casefold())
        if photo is None:
            if abs_path not in unresolved_seen:
                unresolved_seen.add(abs_path)
                report.add("db3", "db3_path_unresolved", abs_path,
                           f"db3 face of “{shown}” sits on a photo this "
                           f"library does not contain — cannot join; skipped")
            continue
        if not any(fcid == cid for _rect, fcid, _n in photo.faces):
            report.add("db3", "db3_face_residue", photo.rel,
                       f"db3 carries a face of “{shown}” ({cid}) on "
                       f"{photo.rel} but the ini has no matching faces= "
                       f"entry — an absent ini face is an authoritative "
                       f"untag (oracle fixture 026); db3 residue not "
                       f"resurrected (§4 gap-fill only)")
    return rescued
