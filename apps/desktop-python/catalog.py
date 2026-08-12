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
carry the merged result. Adopt mode (--thumbs) binds an external
thumbnail cache without running the indexer, so its catalog STARTS
ini-only — a background backfill pass (thumbcache.backfill_catalog,
fauxcasa-cam.12) then runs the read side of the indexer (no thumbnail
work) to fill the identity signals and in-file metadata, resumable via
the backfill_state/backfill_cursor fields below. See
apps/desktop-python/README.md.

Dates/GPS/Rating follow the same two-pass shape (fauxcasa-cam.9/.10/.11,
via apps/desktop-python/metareader.py — the exiv2 seam): scan_library fills
geotag from the ini ``geotag=lat,lon`` key and the 0-5 star count from
``star=yes`` (legacy import = 1, §3 star authority); the indexer then
overrides with in-file values when the file carries them — EXIF
DateTimeOriginal/DateTime -> date_taken (the ini has no per-photo date
key), EXIF GPS -> geotag, XMP Rating 1-5 -> star — because in-file
metadata wins for tier-1 data per §4 (oracle hardening of that precedence
is fauxcasa-ed5.9's). Adopt-mode catalogs get the same values from the
backfill pass instead of the indexer.

Faces/people (§3 People first-class, read-only slice): scan_library parses
per-photo ini `faces=` regions via picasa_db.parse_faces, harvests
[Contacts2] id->name tables with the documented downward-inheritance rule
(an ancestor folder's ini names contacts for its whole subtree; the nearest
definition wins), and resolves each face to a display name. A machine-local
contacts.xml (see load_contacts_xml / default_contacts_xml) wins over
[Contacts2] on name conflicts per §4. A face whose contact id is
UNKNOWN_CONTACT (unconfirmed suggestion) or that no source names stays
unnamed (name None) — the suggested-vs-confirmed distinction this
read-only slice carries — unless a machine-local db3's person albums name
it: the §4 rescue importer (db3rescue.rescue_people, fauxcasa-cam.6/.7)
gap-fills ONLY those still-unnamed ids, source-flagged on
Catalog.db3_contacts, with every conflict/residue on the ImportReport.

Edit recipes (§4 "ini wins for edit recipes"; fauxcasa-cam.15):
scan_library resolves the CURRENT crop from `crop=rect64(..)` (falling
back to the filters= chain's crop64 op — crop= wins when both parse, a
disagreement is an import-report entry) into Photo.crop, and preserves
the raw recipe key/values (filters/crop/redo/text… — EDIT_RECIPE_KEYS)
on Photo.edits for N3 losslessness. Display paths bake the crop — thumbs
at index time (thumbcache._index_one), the viewer at decode — composing
crop -> EXIF orientation -> rotate= (see apps/desktop-python/cropmap.py
for why that order). Full recipe rendering (tilt, color ops, …) is M3
and deliberately NOT displayed; Photo.has_edits drives the honest M1
"edited" cue instead.

Remaining tracer-scope gaps (see apps/desktop-python/README.md): EXIF orientation is
applied at decode (Qt/PIL auto-transform, composed with the rotate= user
turns), but faces-in-XMP is not ingested (fauxcasa-cam.5).
The folder-level Hidden Folders category IS now honored: a folder whose
own [Picasa] section carries `P2category=Hidden Folders` hides all of its
photos, mirroring per-photo hidden=yes (oracle fixture 017) and the stash
treatment — see _is_folder_hidden.

Multiroot plumbing (fauxcasa-ed5.7.2, bead .b of the multi-root design):
Photo.root_id and Folder.root_id identify which library root a row belongs
to ("" = the implicit legacy root); Catalog.roots + Catalog.abs(photo) are
the single choke point for absolute-path composition (see abs()'s
docstring); walk_roots() is the N-root walk primitive. ALBUM MEMBERSHIP IS
ROOT-LOCAL: Album.members are Catalog.photos indices, and each root's
photos form a contiguous slice of that list (design §4/§6/§14) — see the
freeze comment on Album.members. Root reorder is forbidden until M2 remaps
album indices across a regroup.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# `zstandard` is imported LAZILY, inside save_catalog/load_catalog only
# (fauxcasa-ed5.5): every other function in this module (scan_library,
# Catalog/Photo, _group_photo_rows/_ungroup_photo_rows, ...) stays usable
# by a script that has catalog.py's OTHER deps but not this one — see
# scripts/bench-raw-indexing.py / check-ingest-parity.py, which import
# catalog.py but never call save_catalog/load_catalog. Scripts that DO call
# either (test_tracer.py, main.py, bench_scroll.py,
# scripts/bench-multiroot-startup.py) list "zstandard" in their own PEP 723
# dependencies block.

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts"))

import picasa_db  # noqa: E402
from db3rescue import rescue_people  # noqa: E402
from library import (  # noqa: E402
    LEGACY_ROOT_ID,
    LIBRARY_DIR,
    ROOT_MARKER,
    LibraryConfig,
    LibraryRoot,
    legacy_config,
    root_is_online,
)
from rawload import RAW_EXTS, is_raw_suffix  # noqa: E402
from videoload import VIDEO_EXTS, is_video_suffix  # noqa: E402

# Must match scripts/make-thumbcache.py EXTS exactly (cache-order parity):
# the stills set below — the full §5 stills matrix incl. TGA (Qt's qtga
# plugin decodes it) and PSD (Pillow flattened-composite fallback,
# fauxcasa-v46.4) — PLUS Picasa's documented 16-vendor RAW extension
# list (rawload.RAW_EXTS, fauxcasa-v46.1) PLUS Picasa's documented video
# list (videoload.VIDEO_EXTS, fauxcasa-v46.2) — any change to any part
# lands in BOTH files or caches stop binding.
EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
        ".webp", ".tga", ".psd"} | RAW_EXTS | VIDEO_EXTS

INI_NAMES = (".picasa.ini", "Picasa.ini", "picasa.ini")

# Adopt-mode backfill states (fauxcasa-cam.12). A catalog built by the
# indexer (build_cache read every file) is COMPLETE by construction — the
# default below — so only adopt-mode (--thumbs) catalogs, whose indexer
# never ran, carry NOT_STARTED / IN_PROGRESS. IN_PROGRESS means photos
# [0, backfill_cursor) already have their identity signals + in-file
# metadata applied AND persisted; a relaunch resumes at the cursor
# (thumbcache.backfill_catalog owns the pass).
BACKFILL_NOT_STARTED = "not_started"
BACKFILL_IN_PROGRESS = "in_progress"
BACKFILL_COMPLETE = "complete"

# One ini face tag: (rect, contact id, display name or None). rect =
# (left, top, right, bottom) fractions of the STORED pixels — rotate= does
# NOT transform them and EXIF orientation is the consumer's job
# (picasa-ini-format.md "faces="); name None = suggested/unnamed.
FaceTag = tuple[tuple[float, float, float, float], str, str | None]

# Per-file ini keys that carry Picasa edit-recipe state (fauxcasa-cam.15),
# PRESERVED raw on Photo.edits. rotate= is deliberately excluded — it has a
# parsed field of its own and composes live in paint (never a pending
# recipe). This is the survey's edit-key vocabulary (picasa_db
# _INI_EDIT_KEYS) minus rotate, plus text= (the text-tool record that
# travels with textactive=; kept for N3 losslessness even though the survey
# does not count it).
EDIT_RECIPE_KEYS = frozenset(
    "filters crop crop64 flipped redo textactive text".split())


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
    # Which root this photo belongs to (multiroot .b, fauxcasa-ed5.7.2 —
    # design §6 identity key). "" (LEGACY_ROOT_ID) is the reserved id of
    # the implicit legacy root and is the default for every single-root
    # catalog — day-zero behavior is unaffected. `rel` stays ROOT-relative
    # POSIX, untouched by this field; identity is (sha256, root_id, rel).
    root_id: str = LEGACY_ROOT_ID
    # Media kind, 'image' | 'video' (fauxcasa-v46.2): pure extension
    # routing (videoload.VIDEO_EXTS), so it is DERIVED — set at scan and
    # re-derived from rel on catalog load, never persisted (it could only
    # drift). Videos index as poster-frame thumbnails and show a play
    # badge; playback is v46.3.
    media: str = "image"
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
    # Source pixel dimensions (w, h), seeded from the ini's width=/
    # height= keys when both parse (fauxcasa-v46.2 — Picasa records them
    # for video files, the tracer's only pre-decode dim source there;
    # harmless extra signal on stills that carry them). None = unknown.
    dims: tuple[int, int] | None = None
    # Edit recipe (fauxcasa-cam.15). `crop`: the CURRENT crop as (left,
    # top, right, bottom) fractions of the STORED pixels (rect64 grammar —
    # the same frame as faces=: rotate= does not transform it and EXIF
    # orientation is the consumer's job), or None. Resolved at scan:
    # crop= wins over the filters= chain's crop64 op (_resolve_crop).
    # Display paths BAKE it: thumbs at index time, the viewer at decode,
    # composing crop -> EXIF orientation -> rotate= (cropmap.py).
    crop: tuple[float, float, float, float] | None = None
    # The RAW edit-recipe key/value pairs, in ini order, duplicates kept —
    # preserved byte-faithfully for N3 losslessness (full recipe rendering
    # is M3; only the crop is displayed in M1). Keys: EDIT_RECIPE_KEYS.
    edits: tuple[tuple[str, str], ...] = ()
    # Picasa stashes pre-edit originals in .picasaoriginals/; those files
    # are catalog entries (cache-order parity) but never shown in the grid.
    visible: bool = True
    # M1 read-only .picasaoriginals association (fauxcasa-cam.19, spec §10
    # item 18): the catalog INDEX of this photo's stashed pre-edit original
    # — the same-named file in the folder's .picasaoriginals/ (or legacy
    # Originals/) stash, where Picasa File->Save moved the untouched bytes
    # (oracle fixtures 005/019). DERIVED from the photo list on both scan
    # and load (like folder/name/visible/media), never persisted. None =
    # no Picasa-saved original. Un-bake/restore is M3; this is display-only.
    stashed_original: int | None = None
    # Identity + staleness signals, filled by the indexer (build_cache),
    # persisted in the catalog, and used by reconcile (cheap size+mtime
    # diff) and N6 identity (sha256). -1 / None until indexed.
    size: int = -1
    mtime: int = -1
    sha256: str | None = None

    @property
    def has_edits(self) -> bool:
        """The photo carries pending Picasa edit-recipe state — the M1
        honest 'edited' cue (viewer info bar). DERIVED from crop/edits so
        it can never drift from what was persisted. True for a resolved
        crop or any non-empty recipe value, with two carve-outs:
        `textactive=0` alone is NOT an edit (Picasa records the
        overlay-off state — fixture 005 writes it into the post-bake
        stash ini), and rotate= never counts (it is displayed live, not a
        pending recipe; it isn't in EDIT_RECIPE_KEYS at all). The
        unsaved-vs-baked state cue is M3 — this is presence only."""
        if self.crop is not None:
            return True
        for k, v in self.edits:
            if k.lower() == "textactive":
                if v.strip() == "1":
                    return True
            elif v.strip():
                return True
        return False


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
    # Which root this folder belongs to (multiroot .b): mirrors
    # Photo.root_id. Needed because Catalog.folders is a flat dict and
    # `rel` alone collides across roots (two roots can each have a
    # "2019" folder) — the dict KEY carries the disambiguation via
    # _folder_json_key (bare rel for roots[0], "<root_id>/<rel>"
    # otherwise), while this field records which root a given Folder
    # value actually is.
    root_id: str = LEGACY_ROOT_ID


@dataclass
class Album:
    uid: str
    name: str
    date: str | None = None
    description: str | None = None
    members: list[int] = field(default_factory=list)  # catalog photo indices
    # ALBUM-INDEX FREEZE (multiroot design §6/§14 bead .b): members are
    # indices into Catalog.photos, not rel strings, so global catalog
    # ORDER is load-bearing for album identity. With N roots, each root's
    # photos form a CONTIGUOUS slice of Catalog.photos (per-root walk,
    # concatenated in library.json roots-list order — design §4); album
    # indices are therefore root-local to whichever slice they fall in.
    # Any future operation that reorders roots or otherwise regroups the
    # catalog's per-root slices MUST remap every album's member indices or
    # forbid the reorder outright — silently reordering roots corrupts
    # album membership. M1 offers no root-reorder UI, which sidesteps this;
    # remapping (or forbidding it) is explicitly M2 territory (design §7
    # risk 7, §13 open question 7) and must not be broken by any M1
    # regroup operation (e.g. reconcile's catalog swap).
    # §3: an albums= token naming a uid with no [.album:] definition (and no
    # .pal) materializes as a PLACEHOLDER album — "surfaced in the import
    # diagnostics, never dropped". The sidebar marks these visually.
    placeholder: bool = False
    # §4: this album's definition came from a Picasa2Albums .pal file (a
    # .pal-only album, or a placeholder whose definition a .pal filled).
    pal_sourced: bool = False


@dataclass
class ReportEntry:
    """One surfaced import decision: where it came from (source), what
    class of decision it was (kind), the id/path it concerns (subject),
    and the human sentence recording what was chosen and why (detail)."""
    source: str   # "ini", "contacts", "pal", ...
    kind: str     # "unknown_album", "contact_name_conflict", "pal_divergence"…
    subject: str  # album uid, contact id, file name…
    detail: str


@dataclass
class ImportReport:
    """§4: "every conflict is surfaced in the import report, never silently
    resolved." The collector is owned by Catalog and populated during scan
    (fauxcasa-cam.13); it is persisted as JSON beside catalog.json
    (save_report/load_report) and surfaced minimally — an applog summary
    line plus a status-bar count. The full inspector surface is N7/M2."""
    entries: list[ReportEntry] = field(default_factory=list)

    def add(self, source: str, kind: str, subject: str, detail: str) -> None:
        self.entries.append(ReportEntry(source, kind, subject, detail))

    def summary(self) -> str:
        """One applog-sized line: total + per-kind tallies."""
        if not self.entries:
            return "no import notes"
        kinds: dict[str, int] = {}
        for e in self.entries:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        parts = ", ".join(f"{k} x{n}" if n > 1 else k
                          for k, n in kinds.items())
        n = len(self.entries)
        return f"{n} import note{'s' if n != 1 else ''} ({parts})"


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
    # Contact ids whose display name came from the db3 rescue import
    # (fauxcasa-cam.6/.7): people albums exist only in db3, so a name no
    # ini [Contacts2] or contacts.xml provided is gap-filled from there
    # (§4) and source-flagged here — the People sidebar surfaces the
    # provenance, and the flag persists so warm starts keep it.
    db3_contacts: set[str] = field(default_factory=set)
    # Import diagnostics collected while this catalog was built (§4; empty
    # on a warm load until main() re-attaches the persisted report).
    report: ImportReport = field(default_factory=ImportReport)
    # Adopt-mode backfill progress (fauxcasa-cam.12; constants above).
    # COMPLETE by default: the state tracks the BACKFILL job, so a catalog
    # the indexer fills (or will fill — the cold-build path) has nothing
    # pending; main()'s adopt path flips a fresh catalog to NOT_STARTED.
    backfill_state: str = BACKFILL_COMPLETE
    backfill_cursor: int = 0  # photos [0, cursor) done (IN_PROGRESS only)
    # Multiroot plumbing (fauxcasa-ed5.7.2 — bead .b, design §6/§14). The
    # roots this catalog spans, in library.json order; empty for a Catalog
    # built without any library context (e.g. a bare test fixture that
    # never calls abs()). A single-root scan_library()/load_catalog() call
    # always populates exactly one entry, id "" (LEGACY_ROOT_ID) for the
    # implicit legacy library. `root` above is kept unchanged for the many
    # existing consumers that only need the whole-library path (window
    # title, cache_dir_for, restart args) — it is NOT a photo-path
    # composition and is out of this bead's grep-audit scope.
    roots: list[LibraryRoot] = field(default_factory=list)
    # "" for the implicit legacy library; a full uuid4 for an explicit one
    # (mirrors LibraryConfig.library_id — see load_catalog's validation).
    library_id: str = ""
    # Offline-root tolerance (fauxcasa-ed5.7.5, bead .e, design §8). Root
    # ids from `roots` currently judged offline (library.root_is_online()
    # returned False the last time this was checked) — NOT recomputed
    # automatically on every read, so it reflects the state as of the last
    # refresh_offline_ids() call (load_catalog calls it on open; the
    # per-root reconcile loop calls it again on reconcile — "populated
    # when the catalog is opened/reconciled", design §8). Empty by default:
    # a freshly scan_library()-built catalog just walked every one of its
    # roots successfully, so nothing in it can be offline yet.
    offline_ids: set[str] = field(default_factory=set)
    # Freshness signals for externally-modified ini/contacts sidecars
    # (fauxcasa-cam.14 step 4): names/metadata baked into the persisted
    # catalog at scan time never refresh on a warm start until UNRELATED
    # photo drift happens to force a rebuild — a size+mtime signal on each
    # folder's .picasa.ini (keyed (root_id, folder_rel); a folder with no
    # ini has no entry) and on the machine-local contacts.xml closes that
    # gap. Same cheap-signal idiom as Photo.size/mtime and Drift (no
    # hashing — see stat_sig/_folder_ini_sig). Populated by scan_library /
    # _scan_library_config at scan time, persisted (save_catalog v14), and
    # re-derived fresh at reconcile time (reconcile_walk's Drift.
    # ini_changed for the per-folder inis; the contacts_sig comparison in
    # main._reconcile_online_roots, done ONCE for the whole library since
    # contacts.xml is one machine-local file, not a per-root artifact).
    ini_sigs: dict[tuple[str, str], tuple[int, int]] = field(
        default_factory=dict)
    # The one machine-local contacts.xml's signal, or None when no
    # contacts.xml was configured/found at scan time — collapsing "never
    # configured" and "file vanished" to the same value (via stat_sig())
    # so a library with no contacts.xml never phantom-drifts against
    # itself.
    contacts_sig: tuple[int, int] | None = None

    @property
    def visible_count(self) -> int:
        return sum(1 for p in self.photos if p.visible)

    def refresh_offline_ids(self, probes=None) -> None:
        """Recompute `offline_ids` from `roots` (design §8, bead .e): a
        root is offline when library.root_is_online() says its resolved
        path is not a directory — unplugged drive, remapped network share,
        etc. Simple path check only; volume-UUID-based remount recognition
        is bead .f. Call this whenever the catalog is opened (load_catalog)
        or reconciled (the per-root reconcile loop) so abs() and the
        online_roots()/offline_roots() split reflect current reality
        before the next UI read. `probes` (fauxcasa-t0a) is an optional
        volumes.ProbeCache scoped to the caller's operation, so an open
        that just resolved root locations does not re-probe every
        UUID-bound root; None probes directly, per call."""
        self.offline_ids = {r.id for r in self.roots
                            if not root_is_online(r, probes)}

    def online_roots(self) -> list[LibraryRoot]:
        """`roots` minus `offline_ids`, in library.json order (design §8/
        §9: the per-root reconcile loop — and any other per-root operation
        — iterates over exactly this set, never an offline root)."""
        return [r for r in self.roots if r.id not in self.offline_ids]

    def offline_roots(self) -> list[LibraryRoot]:
        """The complement of online_roots(): roots currently in
        `offline_ids`, in library.json order — what a status message or
        sidebar badge lists as unreachable this session."""
        return [r for r in self.roots if r.id in self.offline_ids]

    def abs(self, photo: Photo) -> Path | None:
        """The single choke point for absolute-path composition (design
        §6, extended by bead .e's offline semantics, design §8): look up
        `photo.root_id` among self.roots and join `photo.rel`. Returns
        None when the root is unknown to this catalog OR is currently
        offline (`photo.root_id in self.offline_ids`) — every consumer of
        an absolute photo path MUST go through this method and handle
        None (bead .b's grep-audit requirement, reconfirmed by bead .e's
        own audit). Offline roots' photos deliberately STAY in `photos`
        (design §8's "unplugging a drive does not lose data" rule) with a
        perfectly resolvable root_id; abs() still returns None for them on
        purpose so every consumer shows the offline placeholder instead of
        racing an unplugged drive with an OSError."""
        for r in self.roots:
            if r.id == photo.root_id:
                return None if r.id in self.offline_ids else r.path / photo.rel
        return None

    def photos_for_root(self, root_id: str) -> list[Photo]:
        """This root's contiguous slice of `photos` (design §3/§4/§7: bead
        .c's per-root cache binding needs exactly this walk-order subset).
        Filters on `Photo.root_id` alone — independent of whether `roots`
        happens to be populated — so it works for both a scan_library()/
        load_catalog() catalog and a bare test fixture. LEGACY_ROOT_ID
        ("") is every photo in an unpromoted single-root catalog, so this
        is a no-op filter for the byte-identical legacy path (design §3's
        "no rename or rewrite" guarantee)."""
        return [p for p in self.photos if p.root_id == root_id]


def _image_size(path: Path) -> tuple[int, int] | None:
    """Read dimensions without decoding pixels. None means unreadable/unknown,
    and callers should keep the file so the indexer can surface its error.
    RAW files report None BY DESIGN: their TIFF-based containers make
    QImageReader's sniff return the embedded preview's dimensions (a tiny
    wrong answer), so the size filter must never judge a RAW by it — the
    file is kept and the rawpy path decodes it (rawload module doc).
    Video files likewise report None: QImageReader must never sniff video
    bytes (videoload module doc), so the size filter always keeps them."""
    if is_raw_suffix(path.name) or is_video_suffix(path.name):
        return None
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
                 scan_filter: ScanFilter | None = None,
                 exts: frozenset[str] | set[str] | None = None) -> list[Path]:
    """The shared walk rule: sorted(Path) = path-component order. Any
    change here must also land in scripts/make-thumbcache.py or caches
    stop binding (the shipped benchmark cache uses this order).

    .fauxcasa/ library-state dirs and the .fauxcasa-root id marker are
    excluded from the walk (multiroot .a, fauxcasa-ed5.7.1); this change
    lands in lockstep with the script twin in scripts/make-thumbcache.py.

    `exts` is the EFFECTIVE extension set (None = the full EXTS): the
    File Types panel's per-library include/exclude (fauxcasa-v46.4,
    filetypes.py). Because the walk rule is load-bearing for cache-order
    parity, any non-default set MUST be folded into the cache identity
    by the caller (filetypes.exts_cache_key, the ScanFilter.cache_key
    shape) so each set gets its own cache dir and warm starts cohere."""
    pool = EXTS if exts is None else exts
    nroot = len(root.parts)  # rglob children extend root's own parts
    files = sorted(
        p for p in root.rglob("*")
        if p.suffix.lower() in pool and p.is_file()
        and LIBRARY_DIR not in p.parts[nroot:]  # .fauxcasa/ library state
        and p.name != ROOT_MARKER               # .fauxcasa-root id marker
    )
    if scan_filter is None or not scan_filter.active:
        return files
    return [p for p in files if _passes_scan_filter(p, scan_filter)]


def walk_roots(cfg: LibraryConfig,
               scan_filter: ScanFilter | None = None,
               exts: frozenset[str] | set[str] | None = None
               ) -> list[tuple[str, Path]]:
    """The N-root walk primitive (design §4): walk_library's frozen
    per-root rule run independently over every root in `cfg.roots`, IN
    LIBRARY.JSON ROOTS-LIST ORDER, each hit tagged with that root's id.
    Per-root order is untouched by chaining — the parity invariant this
    preserves is PER ROOT (§3): entry i of root R's fcache = photo i of
    the catalog's slice for R, exactly like today's single-root
    invariant, just decomposed into N independent copies.

    Any change to the walk rule itself still must land in BOTH this
    module and the twin in scripts/make-thumbcache.py in the same commit
    (walk_library's docstring) — this function only adds the outer
    per-root loop and tagging on top of that frozen rule; it does not
    change it."""
    hits: list[tuple[str, Path]] = []
    for r in cfg.roots:
        for p in walk_library(r.path, scan_filter, exts):
            hits.append((r.id, p))
    return hits


def stat_sig(path: Path | None) -> tuple[int, int] | None:
    """Cheap size+mtime freshness signal for a single file (fauxcasa-
    cam.14 step 4) — the same no-parse, no-hash idiom Photo.size/mtime and
    Drift already use for photos, generalized to one arbitrary sidecar
    file. None when `path` is None or unreadable, so "never configured"
    and "vanished since the last scan" collapse to the same value — a
    caller comparing two None-vs-None signals never phantom-drifts.
    Shared by `_folder_ini_sig` (below) and main.py's contacts.xml
    signature."""
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, int(st.st_mtime))


def _folder_ini_sig(folder: Path) -> tuple[int, int] | None:
    """The freshness signal for whichever INI_NAMES variant `folder`
    carries (first match wins, same probe order as _read_folder_ini), or
    None when the folder has no ini under any of the legacy names.
    scan_library uses this to populate Catalog.ini_sigs at scan time;
    reconcile_walk recomputes it fresh per folder to detect Drift.
    ini_changed — both sides read the exact same signal, so an externally
    modified/added/removed ini is never missed by one but not the other."""
    for name in INI_NAMES:
        sig = stat_sig(folder / name)
        if sig is not None:
            return sig
    return None


def _ancestor_closure(folder_rels: set[str]) -> set[str]:
    """Every root-relative prefix of every folder_rel in `folder_rels`,
    including "" (the library root) — fauxcasa-cam.14 step 4, Codex
    review finding 2. reconcile_walk's fresh-side folder_rels only covers
    folders that OWN a photo directly; a folder that inherits [Contacts2]/
    album definitions down to those photos but owns none itself (any
    ancestor, including the root) is never in that set, so a newly
    created ini there — one `catalog.ini_sigs` also has no prior entry
    for, since scan_library's own ini_by_folder is populated by exactly
    this same walked-photo-folders' ancestor closure (see folder_contacts'
    docstring) — was never probed by the union with old_ini_slice either,
    and inherited metadata could go stale with no drift signal at all.
    Always includes "" so an ini newly appearing at the bare library root
    is caught even when every photo lives in a subfolder (or the walk
    finds zero photos at all)."""
    out: set[str] = {""}
    for rel in folder_rels:
        while rel:
            out.add(rel)
            rel = rel.rpartition("/")[0]
    return out


def _read_folder_ini(
        folder: Path) -> tuple[picasa_db.PicasaIni, tuple[int, int] | None] | None:
    """Read whichever INI_NAMES variant `folder` carries, paired with that
    EXACT file's freshness signature stat'd immediately after the read
    succeeds (fauxcasa-cam.14 step 4, Codex review finding 3) — not a
    separate `_folder_ini_sig` call made later (end of scan_library, after
    every OTHER folder's ini has also been read and every photo file has
    been walked): a real-world edit landing in that much larger window
    would persist the OLD parsed content under the NEW signature, so
    reconcile would never notice the edit again. Stat-right-after-read
    also closes the variant-fallback race: the signature is guaranteed to
    describe the SAME path `read_picasa_ini` actually parsed, never a
    DIFFERENT INI_NAMES variant a second independent probe might pick if
    the folder's ini file changed identity between the two calls. None
    sig (file vanished between the read and the stat, vanishingly rare)
    just means no freshness signal for this folder, same as no ini at
    all — never a crash, matching every other stat_sig fail-soft case."""
    for name in INI_NAMES:
        p = folder / name
        if p.is_file():
            try:
                ini = picasa_db.read_picasa_ini(p)
            except OSError:
                continue  # unreadable file; try the legacy names
            return ini, stat_sig(p)
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


def _is_stash_leaf(folder_rel: str) -> bool:
    """True when `folder_rel` IS a Picasa stash directory (its last
    component), vs _is_stashed's any-ancestor hiding test. Only the two
    real stash names count — an arbitrary dot-folder is hidden by
    _is_stashed but is never an association source. Case rule mirrors
    _is_stashed: .picasaoriginals case-insensitive, legacy Originals exact."""
    leaf = folder_rel.rsplit("/", 1)[-1]
    return leaf.lower() == ".picasaoriginals" or leaf == "Originals"


def _link_stashed_originals(photos: list[Photo]) -> None:
    """When Picasa "saves" an edit it bakes the pixels into the visible
    file and moves the untouched original into the folder's stash under
    the SAME file name. Link each such sibling to its stash copy by
    catalog index. Pure derivation over rel paths, so scan_library and
    load_catalog both call it and agree; a stash file whose sibling is
    gone (original moved away) links nothing — fail-soft, no report."""
    by_rel = {p.rel: i for i, p in enumerate(photos)}
    for i, p in enumerate(photos):
        if not _is_stash_leaf(p.folder):
            continue
        parent = p.folder.rsplit("/", 1)[0] if "/" in p.folder else ""
        j = by_rel.get(f"{parent}/{p.name}" if parent else p.name)
        if j is not None:
            photos[j].stashed_original = i


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


def format_file_size(size: int) -> str:
    """Human-readable file size, Picasa-style (1 decimal, binary prefixes).
    Caller must guard against size < 0 (unindexed sentinel)."""
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if size >= threshold:
            return f"{size / threshold:.1f} {unit}"
    return f"{size} B"


# ---- edit recipes (fauxcasa-cam.15) ---------------------------------------
#
# §4: ini wins for edit recipes, and M1 "see your library again" means a
# cropped photo shows CROPPED — Picasa itself rendered unsaved recipes
# applied (oracle fixture 004: the crop lives in the ini ALONE, JPEG
# untouched, no db3 change). This slice ingests the recipe keys (crop
# parsed for display, everything preserved raw); rendering the rest of the
# chain (tilt, color ops, …) is M3.


def _usable_rect(rect: tuple[float, float, float, float] | None) -> bool:
    """A crop rect the display can act on: parsed AND non-degenerate
    (right > left, bottom > top). rect64 values are already in [0, 1]."""
    return rect is not None and rect[0] < rect[2] and rect[1] < rect[3]


def _crop_from_filters(value: str) -> tuple[float, float, float, float] | None:
    """The crop64 op inside a filters= chain -> its rect, or None.
    Grammar: ``crop64=1,<bare rect64 hex>;`` (picasa-ini-format.md). The
    chain is ordered edit history and ops can repeat, so the LAST crop64
    wins — a re-crop appends. Fail-soft per op: an unparseable crop64
    param is skipped, never an exception (§4 robustness)."""
    rect = None
    for name, params in picasa_db.parse_filters(value):
        if name.strip().lower() == "crop64" and len(params) >= 2:
            try:
                got = picasa_db.parse_rect64(params[1].strip())
            except ValueError:
                continue
            if _usable_rect(got):
                rect = got
    return rect


def _resolve_crop(sec: picasa_db.IniSection, rel: str,
                  report: ImportReport) -> tuple[float, float, float, float] | None:
    """The photo's CURRENT crop rect, or None. Source precedence:

    1. ``crop=rect64(..)`` — authoritative per the format doc ("current
       crop; history lives in filters= as crop64 op");
    2. the filters= chain's crop64 op, filling in when crop= is absent or
       unparseable (Picasa normally writes BOTH — fixture 004);
    3. a standalone ``crop64=`` key (gist-era shape, ``1,<hex>`` or a
       bare hex) as the last resort.

    When crop= and the chain both parse and DISAGREE, crop= wins and the
    conflict is surfaced on the import report (§4: never silently
    resolved); the normal agreeing pair stays silent. Degenerate or
    malformed values fail soft per line to the next source."""
    crop = None
    raw = sec.get("crop")
    if raw:
        try:
            crop = picasa_db.parse_rect64(raw.strip())
        except ValueError:
            crop = None
        if not _usable_rect(crop):
            crop = None
    chain = None
    fraw = sec.get("filters")
    if fraw:
        chain = _crop_from_filters(fraw)
    if crop is not None:
        if chain is not None and chain != crop:
            report.add("ini", "crop_conflict", rel,
                       f"crop= says {crop} but the filters= chain's crop64 "
                       f"says {chain} — crop= wins (the format doc's "
                       f"'current crop'; the chain is history)")
        return crop
    if chain is not None:
        return chain
    craw = sec.get("crop64")
    if craw:
        try:
            rect = picasa_db.parse_rect64(craw.split(",")[-1].strip())
        except ValueError:
            return None
        if _usable_rect(rect):
            return rect
    return None


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
    # P2category= is Picasa 3.x (oracle-verified above). category= is the
    # legacy Picasa 2 spelling — inferred from Picasa-era documentation,
    # NOT oracle-observed (no fixture drives a Picasa 2 install).
    val = (psec.get("P2category") or psec.get("category") or "").strip().lower()
    return val == _HIDDEN_FOLDERS_CATEGORY


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


# ---- .pal album files (Picasa2Albums directory, fauxcasa-cam.8) ----------
#
# Picasa 2-era per-album files, one `<32-hex uid>.pal` each, XML-ish
# picasa2album docs per docs/research/sources/forensicir-2007-picasa-analysis.md
# (~lines 234-267): dbid, albumid, property elements (uid string, category
# num, date real64 = OLE automation days, token, name) with a <files> member
# list of `[C]\path\to\file` volume-token paths nested in the name property.
# §4 merge rank (pinned ini > .pal > db3 — spec §10 item 16, fauxcasa-79b):
# ini wins membership; .pal fills gaps ONLY — a .pal-only album materializes
# like a real album flagged pal-sourced, and a divergent .pal's extra members
# become import-report entries, never membership.

_PAL_UID_RE = re.compile(r"[0-9a-f]{32}")
# `[C]\...` / `[CF32-BA1D]\...` volume tokens: strip token + its separator.
_PAL_VOLUME_RE = re.compile(r"^\[[^\]]*\][\\/]?")


@dataclass
class PalAlbum:
    uid: str
    name: str
    date: str | None          # canonical ISO form of the real64 OLE date
    members: list[str]        # volume-token-stripped POSIX paths


def _ole_days_to_iso(value: str) -> str | None:
    """A .pal `date` real64 — OLE automation days since 1899-12-30 — to the
    catalog's canonical ISO string. Fail-soft: a non-numeric or wildly
    out-of-range value is None, never an exception (negative OLE dates use
    a different fraction rule and predate Picasa; not worth decoding)."""
    try:
        days = float(value)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= days < 200000.0):  # 1899-12-30 .. ~year 2447
        return None
    from datetime import datetime, timedelta
    dt = datetime(1899, 12, 30) + timedelta(days=days)
    return dt.replace(microsecond=0).isoformat()


def read_pal(path: Path) -> PalAlbum | None:
    """Parse one .pal file; None on any defect (fail-soft PER FILE — one
    corrupt album file must never sink the rest of the directory). The uid
    comes from the uid property, then <albumid>, then the file's own name
    (Picasa names the file by its uid); no valid 32-hex uid anywhere means
    the file is not a usable album."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    if root.tag != "picasa2album":
        return None
    props: dict[str, str] = {}
    for el in root.iter("property"):
        pname = (el.get("name") or "").strip().lower()
        if pname and pname not in props:
            props[pname] = el.get("value") or ""
    uid = (props.get("uid") or root.findtext("albumid") or "").strip().lower()
    if not _PAL_UID_RE.fullmatch(uid):
        uid = path.stem.strip().lower()
        if not _PAL_UID_RE.fullmatch(uid):
            return None
    name = (props.get("name") or "").strip() or uid[:8]
    date = _ole_days_to_iso(props["date"]) if "date" in props else None
    members: list[str] = []
    for el in root.iter("filename"):
        rel = _PAL_VOLUME_RE.sub("", (el.text or "").strip())
        rel = rel.replace("\\", "/")
        if rel:
            members.append(rel)
    return PalAlbum(uid=uid, name=name, date=date, members=members)


def read_pal_dir(pal_dir: Path) -> tuple[list[PalAlbum], list[str]]:
    """Every parseable *.pal under `pal_dir` (name-sorted for determinism)
    plus the file names that failed to parse (import-report material)."""
    pals: list[PalAlbum] = []
    bad: list[str] = []
    try:
        files = sorted(pal_dir.glob("*.pal"))
    except OSError:
        return [], []
    for f in files:
        pal = read_pal(f)
        if pal is None:
            bad.append(f.name)
        else:
            pals.append(pal)
    return pals, bad


def default_pal_dir() -> Path | None:
    """The machine-local Picasa2Albums directory, if this machine has one.
    Real Picasa puts it at %LocalAppData%\\Google\\Picasa2Albums — a SIBLING
    of Picasa2\\, not inside it (observed empirically:
    docs/research/picasastarter-notes.md, wine-oracle.md) — so that location
    is probed first; Picasa2\\Picasa2Albums second, tolerating data laid out
    after this function's historical (divergent, fauxcasa-1t6) path. None
    when the env var or both directories are absent — discovery is
    best-effort by design, same shape as default_contacts_xml."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    google = Path(base) / "Google"
    for p in (google / "Picasa2Albums", google / "Picasa2" / "Picasa2Albums"):
        if p.is_dir():
            return p
    return None


def _merge_pal_albums(pal_dir: Path, photos: list[Photo],
                      albums: dict[str, Album],
                      report: ImportReport) -> None:
    """Single-root §4 .pal merge — the frozen legacy member resolution:
    a volume-token-stripped member string IS the library rel, matched
    exactly. See _merge_pal_albums_core for the §4 rank rules and
    merge_pal_albums_config for the (root_id, rel)-qualified explicit-
    library variant (fauxcasa-cam.21)."""
    index_by_rel = {p.rel: i for i, p in enumerate(photos)}

    def resolve(member: str) -> tuple[int | None, str | None]:
        i = index_by_rel.get(member)
        return (i, None) if i is not None else (None, "missing")

    _merge_pal_albums_core(pal_dir, photos, albums, report, resolve)


def merge_pal_albums_config(pal_dir: Path, roots: list[tuple[str, Path]],
                            photos: list[Photo], albums: dict[str, Album],
                            report: ImportReport) -> None:
    """Root-aware §4 .pal merge for an explicit multi-root composition
    (fauxcasa-cam.21), run ONCE over the composed catalog. `roots` is
    [(root_id, path)] from the LibraryConfig manifest; `photos` is the
    composed root_id-tagged list with global indices.

    Member resolution is root-qualified: each volume-token-stripped
    member path first translates against every root's path
    (db3rescue.translate_db3_path — the §8 drive-insensitive suffix
    match, casefold-tolerant), then falls back to the legacy direct-rel
    reading per root, so a member can only attach to the (root_id, rel)
    it actually names. A member matching photos in more than one root is
    reported ambiguous and attached NOWHERE — the .pal's volume token is
    a serial read_pal already strips, so no tie-break exists and a guess
    would silently corrupt membership.

    Deliberate divergence from the legacy path (PR #86 review, P3-a): an
    explicit single-root library resolves full volume-stripped members
    via translation, where the frozen legacy exact-only reading could
    not; legacy single-root behavior stays untouched by design."""
    from db3rescue import translate_db3_path
    idx_by_root: dict[str, dict[str, int]] = {}
    fold_by_root: dict[str, dict[str, int]] = {}
    for i, p in enumerate(photos):
        idx_by_root.setdefault(p.root_id, {})[p.rel] = i
        fold_by_root.setdefault(p.root_id, {})[p.rel.casefold()] = i

    def resolve(member: str) -> tuple[int | None, str | None]:
        hits: list[int] = []
        for root_id, root_path in roots:
            idx = idx_by_root.get(root_id, {})
            rel = translate_db3_path(member, root_path)
            j = None
            if rel is not None:
                j = idx.get(rel)
                if j is None:
                    j = fold_by_root.get(root_id, {}).get(rel.casefold())
            if j is None:
                j = idx.get(member)  # legacy direct-rel reading
            if j is not None:
                hits.append(j)
        if not hits:
            return None, "missing"
        if len(hits) > 1:
            return None, "ambiguous"
        return hits[0], None

    _merge_pal_albums_core(pal_dir, photos, albums, report, resolve)


def _merge_pal_albums_core(pal_dir: Path, photos: list[Photo],
                           albums: dict[str, Album],
                           report: ImportReport, resolve) -> None:
    """§4 merge, rank ini > .pal > db3 (pinned: spec §10 item 16, fauxcasa-79b):

    - a uid the ini knows nothing of (no [.album:] definition, no albums=
      token) materializes from its .pal like a real album, flagged
      pal_sourced — membership from the .pal is the gap being filled;
    - a PLACEHOLDER uid (albums= tokens, no definition) gets its missing
      DEFINITION (name/date) from the .pal — membership stays the ini's;
    - an ini-defined album keeps its ini membership verbatim: a divergent
      .pal (extra or absent members) is an import-report entry recording
      the choice, never a membership change; only a missing date is a gap
      the .pal may fill;
    - .pal members that resolve to no catalog photo are reported, and an
      unparseable .pal file is reported and skipped (fail-soft per file).

    `resolve(member) -> (photo index | None, status)` supplies the member
    lookup: status None on a match, "missing" when nothing claims the
    member, "ambiguous" when several roots do (multiroot only — reported
    separately, never attached)."""
    pals, bad = read_pal_dir(pal_dir)
    for fname in bad:
        report.add("pal", "pal_unreadable", fname,
                   "unparseable .pal file skipped (fail-soft per file)")
    if not pals:
        return
    for pal in pals:
        resolved: list[int] = []
        missing: list[str] = []
        ambiguous: list[str] = []
        for rel in pal.members:
            i, status = resolve(rel)
            if i is None:
                (ambiguous if status == "ambiguous" else missing).append(rel)
            elif photos[i].visible:  # same rule as ini membership
                resolved.append(i)
        if ambiguous:
            report.add("pal", "pal_member_ambiguous", pal.uid,
                       f".pal album “{pal.name}” member(s) match photos in "
                       f"more than one root — attached nowhere "
                       f"(root-qualified identity, fauxcasa-cam.21): "
                       + ", ".join(ambiguous))
        album = albums.get(pal.uid)
        if album is None:
            # .pal-only album: nothing to conflict with — the whole album
            # is the gap, filled like a real album, flagged pal-sourced.
            albums[pal.uid] = Album(uid=pal.uid, name=pal.name,
                                    date=pal.date, members=resolved,
                                    pal_sourced=True)
            if missing:
                report.add("pal", "pal_member_missing", pal.uid,
                           f".pal album “{pal.name}” lists {len(missing)} "
                           f"member(s) not in the library: "
                           f"{', '.join(missing)}")
            continue
        if album.placeholder:
            # The .pal IS the missing definition: fill the name/date gap;
            # membership authority stays with the ini's albums= tokens.
            album.name = pal.name
            album.placeholder = False
            album.pal_sourced = True
        if album.date is None and pal.date is not None:
            album.date = pal.date  # the ini definition carried no date
        ini_set = set(album.members)
        pal_set = set(resolved)
        extra = pal_set - ini_set
        absent = ini_set - pal_set
        if extra or absent or missing:
            bits = []
            if extra:
                bits.append(
                    "extra members NOT added (ini wins membership): "
                    + ", ".join(sorted(photos[i].rel for i in extra)))
            if absent:
                bits.append(
                    "ini members the .pal lacks (kept): "
                    + ", ".join(sorted(photos[i].rel for i in absent)))
            if missing:
                bits.append("members not in the library: "
                            + ", ".join(missing))
            report.add("pal", "pal_divergence", pal.uid,
                       f".pal album “{pal.name}” diverges from the ini — "
                       + "; ".join(bits))


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
                 contacts: dict[str, str] | None = None,
                 pal_dir: Path | None = None,
                 exts: frozenset[str] | set[str] | None = None,
                 db3_dir: Path | None = None) -> Catalog:
    """Walk `root` and build the catalog. `contacts` is the machine-local
    contacts.xml id->name map (load_contacts_xml); per §4 it wins over the
    ini [Contacts2] tables when both name a contact. `pal_dir` is a
    Picasa2Albums directory of .pal album files, merged per §4 (ini wins
    membership; .pal fills gaps only — see _merge_pal_albums). `exts` is
    the effective extension set from the File Types panel (walk_library
    docstring). `db3_dir` is a machine-local Picasa db3 directory: its
    person albums gap-fill contact names the ini/contacts.xml never
    provided, source-flagged on Catalog.db3_contacts
    (db3rescue.rescue_people, fauxcasa-cam.6/.7). Conflicts and
    unknown-uid placeholders land on the catalog's ImportReport, never
    resolved silently (fauxcasa-cam.13/.8)."""
    root = root.resolve()
    files = walk_library(root, scan_filter, exts)
    contacts = contacts or {}
    report = ImportReport()

    photos: list[Photo] = []
    folders: dict[str, Folder] = {}
    albums: dict[str, Album] = {}
    # folder_rel -> (ini, name.lower() -> merged section, ini_sig) or None.
    # ini_sig (fauxcasa-cam.14 step 4) is _read_folder_ini's own
    # stat-right-after-read signal, carried alongside the parsed result so
    # the end-of-scan ini_sigs build below never re-stats independently
    # (Codex review finding 3 — see _read_folder_ini's docstring).
    ini_by_folder: dict[str, tuple | None] = {}
    # folder_rel -> merged [Contacts2] id->name for that folder's subtree
    contacts_by_folder: dict[str, dict[str, str]] = {}

    def folder_ini(folder_rel: str) -> tuple | None:
        entry = ini_by_folder.get(folder_rel, False)
        if entry is False:
            read = _read_folder_ini(root / folder_rel if folder_rel else root)
            entry = (read[0], _section_map(read[0]), read[1]) \
                if read is not None else None
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
        photo = Photo(rel=rel, folder=folder_rel, name=name,
                      media="video" if is_video_suffix(name) else "image")
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
            wv, hv = sec.get("width"), sec.get("height")
            if wv and hv:
                # ini width=/height= seed the source dims — Picasa records
                # them for videos (fauxcasa-v46.2). Fail-soft per line (§4
                # robustness): a malformed value just leaves dims unknown.
                try:
                    dims = (int(wv), int(hv))
                except ValueError:
                    dims = None
                if dims is not None and dims[0] > 0 and dims[1] > 0:
                    photo.dims = dims
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
            # Edit recipes (fauxcasa-cam.15): every recipe key/value is
            # preserved RAW, in ini order with duplicates kept (N3
            # losslessness — Photo.edits); the current crop is resolved
            # for display (crop= wins over the filters= chain's crop64;
            # a disagreement lands on the import report). rotate= stays
            # its own parsed field above.
            recipe = tuple((k, v) for k, v in sec.items
                           if k.lower() in EDIT_RECIPE_KEYS)
            if recipe:
                photo.edits = recipe
                photo.crop = _resolve_crop(sec, rel, report)

        # A whole folder in the "Hidden Folders" collection hides every
        # photo under it, exactly like per-photo hidden=yes or a stash dir.
        if photo.hidden or folders[folder_rel].folder_hidden:
            photo.visible = False
        photos.append(photo)

    _link_stashed_originals(photos)

    # Album definitions: [.album:<uid>] sections live in each member
    # folder's ini; collect once per uid, then resolve membership tokens.
    # When folders carry diverging duplicate definitions of one uid,
    # first-wins in walk order — a known-arbitrary choice; Picasa's own
    # resolution rule is unobserved (no oracle fixture yet).  A diverging
    # second definition is an import-report entry (fauxcasa-cam.17).
    album_sources: dict[str, str] = {}  # uid -> folder_rel of first definition
    for folder_key, entry in ini_by_folder.items():
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
                    album_sources[uid] = folder_key
                elif uid:
                    # duplicate definition: check for divergence and report
                    existing = albums[uid]
                    new_name = sec.get("name") or uid[:8]
                    if (new_name != existing.name
                            or sec.get("date") != existing.date
                            or sec.get("description") != existing.description):
                        first = album_sources.get(uid, folder_key)
                        report.add(
                            "ini", "album_redefinition", uid,
                            f'[.album:{uid}] "{new_name}" in '
                            f'"{folder_key or "root"}" diverges from '
                            f'"{existing.name}" first seen in '
                            f'"{first or "root"}" — first definition wins'
                        )

    # Membership + §3 placeholder albums (fauxcasa-cam.13): an albums=
    # token whose uid has no [.album:] definition anywhere used to be
    # skipped silently; it now materializes as a placeholder album —
    # "surfaced in the import diagnostics, never dropped".
    placeholders: dict[str, Album] = {}
    for i, photo in enumerate(photos):
        folders[photo.folder].total_count += 1  # reveal-mode count
        if not photo.visible:
            continue
        folders[photo.folder].photo_count += 1
        for uid in photo.albums:
            album = albums.get(uid)
            if album is None:
                album = placeholders.get(uid)
                if album is None:
                    album = Album(uid=uid, name=f"Unknown album {uid[:8]}",
                                  placeholder=True)
                    placeholders[uid] = album
                    albums[uid] = album  # after real albums: display order
            album.members.append(i)

    # .pal albums (fauxcasa-cam.8): merged AFTER ini resolution so the ini
    # rank holds; may de-placeholder a uid whose definition lives in a .pal.
    if pal_dir is not None:
        _merge_pal_albums(pal_dir, photos, albums, report)

    # Report the uids still unexplained after every source had its say.
    for uid, album in placeholders.items():
        if album.placeholder:
            report.add("ini", "unknown_album", uid,
                       f"albums= references uid {uid} but no [.album:{uid}] "
                       f"definition (or .pal) exists — materialized as "
                       f"placeholder “{album.name}” with "
                       f"{len(album.members)} member(s)")

    # Flat contact registry: every walked folder's effective [Contacts2]
    # table (first-wins across folders, like duplicate album definitions),
    # with contacts.xml names overriding on conflict (§4) — each such
    # conflict is an import-report entry, not a silent resolution.
    # Cross-folder ini disagreements (two folders naming the same cid
    # differently) are also reported once per conflicting id (fauxcasa-cam.17).
    registry: dict[str, str] = {}
    registry_source: dict[str, str] = {}  # cid -> folder_rel that first registered
    reported_cross: set[str] = set()  # cids already reported (one entry per cid)
    for folder_rel in folders:
        for cid, cname in folder_contacts(folder_rel).items():
            if cid not in registry:
                registry[cid] = cname
                registry_source[cid] = folder_rel
            elif registry[cid] != cname and cid not in reported_cross:
                reported_cross.add(cid)
                first = registry_source[cid]
                report.add(
                    "ini", "contact_name_conflict", cid,
                    f'cross-folder [Contacts2]: "{registry[cid]}" '
                    f'(from "{first or "root"}") vs "{cname}" '
                    f'(from "{folder_rel or "root"}") — first-folder wins'
                )
    for cid, xml_name in contacts.items():
        ini_name = registry.get(cid)
        if ini_name is not None and ini_name != xml_name:
            report.add("contacts", "contact_name_conflict", cid,
                       f"[Contacts2] names contact {cid} “{ini_name}” but "
                       f"contacts.xml says “{xml_name}” — contacts.xml "
                       f"wins (§4)")
    registry.update(contacts)

    # db3 rescue (fauxcasa-cam.6/.7), LAST so every other source had its
    # say: person-album names fill only the ids still unnamed (§4 rank
    # ini/contacts.xml > db3), and db3 face rows are checked against the
    # ini-parsed faces for residue surfacing — never membership.
    db3_contacts: set[str] = set()
    if db3_dir is not None:
        db3_contacts = rescue_people(db3_dir, root, photos, registry, report)

    # ini freshness signals (fauxcasa-cam.14 step 4): every folder_rel
    # this scan actually queried an ini for (ini_by_folder — both
    # photo-owning folders and the ancestor folders folder_contacts()
    # walked for [Contacts2] inheritance, so a contacts-only ancestor ini
    # is covered too), keyed (root_id, folder_rel); a folder without an
    # ini (or where the ini couldn't be stat'd) has no entry, same as
    # reconcile_walk's fresh-side convention. The signal itself is
    # `entry[2]`, captured by _read_folder_ini right when THAT folder's
    # ini was actually read — not a fresh _folder_ini_sig probe here
    # (Codex review finding 3): re-statting this late (after every other
    # folder's ini and every photo file has already been walked) would
    # pair the content parsed at THAT folder's own read time with
    # whatever the file looks like NOW, silently swallowing an edit that
    # landed in between.
    ini_sigs: dict[tuple[str, str], tuple[int, int]] = {}
    for folder_rel, entry in ini_by_folder.items():
        if entry is not None and entry[2] is not None:
            ini_sigs[(LEGACY_ROOT_ID, folder_rel)] = entry[2]

    return Catalog(root=root, photos=photos, folders=folders, albums=albums,
                   contacts=registry, db3_contacts=db3_contacts,
                   report=report, ini_sigs=ini_sigs,
                   roots=[LibraryRoot(id=LEGACY_ROOT_ID, path=root)])


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
# v6: placeholder albums for unknown albums= uids and .pal-sourced albums
# (fauxcasa-cam.13/.8) are ingested and persisted (album `placeholder`/`pal`
# flags; the import report lands beside this file as import-report.json) —
# a v5 catalog silently dropped both album classes, so a warm start would
# hide them again; reject and cold-rebuild.
# v7: video files join the walk (videoload.VIDEO_EXTS in EXTS,
# fauxcasa-v46.2) and ini width=/height= dims are ingested (per-photo
# `wh` rows) — a v6 catalog was walked without videos, so a warm start
# would silently hide every video in the library; reject and cold-
# rebuild. (Photo.media is derived from the extension on load, like
# folder/name/visible — never persisted.)
# v8: adopt-mode backfill state (fauxcasa-cam.12) is persisted as a
# top-level `backfill` object when a backfill is pending/underway (absent
# = complete). A v7 adopt catalog carries neither the key nor any way to
# tell "never backfilled" from "complete", so it is rejected and the cold
# walk re-adopts with an explicit NOT_STARTED state — which then backfills.
# v9: TGA and PSD stills join the walk (§5 stills matrix, fauxcasa-v46.4)
# — a v8 catalog was walked without them, so a warm start would silently
# hide every TGA/PSD in the library until some unrelated drift; reject
# and cold-rebuild (the same shape as the v7 video bump).
# v10: Picasa edit-recipe keys (fauxcasa-cam.15) are ingested — the crop=
# rect (or the filters= chain's crop64) drives crop-applied display, the
# raw recipe strings are preserved (per-photo `cp`/`e` rows) — and the
# indexer now BAKES the crop into the thumbnail like EXIF orientation. A
# v9 catalog has neither the fields nor crop-baked thumbs, and nothing in
# the cheap size/mtime drift check would notice (the recipe lives in the
# ini sidecar, not the photo file); reject and cold-rebuild, which also
# rebuilds the machine-local fcache with crops baked. The fcache MAGIC
# version is untouched (the packed layout is unchanged, and the shipped
# benchmark cache stays adoptable — its synthetic photos carry no recipe
# keys, so the bake is a no-op there; same posture as the v2 EXIF bake).
# v11: the db3 rescue import (fauxcasa-cam.6/.7) gap-fills contact names
# from db3 person albums at scan and persists the rescued ids as a
# top-level `db3_contacts` list (the People-sidebar source flag). A v10
# catalog scanned before the rescue carries neither the rescued names nor
# the flag, so a warm start would silently un-name rescued people; reject
# and cold-rebuild.
# v12: multiroot catalog plumbing (fauxcasa-ed5.7.2, design §5/§14 bead
# .b) — ADDITIVE, not a walk-rule change: a per-photo row may carry an
# optional "R" (root_id, ABSENT MEANS roots[0] — see _expand_root_id),
# folder/hidden_folder keys may carry a "<root_id>/<rel>" prefix for
# non-first roots (see _folder_json_key), and the header carries either
# "library_id"+"roots" (explicit libraries) or "library" (implicit
# legacy, unchanged). COMPAT: a v11 file is still accepted when the
# library passed to load_catalog has exactly one root (rows/keys are then
# all implicitly roots[0] — see load_catalog's compat path); a v11 file
# is rejected outright for a multi-root library (cold walk), since a v11
# file predates root disambiguation entirely and cannot express it.
#
# v13: on-disk size budget (fauxcasa-ed5.5, spec §10 item 20 re-baseline
# — see docs/research/catalog-size-analysis.md option f2). The file is now
# zstd level 3 over a FOLDER-GROUPED compact-JSON body: rows are grouped
# by (root_id, folder) — folder path stored once per group (as "p"; a
# group's root_id lives at "R", absent means roots[0], same convention as
# the old per-row "R") and each row carries only its basename ("n") plus
# every other existing field, UNCHANGED (see _group_photo_rows /
# _ungroup_photo_rows). Grouping is done by RUN (a new group starts
# whenever (root_id, folder) differs from the previous row's), never by
# a dict keyed on (root_id, folder) — walk_library's path-sorted order is
# NOT folder-contiguous whenever a subfolder's name sorts between two of
# its parent's own files, so dict-insertion-order grouping would silently
# reorder rows and corrupt album-member index stability; see
# _group_photo_rows for the worked example. "x" (sha256) is base85 of the
# raw 32 bytes instead of 64 hex chars — lossless, decoded back to hex on load so
# every OTHER consumer of Photo.sha256 is unaffected. Detection is by
# content, not the version field: a file starting with the zstd magic
# (ZSTD_MAGIC) IS a v13 file; a plain-JSON file is v11 (single-root compat
# only) or older/foreign (cold walk) — see load_catalog. There is no
# migration path for a plain v12 file (never a shipped format for more
# than this same release) — it now simply fails the version gate like any
# older format; the catalog is a regenerable cache, not a document.
#
# v14 (fauxcasa-cam.14 step 4): persists Catalog.ini_sigs (per-folder
# .picasa.ini size+mtime, nested "ini_sigs": {root_id: {folder_rel:
# [size, mtime]}}) and Catalog.contacts_sig (the one machine-local
# contacts.xml's size+mtime, "contacts_sig": [size, mtime], key absent
# when None) — see save_catalog/load_catalog. Neither signal existed on
# any v13 (or older) file, so a v13 zstd frame now fails the version gate
# below like any other older format and cold-rebuilds; there is no
# migration, same regenerable-cache posture as the v12 note above. Before
# this bump, an externally-edited ini or contacts.xml never refreshed a
# warm start until unrelated photo drift happened to force a rebuild —
# these signals close that gap (reconcile_walk's Drift.ini_changed; the
# contacts_sig check in main._reconcile_online_roots, done ONCE per
# library, not per root).
CATALOG_VERSION = 14
# The last pre-multiroot format (fauxcasa-ed5.7.2, bead .b): load_catalog's
# ONLY version-compat carve-out, and only when the library has exactly one
# root (see load_catalog). A fixed number, not "CATALOG_VERSION - 1" — see
# load_catalog's comment for why that distinction matters going forward.
PRE_MULTIROOT_VERSION = 11

# zstd frame magic (RFC 8878 §3.1.1): the four bytes every
# zstandard-produced frame starts with. save_catalog (v13+) always writes
# a zstd frame; load_catalog uses this, not the "version" field, to decide
# whether a file is the current grouped format or a legacy plain-JSON one
# (see the v13 comment above) — content-sniffing, not a header field,
# because a legacy file has no way to say "I am plain JSON" either.
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# The import report's file name, written beside catalog.json (same cache
# dir) by save_report and re-attached on warm starts via load_report.
REPORT_NAME = "import-report.json"


def save_report(report: ImportReport, path: Path) -> None:
    """Atomically persist the import report as human-readable JSON."""
    data = {
        "version": 1,
        "entries": [
            {"source": e.source, "kind": e.kind,
             "subject": e.subject, "detail": e.detail}
            for e in report.entries
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".report.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_report(path: Path) -> ImportReport:
    """The persisted import report, or an EMPTY one when absent, unreadable,
    or malformed — the report is diagnostics, never a gate (fail-soft)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = [
            ReportEntry(source=str(e["source"]), kind=str(e["kind"]),
                        subject=str(e["subject"]), detail=str(e["detail"]))
            for e in data["entries"]
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return ImportReport()
    return ImportReport(entries=entries)


def _expand_root_id(raw: str | None, roots: list[LibraryRoot]) -> str:
    """THE ONE place the absent-means-roots[0] default (design §5, bead
    .b) is expanded: an absent/None root id — a photo row's missing "R",
    or the "what is the first root" question the folder-key convention
    also needs — resolves to roots[0].id, or LEGACY_ROOT_ID when `roots`
    is empty (a Catalog built without any library context). This is what
    keeps a promoted single-root catalog's photo rows byte-identical:
    roots[0]'s photos never need an explicit "R"."""
    if raw:
        return raw
    return roots[0].id if roots else LEGACY_ROOT_ID


def _folder_json_key(folder_rel: str, folder_root_id: str,
                     first_root_id: str) -> str:
    """Folder-key convention (design §5): roots[0]'s folders serialize as
    the bare rel — byte-identical to a pre-multiroot catalog — every other
    root's folder is prefixed "<root_id>/<rel>" so same-named folders in
    different roots (e.g. a "2019" folder on two drives) don't collide in
    the "folders"/"hidden_folders" maps. Same absent-means-roots[0]
    convention as _expand_root_id, specialized for folder identity."""
    if folder_root_id == first_root_id:
        return folder_rel
    return f"{folder_root_id}/{folder_rel}" if folder_rel else folder_root_id


def _photo_to_row(p: Photo, first_root_id: str) -> dict:
    row: dict = {"r": p.rel}
    if p.root_id != first_root_id:
        row["R"] = p.root_id  # absent means roots[0] — see _expand_root_id
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
    if p.crop is not None:
        row["cp"] = list(p.crop)  # n/65536 fractions: exact JSON round-trip
    if p.edits:
        # raw edit-recipe strings, verbatim (N3; has_edits derives from
        # these on load, so no flag is persisted — it could only drift)
        row["e"] = [[k, v] for k, v in p.edits]
    if p.date_taken:
        row["d"] = p.date_taken
    if p.geotag is not None:
        # decimal degrees rounded to 6 places at parse time, so the JSON
        # float round-trip is exact
        row["g"] = list(p.geotag)
    if p.dims is not None:
        row["wh"] = list(p.dims)  # ini width=/height= seed (v46.2)
    if p.size >= 0:
        row["z"] = p.size
    if p.mtime >= 0:
        row["m"] = p.mtime
    if p.sha256:
        row["x"] = p.sha256
    return row


def _group_photo_rows(rows: list[dict], first_root_id: str) -> list[dict]:
    """Fold flat per-photo rows (each shaped like _photo_to_row's output —
    "r" rel path, absent-means-roots[0] "R", hex "x") into the v13
    folder-grouped shape (fauxcasa-ed5.5): one entry per (root_id, folder)
    — "p" the folder path, "R" the group's root id (same absent-means-
    roots[0] convention, now hoisted to the group since every row in a
    group shares one root_id by construction), "ph" the rows with "r"/"R"
    replaced by a bare basename "n" and "x" re-encoded as base85 of the
    raw 32 bytes (lossless; _ungroup_photo_rows decodes it back to hex).

    ORDER-PRESERVING BY CONSTRUCTION: rows are grouped by RUN, not by a
    dict keyed on (root_id, folder) — a new group starts whenever the key
    differs from the PREVIOUS row's key, even if that key was already
    seen earlier in the list. `rows` is NOT reliably folder-contiguous:
    walk_library sorts by full path (component order), so a folder's own
    files are split apart whenever a subfolder's name sorts between them
    (e.g. "2020/apple.jpg", "2020/summer/b.jpg", "2020/zoo.jpg" — the
    "2020" folder's two files are not adjacent). Grouping by first-seen
    key (a plain dict) would silently move "2020/zoo.jpg" into the first
    "2020" group and corrupt the flattened order on ungroup, which
    load_catalog's Photo reconstruction and Album.members' index-based
    membership both depend on. Run-based grouping costs only a repeated
    folder string in that split case, and reproduces the exact original
    flattened order unconditionally. `rows` may come from freshly-built
    Photo objects (save_catalog) or straight from an old plain-JSON
    file's "photos" array (library._promote_upgrade_catalog regrouping a
    pre-v13 catalog) — both are the same flat shape."""
    groups: list[dict] = []
    prev_key: tuple[str, str] | None = None
    for row in rows:
        rel = row["r"]
        folder, _, name = rel.rpartition("/")
        root_id = row.get("R") or first_root_id
        key = (root_id, folder)
        if key != prev_key:
            group: dict = {"p": folder, "ph": []}
            if root_id != first_root_id:
                group["R"] = root_id
            groups.append(group)
            prev_key = key
        new_row: dict = {"n": name}
        for k, v in row.items():
            if k in ("r", "R"):
                continue
            if k == "x":
                v = base64.b85encode(bytes.fromhex(v)).decode("ascii")
            new_row[k] = v
        groups[-1]["ph"].append(new_row)
    return groups


def _ungroup_photo_rows(groups: list, first_root_id: str) -> list[dict]:
    """Inverse of _group_photo_rows: expand the v13 grouped "photos" array
    back into the flat per-row shape load_catalog's Photo-reconstruction
    loop already expects ("r"/absent-means-roots[0] "R"/hex "x"), in the
    exact original flattened order (group order, then row order within
    each group — see _group_photo_rows' ordering guarantee)."""
    rows: list[dict] = []
    for group in groups:
        folder = group.get("p") or ""
        if not isinstance(folder, str):
            raise TypeError("group 'p' must be a string")
        root_id = group.get("R") or first_root_id
        for row in group.get("ph", ()):
            name = row["n"]  # KeyError on absent -> the defensive net below
            if not isinstance(name, str):
                raise TypeError("row 'n' must be a string")
            rel = f"{folder}/{name}" if folder else name
            new_row: dict = {"r": rel}
            if root_id != first_root_id:
                new_row["R"] = root_id
            for k, v in row.items():
                if k == "n":
                    continue
                if k == "x":
                    v = base64.b85decode(v).hex()
                new_row[k] = v
            rows.append(new_row)
    return rows


def save_catalog(catalog: Catalog, path: Path) -> None:
    """Atomically serialize the catalog to `path` (write-temp-rename).

    Multiroot header (design §5): an explicit library (catalog.library_id
    set) writes "library_id" + a "roots" snapshot (id/path pairs — paths
    are informational/debug only, resolution authority is library.json);
    the implicit legacy library (no library_id) keeps the original
    "library": str(root) header, UNCHANGED, so a single-root catalog's
    header round-trips byte-identical to a pre-multiroot save — and every
    photo row's CONTENT (via the absent-"R" convention below) is preserved
    field-for-field, though not byte-identically: fauxcasa-ed5.5 folder-
    groups the rows and base85-encodes "x" (see _group_photo_rows), so the
    serialized shape itself differs from a pre-multiroot save.

    fauxcasa-ed5.5: the file itself is zstd level 3 over compact-separator
    JSON whose "photos" is folder-grouped (see _group_photo_rows) — the
    on-disk size budget (spec §10 item 20); see
    docs/research/catalog-size-analysis.md for the measurements behind
    this shape. load_catalog detects the format by content (ZSTD_MAGIC),
    not by trusting "version" alone."""
    import zstandard

    first_root_id = _expand_root_id(None, catalog.roots)
    if catalog.library_id:
        header: dict = {
            "library_id": catalog.library_id,
            "roots": [{"id": r.id, "path": str(r.path)}
                      for r in catalog.roots],
        }
    else:
        header = {"library": str(catalog.root)}
    flat_rows = [_photo_to_row(p, first_root_id) for p in catalog.photos]
    # v14 ini freshness signals (fauxcasa-cam.14 step 4): nested by the
    # ACTUAL root_id (never the absent-means-roots[0] trick the photo/
    # folder rows use) — ini_sigs has one entry per ini-bearing folder,
    # far fewer than photo rows, so the byte-shaving isn't worth the
    # ambiguity a root_id-prefixed folder_rel could theoretically
    # introduce. See load_catalog for the inverse.
    ini_sigs_by_root: dict[str, dict[str, list[int]]] = {}
    for (root_id, folder_rel), sig in catalog.ini_sigs.items():
        ini_sigs_by_root.setdefault(root_id, {})[folder_rel] = list(sig)
    data = {
        "version": CATALOG_VERSION,
        **header,
        "photos": _group_photo_rows(flat_rows, first_root_id),
        "ini_sigs": ini_sigs_by_root,
        # only folders that carry a description (title/count are derived);
        # the dict KEY already carries the root-id prefix convention (see
        # _folder_json_key, applied when the folders dict is BUILT in
        # load_catalog/scan_library) — nothing to recompute here.
        "folders": {key: f.description
                    for key, f in catalog.folders.items() if f.description},
        # folder-level "Hidden Folders" membership: can't be re-derived on
        # load (the warm path never re-reads inis), so persist it explicitly
        # — it drives both per-photo visibility and Folder.folder_hidden.
        "hidden_folders": [key for key, f in catalog.folders.items()
                           if f.folder_hidden],
        # contact id -> display name registry (already contacts.xml-merged
        # at scan time; the warm path never re-reads inis or contacts.xml)
        "contacts": catalog.contacts,
        # v11: which registry ids the db3 rescue named (sorted for a
        # deterministic file; the warm path never re-reads db3)
        "db3_contacts": sorted(catalog.db3_contacts),
        "albums": [
            {"uid": a.uid, "name": a.name, "date": a.date,
             "description": a.description, "members": a.members,
             # v6 flags, key-absent when False (keeps the file small)
             **({"placeholder": True} if a.placeholder else {}),
             **({"pal": True} if a.pal_sourced else {})}
            for a in catalog.albums.values()
        ],
    }
    # v14 contacts.xml freshness signal: key absent (not null) when None,
    # same "only present when it means something" convention as
    # "backfill" below — a legacy catalog with no contacts.xml configured
    # writes no key at all, matching a warm load's data.get(...) default.
    if catalog.contacts_sig is not None:
        data["contacts_sig"] = list(catalog.contacts_sig)
    # Backfill progress (cam.12): persisted only while pending/underway,
    # so an indexer-built catalog's file shape is unchanged. The periodic
    # mid-backfill saves ride the same atomic write-temp-rename, so a
    # killed app always finds either the previous cursor or the new one.
    if catalog.backfill_state != BACKFILL_COMPLETE:
        data["backfill"] = {"state": catalog.backfill_state,
                            "cursor": catalog.backfill_cursor}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".catalog.tmp")
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    tmp.write_bytes(zstandard.ZstdCompressor(level=3).compress(payload))
    tmp.replace(path)


def save_catalog_retrying(catalog: Catalog, path: Path,
                          attempts: int = 5,
                          backoff: float = 0.1) -> None:
    """save_catalog with linear-backoff retry on transient OSError.

    On Windows, os.replace onto catalog.json briefly held open by a reader
    (antivirus, search indexer, any tool peeking at the file) raises
    PermissionError (WinError 5 / WinError 32).  Retries up to `attempts`
    times, sleeping `backoff * attempt` seconds between each pair; raises
    the final OSError if all attempts fail.
    """
    # Linear schedule: 0.1 s, 0.2 s, … up to (attempts-1) * backoff.
    total = max(1, attempts)  # attempts <= 0 would fall through to raise None
    err: OSError | None = None
    for attempt in range(total):
        try:
            save_catalog(catalog, path)
            return
        except OSError as e:
            err = e
            if attempt < total - 1:
                time.sleep(backoff * (attempt + 1))
    raise err  # type: ignore[misc]


def load_catalog(path: Path, cfg: Path | LibraryConfig,
                 probes=None) -> Catalog | None:
    """Reconstruct a Catalog from a persisted file, or None if absent,
    unreadable, or an older/foreign/corrupt format (-> caller does a cold
    walk). Derives folder/name/visible and folder title/photo_count the
    same way scan_library does. Caveat: empty-string caption/description
    are normalized to None on both paths, so a loaded catalog matches a
    freshly walked-AND-indexed one for every field consumers read — the
    persisted catalog carries the merged in-file caption/keywords that the
    indexer wrote, which a scan-only walk has not yet applied (see the
    module docstring).

    `cfg` is a LibraryConfig (multiroot .b, design §5/§6); a bare Path is
    also accepted and auto-wrapped via library.legacy_config for source
    compatibility with the many pre-multiroot callers that pass a plain
    library-root path — behaviorally identical to constructing a legacy
    config themselves.

    VERSIONING (design §5): the CURRENT format (CATALOG_VERSION) validates
    "library_id" against `cfg.library_id` for an explicit (non-legacy)
    library — closing the previous gap where the stored library identity
    was never checked. COMPAT: the previous CATALOG_VERSION is still
    accepted, but ONLY when `cfg` has exactly one root — its rows carry no
    "R" key at all, so every photo is unambiguously roots[0]. A
    previous-version file for a multi-root library returns None (cold
    walk): that format cannot express root disambiguation.

    FORMAT (fauxcasa-ed5.5): a v13 file is ALWAYS a zstd frame (ZSTD_MAGIC)
    wrapping folder-grouped compact JSON (see _group_photo_rows); a plain
    (uncompressed) file is only ever accepted at PRE_MULTIROOT_VERSION,
    single-root — a plain v12 (or any other plain version) file, and a
    zstd-wrapped file at any version but CATALOG_VERSION, both fail the
    gate below -> None -> cold rebuild. The catalog is a regenerable
    cache, so there is no migration path for either case, by design.

    zstandard is imported lazily, only inside the `if compressed:`
    branch below — the only place decompression happens — so a
    zstandard-less environment still degrades to a cold walk (None) for
    every plain-JSON file (a legacy v11, or any foreign/corrupt file),
    rather than raising an uncaught ImportError on load_catalog's
    unguarded call site in main()."""
    if not isinstance(cfg, LibraryConfig):
        cfg = legacy_config(cfg)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    compressed = raw[:4] == ZSTD_MAGIC
    if compressed:
        try:
            import zstandard
        except ImportError:
            return None
        try:
            raw = zstandard.ZstdDecompressor().decompress(raw)
        except (zstandard.ZstdError, MemoryError):
            return None
    try:
        data = json.loads(raw)
    except (ValueError, MemoryError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    if version == CATALOG_VERSION:
        if not compressed:
            return None  # v13 is only ever the zstd-wrapped grouped shape
        current = True
    elif version == PRE_MULTIROOT_VERSION and len(cfg.roots) == 1:
        # Compat path (design §5): a v11 file predates root
        # disambiguation entirely (no "R", no "roots" header) and is
        # only unambiguous when the library has exactly one root — every
        # row is then implicitly roots[0]. A HARD-CODED version number,
        # not "CATALOG_VERSION - 1": this carve-out is specific to the
        # v11->v12 multiroot transition and must not silently reopen for
        # some future unrelated bump the same way every prior bump (v1
        # through v11) rejected its immediate predecessor outright.
        if compressed:
            return None  # the v11 carve-out only ever applied to plain JSON
        current = False
    else:
        return None
    if current and not cfg.is_legacy \
            and data.get("library_id") != cfg.library_id:
        return None
    if not cfg.roots:
        return None
    root = cfg.roots[0].path
    root_by_id = {r.id: r for r in cfg.roots}
    first_root_id = _expand_root_id(None, cfg.roots)
    rows_field = data.get("photos")
    if not isinstance(rows_field, list):
        return None

    # Any structural defect in a version-tagged file (the case the version
    # gate is meant to guard) degrades to a cold walk rather than crashing
    # startup — main() calls this unguarded.
    try:
        # v13's "photos" is folder-grouped; ungroup it back into the flat
        # per-row shape the loop below has always expected (hex "x", plain
        # "r"/"R") so the rest of this function is unchanged either way.
        rows = (_ungroup_photo_rows(rows_field, first_root_id)
                if compressed else rows_field)

        hidden_folders = data.get("hidden_folders", [])
        if not isinstance(hidden_folders, list):
            hidden_folders = []
        hidden_folders = set(hidden_folders)

        photos: list[Photo] = []
        for row in rows:
            rel = row["r"]
            folder, _, name = rel.rpartition("/")
            g = row.get("g")
            wh = row.get("wh")
            cp = row.get("cp")
            row_root_id = (_expand_root_id(row.get("R"), cfg.roots)
                           if current else first_root_id)
            folder_key = _folder_json_key(folder, row_root_id, first_root_id)
            p = Photo(
                rel=rel, folder=folder, name=name,
                root_id=row_root_id,
                # media is DERIVED from the extension, like folder/name
                media="video" if is_video_suffix(name) else "image",
                star=int(row.get("s") or 0), caption=row.get("c"),
                keywords=tuple(row.get("k", ())), rotate=row.get("o", 0),
                hidden=bool(row.get("h")), albums=tuple(row.get("a", ())),
                faces=tuple((tuple(rect), cid, fname)
                            for rect, cid, fname in row.get("f", ())),
                date_taken=row.get("d"),
                geotag=(float(g[0]), float(g[1])) if g else None,
                dims=(int(wh[0]), int(wh[1])) if wh else None,
                crop=(float(cp[0]), float(cp[1]),
                      float(cp[2]), float(cp[3])) if cp else None,
                edits=tuple((str(k), str(v)) for k, v in row.get("e", ())),
                size=row.get("z", -1), mtime=row.get("m", -1),
                sha256=row.get("x"),
            )
            p.visible = (not p.hidden and not _is_stashed(folder)
                         and folder_key not in hidden_folders)
            photos.append(p)

        _link_stashed_originals(photos)

        folder_desc = data.get("folders", {})
        if not isinstance(folder_desc, dict):
            folder_desc = {}
        folders: dict[str, Folder] = {}
        for p in photos:
            key = _folder_json_key(p.folder, p.root_id, first_root_id)
            if key not in folders:
                p_root = root_by_id.get(p.root_id)
                root_label = (p_root.path.name or str(p_root.path)) \
                    if p_root is not None else (root.name or str(root))
                title = p.folder.rsplit("/", 1)[-1] if p.folder \
                    else root_label
                folders[key] = Folder(
                    rel=p.folder, title=title,
                    description=folder_desc.get(key),
                    folder_hidden=key in hidden_folders,
                    root_id=p.root_id)
            folders[key].total_count += 1  # reveal-mode count
            if p.visible:
                folders[key].photo_count += 1

        albums: dict[str, Album] = {}
        for a in data.get("albums", []):
            albums[a["uid"]] = Album(
                uid=a["uid"], name=a["name"], date=a.get("date"),
                description=a.get("description"),
                members=list(a.get("members", [])),
                placeholder=bool(a.get("placeholder")),
                pal_sourced=bool(a.get("pal")),
            )

        contacts = data.get("contacts", {})
        if not isinstance(contacts, dict):
            contacts = {}

        db3_contacts = data.get("db3_contacts", [])
        if not isinstance(db3_contacts, list):
            db3_contacts = []
        db3_contacts = {str(c) for c in db3_contacts}

        # v14 ini/contacts freshness signals (fauxcasa-cam.14 step 4):
        # absent on any pre-v14 file (the v11 compat path never carries
        # these keys either) -> {} / None, same as a fresh catalog that
        # simply has no signal yet. Nested by the actual root_id (see
        # save_catalog) — no folder-key inversion needed, unlike
        # "folders"/"hidden_folders".
        ini_sigs: dict[tuple[str, str], tuple[int, int]] = {}
        ini_sigs_raw = data.get("ini_sigs", {})
        if isinstance(ini_sigs_raw, dict):
            for rid, per_folder in ini_sigs_raw.items():
                if not isinstance(per_folder, dict):
                    continue
                for frel, sig in per_folder.items():
                    if isinstance(sig, list) and len(sig) == 2:
                        ini_sigs[(str(rid), str(frel))] = \
                            (int(sig[0]), int(sig[1]))

        contacts_sig: tuple[int, int] | None = None
        contacts_sig_raw = data.get("contacts_sig")
        if isinstance(contacts_sig_raw, list) and len(contacts_sig_raw) == 2:
            contacts_sig = (int(contacts_sig_raw[0]),
                            int(contacts_sig_raw[1]))

        # Backfill progress (cam.12): an absent key means complete (the
        # indexer-built shape — save_catalog omits it then). A non-dict
        # value raises here (AttributeError -> the defensive net); an
        # unknown state string in a version-current file is the same
        # corrupt case, so it degrades to a cold walk too. The cursor is
        # clamped to the photo count so a hand-edited value can never make
        # the resume index past the list.
        backfill_state, backfill_cursor = BACKFILL_COMPLETE, 0
        bf = data.get("backfill")
        if bf is not None:
            state = bf.get("state")
            if state == BACKFILL_IN_PROGRESS:
                backfill_state = state
                backfill_cursor = min(max(0, int(bf.get("cursor", 0))),
                                      len(photos))
            elif state == BACKFILL_NOT_STARTED:
                backfill_state = state
            elif state != BACKFILL_COMPLETE:
                return None
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        return None

    catalog = Catalog(root=root, photos=photos, folders=folders,
                      albums=albums, contacts=contacts,
                      db3_contacts=db3_contacts,
                      backfill_state=backfill_state,
                      backfill_cursor=backfill_cursor,
                      ini_sigs=ini_sigs, contacts_sig=contacts_sig,
                      roots=list(cfg.roots), library_id=cfg.library_id)
    # "populated when the catalog is opened" (design §8, bead .e) — a warm
    # load is exactly an open, so check every root's reachability now
    # rather than leaving offline_ids at its all-online default until the
    # first reconcile. `probes` lets the open's resolve pass share its
    # volume-probe results here (fauxcasa-t0a).
    catalog.refresh_offline_ids(probes)
    return catalog


@dataclass
class Drift:
    """Cheap-signal (size+mtime) diff of a persisted catalog vs the live
    library — the N6 'cheap signals first' check, no hashing."""
    added: int = 0
    removed: int = 0
    modified: int = 0
    # An externally added/removed/modified .picasa.ini sidecar somewhere
    # under this root (fauxcasa-cam.14 step 4 — set by reconcile_walk from
    # Catalog.ini_sigs), OR — folded in by main._reconcile_online_roots,
    # which does this check ONCE for the whole library rather than once
    # per root, since contacts.xml is a single machine-local file — a
    # changed contacts.xml. Either would otherwise bake stale names/
    # metadata into a warm start indefinitely, since neither touches any
    # photo file's own size/mtime.
    ini_changed: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.modified
                    or self.ini_changed)

    def summary(self) -> str:
        return (f"+{self.added} added, -{self.removed} removed, "
                f"~{self.modified} modified")


def reconcile_walk(catalog: Catalog, root: Path,
                   scan_filter: ScanFilter | None = None,
                   cancel=None,
                   exts: frozenset[str] | set[str] | None = None,
                   root_id: str = LEGACY_ROOT_ID,
                   ) -> Drift | None:
    """Fresh walk + stat of ONE root, compared to that root's slice of the
    catalog's stored signals. Photos whose stored signal is absent (-1)
    can't be compared and are never counted as modified (only a genuine
    size/mtime change is). Returns None if the cancel event is set
    mid-walk, so shutdown can reap the background thread promptly instead
    of waiting out a 100k stat-walk on the join timeout. `exts` must be
    the SAME effective extension set the catalog was walked with
    (walk_library docstring), or every excluded file would read as
    phantom drift.

    `root_id` identifies which library root `root` is (default ""/
    LEGACY_ROOT_ID — the single-root/legacy case, unchanged). The
    reconciliation key is (root_id, rel) (design §6): comparing against
    the catalog subset for THIS root_id only means a photo with the same
    `rel` on a different root never counts as added/removed/modified here
    — callers walk each root independently (design §9; looping over all
    online roots is bead .e's job, out of this function's scope).

    Drift.ini_changed (fauxcasa-cam.14 step 4) checks only THIS root's
    ini_sigs slice — every folder_rel `catalog.ini_sigs` already tracks
    for this root_id, unioned with the ANCESTOR CLOSURE of every
    folder_rel the fresh walk just visited (_ancestor_closure — every
    root-relative prefix including "", not just the photo-owning folders
    themselves; a newly created ini in a folder that owns no photo of its
    own, like the bare library root, would otherwise never be probed —
    Codex review finding 2), each re-stat'd via _folder_ini_sig and
    compared against the stored signal. The contacts.xml check is NOT
    here — it's a single machine-local file, not a per-root artifact, so
    it's done ONCE by the multiroot reconcile helper
    (main._reconcile_online_roots), not per root."""
    root = root.resolve()
    files = walk_library(root, scan_filter, exts)
    fresh: dict[tuple[str, str], tuple[int, int]] = {}
    folder_rels: set[str] = set()
    for i, (p, rel) in enumerate(zip(files, rel_paths(root, files))):
        if cancel is not None and i % 512 == 0 and cancel.is_set():
            return None
        try:
            st = p.stat()
            fresh[(root_id, rel)] = (st.st_size, int(st.st_mtime))
        except OSError:
            fresh[(root_id, rel)] = (-1, -1)
        folder_rels.add(rel.rpartition("/")[0])
    old = {(p.root_id, p.rel): (p.size, p.mtime)
           for p in catalog.photos if p.root_id == root_id}
    drift = Drift()
    for key, sig in fresh.items():
        if key not in old:
            drift.added += 1
        elif old[key] != (-1, -1) and old[key] != sig:
            drift.modified += 1
    for key in old:
        if key not in fresh:
            drift.removed += 1

    old_ini_slice = {frel: sig for (rid, frel), sig in catalog.ini_sigs.items()
                     if rid == root_id}
    probe_rels = _ancestor_closure(folder_rels) | set(old_ini_slice)
    for frel in probe_rels:
        fresh_ini_sig = _folder_ini_sig(root / frel if frel else root)
        if old_ini_slice.get(frel) != fresh_ini_sig:
            drift.ini_changed = True
            break
    return drift
