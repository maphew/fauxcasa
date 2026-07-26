#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6", "rawpy", "exiv2", "pillow", "av"]
# ///
"""Reproducible §7 two-root / 100k warm-catalog process-start benchmark.

The canonical benchmark corpus already has two top-level volume slices
(`volA`, `volB`). Preparation treats those directories as independent roots,
scans one explicit catalog, and splits the existing packed fcache by prefix.
Blobs are copied, never decoded; the resulting two files retain exact pixels
and independent per-root positional parity.

Run from a checkout whose machine-local ``cache/`` contains the canonical
benchmark artifacts::

    uv run scripts/bench-multiroot-startup.py --prepare --runs 5

The measured path is a new offscreen process opening an already-indexed
explicit library through main.py, binding both root caches, constructing the
rooted sidebar/grid, and decoding the first viewport. Preparation is outside
the timed samples. Exit 1 means the median misses spec §7's <2,000 ms budget.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "apps" / "desktop-python"
sys.path.insert(0, str(APP))

import library  # noqa: E402
import main as tracer  # noqa: E402
import thumbcache  # noqa: E402
from catalog import save_catalog  # noqa: E402

BENCH_LIBRARY_ID = "00000000-0000-4000-8000-000000000002"
ROOTS = (("a1a1a1a1", "volA", "Volume A"),
         ("b2b2b2b2", "volB", "Volume B"))
BUDGET_MS = 2_000


def _write_subset(source: thumbcache.ThumbCache, indices: list[int],
                  rels: list[str], out: Path, library_id: str,
                  root_id: str) -> None:
    """Stream a photo subset into a valid fcache without decoding blobs."""
    levels = source.levels
    nlevels = len(levels)
    count = len(indices)
    version = 1 if nlevels == 1 else 2
    header_len = 16 if version == 1 else 16 + 2 * nlevels
    next_offset = header_len + count * nlevels * 16
    records = []
    for src_i in indices:
        for li in range(nlevels):
            _old_off, length, width, height = source.entry(src_i, li)
            records.append((next_offset, length, width, height))
            next_offset += length

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with source.path.open("rb") as src, tmp.open("wb") as dst:
        if version == 1:
            dst.write(thumbcache.MAGIC + struct.pack("<III", 1, count, 0))
        else:
            dst.write(thumbcache.MAGIC
                      + struct.pack("<IIHH", 2, count, nlevels, 0))
            dst.write(struct.pack(f"<{nlevels}H", *levels))
        for record in records:
            dst.write(struct.pack("<QIHH", *record))
        for src_i in indices:
            for li in range(nlevels):
                old_off, length, _width, _height = source.entry(src_i, li)
                if length:
                    src.seek(old_off)
                    blob = src.read(length)
                    if len(blob) != length:
                        raise RuntimeError(
                            f"truncated source blob {src_i}/{li}")
                    dst.write(blob)
    os.replace(tmp, out)
    out.with_suffix(".fcache.json").write_text(json.dumps({
        "sidecar_version": 2,
        "count": count,
        "library_id": library_id,
        "root_id": root_id,
        "thumb_edge": levels[source.primary],
        "files": rels,
        **({"levels": levels} if nlevels > 1 else {}),
    }, indent=1))


def prepare(source_library: Path, source_cache: Path,
            fixture_home: Path, cache_root: Path) -> tuple[Path, int]:
    roots = [library.LibraryRoot(id=root_id,
                                 path=(source_library / dirname).resolve(),
                                 label=label)
             for root_id, dirname, label in ROOTS]
    for root in roots:
        if not root.path.is_dir():
            raise SystemExit(f"missing benchmark root: {root.path}")
    fixture_home.mkdir(parents=True, exist_ok=True)
    cfg = library.LibraryConfig(library_id=BENCH_LIBRARY_ID,
                                name="Two-root benchmark", roots=roots,
                                home=fixture_home.resolve())
    library.save_library(cfg)

    cache_dir = thumbcache.cache_dir_for(cfg.library_id, cache_root)
    source = thumbcache.load_cache(source_cache)
    by_root: dict[str, tuple[list[int], list[str]]] = {
        root_id: ([], []) for root_id, _dirname, _label in ROOTS}
    prefix_to_id = {dirname: root_id for root_id, dirname, _label in ROOTS}
    for i, rel in enumerate(source.files):
        prefix, sep, child = rel.partition("/")
        if not sep or prefix not in prefix_to_id:
            raise SystemExit(f"unexpected benchmark rel outside volA/volB: {rel}")
        indices, rels = by_root[prefix_to_id[prefix]]
        indices.append(i)
        rels.append(child)

    catalog = tracer._scan_library_config(
        cfg, None, {}, None, None, None)
    if len(catalog.photos) != source.count:
        raise SystemExit(
            f"source mismatch: scanned {len(catalog.photos):,} photos, "
            f"cache has {source.count:,}")
    save_catalog(catalog, cache_dir / "catalog.json")
    for root_id, _dirname, _label in ROOTS:
        indices, rels = by_root[root_id]
        out = cache_dir / thumbcache.fcache_name(root_id)
        _write_subset(source, indices, rels, out, cfg.library_id, root_id)
        loaded = thumbcache.load_cache(out)
        thumbcache.bind(loaded, catalog, root_id=root_id)
    return cache_dir, len(catalog.photos)


def run_once(home: Path, cache_root: Path, timeout: int) -> dict:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run(
        [sys.executable, str(APP / "main.py"), str(home),
         "--cache-root", str(cache_root), "--quit-after-ready",
         "--timeout", str(timeout)],
        cwd=REPO, env=env, capture_output=True, text=True,
        timeout=timeout + 30)
    events = []
    for line in proc.stdout.splitlines():
        if line.startswith("{"):
            events.append(json.loads(line))
    ready = next((e for e in events if e.get("event") == "ready"), None)
    if proc.returncode or ready is None:
        raise RuntimeError(
            f"tracer failed rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    if not ready.get("warm"):
        raise RuntimeError("benchmark was not a warm catalog/cache open")
    return ready


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source-library", type=Path,
                    default=REPO / "cache" / "benchmark-library")
    ap.add_argument("--source-cache", type=Path,
                    default=REPO / "cache" / "benchmark-thumbs-v1.fcache")
    ap.add_argument("--fixture-home", type=Path,
                    default=REPO / "cache" / "multiroot-benchmark-home")
    ap.add_argument("--cache-root", type=Path,
                    default=REPO / "cache" / "multiroot-benchmark-cache")
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--json-out", type=Path)
    args = ap.parse_args(argv)
    if args.runs < 1:
        ap.error("--runs must be >= 1")

    cache_dir = thumbcache.cache_dir_for(BENCH_LIBRARY_ID, args.cache_root)
    cat_path = cache_dir / "catalog.json"
    if args.prepare or not cat_path.is_file():
        t0 = time.perf_counter()
        cache_dir, photos = prepare(
            args.source_library, args.source_cache,
            args.fixture_home, args.cache_root)
        print(f"prepared {photos:,} photos in {time.perf_counter() - t0:.1f}s "
              f"at {cache_dir}")

    samples = []
    for i in range(args.runs):
        event = run_once(args.fixture_home, args.cache_root, args.timeout)
        samples.append(event)
        print(f"run {i + 1}: {event['cold_start_ms']} ms, "
              f"prep {event['prep_ms']} ms, RSS {event['vm_rss_mb']} MB")
    values = [e["cold_start_ms"] for e in samples]
    median_ms = statistics.median(values)
    result = {
        "benchmark": "two-root-cold-start",
        "photos": samples[0]["photos"],
        "roots": 2,
        "runs_ms": values,
        "median_ms": median_ms,
        "prep_runs_ms": [e["prep_ms"] for e in samples],
        "vm_rss_runs_mb": [e["vm_rss_mb"] for e in samples],
        "budget_ms_exclusive": BUDGET_MS,
        "pass": median_ms < BUDGET_MS,
        "platform": sys.platform,
        "python": sys.version.split()[0],
    }
    print(json.dumps(result, indent=1))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        with args.json_out.open("w", newline="\n") as f:
            f.write(json.dumps(result, indent=1) + "\n")
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
