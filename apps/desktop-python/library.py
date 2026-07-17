"""Library model for Fauxcasa multi-root support (fauxcasa-ed5.7).

A library = one library-home directory + N watched roots (design §2 —
library model and library-home). The home contains .fauxcasa/library.json
(format 1) which carries the durable roots list, a minted library_id, and
per-root identity signals.

Opening a bare directory that has no library.json is an implicit *legacy*
library: a degenerate LibraryConfig with one root of id "" and no home.
This keeps day-zero (single-root) behavior byte-identical — no migration
runs, no prompts appear, no caches are rebuilt.

This module is stdlib-only (json, re, uuid, dataclasses, pathlib).
It carries NO PEP 723 header — only entry scripts (main.py, test_tracer.py)
carry that. It imports from no other app module (one-directional, no circularity).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LIBRARY_DIR = ".fauxcasa"        # per-home library-state directory
LIBRARY_FILE = "library.json"
ROOT_MARKER = ".fauxcasa-root"   # optional in-root id-recovery marker
LIBRARY_FORMAT = 1
# "" is the reserved id of the implicit legacy root; it never appears inside
# a library.json. Ids are never reused after root removal so stale sidecars
# cannot alias.
LEGACY_ROOT_ID = ""

_ROOT_ID_RE = re.compile(r"[0-9a-f]{8}\Z")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class LibraryRoot:
    id: str
    path: Path
    volume_uuid: str | None = None   # populated by bead .f, carried now
    vol_rel: str | None = None
    label: str = ""                  # default label: path.name


@dataclass
class LibraryConfig:
    library_id: str                  # full uuid4 str; "" for implicit legacy
    name: str
    roots: list[LibraryRoot]
    home: Path | None = None         # dir containing .fauxcasa/; None = legacy

    @property
    def is_legacy(self) -> bool:
        return self.home is None


# ---------------------------------------------------------------------------
# Id minting
# ---------------------------------------------------------------------------

def mint_root_id() -> str:
    """8 lowercase hex chars from uuid4. Never path-derived (design N6:
    nothing keyed to drive letters or mount points)."""
    return uuid.uuid4().hex[:8]


def mint_library_id() -> str:
    """Full uuid4 string for a new library identity."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Legacy / convenience constructors
# ---------------------------------------------------------------------------

def legacy_config(path: Path) -> LibraryConfig:
    """Return the implicit legacy LibraryConfig for a bare photo directory.
    One root with id "" (LEGACY_ROOT_ID), no home. Callers receive this
    when resolve_open_path finds no library.json."""
    return LibraryConfig(
        library_id="",
        name="",
        roots=[LibraryRoot(id=LEGACY_ROOT_ID, path=Path(path).resolve())],
        home=None,
    )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def library_json_path(home: Path) -> Path:
    """Canonical path to library.json inside a library-home directory."""
    return home / LIBRARY_DIR / LIBRARY_FILE


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_library(home: Path) -> LibraryConfig | None:
    """Load library.json from *home*. Fail-soft: return None on any parse
    anomaly, missing file, format mismatch, or structural violation (mirrors
    load_catalog's fail-soft discipline).

    Nested roots — where one root path is equal to or an ancestor/descendant
    of another — are rejected here because they would double-count files and
    break the walk parity invariant (design §4). Stored paths are kept
    verbatim (Path(s), NO resolve) — last-known-path semantics; resolution
    chain is bead .f."""
    p = library_json_path(home)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        data = json.loads(text)
    except ValueError:
        return None

    if not isinstance(data, dict):
        return None
    if data.get("format") != LIBRARY_FORMAT:
        return None

    library_id = data.get("library_id")
    if not library_id or not isinstance(library_id, str):
        return None

    raw_roots = data.get("roots")
    if not isinstance(raw_roots, list) or not raw_roots:
        return None

    roots: list[LibraryRoot] = []
    seen_ids: set[str] = set()
    for r in raw_roots:
        if not isinstance(r, dict):
            return None
        rid = r.get("id")
        if not rid or not isinstance(rid, str):
            return None
        if rid == LEGACY_ROOT_ID:
            return None  # "" is reserved for the implicit legacy root
        rpath_raw = r.get("path")
        if not rpath_raw or not isinstance(rpath_raw, str):
            return None
        if rid in seen_ids:
            return None  # duplicate ids
        seen_ids.add(rid)
        roots.append(LibraryRoot(
            id=rid,
            path=Path(rpath_raw),          # verbatim, no resolve
            volume_uuid=r.get("volume_uuid"),
            vol_rel=r.get("vol_rel"),
            label=r.get("label") or "",
        ))

    # Nested-root check: resolve before comparing so symlinks and relative
    # paths don't fool the test. Reject if any two roots are equal or
    # one contains the other.
    resolved = [r.path.resolve() for r in roots]
    for i, ri in enumerate(resolved):
        for j, rj in enumerate(resolved):
            if i == j:
                continue
            if ri == rj or ri.is_relative_to(rj) or rj.is_relative_to(ri):
                return None  # nested or duplicate roots

    return LibraryConfig(
        library_id=library_id,
        name=data.get("name") or "",
        roots=roots,
        home=Path(home),
    )


def save_library(cfg: LibraryConfig) -> None:
    """Atomically write library.json for *cfg*. Raises ValueError for legacy
    configs (a legacy library has no home and is never written). Uses the
    same write-temp-rename discipline as save_catalog."""
    if cfg.is_legacy:
        raise ValueError("cannot save a legacy LibraryConfig (home is None)")

    data = {
        "format": LIBRARY_FORMAT,
        "library_id": cfg.library_id,
        "name": cfg.name,
        "roots": [
            {
                "id": r.id,
                "path": r.path.as_posix(),
                "volume_uuid": r.volume_uuid,
                "vol_rel": r.vol_rel,
                "label": r.label,
            }
            for r in cfg.roots
        ],
    }

    p = library_json_path(cfg.home)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Use with_name to avoid Path.with_suffix(".tmp") => "library.tmp" trap
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Open-by-path handle
# ---------------------------------------------------------------------------

def resolve_open_path(path: Path) -> LibraryConfig:
    """The open-by-path handle. Try to load library.json from *path*; if
    that succeeds return the explicit config. Otherwise return an implicit
    legacy config (fail-soft N3 style — a bare photo dir, or a dir with a
    corrupt/foreign library.json, is an implicit legacy library, keeping
    day-zero single-root behavior unchanged)."""
    cfg = load_library(path)
    if cfg is not None:
        return cfg
    return legacy_config(path)


# ---------------------------------------------------------------------------
# Root management
# ---------------------------------------------------------------------------

def add_root(cfg: LibraryConfig,
             path: Path,
             label: str | None = None) -> LibraryRoot:
    """Add a new watched root to *cfg* and return the new LibraryRoot.
    Raises ValueError for legacy configs (promotion is bead .d) and for
    nested/duplicate roots (design §13 item 1: nested roots break the walk
    parity invariant). The caller is responsible for saving; marker writing
    is the caller's journey step (bead .d wires it)."""
    if cfg.is_legacy:
        raise ValueError(
            "cannot add_root to a legacy LibraryConfig; use promotion (bead .d)")

    new = Path(path).resolve()

    # Validate against existing roots
    for r in cfg.roots:
        existing = r.path.resolve()
        if new == existing:
            raise ValueError(
                f"path {path!r} is already a root in this library")
        if new.is_relative_to(existing):
            raise ValueError(
                f"path {path!r} is inside existing root {r.path!r} "
                "(nested roots break the walk parity invariant)")
        if existing.is_relative_to(new):
            raise ValueError(
                f"existing root {r.path!r} is inside new path {path!r} "
                "(nested roots break the walk parity invariant)")

    # Guard: a new root must not swallow the library-home
    if cfg.home is not None:
        home_resolved = cfg.home.resolve()
        if home_resolved.is_relative_to(new):
            # Check this wasn't already caught above (home inside a root is
            # only an issue when home is *outside* all current roots)
            already_covered = any(
                home_resolved.is_relative_to(r.path.resolve())
                for r in cfg.roots
            )
            if not already_covered:
                raise ValueError(
                    f"path {path!r} would swallow the library-home {cfg.home!r}")

    # Mint a unique id
    existing_ids = {r.id for r in cfg.roots}
    rid = mint_root_id()
    while rid in existing_ids:
        rid = mint_root_id()

    new_root = LibraryRoot(
        id=rid,
        path=Path(path),
        label=label or Path(path).name,
    )
    cfg.roots.append(new_root)
    return new_root


# ---------------------------------------------------------------------------
# Root marker file
# ---------------------------------------------------------------------------

def write_root_marker(root_path: Path, root_id: str) -> bool:
    """Write the optional .fauxcasa-root id-recovery marker. Returns True
    on success, False on any OSError (read-only NAS roots are first-class;
    absence is never an error). No mkdir."""
    marker = root_path / ROOT_MARKER
    try:
        marker.write_text(root_id + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def read_root_marker(root_path: Path) -> str | None:
    """Read the .fauxcasa-root marker and return the root_id if valid.
    Returns None on any OSError or if the content does not match the
    expected 8-lowercase-hex-char format."""
    marker = root_path / ROOT_MARKER
    try:
        content = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if _ROOT_ID_RE.fullmatch(content):
        return content
    return None
