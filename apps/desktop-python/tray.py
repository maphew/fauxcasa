"""Selection tray for the tracer app (fauxcasa-q6l.2): Picasa's photo
tray — a persistent, CROSS-FOLDER held set with Hold/Clear and a live
type-aware readout (spec §5), the universal input every output action
will read. Output actions themselves are M2+; their attachment point is
MainWindow.selection_context() (main.py), which reads this tray.

Identity and ordering decisions (the q6l.1 handoff note asked for these
to be settled BEFORE the readout shape calcified):

- Held photos are identified by REL PATH, not catalog index. Indices
  are positional and remap wholesale when a reconcile rebuild swaps the
  catalog (main.reload_data); rel paths survive by identity, and
  rebind() re-resolves them against the swapped catalog.
- HOLD ORDER (insertion order) is the tray's ordering semantics. A tray
  is a deliberately assembled staging set — the order the user built it
  in is the order output actions should see — so Hold APPENDS; a single
  Hold of a multi-selection appends its members in display order (one
  deterministic run); re-holding an already-held photo keeps its
  original slot. Display order would reshuffle the set on every view
  change, which is exactly what a persistent tray must not do.
- Held photos that VANISH from a swapped catalog (deleted or renamed on
  disk) are dropped from the held set but never silently (N7):
  `vanished` counts them and the owner's readout surfaces the count
  until the next user tray action (hold/clear/remove) acknowledges it —
  including when the ENTIRE held set vanished.

Thumbnails render from the fcache with a small SYNCHRONOUS read per
held photo, memoized per rel — tray counts are small by design, and the
persistent-worker discipline (fauxcasa-gfz) rules out spawning decode
threads for a strip of a dozen thumbs. The grid's cache pair is shared,
never the originals (N4).
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QTransform
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from catalog import Catalog
from thumbcache import ThumbCache

TRAY_H = 56   # strip height (logical px)
THUMB = 44    # held-thumb box edge
PAD = 6
BACKGROUND = QColor(30, 30, 30)
PLACEHOLDER = QColor(60, 60, 60)   # cache not built yet
ERROR_TILE = QColor(96, 40, 40)    # cached error entry / unreadable blob
HINT_FG = QColor(120, 120, 120)
MORE_FG = QColor(200, 200, 200)


class SelectionTray(QWidget):
    """The tray strip: Hold/Clear buttons, the held-thumbnail bar, and
    the readout label (text is computed by the owner — MainWindow knows
    the sidebar view type; the tray only knows what it holds)."""

    navigate = Signal(str)   # a held thumb was clicked: rel path to show
    hold_clicked = Signal()  # Hold button (owner adds the grid selection)
    changed = Signal()       # held set / vanish count changed

    def __init__(self, catalog: Catalog, thumbs: ThumbCache | None,
                 parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.thumbs = thumbs
        # Hold-order rel paths (module docstring: insertion order, no
        # duplicates) + the vanish count the readout must surface (N7).
        self.held: list[str] = []
        self.vanished = 0
        self._rel_idx: dict[str, int] = {}
        # rel -> decoded thumb (None = no usable cached pixel: error tile
        # or unreadable blob). Cleared whenever catalog/cache re-point.
        self._thumb_imgs: dict[str, QImage | None] = {}
        self._rebuild_rel_index()

        self.setFixedHeight(TRAY_H)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(PAD, 0, PAD, 0)
        lay.setSpacing(PAD)
        self.hold_btn = QToolButton(self)
        self.hold_btn.setText("Hold")
        self.hold_btn.setToolTip(
            "Hold the selected photos in the tray (Ctrl+H) — the tray "
            "keeps them across folders, albums, and searches")
        self.hold_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hold_btn.clicked.connect(self.hold_clicked)
        lay.addWidget(self.hold_btn)
        self.clear_btn = QToolButton(self)
        self.clear_btn.setText("Clear")
        self.clear_btn.setToolTip("Empty the tray")
        self.clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_btn.setEnabled(False)
        self.clear_btn.clicked.connect(self.clear)
        lay.addWidget(self.clear_btn)
        self.bar = _ThumbsBar(self)
        lay.addWidget(self.bar, 1)
        self.readout = QLabel(self)
        lay.addWidget(self.readout)

    # ---------- held-set model ----------

    def hold(self, rels) -> int:
        """Append rels not already held, in the iteration order given
        (callers pass display order for one Hold action — module
        docstring). Returns how many were added. Any user tray action
        acknowledges (resets) the vanish note."""
        before = len(self.held)
        for rel in rels:
            if rel not in self.held:
                self.held.append(rel)
        added = len(self.held) - before
        if added or self.vanished:
            self.vanished = 0
            self._notify()
        return added

    def remove(self, rel: str) -> None:
        """Drop one held photo (per-item remove: middle-click / context
        menu on its thumb); order of the rest is untouched."""
        if rel not in self.held:
            return
        self.held.remove(rel)
        self.vanished = 0
        self._notify()

    def clear(self) -> None:
        if not self.held and not self.vanished:
            return
        self.held.clear()
        self.vanished = 0
        self._notify()

    def index_of(self, rel: str) -> int | None:
        """Current catalog index for a held rel (None if it no longer
        resolves — can only happen transiently before a rebind)."""
        return self._rel_idx.get(rel)

    def held_indices(self) -> list[int]:
        """Catalog indices of the held photos, in HOLD ORDER. All held
        rels resolve by construction — rebind() dropped any that don't."""
        return [self._rel_idx[r] for r in self.held if r in self._rel_idx]

    def rebind(self, catalog: Catalog, thumbs: ThumbCache | None) -> int:
        """Re-point at a (possibly swapped) catalog/cache pair and
        re-resolve the held rels against it — the reload_data hook.
        Indices remapped; identity (rel) survives. Held photos absent
        from the new catalog are dropped and COUNTED into `vanished`
        (accumulating across reconciles) so the readout can say so —
        never a silent disappearance (N7). Returns this rebind's drop
        count."""
        self.catalog = catalog
        self.thumbs = thumbs
        self._rebuild_rel_index()
        survivors = [r for r in self.held if r in self._rel_idx]
        dropped = len(self.held) - len(survivors)
        self.held = survivors
        self.vanished += dropped
        self._thumb_imgs.clear()
        self._notify()
        return dropped

    def set_thumbs(self, thumbs: ThumbCache | None) -> None:
        """Adopt a freshly built cache (cold-build finish): same catalog,
        new pixels — drop the memoized thumbs so they re-decode."""
        self.thumbs = thumbs
        self._thumb_imgs.clear()
        self.bar.update()

    def _rebuild_rel_index(self) -> None:
        self._rel_idx = {p.rel: i
                         for i, p in enumerate(self.catalog.photos)}

    def _notify(self) -> None:
        self.clear_btn.setEnabled(bool(self.held))
        self.bar.update()
        self.changed.emit()

    # ---------- thumbnail decode (synchronous, memoized) ----------

    def thumb_image(self, rel: str) -> QImage | None:
        """The held thumb for `rel`, decoded on first use from the fcache
        (module docstring: one small synchronous read, no new threads).
        None = no usable cached pixel; nothing is memoized while there is
        no cache at all, so a cold start upgrades in place once the build
        lands (set_thumbs clears the memo anyway — belt and braces)."""
        if self.thumbs is None:
            return None
        if rel in self._thumb_imgs:
            return self._thumb_imgs[rel]
        img = self._decode(rel)
        self._thumb_imgs[rel] = img
        return img

    def _decode(self, rel: str) -> QImage | None:
        """One tray thumb from the cache pair: cheapest level that covers
        THUMB, EXIF-upright in the cache, Picasa rotate= composed on top
        (same contract as the grid/viewer paths). Broad guard like
        viewer._load_preview: a corrupt index or dying volume degrades to
        an error tile, never an exception in a paint handler."""
        cache = self.thumbs
        idx = self._rel_idx.get(rel)
        if cache is None or idx is None or not (0 <= idx < cache.count):
            return None
        try:
            offset, length, _w, _h = cache.entry(idx, cache.best_level(THUMB))
            if length <= 0:
                return None  # error tile: original failed at build time
            with open(cache.path, "rb") as f:
                f.seek(offset)
                buf = f.read(length)
            img = QImage.fromData(buf, "JPEG")
            if img.isNull():
                return None
            rot = self.catalog.photos[idx].rotate
            if rot:
                img = img.transformed(QTransform().rotate(90 * rot))
            if img.width() > THUMB or img.height() > THUMB:
                img = img.scaled(
                    THUMB, THUMB,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            return img
        except Exception:  # noqa: BLE001 — degrade, never crash (grid.py)
            return None


class _ThumbsBar(QWidget):
    """Custom-painted strip of held thumbnails, left to right in hold
    order. Left-click a thumb: navigate the grid to it; middle-click:
    remove it; right-click: context menu (remove / clear). Overflow
    paints a '+N' tail cell instead of scrolling — tray counts are small
    and every action reads the model, not the pixels."""

    def __init__(self, tray: SelectionTray):
        super().__init__(tray)
        self._tray = tray
        self.setMinimumWidth(THUMB + 2 * PAD)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Preferred)
        self.setToolTip("Held photos — click: show in the grid · "
                        "middle-click: remove · Ctrl+H holds the selection")

    def _cell(self) -> int:
        return THUMB + PAD

    def _slots(self) -> int:
        """How many whole cells fit the current width (>= 1)."""
        return max(1, (self.width() - PAD) // self._cell())

    def _shown(self) -> tuple[int, int]:
        """(thumbs painted, overflow count). All of them when they fit;
        else one cell is sacrificed to the '+N' tail."""
        n = len(self._tray.held)
        slots = self._slots()
        if n <= slots:
            return n, 0
        shown = max(0, slots - 1)
        return shown, n - shown

    def item_at(self, x: int) -> int:
        """Index into tray.held of the PAINTED thumb under viewport x, or
        -1 (gap, '+N' tail, or empty tray)."""
        if x < PAD:
            return -1
        i = (x - PAD) // self._cell()
        shown, _more = self._shown()
        return i if i < shown else -1

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), BACKGROUND)
        tray = self._tray
        if not tray.held:
            painter.setPen(HINT_FG)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter,
                             "  Hold (Ctrl+H) keeps selected photos here "
                             "across folders")
            painter.end()
            return
        shown, more = self._shown()
        y = (self.height() - THUMB) // 2
        for i in range(shown):
            r = QRect(PAD + i * self._cell(), y, THUMB, THUMB)
            img = tray.thumb_image(tray.held[i])
            if img is not None:
                scale = min(THUMB / img.width(), THUMB / img.height(), 1.0)
                dw = max(1, round(img.width() * scale))
                dh = max(1, round(img.height() * scale))
                painter.drawImage(
                    QRect(r.x() + (THUMB - dw) // 2,
                          r.y() + (THUMB - dh) // 2, dw, dh), img)
            else:
                # No cache yet -> placeholder; a cached error -> error tile
                painter.fillRect(
                    r, PLACEHOLDER if tray.thumbs is None else ERROR_TILE)
        if more:
            r = QRect(PAD + shown * self._cell(), y, THUMB, THUMB)
            painter.setPen(MORE_FG)
            painter.drawText(r, Qt.AlignmentFlag.AlignCenter, f"+{more}")
        painter.end()

    def mousePressEvent(self, event) -> None:
        i = self.item_at(int(event.position().x()))
        if i < 0:
            super().mousePressEvent(event)
            return
        rel = self._tray.held[i]
        if event.button() == Qt.MouseButton.LeftButton:
            self._tray.navigate.emit(rel)
        elif event.button() == Qt.MouseButton.MiddleButton:
            self._tray.remove(rel)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        i = self.item_at(int(event.pos().x()))
        if i >= 0:
            rel = self._tray.held[i]
            act = menu.addAction(f"Remove from tray — {rel}")
            act.triggered.connect(lambda: self._tray.remove(rel))
        if self._tray.held:
            menu.addAction("Clear tray").triggered.connect(self._tray.clear)
        if menu.actions():
            menu.exec(event.globalPos())
