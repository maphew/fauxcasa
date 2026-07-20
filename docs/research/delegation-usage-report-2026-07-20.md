# Delegation usage report — 2026-07-20

Are subagents actually running on the lighter models the delegation policy
(CLAUDE.md, PR #61, 2026-07-03) prescribes? Answered from ground truth:
every subagent transcript under
`~/.claude/projects/A--dev-fauxcasa/<session>/subagents/` records the exact
model ID that served each API call.

Regenerate anytime with:

```
uv run scripts/delegation-report.py --by-session
```

## Findings (as of 2026-07-20)

- **Tiering works post-policy.** Sessions after 2026-07-03 show healthy
  haiku/sonnet/opus mixes; the named tiers (`scout`=haiku, `builder`=sonnet,
  `reviewer`=opus in `.claude/agents/`) are honored when used.
- **One pre-policy session dominates the waste.** Session `51b03996`
  (2026-07-02→03, the day the policy merged) spawned 40 subagents — 22 large
  implementation tasks plus 17 workflow agents — with **no `subagent_type`
  and no `model` override on any spawn**. Every one inherited the session
  model (fable) and burned ~1.8M output tokens, roughly 90% of all
  session-model subagent usage ever. This single session is why `/usage`
  suggests "configure subagents with a cheaper model".
- **Residual leak to watch:** spawns using the default
  `general-purpose`/`claude` agent type without a `model:` param silently
  inherit the session model even in recent sessions (e.g. `77211c7a` at 42%
  session-model share). The report's `⚠ INHERITED` flag in section 2b
  catches this (>50% of a session's subagent output tokens on the
  main-session tier).

Caveat: transcripts age out of `~/.claude/projects/` (session `d0220d24`,
scanned 2026-07-17, was gone by 2026-07-20), so historical totals shrink
over time — this report is a point-in-time snapshot, not an archive.

## Full report output

```text
========================================================================
DELEGATION POLICY COMPLIANCE REPORT
Sessions analysed: 15
Date range:        2026-06-19  to  2026-07-17
Policy date:       2026-07-03  (PR #61, tiering guidance live)
========================================================================

-- 1. COMPLIANCE -------------------------------------------------------

Total Agent spawns:     49
  Named-tier spawns:    19  (38.8% of total)

Spawns by subagent_type (named tiers marked with *):
  (untyped)                           22
  reviewer                             9 *
  builder                              8 *
  general-purpose                      6
  claude-code-guide                    2
  scout                                2 *

Explicit model hints in Agent calls: 3
  haiku                                     3

Workflow tool calls:    11  (in 6 session(s))

-- 2. COST -------------------------------------------------------------

Token totals (input + output, no cache):
  Orchestrator (main session):     3,249,894  (461,407 in, 2,788,487 out)
  Subagents (all):                 5,085,036  (1,168,280 in, 3,916,756 out)
  Grand total (approx):            8,334,930

Token distribution by tier (subagent files only):
  builder                1,343,911  (26.4%)
  claude-code-guide          5,289  (0.1%)
  general-purpose        2,216,219  (43.6%)
  reviewer                 429,948  (8.5%)
  scout                     82,542  (1.6%)
  workflow-subagent      1,007,127  (19.8%)

Token distribution by model:
  claude-fable-5                                 2,909,437
  claude-sonnet-5                                1,048,435
  claude-sonnet-4-6                                523,442
  claude-opus-4-8                                  515,891
  claude-haiku-4-5-20251001                         87,831

Workflow runs (11 total, default budget 200,000 tok):
  [2026-07-17] ready-sweep-recon                   47,174 tok  (24% of budget)
  [2026-07-17] ready-sweep-build                  351,651 tok  (176% of budget)
  [2026-07-12] deadlock-5dk-analysis              191,359 tok  (96% of budget)
  [2026-07-02] m1-gap-audit                       431,458 tok  (216% of budget)
  [2026-07-15] pr-queue-review                    110,566 tok  (55% of budget)
  [2026-07-15] wf_d0a92b11-a6d                    110,566 tok  (55% of budget)
  [2026-07-04] ready-sweep-3                      165,129 tok  (83% of budget)
  [2026-07-03] wave1-small-fixes                  177,710 tok  (89% of budget)
  [2026-07-03] multiroot-design                   239,096 tok  (120% of budget)
  [2026-07-03] wf_d2f64340-c7b                    177,710 tok  (89% of budget)
  [2026-07-03] wave3-medium-features              339,636 tok  (170% of budget)

-- 2b. PER-SESSION SUBAGENT MODELS -------------------------------------

  (⚠ marks sessions where >50% of subagent output tokens ran on
   the main-session tier — spawns that inherited the session model
   because no subagent_type/model was set)

  [2026-07-17] 2fd8ff51  (epoch: post, session-model share: 0.0%)
    workflow    claude-haiku-4-5-20251001                 5 agents     363 calls      44,226 out-tok
    workflow    claude-opus-4-8                           4 agents     154 calls      48,900 out-tok
    workflow    claude-sonnet-5                           4 agents     817 calls     300,809 out-tok

  [2026-07-12] 3d23cc2e  (epoch: post, session-model share: 5.3%)
    agent-tool  claude-fable-5                            1 agents      26 calls      20,504 out-tok
    agent-tool  claude-opus-4-8                           3 agents     186 calls      61,855 out-tok
    agent-tool  claude-sonnet-5                           4 agents     842 calls     291,922 out-tok
    workflow    claude-fable-5                            1 agents      15 calls       6,871 out-tok
    workflow    claude-sonnet-5                           4 agents     197 calls     132,080 out-tok

  [2026-07-02] 51b03996  (epoch: pre, session-model share: 100.0%)  ⚠ INHERITED
    agent-tool  claude-fable-5                           23 agents   3,430 calls   1,539,404 out-tok
    workflow    claude-fable-5                           17 agents     628 calls     259,678 out-tok

  [2026-07-15] 75b52c52  (epoch: post, session-model share: 2.9%)
    agent-tool  claude-fable-5                            1 agents      49 calls       9,504 out-tok
    agent-tool  claude-opus-4-8                           2 agents      53 calls      15,146 out-tok
    agent-tool  claude-sonnet-5                           3 agents   1,056 calls     197,548 out-tok
    workflow    claude-opus-4-8                          14 agents     347 calls     109,872 out-tok

  [2026-07-04] 77211c7a  (epoch: post, session-model share: 42.4%)
    agent-tool  claude-opus-4-8                           3 agents     105 calls      34,637 out-tok
    workflow    claude-fable-5                            3 agents      99 calls      65,562 out-tok
    workflow    claude-sonnet-4-6                         3 agents     245 calls      54,609 out-tok

  [2026-07-03] 95f29d13  (epoch: post, session-model share: 0.0%)
    agent-tool  claude-haiku-4-5-20251001                 2 agents      44 calls       4,591 out-tok

  [2026-06-20] cf07192c  (epoch: pre, session-model share: 0.0%)
    agent-tool  claude-opus-4-8                           4 agents     138 calls      39,249 out-tok

  [2026-07-03] e1260aa2  (epoch: post, session-model share: 16.9%)
    agent-tool  claude-haiku-4-5-20251001                 2 agents     191 calls      24,600 out-tok
    agent-tool  claude-opus-4-8                           1 agents      18 calls      12,161 out-tok
    agent-tool  claude-sonnet-4-6                         1 agents      93 calls      18,126 out-tok
    workflow    claude-fable-5                            6 agents      53 calls     114,765 out-tok
    workflow    claude-haiku-4-5-20251001                 4 agents      65 calls       8,688 out-tok
    workflow    claude-opus-4-8                           3 agents     147 calls      53,118 out-tok
    workflow    claude-sonnet-4-6                        17 agents   1,604 calls     448,331 out-tok

-- 3. QUALITY (proxies) -------------------------------------------------

Reviewer spawns: 9

Escalation events (heuristic): 4
  [Heuristic: consecutive Agent calls within same session where tier rank increases (scout→builder→reviewer). False positives possible when two unrelated tasks coincidentally run in order.]
  2026-07-12 3d23cc2e... builder -> reviewer: 'Implement 5dk deadlock fix' -> 'Review 5dk deadlock fix diff'
  2026-07-12 3d23cc2e... builder -> reviewer: 'Implement multiroot .c cache binding' -> 'Review multiroot .c diff'
  2026-07-15 75b52c52... builder -> reviewer: 'Serial branch-update chain, starting #66' -> 'Verify PR #70 merge composition'
  2026-07-03 e1260aa2... scout -> reviewer: 'Map wave-1 code touch points' -> 'Review spec rulings diff'

Metrics requiring manual triangulation (NOT computed here):
  • cost_per_merged_pr — divide total tokens by PRs merged (git log --merges | wc -l)
  • post_merge_fixups — search git log for 'fix(*)' commits on same beads after merge
  • bead_reopens — bd list --status=reopened | wc -l (requires bd/dolt)
  • These need manual triangulation with ccusage, GitHub, and dolt — not computed here.

-- 4. BASELINE (pre / post 2026-07-03) ---------------------------------

  PRE-policy  (baseline):
    Sessions:           6
    Agent spawns:       26
    Named-tier spawns:  0  (0.0%)
    Reviewer spawns:    0
    Workflow runs:      1
    Tokens (approx):    4,347,558

  POST-policy (target):
    Sessions:           9
    Agent spawns:       23
    Named-tier spawns:  19  (82.6%)
    Reviewer spawns:    9
    Workflow runs:      10
    Tokens (approx):    3,987,372

-- COUNTERFACTUAL CAVEAT ------------------------------------------------

  No single number proves effectiveness. What to look for:
  • COMPLIANCE high (named-tier share rising post-policy)
  • COST per merged PR trending down (triangulate with ccusage + git)
  • QUALITY flat or up (reviewer share, fewer post-merge fix commits)
  • If all three trend in the right direction together, the policy
    is likely working — but the repo is also growing, so normalise
    cost by PR count, not absolute tokens.

  Data gaps: model hints are often absent from Agent calls (the
  subagent's actual model is in attributionAgent/model in its own
  transcript). Token counts exclude cache write/read overhead.

========================================================================
```
