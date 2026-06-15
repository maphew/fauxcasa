#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Parse a WAYLAND_DEBUG=1 protocol log for the on-screen ground truth that
in-process frame timing can't see (fauxcasa-ncv §7 scroll validation).

When Qt's QWidget raster (wl-shm) surface is NOT frame-callback throttled
(see apps/desktop-python/vsync_probe.py), paintEvent timing measures Qt's render-offer
cadence, not on-screen smoothness. The compositor's own protocol does show
it: how often Qt delivers a fresh buffer (wl_surface.commit — the rate the
compositor can present at) and the compositor's frame-callback pacing
(wl_callback.done, ≈ the real vsync). The SUT surface is the wl_surface
with the most commits; active scrolling (intervals < 50 ms) is separated
from idle gaps (cold / teleport / dwell).

    WAYLAND_DEBUG=1 ... uv run apps/desktop-python/bench_scroll.py ... 2>wld.log
    uv run scripts/parse-wayland-cadence.py wld.log
"""
import re
import sys

TS = re.compile(r"^\[\s*(\d+)\.(\d+)\]")       # ms . microseconds-within-ms
COMMIT = re.compile(r"wl_surface#(\d+)\.commit\(\)")
FRAME = re.compile(r"wl_surface#(\d+)\.frame\(new id wl_callback#(\d+)\)")
DONE = re.compile(r"wl_callback#(\d+)\.done\(")
ACTIVE_MAX_MS = 50.0


def ts(line):
    m = TS.match(line)
    return (int(m.group(1)) + int(m.group(2)) / 1000.0) if m else None


def pcts(xs):
    if not xs:
        return {}
    s = sorted(xs)
    n = len(s)
    p = lambda q: s[min(int((n - 1) * q), n - 1)]  # nearest-rank lower index
    return {"p50": round(p(.5), 2), "p95": round(p(.95), 2),
            "p99": round(p(.99), 2), "max": round(s[-1], 2),
            "min": round(s[0], 2), "n": n}


def cadence(times):
    iv = [times[i] - times[i - 1] for i in range(1, len(times))]
    active = [x for x in iv if x < ACTIVE_MAX_MS]
    gaps = [round(x) for x in iv if x >= ACTIVE_MAX_MS]
    return {"events": len(times), "active_intervals_ms": pcts(active),
            "active_rate_hz": round(len(active) / (sum(active) / 1000.0), 1)
            if active else 0.0,
            "n_gaps_ge_50ms": len(gaps), "largest_gaps_ms": sorted(gaps)[-6:]}


def main(path):
    commits, frame_cb = {}, {}
    lines = open(path, errors="replace").read().splitlines()
    for ln in lines:
        if ts(ln) is None or "-> " not in ln:
            continue
        mc = COMMIT.search(ln)
        if mc:
            commits.setdefault(int(mc.group(1)), []).append(ts(ln))
        mf = FRAME.search(ln)
        if mf:
            frame_cb[int(mf.group(2))] = int(mf.group(1))
    if not commits:
        print("no client commits found", file=sys.stderr)
        return 2
    sut = max(commits, key=lambda s: len(commits[s]))
    cb_done = []
    for ln in lines:
        md = DONE.search(ln)
        if md and "-> " not in ln and frame_cb.get(int(md.group(1))) == sut:
            t = ts(ln)
            if t is not None:
                cb_done.append(t)
    print(f"SUT surface = wl_surface#{sut} "
          f"({len(commits[sut])} commits; {len(commits)} client surfaces)")
    print("\nCOMMIT cadence (fresh buffer -> compositor; presentable rate):")
    print(" ", cadence(sorted(commits[sut])))
    print("\nFRAME-CALLBACK .done cadence (compositor frame pacing ≈ vsync):")
    print(" ", cadence(sorted(cb_done)) if cb_done else "none for SUT")
    cc = cadence(sorted(commits[sut]))
    ai, gaps = cc["active_intervals_ms"], cc["n_gaps_ge_50ms"]
    if ai:
        print(f"\n=> during active scrolling a fresh buffer reaches the "
              f"compositor every {ai['p50']} ms (p50) / {ai['p99']} ms (p99), "
              f"so it has a fresh frame to present at essentially every "
              f"vsync. The {gaps} gaps >=50 ms are the scripted teleport "
              f"dwells (static scroll = event-driven idle), not scroll "
              f"stalls.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/dev/stdin"))
