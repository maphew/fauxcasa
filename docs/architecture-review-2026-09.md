# Architecture review, September 2026: proportion, balance, and the road to a tool people use

**Status:** written 2026-09-16 against `main` at `b6ddcc2` (bead
fauxcasa-ek0). The owner asked four questions: is the project well
proportioned and balanced; does it avoid over-investing in technical
eddies and backwaters while under-investing in the work people touch and
feel; will it be an effective tool for managing photos; will folks be
happy to use it because it works and does the jobs. This document answers
them with numbers from the repository and then proposes what to do about
the answers.

Every recommendation here is a proposal under the spec's own rule ("change
it by arguing, not by silently diverging", `docs/product-spec.md`).
Follow-up beads get filed after the owner ratifies or amends the
proposals, not before. Appendix B holds the commands that produced every
number, so the review can be re-run in three months and compared.

Intended readers: the owner, and the agents that will do most of the
work. Section "Plain-language summary" is for the owner. Sections 8 to 11
are the parts an agent should read before picking up a bead.

---

## Plain-language summary (for the owner)

The foundations are unusually good. In three months the project produced a
researched spec, a byte-level understanding of Picasa's files checked
against the real program running under Wine, a reader that recovers
faces, albums, captions, stars, crops and hidden state from a Picasa
library better than anything else I know of, a fast grid over 100,000
photos, honest release notes, and a working build pipeline for Windows
and Linux. The test suites are green, CI takes under four minutes, and
the documentation says what the software actually does. That is rare.

The house has no rooms yet. After three months and 497 commits, a person
can look at their library but cannot change anything in it. They cannot
add a caption, make an album, rotate a crooked photo, export a smaller
copy for email, or move a bad shot to the trash. The one thing they can
change, a star, is stored in Fauxcasa's private cache folder, which the
spec's own rule N3 says must never happen. The release notes say so
honestly, but honesty about a gap is not the same as closing it.

The effort went disproportionately into layers nobody sees. About half
of all code churn in the app directory went into a single 20,000-line
test file. The Windows-only decode sandbox took roughly 9,400 lines of
production and test code plus 2,400 lines of design and spike material,
survived nine recorded operating-system surprises, and today protects
only the formats with the safest decoders, on the platform the owner does
not use daily. Five beads and a measurement campaign chased scrolling at
the smallest thumbnail size on a 4K monitor at 125% scaling over 100,000
photos, an edge case of an edge case that is still open. A multi-folder
library model shipped with no menu item to use it. Two thousand lines of
tooling that report on how AI agents are used live in the product repo
and run in its CI.

None of those were bad work. Each has a document explaining why it was
done and each was done carefully. The problem is proportion. The spec's
trust ladder (M1 look, M2 change, M3 edit, M4 live) is right, but M1 took
three months and the next three rungs are each at least as big. At the
current mix the tool people would be happy to use is a year or more away.

Two things should happen this week, before any new feature. Open the real
family archive in the build that exists; the runbook and the redacting
script have been ready since July 23 and nothing has been recorded. And
publish the 0.1 release that has sat as a draft since September 13; the
README's download link currently returns "not found".

After that, the proposal is to rebalance toward the jobs, in this order:
stars, captions, keywords and hidden flags written back into `.picasa.ini`
so Picasa sees them; albums and delete-to-trash; a basic edit room built
on the edit-recipe grammar Picasa already stores and this project already
parses; export with resize and watermark. Each step is a release. Each
release is something a person can do that they could not do before. The
sandbox, the performance chase, the multi-folder model, and the
agent-usage tooling get frozen at their current scope until those land.

Will people be happy to use it? Today, no, because it does nothing Picasa
under Wine does not already do. In six months, if the sequence above
lands with the same care the ingest side got, yes, because it will be the
only program that reads a Picasa library faithfully, writes it back in a
form Picasa still accepts, runs natively on Linux and Windows, and never
phones home. That combination is the product. The ingest work already
built the moat; the write side is what makes it a tool.

---

## 1. The four answers

**Is it well proportioned and balanced?** No. It is well built and poorly
proportioned. By lines of production code (app plus scripts) the split is
roughly 40% plumbing and safety (sandbox, perf harnesses, gates, agent
tooling), 23% ingest and compatibility (which users do feel, as
fidelity), 33% user interface and video playback, and 5% documentation
tooling. By lines added over the history the sandbox alone drew as much
work as the grid, viewer and sidebar combined once its tests are counted.
By closed beads the split is closer to even, but the user-facing beads
were mostly small (a menu, a badge, a key) while the plumbing beads were
large. Test code equals production code in volume, which is healthy, but
four fifths of it sits in one file.

**Does it avoid technical eddies?** No. Section 5 names five. The largest
is the decode sandbox. The clearest is agent-usage reporting living in the
product's CI. The most surprising is the min-zoom scrolling chase, because
the default-zoom numbers were already green.

**Does it under-invest in what people touch?** Yes. Section 6. No writes,
no edits, no export, no trash, no album creation, no add-folder menu, and
the archive it exists for has never been opened in it.

**Will it be an effective tool, and will people be happy?** Not yet, and
not until the write path exists. The ingredients for "yes" are all
present: the ingest fidelity, the oracle harness that can prove Picasa
reads what we write, the recipe grammar already parsed, the performance
headroom. Section 8 describes the architecture that turns those into a
tool and section 9 sequences it.

---

## 2. What was measured

Everything below was computed at `b6ddcc2` on 2026-09-16 with the
commands in Appendix B. Two read-only scout agents verified the code
claims in sections 5.1 and 6.1 by reading the routing code and the
persistence code directly; their findings matched the design documents
and are cited by file and line.

| Measure | Value |
|---|---|
| Age | first commit 2026-06-11, 97 days |
| Commits | 497 (179 in June, 56 in July, 35 in August, 227 in September) |
| Pull requests | 162 |
| Beads | 220 filed, 198 closed, 16 open, 4 in progress |
| App production code (`apps/desktop-python`, excluding tests) | 26,409 lines across 34 modules |
| App test code | 25,145 lines across 5 files |
| Scripts | 11,588 lines production, 2,448 lines tests |
| Docs | 96 tracked files; the spec is 1,076 lines |
| Oracle fixtures | 37 differential fixtures (766 files) |
| CI wall time per push | tests about 1 min, tracer about 1.5 min, bundle about 3 min |
| Release state | `v0.1.0-rc2` draft since 2026-09-13; `/releases/latest` returns 404 |
| Release artifact size | 132 MB Windows zip, 146 MB Linux tarball |

---

## 3. Where the effort went

Three lenses, because each one hides something the others show. Current
line count says what exists. Lines added over history says what was
worked on (a file rewritten three times shows three times). Closed beads
say what the planning process thought it was doing.

### 3.1 By layer

Production lines are current counts. "Added" is the sum of insertions to
the production files in non-merge commits over the whole history (tests
excluded, so the rows compare like with like). Classification of beads by
title is approximate (Appendix C).

| Layer | Production lines | Test lines (current) | Production lines added (history) | Closed beads (approx.) | Do users feel it? |
|---|---:|---:|---:|---:|---|
| Grid, viewer, sidebar, tray, slideshow, peek, inspector, keymap, theme, icons | 11,200 | (in `test_tracer.py`) | 12,400 | 60 | Yes, directly |
| Ingest and compatibility (catalog, thumbcache, library, metareader, inmeta, db3rescue, volumes, filetypes, cropmap, locate, applog, `picasa_db.py`) | 8,600 | 2,600 | 9,700 | 36 | Yes, as fidelity |
| Decode sandbox (decodesvc, decodesvc_win, worker, facade) | 5,000 | 4,400 | 5,500 | 10 | No (invisible when it works; visible when it degrades) |
| Video playback seam (videostream, videoload) | 1,300 | (in `test_tracer.py`) | 1,300 | 3 | Yes |
| Performance harnesses (bench_scroll, vsync_probe, perf-canary, the Python balloon, synthetic library and thumbcache builders, measurement scripts) | 4,900 | 0 | 5,200 | 14 | No |
| Agent-usage tooling (delegation-report, daily-report) | 1,700 | 460 | 1,900 | 5 | No |
| Research, oracle harness, gates (confirm-archive, ingest-parity, oracle-diff, sentinel experiment) | 2,700 | 700 | 2,900 | 45 | Indirectly |
| Documentation tooling (demo library, gallery, dataset and video fetchers, preflight) | 1,950 | 0 | 2,200 | 4 | Yes, through the docs |
| `test_tracer.py` alone | 0 | 20,400 | 36,700 added | (tests for the rows above) | No |

Two numbers stand out. The single test file received 36,702 added lines,
51% of all lines ever added under `apps/desktop-python`. And the sandbox's
production code is 19% of the app directory today (5,022 of 26,409
lines); with its own tests it reached about 10,000 added lines, close to
the 12,400 added to the grid, viewer and sidebar that every user touches.

### 3.2 By time

The sandbox work has a distinct calendar: threat model on 2026-06-12,
design on 07-02, implementation and three PRs between 08-11 and 08-17
(five review rounds on the first), wiring into the app on 09-13, and
ACL-degrade fixes on 09-15. A third of August's 32 non-merge commits were
sandbox hardening; the rest were video playback, the inspector, the
non-blocking first run and the 4K min-zoom measurements.

Every one of September's 227 commits landed between the 13th and the
16th, 135 of them on the 13th alone: the "4-day push" to a release
(fauxcasa-ez2). It produced most of the visible polish (theme, icons,
menus, welcome dialog, gallery, release notes) in four days. Those days
show what the project can do when it points at what users see. They also
show the cost model: a crunch, then a draft release that stalled on two
owner actions.

### 3.3 What the closed beads say

Of 198 closed beads, by title:

- about 60 changed something a user can see or do (the q6l browse epic,
  the visual polish, format support, notices);
- about 36 improved what a user sees without a new verb (the cam ingest
  epic: faces, contacts, .pal, db3 rescue, dates, GPS, ratings, crops);
- about 75 were plumbing, tests, CI, process, or fixes to tests (the i92
  sandbox tree, ed5 gates, multi-root, perf, flaky-test hunts, review
  sweeps, repo hygiene);
- about 25 were research, spec, and oracle work.

Every M1 read-only requirement got its bead and most got closed. Nothing
in the closed list writes to a user's library. The first write bead
(fauxcasa-lgg) was filed on 2026-09-15.

---

## 4. What a user can do today, against the jobs

The spec's own summary of who this is for: people "who want to find, fix,
tag, and share" decades of family photos. The one field report in the spec
named the treasured features: watched folders, watermarks, export to
different resolutions, tagging, sorting, collections.

| Job | Picasa 3.9 | Fauxcasa 0.1 | Gap |
|---|---|---|---|
| Find (browse folders, albums, people; search; view; slideshow) | yes | yes, and faster on large libraries | the custom folder order Picasa kept is not shown |
| Tag (stars, captions, keywords, faces, hide) | yes | stars only, stored in the cache folder, invisible to Picasa | everything else read-only |
| Fix (rotate, crop, straighten, fill light, one-click fixes, undo) | yes | displays Picasa's crops; no editing | the whole edit room |
| Share (export with resize, watermark, email) | yes | no | all of it |
| Organize (albums, move, rename, delete to trash, watched folders) | yes | read-only albums; multi-folder via command line | create/move/delete/rename absent; no add-folder menu |
| Import from camera or card | yes | no | M4 |
| Back up | yes (to disc) | no | M4 |

The menus tell the same story. File has two items (Library, Exit). View
has zoom, Show hidden, Info, Flat folders, a Stars threshold submenu, Clear
stars, Play. Tools has File Types. Help has Shortcuts and About. The
sidebar context menus offer Flat folders and Sort by date/name/size. That
is the full verb list of a photo manager after three months: look, filter,
star.

The keymap is candid about it. `keymap.py` reserves `0` to `5` and `X`
for M2 and lists Ctrl+3, Ctrl+R, Ctrl+M, Ctrl+N, Ctrl+T, Ctrl+E and
Ctrl+P as "planned"; pressing one shows a notice naming the milestone.
That is a good pattern for honesty. It is also a list of what the product
is missing, printed by the product.

---

## 5. The eddies

An eddy here means work that was done well, is documented, and still
absorbed more of the project than its contribution to the jobs above
justifies at this stage. Each entry says what it cost, what it bought,
why it counts as an eddy, and what to do.

### 5.1 The Windows decode sandbox

**Cost.** `decodesvc_win.py` (3,300 lines, 271 of which call into ctypes,
kernel32, advapi32 or userenv), `decodesvc_worker_win.py` (851),
`decodesvc.py` (409), `decodefacade.py` (462): about 5,000 production
lines. Tests: `test_decodesvc_win.py` (3,117 lines, 149 tests),
`test_sandbox_e2e.py` (575 lines, 8 tests), `test_decodefacade.py` (684
lines, 23 tests). Design and spike material: `docs/design/decode-service.md`
(666 lines), `docs/decode-threat-model.md` (287), the AppContainer spike
(1,412). Nine numbered operating-system gotchas recorded as memories
(uv's python.exe trampoline, DETACHED_PROCESS versus conhost, window
station ACLs, the pre-bytecode stdout diagnostic, NUL device denial on
Server 2025 runners, concurrent profile creation races, and more). Five
review rounds on PR 110. A bug tail after wiring: TOO_LARGE responses
written to the thumbnail cache as permanent zero-length tiles
(sandbox-arena-edge-cap), a 64-megapixel viewer cap that the in-process
path never had, softer thumbnails on tight Picasa crops, ACL grant
failures on Dev Drive and ReFS volumes that silently degraded the sandbox
until fauxcasa-yfq made the failure visible and added a staged copy of
the worker under `%LOCALAPPDATA%`.

**Bought.** On Windows, a malformed JPEG, PNG, GIF, BMP, 8-bit TIFF, WebP
or TGA that hijacks a Qt image plugin gets a process with no file system,
no network and a kill-on-close job object. That is real, and it is the
threat model's stated asset: the archive itself.

**Why it is an eddy.** The threat model's decision reads "no exceptions
for simple formats" and "all decoding and metadata parsing of original
files happens in a sandboxed worker pool, on all three platforms, from
M1". The implementation is the mirror image. Per
`thumbcache._index_one` (lines 713 to 766) and
`viewer.load_original_oriented` (lines 251 to 302), RAW, PSD, 16-bit TIFF,
HEIC/HEIF and video are routed in-process before the sandbox check, and
only the remaining stills reach `decodefacade.decode()` (route `"still"`
only; every other route raises `NotSandboxed`). The scan-time header
sniff in `catalog._image_size` (line 549) calls `QImageReader` directly.
So the decoders with the loudest recent CVE histories, libde265 behind
HEIC, LibRaw, FFmpeg via PyAV, and Pillow's PSD and TIFF codecs, all run
with full authority, while Qt's JPEG and PNG plugins, the most fuzzed
decoders on earth, get the locked room. On Linux and macOS
`decodefacade.ensure_started` (line 282) sets in-process unconditionally;
no Linux transport code exists anywhere in the tree, only design prose.
The owner runs Linux daily.

The sandbox also introduced user-visible regressions that the in-process
path never had (the 64 MP cap, the tight-crop softness) and a support
surface ("sandbox degraded" on Dev Drives) that a family user cannot act
on.

**What to do.** Freeze the scope. Keep what exists, it works and is
tested. Do not build the Linux transport, the RAW/PSD/HEIC/video routes,
the metadata-in-sandbox move (i92.4), the hostile corpus and fuzz gates
(i92.5) or any wasm work until the edit room has shipped. Amend the
threat model's decision paragraph to describe what ships (the "in-process
decoders (documented exception)" section already lists three of the five
gaps; add RAW and video and the header sniff, and say Linux has none). If
Linux parity is wanted later, timebox one week for a bubblewrap wrapper
around the existing wire protocol in `decodesvc.py`, and stop if it runs
over. Revisit the whole posture when the app starts accepting files from
outside the library (device import, M4), because that is when untrusted
bytes actually arrive.

### 5.2 Chasing minimum-zoom scrolling on a 4K monitor at 125%

**Cost.** Beads q6l.14, q6l.26, q6l.27 (still open), q7m, k5p, u8c, ncv,
5br, gtr, w7x, ed5.10, ed5.13, plus `bench_scroll.py` (635 lines),
`vsync_probe.py`, `parse-wayland-cadence.py`, `run-balloon-bench.py`,
three result files under `docs/research/ncv-results/`, a 290-line
validation report, and four memories on Wayland compositor cadence,
headless weston, screen-lock stalls and window occlusion.

**Bought.** Real knowledge: Qt's raster surface is unthrottled on Mutter,
so paint interval is not a vsync signal; the honest metric is frame
production time; scaled-in-paint tiles never re-decode on zoom; the fcache
v2 level pick at fractional DPR overshoots by 16x at min zoom. The
default-zoom numbers are green on every configuration measured.

**Why it is an eddy.** The remaining violation is the smallest thumbnail
size, on a 4K display at Windows 125% scaling, flick-scrolling 100,000
photos at 2.5 screens per second. The release notes already carry the
honest line ("can stutter on 4K and other very sharp screens with very
large libraries"). The §7 budgets are a good discipline; this particular
corner has consumed five beads and remains open because the definitive
run needs an idle machine with a real display, which the shared dev box
rarely is.

**What to do.** Close q6l.27 with the release-note line as its resolution.
Keep `perf-canary.py` in CI (it is cheap and catches order-of-magnitude
regressions). Do not start another measurement campaign until either a
user reports the stutter or reference-class hardware appears. Leave the
balloons where they are; they are frozen and their CI job is
path-filtered.

### 5.3 A multi-folder library model with no way to use it

**Cost.** `library.py` (945 lines), `volumes.py` (219), a 628-line design
document, seven sub-beads under ed5.7, a cold-start benchmark, and catalog
version bumps.

**Bought.** A library is now a home directory plus N roots with minted
ids, volume UUIDs, offline tolerance and per-root caches. The field report
in the spec (watched folders as a treasured feature) says the need is
real, and the spec's §3 requires it.

**Why it is an eddy.** It shipped with no menu. The release notes say
"adding a second drive or folder to a library takes a few typed
instructions that most people will not need". The plumbing arrived a full
milestone before the two menu items (Add folder, Remove folder) that
would let a person touch it, and before writes, which matter more.

**What to do.** Add the two menu items in the 0.3 release (section 9);
they call functions that already exist. Do not extend the model
(cross-root move detection, volume self-heal, per-root policies) until
watching lands in M4.

### 5.4 Agent-usage bookkeeping in the product repository

**Cost.** `scripts/delegation-report.py` (1,217 lines) and its 248-line
test, `scripts/daily-report.py` (476 lines), its 213-line test, and
`daily-report.toml`; a `delegation-report` CI job that runs on both
operating systems on every push; `docs/reports/delegation-usage-report-2026-07-20.md`;
and the beads that produced them (loq, dvv, e97, nn9, hi2.1).

**Bought.** Evidence that the tiered-subagent policy was followed 90% of
the time after it was adopted, and a daily status digest.

**Why it is an eddy.** These tools measure how the agents work, not how
the product works. They read Claude Code transcripts under the user's
home directory. A contributor cloning the repo gets 1,900 lines of code
and a CI job that have nothing to do with photos. Bead fauxcasa-nn9
already proposes moving `daily-report.py` to the owner's dotfiles.

**What to do.** Move both scripts and their tests out of the repository
(dotfiles or a separate tools repo), delete the CI job, keep
`daily-report.toml` only if the external script still reads it. Keep the
tiering policy itself in `AGENTS.md`; the policy is useful, the
measurement tooling is not product.

### 5.5 Review rounds and fan-outs

**Cost.** PR 110 had five review rounds. One audit workflow spawned 278
agents, spent about 4.9 million tokens and lost its synthesis to rate
limits (workflow-fanout-cost-lesson). The July 20 delegation report
counted 4.2 million subagent tokens across the 15 sessions it could still
read. The Codex account's monthly quota is exhausted until October 13.

**Bought.** The review culture is real and it pays: a reviewer caught a
regression test that was red for the wrong reason (fauxcasa-9pr), the
zero-length-tile bug, three real problems on PR 140, and the concurrent
profile-creation race in the sandbox warm-up.

**Why it is an eddy.** Not the reviews. The uncapped shape of them. The
lesson memory already says it: cap fan-outs, verify only P1 and P2
findings adversarially, keep the expensive models for judgment.

**What to do.** Write the caps into `AGENTS.md`: two review rounds per PR
unless a P1 is found; adversarial verification only for P1 and P2
findings; a token target inside each bead's description for substantive
work. Section 10 has the rule.

---

## 6. The under-investments

### 6.1 Nothing writes to the library

Every persisted user choice in 0.1 lands in Fauxcasa's cache folder:
`stars.json` via `starstore.save_star_overrides` (starstore.py:72), view
preferences via four `save_*` functions that all funnel into
`_write_library_config` (main.py:505), the remembered library and File
Types into a second `config.json` at the cache root. All eleven write
sites are atomic (temp file plus replace), which is good engineering. None
of them touches `.picasa.ini`, XMP, or any file Picasa reads. Grepping the
tree confirms that the only code emitting `.picasa.ini` is the synthetic
library and demo library generators and the tests (fauxcasa-lgg.1 says the
same).

The spec is explicit that this violates the constitution: N3 says "zero
database-only user state", and the release notes admit that deleting the
cache folder forgets the stars. The star toggle also does not exist in the
slideshow (`slideshow.py` keyPressEvent handles pause, prev, next and exit
only), although the spec makes the slideshow a triage pass.

The M2 epic (fauxcasa-lgg) was filed on 2026-09-15 with a design bead
(lgg.1) and a stars-writer bead (lgg.2). The design bead is well specified
(read-modify-write preserving unknown keys, atomic replace, read-back
verification, drift refusal, freshness interaction, tier-2 placement,
round-trip fixtures plus oracle differential). It is the right first step.
It should have been the first step in July.

### 6.2 No edit room

The spec calls the durable, named, per-photo undo history "the single
strongest emotional peak in the entire tutorial corpus". Nothing in the
tree implements an edit. What does exist is a head start most projects
never get: `catalog.py` already parses `filters=`, `crop=`, `redo=`,
`text=` into `Photo.edits` and resolves the current crop into
`Photo.crop`; the grid and viewer bake that crop; and the oracle corpus
holds fixtures 004 and 005 (crop unsaved and saved to disk, with the
`.picasaoriginals` stash), 006 and 007 (rotate), 018 and 019 (text), 027
(revert a baked edit) and 034 to 037 (one-click enhance, fill light,
straighten, tuning), each a byte-level record of what Picasa writes for
that operation. Section 8.2 builds on this.

### 6.3 No export, no trash, no album, no add-folder

The UI inventory ranks Export, Delete-to-trash, Save/Revert and Folder
Manager as rank 5 (core). Export is also the field-reported treasured
feature (multiple resolutions, watermark) and the family sharing path
(the email preset). None exist. Delete to trash needs one small
dependency (`send2trash`) and one confirmation dialog. Album creation
needs the write layer from 6.1 plus a sidebar drop target. Add folder
needs a menu item over functions in `library.py` that already exist.

### 6.4 The archive this exists for has never been opened in it

The M1 gate has three clauses. Clause 3, "owner confirms the same on the
family archive", has been a pending owner action since 2026-07-23
(fauxcasa-6g8). `scripts/confirm-archive.py` (974 lines, 685 lines of
tests) exists precisely to make that safe: it never prints a path or a
name, only counts and hashed tokens. `docs/m1-gate-confirmation.md`
carries the runbook and an empty confirmation record.

This is the most important gap in the project. Every decision about what
people feel has been made against a synthetic library and a demo library
of stock photos with invented names. The owner, whose archive is the
founding motivation, has not been able to use the browser on it. Any
number of small wrongnesses (a folder-name encoding, a face merge that
misfires on real data, a slow network path) are invisible until then.

### 6.5 The release is a draft and the download link is dead

`v0.1.0-rc2` has been a draft since 2026-09-13. Because the only
published releases are marked pre-release, `/releases/latest` returns
404, and the README tells readers to download from it. The blockers are
fauxcasa-ez2.11 (a manual pass on a real display, which the shared dev box
has not been idle enough to run) and 6g8. The notes are honest, the
artifacts are built and attested. Publishing it as a pre-release now
costs nothing and makes the README true.

---

## 7. Structural debt that will slow the next year

These are not eddies; they are shapes that made sense for a tracer bullet
and now tax every change.

### 7.1 One 20,000-line test file

`test_tracer.py` is 20,402 lines and 628 tests, run as a script that
executes as `__main__` and is imported again as a module by pytest. Every
test shares one QApplication. The memory file records at least six
incidents that were test-interaction artifacts rather than product bugs,
each costing a session: the peek/slideshow deadlocks (5dk), the 2%
access violations (tlv), a hard-stop timer from one run killing a later
one (9pr), two vacuous log assertions on a non-propagating logger (47f,
xf2), a bare event loop that Qt's quit flag turned into a no-op (bw1), and
a shiboken wrapper Heisenbug that reproduced only under the script runner.
The regression-test-in-isolation rule exists because a new test detected
its neighbour's leaked timer instead of the bug.

The promotion document says splitting the monolith is "ordinary
refactoring, not a gate". I would make it the first refactoring, because
it is the cheapest velocity win available: two days of builder work to
move tests into `tests/` by module with a `conftest.py`, run under plain
`pytest`, one process per file in CI so a leaked timer cannot cross files,
with the script entry kept as a thin wrapper for the documented commands.

### 7.2 One 5,500-line main.py

`MainWindow` is 2,786 lines with 84 methods; its `__init__` is 540 lines;
the sidebar is built inline in a 294-line method and rebuilt by swapping
in a fresh tree; `main()` is 841 lines of argument parsing, scripted-run
flags and wiring. Per-library preferences are four load/save pairs at
module level plus `starstore.py`, all writing the same `config.json`.

Extract three modules: `sidebar.py` (the tree model and view),
`librarystate.py` (every per-library persisted preference, the stars, and
the pending-write journal from 8.1, in one place with one atomic writer),
and `cli.py` (argument parsing and the scripted-run flags). Do it as the
first step of the write work, because the write layer needs a single home
anyway and today there is none.

### 7.3 Whole-file saves and no action journal

`save_catalog` writes the whole zstd-compressed catalog atomically. That is
right for a disposable cache. User actions, though, mutate in-memory
objects and rewrite `stars.json` wholesale. N5 (crash-safe, transactional)
needs an append-only journal of user actions so a kill at any point loses
at most the in-flight action, and reconcile can replay. Bead lgg.2 already
calls this the "pending-write journal". It should be built once, in
`librarystate.py`, and used by every write feature after it.

### 7.4 The promotion gate: keep two items, drop one, add one

The tracer-promotion checklist still owes a directory rename out of
`apps/desktop-python/`, a `tr()` externalization pass (there are currently
zero `tr()` calls in 26,000 lines), a CI rename, and "test suite carried
whole". The rename buys users nothing; drop it, the directory name is
fine. The `tr()` pass is a day of mechanical work that buys nothing until
a translator exists; keep it but schedule it with the `main.py` split,
since the same files get touched. Add the test split from 7.1 to the
list, and remove "carried whole".

---

## 8. The architecture for the next year

The read side is done and good. The next year is the write side. Two
components carry almost all of it.

### 8.1 The write layer is the spine

One module, one path, for every change a user makes. It is what lgg.1
specifies, with the journal from 7.3 folded in:

1. **Sidecar writer.** Read `.picasa.ini`, modify only the keys we own,
   preserve every byte we do not understand (unknown sections and keys,
   order, comments, blank lines, CRLF, the mixed encodings cam.14 found),
   write to a temp file in the same directory, `os.replace`, read back and
   re-parse through the existing ingest to verify. Refuse and report if
   the file's size or mtime changed since we read it.
2. **Tier-2 native state.** `library_state_dir` already exists per
   library. Native state Picasa has no home for lives there in
   human-readable files: exact star counts, reject flags, album order,
   manual sort order, ignored faces.
3. **Action journal.** Append-only, one line per user action, fsync'd
   before the UI acknowledges. Marked applied after the sidecar write
   verifies. On start, replay unapplied entries. This is the N5 kill-fuzzer
   substrate and the retry queue for a read-only sidecar.
4. **Status surface.** A per-folder health mark (read-only ini, failed
   write, drift refused) in the sidebar and status bar, and the failures
   in the import-notes dialog. N7: nothing fails silently.
5. **Oracle differential for writes.** Write via Fauxcasa, launch Picasa
   in the Wine oracle, snapshot, diff. The harness exists for reads
   (`scripts/oracle-diff.py`, fixtures 001 to 037); the write direction
   is the M2 gate the spec names.

Stars, captions, keywords, hide, album membership, album order, sort
order, reject flags, edit recipes, and later the make-permanent metadata
writer all go through 1 to 4. Nothing else writes to a library.

### 8.2 Edit recipes are the edit model

Picasa's `filters=` string is already the persisted, ordered, named edit
stack the spec's N2 asks for. This project already parses it. The oracle
fixtures already show the exact grammar Picasa writes for crop, rotate,
straighten, fill light, one-click enhance, tuning and text, and how Save
bakes pixels and stashes the byte-exact original under
`.picasaoriginals`.

So the edit room is a recipe interpreter plus a UI over it:

- A pure function, `render(image, recipe) -> image`, implemented one
  operation at a time against the fixtures: crop64, rotate, tilt, fill
  light, enhance, finetune2, text. Grid thumbnails and the viewer call it
  (today they apply only the crop, so Picasa users see their other edits
  missing).
- The edit room appends operations to the recipe; undo pops; the recipe
  is written through the sidecar writer, so Picasa reads what we wrote.
- Save runs the renderer, writes the baked pixels, stashes the original
  the way fixture 005 shows, and records `stashed_original`. Undo Save
  restores. Revert clears the recipe.
- Export is `render`, then resize, watermark and encode.

Two honest caveats. Some Picasa operations (I'm Feeling Lucky, retouch,
some effects) are proprietary algorithms; pixel parity is impossible and
not required. The goal is to apply something sensible to existing recipes
and to author new edits in operations we fully own. And where our own
operation has no Picasa spelling, it goes in tier-2 native state, not in a
`filters=` string Picasa might choke on; the differential harness decides.

This one component makes M3 real, makes the existing edit display honest,
and gives M4's export its engine.

### 8.3 What stays as it is

The catalog and thumbnail cache pair, the grid, the viewer and its
preview-then-original load, the keymap table, the decode facade, the
library model, `picasa_db.py`, the oracle harness, the ingest-parity gate,
the release workflow with attestation, and the plain-language
documentation style. All of it is sound and none of it blocks the write
side.

The stack decision holds. Python and PySide6 delivered the §7 budgets and
the iteration speed the owner asked for. The sandbox transport was the one
place the host language fought back (the uv trampoline, the venv python,
the site-packages ACL grants), which is one more reason not to expand it.
The bundles are 132 to 146 MB, within the range the stack decision
predicted; that is acceptable under the spec's re-anchored austerity
(memory and cold start, not installer size).

### 8.4 What gets frozen

The sandbox at its current scope. The min-zoom performance chase. The
multi-root model beyond two menu items. The agent-usage tooling (moved
out). New research documents unless a scheduled feature needs one. The
balloons (already frozen).

---

## 9. A sequence that ships a job every few weeks

Each release is a thing a person can newly do, plus the plumbing it
needs, with a target of three to five weeks apiece. Percentages are the
proposed share of closed beads per release that carry the `touch` label
from section 10.

**Now, before 0.2.** Publish `v0.1.0-rc2` as a pre-release so the README
link works. Run the family-archive confirmation and record it. Both are
owner actions with everything prepared.

**0.2, "Your stars are yours" (about 3 weeks, 60% touch).**
`librarystate.py` with the journal; the sidecar writer; stars, captions,
keywords and hide written to `.picasa.ini` with read-back verification and
an oracle differential entry each; star toggle in the slideshow; the
reject flag on `X` with a Rejected collection (native state); the
`main.py` and test-file splits (7.1, 7.2). The N7 gate: make a sidecar
read-only, star a photo, the failure is visible within one action.

**0.3, "Albums and folders" (about 3 weeks, 70% touch).** Create, rename,
delete albums; add and remove members by drag to the sidebar and by
context menu; member order in tier-2 state; delete photos to the OS trash
with a confirmation that names the file's fate; Add folder and Remove
folder menu items over `library.py`; per-folder sort mode persisted in
tier-2 state. The N3 gate in CI: delete every cache, rescan, diff the
user-visible state, zero loss.

**0.4, "Fix it" (about 5 weeks, 60% touch).** The recipe renderer for the
operations Picasa already stored (display parity for rotate, crop,
straighten, fill light, enhance, tuning, text); the edit room with rotate,
crop with ratio presets, straighten, fill light, auto contrast and color,
black-and-white and sepia, red-eye; named undo and redo; Save, Undo Save,
Revert with `.picasaoriginals`. Fixture-replay equivalence as the gate.

**0.5, "Share it" (about 3 weeks, 70% touch).** The export dialog (size
presets, quality, text watermark, order-preserving numbering), the email
preset via `mailto:` and `xdg-email`, an Exports collection, batch rename.

**0.6, "Live in it, part one" (about 4 weeks, 50% touch).** Manual face
tagging (draw a region, name it) and the in-library people registry; the
N5 kill-fuzzer in CI; move folder with cross-volume copy-verify-delete;
per-folder health marks.

After that, the rest of M4 (watching with external-change reconciliation,
device import, backup sets, the make-permanent metadata writer), then
face recognition as the v1.5 headline, exactly as the spec sequences.

If the pace holds, 0.2 through 0.5 land in early 2027 and a person can
find, tag, fix and share. That is the point at which "happy to use it"
becomes a fair question to ask a tester.

---

## 10. Keeping the balance (rules agents can check)

Proportion drifts because plumbing is easier to specify than feelings.
These rules make the drift visible.

1. **Label every bead** with exactly one of `touch` (a person can see or
   do something new), `trust` (data safety: verified writes, gates,
   crash-safety, oracle differentials), `plumbing` (everything else in
   the product), `process` (repo, CI, agents, docs about how we work).
2. **A plumbing bead names its consumer.** The description says which
   `touch` or `trust` bead it serves. If that consumer is not scheduled
   within the current or next release, the plumbing bead waits.
3. **Per release, at least half of closed beads are `touch`**, and
   `process` beads are at most one in ten. The daily report prints the
   30-day ratio (one small addition to `daily-report.py` if it stays, or
   a `bd list` one-liner in Appendix B if it goes).
4. **No measurement campaign without a trigger.** A user report, a failed
   CI gate, or new reference hardware. Curiosity is not a trigger.
5. **Reviews are capped.** Two rounds per PR unless a P1 is found;
   adversarial verification only for P1 and P2 findings; substantive
   workflows state a token target in the bead and report the overshoot.
6. **The owner uses the current build weekly** on the read-only archive
   copy, and files what annoyed them. Those beads go to the front of the
   queue.
7. **Every release ships a published artifact**, pre-release if
   necessary. A draft is not a release.
8. **Re-run this review quarterly.** Appendix B in one sitting, a dated
   section appended below section 3 with the new table, and a sentence on
   whether the ratio moved the right way.

---

## 11. Proposed beads (file after ratification)

Sizes use the gap audit's scale: S hours, M about a day, L several days,
XL needs design first.

| # | Title | Label | Size | Serves |
|---|---|---|---|---|
| 1 | Publish v0.1.0-rc2 as a pre-release; fix the README link | process | S | 6.5 |
| 2 | Owner: family-archive confirmation (existing 6g8) | trust | owner | 6.4 |
| 3 | Split `test_tracer.py` into `tests/` by module with conftest and per-file CI processes | plumbing | M | 7.1, every later bead |
| 4 | Extract `librarystate.py`, `sidebar.py`, `cli.py` from `main.py` | plumbing | L | 7.2, 8.1 |
| 5 | Action journal in `librarystate.py` (append, fsync, replay, applied marks) | trust | M | 8.1 |
| 6 | Sidecar writer (existing lgg.1 design plus skeleton) | trust | L | 8.1 |
| 7 | Stars, captions, keywords, hide written to ini with verification and differential (extends lgg.2) | touch | L | 0.2 |
| 8 | Star toggle in the slideshow; reject flag and Rejected collection | touch | M | 0.2 |
| 9 | Album create/rename/delete/add/remove with tier-2 order; delete to trash | touch | L | 0.3 |
| 10 | Add folder and Remove folder menu items over `library.py` | touch | S | 0.3 |
| 11 | Recipe renderer: display parity for stored Picasa edits, fixture-tested per operation | touch | XL | 0.4 |
| 12 | Edit room v1 with named undo and Save/Undo Save/Revert | touch | XL | 0.4 |
| 13 | Export dialog, email preset, Exports collection | touch | L | 0.5 |
| 14 | Move delegation-report and daily-report out of the repo; drop the CI job (extends nn9) | process | S | 5.4 |
| 15 | Threat model amendment: describe shipped coverage; freeze sandbox scope until after 0.4 | process | S | 5.1 |
| 16 | Close q6l.27 with the release-note resolution | process | S | 5.2 |
| 17 | Bead labels and the 30-day ratio line | process | S | 10 |

Beads 3 and 4 are the only plumbing without a user-visible line of their
own, and both name their consumers.

---

## Appendix A. Measurements

### A.1 Production and test lines by module (current)

| Module | Lines | Non-merge commits | Lines added (history) |
|---|---:|---:|---:|
| test_tracer.py | 20,402 | 141 | 36,702 |
| main.py | 5,528 | 101 | 6,291 |
| decodesvc_win.py | 3,300 | 18 | 3,705 |
| test_decodesvc_win.py | 3,117 | 17 | 3,250 |
| catalog.py | 2,413 | 28 | 2,587 |
| grid.py | 1,914 | 31 | 2,124 |
| viewer.py | 1,842 | 33 | 2,061 |
| thumbcache.py | 1,391 | 33 | 1,705 |
| videostream.py | 1,067 | 2 | 1,067 |
| library.py | 945 | 8 | 1,014 |
| decodesvc_worker_win.py | 851 | 3 | 853 |
| metareader.py | 699 | 8 | 751 |
| test_decodefacade.py | 684 | 8 | 712 |
| bench_scroll.py | 635 | 6 | 644 |
| keymap.py | 604 | 10 | 683 |
| test_sandbox_e2e.py | 575 | 8 | 576 |
| pillowload.py | 477 | 7 | 576 |
| decodefacade.py | 462 | 9 | 524 |
| inmeta.py | 447 | 4 | 481 |
| db3rescue.py | 446 | 3 | 471 |
| decodesvc.py | 409 | 4 | 420 |
| tray.py | 344 | 3 | 363 |
| rawload.py | 295 | 5 | 324 |
| slideshow.py | 254 | 9 | 274 |
| the remaining 14 modules | 2,300 | | |

Whole-history churn by top-level area (lines added / deleted):
`apps/desktop-python` 71,640 / 19,328; `docs` 21,911 / 780; `scripts`
15,002 / 939; `fixtures` 9,986 / 5; `balloons` 6,624 / 4; `.github`
1,746 / 85.

### A.2 Tests

| File | Tests |
|---|---:|
| apps/desktop-python/test_tracer.py | 628 |
| apps/desktop-python/test_decodesvc_win.py | 149 |
| scripts/test_picasa_db.py | 96 |
| apps/desktop-python/test_decodefacade.py | 23 |
| scripts/test_confirm_archive.py | 20 |
| scripts/test_daily_report.py | 16 |
| apps/desktop-python/test_inmeta_datasets.py | 14 |
| scripts/test_delegation_report.py | 11 |
| apps/desktop-python/test_sandbox_e2e.py | 8 |

Test names in `test_tracer.py` by leading topic: viewer 42, grid 34,
metareader 22, mainwindow 17, db3 17, inmeta 16, tray 12, search 12,
inspector 12, reconcile 11, star 10, slideshow 10, index 10, import 10,
folder 10, and a long tail.

### A.3 Beads by epic

| Epic | State | Children closed |
|---|---|---|
| fauxcasa-cam M1 ingest completion | closed | 22 of 22 |
| fauxcasa-v46 M1 formats | closed | 7 of 7 |
| fauxcasa-q6l M1 browse UI | open | 26 of 27 (q6l.27 min-zoom open) |
| fauxcasa-ez2 Release 0.1 | in progress | 14 of 15 (ez2.11 native pass open) |
| fauxcasa-ed5 M1 gates | open | 11 of 13 (multi-root 7 of 7 closed) |
| fauxcasa-i92 M1 decode isolation | open | i92.3 in progress; i92.4, i92.5 blocked |
| fauxcasa-lgg M2 writes | open | 0 of 2, filed 2026-09-15 |

### A.4 What a user can persist in 0.1, and where it goes

"Per-library state dir" is the variant-free directory under the cache
root that `main.library_state_dir` names, one per opened library.

| Choice | Function | File | Picasa sees it? |
|---|---|---|---|
| Star | starstore.save_star_overrides (starstore.py:72) | per-library state dir, `stars.json` | no |
| Sort mode per folder | main.save_sort_modes (main.py:537) | per-library state dir, `config.json` | no |
| Star threshold | main.save_star_min (main.py:562) | per-library state dir, `config.json` | no |
| Flat or tree folders | main.save_folder_view (main.py:586) | per-library state dir, `config.json` | no |
| Window geometry | main.save_window_geometry (main.py:631) | per-library state dir, `config.json` | no |
| Remembered library | main._remember_library (main.py:435) | cache root, `config.json` | no |
| File Types | filetypes.save_excluded_exts (filetypes.py:110) | cache root, `config.json` | no |
| Multi-root library | library.save_library (library.py:408) | library home, `.fauxcasa/library.json` | no |

---

## Appendix B. How to re-measure

Run from the repository root in Git Bash. Each block reproduces one table
above.

```bash
# Line counts per app module, and code versus test totals
for f in $(git ls-files apps | grep '\.py$'); do printf "%7d %s\n" $(wc -l < "$f") "$f"; done | sort -rn
git ls-files apps | grep '\.py$' | grep -v test_ | xargs cat | wc -l
git ls-files apps | grep 'test_.*\.py$' | xargs cat | wc -l

# Commits per month, commits per area, commits per module
git log --format='%ad' --date=format:'%Y-%m' | sort | uniq -c
for d in apps/desktop-python scripts docs fixtures balloons .github; do printf "%5d %s\n" $(git log --no-merges --format=%h -- "$d" | wc -l) "$d"; done

# Lines added and deleted per area over the whole history
for d in apps/desktop-python scripts docs fixtures balloons .github; do git log --no-merges --numstat --format= -- "$d" | awk -v d="$d" '{a+=$1; r+=$2} END {printf "%8d added %8d deleted  %s\n", a, r, d}'; done

# Lines added per module over the whole history
for f in $(git ls-files apps/desktop-python | grep '\.py$'); do git log --no-merges --numstat --format= -- "$f" | awk -v f="$f" '{a+=$1} END {printf "%8d %s\n", a, f}'; done | sort -rn

# Tests per file
for f in $(git ls-files | grep 'test_.*\.py$'); do printf "%5d %s\n" $(grep -cE '^def test_' "$f") "$f"; done | sort -rn

# Beads
bd stats
bd list --type=epic --status=all
bd list --status=closed --limit 300
bd list --status=open --limit 300

# Once labels exist (section 10, rule 3): the 30-day touch ratio
bd list --status=closed --limit 300 | grep -c '\[touch\]'

# Release state
gh release list --limit 10
gh api repos/maphew/fauxcasa/releases/latest --jq .tag_name

# CI wall time for the last 25 runs
gh run list --limit 25 --json name,conclusion,createdAt,updatedAt --jq '.[] | "\(.name) \(.conclusion) \(.createdAt) \(.updatedAt)"'

# Sandbox routing (the facts behind section 5.1)
grep -n "sys.platform" apps/desktop-python/decodefacade.py
grep -n "is_video\|is_raw\|endswith((\".heic\"\|\.psd\|tiff_is_16bit\|STATE_SANDBOXED" apps/desktop-python/thumbcache.py apps/desktop-python/viewer.py
grep -rn "bwrap\|bubblewrap\|seccomp\|Landlock" apps/ scripts/

# Library writes (the facts behind section 6.1)
grep -rn "picasa.ini" apps/desktop-python/*.py | grep -v test_ | grep -i "write\|open(.*'w'\|replace("
grep -n "def save_\|def _write_\|def _remember_" apps/desktop-python/main.py apps/desktop-python/starstore.py apps/desktop-python/library.py apps/desktop-python/filetypes.py
```

---

## Appendix C. Method and limits

The review was done in one session on 2026-09-16. The reviewer read the
spec, the threat model, the promotion gate, the M1 gap audit, the release
notes, the changelog, the README files, the UI inventory, the design
document outlines, every open bead and every closed bead title, the
recorded memories, the CI workflows, and the outlines of the main modules.
Two haiku-tier scout agents then read `decodefacade.py`, `thumbcache.py`,
`viewer.py`, `catalog.py`, `main.py`, `starstore.py`, `library.py`,
`slideshow.py`, `keymap.py` and `inspector.py` and reported the routing,
persistence and action facts cited in sections 5.1, 6.1 and Appendix A.4
with line numbers. An opus-tier reviewer checked the draft's factual
claims against the repository before it was committed.

Limits. Bead classification by title is approximate; the counts in 3.3
could move by ten in either direction without changing the conclusion.
No user was interviewed beyond the one field report the spec records. No
runtime profiling was done; performance statements repeat the project's
own measurements. The proportion judgments are the reviewer's, argued
from the spec's stated priorities; the owner may weigh the sandbox's
protection of the archive higher than this review does, and that is a
legitimate place to argue.
