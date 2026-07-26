#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pytest"]
# ///
"""Focused synthetic tests for delegation-report.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).with_name("delegation-report.py")
SPEC = importlib.util.spec_from_file_location("delegation_report", SCRIPT)
assert SPEC and SPEC.loader
report = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = report
SPEC.loader.exec_module(report)


def _write_jsonl(path: Path, entries: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries),
        encoding="utf-8",
        newline="\n",
    )


def _assistant(
    message_id: str,
    model: str | None,
    output: object,
    *,
    input_tokens: object = 1,
    timestamp: str = "2026-07-04T00:00:00Z",
    content: list[dict] | None = None,
) -> dict:
    message = {
        "id": message_id,
        "usage": {"input_tokens": input_tokens, "output_tokens": output},
        "content": content or [],
    }
    if model is not None:
        message["model"] = model
    return {"type": "assistant", "timestamp": timestamp, "message": message}


def test_progressive_snapshots_count_once_and_use_max_usage(tmp_path: Path) -> None:
    entries = [
        _assistant("msg-1", "claude-opus-4-8", 2),
        _assistant("msg-1", "claude-opus-4-8", 2),
        _assistant("msg-1", "claude-opus-4-8", 684, input_tokens=7),
    ]
    _write_jsonl(tmp_path / "session.jsonl", entries)
    _write_jsonl(tmp_path / "session/subagents/agent-a.jsonl", entries)

    sessions, errors = report.collect_sessions(tmp_path)

    assert not errors
    session = sessions[0]
    assert session.orch_usage.input_tokens == 7
    assert session.orch_usage.output_tokens == 684
    assert session.subagent_usage.output_tokens == 684
    assert session.calls_by_kind_model[("agent-tool", "claude-opus-4-8")] == 1


def test_same_model_share_uses_actual_orchestrator_model(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "session.jsonl",
        [_assistant("main", "claude-opus-4-8", 10)],
    )
    _write_jsonl(
        tmp_path / "session/subagents/agent-a.jsonl",
        [_assistant("sub", "claude-opus-4-8", 11)],
    )

    sessions, _ = report.collect_sessions(tmp_path)
    row = report._by_session_models(sessions)[0]

    assert row["main_model"] == "claude-opus-4-8"
    assert row["same_model_output_share"] == 1.0
    assert row["same_model_warning"] is True
    assert row["session"] == "session"


def test_unknown_model_is_reported_and_reduces_coverage(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "session.jsonl",
        [_assistant("main", "claude-fable-5", 10)],
    )
    _write_jsonl(
        tmp_path / "session/subagents/agent-a.jsonl",
        [
            _assistant("known", "claude-fable-5", 3),
            _assistant("unknown", None, 7),
        ],
    )

    sessions, _ = report.collect_sessions(tmp_path)
    row = report._by_session_models(sessions)[0]

    assert {item["model"] for item in row["breakdown"]} == {
        "(unknown)", "claude-fable-5"}
    assert row["model_metadata_coverage"] == 0.3
    assert row["same_model_output_share"] == 0.3
    assert row["same_model_warning"] is False


def test_zero_output_has_no_share_or_warning() -> None:
    session = report.SessionData(
        session_id="zero",
        start_date="2026-07-04",
        epoch="post",
        main_model="claude-fable-5",
        usage_by_kind_model={("agent-tool", "claude-fable-5"): report.TokenUsage()},
    )

    row = report._by_session_models([session])[0]

    assert row["same_model_output_share"] is None
    assert row["same_model_warning"] is False


@pytest.mark.parametrize(
    ("model", "attribution_agent", "expected"),
    [
        # A declared named tier wins over the model-derived tier, after the
        # same whitespace/case normalization used for arbitrary roles.
        ("claude-haiku-4-5", "  ReViEwEr\t", "reviewer"),
        # Non-tier roles remain visible instead of being folded into the
        # model's tier.
        ("claude-opus-4-8", " General-Purpose ", "general-purpose"),
        # Missing or whitespace-only attribution falls back to the model.
        ("claude-opus-4-8", "", "reviewer"),
        ("claude-sonnet-4-5", "  \t", "builder"),
        ("claude-haiku-4-5", None, "scout"),
    ],
)
def test_tier_for_usage_prefers_normalized_attribution_then_model(
        model: str, attribution_agent: str | None, expected: str) -> None:
    assert report._tier_for_usage(model, attribution_agent) == expected


def test_workflow_resumes_count_as_one_run() -> None:
    session = report.SessionData(session_id="s", start_date="", epoch="unknown")
    session.workflow_runs = [
        report.WorkflowRun("s", "", "first", "wf-1", None, "tool-1"),
        report.WorkflowRun("s", "", "", "wf-1", Path("unused"), "tool-2"),
    ]

    assert len(report._unique_workflow_runs(session)) == 1
    assert report._compliance([session])["workflow_tool_calls"] == 2
    assert report._compliance([session])["workflow_runs"] == 1


def test_partial_workflow_resume_matches_by_transcript_dir() -> None:
    transcript_dir = Path("same")
    session = report.SessionData(session_id="s", start_date="", epoch="unknown")
    session.workflow_runs = [
        report.WorkflowRun("s", "", "first", "", transcript_dir, "tool-1"),
        report.WorkflowRun("s", "", "", "wf-1", transcript_dir, "tool-2"),
    ]

    runs = report._unique_workflow_runs(session)

    assert len(runs) == 1
    assert runs[0].run_id == "wf-1"


def test_workflow_identity_bridge_is_transitive_and_non_mutating() -> None:
    transcript_dir = Path("same")
    session = report.SessionData(session_id="s", start_date="", epoch="unknown")
    session.workflow_runs = [
        report.WorkflowRun("s", "", "first", "wf-1", None, "tool-1"),
        report.WorkflowRun("s", "", "", "", transcript_dir, "tool-2"),
        report.WorkflowRun("s", "", "", "wf-1", transcript_dir, "tool-3"),
    ]

    first = report._unique_workflow_runs(session)
    second = report._unique_workflow_runs(session)

    assert len(first) == len(second) == 1
    assert first[0].run_id == "wf-1"
    assert first[0].transcript_dir == transcript_dir
    assert session.workflow_runs[0].transcript_dir is None


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-07-03T16:00:17Z", "pre"),
        ("2026-07-03T16:37:23Z", "post"),
    ],
)
def test_policy_epoch_uses_merge_timestamp(timestamp: str, expected: str) -> None:
    assert report._epoch(timestamp) == expected


def test_malformed_records_do_not_discard_session(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        "[]\n"
        + json.dumps(_assistant("main", "claude-fable-5", -5, input_tokens="bad"))
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    sessions, errors = report.collect_sessions(tmp_path)

    assert not errors
    assert len(sessions) == 1
    assert sessions[0].orch_usage.total == 0
    assert any("expected object" in warning for warning in sessions[0].parse_errors)


def test_scalar_metadata_does_not_discard_session(tmp_path: Path) -> None:
    malformed = _assistant(
        "main",
        "claude-fable-5",
        5,
        content=[
            {"type": "tool_use", "id": "agent", "name": "Agent", "input": {
                "subagent_type": 7, "description": 8}},
            {"type": "tool_use", "id": "workflow", "name": "Workflow", "input": {
                "script": 9}},
        ],
    )
    malformed["timestamp"] = 7
    _write_jsonl(tmp_path / "session.jsonl", [malformed])

    sessions, errors = report.collect_sessions(tmp_path)

    assert not errors
    assert len(sessions) == 1
    assert sessions[0].start_date == ""
    assert sessions[0].agent_spawns[0].subagent_type == ""
    assert sessions[0].workflow_runs[0].name == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))
