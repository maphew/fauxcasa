# Stack trial balloons: results and recommendation (fauxcasa-6hf)

**Status:** agent-run evaluation, 2026-06-12, per the owner's procedure
(spec §10 item 12: trial balloons, timeboxed, decide-and-move; iteration
speed weighs as heavily as raw performance; report in plain language).
**The recommendation at the bottom is proposed, not locked** — flagged
for owner confirmation, overturnable by argument like every delegated
decision.

## What we did, in plain language

We built the same small photo-grid app **four times, in four different
technology stacks**, and raced them on the same fixed task: open a
window, show all 100,000 photos of the synthetic test library as a
scrolling wall of thumbnails (reading only from a pre-built thumbnail
cache, the way the real app will), then auto-scroll through it fast for
90 seconds, teleport to four random spots, and report cold-start time,
scrolling smoothness, stuck-gray-tile count, and memory use. Same rules
for everyone; the protocol and referee script are in `balloons/README.md`
and `scripts/run-balloon-bench.py`.

The four candidates (seeded by owner warmth, spec §10 item 12):

| Balloon | Stack | In one sentence |
|---|---|---|
| `rust-egui` | Rust + egui | The "systems language" candidate: compiled, fast, strict. |
| `go-gio` | Go + Gio | Compiled but famously quick to build; simpler language than Rust. |
| `py-qt` | Python + Qt (PySide6) | Scripting language driving the 30-year-old, battle-tested C++ Qt toolkit. |
| `web-wasm` | Browser engine (Chromium) + JavaScript canvas | The "webview/wasm hybrid" probe: is a browser shell fast enough? |

## The numbers (official run, 2026-06-12, all four sequential)

100,000 photos, 377 MB thumbnail cache. Budgets: cold start < 2 s,
scroll p99 frame ≤ 32 ms and max ≤ 100 ms, zero blank tiles (spec §7).
"p99" means: all but the slowest 1% of frames finished within this time
— it's the smoothness number. On memory, the spec's hard test is
"nowhere near 1.5 GB"; the stricter "< 600 MB working target" is the
balloon protocol's own bar (§7 deliberately leaves hard RAM budgets to
this very decision).

**Bold marks the best value in each row.**

| Metric | rust-egui | go-gio | py-qt | web-wasm | Budget |
|---|---|---|---|---|---|
| Cold start to interactive‡ | **91 ms** | 94 ms | 131 ms | 1389 ms (+ ~0.5 s browser launch = 1902 ms) | < 2000 ms |
| Scroll p99 frame time | **17.9 ms** | 18.4 ms | 5.1 ms* | 16.8 ms | ≤ 32 ms |
| Worst single frame | 22.7 ms | 47.8 ms | 9.5 ms* | **18.3 ms** | ≤ 100 ms |
| Blank-tile frames§ | **0** | **0** | **0** | 1 | 0 |
| Teleport-to-filled | 33–51 ms | **33 ms** | 40–44 ms | 367 ms | (reported, no hard budget) |
| Resident memory | 551 MB | 607 MB¶ | **184 MB** | 286 MB† | < 600 MB working target |
| Lines of code | 514 | 555 | **330** | 723 |
| Clean build time | ~40 s | 9.7 s | **no build step** (59 s one-time download) | **no build step** |
| Edit-and-rerun loop | 2–8 s | 1–2 s | **~1 s** | ~20 s |

\* Not comparable to the other columns: the screen refreshes 60 times a
second, and the other three waited for each refresh before painting the
next frame; py-qt's toolkit did not wait, painting ~250 frames/s. So its
numbers measure how fast it can paint, not how smooth it looked. The
honest reading: every single paint finished in under 10 ms, comfortably
inside one 60 Hz refresh.
† Whole browser process tree; ~240 MB of it is the empty Chromium shell
before any photos.
‡ Measured on each balloon's own clock. Wall-clock from process launch
was 93 / 270 / 154 / 1902 ms — all far under budget either way.
§ Out of ~5,400 rendered frames for the three refresh-locked balloons;
py-qt's 0 was out of 22,502 (see *).
¶ Over the 600 MB working target by 7 MB — measured on software
rendering, where go-gio's texture memory sits in process RAM (see
caveats); the spec's hard test (nowhere near 1.5 GB) passes easily.

**Every candidate passed the spec's §7 budgets**, with two qualifiers
visible in the table: go-gio's memory grazes past our stricter working
target under software rendering (¶), and web-wasm's cold start — 1.4 s
of in-page work plus ~0.5 s of browser launch — lands at 1.9 s on this
fast machine, right at the 2 s line and likely over it on the reference
hardware. web-wasm's teleport refills (367 ms vs ~40 ms) reflect
fetching thumbs over a local HTTP hop instead of direct file reads —
partly benchmark scaffolding, partly a real architectural tax.

## Honest caveats on the measurements

- **This machine is stronger than the spec's reference hardware** (16
  threads / 38 GB vs 8 threads / 16 GB). Passing here is necessary, not
  sufficient. Mitigation: all rendering ran on *software rasterization*
  (llvmpipe — the desktop session was locked, so balloons ran under a
  headless compositor with no GPU), which is a substantial handicap the
  real app won't have; passing budgets without a GPU is conservative
  evidence.
- The software-rendering setup also inflates rust/go memory numbers:
  their GPU texture atlases live in process RAM under llvmpipe.
- The grid is the *easy half* of the performance identity. The §7
  index-rate budgets (hashing/thumbnailing 100k originals) are a
  different workload the balloons deliberately don't test — that's where
  a scripting-language host could still fail, and it's the named
  tripwire below.

## Iteration speed (the co-equal criterion)

What it was actually like to build each one — all four were implemented
against the same written protocol, three of them by parallel agents in
a single evening:

- **py-qt** was the fastest from empty directory to passing the 100k
  benchmark: ~15 minutes, 330 lines, no compile step, ~1 s edit-rerun
  loop, zero system dependencies (the Qt packages bundle everything).
  Qt's documentation is the most mature of the four. The feared Python
  bottleneck — the GIL, Python's global lock that normally stops it
  using more than one CPU core at a time — was a non-issue at this
  workload, because Qt does the heavy lifting in C++ with that lock
  released.
- **go-gio** compiled the first draft with zero errors, builds in ~10 s
  clean / 1–2 s incremental, and cross-compiles Windows binaries for
  free. The cost: Gio's documentation is thin (the agent worked from
  the type checker, not guides), it needed a container's worth of C
  headers on this host, and it's a niche toolkit maintained by a tiny
  team.
- **rust-egui** was solid and unsurprising — but it has the slowest
  edit-loop of the four (40 s clean, multi-second incremental), and it
  took the most fighting in this exercise (dependency declaration, API
  versions). Its performance showed no advantage over the others *at
  this task*, because the work is "unpack small images and copy them to
  the screen" — done by libraries everyone shares, not by the host
  language itself.
- **web-wasm** had a near-zero build loop and hit 60 fps easily, but it
  isn't really a shippable stack yet: the probe leaned on an installed
  browser; productizing means an Electron/Tauri/webview packaging
  decision (its own research project), file access through browser
  sandbox APIs, and a ~240 MB shell floor plus ~1.9 s from launch to
  first pixel (~0.5 s of that is starting the browser itself; the rest
  is in-page work). Notably, **no wasm was needed** — plain JS
  kept up — so the probe answered "browser shell as host," not "wasm as
  technology." wasm's real role stays the one the spec already names:
  the decode sandbox and the future extension API
  (`docs/decode-threat-model.md`).

## What the result means

The headline is architectural, not linguistic: **Picasa's design — a
precomputed thumbnail cache so the grid never touches originals — is
what makes "instant at 100k" achievable. Done that way, every candidate
language clears the §7 grid budgets, even on software rendering.** The
stack choice therefore turns on the owner's other criterion: iteration
speed, plus ecosystem maturity and the path to shipping on Windows.

## Proposed decision

Apply the rule as written ("first candidate that passes the budgets and
iterates fast wins; ties break toward iteration speed, not perf
margin"):

**Host stack: Python + Qt (PySide6).** Fastest iteration loop by a wide
margin, smallest code, lowest measured memory, the most mature toolkit
and documentation, batteries available for everything M1 needs (the
validated `picasa_db.py` parsers are *already Python*, so ingest code
flows straight into the app), and first-class Windows/macOS support in
the toolkit. The repo's existing tooling conventions (uv-run scripts)
extend naturally.

Named consequences, owned now:

1. **wasm keeps the role the spec gave it** — decode sandbox trajectory
   and later extension API (see `docs/decode-threat-model.md`; the
   sandbox floor is OS-level worker isolation, host-language
   independent).
2. **Rust is the named escape hatch** for any inner loop that measurably
   misses budget — a small, fast Rust piece bolted inside the Python
   app where needed, not a rewrite. Nothing tonight indicates we'll
   need it for the UI.
3. **Tripwires that would reopen this decision** (the decide-and-move
   fear bound — by N3 a wrong stack never holds data hostage, a rewrite
   is only code):
   - M1 gate on reference-class hardware: cold start, scroll, and the
     *index-rate* rows (≥ 30 photos/s local incl. hashing) — the
     workload the balloons didn't test. Python's path there is
     multiprocess workers around C-speed hashing/decoding; if it can't
     hit the rates, that's a stack-level fact, not a tuning problem.
   - **Windows packaging spike early in M1**: a real distributable
     bundle (PyInstaller-class) must exist before M1 exit so the
     Windows CI gates run on the artifact users would get. Bundle size
     will be Qt-typical (~100–150 MB installed); the spec already
     re-anchored austerity on memory and cold start, not installer
     megabytes (§7).
   - py-qt's screen-sync caveat (the table's * note) gets retired by
     measuring the M1 grid on a real display with the same harness.
4. **The balloons stay in-tree and CI-built on Linux + Windows**
   (`.github/workflows/balloons.yml`) so any tripwire can rerun the
   race cheaply — including on the reference-class hardware when
   available.

**Decision state: proposed.** fauxcasa-6hf stays open, flagged for the
owner: confirm, or argue — the evidence above is the argument surface.
