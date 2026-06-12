#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pillow",
# ]
# ///
"""Pre-build the packed thumbnail cache for the stack trial balloons
(fauxcasa-6hf / fauxcasa-rqe). Every balloon renders the 100k grid from
this one file; the format is deliberately trivial so any candidate
language reads it in a few lines.

    uv run scripts/make-thumbcache.py [--library DIR] [--out FILE]

Format (fcthumbs v1, all little-endian):

    header, 16 bytes:  magic b"FCTC" | u32 version=1 | u32 count | u32 0
    index, count * 16: u64 blob offset (from file start) | u32 blob length
                       | u16 thumb width | u16 thumb height
    blobs:             JPEG bytes, quality 80, 256 px long edge

Entry order = sorted library-relative POSIX path. A JSON sidecar
(<out>.json) records count, the source library, and the folder groups
(name, start index, count) in entry order, so a balloon can draw group
headers without ever touching the library itself. The grid budget rule
(N4) is that scrolling never reads originals — balloons may open ONLY
this cache pair.
"""

from __future__ import annotations

import argparse
import io
import json
import struct
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "cache"
MAGIC = b"FCTC"
THUMB_EDGE = 256
EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}


def _make_thumb(path: Path) -> tuple[bytes, int, int]:
    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((THUMB_EDGE, THUMB_EDGE))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return buf.getvalue(), img.width, img.height


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--library", type=Path, default=CACHE / "benchmark-library")
    ap.add_argument("--out", type=Path, default=CACHE / "benchmark-thumbs.fcache")
    ap.add_argument("--jobs", type=int, default=None)
    args = ap.parse_args(argv)
    if not args.library.is_dir():
        print(f"library not found: {args.library}", file=sys.stderr)
        return 2

    files = sorted(
        p for p in args.library.rglob("*")
        if p.suffix.lower() in EXTS and p.is_file()
    )
    if not files:
        print("no images found", file=sys.stderr)
        return 2

    groups: list[dict] = []
    for i, p in enumerate(files):
        folder = p.parent.relative_to(args.library).as_posix()
        if not groups or groups[-1]["name"] != folder:
            groups.append({"name": folder, "start": i, "count": 0})
        groups[-1]["count"] += 1

    index = bytearray()
    offset = 16 + 16 * len(files)
    tmp = args.out.with_suffix(".tmp")
    with tmp.open("wb") as f:
        f.write(b"\x00" * offset)  # header+index placeholder
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            for n, (blob, w, h) in enumerate(
                pool.map(_make_thumb, files, chunksize=32)
            ):
                index += struct.pack("<QIHH", offset, len(blob), w, h)
                f.write(blob)
                offset += len(blob)
                if n % 10000 == 0:
                    print(f"  {n}/{len(files)}", file=sys.stderr)
        f.seek(0)
        f.write(MAGIC + struct.pack("<III", 1, len(files), 0))
        f.write(index)
    tmp.replace(args.out)
    args.out.with_suffix(".fcache.json").write_text(
        json.dumps(
            {
                "count": len(files),
                "library": str(args.library),
                "thumb_edge": THUMB_EDGE,
                "groups": groups,
            },
            indent=1,
        )
    )
    print(f"{len(files)} thumbs -> {args.out} "
          f"({args.out.stat().st_size / 1e6:.0f} MB, {len(groups)} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
