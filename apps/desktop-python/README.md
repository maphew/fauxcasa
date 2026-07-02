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
| In-file metadata | `catalog.py` → `inmeta.py` (at index) | JPEG captions/keywords from XMP `dc:description`/`dc:subject` + IPTC 2:120/2:25 — in-file wins over the ini for tier-1 (§4). EXIF orientation baked at decode |
| Thumbnail cache | `thumbcache.py` | reads/builds packed fcache; machine-local under `cache/tracer-cache/` (N1/N3); identity = sha256 + relpath, staleness = size+mtime cheap signals (N6). Parallel indexer (threads + **scaled decode**) — see below |
| Persistent catalog | `catalog.py` | full catalog (metadata + structure + signals) serialized to JSON; a **warm start loads it and skips the walk** (§7 cold start). Background reconcile diffs cheap signals and rebuilds + atomically swaps on drift |
| Virtualized grid | `grid.py` | balloon lineage: threaded fcache decode, bounded LRU — plus event-driven repaint, real scrollbar, group headers w/ pinned current folder, selection, star badges, error tiles. Never reads originals (N4) |
| Sidebar | `main.py` | All / Starred / folder tree (filesystem truth) / albums (pure references resolved from `albums=` tokens) |
| Search | `main.py` | substring over filename + caption + keywords, live filter |
| Viewer | `viewer.py` | double-click / Enter; **instant cached preview** (the fcache v2 hi-DPI / loupe consumer — the nearest cached level ≥ the viewport's device pixels via `ThumbCache.best_level()`: 512 on a hi-DPI/large window, 256 from a v1 cache) painted while the async original loads (the explicit N4 exception); ←/→ or J/K; Esc back |
| Instrumentation | `main.py` | `READY` line + JSON: cold-start ms, prep ms, `warm`, RSS, and an `indexed` event with photos/s — the §7 numbers |

## Formats

Stills: JPEG, PNG, GIF, BMP, TIFF, WebP (Qt image plugins). **RAW**
(fauxcasa-v46.1): Picasa's documented 16-vendor extension list — DNG,
CRW/CR2, RAW, RAF, 3FR, DCR/KDC, MRW, NEF/NRW, ORF, RW2, PEF, X3F,
ARW/SRF/SR2 — decoded via `rawpy` (an *updatable* LibRaw wheel, never a
frozen table — §6 footgun 14) behind the `rawload.py` seam (the future
decode-sandbox boundary, `docs/decode-threat-model.md`). RAW files route
**by extension before any content sniff** (the TIFF-based containers fool
QImageReader/PIL into decoding the tiny embedded preview IFD); thumbs and
viewer prefer the embedded JPEG preview (`extract_thumb`, cheap and
usually full-size) and fall back to a real demosaic (`postprocess`;
half-size for thumbs). Orientation lands exactly once per path: the
preview's own EXIF tag via the normal JPEG auto-transform, or LibRaw's
flip baked during demosaic. Corrupt RAW → the standard error tile. RAW
originals are never written (§5). Both walkers' `EXTS` sets carry the
RAW list in lockstep (`catalog.py` / `scripts/make-thumbcache.py`) so
caches keep binding; a pre-RAW cache fails `bind()` on the count
mismatch and rebuilds via the existing path.

## The §7 numbers (Linux dev machine, offscreen; overshoots reference HW)

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
- **v2 multi-resolution cache (512/256/128):** the product cache is now
  multi-res. Storage is **~4× the v1 256-only cache** (100k benchmark:
  377 MB → 1.5 GB) — the 512 hi-DPI level dominates, the 128 level is
  cheap; this is *not* the "~30 %" a single small added level would cost.
  **Startup is unaffected** — cold/warm load are catalog-bound and the
  larger fcache's thumbnail blobs are seeked per-tile, not read at load
  (only the level table and the ~3× larger v2 offset index are read, which
  likely accounts for part of the 467 → 511 ms warm-load delta): re-measured
  100k warm load **511 ms** / cold-walk **2.1 s** (Windows dev box,
  offscreen), on par with the Linux/v1 reference above. The grid still renders the 256 level, so
  scroll perf is unchanged until a hi-DPI consumer (`fauxcasa-q7m`) reads
  the 512 level. RSS at ready for the 100k grid is **~199 MB (329 MB
  peak)** on the Windows box, via the cross-platform probe added in
  `fauxcasa-61e`. The **in-app** index rate above is unchanged — adopting
  a pre-built `--thumbs` cache skips in-app thumbnailing entirely — but the
  **offline cache build** does ~3× the per-photo work for v2 (decode to
  512, then JPEG-encode all three levels), so v2 build throughput is lower
  than v1; that offline rate was not separately benchmarked.
- **Grid hi-DPI consumer (`fauxcasa-q7m`), measured 2026-07-02** (Windows
  dev box, 4K@60 Hz, real display, `QT_SCALE_FACTOR` for exact dpr —
  same-box **relative** signal; the canonical Linux dpr-1 baselines are
  untouched by construction): at **default zoom** dpr 2 is
  indistinguishable from the dpr 1 control (p99 15.8 vs 18.3 ms, RSS peak
  631 vs 611 MB, fullscreen) — the hi-DPI sharpness is free where people
  scroll. At **min zoom** (64 px tiles, 1280×800 window) dpr 2 costs real
  money: p99 8.6 → 22.4 ms (still in budget), max 46 → 193 ms, 3 blank
  frames, RSS peak 555 → 764 MB — each tile paints 4× the device pixels
  from a 4× source. Kept the DPR-aware, z1e-preserving design (zoom never
  re-decodes); the min-zoom scaling issue is systemic (tile-count ×
  device-area, present at dpr 1 on large viewports too) and tracked as
  `fauxcasa-q6l.14`.

## Run it

```bash
# default: the synthetic fixture library (builds its own thumb cache
# on first run, tiles appear live while indexing)
uv run apps/desktop-python/main.py

# the 100k benchmark library, adopting the pre-built cache
uv run apps/desktop-python/main.py cache/benchmark-library --thumbs cache/benchmark-thumbs.fcache

# headless screenshot (agents / CI); --finish-build also persists the
# thumb cache before quitting, so the next run starts warm
QT_QPA_PLATFORM=offscreen uv run apps/desktop-python/main.py --finish-build --screenshot /tmp/tracer.png
QT_QPA_PLATFORM=offscreen uv run apps/desktop-python/main.py cache/benchmark-library \
    --thumbs cache/benchmark-thumbs.fcache --screenshot /tmp/t100k.png --scroll-to 0.5

# cold-start probe
QT_QPA_PLATFORM=offscreen uv run apps/desktop-python/main.py --quit-after-ready

# skip icon-sized files during catalog/index
uv run apps/desktop-python/main.py ~/Pictures --min-image-size 100x100

# skip extremely large originals/source rasters
uv run apps/desktop-python/main.py ~/Pictures --max-image-size 12000x12000
```

From a **frozen bundle** there is no bundled synthetic library, so a
no-arg launch opens the library you last picked (remembered in
`<cache-root>/config.json`) or, on first run, prompts for a folder; pass
a library path to override. A headless frozen launch with no library
exits cleanly rather than blocking on a dialog nobody can answer.

Tests: `uv run apps/desktop-python/test_tracer.py`

## EXIF orientation

Policy (decided here; matches Picasa and every modern viewer): **EXIF
orientation is applied at decode**, so photos display upright, and the
Picasa `rotate=` user quarter-turns compose *on top* of the corrected
image. The two transforms are independent — EXIF is the camera's stored
intent (`picasa-ini-format.md`: "EXIF orientation handling is the
consumer's job"), `rotate=` is a user gesture relative to stored pixels.

It is applied **consistently across every path**: the in-app indexer
(`thumbcache.py`, `QImageReader.setAutoTransform`) and the standalone
builder (`scripts/make-thumbcache.py`, PIL `ImageOps.exif_transpose`)
bake it into the thumbnail; the viewer auto-transforms the original on
read. All three use the library's own orientation logic, so all eight
orientations — mirrors included — are handled correctly without any
hand-rolled transform. The thumbnail is thus stored display-upright; only
the cheap `rotate=` turns stay a live display transform, so a rotate never
invalidates the cache. Synthetic/benchmark photos carry no Orientation
tag, so the shipped benchmark `.fcache` is pixel-identical under this
policy and stays valid.

## Deliberate tracer shortcuts (not product decisions)

- In-app cache builder holds all thumb blobs in memory while writing the
  fcache — fine for fixture/medium libraries; huge libraries adopt a
  pre-built fcache (`--thumbs`). Throughput itself is no longer the
  limit (see above).
- Grid renders a single thumbnail resolution (~256 px native): each tile
  is decoded **once** at that size and **scaled in paint** to the current
  tile size, so zoom is pure relayout + re-anchor and never re-decodes the
  JPEG (fauxcasa-z1e). The fcache format is now **dual-version** (fauxcasa-gtr):
  v1 (the default, and the shipped benchmark cache) is a single 256 level;
  v2 (`make-thumbcache.py --levels 512,256,128` or `--levels recommended`,
  `build_cache(levels=...)`) declares a level set in its header and stores a
  photo-major per-level index. One reader loads both. The grid still reads
  the **primary** (256) level, so a v2 cache leaves grid/zoom behaviour
  unchanged; **consuming** the larger levels for a hi-DPI display or a loupe
  larger than 256 (the viewer still reads originals, N4) is the remaining
  product work.
- Persistent catalog is JSON (readable, language-neutral per N3), not the
  spec's compact ~50 B/photo binary catalog. Reconcile rebuilds the
  whole cache on drift rather than patching incrementally, and does not
  yet do N6 move-detection (the 2×2 hash-path matrix). Adopt-mode
  catalogs carry no per-file signals (so reconcile sees only adds/removes
  there).
- Search is a linear scan (fast enough ≤100k), not the per-photo word
  index the spec names.
- `hidden=yes` photos and stash folders (`.picasaoriginals/`, legacy
  `Originals/`) are hidden by default; a **"Show hidden"** toggle reveals
  them (drawn veiled) across the All/Folders/Starred/Search views. Album
  membership stays visible-only, and the folder-level "Hidden Folders"
  category is still ignored — it needs an oracle fixture for its
  `category=` value. No faces, no edits, no writes.
- In-file metadata is read for JPEG captions/keywords only (XMP
  `dc:description`/`dc:subject`, IPTC 2:120/2:25 — `inmeta.py`), the
  tier-1 fields the grid/search/viewer surface; faces-in-XMP, geotags,
  and in-file dates are out of tracer scope (the product wraps a mature
  metadata library, spec §5 P1). The read piggybacks on the index (the
  bytes are already in hand for hashing), so a cold walk shows ini-only
  captions until the index fills the in-file ones; warm starts load the
  merged result from the persisted catalog. **Adopt mode (`--thumbs`)
  binds an external cache without indexing, so its catalog stays ini-only
  (no in-file ingest)** — the benchmark library it targets carries none
  anyway; a real library wanting in-file captions runs a normal build.
- Scripted quits (`--screenshot`/`--quit-after-ready`) abandon an
  in-flight cache build cleanly; it completes on a later run. Pass
  `--finish-build` to hold the quit until the cache lands (raise
  `--timeout` for bigger libraries).
