"""Virtualized thumbnail grid for the tracer app.

Descends from the proven balloons/py-qt design (custom-painted
virtualized grid, threaded JPEG decode from the packed fcache, bounded
LRU of decoded tiles) with the benchmark hacks replaced by product
behavior: event-driven repaints instead of a 240 Hz timer, a real
scrollbar via QAbstractScrollArea, resize handling, group headers with
a pinned current-folder header, selection/activation, star badges, and
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
from time import perf_counter

from PySide6.QtCore import QObject, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPolygonF, QTransform
from PySide6.QtWidgets import QAbstractScrollArea

from catalog import Catalog
from thumbcache import THUMB_EDGE, ThumbCache

HEADER_H = 26
GROUP_GAP = 10
PAD = 8
WORKERS = 4
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
CACHE_BYTES = 256 * 1024 * 1024
CACHE_MAX_ENTRIES = 4096
# Native decoded-tile edge: the cache's own thumb size. Tiles are decoded
# at (or capped to) this and scaled to self.tile in paint.
TILE_NATIVE = THUMB_EDGE
PREFETCH_SCREENS = 1.0

PLACEHOLDER = QColor(60, 60, 60)
ERROR_TILE = QColor(96, 40, 40)
BACKGROUND = QColor(24, 24, 24)
HEADER_BG = QColor(34, 34, 34)
HEADER_FG = QColor(200, 200, 200)
SELECT = QColor(64, 140, 255)
STAR_GOLD = QColor(255, 200, 40)
HIDDEN_VEIL = QColor(0, 0, 0, 110)  # reveal mode: dim hidden/stash tiles


def _star_polygon(cx: float, cy: float, r: float) -> QPolygonF:
    poly = QPolygonF()
    for i in range(10):
        rad = r if i % 2 == 0 else r * 0.42
        ang = -math.pi / 2 + i * math.pi / 5
        poly.append(QPointF(cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
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

    photo_selected = Signal(int)  # catalog index, -1 = none
    photo_activated = Signal(int, list, int)  # catalog idx, display list, pos

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
        self.selected = -1  # catalog index
        # Reveal toggle: when True the default ("All photos"/folder) view
        # also shows hidden=yes photos and stash-folder files, veiled so
        # they read as not-normally-shown. Owned/driven by MainWindow.
        self.reveal = False

        # decode machinery (balloon lineage)
        self.generation = 0  # bumped on zoom/cache change; stale results drop
        self.jobs: queue.SimpleQueue = queue.SimpleQueue()
        self.done: queue.SimpleQueue = queue.SimpleQueue()
        self.pending: set[int] = set()
        self.pending_lock = threading.Lock()
        self.tiles: dict[int, list] = {}  # idx -> [QImage|None(error), frame, nbytes]
        self._cache_bytes = 0  # running sum of tiles' decoded bytes (CACHE_BYTES)
        self.frame_no = 0
        # Optional bench hook: callable(t_start, t_end, blank). Set by the
        # scroll benchmark (tracer/bench_scroll.py) to time frame
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
        for _ in range(WORKERS):
            threading.Thread(target=self._decode_worker, daemon=True).start()

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.verticalScrollBar().valueChanged.connect(
            lambda _v: self.viewport().update()
        )
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

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
        if indices is None:
            indices = [i for i, p in enumerate(cat.photos)
                       if p.visible or self.reveal]
        self.filter_label = label
        by_folder: dict[str, _Group] = {}
        for i in indices:
            f = cat.photos[i].folder
            g = by_folder.get(f)
            if g is None:
                title = cat.folders[f].title if f in cat.folders else f
                g = by_folder[f] = _Group(folder=f, title=title, items=[])
            g.items.append(i)
        self.groups = list(by_folder.values())
        self.display = []
        self.loc = {}
        for gi, g in enumerate(self.groups):
            for n, idx in enumerate(g.items):
                self.loc[idx] = (gi, n)
                self.display.append(idx)
        self.display_pos = {idx: n for n, idx in enumerate(self.display)}
        if self.selected not in self.display_pos:
            self._select(-1)
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

    # ---------- decode pool ----------

    def _decode_worker(self) -> None:
        """Daemon loop; must survive anything a job throws at it (EIO on a
        dying volume, a vanished --thumbs file, a corrupt blob): any
        failure becomes an error tile, never a dead worker or a job that
        silently evaporates."""
        fd = -1
        fd_thumbs = None  # the ThumbCache OBJECT the fd was opened for
        while True:
            gen, idx = self.jobs.get()
            thumbs = self.thumbs
            with self.pending_lock:
                self.pending.discard(idx)
            if gen != self.generation or thumbs is None:
                continue
            if idx not in self.wanted:
                continue  # scrolled away; re-requested if it comes back
            img = None
            try:  # noqa: the worker must outlive ANY per-job failure
                # Reopen when the cache OBJECT changes, not just its path:
                # a reconcile rebuild replaces the fcache at the same path
                # (new inode), so a path compare would keep reading the old
                # unlinked file with the new file's offsets.
                if thumbs is not fd_thumbs:
                    if fd >= 0:
                        os.close(fd)
                    fd, fd_thumbs = -1, None
                    fd = os.open(thumbs.path, os.O_RDONLY)
                    fd_thumbs = thumbs
                offset, length, _w, _h = thumbs.entries[idx]
                if length > 0:
                    buf = os.pread(fd, length, offset)
                    img = QImage.fromData(buf, "JPEG")
                    if img.isNull():
                        img = None
                if img is not None:
                    # Keep the tile at native cache resolution; paint scales
                    # it to the current tile size, so zoom never re-decodes.
                    # Cache blobs are already <= TILE_NATIVE; the cap is a
                    # defensive guard against an oversized blob, not a
                    # per-zoom pre-scale.
                    if img.width() > TILE_NATIVE or img.height() > TILE_NATIVE:
                        img = img.scaled(
                            TILE_NATIVE, TILE_NATIVE,
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
                return  # Qt objects torn down at shutdown

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

    # ---------- painting ----------

    def paintEvent(self, _event) -> None:
        probe = self._frame_probe
        t_start = perf_counter() if probe is not None else 0.0
        self.frame_no += 1
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
                s = max(7.0, self.tile / 14.0)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(STAR_GOLD)
                painter.drawPolygon(_star_polygon(
                    r.right() - s - 2, r.y() + s + 2, s))
            if idx == self.selected:
                painter.setPen(SELECT)
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

    # ---------- input ----------

    def mousePressEvent(self, event) -> None:
        idx = self.photo_at(int(event.position().x()),
                            int(event.position().y()))
        self._select(idx)

    def mouseDoubleClickEvent(self, event) -> None:
        idx = self.photo_at(int(event.position().x()),
                            int(event.position().y()))
        if idx >= 0:
            self._select(idx)
            self._activate(idx)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.selected >= 0:
            self._activate(self.selected)
            return
        if not self.display:
            super().keyPressEvent(event)
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = -1 if key == Qt.Key.Key_Left else 1
            pos = self.display_pos.get(self.selected, -1)
            pos = max(0, min(len(self.display) - 1,
                             pos + step if pos >= 0 else 0))
            target = self.display[pos]
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            target = self._row_step(key == Qt.Key.Key_Down)
            if target < 0:
                return
        else:
            super().keyPressEvent(event)
            return
        self._select(target)
        self._ensure_visible(target)

    def _row_step(self, down: bool) -> int:
        """Move one visual row, preserving the column — group-aware
        (stepping by cols in the flat list misaligns at group seams)."""
        if self.selected not in self.loc:
            return self.display[0]
        gi, n = self.loc[self.selected]
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
        return self.selected

    def _select(self, idx: int) -> None:
        if idx == self.selected:
            return
        self.selected = idx
        self.photo_selected.emit(idx)
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
