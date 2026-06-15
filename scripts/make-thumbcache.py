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

Entry order = sorted(Path) over the library walk, i.e. path COMPONENT
order — NOT a sort of the joined POSIX string (the two differ when one
folder name is a prefix of another that continues past a '/' boundary,
e.g. "2020" vs "2020-01 Trip"; the shipped benchmark cache uses
component order, so this rule is frozen). A JSON sidecar
(<out>.json) records count, the source library, the folder groups
(name, start index, count) and the per-entry library-relative file paths
("files", same order as the index records) in entry order, so a balloon
can draw group headers — and a tracer app can map any tile back to its
source photo — without ever touching the library itself. The grid budget
rule (N4) is that scrolling never reads originals — balloons may open
ONLY this cache pair.

--sidecar-only rewrites just the JSON sidecar from a fresh walk of the
library (validated against the existing .fcache entry count), upgrading
old caches to carry "files" without re-encoding the blobs. It is only
safe if the library has not changed since the cache was built.
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
    from PIL import Image, ImageOps

    with Image.open(path) as img:
        # Bake EXIF orientation (all 8 cases, mirrors too) so this builder
        # agrees with the in-app one (apps/desktop-python/thumbcache.py) and the viewer;
        # the Picasa rotate= user turns are composed live at display, never
        # baked. No-op for images without an Orientation tag.
        img = ImageOps.exif_transpose(img)
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
    ap.add_argument(
        "--sidecar-only",
        action="store_true",
        help="rewrite the JSON sidecar from a fresh library walk; "
        "requires an existing .fcache whose count matches",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="with --sidecar-only: overwrite even if the existing "
        "sidecar's files array disagrees with the fresh walk",
    )
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

    sidecar = {
        "count": len(files),
        # resolved, so consumers comparing against their own resolved
        # library root don't get CWD-dependent spurious mismatches
        "library": str(args.library.resolve()),
        "thumb_edge": THUMB_EDGE,
        "groups": groups,
        "files": [p.relative_to(args.library).as_posix() for p in files],
    }

    if args.sidecar_only:
        if not args.out.is_file():
            print(f"no existing cache at {args.out} — drop --sidecar-only "
                  f"to build one", file=sys.stderr)
            return 2
        try:
            with args.out.open("rb") as f:
                hdr = f.read(16)
            if len(hdr) < 16 or hdr[:4] != MAGIC:
                raise ValueError("not an fcache file")
            cached = struct.unpack("<III", hdr[4:16])[1]
        except (OSError, ValueError) as e:
            print(f"cannot read {args.out}: {e}", file=sys.stderr)
            return 2
        if cached != len(files):
            print(
                f"library walk found {len(files)} files but {args.out} holds "
                f"{cached} thumbs — library changed since build, rebuild instead",
                file=sys.stderr,
            )
            return 2
        # Count alone can't catch equal-count drift, and a wrong rewrite
        # silently mislabels every thumb — require whatever ordering
        # evidence the old sidecar carries (files array on new ones,
        # groups on v1 sidecars) to match the fresh walk.
        sidecar_path = args.out.with_suffix(".fcache.json")
        if sidecar_path.is_file() and not args.force:
            try:
                old = json.loads(sidecar_path.read_text())
            except (OSError, ValueError):
                old = None
            if not isinstance(old, dict):
                old = {}
            old_files = old.get("files")
            old_groups = old.get("groups")
            why = None
            if old_files is not None and old_files != sidecar["files"]:
                if len(old_files) != len(sidecar["files"]):
                    why = (f"old sidecar lists {len(old_files)} files vs "
                           f"{len(files)} in the fresh walk")
                else:
                    diffs = sum(1 for a, b in zip(old_files, sidecar["files"])
                                if a != b)
                    why = f"{diffs} of {len(files)} file entries differ"
            elif old_files is None and old_groups is not None \
                    and old_groups != groups:
                why = "folder groups disagree with the fresh walk"
            if why is not None:
                print(
                    f"existing sidecar disagrees with the fresh walk "
                    f"({why}) — library changed since build; rebuild, or "
                    f"--force to overwrite anyway", file=sys.stderr,
                )
                return 2
        sidecar_path.write_text(json.dumps(sidecar, indent=1))
        print(f"sidecar rewritten for {len(files)} thumbs ({len(groups)} groups)")
        return 0

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
    args.out.with_suffix(".fcache.json").write_text(json.dumps(sidecar, indent=1))
    print(f"{len(files)} thumbs -> {args.out} "
          f"({args.out.stat().st_size / 1e6:.0f} MB, {len(groups)} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
