#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6"]
# ///
"""Tracer bullet app (fauxcasa-pzx): a thin but real end-to-end slice of
the product on the proposed Python + Qt stack.

    uv run tracer/main.py [LIBRARY] [options]

Layers wired end to end: library scan in place -> machine-local catalog
+ packed thumbnail cache -> folder tree + albums sidebar -> virtualized
grid with group headers, stars, search -> async full-image viewer.
Read-only: the only on-disk output is the app's own disposable cache,
never anything inside the library (N1/N3).

Default library is the synthetic fixture library; for the 100k scale
test, adopt the pre-built benchmark cache:

    uv run tracer/main.py cache/benchmark-library \
        --thumbs cache/benchmark-thumbs.fcache

Headless verification (agents/CI):

    QT_QPA_PLATFORM=offscreen uv run tracer/main.py \
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
from pathlib import Path

T0 = time.perf_counter()

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QSlider,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from catalog import (  # noqa: E402
    Catalog,
    load_catalog,
    reconcile_walk,
    save_catalog,
    scan_library,
)
from grid import GridView  # noqa: E402
from thumbcache import (  # noqa: E402
    CacheError,
    ThumbCache,
    bind,
    build_cache,
    cache_dir_for,
    load_cache,
)
from viewer import ViewerPage  # noqa: E402

# Single source of truth for the (provisional) product name — nothing
# else may hard-code it.
APP_NAME = "Fauxcasa"

REPO = Path(__file__).resolve().parent.parent
FROZEN = getattr(sys, "frozen", False)


def _default_cache_root() -> Path:
    """REPO-relative in a source checkout; a per-user writable dir when
    frozen — REPO then points inside the read-only PyInstaller bundle, so
    the app's own disposable cache must go somewhere writable instead."""
    if FROZEN:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        return Path(base) / "fauxcasa-tracer"
    return REPO / "cache" / "tracer-cache"


def read_rss_mb() -> tuple[float, float]:
    """(rss_mb, hwm_mb); zeros where /proc is unavailable (non-Linux)."""
    rss = hwm = 0.0
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = float(line.split()[1]) / 1024.0
                elif line.startswith("VmHWM:"):
                    hwm = float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return rss, hwm


def _emit(signal, *args) -> None:
    """Emit a bridge signal from a worker thread, swallowing the
    RuntimeError raised if the C++ bridge was already torn down at
    shutdown — a worker must never crash the interpreter on its way out."""
    try:
        signal.emit(*args)
    except RuntimeError:
        pass


class _BuildBridge(QObject):
    progress = Signal(int, int)            # done, total (cold build live feed)
    status = Signal(str)                   # inline status text (reconcile)
    finished = Signal(object, object, bool)  # (IndexResult|None, Catalog, is_reconcile)


class MainWindow(QMainWindow):
    def __init__(self, catalog: Catalog, thumbs: ThumbCache | None,
                 cache_dir: Path | None, build_dir: Path | None,
                 warm: bool = False, adopt: bool = False):
        super().__init__()
        self.catalog = catalog
        self.cache_dir = cache_dir
        self.adopt = adopt
        self.ready_reported = False
        self.build_failed = False
        self.last_index_rate = 0.0
        self.setWindowTitle(f"{APP_NAME} tracer — {catalog.root.name}")
        self.resize(1280, 800)

        self.grid = GridView()
        self.viewer = ViewerPage(catalog)

        # --- sidebar: All / Starred / Folders / Albums ---
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self._build_sidebar()
        self.tree.itemClicked.connect(self._sidebar_clicked)

        # --- toolbar: search + zoom ---
        bar = QToolBar()
        bar.setMovable(False)
        self.addToolBar(bar)
        self.search = QLineEdit()
        self.search.setPlaceholderText(
            "search filename, caption, keywords…")
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
            "Reveal hidden=yes photos and stash-folder files (shown veiled)")
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
        self.pages = QStackedWidget()
        self.pages.addWidget(browser)
        self.pages.addWidget(self.viewer)
        self.setCentralWidget(self.pages)

        # --- status bar ---
        self.setStatusBar(QStatusBar())
        self.counts_label = QLabel()
        self.progress_label = QLabel()
        self.meta_label = QLabel()
        self.statusBar().addWidget(self.counts_label)
        self.statusBar().addWidget(self.progress_label)
        self.statusBar().addPermanentWidget(self.meta_label)
        self._show_counts("All photos", self._shown_count())

        # --- wiring ---
        self.grid.photo_selected.connect(self._photo_selected)
        self.grid.photo_activated.connect(self._open_viewer)
        self.viewer.closed.connect(self._close_viewer)
        self.viewer.photo_shown.connect(self._photo_selected)

        self.grid.set_data(catalog, thumbs)

        # --- background index plumbing (modes, not modals) ---
        # Either a COLD build (no cache yet — feeds tiles live so the
        # library is browsable immediately) or, on a WARM start, a
        # background RECONCILE that confirms the persisted catalog still
        # matches disk and rebuilds if it drifted.
        self.build_cancel = threading.Event()
        self._build_thread: threading.Thread | None = None
        self._reconcile_thread: threading.Thread | None = None
        self._bridge = _BuildBridge()
        self._bridge.progress.connect(self._build_progress)
        self._bridge.status.connect(self._on_status)
        self._bridge.finished.connect(self._on_index_finished)

        if build_dir is not None:
            self._start_cold_build(build_dir)
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
                save_catalog(catalog, build_dir / "catalog.json")
                _emit(bridge.finished, result, catalog, False)
            except Exception as e:  # report, never crash the UI
                print(f"cache build failed: {e}", file=sys.stderr)
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
                drift = reconcile_walk(old, old.root, cancel=self.build_cancel)
            except Exception as e:
                print(f"reconcile walk failed: {e}", file=sys.stderr)
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
                fresh = scan_library(old.root)
                result = build_cache(fresh, cache_dir, None,
                                     cancel=self.build_cancel)
                if result is None:
                    return
                save_catalog(fresh, cache_dir / "catalog.json")
                _emit(bridge.finished, result, fresh, True)
            except Exception as e:
                print(f"reindex failed: {e}", file=sys.stderr)
                _emit(bridge.finished, None, fresh or old, True)

        self._reconcile_thread = threading.Thread(target=work, daemon=True)
        self._reconcile_thread.start()

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
            print(f"built cache failed to bind: {e}", file=sys.stderr)
            return
        if catalog is self.catalog:
            self.grid.set_thumbs(cache)        # cold build: same catalog
        else:
            self.reload_data(catalog, cache)   # reconcile: swap in the new
        self.statusBar().showMessage(
            f"indexed {result.photos} photos at {result.rate:.0f}/s", 8000)

    def reload_data(self, catalog: Catalog, thumbs: ThumbCache) -> None:
        """Atomically swap the whole catalog after a reconcile rebuild:
        re-point grid + viewer, rebuild the sidebar, return to the
        browser (a viewer index may no longer be valid)."""
        self.catalog = catalog
        self.viewer.catalog = catalog
        self.pages.setCurrentWidget(self.pages.widget(0))
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.grid.set_data(catalog, thumbs)
        self.tree.clear()
        self._build_sidebar()
        self._show_counts("All photos", self._shown_count())
        self.meta_label.setText("")

    def index_busy(self) -> bool:
        return any(t is not None and t.is_alive()
                   for t in (self._build_thread, self._reconcile_thread))

    def shutdown(self) -> None:
        """Stop and reap the index threads so neither is mid-write (or
        inside a Qt codec) while the interpreter tears down."""
        self.build_cancel.set()
        for t in (self._build_thread, self._reconcile_thread):
            if t is not None and t.is_alive():
                t.join(timeout=5.0)

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
        sidebar (counts and which folders appear both change) and refreshes
        to the All-photos view — the prior filter may have been a star /
        search / album scoped to visible photos."""
        self.grid.reveal = on
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.tree.clear()
        self._build_sidebar()
        self.grid.set_filter(None, "")
        self._show_counts("All photos", self._shown_count())
        self.grid.setFocus()

    # ---------- sidebar ----------

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
                item = QTreeWidgetItem(
                    albums_root,
                    [f"{album.name}  ({len(album.members)})"])
                item.setData(0, Qt.ItemDataRole.UserRole, ("album", uid))
            albums_root.setExpanded(True)

    def _sidebar_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None:
            return
        kind, key = data
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        cat = self.catalog
        if kind == "all":
            self.grid.set_filter(None, "")
            self._show_counts("All photos", self._shown_count())
        elif kind == "starred":
            idxs = [i for i, p in enumerate(cat.photos)
                    if (p.visible or self.grid.reveal) and p.star]
            self.grid.set_filter(idxs, "Starred")
            self._show_counts("Starred", len(idxs))
        elif kind == "folder":
            self.grid.set_filter(None, "")
            self.grid.scroll_to_folder(key)
            self._show_counts("All photos", self._shown_count())
        elif kind == "album":
            album = cat.albums[key]
            self.grid.set_filter(list(album.members), album.name)
            self._show_counts(f"Album “{album.name}”", len(album.members))
        self.grid.setFocus()

    # ---------- search ----------

    def _search_changed(self, text: str) -> None:
        text = text.strip().lower()
        cat = self.catalog
        if not text:
            self.grid.set_filter(None, "")
            self._show_counts("All photos", self._shown_count())
            return
        idxs = [
            i for i, p in enumerate(cat.photos)
            if (p.visible or self.grid.reveal) and (
                text in p.name.lower()
                or (p.caption and text in p.caption.lower())
                or any(text in k.lower() for k in p.keywords)
            )
        ]
        self.grid.set_filter(idxs, f"search: {text}")
        self._show_counts(f"Search “{text}”", len(idxs))

    # ---------- status ----------

    def _show_counts(self, label: str, n: int) -> None:
        reveal = self.grid.reveal
        folders = sum(1 for f in self.catalog.folders.values()
                      if (f.total_count if reveal else f.photo_count))
        self.counts_label.setText(
            f"  {label}: {n} photos · {folders} folders"
            f" · {len(self.catalog.albums)} albums")

    def _photo_selected(self, idx: int) -> None:
        if idx < 0:
            self.meta_label.setText("")
            return
        p = self.catalog.photos[idx]
        parts = [p.rel]
        if p.star:
            parts.append("★")
        if p.caption:
            parts.append(f"“{p.caption}”")
        if p.keywords:
            parts.append("#" + " #".join(p.keywords))
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


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("library", nargs="?",
                    default=str(REPO / "cache" / "synthetic-library"),
                    help="library root to browse (read-only)")
    ap.add_argument("--thumbs", type=Path, default=None,
                    help="adopt an existing .fcache instead of building "
                         "one (e.g. cache/benchmark-thumbs.fcache)")
    ap.add_argument("--cache-root", type=Path, default=None,
                    help="where the app keeps its own disposable caches "
                         "(default: <repo>/cache/tracer-cache in a checkout, "
                         "a per-user cache dir when run as a frozen bundle)")
    ap.add_argument("--rebuild", action="store_true",
                    help="ignore any existing tracer cache and rebuild")
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
    ap.add_argument("--finish-build", action="store_true",
                    help="scripted runs: wait for an in-flight cache "
                         "build before quitting (warm-run scripting)")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="scripted runs (--screenshot/--quit-after-ready) "
                         "abort with exit 1 after this many seconds")
    args = ap.parse_args()
    if args.cache_root is None:
        args.cache_root = _default_cache_root()

    root = Path(args.library).expanduser().resolve()
    if not root.is_dir():
        print(f"library not found: {root}", file=sys.stderr)
        return 2

    # Data prep. Try a WARM start first: load the persisted catalog (no
    # walk) and bind it to the thumbnail cache. Else fall back to a COLD
    # walk + build (or adopt an external --thumbs cache). The cache dir is
    # always derived from the library root, so even an adopted-cache run
    # persists its catalog and warm-starts next time.
    adopt = args.thumbs is not None
    cache_dir = cache_dir_for(root, args.cache_root)
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
            except (CacheError, OSError) as e:
                print(f"persisted cache unusable ({e}); rescanning",
                      file=sys.stderr)

    if catalog is None:  # cold path
        catalog = scan_library(root)
        if adopt:
            try:
                thumbs = load_cache(args.thumbs)
                bind(thumbs, catalog)
            except (CacheError, OSError) as e:
                print(f"cannot adopt {args.thumbs}: {e}", file=sys.stderr)
                return 2
            save_catalog(catalog, cat_path)  # warm-start next time
        else:
            build_dir = cache_dir  # the build thread persists the catalog

    if adopt and thumbs is not None and thumbs.library \
            and Path(thumbs.library).resolve() != root:
        # bind() compares the full path lists, so a library mismatch with
        # identical walks is survivable — but say so loudly.
        print(f"WARNING: adopted cache was built for {thumbs.library!r}, "
              f"not {str(root)!r} — entry paths match, but thumbnails may "
              f"be from another library", file=sys.stderr)

    prep_ms = (time.perf_counter() - t_prep) * 1000.0
    mode = "warm-load" if warm else ("adopt" if adopt else "cold-walk")
    print(f"{mode}: {len(catalog.photos)} photos, "
          f"{len(catalog.folders)} folders, {len(catalog.albums)} albums "
          f"in {prep_ms:.0f} ms", file=sys.stderr)

    app = QApplication([])
    app.setApplicationName(APP_NAME)
    win = MainWindow(catalog, thumbs, cache_dir, build_dir, warm=warm,
                     adopt=adopt)
    if args.zoom != 160:
        win.grid.set_zoom(args.zoom)  # direct: skip the slider debounce
        win.zoom.setValue(args.zoom)
    win.show()

    # READY instrumentation (§7 cold start): poll until every visible
    # tile is decoded, then report cold start + RSS on stdout.
    state = {"scrolled": False, "shot": False, "opened": False}

    def may_quit() -> bool:
        if not args.finish_build:
            return True
        if win.index_busy():
            return False
        if win.build_failed:
            # --finish-build promised a cache; a failed build must not
            # masquerade as a green run.
            print("cache build failed under --finish-build",
                  file=sys.stderr)
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
                "vm_rss_mb": round(rss, 1),
                "vm_hwm_mb": round(hwm, 1),
            }), flush=True)
            if args.quit_after_ready and args.screenshot is None \
                    and args.scroll_to is None and args.open is None \
                    and may_quit():
                app.quit()
                return
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
                print(f"screenshot: {args.screenshot}", file=sys.stderr)
            else:
                print(f"FAILED to save screenshot to {args.screenshot}",
                      file=sys.stderr)
                app.exit(1)
                return
            app.quit()
        elif args.quit_after_ready:
            app.quit()
        else:
            poll.stop()  # interactive run: instrumentation is done

    poll = QTimer()
    poll.setInterval(50)
    poll.timeout.connect(check_ready)
    poll.start()
    # Hard stop for scripted runs: a stuck decode must fail loudly, not
    # hang CI or masquerade as success.
    if args.screenshot is not None or args.quit_after_ready:
        def on_timeout() -> None:
            print(f"TIMEOUT after {args.timeout}s — "
                  f"ready={win.ready_reported} state={state}",
                  file=sys.stderr)
            app.exit(1)

        QTimer.singleShot(int(args.timeout * 1000), on_timeout)

    code = app.exec()
    win.shutdown()  # reap any in-flight cache build cleanly
    rss, hwm = read_rss_mb()
    print(json.dumps({"event": "exit", "vm_rss_mb": round(rss, 1),
                      "vm_hwm_mb": round(hwm, 1)}), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
