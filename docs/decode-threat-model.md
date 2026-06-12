# Decode isolation: threat model and mechanism choice

**Status:** M0-exit deliverable (fauxcasa-bdj). Spec §5 Formats sets the
requirement and the timing: the isolation mechanism is "chosen with the
stack (fauxcasa-6hf) against a written threat model, at M0 exit"; §10
item 12 separately names wasm's decode-sandbox role. Written 2026-06-12.
**Decision state: proposed** — agent draft for owner review alongside
the stack-decision report (`docs/research/stack-balloons.md`),
overturnable by argument like every delegated decision.

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
- **The app's own cache artifacts are trusted by provenance**: thumbnails
  are *re-encoded by our encoder from decoded pixels inside the
  sandbox*, so the cache files the UI reads (the grid's hot path, N4)
  are our own output, not attacker bytes. The UI process never decodes
  an original. (This is exactly the architecture the stack trial
  balloons benchmarked: the grid reads only the packed thumb cache.)

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

## Decision (proposed — owner confirm-or-argue rides fauxcasa-6hf)

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
   spec names. Video stays ffmpeg-in-A for the foreseeable future.
4. Memory-safe decoder implementations are welcome *inside* the sandbox
   (defense in depth), never as a substitute for it.

The mechanism is deliberately **stack-independent**: every candidate
host (Python, Rust, Go, web-shell) can spawn and supervise a sandboxed
worker pool; none of them changes the cost materially. This means the
threat model does not constrain the §10 item 12 stack choice — and the
stack choice cannot weaken the isolation requirement.

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
