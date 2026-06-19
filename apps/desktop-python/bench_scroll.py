#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6"]
# ///
"""Scroll benchmark for the tracer GridView on a REAL display (fauxcasa-ncv).

Retires the asterisk on the stack-balloon py-qt scroll numbers. Those ran
under a HEADLESS weston compositor (llvmpipe) where a 240 Hz QTimer drove
~250 unthrottled paints/s, so the table measured "how fast it can paint",
not "how smooth it looks at vsync". This harness runs the *product* grid
(apps/desktop-python/grid.py GridView, event-driven repaints) on the real desktop
compositor + GPU and reports the §7 scroll budget honestly.

WHY FRAME-PRODUCTION TIME, NOT PAINT INTERVAL (a measured fact about the
host — see scripts/vsync-probe and the §7-validation report): Qt's QWidget
raster (wl-shm) surface on this compositor is NOT frame-callback
throttled — it commits a fresh buffer per paint as fast as it is driven,
and the compositor presents the most recent buffer at each vsync. There is
no frameSwapped/presentation signal for a raster QWidget, so paintEvent
timing measures Qt's render-OFFER cadence, which is paced by our drive
timer + compute, NOT by the compositor. Therefore:
  * The honest, throttle-independent §7 metric is FRAME PRODUCTION TIME
    (paint_cost): wall time inside one paintEvent (decode-pump + layout +
    draw + prefetch-request + evict). When the app is unthrottled, the
    compositor drops a frame only if no fresh buffer was committed since
    the last vsync; the app commits one buffer per paint, so paint_cost <
    one refresh period => a fresh buffer every vsync => smooth at refresh.
  * The interval (paint-start to paint-start) is reported with its
    paint_cost/idle decomposition: idle≈0 means COMPUTE-bound (the app is
    the bottleneck), idle>0 means the cadence has slack. On-screen
    presentation cadence is corroborated OUT OF BAND with WAYLAND_DEBUG
    (wl_surface.commit + wl_callback.done) — see the report.

Protocol (mirrors balloons/README.md so the numbers are comparable):
  1. cold     — load cache, show window, wait until every visible tile is
                decoded; report cold_start_ms.
  2. flick A  — from the top, scroll down at <screens>/s for <seconds> s.
  3. teleport — jump to 25/50/75/99 %, record fill_ms, dwell 2 s each.
  4. flick B  — from 50 %, same speed/duration.
  5. one JSON line, exit 0.
Frame metrics aggregate flick A+B; flick_a vs flick_b are also reported
separately so a thermal/clock droop is visible, not hidden. A "blank"
frame is one in which a strictly-visible tile was still an undecoded
placeholder when painted.

    uv run apps/desktop-python/bench_scroll.py \
        --thumbs cache/benchmark-thumbs.fcache \
        --library cache/benchmark-library [--seconds 45] [--screens 2.5] \
        [--drive-hz N] [--label native] [--no-decorate]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

T0 = time.perf_counter()  # process-start reference for cold_start_ms

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QWindow  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from catalog import Catalog, load_catalog, save_catalog, scan_library  # noqa: E402
from grid import (  # noqa: E402
    CACHE_BYTES,
    CACHE_MAX_ENTRIES,
    PREFETCH_SCREENS,
    WORKERS,
    GridView,
)
from thumbcache import CacheError, ThumbCache, bind, load_cache  # noqa: E402

TELEPORT_STOPS = (0.25, 0.50, 0.75, 0.99)
DWELL_S = 2.0
# A teleport fill that doesn't converge within this many seconds is
# recorded as a (flagged) timeout rather than hanging the whole run. A
# stall here is the occlusion tell: if another window covers the surface,
# the compositor withholds frame callbacks and Qt's Wayland backing store
# stops painting at a STATIC scroll position, so the fill never converges —
# such a run is not occlusion_clean and is excluded from the §7 result.
FILL_TIMEOUT_S = 5.0
WIN = (1280, 800)
FLICKS = ("flick_a", "flick_b")
# Grid zoom bounds, mirroring GridView.set_zoom's clamp (tile = max(64,
# min(256, tile))). MIN zoom = smallest tile = 64 px; MAX zoom = 256 px.
# Tiles are decoded once at native ~256 px and scaled in paint (z1e), so
# these select layout/want-band size, not decode resolution.
ZOOM_MIN_TILE = 64
ZOOM_MAX_TILE = 256
# Frame-callback-timeout signature (QT_WAYLAND_FRAME_CALLBACK_TIMEOUT
# default ~100 ms): a cluster of intervals here means the compositor
# stopped sending callbacks (occluded/idle), not real jank.
TIMEOUT_MS = 100.0


class FrameProbe:
    """Installed on GridView._frame_probe. Records paint start/end
    timestamps into a SEPARATE bucket per flick phase (for paint_cost,
    paint-to-paint interval, and the idle gap between them) plus a
    blank-frame count. Because intervals are derived within a phase's own
    starts array (phase_metrics), the teleport-dwell gap and the
    cold->flick warm-up gap are never counted as one giant interval — the
    first paint of each phase contributes a duration but no interval."""

    def __init__(self) -> None:
        self.tag: str | None = None
        self.rec = {ph: {"starts": [], "ends": [], "blank": 0} for ph in FLICKS}

    def __call__(self, t_start: float, t_end: float, blank: bool) -> None:
        if self.tag is None:
            return
        b = self.rec[self.tag]
        b["starts"].append(t_start)
        b["ends"].append(t_end)
        if blank:
            b["blank"] += 1

    def start(self, tag: str) -> None:
        self.tag = tag

    def stop(self) -> None:
        self.tag = None


def pcts(xs: list[float]) -> dict:
    if not xs:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0,
                "min": 0.0, "mean": 0.0, "n": 0}
    s = sorted(xs)
    n = len(s)

    def p(q: float) -> float:
        # nearest-rank lower index; never overshoots to the max for q<1
        # (int(n*q) would return s[n] == p100 at n=100, q=0.99)
        return s[min(int((n - 1) * q), n - 1)]

    return {"p50": round(p(0.50), 2), "p95": round(p(0.95), 2),
            "p99": round(p(0.99), 2), "max": round(s[-1], 2),
            "min": round(s[0], 2), "mean": round(sum(s) / n, 2), "n": n}


def phase_metrics(b: dict, frame_period: float) -> dict:
    """Derive frame-production, interval, and idle stats for one phase."""
    starts, ends = b["starts"], b["ends"]
    dur = [(e - s) * 1000.0 for s, e in zip(starts, ends)]
    iv = [(starts[i] - starts[i - 1]) * 1000.0 for i in range(1, len(starts))]
    idle = [(starts[i] - ends[i - 1]) * 1000.0 for i in range(1, len(starts))]
    d, v = pcts(dur), pcts(iv)
    within = (100.0 * sum(1 for x in dur if x < frame_period) / len(dur)
              if dur else 0.0)
    return {
        "frames": d["n"],
        "frame_ms": d,                       # paint_cost = §7 gate metric
        "interval_ms": v,
        "idle_ms": pcts(idle),               # idle≈0 => compute-bound
        "blank_tile_frames": b["blank"],
        "deadline_hit_rate_pct": round(within, 1),  # within one refresh
        "missed_vsync_frames": sum(1 for x in iv if x > 1.5 * frame_period),
        "timeout_interval_frames": sum(
            1 for x in iv if abs(x - TIMEOUT_MS) <= 5.0),
        "frame_ms_p99_in_periods": round(d["p99"] / frame_period, 2)
        if frame_period else 0.0,
        "_dur": dur, "_iv": iv, "_idle": idle,  # for combined aggregation
    }


def resolve_zoom(arg: str | None) -> int | None:
    """Map a --zoom keyword/value to a target tile size in px, or None to
    leave the grid at its default tile size (today's behavior, so prior
    runs stay comparable). 'min'/'max' map to the grid's tile-size bounds;
    a number is an explicit tile size, which GridView.set_zoom clamps into
    the 64–256 range."""
    if arg is None:
        return None
    a = arg.strip().lower()
    if a == "min":
        return ZOOM_MIN_TILE
    if a == "max":
        return ZOOM_MAX_TILE
    try:
        return int(a)
    except ValueError:
        raise SystemExit(
            f"--zoom: expected 'min', 'max', or an integer tile size, "
            f"got {arg!r}")


def read_power() -> dict:
    def first_line(p: str) -> str | None:
        try:
            return Path(p).read_text().strip()
        except OSError:
            return None

    on_ac = None
    for p in ("/sys/class/power_supply/ACAD/online",
              "/sys/class/power_supply/AC/online",
              "/sys/class/power_supply/AC0/online"):
        v = first_line(p)
        if v is not None:
            on_ac = (v == "1")
            break
    # The DRM card index + connector name vary by host/GPU, so glob the
    # eDP connector rather than pinning card1-eDP-1 (which is null on any
    # other machine). First match wins; None if there is no eDP panel.
    vrr = next((first_line(str(p)) for p in
                sorted(Path("/sys/class/drm").glob("card*-eDP-*/vrr_capable"))),
               None)
    return {
        "on_ac": on_ac,
        "governor": first_line(
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "vrr_capable": vrr,
    }


def read_rss_mb() -> tuple[float, float]:
    rss = hwm = 0.0
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = float(line.split()[1]) / 1024.0
                elif line.startswith("VmHWM:"):
                    hwm = float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return rss, hwm


def decorate(catalog: Catalog) -> dict:
    """The benchmark catalog carries only paths (every star False, every
    rotate 0) — the LIGHTEST possible paint/decode. Inject a representative,
    deterministic star + rotation distribution so the star-badge polygon
    draw (paint) and the decode-time rotation (worker) are exercised, and
    report the exact fractions so per-frame cost is auditable."""
    stars = rotated = 0
    for i, p in enumerate(catalog.photos):
        if i % 12 == 0:                 # ~8.3 % starred
            p.star = True
            stars += 1
        r = (1 if i % 9 == 4 else 2 if i % 23 == 7 else 3 if i % 31 == 11
             else 0)                    # ~11 % + ~4 % + ~3 % ≈ 18 % rotated
        if r:
            p.rotate = r
            rotated += 1
    n = max(1, len(catalog.photos))
    return {"star_fraction": round(stars / n, 3),
            "rotated_fraction": round(rotated / n, 3)}


def load_data(thumbs_path: Path, library: Path) -> tuple[Catalog, ThumbCache]:
    cat_path = thumbs_path.with_suffix(thumbs_path.suffix + ".catalog.json")
    catalog = load_catalog(cat_path, library) if cat_path.is_file() else None
    if catalog is None:
        catalog = scan_library(library)
        try:
            save_catalog(catalog, cat_path)
        except OSError:
            pass
    cache = load_cache(thumbs_path)
    bind(cache, catalog)
    return catalog, cache


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    repo = Path(__file__).resolve().parents[2]
    ap.add_argument("--thumbs", type=Path,
                    default=repo / "cache" / "benchmark-thumbs.fcache")
    ap.add_argument("--library", type=Path,
                    default=repo / "cache" / "benchmark-library")
    ap.add_argument("--seconds", type=float, default=45.0)
    ap.add_argument("--screens", type=float, default=2.5,
                    help="scroll speed in screens/s (§7 budget allows <= 3)")
    ap.add_argument("--drive-hz", type=float, default=0.0,
                    help="scroll-update rate; 0 = the panel refresh rate")
    ap.add_argument("--zoom", default=None,
                    help="set grid zoom AFTER the cache loads and BEFORE the "
                         "flick phases: 'min' (64 px tiles), 'max' (256 px "
                         "tiles), or an explicit tile size in px (clamped to "
                         "the grid's 64–256 range). Omit to leave the grid at "
                         "its default tile size, reproducing prior runs.")
    ap.add_argument("--label", default="native")
    ap.add_argument("--run-index", type=int, default=0)
    ap.add_argument("--fullscreen", action="store_true",
                    help="show fullscreen on the active output so other "
                         "windows can't occlude the surface (Mutter withholds "
                         "frame callbacks from a covered window, stalling "
                         "paints); at DPR-scaled panels the logical geometry "
                         "is ~the same as the 1280x800 window")
    ap.add_argument("--no-decorate", action="store_true",
                    help="leave the catalog undecorated (lightest paint)")
    args = ap.parse_args()
    target_tile = resolve_zoom(args.zoom)  # validate before the heavy load

    if not args.thumbs.is_file():
        print(f"missing fcache: {args.thumbs}", file=sys.stderr)
        return 2
    if not args.library.is_dir():
        print(f"missing library: {args.library}", file=sys.stderr)
        return 2

    t_prep = time.perf_counter()
    catalog, cache = load_data(args.thumbs, args.library)
    deco = {"star_fraction": 0.0, "rotated_fraction": 0.0}
    if not args.no_decorate:
        deco = decorate(catalog)
    prep_ms = (time.perf_counter() - t_prep) * 1000.0
    print(f"loaded {len(catalog.photos)} photos, {len(catalog.folders)} "
          f"folders in {prep_ms:.0f} ms; decoration={deco}", file=sys.stderr)

    # The Wayland defaults below are for the Linux dev box this harness was
    # written on (Mutter/GNOME). On Windows/macOS, forcing
    # QT_QPA_PLATFORM=wayland aborts startup with "no Qt platform plugin
    # could be initialized" (there is no wayland plugin), so let Qt pick its
    # native plugin there — "windows"/"cocoa". The benchmark needs a real
    # display; the native plugin provides one. QT_IM_MODULE is harmless
    # everywhere.
    if sys.platform.startswith("linux"):
        os.environ.setdefault("QT_QPA_PLATFORM", "wayland")
        os.environ.setdefault("QT_WAYLAND_DISABLE_WINDOWDECORATION", "1")
    os.environ["QT_IM_MODULE"] = ""
    app = QApplication([])

    probe = FrameProbe()
    grid = GridView()
    grid._frame_probe = probe
    grid.set_data(catalog, cache)
    grid.set_filter(None, "")
    grid.resize(*WIN)
    grid.setWindowTitle("fauxcasa bench_scroll")
    if args.fullscreen:
        grid.showFullScreen()
    else:
        grid.show()
    grid.raise_()
    grid.activateWindow()

    # Apply the requested zoom now: the cache (ThumbCache) is already
    # loaded and the viewport realized, so the cold phase decodes + the
    # flick phases run at this tile size / want-band. Omitting --zoom
    # leaves grid.tile at its default, reproducing prior runs exactly.
    if target_tile is not None:
        grid.set_zoom(target_tile)
    print(f"zoom={args.zoom!r} -> tile_px={grid.tile}", file=sys.stderr)

    screen = grid.screen() or app.primaryScreen()
    refresh = screen.refreshRate() or 60.0
    drive_hz = args.drive_hz if args.drive_hz > 0 else refresh
    frame_period = 1000.0 / refresh
    dpr = grid.devicePixelRatioF()

    state = {"phase": "cold", "phase_start": T0, "tel": 0,
             "fill_start": None, "filled": False, "dwell_start": 0.0,
             "cold_ms": 0.0, "fills": [], "fill_timeouts": 0,
             "not_visible_ticks": 0, "flick_ticks": 0, "vis_seen": set()}

    def vp_h() -> int:
        return max(1, grid.viewport().height())

    def speed_px_s() -> float:
        return args.screens * vp_h()

    def max_off() -> int:
        return grid.verticalScrollBar().maximum()

    def advance(to: str, now: float) -> None:
        state["phase"], state["phase_start"] = to, now

    def sample_visibility() -> None:
        wh = grid.windowHandle()
        vis = wh.visibility() if wh is not None else None
        state["vis_seen"].add(vis.value if vis is not None else -1)
        state["flick_ticks"] += 1
        # Windowed=2, Maximized=4, FullScreen=5 are "on screen"; anything
        # else (Hidden=0, Minimized=3) means the surface stops getting
        # frame callbacks and the result is not trustworthy.
        if vis not in (QWindow.Visibility.Windowed,
                       QWindow.Visibility.Maximized,
                       QWindow.Visibility.FullScreen):
            state["not_visible_ticks"] += 1

    def tick() -> None:
        now = time.perf_counter()
        ph = state["phase"]
        elapsed = now - state["phase_start"]
        sb = grid.verticalScrollBar()

        if ph == "cold":
            grid.viewport().update()
            if grid.all_visible_decoded():
                state["cold_ms"] = (now - T0) * 1000.0
                print("READY", flush=True)
                probe.start("flick_a")
                advance("flick_a", now)
        elif ph == "flick_a":
            sb.setValue(int(min(elapsed * speed_px_s(), max_off())))
            sample_visibility()
            if elapsed >= args.seconds:
                probe.stop()
                state["tel"], state["filled"], state["fill_start"] = 0, False, None
                advance("teleport", now)
        elif ph == "teleport":
            i = state["tel"]
            if state["fill_start"] is None:
                sb.setValue(int(TELEPORT_STOPS[i] * max_off()))
                state["fill_start"] = now
                grid.viewport().update()
            elif not state["filled"]:
                grid.viewport().update()
                decoded = grid.all_visible_decoded()
                if decoded or (now - state["fill_start"]) >= FILL_TIMEOUT_S:
                    state["fills"].append(
                        round((now - state["fill_start"]) * 1000.0))
                    if not decoded:
                        state["fill_timeouts"] += 1  # starved decode, see load
                    state["filled"], state["dwell_start"] = True, now
            elif now - state["dwell_start"] >= DWELL_S:
                if i + 1 < len(TELEPORT_STOPS):
                    state["tel"], state["filled"], state["fill_start"] = \
                        i + 1, False, None
                else:
                    probe.start("flick_b")
                    advance("flick_b", now)
        elif ph == "flick_b":
            anchor = 0.5 * max_off()
            sb.setValue(int(min(anchor + elapsed * speed_px_s(), max_off())))
            sample_visibility()
            if elapsed >= args.seconds:
                probe.stop()
                advance("done", now)
        elif ph == "done":
            finish()

    def finish() -> None:
        timer.stop()
        per = {ph: phase_metrics(probe.rec[ph], frame_period) for ph in FLICKS}
        comb_dur = per["flick_a"]["_dur"] + per["flick_b"]["_dur"]
        comb_iv = per["flick_a"]["_iv"] + per["flick_b"]["_iv"]
        comb_idle = per["flick_a"]["_idle"] + per["flick_b"]["_idle"]
        d, v = pcts(comb_dur), pcts(comb_iv)
        blanks = sum(per[ph]["blank_tile_frames"] for ph in FLICKS)
        within = (100.0 * sum(1 for x in comb_dur if x < frame_period)
                  / len(comb_dur)) if comb_dur else 0.0
        expected = refresh * (2 * args.seconds)
        for ph in FLICKS:  # drop bulky raw arrays before emitting
            for k in ("_dur", "_iv", "_idle"):
                per[ph].pop(k, None)
        rss, hwm = read_rss_mb()
        wh = grid.windowHandle()
        try:
            load1, _l5, _l15 = os.getloadavg()
        except (OSError, AttributeError):
            load1 = -1.0
        # ~100 ms frame-callback-timeout cluster: counted once, used both as
        # a reported metric and as an occlusion/contention tell below.
        timeout_frames = sum(1 for x in comb_iv if abs(x - TIMEOUT_MS) <= 5.0)
        out = {
            "candidate": "tracer-grid",
            "label": args.label,
            "run_index": args.run_index,
            "photos": len(catalog.photos),
            # environment / provenance
            "platform": app.platformName(),
            "screen": screen.name(),
            "refresh_rate_hz": round(refresh, 2),
            "frame_period_ms": round(frame_period, 2),
            "drive_hz": round(drive_hz, 2),
            "device_pixel_ratio": round(dpr, 3),
            "window_logical": list(WIN),
            "viewport_logical": [grid.viewport().width(),
                                 grid.viewport().height()],
            "backing_store_px": [round(grid.viewport().width() * dpr),
                                 round(grid.viewport().height() * dpr)],
            "cols": grid.cols,
            "zoom": args.zoom,            # raw --zoom arg (None = default)
            "tile_px": grid.tile,         # effective tile size after set_zoom
            "content_h_px": grid.content_h,
            "group_count": len(grid.groups),
            "scroll_screens_per_s": args.screens,
            "scroll_px_per_s": round(speed_px_s()),
            "prefetch_screens": PREFETCH_SCREENS,
            "cache_bytes": CACHE_BYTES,
            "cache_max_entries": CACHE_MAX_ENTRIES,
            "decode_workers": WORKERS,  # grid's runtime decode-thread count
            **deco,
            **read_power(),
            "load_avg_1min": round(load1, 2),  # >~2 on this 16-thread box = contended
            "window_visibility_codes": sorted(state["vis_seen"]),
            "not_visible_flick_ticks": state["not_visible_ticks"],
            "flick_ticks": state["flick_ticks"],
            "window_visibility_final": (wh.visibility().value
                                        if wh is not None else None),
            "cold_start_ms": round(state["cold_ms"]),
            "prep_ms": round(prep_ms),
            # ---- combined flick metrics (the headline) ----
            "frames": d["n"],
            "frame_ms_p50": d["p50"], "frame_ms_p95": d["p95"],
            "frame_ms_p99": d["p99"], "frame_ms_max": d["max"],
            "frame_ms_min": d["min"], "frame_ms_mean": d["mean"],
            "frame_ms_p99_in_refresh_periods": round(d["p99"] / frame_period, 2),
            "deadline_hit_rate_pct": round(within, 1),
            "interval_ms_p50": v["p50"], "interval_ms_p99": v["p99"],
            "interval_ms_max": v["max"],
            "idle_ms_p50": pcts(comb_idle)["p50"],
            "idle_ms_mean": pcts(comb_idle)["mean"],
            "missed_vsync_frames": sum(1 for x in comb_iv
                                       if x > 1.5 * frame_period),
            "timeout_interval_frames": timeout_frames,
            "frames_vs_expected_ratio": round(d["n"] / expected, 3)
            if expected else 0.0,
            "blank_tile_frames": blanks,
            "fill_ms": state["fills"],
            "fill_timeouts": state["fill_timeouts"],
            "vm_rss_mb": round(rss, 1),
            "vm_hwm_mb": round(hwm, 1),
            # per-phase (thermal/clock droop visible: compare a vs b)
            "flick_a": per["flick_a"],
            "flick_b": per["flick_b"],
            # §7 verdict (frame-production time vs the absolute budget)
            "pass_p99_le_32ms": d["p99"] <= 32.0,
            "pass_max_le_100ms": d["max"] <= 100.0,
            "pass_zero_blank": blanks == 0,
            # NB: QWindow.visibility() reports "Windowed" even when another
            # window covers the surface, so visibility alone can't see
            # occlusion. A stalled fill (fill_timeouts) or a cluster of
            # ~100 ms intervals (frame-callback timeout) are the real
            # occlusion/contention tells; require all three to be clean.
            "occlusion_clean": (state["not_visible_ticks"] == 0
                                and state["fill_timeouts"] == 0
                                and timeout_frames == 0),
        }
        print(json.dumps(out), flush=True)
        app.quit()

    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.setInterval(max(1, round(1000.0 / drive_hz)))
    timer.timeout.connect(tick)
    timer.start()

    budget_s = (2 * args.seconds
                + len(TELEPORT_STOPS) * (DWELL_S + FILL_TIMEOUT_S) + 30)
    QTimer.singleShot(int(budget_s * 1000), lambda: (
        print(f"TIMEOUT after {budget_s:.0f}s in phase {state['phase']}",
              file=sys.stderr), app.exit(1)))

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
