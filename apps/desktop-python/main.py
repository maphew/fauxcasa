#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6", "rawpy", "exiv2", "pillow", "pi-heif", "av", "zstandard"]
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

Persistence readers/writers (per-user config, per-library view prefs,
window geometry, stars) live in librarystate.py — see its docstring.
"""

from __future__ import annotations

import calendar
import importlib
import json
import os
import platform
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

T0 = time.perf_counter()

# P3 finding: an exact-match check (not "in") -- the broker fully
# controls this cmdline, but a substring/membership check would also
# fire for a file literally named "--decode-worker" opened via
# drag-drop/file association, turning the GUI into a pipe-reading worker.
if sys.argv[1:] == ["--decode-worker"]:
    # Windows AppContainer decode worker re-entry (fauxcasa-ez2.9 Stage 1,
    # frozen-bundle P0 finding): a FROZEN bundle spawns its OWN exe as
    # [sys.executable, "--decode-worker"] (decodesvc_win.resolve_worker_
    # python()'s frozen branch + WinSandboxWorker.spawn()) -- sys.executable
    # IS this app, so dispatch to the worker entrypoint BEFORE the heavy
    # app-layer imports below (~200ms wall per the audit measurement) and
    # before argparse could reject the flag. Mirrors the existing
    # videostream `--worker` re-entry pattern. decodesvc_worker_win imports
    # only PySide6.QtCore/QtGui itself (the ~72ms QtGui-offscreen cost the
    # design doc's spawn budget assumes) -- never the rest of this module.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import decodesvc_worker_win

    sys.exit(decodesvc_worker_win.worker_entrypoint())

from PySide6.QtCore import (
    QByteArray,
    QObject,
    QProcess,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QIcon,
    QKeySequence,
    QPainter,
    QPalette,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QStyle,
    QStyleOption,
    QToolBar,
    QToolButton,
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
    LEGACY_ROOT_ID,
    REPORT_NAME,
    Catalog,
    Drift,
    LibraryRoot,
    Photo,
    ScanFilter,
    default_contacts_xml,
    default_pal_dir,
    format_date_taken,
    format_file_size,
    format_geotag,
    load_catalog,
    load_contacts_xml,
    load_report,
    reconcile_walk,
    save_catalog_retrying,
    save_report,
    scan_library,
    stat_sig,
)
from db3rescue import default_db3_dir  # noqa: E402
from filetypes import (  # noqa: E402
    FileTypesDialog,
    effective_exts,
    exts_cache_key,
    load_excluded_exts,
    save_excluded_exts,
)
from grid import (  # noqa: E402
    CompositeThumbCache,
    GridView,
    _date_sort_key,
    folder_key,
)
import cli  # noqa: E402
import icons  # noqa: E402
from inspector import InspectorPanel  # noqa: E402
import keymap  # noqa: E402
import library  # noqa: E402
import sidebar  # noqa: E402
import volumes  # noqa: E402
from thumbcache import (  # noqa: E402
    THUMB_EDGE,
    CacheError,
    ThumbCache,
    backfill_catalog,
    bind,
    build_cache,
    cache_dir_for,
    fcache_name,
    load_cache,
)
from peek import PeekPage  # noqa: E402
from slideshow import SlideshowPage  # noqa: E402
import theme  # noqa: E402
from starstore import (  # noqa: E402
    STAR_OVERRIDES_NAME,
    apply_star_overrides,
    load_star_overrides,
    photo_key,
    save_star_overrides,
)
from tray import SelectionTray  # noqa: E402
from viewer import ViewerPage  # noqa: E402

# Re-export block (fauxcasa-4tu): these names used to be DEFINED in this
# module and moved to librarystate.py; this import is the compatibility
# surface tests and scripts rely on when they reach for main.<name> or
# `from main import <name>` — no other call site in this file should
# import librarystate a second time.
from librarystate import (  # noqa: E402
    _config_path,
    _default_window_size,
    _is_filesystem_root,
    _library_config_path,
    _migrate_library_state,
    _remember_library,
    _remembered_library,
    library_state_dir,
    load_folder_view,
    load_sort_modes,
    load_star_min,
    load_window_geometry,
    save_folder_view,
    save_sort_modes,
    save_star_min,
    save_window_geometry,
)
# Same re-export contract as the librarystate block above, for the names
# fauxcasa-4tu stage 2 moved to cli.py: main.<name>/`from main import
# <name>` compatibility for tests and scripts.
from cli import (  # noqa: E402
    _parse_image_size_arg,
    run_search_probe,
    select_sidebar_view,
)
# Same re-export contract again, for the names fauxcasa-4tu stage 3 moved
# to sidebar.py. ElidingLabel is NOT here: it stays defined in main.py
# (MainWindow's status-bar labels use it too, not just the sidebar).
from sidebar import (  # noqa: E402
    _OFFLINE_DRIVE_NAME_MAX_CHARS,
    _offline_root_labels,
    _plain_tooltip,
    _single_root_offline_message,
)

import applog  # noqa: E402

# Human diagnostics route through this logger -> a rotating log file (so
# nothing is lost in a console=False windowed build) + a stderr mirror when a
# console exists (fauxcasa-pqw). The §7 MACHINE protocol — READY + the
# ready/exit/indexed JSON — stays on raw stdout below, never through here.
log = applog.log

# Single source of truth for the (provisional) product name — nothing
# else may hard-code it. Every user-visible name (window title, --version,
# the cache dir, the log file, the bundled exe) derives from these three
# (rel-0.1 identity, fauxcasa-ez2.3): APP_NAME for prose, APP_SLUG for
# paths/filenames, __version__ for the release stamp.
APP_NAME = "Fauxcasa"
__version__ = "0.1.0"
APP_SLUG = APP_NAME.lower()

# Optional build stamp. The release workflow generates _buildinfo.py next to
# this file (git sha + build date) and PyInstaller collects it; a source
# checkout has none, and an empty sha simply drops the "(sha)" suffix from
# --version / the READY line rather than lying about provenance.
try:
    from _buildinfo import BUILD_DATE, GIT_SHA  # type: ignore[import-not-found]
except ImportError:
    GIT_SHA = BUILD_DATE = ""


def version_string() -> str:
    """The release identity users and bug reports quote: "Fauxcasa 0.1.0",
    with the build's git sha appended when one was stamped in. One
    formatter for --version, the READY JSON and the startup log line."""
    return f"{APP_NAME} {__version__}" + (f" ({GIT_SHA})" if GIT_SHA else "")

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


def _order_starred_newest_first(cat: Catalog, idxs: list[int]) -> list[int]:
    """Newest-first ordering for the date-grouped Starred collection
    (fauxcasa-q6l.20 clause b, spec §5): reuses cam.9's date_taken/mtime
    fallback substrate (grid._date_sort_key, the same one per-folder date
    sort uses) so ordering and grouping agree. Dated photos (date_taken,
    or mtime when date_taken is absent) sort descending by that key;
    genuinely dateless photos (no date_taken, no usable mtime) keep their
    incoming catalog order and sink after every dated photo, landing in
    the trailing Undated group (see _starred_grouper). One _date_sort_key
    call per index (fauxcasa-q6l.20 review nit 11), not three."""
    keyed = [(i, _date_sort_key(cat.photos[i])) for i in idxs]
    dated = sorted((ik for ik in keyed if ik[1][0] == 0),
                   key=lambda ik: ik[1], reverse=True)
    undated = [ik for ik in keyed if ik[1][0] != 0]
    return [i for i, _k in dated] + [i for i, _k in undated]


def _starred_grouper(cat: Catalog, i: int) -> tuple[str, str, str | None]:
    """set_filter `grouper` for the Starred collection (fauxcasa-q6l.20
    clause b): month buckets ('YYYY-MM', title "September 2026") from the
    same date substrate _order_starred_newest_first sorts by, so grouping
    and ordering agree; genuinely dateless photos share one trailing
    'undated' bucket titled "Undated". No description (the header already
    paints the per-group count; repeating it there would be redundant).

    Deliberately never round-trips through datetime.strptime: date_taken's
    year is UNBOUNDED (§6 footgun 16 — metareader applies no year floor),
    so a 5+-digit year or an all-zero EXIF placeholder like
    "0000-05-01T..." both occur in real libraries and the LATTER must
    group cleanly (May of year zero is still a real month bucket) while
    strptime raises on it depending on platform libc. A plain slice +
    guard handles both: a well-formed 4-digit year groups by month, and
    anything that doesn't fit that shape (a 5-digit year included) sinks
    to Undated rather than raising."""
    sort_key = _date_sort_key(cat.photos[i])
    if sort_key[0] == 0:
        s = sort_key[1]
        y, m = s[:4], s[5:7]
        if s[4:5] == "-" and m.isdigit() and 1 <= int(m) <= 12:
            return f"{y}-{m}", f"{calendar.month_name[int(m)]} {y}", None
    return "undated", "Undated", None


APP_DIR = Path(__file__).resolve().parent
REPO = APP_DIR.parents[1]
FROZEN = getattr(sys, "frozen", False)

# Scripted --open + --screenshot runs wait this long for the viewer's
# original to decode before shooting anyway (with a diagnostic), so a
# wedged decode is reported, not silently timed out.
OPEN_WAIT_MS = 10_000

# Status-bar dwell for a "not implemented yet" notice (fauxcasa-s6i): long
# enough to read the milestone it names, short enough to clear before the
# next counts readout matters.
NOTICE_MS = 8_000

# Application icon (rel-0.1). Wordless on purpose: APP_NAME is provisional,
# so the mark must never bake the name into pixels. Source of truth is
# assets/icon.svg; assets/make-icons.py rasterizes the PNG set + .ico that
# the runtime (app_icon) and the PyInstaller spec (EXE icon=, datas)
# consume. Keep in lockstep with make-icons.SIZES.
ICON_SIZES = (16, 32, 48, 64, 128, 256)


def asset_path(name: str) -> Path:
    """apps/desktop-python/assets/<name> in a source checkout, or the same
    file inside the frozen bundle: the spec's datas entries copy assets/
    to sys._MEIPASS/assets, so both layouts resolve through this one seam
    (the _default_cache_root pattern — APP_DIR/REPO point into the
    read-only bundle when frozen, and _MEIPASS is PyInstaller's documented
    contract for where datas land)."""
    base = Path(getattr(sys, "_MEIPASS", APP_DIR)) if FROZEN else APP_DIR
    return base / "assets" / name


def app_icon() -> QIcon:
    """The window/taskbar icon, assembled from the pre-rendered PNG set so
    Qt hands each surface the size rasterized FOR it (16 title bar, 32
    taskbar, 48+ Alt-Tab, 256 hi-DPI) instead of a downscale — and needs
    no QtSvg at runtime (the bundle excludes it). A missing file drops
    that size only; an all-missing set yields a null icon and Qt's
    generic fallback, never a crash."""
    icon = QIcon()
    for px in ICON_SIZES:
        path = asset_path("icon.png" if px == 256 else f"icon-{px}.png")
        if path.is_file():
            icon.addFile(str(path), QSize(px, px))
    return icon


def _set_windows_app_user_model_id() -> None:
    """Windows taskbar identity. Without an explicit AppUserModelID the
    shell groups our windows under the HOST process (python.exe from
    source, the PyInstaller bootloader when frozen) and pins/jump lists
    carry that exe's icon, not ours. Must run BEFORE the first
    QApplication exists — Qt registers the window class at construction.
    Derived from APP_NAME so a rename changes it in exactly one place; a
    no-op off Windows."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            f"{APP_NAME}.Desktop")
    except (AttributeError, OSError) as exc:  # no shell32 (Wine, odd hosts)
        log.debug("AppUserModelID not set: %s", exc)


def _default_cache_root() -> Path:
    """REPO-relative in a source checkout; a per-user writable dir when
    frozen — REPO then points inside the read-only PyInstaller bundle, so
    the app's own disposable cache must go somewhere writable instead."""
    if not FROZEN:
        return REPO / "cache" / f"{APP_SLUG}-cache"
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / APP_NAME / "cache"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / APP_SLUG


def _default_library() -> Path | None:
    """The built-in library to open with no argument. A source checkout
    ships the synthetic fixture library; a frozen bundle ships none (REPO
    points inside the read-only bundle), so there is no default — the app
    recalls the last-opened library or prompts on first run instead."""
    if FROZEN:
        return None
    return REPO / "cache" / "synthetic-library"


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


class WelcomeDialog(QDialog):
    """First-run welcome (fauxcasa-ez2.14): replaces the bare folder
    picker with the app icon, APP_NAME, and a one-paragraph read-only
    promise, then one or two ways to pick a library. `watched_count` is
    the number of Picasa watched folders found to still exist on disk —
    the "Use Picasa's watched folders" button only appears when it is
    >= 1, since offering to import zero folders is a dead end.

    Not exec()'d by this class itself — callers (_prompt_for_library,
    tests) call exec() or drive .choice programmatically, matching the
    keyboard-shortcuts/about dialogs' pattern elsewhere in this module.
    `.choice` is None until a button is clicked: "picasa" or "folder"."""

    def __init__(self, watched_count: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(APP_NAME)
        self.choice: str | None = None

        lay = QVBoxLayout(self)
        header = QHBoxLayout()
        icon_label = QLabel()
        icon_label.setPixmap(app_icon().pixmap(64, 64))
        header.addWidget(icon_label)
        title = QLabel(f"<span style='font-size: 16pt;'>{APP_NAME}</span>")
        header.addWidget(title)
        header.addStretch(1)
        lay.addLayout(header)

        para = QLabel(
            "Fauxcasa browses your existing photo folders read-only. It "
            "never modifies photos, .picasa.ini files or the Picasa "
            "database; it only writes its own cache.")
        para.setWordWrap(True)
        lay.addWidget(para)

        self.picasa_button: QPushButton | None = None
        if watched_count >= 1:
            self.picasa_button = QPushButton(
                f"Use Picasa's watched folders ({watched_count} found)")
            self.picasa_button.clicked.connect(self._pick_picasa)
            lay.addWidget(self.picasa_button)

        self.folder_button = QPushButton("Choose a folder…")
        self.folder_button.clicked.connect(self._pick_folder)
        lay.addWidget(self.folder_button)

        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        lay.addWidget(cancel_button)

    def _pick_picasa(self) -> None:
        self.choice = "picasa"
        self.accept()

    def _pick_folder(self) -> None:
        self.choice = "folder"
        self.accept()


def _existing_picasa_watched_count() -> int:
    """How many of Picasa's registry-listed watched folders still exist on
    disk — the WelcomeDialog's gate for offering the one-click import.
    Fails soft to 0 (no registry entry, wrong platform, or a vanished
    key) exactly like _db3_rescue_enabled's own registry read."""
    try:
        watched = library.picasa_watched_from_registry()
    except RuntimeError:
        return 0
    return sum(1 for w in watched if w.is_dir())


def _import_picasa_watched_for_welcome(cache_root: Path) -> Path | None:
    """The WelcomeDialog's "Use Picasa's watched folders" action: build a
    fresh multi-root library-home from the registry list, entirely inside
    our own cache_root (never touching Picasa's own folders/database —
    the same promise the welcome paragraph makes), reusing
    library.import_picasa_watched exactly as --import-picasa-watched
    registry does. Returns the new library-home path, or None if the
    registry yields nothing usable (fails soft; caller falls back to the
    folder picker)."""
    try:
        folders = library.picasa_watched_from_registry()
    except RuntimeError as e:
        log.error(str(e))
        return None
    home = (cache_root / "picasa-watched-library").expanduser().resolve()
    skipped: list[str] = []
    cfg = library.import_picasa_watched(
        folders, home, name="Picasa Watched Folders", skipped=skipped)
    for msg in skipped:
        log.warning("picasa import: skipped %s", msg)
    if not cfg.roots:
        log.error("no usable Picasa watched folders found — nothing imported")
        return None
    library.save_library(cfg)
    for r in cfg.roots:
        library.write_root_marker(r.path.resolve(), r.id)  # fail-soft
    return cfg.home


def _prompt_for_library(cache_root: Path) -> Path | None:
    """First-run picker for a frozen build with no library yet: show the
    WelcomeDialog and act on the user's choice. Returns None if cancelled
    — or, under a headless/offscreen platform, immediately, rather than
    blocking forever on a modal dialog nobody can answer."""
    # Pre-construction guard: bail BEFORE touching QApplication so a Linux
    # no-DISPLAY launch can't abort the whole process (see _gui_unavailable).
    if _gui_unavailable():
        return None

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    app.setPalette(theme.dark_palette())
    app.setWindowIcon(app_icon())  # the picker dialog is our first window
    # Backstop: an in-process headless platform (e.g. forced offscreen with a
    # DISPLAY present) still can't show a modal — keep this post-construction
    # guard too.
    if app.platformName() in ("offscreen", "minimal", ""):
        return None

    dlg = WelcomeDialog(_existing_picasa_watched_count())
    if dlg.exec() != QDialog.DialogCode.Accepted or dlg.choice is None:
        return None

    if dlg.choice == "picasa":
        chosen = _import_picasa_watched_for_welcome(cache_root)
    else:
        chosen = _choose_library_from_dialog(cache_root)
    if chosen is not None:
        _remember_library(cache_root, chosen)
    return chosen


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


def _paths_related(a: Path, b: Path) -> bool:
    """True when `a` and `b` are the same folder, or one contains the
    other — case-insensitive and normalized (fauxcasa-ez2.13), since a
    Windows watched-folder path from the registry and an opened library
    root can differ only in case or trailing separator."""
    def _norm(p: Path) -> str:
        return str(p.resolve()).rstrip("\\/").lower()
    a_n, b_n = _norm(a), _norm(b)
    return (a_n == b_n or a_n.startswith(b_n + os.sep)
            or b_n.startswith(a_n + os.sep))


def _db3_rescue_enabled(root: Path, explicit: bool) -> tuple[bool, str]:
    """Whether to run the db3/.pal machine-local rescue import for the
    opened `root` (fauxcasa-ez2.13). --db3/--pal-dir default to THIS
    machine's Picasa2 AppData regardless of which library is open, so
    without a gate, opening any folder on a machine that once ran Picasa
    yields a report full of db3_path_unresolved noise about some other
    library entirely. An explicit --db3/--pal-dir always wins (the
    PicasaStarter-relocation case); otherwise the rescue only runs when
    `root` equals, contains, or is contained by one of Picasa's own
    watched roots (registry, Windows-only, fail-soft when absent/
    unsupported). Returns (enabled, reason) — the caller logs the reason
    either way."""
    if explicit:
        return True, "explicit --db3/--pal-dir"
    try:
        watched = library.picasa_watched_from_registry()
    except RuntimeError:
        return False, "no Picasa watched-folders registry entry found"
    if any(_paths_related(root, w) for w in watched):
        return True, "root overlaps a Picasa watched folder"
    return False, f"{root} does not overlap any Picasa watched folder"


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


def _cmd_promote(library_arg: str | None, cache_root: Path) -> int:
    """--promote: promote the legacy library named by the positional
    `library` argument in place (design §10). Reuses _resolve_library for
    the same existence/filesystem-root validation every other invocation
    gets, so a bad path fails exactly the same way it would on a normal
    open. An explicit `library` is REQUIRED (same rule
    _cmd_import_picasa_watched enforces): without one, _resolve_library
    would silently fall through to _default_library() and promote the
    built-in sample library in place."""
    if not library_arg:
        log.error("--promote requires a library path as the positional "
                  "argument")
        return 2
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


def _reconcile_online_roots(
        catalog: Catalog, scan_filter: ScanFilter | None, cancel,
        exts: frozenset[str] | set[str] | None,
        contacts_path: Path | None = None,
        ) -> tuple[Drift | None, list[str]]:
    """Per-online-root reconcile (fauxcasa-ed5.7.5, bead .e, design §8/§9).

    `reconcile_walk` is a single-root primitive by design (its own
    docstring: "looping over all online roots is bead .e's job, out of
    this function's scope") — this is that job. Loop over
    `catalog.online_roots()` only, run `reconcile_walk` per root keyed on
    that root's id, and aggregate the results into one Drift. THE RULE
    (design §8, the load-bearing offline-tolerance invariant): an offline
    root is never passed to `reconcile_walk` at all, so nothing in its
    catalog slice can ever be diffed against a missing directory — an
    unplugged drive's photos are never counted as removed.

    `catalog.roots` is normally non-empty (scan_library/load_catalog both
    populate it, even for the single implicit legacy root, id ""); the
    `or` fallback below only matters for a hand-built Catalog fixture that
    never set `roots` — treated as that one implicit root at `catalog.root`
    so legacy single-root behavior is untouched either way.

    Returns `(None, [])` if a cancel fires mid-walk on any root (same
    None-means-cancelled contract as reconcile_walk itself — the caller's
    existing `drift is None` check keeps working unmodified). Otherwise
    returns `(aggregated Drift, offline root labels)` — the labels are for
    the caller's status-bar text; whether to act on `Drift.changed` is
    still the caller's call, same as before this bead.

    `contacts_path` (fauxcasa-cam.14 step 4): the machine-local
    contacts.xml's freshness is checked HERE, ONCE for the whole library
    — not inside the per-root reconcile_walk loop below — because
    contacts.xml is a single machine-local file, not a per-root artifact;
    checking it once per root would just repeat the same stat N times and
    (for a multi-root library) fold the SAME drift into every root's
    result. None (the default) skips the check entirely, matching every
    existing call site that never threaded a contacts path through."""
    catalog.refresh_offline_ids()
    # NOTE (hi2 item 6): the WALK loop's online set stays inline rather
    # than calling catalog.online_roots() directly. That helper filters
    # `self.roots` as-is, so on a hand-built Catalog fixture that never set
    # `roots` (empty list, per the docstring above) it would return []
    # and this loop would silently reconcile NOTHING instead of falling
    # back to the one implicit legacy root — a real behavior change, not
    # just a refactor. The `roots` local below applies that fallback
    # FIRST, then filters it the same way online_roots() does, so the two
    # stay in lockstep whenever `catalog.roots` is actually populated (the
    # normal case). The returned LABELS, below, are a separate concern —
    # they go through `_offline_root_labels(catalog)` instead, which reads
    # `catalog.roots`/`offline_roots()` directly; see that call for why
    # that is fine even for the bare-fixture case this loop's fallback
    # exists for.
    roots = catalog.roots or [LibraryRoot(id=LEGACY_ROOT_ID, path=catalog.root)]
    online = [r for r in roots if r.id not in catalog.offline_ids]

    total = Drift()
    for r in online:
        d = reconcile_walk(catalog, r.path, scan_filter, cancel=cancel,
                           exts=exts, root_id=r.id)
        if d is None:
            return None, []  # cancelled mid-walk
        total.added += d.added
        total.removed += d.removed
        total.modified += d.modified
        total.ini_changed = total.ini_changed or d.ini_changed

    # The single machine-local contacts.xml check (see the docstring
    # above): folded into the same `ini_changed` flag reconcile_walk
    # already uses for per-root ini drift, since both trigger the exact
    # same rebuild/re-resolve path in the caller (fauxcasa-cam.14 step 4 —
    # also the PR-37 rider: editing contacts.xml now refreshes a warm
    # start instead of never being noticed).
    if stat_sig(contacts_path) != catalog.contacts_sig:
        total.ini_changed = True

    # fauxcasa-hi2 item 4/6: reuse _offline_root_labels rather than
    # deriving a second label list from the `roots`/`online` split above,
    # so the single-root suppression it applies (a single-root library has
    # no badge to show — see its docstring) can only ever live in ONE
    # place. That split stays the walk loop's own concern (the function
    # docstring explains why: the `roots` fallback for a bare-fixture
    # Catalog that never set `roots`) — it is not reused here because a
    # bare-fixture catalog leaves `catalog.roots` empty, which
    # `_offline_root_labels` reads directly, and `refresh_offline_ids()`
    # above only ever populates `offline_ids` from `catalog.roots` too —
    # so the fixture case naturally yields [] from both, in lockstep.
    return total, _offline_root_labels(catalog)


class ElidingLabel(QLabel):
    """A QLabel whose MINIMUM width does not track its text (fauxcasa-a3m).

    QLabel.minimumSizeHint() returns the full sizeHint when word wrap is
    off, and QMainWindow enforces its layout's minimum as a hard resize
    floor. So a status-bar label holding one deep path silently became
    the window's minimum width: selecting a photo under a long folder
    name pushed the floor past 2700px, and from then on the user could
    drag the window bigger but never smaller. Worse, the floor is
    enforced on the NEXT relayout, so the activity row appearing yanked
    the window wider on its own.

    Eliding at paint time fixes both ends without hiding anything:

    - text() still returns the string the caller set, so status wording
      stays readable by callers and tests (nothing downstream has to
      know a label elides);
    - sizeHint() is untouched, so the layout still asks for the full
      width whenever there is room and nothing elides at all;
    - minimumSizeHint() collapses to a fixed ellipsis-width stub, so
      the text can never move the window's floor;
    - the full string goes to the tooltip while it is elided, so the
      part that got cut is a hover away rather than lost.

    The custom paint deliberately mirrors QLabel's own geometry and text
    drawing rather than improvising (QLabelPrivate::layoutRect and
    QLabel::paintEvent): visual (not logical) alignment so a
    right-to-left layout is not silently mirrored, QLabel's real
    default-indent rule, QFrame's frame, and QStyle.drawItemText so the
    disabled palette and a style sheet's color: are honoured the way
    they are on every other label. Only the elided case takes this
    path; text that fits is painted by QLabel itself.

    Tooltip text goes through _plain_tooltip for the usual reason
    (fauxcasa-6vk finding 7): these labels carry catalog text - paths,
    captions, keywords, album and people names - and a caption of
    "<img src=http://...>" would otherwise be INTERPRETED by the
    tooltip's AutoText QLabel. A tooltip the OWNER set always wins
    (decode_sandbox_label shows the degrade reason there); only a
    tooltip this widget installed itself is ever replaced or cleared.
    """

    def __init__(self, parent: QWidget | None = None, *,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight
                 ) -> None:
        super().__init__(parent)
        self._elide_mode = mode
        # The tooltip WE last installed, so an owner-set tooltip is
        # never clobbered and ours is never left behind stale.
        self._own_tooltip = ""
        # Same PlainText discipline as the QLabels this replaces
        # (fauxcasa-6vk finding 7) - set here so no call site can
        # forget it.
        self.setTextFormat(Qt.TextFormat.PlainText)

    # ---------- geometry (QLabelPrivate::layoutRect) ----------

    def _visual_alignment(self) -> int:
        return int(QStyle.visualAlignment(self.layoutDirection(),
                                          self.alignment()))

    def _indent(self) -> int:
        """QLabel's rule: an explicit indent wins; otherwise a FRAMED
        label gets half an 'x' and an unframed one gets nothing."""
        if self.indent() >= 0:
            return self.indent()
        if self.frameWidth():
            return (self.fontMetrics().horizontalAdvance("x") // 2
                    - self.margin())
        return 0

    def _text_rect(self):
        rect = self.contentsRect()
        m = self.margin()
        rect = rect.adjusted(m, m, -m, -m)
        indent = self._indent()
        if indent > 0:
            align = self._visual_alignment()
            if align & Qt.AlignmentFlag.AlignLeft:
                rect.setLeft(rect.left() + indent)
            if align & Qt.AlignmentFlag.AlignRight:
                rect.setRight(rect.right() - indent)
            if align & Qt.AlignmentFlag.AlignTop:
                rect.setTop(rect.top() + indent)
            if align & Qt.AlignmentFlag.AlignBottom:
                rect.setBottom(rect.bottom() - indent)
        return rect

    # ---------- the fix ----------

    def minimumSizeHint(self) -> QSize:
        base = super().minimumSizeHint()
        if not self.text():
            return base
        fm = self.fontMetrics()
        # Room for the ellipsis plus a couple of characters, so a
        # squeezed label still reads as truncated text rather than as a
        # rendering glitch - and the chrome QLabel lays out around it.
        stub = fm.horizontalAdvance("\u2026") + 2 * fm.averageCharWidth()
        margins = self.contentsMargins()
        chrome = (margins.left() + margins.right()
                  + 2 * self.margin() + max(self._indent(), 0))
        return QSize(min(base.width(), stub + chrome), base.height())

    def _set_own_tooltip(self, tip: str) -> None:
        """Install (or withdraw) OUR tooltip without touching one the
        owner set. Skipping the no-op keeps this off the hot path: it
        runs on an elide/un-elide transition, not on every paint."""
        if self.toolTip() not in ("", self._own_tooltip):
            return                       # the owner's tooltip wins
        if self.toolTip() != tip:
            self.setToolTip(tip)
        self._own_tooltip = tip

    def paintEvent(self, event) -> None:
        rect = self._text_rect()
        text = self.text()
        if not text or self.fontMetrics().horizontalAdvance(text) \
                <= rect.width():
            self._set_own_tooltip("")
            super().paintEvent(event)
            return
        self._set_own_tooltip(_plain_tooltip(text))
        painter = QPainter(self)
        # The style paints the widget's own background/border first: a
        # QLabel under a style sheet (activity_label inherits the
        # activity row's) gets nothing otherwise.
        opt = QStyleOption()
        opt.initFrom(self)
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_Widget, opt, painter, self)
        if self.frameWidth():
            self.drawFrame(painter)
        # drawItemText, not a raw pen: it is what QLabel uses, so the
        # disabled palette and a style sheet's color: land the same way
        # here as on any other label.
        self.style().drawItemText(
            painter, rect,
            self._visual_alignment() | int(Qt.TextFlag.TextSingleLine),
            self.palette(), self.isEnabled(),
            self.fontMetrics().elidedText(
                text, self._elide_mode, rect.width()),
            self.foregroundRole())


def _scan_library_config(
        cfg, scan_filter: ScanFilter | None,
        contacts: dict[str, str], pal_dir: Path | None,
        exts, db3_dir: Path | None) -> Catalog:
    """Compose the frozen single-root ingest over manifest-ordered roots.

    Every root's photos stay a contiguous slice. Folder keys use the same
    absent-means-first convention as catalog persistence, while album member
    indices are shifted by that slice's global offset. Duplicate album
    definitions retain the established first-wins rule; memberships from all
    roots are additive, and a later real definition may fill an earlier
    placeholder because a placeholder is explicitly not a definition.

    db3 and .pal enrichment run ONCE against the composed, root-qualified
    catalog (fauxcasa-cam.21) — NOT once per root: a per-root pass would
    report every other root's db3 rows as unresolved, duplicate person
    diagnostics per root, and let two roots carrying the same rel both
    claim one .pal member. The per-root scans below therefore run bare
    (pal_dir/db3_dir None); merge_pal_albums_config and
    rescue_people_config then resolve members and db3 paths through the
    manifest's (root_id, path) pairs. unknown_album reporting moves with
    the .pal pass: a per-root scan can't know a .pal (or another root)
    resolves the uid, so its premature entries are dropped and the class
    is re-emitted once after every source has had its say — preserving
    scan_library's pal-before-unknown_album order for that class (the
    composed report's contacts-vs-pal RELATIVE order does shift; no
    consumer reads entry order — ImportReport.summary() tallies kinds).
    """
    if cfg.is_legacy:
        return scan_library(cfg.roots[0].path, scan_filter, contacts, pal_dir,
                            exts=exts, db3_dir=db3_dir)

    photos = []
    folders = {}
    albums = {}
    merged_contacts = {}
    reports = []
    # ini freshness signals (fauxcasa-cam.14 step 4): each root's
    # scan_library() call returns them keyed LEGACY_ROOT_ID (it never
    # knows the real root id) — root-qualify by re-keying onto the actual
    # root.id, same composition shape as Catalog.ini_sigs' Python type.
    ini_sigs: dict[tuple[str, str], tuple[int, int]] = {}
    identity_catalog = Catalog(
        root=cfg.roots[0].path, photos=[], folders={}, albums={},
        roots=list(cfg.roots), library_id=cfg.library_id)
    for root in cfg.roots:
        one = scan_library(root.path, scan_filter, contacts, None,
                           exts=exts, db3_dir=None)
        offset = len(photos)
        for photo in one.photos:
            photo.root_id = root.id
        photos.extend(one.photos)
        for folder in one.folders.values():
            folder.root_id = root.id
            folders[folder_key(identity_catalog, root.id, folder.rel)] = folder
        for (_rid, folder_rel), sig in one.ini_sigs.items():
            ini_sigs[(root.id, folder_rel)] = sig
        for uid, album in one.albums.items():
            shifted = [offset + i for i in album.members]
            existing = albums.get(uid)
            if existing is None:
                album.members = shifted
                albums[uid] = album
            else:
                existing.members.extend(shifted)
                if existing.placeholder and not album.placeholder:
                    existing.name = album.name
                    existing.date = album.date
                    existing.description = album.description
                    existing.placeholder = False
                    existing.pal_sourced = album.pal_sourced
        for cid, name in one.contacts.items():
            merged_contacts.setdefault(cid, name)
        reports.extend(
            e for e in one.report.entries
            if not (e.source == "ini" and e.kind == "unknown_album"))

    from catalog import ImportReport, merge_pal_albums_config
    from db3rescue import rescue_people_config
    report = ImportReport(reports)

    def _resolved(p: Path) -> Path:
        # scan_library resolves each root before walking ("root =
        # root.resolve()"), so the joiners must see the SAME spelling — a
        # symlinked or relative manifest path would otherwise never share
        # the suffix an absolute db3/.pal member path carries (PR #86
        # review). Fail-soft: an unresolvable (offline) root keeps its
        # stored spelling and simply joins nothing.
        try:
            return Path(p).resolve()
        except (OSError, RuntimeError):
            return Path(p)

    manifest_roots = [(r.id, _resolved(r.path)) for r in cfg.roots]

    if pal_dir is not None:
        merge_pal_albums_config(pal_dir, manifest_roots, photos, albums,
                                report)
    # Re-emit the class the per-root scans held back, now that the .pal
    # pass (and cross-root definitions) had their say — same text, same
    # position in the source order as scan_library's own emission.
    for uid, album in albums.items():
        if album.placeholder:
            report.add("ini", "unknown_album", uid,
                       f"albums= references uid {uid} but no [.album:{uid}] "
                       f"definition (or .pal) exists — materialized as "
                       f"placeholder “{album.name}” with "
                       f"{len(album.members)} member(s)")

    db3_contacts = set()
    if db3_dir is not None:
        db3_contacts = rescue_people_config(
            db3_dir, manifest_roots, photos, merged_contacts, report)

    return Catalog(
        root=cfg.roots[0].path,
        photos=photos,
        folders=folders,
        albums=albums,
        contacts=merged_contacts,
        db3_contacts=db3_contacts,
        report=report,
        ini_sigs=ini_sigs,
        roots=list(cfg.roots),
        library_id=cfg.library_id,
    )


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
    held: tuple[object, ...]


@dataclass
class _MultiRootIndexResult:
    """Aggregate result for one independently-built cache per root.

    A root the build skipped because it was offline (§8 tolerance —
    indexing an unreachable tree would clobber a good fcache with error
    blobs) appears as ``(root_id, None)``; `_offline_root_cache` fills
    its composite slot at bind time."""
    roots: list[tuple[str, object]]
    photos: int
    elapsed_s: float
    workers: int

    @property
    def rate(self) -> float:
        return self.photos / self.elapsed_s if self.elapsed_s else 0.0


def _offline_root_cache(catalog, root_id: str, levels: list[int],
                        cache_dir: Path | None) -> ThumbCache:
    """Composite slot for an offline root the cold build skipped: reuse
    the root's existing on-disk fcache when it still binds and shares the
    built caches' levels, else an all-error-tile placeholder (length-0
    entries render as error tiles everywhere; a zero length means no
    consumer ever reads from the placeholder's path)."""
    path = cache_dir / fcache_name(root_id) if cache_dir else None
    if path is not None and path.is_file():
        try:
            cache = load_cache(path)
            bind(cache, catalog, root_id=root_id)
            if cache.levels == levels:
                return cache
        except (CacheError, OSError):
            pass
    photos = catalog.photos_for_root(root_id)
    level_entries = [[(0, 0, 0, 0)] * len(photos) for _ in levels]
    return ThumbCache(
        path=path if path is not None else Path(fcache_name(root_id)),
        count=len(photos),
        entries=level_entries[0],
        files=[p.rel for p in photos],
        library="",
        library_id=catalog.library_id,
        sidecar_version=2,
        levels=list(levels),
        primary=0,
        level_entries=level_entries,
    )


class _BuildBridge(QObject):
    progress = Signal(int, int)            # done, total (cold build live feed)
    status = Signal(str)                   # inline status text (reconcile)
    # A TERMINAL notice: the worker has already stopped and this is all
    # there is to say (fauxcasa-6vk finding 5). Routed separately from
    # `status` because `status` means "still working" and therefore raises
    # the activity row's busy indicator, which only `finished` takes down.
    notice = Signal(str)                   # terminal notice (no more work)
    finished = Signal(object, object, bool)  # (IndexResult|None, Catalog, is_reconcile)
    backfill_done = Signal(bool)           # adopt-mode backfill: completed?
    # Non-blocking first run (fauxcasa-q6l.13): the background library WALK
    # landed a real Catalog (or None on failure) — see _start_cold_scan /
    # _on_scan_done. Carries the whole Catalog, same object-payload idiom
    # as `finished` above.
    scan_done = Signal(object)


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
                 db3_dir: Path | None = None,
                 contacts_path: Path | None = None,
                 cfg: library.LibraryConfig | None = None,
                 contacts_sig: tuple[int, int] | None = None,
                 state_dir: Path | None = None):
        super().__init__()
        self.catalog = catalog
        self.cache_dir = cache_dir
        # Where user CHOICES live (fauxcasa-6vk finding 2): the variant-free
        # per-library dir (library_state_dir), so a File-Types or scan-size
        # change — which moves cache_dir — never hides the user's stars
        # and sort modes. Defaults to cache_dir when no state dir is
        # given (tests, bench harnesses); main() always passes one.
        self.state_dir = cache_dir if state_dir is None else state_dir
        _migrate_library_state(cache_dir, self.state_dir)
        # User star choices are overlays in Fauxcasa's machine-local cache,
        # never writes into originals/.picasa.ini (the tracer's N1/N3 rule).
        self.star_overrides = load_star_overrides(self.state_dir)
        apply_star_overrides(catalog, self.star_overrides)
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
        # machine-local contacts.xml PATH (fauxcasa-cam.14 step 4, distinct
        # from `contacts` above — the already-parsed name map): kept so a
        # reconcile can re-stat it and notice an external edit even when no
        # photo file itself changed (the PR-37 rider). None when no
        # contacts.xml was configured/found at startup, same as `catalog.
        # contacts_sig` in that case.
        self.contacts_path = contacts_path
        # main()'s pre-scan stat_sig(contacts_path) (fauxcasa-cam.14 step
        # 4), captured immediately before load_contacts_xml read it — kept
        # so a deferred cold scan (fauxcasa-q6l.13, _start_cold_scan) can
        # stamp the real catalog it builds with the SAME signature main()
        # would have stamped had it scanned synchronously, never a fresh
        # re-stat after the (possibly long) walk.
        self.contacts_sig = contacts_sig
        # LibraryConfig this window was opened from (fauxcasa-q6l.13):
        # None for callers that never defer a scan (most tests); the cold
        # non-adopt startup path in main() always supplies it so
        # _start_cold_scan's worker can call _scan_library_config the same
        # way main() would have, off the startup critical path.
        self.cfg = cfg
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
        # Star-threshold predicate composing with any view (fauxcasa-q6l.20
        # clause a, spec §3/§5): 0 = "Any" (off), 1-5 = "N stars or more".
        # Loaded before the first _apply_view/_search_changed call so a
        # remembered threshold shapes the very first paint.
        self._star_min: int = load_star_min(self.state_dir)
        # Library first, product second (rel-0.1 identity): the title bar /
        # taskbar tooltip answers "which library am I in?" before it repeats
        # the app name, and carries no internal codename.
        self.setWindowTitle(f"{catalog.root.name} — {APP_NAME}")
        self.setWindowIcon(app_icon())
        # Initial geometry (fauxcasa-ez2.6 §6): size to the actual screen
        # first (min(1280, 800) baseline, scaled down on a small display),
        # then let a persisted saveGeometry() from a previous run override
        # it — but only when that saved rect is still reachable on THIS
        # screen setup (a monitor unplugged since the last run must not
        # strand the window off-screen).
        self.resize(*_default_window_size())
        saved = load_window_geometry(self.state_dir)
        if saved is not None and self.restoreGeometry(saved):
            screens = QApplication.screens()
            on_screen = any(
                s.availableGeometry().intersects(self.geometry())
                for s in screens)
            if not on_screen:
                self.resize(*_default_window_size())

        self.grid = GridView()
        # Per-folder sort modes (fauxcasa-q6l.11), loaded BEFORE the first
        # set_data/set_filter builds the display so a remembered mode shapes
        # the very first paint. cache_dir=None (tests, degraded runs) means
        # session-only modes; durable-home decision at _library_config_path.
        self.grid.sort_modes = load_sort_modes(self.state_dir)
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
        # Sidebar logic (fauxcasa-4tu stage 3): self.tree/_flat_check/
        # _sidebar_panel stay MainWindow attributes (many non-sidebar call
        # sites read them too); self._sidebar operates on them.
        self._sidebar = sidebar.Sidebar(self)
        # Flat/tree toggle (fauxcasa-q6l.10): small checkbox above the tree;
        # load the persisted choice before _build_sidebar reads it.
        self.tree = self._new_sidebar_tree()
        # Flat/tree state lives on this QCheckBox (ez2.14: no longer shown
        # above the tree — the View menu's "Flat Folders" action and the
        # Folders-root right-click menu are the two visible affordances now,
        # both wired to stay in sync with this same checkbox below/in
        # _build_menus). Kept as a plain, un-parented state holder so every
        # reader of "is flat mode on" (_build_sidebar, _toggle_folder_view)
        # stays unchanged.
        self._flat_check = QCheckBox("Flat")
        self._flat_check.setToolTip(
            "List folders alphabetically instead of as a tree")
        # Block signals while setting initial state — _toggle_folder_view
        # calls _rebuild_sidebar, which is not safe before __init__ completes.
        self._flat_check.blockSignals(True)
        self._flat_check.setChecked(load_folder_view(self.state_dir))
        self._flat_check.blockSignals(False)
        self._build_sidebar()
        # Wire AFTER _build_sidebar so init doesn't trigger a spurious rebuild.
        self._flat_check.toggled.connect(self._toggle_folder_view)

        # --- menu bar: Tools (fauxcasa-v46.4) ---
        # The File Types panel is the first menu item the tracer grows; a
        # menu bar (not another toolbar button) because configuration
        # belongs off the browsing surface — Picasa's own Tools > Options
        # > File Types location, minus the Options tabs we don't have.
        self.tools_menu = self.menuBar().addMenu("&Tools")
        self.file_types_action = self.tools_menu.addAction("File &Types…")
        self.file_types_action.setStatusTip(
            "Choose which file types are scanned into this library")
        self.file_types_action.triggered.connect(self._show_file_types)

        # --- toolbar: search + zoom ---
        bar = QToolBar()
        bar.setMovable(False)
        # Icons + text together (ez2.14) — the glyph is a scan aid, never
        # a replacement for the label every toolbar action already has.
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(bar)
        # 16px runtime-painted glyphs (fauxcasa-ez2.14: no binary assets,
        # icons.py follows theme.TEXT so a future palette swap repaints for
        # free); text labels stay (ToolButtonTextBesideIcon, set below the
        # bar setup) so the icons are a scan aid, not the only affordance.
        self.open_action = bar.addAction(
            icons.make_icon("library", theme.TEXT), "Library…")
        self.open_action.setToolTip("Choose a different photo library folder")
        self.open_action.triggered.connect(self._change_library)
        self.back_action = bar.addAction(
            icons.make_icon("back", theme.TEXT), "Gallery  (Esc)")
        self.back_action.setToolTip(
            "Return to the gallery and folder tree (Esc)")
        self.back_action.triggered.connect(
            lambda: self._close_viewer(self.viewer.current_index()))
        self.back_action.setVisible(False)
        bar.addSeparator()
        # Picasa's green Play (folder/album headers + toolbar) distilled to
        # one toolbar affordance acting on the CURRENT view; F11 is the
        # Picasa-heritage shortcut (fauxcasa-q6l.3).
        self.play_action = bar.addAction(
            icons.make_icon("play", theme.PLAY), "Play")
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
        bar.addSeparator()   # real spacing (fauxcasa-ez2.6), not a padded label
        bar.addWidget(QLabel("Zoom"))
        # Small/large picture glyphs bracket the slider (ez2.14) — a
        # non-interactive visual cue, like the "Zoom" label beside them.
        zoom_small = QLabel()
        zoom_small.setPixmap(
            icons.make_icon("zoom_small", theme.TEXT_MUTED).pixmap(12, 12))
        bar.addWidget(zoom_small)
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
        zoom_large = QLabel()
        zoom_large.setPixmap(
            icons.make_icon("zoom_large", theme.TEXT_MUTED).pixmap(16, 16))
        bar.addWidget(zoom_large)
        bar.addSeparator()   # real spacing (fauxcasa-ez2.6), not a padded label
        self.reveal_box = QCheckBox("Show hidden")
        self.reveal_box.setToolTip(
            "Reveal hidden=yes photos, stash-folder files, and folders in the "
            "Hidden Folders category (shown veiled)")
        self.reveal_box.toggled.connect(self._toggle_reveal)
        bar.addWidget(self.reveal_box)
        # Metadata inspector toggle (fauxcasa-q6l.25). Deliberately NO
        # QAction shortcut for bare "I" here — a window-level bare-letter
        # shortcut would fire while the user is typing in the search box
        # above. The bare-I key is handled per-surface instead (grid.py /
        # viewer.py keyPressEvent -> info_toggle_requested -> toggle()
        # below), so this action's checked state stays truthful either way.
        self.info_action = bar.addAction(
            icons.make_icon("info", theme.TEXT), "Info")
        self.info_action.setCheckable(True)
        # Chord text derived from the keymap, same rule as play_action
        # above (ed5.12) — never hard-code the key.
        _info_chords = " / ".join(s.toString()
                                  for s in keymap.shortcuts("app.info"))
        self.info_action.setToolTip(f"Show photo info ({_info_chords})")
        self.info_action.toggled.connect(self._toggle_inspector)

        self._build_menus()

        # --- pages ---
        browser = QWidget()
        lay = QVBoxLayout(browser)
        lay.setContentsMargins(0, 0, 0, 0)
        split = QSplitter()
        # Sidebar panel: flat/tree toggle above the tree widget so both
        # travel together in the splitter (fauxcasa-q6l.10).
        self._sidebar_panel = QWidget()
        _spanel_lay = QVBoxLayout(self._sidebar_panel)
        _spanel_lay.setContentsMargins(0, 2, 0, 0)
        _spanel_lay.setSpacing(2)
        # _flat_check itself is no longer added here (ez2.14 removed the
        # bare checkbox above the tree) — only self.tree fills the panel.
        _spanel_lay.addWidget(self.tree)
        split.addWidget(self._sidebar_panel)
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

        # Prominent, non-modal activity row directly below the toolbar.
        # Cold indexing used to whisper only in the bottom status bar; on a
        # multi-thousand-photo first run that looked indistinguishable from a
        # stalled gallery. Keep secondary details below, but put the ongoing
        # operation and determinate progress where eyes already are.
        self.activity_row = QWidget()
        self.activity_row.setStyleSheet(
            "background: #fff3c4; color: #3c3320; border-bottom: 1px solid "
            "#d6bd62;")
        activity_lay = QHBoxLayout(self.activity_row)
        activity_lay.setContentsMargins(12, 6, 12, 6)
        # Carries the library root's folder NAME ("Scanning <root>…"):
        # user-authored text, so never let AutoText read it as markup
        # (fauxcasa-6vk finding 7); ElidingLabel sets PlainText itself.
        # Eliding also keeps a deep root name out of the window's resize
        # floor (fauxcasa-a3m).
        self.activity_label = ElidingLabel()
        self.activity_label.setStyleSheet("font-weight: 600; border: none;")
        self.activity_progress = QProgressBar()
        # The count/percent live in activity_label instead (fauxcasa-ez2.6):
        # the windows11 style painted setFormat's text through the bar's own
        # thin 4px fill, unreadable at this width — folding it into the
        # label alongside is both readable and one less thing duplicated.
        self.activity_progress.setTextVisible(False)
        self.activity_progress.setMinimumWidth(280)
        self.activity_progress.setMaximumWidth(320)
        activity_lay.addWidget(self.activity_label, 1)
        activity_lay.addWidget(self.activity_progress)
        self.activity_row.hide()

        # Metadata inspector panel (fauxcasa-q6l.25): a horizontal splitter
        # around `pages` rather than inside either page, so ONE panel
        # instance serves both the grid and the viewer — it stays put
        # while `pages` switches its current widget. Hidden by default
        # (default off keeps existing screenshots/tests untouched); the
        # toolbar's checkable Info action and the bare-I keys below
        # (grid.py / viewer.py keyPressEvent) show/hide it.
        self.inspector = InspectorPanel()
        self.inspector_split = QSplitter()
        self.inspector_split.addWidget(self.pages)
        self.inspector_split.addWidget(self.inspector)
        self.inspector_split.setCollapsible(0, False)
        self.inspector_split.setStretchFactor(0, 1)
        self.inspector.hide()

        central = QWidget()
        central_lay = QVBoxLayout(central)
        central_lay.setContentsMargins(0, 0, 0, 0)
        central_lay.setSpacing(0)
        central_lay.addWidget(self.activity_row)
        central_lay.addWidget(self.inspector_split, 1)
        self.setCentralWidget(central)

        # --- status bar ---
        self.setStatusBar(QStatusBar())
        # PlainText wherever a catalog string lands (fauxcasa-6vk finding
        # 7): counts_label carries album and people names, meta_label the
        # caption/keywords/path readout. A QLabel left in the default
        # AutoText format INTERPRETS a caption of "<b>beach</b>".
        # All three elide (fauxcasa-a3m): a status label's text used to
        # BE the window's minimum width, so one long caption or path
        # stopped the window shrinking. ElidingLabel keeps the PlainText
        # discipline above and puts the full string in the tooltip while
        # it is cut. meta_label leads with an on-disk path, so it elides
        # in the middle: the filename at the tail is the half worth
        # keeping.
        self.counts_label = ElidingLabel()
        self.progress_label = ElidingLabel()
        self.meta_label = ElidingLabel(
            mode=Qt.TextElideMode.ElideMiddle)
        # Import-report count (fauxcasa-cam.13; button fauxcasa-ez2.13): a
        # flat QToolButton (not a QLabel) reading "N import notes" that
        # opens a read-only dialog listing the report entries — deliberately
        # lean chrome; the full inspector surface is N7/M2 work.
        self.notes_label = QToolButton()
        self.notes_label.setAutoRaise(True)
        self.notes_label.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.notes_label.clicked.connect(self._show_import_notes_dialog)
        # Decode-sandbox degrade banner (fauxcasa-ez2.9 Stage 1, item 6):
        # a SEPARATE permanent label from notes_label above -- notes_label
        # is the import-report count (_update_import_notes), a different
        # concern with its own show/hide lifecycle; sharing it would let
        # one overwrite the other. Hidden unless state == "degraded".
        # _sync_decode_sandbox_label sets a tooltip here (the degrade
        # reason); ElidingLabel never clobbers an owner-set tooltip, so
        # the reason survives even when the banner text is elided.
        self.decode_sandbox_label = ElidingLabel()
        self.decode_sandbox_label.setVisible(False)
        self.statusBar().addWidget(self.counts_label)
        self.statusBar().addWidget(self.progress_label)
        self.statusBar().addPermanentWidget(self.notes_label)
        self.statusBar().addPermanentWidget(self.decode_sandbox_label)
        self.statusBar().addPermanentWidget(self.meta_label)
        self._show_counts("All photos", self._shown_count())
        self._update_import_notes()
        # Offline-root status-bar mention (fauxcasa-ed5.7.5, bead .e,
        # design §12): a fresh open surfaces any already-offline root here
        # too, not just the sidebar badge (_build_sidebar/
        # _offline_root_labels) — a transient notice like the backfill
        # banner, not a permanent widget. A single-root library (today's
        # real case — bead .d/.g) has no badge to show at all (fauxcasa-hi2
        # items 4/5), so it gets the friendlier explanatory sentence
        # instead of the terse multi-root badge-style text below.
        single_root_message = _single_root_offline_message(self.catalog)
        if single_root_message:
            self.statusBar().showMessage(single_root_message, 8000)
        else:
            offline_at_open = _offline_root_labels(self.catalog)
            if offline_at_open:
                self.statusBar().showMessage(
                    f"offline: {', '.join(offline_at_open)}", 8000)

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
        # Bare I from EITHER surface flips the SAME checkable toolbar
        # action, so info_action.isChecked() stays the single source of
        # truth for panel visibility (fauxcasa-q6l.25).
        self.grid.info_toggle_requested.connect(self.info_action.toggle)
        self.viewer.info_toggle_requested.connect(self.info_action.toggle)
        self.grid.search_requested.connect(self._focus_search)
        # Not-yet-implemented features never fail silently (fauxcasa-s6i):
        # both surfaces route their notice through one status-bar slot.
        self.grid.notice.connect(self._show_notice)
        self.viewer.notice.connect(self._show_notice)
        # Selection-tray wiring (fauxcasa-q6l.2). The readout also listens
        # to selection_changed and the search box directly — SEPARATE
        # connections, so the status-bar dual mode (_selection_changed)
        # and _search_changed stay untouched.
        self.grid.hold_requested.connect(self._hold_selection)
        self.grid.star_toggle_requested.connect(self._toggle_grid_stars)
        self.grid.star_clear_requested.connect(self._clear_grid_stars)
        self.viewer.hold_requested.connect(self._hold_from_viewer)
        self.viewer.star_toggle_requested.connect(
            lambda idx: self._toggle_stars([idx]))
        self.viewer.star_clear_requested.connect(
            lambda idx: self._clear_stars([idx]))
        self.tray.hold_clicked.connect(self._hold_selection)
        self.tray.navigate.connect(self._tray_navigate)
        self.tray.changed.connect(self._refresh_tray_readout)
        self.grid.selection_changed.connect(
            lambda _sel: self._refresh_tray_readout())
        self.search.textChanged.connect(
            lambda _t: self._refresh_tray_readout())
        # Per-group play button (fauxcasa-q6l.16)
        self.grid.play_group.connect(self._play_group)

        self.grid.set_data(catalog, thumbs)
        if self._star_min:
            # set_data's own set_filter(None, "") call is threshold-blind
            # (fauxcasa-q6l.20 review finding 1): a persisted threshold
            # must already be applied to the very first paint, not wait
            # for the first _apply_view/_apply_star_min call.
            n = self._apply_all_photos()
            self._show_counts(self._label_with_stars("All photos"), n)
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
        # Non-blocking first run (fauxcasa-q6l.13): the background library
        # WALK thread, its target build dir (persisted for the chained
        # _start_cold_build once the walk lands), and its start time (for
        # the cold-scan-ms duration event). _cold_scan_pending is set the
        # instant _start_cold_scan is called — before the thread itself
        # starts — and cleared at the top of _on_scan_done; it is folded
        # into index_busy() below so a scripted --finish-build run never
        # quits mid-walk, only mid-build (the pre-existing guarantee).
        self._scan_thread: threading.Thread | None = None
        self._scan_build_dir: Path | None = None
        self._scan_t0: float | None = None
        self._cold_scan_pending = False
        # Set by _on_scan_done when the background walk raised (Codex
        # cross-vendor review finding 2): the synchronous cold path let
        # that exception propagate out of main() and crash the process —
        # never a fake READY. This flag is how cli.ScriptedRun.check_ready tells
        # a failed deferred walk apart from a genuinely empty, successful
        # one (both leave the placeholder catalog in place).
        self._scan_failed = False
        # Set by shutdown() (Codex cross-vendor review round 3): a cold
        # scan that outlives both a scripted --timeout and shutdown()'s own
        # 5 s join leaves _scan_thread alive after main() has already
        # returned. If a later in-process run reuses the same
        # QApplication (main() supports this — see
        # test_ready_poll_timer_dies_with_the_window) and restarts the event
        # loop, the orphaned worker's queued scan_done can fire into this
        # now-stale window and reload/rebuild against deleted Qt objects.
        # _on_scan_done checks this FIRST and returns immediately once set;
        # shutdown() also disconnects the signal itself as a second,
        # belt-and-braces line of defense for an emit already queued
        # before the disconnect runs.
        self._shut_down = False
        # Parks the backfill's readers between photos while set — the
        # low-priority hook (a future consumer can pause on heavy scroll);
        # tests drive it directly.
        self.backfill_pause = threading.Event()
        self._reconcile_after_backfill = False
        self._bridge = _BuildBridge()
        self._bridge.progress.connect(self._build_progress)
        self._bridge.status.connect(self._on_status)
        self._bridge.notice.connect(self._on_notice)
        self._bridge.finished.connect(self._on_index_finished)
        self._bridge.backfill_done.connect(self._on_backfill_done)
        self._bridge.scan_done.connect(self._on_scan_done)

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

        # Land keyboard focus on the grid (fauxcasa-ez2.6): the browser's
        # main surface, and the one Space/J/K/arrows/star_toggle etc. are
        # bound against — without this, Qt's default first-focusable-widget
        # tab order can leave the search box focused at launch, so Space
        # types a literal space into a query instead of starring the
        # current photo. main() calls it again after win.show() since
        # focus can only really land on a mapped, visible window.
        self.grid.setFocus()

    # ---------- menu bar: File / View / Help (fauxcasa-ez2.6) ----------

    def _build_menus(self) -> None:
        """File / View / Help — added alongside the existing Tools menu
        (v46.4). Every item that already exists as a toolbar QAction
        (open_action/play_action/info_action, and the reveal/flat
        checkboxes) is REUSED here rather than duplicated: a menu click
        and a toolbar click end up on the exact same QAction (or, for the
        two plain QCheckBoxes, a thin checkable QAction kept in lockstep
        with the checkbox so there is still only one place — the
        checkbox — that owns the actual boolean)."""
        menubar = self.menuBar()

        # Kept as attributes (like tools_menu) so the window — and tests —
        # hold a durable reference to each menu instead of re-discovering
        # them through transient findChildren()/QAction.menu() wrappers,
        # which proved timing-sensitive under the script runner.
        file_menu = self.file_menu = menubar.addMenu("&File")
        file_menu.addAction(self.open_action)   # toolbar's "Library…" action
        file_menu.addSeparator()
        exit_action = file_menu.addAction("E&xit")
        exit_action.triggered.connect(self.close)

        view_menu = self.view_menu = menubar.addMenu("&View")
        zoom_in = view_menu.addAction("Zoom &In")
        zoom_in.setShortcut(QKeySequence.StandardKey.ZoomIn)
        zoom_in.triggered.connect(lambda: self._step_zoom(16))
        zoom_out = view_menu.addAction("Zoom &Out")
        zoom_out.setShortcut(QKeySequence.StandardKey.ZoomOut)
        zoom_out.triggered.connect(lambda: self._step_zoom(-16))
        view_menu.addSeparator()

        # reveal_box/_flat_check are plain QCheckBoxes (toolbar/sidebar),
        # not QActions — a checkable QAction here mirrors each one's
        # state both ways so the checkbox stays the single source of
        # truth (its own toggled handler is what actually applies the
        # view change; the action's toggled just forwards to setChecked,
        # guarded against the checkbox's own echo back).
        show_hidden_action = view_menu.addAction("Show &Hidden")
        show_hidden_action.setCheckable(True)
        show_hidden_action.setChecked(self.reveal_box.isChecked())
        show_hidden_action.toggled.connect(self.reveal_box.setChecked)
        self.reveal_box.toggled.connect(show_hidden_action.setChecked)

        view_menu.addAction(self.info_action)   # toolbar's "Info" action

        flat_folders_action = view_menu.addAction("&Flat Folders")
        flat_folders_action.setCheckable(True)
        flat_folders_action.setChecked(self._flat_check.isChecked())
        flat_folders_action.toggled.connect(self._flat_check.setChecked)
        self._flat_check.toggled.connect(flat_folders_action.setChecked)

        view_menu.addSeparator()
        self.star_menu = self._build_star_menu(view_menu)
        # Bulk-unstar (fauxcasa-q6l.20 clause c): no pre-existing "toggle
        # star" menu action to sit next to (star toggle is Space-only,
        # dispatched from grid/viewer keyPressEvent) — deliberately NO
        # QAction shortcut here, same reasoning as info_action's Space/I:
        # a window-level Shift+Space shortcut would fire while typing in
        # the search box. Shift+Space still works per-surface.
        _clear_chords = " / ".join(
            s.toString() for s in keymap.shortcuts("grid.star_clear"))
        # A QMenu hides QAction tooltips (fauxcasa-q6l.20 review nit 9), so
        # the tab-separated shortcut-column convention is what actually
        # makes Shift+Space visible here — no QAction shortcut is bound
        # (see the note above), this is text only.
        self.star_clear_action = view_menu.addAction(
            f"Clear Star(s)\t{_clear_chords}")
        self.star_clear_action.setToolTip(
            f"Clear stars on the current selection ({_clear_chords})")
        self.star_clear_action.triggered.connect(self._menu_clear_stars)

        view_menu.addSeparator()
        view_menu.addAction(self.play_action)   # toolbar's "Play" action

        help_menu = self.help_menu = menubar.addMenu("&Help")
        # fauxcasa-ez2.9: Tools was created first (v46.4, __init__, before
        # this method runs) so it landed leftmost in the bar; reposition it
        # between View and Help so the bar reads File, View, Tools, Help.
        menubar.removeAction(self.tools_menu.menuAction())
        menubar.insertMenu(help_menu.menuAction(), self.tools_menu)
        shortcuts_action = help_menu.addAction("&Keyboard Shortcuts…")
        shortcuts_action.triggered.connect(self._show_shortcuts_dialog)
        help_menu.addSeparator()
        about_action = help_menu.addAction(f"&About {APP_NAME}")
        about_action.triggered.connect(self._show_about)

    # Menu text for each star-threshold radio action, index == threshold
    # (0 = Any/off). fauxcasa-q6l.20 clause a.
    STAR_MENU_LABELS = ("&Any", "&1 star or more", "&2 stars or more",
                       "&3 stars or more", "&4 stars or more", "&5 stars")

    def _build_star_menu(self, view_menu: QMenu) -> QMenu:
        """The View > Stars submenu (fauxcasa-q6l.20 clause a): an
        exclusive radio group, "Any" (off) through "5 stars", the current
        threshold checked. Durable references (self.star_menu,
        self.star_actions) let tests read/drive it without findChildren
        (see the pyside-findchildren-wrapper-heisenbug memory)."""
        menu = view_menu.addMenu("&Stars")
        group = QActionGroup(self)
        group.setExclusive(True)
        self.star_actions: list[QAction] = []
        for n, text in enumerate(self.STAR_MENU_LABELS):
            act = menu.addAction(text)
            act.setCheckable(True)
            act.setChecked(n == self._star_min)
            act.setData(n)
            group.addAction(act)
            act.triggered.connect(
                lambda _checked=False, n=n: self._set_star_min(n))
            self.star_actions.append(act)
        return menu

    def _set_star_min(self, n: int) -> None:
        """Apply + persist the star-threshold predicate, then re-apply the
        current view/search in place (fauxcasa-q6l.20 clause a). Syncs the
        View > Stars radio group's checked state itself rather than
        relying on the caller having been a menu click — a direct/
        programmatic call (tests, a future keyboard chord) must leave the
        menu agreeing with reality too."""
        n = max(0, min(5, n))
        for act in self.star_actions:
            act.setChecked(act.data() == n)
        if n == self._star_min:
            return
        self._star_min = n
        save_star_min(self.state_dir, n)
        self._apply_star_min()

    def _apply_star_min(self) -> None:
        """Re-apply the current view or search in place after the star
        threshold changes — the same reapply-in-place shape _toggle_reveal
        uses for a reveal change, minus the sidebar rebuild (the threshold
        changes only what's SHOWN, not which folders/albums/people exist)."""
        kind, key = self._selected_view()
        search_text = self.search.text()
        sb = self.grid.verticalScrollBar()
        frac = sb.value() / sb.maximum() if sb.maximum() > 0 else 0.0
        if search_text.strip():
            self._search_changed(search_text)
        else:
            self._apply_view(kind, key)
        self.grid.scroll_to_fraction(frac)   # best-effort scroll restore
        self.grid.setFocus()

    def _step_zoom(self, delta: int) -> None:
        """Zoom In/Out menu actions step the SAME slider the toolbar
        drags — one source of truth for the current tile size, clamped
        to the slider's own range."""
        self.zoom.setValue(
            max(self.zoom.minimum(),
                min(self.zoom.maximum(), self.zoom.value() + delta)))

    def _show_notice(self, text: str) -> None:
        """Status-bar notice for a feature the user reached for that is
        not built yet (keymap.PLANNED_KEYS / PLANNED_FEATURES). Transient
        (NOTICE_MS) so it never sticks over the counts readout, and long
        enough to read the milestone it names."""
        self.statusBar().showMessage(text, NOTICE_MS)

    def _show_shortcuts_dialog(self) -> None:
        """Help > Keyboard shortcuts…: a read-only table built at runtime
        from keymap.DEFAULT_SCHEME + keymap.ACTION_LABELS — one source
        of truth, so a chord added/changed in the keymap module shows up
        here without a second hand-maintained copy."""
        from PySide6.QtWidgets import (
            QDialog,
            QHeaderView,
            QTableWidget,
            QTableWidgetItem,
            QVBoxLayout as _QVBoxLayout,
        )

        dlg = QDialog(self)
        dlg.setWindowTitle(f"{APP_NAME} — Keyboard Shortcuts")
        dlg.resize(520, 480)
        lay = _QVBoxLayout(dlg)
        table = QTableWidget(0, 2, dlg)
        table.setHorizontalHeaderLabels(["Action", "Shortcut"])
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        def add_row(label: str, chords: str) -> None:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(label))
            table.setItem(row, 1, QTableWidgetItem(chords))

        for action in sorted(keymap.DEFAULT_SCHEME):
            add_row(keymap.ACTION_LABELS.get(action, action),
                    " / ".join(s.toString() for s in keymap.shortcuts(action)))
        # Not yet available (fauxcasa-s6i): the Picasa chords the app
        # recognises but has not built, and the keyless features a shipped
        # key stands in for — same source the status-bar notices read.
        add_row("— Not yet available —", "")
        seen: set[tuple[str, str]] = set()
        for planned in keymap.PLANNED_KEYS.values():
            for chords, (label, note) in planned.items():
                if (label, chords[0]) in seen:   # shared across surfaces
                    continue
                seen.add((label, chords[0]))
                add_row(f"{label} ({note})", " / ".join(chords))
        for label, note in keymap.PLANNED_FEATURES.items():
            add_row(f"{label} ({note})", "")
        lay.addWidget(table)
        dlg.exec()

    def _show_about(self) -> None:
        """Help > About: app icon, name, version (release identity — same
        formatter as --version/READY), a one-line description, license,
        and the project URL. Built on QMessageBox (not the about()
        convenience function) so the app icon is guaranteed to show —
        about() leaves icon choice to the platform and Windows shows
        none at all."""
        box = QMessageBox(self)
        box.setWindowTitle(f"About {APP_NAME}")
        box.setIconPixmap(app_icon().pixmap(64, 64))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            f"<h3>{version_string()}</h3>"
            "<p>Read-only photo browser in the spirit of Picasa.</p>"
            "<p>License: AGPL-3.0-or-later</p>"
            "<p><a href=\"https://github.com/maphew/fauxcasa\">"
            "https://github.com/maphew/fauxcasa</a></p>")
        box.exec()

    # ---------- background index jobs ----------

    def _start_cold_scan(self, cache_dir: Path) -> None:
        """Non-blocking first run (fauxcasa-q6l.13): move the library WALK
        off the startup critical path so a NAS-scale first run paints
        instantly instead of reading as a hung app. main()'s cold non-adopt
        branch constructs this window around an EMPTY placeholder catalog
        (composed FROM cfg — same roots/library_id/root shape
        _scan_library_config itself would produce — so sidebar/abs()/
        photos_for_root see a coherent multiroot shape from the very first
        paint) and calls this right after win.show(). The walk runs here,
        on a background thread; _on_scan_done reloads the real catalog it
        returns and chains straight into the existing cold BUILD
        (_start_cold_build) — the same scan -> reload -> build order a
        synchronous cold start always had, just off the critical path."""
        self._cold_scan_pending = True
        self._scan_build_dir = cache_dir
        self._scan_t0 = time.perf_counter()
        self._show_activity(f"Scanning {self.catalog.root.name}…")
        self._update_empty_text()  # grid placeholder: cold scan preempts
                                    # the "empty library" verdict below it
        self.grid.viewport().update()  # nothing else repaints the grid here
        bridge = self._bridge
        cfg, scan_filter = self.cfg, self.scan_filter
        contacts, pal_dir, exts, db3_dir = (
            self.contacts, self.pal_dir, self.exts, self.db3_dir)
        contacts_sig = self.contacts_sig

        def work() -> None:
            try:
                catalog = _scan_library_config(
                    cfg, scan_filter, contacts, pal_dir, exts, db3_dir)
                # Same stat-before-read invariant as main()'s synchronous
                # cold path (fauxcasa-cam.14 step 4): contacts_sig was
                # captured in main() BEFORE load_contacts_xml read the
                # file, well before this walk even started — stamp that
                # SAME value, never a fresh re-stat after a walk that may
                # have taken a long time.
                catalog.contacts_sig = contacts_sig
                _emit(bridge.scan_done, catalog)
            except Exception as e:  # report, never crash the UI
                log.error("library scan failed: %s", e)
                _emit(bridge.scan_done, None)

        self._scan_thread = threading.Thread(target=work, daemon=True)
        self._scan_thread.start()

    def _on_scan_done(self, catalog) -> None:
        """The background walk started by _start_cold_scan landed (or
        failed). Clears _cold_scan_pending FIRST (fauxcasa-q6l.13) so
        index_busy() reflects reality for the rest of this handler and any
        --finish-build poll racing it; on success, swaps the real catalog
        in via reload_data (mirrors _on_index_finished's reconcile-swap
        branch) and immediately chains into the cold BUILD, exactly the
        scan -> reload -> build order a synchronous cold start always had.

        Checked FIRST, before anything else (Codex cross-vendor review
        round 3): a scan that outlives shutdown()'s join leaves the worker
        thread alive after main() has already returned this window to the
        caller. main() supports reusing the same QApplication across an
        in-process run (test_ready_poll_timer_dies_with_the_window) — if a
        later run restarts the event loop, the orphaned worker's queued
        scan_done can fire into this now-stale window. shutdown() sets
        _shut_down and disconnects this slot, but an emit already queued
        before that disconnect runs still needs this guard: reload_data/
        _start_cold_build must never touch a window past its shutdown."""
        if self._shut_down:
            return
        self._cold_scan_pending = False
        build_dir, self._scan_build_dir = self._scan_build_dir, None
        cold_ms = (time.perf_counter() - self._scan_t0) * 1000.0 \
            if self._scan_t0 is not None else 0.0
        if catalog is None:
            # Codex cross-vendor review finding 2: the synchronous cold
            # path let a scan exception propagate out of main() and crash
            # the process (never a fake READY, always a nonzero exit for
            # scripted runs). _scan_failed is how cli.ScriptedRun.check_ready
            # tells this apart from a genuinely empty, successful walk —
            # both leave the placeholder catalog in win.catalog. An
            # interactive run isn't killed outright (the window already
            # exists and is otherwise usable) — the failure surfaces via
            # the activity row/status bar/log instead, closer to old
            # behavior than a hard crash would be for THIS entry point.
            self._scan_failed = True
            self._hide_activity()
            self.build_failed = True
            self.statusBar().showMessage(
                "library scan failed — see stderr", 10000)
            return
        # User star choices are overlays in Fauxcasa's machine-local cache
        # (self.star_overrides, loaded once in __init__ from cache_dir) —
        # the ctor's own apply_star_overrides call only ever touched the
        # EMPTY placeholder catalog on this path, so the just-scanned REAL
        # catalog needs the same reapply here, BEFORE reload_data builds
        # the Starred count/filter and before the cold build starts
        # (Codex cross-vendor review finding 1: a --rebuild/invalidated-
        # cache cold run must not silently drop existing star choices).
        apply_star_overrides(catalog, self.star_overrides)
        log.info("cold-scan: %d photos, %d folders, %d albums in %.0f ms",
                 len(catalog.photos), len(catalog.folders),
                 len(catalog.albums), cold_ms)
        if catalog.report.entries:
            # §4 conflicts surfaced, never silent — main()'s own "import
            # report" log only ever covers the EMPTY placeholder on this
            # path (always report-free, so it's a silent no-op there, not
            # a duplicate of this one) — the real summary is only known
            # once the walk lands here (Codex cross-vendor review finding
            # 3).
            log.info("import report: %s", catalog.report.summary())
        # Distinct from the "indexed" event _on_index_finished emits for
        # the BUILD below — prep_ms in the READY line no longer measures
        # this walk (fauxcasa-q6l.13), so it gets its own duration event.
        print(json.dumps({
            "event": "cold-scan", "cold_scan_ms": round(cold_ms),
            "photos": len(catalog.photos),
        }), flush=True)
        self.reload_data(catalog, None)
        if build_dir is not None:
            self._start_cold_build(build_dir)

    def _start_cold_build(self, build_dir: Path) -> None:
        bridge, catalog = self._bridge, self.catalog
        done = [0]  # results arrive out of order; count completions, not idx
        total_photos = len(catalog.photos)
        # Decode what the gallery can show before hidden/cache-only entries.
        # The grid's display order is already folder-grouped and begins at the
        # current viewport, so this is also the most useful publication order.
        gallery_order = list(self.grid.display)
        self._show_activity(
            "Indexing thumbnails — gallery will fill as photos are ready",
            0, total_photos)

        def cb(global_i: int, img) -> None:
            if img is not None:
                self.grid.feed_tile(global_i, img)
            else:
                self.grid.feed_error(global_i)
            done[0] += 1
            _emit(bridge.progress, done[0], total_photos)

        def work() -> None:
            try:
                if catalog.library_id:
                    # Explicit libraries own one cache per manifest root.
                    # Catalog slices are contiguous/in manifest order; map
                    # each builder's local progress index back to the global
                    # catalog index before feeding the live grid.
                    built: list[tuple[str, object]] = []
                    starts: dict[str, int] = {}
                    for i, photo in enumerate(catalog.photos):
                        starts.setdefault(photo.root_id, i)
                    t0 = time.perf_counter()
                    catalog.refresh_offline_ids()
                    for root in catalog.roots:
                        start = starts.get(root.id, 0)
                        if root.id in catalog.offline_ids:
                            # Never index an unreachable root (§8): every
                            # photo would decode to an error blob, and
                            # writing that fcache would clobber a good one
                            # from when the drive was attached. Feed error
                            # tiles for this session and let the composite
                            # reuse the on-disk cache (or a placeholder).
                            for j in range(len(
                                    catalog.photos_for_root(root.id))):
                                cb(start + j, None)
                            built.append((root.id, None))
                            continue

                        def root_cb(i, _total, img, start=start):
                            cb(start + i, img)

                        local_by_global = {
                            global_i: local_i
                            for local_i, global_i in enumerate(
                                i for i, photo in enumerate(catalog.photos)
                                if photo.root_id == root.id)
                        }
                        priority = [local_by_global[i] for i in gallery_order
                                    if i in local_by_global]

                        one = build_cache(
                            catalog, build_dir, root_cb,
                            cancel=self.build_cancel, root_id=root.id,
                            priority_indices=priority)
                        if one is None:
                            return
                        built.append((root.id, one))
                    result = _MultiRootIndexResult(
                        roots=built,
                        photos=sum(one.photos for _rid, one in built
                                   if one is not None),
                        elapsed_s=time.perf_counter() - t0,
                        workers=max((one.workers for _rid, one in built
                                     if one is not None), default=0),
                    )
                else:
                    result = build_cache(
                        catalog, build_dir,
                        lambda i, _total, img: cb(i, img),
                        cancel=self.build_cancel,
                        priority_indices=gallery_order)
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
        rebuilt catalog is swapped in atomically when complete.

        Reconcile runs per ONLINE root only (fauxcasa-ed5.7.5, bead .e,
        design §8/§9 — see _reconcile_online_roots): an offline root's
        catalog entries are never diffed against a missing directory, so
        they can never be counted as removed. main.py doesn't open
        explicit multi-root libraries yet (bead .d/.g), so today's real
        catalogs always carry exactly one (online) root and this is a
        legacy no-op path — it exists so a >1-root catalog reconciles
        safely from the day that lands, without a second migration here."""
        bridge, old, cache_dir = self._bridge, self.catalog, self.cache_dir

        def work() -> None:
            try:
                drift, offline_labels = _reconcile_online_roots(
                    old, self.scan_filter, self.build_cancel, self.exts,
                    self.contacts_path)
            except Exception as e:
                log.error("reconcile walk failed: %s", e)
                return
            if drift is None or self.build_cancel.is_set():
                return  # cancelled mid-walk
            offline_note = ""
            if offline_labels:
                offline_note = (
                    f" — offline, skipped: {', '.join(offline_labels)}")
            if not drift.changed:
                if offline_note:
                    # Nothing changed on the online roots, but the offline
                    # ones are still worth a mention — the user may not
                    # know a drive is unreachable this session.
                    _emit(bridge.notice, f"library unchanged{offline_note}")
                return  # nothing else to do
            multiroot = len(old.roots) > 1
            if self.adopt or multiroot:
                # The thumbs are an external (--thumbs) cache we can't
                # rebuild (adopt-mode) — OR this is an explicit multi-root
                # library, where a full scan_library(old.root) rebuild
                # below is the WRONG shape (single-root signature) and
                # would silently drop every non-first root's photos
                # (design §7 risk 7 / §13 item 7). Bead .e's drift-rebuild
                # choice: surface the drift and keep showing the indexed
                # snapshot (option (b), conservative) rather than a
                # per-root rescan-and-merge (option (a)) — safe index
                # contiguity under a merge needs the per-root
                # reindex/backfill wiring bead .d lands, not present here.
                _emit(bridge.notice,
                      f"library changed since this cache was built "
                      f"({drift.summary()}){offline_note} — showing the "
                      f"indexed snapshot")
                return
            _emit(bridge.status,
                  f"library changed ({drift.summary()}){offline_note} — "
                  f"reindexing…")
            fresh = None
            try:
                # Reload contacts.xml from disk HERE, right before the
                # rescan, instead of reusing self.contacts (the map loaded
                # once at STARTUP) — fauxcasa-cam.14 step 4: if contacts.xml
                # was edited between startup and this reconcile,
                # self.contacts is already stale. Stat BEFORE the read, not
                # after (Codex review round-2 finding 2 — same guiding
                # principle as catalog._read_folder_ini's pre-read stat: a
                # signature must never be NEWER than the content it
                # describes). Stat-after-read would let an edit landing
                # DURING load_contacts_xml's own read pair NEW content with
                # a signature that already reads as current, so a later
                # warm start would trust that content forever. Stat-before
                # instead pairs whatever gets parsed with the OLDER
                # pre-read signature, so a rewrite mid-read is self-healed
                # by one spurious rebuild on the NEXT reconcile.
                if self.contacts_path is not None:
                    contacts_sig = stat_sig(self.contacts_path)
                    rescan_contacts = load_contacts_xml(self.contacts_path)
                else:
                    rescan_contacts, contacts_sig = self.contacts, None
                fresh = scan_library(old.root, self.scan_filter,
                                     rescan_contacts, self.pal_dir,
                                     exts=self.exts, db3_dir=self.db3_dir)
                fresh.contacts_sig = contacts_sig
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
        report_path = self.cache_dir / REPORT_NAME

        def cb(done: int, total: int) -> None:
            if done % 100 == 0 or done == total:
                _emit(bridge.status,
                      f"backfilling metadata {done:,}/{total:,}")

        def work() -> None:
            try:
                result = backfill_catalog(catalog, cat_path, progress=cb,
                                          cancel=self.build_cancel,
                                          pause=self.backfill_pause,
                                          report_path=report_path)
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
        self._hide_activity()
        if not ok:
            return
        apply_star_overrides(self.catalog, self.star_overrides)
        kind, key = self._selected_view()
        self._rebuild_sidebar()
        self._reselect_view(kind, key)
        if kind == "recent" and not self.search.text().strip():
            self._apply_view(kind, key)   # the collection just populated
        self.grid.viewport().update()     # star badges may have changed
        self._update_import_notes()       # infile_override entries landed
        if self.info_action.isChecked():
            # Same-object mutation, so nothing re-emits a selection: the
            # open panel would keep rendering the pre-backfill values for
            # the photo the user is looking at (fauxcasa-6vk finding 6).
            self._toggle_inspector(True)   # re-derives from the selection
        self.statusBar().showMessage("metadata backfill complete", 8000)
        if self._reconcile_after_backfill:
            self._reconcile_after_backfill = False
            self._start_reconcile()

    def _on_status(self, text: str) -> None:
        """PROGRESS text: a worker is still running, so a non-empty message
        raises the activity row's busy indicator (an empty one lowers it).
        A worker whose last act is to explain why it stopped must use
        `notice` instead — see _on_notice (fauxcasa-6vk finding 5)."""
        self.progress_label.setText(("   " + text) if text else "")
        if text:
            self._show_activity(text)
        else:
            self._hide_activity()

    def _on_notice(self, text: str) -> None:
        """A worker's TERMINAL notice: the job is over and nothing further
        will be emitted for it (fauxcasa-6vk finding 5). Reconcile's
        "library unchanged — offline, skipped: …" and adopt/multiroot
        "showing the indexed snapshot" branches return right after saying
        this, so routing them through _on_status left an indeterminate
        spinner running with no job behind it until the next background
        job happened to finish."""
        self.progress_label.setText(("   " + text) if text else "")
        self.statusBar().showMessage(text, 10000)
        self._hide_activity()

    def _on_index_finished(self, result, catalog, is_reconcile: bool) -> None:
        self.progress_label.setText("")
        self._hide_activity()
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
            if isinstance(result, _MultiRootIndexResult):
                caches = {}
                skipped: list[str] = []
                for root_id, one in result.roots:
                    if one is None:      # offline root: build skipped (§8)
                        skipped.append(root_id)
                        continue
                    cache = load_cache(one.path)
                    bind(cache, catalog, root_id=root_id)
                    caches[root_id] = cache
                levels = (next(iter(caches.values())).levels
                          if caches else [THUMB_EDGE])
                for root_id in skipped:
                    caches[root_id] = _offline_root_cache(
                        catalog, root_id, levels, self.cache_dir)
                cache = (next(iter(caches.values())) if len(caches) == 1
                         else CompositeThumbCache(catalog, caches))
            else:
                cache = load_cache(result.path)
                bind(cache, catalog)
        except CacheError as e:
            log.error("built cache failed to bind: %s", e)
            return
        # Indexers import source ratings/stars. Explicit Fauxcasa choices
        # win on this machine and survive both cold builds and rescans.
        apply_star_overrides(catalog, self.star_overrides)
        if catalog is self.catalog:
            self.grid.set_thumbs(cache)        # cold build: same catalog
            self.viewer.set_thumbs(cache)      # ...so the viewer previews too
            self.tray.set_thumbs(cache)        # ...and held thumbs render
            self._refresh_recent_count()       # mtimes just landed (q6l.7)
            # build_cache merged in-file captions/keywords into these SAME
            # Photo objects in place — the prebuilt haystacks are stale.
            self._rebuild_search_index()
            # ...and so are the People/Starred sidebar counts (fauxcasa-
            # cam.5: XMP-only faces and XMP Rating stars land on these
            # SAME Photo objects too, only now, at cold-build completion —
            # same rebuild-then-reselect shape as _on_backfill_done, so a
            # cold index's People section reflects XMP faces immediately,
            # with no reveal toggle or relaunch needed to see them).
            kind, key = self._selected_view()
            self._rebuild_sidebar()
            self._reselect_view(kind, key)
            self._update_import_notes()  # the cold build collected a fresh report
            if self.info_action.isChecked():
                # The build merged in-file metadata into these SAME Photo
                # objects; no selection signal follows, so an open panel
                # would still show the pre-build values (fauxcasa-6vk
                # finding 6). reload_data does this for the other branch.
                self._toggle_inspector(True)
        else:
            self.reload_data(catalog, cache)   # reconcile: swap in the new
        # User-facing wording (fauxcasa-ez2.6 §7): plain "ready" status, not
        # an indexing-rate number nobody but a dev cares about. The JSON
        # "indexed" event above (machine protocol, §7) keeps rate_per_s.
        self.statusBar().showMessage(
            f"Library ready — {result.photos:,} photos", 8000)

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
        # Leave the viewer the way _close_viewer does (fauxcasa-6vk finding
        # 4): forcing the page back to the browser without retiring the
        # viewer-only Gallery/Esc action left it on the toolbar over the
        # grid, where it does nothing.
        self.back_action.setVisible(False)
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
        # Threshold-aware (fauxcasa-q6l.20 review finding 3): set_data just
        # above already re-applied the UNFILTERED default view, so redo it
        # through _apply_all_photos or an active star filter silently drops
        # mid-session on the next reconcile swap.
        n = self._apply_all_photos()
        self._show_counts(self._label_with_stars("All photos"), n)
        self._update_import_notes()   # the rescan collected a fresh report
        self.meta_label.setText("")
        if self.info_action.isChecked():
            # Re-derive the inspector from the NEW catalog: set_data can
            # keep the same current index across the swap without
            # emitting, but that index now names a different photo — a
            # stale panel would show the old catalog's metadata for a
            # photo the user may then act on (fauxcasa-q6l.25 review).
            self._toggle_inspector(True)

    def index_busy(self) -> bool:
        # _cold_scan_pending covers the gap between _start_cold_scan
        # setting it and the walk thread actually landing (fauxcasa-q6l.13)
        # — including the moment before _scan_thread.start() runs — so a
        # scripted --finish-build run never quits while a cold scan is
        # still in flight, only once the chained build finishes too.
        return self._cold_scan_pending or any(
            t is not None and t.is_alive()
            for t in (self._build_thread, self._reconcile_thread))

    def shutdown(self) -> None:
        """Stop and reap the index threads so none is mid-write (or
        inside a Qt codec) while the interpreter tears down. The backfill
        persists its cursor on cancel, so a quit mid-backfill simply
        resumes on the next launch (its final save is why it gets the
        same join, not a bare daemon abandon)."""
        self.build_cancel.set()
        self.backfill_pause.clear()  # a paused backfill must see the cancel
        # _scan_thread (fauxcasa-q6l.13) has no cancel token — scan_library/
        # _scan_library_config walk to completion — so this join is a bounded
        # wait, not a real interrupt; the thread is a daemon and the process
        # exiting reaps it regardless. Joined anyway so a quit mid-scan in
        # tests doesn't race a still-running walk against interpreter teardown.
        for t in (self._build_thread, self._reconcile_thread,
                  self._backfill_thread, self._scan_thread):
            if t is not None and t.is_alive():
                t.join(timeout=5.0)
        # Codex cross-vendor review round 3: set unconditionally, whether
        # the join above reaped _scan_thread or gave up on a walk that
        # outlived it — either way this window must never react to a
        # scan_done that fires after this point (see the docstring on
        # _shut_down in __init__). Disconnecting the signal here is a
        # second line of defense for an emit already queued before this
        # runs; swallow the RuntimeError a torn-down C++ bridge or an
        # already-disconnected slot would raise (shutdown() itself may
        # run more than once in some call sequences — never fatal here).
        self._shut_down = True
        try:
            self._bridge.scan_done.disconnect(self._on_scan_done)
        except (RuntimeError, TypeError):
            pass

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
        save_window_geometry(self.state_dir, self.saveGeometry())
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

    def _toggle_folder_view(self, flat: bool) -> None:
        """Switch the folder sidebar between flat and tree layouts, persist the
        choice, and rebuild the sidebar while preserving the current selection."""
        save_folder_view(self.state_dir, flat)
        kind, key = self._selected_view()
        self._rebuild_sidebar()
        self._reselect_view(kind, key)

    # ---------- sidebar ----------

    def _new_sidebar_tree(self) -> QTreeWidget:
        """Delegates to self._sidebar (fauxcasa-4tu stage 3): kept as a
        one-line MainWindow method so self._new_sidebar_tree() call sites
        and Qt signal connections that name it still resolve."""
        return self._sidebar._new_sidebar_tree()

    def _rebuild_sidebar(self) -> None:
        """Delegates to self._sidebar; see its docstring for why a fresh
        tree is swapped in rather than clear()-ing the live one."""
        self._sidebar._rebuild_sidebar()

    def _build_sidebar(self) -> None:
        """Delegates to self._sidebar."""
        self._sidebar._build_sidebar()

    def _recent_indices(self) -> list[int]:
        """The Recently Updated set for the current reveal state (semantics
        + one-line mtime decision live on recent_indices above). Stays on
        MainWindow (fauxcasa-4tu stage 3): _apply_view (not sidebar-only)
        calls it too, alongside sidebar._recent_label."""
        return recent_indices(self.catalog, self.grid.reveal)

    def _refresh_recent_count(self) -> None:
        """Delegates to self._sidebar."""
        self._sidebar._refresh_recent_count()

    def _people_counts(self) -> tuple[dict[str, int], int, dict[str, set]]:
        """Delegates to self._sidebar."""
        return self._sidebar._people_counts()

    def _sidebar_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        """Delegates to self._sidebar; kept as a one-line MainWindow method
        so Qt signal connections that name self._sidebar_clicked resolve."""
        self._sidebar._sidebar_clicked(item, _col)

    def _scope_indices(self, idxs: list[int]) -> list[int]:
        """The single star-threshold choke point (fauxcasa-q6l.20 clause a,
        spec §3/§5): every view (folder, album, search, starred, recent,
        person, unnamed) passes its candidate indices through here so "N
        stars or more" composes with any of them. A no-op at the default
        threshold (0 = Any)."""
        if self._star_min <= 0:
            return idxs
        cat = self.catalog
        return [i for i in idxs if cat.photos[i].star >= self._star_min]

    def _label_with_stars(self, label: str) -> str:
        """Append the active star-threshold suffix to a view/search label
        (fauxcasa-q6l.20 clause a, spec N7): a filtered-empty grid must
        never read as a silent no-op. No-op at the default threshold
        (0 = Any)."""
        if self._star_min <= 0:
            return label
        return f"{label}  ≥{self._star_min}★"

    def _apply_all_photos(self) -> int:
        """The default folder-grouped view, star-threshold aware. Plain
        set_filter(None) is what triggers BOTH the default folder grouping
        AND the per-folder sort_modes lookup (set_filter's `default_view`),
        so a threshold — which must filter that same set while keeping
        both — computes the indices itself and opts back into the sort
        pass via `default_sort=True` instead. Returns the shown count."""
        cat = self.catalog
        if self._star_min > 0:
            idxs = self._scope_indices(
                [i for i, p in enumerate(cat.photos)
                 if p.visible or self.grid.reveal])
            self.grid.set_filter(idxs, self._label_with_stars("All photos"),
                                 default_sort=True)
            return len(idxs)
        self.grid.set_filter(None, "")
        return self._shown_count()

    def _apply_view(self, kind: str, key: str) -> None:
        """Apply a sidebar view's grid filter + status counts WITHOUT touching
        the search box. Shared by _sidebar_clicked and the Show-hidden toggle,
        so the toggle can re-apply the active view (recomputed for the new
        reveal state) instead of snapping to All photos."""
        cat = self.catalog
        if kind == "starred":
            idxs = [i for i, p in enumerate(cat.photos)
                    if (p.visible or self.grid.reveal) and p.star]
            # "Starred under threshold N" is star >= max(1, N): the base
            # filter above already guarantees >=1, so _scope_indices (a
            # no-op at N<=0) only ever tightens it further.
            idxs = self._scope_indices(idxs)
            idxs = _order_starred_newest_first(cat, idxs)
            label = self._label_with_stars("Starred")
            self.grid.set_filter(idxs, label, grouper=_starred_grouper)
            self._show_counts(label, len(idxs))
        elif kind == "recent":
            idxs = self._scope_indices(self._recent_indices())
            label = self._label_with_stars("Recently Updated")
            self.grid.set_filter(idxs, label)
            self._show_counts(label, len(idxs))
            if not idxs and self._star_min > 0:
                # fauxcasa-q6l.20 review finding 7: don't misattribute an
                # empty view to the backfill when the star threshold is
                # what's actually filtering everything out.
                self.statusBar().showMessage(
                    "No recently updated photos at this star threshold — "
                    "try View > Stars > Any", 8000)
            elif not idxs and self.catalog.backfill_state != BACKFILL_COMPLETE:
                # empty-state honesty (cam.12): mtimes are still being
                # backfilled, so an empty view is pending, not final
                self.statusBar().showMessage(
                    "Recently Updated fills in as the metadata backfill "
                    "indexes file dates…", 8000)
        elif kind == "album" and key in cat.albums:
            album = cat.albums[key]
            idxs = self._scope_indices(list(album.members))
            self.grid.set_filter(idxs, self._label_with_stars(album.name))
            self._show_counts(
                self._label_with_stars(f"Album “{album.name}”"), len(idxs))
        elif kind == "person":
            idxs = self._scope_indices([
                i for i, p in enumerate(cat.photos)
                if (p.visible or self.grid.reveal)
                and any(n == key for _rect, _cid, n in p.faces)])
            self.grid.set_filter(idxs, self._label_with_stars(key))
            self._show_counts(
                self._label_with_stars(f"Person “{key}”"), len(idxs))
        elif kind == "unnamed":
            idxs = self._scope_indices([
                i for i, p in enumerate(cat.photos)
                if (p.visible or self.grid.reveal)
                and any(n is None for _rect, _cid, n in p.faces)])
            label = self._label_with_stars("Unnamed faces")
            self.grid.set_filter(idxs, label)
            self._show_counts(label, len(idxs))
        else:  # "all", "folder", or an album that no longer exists
            n = self._apply_all_photos()
            if kind == "folder":
                self.grid.scroll_to_folder(key)
            self._show_counts(self._label_with_stars("All photos"), n)

    # ---------- per-folder sort modes (fauxcasa-q6l.11) ----------

    def _sidebar_menu(self, point) -> None:
        """Delegates to self._sidebar."""
        self._sidebar._sidebar_menu(point)

    def _folders_root_menu(self) -> QMenu:
        """Delegates to self._sidebar."""
        return self._sidebar._folders_root_menu()

    def _folder_sort_menu(self, rel: str) -> QMenu:
        """Delegates to self._sidebar."""
        return self._sidebar._folder_sort_menu(rel)

    def _set_folder_sort(self, rel: str, mode: str) -> None:
        """Delegates to self._sidebar."""
        self._sidebar._set_folder_sort(rel, mode)

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
            p_folder_key = folder_key(cat, p.root_id, p.folder)
            append((i, "\n".join((
                p.name,
                p.caption or "",
                " ".join(p.keywords),
                " ".join(n for _rect, _cid, n in p.faces if n),  # people (§5)
                folder_hay.get(p_folder_key, p.folder.lower()),
            )).lower()))
        self._search_pairs = pairs
        if cat.visible_count == len(cat.photos):
            self._search_pairs_vis = pairs  # nothing hidden: share the list
        else:
            self._search_pairs_vis = [
                ih for ih, p in zip(pairs, cat.photos) if p.visible]
        log.info("search index: %d haystacks in %.0f ms",
                 len(pairs), (time.perf_counter() - t0) * 1000.0)

    def _focus_search(self) -> None:
        """Ctrl+F / '/' from the grid (fauxcasa-ez2.6, keymap.app.search):
        jump to the search box and select any existing text, so typing
        immediately replaces a stale query instead of appending to it."""
        self.search.setFocus()
        self.search.selectAll()

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
            n = self._apply_all_photos()
            self._show_counts(self._label_with_stars("All photos"), n)
            self.last_search_hits = n
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
        idxs = self._scope_indices([ih[0] for ih in cur])
        q = text.strip().lower()
        self.grid.set_filter(idxs, self._label_with_stars(f"search: {q}"))
        self._show_counts(
            self._label_with_stars(f"Search “{q}”"), len(idxs))
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
        self.tray.hold(self.tray.photo_key(self.catalog.photos[i])
                       for i in order)

    def _hold_from_viewer(self, idx: int) -> None:
        """Ctrl+H in the viewer: hold the photo on screen."""
        if 0 <= idx < len(self.catalog.photos):
            self.tray.hold([self.tray.photo_key(self.catalog.photos[idx])])

    def _tray_navigate(self, rel: object) -> None:
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
                # clear to All photos as the last resort. Threshold-aware
                # (fauxcasa-q6l.20 review finding 5) via _apply_all_photos
                # — deliberately does NOT clear an active star threshold
                # as a side effect of a tray click; a below-threshold held
                # photo stays absent and falls to the existing "Photo not
                # visible in any view" message below, which already says
                # so honestly (N7) without silently discarding the user's
                # filter choice.
                self.search.blockSignals(True)
                self.search.clear()
                self.search.blockSignals(False)
                n = self._apply_all_photos()
                self._reselect_view("all", "")
                self._show_counts(self._label_with_stars("All photos"), n)
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

    def _update_empty_text(self) -> None:
        """Central empty-state wording for the grid's painted placeholder
        (grid.empty_text, fauxcasa-ez2.4's "an empty view painted nothing"
        finding). Called after every place the grid's display set can
        change (_apply_view/_search_changed/_show_counts all flow here;
        _start_cold_scan/_on_scan_done call it too since they flip
        _cold_scan_pending WITHOUT going through _show_counts) — reads
        live state instead of taking a kind/count argument, so it can
        never drift out of sync with what _apply_view/_search_changed
        actually left on screen. A non-empty display always wins (no
        text competes with real tiles). Priority once empty: an active
        search names the query; a folder view (never the true "no
        library" case — folders never exist without photos) gets the
        gentler "this folder" wording; a cold scan in flight preempts
        the "empty library" verdict below it since the walk hasn't
        landed yet and the library may not be empty at all; only once
        neither applies does an empty All-photos view get the terminal
        "no photos anywhere" copy. Every other empty view (starred,
        recent, an album, a person) is left wordless by design — those
        are ordinary "nothing here yet" states, not the three the audit
        called out."""
        if self.grid.display:
            self.grid.empty_text = ""
            return
        query = self.search.text().strip()
        if query:
            self.grid.empty_text = f'No photos match "{query}"'
            return
        kind, _key = self._selected_view()
        if kind == "folder":
            self.grid.empty_text = "This folder has no photos"
            return
        if getattr(self, "_cold_scan_pending", False):
            # getattr guard: __init__ calls _show_counts (line ~1583)
            # before _cold_scan_pending itself is first set (line ~1652).
            self.grid.empty_text = f"Scanning {self.catalog.root.name}…"
            return
        if kind == "all" and self._shown_count() == 0:
            self.grid.empty_text = (
                f"No photos found under {self.catalog.root.name} — "
                "use Library… to pick another folder")
            return
        self.grid.empty_text = ""

    def _show_counts(self, label: str, n: int) -> None:
        reveal = self.grid.reveal
        folders = sum(1 for f in self.catalog.folders.values()
                      if (f.total_count if reveal else f.photo_count))
        self._update_empty_text()
        self.counts_label.setText(
            f"  {label}: {n} photos · {folders} folders"
            f" · {len(self.catalog.albums)} albums")

    def _update_import_notes(self) -> None:
        """The lean import-report surface (fauxcasa-cam.13, button
        fauxcasa-ez2.13): a permanent status-bar button reading "N import
        notes" when the catalog's report has entries worth surfacing,
        opening a read-only dialog on click. Hidden entirely at zero —
        most libraries have no notes and deserve no chrome. status_count()
        excludes db3_path_unresolved (machine residue about whatever
        Picasa library last ran on THIS machine, not a conflict inside the
        opened library) — a library with only that kind of note shows
        zero import notes, matching fauxcasa-ez2.13."""
        n = self.catalog.report.status_count()
        if n == 0:
            self.notes_label.setVisible(False)
            self.notes_label.setText("")
            self.notes_label.setToolTip("")
            return
        self.notes_label.setText(
            f"{n} import note{'s' if n != 1 else ''}  ")
        visible_entries = [e for e in self.catalog.report.entries
                          if e.kind != "db3_path_unresolved"]
        shown = [f"[{e.source}] {e.kind}: {e.detail}"
                for e in visible_entries[:6]]
        if n > len(shown):
            shown.append(f"… and {n - len(shown)} more — click for details")
        self.notes_label.setToolTip(_plain_tooltip("\n".join(shown)))
        self.notes_label.setVisible(True)

    def _show_import_notes_dialog(self) -> None:
        """Click target for the status-bar import-notes button
        (fauxcasa-ez2.13): a small read-only dialog listing every report
        row (kind, count, up to 20 example rels/subjects) plus a "Reveal
        report file" button that locates import-report.json on disk."""
        from PySide6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QHeaderView,
            QPushButton,
            QTableWidget,
            QTableWidgetItem,
            QVBoxLayout as _QVBoxLayout,
        )

        import locate

        dlg = QDialog(self)
        dlg.setWindowTitle(f"{APP_NAME} — Import Notes")
        dlg.resize(640, 420)
        lay = _QVBoxLayout(dlg)
        rows = self.catalog.report.grouped_entries()
        table = QTableWidget(0, 3, dlg)
        table.setHorizontalHeaderLabels(["Kind", "Count", "Examples"])
        table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        for source, kind, count, examples in rows:
            r = table.rowCount()
            table.insertRow(r)
            table.setItem(r, 0, QTableWidgetItem(f"[{source}] {kind}"))
            table.setItem(r, 1, QTableWidgetItem(str(count)))
            table.setItem(r, 2, QTableWidgetItem(", ".join(examples[:20])))
        lay.addWidget(table)

        report_path = (self.cache_dir / REPORT_NAME
                      if self.cache_dir is not None else None)
        reveal_btn = QPushButton("Reveal report file")
        reveal_btn.setEnabled(report_path is not None and report_path.is_file())
        if report_path is not None:
            reveal_btn.clicked.connect(
                lambda: locate.reveal_in_file_manager(report_path))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.addButton(reveal_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(dlg.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(
            dlg.accept)
        lay.addWidget(buttons)
        dlg.exec()

    def _selection_changed(self, selection: set) -> None:
        """Grid multi-select -> status label (spec §5 dual mode): exactly
        one selected shows that photo's metadata; several show the
        aggregate count; none clears (fauxcasa-q6l.1). Same dual mode
        drives the inspector panel (fauxcasa-q6l.25) when it's visible —
        reusing this existing path rather than adding new plumbing."""
        if len(selection) == 1:
            self._photo_selected(next(iter(selection)))
        elif selection:
            self.meta_label.setText(f"{len(selection)} photos selected  ")
            if self.info_action.isChecked():
                self.inspector.set_many(len(selection))
        else:
            self.meta_label.setText("")
            if self.info_action.isChecked():
                # Empty selection still has a keyboard-focused current
                # item — show it, matching what toggling the panel ON
                # with nothing selected shows (_toggle_inspector).
                if self.grid.current >= 0:
                    self._refresh_inspector(
                        self.catalog.photos[self.grid.current])
                else:
                    self.inspector.set_none()

    def _photo_selected(self, idx: int) -> None:
        """Single-photo status readout (§5 dual mode): path, star count
        (★ repeated — one glyph per star, so a count > 1 reads at a
        glance), capture date, geotag coordinates (§3 geotag v1 display,
        fauxcasa-cam.9/.10/.11), caption, keywords, a "Picasa-saved
        original kept" chip when a stash copy exists (fauxcasa-cam.19),
        and image dimensions + human-readable file size when known
        (fauxcasa-q6l.12). Also feeds the inspector panel (fauxcasa-q6l.25)
        when visible — this one method already fires on grid selection,
        viewer navigation (photo_shown), and after a star toggle, so the
        panel needs no new wiring."""
        if idx < 0:
            self.meta_label.setText("")
            if self.info_action.isChecked():
                self.inspector.set_none()
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
        if p.dims is not None:
            parts.append(f"{p.dims[0]} × {p.dims[1]}")
        if p.size >= 0:
            parts.append(format_file_size(p.size))
        self.meta_label.setText("   ".join(parts) + "  ")
        if self.info_action.isChecked():
            self._refresh_inspector(p)

    def _refresh_inspector(self, photo: Photo) -> None:
        """Populate the inspector panel for `photo` (fauxcasa-q6l.25).
        Album UID -> display-name resolution happens HERE, not in
        inspector.py (spec: "resolution happens in main.py, not the
        panel") — the same catalog.albums map the sidebar already reads
        (_build_sidebar)."""
        names = [self.catalog.albums[uid].name for uid in photo.albums
                 if uid in self.catalog.albums]
        self.inspector.set_photo(photo, album_names=names)

    def _toggle_inspector(self, checked: bool) -> None:
        """Show/hide the inspector panel (fauxcasa-q6l.25) and refresh it
        from whatever the CURRENT view already has selected on show —
        "when the panel is hidden, skip populate work but refresh once on
        show" (spec). Works from either page: the panel is one instance
        outside the pages stack (module docstring, main.__init__)."""
        self.inspector.setVisible(checked)
        if not checked:
            return
        if self.pages.currentWidget() is self.viewer:
            idx = self.viewer.current_index()
            if idx >= 0:
                self._refresh_inspector(self.catalog.photos[idx])
            else:
                self.inspector.set_none()
            return
        sel = self.grid.selection
        if len(sel) == 1:
            self._refresh_inspector(self.catalog.photos[next(iter(sel))])
        elif sel:
            self.inspector.set_many(len(sel))
        elif self.grid.current >= 0:
            self._refresh_inspector(self.catalog.photos[self.grid.current])
        else:
            self.inspector.set_none()

    def _refresh_star_count(self) -> None:
        # Deliberately ignores _star_min (fauxcasa-q6l.20 review nit 8):
        # this is the sidebar's total-starred FACT (>=1), matching what a
        # threshold of Any would show; the active view's own >=N★ label
        # suffix (_label_with_stars) is what discloses the narrower count.
        reveal = self.grid.reveal
        n = sum(1 for p in self.catalog.photos
                if (p.visible or reveal) and p.star)
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == ("starred", ""):
                it.value().setText(0, f"Starred  ({n})")
                return
            it += 1

    def _toggle_grid_stars(self) -> None:
        indices = list(self.grid.selection)
        if not indices and self.grid.current >= 0:
            indices = [self.grid.current]
        self._toggle_stars(indices)

    def _clear_grid_stars(self) -> None:
        """Shift+Space in the grid (fauxcasa-q6l.20 clause c): clear stars
        on the WHOLE current selection, same scope Ctrl+A just selected —
        or the current photo alone with no multi-selection, matching
        _toggle_grid_stars' fallback."""
        indices = list(self.grid.selection)
        if not indices and self.grid.current >= 0:
            indices = [self.grid.current]
        self._clear_stars(indices)

    def _clear_stars(self, indices: list[int]) -> None:
        """Bulk-unstar (fauxcasa-q6l.20 clause c, spec §3): set star=0 on
        every given photo — unconditionally (unlike _toggle_stars' mixed-
        selection normalization, "clear" has only one outcome), so a
        selection scoped to a folder/search/Starred view only ever clears
        what's actually in that scope."""
        self._set_stars(indices, 0)

    def _menu_clear_stars(self) -> None:
        """View > Clear Star(s): the same bulk-unstar as Shift+Space,
        dispatched to whichever surface is showing (fauxcasa-q6l.20 clause
        c). There is no pre-existing "toggle star" menu action to sit next
        to — star toggle has always been Space-only, dispatched from grid/
        viewer keyPressEvent — so this is the feature's one menu entry
        point."""
        if self.pages.currentWidget() is self.viewer:
            idx = self.viewer.current_index()
            if idx >= 0:
                self._clear_stars([idx])
        else:
            self._clear_grid_stars()

    def _toggle_stars(self, indices: list[int]) -> None:
        """Picasa Space semantics, persisted only in Fauxcasa's cache.

        A mixed selection is normalized to starred; an all-starred selection
        is cleared. This makes one press deterministic for bulk selection.
        """
        valid = sorted({
            i for i in indices if 0 <= i < len(self.catalog.photos)
        })
        if not valid:
            return
        target = 0 if all(self.catalog.photos[i].star for i in valid) else 1
        self._set_stars(valid, target)

    def _set_stars(self, indices: list[int], target: int) -> None:
        """The shared tail of _toggle_stars/_clear_stars (fauxcasa-q6l.20
        review finding 6 — this used to be duplicated near-verbatim in
        both): set `target` on every valid index in ONE starstore save,
        then refresh every surface that reads star state."""
        indices = sorted({
            i for i in indices if 0 <= i < len(self.catalog.photos)
        })
        if not indices:
            return
        for i in indices:
            photo = self.catalog.photos[i]
            photo.star = target
            self.star_overrides[photo_key(photo)] = target
        try:
            save_star_overrides(self.state_dir, self.star_overrides)
        except OSError as e:
            log.error("could not save star choices: %s", e)
            self.statusBar().showMessage(
                "star changed for this session; could not save it", 8000)
        self.grid.viewport().update()
        self.viewer.update()
        self.tray.update()
        self._refresh_star_count()
        self._resync_starred_view()
        if self.pages.currentWidget() is self.viewer:
            self._photo_selected(self.viewer.current_index())
        elif len(self.grid.selection) > 1:
            # Dual mode (§5): a bulk star over a multi-selection must
            # keep the status/inspector in "N photos selected" — jumping
            # to the current photo's single readout would contradict the
            # grid's still-live selection (fauxcasa-q6l.25 review).
            self._selection_changed(self.grid.selection)
        else:
            self._photo_selected(self.grid.current)

    def _resync_starred_view(self) -> None:
        """Re-materialize the active view after a star change made from
        INSIDE it (fauxcasa-6vk finding 1). `set_filter` snapshots the
        matching catalog indices, so a star change that drops a photo out
        of the CURRENTLY DISPLAYED scope otherwise leaves a stale tile —
        and a stale status-line count — until the next view switch.

        Runs when the Starred collection is the active sidebar selection
        OR a star threshold is active (fauxcasa-q6l.20 review finding 4):
        either can drop a photo out of scope, whether that scope is the
        sidebar's Starred view, a plain folder/album view narrowed by
        threshold, or a search scoped by both — so the search-vs-view
        dispatch below mirrors _apply_star_min's.

        Grid page only. The VIEWER's display list is deliberately frozen
        for the duration of a navigation session (a photo unstarred while
        viewing must stay reachable with Left/Right), so it is never
        re-derived here."""
        if self.pages.currentWidget() is self.viewer:
            return
        kind, key = self._selected_view()
        if kind != "starred" and self._star_min <= 0:
            return
        keep = self.grid.current
        pos = self.grid.display_pos.get(keep, 0)
        sb = self.grid.verticalScrollBar()
        frac = sb.value() / sb.maximum() if sb.maximum() > 0 else 0.0
        search_text = self.search.text()
        if search_text.strip():
            self._search_changed(search_text)
        else:
            self._apply_view(kind, key)
        if keep not in self.grid.display_pos and self.grid.display:
            # The current photo just left the view: land on the nearest
            # surviving display position rather than on nothing at all
            # (set_filter clears current to -1 when it vanishes).
            self.grid._select(
                self.grid.display[min(pos, len(self.grid.display) - 1)])
        self.grid.scroll_to_fraction(frac)   # best-effort scroll restore

    def _build_progress(self, done: int, total: int) -> None:
        # No progress_label duplicate here (fauxcasa-ez2.6 §7): the activity
        # row is already visible and carries the same count + percent.
        pct = round(100 * done / total) if total else 0
        self._show_activity(
            f"Indexing thumbnails — {done:,} of {total:,} ready ({pct}%)",
            done, total)

    def _show_activity(self, text: str, done: int | None = None,
                       total: int | None = None) -> None:
        self.activity_label.setText(text)
        if done is None or total is None:
            self.activity_progress.setRange(0, 0)  # honest busy indicator
            self.activity_progress.setFormat("")
        else:
            maximum = max(1, total)
            self.activity_progress.setRange(0, maximum)
            self.activity_progress.setValue(min(done, maximum))
            self.activity_progress.setFormat("%v / %m photos   %p%")
        self.activity_row.show()

    def _hide_activity(self) -> None:
        self.activity_row.hide()

    # ---------- viewer ----------

    def _open_viewer(self, _idx: int, display: list, pos: int) -> None:
        self.back_action.setVisible(True)
        self.pages.setCurrentWidget(self.viewer)
        self.viewer.show_photo(list(display), pos)
        self.viewer.setFocus()

    def _close_viewer(self, idx: int) -> None:
        self.back_action.setVisible(False)
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
            self._slideshow.setWindowIcon(self.windowIcon())  # own top-level
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

    def _play_group(self, folder_key: str) -> None:
        """Start a slideshow over a specific folder group's items
        (fauxcasa-q6l.16 — per-group play button in the grid header).
        Looks up the group by folder key in the current grid and plays
        its items from position 0; no-op if the group is missing or
        empty (can happen when the grid view just changed)."""
        group = next(
            (g for g in self.grid.groups if g.folder == folder_key), None)
        if group is None or not group.items:
            return
        display = list(group.items)
        if self._slideshow is None:
            self._slideshow = SlideshowPage(self.catalog, self.grid.thumbs)
            self._slideshow.setWindowIcon(self.windowIcon())  # own top-level
            self._slideshow.closed.connect(self._slideshow_closed)
        else:
            self._slideshow.catalog = self.catalog
            self._slideshow.set_thumbs(self.grid.thumbs)
        self._slideshow.start(display, 0)

    # ---------- hover peek (fauxcasa-q6l.5) ----------

    def _show_peek(self, idx: int) -> None:
        """Ctrl+Alt hover on a grid photo (the grid's trigger machine):
        full-screen peek on the screen containing the cursor. The surface
        is input-transparent and non-activating (peek.py), so the grid
        keeps the focus and the whole event stream — no focus juggling to
        undo on dismissal."""
        if self._peek_page is None:
            self._peek_page = PeekPage(self.catalog, self.grid.thumbs)
            self._peek_page.setWindowIcon(self.windowIcon())  # own top-level
        else:
            # Reused surface: re-point at the live catalog/cache pair (the
            # slideshow discipline — a reconcile may have swapped both).
            self._peek_page.catalog = self.catalog
            self._peek_page.set_thumbs(self.grid.thumbs)
        self._peek_page.peek(idx)

    def _hide_peek(self) -> None:
        if self._peek_page is not None:
            self._peek_page.dismiss()


BUNDLE_RUNTIME_MODULES = (
    "PySide6.QtWidgets",
    # QtMultimedia ships from PySide6-Addons for QAudioSink ONLY (video
    # playback audio, fauxcasa-v46.3). viewer._make_audio_sink guards its
    # import, so a bundle built without Addons — or a binary filter that
    # over-strips Qt6Multimedia — would be silently MUTE; this probe makes
    # --bundle-self-check fail loudly instead.
    "PySide6.QtMultimedia",
    "rawpy",
    "exiv2",
    "PIL.Image",
    # HEIC/HEIF opener (fauxcasa-y5b, pillowload.py): pi_heif ships native
    # libheif/libde265 binaries alongside its Python code, which
    # PyInstaller collects via the spec's `pi_heif` hiddenimport and its
    # binary-dependency walker (see fauxcasa-tracer.spec) — this probe
    # catches a build that silently lost it.
    "pi_heif",
    "av",
    "zstandard",
)


def _bundle_dependency_failures() -> dict[str, str]:
    """Import every lazily loaded shipping dependency inside the artifact.

    PyInstaller can finish successfully when a build environment omits one
    of these modules: the app then degrades at runtime (missing metadata,
    RAW/video/PSD/HEIC support) instead of failing the build. Return only
    module and exception *type* so CI diagnostics cannot leak paths or file
    data.
    """
    failures: dict[str, str] = {}
    for name in BUNDLE_RUNTIME_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — probe reports all failures
            failures[name] = type(exc).__name__
    return failures


def main() -> int:
    if "--worker" in sys.argv[1:]:
        # Video-decode worker re-entry (fauxcasa-v46.3): a FROZEN bundle
        # spawns its videostream worker as [sys.executable, "--worker",
        # ...] — sys.executable IS this app — so dispatch to the worker
        # entrypoint BEFORE argparse can reject the flag (and before any
        # stdout print corrupts the framed wire protocol).
        import videostream

        return videostream._worker_main(sys.argv[1:])
    # Taskbar identity before ANY QApplication — the frozen first-run
    # picker (_prompt_for_library) may construct one long before the
    # main window's own creation below.
    _set_windows_app_user_model_id()
    # --help is USER-facing: what the app is, the read-only promise, and
    # where its two writable artifacts live. The module docstring above
    # stays the developer's map (bead ids, repo-relative commands) and is
    # deliberately not reused here (rel-0.1 identity, fauxcasa-ez2.3).
    ap = cli.build_parser(
        prog=APP_SLUG,
        description=(
            f"{APP_NAME} {__version__} — browse your photo library: folders, "
            "albums, people, stars, search, slideshow.\n"
            "Read-only: your photos, Picasa sidecars and database are never "
            "modified. The only things written are a rebuildable catalog + "
            "thumbnail cache and a log file, both under the cache directory "
            "(--cache-root shows/overrides where that is)."),
        version_text=version_string())
    args = ap.parse_args()
    if args.require_sandbox:
        os.environ["FAUXCASA_DECODE_SANDBOX"] = "require"
    # P3 finding: validate FAUXCASA_DECODE_SANDBOX here, at startup -- a
    # bad value must be a clear error, never fail-open. decodefacade.
    # sandbox_mode() raises ValueError for a bad value; previously that
    # only surfaced from inside DecodeService.ensure_started()'s first
    # call, AFTER it had already marked itself started, so every later
    # call silently returned in-process with no error at all.
    import decodefacade
    try:
        decodefacade.sandbox_mode()
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        if FROZEN:
            # A console-less frozen exe has no stderr any user will ever
            # see -- the app just silently disappears on a bad env value
            # (release-0.1 review P2-5). No QApplication exists yet this
            # early in startup, and QMessageBox needs one -- construct a
            # throwaway instance just for this one dialog.
            _err_app = QApplication.instance() or QApplication([])
            QMessageBox.critical(None, APP_NAME, f"error: {e}")
        return 2
    if args.bundle_self_check:
        failures = _bundle_dependency_failures()
        print(json.dumps({
            "event": "bundle-self-check",
            "modules": list(BUNDLE_RUNTIME_MODULES),
            "failures": failures,
        }, sort_keys=True), flush=True)
        return 1 if failures else 0
    if args.cache_root is None:
        args.cache_root = _default_cache_root()

    # Wire diagnostics before anything that can log: the rotating log file
    # lives beside the per-library caches (top-level, like config.json), so a
    # console=False build still has a record of warnings, Qt messages, and
    # uncaught tracebacks (fauxcasa-pqw).
    applog.setup(args.cache_root, APP_SLUG)
    log.info("%s starting on %s", version_string(), platform.platform())

    # Multi-root management actions (bead .d, design §10): each is a
    # standalone on-disk operation, never the normal open — see the
    # docstrings on _cmd_promote/_cmd_add_root/_cmd_import_picasa_watched.
    if args.promote:
        return _cmd_promote(args.library, args.cache_root)
    if args.add_root is not None:
        return _cmd_add_root(args.library, args.cache_root, args.add_root)
    if args.import_picasa_watched is not None:
        return cli._cmd_import_picasa_watched(args.library,
                                              args.import_picasa_watched)

    root = _resolve_library(args.library, args.cache_root)
    if root is None:
        return 2
    # One volume-probe cache for the whole open-and-scan operation
    # (fauxcasa-t0a): the resolve pass here and load_catalog's
    # refresh_offline_ids below share results, so N UUID-bound roots cost
    # N probes per open instead of up to 2N serial subprocess launches
    # (macOS diskutil). Scoped to THIS open only — later reconcile passes
    # probe freshly so remounts are always re-observed.
    open_probes = volumes.ProbeCache()
    cfg = library.resolve_open_path(root, open_probes)

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
    # Stat BEFORE the read (Codex review round-2 finding 2 — same
    # guiding principle as catalog._read_folder_ini's own pre-read stat:
    # a freshness signature must never be NEWER than the content it
    # describes). _scan_library_config below can take a long time on a
    # big library; stamping stat_sig(contacts_path) only AFTER it returns
    # would let an external contacts.xml edit DURING the scan pair NEW
    # content with a signature that already reads as current, so a later
    # reconcile would never notice the edit. Capturing it here instead —
    # right before contacts.xml is actually read — pairs whatever gets
    # parsed with the OLDER pre-read signature, so a rewrite mid-scan is
    # self-healed by one spurious rebuild on the next reconcile instead
    # of persisting stale content forever.
    contacts_sig = stat_sig(contacts_path)
    if contacts_path is not None:
        contacts = load_contacts_xml(contacts_path)
        if contacts:
            log.info("contacts: %d names from %s", len(contacts),
                     contacts_path)
        elif args.contacts is not None:
            log.warning("no contacts loaded from %s", contacts_path)

    # Picasa2Albums .pal files (fauxcasa-cam.8) / db3 rescue import
    # (fauxcasa-cam.6/.7): an explicit --pal-dir/--db3 wins (the
    # PicasaStarter-relocation case), else the machine-local default when
    # present AND the opened root actually relates to Picasa's own
    # library (fauxcasa-ez2.13) — the machine-local default is keyed on
    # THIS machine, not the opened library, so without this gate opening
    # any folder on a machine that once ran Picasa floods the import
    # report with db3_path_unresolved noise about some other library.
    # Read-only enrichment either way: absence is never fatal, an
    # explicitly named directory that doesn't exist earns a warning.
    rescue_explicit = args.pal_dir is not None or args.db3 is not None
    rescue_enabled, rescue_reason = _db3_rescue_enabled(root, rescue_explicit)
    log.info("db3/pal rescue: %s (%s)",
             "enabled" if rescue_enabled else "skipped", rescue_reason)

    pal_dir = args.pal_dir or (default_pal_dir() if rescue_enabled else None)
    if pal_dir is not None and not pal_dir.is_dir():
        if args.pal_dir is not None:
            log.warning("--pal-dir %s is not a directory; ignored", pal_dir)
        pal_dir = None
    if pal_dir is not None:
        log.info("albums: merging .pal files from %s", pal_dir)

    db3_dir = args.db3 or (default_db3_dir() if rescue_enabled else None)
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
    # Explicit libraries key the cache directory on durable library_id;
    # implicit legacy opens retain the exact historical path digest.
    library_key = cfg.library_id or str(root.resolve())
    cache_dir = cache_dir_for(library_key, args.cache_root,
                              scan_filter.cache_key()
                              + exts_cache_key(excluded_exts))
    # User choices (stars, sort modes) are keyed on the LIBRARY, never on
    # the walk variant above (fauxcasa-6vk finding 2) — the two dirs are
    # the same path whenever the walk is the default one.
    state_dir = library_state_dir(library_key, args.cache_root)
    cat_path = cache_dir / "catalog.json"
    if adopt and not cfg.is_legacy:
        log.error("--thumbs is a single-cache legacy option; explicit "
                  "libraries use one managed fcache per root")
        return 2
    if cfg.is_legacy:
        cache_paths = [(LEGACY_ROOT_ID,
                        args.thumbs if adopt
                        else cache_dir / fcache_name(LEGACY_ROOT_ID))]
    else:
        cache_paths = [(r.id, cache_dir / fcache_name(r.id))
                       for r in cfg.roots]

    catalog: Catalog | None = None
    thumbs: object | None = None
    build_dir: Path | None = None
    warm = False
    # Non-blocking first run (fauxcasa-q6l.13): set True only by the cold
    # non-adopt branch below — main() then defers to win._start_cold_scan
    # after win.show() instead of walking here on the startup critical path.
    cold_scan_needed = False

    t_prep = time.perf_counter()
    if not args.rebuild:
        loaded = load_catalog(cat_path, cfg, probes=open_probes)
        if loaded is not None and all(path.is_file()
                                      for _rid, path in cache_paths):
            try:
                caches = {}
                for root_id, path in cache_paths:
                    cached = load_cache(path)
                    bind(cached, loaded,
                         root_id=None if cfg.is_legacy else root_id)
                    caches[root_id] = cached
                thumbs = (next(iter(caches.values())) if len(caches) == 1
                          else CompositeThumbCache(loaded, caches))
                catalog, warm = loaded, True
                # the report was persisted beside the catalog at scan time;
                # re-attach it so the status-bar count survives warm starts
                catalog.report = load_report(cache_dir / REPORT_NAME)
            except (CacheError, OSError) as e:
                log.warning("persisted cache unusable (%s); rescanning", e)
        if loaded is not None and catalog is None and not cfg.is_legacy:
            # Missing or unusable per-root caches must not discard the
            # loaded catalog (PR #81 review): an offline root walks as
            # EMPTY, so falling through to _scan_library_config would
            # persist a new catalog without that root's photos — the §8
            # offline-tolerance invariant says those entries survive.
            # Keep the catalog and let the cold build re-index caches;
            # _start_cold_build skips offline roots so a good fcache is
            # never overwritten with error blobs.
            log.warning("per-root caches missing or unusable; re-indexing "
                        "from the persisted catalog (no rescan)")
            catalog = loaded
            catalog.report = load_report(cache_dir / REPORT_NAME)
            build_dir = cache_dir

    if catalog is None:  # cold path
        if adopt:
            # Adopt keeps the SYNCHRONOUS walk (fauxcasa-q6l.13 spec item
            # 3): an adopted --thumbs cache is bound to catalog INDICES the
            # scan just produced, so there is no sound empty-placeholder
            # shape to show first here the way the non-adopt branch below
            # can.
            catalog = _scan_library_config(
                cfg, scan_filter, contacts, pal_dir, exts, db3_dir)
            # scan_library/_scan_library_config only see the already-parsed
            # `contacts` name map, never the contacts.xml PATH — stamp the
            # freshness signal captured BEFORE load_contacts_xml ran
            # (above), not a fresh re-stat here (Codex review round-2
            # finding 2): the scan this cold path just ran can take a long
            # time, and re-statting only now would risk pairing a
            # mid-scan external edit's NEW content with a signature that
            # already looks current (fauxcasa-cam.14 step 4).
            catalog.contacts_sig = contacts_sig
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
            # Non-blocking first run (fauxcasa-q6l.13): don't walk here —
            # an unbounded-size NAS library would block first paint and
            # read as a hung app. Build an EMPTY catalog straight FROM cfg
            # (same roots/library_id/root shape _scan_library_config
            # itself would produce) so the window, sidebar, abs() and
            # photos_for_root all see a coherent multiroot shape from the
            # very first paint; the real walk runs on MainWindow's
            # background thread (_start_cold_scan), started right after
            # win.show() below.
            catalog = Catalog(
                root=cfg.roots[0].path, photos=[], folders={}, albums={},
                roots=list(cfg.roots), library_id=cfg.library_id)
            cold_scan_needed = True

    if adopt and isinstance(thumbs, ThumbCache) and thumbs.library \
            and Path(thumbs.library).resolve() != root:
        # bind() compares the full path lists, so a library mismatch with
        # identical walks is survivable — but say so loudly.
        log.warning("adopted cache was built for %r, not %r — entry paths "
                    "match, but thumbnails may be from another library",
                    thumbs.library, str(root))

    prep_ms = (time.perf_counter() - t_prep) * 1000.0
    if warm:
        mode = "warm-load"
    elif adopt:
        mode = "adopt"
    elif cold_scan_needed:
        # fauxcasa-q6l.13: prep_ms here only measures the EMPTY placeholder
        # catalog's construction, not the walk — the walk runs later on
        # MainWindow's background thread and gets its own "cold-scan" event
        # (see _on_scan_done) since prep_ms no longer covers it.
        mode = "cold-scan-deferred"
    else:
        mode = "cold-walk"
    log.info("%s: %d photos, %d folders, %d albums in %.0f ms",
             mode, len(catalog.photos), len(catalog.folders),
             len(catalog.albums), prep_ms)
    if catalog.report.entries:
        # §4: conflicts are surfaced, never silent — the applog line plus
        # the status-bar count are the minimal v1 surface (full inspector
        # is N7/M2). Details live in the persisted import-report.json.
        # On the cold_scan_needed path `catalog` here is the EMPTY
        # placeholder — its report is always entry-free, so this is
        # naturally a no-op, never a duplicate of _on_scan_done's own
        # "import report" log for the real, just-scanned catalog
        # (fauxcasa-q6l.13, Codex cross-vendor review finding 3).
        log.info("import report: %s", catalog.report.summary())

    # A frozen first-run picker may already have created the app.
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    app.setPalette(theme.dark_palette())
    # App-wide default: every top-level (message boxes, the File Types
    # dialog, ...) inherits it; MainWindow/slideshow/peek also set it
    # explicitly so a window built outside main() (tests) carries it too.
    app.setWindowIcon(app_icon())

    # Decode-sandbox session-start decision (fauxcasa-ez2.9 Stage 1, item
    # 6): try the sandbox (or note in-process/off) ONCE, BEFORE
    # MainWindow() is constructed (review P2-7) -- MainWindow.__init__
    # can start _start_cold_build/_start_backfill/_start_reconcile,
    # every one of which reaches thumbcache.build_cache and reads
    # decodefacade.get_service().state at the thumbcache._index_one call
    # site (fauxcasa-ez2.9 Stage 2). Left after window construction (as
    # it was previously), those indexing threads could start and finish
    # entirely on the STATE_IN_PROCESS default before ensure_started()
    # ever ran, decoding unsandboxed while the run later prints
    # {"event": "decode-sandbox", "state": "sandboxed"} -- correct
    # pixels, but an overstated security claim. Nothing below needs the
    # window; the --require-sandbox early `return 1` is now also
    # strictly better -- it no longer flashes a window first. The
    # READY-time event print and the status-bar sync timer stay where
    # they were, after win.show() -- state is already final by then.
    import decodefacade
    decode_svc = decodefacade.get_service()
    try:
        decode_svc.ensure_started()
    except decodefacade.DecodeSandboxRequiredError as e:
        log.error("--require-sandbox: decode sandbox unavailable: %s", e)
        print(f"error: --require-sandbox but the decode sandbox failed to "
              f"start: {e}", file=sys.stderr)
        return 1

    win = MainWindow(catalog, thumbs, cache_dir, build_dir, scan_filter,
                     warm=warm, adopt=adopt, cache_root=args.cache_root,
                     contacts=contacts, pal_dir=pal_dir,
                     excluded_exts=excluded_exts,
                     thumbs_path=args.thumbs, db3_dir=db3_dir,
                     contacts_path=contacts_path, cfg=cfg,
                     contacts_sig=contacts_sig, state_dir=state_dir)
    if args.zoom != 160:
        win.grid.set_zoom(args.zoom)  # direct: skip the slider debounce
        win.zoom.setValue(args.zoom)
    if args.window_size is not None:
        # Before show(), not after: a stable screenshot size regardless of
        # the machine's screen (screenshot testing).
        win.resize(*args.window_size)
    win.show()
    win.grid.setFocus()  # only really lands once the window is mapped

    if cold_scan_needed:
        # Non-blocking first run (fauxcasa-q6l.13): the window is already
        # painted (empty) — start the deferred walk now, off the startup
        # critical path. index_busy() (via _cold_scan_pending) is what
        # keeps a scripted --finish-build run from quitting before it (and
        # the build it chains into) land.
        win._start_cold_scan(cache_dir)

    # The check_ready() scripted-run state machine (fauxcasa-4tu stage 2,
    # commit 3): cli.ScriptedRun owns the ready-poll/decode-sandbox-poll/
    # hard-stop QTimers and the READY-instrumentation state that used to be
    # a closure over main()'s own locals. start()/stop() bracket app.exec()
    # so every timer this run owns is parented to `win` and explicitly
    # stopped before shutdown (fauxcasa-q6l.15, fauxcasa-9pr).
    scripted = cli.ScriptedRun(
        win, app, args, decode_svc=decode_svc, t0=T0, prep_ms=prep_ms,
        cat_path=cat_path, warm=warm, version=__version__, git_sha=GIT_SHA,
        open_wait_ms=OPEN_WAIT_MS, read_rss_mb=read_rss_mb)
    scripted.start()

    code = app.exec()
    scripted.stop()  # belt to the parenting suspenders: never fire post-exec
    win.shutdown()  # reap any in-flight cache build cleanly
    rss, hwm = read_rss_mb()
    print(json.dumps({"event": "exit", "vm_rss_mb": round(rss, 1),
                      "vm_hwm_mb": round(hwm, 1)}), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
