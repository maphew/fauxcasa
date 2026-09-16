#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "PySide6", "pillow", "pi-heif", "exiv2", "rawpy", "av", "zstandard"]
# ///
"""Thin wrapper: runs every apps/desktop-python/tests/test_*.py as its own
pytest process (fauxcasa-l09), one pytest process per file, so a leaked
timer or Qt wrapper from one module's tests cannot cross into another
module's. CLI args (-k, -x, -q, -v, ...) are forwarded to every file
unchanged. `--list-files` prints the file list and exits 0 without
running anything, for cheap debugging."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
TESTS_DIR = APP_DIR / "tests"


def _test_files() -> list[Path]:
    return sorted(TESTS_DIR.glob("test_*.py"))


def main(argv: list[str]) -> int:
    files = _test_files()
    if "--list-files" in argv:
        for f in files:
            print(f.relative_to(APP_DIR))
        return 0

    # The pre-split file set this lazily inside _offscreen_app; keeping
    # that too is harmless. Set here as well so AGENTS.md's documented
    # QT_QPA_PLATFORM=offscreen stays optional for a manual run.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    pytest_args = argv or ["-q"]
    stop_at_first_failure = "-x" in pytest_args

    failures = 0
    for f in files:
        start = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(f), *pytest_args],
            cwd=str(APP_DIR),
        )
        elapsed = time.monotonic() - start
        rc = proc.returncode
        ok = rc in (0, 5)  # 5: no tests collected, e.g. -k matched nothing here
        if not ok:
            failures += 1
        print(f"{f.name}: rc={rc} ({elapsed:.1f}s)")
        if not ok and stop_at_first_failure:
            break

    print(f"{len(files)} files, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
