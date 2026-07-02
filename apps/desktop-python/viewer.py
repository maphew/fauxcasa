"""Single-photo viewer page for the tracer app.

Loading the original file here is the deliberate, explicit exception to
N4 (the grid itself never reads originals): the user asked to see this
one photo, and it loads asynchronously so the UI never blocks on a slow
volume. Navigation walks the grid's current display order.

While that original decodes off-thread, the viewer paints an INSTANT
stand-in read from the same thumbnail-cache pair the grid uses — the
fcache v2 hi-DPI / loupe consumer (fauxcasa-9pp). On a large or hi-DPI
window it pulls — via ThumbCache.best_level()/entry() — the smallest
cached level that covers the viewport's device pixels, or the largest
level available when none is big enough (the 512 px level of a v2 cache
on a typical window); a single-level v1 cache yields its 256 px level.
The preview fills the box the original will occupy, so for the common
case — a photo larger than the window — the hand-off is a pure
sharpen-in-place (see _display_rect for the small-original caveat).
Reading the cache is well within the grid budget (N4) — it is the
originals the grid must never touch, not the cache it owns.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

from catalog import Catalog, format_date_taken, format_geotag
from thumbcache import THUMB_EDGE, ThumbCache

BACKGROUND = QColor(12, 12, 12)
CAPTION_BG = QColor(0, 0, 0, 170)


def load_original(path: str, rotate: int) -> QImage:
    """Decode a full original: EXIF auto-orientation on read (so it matches
    the EXIF-baked grid thumbnails), then the Picasa rotate= user
    quarter-turns composed on top — see apps/desktop-python/README.md
    "EXIF orientation". The ONE full-image decode path, shared by the
    viewer's async load and the slideshow's dwell prefetch (slideshow.py),
    so every consumer orients identically. Returns a null QImage on
    failure. Thread-safe: QImage (unlike QPixmap) may be built off the GUI
    thread, and callers do call this from worker threads.

    RAW files route by extension to rawload BEFORE QImageReader can sniff
    the TIFF-based container (rawload module doc): embedded JPEG preview
    first — usually full-size, so the viewer stays responsive — else a
    full demosaic; orientation lands exactly once on either path, and the
    rotate= turns compose on top exactly as for any other format."""
    from PySide6.QtGui import QImageReader

    from rawload import is_raw_suffix, load_raw_qimage

    if is_raw_suffix(path):
        try:
            data = Path(path).read_bytes()
        except OSError:
            return QImage()
        img = load_raw_qimage(data)
    else:
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        img = reader.read()
    if not img.isNull() and rotate:
        from PySide6.QtGui import QTransform

        img = img.transformed(QTransform().rotate(90 * rotate))
    return img


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
        # ONE persistent, lazily started decode worker fed by a job queue —
        # NOT a thread per navigation. Short-lived Qt-touching threads exit
        # through Qt's per-thread native cleanup, and on offscreen Windows
        # that churn is exactly the cumulative state that tips the
        # fauxcasa-gfz access violations; the grid's long-lived pool is the
        # same discipline. Stale jobs cost one queue hop and bail on the
        # serial guard, so the queue stays bounded under key-repeat.
        self._jobs: queue.Queue = queue.Queue()
        self._decoder: threading.Thread | None = None
        self._loader = _Loader()
        self._loader.loaded.connect(self._on_loaded)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _submit(self, job) -> None:
        """Queue a decode job on the persistent worker (started on first
        use, so a viewer that never shows a photo never owns a thread)."""
        if self._decoder is None or not self._decoder.is_alive():
            self._decoder = threading.Thread(
                target=self._decode_loop, daemon=True)
            self._decoder.start()
        self._jobs.put(job)

    def _decode_loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return  # quiesce() sentinel: retire the worker
            job()

    def quiesce(self, timeout: float = 5.0) -> None:
        """Retire the decode worker BEFORE this widget is destroyed: bump
        the stale-guard serial so any queued job bails instead of emitting,
        then send the sentinel and join. A job that already passed its final
        serial check could otherwise emit into a receiver being torn down
        concurrently — the fauxcasa-gfz Windows access-violation family
        (grid.stop() is the same discipline for the grid's pool).
        Deliberately NOT called on ordinary navigation or viewer close: the
        serial guards handle staleness there, and joining a decode mid-read
        could stall the UI on a slow volume — this is for teardown paths
        (tests delete widgets aggressively)."""
        self._serial += 1
        t = self._decoder
        if t is not None and t.is_alive():
            self._jobs.put(None)
            t.join(timeout)

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

    def _take_prefetched(self, idx: int) -> QImage | None:
        """Subclass hook: hand back an already-decoded original for `idx`,
        or None. The base viewer never prefetches; the slideshow decodes the
        NEXT photo's original during the current dwell and surrenders it
        here, making a timed advance a pure swap (slideshow.py)."""
        return None

    def _load_current(self) -> None:
        idx = self.current_index()
        if idx < 0:
            return
        self._serial += 1
        serial = self._serial
        ready = self._take_prefetched(idx)
        if ready is not None:
            # A prefetcher already decoded this original during the previous
            # dwell: show it NOW — no preview flash, no redundant decode. The
            # serial bump above stales any in-flight load (_on_loaded guard),
            # exactly as a normal navigation would.
            self.loading = False
            self.image = ready
            self.preview = None
            self.photo_shown.emit(idx)
            self.update()
            return
        self.image = None
        self.loading = True
        path = str(self.catalog.root / self.catalog.photos[idx].rel)
        rotate = self.catalog.photos[idx].rotate
        # Show a cached stand-in NOW — synchronous, but cheap (a <= 512 px
        # JPEG decodes in a couple of ms) — so the viewport is never blank
        # while the worker below decodes the full original. This is the
        # fcache v2 loupe consumer: best_level() reads the >256 level on a
        # large/hi-DPI window, the 256 level from a v1 cache (fauxcasa-9pp).
        # Holding an arrow key pays this once per step inline, but it is bounded
        # by the key-repeat rate (one cheap decode per shown photo), not a
        # growing backlog — unlike the original, which needs the stale guards.
        self.preview = self._load_preview(idx, rotate)

        def work() -> None:
            # Stale checks bound the decode backlog when the user holds
            # an arrow key: superseded loads bail before the expensive
            # decode, and the emit is guarded against Qt teardown.
            if serial != self._serial:
                return
            img = load_original(path, rotate)
            if serial != self._serial:
                return
            try:
                self._loader.loaded.emit(serial, img)
            except RuntimeError:
                pass  # loader deleted at shutdown

        self._submit(work)
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
        # Read + decode + rotate under ONE broad guard, exactly as the grid's
        # decode worker does (grid.py): a corrupt index can hand us a multi-GiB
        # `length` (the read then raises MemoryError, not an OSError), a dying
        # volume an EIO, a bad blob a null decode. ANY of these must degrade to
        # "no preview" (the loading text) — never an exception escaping into the
        # synchronous Qt event handler that called us and aborting the UI.
        # A plain buffered "rb" + seek + read (NOT os.pread) keeps this portable:
        # os.pread is Unix-only — absent on Windows — and os.open there defaults
        # to text mode, which would mangle the JPEG bytes. One synchronous read,
        # so the atomic-offset reason the threaded grid uses os.pread for is moot.
        try:
            with open(cache.path, "rb") as f:
                f.seek(offset)
                buf = f.read(length)
            img = QImage.fromData(buf, "JPEG")
            if img.isNull():
                return None
            if rotate:
                from PySide6.QtGui import QTransform

                img = img.transformed(QTransform().rotate(90 * rotate))
            return img
        except Exception:  # noqa: BLE001 — match grid.py: degrade, never crash
            return None

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

    @staticmethod
    def _display_rect(box_w: int, box_h: int, src_w: int, src_h: int,
                      cap: bool) -> QRect:
        """Centered, aspect-fit rect for `src` painted in a `box`. `cap=True`
        clamps the scale to 1:1 — the ORIGINAL never upscales past native, so a
        small photo stays crisp. The preview passes `cap=False`, filling the box
        the original will occupy: when the original is at least window-sized
        (the common photo case) both compute the SAME rect, so the hand-off is a
        pure sharpen-in-place. (An original SMALLER than the window draws capped
        at native, smaller than the filled preview — a one-off downward pop we
        accept: the cache stores thumb dims, not the original's, so the preview
        cannot predict that fit.)"""
        fit = min(box_w / src_w, box_h / src_h)
        if cap:
            fit = min(fit, 1.0)
        dw = max(1, round(src_w * fit))
        dh = max(1, round(src_h * fit))
        return QRect((box_w - dw) // 2, (box_h - dh) // 2, dw, dh)

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
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(
                self._display_rect(w, h, shown.width(), shown.height(),
                                   cap=self.image is not None),
                shown)
        else:
            painter.setPen(QColor(150, 150, 150))
            msg = "loading…" if self.loading else "could not decode this file"
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, msg)

        photo = self.catalog.photos[idx]
        parts = [f"{self.pos + 1}/{len(self.display)}", photo.rel]
        if photo.star:
            parts.append("★" * min(photo.star, 5))  # 0-5 count (cam.11)
        if photo.date_taken:
            parts.append(format_date_taken(photo.date_taken))
        if photo.geotag is not None:
            parts.append(format_geotag(photo.geotag))  # §3 geotag readout
        if photo.caption:
            parts.append(f"“{photo.caption}”")
        bar = "   ·   ".join(parts)
        painter.fillRect(0, h - 30, w, 30, CAPTION_BG)
        painter.setPen(QColor(220, 220, 220))
        painter.drawText(10, h - 30, w - 20, 30,
                         Qt.AlignmentFlag.AlignVCenter, bar)
        painter.end()
