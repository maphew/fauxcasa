"""Monochrome runtime-painted glyphs for toolbar/sidebar icons (polish
day 2, fauxcasa-ez2.14).

No binary assets: each glyph is a small QPainter routine over a
transparent QPixmap, so the icon set follows theme.py's palette (a
future theme swap repaints for free) and needs no rasterizer step like
assets/make-icons.py's app-icon PNGs. Every glyph is registered at both
1x (16 px) and 2x (32 px) into one QIcon via addPixmap — Qt picks the
matching pixmap for the current devicePixelRatio instead of scaling a
single bitmap blurry.

Glyph names are the vocabulary this module understands; make_icon raises
KeyError on an unknown name (a typo should fail loud, not paint a blank
tile) via the _GLYPHS dict below acting as the single source of truth.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QPolygonF

# Base (1x) glyph edge, matching the toolbar/sidebar's 16 px convention;
# the 2x pixmap is rendered at double resolution with the same logical
# geometry (painter.scale), not an upscaled bitmap.
BASE_SIZE = 16


def _new_pixmap(scale: int) -> tuple[QPixmap, QPainter]:
    px = BASE_SIZE * scale
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(scale, scale)
    return pm, painter


def _draw_folder(painter: QPainter, color: QColor) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    # Tab + body, Picasa-folder-tree proportions inside a 16x16 box.
    painter.drawRoundedRect(QRectF(2, 6, 5, 2), 1, 1)
    painter.drawRoundedRect(QRectF(2, 6.5, 12, 7), 1, 1)


def _draw_album(painter: QPainter, color: QColor) -> None:
    # A stack of two photo tiles (album = a grouped set of photos).
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color.red(), color.green(), color.blue(), 140))
    painter.drawRoundedRect(QRectF(4, 2, 10, 8), 1, 1)
    painter.setBrush(color)
    painter.drawRoundedRect(QRectF(2, 6, 10, 8), 1, 1)


def _draw_person(painter: QPainter, color: QColor) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(QRectF(5, 2, 6, 6))       # head
    painter.drawEllipse(QRectF(2, 9, 12, 8))       # shoulders (clipped by
    #                                                 the 16px box below)


def _draw_star(painter: QPainter, color: QColor) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    cx, cy, r_out, r_in = 8.0, 8.0, 6.5, 2.7
    pts = []
    import math
    for i in range(10):
        r = r_out if i % 2 == 0 else r_in
        ang = math.pi / 2 + i * math.pi / 5
        pts.append(QPointF(cx + r * math.cos(ang), cy - r * math.sin(ang)))
    painter.drawPolygon(QPolygonF(pts))


def _draw_clock(painter: QPainter, color: QColor) -> None:
    pen = painter.pen()
    pen.setColor(color)
    pen.setWidthF(1.4)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QRectF(2, 2, 12, 12))
    painter.drawLine(QPointF(8, 8), QPointF(8, 4.2))
    painter.drawLine(QPointF(8, 8), QPointF(11, 9.5))


def _draw_library(painter: QPainter, color: QColor) -> None:
    # "Library…" toolbar action: an open folder (same body as _draw_folder,
    # distinct enough at 16px via the toolbar's text label alongside it).
    _draw_folder(painter, color)


def _draw_back(painter: QPainter, color: QColor) -> None:
    # Left-pointing chevron/arrow: "← Gallery" back action.
    pen = painter.pen()
    pen.setColor(color)
    pen.setWidthF(1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPolyline(QPolygonF([
        QPointF(10, 3), QPointF(5, 8), QPointF(10, 13),
    ]))


def _draw_play(painter: QPainter, color: QColor) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawPolygon(QPolygonF([
        QPointF(4, 2.5), QPointF(4, 13.5), QPointF(13, 8),
    ]))


def _draw_info(painter: QPainter, color: QColor) -> None:
    pen = painter.pen()
    pen.setColor(color)
    pen.setWidthF(1.4)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QRectF(2, 2, 12, 12))
    painter.setBrush(color)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(QRectF(7.1, 4.3, 1.8, 1.8))       # dot
    painter.drawRoundedRect(QRectF(7.1, 7.2, 1.8, 5), 0.6, 0.6)  # stem


def _draw_zoom_small(painter: QPainter, color: QColor) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawRoundedRect(QRectF(5.5, 5.5, 5, 5), 0.8, 0.8)


def _draw_zoom_large(painter: QPainter, color: QColor) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawRoundedRect(QRectF(2, 2, 12, 12), 1.2, 1.2)


_GLYPHS = {
    "library": _draw_library,
    "back": _draw_back,
    "play": _draw_play,
    "info": _draw_info,
    "folder": _draw_folder,
    "album": _draw_album,
    "person": _draw_person,
    "star": _draw_star,
    "clock": _draw_clock,
    "zoom_small": _draw_zoom_small,
    "zoom_large": _draw_zoom_large,
}


def make_icon(name: str, color: QColor) -> QIcon:
    """A QIcon for glyph `name`, painted in `color` at 1x and 2x (16/32 px)
    so hi-DPI toolbars/trees get a crisp pixmap instead of a blurry
    upscale. Raises KeyError for an unknown name — see module docstring."""
    draw = _GLYPHS[name]
    icon = QIcon()
    for scale in (1, 2):
        pm, painter = _new_pixmap(scale)
        draw(painter, color)
        painter.end()
        icon.addPixmap(pm)
    return icon
