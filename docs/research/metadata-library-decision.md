# Metadata library: wrap exiv2, keep ExifTool as the referee (fauxcasa-cam.16)

**Status:** agent-run evaluation, 2026-07-02, per the spec's own rule for
candidate evaluations (plain language, real measurements, decide-and-move).
**The recommendation at the bottom is proposed, not locked** — the owner
ratifies this decision; it is overturnable by argument like every delegated
decision. Spike script: `docs/research/spikes/metadata-lib-spike.py`
(re-runnable on any machine with uv + ExifTool).

## The question, in one paragraph

The spec (§5 P1) already rules that the future metadata *writer* must be
"built on a mature metadata library (exiv2/exiftool-class — wrap, don't
hand-roll)" — that rule exists because Picasa itself corrupted Olympus/Kodak
MakerNote data when it wrote metadata by hand (§6 footgun 15). Today's
*reader* (`apps/desktop-python/inmeta.py`) is a small hand-rolled parser that
reads exactly two fields (caption, keywords) from JPEGs only. Every remaining
M1 ingest item — capture dates, GPS coordinates, star ratings, face regions,
oversized XMP packets, and TIFF/PNG/WebP files — would multiply that
hand-rolled surface in exactly the corner-case territory the rule exists to
avoid. So: which library do we wrap, and is "hand-roll just the easy three
(dates, GPS, rating) and wrap a library later for faces" a defensible middle
road? One constraint shapes everything: the threat model
(`docs/decode-threat-model.md`) puts the metadata parser **inside the decode
sandbox** — it must run in a worker process that is handed one file's bytes
and has no other powers.

## What we did, in plain language

We built a small corpus of test files whose metadata content is known
exactly, then asked each candidate library to read it back, and timed each
one over 100 files — all on the actual Windows development machine, each
candidate installed fresh into its own environment so the install experience
counts too. The corpus:

- **A real Picasa-written JPEG** — the committed oracle fixture from action
  013 (`fixtures/oracle/013-face-tag-manual/after/.../photo04.jpg`), whose
  XMP caption/keywords + IPTC mirror + EXIF date were written by actual
  Picasa 3.9 (synthetic image content, safe to commit — and note: Picasa
  wrote **no** face geometry into this JPEG; its XMP face-writing is the
  opt-in §6-footgun-13 behavior, so face fixtures must be synthesized).
- **A "full metadata" JPEG** — synthetic pixels, metadata written by
  ExifTool 13.52 (an independent reference implementation, not one of the
  in-process candidates): EXIF `DateTimeOriginal`, GPS rationals with
  hemisphere refs (Whitehorse — western longitude exercises the sign logic),
  XMP `Rating=3`, caption/keywords in XMP + IPTC, and an MWG `RegionInfo`
  with two named face regions (`Alice Example`, `Bob Example`).
- **An ExtendedXMP JPEG** — same, plus a 70,000-character XMP field, which
  forces the packet past the 64 KB JPEG segment limit so it must be split
  into "ExtendedXMP" continuation segments (the Adobe/Google convention;
  reading it back at full length proves a reader reassembles the split).
- **TIFF, PNG, and WebP carriers** — the same metadata in the three
  non-JPEG containers M1 must read (XMP in TIFF tag 0x02BC, PNG iTXt,
  WebP XMP chunk).

The candidates:

| Candidate | What it is | In one sentence |
|---|---|---|
| `pyexiv2` | PyPI `pyexiv2` (LeoHsiao1) | The exiv2 C++ library (the same engine digiKam and darktable rely on) inside the Python process, with a friendly dictionary-style API. |
| `exiv2` | PyPI `exiv2` (python-exiv2, Jim Easterbrook) | The other exiv2 binding: a low-level, complete mirror of the C++ API. |
| `exiftool` | PyPI `PyExifTool` + the ExifTool program | The gold-standard Perl metadata tool, driven as a helper program in its fast "stay-open" batch mode. |
| `pillow` | PyPI `Pillow` | The Python imaging library the app likely bundles anyway — how far does its own metadata surface go? |
| `xmptoolkit` | PyPI `python-xmp-toolkit` | Binding to Adobe's own XMP engine (via the Exempi C library). |
| `inmeta` | the current hand-rolled reader | The do-nothing baseline. |

## What each candidate actually returned

Every cell is real output from the spike run (2026-07-02, Windows 11,
Python 3.12; raw JSON archived by the script). "OK" means the value came
back exactly right, including GPS as signed decimals (60.72125, −135.05685)
and both face regions with names + geometry.

| Test | pyexiv2 2.15.5 (exiv2 0.28.7) | exiv2 0.18.1 (exiv2 0.28.8) | PyExifTool 0.5.6 (ExifTool 13.52) | Pillow 12.3.0 | python-xmp-toolkit | inmeta (today) |
|---|---|---|---|---|---|---|
| Picasa-written caption+keywords (oracle JPEG) | OK | OK | OK | OK | **failed** | OK |
| EXIF `DateTimeOriginal` | OK | OK | OK | OK | — | no |
| GPS → signed decimal degrees | OK | OK | OK | OK | — | no |
| XMP `Rating` | OK (string `"3"`) | OK (string `"3"`) | OK (number 3) | OK (string `"3"`) | — | no |
| mwg-rs face regions (2, nested RDF) | OK, names+geometry | OK, names+geometry | OK, names+geometry | OK, names+geometry | — | no |
| ExtendedXMP reassembly (70 KB field back at full length) | **main packet only** | **main packet only** | **OK (70,000)** | **main packet only** | — | no |
| TIFF / PNG / WebP carriers (all fields) | OK / OK / OK | OK / OK / OK | OK / OK / OK | OK / OK / OK | — | no (JPEG-only) |
| Corrupt/truncated/empty input | catchable error, no crash | not tested | (subprocess shrugs) | not tested | — | returns empty |

`python-xmp-toolkit` installed but every read failed with
`ExempiLoadError('Exempi library not found.')` — it needs the Exempi C
library, which has no sane Windows packaging. On a Windows-first project it
is dead on arrival; eliminated.

Two findings deserve plain-language emphasis:

- **Only ExifTool reassembles ExtendedXMP — and for exiv2 that gap is
  permanent policy, not a bug.** The exiv2 engine (both bindings) and
  Pillow silently read only the main 64 KB packet; the exiv2 project has
  stated it does not accept ExtendedXMP bug reports (its embedded Adobe XMP
  SDK "does not understand HasExtendedXMP" —
  https://dev.exiv2.org/boards/3/topics/3124), and Pillow's `getxmp()`
  design explicitly ignores the continuation segments
  (https://github.com/python-pillow/Pillow/issues/5076). The spike's
  candidates still returned rating and faces here because those happened to
  fit in the main packet — but a file whose face regions overflow into the
  extension (Google's own camera output uses ExtendedXMP) would silently
  lose them: exactly the N7-shaped silence fauxcasa-cam.5 exists to fix.
  However: we proved the gap is *only* the JPEG segment-stitching, not the
  XML parsing — when the same 70 KB packet was handed to exiv2 inside a
  TIFF (which has no 64 KB limit), it parsed everything perfectly,
  `label_len=70000, rating=3, faces=['Alice Example', 'Bob Example']`. So a
  thin shim — reassemble the split segments (the existing `inmeta.py`
  segment walker already walks JPEG segments), then hand the joined packet
  to the library — closes the gap while the library still does all the hard
  parsing.
- **Non-ASCII Windows filenames break both exiv2 bindings in
  open-by-filename mode.** A copy of the test file named
  `café-фото-写真.jpg` — exactly what a real family archive contains —
  failed to open by path in both (`errno 2` / `Invalid argument`). Both
  bindings work perfectly when handed the file's **bytes** instead
  (`pyexiv2.ImageData`, `exiv2.ImageFactory.open(bytes)`). This is no
  hardship — it is *exactly* the shape our architecture already requires:
  the indexer holds the bytes anyway (hashing + decode), and the sandbox
  design has the broker hand workers a file's bytes/handle rather than
  letting workers open paths. The "workaround" is the design.

## Speed (100-file timing corpus, same machine)

| Reader | Per file | 100,000-photo library, metadata pass |
|---|---|---|
| inmeta (2 fields, baseline) | 0.12 ms | ~12 s |
| exiv2 binding, full read | 0.31 ms | ~31 s |
| Pillow, full read | 0.35 ms | ~35 s |
| pyexiv2, full read (by path / by bytes) | 0.36 / 0.50 ms | ~36–50 s |
| ExifTool, stay-open batch | 3.8–3.9 ms | ~6.5 min |
| ExifTool, one process per file | 141.8 ms | ~4 hours |

Reading *everything* with a wrapped library costs roughly 3–4× the current
two-field hand-rolled read — call it 0.2 ms extra per photo — which is noise
next to thumbnail decoding. There is **no performance argument for
hand-rolling.** ExifTool in batch mode is 10× slower but still viable;
one-process-per-file is not. All candidates imported in 11–49 ms and
installed from wheels on this Windows box in seconds (pyexiv2 and
python-exiv2 both confirmed installing and importing on Python 3.13, and
pyexiv2 even on 3.14rc3 — no compiler involved).

## License, packaging, and maintenance (web-verified 2026-07)

The app is AGPL-3.0 (`LICENSE`).

- **exiv2** (the C++ engine) is **GPL-2.0-or-later** — confirmed in its
  README ("either version 2 of the License, or (at your option) any later
  version"). The "or later" lets it be used under GPLv3 terms, and GPLv3
  and AGPL-3.0 are explicitly compatible with each other (each license's
  §13 permits the combination). Compatible. A GPLv2-*only* component would
  have been a problem; none is present. Current upstream release: 0.28.8
  (2026-03-01). It is continuously fuzzed on OSS-Fuzz and carries a long
  CVE record (~125 NVD entries since 2005, overwhelmingly parser
  out-of-bounds reads/DoS, advisories through 0.28.8) — see the sandbox
  section for why that record is an argument *for* our architecture, not
  against the library.
- **pyexiv2** binding: **GPL-3.0** (AGPL-compatible). v2.15.5 (2025-10-14),
  bundles exiv2 **0.28.7** — i.e. it currently **lags one upstream security
  release**. Healthy repo (0 open issues, active through late 2025), single
  maintainer. Wheels cp38–cp314 for win/linux/mac x86-64 + arm64 (no
  Windows-arm64). Its README documents it is **not thread-safe** (global
  C++ state) — irrelevant to us by construction, since the sandbox design
  uses a pool of *processes*, one job at a time each.
- **python-exiv2** (PyPI `exiv2`): **GPL-3.0-or-later** (AGPL-compatible).
  v0.18.1 (2026-03-02) ships exiv2 **0.28.8 — rebuilt within a day of the
  upstream security release**, and its earlier releases show the same
  tracking cadence. Broadest wheel matrix of any candidate: cp310–cp314
  including the free-threaded cp314t, win/linux/mac, x86-64 + arm64.
  Single (veteran) maintainer.
- **ExifTool** is dual-licensed Perl Artistic / GPL. Spawning it as a
  subprocess next to an AGPL app is mere aggregation — clean both ways.
  But *shipping* it on Windows means bundling the ~11 MB packaged-Perl
  distribution per release and tracking its updates ourselves: **no PyPI
  package vendors the exiftool binary** (the Node-style `exiftool-vendored`
  has no Python equivalent), and the PyExifTool wrapper itself is dormant
  (last release 2023) though functionally stable. Current upstream is
  13.59; this machine's 13.52 worked flawlessly.
- **Pillow** is MIT-CMU — no constraint.
- Wheel install experience measured here: `uv run --with pyexiv2` resolved,
  downloaded, and ran in 2.5 s; `--with exiv2` in 6.3 s; both confirmed
  importing on Python 3.13 (and pyexiv2 on 3.14rc3) with zero system
  setup, no compiler. That is the whole install story.

## Sandbox fit (the i92 constraint)

The threat model is blunt: "the metadata parser (exiv2-class) sits inside
the same boundary as pixel decoders," because EXIF/XMP/MakerNote parsers
have a long exploit history — exiv2's own substantial CVE/fuzzing record is
not a strike against it, it is the *reason the sandbox rule exists for
whatever parser we use, including any hand-rolled one*.

- **exiv2 bindings (in-process C extension):** compose trivially. The
  sandboxed worker imports the module and calls it on the bytes the broker
  handed over — bytes-mode is first-class (proven above), so the worker
  never needs to open a path. A crash from hostile input costs one worker,
  by design. Fail-soft confirmed hands-on: garbage, truncated, and empty
  inputs raise ordinary catchable errors.
- **ExifTool (subprocess model):** composes awkwardly. Our worker *is* a
  locked-down subprocess; ExifTool would be a long-lived Perl *grandchild*
  process inside it. The sandbox profile would have to permit spawning
  executables (a thing sandboxes exist to forbid), and the stay-open batch
  protocol passes *filenames*, which collides head-on with "workers cannot
  open anything themselves." Feeding bytes via stdin forfeits batch mode
  (back to ~140 ms/file). Fine as a dev-side tool; wrong shape to ship.
- **Pillow:** composes fine (in-process), but see below.

## The candidates we eliminate, and why

- **python-xmp-toolkit** — does not run on Windows (needs the Exempi C
  library; its own docs say it "has not been tested on Windows" and no
  Windows packaging exists — our spike confirmed the dead end first-hand
  despite a surprise late-2025 release). XMP-only anyway (no EXIF/IPTC).
  Out.
- **py3exiv2** (the older Boost.Python exiv2 binding) — source-only, zero
  wheels, no Windows support, dormant since 2023. Out.
- **PyExifTool as the shipped reader** — the best *reader* on pure results
  (only one to reassemble ExtendedXMP natively), and ExifTool's tag coverage
  is the industry reference. But: 10× slower in its best mode, an ~11 MB
  packed-Perl distribution to bundle and update on three platforms, and the
  subprocess model composes badly with the sandbox worker design (above).
  **Keep it as the referee, not the player**: it already writes our
  fixtures, and CI can diff our wrapped-library reads against ExifTool's
  as a cross-check corpus.
- **Pillow alone** — genuinely surprising in the spike: its `getexif()` +
  `getxmp()` read dates, GPS, rating, *and* the nested face regions, in all
  four containers. But `getxmp()` returns an untyped dict-of-dicts we'd
  have to interpret ourselves (our spike code to fish faces out of it was
  the hairiest of the four), and it **strips XML namespaces from the keys**
  (verified in Pillow's source), so `mwg-rs:Name` and any other
  namespace's `Name` collide — raw XML shape, not XMP semantics. No
  ExtendedXMP reassembly (explicitly ignored by design), no real IPTC
  surface — and **no metadata-writing path at all**, so the §5 P1 writer
  (the actual reason the "wrap a mature library" rule exists) would still
  need exiv2-class machinery later. It's a fallback, not a foundation.
- **exifmwg** (a young MPL-2.0 library with its own exiv2 bindings,
  purpose-built for exactly our MWG-regions use case, active through
  end-2025) — too small and new to bet the ingest on today, but worth
  watching; its existence is further evidence that mwg-rs-on-exiv2 is the
  well-trodden path.
- **Extending inmeta.py for "just dates + GPS + rating"** — evaluated
  honestly: rating alone would be cheap (one more key in the XMP it already
  parses), but dates and GPS mean hand-rolling a full EXIF/TIFF IFD walker —
  endianness, offsets, rational types — which is *precisely* the
  offset-sensitive territory where Picasa's own MakerNote corruption
  happened and precisely what §5 P1 forbids hand-rolling for the writer.
  We'd write and fuzz-test a parser we're contractually going to replace.
  The library reads the same fields at effectively the same speed. The only
  part of inmeta.py with lasting value is its defensive JPEG segment
  walker, which is exactly the piece the ExtendedXMP shim needs — it gets
  *promoted*, not extended.

## RECOMMENDATION (proposed — owner ratifies)

**Wrap exiv2. Primary binding: `python-exiv2` (PyPI name `exiv2`), used in
bytes-mode, running inside the sandbox worker. Do not extend the
hand-rolled reader.** Concretely:

1. **One product reader module wrapping python-exiv2** behind a small typed
   interface (dates, GPS decimals, rating int, caption, keywords, face
   regions list) so call sites never see library keys. Bytes in, plain data
   out — the same fail-soft contract inmeta.py has today (per-file errors
   yield empty fields, never abort the index).
2. **A ~20-line ExtendedXMP shim** in front of it: reuse the inmeta segment
   walker to collect and join split XMP segments (match by the
   `HasExtendedXMP` GUID), then hand the joined packet to the library
   (proven viable — exiv2 parsed the 70 KB packet flawlessly when the
   container allowed it). This closes the one real read gap exiv2 has
   against ExifTool, a gap exiv2 upstream has declared permanent.
3. **Why python-exiv2 over pyexiv2:** identical results and speed in the
   spike (same engine, same flattened key strings), so the tie-breakers are
   currency and reach — python-exiv2 ships the latest exiv2 security
   release within a day of upstream (0.28.8 vs pyexiv2's 0.28.7) and has
   the broadest wheel matrix (cp310–cp314 incl. free-threaded, all three
   OSes, both architectures). For a component whose entire job is parsing
   untrusted input, how fast security fixes reach us outweighs pyexiv2's
   admittedly friendlier dictionary API — and the API difference is
   confined to the one wrapper module (our spike implemented both shims;
   the difference was a few lines). **pyexiv2 is the named fallback
   binding**: same engine, same keys, a contained swap if python-exiv2's
   maintenance ever stalls. This sub-choice is deliberately low-stakes and
   reversible.
4. **ExifTool stays as the independent referee**: fixture writer (it wrote
   this spike's corpus) and CI differential checks of our reader against
   its output. Never shipped inside the app.
5. **inmeta.py is untouched for the tracer** (zero-dependency, two fields,
   fine at its job); the product reader supersedes it at ingest, and the
   segment walker lives on in the shim.
6. **The sandbox rule is unchanged and non-negotiable** (i92.4): the wrapped
   library parses untrusted bytes, so it runs in the worker pool alongside
   pixel decoding — its CVE history is the argument *for* this architecture,
   under which library exploits are contained by construction.
7. **One honest forward flag for the M2+ writer:** exiv2 *reads* mwg-rs
   regions cleanly (supported since exiv2 0.22), but community reports say
   rewriting/deleting nested region structs is its fragile corner. That is
   a writer-milestone problem, squarely covered by §5 P1's round-trip
   verification + the ExifTool referee; it does not affect this read-path
   decision, but the M2 writer bead should re-examine it.

## What this means for the dependent beads

- **fauxcasa-cam.5 (faces-in-XMP + ExtendedXMP)** — unblocked with the hard
  part bought, not built: mwg-rs nested-RDF parsing came back correct
  out-of-the-box (`Xmp.mwg-rs.Regions/mwg-rs:RegionList[n]/...` keys);
  ExtendedXMP is the shim in item 2. Face fixtures must be
  ExifTool-synthesized (real Picasa doesn't write region XMP by default —
  confirmed against oracle fixture 013).
- **fauxcasa-cam.9 (dates)** — read `Exif.Photo.DateTimeOriginal` (fallback
  `Exif.Image.DateTime`, then mtime) from the wrapper. Values arrive as
  strings; we parse them ourselves, which keeps footgun 16 dead (no year
  floor — pre-1903 scan dates stay legal).
- **fauxcasa-cam.10 (GPS)** — rationals + hemisphere refs → signed decimals,
  verified against known coordinates including the western-hemisphere sign.
- **fauxcasa-cam.11 (Rating)** — `Xmp.xmp.Rating` arrives as a string
  (`"3"`); the wrapper converts to int and clamps to the −1/0–5 model.
- **fauxcasa-i92.4 (metadata in the sandbox)** — bytes-mode reading is the
  native shape for broker-hands-bytes workers; the same worker that decodes
  pixels parses metadata with the bytes already in hand. No new IPC needed.
- Language note: this decision does not prejudge the app-stack decision —
  exiv2 is a C++ library with bindings in every candidate stack (Python's
  two here; Rust/Go/JS equivalents exist), and the interface in item 1 is
  the stack-portable seam.

## Re-running the spike

```
uv run docs/research/spikes/metadata-lib-spike.py run
```

Needs uv and ExifTool on PATH. It builds the corpus in a temp directory
(fixtures are synthesized fresh; the only repo input is the committed
synthetic oracle JPEG), runs every candidate in its own isolated
environment, prints per-candidate JSON, and writes raw results next to the
corpus. `--only pyexiv2,inmeta` runs a subset.
