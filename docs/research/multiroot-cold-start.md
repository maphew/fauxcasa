# Two-root cold-start validation

Bead `fauxcasa-ed5.7.7` adds the first real two-root startup path: an
explicit library catalog is loaded once, each manifest root's independent
fcache is bound to that root's contiguous catalog slice, and a composite UI
cache maps global photo indices back to root-local entries. The rooted folder
tree and first visible thumbnails are constructed before `READY`.

## Result

The official five-run sequence on 2026-07-21 passed spec §7's strict
`<2,000 ms` cold-start budget:

| Run | READY | data prep | RSS |
|---:|---:|---:|---:|
| 1 | 777 ms | 377 ms | 223.2 MB |
| 2 | 781 ms | 381 ms | 222.7 MB |
| 3 | 766 ms | 381 ms | 222.6 MB |
| 4 | 784 ms | 387 ms | 222.9 MB |
| 5 | 773 ms | 382 ms | 222.9 MB |
| **median** | **777 ms** | **381 ms** | **222.9 MB** |

`READY` is measured from process/module start through catalog load, both
per-root cache loads and binds, sidebar/grid construction, window show, and
decode of every visible tile. Each sample is a fresh process opening the
already-indexed 100,000-photo library; benchmark fixture/cache preparation is
outside the timed samples. The filesystem page cache was not flushed between
runs, matching the project's established "already-indexed library to
interactive grid" startup measurement rather than claiming storage-cold I/O.

Machine-readable results are in
[`multiroot-startup-results.json`](multiroot-startup-results.json).

## Reproduction

The canonical synthetic corpus already separates its 100,000 entries beneath
`volA/` and `volB/`. The harness treats those as roots, scans an explicit
manifest, and stream-splits the existing v1 packed cache by prefix. It copies
the exact cached JPEG blobs without re-decoding; the two output caches sum to
the source cache's payload and each is verified with the production per-root
`bind()` before measurement.

From this Windows worktree, the exact official command was:

```powershell
uv run scripts/bench-multiroot-startup.py `
  --source-library A:\dev\fauxcasa\cache\benchmark-library `
  --source-cache A:\dev\fauxcasa\cache\benchmark-thumbs-v1.fcache `
  --fixture-home A:\dev\fauxcasa\cache\multiroot-benchmark-home `
  --cache-root A:\dev\fauxcasa\cache\multiroot-benchmark-cache `
  --runs 5 --timeout 180 `
  --json-out docs\research\multiroot-startup-results.json
```

Use `--prepare` on the first run or whenever the source benchmark cache
changes. Preparation is idempotent and never changes the source library or
source cache.

## Environment and caveat

- Windows 11 Pro 10.0.26200 (build 26200), native NTFS paths
- Python 3.13.1, PySide6 offscreen platform
- Intel Core i5-13600KF, 14 cores / 20 logical processors
- 63.9 GB RAM
- canonical 100,000-photo synthetic library and 376.5 MB v1 source fcache

This machine materially exceeds §7's 8-thread / 16 GB reference hardware.
The result is strong regression evidence for the two-root implementation but
is **necessary, not sufficient** for the reference-class release gate. An
official run on reference-class hardware remains required before release.
