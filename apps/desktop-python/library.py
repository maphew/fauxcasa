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
import shutil
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


# ---------------------------------------------------------------------------
# Promotion (Journey A, design §1/§10, bead .d)
# ---------------------------------------------------------------------------
#
# promote_library() is the FIRST "Add folder to library..." (or explicit
# "Convert to library...") on a legacy single-root library: mint a library
# identity, rename ONE cache directory, and additively rewrite only
# catalog.json's header. NO re-walk, no re-hash, no thumbnail rebuild —
# photo rows and per-root fcache/sidecar CONTENT are byte-untouched, only
# paths/names change (design §1's "promotion is a rename, not a re-index").
#
# Each phase below is its OWN top-level function (not inlined into
# promote_library) purely so a test can monkeypatch exactly one phase to
# raise and assert the rest of promote_library rolls back correctly —
# these are internal helpers, not part of the module's public surface.

class PromotionError(Exception):
    """Raised by promote_library's own precondition checks (e.g. a target
    cache dir already exists). Failures from the underlying filesystem
    calls propagate as their own OSError/ValueError instead — both are
    caught and rolled back identically by promote_library."""


def _promote_rename_cache_dir(old_dir: Path, new_dir: Path) -> bool:
    """Rename the WHOLE per-library cache dir in one shot (design §7/§10):
    catalog.json, thumbs.fcache(.json), config.json, import-report.json all
    move atomically together. Returns False (no-op, not an error) if
    `old_dir` doesn't exist — a legacy library that was never opened/
    scanned has no cache dir to migrate; promotion still succeeds and the
    first open simply does a cold walk into the new digest, like any fresh
    library. Raises PromotionError if `new_dir` already exists (would
    silently clobber another library's cache)."""
    if not old_dir.is_dir():
        return False
    if new_dir.exists():
        raise PromotionError(f"target cache dir already exists: {new_dir}")
    old_dir.rename(new_dir)
    return True


def _promote_rename_fcache(cache_dir: Path, root_id: str) -> tuple[bool, bool]:
    """Rename thumbs.fcache -> thumbs-<root_id>.fcache and
    thumbs.fcache.json -> thumbs-<root_id>.fcache.json WITHIN `cache_dir`.
    Design §10 point 3: "untouched beyond the rename" — sidecar CONTENT is
    NOT rewritten (no library_id/root_id/sidecar_version added here); it
    stays legacy-shaped and still parses fine under the typed-entry reader
    (thumbcache.parse_sidecar_entry accepts plain-string entries
    unconditionally) and still binds fine (thumbcache.bind() only compares
    the files[] list, never a header key). Returns (fcache_renamed,
    sidecar_renamed) so the caller can roll back precisely what ran —
    either half may be absent (e.g. a cache dir with no persisted sidecar
    yet, mid-build)."""
    old_fcache = cache_dir / "thumbs.fcache"
    old_sidecar = cache_dir / "thumbs.fcache.json"
    new_fcache = cache_dir / f"thumbs-{root_id}.fcache"
    new_sidecar = cache_dir / f"thumbs-{root_id}.fcache.json"
    fcache_renamed = sidecar_renamed = False
    if old_fcache.is_file():
        old_fcache.rename(new_fcache)
        fcache_renamed = True
    if old_sidecar.is_file():
        old_sidecar.rename(new_sidecar)
        sidecar_renamed = True
    return fcache_renamed, sidecar_renamed


def _promote_upgrade_catalog(cat_path: Path, library_id: str, root_id: str,
                             root: Path, catalog_version: int) -> bool:
    """Header-only in-place upgrade of catalog.json (design §5/§10 — the
    migrator N3 says could be skipped, kept only to preserve expensive
    sha256 backfill): add "library_id"/"roots", drop "library", bump
    "version" — PHOTO ROWS (data["photos"]) are copied over VERBATIM, never
    touched, which is exactly what keeps the promoted-rows-equal-old-rows
    property true. The original file is preserved as "catalog.json.bak"
    BEFORE the atomic write-temp-rename, so a crash between the two leaves
    either the untouched original or the fully-upgraded file, never a
    half-written one. Returns False (no-op) if `cat_path` doesn't exist yet
    (a never-scanned legacy library has no catalog to upgrade). Any parse
    anomaly (malformed JSON, non-dict document) raises — promote_library
    treats that as a failure of THIS step and rolls the whole promotion
    back, per design §10 point 4 ("on any failure at any step")."""
    if not cat_path.is_file():
        return False
    old_text = cat_path.read_text(encoding="utf-8")
    data = json.loads(old_text)  # ValueError on malformed JSON -> caller rolls back
    if not isinstance(data, dict):
        raise PromotionError(f"{cat_path}: not a JSON object")

    new_data: dict = {
        "version": catalog_version,
        "library_id": library_id,
        "roots": [{"id": root_id, "path": str(root)}],
    }
    for k, v in data.items():
        if k in ("version", "library", "library_id", "roots"):
            continue
        new_data[k] = v  # "photos" et al. copied verbatim, never touched

    bak_path = cat_path.with_name(cat_path.name + ".bak")
    bak_path.write_text(old_text, encoding="utf-8")
    tmp = cat_path.with_name(cat_path.name + ".tmp")
    tmp.write_text(json.dumps(new_data), encoding="utf-8")
    tmp.replace(cat_path)
    return True


def _promote_rollback(root: Path, home: Path, old_dir: Path, new_dir: Path,
                      root_id: str, *, library_json_written: bool,
                      marker_written: bool, cache_dir_renamed: bool) -> None:
    """Best-effort undo of whatever promote_library got through before it
    failed, most-recently-attempted-step first. Every sub-step is
    independently guarded (one failed undo must never stop the rest) —
    worst case some cruft (a stray .bak/.tmp, an orphaned marker) is left
    behind, but the library is always left in a state that still opens as
    implicit-legacy (design §10 point 4's reversal contract: delete
    `.fauxcasa/` and the app falls back to the original cold walk)."""
    if cache_dir_renamed:
        cat_path = new_dir / "catalog.json"
        bak_path = new_dir / "catalog.json.bak"
        tmp_path = new_dir / "catalog.json.tmp"
        try:
            if bak_path.is_file():
                cat_path.write_text(bak_path.read_text(encoding="utf-8"),
                                    encoding="utf-8")
                bak_path.unlink()
        except OSError:
            pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            (new_dir / f"thumbs-{root_id}.fcache.json").rename(
                new_dir / "thumbs.fcache.json")
        except OSError:
            pass
        try:
            (new_dir / f"thumbs-{root_id}.fcache").rename(
                new_dir / "thumbs.fcache")
        except OSError:
            pass
        try:
            new_dir.rename(old_dir)
        except OSError:
            pass
    if marker_written:
        try:
            (root / ROOT_MARKER).unlink(missing_ok=True)
        except OSError:
            pass
    if library_json_written:
        shutil.rmtree(home / LIBRARY_DIR, ignore_errors=True)


def promote_library(root: Path, *, cache_root: Path,
                    home: Path | None = None) -> LibraryConfig:
    """Journey A, concretely (design §1/§10): the FIRST "Add folder to
    library..." (or explicit "Convert to library...") on a legacy
    single-root library at `root`. Mints a library identity, renames one
    cache directory, and additively rewrites only catalog.json's header —
    NO re-walk, no re-hash, no thumbnail rebuild.

    `home` defaults to INSIDE `root` (<root>/.fauxcasa/library.json,
    design §2's default location rule); pass an explicit `home` OUTSIDE
    `root` when `root` itself is read-only (the first-class
    read-only-NAS-root case). `volume_uuid` is left None — capturing it is
    bead .f (volumes.py does not exist yet); the resolution chain (design
    §8) tolerates a null UUID by design, so this is a safe deferral.

    On ANY failure, rolls back everything attempted so far (see
    _promote_rollback) and RE-RAISES the original exception — the caller
    (and every test here) sees the real error, not a wrapped one. After a
    failed promote_library call the on-disk state always still opens as
    implicit-legacy (design §10 point 4's reversal contract)."""
    root = Path(root).resolve()
    home = Path(home).resolve() if home is not None else root
    cache_root = Path(cache_root)

    # Deferred imports: catalog.py imports FROM library.py at module scope
    # (Photo.root_id, Catalog.roots use LibraryRoot/LibraryConfig), so a
    # top-level "import catalog"/"import thumbcache" here would be
    # circular. By the time this FUNCTION runs, library.py has already
    # finished loading — Python fully executes a module body, including
    # every `def`, before any external caller can invoke one of its
    # functions — so catalog.py's own "from library import ..." is
    # guaranteed already resolved by then; a deferred import here is safe
    # regardless of which module the caller imported first.
    from catalog import CATALOG_VERSION
    from thumbcache import cache_dir_for

    library_id = mint_library_id()
    root_id = mint_root_id()
    label = root.name or str(root)

    old_dir = cache_dir_for(str(root), cache_root)
    new_dir = cache_dir_for(library_id, cache_root)

    library_json_written = False
    marker_written = False
    cache_dir_renamed = False

    try:
        new_root = LibraryRoot(id=root_id, path=root, label=label)
        cfg = LibraryConfig(library_id=library_id, name=label,
                            roots=[new_root], home=home)
        save_library(cfg)
        library_json_written = True
        marker_written = write_root_marker(root, root_id)  # fail-soft

        cache_dir_renamed = _promote_rename_cache_dir(old_dir, new_dir)
        if cache_dir_renamed:
            _promote_rename_fcache(new_dir, root_id)
            _promote_upgrade_catalog(new_dir / "catalog.json", library_id,
                                     root_id, root, CATALOG_VERSION)

        return cfg
    except Exception:
        _promote_rollback(
            root, home, old_dir, new_dir, root_id,
            library_json_written=library_json_written,
            marker_written=marker_written,
            cache_dir_renamed=cache_dir_renamed,
        )
        raise


# ---------------------------------------------------------------------------
# Picasa watched-folders import (Journey B, design §1/§10, bead .d)
# ---------------------------------------------------------------------------

def import_picasa_watched(folders: list[Path], home: Path, name: str = "",
                          skipped: list[str] | None = None) -> LibraryConfig:
    """Journey B, concretely (design §1): build a FRESH library-home from a
    Picasa watched-folders list, one root per folder, in list order.
    Nested or duplicate folders are SKIPPED rather than aborting the whole
    import (Picasa users routinely accumulate stale/overlapping watched
    entries over the years) — reuses add_root's own nested-root validation
    for the accept/reject rule so it is identical to the interactive
    add-root rule, not a second copy of it.

    `skipped`, if given, is appended with one human-readable reason per
    skipped folder (design doesn't prescribe a report shape; main.py's CLI
    wrapper logs these as warnings)."""
    cfg = LibraryConfig(library_id=mint_library_id(), name=name, roots=[],
                        home=Path(home))
    for folder in folders:
        path = Path(folder)
        if not path.is_dir():
            if skipped is not None:
                skipped.append(f"{folder}: not a directory")
            continue
        try:
            add_root(cfg, path, label=path.name)
        except ValueError as e:
            if skipped is not None:
                skipped.append(f"{folder}: {e}")
    return cfg


# ---------------------------------------------------------------------------
# Picasa watched-folders registry seam (Windows only, bead .d)
# ---------------------------------------------------------------------------
#
# RECON NOTE: docs/research/picasa-binary-notes.md:107 confirms the
# registry ROOT ("Watched-folder config: Preferences\HotFolders") but NOT
# the exact value SHAPE (single REG_SZ list vs. a subkey-per-folder tree) —
# that detail was never recovered from the binary dump. This reader targets
# the best-known location, HKCU\Software\Google\Picasa\Picasa2\Preferences
# \HotFolders as a single REG_SZ, and accepts ';'/'|'/newline as a
# separator defensively since the true delimiter is unconfirmed. If real
# Picasa installs turn out to use a different shape, only
# _read_hotfolders_raw needs to change — everything downstream (the
# separator-tolerant split) is unaffected.

_HOTFOLDERS_KEY = r"Software\Google\Picasa\Picasa2\Preferences"
_HOTFOLDERS_VALUE = "HotFolders"


def _read_hotfolders_raw() -> str | None:
    """Seam for tests: the raw registry read, isolated in its own function
    so it can be monkeypatched without touching winreg at all. Returns None
    when winreg is unavailable (non-Windows) or the key/value is absent —
    NEVER raises; picasa_watched_from_registry turns an absent value into
    the user-facing error."""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _HOTFOLDERS_KEY) as key:
            value, _type = winreg.QueryValueEx(key, _HOTFOLDERS_VALUE)
    except OSError:
        return None
    return str(value) if value else None


def picasa_watched_from_registry() -> list[Path]:
    """Picasa's watched-folders list from the Windows registry (design §1
    Journey B; see the RECON NOTE above the seam functions for the exact
    key and its caveats). Fail-soft would silently hand the user zero
    roots when they explicitly asked for the registry source — so instead
    this raises RuntimeError with a clear, actionable message when the
    key/value is absent, non-Windows, or empty; the CALLER (main.py) is
    responsible for catching it and printing a friendly error rather than
    a traceback."""
    raw = _read_hotfolders_raw()
    if not raw:
        raise RuntimeError(
            "Picasa watched-folders registry value not found "
            rf"(HKCU\{_HOTFOLDERS_KEY}\{_HOTFOLDERS_VALUE}) — pass a "
            "list file instead"
        )
    parts = re.split(r"[;|\n]+", raw)
    return [Path(p) for p in (s.strip() for s in parts) if p]
