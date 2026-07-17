#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6", "rawpy", "exiv2", "pillow", "av"]
# ///
"""Tracer bullet app (fauxcasa-pzx): a thin but real end-to-end slice of
the product on the proposed Python + Qt stack.

    uv run apps/desktop-python/main.py [LIBRARY] [options]

Layers wired end to end: library scan in place -> machine-local catalog
+ packed thumbnail cache -> folder tree + albums sidebar -> virtualized
grid with group headers, stars, search -> async full-image viewer.
Read-only: the only on-disk output is the app's own disposable cache,
never anything inside the library (N1/N3).

Default library is the synthetic fixture library; for the 100k scale
test, adopt the pre-built benchmark cache:

    uv run apps/desktop-python/main.py cache/benchmark-library \
        --thumbs cache/benchmark-thumbs.fcache

Headless verification (agents/CI):

    QT_QPA_PLATFORM=offscreen uv run apps/desktop-python/main.py \
        --screenshot /tmp/tracer.png [--scroll-to 0.5] [--quit-after-ready]

Scripted quits abandon any in-flight thumbnail-cache build (cleanly —
nothing partial is left behind) unless --finish-build holds the quit
until the build lands (raise --timeout for bigger libraries). For
deterministic warm runs at scale, pre-build with
scripts/make-thumbcache.py and adopt via --thumbs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

T0 = time.perf_counter()

from PySide6.QtCore import QObject, QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QActionGroup, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QSlider,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from catalog import (  # noqa: E402
    BACKFILL_COMPLETE,
    BACKFILL_NOT_STARTED,
    REPORT_NAME,
    Catalog,
    Photo,
    ScanFilter,
    default_contacts_xml,
    default_pal_dir,
    format_date_taken,
    format_geotag,
    load_catalog,
    load_contacts_xml,
    load_report,
    reconcile_walk,
    save_catalog_retrying,
    save_report,
    scan_library,
)
from db3rescue import default_db3_dir  # noqa: E402
from filetypes import (  # noqa: E402
    FileTypesDialog,
    effective_exts,
    exts_cache_key,
    load_excluded_exts,
    save_excluded_exts,
)
from grid import DEFAULT_SORT_MODE, SORT_MODES, GridView  # noqa: E402
import keymap  # noqa: E402
import library  # noqa: E402
from thumbcache import (  # noqa: E402
    CacheError,
    ThumbCache,
    backfill_catalog,
    bind,
    build_cache,
    cache_dir_for,
    load_cache,
)
from peek import PeekPage  # noqa: E402
from slideshow import SlideshowPage  # noqa: E402
from tray import SelectionTray  # noqa: E402
from viewer import ViewerPage  # noqa: E402

import applog  # noqa: E402

# Human diagnostics route through this logger -> a rotating log file (so
# nothing is lost in a console=False windowed build) + a stderr mirror when a
# console exists (fauxcasa-pqw). The §7 MACHINE protocol — READY + the
# ready/exit/indexed JSON — stays on raw stdout below, never through here.
log = applog.log

# Single source of truth for the (provisional) product name — nothing
# else may hard-code it.
APP_NAME = "Fauxcasa"

# "Recently Updated" auto-collection (fauxcasa-q6l.7). DECISION (2026-07-02):
# in a read-only app "updated" means FILE MTIME — the honest proxy until the
# app itself writes anything (Picasa's collection tracked its own recent
# edits; we have none yet). Window: mtime within the last RECENT_DAYS days
# (Picasa kept the collection to recent edits; 30 days is the pick). If the
# window is empty — a library untouched for months — fall back to the
# RECENT_FALLBACK_K most recently modified photos so the collection is never
# uselessly empty. Photo.mtime is filled by the indexer and persisted;
# unindexed photos (mtime < 0) never qualify, so those runs honestly show a
# count of 0 — for an adopt-mode (--thumbs) catalog only until the
# background backfill (fauxcasa-cam.12) fills real mtimes in, at which
# point the collection populates and the count refreshes.
RECENT_DAYS = 30
RECENT_FALLBACK_K = 100


def recent_indices(catalog: Catalog, reveal: bool,
                   now: float | None = None) -> list[int]:
    """Catalog indices of the Recently Updated auto-collection: photos
    (visible, or all under reveal) whose file mtime falls within the last
    RECENT_DAYS days — else the RECENT_FALLBACK_K most recently modified
    ones. Returned in catalog order (the grid regroups by folder anyway);
    `now` is injectable for tests."""
    if now is None:
        now = time.time()
    cutoff = now - RECENT_DAYS * 86400
    known = [(i, p.mtime) for i, p in enumerate(catalog.photos)
             if (p.visible or reveal) and p.mtime >= 0]
    idxs = [i for i, m in known if m >= cutoff]
    if idxs:
        return idxs
    known.sort(key=lambda im: im[1], reverse=True)
    return sorted(i for i, _m in known[:RECENT_FALLBACK_K])

APP_DIR = Path(__file__).resolve().parent
REPO = APP_DIR.parents[1]
FROZEN = getattr(sys, "frozen", False)


def _default_cache_root() -> Path:
    """REPO-relative in a source checkout; a per-user writable dir when
    frozen — REPO then points inside the read-only PyInstaller bundle, so
    the app's own disposable cache must go somewhere writable instead."""
    if FROZEN:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        return Path(base) / "fauxcasa-tracer"
    return REPO / "cache" / "tracer-cache"


def _default_library() -> Path | None:
    """The built-in library to open with no argument. A source checkout
    ships the synthetic fixture library; a frozen bundle ships none (REPO
    points inside the read-only bundle), so there is no default — the app
    recalls the last-opened library or prompts on first run instead."""
    if FROZEN:
        return None
    return REPO / "cache" / "synthetic-library"


def _config_path(cache_root: Path) -> Path:
    """Per-user config, beside (never inside) the per-library cache dirs —
    cache_dir_for() names those by a 16-hex digest, so 'config.json' is
    collision-free."""
    return cache_root / "config.json"


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


def _remember_library(cache_root: Path, library: Path) -> None:
    """Persist the chosen library so the next no-arg (double-click) launch
    reopens it. Best-effort: a write failure must never abort the launch.
    Writes via a per-process temp sibling + os.replace so a second frozen
    instance launching concurrently can never read a half-written (torn)
    config — it sees either the old file or the whole new one."""
    cfg = _config_path(cache_root)
    tmp = cfg.with_name(f"{cfg.name}.{os.getpid()}.tmp")
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"library": str(library)}))
        os.replace(tmp, cfg)
    except OSError as e:
        log.warning("could not remember library choice: %s", e)
        try:
            tmp.unlink()
        except OSError:
            pass


# Per-folder sort modes (fauxcasa-q6l.11) — DURABLE-HOME DECISION
# (2026-07-02): a per-folder sort mode is a VIEW preference of a read-only
# app, so it lives MACHINE-LOCAL in a per-LIBRARY config.json inside that
# library's own cache dir (beside catalog.json — cache_dir_for() names the
# dir by a digest of the library path, so the prefs follow the library
# without touching it, N1). N3 says durable state lives in the library, and
# Picasa's MANUAL sort order is on the rebuild-loss regression list — but
# v1's date/name/size modes are recomputable views, not user-authored
# order (manual mode IS user-authored, and is blocked on the missing db3
# oracle fixture — out of scope here), so losing this file costs one
# right-click, not data. REVISIT AT M2: when tier-2 library-home state
# lands (the albums order file), sort modes may move there so a library
# carries its view prefs between machines.
def _library_config_path(cache_dir: Path) -> Path:
    """Machine-local per-library view prefs, beside catalog.json. The name
    'config.json' is collision-free in the cache dir (catalog.json,
    thumbs.fcache, import-report.json) and mirrors the per-user config.json
    at the cache ROOT (_config_path) in shape and fail-soft handling."""
    return cache_dir / "config.json"


def load_sort_modes(cache_dir: Path | None) -> dict[str, str]:
    """The persisted per-folder sort modes (folder rel-path -> mode), or {}.
    Tolerates a missing/garbage file, a non-object document, and unknown
    mode values (dropped) — view prefs are a convenience, never a gate.
    Default-mode entries are dropped too: absent == DEFAULT_SORT_MODE."""
    if cache_dir is None:
        return {}
    try:
        data = json.loads(_library_config_path(cache_dir).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    modes = data.get("sort_modes")
    if not isinstance(modes, dict):
        return {}
    return {rel: mode for rel, mode in modes.items()
            if isinstance(rel, str) and mode in SORT_MODES
            and mode != DEFAULT_SORT_MODE}


def save_sort_modes(cache_dir: Path | None, modes: dict[str, str]) -> None:
    """Persist the non-default per-folder sort modes. Best-effort (a write
    failure never breaks the session — the in-memory modes still apply) and
    torn-proof via the same temp-sibling + os.replace dance as
    _remember_library. Rewrites the whole document: sort_modes is its only
    key today; a future second view pref must merge, not clobber."""
    if cache_dir is None:
        return  # no cache dir (tests, degraded runs): session-only modes
    keep = {rel: mode for rel, mode in sorted(modes.items())
            if mode in SORT_MODES and mode != DEFAULT_SORT_MODE}
    cfg = _library_config_path(cache_dir)
    tmp = cfg.with_name(f"{cfg.name}.{os.getpid()}.tmp")
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"sort_modes": keep}))
        os.replace(tmp, cfg)
    except OSError as e:
        log.warning("could not persist sort modes: %s", e)
        try:
            tmp.unlink()
        except OSError:
            pass


def _gui_unavailable() -> bool:
    """Whether a real windowing GUI is reachable — decided from the
    ENVIRONMENT ALONE, before any QApplication is constructed. This matters
    because on Linux the default 'xcb' plugin, given no DISPLAY/WAYLAND, makes
    QApplication([]) call qFatal()/abort() (exit 134) — it dies before any
    post-construction platformName() guard can run. A headless Qt platform
    (offscreen/minimal/vnc), or a Linux session with neither DISPLAY nor
    WAYLAND_DISPLAY, means nobody can answer a modal picker, so the caller
    should bail to the friendly no-library path instead of crashing."""
    plat = os.environ.get("QT_QPA_PLATFORM", "").split(":", 1)[0].strip()
    if plat in ("offscreen", "minimal", "vnc"):
        return True
    if sys.platform.startswith("linux"):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return True
    return False


def _prompt_for_library(cache_root: Path) -> Path | None:
    """First-run picker for a frozen build with no library yet: ask the
    user to choose a photo-library folder and remember it. Returns None if
    cancelled — or, under a headless/offscreen platform, immediately,
    rather than blocking forever on a modal dialog nobody can answer."""
    # Pre-construction guard: bail BEFORE touching QApplication so a Linux
    # no-DISPLAY launch can't abort the whole process (see _gui_unavailable).
    if _gui_unavailable():
        return None

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    app.setApplicationName(APP_NAME)
    # Backstop: an in-process headless platform (e.g. forced offscreen with a
    # DISPLAY present) still can't show a modal — keep this post-construction
    # guard too.
    if app.platformName() in ("offscreen", "minimal", ""):
        return None
    library = _choose_library_from_dialog(cache_root)
    if library is not None:
        _remember_library(cache_root, library)
    return library


def _choose_library_from_dialog(cache_root: Path, parent=None,
                                start: Path | None = None) -> Path | None:
    """Prompt for a library folder and validate choices that would be harmful.

    `cache_root` is accepted so callers share one signature around the
    library-choice helpers; the dialog itself does not write config.
    """
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    start_dir = str(start) if start is not None and start.is_dir() else ""
    while True:
        chosen = QFileDialog.getExistingDirectory(
            parent, f"{APP_NAME} — choose your photo library folder", start_dir)
        if not chosen:
            return None
        library = Path(chosen).expanduser().resolve()
        if _is_filesystem_root(library):
            QMessageBox.warning(
                parent,
                APP_NAME,
                "Choose a photo-library folder inside this filesystem, "
                "not the filesystem root.",
            )
            continue
        return library


def _restart_command(
    library: Path,
    cache_root: Path,
    scan_filter: "ScanFilter | None" = None,
    thumbs: "Path | None" = None,
) -> tuple[str, list[str]]:
    """Command used by the in-app Open and File Types actions to relaunch.

    Relaunching keeps startup's warm/cold/adopt logic single-sourced in main().
    Pass-through flags reproduce the original scan constraints on relaunch:
    --min-image-size / --max-image-size are always forwarded when set;
    --thumbs is forwarded only when the caller explicitly passes it — an adopted
    cache binds to a specific walk; callers must only pass it when the relaunch
    walks the same files.
    """
    extra: list[str] = []
    if scan_filter is not None:
        if scan_filter.min_width and scan_filter.min_height:
            extra += ["--min-image-size",
                      f"{scan_filter.min_width}x{scan_filter.min_height}"]
        if scan_filter.max_width and scan_filter.max_height:
            extra += ["--max-image-size",
                      f"{scan_filter.max_width}x{scan_filter.max_height}"]
    if thumbs is not None:
        extra += ["--thumbs", str(thumbs)]
    if FROZEN:
        return sys.executable, [str(library), "--cache-root", str(cache_root)] + extra
    return sys.executable, [
        str(APP_DIR / "main.py"), str(library), "--cache-root", str(cache_root)
    ] + extra


def _explain_not_a_library(root: Path) -> None:
    """Say WHY a path can't be opened as a library on stderr: a path that
    exists but is a regular file (or other non-dir) gets a clearer message
    than one that is simply missing — 'library not found' is misleading when
    the user pointed at a file."""
    if root.exists():
        log.error("not a folder — a library must be a directory: %s", root)
    else:
        log.error("library not found: %s", root)


def _explain_filesystem_root(root: Path) -> None:
    log.error("refusing to scan filesystem root as a photo library: %s; "
              "choose a folder inside it", root)


def _resolve_library(arg: str | None, cache_root: Path) -> Path | None:
    """Choose and validate the library to browse. Order: an explicit
    argument, else the built-in default (a checkout's synthetic library),
    else — for a frozen bundle with neither — the library remembered from a
    previous run, else a first-run folder picker. A frozen launch also
    remembers an explicit argument so the next double-click reopens it.
    Returns None (after explaining why on stderr) when nothing is usable."""
    if arg is not None:
        root = Path(arg).expanduser().resolve()
        if not root.is_dir():
            _explain_not_a_library(root)
            return None
        if _is_filesystem_root(root):
            _explain_filesystem_root(root)
            return None
        if FROZEN:
            _remember_library(cache_root, root)
        return root

    default = _default_library()
    if default is not None:                       # source checkout
        root = default.expanduser().resolve()
        if not root.is_dir():
            _explain_not_a_library(root)
            return None
        if _is_filesystem_root(root):
            _explain_filesystem_root(root)
            return None
        return root

    # Frozen bundle, no library given: recall the last choice or prompt.
    root = _remembered_library(cache_root)
    if root is not None:
        return root
    root = _prompt_for_library(cache_root)
    if root is None:
        log.error("no library selected — pass a library folder, or pick one "
                  "when prompted")
        return None
    return root


# ---------------------------------------------------------------------------
# Multi-root management CLI actions (fauxcasa-ed5.7.4, bead .d, design §10)
# ---------------------------------------------------------------------------
#
# --promote / --add-root / --import-picasa-watched are management
# operations, not the normal open path: each validates its arguments,
# performs the on-disk change via library.py, prints a one-line summary,
# and returns an exit code WITHOUT proceeding to the normal warm/cold-walk
# open below — opening an explicit multi-root library through the grid/tree
# is bead .g's scope (the "tree per root" UI); today's open flow only knows
# how to browse a single Path. Re-running the app afterwards (implicit
# legacy open for an un-promoted root, or a future explicit-open path once
# .g lands) picks up the change.

def _cmd_promote(library_arg: str | None, cache_root: Path) -> int:
    """--promote: promote the legacy library named by the positional
    `library` argument in place (design §10). Reuses _resolve_library for
    the same existence/filesystem-root validation every other invocation
    gets, so a bad path fails exactly the same way it would on a normal
    open."""
    root = _resolve_library(library_arg, cache_root)
    if root is None:
        return 2
    try:
        cfg = library.promote_library(root, cache_root=cache_root)
    except Exception as e:
        log.error("promotion failed (rolled back, %s is still a plain "
                  "legacy library): %s", root, e)
        return 2
    print(f"promoted: library home at {cfg.home}")
    return 0


def _cmd_add_root(library_arg: str | None, cache_root: Path,
                  new_root: Path) -> int:
    """--add-root PATH: add PATH as a new watched root to the
    already-promoted library-home named by the positional `library`
    argument. Mint + save + fail-soft marker only — no indexing runs here;
    the new root's empty/mismatched fcache rides the existing bind()
    mismatch -> reindex path the next time this library is actually
    opened (design §10: "mint id, append to roots, save library.json, mint
    a new empty thumbs-<root_id>.fcache -> bind mismatch -> incremental
    reindex for the new root only")."""
    home = _resolve_library(library_arg, cache_root)
    if home is None:
        return 2
    cfg = library.resolve_open_path(home)
    if cfg.is_legacy:
        log.error("%s is not a library-home (no .fauxcasa/library.json) — "
                  "run --promote first", home)
        return 2
    try:
        added = library.add_root(cfg, new_root)
    except ValueError as e:
        log.error("cannot add root: %s", e)
        return 2
    library.write_root_marker(Path(new_root).resolve(), added.id)  # fail-soft
    library.save_library(cfg)
    print(f"added root {added.id} ({added.label}) to {home}")
    return 0


def _read_watched_list_file(path: Path) -> list[Path]:
    """One folder path per line; blank lines and '#'-prefixed comments are
    skipped. Raises OSError if `path` can't be read — the caller turns
    that into a friendly CLI error."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Path(s) for s in (line.strip() for line in lines)
            if s and not s.startswith("#")]


def _cmd_import_picasa_watched(library_arg: str | None,
                               listfile: str) -> int:
    """--import-picasa-watched LISTFILE: create a FRESH library-home at the
    positional `library` argument from a Picasa watched-folders list.
    `listfile` is a text file (one path per line, '#' comments), or the
    literal 'registry' to read Picasa's own HKCU watched-folders list
    (Windows only; library.picasa_watched_from_registry fails soft with a
    clear RuntimeError when absent/unsupported, caught here)."""
    if not library_arg:
        log.error("--import-picasa-watched requires a library-home path "
                  "as the positional argument")
        return 2
    home = Path(library_arg).expanduser().resolve()

    if listfile == "registry":
        try:
            folders = library.picasa_watched_from_registry()
        except RuntimeError as e:
            log.error(str(e))
            return 2
    else:
        try:
            folders = _read_watched_list_file(Path(listfile))
        except OSError as e:
            log.error("cannot read %s: %s", listfile, e)
            return 2

    skipped: list[str] = []
    cfg = library.import_picasa_watched(folders, home, name=home.name,
                                        skipped=skipped)
    for msg in skipped:
        log.warning("picasa import: skipped %s", msg)
    if not cfg.roots:
        log.error("no usable watched folders found — nothing imported")
        return 2
    library.save_library(cfg)
    for r in cfg.roots:
        library.write_root_marker(r.path.resolve(), r.id)  # fail-soft
    print(f"imported {len(cfg.roots)} root(s) into {cfg.home}")
    return 0


def _parse_image_size_arg(value: str) -> tuple[int, int]:
    raw = value.strip().lower()
    sep = "x" if "x" in raw else ","
    parts = raw.split(sep, 1)
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT")
    try:
        width, height = (int(parts[0]), int(parts[1]))
    except ValueError as e:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT") from e
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("WIDTH and HEIGHT must be positive")
    return width, height


def read_rss_mb() -> tuple[float, float]:
    """(rss_mb, hwm_mb) for the current process.

    Linux reads /proc/self/status (VmRSS/VmHWM) — the fast path. Windows uses
    GetProcessMemoryInfo (WorkingSetSize/PeakWorkingSetSize) via ctypes. macOS
    is not yet covered and returns zeros. Any probe failure returns (0.0, 0.0)
    rather than raising — callers use this for instrumentation only and it must
    never crash startup.
    """
    rss = hwm = 0.0
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = float(line.split()[1]) / 1024.0
                elif line.startswith("VmHWM:"):
                    hwm = float(line.split()[1]) / 1024.0
        if rss or hwm:
            return rss, hwm
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            # GetCurrentProcess returns the pseudo-handle (HANDLE)-1; without an
            # explicit HANDLE restype ctypes truncates it to 32 bits on 64-bit
            # Python and GetProcessMemoryInfo fails with ERROR_INVALID_HANDLE.
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            handle = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb):
                rss = counters.WorkingSetSize / 1024.0 / 1024.0
                hwm = counters.PeakWorkingSetSize / 1024.0 / 1024.0
        except Exception:
            pass
    # macOS (darwin) is not yet covered; returns zeros.
    return rss, hwm


def _emit(signal, *args) -> None:
    """Emit a bridge signal from a worker thread, swallowing the
    RuntimeError raised if the C++ bridge was already torn down at
    shutdown — a worker must never crash the interpreter on its way out."""
    try:
        signal.emit(*args)
    except RuntimeError:
        pass


@dataclass(frozen=True)
class SelectionContext:
    """What an output action would act on RIGHT NOW — the M2 attachment
    point. Spec §5 makes the tray/selection the universal input to every
    output action, with enablement keyed to selection type: future
    output actions (export, movie, print, email…) call
    MainWindow.selection_context() and key their enablement off `kind`
    instead of re-deriving UI state from widgets.

    kind:    "held" when the tray holds photos (the tray wins — Picasa's
             contract), else "photos" for a grid multi-selection, else
             the active view's own type ("folder", "album", "starred",
             "recent", "person", "unnamed", "search", "all").
    indices: catalog indices the action acts on, in the input's own
             order — HOLD order for "held" (tray.py's ordering
             decision), display order otherwise.
    held:    the held rel paths (hold order) regardless of kind, so an
             action can still offer a use-tray/use-view choice later.
    """
    kind: str
    indices: tuple[int, ...]
    held: tuple[str, ...]


class _BuildBridge(QObject):
    progress = Signal(int, int)            # done, total (cold build live feed)
    status = Signal(str)                   # inline status text (reconcile)
    finished = Signal(object, object, bool)  # (IndexResult|None, Catalog, is_reconcile)
    backfill_done = Signal(bool)           # adopt-mode backfill: completed?


class MainWindow(QMainWindow):
    def __init__(self, catalog: Catalog, thumbs: ThumbCache | None,
                 cache_dir: Path | None, build_dir: Path | None,
                 scan_filter: ScanFilter | None = None,
                 warm: bool = False, adopt: bool = False,
                 cache_root: Path | None = None,
                 contacts: dict[str, str] | None = None,
                 pal_dir: Path | None = None,
                 excluded_exts: set[str] | None = None,
                 thumbs_path: Path | None = None,
                 db3_dir: Path | None = None):
        super().__init__()
        self.catalog = catalog
        self.cache_dir = cache_dir
        self.scan_filter = scan_filter
        # --thumbs path preserved for any relaunch that walks the same file
        # set as this run; a File-Types change changes the walk and must not
        # forward it — that relaunch cold-rebuilds (see _show_file_types).
        self.thumbs_path = thumbs_path
        # File Types panel choice (fauxcasa-v46.4): the persisted excluded
        # set and the effective walk set derived from it, kept so every
        # background rescan/reconcile walks exactly what startup walked —
        # an excluded extension must never read as phantom drift.
        self.excluded_exts = set(excluded_exts or ())
        self.exts = effective_exts(self.excluded_exts)
        # machine-local contacts.xml names, kept so a reconcile rebuild's
        # rescan resolves faces the same way the startup scan did
        self.contacts = contacts or {}
        # Picasa2Albums .pal directory, kept for the same reason: a
        # reconcile rescan must merge albums the way the startup scan did
        self.pal_dir = pal_dir
        # machine-local db3 directory (fauxcasa-cam.6/.7), same reason: a
        # reconcile rescan must rescue person names the way startup did
        self.db3_dir = db3_dir
        self.cache_root = cache_root or (
            cache_dir.parent if cache_dir is not None else _default_cache_root()
        )
        self.adopt = adopt
        self.ready_reported = False
        self.build_failed = False
        self.last_index_rate = 0.0
        # §7 search-latency instrumentation (fauxcasa-ed5.4): _search_changed
        # records its own end-to-end latency + hit count here; the
        # --search-probe harness (run_search_probe) reads them per query.
        self.last_search_ms = 0.0
        self.last_search_hits = 0
        # §7 search index (fauxcasa-ed5.4): (catalog index, lowercase
        # haystack) pairs parallel to catalog.photos, owned by the WINDOW —
        # deliberately not Photo fields — and rebuilt at every
        # filter-relevant mutation point (see _rebuild_search_index).
        self._search_pairs: list[tuple[int, str]] = []
        self._search_pairs_vis: list[tuple[int, str]] = []
        self._rebuild_search_index()
        self.setWindowTitle(f"{APP_NAME} tracer — {catalog.root.name}")
        self.resize(1280, 800)

        self.grid = GridView()
        # Per-folder sort modes (fauxcasa-q6l.11), loaded BEFORE the first
        # set_data/set_filter builds the display so a remembered mode shapes
        # the very first paint. cache_dir=None (tests, degraded runs) means
        # session-only modes; durable-home decision at _library_config_path.
        self.grid.sort_modes = load_sort_modes(cache_dir)
        # The viewer shares the grid's cache pair: it paints an instant cached
        # preview (the nearest v2 level) while the full original loads
        # (fauxcasa-9pp). thumbs is None on a cold start until the build lands.
        self.viewer = ViewerPage(catalog, thumbs)
        # Full-screen slideshow surface (fauxcasa-q6l.3): created lazily on
        # first Play, then REUSED (re-pointed at the current catalog/cache
        # each start) — never deleted mid-run, so its decode threads can
        # never race a widget teardown (fauxcasa-gfz).
        self._slideshow: SlideshowPage | None = None
        # Ctrl+Alt hover peek surface (fauxcasa-q6l.5): same lifecycle
        # discipline — lazily created on the first trigger, then reused and
        # re-pointed, never deleted mid-run.
        self._peek_page: PeekPage | None = None

        # --- sidebar: All / Starred / Folders / Albums ---
        self.tree = self._new_sidebar_tree()
        self._build_sidebar()

        # --- menu bar: Tools (fauxcasa-v46.4) ---
        # The File Types panel is the first menu item the tracer grows; a
        # menu bar (not another toolbar button) because configuration
        # belongs off the browsing surface — Picasa's own Tools > Options
        # > File Types location, minus the Options tabs we don't have.
        tools_menu = self.menuBar().addMenu("&Tools")
        self.file_types_action = tools_menu.addAction("File &Types…")
        self.file_types_action.setStatusTip(
            "Choose which file types are scanned into this library")
        self.file_types_action.triggered.connect(self._show_file_types)

        # --- toolbar: search + zoom ---
        bar = QToolBar()
        bar.setMovable(False)
        self.addToolBar(bar)
        self.open_action = bar.addAction("Open...")
        self.open_action.setToolTip("Choose a different photo library folder")
        self.open_action.triggered.connect(self._change_library)
        bar.addSeparator()
        # Picasa's green Play (folder/album headers + toolbar) distilled to
        # one toolbar affordance acting on the CURRENT view; F11 is the
        # Picasa-heritage shortcut (fauxcasa-q6l.3).
        self.play_action = bar.addAction("▶ Play")
        # Chord text is derived from the keymap so the tooltip stays current
        # when chords are added or changed (ed5.12); never hard-code "F11".
        _play_chords = " / ".join(s.toString()
                                  for s in keymap.shortcuts("app.play"))
        self.play_action.setToolTip(
            f"Slideshow of the current view ({_play_chords})"
            " — Space pauses, Esc exits")
        # The binding comes from the keymap default scheme (q6l.8).
        self.play_action.setShortcuts(keymap.shortcuts("app.play"))
        self.play_action.triggered.connect(self._start_slideshow)
        bar.addSeparator()
        self.search = QLineEdit()
        self.search.setPlaceholderText(
            "search filename, caption, keywords, people, folder…"
            "  -term excludes")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(360)
        self.search.textChanged.connect(self._search_changed)
        bar.addWidget(self.search)
        bar.addWidget(QLabel("  zoom "))
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(64, 256)
        self.zoom.setValue(160)
        self.zoom.setMaximumWidth(160)
        # Debounce slider drags: each integer step would otherwise clear
        # and re-decode the whole tile cache (~190 times per full drag).
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.setInterval(150)
        self._zoom_timer.timeout.connect(
            lambda: self.grid.set_zoom(self.zoom.value()))
        self.zoom.valueChanged.connect(
            lambda _v: self._zoom_timer.start())
        bar.addWidget(self.zoom)
        self.reveal_box = QCheckBox("  Show hidden")
        self.reveal_box.setToolTip(
            "Reveal hidden=yes photos, stash-folder files, and folders in the "
            "Hidden Folders category (shown veiled)")
        self.reveal_box.toggled.connect(self._toggle_reveal)
        bar.addWidget(self.reveal_box)

        # --- pages ---
        browser = QWidget()
        lay = QVBoxLayout(browser)
        lay.setContentsMargins(0, 0, 0, 0)
        split = QSplitter()
        split.addWidget(self.tree)
        split.addWidget(self.grid)
        split.setSizes([240, 1040])
        split.setCollapsible(1, False)
        lay.addWidget(split)
        # Selection tray (fauxcasa-q6l.2): docked at the bottom of the
        # browser page, Picasa's location. Held identity/order decisions
        # live in tray.py; the typed readout text is computed here
        # (_tray_readout_text) because only MainWindow knows the sidebar
        # view type the spec's phrasing keys on.
        self.tray = SelectionTray(catalog, thumbs)
        lay.addWidget(self.tray)
        self.pages = QStackedWidget()
        self.pages.addWidget(browser)
        self.pages.addWidget(self.viewer)
        self.setCentralWidget(self.pages)

        # --- status bar ---
        self.setStatusBar(QStatusBar())
        self.counts_label = QLabel()
        self.progress_label = QLabel()
        self.meta_label = QLabel()
        # Import-report count (fauxcasa-cam.13): "N import notes" with the
        # first few entries in the tooltip — deliberately lean; the full
        # inspector surface is N7/M2 work.
        self.notes_label = QLabel()
        self.statusBar().addWidget(self.counts_label)
        self.statusBar().addWidget(self.progress_label)
        self.statusBar().addPermanentWidget(self.notes_label)
        self.statusBar().addPermanentWidget(self.meta_label)
        self._show_counts("All photos", self._shown_count())
        self._update_import_notes()

        # --- wiring ---
        # The grid's set-valued signal drives the status label (single
        # metadata vs "N photos selected"); the viewer stays single-photo
        # and keeps feeding _photo_selected directly (fauxcasa-q6l.1).
        self.grid.selection_changed.connect(self._selection_changed)
        self.grid.photo_activated.connect(self._open_viewer)
        self.grid.peek_requested.connect(self._show_peek)
        self.grid.peek_released.connect(self._hide_peek)
        self.viewer.closed.connect(self._close_viewer)
        self.viewer.photo_shown.connect(self._photo_selected)
        # Selection-tray wiring (fauxcasa-q6l.2). The readout also listens
        # to selection_changed and the search box directly — SEPARATE
        # connections, so the status-bar dual mode (_selection_changed)
        # and _search_changed stay untouched.
        self.grid.hold_requested.connect(self._hold_selection)
        self.viewer.hold_requested.connect(self._hold_from_viewer)
        self.tray.hold_clicked.connect(self._hold_selection)
        self.tray.navigate.connect(self._tray_navigate)
        self.tray.changed.connect(self._refresh_tray_readout)
        self.grid.selection_changed.connect(
            lambda _sel: self._refresh_tray_readout())
        self.search.textChanged.connect(
            lambda _t: self._refresh_tray_readout())

        self.grid.set_data(catalog, thumbs)
        self._refresh_tray_readout()

        # --- background index plumbing (modes, not modals) ---
        # Either a COLD build (no cache yet — feeds tiles live so the
        # library is browsable immediately) or, on a WARM start, a
        # background RECONCILE that confirms the persisted catalog still
        # matches disk and rebuilds if it drifted.
        self.build_cancel = threading.Event()
        self._build_thread: threading.Thread | None = None
        self._reconcile_thread: threading.Thread | None = None
        self._backfill_thread: threading.Thread | None = None
        # Parks the backfill's readers between photos while set — the
        # low-priority hook (a future consumer can pause on heavy scroll);
        # tests drive it directly.
        self.backfill_pause = threading.Event()
        self._reconcile_after_backfill = False
        self._bridge = _BuildBridge()
        self._bridge.progress.connect(self._build_progress)
        self._bridge.status.connect(self._on_status)
        self._bridge.finished.connect(self._on_index_finished)
        self._bridge.backfill_done.connect(self._on_backfill_done)

        if build_dir is not None:
            self._start_cold_build(build_dir)
        elif cache_dir is not None \
                and catalog.backfill_state != BACKFILL_COMPLETE:
            # Adopt-mode backfill (fauxcasa-cam.12): the catalog was bound
            # to a prebuilt cache without the indexer ever reading a file,
            # so fill signals + in-file metadata in the background —
            # resuming from the persisted cursor on a relaunch. A warm
            # start's reconcile is DEFERRED until the backfill lands: both
            # walk the same files, and reconcile diffing signals the
            # backfill is mid-writing would report phantom drift.
            self._reconcile_after_backfill = warm
            self._start_backfill()
        elif warm and cache_dir is not None:
            self._start_reconcile()

    # ---------- background index jobs ----------

    def _start_cold_build(self, build_dir: Path) -> None:
        bridge, catalog = self._bridge, self.catalog
        done = [0]  # results arrive out of order; count completions, not idx

        def cb(i: int, total: int, img) -> None:
            if img is not None:
                self.grid.feed_tile(i, img)
            else:
                self.grid.feed_error(i)
            done[0] += 1
            if done[0] % 5 == 0 or done[0] == total:
                _emit(bridge.progress, done[0], total)

        def work() -> None:
            try:
                result = build_cache(catalog, build_dir, cb,
                                     cancel=self.build_cancel)
                if result is None:
                    return  # cancelled
                save_catalog_retrying(catalog, build_dir / "catalog.json")
                save_report(catalog.report, build_dir / REPORT_NAME)
                _emit(bridge.finished, result, catalog, False)
            except Exception as e:  # report, never crash the UI
                log.error("cache build failed: %s", e)
                _emit(bridge.finished, None, catalog, False)

        self._build_thread = threading.Thread(target=work, daemon=True)
        self._build_thread.start()

    def _start_reconcile(self) -> None:
        """Warm start: confirm the persisted catalog still matches disk.
        No live feed — the grid shows the loaded catalog, and a fresh
        index uses new indices that would misalign with it, so any
        rebuilt catalog is swapped in atomically when complete."""
        bridge, old, cache_dir = self._bridge, self.catalog, self.cache_dir

        def work() -> None:
            try:
                drift = reconcile_walk(old, old.root, self.scan_filter,
                                       cancel=self.build_cancel,
                                       exts=self.exts)
            except Exception as e:
                log.error("reconcile walk failed: %s", e)
                return
            if drift is None or self.build_cancel.is_set() or not drift.changed:
                return  # cancelled, or library unchanged
            if self.adopt:
                # The thumbs are an external (--thumbs) cache we can't
                # rebuild; surface the drift and leave the view as-is.
                _emit(bridge.status,
                      f"library changed since this cache was built "
                      f"({drift.summary()}) — showing the indexed snapshot")
                return
            _emit(bridge.status,
                  f"library changed ({drift.summary()}) — reindexing…")
            fresh = None
            try:
                fresh = scan_library(old.root, self.scan_filter,
                                     self.contacts, self.pal_dir,
                                     exts=self.exts, db3_dir=self.db3_dir)
                result = build_cache(fresh, cache_dir, None,
                                     cancel=self.build_cancel)
                if result is None:
                    return
                save_catalog_retrying(fresh, cache_dir / "catalog.json")
                save_report(fresh.report, cache_dir / REPORT_NAME)
                _emit(bridge.finished, result, fresh, True)
            except Exception as e:
                log.error("reindex failed: %s", e)
                _emit(bridge.finished, None, fresh or old, True)

        self._reconcile_thread = threading.Thread(target=work, daemon=True)
        self._reconcile_thread.start()

    def _start_backfill(self) -> None:
        """Adopt-mode metadata + identity-signal backfill (fauxcasa-cam.12):
        run the read side of the indexer (thumbcache.backfill_catalog — no
        thumbnail work) over the already-bound catalog, in place. Progress
        rides the same inline status text as reconcile (modes, not modals);
        the pass persists the catalog every BACKFILL_PERSIST_EVERY photos
        and on cancel, so a quit mid-pass resumes on the next launch.
        In-file captions/keywords/dates/GPS/ratings supersede the ini-tier
        values photo by photo as the pass reaches them (§4 tier-1)."""
        bridge, catalog = self._bridge, self.catalog
        cat_path = self.cache_dir / "catalog.json"

        def cb(done: int, total: int) -> None:
            if done % 100 == 0 or done == total:
                _emit(bridge.status,
                      f"backfilling metadata {done:,}/{total:,}")

        def work() -> None:
            try:
                result = backfill_catalog(catalog, cat_path, progress=cb,
                                          cancel=self.build_cancel,
                                          pause=self.backfill_pause)
            except Exception as e:  # report, never crash the UI
                log.error("metadata backfill failed: %s", e)
                _emit(bridge.backfill_done, False)
                return
            if result is not None:
                log.info("backfill: %d photos in %.1f s (%d workers)",
                         result.photos, result.elapsed_s, result.workers)
            _emit(bridge.backfill_done, result is not None)

        self._backfill_thread = threading.Thread(target=work, daemon=True)
        self._backfill_thread.start()

    def _on_backfill_done(self, ok: bool) -> None:
        """Backfill finished (ok) or was cancelled/failed (not ok — a
        cancel resumes next launch, a failure is logged). On success the
        catalog was mutated in place, so refresh what reads it: the sidebar
        counts (Starred may grow from XMP ratings, Recently Updated
        populates now that mtimes are real — the PR #41 rider), the grid's
        painted star badges, and — if the user is looking at the Recently
        Updated view outside a search — the view itself."""
        self.progress_label.setText("")
        if not ok:
            return
        kind, key = self._selected_view()
        self._rebuild_sidebar()
        self._reselect_view(kind, key)
        if kind == "recent" and not self.search.text().strip():
            self._apply_view(kind, key)   # the collection just populated
        self.grid.viewport().update()     # star badges may have changed
        self.statusBar().showMessage("metadata backfill complete", 8000)
        if self._reconcile_after_backfill:
            self._reconcile_after_backfill = False
            self._start_reconcile()

    def _on_status(self, text: str) -> None:
        self.progress_label.setText(("   " + text) if text else "")

    def _on_index_finished(self, result, catalog, is_reconcile: bool) -> None:
        self.progress_label.setText("")
        if result is None:
            if is_reconcile:
                # A background refresh failure doesn't invalidate the
                # already-loaded, correct cache this run is using — surface
                # it, but don't fail an otherwise-good run (--finish-build).
                self.statusBar().showMessage(
                    "background reindex failed — see stderr", 10000)
            else:
                self.build_failed = True
                self.statusBar().showMessage(
                    "indexing failed — see stderr", 10000)
            return
        self.last_index_rate = result.rate
        print(json.dumps({
            "event": "indexed", "photos": result.photos,
            "elapsed_s": round(result.elapsed_s, 3),
            "rate_per_s": round(result.rate, 1), "workers": result.workers,
        }), flush=True)
        try:
            cache = load_cache(result.path)
            bind(cache, catalog)
        except CacheError as e:
            log.error("built cache failed to bind: %s", e)
            return
        if catalog is self.catalog:
            self.grid.set_thumbs(cache)        # cold build: same catalog
            self.viewer.set_thumbs(cache)      # ...so the viewer previews too
            self.tray.set_thumbs(cache)        # ...and held thumbs render
            self._refresh_recent_count()       # mtimes just landed (q6l.7)
            # build_cache merged in-file captions/keywords into these SAME
            # Photo objects in place — the prebuilt haystacks are stale.
            self._rebuild_search_index()
        else:
            self.reload_data(catalog, cache)   # reconcile: swap in the new
        self.statusBar().showMessage(
            f"indexed {result.photos} photos at {result.rate:.0f}/s", 8000)

    def reload_data(self, catalog: Catalog, thumbs: ThumbCache) -> None:
        """Atomically swap the whole catalog after a reconcile rebuild:
        re-point grid + viewer, rebuild the sidebar, return to the
        browser (a viewer index may no longer be valid)."""
        self.catalog = catalog
        self._rebuild_search_index()           # new photos -> new haystacks
        self.viewer.catalog = catalog
        self.viewer.set_thumbs(thumbs)         # reconciled cache for previews
        if self._slideshow is not None and self._slideshow.isVisible():
            # Same reason the viewer page is left: the show's display
            # indices belong to the old catalog. (An idle surface is fine —
            # _start_slideshow re-points it before the next show.)
            self._slideshow._exit()
        # A peeked index belongs to the old catalog too (peek_released
        # -> _hide_peek; idle no-op otherwise).
        self.grid._end_peek()
        self.pages.setCurrentWidget(self.pages.widget(0))
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.grid.set_data(catalog, thumbs)
        self._rebuild_sidebar()
        # Held photos survive the swap BY IDENTITY (rel path): rebind
        # re-resolves them against the new catalog's indices and counts
        # any that vanished for the readout's note (fauxcasa-q6l.2).
        self.tray.rebind(catalog, thumbs)
        self._show_counts("All photos", self._shown_count())
        self._update_import_notes()   # the rescan collected a fresh report
        self.meta_label.setText("")

    def index_busy(self) -> bool:
        return any(t is not None and t.is_alive()
                   for t in (self._build_thread, self._reconcile_thread))

    def shutdown(self) -> None:
        """Stop and reap the index threads so none is mid-write (or
        inside a Qt codec) while the interpreter tears down. The backfill
        persists its cursor on cancel, so a quit mid-backfill simply
        resumes on the next launch (its final save is why it gets the
        same join, not a bare daemon abandon)."""
        self.build_cancel.set()
        self.backfill_pause.clear()  # a paused backfill must see the cancel
        for t in (self._build_thread, self._reconcile_thread,
                  self._backfill_thread):
            if t is not None and t.is_alive():
                t.join(timeout=5.0)

    def _change_library(self) -> None:
        root = _choose_library_from_dialog(
            self.cache_root, self, self.catalog.root)
        if root is None:
            return
        # Different library: forward scan-size constraints but NOT --thumbs
        # (the adopted cache is specific to the previous library).
        program, args = _restart_command(root, self.cache_root,
                                         scan_filter=self.scan_filter)
        started = QProcess.startDetached(program, args)
        ok = started[0] if isinstance(started, tuple) else started
        if not ok:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, APP_NAME, f"Could not open the selected folder:\n{root}")
            return
        _remember_library(self.cache_root, root)
        self.shutdown()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _show_file_types(self) -> None:
        """Tools > File Types… (§5: per-extension include/exclude panel —
        Picasa's Tools > Options > File Types). A plain modal dialog by
        judgment (filetypes module doc): one-shot configuration with a
        cache-identity consequence, not a browsing workflow. An accepted
        change persists per-library into config.json and relaunches
        through the same single-sourced startup as Open... — the walk AND
        the cache dir both change with the extension set, and toggling a
        type back returns to the previous warm cache (exts_cache_key)."""
        dlg = FileTypesDialog(self.excluded_exts, self)
        if not dlg.exec_():
            return
        excluded = dlg.excluded()
        if excluded == self.excluded_exts:
            return
        if not save_excluded_exts(self.cache_root, self.catalog.root,
                                  excluded):
            self.statusBar().showMessage(
                "could not save the file-type choice — see the log", 8000)
            return
        # Same library: forward scan-size constraints only. An adopted cache
        # binds to a specific walk; a File-Types change changes the walk, so
        # the relaunch must cold-rebuild the cache.
        program, args = _restart_command(self.catalog.root, self.cache_root,
                                         scan_filter=self.scan_filter)
        started = QProcess.startDetached(program, args)
        ok = started[0] if isinstance(started, tuple) else started
        if not ok:
            # The choice IS saved; it just needs a launch to apply.
            self.statusBar().showMessage(
                "file-type choice saved — restart to apply", 8000)
            return
        self.shutdown()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, event) -> None:
        self.build_cancel.set()
        super().closeEvent(event)

    # ---------- reveal (show hidden) ----------

    def _shown_count(self) -> int:
        """Total photos in the current view mode: all photos when revealing
        hidden/stash files, else only the normally-visible ones."""
        cat = self.catalog
        return len(cat.photos) if self.grid.reveal else cat.visible_count

    def _toggle_reveal(self, on: bool) -> None:
        """Show/hide hidden=yes photos and stash-folder files. Rebuilds the
        sidebar (counts and which folders appear both change) but PRESERVES
        the active view across the toggle (fauxcasa-x1l): the current search,
        or the selected folder/album/star, plus the scroll position — rather
        than snapping back to All photos. The visible set changes under
        reveal, so each view is recomputed for the new state, not merely
        re-pointed."""
        kind, key = self._selected_view()
        search_text = self.search.text()
        sb = self.grid.verticalScrollBar()
        frac = sb.value() / sb.maximum() if sb.maximum() > 0 else 0.0

        self.grid.reveal = on
        self._rebuild_sidebar()
        self._reselect_view(kind, key)

        if search_text.strip():
            self._search_changed(search_text)   # reveal-aware re-filter
            self.grid.scroll_to_fraction(frac)
        elif kind == "folder":
            self._apply_view(kind, key)          # scroll_to_folder re-pins it
        else:
            self._apply_view(kind, key)
            self.grid.scroll_to_fraction(frac)   # best-effort scroll restore
        self.grid.setFocus()
        self._refresh_tray_readout()             # view counts changed (q6l.2)

    def _selected_view(self) -> tuple[str, str]:
        """The (kind, key) of the active sidebar selection, defaulting to the
        All-photos view when nothing selectable is current."""
        item = self.tree.currentItem()
        if item is not None:
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data is not None:
                return data
        return ("all", "")

    def _reselect_view(self, kind: str, key: str) -> None:
        """Restore the sidebar's current-item highlight to (kind, key) after
        a rebuild; silently no-ops if that view no longer exists (e.g. a stash
        folder that only appears under reveal)."""
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == (kind, key):
                self.tree.setCurrentItem(it.value())
                return
            it += 1

    # ---------- sidebar ----------

    def _new_sidebar_tree(self) -> QTreeWidget:
        """Create and wire a fresh sidebar tree. Factored out so a rebuild can
        swap in a brand-new widget rather than clear() the live one."""
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.itemClicked.connect(self._sidebar_clicked)
        # After the view applies: the tray readout's type/count follow the
        # sidebar selection (q6l.2). Connection order makes this run second.
        tree.itemClicked.connect(
            lambda *_a: self._refresh_tray_readout())
        # Folder context menu (fauxcasa-q6l.11): per-folder sort modes.
        # Wired HERE so every swapped-in rebuild tree (the gfz fresh-widget
        # pattern) carries the menu, not just the first one.
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(self._sidebar_menu)
        return tree

    def _rebuild_sidebar(self) -> None:
        """Repopulate the sidebar after a reveal toggle or reconcile reload.

        Swaps in a freshly built QTreeWidget and defers the old one's
        destruction via deleteLater(), instead of self.tree.clear()+repopulate.
        On the real Windows Qt platform, clear()-ing a QTreeWidget that has a
        current item set intermittently aborts with a native access violation
        in the in-place item teardown (fauxcasa-gfz) — reproducible in a single
        shown window with a running event loop, so it can crash the real app on
        a Show-hidden toggle or a post-reconcile reload, not just the tests.
        Building a new tree and letting the event loop free the old one at a
        safe point sidesteps that teardown path entirely."""
        old = self.tree
        split = old.parentWidget()
        new = self._new_sidebar_tree()
        idx = split.indexOf(old)
        self.tree = new
        split.replaceWidget(idx, new)
        self._build_sidebar()
        old.deleteLater()

    def _build_sidebar(self) -> None:
        cat = self.catalog
        reveal = self.grid.reveal
        t = self.tree

        def fcount(folder) -> int:
            return folder.total_count if reveal else folder.photo_count

        all_item = QTreeWidgetItem(
            t, [f"All photos  ({self._shown_count()})"])
        all_item.setData(0, Qt.ItemDataRole.UserRole, ("all", ""))
        starred = sum(
            1 for p in cat.photos if (p.visible or reveal) and p.star)
        star_item = QTreeWidgetItem(t, [f"★ Starred  ({starred})"])
        star_item.setData(0, Qt.ItemDataRole.UserRole, ("starred", ""))
        # Recently Updated auto-collection (fauxcasa-q6l.7): mtime recency,
        # semantics in recent_indices(). Live count like Starred — rebuilt
        # with the sidebar, plus a cold-build refresh once mtimes exist.
        recent_item = QTreeWidgetItem(t, [self._recent_label()])
        recent_item.setData(0, Qt.ItemDataRole.UserRole, ("recent", ""))

        folders_root = QTreeWidgetItem(t, ["Folders"])
        folders_root.setFlags(
            folders_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        nodes: dict[str, QTreeWidgetItem] = {"": folders_root}

        def node_for(rel: str) -> QTreeWidgetItem:
            if rel in nodes:
                return nodes[rel]
            parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
            parent = node_for(parent_rel)
            item = QTreeWidgetItem(parent, [rel.split("/")[-1]])
            item.setData(0, Qt.ItemDataRole.UserRole, ("folder", rel))
            nodes[rel] = item
            return item

        for rel, folder in cat.folders.items():
            if fcount(folder) == 0:
                continue  # empty (off-reveal: stash/hidden-only) folders out
            item = node_for(rel)
            item.setText(0, f"{folder.title}  ({fcount(folder)})")
        folders_root.setExpanded(True)

        if cat.albums:
            albums_root = QTreeWidgetItem(t, ["Albums"])
            albums_root.setFlags(
                albums_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            for uid, album in cat.albums.items():
                # §3: a placeholder (albums= uid with no definition) is
                # never dropped — shown dimmed/italic with a "?" suffix so
                # the gap is visible, not silent (import report has the
                # entry). A .pal-sourced album reads like a real one; its
                # provenance lives in the tooltip.
                suffix = " ?" if album.placeholder else ""
                item = QTreeWidgetItem(
                    albums_root,
                    [f"{album.name}{suffix}  ({len(album.members)})"])
                item.setData(0, Qt.ItemDataRole.UserRole, ("album", uid))
                if album.placeholder:
                    f = item.font(0)
                    f.setItalic(True)
                    item.setFont(0, f)
                    item.setForeground(0, t.palette().brush(
                        QPalette.ColorGroup.Disabled,
                        QPalette.ColorRole.Text))
                    item.setToolTip(
                        0, "Referenced by albums= lines but defined nowhere "
                           "— placeholder (see import notes)")
                elif album.pal_sourced:
                    item.setToolTip(
                        0, "Album definition from a Picasa2Albums .pal file")
            albums_root.setExpanded(True)

        # People (read-only v1 slice, fauxcasa-cam.3): named people with
        # photo counts, clicking filters the grid like an album, plus an
        # explicit "Unnamed faces" affordance for photos carrying
        # suggested/unresolved face regions (N7: the gap is never silent).
        # Counts are live: rebuilt with the sidebar on reveal/reconcile.
        people, unnamed, name_cids = self._people_counts()
        if people or unnamed:
            # db3-rescued people are source-flagged (fauxcasa-cam.7): the
            # name exists only because the §4 rescue importer read it from
            # a machine-local db3 person album — provenance in the tooltip,
            # like a .pal-sourced album's. Rekeyed honestly by contact id,
            # not display name (fauxcasa-7aj.2): two DIFFERENT contacts can
            # share a display name (one ini-named, one db3-rescued), so a
            # name is flagged only when EVERY contact id that resolves to
            # it is db3-rescued — a name also known via a non-db3 contact
            # id must not be tarred with the rescue flag.
            db3_names = {name for name, cids in name_cids.items()
                         if cids and cids <= cat.db3_contacts}
            people_root = QTreeWidgetItem(t, ["People"])
            people_root.setFlags(
                people_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            for person in sorted(people, key=str.lower):
                item = QTreeWidgetItem(
                    people_root, [f"{person}  ({people[person]})"])
                item.setData(0, Qt.ItemDataRole.UserRole, ("person", person))
                if person in db3_names:
                    item.setToolTip(
                        0, "Name rescued from the Picasa db3 database — "
                           "no .picasa.ini or contacts.xml names this "
                           "person (see import notes)")
            if unnamed:
                item = QTreeWidgetItem(
                    people_root, [f"Unnamed faces  ({unnamed})"])
                item.setData(0, Qt.ItemDataRole.UserRole, ("unnamed", ""))
            people_root.setExpanded(True)

    def _recent_indices(self) -> list[int]:
        """The Recently Updated set for the current reveal state (semantics
        + one-line mtime decision live on recent_indices above)."""
        return recent_indices(self.catalog, self.grid.reveal)

    def _recent_label(self) -> str:
        """The sidebar text for Recently Updated. Honesty hint (the PR #41
        rider, fauxcasa-cam.12): while an adopt-mode backfill has not yet
        filled real mtimes, an empty collection says WHY it is empty
        instead of a bare 0 — the count appears once the backfill lands
        (or immediately, if some already-backfilled photos qualify)."""
        n = len(self._recent_indices())
        if n == 0 and self.catalog.backfill_state != BACKFILL_COMPLETE:
            return "↻ Recently Updated  (indexing metadata…)"
        return f"↻ Recently Updated  ({n})"

    def _refresh_recent_count(self) -> None:
        """A COLD build fills Photo.mtime in-place only after the sidebar was
        first built (its count then read 0) — update just that item's label.
        setText on a live item is safe; only clear()+repopulate of a tree
        with a current item is the fauxcasa-gfz crash path."""
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == ("recent", ""):
                it.value().setText(0, self._recent_label())
                return
            it += 1

    def _people_counts(self) -> tuple[dict[str, int], int, dict[str, set]]:
        """Per-person photo tallies for the current reveal state: {name:
        photo count} over named faces, plus how many photos carry at least
        one unnamed (suggested/unresolved) face. Photo counts, not face
        counts — a photo with the same person tagged twice counts once.
        Also returns {name: {contact ids that resolved to it}} — the
        db3-rescued sidebar flag (fauxcasa-7aj.2) needs this to rekey by
        contact id rather than display name, since two DIFFERENT contacts
        can share a display name."""
        reveal = self.grid.reveal
        people: dict[str, int] = {}
        name_cids: dict[str, set] = {}
        unnamed = 0
        for p in self.catalog.photos:
            if not (p.visible or reveal) or not p.faces:
                continue
            names = {n for _rect, _cid, n in p.faces if n}
            for n in names:
                people[n] = people.get(n, 0) + 1
            for _rect, cid, n in p.faces:
                if n:
                    name_cids.setdefault(n, set()).add(cid)
            if any(n is None for _rect, _cid, n in p.faces):
                unnamed += 1
        return people, unnamed, name_cids

    def _sidebar_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None:
            return
        # Track the active view so a Show-hidden toggle can preserve it
        # (fauxcasa-x1l). On a real click Qt has already made this the
        # current item; set it explicitly so a programmatic call agrees.
        self.tree.setCurrentItem(item)
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._apply_view(*data)
        self.grid.setFocus()

    def _apply_view(self, kind: str, key: str) -> None:
        """Apply a sidebar view's grid filter + status counts WITHOUT touching
        the search box. Shared by _sidebar_clicked and the Show-hidden toggle,
        so the toggle can re-apply the active view (recomputed for the new
        reveal state) instead of snapping to All photos."""
        cat = self.catalog
        if kind == "starred":
            idxs = [i for i, p in enumerate(cat.photos)
                    if (p.visible or self.grid.reveal) and p.star]
            self.grid.set_filter(idxs, "Starred")
            self._show_counts("Starred", len(idxs))
        elif kind == "recent":
            idxs = self._recent_indices()
            self.grid.set_filter(idxs, "Recently Updated")
            self._show_counts("Recently Updated", len(idxs))
            if not idxs and self.catalog.backfill_state != BACKFILL_COMPLETE:
                # empty-state honesty (cam.12): mtimes are still being
                # backfilled, so an empty view is pending, not final
                self.statusBar().showMessage(
                    "Recently Updated fills in as the metadata backfill "
                    "indexes file dates…", 8000)
        elif kind == "album" and key in cat.albums:
            album = cat.albums[key]
            self.grid.set_filter(list(album.members), album.name)
            self._show_counts(f"Album “{album.name}”", len(album.members))
        elif kind == "person":
            idxs = [i for i, p in enumerate(cat.photos)
                    if (p.visible or self.grid.reveal)
                    and any(n == key for _rect, _cid, n in p.faces)]
            self.grid.set_filter(idxs, key)
            self._show_counts(f"Person “{key}”", len(idxs))
        elif kind == "unnamed":
            idxs = [i for i, p in enumerate(cat.photos)
                    if (p.visible or self.grid.reveal)
                    and any(n is None for _rect, _cid, n in p.faces)]
            self.grid.set_filter(idxs, "Unnamed faces")
            self._show_counts("Unnamed faces", len(idxs))
        else:  # "all", "folder", or an album that no longer exists
            self.grid.set_filter(None, "")
            if kind == "folder":
                self.grid.scroll_to_folder(key)
            self._show_counts("All photos", self._shown_count())

    # ---------- per-folder sort modes (fauxcasa-q6l.11) ----------

    def _sidebar_menu(self, point) -> None:
        """Right-click on the sidebar: FOLDER items get the sort-mode menu
        (spec §5 per-folder sort; the manual mode is blocked on the db3
        oracle fixture and absent). Every other item kind — albums keep
        membership order, auto-collections keep catalog order — has no
        menu, which is the folder-scoped contract made visible."""
        item = self.tree.itemAt(point)
        if item is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] != "folder":
            return
        menu = self._folder_sort_menu(data[1])
        menu.exec(self.tree.viewport().mapToGlobal(point))

    def _folder_sort_menu(self, rel: str) -> QMenu:
        """Build (without exec'ing — the seam tests drive) the context menu
        for one folder: a checkable, mutually exclusive action per sort
        mode, the folder's current mode checked."""
        menu = QMenu(self.tree)
        menu.addSection("Sort by")
        group = QActionGroup(menu)
        current = self.grid.sort_modes.get(rel, DEFAULT_SORT_MODE)
        for mode in SORT_MODES:
            act = menu.addAction(mode.capitalize())
            act.setCheckable(True)
            act.setChecked(mode == current)
            act.setData(mode)
            group.addAction(act)
            act.triggered.connect(
                lambda _checked=False, m=mode: self._set_folder_sort(rel, m))
        return menu

    def _set_folder_sort(self, rel: str, mode: str) -> None:
        """Apply + persist one folder's sort mode. The mode reshapes the
        folder-grouped default view only, so 'apply immediately' means:
        when that view is showing (no search, All-photos/folder selection),
        rebuild it and bring the re-sorted folder into view; an active
        search/album/starred view keeps its own order by design and picks
        the mode up on the next return to the folder view."""
        if mode == DEFAULT_SORT_MODE:
            self.grid.sort_modes.pop(rel, None)
        else:
            self.grid.sort_modes[rel] = mode
        save_sort_modes(self.cache_dir, self.grid.sort_modes)
        if self.search.text().strip():
            return
        kind, key = self._selected_view()
        if kind in ("all", "folder"):
            self._apply_view(kind, key)
            self.grid.scroll_to_folder(rel)

    # ---------- search ----------

    def _rebuild_search_index(self) -> None:
        """Precompute every photo's lowercase search haystack ONCE per
        catalog load / filter-relevant mutation, instead of rebuilding the
        strings per photo per keystroke — at 100k that string-building
        dominated _search_changed at ~58-95 ms/keystroke, over the §7
        50 ms budget; scanning these prebuilt pairs is single-digit ms
        (fauxcasa-ed5.4, before/after numbers in the bead/PR).

        Sync points (photos are otherwise immutable in this read-only app):
        construction, reload_data (reconcile swapped in a new catalog), and
        the cold-build finish (build_cache merges in-file captions/keywords
        into the SAME Photo objects in place). _search_changed also rebuilds
        as a backstop if the pair count no longer matches the catalog.

        The haystack text is exactly what the per-keystroke scan built:
        every searchable field newline-joined (terms are whitespace-free, so
        no term can straddle two fields), folder title + rel path shared per
        folder. The visible-only subset is precomputed too (same string
        objects, so the memory cost is one extra list of references) because
        off-reveal searches — the common case — then skip the per-photo
        visibility test entirely."""
        cat = self.catalog
        t0 = time.perf_counter()
        folder_hay = {rel: f"{f.title}\n{rel}".lower()
                      for rel, f in cat.folders.items()}
        pairs: list[tuple[int, str]] = []
        append = pairs.append
        for i, p in enumerate(cat.photos):
            append((i, "\n".join((
                p.name,
                p.caption or "",
                " ".join(p.keywords),
                " ".join(n for _rect, _cid, n in p.faces if n),  # people (§5)
                folder_hay[p.folder],
            )).lower()))
        self._search_pairs = pairs
        if cat.visible_count == len(cat.photos):
            self._search_pairs_vis = pairs  # nothing hidden: share the list
        else:
            self._search_pairs_vis = [
                ih for ih, p in zip(pairs, cat.photos) if p.visible]
        log.info("search index: %d haystacks in %.0f ms",
                 len(pairs), (time.perf_counter() - t0) * 1000.0)

    @staticmethod
    def _parse_query(text: str) -> tuple[list[str], list[str]]:
        """Whitespace-tokenized query -> (positive, negative) lowercase
        terms. Positive terms AND together; a '-'-prefixed token excludes
        any photo it matches (§5 negation — a negation-only query is valid:
        Picasa users' all-photos hack was exactly that). A lone '-' — a
        negation still being typed — contributes nothing rather than
        blanking the grid."""
        pos: list[str] = []
        neg: list[str] = []
        for tok in text.lower().split():
            if tok.startswith("-"):
                if len(tok) > 1:
                    neg.append(tok[1:])
            else:
                pos.append(tok)
        return pos, neg

    def _search_changed(self, text: str) -> None:
        # Timed end to end — parse + filter scan + grid.set_filter + status
        # label — because §7's budget is "search keystroke -> filtered grid
        # < 50 ms". The repaint after set_filter is the grid's normal async
        # frame and is not included (fauxcasa-ed5.4).
        t0 = time.perf_counter()
        pos, neg = self._parse_query(text)
        if not pos and not neg:
            self.grid.set_filter(None, "")
            self._show_counts("All photos", self._shown_count())
            self.last_search_hits = self._shown_count()
            self.last_search_ms = (time.perf_counter() - t0) * 1000.0
            return
        # Scan the PREBUILT haystack pairs (_rebuild_search_index) as a term
        # cascade — each positive pass keeps its matches (AND: survivors
        # matched every earlier term), each negative pass drops its matches
        # — list comprehensions per term measure ~3-4x faster at 100k than
        # one pass with all()/any() generator predicates per photo, and
        # order (catalog order) is preserved throughout.
        if len(self._search_pairs) != len(self.catalog.photos):
            self._rebuild_search_index()  # backstop; the sync points above
        cur = (self._search_pairs if self.grid.reveal
               else self._search_pairs_vis)
        for term in pos:
            cur = [ih for ih in cur if term in ih[1]]
        for term in neg:
            cur = [ih for ih in cur if term not in ih[1]]
        idxs = [ih[0] for ih in cur]
        q = text.strip().lower()
        self.grid.set_filter(idxs, f"search: {q}")
        self._show_counts(f"Search “{q}”", len(idxs))
        self.last_search_hits = len(idxs)
        self.last_search_ms = (time.perf_counter() - t0) * 1000.0

    # ---------- selection tray (fauxcasa-q6l.2) ----------

    def selection_context(self) -> SelectionContext:
        """The universal-input snapshot output actions read — see
        SelectionContext (the M2 attachment point). Precedence mirrors
        Picasa: held photos win, then the grid selection, then the whole
        active view."""
        held = tuple(self.tray.held)
        if held:
            return SelectionContext(
                "held", tuple(self.tray.held_indices()), held)
        if self.grid.selection:
            pos = self.grid.display_pos
            idxs = sorted(self.grid.selection,
                          key=lambda i: pos.get(i, len(pos)))
            return SelectionContext("photos", tuple(idxs), held)
        kind, _key = self._selected_view()
        if self.search.text().strip():
            kind = "search"
        return SelectionContext(kind, tuple(self.grid.display), held)

    def _hold_selection(self) -> None:
        """Hold button / Ctrl+H in the grid: append the current grid
        selection to the tray. Within this ONE action members go in
        display order (deterministic); across actions the tray keeps
        hold order (tray.py's ordering decision). Empty selection is a
        no-op — Hold never guesses."""
        sel = self.grid.selection
        if not sel:
            return
        pos = self.grid.display_pos
        order = sorted(sel, key=lambda i: pos.get(i, len(pos)))
        self.tray.hold(self.catalog.photos[i].rel for i in order)

    def _hold_from_viewer(self, idx: int) -> None:
        """Ctrl+H in the viewer: hold the photo on screen."""
        if 0 <= idx < len(self.catalog.photos):
            self.tray.hold([self.catalog.photos[idx].rel])

    def _tray_navigate(self, rel: str) -> None:
        """A held thumb was clicked: show that photo in the grid — back
        on the browser page, falling back to the All-photos view when
        the active filter doesn't show it (a held photo is cross-folder
        by design, so a search/album/starred view may not contain it).
        Hidden photos auto-reveal when reveal is off (q6l.18)."""
        idx = self.tray.index_of(rel)
        if idx is None:
            return
        self.pages.setCurrentWidget(self.pages.widget(0))
        revealed = False
        if idx not in self.grid.display_pos:
            # Hidden photo with reveal off: auto-reveal before falling back
            # to All photos — avoids a silent no-op (N7) when the only
            # absence reason is the hidden flag.
            photo = self.catalog.photos[idx]
            if not self.grid.reveal and not photo.visible:
                self.reveal_box.setChecked(True)   # triggers _toggle_reveal
                revealed = True
            if idx not in self.grid.display_pos:
                # Still absent (filter reason or reveal didn't surface it):
                # clear to All photos as the last resort.
                self.search.blockSignals(True)
                self.search.clear()
                self.search.blockSignals(False)
                self.grid.set_filter(None, "")
                self._reselect_view("all", "")
                self._show_counts("All photos", self._shown_count())
        if idx in self.grid.display_pos:
            self.grid._select(idx)
            self.grid._ensure_visible(idx)
            if revealed:
                self.statusBar().showMessage(
                    "Hidden photos revealed to show this photo", 4000)
        else:
            # reveal may have been toggled ON as a side effect of this
            # navigation; name that action in the message so state and
            # message agree (N7 — no silent side effects).
            msg = ("Hidden photos revealed, but the photo is not visible "
                   "in any view" if revealed
                   else "Photo not visible in any view")
            self.statusBar().showMessage(msg, 4000)
        self.grid.setFocus()
        self._refresh_tray_readout()

    def _refresh_tray_readout(self) -> None:
        tray = getattr(self, "tray", None)  # sidebar exists before tray
        if tray is not None:
            tray.readout.setText(self._tray_readout_text())

    def _tray_readout_text(self) -> str:
        """The tray's live type-aware readout (spec §5: "Folder Selected
        — 14 photos"). Precedence: held set -> grid selection -> the
        active view's type. The vanish note (held photos a reconcile
        dropped) rides along until the next tray action acknowledges it
        — including when the ENTIRE held set vanished; dropping them
        silently is exactly what N7 forbids. Lives in the tray strip;
        the status bar keeps its own dual mode untouched (q6l.1)."""
        def photos(n: int) -> str:
            return f"{n} photo" + ("" if n == 1 else "s")

        parts = []
        if self.tray.held:
            parts.append(f"{photos(len(self.tray.held))} held")
        if self.tray.vanished:
            v = self.tray.vanished
            parts.append(f"{v} held photo{'' if v == 1 else 's'} "
                         "no longer in the library")
        if parts:
            return " — ".join(parts)
        if self.grid.selection:
            return f"{photos(len(self.grid.selection))} selected"
        kind, key = self._selected_view()
        if self.search.text().strip():
            return f"Search — {photos(len(self.grid.display))}"
        if kind == "folder" and key in self.catalog.folders:
            f = self.catalog.folders[key]
            n = f.total_count if self.grid.reveal else f.photo_count
            return f"Folder selected — {photos(n)}"
        if kind == "album" and key in self.catalog.albums:
            n = len(self.catalog.albums[key].members)
            return f"Album selected — {photos(n)}"
        return photos(len(self.grid.display))

    # ---------- status ----------

    def _show_counts(self, label: str, n: int) -> None:
        reveal = self.grid.reveal
        folders = sum(1 for f in self.catalog.folders.values()
                      if (f.total_count if reveal else f.photo_count))
        self.counts_label.setText(
            f"  {label}: {n} photos · {folders} folders"
            f" · {len(self.catalog.albums)} albums")

    def _update_import_notes(self) -> None:
        """The lean import-report surface (fauxcasa-cam.13): a permanent
        status-bar count when the catalog's report has entries, with the
        first few in the tooltip. Hidden entirely at zero — most libraries
        have no notes and deserve no chrome. The full inspector is N7/M2."""
        entries = self.catalog.report.entries
        if not entries:
            self.notes_label.setVisible(False)
            self.notes_label.setText("")
            self.notes_label.setToolTip("")
            return
        n = len(entries)
        self.notes_label.setText(
            f"{n} import note{'s' if n != 1 else ''}  ")
        shown = [f"[{e.source}] {e.kind}: {e.detail}" for e in entries[:6]]
        if n > len(shown):
            shown.append(f"… and {n - len(shown)} more (see {REPORT_NAME})")
        self.notes_label.setToolTip("\n".join(shown))
        self.notes_label.setVisible(True)

    def _selection_changed(self, selection: set) -> None:
        """Grid multi-select -> status label (spec §5 dual mode): exactly
        one selected shows that photo's metadata; several show the
        aggregate count; none clears (fauxcasa-q6l.1)."""
        if len(selection) == 1:
            self._photo_selected(next(iter(selection)))
        elif selection:
            self.meta_label.setText(f"{len(selection)} photos selected  ")
        else:
            self.meta_label.setText("")

    def _photo_selected(self, idx: int) -> None:
        """Single-photo status readout (§5 dual mode): path, star count
        (★ repeated — one glyph per star, so a count > 1 reads at a
        glance), capture date, geotag coordinates (§3 geotag v1 display,
        fauxcasa-cam.9/.10/.11), caption, keywords."""
        if idx < 0:
            self.meta_label.setText("")
            return
        p = self.catalog.photos[idx]
        parts = [p.rel]
        if p.star:
            parts.append("★" * min(p.star, 5))
        if p.date_taken:
            parts.append(format_date_taken(p.date_taken))
        if p.geotag is not None:
            parts.append(format_geotag(p.geotag))
        if p.caption:
            parts.append(f"“{p.caption}”")
        if p.keywords:
            parts.append("#" + " #".join(p.keywords))
        if p.stashed_original is not None:
            # baked edit with the untouched original still in the stash
            # (fauxcasa-cam.19); restore machinery is M3.
            parts.append("Picasa-saved original kept")
        self.meta_label.setText("   ".join(parts) + "  ")

    def _build_progress(self, done: int, total: int) -> None:
        self.progress_label.setText(f"   indexing {done}/{total}…")

    # ---------- viewer ----------

    def _open_viewer(self, _idx: int, display: list, pos: int) -> None:
        self.pages.setCurrentWidget(self.viewer)
        self.viewer.show_photo(list(display), pos)
        self.viewer.setFocus()

    def _close_viewer(self, idx: int) -> None:
        self.pages.setCurrentWidget(self.pages.widget(0))
        if idx >= 0:
            self.grid._select(idx)
            self.grid._ensure_visible(idx)
        self.grid.setFocus()

    # ---------- slideshow (fauxcasa-q6l.3) ----------

    def _start_slideshow(self) -> None:
        """Play the CURRENT display set — whatever the grid is showing
        (folder view, album, All, Starred, search results) — full-screen,
        from the current photo (or the first when none is selected).
        Read-only playback; the overlay star/reject controls are M2. The
        show is a separate top-level surface, so the grid/viewer beneath
        keeps its exact state for the Esc return trip."""
        display = list(self.grid.display)
        if not display:
            return
        if self.pages.currentWidget() is self.viewer:
            current = self.viewer.current_index()
        else:
            current = self.grid.current  # catalog index, -1 = none (#36)
        pos = self.grid.display_pos.get(current, 0)
        if self._slideshow is None:
            self._slideshow = SlideshowPage(self.catalog, self.grid.thumbs)
            self._slideshow.closed.connect(self._slideshow_closed)
        else:
            # Reused surface: re-point at the live catalog/cache pair (a
            # reconcile may have swapped both since the last show).
            self._slideshow.catalog = self.catalog
            self._slideshow.set_thumbs(self.grid.thumbs)
        self._slideshow.start(display, pos)

    def _slideshow_closed(self, _idx: int) -> None:
        # The surface hid itself; the pages beneath were never touched —
        # "back to exactly the prior state" is just re-taking focus on
        # whichever page is current.
        self.activateWindow()
        current = self.pages.currentWidget()
        (self.viewer if current is self.viewer else self.grid).setFocus()

    # ---------- hover peek (fauxcasa-q6l.5) ----------

    def _show_peek(self, idx: int) -> None:
        """Ctrl+Alt hover on a grid photo (the grid's trigger machine):
        full-screen peek on the screen containing the cursor. The surface
        is input-transparent and non-activating (peek.py), so the grid
        keeps the focus and the whole event stream — no focus juggling to
        undo on dismissal."""
        if self._peek_page is None:
            self._peek_page = PeekPage(self.catalog, self.grid.thumbs)
        else:
            # Reused surface: re-point at the live catalog/cache pair (the
            # slideshow discipline — a reconcile may have swapped both).
            self._peek_page.catalog = self.catalog
            self._peek_page.set_thumbs(self.grid.thumbs)
        self._peek_page.peek(idx)

    def _hide_peek(self) -> None:
        if self._peek_page is not None:
            self._peek_page.dismiss()


def run_search_probe(win: MainWindow, spec: str) -> list[dict]:
    """§7 search-latency probe (fauxcasa-ed5.4): drive each comma-separated
    query through the live search box exactly as its final keystroke would
    (setText -> textChanged -> _search_changed, synchronously) and print one
    machine-readable {"event": "search", "query", "ms", "hits"} line per
    query for the CI/dev harness. `ms` is _search_changed end to end (see
    its docstring comment for what that covers). The box is cleared between
    queries — a repeated identical query would otherwise not re-fire
    textChanged — and left empty afterwards. Blank segments are skipped, so
    a trailing comma is harmless."""
    events: list[dict] = []
    for q in (t.strip() for t in spec.split(",")):
        if not q:
            continue
        win.search.setText("")
        win.search.setText(q)
        ev = {"event": "search", "query": q,
              "ms": round(win.last_search_ms, 3),
              "hits": win.last_search_hits}
        print(json.dumps(ev), flush=True)
        events.append(ev)
    win.search.setText("")
    return events


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("library", nargs="?", default=None,
                    help="library root to browse (read-only). Default: the "
                         "bundled synthetic library in a source checkout; in "
                         "a frozen build, the library you last opened, or one "
                         "you pick on first run")
    ap.add_argument("--thumbs", type=Path, default=None,
                    help="adopt an existing .fcache instead of building "
                         "one (e.g. cache/benchmark-thumbs.fcache)")
    ap.add_argument("--cache-root", type=Path, default=None,
                    help="where the app keeps its own disposable caches "
                         "(default: <repo>/cache/tracer-cache in a checkout, "
                         "a per-user cache dir when run as a frozen bundle)")
    ap.add_argument("--rebuild", action="store_true",
                    help="ignore any existing tracer cache and rebuild")
    ap.add_argument("--promote", action="store_true",
                    help="promote the legacy library given as the "
                         "positional argument to a multi-root-capable "
                         "library-home in place (design §10: mint "
                         "identity, rename cache dir, header-upgrade "
                         "catalog.json — no re-walk/re-hash), print the "
                         "resulting home, and exit")
    ap.add_argument("--add-root", type=Path, default=None, metavar="PATH",
                    help="add PATH as a new watched root to the "
                         "already-promoted library-home given as the "
                         "positional argument, then exit")
    ap.add_argument("--import-picasa-watched", type=str, default=None,
                    metavar="LISTFILE",
                    help="create a fresh library-home at the positional "
                         "argument from a Picasa watched-folders list: "
                         "LISTFILE is a text file (one path per line, "
                         "'#' comments), or the literal 'registry' to "
                         "read Picasa's own watched-folders list from the "
                         "Windows registry, then exit")
    ap.add_argument("--contacts", type=Path, default=None,
                    help="Picasa contacts.xml for face names (read-only; "
                         "default: the machine-local copy under "
                         "%%LocalAppData%%\\Google\\Picasa2 when present)")
    ap.add_argument("--pal-dir", type=Path, default=None,
                    help="Picasa2Albums directory of .pal album files "
                         "(read-only; merged per spec §4 — ini wins "
                         "membership, .pal fills gaps; default: the "
                         "machine-local Picasa2Albums under "
                         "%%LocalAppData%%\\Google\\Picasa2 when present)")
    ap.add_argument("--db3", type=Path, default=None,
                    help="Picasa db3 directory for the §4 rescue import "
                         "(read-only; person-album names gap-fill contacts "
                         "the ini/contacts.xml never named; default: the "
                         "machine-local db3 under "
                         "%%LocalAppData%%\\Google\\Picasa2 when present — "
                         "use this flag for PicasaStarter relocations)")
    ap.add_argument("--min-image-size", type=_parse_image_size_arg,
                    metavar="WIDTHxHEIGHT",
                    help="ignore images smaller than WIDTHxHEIGHT during "
                         "catalog scan (for icons and thumbnails)")
    ap.add_argument("--max-image-size", type=_parse_image_size_arg,
                    metavar="WIDTHxHEIGHT",
                    help="ignore images larger than WIDTHxHEIGHT during "
                         "catalog scan (for huge source or GIS images)")
    ap.add_argument("--zoom", type=int, default=160)
    ap.add_argument("--screenshot", type=Path, default=None,
                    help="save a PNG once the viewport is fully decoded, "
                         "then quit (pairs with QT_QPA_PLATFORM=offscreen)")
    ap.add_argument("--scroll-to", type=float, default=None, metavar="FRAC",
                    help="after ready, jump to this scroll fraction (0-1)")
    ap.add_argument("--open", type=int, default=None, metavar="N",
                    help="after ready, open the viewer on the Nth photo "
                         "of the current view (screenshot testing)")
    ap.add_argument("--quit-after-ready", action="store_true",
                    help="exit right after the READY line (perf probe)")
    ap.add_argument("--search-probe", type=str, default=None, metavar="TERMS",
                    help="after READY, run each comma-separated query "
                         "through the search box, print a machine-readable "
                         '{"event":"search",...} line per query, then quit '
                         "(§7 latency probe; offscreen-safe; a query may "
                         "contain spaces and -negations)")
    ap.add_argument("--finish-build", action="store_true",
                    help="scripted runs: wait for an in-flight cache "
                         "build before quitting (warm-run scripting)")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="scripted runs (--screenshot/--quit-after-ready) "
                         "abort with exit 1 after this many seconds")
    args = ap.parse_args()
    if args.cache_root is None:
        args.cache_root = _default_cache_root()

    # Wire diagnostics before anything that can log: the rotating log file
    # lives beside the per-library caches (top-level, like config.json), so a
    # console=False build still has a record of warnings, Qt messages, and
    # uncaught tracebacks (fauxcasa-pqw).
    applog.setup(args.cache_root)

    # Multi-root management actions (bead .d, design §10): each is a
    # standalone on-disk operation, never the normal open — see the
    # docstrings on _cmd_promote/_cmd_add_root/_cmd_import_picasa_watched.
    if args.promote:
        return _cmd_promote(args.library, args.cache_root)
    if args.add_root is not None:
        return _cmd_add_root(args.library, args.cache_root, args.add_root)
    if args.import_picasa_watched is not None:
        return _cmd_import_picasa_watched(args.library,
                                          args.import_picasa_watched)

    root = _resolve_library(args.library, args.cache_root)
    if root is None:
        return 2

    min_w, min_h = args.min_image_size or (0, 0)
    max_w, max_h = args.max_image_size or (0, 0)
    scan_filter = ScanFilter(min_width=min_w, min_height=min_h,
                             max_width=max_w, max_height=max_h)

    # Per-library File Types choice (fauxcasa-v46.4): the persisted
    # excluded set drives the effective walk set for every scan below AND
    # folds into the cache identity exactly like the scan filter — a
    # changed extension set is its own cache dir; the default ("" key)
    # keeps existing caches binding, and re-enabling a type returns to
    # the earlier warm cache.
    excluded_exts = load_excluded_exts(args.cache_root, root)
    exts = effective_exts(excluded_exts)
    if excluded_exts:
        log.info("file types: excluding %s (Tools > File Types)",
                 ", ".join(sorted(excluded_exts)))

    # Face names from the machine-local contacts.xml (fauxcasa-cam.2):
    # an explicit --contacts wins, else the Picasa AppData default when
    # present. Read-only enrichment — absence or garbage is never fatal,
    # but an explicitly named file that yields nothing is worth a warning.
    contacts: dict[str, str] = {}
    contacts_path = args.contacts or default_contacts_xml()
    if contacts_path is not None:
        contacts = load_contacts_xml(contacts_path)
        if contacts:
            log.info("contacts: %d names from %s", len(contacts),
                     contacts_path)
        elif args.contacts is not None:
            log.warning("no contacts loaded from %s", contacts_path)

    # Picasa2Albums .pal files (fauxcasa-cam.8): an explicit --pal-dir wins,
    # else the machine-local Picasa2Albums default when present. Same
    # read-only-enrichment posture as contacts.xml — but an explicitly
    # named directory that doesn't exist earns a warning, not silence.
    pal_dir = args.pal_dir or default_pal_dir()
    if pal_dir is not None and not pal_dir.is_dir():
        if args.pal_dir is not None:
            log.warning("--pal-dir %s is not a directory; ignored", pal_dir)
        pal_dir = None
    if pal_dir is not None:
        log.info("albums: merging .pal files from %s", pal_dir)

    # db3 rescue import (fauxcasa-cam.6/.7): an explicit --db3 wins (the
    # PicasaStarter-relocation case), else the machine-local db3 default
    # when present. Same read-only-enrichment posture as contacts.xml /
    # --pal-dir: absence is never fatal, an explicitly named directory
    # that doesn't exist earns a warning, not silence.
    db3_dir = args.db3 or default_db3_dir()
    if db3_dir is not None and not db3_dir.is_dir():
        if args.db3 is not None:
            log.warning("--db3 %s is not a directory; ignored", db3_dir)
        db3_dir = None
    if db3_dir is not None:
        log.info("db3: rescue import from %s", db3_dir)

    # Data prep. Try a WARM start first: load the persisted catalog (no
    # walk) and bind it to the thumbnail cache. Else fall back to a COLD
    # walk + build (or adopt an external --thumbs cache). The cache dir is
    # always derived from the library root, so even an adopted-cache run
    # persists its catalog and warm-starts next time.
    adopt = args.thumbs is not None
    # An implicit legacy single-root open keys on the resolved path itself
    # (multiroot .c, design §7) — reproduces every existing cache dir's
    # digest exactly; main.py doesn't open explicit multi-root libraries
    # yet (that's bead .d/.g), so this is always the legacy key here.
    cache_dir = cache_dir_for(str(root.resolve()), args.cache_root,
                              scan_filter.cache_key()
                              + exts_cache_key(excluded_exts))
    cat_path = cache_dir / "catalog.json"
    thumbs_path = args.thumbs if adopt else cache_dir / "thumbs.fcache"

    catalog: Catalog | None = None
    thumbs: ThumbCache | None = None
    build_dir: Path | None = None
    warm = False

    t_prep = time.perf_counter()
    if not args.rebuild:
        loaded = load_catalog(cat_path, root)
        if loaded is not None and thumbs_path.is_file():
            try:
                cached = load_cache(thumbs_path)
                bind(cached, loaded)
                catalog, thumbs, warm = loaded, cached, True
                # the report was persisted beside the catalog at scan time;
                # re-attach it so the status-bar count survives warm starts
                catalog.report = load_report(cache_dir / REPORT_NAME)
            except (CacheError, OSError) as e:
                log.warning("persisted cache unusable (%s); rescanning", e)

    if catalog is None:  # cold path
        catalog = scan_library(root, scan_filter, contacts, pal_dir,
                               exts=exts, db3_dir=db3_dir)
        if adopt:
            try:
                thumbs = load_cache(args.thumbs)
                bind(thumbs, catalog)
            except (CacheError, OSError) as e:
                log.error("cannot adopt %s: %s", args.thumbs, e)
                return 2
            # The indexer never ran, so signals + in-file metadata are
            # pending: record that, and MainWindow starts the background
            # backfill pass (fauxcasa-cam.12) which persists as it goes.
            catalog.backfill_state = BACKFILL_NOT_STARTED
            # Same transient-reader hazard as the build-thread saves
            # (fauxcasa-cam.18) — and uncaught here it would crash startup.
            save_catalog_retrying(catalog, cat_path)  # warm-start next time
            save_report(catalog.report, cache_dir / REPORT_NAME)
        else:
            build_dir = cache_dir  # the build thread persists the catalog

    if adopt and thumbs is not None and thumbs.library \
            and Path(thumbs.library).resolve() != root:
        # bind() compares the full path lists, so a library mismatch with
        # identical walks is survivable — but say so loudly.
        log.warning("adopted cache was built for %r, not %r — entry paths "
                    "match, but thumbnails may be from another library",
                    thumbs.library, str(root))

    prep_ms = (time.perf_counter() - t_prep) * 1000.0
    mode = "warm-load" if warm else ("adopt" if adopt else "cold-walk")
    log.info("%s: %d photos, %d folders, %d albums in %.0f ms",
             mode, len(catalog.photos), len(catalog.folders),
             len(catalog.albums), prep_ms)
    if catalog.report.entries:
        # §4: conflicts are surfaced, never silent — the applog line plus
        # the status-bar count are the minimal v1 surface (full inspector
        # is N7/M2). Details live in the persisted import-report.json.
        log.info("import report: %s", catalog.report.summary())

    # A frozen first-run picker may already have created the app.
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(APP_NAME)
    win = MainWindow(catalog, thumbs, cache_dir, build_dir, scan_filter,
                     warm=warm, adopt=adopt, cache_root=args.cache_root,
                     contacts=contacts, pal_dir=pal_dir,
                     excluded_exts=excluded_exts,
                     thumbs_path=args.thumbs, db3_dir=db3_dir)
    if args.zoom != 160:
        win.grid.set_zoom(args.zoom)  # direct: skip the slider debounce
        win.zoom.setValue(args.zoom)
    win.show()

    # READY instrumentation (§7 cold start): poll until every visible
    # tile is decoded, then report cold start + RSS on stdout.
    state = {"scrolled": False, "shot": False, "opened": False,
             "probed": False}

    def may_quit() -> bool:
        if not args.finish_build:
            return True
        if win.index_busy():
            return False
        if win.build_failed:
            # --finish-build promised a cache; a failed build must not
            # masquerade as a green run.
            log.error("cache build failed under --finish-build")
            app.exit(1)
            return False
        return True

    def check_ready() -> None:
        if not win.grid.all_visible_decoded():
            return
        if not win.ready_reported:
            win.ready_reported = True
            cold_ms = (time.perf_counter() - T0) * 1000.0
            rss, hwm = read_rss_mb()
            # §7 catalog-size row (fauxcasa-ed5.3): the persisted
            # catalog.json's on-disk bytes, normalized per photo. 0 = not
            # yet persisted (a cold build writes it when the index lands).
            # KNOWN TENSION (fauxcasa-1jb): the ~50 B/photo budget is
            # oracle-derived, and the tracer's JSON rows (rel + sha256
            # alone are ~100 chars) do not meet it — this field MEASURES
            # the row honestly; it does not claim the budget.
            try:
                cat_bytes = cat_path.stat().st_size
            except OSError:
                cat_bytes = 0
            print("READY", flush=True)
            print(json.dumps({
                "event": "ready",
                "cold_start_ms": round(cold_ms),
                "prep_ms": round(prep_ms),
                "warm": warm,
                "photos": len(catalog.photos),
                "visible_photos": catalog.visible_count,
                "folders": len(catalog.folders),
                "albums": len(catalog.albums),
                "catalog_bytes": cat_bytes,
                "catalog_bytes_per_photo": round(
                    cat_bytes / max(1, len(catalog.photos)), 1),
                "vm_rss_mb": round(rss, 1),
                "vm_hwm_mb": round(hwm, 1),
            }), flush=True)
            if args.quit_after_ready and args.screenshot is None \
                    and args.scroll_to is None and args.open is None \
                    and args.search_probe is None and may_quit():
                app.quit()
                return
        if args.search_probe is not None and not state["probed"]:
            # §7 search probe (fauxcasa-ed5.4): run once, right after READY,
            # then fall through to the normal scripted-quit path below —
            # --search-probe implies quit (see the bottom of check_ready).
            state["probed"] = True
            run_search_probe(win, args.search_probe)
        if args.scroll_to is not None and not state["scrolled"]:
            state["scrolled"] = True
            win.grid.scroll_to_fraction(args.scroll_to)
            return  # wait for the new viewport to decode
        if args.open is not None and not state["opened"]:
            state["opened"] = True
            display = win.grid.display
            if display:
                pos = max(0, min(len(display) - 1, args.open))
                win._open_viewer(display[pos], display, pos)
            return
        if state["opened"] and win.viewer.loading:
            return  # let the original finish loading before the shot
        if not may_quit():
            return  # --finish-build: hold the quit for the cache build
        if args.screenshot is not None and not state["shot"]:
            state["shot"] = True
            if win.grab().save(str(args.screenshot)):
                log.info("screenshot: %s", args.screenshot)
            else:
                log.error("FAILED to save screenshot to %s", args.screenshot)
                app.exit(1)
                return
            app.quit()
        elif args.quit_after_ready or args.search_probe is not None:
            app.quit()
        else:
            poll.stop()  # interactive run: instrumentation is done

    # Parented to the window it polls (fauxcasa-q6l.15): check_ready
    # dereferences win.grid, and the timeout connection forms a Python
    # reference cycle (poll -> check_ready -> poll) that kept the orphan
    # timer alive past main()'s return — an in-process caller (the test
    # suite) that later deleted the window got a stale fire into a deleted
    # GridView (rediscovered independently three times before the fix).
    poll = QTimer(win)
    poll.setInterval(50)
    poll.timeout.connect(check_ready)
    poll.start()
    # Hard stop for scripted runs: a stuck decode must fail loudly, not
    # hang CI or masquerade as success.
    if args.screenshot is not None or args.quit_after_ready \
            or args.search_probe is not None:
        def on_timeout() -> None:
            log.error("TIMEOUT after %ss — ready=%s state=%s",
                      args.timeout, win.ready_reported, state)
            app.exit(1)

        QTimer.singleShot(int(args.timeout * 1000), on_timeout)

    code = app.exec()
    poll.stop()  # belt to the parenting suspenders: never fire post-exec
    win.shutdown()  # reap any in-flight cache build cleanly
    rss, hwm = read_rss_mb()
    print(json.dumps({"event": "exit", "vm_rss_mb": round(rss, 1),
                      "vm_hwm_mb": round(hwm, 1)}), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
