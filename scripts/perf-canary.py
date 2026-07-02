#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""§7 CI perf canaries (fauxcasa-ed5.3): cold-start assert, search-latency
probe, and catalog-size REPORT over the small deterministic synthetic
library, parsed from the tracer's machine-readable stdout protocol
(READY / {"event": "ready"|"search"} lines).

HONESTY CONTRACT — read before tightening a threshold:

- These are CANARIES, not the §7 budgets. The §7 rows (cold start < 2 s,
  search keystroke -> filtered grid < 50 ms) are defined at 100k photos on
  reference hardware; this job runs 24 photos on shared CI runners whose
  speed varies run to run and machine to machine. A generous scaled
  threshold here catches order-of-magnitude regressions — an accidental
  synchronous rescan on the warm path, per-keystroke disk IO, a quadratic
  filter scan — and that is ALL it certifies. The definitive 100k numbers
  are dev-box measurements, recorded in beads fauxcasa-ed5.4/.3 and the PR
  that added this file (2026-07-02: warm cold start ~0.57 s; search
  5.6-30 ms across 0..100k-hit queries).

- Catalog size: the §7 row says ~50 B/photo core catalog (oracle-measured)
  and there is a KNOWN TENSION, tracked as fauxcasa-1jb: the tracer's JSON
  catalog rows carry rel + sha256 (64 hex chars) once the indexer has run,
  which cannot meet ~50 B/photo. This job REPORTS bytes/photo so the number
  is visible in every CI run, and fails only past a generous ceiling that
  flags runaway growth (a new per-photo blob landing in the catalog), not
  the known tension. Do not "fix" a ceiling failure by raising the ceiling
  silently — file it against fauxcasa-1jb.

Flow (all subprocesses run offscreen, timeout-armed):
  1. generate the default 24-photo synthetic library if absent
     (deterministic; existing files are never rewritten),
  2. COLD run (--finish-build) to index + persist the tracer cache into a
     throwaway --cache-root,
  3. WARM run parsed for the ready JSON -> cold-start assert + catalog
     report,
  4. --search-probe run parsed for search events -> per-query assert.

Usage::

    uv run scripts/perf-canary.py            # CI entry point, exit 0 = green
    uv run scripts/perf-canary.py --keep     # keep the throwaway cache root

NOTE: Python here is gate tooling per the repo script conventions; it does
not decide the application implementation language.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAIN = REPO / "apps" / "desktop-python" / "main.py"
LIBRARY = REPO / "cache" / "synthetic-library"

# --- canary thresholds (see the honesty contract above) --------------------
# Warm start over 24 photos: dominated by interpreter + Qt import, ~1-3 s on
# shared runners; 10 s only fires on a ~5-10x pathology (§7 budget: 2 s at
# 100k on reference hardware — NOT asserted here).
COLD_CANARY_MS = 10_000
# Search over 24 photos is sub-millisecond; 500 ms only fires on a
# pathology (per-keystroke IO / quadratic scan). §7 budget: 50 ms at 100k.
SEARCH_CANARY_MS = 500
# ~50 B/photo is the §7 row; the sha256-bearing JSON rows measure ~150-250.
# The ceiling flags runaway growth only (fauxcasa-1jb owns the tension).
CATALOG_CEILING_B_PER_PHOTO = 2_048

PROBE = "beach,winter,-beach,garden photo,zzznope"


def run_tracer(extra: list[str], timeout_s: int = 300) -> list[dict]:
    """One offscreen tracer run; returns the parsed machine-event lines."""
    uv = shutil.which("uv")
    if uv is None:
        sys.exit("error: uv not on PATH (https://docs.astral.sh/uv/)")
    cmd = [uv, "run", str(MAIN), str(LIBRARY),
           "--timeout", str(timeout_s - 30), *extra]
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout_s, env=env, cwd=REPO)
    events = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        sys.exit(f"tracer run failed (rc={proc.returncode}): {' '.join(cmd)}")
    return events


def ensure_library() -> None:
    """Generate the default synthetic library only if absent — never touch
    an existing one (other local processes may be reading it; generation is
    deterministic and skips existing files anyway)."""
    if LIBRARY.is_dir() and any(LIBRARY.rglob("*.jpg")):
        return
    uv = shutil.which("uv")
    subprocess.run([uv, "run", str(REPO / "scripts" /
                                   "make-synthetic-library.py")],
                   check=True, timeout=300, cwd=REPO)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway cache root for inspection")
    args = ap.parse_args(argv)

    ensure_library()
    failures: list[str] = []
    cache_root = Path(tempfile.mkdtemp(prefix="fauxcasa-perf-canary-"))
    try:
        common = ["--cache-root", str(cache_root)]

        print("== pass 1: cold run (index + persist the tracer cache) ==")
        run_tracer([*common, "--quit-after-ready", "--finish-build"])

        print("== pass 2: warm run (cold-start canary + catalog report) ==")
        events = run_tracer([*common, "--quit-after-ready"])
        ready = next(e for e in events if e.get("event") == "ready")
        print(json.dumps(ready, indent=1))
        if not ready.get("warm"):
            failures.append(
                "pass 2 was not a warm start — pass 1 failed to persist the "
                "catalog/cache (the §7 cold-start row is 'already-indexed "
                "library -> interactive grid')")
        cold_ms = ready.get("cold_start_ms", 1 << 30)
        if cold_ms >= COLD_CANARY_MS:
            failures.append(
                f"cold start {cold_ms} ms >= {COLD_CANARY_MS} ms canary "
                f"(order-of-magnitude regression; §7 budget is 2 s at 100k "
                f"on reference hardware, measured on the dev box)")
        bpp = ready.get("catalog_bytes_per_photo", 0)
        print(f"catalog: {ready.get('catalog_bytes', 0)} bytes for "
              f"{ready.get('photos', 0)} photos = {bpp} B/photo "
              f"(§7 row ~50 B/photo; known tension fauxcasa-1jb — "
              f"REPORTED here, ceiling {CATALOG_CEILING_B_PER_PHOTO})")
        if not ready.get("catalog_bytes"):
            failures.append("catalog_bytes missing/0 in the warm ready JSON")
        if bpp > CATALOG_CEILING_B_PER_PHOTO:
            failures.append(
                f"catalog {bpp} B/photo > {CATALOG_CEILING_B_PER_PHOTO} "
                f"ceiling — runaway growth, file against fauxcasa-1jb")

        print("== pass 3: search-latency probe ==")
        events = run_tracer([*common, "--search-probe", PROBE])
        searches = [e for e in events if e.get("event") == "search"]
        n_queries = len([q for q in PROBE.split(",") if q.strip()])
        if len(searches) != n_queries:
            failures.append(f"expected {n_queries} search events, "
                            f"got {len(searches)}")
        for ev in searches:
            print(json.dumps(ev))
            if ev["ms"] >= SEARCH_CANARY_MS:
                failures.append(
                    f"search {ev['query']!r} took {ev['ms']} ms >= "
                    f"{SEARCH_CANARY_MS} ms canary (§7 budget is 50 ms at "
                    f"100k, measured on the dev box)")
    finally:
        if args.keep:
            print(f"kept cache root: {cache_root}")
        else:
            shutil.rmtree(cache_root, ignore_errors=True)

    if failures:
        print(f"\nFAIL: {len(failures)} canary problem(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nPASS: cold start {cold_ms} ms < {COLD_CANARY_MS} ms; "
          f"{len(searches)} probe queries < {SEARCH_CANARY_MS} ms; "
          f"catalog {bpp} B/photo reported "
          f"(ceiling {CATALOG_CEILING_B_PER_PHOTO})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
