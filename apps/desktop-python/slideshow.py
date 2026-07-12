"""Full-screen timed slideshow over the current display set (fauxcasa-q6l.3).

M1 playback only — the read-only loop the product spec's §5 slideshow
bullet names; the overlay star/reject triage controls are M2 writes and
deliberately absent. "Modes, not modals" (§1): the show is its own
top-level full-screen surface, so the browser underneath is left EXACTLY
as it was — Esc simply puts you back where you started.

Rendering rides ViewerPage unchanged: the instant cached preview paints
synchronously while the full original decodes off-thread, so a slow
volume never blanks or blocks the show. On top of that this subclass adds
the playback loop:

- a dwell timer that auto-advances (SLIDE_DELAY_MS default);
- WRAP-AROUND at either end — the Picasa evidence corpus
  (docs/research/picasa-video-notes.md §1.6) documents how a slideshow
  STARTS but not what it does at the last photo, so we loop, the common
  slideshow convention (an explicit product decision can override later);
- Space pause/resume; Left/Right and J/K manual nav that keeps playing
  but restarts the full dwell; Esc/Backspace (or a double-click) exits;
- a dwell-time PREFETCH of the next photo's original, handed to
  ViewerPage._load_current via the _take_prefetched hook, so a timed
  advance is a pure image swap, not a fresh decode.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter

import keymap
from viewer import CAPTION_BG, ViewerPage, load_original

# Dwell per slide. 4 s follows the one delay default the Picasa corpus
# records — Movie Maker's "Slide Duration: 4.0s" (picasa-video-notes §1.6).
SLIDE_DELAY_MS = 4000
# The control hint auto-hides after this long (modes, not modals — no
# blocking chrome, just a transient strip).
HINT_MS = 4000
HINT_TEXT = "Space pause   ·   ←/→ next/prev   ·   Esc exit"
PAUSED_TEXT = "paused — Space resumes"


class SlideshowPage(ViewerPage):
    """A ViewerPage that plays itself. start() takes the same
    (display, pos) contract as ViewerPage.show_photo and goes full-screen;
    the `closed` signal (inherited) fires on exit so the owner can restore
    focus. The owner keeps ONE instance for the window's life and re-points
    catalog/thumbs before each start (as MainWindow.reload_data does for
    the viewer) — never deleting a live surface means its decode threads
    can never race a widget teardown (the fauxcasa-gfz crash family)."""

    def __init__(self, catalog, thumbs=None, delay_ms: int = SLIDE_DELAY_MS,
                 parent=None):
        super().__init__(catalog, thumbs, parent)
        self.setWindowTitle("Slideshow")
        # No face overlay on a glance surface (fauxcasa-cam.4): a timed
        # show is for watching, not inspecting — boxes would be noise.
        # (This surface's keyPressEvent doesn't route F anyway; the flag
        # makes the policy explicit and testable.)
        self.face_overlay_allowed = False
        self.paused = False
        self._timer = QTimer(self)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._advance)
        # Auto-hiding control hint (drawn in paintEvent while visible).
        self._hint_visible = False
        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.setInterval(HINT_MS)
        self._hint_timer.timeout.connect(self._hide_hint)
        # Next-photo prefetch: (catalog idx, decoded original | None-for-
        # failed). Decoded on the viewer's ONE persistent decode worker
        # (ViewerPage._submit) — queued after the current photo's own
        # decode, which is exactly the right order — and consumed on the
        # GUI thread via _take_prefetched; hence the lock. The serial ages
        # out jobs superseded by a newer kick (same pattern as the
        # viewer's own stale-load guard).
        self._prefetch_lock = threading.Lock()
        self._prefetched: tuple[int, QImage | None] | None = None
        self._prefetch_serial = 0

    def quiesce(self, timeout: float = 5.0) -> None:
        """Teardown discipline (see ViewerPage.quiesce): also age out any
        queued dwell-prefetch job, then retire the shared decode worker."""
        with self._prefetch_lock:
            self._prefetch_serial += 1
        super().quiesce(timeout)

    # ---------- lifecycle ----------

    def start(self, display: list[int], pos: int) -> None:
        """Go full-screen and play `display` from `pos` (the ViewerPage
        show_photo contract: catalog indices in display order)."""
        self.paused = False
        self._hint_visible = True
        self._hint_timer.start()
        self.showFullScreen()
        self.setFocus()
        self.activateWindow()
        self.show_photo(display, pos)   # instant preview + async original
        self._timer.start()

    def _exit(self) -> None:
        self._timer.stop()
        self.hide()
        self.closed.emit(self.current_index())

    def closeEvent(self, event) -> None:
        # A system close (Alt-F4) must tell the owner too, not just vanish.
        self._timer.stop()
        self.closed.emit(self.current_index())
        super().closeEvent(event)

    # ---------- playback ----------

    def _step(self, delta: int) -> None:
        """Wrap-around navigation (the base viewer clamps at the ends; a
        slideshow loops — see the module docstring for why)."""
        if not self.display:
            return
        self.pos = (self.pos + delta) % len(self.display)
        self._load_current()

    def _advance(self) -> None:
        self._step(1)

    def _manual(self, delta: int) -> None:
        """Arrow/J/K nav: move, and if playing, restart the FULL dwell so
        the photo the user just chose gets its whole time on screen.
        While paused, navigates but stays paused."""
        self._step(delta)
        if not self.paused:
            self._timer.start()

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        if self.paused:
            self._timer.stop()
        else:
            self._timer.start()
        self.update()

    def keyPressEvent(self, event) -> None:
        # Bindings via the keymap default scheme (fauxcasa-q6l.8).
        if keymap.matches(event, "slideshow.pause"):
            self.toggle_pause()
        elif keymap.matches(event, "slideshow.prev"):
            self._manual(-1)
        elif keymap.matches(event, "slideshow.next"):
            self._manual(1)
        elif keymap.matches(event, "slideshow.exit"):
            self._exit()
        else:
            event.ignore()

    def mouseDoubleClickEvent(self, _event) -> None:
        self._exit()

    # ---------- next-photo prefetch ----------

    def _load_current(self) -> None:
        super()._load_current()        # paint current (preview -> original)
        self._kick_prefetch()          # ...and warm the next during the dwell

    def _kick_prefetch(self) -> None:
        """Decode the NEXT photo's original off-thread during this dwell.
        _take_prefetched hands it to _load_current on advance, making the
        timed step a pure swap. A failed decode is remembered as None so
        the advance just takes the normal preview + async path."""
        if len(self.display) < 2:
            return
        nxt = self.display[(self.pos + 1) % len(self.display)]
        with self._prefetch_lock:
            if self._prefetched is not None and self._prefetched[0] == nxt:
                return                 # already decoded (or decode failed)
            self._prefetch_serial += 1
            serial = self._prefetch_serial
            self._prefetched = None    # drop a stale neighbour
        photo = self.catalog.photos[nxt]
        # Catalog.abs() is the single choke point for absolute-path
        # composition (multiroot .b, design §6). An offline/unresolvable
        # root (None) degrades to the same fail-soft path as an unreadable
        # file (empty path -> OSError -> null image) — the actual offline
        # placeholder/badge is bead .e's job.
        abs_path = self.catalog.abs(photo)
        path = str(abs_path) if abs_path is not None else ""
        rotate = photo.rotate

        def work() -> None:
            with self._prefetch_lock:
                if serial != self._prefetch_serial:
                    return             # superseded before it even started
            img = load_original(path, rotate)
            with self._prefetch_lock:
                if serial != self._prefetch_serial:
                    return             # superseded by a newer kick
                self._prefetched = (nxt, None if img.isNull() else img)

        self._submit(work)

    def _take_prefetched(self, idx: int) -> QImage | None:
        with self._prefetch_lock:
            got = self._prefetched
            if got is not None and got[0] == idx and got[1] is not None:
                self._prefetched = None
                return got[1]
        return None

    # ---------- hint strip ----------

    def _hide_hint(self) -> None:
        self._hint_visible = False
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)      # photo + caption bar, unchanged
        if not (self.paused or self._hint_visible):
            return
        painter = QPainter(self)
        painter.fillRect(0, 0, self.width(), 30, CAPTION_BG)
        painter.setPen(QColor(220, 220, 220))
        painter.drawText(0, 0, self.width(), 30,
                         Qt.AlignmentFlag.AlignCenter,
                         PAUSED_TEXT if self.paused else HINT_TEXT)
        painter.end()
