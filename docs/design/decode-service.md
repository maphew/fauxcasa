# Decode service: the sandboxed broker/worker interface

**Status:** design deliverable for fauxcasa-i92.2 (blocks i92.3, the
implementation). Written 2026-07-02. **Decision state: proposed** — owner
review-by-argument, like every delegated decision; the calls most worth
fighting over carry **⚖ argue** markers. The parent commitments are
`docs/decode-threat-model.md` (mechanism A: broker/worker pool, fd/shm in,
fixed-shape length-checked pixel buffers out, no format exceptions,
decoder-bundle versioning, CI escape gates) and spec §5 Formats / §9 M1.
Measurements in this doc were taken on the Windows dev machine on
2026-07-02 by `docs/research/spikes/decode-worker-spawn-spike.py`
(re-runnable: `uv run docs/research/spikes/decode-worker-spawn-spike.py`).

A skeleton of the typed interface lives at
`apps/desktop-python/decodesvc.py` (dataclasses + abstract transport, no
implementation; not yet imported by the app).

## Plain-language summary (for the owner)

Photos and videos from the outside world can be booby-trapped, and the
programs that turn them into pixels have a long history of being tricked
into running an attacker's code. Our answer (already ratified in
substance) is the locked room with a mail slot: decoding happens in
separate helper processes that the operating system has stripped of all
powers. This document is the blueprint for the mail slot itself — exactly
what goes in, what comes out, and what happens when a helper dies.

The important calls, in plain words:

1. **Helpers are hired once and reused, not hired per photo.** Starting a
   decode-ready helper on this Windows machine costs ~92 ms — fine to pay
   eight times at startup, ruinous to pay 100,000 times during an index.
   Talking to an already-running helper costs ~0.01 ms.
2. **A helper never opens files.** The app opens the photo and passes the
   open file through the slot (the operating system has a way to hand an
   open file to another process on every platform). A hijacked helper
   holds exactly one photo and nothing else.
3. **Only raw pixels come back out** — through a fixed-size shared-memory
   window whose dimensions the app double-checks before touching. We go
   one step further than the threat model asked: helpers return *only*
   raw pixels, never JPEG files, so nothing a hijacked helper authors is
   ever re-parsed by the trusted app. The app does its own JPEG encoding
   for the thumbnail cache.
4. **A helper crash costs one photo, never the app.** The photo gets one
   retry on a fresh helper, then an honest error tile.
5. **Video playback is the hard case, decided honestly:** the same locked
   room decodes video frames and streams them out through the slot
   (a shared-memory ring). The tempting shortcut — letting Qt's media
   player decode video inside the main app — is a straight violation of
   the threat model at its weakest point, so it is allowed only as an
   explicit, time-boxed, owner-ratified exception if the M1 schedule
   forces it (⚖ argued below, both sides).
6. **The decoders are a versioned, replaceable bundle** — a security fix
   to a decoder ships without shipping a new app.

Nothing here slows the app down measurably: the numbers section shows the
sandbox overhead is microseconds against the milliseconds a decode costs,
and the index-rate budget (§7, ≥ 30 photos/s) survives with a wide margin.

## Scope

Designed here: the process model, the wire interface (requests, responses,
errors, versioning), how one interface serves the three workload classes
(stills/RAW one-shot, index-time batch, video poster + playback
streaming), per-platform authority stripping, the migration plan for the
existing call sites, and the M1 CI escape-gate test plan.

Explicitly not here: the sandbox launcher implementation (i92.3), the
metadata wrapper module (i92.4, decided in
`docs/research/metadata-library-decision.md`), the probe corpus content
(i92.5), and wasm decoder adoption (mechanism B — it slots *inside* this
interface later, invisible to callers, per the threat model).

## 1. Process model

```
 UI process (trusted)                       sandboxed workers (untrusted)
┌─────────────────────────────┐             ┌──────────────────────────┐
│  grid / viewer / indexer    │   control   │ worker 1  (pool, reused) │
│        │                    │  channel +  ├──────────────────────────┤
│  DecodeService facade       │  fd/handle  │ worker 2                 │
│        │                    │  ═════════> ├──────────────────────────┤
│  Broker (opens files,       │             │ ...                      │
│  validates every response,  │  <═════════ ├──────────────────────────┤
│  owns worker lifecycle)     │  shm arena  │ worker N                 │
└─────────────────────────────┘   (pixels)  └──────────────────────────┘
```

### Broker: an in-app component, not a third process

The broker is a module inside the UI process (the "trusted side" of the
threat model), not a separate daemon. It is the only code that opens
original files, the only code that spawns/kills workers, and the only
code that reads worker responses — every response passes its validation
checklist (§2.4) before any other trusted code sees it. Keeping it
in-process avoids a second IPC hop on every job and a second lifecycle to
supervise; the threat model already accepts the broker as trusted code
with a small, audited, later-fuzzed surface.

### Worker pool: persistent, reused, lazily started

Measured on this box (Windows 11, Python 3.13, 2026-07-02 spike run):

| What | Cost |
|---|---|
| Bare interpreter spawn (`python -c pass`, round-trip) | 21.6 ms median |
| Decode-ready spawn (child imports `PySide6.QtGui`, to ready-line) | **92.3 ms median** |
| Round-trip to an already-warm worker (small message, pipes) | **0.012 ms median** (max outlier 17 ms — scheduler noise) |
| Create a 91.6 MiB shm segment | 0.1 ms |
| Copy one 24 MP RGBA frame into shm | 5.9 ms (~15 GiB/s) |

The full worker stack (QtGui + rawpy + PyAV + exiv2) will import slower
than QtGui alone — call it 200–400 ms, to be re-measured in i92.3. The
conclusion is already unambiguous: **per-file worker spawn is ruled out**
(92 ms floor caps a worker at ~11 jobs/s before any decoding — the §7
index budget would hinge entirely on spawn); **a reused worker's per-job
overhead is noise** (12 µs + one small shm copy). So:

- **Pool size:** `min(8, cpu_count)` workers for batch work (mirrors
  today's `INDEX_WORKERS`), **plus one reserved interactive worker** that
  the index queue may never occupy — the viewer/slideshow must stay
  responsive mid-index (N4 spirit; today's thread design has the same
  property by accident of queue ordering, this makes it structural).
- **Lazy start, warm reuse:** no workers before the first decode need;
  the indexer warms the full pool (spawns run in parallel; wall-clock
  cost ~one worker's spawn, well under a second, once per session). The
  viewer's first photo pays one worker spawn (~worst 400 ms) *once per
  session*, overlapped with the instant fcache preview it already paints,
  so the user never stares at a blank viewport for it.
- **Idle policy:** after the index completes, the pool may shrink to the
  one interactive worker (workers are ~50–100 MB RSS each with Qt
  loaded; eight of them idle is real memory). Shrink timer, e.g. 60 s.
- **One outstanding job per worker.** No pipelining inside a worker —
  it keeps kill/timeout semantics trivially attributable. A video
  playback stream pins its worker exclusively for the stream's life
  (§3c).

### Kill, timeout, crash: one decode, never the app

- **Timeout:** every job carries a deadline (defaults: stills decode
  20 s, RAW 30 s, index batch 30 s, poster 30 s, streaming 5 s per-frame
  progress). On expiry the broker kills the worker process outright
  (TerminateProcess / SIGKILL — never a polite request to possibly-owned
  code) and respawns the slot in the background (~92–400 ms, off the
  hot path).
- **Crash (worker exits mid-job):** the outstanding job fails with
  `WORKER_CRASHED`; the broker **retries it once on a fresh worker**
  (distinguishes a transient kill/OOM from a deterministic
  decoder-killer), then marks it a permanent decode failure — the
  existing error-tile path (`length 0` in the fcache; "could not decode
  this file" in the viewer). This is the threat model's "job
  retried/flagged" verification item, and N5's spirit: the app shrugs.
- **Protocol violation** (malformed response, out-of-bounds buffer
  claim, oversized message): treated as *evidence of compromise*, not as
  a bug to tolerate — kill the worker, fail the job permanently (no
  retry: the file is now suspect), log loudly (N7 — this is exactly the
  event the maintenance surface should show), and increment a
  per-session counter the escape-gate tests assert on.
- **App exit:** workers are tied to the app's life by construction
  (§4: Windows job object with kill-on-close; Linux PR_SET_PDEATHSIG +
  pipe-closed exit). No orphan decoders, ever.
- **Cancellation** (user navigated away mid-decode): jobs not yet
  dispatched are dropped from the broker queue (free). A dispatched
  still/RAW job is allowed to finish and its result discarded — the
  viewer's serial-guard pattern today, and cheaper than a 92–400 ms
  respawn. A dispatched job past its class deadline, and any abandoned
  *stream*, is killed. The interactive lane is one worker deep, so the
  worst case is one stale decode of latency, exactly as today.

## 2. The wire interface

Three channels per worker, all created by the broker at spawn:

1. **Control channel** — a duplex byte stream carrying length-prefixed
   JSON messages (4-byte little-endian length, then UTF-8 JSON, hard cap
   64 KiB per message — anything larger is a protocol violation). JSON
   because the vocabulary is small, the payloads are tiny, it needs no
   dependency, and every future host language reads it; the length
   prefix keeps framing binary-safe. **Pixels never travel on the
   control channel.**
   - Linux: an `AF_UNIX` socketpair (needed anyway for fd passing).
   - Windows: an anonymous pipe pair (stdin/stdout of the worker).
2. **File input** — an open, read-only file passed OS-natively per job:
   - **Linux: the fd, via `SCM_RIGHTS`** over the control socket,
     attached to the job message. The kernel-native way to hand a
     capability; the worker can `pread`/`mmap` it but can never open
     anything else (§4).
   - **Windows: `DuplicateHandle` into the worker per job.** The broker
     opened the file with `CreateFile(GENERIC_READ, FILE_SHARE_READ)`;
     it duplicates the handle into the worker process (it owns the
     worker's process handle as its parent) and sends the numeric handle
     value in the job message; the worker adopts it via
     `msvcrt.open_osfhandle`. Chosen over handle *inheritance* because
     inheritance only works for handles that exist at spawn time —
     useless for a persistent pool — and over bytes-over-pipe for the
     reasons below. This is the standard broker pattern (it is how
     Chromium hands files to its sandboxed children).
   - **macOS:** as Linux (`SCM_RIGHTS` over `AF_UNIX`), when hardware
     exists.

   **Why a handle and not bytes over the pipe:** (a) one read — the
   worker reads the file once and hashes/parses/decodes those same bytes
   (the index piggyback, §3b), instead of the broker reading + copying
   them across; (b) zero-copy for 100 MB RAW files and multi-GB videos;
   (c) *seekable* input, which video demuxing requires (moov-at-end MP4s
   defeat pipe input) — the streaming seam (§3c) falls out of the same
   mechanism instead of needing a second one; (d) it matches the threat
   model's own words ("passes fds/buffers; workers cannot open anything
   themselves"). The cost is per-platform passing code — small, and
   quarantined inside the transport.
3. **Response arena** — one shared-memory segment per worker,
   **broker-created, fixed size (default 256 MiB), handle granted at
   spawn**, mapped read-write by the worker and read-only¹ by the broker.
   Anonymous (Linux `memfd_create`, Windows anonymous file mapping via
   `CreateFileMapping(INVALID_HANDLE_VALUE)` + `DuplicateHandle`), *not*
   name-based `multiprocessing.shared_memory` — capability-style: there
   is no name a compromised worker could use to open someone else's
   segment, and nothing the worker says can make the broker map new
   memory. All bulk data (pixels, hashed-bytes side-products) lives at
   `(offset, length)` positions inside this arena, described in the JSON
   response and validated before use (§2.4).

   ¹ Read-only mapping on the broker side is a hardening-ladder item;
   v1 maps it RW for simplicity, which is safe because the broker never
   *executes* or *re-parses* arena content — see §3b.

### 2.1 Version handshake (decoder-bundle versioning)

First message on the control channel, worker → broker:

```json
{ "hello": 1,
  "proto": 1,
  "bundle": { "id": "fauxcasa-decoders",
              "version": "2026.07.02",
              "components": { "qt-imageformats": "6.9.1",
                              "libraw": "0.21.4/rawpy 0.25.0",
                              "libav": "7.1/PyAV 14.x",
                              "exiv2": "0.28.8/python-exiv2 0.18.1" } },
  "ops": ["decode", "index", "poster", "stream"],
  "arena_bytes": 268435456,
  "max_pixels": 67108864 }
```

- `proto` is the wire-protocol major version: the broker requires an
  exact match and refuses the worker otherwise (fail loud, N7). New
  fields are additive within a major; anything structural bumps it.
- `bundle` is the threat-model commitment made concrete: the decoder
  bundle (worker entry point + decoder libraries) is a versioned
  artifact the broker selects at spawn — newest compatible bundle wins —
  so a decoder security release ships as a bundle drop, not an app
  release (§6 footgun 14).
- **The bundle version is recorded in the artifacts it produced**: the
  fcache sidecar and catalog entries gain a `decoded_by` field. Two
  cheap-now payoffs: (a) after a bundle security update, files that
  previously *failed* to decode can be automatically retried (a fixed
  decoder may now read them); (b) a bundle found to produce *wrong*
  output has an exact blast radius for re-index. Retrofitting this after
  caches exist in the wild is the expensive path the bead warns about.
- The broker never trusts `hello` claims for enforcement: `arena_bytes`
  is what the broker allocated (mismatch = protocol violation),
  `max_pixels` is clamped to the broker's own limits.

### 2.2 Request shape

One JSON message per job, broker → worker; the file handle rides
OS-natively (§2). Common fields: `id` (u64, unique per session), `op`,
`deadline_ms` (informational — enforcement is broker-side kill).

```json
{ "id": 41, "op": "decode", "edge": 0, "pixfmt": "RGBA8" }
{ "id": 42, "op": "index",  "levels": [512, 256, 128],
  "want_hash": true, "want_meta": true }
{ "id": 43, "op": "poster", "edge": 512, "at": 0.1 }
{ "id": 44, "op": "stream_open", "slots": 8, "max_edge": 3840 }
```

- `edge: 0` means full resolution; `edge: N` requests a scaled decode to
  fit an N-px long edge (the DCT-scaled decode that makes the indexer
  fast stays available across the boundary).
- `at` for poster: fraction of duration (with a worker-side fallback to
  the first decodable frame).
- Stream control after `stream_open`: `{"id":44,"op":"stream_seek","pts_us":...}`,
  `{"id":44,"op":"stream_free","slot":3}` (broker returns a ring slot),
  `{"id":44,"op":"stream_close"}`. See §3c.

### 2.3 Response shape

```json
{ "id": 42, "ok": true,
  "sha256": "…64 hex…",
  "source": { "w": 6000, "h": 4000 },
  "meta": { "caption": "…", "keywords": ["…"], "taken": "2019:07:14 10:31:02",
            "gps": [60.72125, -135.05685], "rating": 3,
            "faces": [ {"name": "…", "x": 0.31, "y": 0.22, "w": 0.11, "h": 0.14} ] },
  "levels": [ { "w": 512, "h": 341, "stride": 2048, "pixfmt": "RGBA8",
                "off": 0, "len": 698368 },
              { "w": 256, "h": 171, "stride": 1024, "off": 698368, "len": 175104 },
              { "w": 128, "h": 85,  "stride": 512,  "off": 873472, "len": 43520 } ] }

{ "id": 41, "ok": false, "error": "CORRUPT", "detail": "libjpeg: bad Huffman code" }
```

Every pixel buffer is described by the fixed shape
`{w, h, stride, pixfmt, off, len}` — offset/length into that worker's
arena. `pixfmt` is a closed enum: `RGBA8`, `BGRA8`, `GRAY8` (v1 may ship
`RGBA8` only; the enum exists so adding one is additive).

### 2.4 The validation checklist (the trusted side's whole job)

Applied to *every* buffer description before any trusted code touches
arena bytes; any failure is a protocol violation (§1 semantics — kill,
fail, log):

1. `0 < w ≤ MAX_EDGE` and `0 < h ≤ MAX_EDGE` (default 32 768) and
   `w*h ≤ max_pixels` (default 64 MP — a §10-style revisit knob; larger
   panoramas get an honest `TOO_LARGE` error, and a "tiled decode"
   follow-up bead exists rather than a silent cap).
2. `stride ≥ w * bpp(pixfmt)` and `stride % 4 == 0`.
3. `len == stride * h` exactly.
4. `off ≥ 0`, `off % 4 == 0`, `off + len ≤ arena_bytes` (checked with
   overflow-safe arithmetic; buffers within one response must not
   overlap).
5. String fields clamped: caption ≤ 8 KiB, ≤ 64 keywords, ≤ 128 faces,
   face geometry finite and within [0, 1]; `sha256` exactly 64 lowercase
   hex chars; unknown JSON fields ignored (additive evolution), unknown
   *ops or pixfmts* rejected.
6. The broker **copies** what it keeps out of the arena before releasing
   the slot for the next job — no trusted-side references into memory a
   worker can still write.

This checklist is codified as `PixelBuffer.validate()` in the skeleton
module — the one piece of the skeleton with real code, because it *is*
the security boundary.

### 2.5 Error taxonomy

| Code | Meaning | Broker behaviour |
|---|---|---|
| `UNSUPPORTED` | no bundled decoder claims the format | permanent; error tile; N7-visible per-extension count |
| `CORRUPT` | decoder engaged and failed (truncated/hostile/broken file) | permanent; error tile |
| `TOO_LARGE` | dimensions/pixels beyond caps (checklist item 1, or worker-side pre-check) | permanent; error tile + honest message (not a silent skip) |
| `TIMEOUT` | deadline hit; broker killed the worker | retry once on a fresh worker, then permanent |
| `WORKER_CRASHED` | process died mid-job | retry once on a fresh worker, then permanent + flagged |
| `PROTOCOL` | malformed/malicious response | no retry; kill worker; loud log; escape-gate counter |
| `CANCELLED` | broker withdrew the job | silent |

`CORRUPT` vs `UNSUPPORTED` matters for the maintenance surface ("what
does the app think about this file?") and for the bundle-update retry
(§2.1): both are retried after a bundle version bump, neither before.

## 3. Three workload classes, one interface

### (a) Stills / RAW one-shot: fd in → pixels out

`op: "decode"` — the viewer's `load_original`, the slideshow's dwell
prefetch, and any future 1:1-zoom tile source. The worker: reads from the
handed fd, decodes (QImageReader for stills; rawpy for RAW by extension
routing — the v46.1 TIFF-container trap is worker-internal), applies EXIF
auto-orientation (so the boundary's output is display-upright, matching
today's contract), writes pixels to the arena, answers with one
`PixelBuffer`. The Picasa `rotate=` quarter-turns stay a trusted-side
display transform, exactly as today.

RAW note: LibRaw's large intermediate buffers live inside the worker and
die with it; only the final (possibly `edge`-scaled) RGBA output crosses.
A 24 MP full decode costs one ~92 MiB arena write (~6 ms measured) — noise
against a multi-hundred-ms RAW demosaic. The 256 MiB default arena holds
a 64 MP RGBA frame; `max_pixels` and arena size move together.

### (b) Index-time batch: the piggyback preserved

`op: "index"` — one worker visit per file does everything today's
`_index_one` does in one thread visit: **read once from the fd → sha256
those bytes (N6 identity) → parse metadata from those bytes → scaled
decode → downscale to each level**. The bytes-in-hand piggyback survives
the boundary because the *worker* holds the bytes, not the broker.

- Metadata parsing is the i92.4 wrapper (python-exiv2, bytes-mode) —
  bytes-mode is not a workaround here, it is the native shape (the
  metadata memo: "the 'workaround' is the design"). The ExtendedXMP shim
  runs inside the worker too (same untrusted bytes).
- **Deliberate strengthening — pixels out, never encodings: ⚖ argue.**
  The threat model's text has thumbnails "re-encoded by our encoder …
  inside the sandbox", and trusts cache files by provenance. But under
  the threat model's own assumption (the worker *will* eventually be
  compromised), a worker-authored JPEG is attacker-authored bytes that
  the *unsandboxed* grid decodes on every scroll — a clean two-stage
  escape route (hostile original → compromised worker → hostile thumb →
  UI-process JPEG decoder). This design closes it: **workers return raw
  pixel buffers per level; the broker (trusted side) JPEG-encodes them
  into the fcache.** Encoding trusted, shape-validated pixel buffers is
  categorically safer than decoding attacker-structured input — the
  encoder consumes plain arrays, not parseable structure. Cost: ~1.4 MiB
  of arena per photo for the (512, 256, 128) levels (~0.1 ms copy) and
  the JPEG-encode CPU moves to the trusted side (~1–3 ms/photo,
  parallelized on a small trusted thread pool whose *input is trusted* —
  threads are fine there). The provenance claim becomes airtight:
  *nothing the UI process ever parses was authored inside the sandbox.*
  The argue-side: it edits a ratified-in-substance document's mechanism
  detail (the threat model's *intent* — cache trusted by provenance — is
  preserved and strengthened; i92.1 ratification should absorb this
  amendment), and it spends trusted-side CPU the sandbox was supposed to
  offload. The hash result is worker-reported and a compromised worker
  can lie about it — accepted, matching the threat model's "lie about
  pixel content" residual: mislabeling the hostile file's own identity
  corrupts nothing else.
- Per-photo overhead vs today's threads: ~12 µs round-trip + ~0.1 ms
  arena copy + fd passing (µs) — against ~7 ms/photo of decode work at
  the measured 139 photos/s. Processes also end the GIL truce the
  current design carefully maintains (the scaled-decode-releases-the-GIL
  trick): workers parallelize *regardless* of what rawpy/exiv2/PyAV do
  with the GIL, which threads could never promise for the M1 decoder
  matrix. Expected index rate: ≥ today's, comfortably ≥ the §7 30/s gate
  (§6 does the arithmetic).

### (c) Video: poster grab and the playback seam

**Poster (one-shot):** `op: "poster"` is interface-identical to `decode`
— fd in, one `PixelBuffer` out — so the indexer treats a video like a
still with a different clock budget. Engine, decided: **in-worker libav
(PyAV) reading the handed fd through a seekable file object**, not an
ffmpeg *subprocess* inside the worker. Reasons: (a) a grandchild process
punches a hole in the sandbox exactly where it should be tightest —
Linux seccomp/Landlock would have to permit `execve`, Windows the
child-process policy; (b) ffmpeg-the-CLI wants a *named* input, and the
worker has no filesystem — stdin-pipe input defeats moov-at-end MP4s
(real family archives are full of them); (c) one wire protocol for every
op. The v46.2 plan (imageio-ffmpeg subprocess, per the fetch-videos.py
precedent) remains the right *pre-sandbox interim* — it becomes the
in-worker PyAV path when this service lands, behind the same facade
call. ⚖ argue: this adds PyAV to the bundle where v46.2/.3 planned
imageio-ffmpeg + QtMultimedia's ffmpeg; the counterweight is that the
bundle then carries libav *once*, versioned, serving poster and
playback both. (If i92.3 hits a PyAV wall, the documented fallback is
ffmpeg-as-the-worker-binary — the sandbox wraps the ffmpeg *process
itself* with argv composed by the broker and output as rawvideo on a
pipe; it costs a second protocol and loses seekable input, which is why
it is the fallback.)

**Playback streaming — the honest seam. ⚖ argue (the big one).**

Two real options, compared without flinching:

| | A. Sandboxed decoder → shm frame ring | B. QtMultimedia (QMediaPlayer, ffmpeg backend) in-process |
|---|---|---|
| Threat model | **Compliant.** Untrusted container/codec parsing stays in the locked room; the UI receives fixed-shape frames + PCM. | **A direct violation** — "no ambient authority, no exceptions" — at the *highest-risk* spot in the whole matrix (ffmpeg demuxers/codecs are the CVE fountain the threat model quotes). |
| Mechanism | Stream pins one worker. Ring of K slots in the arena (e.g. 8 × max_edge frame); worker decodes ahead, `frame {slot, pts}` messages; broker paints QImages (no QtMultimedia video path needed) and returns `stream_free {slot}`; audio as PCM chunks in a reserved arena region, played through `QAudioSink` — *whose input is our sandbox's output, not attacker bytes*. A/V sync on the audio clock; seek = `stream_seek` + ring flush. Backpressure = ring full. | Ship v46.3 as written: QMediaPlayer + QVideoWidget, `QT_MEDIA_BACKEND=ffmpeg` pinned (no silent WMF fallback, N7), QtMultimedia + ffmpeg DLLs into the bundle. |
| Engineering cost | The real price: A/V sync, seeking, EOF, rate control — a solid one-to-two-week seam, *scoped* by M1 needs (play/pause/seek/position — the v46.3 transport surface, no filters, no playlists). Software decode of 1080p is trivial on 2020s hardware; 4K H.264/HEVC software decode is the perf risk — measure in i92.3; hardware decode is forfeited in v1 (ladder item: hw-decode *inside* the sandbox where the platform allows). | Days, not weeks. Uses Qt's tested A/V machinery, hardware decode included. |
| Exposure honesty | Playback and *indexing* both covered — one boundary for every byte of every video. | Poster/index stays sandboxed (that is the drive-by mass-exposure path: *every* file, automatically, at index time). Playback exploitation requires the user to double-click the specific hostile file — user-initiated, per-file, materially narrower. Still: "double-click a video someone sent you" is precisely a family-archive attack story. |

**Recommendation: A is the design.** The decode service streams frames
over the shm ring; the interface in §2.2/§2.3 already carries it, so
implementing A is pool work, not redesign work — which is exactly the
retrofit-risk the bead exists to kill. **B is permitted only as an
owner-ratified, time-boxed M1 schedule valve**, and only with all four
conditions: (1) index/poster of video *still sandboxed* (the automatic
mass-exposure path stays closed); (2) `QT_MEDIA_BACKEND=ffmpeg` pinned
and the QtMultimedia-ffmpeg DLLs versioned *inside the decoder bundle*
so the security-update cadence holds for them too; (3) a written line in
the threat model's residual-risks section naming the exception and its
expiry (M2 — the "trust it with changes" milestone must not ship with an
in-process video decoder); (4) a standing bead for the A migration. The
owner should overrule A only by accepting those four in writing — that
is the ⚖ argument.

## 4. Per-platform authority stripping

Shared pattern: **two-phase startup**. Phase 1 (at spawn, full-ish
authority): import the decode stack, map the arena, say `hello`. Phase 2
(before the first job): the worker irreversibly drops what it can drop
from inside, the broker independently applies what only a parent can
apply — and the broker sends no job until the worker reports `locked`
plus, on Linux, the broker has verified `no_new_privs`/seccomp state via
`/proc/<pid>/status`. (CI escape gates are the real verification; the
report is just sequencing.) Workers must import *everything* during
phase 1 — Python's lazy imports would otherwise fail post-lockdown; the
worker warmup routine imports the full stack and touches the codec paths
it needs, and the escape gates run every op against a locked worker to
catch stragglers.

| | Linux (v1 minimal) | Windows (v1 minimal) | macOS (when hardware exists) |
|---|---|---|---|
| Process container | `unshare`: user, mount, PID, net, IPC, UTS namespaces; empty tmpfs root with the Python runtime + decoder bundle bind-mounted read-only (bubblewrap-class, done directly or via bwrap when present) | **AppContainer** (`CreateAppContainerProfile` + spawn with the container SID, zero capabilities) inside a **job object**: `ACTIVE_PROCESS=1`, `KILL_ON_JOB_CLOSE`, `JOB_OBJECT_UILIMIT_ALL`, per-process memory limit | `sandbox_init` with a deny-default profile: allow read of the app bundle + runtime, mach essentials; nothing else |
| Filesystem | Landlock ruleset (phase 2): read-only on the runtime + bundle mounts, deny everything else, deny all writes | AppContainer default-deny; the runtime/bundle dir gets a read ACL for the container SID (static, read-only — the Windows analogue of the bind mounts) | profile `file-read*` on bundle only; no writes |
| Network | net namespace = no interfaces at all | **no AppContainer capabilities = no network** (this is *why* AppContainer is v1, not the ladder: a restricted token alone does not block the network, and the M1 escape gate asserts "cannot reach the network" per platform) | profile denies `network*` |
| New processes | `RLIMIT_NPROC` floor + seccomp-deny `execve` (ladder formalizes); PID namespace contains any surprise | `PROC_THREAD_ATTRIBUTE_CHILD_PROCESS_POLICY` = no child processes; job object active-process cap as belt-and-braces | profile denies `process-exec` |
| Resources | `RLIMIT_AS` (e.g. 2 GiB), `RLIMIT_CPU`, `RLIMIT_NOFILE` (tiny), `RLIMIT_CORE=0` | job object memory + (ladder) CPU rate control | rlimits |
| Lifetime tie | `PR_SET_PDEATHSIG(SIGKILL)` + exit-on-control-EOF | `KILL_ON_JOB_CLOSE` | exit-on-control-EOF |
| Misc v1 | `no_new_privs`; close every fd except control + arena | DEP permanent, force-ASLR, heap-terminate-on-corruption mitigation policies | hardened runtime flags |
| **Hardening ladder** (post-v1, in rough order) | seccomp *allowlist* (read/pread/mmap/mremap/munmap/futex/clock_gettime/write-to-control/exit…); cgroup v2 memory+pids; broker-side read-only arena mapping; wasm decoders (mechanism B) shrinking native-parser exposure | LPAC (less-privileged AppContainer); **win32k syscall disable** (the threat model names win32k as the residual surface — needs verification that QtGui image codecs run offscreen without user32/gdi32 syscalls, hence ladder not v1); Untrusted integrity level post-warmup; code-integrity policy | App Sandbox entitlements for distribution; same wasm trajectory |

The v1 sets are deliberately the *minimal honest* ones: each row exists
to make a specific escape-gate assertion pass (§7), not to look
impressive. Every ladder item gets its own bead at i92.3 close.

## 5. Skeleton interface module

`apps/desktop-python/decodesvc.py` (committed with this design; imported
by nothing yet): the closed enums (`PixelFormat`, `ErrorCode`), the
dataclasses for `hello`/requests/results, `PixelBuffer.validate()`
implementing §2.4 items 1–4 for real (it is the boundary's core
invariant and deserves to be born tested), an abstract `Transport`, and
the `DecodeService` facade with the call-site-facing signatures:

```python
svc.decode(path, edge=0)            -> DecodeResult      # viewer, slideshow
svc.index(path, levels=(512,256,128)) -> IndexResult     # thumbcache builder
svc.poster(path, edge=512, at=0.1)  -> DecodeResult      # video index
svc.open_stream(path, max_edge, slots=8) -> StreamHandle # playback
```

Two transports will implement it in i92.3: `SandboxedPoolTransport` (the
real thing) and `InProcessTransport` (today's behaviour behind the new
interface — the migration vehicle, below).

## 6. Migration plan and the perf budget

**Facade first, sandbox second.** The step that makes everything else
cheap: introduce `DecodeService` with `InProcessTransport` and move call
sites onto it *before* the sandbox exists. Flipping to
`SandboxedPoolTransport` is then a construction-site switch, not a
call-site rewrite — and the in-flight v46.1 (RAW) and v46.2 (video
poster) work should target the facade from day one so RAW and video are
born migrated.

Order, with reasons:

1. **thumbcache indexer** (`build_cache`/`_index_one` → `svc.index`) —
   first because it is the *mass-exposure* path (every untrusted file,
   automatically, at index time — the drive-by story) and the §7
   perf-gate risk lives here; prove both at once on the 100k benchmark
   library. The trusted side keeps: catalog writes, fcache packing, and
   (new, §3b) JPEG encoding of returned pixel levels.
2. **viewer + slideshow** (`load_original` → `svc.decode`) — the
   remaining original-decode path; the slideshow's dwell prefetch rides
   the same call. The persistent-single-worker discipline the viewer
   grew in PR #40 maps 1:1 onto the reserved interactive worker.
3. **RAW (v46.1) and video poster (v46.2)** — land on the facade
   directly (coordinate with those beads; if they ship before the
   facade, their call sites are the two functions above anyway).
4. **Video playback (v46.3)** — per the §3c decision.

**Stays outside the sandbox, by provenance:** `grid._decode_worker`
(reads only app-written fcache blobs — and §3b makes that provenance
airtight), and `viewer._load_preview` (same fcache). The metadata
*ini/db3/contacts* parsers also stay outside for now — they are not in
this bead's scope; the threat model's boundary names in-file
EXIF/XMP/MakerNote parsing (which moves inside via §3b), while
Picasa-sidecar parsing is a separate risk conversation (candidate
follow-up bead).

**The §7 arithmetic** (budget: ≥ 30 photos/s local, ≥ 10/s slow volume;
today's measured in-app rate: 139/s on 8 threads):

| Component | Per photo | Basis |
|---|---|---|
| Decode + hash + metadata (in worker) | ~7 ms | today's 139/s, same work, same scaled decode |
| Control round-trip | 0.012 ms | measured (warm worker) |
| fd/handle passing | ~0.01 ms | one syscall-ish op (SCM_RIGHTS/DuplicateHandle) |
| Arena write + broker copy-out, ~1.4 MiB levels | ~0.2 ms | measured 15 GiB/s, ×2 for both sides |
| Trusted-side JPEG encode (3 levels) | 1–3 ms | *parallel, trusted-input threads; overlapped with worker decode* |
| One-time pool spawn (8 workers) | ~0.1–0.4 s per session | measured 92 ms/worker (QtGui); full stack TBD in i92.3 |

Worker-side cost per photo rises ≪ 5%; the encode moves to the broker
side but runs on otherwise-idle trusted threads concurrently with the
next photo's sandboxed decode, so pipeline throughput holds. Processes
also remove the GIL ceiling that threads would hit the moment rawpy or
exiv2 holds the lock. Estimate: **the sandboxed pool lands at ≥ today's
rate within noise, 4–5× above the 30/s gate** — and the gate itself (§7
row, CI) is the enforcement, not this estimate. Risk to watch in i92.3:
Windows AppContainer spawn is costlier than a bare spawn (profile setup,
ACLs) — it is off the hot path (pool warm-up + crash refill only), but
measure it against the "crash costs one decode" promise (a crash also
costs one slot's refill latency; the pool absorbs it because the other
workers keep draining the queue).

## 7. M1 CI escape gates (the test plan i92.5 implements)

From the threat model's verification section, made concrete:

1. **Probe worker (per platform).** A *cooperating malicious* worker —
   same spawn path, same sandbox, hostile payload — attempts, and must
   fail: open a file outside its handed fd (absolute, relative, and
   `..`-traversal; on Windows also `\\?\` and device paths; registry
   read/write); create/connect a socket (TCP, UDP, DNS lookup);
   `execve`/`CreateProcess`; write anywhere but its arena (tmp dirs,
   the library, the cache); exceed its memory limit without being
   killed; outlive a broker kill (job-object/PDEATHSIG check); on
   Windows additionally clipboard/SendMessage/desktop handles
   (UILIMIT_ALL). **Any success fails the build.** Runs on
   ubuntu-latest + windows-latest; macOS enters the matrix with the
   hardware (§8 of the spec is explicit that this is blocked on access,
   not intent).
2. **Hostile-file corpus through the real pool.** Truncated, fuzzed, and
   dimension-lying JPEG/PNG/GIF/TIFF/WebP/RAW/MP4 samples (synthetic —
   privacy rule — seeded from the format research; grown by fuzz smoke
   over time): every file yields a taxonomy-correct error or valid
   pixels, zero app-process crashes, zero `PROTOCOL` events *from the
   stock worker* (a protocol event here means our own worker is buggy).
3. **Malicious-response fuzz (broker-side).** A fake worker speaks the
   protocol and lies: oversized `len`, overlapping buffers, `off` past
   the arena, negative/huge dims, 100 MB captions, unknown ops, garbage
   JSON, mid-message EOF. The broker must take the §1 protocol-violation
   path every time — this is the §2.4 checklist's unit-test suite, and
   it can run on every platform cheaply (no sandbox needed: it tests the
   *trusted* side).
4. **Crash robustness (N5 tie-in).** `kill -9` / `TerminateProcess` a
   worker mid-decode on a *valid* file: job retried once and succeeds;
   UI-facing state intact; pool refills; no stall. Repeat under
   index-in-flight load.
5. **Lockdown sequencing.** Assert no job is dispatched before `locked`;
   assert a worker that skips phase 2 is refused.
6. **Version handshake.** Wrong `proto` major → refused loudly;
   bundle-version bump → previously-`CORRUPT` files are re-queued
   (the §2.1 retry contract).

Gate placement: 1–3 land with i92.3 (the pool cannot merge without
them — the threat model calls them the M1 gate); 4 extends the existing
N5 story; 5–6 are cheap unit gates. Fuzz *smoke* (continuous corpus
growth) is i92.5's ongoing half.

## 8. Deviations, open items, candidate beads

- **Deviation from the threat-model text (flagged, ⚖ in §3b):** encoding
  moves to the trusted side; workers return only raw pixels. Strictly
  stronger; needs a one-line amendment when i92.1 ratifies.
- **PyAV in the bundle** (⚖ in §3c) — touches v46.2/.3's stated tooling;
  those beads should be updated when this design is ratified.
- Candidate follow-up beads (report-only, not filed): tiled/streamed
  decode for > 64 MP panoramas (today: honest `TOO_LARGE`); sandboxing
  the *Picasa-sidecar* parsers (ini/db3/contacts.xml — outside this
  bead's boundary but they also parse foreign bytes); hardware video
  decode inside the sandbox (ladder); worker RSS budget + idle-shrink
  tuning; AppContainer spawn-cost measurement on the refill path;
  win32k-disable compatibility test for QtGui offscreen decode.

## Appendix: raw measurements (2026-07-02, Windows 11 dev box, Python 3.13.1)

```json
{ "bare_spawn":          { "min_ms": 21.161, "median_ms": 21.631, "max_ms": 23.945, "n": 15 },
  "decode_ready_spawn":  { "min_ms": 90.904, "median_ms": 92.325, "max_ms": 94.702, "n": 15 },
  "persistent_roundtrip":{ "min_ms": 0.010,  "median_ms": 0.012,  "max_ms": 17.326, "n": 200 },
  "shm_frame_copy":      { "frame_mib": 91.6, "segment_create_ms": 0.1,
                           "copy_min_ms": 5.9, "copy_gib_per_s": 15.1 } }
```

Produced by `docs/research/spikes/decode-worker-spawn-spike.py`
(PEP 723; `uv run` it to reproduce on any machine).
