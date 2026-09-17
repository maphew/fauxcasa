"""The single home for everything Fauxcasa persists on the user's behalf,
in two halves.

Half A, "Machine-local preferences (cache root)": per-user, per-library
VIEW state that lives beside (never inside) the app's disposable cache —
config.json at the cache root (last-opened library) and config.json inside
each library's variant-free state dir (sort modes, star threshold, folder
view flat/tree, window geometry). None of it is durable library data; all
of it is a convenience that fails soft on a missing/garbage file.

Half B, "In-library tier-2 state": the home for state written INSIDE the
library itself (the sidecar-first write layer, fauxcasa-lgg.1 design, the
fauxcasa-lgg.3 journal, the fauxcasa-lgg.5 stars.json migration). Today
that tier holds only stars.json, via starstore.py — re-exported here so
callers have one import site for "everything Fauxcasa persists" without
needing to know the sidecar-vs-cache-root split. This module writes NO new
files to disk and creates NO new directories of its own: see
docs/releases/v0.1.0.md ("What Fauxcasa writes on your computer") for the
authoritative file inventory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QByteArray
from PySide6.QtWidgets import QApplication

import applog
from grid import DEFAULT_SORT_MODE, SORT_MODES
from starstore import (  # noqa: F401 (re-exported for callers)
    STAR_OVERRIDES_NAME,
    apply_star_overrides,
    load_star_overrides,
    photo_key,
    save_star_overrides,
)
from thumbcache import cache_dir_for

log = applog.log

# ---------------------------------------------------------------------------
# Half A: machine-local preferences (cache root)
# ---------------------------------------------------------------------------


def _config_path(cache_root: Path) -> Path:
    """Per-user config, beside (never inside) the per-library cache dirs —
    cache_dir_for() names those by a 16-hex digest, so 'config.json' is
    collision-free."""
    return cache_root / "config.json"


def _is_filesystem_root(path: Path) -> bool:
    """True for anchors such as '/', 'C:\\', and UNC share roots.

    Opening a whole volume as a photo library makes the first-run picker vanish
    while startup recursively scans the OS tree before the main window exists.
    """
    try:
        p = path.resolve()
    except OSError:
        p = path.absolute()
    return p.parent == p


def _remembered_library(cache_root: Path) -> Path | None:
    """The library chosen on a previous (frozen) run, if it still exists on
    disk; a vanished one is ignored so the app re-prompts. Tolerates a
    missing or garbage config file — recall is a convenience, not a gate."""
    try:
        data = json.loads(_config_path(cache_root).read_text())
    except (OSError, ValueError):
        return None
    # A valid-but-non-object JSON value ('null', '42', '[]') parses fine but
    # has no .get — guard it here, else the AttributeError escapes the
    # (OSError, ValueError) catch and crashes the launch instead of being
    # treated as 'nothing remembered'.
    if not isinstance(data, dict):
        return None
    lib = data.get("library")
    if not isinstance(lib, str) or not lib:
        return None
    p = Path(lib)
    if not p.is_dir():
        return None
    if _is_filesystem_root(p):
        log.warning("ignoring remembered filesystem root library: %s", p)
        return None
    return p


def _config_update(cache_root: Path, key: str, value) -> OSError | None:
    """Shared atomic merge-one-key-into-config.json idiom (fauxcasa-6y0,
    extracted from _remember_library): read the existing doc first (a
    missing/garbage file fails soft to {}) and set only KEY — a blind
    overwrite would wipe whatever else lives in this same per-user
    config.json (e.g. filetypes.save_excluded_exts's own keys, this
    module's other key). Writes via a per-process temp sibling +
    os.replace so a second frozen instance launching concurrently can
    never read a half-written (torn) config — it sees either the old
    file or the whole new one. Returns the OSError on a write failure
    (never raised — callers decide how to log it) or None on success."""
    cfg = _config_path(cache_root)
    tmp = cfg.with_name(f"{cfg.name}.{os.getpid()}.tmp")
    try:
        data = json.loads(cfg.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[key] = value
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data))
        os.replace(tmp, cfg)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        return e
    return None


def _remember_library(cache_root: Path, library: Path) -> None:
    """Persist the chosen library so the next no-arg (double-click) launch
    reopens it. Best-effort: a write failure must never abort the launch."""
    err = _config_update(cache_root, "library", str(library))
    if err is not None:
        log.warning("could not remember library choice: %s", err)


# Per-folder sort modes (fauxcasa-q6l.11) — DURABLE-HOME DECISION
# (2026-07-02): a per-folder sort mode is a VIEW preference of a read-only
# app, so it lives MACHINE-LOCAL in a per-LIBRARY config.json inside that
# library's own state dir (library_state_dir() names the dir by a digest
# of the library identity, so the prefs follow the library without
# touching it, N1 — and, since fauxcasa-6vk finding 2, without following
# the WALK: a File-Types change must not lose the user's sort choices).
# N3 says durable state lives in the library, and Picasa's MANUAL sort
# order is on the rebuild-loss regression list — but
# v1's date/name/size modes are recomputable views, not user-authored
# order (manual mode IS user-authored, and is blocked on the missing db3
# oracle fixture — out of scope here), so losing this file costs one
# right-click, not data. REVISIT AT M2: when tier-2 library-home state
# lands (the albums order file), sort modes may move there so a library
# carries its view prefs between machines.
def _library_config_path(state_dir: Path) -> Path:
    """Machine-local per-library view prefs, in the library STATE dir
    (variant-free — fauxcasa-6vk finding 2; for a default walk that is
    the same directory catalog.json lives in). The name
    'config.json' is collision-free there (catalog.json,
    thumbs.fcache, import-report.json) and mirrors the per-user config.json
    at the cache ROOT (_config_path) in shape and fail-soft handling."""
    return state_dir / "config.json"


def _read_library_config(state_dir: Path) -> dict:
    """Read the raw per-library config dict, or {} on any failure.
    View prefs are a convenience, never a gate."""
    try:
        data = json.loads(_library_config_path(state_dir).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_library_config(state_dir: Path, data: dict) -> None:
    """Persist the per-library config atomically via temp-sibling + os.replace.
    Best-effort: a write failure never breaks the session."""
    cfg = _library_config_path(state_dir)
    tmp = cfg.with_name(f"{cfg.name}.{os.getpid()}.tmp")
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data))
        os.replace(tmp, cfg)
    except OSError as e:
        log.warning("could not persist library config: %s", e)
        try:
            tmp.unlink()
        except OSError:
            pass


def load_sort_modes(state_dir: Path | None) -> dict[str, str]:
    """The persisted per-folder sort modes (folder rel-path -> mode), or {}.
    Tolerates a missing/garbage file, a non-object document, and unknown
    mode values (dropped) — view prefs are a convenience, never a gate.
    Default-mode entries are dropped too: absent == DEFAULT_SORT_MODE."""
    if state_dir is None:
        return {}
    modes = _read_library_config(state_dir).get("sort_modes")
    if not isinstance(modes, dict):
        return {}
    return {rel: mode for rel, mode in modes.items()
            if isinstance(rel, str) and mode in SORT_MODES
            and mode != DEFAULT_SORT_MODE}


def save_sort_modes(state_dir: Path | None, modes: dict[str, str]) -> None:
    """Persist the non-default per-folder sort modes. Best-effort and
    torn-proof via temp-sibling + os.replace. Merges into the existing config
    doc so other view prefs (folder_view_flat) survive the write."""
    if state_dir is None:
        return  # no state dir (tests, degraded runs): session-only modes
    keep = {rel: mode for rel, mode in sorted(modes.items())
            if mode in SORT_MODES and mode != DEFAULT_SORT_MODE}
    doc = _read_library_config(state_dir)
    doc["sort_modes"] = keep
    _write_library_config(state_dir, doc)


def load_star_min(state_dir: Path | None) -> int:
    """The persisted star-threshold predicate (fauxcasa-q6l.20 clause a):
    0 ("Any", off) through 5. Tolerates missing/garbage config and an
    out-of-range value — view prefs are a convenience, never a gate.
    Absent/invalid == 0."""
    if state_dir is None:
        return 0
    v = _read_library_config(state_dir).get("star_min")
    return v if isinstance(v, int) and not isinstance(v, bool) \
        and 0 <= v <= 5 else 0


def save_star_min(state_dir: Path | None, star_min: int) -> None:
    """Persist the star-threshold predicate. Best-effort and torn-proof.
    Merges into the existing config so sort_modes/folder_view_flat
    survive the write. The default (0/Any) is stored as absent, not 0."""
    if state_dir is None:
        return
    doc = _read_library_config(state_dir)
    if 0 < star_min <= 5:
        doc["star_min"] = star_min
    else:
        doc.pop("star_min", None)
    _write_library_config(state_dir, doc)


def load_folder_view(state_dir: Path | None) -> bool:
    """Whether the folder sidebar should use flat (True) or tree (False) mode.
    Default False (tree). Tolerates missing/garbage config — view prefs are
    a convenience, never a gate. Absent key == tree (False)."""
    if state_dir is None:
        return False
    v = _read_library_config(state_dir).get("folder_view_flat")
    return bool(v) if isinstance(v, bool) else False


def save_folder_view(state_dir: Path | None, flat: bool) -> None:
    """Persist the flat/tree folder sidebar choice. Best-effort and
    torn-proof. Merges into the existing config so sort_modes survives.
    The default (tree/False) is stored as absent, not False."""
    if state_dir is None:
        return
    doc = _read_library_config(state_dir)
    if flat:
        doc["folder_view_flat"] = True
    else:
        doc.pop("folder_view_flat", None)
    _write_library_config(state_dir, doc)


def _default_window_size() -> tuple[int, int]:
    """min(1280, 0.9 * available width) x min(800, 0.9 * available height)
    (fauxcasa-ez2.6 §6): the v1 baseline 1280x800 fit fine on a normal
    monitor but spilled off-screen on a small laptop display — this scales
    down to the actual screen instead. Falls back to the bare 1280x800
    baseline when no primary screen is reported (rare; some CI/offscreen
    setups)."""
    screen = QApplication.primaryScreen()
    if screen is None:
        return 1280, 800
    avail = screen.availableGeometry()
    return (min(1280, round(avail.width() * 0.9)),
            min(800, round(avail.height() * 0.9)))


def load_window_geometry(state_dir: Path | None) -> QByteArray | None:
    """The persisted QWidget.saveGeometry() blob (base64 in config.json),
    decoded back to a QByteArray, or None if absent/garbage. View prefs
    are a convenience, never a gate — QByteArray.fromBase64 never raises
    (invalid input just decodes to something restoreGeometry() rejects),
    but an empty/near-empty result is treated as absent so the caller
    falls straight to the freshly computed default size."""
    if state_dir is None:
        return None
    b64 = _read_library_config(state_dir).get("window_geometry")
    if not isinstance(b64, str) or not b64:
        return None
    data = QByteArray.fromBase64(b64.encode("ascii"))
    return data if data.size() > 0 else None


def save_window_geometry(state_dir: Path | None, geometry: QByteArray) -> None:
    """Persist QWidget.saveGeometry() as base64, merged into the existing
    config doc so sort_modes/folder_view_flat survive the write."""
    if state_dir is None:
        return
    doc = _read_library_config(state_dir)
    doc["window_geometry"] = bytes(geometry.toBase64()).decode("ascii")
    _write_library_config(state_dir, doc)


def library_state_dir(library_key: str, cache_root: Path) -> Path:
    """The VARIANT-FREE per-library directory that holds user CHOICES —
    stars.json and the view-prefs config.json (fauxcasa-6vk finding 2).

    `cache_dir_for(library_key, cache_root, variant)` keys the disposable
    cache on the WALK (scan filter + excluded extensions) so a different
    walk gets a different thumbs/catalog pair. That is right for derived
    data and wrong for user choices: changing File Types or
    --min-image-size would otherwise hide every star the user has set.
    Passing no variant returns the same directory the default walk uses,
    so a plain single-variant library keeps one directory for both.
    """
    return cache_dir_for(library_key, cache_root)


def _migrate_library_state(cache_dir: Path | None,
                           state_dir: Path | None) -> None:
    """Adopt user state left in a VARIANT cache dir into the variant-free
    state dir (fauxcasa-6vk finding 2), so testers who starred photos
    while running with a File-Types/scan-size variant keep those stars.

    A MERGE, not a one-shot copy (Codex cross-vendor review): a tester who
    starred photos on the default walk AND more photos under a variant has
    state in BOTH places, so "skip if the base file already exists" would
    silently drop every variant-only choice. Every key the base does not
    have is adopted; every key it does have WINS, because the base dir is
    where this build has been writing. Per-photo for stars and per-folder
    for sort modes — the granularity the user actually chose at.

    Idempotent (a second launch finds nothing left to adopt and writes
    nothing), copy-never-move (the variant file stays where an older build
    would still find it), and best-effort: state is a convenience."""
    if state_dir is None or cache_dir is None or state_dir == cache_dir:
        return
    try:
        variant_stars = load_star_overrides(cache_dir)
        if variant_stars:
            stars = load_star_overrides(state_dir)
            adopted = {k: v for k, v in variant_stars.items()
                       if k not in stars}
            if adopted:
                stars.update(adopted)
                save_star_overrides(state_dir, stars)
                log.info("adopted %d star choice(s) from %s",
                         len(adopted), cache_dir)
        variant_cfg = _read_library_config(cache_dir)
        if variant_cfg:
            doc = _read_library_config(state_dir)
            merged = dict(doc)
            changed = False
            for key, value in variant_cfg.items():
                if key not in merged:
                    merged[key] = value           # e.g. folder_view_flat
                    changed = True
                elif (key == "sort_modes" and isinstance(value, dict)
                        and isinstance(merged[key], dict)):
                    # Per-FOLDER merge: the sort-mode analogue of the
                    # per-photo star merge above — a folder sorted only
                    # under the variant must not be lost just because some
                    # OTHER folder was sorted on the base walk.
                    extra = {rel: mode for rel, mode in value.items()
                             if rel not in merged[key]}
                    if extra:
                        merged[key] = {**merged[key], **extra}
                        changed = True
            if changed:
                _write_library_config(state_dir, merged)
                log.info("adopted library view prefs from %s", cache_dir)
    except OSError as e:
        log.warning("could not migrate library state: %s", e)


# ---------------------------------------------------------------------------
# Half B: in-library tier-2 state
# ---------------------------------------------------------------------------
#
# The sidecar-first write layer (fauxcasa-lgg.1 design, the fauxcasa-lgg.3
# journal, the fauxcasa-lgg.5 stars.json migration) writes state INSIDE the
# library itself rather than machine-local. Today that tier holds only
# stars.json, via starstore.py — STAR_OVERRIDES_NAME, load_star_overrides,
# save_star_overrides, apply_star_overrides, and photo_key are imported
# above and re-exported from here so callers have one import site for
# "everything Fauxcasa persists," without needing to know which half of
# this module a given name actually lives in.
