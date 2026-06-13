# Tracer bullet app (fauxcasa-pzx)

A thin but **real** end-to-end slice of the product on the proposed
Python + Qt (PySide6) stack — the next step after the
[stack balloons](../balloons/README.md) proved the grid budgets. The
balloon was a benchmark; this is product architecture: every layer the
M1 milestone needs exists here in miniature, wired together.

**Status: experiment.** The stack decision (fauxcasa-6hf) is owner-
confirmed for trial; this code is evidence, not yet the application.
The app name is provisional — `APP_NAME` in `main.py` is the single
source of truth.

## What's wired end to end

| Layer | File | Notes |
|---|---|---|
| Library scan in place | `catalog.py` | walk rule byte-identical to `make-thumbcache.py`, so catalog order == cache order |
| Picasa metadata | `catalog.py` → `scripts/picasa_db.py` | stars, captions, keywords, rotate, hidden, albums, folder names/descriptions from `.picasa.ini` |
| Thumbnail cache | `thumbcache.py` | reads/builds packed fcache; machine-local under `cache/tracer-cache/` (N1/N3); identity = sha256 + relpath, staleness = size+mtime cheap signals (N6). Parallel indexer (threads + **scaled decode**) — see below |
| Persistent catalog | `catalog.py` | full catalog (metadata + structure + signals) serialized to JSON; a **warm start loads it and skips the walk** (§7 cold start). Background reconcile diffs cheap signals and rebuilds + atomically swaps on drift |
| Virtualized grid | `grid.py` | balloon lineage: threaded fcache decode, bounded LRU — plus event-driven repaint, real scrollbar, group headers w/ pinned current folder, selection, star badges, error tiles. Never reads originals (N4) |
| Sidebar | `main.py` | All / Starred / folder tree (filesystem truth) / albums (pure references resolved from `albums=` tokens) |
| Search | `main.py` | substring over filename + caption + keywords, live filter |
| Viewer | `viewer.py` | double-click / Enter; async original load (the explicit N4 exception); ←/→ or J/K; Esc back |
| Instrumentation | `main.py` | `READY` line + JSON: cold-start ms, prep ms, `warm`, RSS, and an `indexed` event with photos/s — the §7 numbers |

## The §7 numbers (dev machine, offscreen; overshoots reference HW)

- **Cold start, already-indexed (warm load, no walk):** 100k photos in
  **467 ms** (`prep` 221 ms to load the 5 MB catalog), vs 1893 ms when a
  cold walk is needed. Budget < 2 s. ✅
- **Initial index, including content hashing:** the load-bearing detail
  is **scaled decode** (`QImageReader.setScaledSize` → libjpeg DCT decode
  straight to thumb size). A naive full decode of a 24 MP original is the
  whole cost and holds the GIL (~14 photos/s, threads give no speedup);
  scaled decode is ~30× cheaper and lets threads scale: **139 photos/s**
  in-app on realistic 3.3 MB / 8 MP photos (8 workers), vs the §7 ≥ 30/s
  budget. ✅ No multiprocessing or extra deps needed.

## Run it

```bash
# default: the synthetic fixture library (builds its own thumb cache
# on first run, tiles appear live while indexing)
uv run tracer/main.py

# the 100k benchmark library, adopting the pre-built cache
uv run tracer/main.py cache/benchmark-library --thumbs cache/benchmark-thumbs.fcache

# headless screenshot (agents / CI); --finish-build also persists the
# thumb cache before quitting, so the next run starts warm
QT_QPA_PLATFORM=offscreen uv run tracer/main.py --finish-build --screenshot /tmp/tracer.png
QT_QPA_PLATFORM=offscreen uv run tracer/main.py cache/benchmark-library \
    --thumbs cache/benchmark-thumbs.fcache --screenshot /tmp/t100k.png --scroll-to 0.5

# cold-start probe
QT_QPA_PLATFORM=offscreen uv run tracer/main.py --quit-after-ready
```

Tests: `uv run tracer/test_tracer.py`

## Deliberate tracer shortcuts (not product decisions)

- In-app cache builder holds all thumb blobs in memory while writing the
  fcache — fine for fixture/medium libraries; huge libraries adopt a
  pre-built fcache (`--thumbs`). Throughput itself is no longer the
  limit (see above).
- Single thumbnail resolution (256 px), re-decoded on zoom change; the
  spec calls for a multi-resolution cache.
- Persistent catalog is JSON (readable, language-neutral per N3), not the
  spec's compact ~50 B/photo binary catalog. Reconcile rebuilds the
  whole cache on drift rather than patching incrementally, and does not
  yet do N6 move-detection (the 2×2 hash-path matrix). Adopt-mode
  catalogs carry no per-file signals (so reconcile sees only adds/removes
  there).
- Search is a linear scan (fast enough ≤100k), not the per-photo word
  index the spec names.
- `hidden=yes` photos and stash folders (`.picasaoriginals/`, legacy
  `Originals/`) are excluded from every view (no reveal toggle yet);
  the folder-level "Hidden Folders" category is ignored. No faces, no
  edits, no writes.
- In-file IPTC/XMP is not read: real Picasa stores JPEG captions in
  IPTC, so those are invisible here (ini `caption=` covers the rest).
- EXIF orientation is not applied anywhere — uniformly stored-pixel
  rendering, consistent across grid/viewer/both cache builders.
- Scripted quits (`--screenshot`/`--quit-after-ready`) abandon an
  in-flight cache build cleanly; it completes on a later run. Pass
  `--finish-build` to hold the quit until the cache lands (raise
  `--timeout` for bigger libraries).
