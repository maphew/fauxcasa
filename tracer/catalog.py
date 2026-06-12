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

Known tracer-scope gaps (see tracer/README.md): in-file IPTC/XMP
captions/keywords are not read (real Picasa stores JPEG captions in
IPTC, ini caption= covers other formats), EXIF orientation is not
applied anywhere (uniformly stored-pixel rendering, matching the
thumbnail builders), and the folder-level Hidden Folders category is
ignored — only per-photo hidden=yes (oracle fixture 017) is honored.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

import picasa_db  # noqa: E402

# Must match scripts/make-thumbcache.py EXTS exactly (cache-order parity).
EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}

INI_NAMES = (".picasa.ini", "Picasa.ini", "picasa.ini")


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


@dataclass
class Folder:
    rel: str
    title: str
    description: str | None = None
    photo_count: int = 0  # visible photos only


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


def walk_library(root: Path) -> list[Path]:
    """The shared walk rule: sorted(Path) = path-component order. Any
    change here must also land in scripts/make-thumbcache.py or caches
    stop binding (the shipped benchmark cache uses this order)."""
    return sorted(
        p for p in root.rglob("*") if p.suffix.lower() in EXTS and p.is_file()
    )


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


def scan_library(root: Path) -> Catalog:
    root = root.resolve()
    files = walk_library(root)

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
            desc = psec.get("description") if psec is not None else None
            folders[folder_rel] = Folder(rel=folder_rel, title=title,
                                         description=desc)

        sec = secmap.get(name.lower())
        if sec is not None:
            photo.star = _flag(sec, "star")  # presence-only key
            photo.caption = sec.get("caption")
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

        if photo.hidden:
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
        if not photo.visible:
            continue
        folders[photo.folder].photo_count += 1
        for uid in photo.albums:
            if uid in albums:
                albums[uid].members.append(i)

    return Catalog(root=root, photos=photos, folders=folders, albums=albums)
