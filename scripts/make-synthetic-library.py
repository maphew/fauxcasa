#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pillow",
#   "piexif",
# ]
# ///
"""Generate a synthetic photo library at cache/synthetic-library/ for
differential testing against real Picasa (the Wine oracle) and Fauxcasa.

Fully synthetic — safe to show to agents, screenshot, or publish. Deterministic:
same output every run. Each photo is visually distinct (color + label) and
carries EXIF DateTimeOriginal matching its folder's date.
"""

from pathlib import Path

import piexif
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent / "cache" / "synthetic-library"

FOLDERS = [
    ("2009-07-04 Beach Day", (2009, 7, 4), 8),
    ("2010-12-25 Winter Holiday", (2010, 12, 25), 8),
    ("2015-03-15 Garden Project", (2015, 3, 15), 8),
]

SIZES = [(1600, 1200), (1200, 1600), (2048, 1536), (800, 600)]


def make_photo(path: Path, label: str, idx: int, date: tuple[int, int, int]) -> None:
    w, h = SIZES[idx % len(SIZES)]
    hue = (idx * 137) % 360  # golden-angle spacing keeps colors distinct
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(0, h, 4):  # coarse vertical gradient, blocked for speed
        from colorsys import hsv_to_rgb
        r, g, b = hsv_to_rgb(hue / 360, 0.6, 0.35 + 0.5 * y / h)
        row = (int(r * 255), int(g * 255), int(b * 255))
        for yy in range(y, min(y + 4, h)):
            for x in range(w):
                px[x, yy] = row
    draw = ImageDraw.Draw(img)
    text = f"{label}\n#{idx:02d}  {w}x{h}"
    draw.multiline_text((w // 10, h // 3), text, fill="white", font_size=w // 16)

    y_, m_, d_ = date
    stamp = f"{y_}:{m_:02d}:{d_:02d} {9 + idx}:00:00"
    exif = piexif.dump({
        "0th": {piexif.ImageIFD.Make: "Fauxcasa", piexif.ImageIFD.Model: "SyntheticCam"},
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: stamp,
            piexif.ExifIFD.DateTimeDigitized: stamp,
        },
    })
    img.save(path, "JPEG", quality=85, exif=exif)


def main() -> None:
    for folder, date, count in FOLDERS:
        d = ROOT / folder
        d.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            f = d / f"photo{i:02d}.jpg"
            if not f.exists():
                make_photo(f, folder, i, date)
        print(f"{folder}: {count} photos")
    print(f"library at {ROOT}")


if __name__ == "__main__":
    main()
