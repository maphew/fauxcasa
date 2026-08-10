"""Virtualized thumbnail grid for the tracer app.

Descends from the proven balloons/py-qt design (custom-painted
virtualized grid, threaded JPEG decode from the packed fcache, bounded
LRU of decoded tiles) with the benchmark hacks replaced by product
behavior: event-driven repaints instead of a 240 Hz timer, a real
scrollbar via QAbstractScrollArea, resize handling, group headers with
a pinned current-folder header, multi-selection (set + current/anchor,
Ctrl/Shift semantics) and activation, star + geotag + video-play corner badges, and
error tiles for undecodable entries. Scrolling reads ONLY the cache
pair, never originals (N4).
"""

from __future__ import annotations

import math
import os
import queue
import threading
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter

from PySide6.QtCore import QObject, QPointF, QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QImage,
    QPainter,
    QPen,
    QPolygonF,
    QTransform,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import keymap
from catalog import Catalog
from keymap import PEEK_MODS  # noqa: F401 -- re-export (see _MOD_OF note)
from locate import reveal_in_file_manager
from thumbcache import THUMB_EDGE, ThumbCache

HEADER_H = 26
GROUP_GAP = 10
PAD = 8
WORKERS = 4
# Sentinel pushed onto the job queue to retire a decode worker cleanly
# (fauxcasa-gfz). Workers otherwise block forever on jobs.get(); without a
# way out they outlive the widget as immortal daemons that accumulate across
# a process and race the main thread on Qt state during teardown.
_STOP = object()


def folder_key(catalog: Catalog, root_id: str, rel: str) -> str:
    """Root-qualified folder identity, with exact one-root passthrough.

    Catalog persistence uses the same absent-means-first-root convention:
    roots[0] keeps the historical bare rel, later roots use
    ``<root_id>/<rel>``.  Keeping this helper on the UI side prevents two
    roots' same-named folders from sharing a grid group, sort mode, or
    sidebar target while leaving every legacy/single-root key unchanged.
    """
    roots = catalog.roots
    if len(roots) <= 1 or (roots and root_id == roots[0].id):
        return rel
    return f"{root_id}/{rel}" if rel else root_id


class CompositeThumbCache:
    """Catalog-index facade over N independent per-root fcaches.

    Existing consumers intentionally keep the small ``ThumbCache`` duck type
    (``count``, ``best_level()``, ``entry()``, ``path``). ``entry`` maps a
    global catalog index to the owning root's local cache index and records
    that cache path thread-locally; the immediately following read therefore
    opens the matching per-root file even in the grid's worker pool.
    """

    def __init__(self, catalog: Catalog, caches: dict[str, ThumbCache]):
        self._caches = dict(caches)
        first_root, first_cache = next(iter(self._caches.items()))
        for root_id, cache in self._caches.items():
            if cache.levels != first_cache.levels:
                raise ValueError(
                    f"CompositeThumbCache: root {root_id!r} has levels "
                    f"{cache.levels!r}, but root {first_root!r} (the first) "
                    f"has {first_cache.levels!r} — every per-root cache must "
                    "share an identical levels list, since best_level() "
                    "resolves an index against only the first root's cache "
                    "and entry() forwards that same index into whichever "
                    "root's cache actually owns the photo")
        next_local = {root_id: 0 for root_id in caches}
        self._entries: list[tuple[ThumbCache, int]] = []
        for photo in catalog.photos:
            cache = self._caches[photo.root_id]
            local = next_local[photo.root_id]
            next_local[photo.root_id] = local + 1
            self._entries.append((cache, local))
        self.count = len(self._entries)
        self.files = [p.rel for p in catalog.photos]
        self._thread = threading.local()

    def best_level(self, edge: int) -> int:
        # Resolves a levels-list INDEX using the first root's cache. This is
        # safe only because __init__ enforces that every per-root cache
        # shares an identical `levels` list (all per-root fcaches are built
        # together with the same configured levels) — entry() then forwards
        # this SAME index into whichever root's cache actually owns the
        # photo, with no per-root fallback. A cache with a different levels
        # list (e.g. a stray v1 single-level cache) would IndexError in
        # entry(); __init__ rejects that combination up front instead.
        return next(iter(self._caches.values())).best_level(edge)

    def entry(self, idx: int, level: int | None = None):
        cache, local = self._entries[idx]
        self._thread.path = cache.path
        return cache.entry(local, level)

    @property
    def path(self):
        path = getattr(self._thread, "path", None)
        if path is not None:
            return path
        return next(iter(self._caches.values())).path
# Decoded-tile RAM bound. The real cost is BYTES, not entries. Tiles are
# kept at native cache resolution (~256 px, TILE_NATIVE) and SCALED IN
# PAINT to the current zoom, so ONE decode serves every zoom level — zoom
# no longer re-decodes the JPEG (fauxcasa-z1e). A square 256x256 RGB32
# tile is 256 KB; a non-square tile fit in the 256 box is smaller (e.g.
# 256x171 ~175 KB) and an error tile is 0 bytes, so a flat entry cap would
# mis-budget both ways. So bound by summed decoded bytes (CACHE_BYTES),
# with a generous entry backstop (CACHE_MAX_ENTRIES) so a degenerate
# all-error library —
# zero-byte tiles, no byte pressure — still can't grow the dict without
# bound. Headroom re-examination for the native-tile design: tiles are now
# uniform across zoom, so the byte budget binds at ~1024 square tiles at
# EVERY zoom (it used to bind only near max zoom, where tiles were already
# 256 px; small-zoom tiles used to be ~16 KB and never byte-bound). On the
# §7 bench window (1280x800) the worst case — min zoom, 1-screen prefetch
# each way — is a ~560-tile want-band ≈ 140 MB, well under CACHE_BYTES with
# LRU slack to spare. The byte budget binds for real tiles; the entry
# backstop only for tiny/error ones. Neither bound ever evicts what the
# current paint wants (the want-band), so eviction can't thrash the tiles a
# paint needs; on an extreme window at min zoom the want-band alone can
# exceed CACHE_BYTES, and _evict keeps it anyway (brief overshoot beats
# thrash). MEASURED (fauxcasa-5br, bench_scroll.py --zoom min on the §7 100k
# library): no scroll-fps regression vs the pre-z1e pre-scaled design at
# default zoom; at MIN zoom the native-tile design costs ~2.6x paint p50 and
# ~+326 MB RSS (pre 266 -> post 592 MB) — still inside the §7 gates (p99
# ~13 ms < 32 ms) but total process RSS peaks ~590 MB: under the ~600 MB
# working target, above the old 512 MB figure. Reclaiming that headroom
# (lower CACHE_BYTES, or zoom-aware tile sizing) is tracked separately.
# NOTE: the px/byte figures above reason at devicePixelRatio 1 (256 px / 256 KB
# tiles). The hi-DPI consumer (fauxcasa-q7m) decodes up to TILE_NATIVE*d, so at
# d==2 tiles are ~512 px / ~1 MB and the want-band's byte footprint is ~d^2
# larger; the budget still binds correctly (eviction sums img.sizeInBytes(), not
# entries), but the worked example is dpr-1 and the d>1 RSS re-measurement is
# owed with the §7 re-baseline (fauxcasa-q7m / fauxcasa-k5p).
CACHE_BYTES = 256 * 1024 * 1024
CACHE_MAX_ENTRIES = 4096
# Native decoded-tile edge at devicePixelRatio 1: the cache's own thumb size
# and the max zoom tile (set_zoom caps self.tile at 256 == THUMB_EDGE). Tiles
# are decoded at (or capped to) the DPR-scaled native edge and scaled to
# self.tile in paint.
#
# fcache v2 hi-DPI consumer (fauxcasa-q7m): on a display with
# devicePixelRatio d, a tile drawn at self.tile LOGICAL px covers self.tile*d
# DEVICE px, so a 256 native tile is upscaled (soft) once d>1. The grid now
# decodes the nearest v2 level >= TILE_NATIVE*d (best_level/entry) and caps to
# TILE_NATIVE*d, so the device footprint is filled at native resolution. The
# base is the MAX tile (TILE_NATIVE), NOT the current self.tile: keeping it
# zoom-independent preserves the z1e invariant (one decode per photo serves
# every zoom; zoom never re-decodes). The bead's "tile*devicePixelRatio" is
# read as max-tile*dpr for that reason. At d==1 best_level(256) selects the
# same 256 level the grid read before, so this is a no-op and the §7 numbers
# (measured at d==1) are unchanged by construction — this no-op holds for any cache that CONTAINS
# a 256 level (v1 [256] and RECOMMENDED_LEVELS both do; a hand-built set
# lacking 256, e.g. [512,128], would instead read the 512 blob capped to 256,
# not the old primary, but no shipped config builds such a set); d>1 reads the
# 512 level of
# a v2 cache (or falls back to the largest level a v1 cache offers) at ~d^2 the
# decoded bytes -> its own RSS/scroll-fps re-measurement is owed (fauxcasa-q7m,
# pairs with the §7 v2 re-baseline fauxcasa-k5p).
TILE_NATIVE = THUMB_EDGE
PREFETCH_SCREENS = 1.0

PLACEHOLDER = QColor(60, 60, 60)
ERROR_TILE = QColor(96, 40, 40)
BACKGROUND = QColor(24, 24, 24)
HEADER_BG = QColor(34, 34, 34)
HEADER_FG = QColor(200, 200, 200)
SELECT = QColor(64, 140, 255)
STAR_GOLD = QColor(255, 200, 40)
GEO_TEAL = QColor(64, 205, 175)  # geotag badge (fauxcasa-cam.10)
PLAY_WHITE = QColor(235, 235, 235)  # video play badge (fauxcasa-v46.2)
HIDDEN_VEIL = QColor(0, 0, 0, 110)  # reveal mode: dim hidden/stash tiles
# Selection pens, hoisted out of the paint loop (fauxcasa-q6l.1): every
# member of the selection set gets the thin rect; the CURRENT item gets the
# stronger border; a current item the user Ctrl-toggled OUT of the set keeps
# a dashed focus cue so keyboard navigation never goes invisible.
PEN_SELECTED = QPen(SELECT, 1)
PEN_CURRENT = QPen(SELECT, 3)
PEN_FOCUS = QPen(SELECT, 1, Qt.PenStyle.DashLine)
# The hover-peek trigger chord (fauxcasa-q6l.5) lives in the keymap —
# the single source of truth for bindings (fauxcasa-q6l.8); re-exported
# here because tests and callers know it as grid.PEEK_MODS.
_MOD_OF = {Qt.Key.Key_Control: Qt.KeyboardModifier.ControlModifier,
           Qt.Key.Key_Alt: Qt.KeyboardModifier.AltModifier}


# ---- per-folder sort modes (fauxcasa-q6l.11) ------------------------------
# Spec §5 names date / name / size / manual; manual (display of Picasa's
# persisted manual order) is blocked on the missing db3 oracle fixture and
# deliberately absent here. Modes apply to the folder-grouped DEFAULT view
# only (All photos / folder navigation — one continuous surface); explicit
# display sets keep their given order: album order is membership order
# (manual/membership order per Picasa — out of scope for this feature),
# search/starred/recent stay in catalog order.
SORT_NAME = "name"
SORT_DATE = "date"
SORT_SIZE = "size"
SORT_MODES = (SORT_NAME, SORT_DATE, SORT_SIZE)
# The DEFAULT is "name", named honestly: walk_library yields sorted(Path)
# — path-COMPONENT order — so within one folder (a shared prefix) catalog
# order IS filename order per pathlib's platform norms (case-insensitive
# on Windows, case-sensitive on POSIX). "Sort by name" therefore keeps the
# group's catalog slice untouched, which both preserves the exact
# pre-q6l.11 display order as the default and avoids inventing a second,
# subtly different name collation next to the walk rule's.
DEFAULT_SORT_MODE = SORT_NAME


def _date_sort_key(photo) -> tuple:
    """Date-mode key: date_taken (canonical 'YYYY-MM-DDTHH:MM:SS' — sorts
    lexically for ANY zero-padded year, pre-1903 included, per §6 footgun
    16: never a year floor), else the file mtime rendered in the same
    canonical local-time form so dated and mtime-only photos interleave
    chronologically, else sink below every dated photo. The (0, str) /
    (1,) shapes compare cleanly and the sunk tail stays deterministic:
    sort stability keeps its incoming name/walk order."""
    if photo.date_taken:
        return (0, photo.date_taken)
    if photo.mtime >= 0:
        try:  # Windows rejects pre-epoch timestamps (OSError); sink those
            stamp = datetime.fromtimestamp(photo.mtime)
        except (OSError, OverflowError, ValueError):
            return (1,)
        return (0, stamp.strftime("%Y-%m-%dT%H:%M:%S"))
    return (1,)


def sort_folder_items(items: list[int], photos, mode: str) -> list[int]:
    """One folder group's catalog indices, reordered per `mode`. Always a
    pure PERMUTATION of `items`: only display order changes, so the
    catalog/fcache index binding (tiles, decode jobs, selection — all
    keyed by catalog index) is untouched by construction. Ascending only
    (v1 keeps direction out of the persisted state — remembering a
    direction is cheap to add at M2 if wanted); ties and unsortable
    entries (no date, unindexed size -1) keep their incoming name/walk
    order via sort stability, the unsortable tail sinking to the end."""
    if mode == SORT_DATE:
        return sorted(items, key=lambda i: _date_sort_key(photos[i]))
    if mode == SORT_SIZE:
        return sorted(items, key=lambda i: (0, photos[i].size)
                      if photos[i].size >= 0 else (1,))
    return list(items)  # SORT_NAME (or unknown): the walk's name order


def _star_polygon(cx: float, cy: float, r: float) -> QPolygonF:
    poly = QPolygonF()
    for i in range(10):
        rad = r if i % 2 == 0 else r * 0.42
        ang = -math.pi / 2 + i * math.pi / 5
        poly.append(QPointF(cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    return poly


def _play_polygon(cx: float, cy: float, r: float) -> QPolygonF:
    """Right-pointing play triangle for the video corner badge
    (fauxcasa-v46.2): (cx, cy) is the shape center, r the half-height —
    the same size convention as _star_polygon/_pin_polygon so the three
    badges read as a set. Width is trimmed (0.75r each way) so the
    triangle's visual weight matches the other glyphs'."""
    poly = QPolygonF()
    poly.append(QPointF(cx - r * 0.75, cy - r))
    poly.append(QPointF(cx + r * 0.75, cy))
    poly.append(QPointF(cx - r * 0.75, cy + r))
    return poly


def _pin_polygon(cx: float, cy: float, r: float) -> QPolygonF:
    """Map-pin teardrop for the geotag corner badge (fauxcasa-cam.10):
    a round head swept as a 13-point arc, closed through a point at the
    bottom. (cx, cy) is the shape center, r the half-height — the same
    size convention as _star_polygon so the two badges read as a set."""
    poly = QPolygonF()
    poly.append(QPointF(cx, cy + r))  # the tip
    head_r, head_cy = r * 0.68, cy - r * 0.28
    for i in range(13):  # lower-right, over the top, to lower-left
        ang = math.radians(40.0 - i * 22.0)
        poly.append(QPointF(cx + head_r * math.cos(ang),
                            head_cy + head_r * math.sin(ang)))
    return poly


@dataclass
class _Group:
    folder: str
    title: str
    items: list[int]  # catalog/cache indices
    y: int = 0  # header top, content coords
    grid_y: int = 0
    height: int = 0  # header + rows, excl. gap


class _Notifier(QObject):
    tile_ready = Signal()


class GridView(QAbstractScrollArea):
    """set_data() once, then set_filter()/set_zoom() as the user drives."""

    photo_selected = Signal(int)  # CURRENT item (catalog index), -1 = none
    photo_activated = Signal(int, list, int)  # catalog idx, display list, pos
    # The multi-select payload (fauxcasa-q6l.1): a set[int] of selected
    # catalog indices (a defensive copy — receivers may keep or mutate it).
    # Emitted whenever the SET changes; photo_selected still tracks the
    # current item for single-photo consumers (viewer open/close).
    selection_changed = Signal(object)
    # Hover full-screen peek trigger (fauxcasa-q6l.5): Picasa's own gesture,
    # Ctrl+Alt while hovering a photo (a bare hover never fires). The grid
    # owns only this trigger state machine; the owner shows/hides the
    # input-transparent full-screen surface (peek.py), which is what keeps
    # every subsequent mouse/key event landing HERE for dismissal.
    peek_requested = Signal(int)  # catalog idx to show (re-emits on retarget)
    peek_released = Signal()      # trigger ended: dismiss the peek
    # Ctrl+H — Picasa's hold key (fauxcasa-q6l.2): ask the owner to add
    # the CURRENT selection set to the selection tray. The grid only
    # emits; the tray (rel-path identity, hold order) is MainWindow's.
    hold_requested = Signal()
    # Space — MainWindow applies Picasa's add/remove-star semantics and owns
    # the machine-local persistence.
    star_toggle_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.catalog: Catalog | None = None
        self.thumbs: ThumbCache | None = None
        self.tile = 160
        self.groups: list[_Group] = []
        self.display: list[int] = []  # flat catalog indices in display order
        self.display_pos: dict[int, int] = {}
        self.loc: dict[int, tuple[int, int]] = {}  # idx -> (group_i, n)
        self.filter_label = ""
        self.content_h = 0
        self.cols = 1
        # Selection model (fauxcasa-q6l.1): a SET of selected catalog
        # indices plus a current item (keyboard focus, what Enter opens,
        # what the stronger border marks) and an anchor (where Shift
        # ranges grow from). Plain click collapses the set to one; Ctrl
        # toggles membership; Shift selects the anchor..hit range in
        # display order.
        self.selection: set[int] = set()
        self.current = -1  # catalog index, -1 = none
        self.anchor = -1   # catalog index Shift-ranges grow from
        # Reveal toggle: when True the default ("All photos"/folder) view
        # also shows hidden=yes photos and stash-folder files, veiled so
        # they read as not-normally-shown. Owned/driven by MainWindow.
        self.reveal = False
        # Per-folder sort modes (fauxcasa-q6l.11): folder rel-path ->
        # SORT_* constant; an absent key means DEFAULT_SORT_MODE (name =
        # walk order). Loaded/persisted by MainWindow (machine-local
        # per-library config — see main._set_folder_sort); the grid only
        # consumes it when set_filter builds the default view's groups.
        self.sort_modes: dict[str, str] = {}
        # Hover-peek state (fauxcasa-q6l.5): the last known viewport hover
        # position (mouse tracking below feeds it button-free, so a later
        # Ctrl+Alt press can trigger without a move — Picasa's actual
        # gesture), the idx currently peeked (-1 = none), and the Esc/click
        # suppression that holds re-trigger until Ctrl+Alt drop.
        self._hover: QPointF | None = None
        self._peek_idx = -1
        self._peek_suppressed = False

        # decode machinery (balloon lineage)
        self.generation = 0  # bumped on zoom/cache change; stale results drop
        self.jobs: queue.SimpleQueue = queue.SimpleQueue()
        self.done: queue.SimpleQueue = queue.SimpleQueue()
        self.pending: set[int] = set()
        self.pending_lock = threading.Lock()
        self.tiles: dict[int, list] = {}  # idx -> [QImage|None(error), frame, nbytes]
        self._cache_bytes = 0  # running sum of tiles' decoded bytes (CACHE_BYTES)
        # DPR-scaled native decode edge (fauxcasa-q7m). Read on the GUI thread
        # in paintEvent (devicePixelRatioF needs a screen) and stashed for the
        # decode workers; TILE_NATIVE until the first paint resolves the real
        # ratio. A plain int — workers only read it.
        self._tile_native = TILE_NATIVE
        self.frame_no = 0
        # Optional bench hook: callable(t_start, t_end, blank). Set by the
        # scroll benchmark (apps/desktop-python/bench_scroll.py) to time frame
        # production and count blank-tile frames; None in normal use, so
        # the per-frame cost is one identity compare.
        self._frame_probe = None
        # Indices the last paint wanted (visible + prefetch). Workers drop
        # jobs that fell out of this set — a fast scrollbar drag would
        # otherwise leave thousands of stale decodes ahead of the visible
        # ones in the FIFO. Replaced wholesale each paint (atomic ref).
        self.wanted: frozenset[int] = frozenset()
        self._notifier = _Notifier()
        self._notifier.tile_ready.connect(self.viewport().update)
        self._stopping = False
        self._workers = [
            threading.Thread(target=self._decode_worker, daemon=True,
                             name=f"grid-decode-{i}")
            for i in range(WORKERS)
        ]
        for t in self._workers:
            t.start()

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.verticalScrollBar().valueChanged.connect(
            lambda _v: self.viewport().update()
        )
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        # Button-free hover for the peek trigger; the tooltip is the
        # discoverability hint for the modifier chord (it has no other UI).
        self.viewport().setMouseTracking(True)
        self.setToolTip("Ctrl+Alt while hovering a photo: full-screen peek")
        self._make_jump_cluster()

    def _make_jump_cluster(self) -> None:
        """Jump-to-folder/end buttons beside the scrollbar (fauxcasa-q6l.9).
        Spec §5 keeps a conventional scrollbar PLUS Picasa's jump buttons
        (the recentering thumb was deliberately dropped). addScrollBarWidget
        puts the cluster in the vertical scrollbar's own column, below the
        bar, sized to the bar's width — it appears and hides with the bar,
        so it never exists when everything already fits. No key bindings
        here: the research corpus documents no Picasa keys for these
        buttons, and the keymap layer is fauxcasa-q6l.8."""
        ext = self.style().pixelMetric(
            QStyle.PixelMetric.PM_ScrollBarExtent, None, self)
        cluster = QWidget(self)
        lay = QVBoxLayout(cluster)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        def btn(glyph: str, tip: str, slot) -> QToolButton:
            b = QToolButton(cluster)
            b.setText(glyph)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.setFixedSize(ext, ext)
            # Never steal keyboard focus from the grid (triage keys live
            # there); these are mouse affordances only.
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(slot)
            lay.addWidget(b)
            return b

        self.btn_top = btn("⤒", "Jump to top", self.jump_to_top)
        self.btn_prev_folder = btn("↑", "Previous folder (from mid-folder: "
                                   "top of this folder)", self.jump_prev_folder)
        self.btn_next_folder = btn("↓", "Next folder", self.jump_next_folder)
        self.btn_end = btn("⤓", "Jump to end", self.jump_to_end)
        self.addScrollBarWidget(cluster, Qt.AlignmentFlag.AlignBottom)

    # ---------- data & layout ----------

    def set_data(self, catalog: Catalog, thumbs: ThumbCache | None) -> None:
        """Point the grid at a catalog + cache. Also used by reload_data to
        swap in a reconciled catalog mid-session, so it must invalidate the
        decoded-tile cache: tiles are keyed by catalog index, and after a
        rebuild index i is a different photo from a different fcache inode.
        (No-op cost on the initial empty-grid call.)"""
        self.catalog = catalog
        self.thumbs = thumbs
        self._invalidate_tiles()
        self.set_filter(None, "")

    def set_thumbs(self, thumbs: ThumbCache) -> None:
        """Swap in a freshly built cache. Invalidate so every tile
        re-decodes from the new cache at native size (paint scales it)."""
        self.thumbs = thumbs
        self._invalidate_tiles()
        self.viewport().update()

    def feed_tile(self, idx: int, img: QImage) -> None:
        """Live tile from the in-app cache builder (any thread). Arrives
        at native thumb size; rotate here so all display paths agree."""
        # The done queue only drains on paint, and hidden/minimized
        # windows don't paint — bound the backlog (~130 MB worst case)
        # rather than buffer a whole library; dropped tiles re-decode
        # from the finished cache after set_thumbs anyway.
        if self.done.qsize() >= 512:
            return
        if self.catalog is not None:
            rot = self.catalog.photos[idx].rotate
            if rot:
                img = img.transformed(QTransform().rotate(90 * rot))
        self.done.put((self.generation, idx, img))
        self._notifier.tile_ready.emit()

    def feed_error(self, idx: int) -> None:
        """Undecodable file seen by the builder -> error tile now, so
        READY doesn't stall on a corrupt file in the first viewport."""
        self.done.put((self.generation, idx, None))
        self._notifier.tile_ready.emit()

    def set_filter(self, indices: list[int] | None, label: str) -> None:
        """indices=None -> all visible photos grouped by folder; otherwise
        an explicit display set (album members, stars, search hits).
        Grouping is by folder key (not consecutive runs): with nested
        subfolders, component sort order interleaves a parent's own files
        around its subfolders' blocks, which would split the parent into
        several same-title groups. Display order is therefore a
        display-level regrouping of cache order; items carry their
        catalog indices so decode mapping is unaffected."""
        if self.catalog is None:
            return
        cat = self.catalog
        default_view = indices is None
        if default_view:
            indices = [i for i, p in enumerate(cat.photos)
                       if p.visible or self.reveal]
        self.filter_label = label
        by_folder: dict[str, _Group] = {}
        for i in indices:
            photo = cat.photos[i]
            f = folder_key(cat, photo.root_id, photo.folder)
            g = by_folder.get(f)
            if g is None:
                title = cat.folders[f].title if f in cat.folders else f
                g = by_folder[f] = _Group(folder=f, title=title, items=[])
            g.items.append(i)
        self.groups = list(by_folder.values())
        # Per-folder sort modes (fauxcasa-q6l.11): reorder each group's
        # DISPLAY slice — never the catalog — on the folder-grouped default
        # view only. Explicit display sets keep their given order (albums =
        # membership order, search/starred/recent = catalog order); the
        # module constants block up top records the scoping rationale.
        if default_view and self.sort_modes:
            for g in self.groups:
                mode = self.sort_modes.get(g.folder, DEFAULT_SORT_MODE)
                if mode != DEFAULT_SORT_MODE:
                    g.items = sort_folder_items(g.items, cat.photos, mode)
        self.display = []
        self.loc = {}
        for gi, g in enumerate(self.groups):
            for n, idx in enumerate(g.items):
                self.loc[idx] = (gi, n)
                self.display.append(idx)
        self.display_pos = {idx: n for n, idx in enumerate(self.display)}
        # Selection policy on a view change (fauxcasa-q6l.1): the set
        # COLLAPSES to the current item if the new view still shows it,
        # else clears entirely. Simple, and it preserves the old
        # single-select behavior (a selected photo survives a filter
        # change it still matches); cross-view persistence is the
        # selection tray's job (fauxcasa-q6l.2), not the grid's.
        self._select(self.current if self.current in self.display_pos else -1)
        self._relayout()
        self.verticalScrollBar().setValue(0)

    def _invalidate_tiles(self) -> None:
        self.generation += 1
        self.tiles.clear()
        self._cache_bytes = 0
        with self.pending_lock:
            self.pending.clear()

    def set_zoom(self, tile: int) -> None:
        tile = max(64, min(256, tile))
        if tile == self.tile:
            return
        # Keep the viewport anchored on the same content while row
        # heights change ~5x across the zoom range.
        anchor = None
        top = self.verticalScrollBar().value()
        # Probe past a header band / group gap (e.g. right after a
        # sidebar folder click pins a header at the top), where a 1 px
        # probe would find no item and the anchor would be lost.
        probe = HEADER_H + GROUP_GAP + 6
        for g, n, _idx in self._visible_items(top, top + probe):
            anchor = (g, n)  # group objects survive a zoom relayout
            break
        self.tile = tile
        # No tile invalidation on zoom: tiles are kept at native cache
        # resolution and scaled in paint, so the same decoded tile serves
        # every zoom level. Zoom is pure relayout + re-anchor; the JPEG is
        # never re-decoded just because the tile size changed (fauxcasa-z1e).
        self._relayout()
        if anchor is not None:
            self.verticalScrollBar().setValue(
                self._item_rect(anchor[0], anchor[1]).top() - HEADER_H)

    def _cell(self) -> int:
        return self.tile + PAD

    def _relayout(self) -> None:
        w = max(1, self.viewport().width())
        cell = self._cell()
        self.cols = max(1, (w - PAD) // cell)
        y = 0
        for g in self.groups:
            g.y = y
            g.grid_y = y + HEADER_H + 4
            rows = (len(g.items) + self.cols - 1) // self.cols
            g.height = HEADER_H + 4 + rows * cell
            y += g.height + GROUP_GAP
        self.content_h = y
        sb = self.verticalScrollBar()
        sb.setRange(0, max(0, self.content_h - self.viewport().height()))
        sb.setPageStep(self.viewport().height())
        sb.setSingleStep(self._cell() // 3)
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    # ---------- navigation API ----------

    def scroll_to_folder(self, folder_rel: str) -> None:
        for g in self.groups:
            if g.folder == folder_rel:
                self.verticalScrollBar().setValue(g.y)
                return

    def scroll_to_fraction(self, frac: float) -> None:
        sb = self.verticalScrollBar()
        sb.setValue(int(sb.maximum() * max(0.0, min(1.0, frac))))

    def current_group_index(self) -> int:
        """Index into self.groups of the group under the viewport top: the
        last group whose header y is <= the scroll position (same bisect
        the pinned-header logic uses). -1 when there are no groups."""
        if not self.groups:
            return -1
        ys = [g.y for g in self.groups]
        return max(0, bisect_right(ys, self.verticalScrollBar().value()) - 1)

    def jump_prev_folder(self) -> None:
        """Step the viewport up one folder boundary, Picasa-style: from
        MID-group first snap to the current group's own top; from a group
        top go to the previous group's top; no-op at the top of the first
        group."""
        gi = self.current_group_index()
        if gi < 0:
            return
        sb = self.verticalScrollBar()
        if sb.value() > self.groups[gi].y:
            sb.setValue(self.groups[gi].y)
        elif gi > 0:
            sb.setValue(self.groups[gi - 1].y)

    def jump_next_folder(self) -> None:
        """Step the viewport down to the next folder's header (setValue
        clamps near the end); no-op inside the last group."""
        gi = self.current_group_index()
        if 0 <= gi and gi + 1 < len(self.groups):
            self.verticalScrollBar().setValue(self.groups[gi + 1].y)

    def jump_to_top(self) -> None:
        self.verticalScrollBar().setValue(0)

    def jump_to_end(self) -> None:
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ---------- decode pool ----------

    def _decode_worker(self) -> None:
        """Daemon loop; must survive anything a job throws at it (EIO on a
        dying volume, a vanished --thumbs file, a corrupt blob): any
        failure becomes an error tile, never a dead worker or a job that
        silently evaporates."""
        fd = -1
        fd_thumbs = None  # the ThumbCache OBJECT the fd was opened for
        fd_path = None    # composite caches select a per-root path per entry
        while True:
            job = self.jobs.get()
            if job is _STOP:
                break  # clean retirement (fauxcasa-gfz); fd closed below
            gen, idx = job
            thumbs = self.thumbs
            with self.pending_lock:
                self.pending.discard(idx)
            if gen != self.generation or thumbs is None:
                continue
            if idx not in self.wanted:
                continue  # scrolled away; re-requested if it comes back
            img = None
            try:  # noqa: the worker must outlive ANY per-job failure
                native = self._tile_native
                offset, length, _w, _h = thumbs.entry(
                    idx, thumbs.best_level(native))
                source_path = thumbs.path
                # Reopen when the cache OBJECT or selected root path changes:
                # a reconcile rebuild replaces the fcache at the same path
                # (new inode), so a path compare would keep reading the old
                # unlinked file with the new file's offsets.
                if thumbs is not fd_thumbs or source_path != fd_path:
                    if fd >= 0:
                        os.close(fd)
                    fd, fd_thumbs, fd_path = -1, None, None
                    # O_BINARY (Windows) keeps text-mode translation from
                    # corrupting JPEG bytes; it is 0/absent on POSIX, so the
                    # getattr keeps this portable (fauxcasa-uix).
                    fd = os.open(
                        source_path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                    )
                    fd_thumbs = thumbs
                    fd_path = source_path
                # fcache v2 hi-DPI consumer (fauxcasa-q7m): read the cheapest
                # level that covers the DPR-scaled native edge. v1 / d==1 ->
                # the 256 level the grid always read (no-op); a hi-DPI display
                # reads the 512 v2 level, or the largest level a v1 cache has.
                if length > 0:
                    # os.pread is Unix-only (it raises AttributeError on
                    # Windows, which the broad except below would turn into an
                    # error tile for EVERY thumb). This fd is single-owner and
                    # read single-threaded per worker, so seek+read is the
                    # portable equivalent with no concurrent-fd race
                    # (fauxcasa-uix).
                    os.lseek(fd, offset, 0)
                    buf = os.read(fd, length)
                    img = QImage.fromData(buf, "JPEG")
                    if img.isNull():
                        img = None
                if img is not None:
                    # Keep the tile at native cache resolution; paint scales
                    # it to the current tile size, so zoom never re-decodes.
                    # Cache blobs are already <= the chosen level; the cap is a
                    # defensive guard against an oversized blob and trims a
                    # larger level down to the device footprint we actually
                    # paint (best_level returns the smallest SUFFICIENT level,
                    # which may exceed `native` when no exact match exists).
                    if img.width() > native or img.height() > native:
                        img = img.scaled(
                            native, native,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    rot = (self.catalog.photos[idx].rotate
                           if self.catalog else 0)
                    if rot:
                        img = img.transformed(QTransform().rotate(90 * rot))
                    img = img.convertToFormat(QImage.Format.Format_RGB32)
            except Exception:
                # EIO, a 4 GiB length from a corrupt index (MemoryError),
                # anything: this job becomes an error tile, the worker
                # lives. A dead worker re-kills its replacement via the
                # paint loop's re-request, wiping out the whole pool.
                img = None
            try:
                self.done.put((gen, idx, img))
                self._notifier.tile_ready.emit()
            except RuntimeError:
                break  # Qt objects torn down at shutdown; close fd below
        if fd >= 0:
            os.close(fd)

    def stop(self) -> None:
        """Retire the decode workers and join them. Idempotent. Called from
        closeEvent (and test teardown) so the daemon pool does not outlive the
        widget — immortal workers accumulate across a process and race the main
        thread on Qt state during teardown, which on Windows surfaces as an
        access violation in an unrelated later test (fauxcasa-gfz)."""
        if self._stopping:
            return
        self._stopping = True
        for _ in self._workers:
            self.jobs.put(_STOP)  # one sentinel retires one worker
        for t in self._workers:
            t.join(timeout=1.0)

    def closeEvent(self, event) -> None:
        self.stop()
        super().closeEvent(event)

    def _pump_decoded(self) -> None:
        while True:
            try:
                gen, idx, img = self.done.get_nowait()
            except queue.Empty:
                break
            if gen != self.generation:
                continue
            old = self.tiles.get(idx)
            if old is not None:
                self._cache_bytes -= old[2]
            nbytes = img.sizeInBytes() if img is not None else 0
            # All tiles are stored at native size; paint aspect-fits and
            # scales them to the current tile size.
            self.tiles[idx] = [img, self.frame_no, nbytes]
            self._cache_bytes += nbytes

    def _request(self, idx: int) -> None:
        if idx in self.tiles or self.thumbs is None:
            return
        with self.pending_lock:
            if idx in self.pending:
                return
            self.pending.add(idx)
        # No tile size in the job: tiles decode at native resolution and
        # paint scales them, so a decode is zoom-independent.
        self.jobs.put((self.generation, idx))

    def _evict(self) -> None:
        # Trim oldest-first until BOTH bounds hold: summed decoded bytes
        # <= CACHE_BYTES (the real RAM limit) and entry count <= the
        # backstop. Never evict what the current paint wants: at small
        # tile sizes a big viewport's want-band can be large, and the
        # entry backstop floors at the want-band size so trimming can't
        # re-evict just-requested tiles every frame (a permanent
        # decode/evict/repaint livelock). If the want-band alone exceeds a
        # bound, we keep it anyway — overshooting briefly beats thrashing.
        wanted = self.wanted
        entry_cap = max(CACHE_MAX_ENTRIES, len(wanted) + 128)
        if self._cache_bytes <= CACHE_BYTES and len(self.tiles) <= entry_cap:
            return
        by_age = sorted(
            (kv for kv in self.tiles.items() if kv[0] not in wanted),
            key=lambda kv: kv[1][1],
        )
        for idx, ent in by_age:
            if self._cache_bytes <= CACHE_BYTES and len(self.tiles) <= entry_cap:
                break
            self._cache_bytes -= ent[2]
            del self.tiles[idx]

    # ---------- hit testing ----------

    def _visible_groups(self, top: int, bottom: int):
        ys = [g.y for g in self.groups]
        i = max(0, bisect_right(ys, top) - 1)
        while i < len(self.groups) and self.groups[i].y < bottom:
            g = self.groups[i]
            if g.y + g.height > top:
                yield g
            i += 1

    def _item_rect(self, g: _Group, n: int) -> QRect:
        cell = self._cell()
        row, col = divmod(n, self.cols)
        return QRect(PAD + col * cell, g.grid_y + row * cell,
                     self.tile, self.tile)

    def _visible_items(self, top: int, bottom: int):
        """Yield (group, n_in_group, catalog_idx) for items whose row band
        intersects [top, bottom)."""
        cell = self._cell()
        for g in self._visible_groups(top, bottom):
            r0 = max(0, (top - g.grid_y) // cell)
            r1 = (bottom - g.grid_y + cell - 1) // cell
            a = r0 * self.cols
            b = min(r1 * self.cols, len(g.items))
            for n in range(a, b):
                yield g, n, g.items[n]

    def _sticky(self, top: int) -> tuple[_Group, int] | None:
        """The group whose header should be pinned at the viewport top,
        with its push-up offset (0 = fully pinned, approaching -HEADER_H
        as the next group's in-flow header slides in)."""
        if not self.groups:
            return None
        ys = [g.y for g in self.groups]
        i = bisect_right(ys, top) - 1
        if i < 0:
            return None
        g = self.groups[i]
        if g.y >= top:
            return None  # its own in-flow header is still visible
        push = min(0, g.y + g.height + GROUP_GAP - top - HEADER_H)
        if push <= -HEADER_H:
            return None
        return g, push

    def photo_at(self, vx: int, vy: int) -> int:
        top = self.verticalScrollBar().value()
        st = self._sticky(top)
        if st is not None and st[1] <= vy < st[1] + HEADER_H:
            return -1  # click landed on the pinned header band
        y = vy + top
        for g, n, idx in self._visible_items(y, y + 1):
            r = self._item_rect(g, n)
            if r.contains(vx, y):
                return idx
        return -1

    def all_visible_decoded(self) -> bool:
        """READY instrumentation: every strictly-visible tile decoded
        (or marked error). Empty view counts as decoded."""
        top = self.verticalScrollBar().value()
        bottom = top + self.viewport().height()
        for _g, _n, idx in self._visible_items(top, bottom):
            if idx not in self.tiles:
                return False
        return True

    def _refresh_tile_native(self) -> None:
        """Recompute the DPR-scaled native decode edge on the GUI thread
        (fauxcasa-q7m). devicePixelRatioF() needs a live screen, so this runs
        in paintEvent, not the worker. The base is TILE_NATIVE (the max zoom
        tile), not self.tile, so the native size is zoom-independent (z1e: one
        decode per photo serves every zoom). On a change — first real paint, or
        the window dragged to a monitor with a different ratio — invalidate so
        in-flight and cached tiles re-decode at the new level; unchanged is the
        common case and costs one int compare."""
        want = max(TILE_NATIVE, round(TILE_NATIVE * self.devicePixelRatioF()))
        if want != self._tile_native:
            self._tile_native = want
            self._invalidate_tiles()

    # ---------- painting ----------

    def paintEvent(self, _event) -> None:
        probe = self._frame_probe
        t_start = perf_counter() if probe is not None else 0.0
        self.frame_no += 1
        self._refresh_tile_native()
        self._pump_decoded()
        vp = self.viewport()
        top = self.verticalScrollBar().value()
        bottom = top + vp.height()

        # The want-band (visible + prefetch) gates worker dequeues and
        # eviction, so publish it before drawing or requesting anything.
        margin = int(vp.height() * PREFETCH_SCREENS)
        band = [(g, n, idx) for g, n, idx in
                self._visible_items(top - margin, bottom + margin)]
        self.wanted = frozenset(idx for _g, _n, idx in band)

        painter = QPainter(vp)
        painter.fillRect(vp.rect(), BACKGROUND)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)

        blank = False  # bench: any strictly-visible tile still undecoded
        for g, n, idx in self._visible_items(top, bottom):
            r = self._item_rect(g, n).translated(0, -top)
            t = self.tiles.get(idx)
            if t is None:
                painter.fillRect(r, PLACEHOLDER)
                blank = True
            elif t[0] is None:
                painter.fillRect(r, ERROR_TILE)
                t[1] = self.frame_no
            else:
                t[1] = self.frame_no
                img = t[0]
                # Aspect-fit, centered, and SCALED to the current tile size.
                # Every tile is held at native (~256 px) resolution, so this
                # per-paint scale is what makes zoom cheap (no re-decode);
                # SmoothPixmapTransform (set above) keeps the blit clean.
                scale = min(self.tile / img.width(),
                            self.tile / img.height(), 1.0)
                dw = max(1, round(img.width() * scale))
                dh = max(1, round(img.height() * scale))
                target = QRect(r.x() + (self.tile - dw) // 2,
                               r.y() + (self.tile - dh) // 2, dw, dh)
                painter.drawImage(target, img)
            photo = self.catalog.photos[idx]
            # Reveal mode shows hidden=yes / stash files; veil them so they
            # read as not-normally-shown (star + selection stay on top).
            if self.reveal and not photo.visible:
                painter.fillRect(r, HIDDEN_VEIL)
            if photo.star:
                # count >= 1 shows the badge (star is 0-5 now; the exact
                # count reads out in the status bar / viewer info line)
                s = max(7.0, self.tile / 14.0)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(STAR_GOLD)
                painter.drawPolygon(_star_polygon(
                    r.right() - s - 2, r.y() + s + 2, s))
            if photo.geotag is not None:
                # Geotag corner badge (§5 corner badges; fauxcasa-cam.10) —
                # BOTTOM-right, its own corner: star owns top-right and the
                # M2 reject badge will claim a third.
                s = max(7.0, self.tile / 14.0)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(GEO_TEAL)
                painter.drawPolygon(_pin_polygon(
                    r.right() - s - 2, r.bottom() - s - 2, s))
            if photo.media == "video":
                # Play-glyph corner badge (fauxcasa-v46.2): a video tile is
                # a poster frame, indistinguishable from a still without a
                # mark. BOTTOM-LEFT, its own corner — star owns top-right,
                # geotag pin bottom-right.
                s = max(7.0, self.tile / 14.0)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(PLAY_WHITE)
                painter.drawPolygon(_play_polygon(
                    r.x() + s + 2, r.bottom() - s - 2, s))
            # Multi-select paint (fauxcasa-q6l.1): every selected tile
            # shows the selection rect; the current item is distinct via
            # the stronger border (dashed focus cue if Ctrl-toggled out of
            # the set). Pens are module constants — no per-tile allocation.
            if idx == self.current:
                painter.setPen(PEN_CURRENT if idx in self.selection
                               else PEN_FOCUS)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(r.adjusted(-2, -2, 1, 1))
            elif idx in self.selection:
                painter.setPen(PEN_SELECTED)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(r.adjusted(-2, -2, 1, 1))

        # in-flow group headers, then the pinned copy of the current one
        for g in self._visible_groups(top, bottom):
            if g.y >= top:
                self._draw_header(painter, g, g.y - top, vp.width())
        st = self._sticky(top)
        if st is not None:
            self._draw_header(painter, st[0], st[1], vp.width())
        painter.end()

        # request the band, then bound the tile cache
        for _g, _n, idx in band:
            self._request(idx)
        self._evict()

        if probe is not None:
            probe(t_start, perf_counter(), blank)

    def _draw_header(self, painter: QPainter, g: _Group, y: int,
                     width: int) -> None:
        painter.fillRect(0, y, width, HEADER_H, HEADER_BG)
        painter.setPen(HEADER_FG)
        label = f"{g.title}   ·   {len(g.items)}"
        painter.drawText(QRect(PAD, y, width - 2 * PAD, HEADER_H),
                         Qt.AlignmentFlag.AlignVCenter, label)

    # ---------- hover peek trigger (fauxcasa-q6l.5) ----------

    def _peek_update(self, mods) -> None:
        """Drive the peek trigger state machine from the CURRENT modifier
        state and last known hover position — mouse moves, Ctrl/Alt presses
        and releases all funnel here. While the chord is held, hovering a
        new photo re-points the peek (peek_requested re-emits) and hovering
        off any photo — or the peek being suppressed by Esc/click — ends
        it; the chord dropping always ends it and re-arms suppression."""
        if (mods & PEEK_MODS) != PEEK_MODS:
            self._peek_suppressed = False   # trigger dropped: re-arm
            self._end_peek()
            return
        if self._peek_suppressed:
            return
        pos = self._hover
        if pos is None:
            # Chord pressed before any tracked move (e.g. focus arrived by
            # keyboard): fall back to the live cursor position.
            pos = QPointF(self.viewport().mapFromGlobal(QCursor.pos()))
        idx = self.photo_at(int(pos.x()), int(pos.y()))
        if idx == self._peek_idx:
            return
        if idx >= 0:
            self._peek_idx = idx
            self.peek_requested.emit(idx)
        else:
            self._end_peek()

    def _end_peek(self) -> None:
        """Dismiss an active peek (no-op otherwise). Also the owner's hook
        for forced dismissal (e.g. a reconcile swap invalidates the peeked
        index — main.reload_data)."""
        if self._peek_idx >= 0:
            self._peek_idx = -1
            self.peek_released.emit()

    def mouseMoveEvent(self, event) -> None:
        # Mouse tracking is on: every move updates the hover position (so a
        # later Ctrl+Alt press can trigger in place) and feeds the trigger
        # state machine — the peek surface is input-transparent, so these
        # moves keep arriving while it covers the cursor.
        self._hover = event.position()
        self._peek_update(event.modifiers())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = None
        self._end_peek()
        super().leaveEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() in _MOD_OF:
            # Either modifier up ends the chord. Mask the released key's
            # own bit out — modifiers() timing around the modifier keys
            # themselves is platform-wobbly, so normalize explicitly.
            self._peek_update(event.modifiers() & ~_MOD_OF[event.key()])
        super().keyReleaseEvent(event)

    # ---------- input ----------

    def mousePressEvent(self, event) -> None:
        """Modifier-aware selection (fauxcasa-q6l.1). Qt's standard
        modifiers are platform-correct: on macOS, Cmd reports as
        ControlModifier — no raw key handling here."""
        if self._peek_idx >= 0:
            # Any click dismisses the peek and holds it dismissed until the
            # chord drops; the click then does its normal selection work.
            self._peek_suppressed = True
            self._end_peek()
        idx = self.photo_at(int(event.position().x()),
                            int(event.position().y()))
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if idx < 0:
            # Background/header click clears (as before) — but not with a
            # modifier held, so a near-miss during Ctrl/Shift assembly
            # can't nuke a painstaking selection.
            if not (ctrl or shift):
                self._select(-1)
            return
        if shift and self.anchor in self.display_pos:
            rng = self._range(self.anchor, idx)
            # Shift+click: anchor..hit range replaces the selection;
            # Ctrl+Shift+click adds the range instead (both standard).
            sel = (self.selection | rng) if ctrl else rng
            self._set_selection(sel, idx, self.anchor)
        elif ctrl:
            sel = set(self.selection)
            sel.symmetric_difference_update({idx})  # toggle membership
            self._set_selection(sel, idx, idx)
        else:
            self._select(idx)

    def mouseDoubleClickEvent(self, event) -> None:
        # A modified double-click is selection assembly (the press half
        # already toggled/ranged) — never an accidental viewer open.
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                | Qt.KeyboardModifier.ShiftModifier):
            return
        idx = self.photo_at(int(event.position().x()),
                            int(event.position().y()))
        if idx >= 0:
            self._select(idx)
            self._activate(idx)

    def keyPressEvent(self, event) -> None:
        # Every binding is a keymap lookup (fauxcasa-q6l.8): the default-
        # scheme table is the single source of truth; this handler only
        # sequences the context layering (peek overlay first, then grid).
        key = event.key()
        if keymap.matches(event, "peek.dismiss") and self._peek_idx >= 0:
            # Esc dismisses the peek — and ONLY the peek this press (the
            # selection-collapse Esc below is untouched for the next one) —
            # suppressed until the chord drops, so a twitch can't re-fire.
            self._peek_suppressed = True
            self._end_peek()
            return
        if key in _MOD_OF:
            # Ctrl+Alt completed over a photo triggers WITHOUT a mouse move
            # — Picasa's actual gesture. OR the pressed key's own bit in
            # (see keyReleaseEvent for why we normalize explicitly).
            self._peek_update(event.modifiers() | _MOD_OF[key])
        if keymap.matches(event, "grid.hold"):
            # Ctrl+H: hold the current selection in the tray (q6l.2).
            self.hold_requested.emit()
            return
        if keymap.matches(event, "grid.star_toggle"):
            self.star_toggle_requested.emit()
            return
        if keymap.matches(event, "grid.select_all"):
            # Ctrl+A (Cmd+A on macOS via the StandardKey): the whole current
            # display set. Current/anchor keep their place if shown.
            if self.display:
                cur = (self.current if self.current in self.display_pos
                       else self.display[0])
                anc = (self.anchor if self.anchor in self.display_pos
                       else cur)
                self._set_selection(set(self.display), cur, anc)
            return
        if keymap.matches(event, "grid.deselect"):
            # Ctrl+D: Picasa "Deselect photos" — clears the selection set;
            # the current item stays as keyboard focus but is deselected.
            self._set_selection(set(), self.current, self.current)
            return
        if keymap.matches(event, "grid.invert"):
            # Ctrl+I: Picasa "Invert selection" — flip membership for every
            # photo in the current view; current item stays as keyboard focus.
            if self.display:
                new_sel = set(self.display) - self.selection
                cur = (self.current if self.current in self.display_pos
                       else -1)
                self._set_selection(new_sel, cur, cur)
            return
        if keymap.matches(event, "grid.locate"):
            # Ctrl+Enter: Picasa's Locate on Disk — reveal the CURRENT item
            # in the OS file manager (q6l.8). Checked BEFORE grid.open's
            # key_only Return so the chord reveals instead of activating.
            if self.current >= 0 and self.catalog is not None:
                # Catalog.abs() is the single choke point for absolute-path
                # composition (multiroot .b, design §6); None means the
                # photo's root is missing/offline (bead .e, design §8) —
                # nothing to reveal, a silent no-op.
                target = self.catalog.abs(self.catalog.photos[self.current])
                if target is not None:
                    reveal_in_file_manager(target)
            return
        if keymap.matches(event, "grid.clear"):
            # Esc: collapse the set to the current item only (clears all
            # when there is no current item).
            self._select(self.current)
            return
        if keymap.matches(event, "grid.open") and self.current >= 0:
            self._activate(self.current)
            return
        if not self.display:
            super().keyPressEvent(event)
            return
        fwd = keymap.matches(event, "grid.next")   # Right / J (q6l.8)
        if fwd or keymap.matches(event, "grid.prev"):
            step = 1 if fwd else -1
            pos = self.display_pos.get(self.current, -1)
            pos = max(0, min(len(self.display) - 1,
                             pos + step if pos >= 0 else 0))
            target = self.display[pos]
        elif keymap.matches(event, "grid.row_up") \
                or keymap.matches(event, "grid.row_down"):
            target = self._row_step(keymap.matches(event, "grid.row_down"))
            if target < 0:
                return
        elif keymap.matches(event, "grid.first"):
            # Home: jump to the first photo in the current view (ed5.12).
            target = self.display[0]
        elif keymap.matches(event, "grid.last"):
            # End: jump to the last photo in the current view (ed5.12).
            target = self.display[-1]
        else:
            super().keyPressEvent(event)
            return
        if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                and self.anchor in self.display_pos):
            # Shift+arrows: the selection becomes the anchor..target range
            # — the anchor stays put while the current end walks, so
            # extending then shrinking behaves like every native list.
            self._set_selection(self._range(self.anchor, target),
                                target, self.anchor)
        else:
            self._select(target)
        self._ensure_visible(target)

    def _row_step(self, down: bool) -> int:
        """Move one visual row, preserving the column — group-aware
        (stepping by cols in the flat list misaligns at group seams)."""
        if self.current not in self.loc:
            return self.display[0]
        gi, n = self.loc[self.current]
        g = self.groups[gi]
        row, col = divmod(n, self.cols)
        last_row = (len(g.items) - 1) // self.cols
        if down:
            if row < last_row:
                return g.items[min(n + self.cols, len(g.items) - 1)]
            if gi + 1 < len(self.groups):
                nxt = self.groups[gi + 1]
                return nxt.items[min(col, len(nxt.items) - 1)]
        else:
            if row > 0:
                return g.items[(row - 1) * self.cols + col]
            if gi > 0:
                prev = self.groups[gi - 1]
                prow = (len(prev.items) - 1) // self.cols
                return prev.items[min(prow * self.cols + col,
                                      len(prev.items) - 1)]
        return self.current

    def _range(self, a: int, b: int) -> set[int]:
        """Catalog indices between two displayed items, inclusive, in
        display order (spans group seams — display is the flat list)."""
        pa, pb = self.display_pos[a], self.display_pos[b]
        lo, hi = (pa, pb) if pa <= pb else (pb, pa)
        return set(self.display[lo:hi + 1])

    def _select(self, idx: int) -> None:
        """Plain single-select: the set collapses to {idx} (empty for -1)
        and idx becomes both current and anchor."""
        self._set_selection({idx} if idx >= 0 else set(), idx, idx)

    def _set_selection(self, selection: set[int], current: int,
                       anchor: int) -> None:
        """The one mutation point for the selection model. Emits
        photo_selected when the CURRENT item changes and selection_changed
        (a set copy) when the SET changes; no-op mutations emit nothing."""
        self.anchor = anchor
        sel_changed = selection != self.selection
        cur_changed = current != self.current
        if not (sel_changed or cur_changed):
            return
        self.selection = selection
        self.current = current
        if cur_changed:
            self.photo_selected.emit(current)
        if sel_changed:
            self.selection_changed.emit(set(selection))
        self.viewport().update()

    def _activate(self, idx: int) -> None:
        pos = self.display_pos.get(idx, 0)
        self.photo_activated.emit(idx, self.display, pos)

    def _ensure_visible(self, idx: int) -> None:
        if idx not in self.loc:
            return
        gi, n = self.loc[idx]
        r = self._item_rect(self.groups[gi], n)
        sb = self.verticalScrollBar()
        top = sb.value()
        if r.top() < top + HEADER_H:
            sb.setValue(r.top() - HEADER_H - 4)
        elif r.bottom() > top + self.viewport().height():
            sb.setValue(r.bottom() - self.viewport().height() + 4)
