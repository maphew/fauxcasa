# Decode isolation: threat model and mechanism choice

**Status:** M0-exit deliverable (fauxcasa-bdj). Spec §5 Formats sets the
requirement and the timing: the isolation mechanism is "chosen with the
stack (fauxcasa-6hf) against a written threat model, at M0 exit"; §10
item 12 separately names wasm's decode-sandbox role. Written 2026-06-12.
**Decision state: decided** — ratified by the owner (maphew,
2026-08-11, fauxcasa-i92.1), absorbing the decode-service design's
strengthening amendment (workers return *only raw pixels*; the trusted
side does all encoding — `docs/design/decode-service.md` §3b) and
**declining the QtMultimedia M1 schedule valve**: video playback ships
as the sandboxed streaming seam (shm frame ring + PCM audio), per the
design's own recommendation. Originally an agent draft written
alongside the stack-decision report
(`docs/research/stack-balloons.md`); overturnable by argument like
every decision, but the burden now sits with the challenger.

## Why this document exists

Decoding untrusted files is Fauxcasa's primary attack surface. The
evidence is Picasa's own history: its final years saw three separate
decoder-vulnerability patch rounds, and the decoder families we must
bundle (libjpeg-class, PNG, TIFF, GIF, WebP, LibRaw-class RAW, ffmpeg-class
video) have a continuous CVE stream to this day. The spec's requirement
is binding: **decode of untrusted input runs with no ambient authority**
(§5 Formats), decided before M1 work begins. In plain words, "no ambient
authority" means: the code that opens a photo may touch only the single
photo it was handed — not your other files, not your disk, not the
internet — so even a booby-trapped photo that fully hijacks the decoder
gets nothing.

## Assets

1. **The family archive** — irreplaceable originals on disk. Threat: a
   compromised decoder writes/deletes/encrypts them. This is the asset
   the whole project exists to protect.
2. **Library state** (tier-2 sidecars, library-home files) — corruption
   or exfiltration of names, faces, places.
3. **The user's machine and accounts** — code execution beyond the app;
   network exfiltration of photos or credentials.

## Attacker model

A single malicious media file that the app will index and render,
arriving by any of the normal paths a family archive accretes files:
saved email attachments, USB sticks and SD cards, a NAS share other
machines write to, files downloaded by other household members, a
maliciously crafted photo shared into a group chat and auto-saved. The
attacker controls file bytes completely (every container field, every
EXIF/MakerNote byte, every codec stream) and may target any bundled
decoder or metadata parser. We assume the decoder component **will**
eventually be compromised by some input; the design question is what
that buys the attacker.

Out of scope: an attacker with existing code execution on the machine;
malicious *applications* sharing the library (covered by N5/N6
robustness, not security isolation); availability attacks that merely
crash a decode worker (the app must shrug those off anyway per N5).

## Trust boundaries

```
untrusted bytes ──> [ DECODE SERVICE ]  ──pixels/parsed fields──> trusted UI/index
 (originals,          sandboxed,                (plain data,
  any source)         no ambient authority)      validated shapes)
```

- **Original file bytes are always untrusted input** — including
  embedded thumbnails, XMP/IPTC/EXIF blocks, and MakerNotes. The
  metadata parser (exiv2-class) sits inside the same boundary as pixel
  decoders.
- **Decoder output is data, not code**: fixed-shape pixel buffers and
  length-checked field lists. The receiving side validates dimensions
  and sizes before use.
- **The app's own cache artifacts are trusted by provenance**: workers
  return *only raw pixel buffers* — never encoded files — and the
  trusted side does its own JPEG encoding for the thumbnail cache, so
  nothing a hijacked worker authors is ever re-parsed by trusted code
  (strengthened from the original "re-encoded inside the sandbox"
  wording by `docs/design/decode-service.md` §3b, absorbed at
  ratification). The cache files the UI reads (the grid's hot path,
  N4) are thus our own output, not attacker bytes. The UI process
  never decodes an original. (This is exactly the architecture the
  stack trial balloons benchmarked: the grid reads only the packed
  thumb cache.)

## Mechanism options considered

In plain words first:

- **A — locked room with a mail slot.** Decoding runs in separate helper
  processes the operating system has stripped of all powers: a photo is
  passed in through the slot, pixels come back out, and the room has no
  other doors (no file access, no network).
- **B — sealed virtual machine.** The decoder programs are rebuilt to
  run inside a wasm container that physically has no way to reach the
  outside unless we add one.
- **C — safer building materials.** Rewrite decoders in a language that
  prevents the most common kind of security bug — but they still run
  inside the main app, with all the app's powers, so this alone never
  meets the requirement.

| Mechanism | What it buys | Costs / limits |
|---|---|---|
| **A. Sandboxed decode subprocess pool** — decode runs in worker processes stripped of ambient authority: Linux `bubblewrap`-class namespace + seccomp (or Landlock); Windows AppContainer / restricted token + job object; macOS App Sandbox / `sandbox-exec` profile. Input arrives as an open fd or shared-memory blob; output leaves as pixel buffers over shm; no filesystem view, no network. | Works with the *real* decoder matrix unchanged (ffmpeg-class, LibRaw-class — the §5 "updatable decode library" requirement); crash isolation for free (a decoder segfault costs one job, satisfying N5's spirit); per-job kill/timeout. | Per-platform sandbox engineering (three implementations of "strip authority"); IPC design; care that the broker hands workers only the one file. |
| **B. wasm-compiled decoders** (wasmtime/wasmer runtime; codecs compiled to wasm32-wasi) | Capability-based by construction, byte-identical sandbox on all three platforms; in-process speed; the spec already names wasm as the natural decode-sandbox + extension-API substrate (§10 item 12). | Codec coverage is the blocker today: jpeg/png/webp compile well; LibRaw is feasible-with-effort; ffmpeg-class video in wasm is still hard/slow. ~1.2–2× decode-time tax. Runtime maturity risk owned by us. |
| **C. Memory-safe (Rust) decoders in-process** | Eliminates the memory-corruption bug class at the source for covered formats. | Coverage gaps exactly where risk is highest (vendor RAW matrix, video); "memory-safe" crates still embed unsafe/C under the hood; logic bugs still parse attacker input in-process with full authority — does not meet "no ambient authority" on its own. |

## Decision (ratified by owner — maphew, 2026-08-11, fauxcasa-i92.1)

**A is the floor, B is the trajectory, C is never sufficient alone.**

1. **All decoding and metadata parsing of original files happens in a
   sandboxed worker pool (mechanism A), on all three platforms, from
   M1.** No exceptions for "simple" formats — JPEG parsers have the
   richest exploit history of all. The broker process opens files and
   passes fds/buffers; workers cannot open anything themselves.
2. **The UI/index process decodes only app-written cache artifacts**
   (thumbnails, preview tiles). This keeps the N4 hot path fast and
   outside the sandbox boundary by provenance, not by exception.
3. **wasm decoders (mechanism B) are adopted opportunistically** —
   starting with stills codecs where the wasm port is mature — inside
   the same worker-pool interface, so the host stack never cares which
   engine ran. This is also the seed of the later extension API the
   spec names. Video stays ffmpeg-in-A for the foreseeable future —
   including **playback**: the owner declined the QtMultimedia
   in-process schedule valve (`docs/design/decode-service.md` §3),
   so v46.3 ships as the sandboxed frame-streaming seam and no
   residual-risk exception is recorded here.
4. Memory-safe decoder implementations are welcome *inside* the sandbox
   (defense in depth), never as a substitute for it.

The mechanism is deliberately **stack-independent**: every candidate
host (Python, Rust, Go, web-shell) can spawn and supervise a sandboxed
worker pool; none of them changes the cost materially. This means the
threat model does not constrain the §10 item 12 stack choice — and the
stack choice cannot weaken the isolation requirement.

## In-process decoders (documented exception)

Three format fallbacks decode in-process today, on all platforms,
outside the sandboxed worker pool the Decision above prescribes as the
floor from M1: **PSD** (fauxcasa-v46.4), **16-bit TIFF** (fauxcasa-v46.7)
and **HEIC/HEIF** (fauxcasa-y5b). All three share one shape: the pinned
PySide6 build ships no Qt plugin for them (PSD, HEIC/HEIF) or Qt's own
plugin corrupts the pixels (16-bit TIFF grayscale silently clips to
white on Linux), so `pillowload.py` decodes the same bytes with Pillow
instead — for HEIC/HEIF, Pillow itself has no built-in HEIF reader, so
`pillowload.py` additionally registers **pi-heif**'s opener (PyPI
`pi-heif`, wrapping libheif 1.23.0 + the libde265 HEVC decoder,
LGPLv3 — see `docs/research/heic-decode-decision.md` for the licensing
analysis and why pi-heif was chosen over pillow-heif). This is a
sandbox-worker gap, not a wasm-adoption gap: none of the three route
through mechanism B either, they simply never left the UI/index
process.

The risk is real, not theoretical, for HEIC/HEIF specifically: libde265
has a public CVE history (heap overflows and out-of-bounds reads in
malformed-bitstream handling), and it now runs in-process against
attacker-controlled bytes on every `.heic`/`.heif` file a library
contains, with the same ambient authority as the rest of the UI/index
process. PSD and 16-bit TIFF carry the analogous risk for Pillow's own
PSD/TIFF codecs, already accepted before this bead.

Tracked for migration into the sandboxed worker pool under
**fauxcasa-i92** (the decode-isolation epic this document belongs to),
not resolved here: moving these three fallbacks behind the broker/worker
boundary closes the gap without changing `pillowload.py`'s bytes-in/
pixels-out interface, which was deliberately kept sandbox-service-shaped
for exactly this future move (see `pillowload.py`'s module docstring).

## Verification (becomes CI gates)

- **M1 gate (with decode isolation landing):** a test worker, handed a
  hostile-format probe corpus, demonstrably cannot (a) open a file
  outside its handed-in fd, (b) reach the network, (c) write anywhere
  but its output shm — asserted per platform.
- **Crash robustness (ties to N5 kill-fuzzer):** kill -9 a worker
  mid-decode → job retried/flagged, app state intact, no UI stall.
- **Fuzz smoke:** the decoder corpus (vendor RAW samples, truncated/
  malformed files from the format research) runs through the pool under
  the sandbox in CI; any worker escape attempt fails the build.
- Decoder libraries are *updatable* (§6 footgun 14): the pool interface
  versions the decoder bundle so security updates ship without app
  releases.

## Sandbox degraded on a Dev Drive / non-owned volume (operational note)

`decodefacade` logs `decode sandbox failed to start, degrading to
in-process: ...` and the status bar says the sandbox is off. On Windows the
usual cause when running from a **source checkout** is the volume the
checkout sits on, not the code (fauxcasa-yfq, found 2026-09-15 on the dev
box's `A:` ReFS drive).

What is going on: the AppContainer token only gets read access to a path
if that path's ACL grants it to the container SID (or to `ALL APPLICATION
PACKAGES`). At spawn, `decodesvc_win.grant_read_execute_once` adds that ACE
to three directories: the base interpreter dir, the worker `PYTHONPATH`
(the PySide6 site-packages) and the directory holding
`decodesvc_worker_win.py`. The grant is best-effort by design (a Program
Files install already grants `ALL APPLICATION PACKAGES` and denies
`WRITE_DAC`, and must still spawn), so on a volume that refuses it, such
as a Dev Drive / ReFS volume whose root ACL is `Authenticated Users:
Modify` with no `WRITE_DAC`, or any volume the user does not own,
`SetNamedSecurityInfoW` fails with `err=5` even on an empty directory the
user just created, and `icacls /grant` fails the same way. The worker
process then cannot open its own script: the interpreter prints
`can't open file ...: [Errno 13] Permission denied` and exits 2 before it
ever speaks the protocol.

What happens now:

- The grant failure is logged at **WARNING** with the path (once per path
  per process), not INFO.
- A worker that exits before its hello frame has its remaining stdout and
  stderr read into the spawn error, so the reason names the failed grant
  and the unreadable path instead of the earlier red herring (`PROTOCOL:
  incoming control frame 1432107587 bytes exceeds MAX_CONTROL_MSG`, which
  was the second diagnostic line's first four bytes `C:\U` read as a frame
  length). A non-zero pre-hello exit is reported as `WORKER_CRASHED`, not
  `PROTOCOL`: the process never processed untrusted input, so nothing it
  printed is evidence of compromise.
- When the grant on the **worker-script directory** fails, the broker
  copies `decodesvc_worker_win.py` to
  `%LOCALAPPDATA%\Fauxcasa\cache\sandbox-worker\<sha256[:16]>\` (the same
  root the ACL markers use, always grantable), grants that directory, and
  launches from the copy. The copy is content-hashed and re-verified byte
  for byte on every spawn, so an edited source never runs from a stale
  copy; hash directories a day old are pruned. Only that one file moves:
  the worker is deliberately self-contained (stdlib, ctypes and PySide6
  only, no repo sibling imports, since its import list is the sandbox's
  attack-surface budget), so there is no import closure to chase. This
  mirrors how the frozen bundle already behaves, with its payload under
  Program Files or `%LOCALAPPDATA%`.
- The **PySide6 site-packages cannot be staged**: it is hundreds of MB of
  Qt, and copying it per spawn is not a fallback. If the uv cache or venv
  is on the same ungrantable volume (for example `UV_CACHE_DIR` pointed at
  the Dev Drive), the sandbox still degrades, and the error now says so
  and names the fix: put the uv cache / venv on a grantable volume (unset
  `UV_CACHE_DIR` so it defaults to `%LOCALAPPDATA%\uv`), or set
  `FAUXCASA_WORKER_PYTHON` to an interpreter whose site-packages is on
  one. The base interpreter dir is under `%APPDATA%\uv\python` for
  uv-managed CPython, which is grantable.

None of this affects the threat model's properties: the staged copy is a
byte-identical file in a directory the container receives read+execute on,
exactly what the source directory would have received had the grant
worked, and the pre-hello output is broker-controlled interpreter output
read only after the worker has exited. `FAUXCASA_DECODE_SANDBOX=require`
(`--require-sandbox`) still refuses to start rather than degrade.

## Residual risks (owned, not hidden)

- The OS sandbox itself (kernel syscall surface, win32k on Windows) is
  the remaining attack surface; mitigated by the tightest practical
  seccomp/AppContainer profiles and by wasm adoption shrinking how
  often native parsers face raw input.
- The broker is trusted code handling untrusted *file names/paths* —
  small, audited surface; fuzzed at M2.
- Shared-memory protocol bugs (size confusion) — fixed-shape, length-
  prefixed buffers, validated on the trusted side; covered by the fuzz
  smoke.
- A compromised worker can lie about pixel content (display a wrong
  image) — accepted: confidentiality/integrity of the *archive* is
  preserved; rendering integrity of a hostile file is not a protected
  asset.
- **A bare, unconnected `socket()` call inside the AppContainer
  succeeds, ratified as inert (fauxcasa-i92.6).** WinSock's `socket()`
  is a local kernel-object allocation with no capability check, so it
  always succeeds; a zero-capability AppContainer holds no network
  capabilities and blocks loopback by default (lifting that needs an
  explicit `CheckNetIsolation LoopbackExempt`), so WFP denies every
  actual I/O attempt, including `connect`, `send`, `bind`, `listen`,
  `sendto`, and `getaddrinfo`, as the i92.3 probes confirm. This is
  invalidated if the worker's AppContainer is ever granted a loopback
  exemption or any network capability, at which point socket creation
  itself would need to be blocked.
