# §7 RAW indexing throughput: embedded-preview vs demosaic-fallback (fauxcasa-ed5.13)

**Status:** measured, 2026-07-11. Both RAW decode paths in `rawload.py` were
timed through the real indexer (`thumbcache.build_cache`) against the §7
budget of **>= 30 photos/s, including content hashing**. Split from
fauxcasa-v46.5 item 5; the epic is fauxcasa-ed5.

## TL;DR

| path | build_cache median | min | max | per-photo decode primitive | verdict |
|---|---:|---:|---:|---:|---|
| embedded-preview | **120.6 photos/s** | 88.8 | 123.0 | 26.0 ms (extract + scaled JPEG decode) | **PASS**, ~4x margin |
| demosaic-fallback | **33.0 photos/s** | 32.2 | 33.3 | 87.2-88.1 ms (LibRaw `postprocess(half_size=True)`) | **PASS**, but only ~10% margin |

Both paths clear the >= 30 photos/s budget at 12 MP on this box, but the
margins are not remotely comparable. The embedded-preview path (the common
case — most real-world RAWs carry a JPEG preview) rides the same
DCT-scaled-decode machinery as a plain JPEG original and passes with room to
spare. The demosaic-fallback path (no usable preview) is a **real per-photo
LibRaw demosaic** that costs **~3.4x** the embedded-preview path's decode time
per photo (88 ms vs 26 ms), and the resulting build_cache throughput sits
right at the edge of the budget — a ~10% margin on hardware that itself
exceeds the §7 reference machine (see the caveat below). This is exactly the
"suspected slow path" the bead named, confirmed and quantified rather than
just suspected.

**The caveat that applies to every number below:** this dev box (14
physical / 20 logical Intel Core Ultra-class threads measured via
`platform.processor()` / `psutil`) **exceeds** the §7 reference hardware
(8 threads / 16 GB). Passing here is **necessary, not sufficient** — a
10%-margin pass on hardware substantially above the reference class is not
the same as a pass on the reference class itself, and the demosaic-fallback
number is the one to watch closest if reference-hardware measurement ever
becomes practical.

## Methodology

**Corpus.** Synthetic-only, per the project's privacy rule (real Picasa/RAW
data may never be committed or used as fixtures) — nothing here touches a
real photo. The generator (`scripts/bench-raw-indexing.py`) ports
`test_tracer.py`'s `_make_dng` (a hand-rolled, LibRaw-verified minimal DNG
1.4: TIFF container, RGGB CFA, the tags `rawpy`/LibRaw's `identify()`
requires) at throughput-realistic size and content:

- **Resolution: 4000x3000 (12 MP)**, a realistic consumer-camera RAW size —
  the test fixture's 32x24 px is fine for correctness but decode cost scales
  with sensor area, so it would have measured nothing. 12 MP generation was
  practical (see below); the 6 MP fallback named in the task spec was not
  needed.
- **Corpus size: 60 embedded-preview DNGs + 60 no-preview DNGs**, the
  suggested default — generation and the timed runs stayed fast enough
  (corpus generation ~39 s, each timed build_cache run 0.5-1.9 s) that no
  reduction was needed.
- **Content: seeded per file**, not the test fixture's one fixed gradient —
  each RAW's CFA mosaic is `numpy`-seeded noise (`Generator.integers(0, 4096,
  ...)`), and each embedded preview is a seeded gradient+noise JPEG
  (quality 85) at the same 4000x3000 size, so decode work is not degenerate
  or accidentally cacheable across files.
- **Fidelity caveats, stated honestly:** the CFA strip is uncompressed (real
  RAW formats are frequently lossless-compressed, which would add a
  decompression cost this benchmark does not exercise) and stored as a
  single strip; the color matrix is an identity placeholder; there is no
  real sensor noise/defect structure. LibRaw's demosaic (AHD by default)
  still runs its real interpolation over the full seeded mosaic, so the
  *demosaic* cost measured is real — only the *decompression* cost some real
  RAW containers would add on top is absent.

**What "indexing" means here, and what was timed.** Per `thumbcache.py`'s
module doc, "indexing" is `_index_one`'s full per-photo work — read bytes,
sha256 content hash, in-file metadata (caption/keywords/date/GPS/rating),
RAW-route decode, downscale, JPEG re-encode — run across
`thumbcache.INDEX_WORKERS` (8 on this box) threads by `build_cache`. This
benchmark calls `thumbcache.build_cache` directly (the same function
`main.py`'s cold-start path calls, with no `levels` override — the app's
actual default is the single 256 px v1 cache; `RECOMMENDED_LEVELS` is for a
not-yet-wired hi-DPI/loupe consumer and is not what "§7 throughput" means
today). `IndexResult.rate` — `photos / elapsed_s`, elapsed measured around
the `ThreadPoolExecutor` pass, excluding the final atomic `.fcache` file
write — is the same metric the app itself would report. **3 runs per path,
median + min/max reported** (also spot-checked with a second independent
3-run set — see Results).

**Isolated primitives.** To attribute where build_cache's time goes between
the two paths, `rawload.raw_preview_jpeg` (LibRaw `extract_thumb`) plus the
same scaled-JPEG `QImageReader` decode `_index_one` rides the preview bytes
through, and `rawload.raw_demosaic_qimage(half_size=True)` alone (a
half-size LibRaw `postprocess()` IS the decode step for that path — no
separate JPEG involved) were each timed single-threaded, one file at a time,
over the whole corpus. The shared downstream steps (final downscale to the
cache's target edge, JPEG re-encode) are common to every path — RAW or
not — so they are deliberately excluded from this isolation to show the
RAW-specific decode cost cleanly.

**Quiet-box check.** Before every timed run the script (`assert_quiet_box`)
enumerates running processes via `psutil` and refuses (or warns, with
`--force`) if another python-ish process is found, so a stray test run
never silently contends for CPU with the numbers below. Both production
runs reported here started with a clean check (no warning printed).

## Results

CPU (measured, `platform`/`psutil`): `Intel64 Family 6 Model 183 Stepping 1,
GenuineIntel`, 14 physical / 20 logical threads, Windows-11-10.0.26200-SP0.
`thumbcache.INDEX_WORKERS = 8` (the thread-pool width `build_cache` actually
uses, capped at 8 regardless of core count).

Corpus: 60 embedded-preview DNGs + 60 no-preview DNGs, 4000x3000 (12 MP)
each, generated in ~39 s (~3.1 GB on disk, not committed).

### build_cache (the real indexer), 3 runs each — first set

| path | run 1 | run 2 | run 3 | median | min | max |
|---|---:|---:|---:|---:|---:|---:|
| embedded-preview | 88.8/s | 117.6/s | 123.0/s | **117.6/s** | 88.8/s | 123.0/s |
| demosaic-fallback | 32.2/s | 33.0/s | 33.3/s | **33.0/s** | 32.2/s | 33.3/s |

### build_cache, second independent 3-run set (reproducibility spot-check)

| path | run 1 | run 2 | run 3 | median | min | max |
|---|---:|---:|---:|---:|---:|---:|
| embedded-preview | 96.1/s | 120.6/s | 121.8/s | **120.6/s** | 96.1/s | 121.8/s |
| demosaic-fallback | 33.0/s | 32.7/s | 33.2/s | **33.0/s** | 32.7/s | 33.3/s |

The demosaic-fallback rate is tight and reproducible run-to-run
(32.2-33.3/s across both sets); the embedded-preview rate has more spread
(88.8-123.0/s) — consistent with thread-pool warm-up/scheduling noise
mattering more when per-photo work is cheap (~26 ms) than when it is
expensive (~88 ms) and dominates the wall clock regardless.

### Isolated per-photo decode primitives (single-threaded, 60 files each)

| primitive | median | min | max |
|---|---:|---:|---:|
| `raw_preview_jpeg` extraction + scaled JPEG decode | **25.9-26.0 ms** | 25.5 ms | 34.4 ms |
| `raw_demosaic_qimage(half_size=True)` | **87.2-88.1 ms** | 80.2 ms | 102.9 ms |

The demosaic path costs **~3.4x** the preview path's per-photo decode time —
the direct, single-threaded explanation for why demosaic-fallback
build_cache throughput sits so much closer to the budget line even with the
same 8-way thread pool behind both.

## Verdict vs the §7 budget (>= 30 photos/s, including hashing)

- **Embedded-preview path: PASS**, ~4x margin (median 117.6-120.6 photos/s).
  This is the common real-world case (most RAWs carry a JPEG preview), and
  it behaves like any other JPEG-original indexing throughput — no RAW-
  specific risk here.
- **Demosaic-fallback path: PASS, but by only ~10%** (median 33.0 photos/s
  both runs, min 32.2/s). This is the "suspected slow path" the bead named,
  and the measurement confirms the suspicion was well-founded: it is a real
  bottleneck relative to the preview path (3.4x the per-photo cost) and its
  margin over budget is thin — on the actual §7 reference hardware (8
  threads / 16 GB, well below this box's 14p/20l threads), there is a real
  possibility this path would fail the budget outright. The mandatory
  caveat below applies most sharply here.

**Mandatory caveat:** this dev box exceeds the §7 reference hardware (8
threads / 16 GB) — passing here is **necessary, not sufficient** (the same
phrasing this repo's other §7 measurements use, e.g.
`docs/research/scroll-vsync-validation.md`). Given the demosaic path's thin
margin even on stronger-than-reference hardware, a reference-class
measurement of this specific path is the natural follow-up if/when that
hardware gate becomes practical (tracked alongside the other still-owed
reference-class rows).

## Reproduce

```
uv run scripts/bench-raw-indexing.py                 # default: 60+60 @ 4000x3000, 3 runs
uv run scripts/bench-raw-indexing.py --n 20 --keep    # smaller/faster, keep the corpus
uv run scripts/bench-raw-indexing.py --json out.json  # machine-readable results alongside stdout
```

The corpus is generated under `cache/raw-bench/` (gitignored; `--keep` skips
the cleanup so the DNGs can be inspected) and is never committed.
