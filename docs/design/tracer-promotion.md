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

## Amendment — 2026-09-13

Fauxcasa 0.1.0 ships from `apps/desktop-python/` — the tracer stops being
disposable evidence at this release, but the directory does not move yet.
Status against the gate checklist:

- **Item 1 (M1 gate green).** Clause 1 (N4 budgets on the 100k synthetic
  library) is mostly green; min-zoom scrolling on high-DPI displays remains
  over budget (`fauxcasa-q6l.27`). Clause 2 (survey cross-check, zero
  ingest loss on synthetic corpora) is green in CI. Clause 3 (owner
  confirmation on the family archive) has not yet been recorded — see
  `docs/m1-gate-confirmation.md` (`fauxcasa-6g8`).
- **Item 3 (APP_NAME honored everywhere)** — done for 0.1 (fauxcasa-ez2's
  identity work: window titles, cache directory names, config keys,
  bundle/installer names, CI artifact names, and `fauxcasa-tracer.spec` all
  derive from `APP_NAME`/the release identity constants; nothing left
  hard-coding "tracer").
- **Item 5 (README flip)** — done for 0.1. `apps/desktop-python/README.md`'s
  "Status: experiment… evidence, not yet the application" paragraph is
  replaced with product framing, and the "Deliberate tracer shortcuts"
  section is now "Known simplifications (tracked)", each item tagged with
  its bead or `product-accepted`.
- **Items 2 (rename out of `apps/desktop-python/`), 4 (`tr()` i18n
  externalization pass), 6 (CI rename), and 7 (test suite carried whole)**
  — deferred to the 0.2 promotion PR. Nothing in 0.1 depends on them.

**Waiver of the same-PR rule (Sequencing note, above).** That rule ties the
rename (item 2) and the README flip (item 5) to one PR, so no commit range
calls the code an experiment while it lives in a product home, or vice
versa. For 0.1, item 5 lands **without** item 2: the README flip here is a
release-notes truth requirement (0.1.0 ships and must not describe itself
as "not yet the application"), not a code-home statement — the code still
lives in `apps/desktop-python/`, and the README says so. The rule is waived
for this one case; it resumes governing the eventual 0.2 rename + any
further README changes tied to it.
