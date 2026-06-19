"""Single-photo viewer page for the tracer app.

Loading the original file here is the deliberate, explicit exception to
N4 (the grid itself never reads originals): the user asked to see this
one photo, and it loads asynchronously so the UI never blocks on a slow
volume. Navigation walks the grid's current display order.

While that original decodes off-thread, the viewer paints an INSTANT
stand-in read from the same thumbnail-cache pair the grid uses — the
fcache v2 hi-DPI / loupe consumer (fauxcasa-9pp). On a large or hi-DPI
window it pulls the nearest cached level >= the viewport's device pixels
via ThumbCache.best_level()/entry() (the 512 px level of a v2 cache); a
single-level v1 cache yields its 256 px level. The preview fills the box
the original will occupy, so the hand-off is a sharpen-in-place, never a
size pop. Reading the cache is well within the grid budget (N4) — it is
the originals the grid must never touch, not the cache it owns.
"""

from __future__ import annotations

import os
import threading

from PySide6.QtCore import QObject, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

from catalog import Catalog
from thumbcache import THUMB_EDGE, ThumbCache

BACKGROUND = QColor(12, 12, 12)
CAPTION_BG = QColor(0, 0, 0, 170)


class _Loader(QObject):
    loaded = Signal(int, QImage)  # serial, image (null = failed)


class ViewerPage(QWidget):
    closed = Signal(int)  # catalog index in view when closed
    photo_shown = Signal(int)

    def __init__(self, catalog: Catalog, thumbs: ThumbCache | None = None,
                 parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.thumbs = thumbs
        self.display: list[int] = []
        self.pos = 0
        self.image: QImage | None = None
        # Instant low-res stand-in from the thumb cache, painted while the
        # full original decodes off-thread (loupe preview, fauxcasa-9pp).
        self.preview: QImage | None = None
        self.loading = False
        self._serial = 0
        self._loader = _Loader()
        self._loader.loaded.connect(self._on_loaded)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_thumbs(self, thumbs: ThumbCache | None) -> None:
        """Adopt a freshly built or reconciled cache (cold-build finish or the
        reconcile swap in main), so the next photo shown gets an instant
        preview. Previews are decoded per-show, so there is no stale tile state
        to invalidate — just re-point at the new cache."""
        self.thumbs = thumbs

    def show_photo(self, display: list[int], pos: int) -> None:
        self.display = display
        self.pos = max(0, min(len(display) - 1, pos))
        self._load_current()

    def current_index(self) -> int:
        if not self.display:
            return -1
        return self.display[self.pos]

    def _load_current(self) -> None:
        idx = self.current_index()
        if idx < 0:
            return
        self.image = None
        self.loading = True
        self._serial += 1
        serial = self._serial
        path = str(self.catalog.root / self.catalog.photos[idx].rel)
        rotate = self.catalog.photos[idx].rotate
        # Show a cached stand-in NOW — synchronous, but cheap (a <= 512 px
        # JPEG decodes in a couple of ms) — so the viewport is never blank
        # while the worker below decodes the full original. This is the
        # fcache v2 loupe consumer: best_level() reads the >256 level on a
        # large/hi-DPI window, the 256 level from a v1 cache (fauxcasa-9pp).
        self.preview = self._load_preview(idx, rotate)

        def work() -> None:
            # Stale checks bound the decode backlog when the user holds
            # an arrow key: superseded loads bail before the expensive
            # decode, and the emit is guarded against Qt teardown.
            if serial != self._serial:
                return
            # Apply EXIF orientation on read (setAutoTransform), so the
            # viewer matches the EXIF-baked grid thumbnails; the Picasa
            # rotate= user quarter-turns compose on top. See
            # apps/desktop-python/README.md "EXIF orientation".
            from PySide6.QtGui import QImageReader

            reader = QImageReader(path)
            reader.setAutoTransform(True)
            img = reader.read()
            if serial != self._serial:
                return
            if not img.isNull() and rotate:
                from PySide6.QtGui import QTransform

                img = img.transformed(QTransform().rotate(90 * rotate))
            try:
                self._loader.loaded.emit(serial, img)
            except RuntimeError:
                pass  # loader deleted at shutdown

        threading.Thread(target=work, daemon=True).start()
        self.photo_shown.emit(idx)
        self.update()

    def _preview_min_edge(self) -> int:
        """Long edge, in DEVICE pixels, the preview should cover so best_level()
        picks a level that is sharp at this window's DPI. Floored at the grid's
        256 px tile (never preview below the grid); a large or hi-DPI window
        clears 256 and selects a larger v2 level."""
        longest = max(self.width(), self.height())
        return max(THUMB_EDGE, round(longest * self.devicePixelRatioF()))

    def _load_preview(self, idx: int, rotate: int) -> QImage | None:
        """Decode the nearest cached level for `idx` as an instant stand-in, or
        None when there is no usable cached pixel: no cache, an out-of-range
        index, an error-tile entry (zero-length blob), or an unreadable/corrupt
        blob — the caller then falls back to the loading text. The cached thumb
        is EXIF-upright; the Picasa rotate= quarter-turns compose on top exactly
        as the grid and the original path do, so every display path agrees."""
        cache = self.thumbs
        if cache is None or not (0 <= idx < cache.count):
            return None
        level = cache.best_level(self._preview_min_edge())
        offset, length, _w, _h = cache.entry(idx, level)
        if length <= 0:
            return None  # error tile: the original failed to decode at build
        try:
            fd = os.open(cache.path, os.O_RDONLY)
            try:
                buf = os.pread(fd, length, offset)
            finally:
                os.close(fd)
        except OSError:
            return None  # cache vanished / EIO: just skip the preview
        img = QImage.fromData(buf, "JPEG")
        if img.isNull():
            return None
        if rotate:
            from PySide6.QtGui import QTransform

            img = img.transformed(QTransform().rotate(90 * rotate))
        return img

    def _on_loaded(self, serial: int, img: QImage) -> None:
        if serial != self._serial:
            return  # user already moved on
        self.loading = False
        if img.isNull():
            self.image = None     # keep painting the preview, if we have one
        else:
            self.image = img
            self.preview = None   # the full original supersedes the preview
        self.update()

    def _step(self, delta: int) -> None:
        if not self.display:
            return
        self.pos = max(0, min(len(self.display) - 1, self.pos + delta))
        self._load_current()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
            self.closed.emit(self.current_index())
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_K):
            self._step(-1)
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_J, Qt.Key.Key_Space):
            self._step(1)
        else:
            super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, _event) -> None:
        self.closed.emit(self.current_index())

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), BACKGROUND)
        w, h = self.width(), self.height()
        idx = self.current_index()
        if idx < 0:
            painter.end()
            return
        # The full original once it has arrived, else the instant cached
        # preview; only when neither exists do we fall back to text.
        shown = self.image if self.image is not None else self.preview
        if shown is not None:
            fit = min(w / shown.width(), h / shown.height())
            # The original never upscales past 1:1 (a small photo stays crisp at
            # native size); the low-res preview DOES fill the box the original
            # will occupy, so the swap is a sharpen-in-place, not a size pop.
            if self.image is not None:
                fit = min(fit, 1.0)
            dw = max(1, round(shown.width() * fit))
            dh = max(1, round(shown.height() * fit))
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(
                QRect((w - dw) // 2, (h - dh) // 2, dw, dh), shown)
        else:
            painter.setPen(QColor(150, 150, 150))
            msg = "loading…" if self.loading else "could not decode this file"
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, msg)

        photo = self.catalog.photos[idx]
        parts = [f"{self.pos + 1}/{len(self.display)}", photo.rel]
        if photo.star:
            parts.append("★")
        if photo.caption:
            parts.append(f"“{photo.caption}”")
        bar = "   ·   ".join(parts)
        painter.fillRect(0, h - 30, w, 30, CAPTION_BG)
        painter.setPen(QColor(220, 220, 220))
        painter.drawText(10, h - 30, w - 20, 30,
                         Qt.AlignmentFlag.AlignVCenter, bar)
        painter.end()
