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


def test_empty_gates_command_raises_clear_error(tmp_path: Path) -> None:
    # PR 144 review finding: an empty command list passed validation
    # (isinstance(list) + all() over zero items is vacuously true) and
    # then crashed the gates section with a bare IndexError from
    # subprocess.run([]). An empty gates command is always a mistake --
    # there is no meaningful "disabled" reading of it -- so it must be
    # rejected here, before it can reach subprocess.run().
    (tmp_path / "daily-report.toml").write_text(
        "[gates]\ncommand = []\n", encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"\[gates\]\.command must not be empty"):
        daily_report.load_config(tmp_path)


def test_empty_pollution_paths_is_accepted_as_disabled(tmp_path: Path) -> None:
    # Unlike an empty gates command, an empty paths list is a legitimate
    # config for a repo that doesn't use fauxcasa's Dolt-sync
    # .beads/issues.jsonl-untracked convention: it disables the
    # pollution gate rather than being rejected.
    (tmp_path / "daily-report.toml").write_text(
        "[pollution_gate]\npaths = []\n", encoding="utf-8",
    )

    config = daily_report.load_config(tmp_path)

    assert config.pollution_paths == []


def test_pollution_gate_with_empty_paths_reports_disabled_not_pass() -> None:
    # PR 144 review finding: an empty paths list must not print a PASS
    # that looks identical to "checked every path and none is tracked" --
    # it must say the gate is disabled.
    lines, gate_passed = daily_report._pollution_gate(Path("."), [])

    assert gate_passed is True
    assert lines == ["Pollution gate: disabled ([pollution_gate].paths is empty)"]
    assert not any("PASS" in line or "FAIL" in line for line in lines)


def test_non_table_gates_section_raises_clear_error(tmp_path: Path) -> None:
    # PR 144 review finding: data.get("gates", {}).get("command", ...)
    # raised an unhandled AttributeError when [gates] itself wasn't a
    # table (e.g. `gates = "preflight"` at the top level), escaping the
    # clean ValueError -> exit-2 path.
    (tmp_path / "daily-report.toml").write_text(
        'gates = "preflight"\n', encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"\[gates\] must be a table"):
        daily_report.load_config(tmp_path)


def test_non_table_pollution_gate_section_raises_clear_error(tmp_path: Path) -> None:
    (tmp_path / "daily-report.toml").write_text(
        'pollution_gate = "off"\n', encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"\[pollution_gate\] must be a table"):
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
