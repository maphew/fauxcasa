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

from catalog import Catalog, scan_library  # noqa: E402
from grid import GridView  # noqa: E402
from thumbcache import (  # noqa: E402
    CacheError,
    ThumbCache,
    bind,
    build_cache,
    cache_dir_for,
    load_cache,
    stale,
)
from viewer import ViewerPage  # noqa: E402

# Single source of truth for the (provisional) product name — nothing
# else may hard-code it.
APP_NAME = "Fauxcasa"

REPO = Path(__file__).resolve().parent.parent


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


class _BuildBridge(QObject):
    progress = Signal(int, int)  # done, total
    finished = Signal(str)  # fcache path, "" on failure


class MainWindow(QMainWindow):
    def __init__(self, catalog: Catalog, thumbs: ThumbCache | None,
                 build_dir: Path | None):
        super().__init__()
        self.catalog = catalog
        self.ready_reported = False
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
        self._show_counts("All photos", catalog.visible_count)

        # --- wiring ---
        self.grid.photo_selected.connect(self._photo_selected)
        self.grid.photo_activated.connect(self._open_viewer)
        self.viewer.closed.connect(self._close_viewer)
        self.viewer.photo_shown.connect(self._photo_selected)

        self.grid.set_data(catalog, thumbs)

        # --- background cache build (small libraries, no cache yet) ---
        # Indexing reports inline (modes, not modals) and feeds tiles to
        # the grid live: the library is browsable immediately.
        self.build_cancel = threading.Event()
        self.build_failed = False
        if build_dir is not None:
            bridge = _BuildBridge()
            bridge.progress.connect(self._build_progress)
            bridge.finished.connect(self._build_finished)
            self._bridge = bridge  # keep alive

            def work() -> None:
                def cb(i: int, total: int, img) -> None:
                    if img is not None:
                        self.grid.feed_tile(i, img)
                    else:
                        self.grid.feed_error(i)
                    if i % 5 == 0 or i == total - 1:
                        bridge.progress.emit(i + 1, total)

                try:
                    out = build_cache(self.catalog, build_dir, cb,
                                      cancel=self.build_cancel)
                    if out is not None:
                        bridge.finished.emit(str(out))
                except RuntimeError:
                    pass  # Qt objects torn down during shutdown
                except Exception as e:  # report, never crash the UI
                    print(f"cache build failed: {e}", file=sys.stderr)
                    try:
                        bridge.finished.emit("")
                    except RuntimeError:
                        pass

            self._build_thread = threading.Thread(target=work, daemon=True)
            self._build_thread.start()

    def shutdown(self) -> None:
        """Stop and reap the build thread so it can't be mid-write (or
        inside a Qt codec) while the interpreter tears down."""
        self.build_cancel.set()
        t = getattr(self, "_build_thread", None)
        if t is not None and t.is_alive():
            t.join(timeout=5.0)

    def closeEvent(self, event) -> None:
        self.build_cancel.set()
        super().closeEvent(event)

    # ---------- sidebar ----------

    def _build_sidebar(self) -> None:
        cat = self.catalog
        t = self.tree
        all_item = QTreeWidgetItem(
            t, [f"All photos  ({cat.visible_count})"])
        all_item.setData(0, Qt.ItemDataRole.UserRole, ("all", ""))
        starred = sum(
            1 for p in cat.photos if p.visible and p.star)
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
            if folder.photo_count == 0:
                continue  # stash/hidden-only folders stay out of the tree
            item = node_for(rel)
            item.setText(0, f"{folder.title}  ({folder.photo_count})")
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
            self._show_counts("All photos", cat.visible_count)
        elif kind == "starred":
            idxs = [i for i, p in enumerate(cat.photos)
                    if p.visible and p.star]
            self.grid.set_filter(idxs, "Starred")
            self._show_counts("Starred", len(idxs))
        elif kind == "folder":
            self.grid.set_filter(None, "")
            self.grid.scroll_to_folder(key)
            self._show_counts("All photos", cat.visible_count)
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
            self._show_counts("All photos", cat.visible_count)
            return
        idxs = [
            i for i, p in enumerate(cat.photos)
            if p.visible and (
                text in p.name.lower()
                or (p.caption and text in p.caption.lower())
                or any(text in k.lower() for k in p.keywords)
            )
        ]
        self.grid.set_filter(idxs, f"search: {text}")
        self._show_counts(f"Search “{text}”", len(idxs))

    # ---------- status ----------

    def _show_counts(self, label: str, n: int) -> None:
        folders = sum(1 for f in self.catalog.folders.values()
                      if f.photo_count)
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

    def _build_finished(self, path: str) -> None:
        self.progress_label.setText("")
        if not path:
            self.build_failed = True
            self.statusBar().showMessage(
                "thumbnail cache build failed — see stderr", 10000)
            return
        try:
            cache = load_cache(Path(path))
            bind(cache, self.catalog)
            self.grid.set_thumbs(cache)
        except CacheError as e:
            print(f"built cache failed to bind: {e}", file=sys.stderr)

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
    ap.add_argument("--cache-root", type=Path,
                    default=REPO / "cache" / "tracer-cache",
                    help="where the app keeps its own disposable caches")
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

    root = Path(args.library).expanduser().resolve()
    if not root.is_dir():
        print(f"library not found: {root}", file=sys.stderr)
        return 2

    t_scan = time.perf_counter()
    catalog = scan_library(root)
    scan_ms = (time.perf_counter() - t_scan) * 1000.0
    print(f"scan: {len(catalog.photos)} photos, "
          f"{len(catalog.folders)} folders, {len(catalog.albums)} albums "
          f"in {scan_ms:.0f} ms", file=sys.stderr)

    thumbs: ThumbCache | None = None
    build_dir: Path | None = None
    if args.thumbs is not None:
        try:
            thumbs = load_cache(args.thumbs)  # adopted caches must bind
            bind(thumbs, catalog)
        except (CacheError, OSError) as e:
            print(f"cannot adopt {args.thumbs}: {e}", file=sys.stderr)
            return 2
        # bind() compares the full path lists, so a library mismatch with
        # identical walks is survivable — but say so loudly, because the
        # adopted cache has no freshness signals at all.
        if thumbs.library and Path(thumbs.library).resolve() != root:
            print(f"WARNING: adopted cache was built for "
                  f"{thumbs.library!r}, not {str(root)!r} — entry paths "
                  f"match, but thumbnails may be from another library",
                  file=sys.stderr)
    else:
        cache_dir = cache_dir_for(root, args.cache_root)
        fcache = cache_dir / "thumbs.fcache"
        if not args.rebuild and fcache.is_file():
            try:
                if not stale(cache_dir, catalog):
                    cached = load_cache(fcache)
                    bind(cached, catalog)
                    thumbs = cached
            except CacheError as e:
                print(f"existing cache unusable ({e}); rebuilding",
                      file=sys.stderr)
        if thumbs is None:
            build_dir = cache_dir

    app = QApplication([])
    app.setApplicationName(APP_NAME)
    win = MainWindow(catalog, thumbs, build_dir)
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
        t = getattr(win, "_build_thread", None)
        if t is not None and t.is_alive():
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
                "scan_ms": round(scan_ms),
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
