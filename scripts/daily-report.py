#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""One-shot daily status/health report for this repo (fauxcasa).

Composes a single readable console report from five sections, in order:

  1. Git         - fetch, branch/ahead-behind, dirty/clean, worktree list.
  2. CI           - recent gh workflow runs and open PRs (gh CLI).
  3. Beads health - bd stats/ready/in-progress/stale/orphans, plus the
                    .beads/issues.jsonl pollution gate.
  4. Delegation   - uv run scripts/delegation-report.py --since <date>.
  5. Quality      - optional (--gates): uv run scripts/preflight.py --fast.

Degrades gracefully: a missing `gh` or `bd` binary prints a one-line
warning for that section and the report continues. The only things that
can fail the exit code are actual gate failures (the beads-jsonl pollution
gate, or preflight when --gates is passed) -- unavailable tools are
warnings, not failures.

Usage:
  uv run scripts/daily-report.py
  uv run scripts/daily-report.py --since 2026-08-01
  uv run scripts/daily-report.py --gates
  uv run scripts/daily-report.py --skip ci --skip delegation

Stdlib only, so it runs anywhere uv/Python does, no extra deps.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

DEFAULT_TIMEOUT = 120  # seconds
SECTIONS = ["git", "ci", "beads", "delegation", "gates"]


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    )
    return Path(out.stdout.strip())


def run(cmd: list[str], timeout: int = DEFAULT_TIMEOUT, cwd: Path | None = None,
        env: dict | None = None) -> tuple[int, str, str]:
    """Run cmd, returning (exit_code, stdout text, stderr text).

    stdout and stderr stay separate: a successful command can still emit
    warnings on stderr (e.g. git warning about an unreadable global
    excludes file), and mixing those into parsed stdout turns a clean
    `git status --porcelain` into a false DIRTY (Codex review finding).

    Never raises for a missing binary or nonzero exit -- returns a
    negative exit code and a message instead, so callers can degrade
    gracefully.
    """
    try:
        # Capture raw bytes and decode as UTF-8 ourselves: on Windows,
        # text=True decodes with the console's legacy codepage (cp1252),
        # which mangles UTF-8 output from tools like bd/gh (emoji, section
        # signs) into mojibake instead of cleanly falling back.
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=timeout,
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        return proc.returncode, out.strip(), err.strip()
    except FileNotFoundError:
        return -1, "", f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", errors="replace")
        err = (e.stderr or b"").decode("utf-8", errors="replace")
        return -2, out.strip(), f"TIMEOUT after {timeout}s\n{err.strip()}".strip()
    except OSError as exc:
        return -3, "", f"{cmd[0]}: {exc}"


def _text(out: str, err: str) -> str:
    """Combined display text for sections that just show a command's
    output verbatim (never for parsing)."""
    return "\n".join(p for p in (out, err) if p)


def _header(title: str) -> str:
    bar = "=" * 72
    return f"{bar}\n{title}\n{bar}"


def _trim(text: str, max_lines: int = 30) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    shown = lines[:max_lines]
    return "\n".join(shown) + f"\n... ({len(lines) - max_lines} more lines)"


# ---------------------------------------------------------------------------
# Section: Git
# ---------------------------------------------------------------------------

def section_git(root: Path) -> str:
    lines = [_header("1. GIT"), ""]

    fetch_rc, fetch_out, fetch_err = run(["git", "fetch", "origin", "--quiet"], cwd=root)
    if fetch_rc != 0:
        lines.append(f"[warn] git fetch origin failed (rc={fetch_rc}): {_text(fetch_out, fetch_err) or '(no output)'}")
    else:
        lines.append("git fetch origin: ok")
    lines.append("")

    status_rc, status_out, status_err = run(["git", "status", "-sb"], cwd=root)
    lines.append("Branch / ahead-behind (git status -sb):")
    if status_rc == 0:
        lines.append(_trim(status_out))
    else:
        lines.append(f"  [error] rc={status_rc}: {_text(status_out, status_err)}")
    lines.append("")

    ahead_behind_rc, ahead_behind_out, ahead_behind_err = run(
        ["git", "rev-list", "--left-right", "--count", "@{u}...HEAD"], cwd=root)
    if ahead_behind_rc == 0:
        parts = ahead_behind_out.split()
        if len(parts) == 2:
            lines.append(f"Behind / ahead of upstream: {parts[0]} behind, {parts[1]} ahead")
    else:
        lines.append(f"[info] no upstream configured or rev-list failed: {ahead_behind_err or ahead_behind_out}")
    lines.append("")

    dirty_rc, dirty_out, dirty_err = run(["git", "status", "--porcelain"], cwd=root)
    if dirty_rc == 0:
        dirty = bool(dirty_out.strip())
        lines.append(f"Working tree: {'DIRTY' if dirty else 'clean'}")
        if dirty:
            lines.append(_trim(dirty_out))
    else:
        lines.append(f"[error] git status --porcelain rc={dirty_rc}: {_text(dirty_out, dirty_err)}")
    lines.append("")

    wt_rc, wt_out, wt_err = run(["git", "worktree", "list"], cwd=root)
    lines.append("Worktrees (git worktree list):")
    if wt_rc == 0:
        lines.append(_trim(wt_out))
    else:
        lines.append(f"  [error] rc={wt_rc}: {_text(wt_out, wt_err)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section: CI
# ---------------------------------------------------------------------------

def section_ci(root: Path) -> str:
    lines = [_header("2. CI"), ""]

    gh_check_rc, _, _ = run(["gh", "--version"], cwd=root, timeout=15)
    if gh_check_rc != 0:
        lines.append(f"gh unavailable: gh CLI not found on PATH (rc={gh_check_rc})")
        return "\n".join(lines)

    runs_rc, runs_out, runs_err = run(["gh", "run", "list", "--limit", "5"], cwd=root)
    lines.append("Recent workflow runs (gh run list --limit 5):")
    if runs_rc == 0:
        lines.append(_trim(_text(runs_out, runs_err)))
    else:
        lines.append(f"gh unavailable: gh run list failed (rc={runs_rc}): {_text(runs_out, runs_err) or '(no output)'}")
    lines.append("")

    prs_rc, prs_out, prs_err = run(["gh", "pr", "list", "--state", "open"], cwd=root)
    lines.append("Open pull requests (gh pr list --state open):")
    if prs_rc == 0:
        lines.append(_trim(prs_out) if prs_out else "  (none)")
    else:
        lines.append(f"gh unavailable: gh pr list failed (rc={prs_rc}): {_text(prs_out, prs_err) or '(no output)'}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section: Beads health
# ---------------------------------------------------------------------------

def _pollution_gate(root: Path) -> tuple[list[str], bool]:
    """The .beads/issues.jsonl untracked gate. Only the expected
    no-match exit (rc 1) is a PASS: rc 0 means git tracks the file
    (pollution), and anything else (128 corrupt index, timeout, missing
    git) is an operational error that must FAIL rather than silently
    pass (Codex review finding)."""
    gate_rc, gate_out, gate_err = run(
        ["git", "ls-files", "--error-unmatch", "--", ".beads/issues.jsonl"],
        cwd=root)
    gate_passed = gate_rc == 1
    lines = ["Pollution gate (.beads/issues.jsonl must be untracked):",
             f"  [{'PASS' if gate_passed else 'FAIL'}] git ls-files --error-unmatch -- .beads/issues.jsonl"]
    if gate_rc == 0:
        lines.append("    .beads/issues.jsonl is tracked by git -- this is pollution.")
    elif gate_rc != 1:
        lines.append(f"    operational error (rc={gate_rc}): {_text(gate_out, gate_err)}")
    return lines, gate_passed


def section_beads(root: Path) -> tuple[str, bool]:
    """Return (report_text, pollution_gate_passed)."""
    lines = [_header("3. BEADS HEALTH"), ""]

    bd_check_rc, _, _ = run(["bd", "--version"], cwd=root, timeout=15)
    if bd_check_rc != 0:
        lines.append("bd unavailable: bd CLI not found on PATH")
        lines.append("")
        gate_lines, gate_passed = _pollution_gate(root)
        lines.extend(gate_lines)
        return "\n".join(lines), gate_passed

    bd_commands = [
        ("bd stats", ["bd", "stats"]),
        ("bd ready", ["bd", "ready"]),
        ("bd list --status=in_progress", ["bd", "list", "--status=in_progress"]),
        ("bd stale", ["bd", "stale"]),
        ("bd orphans", ["bd", "orphans"]),
    ]
    for label, cmd in bd_commands:
        rc, out, err = run(cmd, cwd=root)
        lines.append(f"{label}:")
        if rc == 0:
            lines.append(_trim(out) if out else "  (no output)")
        else:
            lines.append(f"  [error] rc={rc}: {_trim(_text(out, err))}")
        lines.append("")

    gate_lines, gate_passed = _pollution_gate(root)
    lines.extend(gate_lines)
    return "\n".join(lines), gate_passed


# ---------------------------------------------------------------------------
# Section: Delegation report
# ---------------------------------------------------------------------------

def section_delegation(root: Path, since: str) -> str:
    lines = [_header("4. DELEGATION REPORT"), ""]
    rc, out, err = run(
        ["uv", "run", "scripts/delegation-report.py", "--since", since],
        cwd=root, timeout=180)
    if rc == 0:
        lines.append(out)
    else:
        lines.append(f"[error] delegation-report.py failed (rc={rc}):")
        lines.append(_text(out, err))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section: Quality gates
# ---------------------------------------------------------------------------

def section_gates(root: Path, enabled: bool) -> tuple[str, bool]:
    """Return (report_text, gates_passed_or_skipped)."""
    lines = [_header("5. QUALITY GATES"), ""]
    if not enabled:
        lines.append("Skipped (pass --gates to run uv run scripts/preflight.py --fast).")
        return "\n".join(lines), True

    rc, out, err = run(
        ["uv", "run", "scripts/preflight.py", "--fast"],
        cwd=root, timeout=10 * 60)
    lines.append(f"uv run scripts/preflight.py --fast  (rc={rc})")
    lines.append(_trim(_text(out, err), max_lines=60))
    return "\n".join(lines), rc == 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # Ensure UTF-8 output on Windows (default console is cp1252) so that
    # subprocess output (e.g. delegation-report.py's bullets) round-trips.
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        import io
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is not None:
            sys.stdout = io.TextIOWrapper(
                buffer, encoding="utf-8", errors="replace", newline="\n"
            )

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    def _iso_date(s: str) -> str:
        # Validate here rather than letting the nested delegation-report
        # argparse fail with a section [error] while this script still
        # exits 0 (Codex review finding).
        try:
            return date.fromisoformat(s).isoformat()
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {s!r}") from e

    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        type=_iso_date,
        default=None,
        help="Start date for the delegation report (default: 7 days ago)",
    )
    parser.add_argument(
        "--gates",
        action="store_true",
        help="Also run uv run scripts/preflight.py --fast (off by default)",
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        choices=SECTIONS,
        metavar="SECTION",
        help=f"Skip a section (repeatable). Choices: {', '.join(SECTIONS)}",
    )
    args = parser.parse_args()

    skip = set(args.skip)
    since = args.since or (date.today() - timedelta(days=7)).isoformat()

    root = repo_root()
    report_sections: list[str] = []
    gates_ok = True

    if "git" not in skip:
        report_sections.append(section_git(root))
    if "ci" not in skip:
        report_sections.append(section_ci(root))
    if "beads" not in skip:
        text, pollution_ok = section_beads(root)
        report_sections.append(text)
        gates_ok = gates_ok and pollution_ok
    if "delegation" not in skip:
        report_sections.append(section_delegation(root, since))
    if "gates" not in skip:
        text, ok = section_gates(root, args.gates)
        report_sections.append(text)
        gates_ok = gates_ok and ok

    print(f"DAILY STATUS REPORT - {root.name}  ({date.today().isoformat()})")
    print()
    print("\n\n".join(report_sections))
    print()
    print(_header("SUMMARY"))
    print(f"Gates: {'PASS' if gates_ok else 'FAIL'}")

    return 0 if gates_ok else 1


if __name__ == "__main__":
    sys.exit(main())
