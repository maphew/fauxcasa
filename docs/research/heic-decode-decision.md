# HEIC/HEIF decode: library choice, bundling, and sandbox posture (fauxcasa-y5b)

**Status:** agent-run recon + implementation notes, 2026-09-15, written for
a non-programmer owner read before HEIC support ships in a release. This
memo answers the three questions the split bead (fauxcasa-y5b, itself split
from fauxcasa-ilu) asked to be settled before coding. It reports facts and
file locations; it does not make the owner's licensing call (see
"Licensing" below).

## Why this exists

Apple's iPhones save photos in HEIC/HEIF by default. Fauxcasa's v0.1
release notes originally said "not shown yet" for these files
(`docs/releases/v0.1.0.md`, before this bead). This memo is the record of
what it took to turn that into real decode support: thumbnails and the
viewer now show HEIC/HEIF photos, using the same code path (Pillow) that
already handles Photoshop PSD files and 16-bit TIFFs.

## 1. Licensing

**The decision made:** the app depends on **pi-heif** (PyPI package
`pi-heif`, version 1.4.0 pinned), not the more commonly recommended
**pillow-heif** package, even though both come from the same upstream
project (<https://github.com/bigcat88/pillow_heif>) and expose an
identical Python API.

**Why they differ:** pillow-heif's published wheels bundle BOTH a HEIF
decoder AND an HEIF/AVIF **encoder** — the encoder links libx265, which is
GPL-2.0-or-later licensed. That makes pillow-heif's own compiled wheels
GPLv2 as a whole, even though the app would only ever call the decode
side. pi-heif is the same project's **decode-only** build: no encoder, no
libx265, and its wheels declare `BSD-3-Clause` for the Python binding
code with `LGPLv3` for the bundled native libraries (`libheif` and
`libde265`) — see `pi_heif-1.4.0.dist-info/licenses/LICENSE.txt` and
`LICENSES_bundled.txt` inside the installed wheel, and the corresponding
entry added to `THIRD-PARTY-NOTICES.md`. LGPLv3 is compatible with
shipping inside an AGPLv3 application (Fauxcasa's own license,
`LICENSE`); GPLv2 is a materially harder combination to justify and was
the reason the owner deferred this work in the first place
(fauxcasa-ilu, 2026-09-14).

Verified on the dev box with `pi_heif.libheif_info()` (`uv run
--no-project --with pi-heif python -c "import pi_heif; print(pi_heif.
libheif_info())"` → `{'libheif': '1.23.0', ..., 'decoders': {'libde265':
'libde265 HEVC decoder, version 1.1.1'}}`): pi-heif 1.4.0 bundles
**libheif 1.23.0** and **libde265 1.1.1** — both newer than the
versions the wheel's own (slightly stale-looking) `LICENSES_bundled.txt`
template cites (v1.18.1 / v1.0.15). Both native components are treated
as `LGPL-3.0-only` per the wheel's declared binary-distribution license;
see `THIRD-PARTY-NOTICES.md`'s pi-heif entry for the "verify upstream"
caveat on the exact point-release license text.

**What this memo does NOT decide — owner call before v0.1.0 ships with
HEIC on:** HEIC/HEIF's video-derived codec, HEVC/H.265, is
**patent-encumbered**. Patent licensing is legally independent of the
copyright licenses discussed above (LGPLv3/BSD-3-Clause cover the *code*,
not any patent rights needed to practice HEVC decoding/encoding).
libde265 is an open-source HEVC **decoder** implementation; using it does
not itself grant or deny any patent license the owner might need for
distributing decode capability. Fauxcasa ships **unsigned binaries** —
this memo does not assess whether unsigned, patent-encumbered-codec
distribution carries meaningfully different risk than any other open-
source app bundling HEVC decode (many do, incl. FFmpeg-based apps this
project already bundles via PyAV — see `THIRD-PARTY-NOTICES.md`'s PyAV
entry, which notes GPL-licensed x264/x265 codec libraries already ship
in that dependency). The owner should treat "ship HEIC decode on by
default" as a decision to make consciously, not a default that fell out
of this bead, even though the copyright-license side is clean (LGPLv3
compatible with AGPLv3).

## 2. PyInstaller bundling

**What we found:** pi-heif's wheel on Windows is a `delvewheel`-repaired
build (see `pi_heif-1.4.0.dist-info/DELVEWHEEL`). Unlike some native
wheels, its compiled extension module (`_pi_heif.cp3xx-win_amd64.pyd`)
and the native libraries it loads (`libheif-*.dll`, `libde265-*.dll`,
plus MinGW runtime DLLs `libgcc_s_seh`, `libstdc++`, `libwinpthread`) are
installed as **top-level files in site-packages**, not inside the
`pi_heif/` package folder itself. `pi_heif/__init__.py` calls
`os.add_dll_directory()` on its own parent directory at import time (a
delvewheel-injected patch) so Windows can find those DLLs at runtime —
this only works when `_pi_heif`'s `.pyd` and the DLLs actually land in
the same relative location inside the frozen bundle.

pi-heif ships **no PyInstaller hook** of its own (no `__pyinstaller`
subpackage, no `pyinstaller40` entry point) — confirmed by inspecting the
installed package directory. The plan going in was: no hook means add
`--collect-all pi_heif` to the build command as a fallback.

**What we actually did, and why:** `--collect-all` turned out to be
unnecessary AND would not have applied anyway, since this project builds
via a PyInstaller **`.spec` file**
(`apps/desktop-python/fauxcasa-tracer.spec`), and PyInstaller ignores most
CLI collection flags once a spec file is given — the spec's own
`Analysis()` call is authoritative. Instead, `"pi_heif"` was added to the
spec's `_hidden` (hiddenimports) list, exactly like the existing `"PIL"`
entries, so PyInstaller's static analysis is guaranteed to trace
`pillowload.py`'s lazy `from pi_heif import register_heif_opener` import.

**Verified with a real local frozen build on this Windows box**
(`uv run --with "PySide6-Essentials==6.11.1" --with
"PySide6-Addons==6.11.1" --with "pyinstaller==6.20.0" --with
"rawpy==0.27.0" --with "exiv2==0.18.1" --with "av==18.0.0" --with
"pillow==12.3.0" --with "pi-heif==1.4.0" --with "zstandard==0.23.0"
pyinstaller --noconfirm --clean apps/desktop-python/fauxcasa-tracer.spec`,
pinned to Python 3.12 to match `.github/workflows/bundle.yml`):

- `dist/fauxcasa-console/_internal/_pi_heif.cp312-win_amd64.pyd`,
  `libheif-*.dll`, and `libde265-*.dll` all land directly under
  `_internal/` — PyInstaller's own binary-dependency walker discovered
  and copied them automatically once `pi_heif` was a traced import,
  without any `--collect-all` or manual `binaries=[...]` entry in the
  spec.
- `--bundle-self-check` (the frozen artifact's own lazy-import probe,
  `main.py:BUNDLE_RUNTIME_MODULES`, which now includes `"pi_heif"`)
  reported zero failures for `pi_heif`.
- Running the frozen `fauxcasa-console.exe` against a one-photo library
  containing the committed synthetic fixture
  (`fixtures/heic-smoke/synthetic.heic`) produced a real, non-error-tile
  thumbnail at the fixture's native 96x64 size (verified by reading the
  packed thumbnail cache's binary index directly) — the frozen bundle
  decodes HEIC end to end, not just imports the module.

`.github/workflows/bundle.yml` and `.github/workflows/release.yml` (kept
in lockstep by hand per their own header comments) now pin
`PIHEIF: "pi-heif==1.4.0"` alongside the existing `PILLOW` pin, pass
`--with "${{ env.PIHEIF }}"` to the same `pyinstaller ... fauxcasa-tracer.spec`
invocation used locally, and add a `synthetic.heic`-copy step plus an
"Assert HEIC thumbnail decoded in frozen artifact (pi-heif gated)" step
mirroring the pre-existing synthetic-DNG/rawpy gate, so a future build
that silently drops pi-heif from the build environment fails CI instead
of shipping a build that error-tiles every HEIC file.

## 3. Sandbox posture

`docs/decode-threat-model.md` requires all decoding of untrusted input to
run inside a sandboxed worker pool as the M1 floor. That document did not
previously call out that this floor already has exceptions: **PSD**
(fauxcasa-v46.4) and **16-bit TIFF** (fauxcasa-v46.7) both decode
**in-process**, via `apps/desktop-python/pillowload.py`, because the
pinned PySide6 build has no PSD plugin and Qt's own TIFF plugin corrupts
16-bit grayscale on Linux.

HEIC/HEIF joins that same pre-existing exception rather than creating a
new one: Qt has no HEIF plugin either, so `pillowload.pillow_qimage()`
routes `.heic`/`.heif` bytes through the identical Pillow fallback,
registering pi-heif's opener lazily. `docs/decode-threat-model.md` now
has an explicit **"In-process decoders (documented exception)"**
subsection naming all three formats, stating plainly that this is a real,
not theoretical, risk for HEIC specifically — libde265 has a public CVE
history for malformed-bitstream handling, and it now runs in-process
against attacker-controlled bytes with the same ambient authority as the
rest of the UI/index process — and tracking the fix (moving all three
fallbacks behind the sandboxed broker/worker boundary) under the
decode-isolation epic, **fauxcasa-i92**. `pillowload.py`'s bytes-in/
pixels-out interface was already documented as sandbox-service-shaped for
exactly this future move, so no interface change is needed when that
migration happens — only where the call runs.

## Files touched by this bead, for reference

- `apps/desktop-python/pillowload.py` — `_ensure_heif_opener()`, the
  lazy once-per-process pi-heif registration.
- `apps/desktop-python/thumbcache.py`, `apps/desktop-python/viewer.py` —
  HEIC/HEIF pre-routed to the Pillow fallback, alongside the existing PSD
  branch.
- `apps/desktop-python/catalog.py`, `scripts/make-thumbcache.py` — `.heic`/
  `.heif` added to the matched `EXTS` sets.
- `apps/desktop-python/fauxcasa-tracer.spec` — `"pi_heif"` hiddenimport.
- `.github/workflows/bundle.yml`, `.github/workflows/release.yml` —
  `PIHEIF` pin, build command `--with`, smoke-library fixture, and the
  frozen-artifact decode assertion.
- `fixtures/heic-smoke/synthetic.heic` — the committed synthetic test
  fixture (see `fixtures/heic-smoke/synthetic.heic.txt` for provenance).
- `docs/decode-threat-model.md`, `docs/releases/v0.1.0.md`,
  `THIRD-PARTY-NOTICES.md`, `scripts/make-demo-library.py` — documentation
  and demo-library-count updates.
