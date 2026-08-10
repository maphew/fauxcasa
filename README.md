# Fauxcasa

Fauxcasa is an open source local photo manager in the spirit of Picasa.
The project exists because useful personal software should survive the
loss of vendor, maintainer, or business model.

It is under active development and not ready for regular use.

## Windows preview

A read-only preview for Windows 10/11 x64 is available for trusted testers:

1. Download
   [`fauxcasa-tracer-tracer-test-v1-windows-x64.zip`](https://github.com/maphew/fauxcasa/releases/download/tracer-test-v1/fauxcasa-tracer-tracer-test-v1-windows-x64.zip).
2. Use **Extract All**. The app is not a one-file executable; keep the
   extracted folder together.
3. Open `fauxcasa-tracer-gui` and run `fauxcasa-tracer-gui.exe`.
4. Choose the top-level folder containing the photos you want to browse.

The preview scans photos in place but does **not** modify photos, Picasa
sidecars, or the Picasa database. It writes only a rebuildable catalog and
thumbnail cache under your user cache directory. A checksum is published next
to the download.

This is an unsigned alpha, so Windows SmartScreen may show an unknown-publisher
warning. The initial folder scan happens before the window appears and can take
time for a large or network-hosted library. Video files have poster images but
do not play yet; editing, tagging, exporting, and other write operations are
not implemented. Original-media decoding is not sandboxed yet, so use only a
library whose files you trust. See the
[preview release notes](https://github.com/maphew/fauxcasa/releases/tag/tracer-test-v1)
for the complete scope and report problems through
[GitHub Issues](https://github.com/maphew/fauxcasa/issues).

## Installation

Install uv from [https://docs.astral.sh/uv/](https://docs.astral.sh/uv/).
Install just from [https://just.systems/](https://just.systems/) for the
root command runners.

## Usage

```
just py ~/Pictures
```

The current desktop prototype lives under `apps/desktop-python/`. Common
commands are exposed from the repo root:

```
just py ~/Pictures
just py-test
just py-smoke
```

Repository layout:

```
apps/desktop-python/ = experimental desktop app
scripts/             = developer/research/build utilities used around the app
```

## Storage And Picasa Data

The current `tracer` prototype does not write a photo database into your
library. It scans the library in place, reads compatible metadata, and writes
only its own rebuildable catalog and thumbnail cache.

By default, that cache is stored outside the photo library:

- Source checkout: `cache/tracer-cache/<library-digest>/`
- Frozen app build: `$XDG_CACHE_HOME/fauxcasa-tracer/`, or
  `~/.cache/fauxcasa-tracer/` when `XDG_CACHE_HOME` is not set

Pass `--cache-root <path>` to choose a different cache location. The per-library
cache currently contains files such as `catalog.json`, `thumbs.fcache`, and
`thumbs.fcache.json`.

Existing Picasa sidecar files are read today. The prototype reads
`.picasa.ini`, `Picasa.ini`, and `picasa.ini` files for stars, captions,
keywords, rotation, hidden flags, albums, and folder descriptions. It does not
write those files yet.

The product plan is for durable user state to live in or beside the photo
library, not in a private database that becomes a lock-in point. Fauxcasa is
intended to import existing Picasa `.picasa.ini` files, `.pal` album files,
`contacts.xml`, db3 `.pmp` data, `.picasaoriginals`, and in-file
XMP/IPTC/EXIF. Version 1 is planned to write durable state as
Picasa-compatible sidecars where possible, plus standard in-file metadata under
the metadata write policy; writing Picasa's db3 database is not a v1 goal.

## Licensing

Fauxcasa uses strong copyleft by default so the work and its community remain a
commons.

- Application code, scripts, tests, and build files are licensed under the GNU
  Affero General Public License, version 3 or later: `AGPL-3.0-or-later`.
- Original project documentation written for Fauxcasa is licensed under Creative
  Commons Attribution-ShareAlike 4.0 International: `CC-BY-SA-4.0`.
- Archived research material is source-attributed reference material. It is not
  relicensed by this repository unless a specific file says otherwise.
- The Fauxcasa name, logos, icons, and other project branding are reserved as
  trademarks or service marks. The copyright licenses do not grant trademark
  rights or permission to imply project endorsement.

The AGPL matters for this project because network features such as cross-machine
sync may become part of the product. Modified versions that users interact with
over a network must offer the corresponding source code as required by the
license.

See `LICENSE` for the full AGPL-3.0 text, `docs/LICENSE.md` for documentation
terms, `docs/research/NOTICE.md` for archived research notes, and
`CONTRIBUTING.md` for contribution terms.
