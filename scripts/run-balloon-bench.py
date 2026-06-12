#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Build and benchmark every stack trial balloon sequentially
(fauxcasa-6hf). See balloons/README.md for the protocol.

    uv run scripts/run-balloon-bench.py [--fcache PATH] [--only NAME]
        [--display SOCKET]

Runs each balloon against the 100k cache under the given Wayland socket
(default wayland-bench — the headless weston used when the desktop
session is locked), captures its contract JSON line plus spawn->READY
wall time, sanity-checks the frame count (a deficit means the run
stalled — rerun on an idle machine), and writes cache/balloon-results.json.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CACHE = REPO / "cache"

BALLOONS = {
    "rust-egui": {
        "build": ["cargo", "build", "--release"],
        "build_cwd": REPO / "balloons/rust-egui",
        "run": [str(REPO / "balloons/rust-egui/target/release/balloon-rust-egui")],
    },
    "go-gio": {
        "build": [
            "toolbox", "run", "-c", "benchbox", "bash", "-c",
            f"cd {REPO}/balloons/go-gio && go build -o balloon-go-gio .",
        ],
        "build_cwd": REPO,
        "run_wrapper": "benchbox",  # binary needs the toolbox runtime libs
        "run": [str(REPO / "balloons/go-gio/balloon-go-gio")],
    },
    "py-qt": {
        "build": None,
        "run": ["uv", "run", "--script", str(REPO / "balloons/py-qt/main.py")],
    },
    "web-wasm": {
        "build": None,
        "run": ["uv", "run", "--script", str(REPO / "balloons/web-wasm/run.py")],
    },
}

EXPECTED_FLICK_SECONDS = 90.0  # two 45 s passes


def run_one(name: str, spec: dict, fcache: Path, display: str) -> dict:
    if spec.get("build"):
        t0 = time.monotonic()
        r = subprocess.run(spec["build"], cwd=spec.get("build_cwd", REPO),
                           capture_output=True, text=True)
        if r.returncode != 0:
            return {"candidate": name, "error": "build failed",
                    "stderr": r.stderr[-2000:]}
        build_s = time.monotonic() - t0
    else:
        build_s = 0.0

    cmd = spec["run"] + [str(fcache)]
    if spec.get("run_wrapper") == "benchbox":
        inner = " ".join(f"'{c}'" for c in cmd)
        cmd = ["toolbox", "run", "-c", "benchbox", "bash", "-c",
               f"WAYLAND_DISPLAY={display} {inner}"]
    print(f"== {name}: running ...", file=sys.stderr)
    t0 = time.monotonic()
    ready_wall_ms = None
    result_line = None
    import os

    env = dict(os.environ, WAYLAND_DISPLAY=display)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, env=env,
                            stderr=subprocess.DEVNULL)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line == "READY" and ready_wall_ms is None:
                ready_wall_ms = (time.monotonic() - t0) * 1000.0
            elif line.startswith("{"):
                result_line = line
        proc.wait(timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"candidate": name, "error": "timeout"}
    if result_line is None:
        return {"candidate": name, "error": f"no JSON (exit {proc.returncode})"}
    out = json.loads(result_line)
    out["spawn_to_ready_wall_ms"] = round(ready_wall_ms or 0.0)
    out["orchestrator_build_s"] = round(build_s, 1)
    # sanity: a big frame deficit vs the flick wall-time means stalls ate
    # wall clock (or the cadence isn't 60 Hz) — flag, don't fail
    frames, p50 = out.get("frames", 0), out.get("p50_ms", 0.0)
    if p50:
        expected = EXPECTED_FLICK_SECONDS * 1000.0 / p50
        out["frame_deficit_pct"] = round(100.0 * (1.0 - frames / expected), 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fcache", type=Path,
                    default=CACHE / "benchmark-thumbs.fcache")
    ap.add_argument("--only", choices=sorted(BALLOONS), default=None)
    ap.add_argument("--display", default="wayland-bench")
    args = ap.parse_args()
    if not args.fcache.is_file():
        print(f"missing fcache: {args.fcache}", file=sys.stderr)
        return 2

    results = []
    for name, spec in BALLOONS.items():
        if args.only and name != args.only:
            continue
        res = run_one(name, spec, args.fcache, args.display)
        print(json.dumps(res), file=sys.stderr)
        results.append(res)

    out = CACHE / "balloon-results.json"
    out.write_text(json.dumps(results, indent=1))
    print(f"results -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
