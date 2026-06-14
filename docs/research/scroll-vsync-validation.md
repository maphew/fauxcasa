# §7 scroll validation on a real display + Windows packaging spike (fauxcasa-ncv)

**Status:** done, 2026-06-13. Both M1 tripwires from the stack decision
(fauxcasa-6hf, `stack-balloons.md`) are retired. Methodology was
adversarially pre-mortemed before the run; the controls it demanded are
implemented and reported below.

## TL;DR

1. **The py-qt scroll asterisk is retired.** The *product* grid
   (`tracer/grid.py`, event-driven repaints) was measured on a real
   display + real GPU running the balloon protocol (5 clean runs, ~15.8 k
   frames each). Frame **production time** (the honest, throttle-independent
   §7 metric) is **p99 5.7 ms, worst-case max 10.1 ms, with zero blank
   tiles** — the §7 scroll budget (p99 ≤ 32 ms, max ≤ 100 ms, zero blanks)
   passes with a **5–6×** margin. The compositor's own protocol log confirms
   fresh buffers reach it every **5.9 ms** during scrolling while it paces
   at the **6.06 ms (165 Hz)** vsync — i.e. it has a fresh frame to present
   at essentially every vsync, so the scroll is genuinely smooth on screen,
   not just "fast to paint offscreen".
2. **A real Windows-class bundle exists.** `tracer/fauxcasa-tracer.spec`
   (PyInstaller, onedir) builds a launchable artifact; the **frozen** exe
   runs headless and decodes JPEG thumbnails (non-blank screenshot). CI
   (`.github/workflows/bundle.yml`) builds + smoke-tests it on
   windows-latest (and ubuntu for early warning).

**The one caveat that still holds:** this machine (16-thread Ryzen 7840HS,
Radeon 780M, on AC) **exceeds** the §7 reference hardware (8-thread /
16 GB). Passing here is **necessary, not sufficient** — the reference-class
hardware gate named in `stack-balloons.md` (cold start + scroll + the
*index-rate* rows) is still owed and remains the real sufficiency test.

---

## 1. What the asterisk was

The stack-balloon table (`stack-balloons.md`) marked py-qt's scroll numbers
(p99 5.1 ms\*, max 9.5 ms\*) with:

> the screen refreshes 60 times a second, and the other three waited for
> each refresh before painting the next frame; py-qt's toolkit did not
> wait, painting ~250 frames/s. So its numbers measure how fast it can
> paint, not how smooth it looked.

Two roots: **(a)** software rendering (headless weston / llvmpipe, no GPU),
and **(b)** unthrottled paints measured as a frame *interval*. Retiring it
means measuring the real grid on a real display and showing the on-screen
result is actually smooth.

## 2. Key discovery: Qt's raster QWidget is *unthrottled* here too

Before trusting any number, the pre-mortem demanded a throttle-probe gate
(`tracer/vsync_probe.py`, raw output in `ncv-results/throttle-probe.txt`).
A trivial `QWidget` driven by a fixed-rate timer, on this compositor
(GNOME/Mutter, Wayland, AMD 780M, **eDP-1 @ 165 Hz**):

| timer | paint_hz | reading |
|---|---|---|
| 60 Hz | 55.7 | inconclusive (timer < refresh) |
| 240 Hz | 235.0 | **paints exceed 165 Hz → unthrottled** |
| 1000 Hz | 597.3 | **paints exceed 165 Hz → unthrottled** |

Paint cadence **tracks the timer** and runs far past the 165 Hz refresh; a
vsync-locked surface would pin at ~165 regardless. So Qt's QWidget raster
(wl-shm) surface is **not** frame-callback throttled on this stack — the
same behavior the balloon hit under headless weston, now confirmed on real
hardware. (There is no `frameSwapped`/presentation signal for a raster
QWidget, and Qt does **not** use the `wp_presentation` protocol the
compositor advertises — 0 `.presented` events.)

**Consequence:** paint-to-paint *interval* is **not** a valid vsync-cadence
signal here. The honest §7 metric is **frame production time** (wall time
inside one `paintEvent`: decode-pump + layout + draw + prefetch-request +
evict). When the app is unthrottled, the compositor drops a frame only if
no fresh buffer was committed since the last vsync; the app commits one
buffer per paint, so *frame production < one refresh period ⇒ a fresh
buffer every vsync ⇒ smooth at refresh*. We corroborate that the buffers
actually arrive with the compositor's own commit/callback log (§5).

## 3. Methodology

System under test: the **product** `GridView`, unchanged except a
zero-cost-when-off frame probe (`grid.py`, `_frame_probe`). Driver:
`tracer/bench_scroll.py` runs the balloon protocol (cold → flick-A 45 s →
teleport 25/50/75/99 % → flick-B 45 s) on the 100 k benchmark cache, in a
1280×800 logical window, scrolling at a **time-based** offset (so the
*speed* is exact regardless of paint rate). Controls implemented per the
pre-mortem:

- **Frame production time** is the headline metric; interval is reported
  with its `idle = interval − paint_cost` decomposition (idle ≈ 3.3 ms at
  native drive → not compute-bound; there is slack).
- **Compositor ground truth** via `WAYLAND_DEBUG=1` (§5).
- **Occlusion guards:** window visibility sampled every tick
  (`window_visibility_codes == [2]` Windowed for the whole run, all 9
  runs); the 100 ms frame-callback-timeout signature is counted
  (`timeout_interval_frames == 0`, so no "hidden surface" false jank);
  runs wrapped in `systemd-inhibit --what=idle:sleep` with the screensaver
  deactivated before each run (see §4a — a locked screen stalls paints).
- **Representative catalog:** the benchmark catalog carries only paths
  (every star False, rotate 0 — the lightest possible paint), so the
  harness injects a deterministic, disclosed **8.3 % starred / 17.7 %
  rotated** distribution, exercising the star-badge polygon (paint) and
  decode-time rotation (workers). 1548 folder groups → sticky headers
  exercised.
- **N = 5** for the primary config; also a **3.0 screens/s** run (the §7
  budget edge, not just 2.5) and a **60 Hz-capped** drive (maps onto the
  §7 "60 fps" target / a reference-class panel).
- Per-phase **flick-A vs flick-B** stats (no thermal/clock droop seen).
- Provenance recorded: refresh, **devicePixelRatio = 2.0** (the 1280×800
  logical window is a **2528×1596 device-pixel** backing store — 4× the
  balloon's pixel count), backing-store px, cols, group/star/rotate
  fractions, prefetch/cache_bytes/cache_max_entries, on_ac, governor.

What we **cannot** measure in-process: true scanout timestamps (Qt doesn't
use `wp_presentation`). The commit cadence (§5) is the honest proxy.

## 4. Results (9 runs, raw in `ncv-results/scroll-final.jsonl`)

100 000 photos, 377 MB thumbnail cache, eDP-1 @ 165 Hz (6.06 ms period),
DPR 2.0 (1280×800 logical = 2528×1596 device-px backing store), on AC /
powersave governor, screen kept active (§4a). Each run ~15.8 k frames over
the 90 s of flick (≈169 fps), all `occlusion_clean`. **Frame production
time** (= §7 "frame time"):

| config | n | p50 | p99 (median) | p99 (worst) | max (worst) | deadline-hit | blanks |
|---|---|---|---|---|---|---|---|
| native 165 Hz @ 2.5 scr/s | 5 | 2.62 ms | **5.71 ms** | 5.91 ms | 10.1 ms | 99.3 % | **0** |
| edge @ 3.0 scr/s | 2 | 2.61 ms | 5.86 ms | 5.86 ms | 9.2 ms | 99.2 % | **0** |
| 60 Hz-capped drive @ 2.5 | 2 | 3.56 ms | 8.39 ms | 8.39 ms | 11.6 ms | 94.4 % | **0** |

- **Every run passes** the §7 scroll budget (p99 ≤ 32 ms, max ≤ 100 ms,
  zero blanks) — p99 by ~5–6×, max by ~9×.
- *deadline-hit* = fraction of frames produced within one **165 Hz**
  refresh period (6.06 ms); 99.3 % at native. Against the §7-relevant
  **60 Hz** period (16.7 ms) effectively 100 % of frames fit.
- The 60 Hz-capped run's interval p50 is **16.4 ms ≈ the 60 Hz period**,
  directly demonstrating the grid sustains the §7 "60 fps target".
- RSS ~341 MB, peak HWM 346–359 MB. Teleport refills **61–85 ms**
  (median 75 ms).
- **Note on interval-based metrics:** because Qt is unthrottled (§2),
  paint-to-paint interval is drive-paced, not vsync-paced; the
  `missed_vsync_frames` count (interval > 1.5× the 165 Hz period) is
  therefore dominated by the by-design 16.7 ms intervals of the
  60 Hz-drive runs and is **not** a meaningful smoothness signal here.
  Frame production time + deadline-hit + the §5 commit cadence are.

### 4a. Measurement reliability (a real trap, now guarded)

A GUI scroll benchmark on a live compositor is only valid if the surface
is actually being composited. Two failure modes bit during this work and
are now detected, not silently mis-measured:

- **Screen idle-lock.** When the GNOME session idle-locks (`LockedHint=yes`)
  the compositor stops sending frame callbacks; Qt's Wayland backing store
  then paints almost never (observed: ~30 frames over a 90 s flick instead
  of ~15.8 k, and every teleport fill stalls). The frame-*production*
  numbers from such a run look fine (each of the few paints is fast) but
  are a 30-sample lie. Fix: deactivate the screensaver before each run
  (`org.gnome.ScreenSaver.SetActive false`) and wrap in `systemd-inhibit`.
- **Window occlusion / CPU contention from concurrent agents.** This
  machine was shared with other agents (a Wine/Picasa3 oracle, etc.)
  during the session; a covering window similarly withholds frame
  callbacks, and CPU contention inflates the production-time tails.
- **The guard:** every run records `frames`, `frames_vs_expected_ratio`,
  `fill_timeouts`, the ~100 ms frame-callback-timeout cluster,
  `load_avg_1min`, and an `occlusion_clean` flag (true only when no fill
  timed out, no timeout-interval cluster, and the window stayed Windowed).
  **All 9 runs in §4 are `occlusion_clean` with ~15.8 k frames** (≈ the
  expected 169 fps × 90 s) — i.e. genuinely unobstructed, not the
  30-frame artifact. (`QWindow.visibility()` alone is insufficient — it
  reports "Windowed" even when covered; the fill/timeout tells are the
  real signal.) This validates the pre-mortem's occlusion warning.

## 5. Compositor ground truth (`ncv-results/wayland-cadence.txt`)

`WAYLAND_DEBUG=1` over a flick run, parsed by
`scripts/parse-wayland-cadence.py` (the debug logging perturbs the app's
own timing, so this is used **only** for the compositor's cadence):

- **Fresh-buffer delivery** (SUT `wl_surface.commit`): **p50 5.93 ms**, p95
  7.5 ms, p99 11.3 ms during active scrolling (2152 commits) — Qt feeds the
  compositor a new buffer faster than the 6.06 ms vsync, continuously (the
  only gaps ≥ 50 ms are the 4 scripted teleport dwells, not scroll stalls).
- **Compositor frame pacing** (`wl_callback.done`): **p50 6.1 ms ≈ the
  165 Hz vsync** — the compositor acknowledges the surface at the true
  panel refresh (also confirms a fixed 165 Hz mode, no VRR surprise).

So on a real display + real GPU the compositor has a fresh frame to present
at essentially every vsync. This is what closes the asterisk's "we only
measured offscreen paint speed" gap — with the bounded honesty that
`wp_presentation` scanout timestamps weren't available, so commit cadence
is the proxy.

## 6. Caveats (so this is not oversold)

- **Stronger than reference hardware.** 16 threads vs 8, more RAM, and —
  reversing the balloon's llvmpipe handicap — a **real 780M hardware-GL
  compositor**, so the render/composite side is now *easier*, not harder.
  Vsync lock is a property of the panel and transfers; whether the app
  *renders within budget* on the 8-thread / 16 GB reference box does
  **not**. **Passing here is necessary, not sufficient.**
- The still-owed sufficiency test is the **reference-class hardware gate**
  (`stack-balloons.md` tripwires): cold start + scroll + the **index-rate**
  rows (≥ 30 photos/s local incl. hashing — the workload the grid does not
  exercise; tracked as the residual risk on fauxcasa-hw0).
- Measured on AC + **powersave** governor (recorded). Powersave makes the
  result *conservative* on the clock axis — AC + performance would only be
  faster.
- This is **"the product grid on a real vsync display"**, not "the balloon
  harness on a real display" — `GridView` is heavier than the balloon grid
  (prefetch 1.0 screen vs 2.0, group headers, sticky header, star
  polygons, per-paint evict), so it is a tougher SUT, not a like-for-like
  re-run of the balloon number.

## 7. Windows packaging spike

Deliverable: a real distributable so the Windows CI gates run on the
artifact users would get, not on source.

- **`tracer/fauxcasa-tracer.spec`** — PyInstaller, **onedir** (not onefile:
  onefile re-extracts the whole bundle each launch → multi-second cold
  start, which fails §7's cold-start anchor), `console=True` (the tracer's
  READY/JSON stdout is how the headless gate validates it; a shipping GUI
  build flips to `console=False`). Built against **PySide6-Essentials**
  (omits WebEngine/QML/Qt3D/Charts at the source) with belt-and-suspenders
  Qt excludes; **keeps** `imageformats/qjpeg`, `platforms/` and `styles/`;
  `--noupx`. Sibling modules (`catalog`/`grid`/`thumbcache`/`viewer`) and
  `picasa_db` are explicit hidden-imports.
- **Validated locally (Linux onedir as the proxy):** the build succeeds and
  the **frozen** exe runs headless against a JPEG smoke library, exits 0,
  builds its cache, and produces a **non-blank** screenshot (the decoded
  blue fixtures appear, not gray placeholders) — i.e. `qjpeg` is bundled
  and JPEG decode works in the frozen app. RSS ~90 MB.
- **Size:** 180 MB unpacked on Linux (libpython3.14 34 MB + libicudata
  31 MB + Qt + a Linux-only libgtk pulled by the GTK platform theme). The
  Windows bundle will be smaller (no libgtk, dll-not-.so python, split
  ICU) — expect the spec's **~100–150 MB** band. Size is a **soft** gate
  (§7 anchors austerity on memory + cold start, not installer MB). Top trim
  levers if wanted later: drop `translations/*.qm`, the Windows-only
  `opengl32sw.dll` (~20 MB, only if GPU-less RDP isn't a target), and
  ICU data — all gated behind the smoke run.
- **`.github/workflows/bundle.yml`** — builds on windows-latest +
  ubuntu-latest (`fail-fast: false`), runs the frozen artifact headless,
  **asserts the screenshot is non-blank**, prints bundle size to the step
  summary (warn-only > 200 MB), uploads the bundle + screenshot.
- **Known follow-up (not blocking):** `main.py`'s `__file__`-relative
  default library / `--cache-root` point inside the (read-only) bundle when
  frozen — fine for the CI smoke (it passes explicit paths) and a
  no-arg launch fails *gracefully* ("library not found"), but a shipping
  double-click build should derive a writable cache dir under
  `getattr(sys, 'frozen', False)`. Filed as a follow-up bead.

## 8. Reproduce

```bash
# 0. throttle-probe gate (is the raster surface vsync-throttled here?)
for hz in 60 240 1000; do WAYLAND_DISPLAY=wayland-0 QT_QPA_PLATFORM=wayland \
  uv run tracer/vsync_probe.py --hz $hz; done

# 1. scroll benchmark (sequential, idle machine; needs the 100k cache).
#    Deactivate the screensaver first — a locked screen stalls frame
#    callbacks and silently produces a ~30-frame run (see §4a); confirm
#    occlusion_clean==true and frames≈15k in the output.
gdbus call --session --dest org.gnome.ScreenSaver \
  --object-path /org/gnome/ScreenSaver \
  --method org.gnome.ScreenSaver.SetActive false
systemd-inhibit --what=idle:sleep WAYLAND_DISPLAY=wayland-0 QT_QPA_PLATFORM=wayland \
  uv run tracer/bench_scroll.py \
    --thumbs cache/benchmark-thumbs.fcache --library cache/benchmark-library \
    --seconds 45 --screens 2.5            # add --drive-hz 60 / --screens 3.0 for the variants

# 2. compositor ground truth
WAYLAND_DEBUG=1 ... uv run tracer/bench_scroll.py ... --seconds 6 2>wld.log
uv run scripts/parse-wayland-cadence.py wld.log

# 3. Windows-class bundle (Linux proxy locally; real artifact in CI)
uv run --with "PySide6-Essentials==6.11.1" --with "pyinstaller==6.20.0" \
  pyinstaller --noconfirm --clean tracer/fauxcasa-tracer.spec
QT_QPA_PLATFORM=offscreen dist/fauxcasa-tracer/fauxcasa-tracer ci-library \
  --cache-root ./ci-cache --finish-build --screenshot frozen-ci.png --scroll-to 0.5
```

Raw artifacts: `ncv-results/{throttle-probe.txt, scroll-final.jsonl,
wayland-cadence.txt}`.
