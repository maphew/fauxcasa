#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest"]
# ///
"""Focused tests for scripts/daily-report.py's config parsing
(fauxcasa-nn9).

Covers load_config()'s four contracts:
  - no daily-report.toml at all -> fauxcasa defaults
  - a daily-report.toml present but missing individual keys -> defaults
    for the missing keys
  - a daily-report.toml with explicit values -> those values, verbatim
  - a malformed daily-report.toml (bad TOML, or a key of the wrong
    shape) -> a clear ValueError, not a silent fallback
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("daily-report.py")
SPEC = importlib.util.spec_from_file_location("daily_report", SCRIPT)
assert SPEC and SPEC.loader
daily_report = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = daily_report
SPEC.loader.exec_module(daily_report)


def test_no_config_file_yields_fauxcasa_defaults(tmp_path: Path) -> None:
    config = daily_report.load_config(tmp_path)

    assert config.gates_command == daily_report.DEFAULT_GATES_COMMAND
    assert config.pollution_paths == daily_report.DEFAULT_POLLUTION_PATHS


def test_empty_config_file_yields_fauxcasa_defaults(tmp_path: Path) -> None:
    (tmp_path / "daily-report.toml").write_text("", encoding="utf-8")

    config = daily_report.load_config(tmp_path)

    assert config.gates_command == daily_report.DEFAULT_GATES_COMMAND
    assert config.pollution_paths == daily_report.DEFAULT_POLLUTION_PATHS


def test_config_with_only_gates_falls_back_for_missing_pollution_gate(tmp_path: Path) -> None:
    (tmp_path / "daily-report.toml").write_text(
        '[gates]\ncommand = ["uv", "run", "scripts/check.py"]\n',
        encoding="utf-8",
    )

    config = daily_report.load_config(tmp_path)

    assert config.gates_command == ["uv", "run", "scripts/check.py"]
    assert config.pollution_paths == daily_report.DEFAULT_POLLUTION_PATHS


def test_config_with_only_pollution_gate_falls_back_for_missing_gates(tmp_path: Path) -> None:
    (tmp_path / "daily-report.toml").write_text(
        '[pollution_gate]\npaths = ["secrets.env"]\n',
        encoding="utf-8",
    )

    config = daily_report.load_config(tmp_path)

    assert config.gates_command == daily_report.DEFAULT_GATES_COMMAND
    assert config.pollution_paths == ["secrets.env"]


def test_config_overrides_both_keys(tmp_path: Path) -> None:
    (tmp_path / "daily-report.toml").write_text(
        '[gates]\n'
        'command = ["uv", "run", "scripts/other-gate.py"]\n'
        '\n'
        '[pollution_gate]\n'
        'paths = ["a.jsonl", "b.jsonl"]\n',
        encoding="utf-8",
    )

    config = daily_report.load_config(tmp_path)

    assert config.gates_command == ["uv", "run", "scripts/other-gate.py"]
    assert config.pollution_paths == ["a.jsonl", "b.jsonl"]


def test_malformed_toml_raises_clear_error(tmp_path: Path) -> None:
    (tmp_path / "daily-report.toml").write_text(
        "this is not valid toml [[[", encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid TOML"):
        daily_report.load_config(tmp_path)


def test_wrong_type_gates_command_raises_clear_error(tmp_path: Path) -> None:
    (tmp_path / "daily-report.toml").write_text(
        '[gates]\ncommand = "uv run scripts/check.py"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"\[gates\]\.command must be a list of strings"):
        daily_report.load_config(tmp_path)


def test_wrong_type_pollution_paths_raises_clear_error(tmp_path: Path) -> None:
    (tmp_path / "daily-report.toml").write_text(
        '[pollution_gate]\npaths = "secrets.env"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"\[pollution_gate\]\.paths must be a list of strings"):
        daily_report.load_config(tmp_path)


def test_non_string_list_items_raise_clear_error(tmp_path: Path) -> None:
    (tmp_path / "daily-report.toml").write_text(
        '[gates]\ncommand = ["uv", "run", 7]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"\[gates\]\.command must be a list of strings"):
        daily_report.load_config(tmp_path)


def test_committed_fauxcasa_config_matches_defaults() -> None:
    """The repo-root daily-report.toml committed for fauxcasa itself must
    state the same values as the script's built-in defaults (fauxcasa-nn9
    requirement: defaults preserve current fauxcasa behaviour exactly)."""
    repo_root = SCRIPT.parent.parent
    config = daily_report.load_config(repo_root)

    assert config.gates_command == daily_report.DEFAULT_GATES_COMMAND
    assert config.pollution_paths == daily_report.DEFAULT_POLLUTION_PATHS


def test_claude_transcript_dir_matches_known_munging() -> None:
    # Verified empirically against real ~/.claude/projects/ entries
    # (fauxcasa-nn9): colons, slashes, backslashes, and dots all become
    # '-', with no collapsing of the resulting runs.
    root = Path("A:/dev/fauxcasa")
    result = daily_report.claude_transcript_dir(root)

    assert result == Path.home() / ".claude" / "projects" / "A--dev-fauxcasa"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))
