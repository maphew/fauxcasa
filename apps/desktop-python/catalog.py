"""Library catalog for the tracer app: scan a library root in place and
attach Picasa metadata from .picasa.ini sidecars.

The walk rule (image extensions only, sorted by pathlib's Path ordering
— i.e. path COMPONENT order, which differs from a sort of the joined
POSIX string when a folder name extends past another at a '/' boundary)
is identical to scripts/make-thumbcache.py so that catalog order ==
thumbnail-cache entry order and an fcache index maps 1:1 to a catalog
photo. Scanning never writes into the library (N1); metadata parsing
reuses scripts/picasa_db.py, the project's researched Picasa-format
reader.

Captions/keywords precedence (§4 tier-1): scan_library fills
caption/keywords from the ini here; the indexer (thumbcache.build_cache,
via apps/desktop-python/inmeta.py) then overrides them with a JPEG's in-file
XMP/IPTC values when present — real Picasa stores JPEG captions/keywords
in-file and uses ini caption=/keywords= only for formats with no
XMP/IPTC home. So a freshly walked but not-yet-indexed catalog shows
ini-only captions; once indexed, the persisted catalog and warm starts
carry the merged result. Adopt mode (--thumbs) is the exception: it binds
an external thumbnail cache without running the indexer, so its catalog
stays ini-only (the in-file read piggybacks on the index's file reads,
which adopt mode skips by design — N4). See apps/desktop-python/README.md.

Dates/GPS/Rating follow the same two-pass shape (fauxcasa-cam.9/.10/.11,
via apps/desktop-python/metareader.py — the exiv2 seam): scan_library fills
geotag from the ini ``geotag=lat,lon`` key and the 0-5 star count from
``star=yes`` (legacy import = 1, §3 star authority); the indexer then
overrides with in-file values when the file carries them — EXIF
DateTimeOriginal/DateTime -> date_taken (the ini has no per-photo date
key), EXIF GPS -> geotag, XMP Rating 1-5 -> star — because in-file
metadata wins for tier-1 data per §4 (oracle hardening of that precedence
is fauxcasa-ed5.9's). The adopt-mode ini-only caveat applies identically.

Faces/people (§3 People first-class, read-only slice): scan_library parses
per-photo ini `faces=` regions via picasa_db.parse_faces, harvests
[Contacts2] id->name tables with the documented downward-inheritance rule
(an ancestor folder's ini names contacts for its whole subtree; the nearest
definition wins), and resolves each face to a display name. A machine-local
contacts.xml (see load_contacts_xml / default_contacts_xml) wins over
[Contacts2] on name conflicts per §4. A face whose contact id is
UNKNOWN_CONTACT (unconfirmed suggestion) or that no source names stays
unnamed (name None) — the suggested-vs-confirmed distinction this
read-only slice carries.

Remaining tracer-scope gaps (see apps/desktop-python/README.md): EXIF orientation is
applied at decode (Qt/PIL auto-transform, composed with the rotate= user
turns), but faces-in-XMP is not ingested (fauxcasa-cam.5).
The folder-level Hidden Folders category IS now honored: a folder whose
own [Picasa] section carries `P2category=Hidden Folders` hides all of its
photos, mirroring per-photo hidden=yes (oracle fixture 017) and the stash
treatment — see _is_folder_hidden.
"""

from __future__ import annotations

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts"))

import picasa_db  # noqa: E402

# Must match scripts/make-thumbcache.py EXTS exactly (cache-order parity).
EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}

INI_NAMES = (".picasa.ini", "Picasa.ini", "picasa.ini")

# One ini face tag: (rect, contact id, display name or None). rect =
# (left, top, right, bottom) fractions of the STORED pixels — rotate= does
# NOT transform them and EXIF orientation is the consumer's job
# (picasa-ini-format.md "faces="); name None = suggested/unnamed.
FaceTag = tuple[tuple[float, float, float, float], str, str | None]


@dataclass(frozen=True)
class ScanFilter:
    min_width: int = 0
    min_height: int = 0
    max_width: int = 0
    max_height: int = 0

    @property
    def active(self) -> bool:
        return bool(self.min_width or self.min_height
                    or self.max_width or self.max_height)

    def cache_key(self) -> str:
        if not self.active:
            return ""
        return ("scan:"
                f"min={self.min_width}x{self.min_height};"
                f"max={self.max_width}x{self.max_height}")


@dataclass
class Photo:
    rel: str  # library-relative POSIX path
    folder: str  # library-relative POSIX folder path ("" = root)
    name: str
    # Star COUNT, 0-5 (§3 star authority; fauxcasa-cam.11): ini star=yes
    # imports as 1, in-file XMP Rating 1-5 as that count, 0 = unstarred.
    # Was a bool; every consumer that treated it as truthy (badge, Starred
    # view, status ★) still works — count >= 1 is "starred".
    star: int = 0
    caption: str | None = None
    keywords: tuple[str, ...] = ()
    rotate: int = 0  # quarter-turns clockwise, from rotate=rotate(N)
    hidden: bool = False
    albums: tuple[str, ...] = ()
    faces: tuple[FaceTag, ...] = ()
    # Capture date, canonical "YYYY-MM-DDTHH:MM:SS" from in-file EXIF
    # (metareader, fauxcasa-cam.9). Year is UNBOUNDED (§6 footgun 16:
    # scanned photos predate 1903). None until indexed / when absent;
    # consumers may fall back to mtime for grouping (q6l.11's call).
    date_taken: str | None = None
    # Signed decimal (lat, lon) — §3 geotag v1: read, preserve, display
    # (fauxcasa-cam.10). ini geotag= at scan; in-file EXIF GPS overrides
    # at index (§4 tier-1: in-file wins for standard metadata).
    geotag: tuple[float, float] | None = None
    # Picasa stashes pre-edit originals in .picasaoriginals/; those files
    # are catalog entries (cache-order parity) but never shown in the grid.
    visible: bool = True
    # Identity + staleness signals, filled by the indexer (build_cache),
    # persisted in the catalog, and used by reconcile (cheap size+mtime
    # diff) and N6 identity (sha256). -1 / None until indexed.
    size: int = -1
    mtime: int = -1
    sha256: str | None = None


@dataclass
class Folder:
    rel: str
    title: str
    description: str | None = None
    photo_count: int = 0  # visible photos only
    total_count: int = 0  # all photos incl. hidden/stash (reveal-mode counts)
    # The whole folder sits in Picasa's built-in "Hidden Folders" collection
    # ([Picasa] P2category=Hidden Folders): every photo under it is forced
    # invisible (like a stash folder), so the sidebar can surface it only
    # under reveal mode and distinguish it from a normal folder.
    folder_hidden: bool = False


@dataclass
class Album:
    uid: str
    name: str
    date: str | None = None
    description: str | None = None
    members: list[int] = field(default_factory=list)  # catalog photo indices


@dataclass
class Catalog:
    root: Path
    photos: list[Photo]
    folders: dict[str, Folder]  # insertion order = display order
    albums: dict[str, Album]
    # Merged contact registry, 16-hex id -> display name: every folder's
    # [Contacts2] (first-wins across folders, like duplicate album defs)
    # overridden by the machine-local contacts.xml (§4: xml wins on name
    # conflicts). Per-face names on Photo.faces are resolved through the
    # folder inheritance chain instead, so they stay right even where
    # folders disagree; this registry is the flat, persistable union.
    contacts: dict[str, str] = field(default_factory=dict)

    @property
    def visible_count(self) -> int:
        return sum(1 for p in self.photos if p.visible)


def _image_size(path: Path) -> tuple[int, int] | None:
    """Read dimensions without decoding pixels. None means unreadable/unknown,
    and callers should keep the file so the indexer can surface its error."""
    try:
        from PySide6.QtGui import QImageReader
    except ImportError:
        return None
    try:
        size = QImageReader(str(path)).size()
    except OSError:
        return None
    if not size.isValid():
        return None
    return size.width(), size.height()


def _passes_scan_filter(path: Path, scan_filter: ScanFilter | None) -> bool:
    if scan_filter is None or not scan_filter.active:
        return True
    dims = _image_size(path)
    if dims is None:
        return True
    w, h = dims
    if scan_filter.min_width and w < scan_filter.min_width:
        return False
    if scan_filter.min_height and h < scan_filter.min_height:
        return False
    if scan_filter.max_width and w > scan_filter.max_width:
        return False
    if scan_filter.max_height and h > scan_filter.max_height:
        return False
    return True


def walk_library(root: Path,
                 scan_filter: ScanFilter | None = None) -> list[Path]:
    """The shared walk rule: sorted(Path) = path-component order. Any
    change here must also land in scripts/make-thumbcache.py or caches
    stop binding (the shipped benchmark cache uses this order)."""
    files = sorted(
        p for p in root.rglob("*") if p.suffix.lower() in EXTS and p.is_file()
    )
    if scan_filter is None or not scan_filter.active:
        return files
    return [p for p in files if _passes_scan_filter(p, scan_filter)]


def _read_folder_ini(folder: Path) -> picasa_db.PicasaIni | None:
    for name in INI_NAMES:
        p = folder / name
        if p.is_file():
            try:
                return picasa_db.read_picasa_ini(p)
            except OSError:
                continue  # unreadable file; try the legacy names
    return None


def _section_map(ini: picasa_db.PicasaIni) -> dict[str, picasa_db.IniSection]:
    """name.lower() -> section, with duplicate sections merged in order
    (real-world inis contain duplicates; IniSection.get is first-key-wins,
    and concatenating preserves that across the duplicates)."""
    out: dict[str, picasa_db.IniSection] = {}
    for sec in ini.sections:
        key = sec.name.lower()
        prev = out.get(key)
        if prev is None:
            out[key] = sec
        else:
            prev = picasa_db.IniSection(name=prev.name,
                                        items=list(prev.items))
            prev.items.extend(sec.items)
            out[key] = prev
    return out


def _is_stashed(folder_rel: str) -> bool:
    """Stash folders are not user-browsable: dot-folders (.picasaoriginals,
    Picasa 3) and the legacy `Originals` name (Picasa 1.x/2.x)."""
    return any(part.startswith(".") or part == "Originals"
               for part in folder_rel.split("/") if part)


def _flag(sec: picasa_db.IniSection, key: str) -> bool:
    return (sec.get(key) or "").strip().lower() == "yes"


def _parse_ini_geotag(value: str) -> tuple[float, float] | None:
    """One ini ``geotag=lat,lon`` value (decimal floats, per
    picasa-ini-format.md) -> signed (lat, lon). Fail-soft per line
    (§4 robustness rule): a malformed or out-of-range value is None,
    never an exception."""
    parts = value.split(",")
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (abs(lat) <= 90.0 and abs(lon) <= 180.0):
        return None
    return (lat, lon)


def format_geotag(geotag: tuple[float, float]) -> str:
    """The coordinates readout (§3 geotag v1: read, preserve, display),
    shared by the status bar and the viewer info line so both surfaces
    show identical text: signed decimal degrees, 5 places (~1 m)."""
    return f"{geotag[0]:.5f}, {geotag[1]:.5f}"


def format_date_taken(date_taken: str) -> str:
    """Human form of the canonical capture date: the ISO 'T' becomes a
    space; no other reinterpretation (footgun 16: never a year floor)."""
    return date_taken.replace("T", " ")


# Picasa hides a WHOLE folder by putting it in the built-in "Hidden Folders"
# collection, recorded in the folder's own [Picasa] section as
# `P2category=Hidden Folders` (the sibling of the normal `Folders on Disk`).
# Matched defensively: trimmed, case-insensitive, against the exact value
# "Hidden Folders" — an absent or any other P2category (Folders on Disk,
# Exported Pictures, Projects (internal), …) is NOT folder-hidden, and a
# folder with no [Picasa] section reads as not hidden.
#
# ORACLE-VERIFIED by fixture 032-hide-folder (fauxcasa-8rl): driving real
# Picasa 3.9 to hide a folder writes exactly `[Picasa]` + `P2category=Hidden
# Folders` to the folder's own .picasa.ini (the string mark this matcher
# reads), with a db3 mirror (the folder's albumdata_category row flips 2
# 'Folders on Disk' -> 7 'Hidden Folders') and NO per-photo hidden=yes. So the
# case-insensitive "Hidden Folders" match below is exactly right as-is.
_HIDDEN_FOLDERS_CATEGORY = "hidden folders"


def _is_folder_hidden(psec: picasa_db.IniSection | None) -> bool:
    if psec is None:
        return False
    return (psec.get("P2category") or "").strip().lower() \
        == _HIDDEN_FOLDERS_CATEGORY


# ---- faces/people: contacts.xml + [Contacts2] harvest --------------------

_CONTACT_ID_RE = re.compile(r"[0-9a-fA-F]{1,16}")


def load_contacts_xml(path: Path) -> dict[str, str]:
    """Parse Picasa's machine-local contacts.xml -> {16-hex id: name}.

    Format per oracle fixture 014: <contacts><contact id="<16-hex>"
    name="..." modified_time="..." local_contact="1"/></contacts>. Ids are
    zero-padded to 16 so they join ini faces= ids (padded by parse_faces)
    and [Contacts2] keys. Defensive fail-soft: a missing, unreadable, or
    malformed file yields {}, and a defective entry (no usable id/name)
    skips that entry only — the machine-local contacts file is an
    enrichment, never a gate, and it is strictly read-only here."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return {}
    out: dict[str, str] = {}
    for el in root.iter("contact"):
        cid = (el.get("id") or "").strip().lower()
        name = (el.get("name") or "").strip()
        if not name or not _CONTACT_ID_RE.fullmatch(cid):
            continue
        out[cid.zfill(16)] = name
    return out


def default_contacts_xml() -> Path | None:
    """The machine-local Picasa 3.9 contacts.xml, if this machine has one:
    %LocalAppData%\\Google\\Picasa2\\contacts\\contacts.xml (the observed
    Windows location; a Wine prefix maps the same path). None when the env
    var or the file is absent — discovery is best-effort by design."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    p = Path(base) / "Google" / "Picasa2" / "contacts" / "contacts.xml"
    return p if p.is_file() else None


def _harvest_contacts2(secmap: dict, out: dict[str, str]) -> None:
    """Merge one folder's [Contacts2] id->name entries into `out` (callers
    pass the ancestor merge in, so this folder's entries override — the
    nearest definition wins under the downward-inheritance rule). Value
    grammar: `<Display Name>;;` — split on ';', field 0. Duplicate keys
    keep the first, matching IniSection.get. Legacy [Contacts] values
    (`<account>_lh,<web id hex>`) carry NO display name — the format doc
    is explicit ("no names; need contacts.xml") — so that section is
    deliberately not read: its ids resolve through contacts.xml, and its
    web-id values must never surface as person names. Fail-soft per line:
    a malformed id or empty name skips that entry only."""
    sec = secmap.get("contacts2")
    if sec is None:
        return
    seen: set[str] = set()
    for key, value in sec.items:
        cid = key.strip().lower()
        if not _CONTACT_ID_RE.fullmatch(cid):
            continue
        cid = cid.zfill(16)
        if cid in seen:
            continue
        seen.add(cid)
        name = value.split(";", 1)[0].strip()
        if name:
            out[cid] = name


def rel_paths(root: Path, files: list[Path]) -> list[str]:
    """Library-relative POSIX paths — exactly relative_to().as_posix(),
    but via string-slicing the constant root prefix: two relative_to()
    calls per file cost whole seconds at 100k. True roots ('/', 'D:\\',
    UNC shares) keep their trailing separator in str(), so the +1 for
    the joining separator is conditional — and a one-time parity probe
    guards the shortcut wholesale, falling back to relative_to()."""
    if not files:
        return []
    prefix = str(root)
    plen = len(prefix) if prefix.endswith(os.sep) else len(prefix) + 1
    sep = os.sep

    def fast(p: Path) -> str:
        s = str(p)[plen:]
        return s.replace(sep, "/") if sep != "/" else s

    if fast(files[0]) != files[0].relative_to(root).as_posix():
        return [p.relative_to(root).as_posix() for p in files]
    return [fast(p) for p in files]


def scan_library(root: Path,
                 scan_filter: ScanFilter | None = None,
                 contacts: dict[str, str] | None = None) -> Catalog:
    """Walk `root` and build the catalog. `contacts` is the machine-local
    contacts.xml id->name map (load_contacts_xml); per §4 it wins over the
    ini [Contacts2] tables when both name a contact."""
    root = root.resolve()
    files = walk_library(root, scan_filter)
    contacts = contacts or {}

    photos: list[Photo] = []
    folders: dict[str, Folder] = {}
    albums: dict[str, Album] = {}
    # folder_rel -> (ini, name.lower() -> merged section) or None
    ini_by_folder: dict[str, tuple | None] = {}
    # folder_rel -> merged [Contacts2] id->name for that folder's subtree
    contacts_by_folder: dict[str, dict[str, str]] = {}

    def folder_ini(folder_rel: str) -> tuple | None:
        entry = ini_by_folder.get(folder_rel, False)
        if entry is False:
            ini = _read_folder_ini(root / folder_rel if folder_rel else root)
            entry = (ini, _section_map(ini)) if ini is not None else None
            ini_by_folder[folder_rel] = entry
        return entry

    def folder_contacts(folder_rel: str) -> dict[str, str]:
        """The [Contacts2] id->name table in effect for `folder_rel`:
        ancestors' entries inherited downward (picasa-ini-format.md — ids
        are 'inherited downward from ancestor folders' inis'), the nearest
        definition winning. Ancestor inis are read on demand — a root-level
        ini with only a [Contacts2] table names faces in photo-bearing
        subfolders even though the walk never visits the root itself."""
        got = contacts_by_folder.get(folder_rel)
        if got is not None:
            return got
        if folder_rel:
            parent = folder_rel.rsplit("/", 1)[0] if "/" in folder_rel else ""
            merged = dict(folder_contacts(parent))
        else:
            merged = {}
        entry = folder_ini(folder_rel)
        if entry is not None:
            _harvest_contacts2(entry[1], merged)
        contacts_by_folder[folder_rel] = merged
        return merged

    for p, rel in zip(files, rel_paths(root, files)):
        folder_rel, _, name = rel.rpartition("/")
        photo = Photo(rel=rel, folder=folder_rel, name=name)
        if _is_stashed(folder_rel):
            photo.visible = False

        entry = folder_ini(folder_rel)
        secmap = entry[1] if entry is not None else {}

        if folder_rel not in folders:
            # The folder's on-disk name IS its display name (N1: the
            # sidebar mirrors the filesystem). Picasa's ini [Picasa]
            # name= just snapshots that name and goes stale on renames.
            title = folder_rel.rsplit("/", 1)[-1] if folder_rel \
                else (root.name or str(root))  # name is '' at a true root
            psec = secmap.get("picasa")
            # "" (empty description= line) normalized to None so a loaded
            # catalog matches a walked one (an absent key reads as None).
            desc = (psec.get("description") or None) if psec else None
            folders[folder_rel] = Folder(rel=folder_rel, title=title,
                                         description=desc,
                                         folder_hidden=_is_folder_hidden(psec))

        sec = secmap.get(name.lower())
        if sec is not None:
            # star= is a presence-only key with no count: legacy star=yes
            # imports as 1 star (§3). XMP Rating may raise it at index time.
            photo.star = 1 if _flag(sec, "star") else 0
            photo.caption = sec.get("caption") or None  # "" -> None
            kw = sec.get("keywords")
            if kw:
                photo.keywords = tuple(
                    k.strip() for k in kw.split(",") if k.strip()
                )
            rot = sec.get("rotate")
            if rot:
                try:
                    photo.rotate = picasa_db.parse_rotate(rot) % 4
                except ValueError:
                    pass
            photo.hidden = _flag(sec, "hidden")
            gt = sec.get("geotag")
            if gt:
                # The non-EXIF geotag source (fauxcasa-cam.10): in-file GPS,
                # when present, overrides this at index time (§4 tier-1).
                photo.geotag = _parse_ini_geotag(gt)
            al = sec.get("albums")
            if al:
                photo.albums = tuple(
                    a.strip().lower() for a in al.split(",") if a.strip()
                )
            fv = sec.get("faces")
            if fv:
                try:
                    parsed = picasa_db.parse_faces(fv)
                except ValueError:
                    parsed = []  # fail-soft per-line: skip a bad faces= line
                if parsed:
                    local = folder_contacts(folder_rel)
                    # contacts.xml wins over [Contacts2] (§4); an
                    # UNKNOWN_CONTACT id (unconfirmed suggestion) or an id
                    # no source names stays unnamed (None).
                    photo.faces = tuple(
                        (rect, cid,
                         None if cid == picasa_db.UNKNOWN_CONTACT
                         else contacts.get(cid) or local.get(cid))
                        for rect, cid in parsed
                    )

        # A whole folder in the "Hidden Folders" collection hides every
        # photo under it, exactly like per-photo hidden=yes or a stash dir.
        if photo.hidden or folders[folder_rel].folder_hidden:
            photo.visible = False
        photos.append(photo)

    # Album definitions: [.album:<uid>] sections live in each member
    # folder's ini; collect once per uid, then resolve membership tokens
    # (orphaned tokens are documented ini/db drift — skip silently).
    # When folders carry diverging duplicate definitions of one uid,
    # first-wins in walk order — a known-arbitrary choice; Picasa's own
    # resolution rule is unobserved (no oracle fixture yet).
    for entry in ini_by_folder.values():
        if entry is None:
            continue
        for sec in entry[0].sections:
            if sec.name.lower().startswith(".album:"):
                uid = sec.name.split(":", 1)[1].strip().lower()
                if uid and uid not in albums:
                    albums[uid] = Album(
                        uid=uid,
                        name=sec.get("name") or uid[:8],
                        date=sec.get("date"),
                        description=sec.get("description"),
                    )

    for i, photo in enumerate(photos):
        folders[photo.folder].total_count += 1  # reveal-mode count
        if not photo.visible:
            continue
        folders[photo.folder].photo_count += 1
        for uid in photo.albums:
            if uid in albums:
                albums[uid].members.append(i)

    # Flat contact registry: every walked folder's effective [Contacts2]
    # table (first-wins across folders, like duplicate album definitions),
    # with contacts.xml names overriding on conflict (§4).
    registry: dict[str, str] = {}
    for folder_rel in folders:
        for cid, cname in folder_contacts(folder_rel).items():
            registry.setdefault(cid, cname)
    registry.update(contacts)

    return Catalog(root=root, photos=photos, folders=folders, albums=albums,
                   contacts=registry)


# ---- persistent catalog (load-without-walking, §7 cold start) ------------
#
# The full catalog (display metadata + structure + per-file signals)
# serialized to JSON so a warm start rebuilds the grid without walking the
# library. JSON is deliberate: human-readable and language-neutral, so the
# format survives a stack swap (N3). Folder/name/visible and folder
# title/photo_count are DERIVED on load (never stored — they'd just drift);
# only what the walk+ini parse can't reproduce is persisted (folder
# descriptions, album definitions, per-file signals). Short keys keep the
# file small, but a hex sha256 dominates each row, so this lands well above
# the spec's ~50 B/photo binary-catalog target — a compact binary catalog
# is future work (a tracer is JSON-first).

# Bump when a code change alters what an indexed catalog should contain, so
# load_catalog rejects pre-change machine-local catalogs and main() does a
# cold walk + rebuild instead of warm-starting stale data. v2: the indexer
# now reads in-file XMP/IPTC captions/keywords and bakes EXIF orientation
# into the thumbnail (fauxcasa-w9e) — a v1 catalog has neither, and nothing
# in the cheap size/mtime drift check would notice (the files are unchanged;
# only our interpretation of them changed). The fcache MAGIC version is left
# at 1 on purpose: the packed thumbnail format is unchanged, and the shipped
# benchmark .fcache stays adoptable (its synthetic photos carry no
# Orientation tag, so the bake is a no-op).
# v3: the folder-level "Hidden Folders" category (folder [Picasa]
# P2category=Hidden Folders) now forces its photos invisible and is
# persisted as a `hidden_folders` list — a v2 catalog has neither, so it
# would wrongly show a hidden folder's photos; reject it and cold-rebuild.
# v4: ini faces= regions + resolved contact names (fauxcasa-cam.1/.2) are
# ingested and persisted (per-photo `f` rows + a `contacts` registry) — a
# v3 catalog has neither, so a warm start would silently show an empty
# People surface; reject it and cold-rebuild.
# v5: in-file capture date, GPS, and XMP Rating (fauxcasa-cam.9/.10/.11)
# are ingested (ini geotag= at scan, metareader at index) and persisted
# (per-photo `d`/`g` rows; `s` is now the 0-5 star COUNT, not a 0/1 flag) —
# a v4 catalog has none of these, so a warm start would silently drop
# dates/geotags and cap every star count at 1; reject and cold-rebuild.
CATALOG_VERSION = 5


def _photo_to_row(p: Photo) -> dict:
    row: dict = {"r": p.rel}
    if p.star:
        row["s"] = p.star  # 0-5 count since v5 (0 = key absent)
    if p.caption:
        row["c"] = p.caption
    if p.keywords:
        row["k"] = list(p.keywords)
    if p.rotate:
        row["o"] = p.rotate
    if p.hidden:
        row["h"] = 1
    if p.albums:
        row["a"] = list(p.albums)
    if p.faces:
        # rect fractions are n/65536 — exact binary fractions, so they
        # round-trip through JSON floats byte-identically.
        row["f"] = [[list(rect), cid, name] for rect, cid, name in p.faces]
    if p.date_taken:
        row["d"] = p.date_taken
    if p.geotag is not None:
        # decimal degrees rounded to 6 places at parse time, so the JSON
        # float round-trip is exact
        row["g"] = list(p.geotag)
    if p.size >= 0:
        row["z"] = p.size
    if p.mtime >= 0:
        row["m"] = p.mtime
    if p.sha256:
        row["x"] = p.sha256
    return row


def save_catalog(catalog: Catalog, path: Path) -> None:
    """Atomically serialize the catalog to `path` (write-temp-rename)."""
    data = {
        "version": CATALOG_VERSION,
        "library": str(catalog.root),
        "photos": [_photo_to_row(p) for p in catalog.photos],
        # only folders that carry a description (title/count are derived)
        "folders": {f.rel: f.description
                    for f in catalog.folders.values() if f.description},
        # folder-level "Hidden Folders" membership: can't be re-derived on
        # load (the warm path never re-reads inis), so persist it explicitly
        # — it drives both per-photo visibility and Folder.folder_hidden.
        "hidden_folders": [f.rel for f in catalog.folders.values()
                           if f.folder_hidden],
        # contact id -> display name registry (already contacts.xml-merged
        # at scan time; the warm path never re-reads inis or contacts.xml)
        "contacts": catalog.contacts,
        "albums": [
            {"uid": a.uid, "name": a.name, "date": a.date,
             "description": a.description, "members": a.members}
            for a in catalog.albums.values()
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".catalog.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def load_catalog(path: Path, root: Path) -> Catalog | None:
    """Reconstruct a Catalog from a persisted file, or None if absent,
    unreadable, or an older/foreign/corrupt format (-> caller does a cold
    walk). Derives folder/name/visible and folder title/photo_count the
    same way scan_library does. Caveat: empty-string caption/description
    are normalized to None on both paths, so a loaded catalog matches a
    freshly walked-AND-indexed one for every field consumers read — the
    persisted catalog carries the merged in-file caption/keywords that the
    indexer wrote, which a scan-only walk has not yet applied (see the
    module docstring)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != CATALOG_VERSION:
        return None
    root = root.resolve()
    rows = data.get("photos")
    if not isinstance(rows, list):
        return None

    # Any structural defect in a version-tagged file (the case the version
    # gate is meant to guard) degrades to a cold walk rather than crashing
    # startup — main() calls this unguarded.
    try:
        hidden_folders = data.get("hidden_folders", [])
        if not isinstance(hidden_folders, list):
            hidden_folders = []
        hidden_folders = set(hidden_folders)

        photos: list[Photo] = []
        for row in rows:
            rel = row["r"]
            folder, _, name = rel.rpartition("/")
            g = row.get("g")
            p = Photo(
                rel=rel, folder=folder, name=name,
                star=int(row.get("s") or 0), caption=row.get("c"),
                keywords=tuple(row.get("k", ())), rotate=row.get("o", 0),
                hidden=bool(row.get("h")), albums=tuple(row.get("a", ())),
                faces=tuple((tuple(rect), cid, fname)
                            for rect, cid, fname in row.get("f", ())),
                date_taken=row.get("d"),
                geotag=(float(g[0]), float(g[1])) if g else None,
                size=row.get("z", -1), mtime=row.get("m", -1),
                sha256=row.get("x"),
            )
            p.visible = (not p.hidden and not _is_stashed(folder)
                         and folder not in hidden_folders)
            photos.append(p)

        folder_desc = data.get("folders", {})
        if not isinstance(folder_desc, dict):
            folder_desc = {}
        folders: dict[str, Folder] = {}
        for p in photos:
            if p.folder not in folders:
                title = p.folder.rsplit("/", 1)[-1] if p.folder \
                    else (root.name or str(root))
                folders[p.folder] = Folder(
                    rel=p.folder, title=title,
                    description=folder_desc.get(p.folder),
                    folder_hidden=p.folder in hidden_folders)
            folders[p.folder].total_count += 1  # reveal-mode count
            if p.visible:
                folders[p.folder].photo_count += 1

        albums: dict[str, Album] = {}
        for a in data.get("albums", []):
            albums[a["uid"]] = Album(
                uid=a["uid"], name=a["name"], date=a.get("date"),
                description=a.get("description"),
                members=list(a.get("members", [])),
            )

        contacts = data.get("contacts", {})
        if not isinstance(contacts, dict):
            contacts = {}
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        return None

    return Catalog(root=root, photos=photos, folders=folders, albums=albums,
                   contacts=contacts)


@dataclass
class Drift:
    """Cheap-signal (size+mtime) diff of a persisted catalog vs the live
    library — the N6 'cheap signals first' check, no hashing."""
    added: int = 0
    removed: int = 0
    modified: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.modified)

    def summary(self) -> str:
        return (f"+{self.added} added, -{self.removed} removed, "
                f"~{self.modified} modified")


def reconcile_walk(catalog: Catalog, root: Path,
                   scan_filter: ScanFilter | None = None,
                   cancel=None) -> Drift | None:
    """Fresh walk + stat, compared to the catalog's stored signals.
    Photos whose stored signal is absent (-1) can't be compared and are
    never counted as modified (only a genuine size/mtime change is).
    Returns None if the cancel event is set mid-walk, so shutdown can
    reap the background thread promptly instead of waiting out a 100k
    stat-walk on the join timeout."""
    root = root.resolve()
    files = walk_library(root, scan_filter)
    fresh: dict[str, tuple[int, int]] = {}
    for i, (p, rel) in enumerate(zip(files, rel_paths(root, files))):
        if cancel is not None and i % 512 == 0 and cancel.is_set():
            return None
        try:
            st = p.stat()
            fresh[rel] = (st.st_size, int(st.st_mtime))
        except OSError:
            fresh[rel] = (-1, -1)
    old = {p.rel: (p.size, p.mtime) for p in catalog.photos}
    drift = Drift()
    for rel, sig in fresh.items():
        if rel not in old:
            drift.added += 1
        elif old[rel] != (-1, -1) and old[rel] != sig:
            drift.modified += 1
    for rel in old:
        if rel not in fresh:
            drift.removed += 1
    return drift
