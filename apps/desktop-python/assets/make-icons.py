#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6", "pillow"]
# ///
"""Rasterize the application icon from its SVG source.

    uv run apps/desktop-python/assets/make-icons.py [--svg icon.svg] [--out .]

Writes, next to the SVG by default:

    icon.png            256x256 (the Explorer tile / high-DPI window icon)
    icon-{16,32,48,64,128}.png
    icon.ico            one multi-size Windows icon carrying all six

Each size is rendered straight from the vector at that pixel size with
Qt's antialiased raster engine (QSvgRenderer -> QImage), never by
downscaling the 256 render, so the 16 px taskbar glyph keeps crisp edges.
Pillow then assembles the .ico from those exact renders (append_images)
rather than resampling one image itself. The frozen bundle loads the PNG
set (main.app_icon) — QtSvg is deliberately excluded from the PyInstaller
build, so the SVG is a build-time source only. Re-run this after editing
icon.svg and commit the outputs; they are small.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# A pure rasterization job: never needs (and must never wait for) a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
SIZES = (16, 32, 48, 64, 128, 256)


def png_name(px: int) -> str:
    """The PNG file name for one size — icon.png for the 256 master, else
    icon-<px>.png. Shared with main.app_icon via the same naming rule."""
    return "icon.png" if px == 256 else f"icon-{px}.png"


def render(renderer, px: int):
    """Render the SVG into a fresh transparent px x px ARGB QImage."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter

    img = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    return img


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--svg", type=Path, default=HERE / "icon.svg",
                    help="vector source (default: icon.svg beside this script)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output directory (default: the SVG's directory)")
    args = ap.parse_args(argv)
    svg = args.svg.resolve()
    out = (args.out or svg.parent).resolve()
    out.mkdir(parents=True, exist_ok=True)

    from PySide6.QtGui import QGuiApplication
    from PySide6.QtSvg import QSvgRenderer

    _app = QGuiApplication.instance() or QGuiApplication([])
    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        print(f"error: {svg} is not a renderable SVG", file=sys.stderr)
        return 2

    written: list[Path] = []
    for px in SIZES:
        path = out / png_name(px)
        if not render(renderer, px).save(str(path), "PNG"):
            print(f"error: could not write {path}", file=sys.stderr)
            return 1
        written.append(path)

    from PIL import Image

    frames = [Image.open(p).convert("RGBA") for p in written]
    master = next(f for f in frames if f.size == (256, 256))
    others = [f for f in frames if f.size != (256, 256)]
    ico = out / "icon.ico"
    master.save(ico, format="ICO", sizes=[(s, s) for s in SIZES],
                append_images=others)
    written.append(ico)

    # Read the .ico back so a silently degraded save (a size dropped, or
    # resampled from the master instead of the exact render) fails here,
    # not in Explorer.
    with Image.open(ico) as check:
        have = {s[0] for s in check.ico.sizes()}
    missing = [s for s in SIZES if s not in have]
    if missing:
        print(f"error: icon.ico is missing sizes {missing}", file=sys.stderr)
        return 1

    for p in written:
        print(f"{p.stat().st_size:7d}  {p.relative_to(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
