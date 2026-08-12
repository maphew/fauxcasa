# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

## Working safely: commit early and often

**Owner directive (maphew, 2026-06-14): never wait to be asked to commit.**
Commit each working increment as you go so progress survives power loss,
crashes, and context resets — re-runnable state is the goal. This is a
standing instruction and **overrides the conservative "don't commit/push
unless asked" default** in the managed Beads block below (which itself
defers to current instructions).

- **Commit** every coherent, green (tests/build pass) increment on a feature
  branch — don't batch a whole session into one commit, and don't end a
  session with uncommitted work.
- Default to a **dedicated worktree on a feature branch** to isolate work —
  always use a worktree unless asked otherwise — and use **PRs** to
  integrate; with **branches**, **beads**, and **commits** these are the
  safety nets, used freely, not only at session close.
- **Push** (`git push` / `bd dolt push`) at natural checkpoints so the net
  reaches the remote, not just local disk.
- The only override: a *current* "do not commit" / "do not push" instruction
  still wins for that session.

<!--
  MAINTAINER NOTE — the managed "BEADS INTEGRATION" block below is
  intentionally customized and diverges from its generator output. It is
  tagged `profile:minimal hash:970c3bf2`, but the contents were hand-edited to
  team-maintainer policy (see the "Active profile" line and "Agent Context
  Profiles" section inside it, plus the top-level "Working safely" section
  above), so the recorded hash no longer matches the content. Do not let `bd
  setup` / regen silently revert it: if you regenerate, re-apply these edits
  (or bump the profile and hash on purpose) rather than clobbering them. This
  note sits outside the markers so regen cannot remove it without notice.
-->
<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:970c3bf2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

**Active profile: team-maintainer** (repository opt-in by maphew, 2026-06-11).

The **commit-early-and-often** policy in the top-level "Working safely: commit
early and often" section applies under *every* profile below, so the profiles
no longer differ in commit authority (this is why the old "don't commit unless
asked" Conservative default is gone — see that section as the single source of
truth). What a profile selects instead is the **scope of end-of-session
automation and handoff** — how much the agent does for you versus reports back:

- **Conservative (report-first)**: Use `bd` for task tracking and commit
  working increments as usual, but at handoff *report* changed files,
  validation, and suggested next commands rather than auto-closing beads or
  pushing.
- **Minimal**: Keep tool instruction files as thin pointers to `bd prime`;
  otherwise follow the active profile.
- **Team-maintainer** (active): Agents also close beads, run quality gates, and
  push at natural checkpoints and session close.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Commit working increments as you go (worktree + feature branch). At
   # checkpoints and session close, also reach the remote — unless told not to:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Commit early and often to keep work safe; push at natural checkpoints. Only a current "do not commit"/"do not push" instruction holds you back.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

## Delegation policy: tier subagent models by task complexity

**Owner directive (maphew, 2026-07-03).** Sessions start on a smart model to
understand the problem and build the plan; execution is then delegated to
subagents on the cheapest model adequate for each piece. When spawning
subagents, pick the tier deliberately — do not default everything to the
session model.

Named tiers live in `.claude/agents/` — prefer them over ad-hoc spawns:

- **scout** (haiku, read-only) — searches, file inventories, "where is X",
  summarizing files, running tests/commands and reporting output verbatim.
- **builder** (sonnet, can edit) — well-scoped implementation with a clear
  spec: exact files named, acceptance criteria stated. Give it a spec, not
  a problem.
- **reviewer** (opus, read-only) — correctness review of diffs and designs
  before integration, especially builder output.

Keep in the orchestrator session (no delegation, or `inherit`): design
decisions, ambiguous debugging, anything where the spec doesn't exist yet.

Rules of thumb:
- Prefer several precisely-scoped delegations over one vague one — a
  subagent that must rediscover context you already hold wastes more than
  its model tier saves.
- Escalate rather than retry: if a scout/builder result is wrong or the
  task proved harder than scoped, redo it at a higher tier or in-session
  instead of re-spawning the same tier.
- Do **not** set `CLAUDE_CODE_SUBAGENT_MODEL` — it overrides per-spawn
  model choice and flattens this tiering.

## Workflow orchestration: standing opt-in

**Owner directive (maphew, 2026-07-03): multi-agent Workflow orchestration
is pre-authorized for every substantive task in this repo.** Treat this
section as the explicit, durable user opt-in that the Workflow tool
requires — do not wait for "use a workflow" or the `ultracode` keyword in
a prompt.

- Reach for a workflow whenever a task fans out (multiple files, fixtures,
  review dimensions, search angles), needs adversarial verification, or
  benefits from per-agent model/effort control. Work solo only on
  conversational turns, single lookups, and trivial mechanical edits where
  orchestration overhead would exceed the work itself.
- **Default token budget: +200k per substantive task.** A "+Nk" directive
  in the current prompt overrides it. The harness only sets a hard
  `budget.total` from an in-prompt directive, so workflow scripts must
  self-enforce the default:
  `const TARGET = budget.total ?? 200_000` — check `budget.spent()`
  between stages, stop spawning as the target nears, and `log()` any
  coverage dropped because of it.
- Inside workflows, tier `agent()` calls per the delegation policy above:
  `model: 'haiku', effort: 'low'` for mechanical stages; omit overrides
  (inherit) for design, judge, and verify stages.
- A *current* prompt saying "no workflow" / "keep it cheap" wins for that
  turn, same as the commit-policy override rule.

## Build & Test

This is a Python/uv project; each script is self-contained (PEP 723) and
runs via `uv run`, no separate install step. CI (`.github/workflows/tests.yml`,
`tracer.yml`) runs these gates:

```bash
uv run scripts/test_delegation_report.py -q
uv run scripts/test_picasa_db.py -q
uv run scripts/check-ingest-parity.py
QT_QPA_PLATFORM=offscreen uv run apps/desktop-python/test_tracer.py -q
git diff --quiet -- .beads/issues.jsonl   # no beads-jsonl pollution

# one-liner mirroring all of the above locally (fauxcasa-op6):
uv run scripts/preflight.py            # add --fast to skip the tracer suite
```

## Architecture Overview

_Add a brief overview of your project architecture_

## Conventions & Patterns

- **Repo tooling/scripts are self-contained Python** with PEP 723 inline
  metadata and the `#!/usr/bin/env -S uv run --script` shebang, so they run
  cross-platform with no setup beyond uv
  (https://docs.astral.sh/uv/guides/scripts/). On Windows: `uv run scripts/<name>.py`.
  Bundle binary deps as packages where possible (e.g. `imageio-ffmpeg` instead
  of requiring system ffmpeg). See `scripts/fetch-videos.py` for the pattern.
  This applies to utility scripts only — **the implementation language for the
  Fauxcasa application itself is deliberately undecided**; don't let tooling
  choices prejudge it.
- **Privacy**: real Picasa test data is personal and lives outside the repo;
  committed fixtures must be synthetic. See `bd remember` key
  `privacy-real-picasa-data` for the full rules (loaded at session start).
