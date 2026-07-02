"""Hover full-screen peek surface (fauxcasa-q6l.5) — the second N4
exception (the first is the viewer's explicit 1:1 zoom).

Picasa's trigger, verbatim from the shortcut corpus
(docs/research/sources/picasaresources/keyboard-shortcuts.md): "Hover
over a photo and use Ctrl-Alt: Full-screen photo preview". The modifier
chord means a bare hover can never accidentally fire. The GRID owns the
trigger state machine (GridView.peek_requested / peek_released — hover
tracking, Ctrl+Alt press/release, click and Esc dismissal); this class
only displays: a frameless, full-screen, INPUT-TRANSPARENT top-level
surface on the screen containing the cursor (multi-monitor: the peek
appears where the user is working).

Input transparency is the load-bearing choice: the peek necessarily
appears UNDER the cursor that triggered it, so without
WindowTransparentForInput it would swallow the very mouse stream the
trigger needs — hover to the next photo re-points the peek, mouse-out
dismisses, a click dismisses-and-selects, all through the grid.
WindowDoesNotAcceptFocus + WA_ShowWithoutActivating (+ show(), never
showFullScreen(), which activates) keep the keyboard focus on the grid
too, so Ctrl/Alt release and Esc land there: the peek never steals
persistent focus from the grid.

Rendering rides ViewerPage unchanged — the instant cached preview from
the shared thumb cache paints synchronously while the full original
decodes on the ONE persistent decode worker (the same machinery the
viewer and slideshow share; no per-peek threads, fauxcasa-gfz). The
owner keeps ONE instance for the window's life and re-points
catalog/thumbs before each peek, the slideshow lifecycle discipline:
never deleting a live surface means its decode worker can never race a
widget teardown.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor, QGuiApplication

from viewer import ViewerPage


class PeekPage(ViewerPage):
    """peek(idx) shows one photo full-screen at fit; dismiss() hides. All
    lifecycle decisions (when to show, re-point, or dismiss) belong to the
    owner, driven by the grid's trigger signals."""

    def __init__(self, catalog, thumbs=None, parent=None):
        super().__init__(catalog, thumbs, parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # The base viewer takes StrongFocus for its triage keys; a peek is
        # pure output and must never pull focus off the grid.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # No face overlay on a glance surface (fauxcasa-cam.4): a peek is
        # a sub-second full-screen flash — boxes and labels there are
        # noise, and the surface takes no keys to toggle them anyway.
        self.face_overlay_allowed = False

    @staticmethod
    def _target_screen():
        """The screen containing the cursor, falling back to the primary
        (screenAt returns None in a cursor-less/headless session)."""
        return (QGuiApplication.screenAt(QCursor.pos())
                or QGuiApplication.primaryScreen())

    def peek(self, idx: int) -> None:
        """Show catalog index `idx` full-screen AT FIT on the cursor's
        screen. show_photo funnels through _load_current, so any zoom state
        a previous peek left resets, the cached preview paints instantly,
        and the original decodes async on the persistent worker."""
        screen = self._target_screen()
        if screen is not None:
            # Frameless + the whole screen geometry IS full-screen, without
            # showFullScreen()'s window activation.
            self.setGeometry(screen.geometry())
        self.show_photo([idx], 0)
        self.show()

    def dismiss(self) -> None:
        self.hide()
