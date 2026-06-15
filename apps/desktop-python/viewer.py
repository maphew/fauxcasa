"""Single-photo viewer page for the tracer app.

Loading the original file here is the deliberate, explicit exception to
N4 (the grid itself never reads originals): the user asked to see this
one photo, and it loads asynchronously so the UI never blocks on a slow
volume. Navigation walks the grid's current display order.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

from catalog import Catalog

BACKGROUND = QColor(12, 12, 12)
CAPTION_BG = QColor(0, 0, 0, 170)


class _Loader(QObject):
    loaded = Signal(int, QImage)  # serial, image (null = failed)


class ViewerPage(QWidget):
    closed = Signal(int)  # catalog index in view when closed
    photo_shown = Signal(int)

    def __init__(self, catalog: Catalog, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.display: list[int] = []
        self.pos = 0
        self.image: QImage | None = None
        self.loading = False
        self._serial = 0
        self._loader = _Loader()
        self._loader.loaded.connect(self._on_loaded)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

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

    def _on_loaded(self, serial: int, img: QImage) -> None:
        if serial != self._serial:
            return  # user already moved on
        self.loading = False
        self.image = None if img.isNull() else img
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
        if self.image is not None:
            img = self.image
            scale = min(w / img.width(), h / img.height(), 1.0)
            dw, dh = int(img.width() * scale), int(img.height() * scale)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(
                QRect((w - dw) // 2, (h - dh) // 2, dw, dh), img)
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
