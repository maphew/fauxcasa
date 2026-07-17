# Tracer-to-product promotion gate (fauxcasa-mqi)

**Status:** defined 2026-07-03 (spec §10 item 21). Overturnable by argument,
like every delegated ruling.

**The problem.** The stack decision is confirmed and every M1 feature is
landing in `apps/desktop-python/`, but that directory's README still says
"experiment… evidence, not yet the application." Nothing tracked when or how
the tracer stops being disposable. Left alone, M1 would complete — and M2
would start *writing user libraries* — inside code the project itself labels
throwaway. That is the failure this gate designs away.

## When: at M1 exit, before any M2 write-path work

Promotion is the act that closes M1. The bright line is **writes**: M1 is
read-only, so "evidence" is an honest label for it; M2's first deliverable
touches a user's `.picasa.ini` files. Code that writes a treasured family
archive must have shed the disposable label first — not as ceremony, but
because the label governs real behavior (how carefully we refactor, what the
README licenses contributors to assume, whether shortcuts are acceptable).

**Promotion is not a rewrite.** The tracer graduates as-is; the §9 M0 note
already says architecture stays swappable per N3. The gate below is a
checklist of debts the "experiment" label deliberately licensed, now called
in.

## The gate checklist

Promotion is complete when all of these hold. Each becomes a bead under the
promotion epic when promotion starts.

1. **M1 gate green.** The §9 M1 gate (N4 budgets on the 100k synthetic
   library, survey cross-check zero-loss, owner confirmation on the family
   archive) passes. Promotion never front-runs the milestone it closes.
2. **Name and home.** The code moves out of `apps/desktop-python/` into the
   product's real home (final path decided at promotion; `apps/` implies a
   sibling experiment that no longer exists). One rename commit, history
   preserved (`git mv`).
3. **APP_NAME honored everywhere.** The spec's name note says the app name
   is a single swappable constant. Audit: window titles, cache directory
   names (`cache/tracer-cache/`), config keys, bundle/installer names, CI
   artifact names, the `fauxcasa-tracer.spec` PyInstaller file — all derive
   from `APP_NAME`, none hard-code "tracer" or "fauxcasa".
4. **i18n externalization pass.** Per the fauxcasa-64z ruling (§5
   Maintenance): the tracer's exemption from "string externalization from
   day one" *ends at promotion*. Every user-visible literal moves behind the
   chosen mechanism (Qt `tr()`/`QCoreApplication.translate`, given the
   stack). Full localization stays post-v1; the externalization is what
   promotion owes.
5. **README flip.** The "Status: experiment… evidence, not yet the
   application" paragraph is replaced by product framing. The deliberate
   tracer-shortcuts section converts to beads (each shortcut either
   graduates to a tracked debt or is closed as product-accepted).
6. **CI renamed and preserved.** `tracer.yml` (and its job/artifact names)
   follows the rename; the gate matrix (ubuntu + windows) carries over
   unchanged. Bundle smoke stays green through the move.
7. **Test suite carried whole.** `test_tracer.py` moves and keeps passing.
   Splitting the monolith is *not* a promotion criterion — it's ordinary
   refactoring, allowed before or after.
8. **Beads bookkeeping.** A promotion epic exists; per-file/-surface renames
   and the checklist above are its children; the tracer README's shortcut
   list is reconciled against open beads so nothing labeled "shortcut" is
   silently forgotten.

## What promotion does NOT require

- No installer/packaging beyond what M1 already ships (the frozen bundle
  exists; a real installer is later packaging work, not a promotion
  criterion).
- No module re-architecture, no test split, no performance work — those are
  ordinary beads, not gate criteria.
- No name decision. "Fauxcasa" remains provisional; criterion 3 is exactly
  what makes the eventual real name a one-constant change.

## Sequencing note

Criteria 2–8 are a few days of mechanical work with CI as the net. The only
scheduling constraint worth stating: do the rename (2) and README flip (5)
in the same PR, so no commit range exists where the code lives in a product
home while calling itself an experiment, or vice versa.
