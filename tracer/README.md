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
| Thumbnail cache | `thumbcache.py` | reads/builds packed fcache; machine-local under `cache/tracer-cache/` (N1/N3); identity = sha256 + relpath, staleness = size+mtime cheap signals (N6) |
| Virtualized grid | `grid.py` | balloon lineage: threaded fcache decode, bounded LRU — plus event-driven repaint, real scrollbar, group headers w/ pinned current folder, selection, star badges, error tiles. Never reads originals (N4) |
| Sidebar | `main.py` | All / Starred / folder tree (filesystem truth) / albums (pure references resolved from `albums=` tokens) |
| Search | `main.py` | substring over filename + caption + keywords, live filter |
| Viewer | `viewer.py` | double-click / Enter; async original load (the explicit N4 exception); ←/→ or J/K; Esc back |
| Instrumentation | `main.py` | `READY` line + JSON: cold-start ms, scan ms, RSS — the §7 numbers |

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

- In-app cache builder is sequential and holds blobs in memory — fine
  for fixture libraries; the real indexer (≥30 photos/s incl. hashing,
  §7) is separate future work. Big libraries adopt a pre-built fcache.
- Single thumbnail resolution (256 px), re-decoded on zoom change; the
  spec calls for a multi-resolution cache.
- Catalog is in-RAM from a fresh walk each start; no persistent catalog
  DB yet. Adopted caches get no content hashes.
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
