#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6", "rawpy", "exiv2", "pillow", "av", "numpy", "psutil"]
# ///
"""§7 RAW indexing throughput (fauxcasa-ed5.13): measure the tracer's real
indexer (thumbcache.build_cache) over a synthetic RAW corpus, split into the
two decode paths rawload.py routes between --

  - embedded-preview path: the RAW carries a JPEG preview (LibRaw
    extract_thumb), which rides the ordinary scaled-JPEG decode -- expected
    fast, comparable to a plain JPEG original.
  - demosaic-fallback path: no usable preview, so a real half-size LibRaw
    postprocess() produces the thumbnail pixels directly -- the suspected
    slow path (thumbcache.py's INDEX_WORKERS comment).

-- against the spec §7 budget: >= 30 photos/s sustained, INCLUDING content
hashing, on the local volume.

SYNTHETIC CORPUS, NOT REAL PHOTOS (privacy rule -- see bd memory
'privacy-real-photos'/'privacy-real-picasa-data'): every DNG here is
hand-assembled bytes, ported from test_tracer.py's `_make_dng` (a minimal
but LibRaw-valid little-endian DNG 1.4: TIFF container, RGGB CFA, the tags
identify() requires). Two differences from the test fixture, both load-
bearing for a THROUGHPUT measurement rather than a correctness one:

  1. Resolution. The test fixture is 32x24 px -- fine for exercising a code
     path, uselessly small for measuring decode cost (which scales with
     sensor area). This script defaults to 4000x3000 (~12 MP), a realistic
     consumer-camera raw size.
  2. Content. The test fixture's CFA is one fixed deterministic gradient.
     Here every file gets its own numpy-seeded noise mosaic (and, for the
     preview variant, its own seeded gradient+noise JPEG) so per-file decode
     work is not degenerate/cacheable.

Everything else -- tag layout, the SubIFD split between preview and raw,
the tags LibRaw's identify() needs -- is unchanged from the verified test
fixture.

Usage::

    uv run scripts/bench-raw-indexing.py                  # default: 60+60 @ 4000x3000, 3 runs
    uv run scripts/bench-raw-indexing.py --n 20 --width 3000 --height 2000
    uv run scripts/bench-raw-indexing.py --keep            # keep the corpus for inspection
    uv run scripts/bench-raw-indexing.py --json out.json   # also write machine-readable results

Exits non-zero if the box isn't quiet (other python-ish processes running)
unless --force is passed; this is a measurement caveat, not a pass/fail gate
-- see docs/research/raw-indexing-throughput.md for the verdict.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import shutil
import statistics
import struct
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "apps" / "desktop-python"))

import numpy as np
import psutil
from PIL import Image

from catalog import Catalog, Photo
from rawload import raw_demosaic_qimage, raw_preview_jpeg
from thumbcache import INDEX_WORKERS, THUMB_EDGE, build_cache

CACHE_ROOT = REPO / "cache" / "raw-bench"
BUDGET_PHOTOS_PER_S = 30.0  # spec §7: initial index, including content hashing

# ---------------------------------------------------------------------------
# DNG fixture generator -- ported from test_tracer.py's _make_dng (see the
# module docstring for exactly what changed and why: resolution + per-file
# content only; the TIFF/DNG tag layout is unchanged from that verified
# fixture, which rawpy/LibRaw round-trips (imread + postprocess +
# extract_thumb), so this benchmark's decode paths are the SAME code the
# tests exercise, just at throughput-realistic size).
# ---------------------------------------------------------------------------

_SHORT, _LONG, _BYTE, _ASCII, _SRAT = 3, 4, 1, 2, 10


def _dng_ifd(entries: list, ifd_off: int) -> bytes:
    """Serialize one TIFF IFD at ifd_off: sorted 12-byte entries, values
    <= 4 bytes inline, larger payloads appended after the table (word-
    aligned). entries: (tag, type, count, payload_bytes)."""
    entries = sorted(entries, key=lambda e: e[0])
    data_off = ifd_off + 2 + 12 * len(entries) + 4
    table = struct.pack("<H", len(entries))
    data = b""
    for tag, typ, count, payload in entries:
        if len(payload) <= 4:
            table += struct.pack("<HHI", tag, typ, count) \
                + payload.ljust(4, b"\0")
        else:
            if (data_off + len(data)) % 2:
                data += b"\0"
            table += struct.pack("<HHII", tag, typ, count,
                                 data_off + len(data))
            data += payload
    return table + struct.pack("<I", 0) + data


def _dng_ifd_size(entries: list) -> int:
    return 2 + 12 * len(entries) + 4 + sum(
        len(p) + (len(p) % 2) for _t, _y, _c, p in entries if len(p) > 4)


def _mosaic_strip(w: int, h: int, seed: int) -> bytes:
    """A seeded 16-bit RGGB CFA mosaic, w*h samples -- the raw pixel payload.
    Real noise (not the test fixture's fixed gradient) so per-file demosaic
    work is not degenerate."""
    rng = np.random.default_rng(seed)
    mosaic = rng.integers(0, 4096, size=(h, w), dtype=np.uint16)
    return mosaic.astype("<u2").tobytes()


def _preview_jpeg(w: int, h: int, seed: int, quality: int) -> bytes:
    """A seeded gradient+noise JPEG at (w, h) -- stands in for a camera's
    embedded full-size preview. Gradient gives it photo-like low-frequency
    structure; noise keeps entropy (and JPEG encode/decode cost) realistic
    and non-degenerate per file."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    denom_w = max(w - 1, 1)
    denom_h = max(h - 1, 1)
    grad = np.stack(
        [
            (xx * 255 // denom_w),
            (yy * 255 // denom_h),
            ((xx + yy) * 255 // max(denom_w + denom_h, 1)),
        ],
        axis=-1,
    ).astype(np.int16)
    noise = rng.integers(-24, 25, size=(h, w, 3), dtype=np.int16)
    arr = np.clip(grad + noise, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def make_dng(path: Path, w: int, h: int, seed: int,
            preview_jpeg: bytes | None, preview_size: tuple[int, int],
            orientation: int = 1) -> Path:
    """A synthetic DNG at (w, h) -- see the module docstring for provenance.
    With `preview_jpeg`, IFD0 is a JPEG-compressed preview (the layout real
    cameras use) and the CFA raw lives in a SubIFD; without, the raw IS
    IFD0 and the file carries no thumbnail at all (forces the demosaic
    fallback) -- identical structure to test_tracer._make_dng, just at
    realistic resolution with seeded-noise content."""
    strip = _mosaic_strip(w, h, seed)
    cam = b"Fauxcasa Synthetic\0"
    cm = b"".join(struct.pack("<ii", v, 10000) for v in
                  (10000, 0, 0, 0, 10000, 0, 0, 0, 10000))  # identity XYZ

    def E(tag, typ, fmt, *vals):
        return (tag, typ, len(vals) if len(vals) > 1 else 1,
                struct.pack(fmt, *vals))

    raw_entries = [
        E(254, _LONG, "<I", 0),            # NewSubfileType: the raw image
        E(256, _LONG, "<I", w), E(257, _LONG, "<I", h),
        E(258, _SHORT, "<H", 16),          # 16-bit samples
        E(259, _SHORT, "<H", 1),           # uncompressed
        E(262, _SHORT, "<H", 32803),       # PhotometricInterpretation: CFA
        E(277, _SHORT, "<H", 1),           # 1 sample/px
        E(278, _LONG, "<I", h),            # RowsPerStrip
        E(279, _LONG, "<I", len(strip)),   # StripByteCounts
        E(284, _SHORT, "<H", 1),
        E(33421, _SHORT, "<HH", 2, 2),     # CFARepeatPatternDim
        (33422, _BYTE, 4, bytes([0, 1, 1, 2])),  # CFAPattern: RGGB
        E(50714, _SHORT, "<H", 0),         # BlackLevel
        E(50717, _LONG, "<I", 4095),       # WhiteLevel
    ]
    shared = [
        (50706, _BYTE, 4, bytes([1, 4, 0, 0])),      # DNGVersion 1.4
        (50708, _ASCII, len(cam), cam),              # UniqueCameraModel
        (50721, _SRAT, 9, cm),                       # ColorMatrix1
        E(50778, _SHORT, "<H", 21),                  # CalibrationIlluminant1
        E(274, _SHORT, "<H", orientation),           # Orientation
    ]

    if preview_jpeg is None:
        ifd0 = raw_entries + shared + [E(273, _LONG, "<I", 0)]
        strip_off = 8 + _dng_ifd_size(ifd0)
        ifd0[-1] = E(273, _LONG, "<I", strip_off)    # StripOffsets -> raw
        out = struct.pack("<2sHI", b"II", 42, 8) + _dng_ifd(ifd0, 8)
        assert len(out) == strip_off
        out += strip
    else:
        pw, ph = preview_size
        ifd0 = [
            E(254, _LONG, "<I", 1),        # reduced-resolution preview
            E(256, _LONG, "<I", pw), E(257, _LONG, "<I", ph),
            (258, _SHORT, 3, struct.pack("<HHH", 8, 8, 8)),
            E(259, _SHORT, "<H", 7),       # JPEG-compressed strip
            E(262, _SHORT, "<H", 6),       # YCbCr
            E(277, _SHORT, "<H", 3),
            E(278, _LONG, "<I", ph),
            E(279, _LONG, "<I", len(preview_jpeg)),
            E(273, _LONG, "<I", 0),        # -> preview jpeg (patched below)
            E(330, _LONG, "<I", 0),        # SubIFDs -> raw (patched below)
        ] + shared
        raw_ifd = raw_entries + [E(273, _LONG, "<I", 0)]
        sub_off = 8 + _dng_ifd_size(ifd0)
        jpeg_off = sub_off + _dng_ifd_size(raw_ifd)
        strip_off = jpeg_off + len(preview_jpeg)
        ifd0 = [E(273, _LONG, "<I", jpeg_off) if e[0] == 273
                else E(330, _LONG, "<I", sub_off) if e[0] == 330
                else e for e in ifd0]
        raw_ifd[-1] = E(273, _LONG, "<I", strip_off)
        out = struct.pack("<2sHI", b"II", 42, 8) + _dng_ifd(ifd0, 8)
        assert len(out) == sub_off
        out += _dng_ifd(raw_ifd, sub_off)
        assert len(out) == jpeg_off
        out += preview_jpeg + strip
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out)
    return path


def generate_corpus(root: Path, n: int, w: int, h: int, with_preview: bool,
                    seed0: int, quality: int) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    prefix = "p" if with_preview else "n"
    paths = []
    for i in range(n):
        seed = seed0 + i
        preview_jpeg = _preview_jpeg(w, h, seed, quality) if with_preview \
            else None
        preview_size = (w, h) if with_preview else (0, 0)
        p = root / f"{prefix}{i:04d}.dng"
        make_dng(p, w, h, seed, preview_jpeg, preview_size)
        paths.append(p)
    return paths


def make_catalog(root: Path, files: list[Path]) -> Catalog:
    photos = [Photo(rel=f.name, folder="", name=f.name) for f in files]
    return Catalog(root=root, photos=photos, folders={}, albums={})


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def assert_quiet_box(force: bool) -> None:
    """Refuse (or warn, with --force) if another python-ish process is
    running -- CPU contention from an unrelated test run would silently
    invalidate the photos/s numbers below."""
    me = psutil.Process()
    mine = {me.pid}
    try:
        mine.add(me.ppid())
    except (psutil.Error, OSError):
        pass
    offenders = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        if p.pid in mine:
            continue
        name = (p.info.get("name") or "").lower()
        if "python" in name or "pytest" in name:
            cmd = " ".join(p.info.get("cmdline") or [])
            offenders.append(f"pid={p.pid} name={p.info.get('name')} "
                             f"cmd={cmd[:160]}")
    if not offenders:
        return
    msg = ("other python-ish process(es) detected -- the box is not quiet, "
          "so throughput numbers below would be contended:\n  " +
          "\n  ".join(offenders))
    if force:
        print(f"WARNING: {msg}\n(continuing: --force)", file=sys.stderr)
    else:
        sys.exit(f"refusing to benchmark: {msg}\n"
                 f"(close them, or pass --force to proceed anyway)")


def cpu_info() -> dict:
    return {
        "processor": platform.processor() or platform.machine(),
        "logical_cpus": os.cpu_count(),
        "physical_cpus": psutil.cpu_count(logical=False),
        "system": platform.platform(),
    }


def bench_build_cache(root: Path, files: list[Path],
                      runs: int) -> tuple[list[float], list[float]]:
    """Times the REAL indexer path: thumbcache.build_cache over a Catalog
    pointed at `root`/`files`, at the app's actual default levels (main.py
    calls build_cache with no `levels` arg -- the single 256 px v1 cache;
    RECOMMENDED_LEVELS is for a not-yet-wired hi-DPI consumer, so it is NOT
    what §7 throughput means today). This is read + hash (sha256) + in-file
    metadata + RAW-route decode + JPEG re-encode, exactly _index_one's
    per-photo work, threaded across INDEX_WORKERS -- the whole of what
    "indexing" means in this codebase (thumbcache.py module doc)."""
    rates, elapsed = [], []
    for _ in range(runs):
        cat = make_catalog(root, files)
        with tempfile.TemporaryDirectory(
                prefix="fauxcasa-raw-bench-out-") as td:
            result = build_cache(cat, Path(td))
        rates.append(result.rate)
        elapsed.append(result.elapsed_s)
    return rates, elapsed


def bench_primitives(preview_files: list[Path],
                     no_preview_files: list[Path]) -> tuple[list[float],
                                                            list[float]]:
    """Single-threaded, per-file timing of the two rawload primitives in
    isolation, to attribute where build_cache's time goes:

      - preview path: raw_preview_jpeg (LibRaw extract_thumb) + the same
        scaled-JPEG QImageReader decode _index_one rides the result
        through (setScaledSize to the 256 px top level) -- "extraction +
        JPEG decode".
      - demosaic path: raw_demosaic_qimage(half_size=True) alone -- a real
        LibRaw postprocess() IS the decode step for this path (no separate
        JPEG involved); _index_one's post-decode downscale/JPEG-encode is
        shared machinery common to every path (RAW or not), so it is
        deliberately excluded here to isolate the RAW-specific cost.

    Returns (preview_ms_per_file, demosaic_ms_per_file)."""
    from PySide6.QtCore import QBuffer, QIODevice, QSize
    from PySide6.QtGui import QImageReader

    top = THUMB_EDGE
    preview_ms = []
    for f in preview_files:
        data = f.read_bytes()
        t0 = time.perf_counter()
        jpeg = raw_preview_jpeg(data)
        if jpeg is None:
            sys.exit(f"{f}: expected an embedded preview, got none "
                     f"(fixture bug)")
        buf = QBuffer()
        buf.setData(jpeg)
        buf.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buf)
        reader.setAutoTransform(True)
        sz = reader.size()
        if sz.isValid() and (sz.width() > top or sz.height() > top):
            s = min(top / sz.width(), top / sz.height())
            reader.setScaledSize(QSize(max(1, round(sz.width() * s)),
                                       max(1, round(sz.height() * s))))
        img = reader.read()
        t1 = time.perf_counter()
        if img.isNull():
            sys.exit(f"{f}: preview JPEG failed to decode (fixture bug)")
        preview_ms.append((t1 - t0) * 1000.0)

    demosaic_ms = []
    for f in no_preview_files:
        data = f.read_bytes()
        t0 = time.perf_counter()
        img = raw_demosaic_qimage(data, half_size=True)
        t1 = time.perf_counter()
        if img.isNull():
            sys.exit(f"{f}: demosaic failed (fixture bug)")
        demosaic_ms.append((t1 - t0) * 1000.0)

    return preview_ms, demosaic_ms


def _stats_line(label: str, rates: list[float]) -> str:
    med, lo, hi = statistics.median(rates), min(rates), max(rates)
    verdict = "PASS" if med >= BUDGET_PHOTOS_PER_S else "FAIL"
    return (f"{label:14s} median={med:7.1f} photos/s  "
           f"min={lo:7.1f}  max={hi:7.1f}   "
           f"vs >= {BUDGET_PHOTOS_PER_S:.0f}/s -> {verdict}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--n", type=int, default=60,
                    help="files per path-variant (default: 60 preview + "
                        "60 no-preview)")
    ap.add_argument("--width", type=int, default=4000)
    ap.add_argument("--height", type=int, default=3000)
    ap.add_argument("--runs", type=int, default=3,
                    help="build_cache repetitions per variant")
    ap.add_argument("--quality", type=int, default=85,
                    help="preview JPEG quality")
    ap.add_argument("--keep", action="store_true",
                    help="keep the generated corpus (cache/raw-bench/...)")
    ap.add_argument("--force", action="store_true",
                    help="proceed even if other python-ish processes are "
                        "running (see assert_quiet_box)")
    ap.add_argument("--json", type=Path, default=None,
                    help="also write machine-readable results to this path")
    args = ap.parse_args(argv)

    assert_quiet_box(args.force)
    info = cpu_info()
    print(f"CPU: {info['processor']!r}  "
         f"logical={info['logical_cpus']} physical={info['physical_cpus']}")
    print(f"platform: {info['system']}")
    print(f"thumbcache.INDEX_WORKERS = {INDEX_WORKERS}  "
         f"(ThreadPoolExecutor width build_cache actually uses)")
    mp = args.width * args.height / 1_000_000
    print(f"corpus: {args.n} preview-DNG + {args.n} no-preview-DNG "
         f"@ {args.width}x{args.height} ({mp:.1f} MP), {args.runs} runs each")

    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    corpus_root = Path(tempfile.mkdtemp(prefix="corpus-", dir=str(CACHE_ROOT)))
    results: dict = {"cpu": info, "index_workers": INDEX_WORKERS,
                     "n": args.n, "width": args.width, "height": args.height,
                     "runs": args.runs}
    try:
        t0 = time.perf_counter()
        preview_root = corpus_root / "preview"
        nopreview_root = corpus_root / "nopreview"
        preview_files = generate_corpus(preview_root, args.n, args.width,
                                        args.height, True, 1_000_000,
                                        args.quality)
        nopreview_files = generate_corpus(nopreview_root, args.n, args.width,
                                          args.height, False, 2_000_000,
                                          args.quality)
        gen_s = time.perf_counter() - t0
        total_bytes = sum(f.stat().st_size for f in preview_files) + \
            sum(f.stat().st_size for f in nopreview_files)
        print(f"corpus generated in {gen_s:.1f}s "
             f"({total_bytes / 1e9:.2f} GB on disk)")

        print("\n== build_cache (the real indexer) ==")
        for label, root, files in (
            ("embedded-preview", preview_root, preview_files),
            ("demosaic-fallback", nopreview_root, nopreview_files),
        ):
            rates, elapsed = bench_build_cache(root, files, args.runs)
            for i, (r, e) in enumerate(zip(rates, elapsed)):
                print(f"  [{label}] run {i + 1}/{args.runs}: "
                     f"{len(files)} photos in {e:.3f}s = {r:.1f} photos/s")
            results[label] = {"rates": rates, "elapsed_s": elapsed}

        print("\n== isolated rawload primitives (single-threaded) ==")
        preview_ms, demosaic_ms = bench_primitives(preview_files,
                                                    nopreview_files)
        results["preview_primitive_ms"] = preview_ms
        results["demosaic_primitive_ms"] = demosaic_ms
        pmed, dmed = statistics.median(preview_ms), \
            statistics.median(demosaic_ms)
        print(f"  raw_preview_jpeg + scaled decode:  "
             f"median {pmed:7.2f} ms/photo  "
             f"(min {min(preview_ms):.2f}  max {max(preview_ms):.2f})")
        print(f"  raw_demosaic_qimage(half_size):    "
             f"median {dmed:7.2f} ms/photo  "
             f"(min {min(demosaic_ms):.2f}  max {max(demosaic_ms):.2f})")

        print("\n== verdict vs spec §7 (>= 30 photos/s, incl. hashing) ==")
        print(_stats_line("embedded-preview", results["embedded-preview"]
                          ["rates"]))
        print(_stats_line("demosaic-fallback", results["demosaic-fallback"]
                          ["rates"]))
        print("\nCAVEAT: this dev box exceeds the §7 reference hardware "
             "(8 threads/16GB) -- a pass here is necessary, not "
             "sufficient. See docs/research/raw-indexing-throughput.md.")

        if args.json:
            args.json.write_text(json.dumps(results, indent=1))
            print(f"\nwrote {args.json}")
    finally:
        if args.keep:
            print(f"\nkept corpus at {corpus_root}")
        else:
            shutil.rmtree(corpus_root, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
