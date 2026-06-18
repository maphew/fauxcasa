# Stack trial balloons (fauxcasa-6hf)

Four candidate stacks, one fixed task, identical rules. Per the spec (§10
item 12, owner procedure 2026-06-11): every balloon renders the 100k
synthetic library grid from the pre-built thumbnail cache; the first
candidate that passes the §7 budgets and iterates fast wins; ties break
toward iteration speed. These are throwaway prototypes — quality bar is
"honest benchmark", not "product code" — but they stay buildable in CI
(Linux + Windows) as the §10 "green builds from day one" evidence.

## Inputs

Built by `scripts/make-synthetic-library.py --benchmark 100000` and
`scripts/make-thumbcache.py` (machine-local, gitignored):

- `cache/benchmark-thumbs.fcache` — packed thumbnails. Header 16 bytes:
  magic `FCTC`, u32 version=1, u32 count, u32 reserved (little-endian).
  Then `count` 16-byte index records: u64 blob offset, u32 blob length,
  u16 width, u16 height. Then JPEG blobs (256 px long edge, q80). The
  shipped benchmark cache is v1; **check the version word before parsing** —
  v2 (multi-resolution) packs `u16 nlevels | u16 0` in the reserved word,
  followed by an `nlevels`-entry u16 level table and a photo-major
  `count*nlevels` index (see `scripts/make-thumbcache.py` for the layout).
- `cache/benchmark-thumbs.fcache.json` — count + folder groups (unused by
  balloons today; reserved for group headers later).

**Balloons may open ONLY the `.fcache` pair.** The grid never reads
originals (N4) — touching `cache/benchmark-library/` disqualifies the run.

## The fixed task

A window of **1280×800 logical pixels** showing a continuous virtualized
grid of all N thumbnails in cache order: square cells of **168 px**
(160 px tile + 8 px gutter), thumbs aspect-fit and centered,
`cols = floor(viewport_width / 168)`, light-gray placeholder for tiles
whose thumb isn't decoded yet. Asynchronous decode with prefetch is
expected (that *is* the architecture under test); the decoded-thumbnail
RAM cache must be **bounded** (suggested ≈ 512 MB; state the bound in
code) — unbounded caches turn the scroll test into a RAM purchase.

Scripted run, no user input:

1. **Cold start** — from process start to the first frame in which every
   visible tile is decoded. Report `cold_start_ms` (internal clock) and
   print `READY` on its own stdout line at that moment (the runner also
   measures wall time from spawn).
2. **Flick-scroll A** — from the top, scroll down at **2000 px/s**
   (2.5 screens/s; budget allows ≤ 3) for **45 s**, recording every
   frame-to-frame interval.
3. **Teleports** — jump instantly to 25%, 50%, 75%, 99% scroll positions;
   at each, record ms until the viewport is fully decoded (`fill_ms`),
   dwell 2 s after fill.
4. **Flick-scroll B** — from the 50% position, same speed, **45 s**.
5. Print one JSON line to stdout and exit 0.

Frame metrics aggregate phases 2+4 only. A **blank-tile frame** is a
rendered frame during 2+4 in which ≥1 visible tile is still a placeholder.

## Output contract

```json
{"candidate": "rust-egui", "photos": 100000,
 "cold_start_ms": 0, "frames": 0, "p50_ms": 0.0, "p99_ms": 0.0,
 "max_ms": 0.0, "blank_tile_frames": 0, "fill_ms": [0, 0, 0, 0],
 "vm_rss_mb": 0.0, "vm_hwm_mb": 0.0}
```

`vm_rss_mb`/`vm_hwm_mb` from `/proc/self/status` (Linux); 0 where
unavailable (the runner measures externally too).

CLI contract: `<balloon> <path-to-fcache> [--seconds S] [--speed PX_S]`
(defaults 45 / 2000). Smaller caches (e.g. a 2k-thumb smoke cache) must
work — never hard-code N.

## §7 budgets under test (reference: spec §7)

| Metric | Budget |
|---|---|
| cold_start_ms | < 2000 (target well under — this is cache-only, no indexing) |
| p99_ms | ≤ 32, and max_ms ≤ 100 |
| blank_tile_frames | 0 |
| resident memory | soul test: nowhere near 1.5 GB; working target < 600 MB |

Dev-machine numbers (16-thread Ryzen 7840HS) overshoot the §7 reference
hardware (8 threads, 16 GB); passing here is necessary, not sufficient —
the report must say so.

## Candidates

| dir | stack | notes |
|---|---|---|
| `rust-egui/` | Rust + eframe/egui | reference implementation |
| `go-gio/` | Go + gioui.org | |
| `py-qt/` | Python + PySide6 | Qt's C++ engine renders; Python feeds it |
| `web-wasm/` | TS/JS + canvas in a browser shell | the wasm-hybrid probe; decode via `createImageBitmap`, fcache over local HTTP |

Run everything: `uv run scripts/run-balloon-bench.py` (builds, runs each
balloon sequentially on an idle machine, collects JSON + external RSS,
writes `cache/balloon-results.json`).
