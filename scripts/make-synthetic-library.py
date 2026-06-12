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

``--scale N`` instead writes cache/synthetic-library-scale/ — N small
solid-color photos spread over N/25 folders — for indexing-at-scale runs
against the oracle (bead fauxcasa-ok6). The default library is untouched.

``--benchmark N`` writes cache/benchmark-library/ — the M0 deliverable
(spec §9): N photos (reference target 100_000) with *defined composition*
so §7 numbers are reproducible: seeded-deterministic EXIF-date and
folder-shape distributions over a 25-year family-archive arc, two volume
roots (the §7 reference library spans two volumes), byte-identical
duplicates (~2%), missing-EXIF strays (~5%), mixed formats
(JPEG/PNG/GIF/BMP/TIFF/WebP), and a documented file-size distribution
scaled by ``--size-scale`` (default 0.02 keeps 100k photos ≈ 22 GB; 1.0
reproduces the documented multi-MB camera-JPEG distribution ≈ 500 GB and
is required for full-fidelity §7 *index-rate* gates — grid/scroll/cold
gates read only the thumbnail cache and are size-scale-independent;
see scripts/make-thumbcache.py).
"""

import argparse
import random
from pathlib import Path

import piexif
from PIL import Image, ImageDraw

CACHE = Path(__file__).resolve().parent.parent / "cache"
ROOT = CACHE / "synthetic-library"
SCALE_ROOT = CACHE / "synthetic-library-scale"
BENCH_ROOT = CACHE / "benchmark-library"

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


SCALE_SIZES = [(640, 480), (480, 640), (800, 600), (320, 240)]


def make_photo_fast(path: Path, label: str, idx: int, date: tuple[int, int, int]) -> None:
    """Solid-color variant for scale runs: same EXIF scheme, ~10ms each."""
    from colorsys import hsv_to_rgb

    w, h = SCALE_SIZES[idx % len(SCALE_SIZES)]
    hue = (idx * 137) % 360
    r, g, b = hsv_to_rgb(hue / 360, 0.6, 0.6)
    img = Image.new("RGB", (w, h), (int(r * 255), int(g * 255), int(b * 255)))
    draw = ImageDraw.Draw(img)
    draw.multiline_text(
        (w // 10, h // 3), f"{label}\n#{idx:02d}", fill="white", font_size=w // 12
    )
    y_, m_, d_ = date
    stamp = f"{y_}:{m_:02d}:{d_:02d} {idx % 24:02d}:{idx % 60:02d}:00"
    exif = piexif.dump({
        "0th": {piexif.ImageIFD.Make: "Fauxcasa", piexif.ImageIFD.Model: "SyntheticCam"},
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: stamp,
            piexif.ExifIFD.DateTimeDigitized: stamp,
        },
    })
    img.save(path, "JPEG", quality=70, exif=exif)


def make_scale_library(n_photos: int) -> None:
    per_folder = 25
    n_folders = max(1, (n_photos + per_folder - 1) // per_folder)
    left = n_photos
    for fi in range(n_folders):
        date = (2000 + fi % 20, 1 + fi % 12, 1 + fi % 28)
        folder = f"{date[0]}-{date[1]:02d}-{date[2]:02d} Scale Batch {fi:03d}"
        d = SCALE_ROOT / folder
        d.mkdir(parents=True, exist_ok=True)
        count = min(per_folder, left)
        left -= count
        for i in range(count):
            f = d / f"photo{i:03d}.jpg"
            if not f.exists():
                make_photo_fast(f, folder, fi * per_folder + i, date)
    print(f"{n_photos} photos in {n_folders} folders at {SCALE_ROOT}")


# --------------------------------------------------------------------------
# --benchmark: the M0 100k library with defined composition (spec §9)
# --------------------------------------------------------------------------

BENCH_SEED = 20260612  # fixed: same composition every run, every machine

# Event-folder size distribution (family-archive shape): weights chosen so
# the mean is ~55 photos/folder — 100k photos land in ~1800 folders.
FOLDER_SHAPES = [
    # (min_photos, max_photos, weight)  — small outings dominate by count
    (4, 15, 45),     # quick outings, single-subject bursts
    (16, 60, 35),    # day trips, birthdays
    (61, 250, 17),   # vacations, holiday seasons
    (251, 900, 3),   # weddings, the mega-events
]

# Year weights 2000..2025: film-scan trickle, digital ramp, phone-era flood.
YEAR_WEIGHTS = [1, 1, 2, 2, 3, 4, 4, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10,
                10, 11, 11, 12, 12, 13, 13, 14]

EVENT_WORDS = (
    "Beach Lake Garden Picnic Birthday Holiday Camping Hike Visit Market "
    "Festival Parade Museum Zoo Park River Snow Spring Summer Autumn Winter "
    "Reunion Wedding Graduation Concert Fair Harvest Cabin Road Trip"
).split()

UNDATED_FOLDERS = ["Scans", "Misc", "Phone Dump", "Old Album Box", "Inbox"]

# File-size distribution at --size-scale 1.0 (documented camera-JPEG shape):
# log-normal, median ~3.0 MB, clamped to [0.4 MB, 14 MB]. Sizes are hit by
# padding valid JPEG COM segments — files stay well-formed images.
SIZE_LOGNORM_MU, SIZE_LOGNORM_SIGMA = 14.9, 0.55  # ln-bytes
SIZE_CLAMP = (400_000, 14_000_000)

BENCH_FORMATS = [  # (PIL format, extension, weight)
    ("JPEG", ".jpg", 90),
    ("PNG", ".png", 4),
    ("GIF", ".gif", 2),
    ("BMP", ".bmp", 1),
    ("TIFF", ".tif", 2),
    ("WEBP", ".webp", 1),
]

BENCH_DIMS = [(1600, 1200), (1200, 1600), (2048, 1536), (1024, 768),
              (1920, 1080), (800, 600)]

DUPLICATE_RATE = 0.02   # byte-identical copies dropped into other folders
NO_EXIF_RATE = 0.05     # strays with no EXIF dates (mtime is their truth)


def _bench_photo_bytes(idx: int, label: str, fmt: str, dims: tuple[int, int],
                       stamp: str | None, target_size: int | None) -> bytes:
    """Render one deterministic photo to bytes (solid hue + label text)."""
    import io
    from colorsys import hsv_to_rgb

    w, h = dims
    hue = (idx * 137) % 360
    r, g, b = hsv_to_rgb(hue / 360, 0.6, 0.6)
    img = Image.new("RGB", (w, h), (int(r * 255), int(g * 255), int(b * 255)))
    draw = ImageDraw.Draw(img)
    draw.multiline_text(
        (w // 12, h // 3), f"{label}\n#{idx:06d}", fill="white", font_size=w // 14
    )
    buf = io.BytesIO()
    if fmt == "JPEG" and stamp is not None:
        exif = piexif.dump({
            "0th": {piexif.ImageIFD.Make: "Fauxcasa",
                    piexif.ImageIFD.Model: "SyntheticCam"},
            "Exif": {piexif.ExifIFD.DateTimeOriginal: stamp,
                     piexif.ExifIFD.DateTimeDigitized: stamp},
        })
        img.save(buf, fmt, quality=70, exif=exif)
    else:
        img.save(buf, fmt)
    data = buf.getvalue()
    if fmt == "JPEG" and target_size is not None and target_size > len(data):
        # pad with COM segments (0xFFFE, <=65533 payload each) before EOI;
        # the file stays a valid JPEG at the documented byte size
        pad_total = target_size - len(data)
        segs = []
        while pad_total > 0:
            payload = min(pad_total, 65000)
            segs.append(b"\xff\xfe" + (payload + 2).to_bytes(2, "big")
                        + b"\x00" * payload)
            pad_total -= payload + 4
        data = data[:-2] + b"".join(segs) + data[-2:]
    return data


def _bench_plan(n_photos: int, size_scale: float) -> list[dict]:
    """The deterministic composition: a flat list of photo work orders."""
    rng = random.Random(BENCH_SEED)
    plan: list[dict] = []
    folder_year_seq: dict[int, int] = {}
    idx = 0
    while idx < n_photos:
        year = rng.choices(range(2000, 2026), weights=YEAR_WEIGHTS)[0]
        lo, hi, _w = rng.choices(FOLDER_SHAPES,
                                 weights=[s[2] for s in FOLDER_SHAPES])[0]
        count = min(rng.randint(lo, hi), n_photos - idx)
        month, day = rng.randint(1, 12), rng.randint(1, 28)
        volume = "volA" if rng.random() < 0.7 else "volB"  # two-volume split
        seq = folder_year_seq.get(year, 0)
        folder_year_seq[year] = seq + 1
        if rng.random() < 0.04:  # undated shoebox folders, no event date
            name = f"{rng.choice(UNDATED_FOLDERS)} {year}-{seq:02d}"
            dated = False
        else:
            event = " ".join(rng.sample(EVENT_WORDS, rng.randint(1, 2)))
            name = f"{year}-{month:02d}-{day:02d} {event} {seq:02d}"
            dated = True
        # ~20% of folders nest under a year directory (mixed folder shapes)
        rel = (f"{volume}/{year}/{name}" if rng.random() < 0.2
               else f"{volume}/{name}")
        for i in range(count):
            fmt, ext, _w2 = rng.choices(BENCH_FORMATS,
                                        weights=[f[2] for f in BENCH_FORMATS])[0]
            dims = rng.choice(BENCH_DIMS)
            if dated and rng.random() > NO_EXIF_RATE and fmt == "JPEG":
                stamp = (f"{year}:{month:02d}:{day:02d} "
                         f"{rng.randint(8, 21):02d}:{rng.randint(0, 59):02d}:00")
            else:
                stamp = None
            target = None
            if fmt == "JPEG" and size_scale > 0:
                raw = rng.lognormvariate(SIZE_LOGNORM_MU, SIZE_LOGNORM_SIGMA)
                target = int(max(SIZE_CLAMP[0], min(SIZE_CLAMP[1], raw))
                             * size_scale)
            plan.append({"idx": idx, "rel": f"{rel}/img{i:04d}{ext}",
                         "label": name, "fmt": fmt, "dims": dims,
                         "stamp": stamp, "target": target})
            idx += 1
    # byte-identical duplicates: replace the tail ~2% of work orders with
    # copies of earlier photos, landed in *different* folders
    n_dupes = int(len(plan) * DUPLICATE_RATE)
    for k in range(n_dupes):
        victim = plan[-(k + 1)]
        src = plan[rng.randrange(0, len(plan) - n_dupes)]
        stem = (Path(victim["rel"]).parent
                / f"copy{k:04d}-of-{Path(src['rel']).name}")
        plan[-(k + 1)] = {**src, "rel": str(stem), "dupe_of": src["rel"]}
    return plan


def _bench_write_one(args: tuple) -> int:
    plan_item, root = args
    out = root / plan_item["rel"]
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        return 0
    data = _bench_photo_bytes(
        plan_item["idx"], plan_item["label"], plan_item["fmt"],
        tuple(plan_item["dims"]), plan_item["stamp"], plan_item["target"],
    )
    out.write_bytes(data)
    return len(data)


def make_benchmark_library(n_photos: int, size_scale: float, jobs: int) -> None:
    import json
    from concurrent.futures import ProcessPoolExecutor

    plan = _bench_plan(n_photos, size_scale)
    # dupes must be written after their sources; sources first, then dupes
    sources = [p for p in plan if "dupe_of" not in p]
    dupes = [p for p in plan if "dupe_of" in p]
    written = 0
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for n in pool.map(_bench_write_one,
                          ((p, BENCH_ROOT) for p in sources), chunksize=64):
            written += n
    for p in dupes:
        out = BENCH_ROOT / p["rel"]
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists():
            out.write_bytes((BENCH_ROOT / p["dupe_of"]).read_bytes())
    manifest = {
        "photos": len(plan), "seed": BENCH_SEED, "size_scale": size_scale,
        "duplicates": len(dupes),
        "folders": len({str(Path(p["rel"]).parent) for p in plan}),
    }
    (BENCH_ROOT / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"{len(plan)} photos ({len(dupes)} dupes) in {manifest['folders']} "
          f"folders at {BENCH_ROOT} (size_scale={size_scale})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--scale",
        type=int,
        default=None,
        metavar="N",
        help=f"write N fast photos to {SCALE_ROOT.name}/ instead",
    )
    ap.add_argument(
        "--benchmark",
        type=int,
        default=None,
        metavar="N",
        help=f"write the N-photo M0 benchmark library to {BENCH_ROOT.name}/",
    )
    ap.add_argument(
        "--size-scale",
        type=float,
        default=0.02,
        help="benchmark file-size scale (1.0 = documented camera-JPEG "
        "distribution, ~500 GB at 100k; default 0.02 ≈ 22 GB)",
    )
    ap.add_argument(
        "--jobs", type=int, default=None,
        help="parallel writers for --benchmark (default: CPU count)",
    )
    args = ap.parse_args()
    if args.scale is not None:
        if args.scale < 1:
            ap.error("--scale must be >= 1")
        make_scale_library(args.scale)
        return
    if args.benchmark is not None:
        if args.benchmark < 1:
            ap.error("--benchmark must be >= 1")
        import os
        make_benchmark_library(
            args.benchmark, args.size_scale, args.jobs or os.cpu_count() or 4
        )
        return
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
