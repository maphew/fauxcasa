#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Delegation-policy compliance and cost report for Claude Code session transcripts.

Measures whether the repo's tiered delegation / workflow guidance (CLAUDE.md,
merged in PR #61 on 2026-07-03) is working by parsing Claude Code JSONL
transcript files.

Default input directory:
  ~/.claude/projects/A--dev-fauxcasa/

Schema observed (schema may drift; parser is defensive throughout):
  Top-level *.jsonl  — main-session transcripts (one per session UUID)
  <session_id>/subagents/<agent_id>.jsonl  — direct subagent transcripts
  <session_id>/subagents/workflows/<wf_id>/<agent_id>.jsonl — workflow agent files
  <session_id>/subagents/workflows/<wf_id>/journal.jsonl   — workflow run log

Key fields used:
  entry.type == 'assistant'
    .message.model            — model ID (e.g. 'claude-haiku-4-5-20251001')
    .message.usage            — {input_tokens, output_tokens,
                                 cache_creation_input_tokens,
                                 cache_read_input_tokens}
    .message.content[]        — tool_use items:
       name='Agent'  input={subagent_type, model?, description, prompt}
       name='Workflow' input={script}   (JS with embedded meta block)
  entry.attributionAgent      — set on subagent entries ('scout','builder',
                                'reviewer','general-purpose', …)
  entry.timestamp             — ISO-8601 string

Named delegation tiers (per CLAUDE.md):
  scout    — haiku, read-only searches
  builder  — sonnet, well-scoped implementation
  reviewer — opus, correctness review

Policy date: 2026-07-03 (PR #61 merged)

Usage:
  uv run scripts/delegation-report.py
  uv run scripts/delegation-report.py --since 2026-07-01
  uv run scripts/delegation-report.py --baseline
  uv run scripts/delegation-report.py --by-session
  uv run scripts/delegation-report.py --json
  uv run scripts/delegation-report.py --dir /path/to/project/transcripts
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POLICY_MERGED_AT = datetime(2026, 7, 3, 16, 37, 23, tzinfo=timezone.utc)
POLICY_DATE = POLICY_MERGED_AT.date()
BUDGET_DEFAULT = 200_000          # default per-task token budget in CLAUDE.md

NAMED_TIERS = {"scout", "builder", "reviewer"}

# Model-to-tier mapping for subagent files (attributionAgent may be absent)
MODEL_TIER: dict[str, str] = {
    "claude-haiku": "scout",
    "claude-sonnet": "builder",
    "claude-opus": "reviewer",
}

DEFAULT_DIR = Path.home() / ".claude" / "projects" / "A--dev-fauxcasa"

TIER_ORDER = {"scout": 0, "builder": 1, "reviewer": 2}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class AgentSpawn:
    """One Agent tool_use call in a main-session transcript."""
    session_id: str
    timestamp: str                 # ISO-8601 or ''
    subagent_type: str             # from input.subagent_type (may be '')
    model_hint: str                # from input.model (may be '')
    description: str               # from input.description (may be '')
    tool_use_id: str = ""
    agent_id: str = ""             # from the matching tool result, when present


@dataclass
class WorkflowRun:
    """One Workflow tool_use call in a main-session transcript."""
    session_id: str
    timestamp: str
    name: str                      # parsed from JS meta block, or ''
    run_id: str                    # from tool result toolUseResult.runId, or ''
    transcript_dir: Path | None    # where the wf agent files live
    tool_use_id: str = ""


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def total_with_cache(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_creation_tokens + self.cache_read_tokens)

    def __iadd__(self, other: "TokenUsage") -> "TokenUsage":
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cache_read_tokens += other.cache_read_tokens
        return self


@dataclass
class SessionData:
    session_id: str
    start_date: str                # YYYY-MM-DD, '' if unknown
    epoch: str                     # 'pre' | 'post'
    main_model: str = ""           # dominant orchestrator model by output tokens
    agent_spawns: list[AgentSpawn] = field(default_factory=list)
    workflow_runs: list[WorkflowRun] = field(default_factory=list)
    # token usage in the main transcript (orchestrator messages only)
    orch_usage: TokenUsage = field(default_factory=TokenUsage)
    # token usage across all subagent files for this session
    subagent_usage: TokenUsage = field(default_factory=TokenUsage)
    # model → TokenUsage (subagent files)
    usage_by_model: dict[str, TokenUsage] = field(default_factory=dict)
    # (model, attributionAgent) → TokenUsage (subagent files) — the tier
    # rollup (fauxcasa-7aj.3) needs the DECLARED role alongside the model,
    # since the same model backs both a named-tier spawn and a workflow
    # inherit-model step (see _tier_for_usage).
    usage_by_model_role: dict[tuple[str, str], TokenUsage] = field(
        default_factory=dict)
    # (kind, model) → TokenUsage / API-call count / transcript-file count, where
    # kind is 'workflow' (spawned via the Workflow tool) or 'agent-tool'
    # (spawned via the Agent tool). This per-session split surfaces sessions
    # where subagent models match the dominant main-session model.
    usage_by_kind_model: dict[tuple[str, str], TokenUsage] = field(
        default_factory=dict)
    calls_by_kind_model: dict[tuple[str, str], int] = field(
        default_factory=dict)
    transcripts_by_kind_model: dict[tuple[str, str], int] = field(
        default_factory=dict)
    parse_errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _safe_read_jsonl(path: Path) -> tuple[list[dict], list[str]]:
    """Read a JSONL file; return (entries, error_messages)."""
    entries: list[dict] = []
    errors: list[str] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        errors.append(
                            f"{path.name}:{lineno} expected object, got "
                            f"{type(value).__name__}")
                        continue
                    entries.append(value)
                except json.JSONDecodeError as exc:
                    errors.append(f"{path.name}:{lineno} bad JSON: {exc}")
    except OSError as exc:
        errors.append(f"cannot open {path}: {exc}")
    return entries, errors


def _token_count(value: Any) -> int:
    """Return a valid transcript token count, or zero for malformed values."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _extract_usage(usage: Any) -> TokenUsage:
    """Pull non-negative integer token counts from a usage object."""
    if not isinstance(usage, dict):
        return TokenUsage()
    return TokenUsage(
        input_tokens=_token_count(usage.get("input_tokens", 0)),
        output_tokens=_token_count(usage.get("output_tokens", 0)),
        cache_creation_tokens=_token_count(usage.get("cache_creation_input_tokens", 0)),
        cache_read_tokens=_token_count(usage.get("cache_read_input_tokens", 0)),
    )


def _max_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    """Merge progressive snapshots of one API response without double-counting."""
    return TokenUsage(
        input_tokens=max(left.input_tokens, right.input_tokens),
        output_tokens=max(left.output_tokens, right.output_tokens),
        cache_creation_tokens=max(left.cache_creation_tokens, right.cache_creation_tokens),
        cache_read_tokens=max(left.cache_read_tokens, right.cache_read_tokens),
    )


def _assistant_records(entries: list[dict]) -> list[tuple[dict, dict, TokenUsage]]:
    """Return one usage record per assistant message/API response.

    Claude transcripts may repeat a message ID as thinking, text, and tool-use
    snapshots. Keep the latest metadata and maximum observed token fields.
    Rows without a message ID remain distinct because they have no safe key.
    """
    records: dict[str, tuple[dict, dict, TokenUsage]] = {}
    order: list[str] = []
    for index, entry in enumerate(entries):
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        usage_raw = msg.get("usage")
        if not isinstance(usage_raw, dict) or not usage_raw:
            continue
        message_id = msg.get("id")
        key = f"id:{message_id}" if isinstance(message_id, str) and message_id else f"row:{index}"
        usage = _extract_usage(usage_raw)
        if key not in records:
            order.append(key)
            records[key] = (entry, msg, usage)
        else:
            _, _, previous = records[key]
            records[key] = (entry, msg, _max_usage(previous, usage))
    return [records[key] for key in order]


def _wf_name_from_script(script: str) -> str:
    """Try to extract the workflow name from a JS meta block."""
    m = re.search(r"name\s*:\s*['\"]([^'\"]+)['\"]", script)
    return m.group(1) if m else ""


def _entry_timestamp(entry: dict) -> str:
    return entry.get("timestamp", "") or ""


def _date_str(ts: str) -> str:
    """Return YYYY-MM-DD from an ISO timestamp, or ''."""
    if ts and len(ts) >= 10:
        return ts[:10]
    return ""


def _epoch(start_timestamp: str) -> str:
    if not start_timestamp:
        return "unknown"
    try:
        started = datetime.fromisoformat(start_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return "post" if started >= POLICY_MERGED_AT else "pre"


# ---------------------------------------------------------------------------
# Subagent usage aggregation
# ---------------------------------------------------------------------------

def _accumulate_subagent_dir(
    subagent_dir: Path,
    session: SessionData,
) -> None:
    """Walk <session_id>/subagents/ and add usage to session."""
    if not subagent_dir.is_dir():
        return
    for jsonl_file in subagent_dir.rglob("*.jsonl"):
        if jsonl_file.name == "journal.jsonl":
            continue
        entries, errors = _safe_read_jsonl(jsonl_file)
        session.parse_errors.extend(errors)
        kind = ("workflow"
                if "workflows" in jsonl_file.relative_to(subagent_dir).parts
                else "agent-tool")
        file_models: dict[str, int] = defaultdict(int)
        for entry, msg, usage in _assistant_records(entries):
            session.subagent_usage += usage
            raw_model = msg.get("model", "")
            model = raw_model if isinstance(raw_model, str) and raw_model else "(unknown)"
            if model not in session.usage_by_model:
                session.usage_by_model[model] = TokenUsage()
            session.usage_by_model[model] += usage
            raw_role = entry.get("attributionAgent", "")
            role = raw_role if isinstance(raw_role, str) else ""
            key = (model, role)
            if key not in session.usage_by_model_role:
                session.usage_by_model_role[key] = TokenUsage()
            session.usage_by_model_role[key] += usage
            km = (kind, model)
            if km not in session.usage_by_kind_model:
                session.usage_by_kind_model[km] = TokenUsage()
            session.usage_by_kind_model[km] += usage
            session.calls_by_kind_model[km] = (
                session.calls_by_kind_model.get(km, 0) + 1)
            file_models[model] += 1
        if file_models:
            # This is a transcript count, not a claim that every file maps to
            # one top-level Agent spawn.
            dominant = max(file_models, key=file_models.get)
            ka = (kind, dominant)
            session.transcripts_by_kind_model[ka] = (
                session.transcripts_by_kind_model.get(ka, 0) + 1)


# ---------------------------------------------------------------------------
# Main session transcript parser
# ---------------------------------------------------------------------------

def _parse_main_transcript(
    jsonl_path: Path,
    subagent_dir: Path,
) -> SessionData:
    """Parse one top-level session transcript and its subagent dir."""
    entries, errors = _safe_read_jsonl(jsonl_path)

    session_id = ""
    timestamps: list[str] = []
    spawn_list: list[AgentSpawn] = []
    workflow_list: list[WorkflowRun] = []
    orch_usage = TokenUsage()
    orch_model_usage: dict[str, TokenUsage] = defaultdict(TokenUsage)

    # We also need to pick up Workflow tool results for run_id/transcript_dir.
    # Build a map tool_use_id → WorkflowRun to patch from subsequent user entries.
    pending_wf: dict[str, WorkflowRun] = {}
    pending_agents: dict[str, AgentSpawn] = {}
    seen_tool_uses: set[str] = set()

    for _, msg, usage in _assistant_records(entries):
        orch_usage += usage
        raw_model = msg.get("model", "")
        if isinstance(raw_model, str) and raw_model:
            orch_model_usage[raw_model] += usage

    for entry in entries:
        etype = entry.get("type", "")
        sid = entry.get("sessionId", "") or ""
        if sid and not session_id:
            session_id = sid
        ts = _entry_timestamp(entry)
        if ts:
            timestamps.append(ts)

        if etype == "assistant":
            msg = entry.get("message") or {}
            if not isinstance(msg, dict):
                continue
            content = msg.get("content") or []
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "tool_use":
                    continue
                raw_tool_use_id = item.get("id", "")
                tool_use_id = raw_tool_use_id if isinstance(raw_tool_use_id, str) else ""
                if tool_use_id:
                    if tool_use_id in seen_tool_uses:
                        continue
                    seen_tool_uses.add(tool_use_id)
                tool_name = item.get("name", "")
                inp = item.get("input") or {}
                if not isinstance(inp, dict):
                    continue
                if tool_name == "Agent":
                    spawn = AgentSpawn(
                        session_id=session_id,
                        timestamp=ts,
                        subagent_type=inp.get("subagent_type", "") or "",
                        model_hint=inp.get("model", "") or "",
                        description=inp.get("description", "") or "",
                        tool_use_id=tool_use_id,
                    )
                    spawn_list.append(spawn)
                    if tool_use_id:
                        pending_agents[tool_use_id] = spawn
                elif tool_name == "Workflow":
                    script = inp.get("script", "") or ""
                    wfr = WorkflowRun(
                        session_id=session_id,
                        timestamp=ts,
                        name=_wf_name_from_script(script),
                        run_id="",
                        transcript_dir=None,
                        tool_use_id=tool_use_id,
                    )
                    workflow_list.append(wfr)
                    if tool_use_id:
                        pending_wf[tool_use_id] = wfr

        elif etype == "user":
            # Patch workflow runs from their tool results
            msg = entry.get("message") or {}
            if not isinstance(msg, dict):
                continue
            content = msg.get("content") or []
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "tool_result":
                    continue
                tool_use_id = item.get("tool_use_id", "")
                tur = entry.get("toolUseResult") or {}
                if not isinstance(tur, dict):
                    tur = {}
                if tool_use_id in pending_agents:
                    agent_id = tur.get("agentId", "") or ""
                    if isinstance(agent_id, str):
                        pending_agents.pop(tool_use_id).agent_id = agent_id
                    continue
                if tool_use_id not in pending_wf:
                    continue
                wfr = pending_wf.pop(tool_use_id)
                # Try toolUseResult field first
                run_id = tur.get("runId", "") or ""
                tdir = tur.get("transcriptDir", "") or ""
                if not run_id:
                    # Fallback: parse plain-text content
                    inner = item.get("content", [])
                    text = ""
                    if isinstance(inner, list):
                        for c in inner:
                            if isinstance(c, dict) and c.get("type") == "text":
                                text += c.get("text", "")
                    elif isinstance(inner, str):
                        text = inner
                    m = re.search(r"Run ID:\s*(\S+)", text)
                    if m:
                        run_id = m.group(1)
                    m2 = re.search(r"Transcript dir:\s*(.+)", text)
                    if m2:
                        tdir = m2.group(1).strip()
                wfr.run_id = run_id
                if tdir:
                    wfr.transcript_dir = Path(tdir)

    timestamps.sort()
    start_timestamp = timestamps[0] if timestamps else ""
    start_date = _date_str(start_timestamp)
    main_model = ""
    if orch_model_usage:
        main_model = max(
            orch_model_usage,
            key=lambda model: (
                orch_model_usage[model].output_tokens,
                orch_model_usage[model].total,
                model,
            ),
        )

    session = SessionData(
        session_id=session_id or jsonl_path.stem,
        start_date=start_date,
        epoch=_epoch(start_timestamp),
        main_model=main_model,
        agent_spawns=spawn_list,
        workflow_runs=workflow_list,
        orch_usage=orch_usage,
        parse_errors=errors,
    )

    # Accumulate subagent usage
    _accumulate_subagent_dir(subagent_dir, session)

    return session


# ---------------------------------------------------------------------------
# Collect all sessions
# ---------------------------------------------------------------------------

def collect_sessions(
    transcript_dir: Path,
    since: date | None = None,
    until: date | None = None,
) -> tuple[list[SessionData], list[str]]:
    """Return (sessions, global_errors) from the transcript directory."""
    sessions: list[SessionData] = []
    global_errors: list[str] = []

    if not transcript_dir.is_dir():
        global_errors.append(f"Transcript dir not found: {transcript_dir}")
        return sessions, global_errors

    for jsonl_file in sorted(transcript_dir.glob("*.jsonl")):
        session_dir = transcript_dir / jsonl_file.stem
        subagent_dir = session_dir / "subagents" if session_dir.is_dir() else Path("/nonexistent")
        try:
            session = _parse_main_transcript(jsonl_file, subagent_dir)
        except Exception as exc:
            global_errors.append(f"Fatal error parsing {jsonl_file.name}: {exc}")
            continue

        # Date filter
        if session.start_date:
            try:
                sd = date.fromisoformat(session.start_date)
            except ValueError:
                sd = None
            if sd:
                if since and sd < since:
                    continue
                if until and sd > until:
                    continue

        sessions.append(session)

    return sessions, global_errors


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def _classify_spawn_type(spawn: AgentSpawn) -> str:
    """Return a normalised tier label for a spawn."""
    st = (spawn.subagent_type or "").lower().strip()
    if st in NAMED_TIERS:
        return st
    if st:
        return st          # e.g. 'claude-code-guide', 'statusline-setup'
    return "(untyped)"


def _tier_for_model(model: str) -> str:
    m = model.lower()
    for prefix, tier in MODEL_TIER.items():
        if m.startswith(prefix):
            return tier
    if "fable" in m:
        return "main-session"
    return "other"


def _tier_for_usage(model: str, attribution_agent: str) -> str:
    """Classify one (model, attributionAgent) usage bucket for the cost-
    by-tier rollup (fauxcasa-7aj.3 nit): prefer the harness's DECLARED role
    over guessing from the model name. The same model backs both a genuine
    named-tier spawn and an unrelated role — e.g. claude-opus-4-8 shows up
    with attributionAgent='reviewer' (a real reviewer spawn) AND
    attributionAgent='general-purpose' (a workflow step that inherited the
    session's own model, per CLAUDE.md's `model: inherit` guidance for
    design/judge/verify stages — opus because the ORCHESTRATING session is
    on a smart model, not because the step is doing reviewer work).
    _tier_for_model alone conflated these: every opus usage record landed
    in 'reviewer' regardless of role. Here, a NAMED_TIERS attributionAgent
    wins outright; anything else keeps its own role label (so
    'general-purpose'/'workflow-subagent' surface honestly instead of
    being folded into 'reviewer'); only a genuinely absent attributionAgent
    falls back to the model-prefix guess."""
    aa = (attribution_agent or "").lower().strip()
    if aa in NAMED_TIERS:
        return aa
    if aa:
        return aa
    return _tier_for_model(model)


def _unique_workflow_runs(session: SessionData) -> list[WorkflowRun]:
    """Collapse resume tool calls that refer to the same workflow run."""
    unique: dict[str, WorkflowRun] = {}
    for index, run in enumerate(session.workflow_runs):
        if run.run_id:
            key = f"run:{run.run_id}"
        elif run.transcript_dir:
            key = f"dir:{run.transcript_dir}"
        elif run.tool_use_id:
            key = f"tool:{run.tool_use_id}"
        else:
            key = f"row:{index}"
        existing = unique.get(key)
        if existing is None:
            unique[key] = run
        else:
            if not existing.name and run.name:
                existing.name = run.name
            if not existing.transcript_dir and run.transcript_dir:
                existing.transcript_dir = run.transcript_dir
    return list(unique.values())


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _compliance(sessions: list[SessionData]) -> dict:
    by_type: dict[str, int] = defaultdict(int)
    by_model: dict[str, int] = defaultdict(int)
    named_count = 0
    total_count = 0

    for sess in sessions:
        for sp in sess.agent_spawns:
            label = _classify_spawn_type(sp)
            by_type[label] += 1
            total_count += 1
            if label in NAMED_TIERS:
                named_count += 1
            model = sp.model_hint or ""
            if model:
                by_model[model] += 1

    # Workflow runs per session
    wf_sessions = sum(1 for s in sessions if s.workflow_runs)
    total_wf_calls = sum(len(s.workflow_runs) for s in sessions)
    total_wf_runs = sum(len(_unique_workflow_runs(s)) for s in sessions)

    share = named_count / total_count if total_count else 0.0

    return {
        "total_agent_spawns": total_count,
        "by_type": dict(sorted(by_type.items())),
        "named_tier_spawns": named_count,
        "named_tier_share": round(share, 3),
        "explicit_model_hint_count": sum(by_model.values()),
        "model_hints": dict(sorted(by_model.items())),
        "workflow_tool_calls": total_wf_calls,
        "workflow_runs": total_wf_runs,
        "sessions_with_workflows": wf_sessions,
    }


def _cost(sessions: list[SessionData]) -> dict:
    total_orch = TokenUsage()
    total_sub = TokenUsage()
    model_usage: dict[str, TokenUsage] = defaultdict(TokenUsage)
    wf_token_totals: list[dict] = []

    for sess in sessions:
        total_orch += sess.orch_usage
        total_sub += sess.subagent_usage
        for model, usage in sess.usage_by_model.items():
            model_usage[model] += usage

    # Per-tier rollup (fauxcasa-7aj.3): classified per (model, role) pair,
    # NOT per pre-aggregated model total — _tier_for_usage needs the
    # attributionAgent alongside the model to avoid folding a workflow's
    # inherit-model steps into 'reviewer' just because they happen to run
    # on the same model as a real reviewer spawn.
    tier_usage: dict[str, TokenUsage] = defaultdict(TokenUsage)
    for sess in sessions:
        for (model, role), usage in sess.usage_by_model_role.items():
            tier = _tier_for_usage(model, role)
            tier_usage[tier] += usage

    # Workflow token totals from subagent dirs
    for sess in sessions:
        for wfr in _unique_workflow_runs(sess):
            wf_usage = TokenUsage()
            if wfr.transcript_dir and wfr.transcript_dir.is_dir():
                for jf in wfr.transcript_dir.rglob("*.jsonl"):
                    if jf.name == "journal.jsonl":
                        continue
                    entries, _ = _safe_read_jsonl(jf)
                    for _, _, usage in _assistant_records(entries):
                        wf_usage += usage
            wf_token_totals.append({
                "session": sess.session_id[:8],
                "date": sess.start_date,
                "name": wfr.name or wfr.run_id or "(unknown)",
                "run_id": wfr.run_id,
                "input_tokens": wf_usage.input_tokens,
                "output_tokens": wf_usage.output_tokens,
                "total": wf_usage.total,
                "budget_default": BUDGET_DEFAULT,
                "pct_of_budget": (
                    round(wf_usage.total / BUDGET_DEFAULT * 100, 1)
                    if wf_usage.total else None
                ),
            })

    return {
        "orchestrator_tokens": {
            "input": total_orch.input_tokens,
            "output": total_orch.output_tokens,
            "total": total_orch.total,
        },
        "subagent_tokens": {
            "input": total_sub.input_tokens,
            "output": total_sub.output_tokens,
            "total": total_sub.total,
        },
        "by_model": {
            model: {"input": u.input_tokens, "output": u.output_tokens, "total": u.total}
            for model, u in sorted(model_usage.items())
        },
        "by_tier": {
            tier: {"input": u.input_tokens, "output": u.output_tokens, "total": u.total}
            for tier, u in sorted(tier_usage.items())
        },
        "workflow_runs": wf_token_totals,
    }


def _by_session_models(sessions: list[SessionData]) -> list[dict]:
    """Per-session subagent model usage, split workflow vs agent-tool.

    The global by_model rollup can't distinguish 'healthy tiering everywhere'
    from 'one session ran everything on the session model' — this view can.
    same_model_output_share compares actual subagent model IDs with the
    dominant model in the main transcript. It is evidence of model matching,
    not proof that a spawn inherited rather than explicitly selected a model.
    """
    rows: list[dict] = []
    for sess in sessions:
        if not sess.usage_by_kind_model:
            continue
        breakdown = []
        for (kind, model), usage in sorted(sess.usage_by_kind_model.items()):
            breakdown.append({
                "kind": kind,
                "model": model,
                "transcripts": sess.transcripts_by_kind_model.get((kind, model), 0),
                "api_calls": sess.calls_by_kind_model.get((kind, model), 0),
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            })
        total_out = sum(b["output_tokens"] for b in breakdown)
        same_model_out = sum(
            b["output_tokens"] for b in breakdown
            if sess.main_model and b["model"] == sess.main_model
        )
        known_out = sum(
            b["output_tokens"] for b in breakdown if b["model"] != "(unknown)"
        )
        share = same_model_out / total_out if total_out and sess.main_model else None
        rows.append({
            "session": sess.session_id,
            "date": sess.start_date,
            "epoch": sess.epoch,
            "main_model": sess.main_model or None,
            "breakdown": breakdown,
            "same_model_output_share": round(share, 3) if share is not None else None,
            "same_model_warning": bool(
                total_out and sess.main_model and same_model_out * 2 > total_out),
            "model_metadata_coverage": (
                round(known_out / total_out, 3) if total_out else None),
        })
    return sorted(rows, key=lambda row: (row["date"], row["session"]))


def _quality(sessions: list[SessionData]) -> dict:
    reviewer_count = sum(
        1 for s in sessions
        for sp in s.agent_spawns
        if _classify_spawn_type(sp) == "reviewer"
    )

    # Escalation heuristic: consecutive Agent spawns in same session
    # where the second spawn's tier is strictly higher than the first.
    escalation_events: list[dict] = []
    for sess in sessions:
        spawns = sess.agent_spawns
        for i in range(1, len(spawns)):
            prev = _classify_spawn_type(spawns[i - 1])
            curr = _classify_spawn_type(spawns[i])
            prev_ord = TIER_ORDER.get(prev, -1)
            curr_ord = TIER_ORDER.get(curr, -1)
            if prev_ord >= 0 and curr_ord > prev_ord:
                escalation_events.append({
                    "session": sess.session_id[:8],
                    "date": sess.start_date,
                    "from_tier": prev,
                    "to_tier": curr,
                    "from_desc": spawns[i - 1].description[:60],
                    "to_desc": spawns[i].description[:60],
                })

    manual_metrics = [
        "cost_per_merged_pr — divide total tokens by PRs merged (git log --merges | wc -l)",
        "post_merge_fixups — search git log for 'fix(*)' commits on same beads after merge",
        "bead_reopens — bd list --status=reopened | wc -l (requires bd/dolt)",
        "These need manual triangulation with ccusage, GitHub, and dolt — not computed here.",
    ]

    return {
        "reviewer_spawns": reviewer_count,
        "escalation_events_heuristic": escalation_events,
        "escalation_note": (
            "Heuristic: consecutive Agent calls within same session where tier "
            "rank increases (scout→builder→reviewer). False positives possible "
            "when two unrelated tasks coincidentally run in order."
        ),
        "manual_triangulation_needed": manual_metrics,
    }


def _baseline_split(sessions: list[SessionData]) -> dict:
    pre = [s for s in sessions if s.epoch == "pre"]
    post = [s for s in sessions if s.epoch == "post"]
    unknown = [s for s in sessions if s.epoch not in ("pre", "post")]

    def _summary(group: list[SessionData]) -> dict:
        total_spawns = sum(len(s.agent_spawns) for s in group)
        named = sum(
            1 for s in group
            for sp in s.agent_spawns
            if _classify_spawn_type(sp) in NAMED_TIERS
        )
        wf = sum(len(_unique_workflow_runs(s)) for s in group)
        reviewer = sum(
            1 for s in group
            for sp in s.agent_spawns
            if _classify_spawn_type(sp) == "reviewer"
        )
        total_tokens = sum(
            s.orch_usage.total + s.subagent_usage.total for s in group
        )
        share = round(named / total_spawns, 3) if total_spawns else None
        return {
            "sessions": len(group),
            "agent_spawns": total_spawns,
            "named_tier_spawns": named,
            "named_tier_share": share,
            "reviewer_spawns": reviewer,
            "workflow_runs": wf,
            "total_tokens_approx": total_tokens,
        }

    return {
        "policy_date": str(POLICY_DATE),
        "pre_policy": _summary(pre),
        "post_policy": _summary(post),
        "unknown_epoch": _summary(unknown),
    }


# ---------------------------------------------------------------------------
# Text formatter
# ---------------------------------------------------------------------------

def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _pct(share: float | None) -> str:
    if share is None:
        return "n/a"
    return f"{share * 100:.1f}%"


def render_text(
    sessions: list[SessionData],
    global_errors: list[str],
    since: date | None,
    until: date | None,
    baseline_mode: bool,
    by_session_mode: bool = False,
) -> str:
    lines: list[str] = []
    add = lines.append

    date_range = ""
    if since or until:
        parts = []
        if since:
            parts.append(f"since {since}")
        if until:
            parts.append(f"until {until}")
        date_range = " (" + ", ".join(parts) + ")"

    add("=" * 72)
    add(f"DELEGATION POLICY COMPLIANCE REPORT{date_range}")
    add(f"Sessions analysed: {len(sessions)}")
    if sessions:
        dates = [s.start_date for s in sessions if s.start_date]
        if dates:
            add(f"Date range:        {min(dates)}  to  {max(dates)}")
    add(f"Policy date:       {POLICY_DATE}  (PR #61, tiering guidance live)")
    add("=" * 72)
    add("")

    # --- COMPLIANCE ---
    comp = _compliance(sessions)
    add("-- 1. COMPLIANCE " + "-" * 55)
    add("")
    add(f"Total Agent spawns:     {_fmt_int(comp['total_agent_spawns'])}")
    add(f"  Named-tier spawns:    {_fmt_int(comp['named_tier_spawns'])}  "
        f"({_pct(comp['named_tier_share'])} of total)")
    add("")
    add("Spawns by subagent_type (named tiers marked with *):")
    for stype, cnt in sorted(comp["by_type"].items(), key=lambda kv: -kv[1]):
        marker = " *" if stype in NAMED_TIERS else ""
        add(f"  {stype:<30}  {_fmt_int(cnt):>6}{marker}")
    add("")
    add(f"Explicit model hints in Agent calls: {_fmt_int(comp['explicit_model_hint_count'])}")
    if comp["model_hints"]:
        for m, cnt in sorted(comp["model_hints"].items(), key=lambda kv: -kv[1]):
            add(f"  {m:<40}  {cnt}")
    add("")
    add(f"Workflow tool calls:    {_fmt_int(comp['workflow_tool_calls'])}  "
        f"(in {comp['sessions_with_workflows']} session(s))")
    if comp["workflow_runs"] != comp["workflow_tool_calls"]:
        add(f"Unique workflow runs:  {_fmt_int(comp['workflow_runs'])}  "
            "(resume calls collapsed by run ID)")
    add("")

    # --- COST ---
    cost = _cost(sessions)
    add("-- 2. COST " + "-" * 61)
    add("")
    ot = cost["orchestrator_tokens"]
    st = cost["subagent_tokens"]
    grand = ot["total"] + st["total"]
    add(f"Token totals (input + output, no cache):")
    add(f"  Orchestrator (main session):  {_fmt_int(ot['total']):>12}  "
        f"({_fmt_int(ot['input'])} in, {_fmt_int(ot['output'])} out)")
    add(f"  Subagents (all):              {_fmt_int(st['total']):>12}  "
        f"({_fmt_int(st['input'])} in, {_fmt_int(st['output'])} out)")
    add(f"  Grand total (approx):         {_fmt_int(grand):>12}")
    add("")
    if cost["by_tier"]:
        add("Token distribution by tier (subagent files only):")
        for tier, u in sorted(cost["by_tier"].items()):
            pct = u["total"] / st["total"] * 100 if st["total"] else 0
            add(f"  {tier:<18}  {_fmt_int(u['total']):>12}  ({pct:.1f}%)")
        add("")
    if cost["by_model"]:
        add("Token distribution by model:")
        for model, u in sorted(cost["by_model"].items(), key=lambda kv: -kv[1]["total"]):
            add(f"  {model:<44}  {_fmt_int(u['total']):>10}")
        add("")
    if cost["workflow_runs"]:
        add(f"Workflow runs ({len(cost['workflow_runs'])} total, default budget {_fmt_int(BUDGET_DEFAULT)} tok):")
        for wf in cost["workflow_runs"]:
            budget_note = (
                f"  ({wf['pct_of_budget']:.0f}% of budget)"
                if wf["pct_of_budget"] is not None
                else "  (no token data)"
            )
            add(f"  [{wf['date']}] {wf['name']:<32}  "
                f"{_fmt_int(wf['total']):>8} tok{budget_note}")
        add("")

    # --- PER-SESSION SUBAGENT MODELS ---
    if by_session_mode:
        add("-- 2b. PER-SESSION SUBAGENT MODELS " + "-" * 37)
        add("")
        add("  (! marks sessions where >50% of subagent output tokens used the")
        add("   dominant main-session model. A model match is not proof that the")
        add("   spawn inherited rather than explicitly selected that model.)")
        add("")
        for row in _by_session_models(sessions):
            share = row["same_model_output_share"]
            flag = "  ! SAME MODEL" if row["same_model_warning"] else ""
            add(f"  [{row['date']}] {row['session'][:8]}  "
                f"(epoch: {row['epoch']}, "
                f"main model: {row['main_model'] or 'unknown'}, "
                f"same-model share: {_pct(share)}, "
                f"model coverage: {_pct(row['model_metadata_coverage'])}){flag}")
            for b in row["breakdown"]:
                add(f"    {b['kind']:<10}  {b['model']:<38}  "
                    f"{b['transcripts']:>3} files  {_fmt_int(b['api_calls']):>6} calls  "
                    f"{_fmt_int(b['output_tokens']):>10} out-tok")
            add("")

    # --- QUALITY ---
    qual = _quality(sessions)
    add("-- 3. QUALITY (proxies) " + "-" * 49)
    add("")
    add(f"Reviewer spawns: {qual['reviewer_spawns']}")
    add("")
    esc = qual["escalation_events_heuristic"]
    add(f"Escalation events (heuristic): {len(esc)}")
    add(f"  [{qual['escalation_note']}]")
    if esc:
        for ev in esc[:10]:
            # Both descriptions (fauxcasa-7aj.3): the FROM one alone only
            # shows what got escalated away from, not what it became — the
            # TO description was computed and put on JSON output all along
            # but dropped on the way to the text report.
            add(
                f"  {ev['date']} {ev['session']}... "
                f"{ev['from_tier']} -> {ev['to_tier']}: "
                f"'{ev['from_desc']}' -> '{ev['to_desc']}'"
            )
        if len(esc) > 10:
            add(f"  ... {len(esc) - 10} more")
    add("")
    add("Metrics requiring manual triangulation (NOT computed here):")
    for note in qual["manual_triangulation_needed"]:
        add(f"  • {note}")
    add("")

    # --- BASELINE ---
    bs = _baseline_split(sessions)
    add("-- 4. BASELINE (pre / post 2026-07-03) " + "-" * 33)
    add("")
    for label, key in [("PRE-policy  (baseline)", "pre_policy"),
                        ("POST-policy (target)", "post_policy")]:
        g = bs[key]
        add(f"  {label}:")
        add(f"    Sessions:           {g['sessions']}")
        add(f"    Agent spawns:       {g['agent_spawns']}")
        add(f"    Named-tier spawns:  {g['named_tier_spawns']}  "
            f"({_pct(g['named_tier_share'])})")
        add(f"    Reviewer spawns:    {g['reviewer_spawns']}")
        add(f"    Workflow runs:      {g['workflow_runs']}")
        add(f"    Tokens (approx):    {_fmt_int(g['total_tokens_approx'])}")
        add("")

    if baseline_mode:
        add("  [--baseline mode: full session list by epoch]")
        for epoch in ("pre", "post"):
            group = [s for s in sessions if s.epoch == epoch]
            add(f"  {epoch.upper()} sessions:")
            for s in group:
                add(f"    {s.start_date}  {s.session_id[:20]}  "
                    f"spawns={len(s.agent_spawns)}  wf={len(s.workflow_runs)}")
        add("")

    # --- ERRORS ---
    all_errors = list(global_errors)
    for s in sessions:
        all_errors.extend(s.parse_errors)
    if all_errors:
        add("-- PARSE WARNINGS " + "-" * 54)
        for err in all_errors[:20]:
            add(f"  ! {err}")
        if len(all_errors) > 20:
            add(f"  … {len(all_errors) - 20} more")
        add("")

    # --- FOOTER ---
    add("-- COUNTERFACTUAL CAVEAT " + "-" * 48)
    add("")
    add("  No single number proves effectiveness. What to look for:")
    add("  • COMPLIANCE high (named-tier share rising post-policy)")
    add("  • COST per merged PR trending down (triangulate with ccusage + git)")
    add("  • QUALITY flat or up (reviewer share, fewer post-merge fix commits)")
    add("  • If all three trend in the right direction together, the policy")
    add("    is likely working — but the repo is also growing, so normalise")
    add("    cost by PR count, not absolute tokens.")
    add("")
    add("  Data gaps: model hints are often absent from Agent calls (the")
    add("  subagent's actual model is in message.model in its own transcript;")
    add("  attributionAgent separately records its declared role. The")
    add("  token counts exclude cache write/read overhead.")
    add("")
    add("=" * 72)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------

def render_json(
    sessions: list[SessionData],
    global_errors: list[str],
    since: date | None,
    until: date | None,
) -> str:
    all_errors = list(global_errors)
    for s in sessions:
        all_errors.extend(s.parse_errors)

    out = {
        "meta": {
            "schema_version": 2,
            "sessions_analysed": len(sessions),
            "policy_date": str(POLICY_DATE),
            "since": str(since) if since else None,
            "until": str(until) if until else None,
        },
        "compliance": _compliance(sessions),
        "cost": _cost(sessions),
        "by_session_models": _by_session_models(sessions),
        "quality": _quality(sessions),
        "baseline": _baseline_split(sessions),
        "parse_errors": all_errors[:100],
    }
    return json.dumps(out, indent=2, default=str)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Expected YYYY-MM-DD, got {s!r}")


def main() -> None:
    # Ensure UTF-8 output on Windows (default console is cp1252)
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
    parser.add_argument(
        "--dir",
        metavar="PATH",
        default=str(DEFAULT_DIR),
        help=f"Transcript directory (default: {DEFAULT_DIR})",
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        type=_parse_date,
        default=None,
        help="Include only sessions starting on or after this date",
    )
    parser.add_argument(
        "--until",
        metavar="YYYY-MM-DD",
        type=_parse_date,
        default=None,
        help="Include only sessions starting on or before this date",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Show full session list split by pre/post policy date",
    )
    parser.add_argument(
        "--by-session",
        action="store_true",
        help="Show per-session subagent model breakdown "
             "(workflow vs agent-tool), flagging main-model matches",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON instead of text",
    )
    args = parser.parse_args()

    transcript_dir = Path(args.dir)
    sessions, errors = collect_sessions(
        transcript_dir,
        since=args.since,
        until=args.until,
    )

    if args.json:
        print(render_json(sessions, errors, args.since, args.until))
    else:
        print(render_text(sessions, errors, args.since, args.until,
                          args.baseline, args.by_session))


if __name__ == "__main__":
    main()
