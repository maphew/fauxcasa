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

Remaining tracer-scope gaps (see apps/desktop-python/README.md): EXIF orientation is
applied at decode (Qt/PIL auto-transform, composed with the rotate= user
turns), but faces-in-XMP, geotags, and in-file dates are not ingested.
The folder-level Hidden Folders category IS now honored: a folder whose
own [Picasa] section carries `P2category=Hidden Folders` hides all of its
photos, mirroring per-photo hidden=yes (oracle fixture 017) and the stash
treatment — see _is_folder_hidden.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts"))

import picasa_db  # noqa: E402

# Must match scripts/make-thumbcache.py EXTS exactly (cache-order parity).
EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}

INI_NAMES = (".picasa.ini", "Picasa.ini", "picasa.ini")


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
    star: bool = False
    caption: str | None = None
    keywords: tuple[str, ...] = ()
    rotate: int = 0  # quarter-turns clockwise, from rotate=rotate(N)
    hidden: bool = False
    albums: tuple[str, ...] = ()
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
                 scan_filter: ScanFilter | None = None) -> Catalog:
    root = root.resolve()
    files = walk_library(root, scan_filter)

    photos: list[Photo] = []
    folders: dict[str, Folder] = {}
    albums: dict[str, Album] = {}
    # folder_rel -> (ini, name.lower() -> merged section) or None
    ini_by_folder: dict[str, tuple | None] = {}

    for p, rel in zip(files, rel_paths(root, files)):
        folder_rel, _, name = rel.rpartition("/")
        photo = Photo(rel=rel, folder=folder_rel, name=name)
        if _is_stashed(folder_rel):
            photo.visible = False

        entry = ini_by_folder.get(folder_rel, False)
        if entry is False:
            ini = _read_folder_ini(p.parent)
            entry = (ini, _section_map(ini)) if ini is not None else None
            ini_by_folder[folder_rel] = entry
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
            photo.star = _flag(sec, "star")  # presence-only key
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
            al = sec.get("albums")
            if al:
                photo.albums = tuple(
                    a.strip().lower() for a in al.split(",") if a.strip()
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

    return Catalog(root=root, photos=photos, folders=folders, albums=albums)


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
CATALOG_VERSION = 3


def _photo_to_row(p: Photo) -> dict:
    row: dict = {"r": p.rel}
    if p.star:
        row["s"] = 1
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
            p = Photo(
                rel=rel, folder=folder, name=name,
                star=bool(row.get("s")), caption=row.get("c"),
                keywords=tuple(row.get("k", ())), rotate=row.get("o", 0),
                hidden=bool(row.get("h")), albums=tuple(row.get("a", ())),
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
    except (KeyError, TypeError, AttributeError, ValueError):
        return None

    return Catalog(root=root, photos=photos, folders=folders, albums=albums)


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
