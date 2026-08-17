#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Local mirror of this repo's CI gates (fauxcasa-op6).

`bd preflight --check` (bd 1.2.1) runs hardcoded gates written for the
beads project itself: `go test`, `golangci-lint`, `gofmt`, go.sum/nix
version sync. None of those apply here — this repo is Python/uv with CI
defined in .github/workflows/tests.yml and tracer.yml. bd exposes no
per-repo preflight config, so this script fills that gap by running the
same checks CI runs, locally, before you push.

Checks (derived from .github/workflows/tests.yml and tracer.yml):
  - uv run scripts/test_delegation_report.py -q
  - uv run scripts/test_picasa_db.py -q
  - uv run scripts/check-ingest-parity.py
  - QT_QPA_PLATFORM=offscreen uv run apps/desktop-python/test_tracer.py -q   (skipped with --fast)
  - QT_QPA_PLATFORM=offscreen uv run apps/desktop-python/test_decodesvc_win.py -q
    (skipped with --fast; on Windows this spawns real AppContainer sandbox
    workers -- the i92.3 escape gates; elsewhere only the trusted-side
    protocol fuzz runs)
  - git ls-files --error-unmatch -- .beads/issues.jsonl must FAIL            (the one bd preflight
    check that still applies here: no beads-jsonl pollution — the file is
    pollution iff git tracks it, whether staged or committed; a plain
    `git diff` can never see it because the path is gitignored)

Deliberately NOT run: scripts/perf-canary.py. It's the CI-only §7
perf-canary job (cold start / search latency / catalog size probes on a
generated 24-photo corpus) — a canary against order-of-magnitude
regressions, not a local dev-loop check, so it's left to CI.

Usage:
  uv run scripts/preflight.py            # run all checks
  uv run scripts/preflight.py --fast     # skip the slow suites (tracer + decode-sandbox)
  uv run scripts/preflight.py --json     # machine-readable output

Exits nonzero if any check fails (mirrors `bd preflight --check`).
Stdlib only, so it runs anywhere uv/Python does, no extra deps.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

TIMEOUT_SECONDS = 10 * 60  # 10 min hard cap per check


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    )
    return Path(out.stdout.strip())


def checks(root: Path, fast: bool) -> list[dict]:
    result = [
        {
            "name": "delegation-report tests",
            "command": ["uv", "run", "scripts/test_delegation_report.py", "-q"],
            "cwd": root,
            "env": None,
        },
        {
            "name": "picasa_db tests",
            "command": ["uv", "run", "scripts/test_picasa_db.py", "-q"],
            "cwd": root,
            "env": None,
        },
        {
            "name": "ingest-parity gate",
            "command": ["uv", "run", "scripts/check-ingest-parity.py"],
            "cwd": root,
            "env": None,
        },
    ]
    if not fast:
        result.append({
            "name": "tracer tests",
            "command": ["uv", "run", "apps/desktop-python/test_tracer.py", "-q"],
            "cwd": root,
            "env": {"QT_QPA_PLATFORM": "offscreen"},
        })
        result.append({
            "name": "decode-sandbox suite",
            "command": ["uv", "run", "apps/desktop-python/test_decodesvc_win.py", "-q"],
            "cwd": root,
            "env": {"QT_QPA_PLATFORM": "offscreen"},
        })
    result.append({
        "name": "no beads-jsonl pollution",
        "command": ["git", "ls-files", "--error-unmatch", "--", ".beads/issues.jsonl"],
        "cwd": root,
        "env": None,
        # rc 0 means git tracks the file (staged or committed) == pollution
        "expect_nonzero": True,
    })
    return result


def run_check(check: dict) -> dict:
    import os

    env = None
    if check["env"]:
        env = dict(os.environ)
        env.update(check["env"])

    cmd_str = " ".join(check["command"])
    if check["env"]:
        prefix = " ".join(f"{k}={v}" for k, v in check["env"].items())
        cmd_str = f"{prefix} {cmd_str}"

    try:
        proc = subprocess.run(
            check["command"],
            cwd=check["cwd"],
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        passed = (
            proc.returncode != 0 if check.get("expect_nonzero")
            else proc.returncode == 0
        )
        output = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as e:
        passed = False
        output = (
            f"TIMEOUT after {TIMEOUT_SECONDS}s\n"
            f"{(e.stdout or '')}{(e.stderr or '')}"
        )

    return {
        "name": check["name"],
        "passed": passed,
        "command": cmd_str,
        "output": output.strip(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true",
                         help="skip the slow suites (tracer + decode-sandbox)")
    parser.add_argument("--json", action="store_true",
                         help="machine-readable output (name/passed/command/output)")
    args = parser.parse_args()

    root = repo_root()
    results = [run_check(c) for c in checks(root, args.fast)]
    all_passed = all(r["passed"] for r in results)

    if args.json:
        print(json.dumps({
            "checks": results,
            "passed": all_passed,
            "summary": f"{sum(r['passed'] for r in results)}/{len(results)} checks passed",
        }, indent=2))
    else:
        for r in results:
            status = "PASS" if r["passed"] else "FAIL"
            print(f"[{status}] {r['name']}  ({r['command']})")
            if not r["passed"] and r["output"]:
                for line in r["output"].splitlines():
                    print(f"    {line}")
        print()
        passed_count = sum(r["passed"] for r in results)
        print(f"{passed_count}/{len(results)} checks passed")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
