# Delegation usage report - 2026-07-20

This point-in-time report asks whether observed subagents used the model families
prescribed by the tiered delegation policy in `CLAUDE.md` (PR #61, merged at
2026-07-03 16:37:23 UTC). It reads model IDs and token usage from retained
Claude Code transcripts under `~/.claude/projects/A--dev-fauxcasa/`.

The source transcript set is mutable and ages out. The command below regenerates
the current live view; it cannot reproduce this snapshot after inputs change:

```console
uv run scripts/delegation-report.py --by-session
```

## Findings

- Post-policy named-tier adoption was 19 of 21 top-level Agent spawns (90.5%)
  in the retained corpus. Observed named roles used their configured model
  families: scout on Haiku, builder on Sonnet, and reviewer on Opus.
- Session `51b03996` is the main retained same-model outlier. Its 23 direct and
  17 workflow transcript files used Fable, matching the main session, for
  1,760,756 output tokens. The main transcript contains 22 untyped top-level
  Agent calls; the extra direct transcript file is deliberately not presented
  as another correlated spawn.
- That session accounts for about 91% of retained Fable subagent output. It is
  therefore a likely major contributor to recommendations to configure cheaper
  subagent models, but this report does not inspect the recommendation logic.
- Session `cf07192c` demonstrates why model matching must use each session's
  actual main model: its main and four direct transcript files used Opus, so its
  same-model share is 100%. A Fable-only heuristic incorrectly reported 0%.
- A same-model match is not proof of accidental inheritance. Workflow stages
  can intentionally use `model: inherit`; the report flags model concentration
  for inspection rather than labeling it a leak.

## Snapshot

Generated from the retained transcript corpus on 2026-07-20 after deduplicating
progressive assistant snapshots by `message.id` and collapsing workflow resume
calls by run ID.

| Metric | Value |
|---|---:|
| Sessions analyzed | 15 |
| Top-level Agent spawns | 49 |
| Named-tier spawns | 19 |
| Workflow tool calls | 11 |
| Unique workflow runs | 9 |
| Distinct subagent API responses | 5,366 |
| Orchestrator tokens, input + output | 1,092,674 |
| Subagent tokens, input + output | 4,242,742 |
| Subagent output tokens | 3,816,555 |
| Pre-policy named-tier share | 0 of 28 (0.0%) |
| Post-policy named-tier share | 19 of 21 (90.5%) |

Subagent token totals by model:

| Model | Input | Output | Total |
|---|---:|---:|---:|
| `claude-fable-5` | 309,497 | 1,936,098 | 2,245,595 |
| `claude-sonnet-5` | 63,457 | 914,275 | 977,732 |
| `claude-sonnet-4-6` | 1,128 | 512,874 | 514,002 |
| `claude-opus-4-8` | 49,715 | 372,107 | 421,822 |
| `claude-haiku-4-5-20251001` | 2,390 | 81,201 | 83,591 |

## Method And Limits

- One API response is one distinct `message.id` within a transcript file.
  Progressive thinking, text, and tool-use snapshots retain the maximum
  observed value for each usage field instead of being summed repeatedly.
- Rows without a message ID remain distinct because they have no safe identity.
- `message.model` is the actual serving model. `attributionAgent` separately
  records the declared role. Missing model metadata is reported as `(unknown)`
  and reduces the displayed coverage percentage.
- The main-session model is the model with the most orchestrator output tokens.
  Same-model share compares subagent output against that model. It does not
  establish whether the model was inherited or explicitly selected.
- "Files" in the per-session section means transcript files, not correlated
  spawn count. Top-level Agent tool uses are counted separately.
- Token totals exclude cache creation/read overhead.
- The policy split uses PR #61's merge timestamp, not midnight on its merge day.
- Historical totals can shrink as transcripts age out, so this document is a
  point-in-time aggregate rather than a reproducible archive of raw transcripts.
