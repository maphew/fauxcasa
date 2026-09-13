# Third-Party Notices

Fauxcasa itself is licensed under the GNU Affero General Public License,
version 3 or later (`AGPL-3.0-or-later`). See `LICENSE` for the full text.

**Complete corresponding source for Fauxcasa is available at
<https://github.com/maphew/fauxcasa> (tag matching the release version).**

This file accompanies the Windows binary distribution built by
`.github/workflows/bundle.yml` and lists the third-party components bundled
into that distribution, along with the license each is distributed under and
where its license text lives inside the bundle or upstream. Versions below
are the exact pins used by the bundle workflow at the time this notice was
written; verify against the workflow file for the version you are shipping.

License metadata below was read directly from each package's installed wheel
(`importlib.metadata`) on 2026-09-13. Where metadata was ambiguous or a
license text file could not be located in the wheel, that is called out
explicitly as "verify upstream" rather than guessed.

## Components

### PySide6-Essentials 6.11.1

- Upstream: <https://pyside.org> / <https://code.qt.io/cgit/pyside/pyside-setup.git>
- License (SPDX, as declared in wheel metadata): `LGPL-3.0-only OR
  GPL-2.0-only OR GPL-3.0-only`
- License text in wheel: only a commercial-license reference file is bundled
  (`pyside6_essentials-*.dist-info/licenses/LicenseRef-Qt-Commercial.txt`).
  The wheel metadata does not embed the LGPL/GPL text itself; see the
  upstream Qt/PySide licensing pages (<https://www.qt.io/licensing/>,
  <https://doc.qt.io/qtforpython-6/licenses.html>) for the applicable terms.
  **Verify upstream** which specific Qt modules in this wheel are
  LGPL-licensed vs GPL-licensed before relying on this notice for
  compliance purposes.
- Why bundled: Qt runtime libraries (core, GUI, widgets, etc.) used by the
  PySide6 desktop UI.

### PySide6-Addons 6.11.1

- Upstream: <https://pyside.org>
- License (SPDX, as declared in wheel metadata): `LGPL-3.0-only OR
  GPL-2.0-only OR GPL-3.0-only`
- License text in wheel: same as PySide6-Essentials above (commercial
  license reference file only; **verify upstream** for per-module terms).
- Why bundled: additional Qt modules (e.g. multimedia) used by the desktop
  UI.

### PyInstaller 6.20.0

- Upstream: <https://pyinstaller.org>
- License (as declared in wheel metadata): "GPLv2-or-later with a special
  exception which allows to use PyInstaller to build and distribute
  non-free programs (including commercial ones)." Classifier: `License ::
  OSI Approved :: GNU General Public License v2 (GPLv2)`.
- License text in wheel: `pyinstaller-6.20.0.dist-info/licenses/COPYING.txt`
- Why bundled: not itself redistributed inside the built app; used only as
  the build tool that packages the frozen application. Listed here for
  transparency about the build chain, not because its own code ships in
  the output.

### rawpy 0.27.0

- Upstream: <https://github.com/letmaik/rawpy>
- License (SPDX, `License-Expression` in wheel metadata): `MIT`
- License text in wheel: `rawpy-0.27.0.dist-info/licenses/LICENSE`
- Why bundled: RAW photo decoding (Canon CR2/CR3, Nikon NEF, etc.).
- **Bundled LibRaw library**: rawpy statically/dynamically links LibRaw.
  The wheel additionally ships
  `rawpy-0.27.0.dist-info/licenses/LICENSE.LibRaw`, which contains the GNU
  Lesser General Public License, version 2.1 (`LGPL-2.1-only`) text. No
  CDDL license text is present in this wheel's `licenses/` directory
  (LibRaw is dual-licensed LGPL-2.1/CDDL upstream; this build's notices
  only include the LGPL-2.1 text, so treat the LibRaw component here as
  `LGPL-2.1-only` unless upstream confirms otherwise — **verify upstream**).

### exiv2 (python-exiv2) 0.18.1

- Upstream: <https://github.com/jim-easterbrook/python-exiv2>
- License (as declared in wheel metadata `License` field): "GNU GPL".
  Classifier: `License :: OSI Approved :: GNU General Public License v3 or
  later (GPLv3+)`.
- License text in wheel: `exiv2-0.18.1.dist-info/licenses/LICENSE`
- Why bundled: reading/writing EXIF, IPTC, and XMP metadata in photo files.
- Note: this Python binding package wraps the C++ Exiv2 library, which is
  itself GPL-2.0-or-later upstream (<https://exiv2.org>); the wheel's own
  classifier states GPLv3+. **Verify upstream** which GPL version(s) apply
  to the bundled native Exiv2 library specifically, since Exiv2 relicensed
  from GPL-2.0 to GPL-2.0-or-later historically and python-exiv2 may pin a
  specific Exiv2 release.

### Pillow 12.3.0

- Upstream: <https://python-pillow.org> /
  <https://github.com/python-pillow/Pillow>
- License (SPDX, `License-Expression` in wheel metadata): `MIT-CMU`
- License text in wheel: `pillow-12.3.0.dist-info/licenses/LICENSE`
- Why bundled: general image loading, resizing, and thumbnail generation.

### PyAV (av) 18.0.0

- Upstream: <https://github.com/PyAV-Org/PyAV>
- License (SPDX, `License-Expression` in wheel metadata): `BSD-3-Clause`
  (the PyAV binding code itself).
- License text in wheel: `av-18.0.0.dist-info/licenses/LICENSE.txt`
  (BSD-3-Clause-style text; also `AUTHORS.py` / `AUTHORS.rst`).
- Why bundled: video decoding and playback with audio.
- **Bundled FFmpeg build**: this wheel is a `delvewheel`-repaired build
  (`av-18.0.0.dist-info/DELVEWHEEL`) that vendors FFmpeg shared libraries
  directly in an `av.libs/` directory alongside the package, including
  `avcodec`, `avformat`, `avutil`, `avdevice`, `avfilter`, `swscale`, and
  `swresample` DLLs. `av.library_versions` reports FFmpeg component
  versions such as `libavcodec (62, 28, 102)`.
  No standalone FFmpeg `LICENSE`/`COPYING`/`NOTICE` file is present
  alongside those DLLs in the wheel, so the build's exact license (LGPL vs
  GPL configuration) is **not self-declared in the wheel** and must be
  inferred from which codecs are linked in.
  `av.libs/` also ships `libx264-165-*.dll`, `libx265-*.dll`, and
  `libSvtAv1Enc-*.dll` — **libx264 and libx265 are GPL-2.0-or-later
  licensed codec libraries**. Their presence means this FFmpeg build is
  linked against GPL components and should be treated as a
  **GPL-licensed FFmpeg build**, not the permissive LGPL configuration,
  until upstream PyAV wheel metadata states otherwise. **Verify upstream**
  (PyAV's wheel build scripts / cibuildwheel config) for the authoritative
  FFmpeg license classification and obtain the corresponding FFmpeg/x264/
  x265/libvpx/SVT-AV1/dav1d source or build scripts if redistributing this
  wheel's binaries.
  Other libraries observed in `av.libs/`: `libdav1d` (BSD-2-Clause),
  `libvpx` (BSD-3-Clause), `libwebp`/`libsharpyuv`/`libwebpmux` (BSD-style),
  `libopus` (BSD-3-Clause), `libmp3lame` (LGPL-2.0-or-later), `libopencore-
  amrnb`/`libopencore-amrwb` (Apache-2.0), `libvpl` (MIT), `libstdc++` and
  `libgcc_s_seh` (GPL-3.0-or-later WITH GCC-exception, from the MinGW
  toolchain), `libwinpthread` (MIT-style / BSD-style MinGW runtime),
  `libiconv` (LGPL-2.1-or-later), `zlib1` (Zlib). **Verify upstream** the
  exact license terms and required source offers for each before shipping
  under a license other than a copyleft one, since the presence of GPL
  codec libraries in this dependency chain is itself a reason strong
  copyleft (AGPL) is appropriate for the app that bundles it.

### zstandard 0.23.0

- Upstream: <https://github.com/indygreg/python-zstandard>
- License (as declared in wheel metadata `License` field and classifier):
  `BSD` / `License :: OSI Approved :: BSD License`.
- License text in wheel: `zstandard-0.23.0.dist-info/LICENSE`
- Why bundled: zstd compression for the local catalog file format.

### NumPy (transitive dependency)

- Upstream: <https://numpy.org>
- License (SPDX, `License-Expression` in wheel metadata, version observed
  during this verification pass, 2.5.3 — **the exact version pulled into
  the bundle depends on dependency resolution at bundle-build time and
  should be re-verified against the bundle's actual lockfile/build log**):
  `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`
- License text in wheel: `numpy-<version>.dist-info/licenses/LICENSE.txt`
  plus multiple per-vendored-component license files under
  `numpy/_core/...`, `numpy/random/...`, `numpy/ma/...` (bundled
  third-party numerics code such as libdivide, pocketfft, x86-simd-sort,
  highway, lapack_lite).
- Why bundled: array/matrix operations used by rawpy and image processing.
- Pulled in transitively by rawpy and/or av; not directly pinned by
  `bundle.yml`. **Verify upstream** the resolved version and full license
  set against the actual bundle build log for each release.

### CPython (embedded interpreter, via PyInstaller)

- Upstream: <https://www.python.org>
- License: Python Software Foundation License, version 2 (PSF-2.0), a
  permissive license.
- License text: <https://docs.python.org/3/license.html>
- Why bundled: PyInstaller embeds a Python interpreter and standard
  library into the frozen application.

## AGPL corresponding source

The packaged Fauxcasa application is licensed under the GNU Affero General
Public License, version 3 or later (`AGPL-3.0-or-later`). These notices
accompany the binary distribution to satisfy third-party attribution and
license-text obligations for the components listed above.

Complete corresponding source for Fauxcasa is available at
<https://github.com/maphew/fauxcasa> (tag matching the release version).

## Maintenance note

This file should be regenerated or re-verified whenever
`.github/workflows/bundle.yml` changes its pinned dependency versions,
using the same `importlib.metadata`-based verification method described
above rather than carrying forward unverified claims.
