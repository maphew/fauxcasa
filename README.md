# Fauxcasa

Fauxcasa is an open source local photo manager in the spirit of Picasa.
The project exists because useful personal software should survive the
loss of vendor, maintainer, or business model.

It is under active development and not ready for regular use.

## Download

Fauxcasa is a read-only Picasa-library browser for Windows 10/11 x64 and
Linux x64.

1. Get the current build from the
   [latest release](https://github.com/maphew/fauxcasa/releases/latest).
2. Windows: use **Extract All**. The app is not a one-file executable; keep
   the extracted folder together, then open the folder and run
   `fauxcasa.exe`. Linux: extract the `tar.gz` and run the `fauxcasa`
   binary, or run from source (see Installation/Usage below).
3. Choose the top-level folder containing the photos you want to browse.

Fauxcasa scans photos in place but does **not** modify photos, Picasa
sidecars, or the Picasa database, and normal browsing never writes into your
library. It writes a rebuildable catalog and thumbnail cache under your user
cache directory: `%LOCALAPPDATA%\Fauxcasa\cache` on Windows, or
`$XDG_CACHE_HOME/fauxcasa` (`~/.cache/fauxcasa` if unset) elsewhere. That
cache directory also holds `config.json` (remembered library, preferences,
and File Types selections) and `fauxcasa.log` (a rotating diagnostic log —
include a redacted copy when filing a bug). The optional `--promote`,
`--add-root`, and `--import-picasa-watched` CLI commands, and the first-run
"Use Picasa's watched folders" button, do write a `library.json` and/or a
small `.fauxcasa-root` marker into the library itself — see "Files this app
writes" in the [release notes](docs/releases/v0.1.0.md) for the full list.

This is an unsigned build, so Windows SmartScreen may show an
unknown-publisher warning: right-click the zip, open Properties, and click
Unblock before Extract All, or click "More info" then "Run anyway" at
launch. Original-media decoding runs in-process and is not sandboxed yet on
any platform, so use only a library whose files you trust. See the
[release notes](https://github.com/maphew/fauxcasa/releases/latest) for the
complete scope and report problems through
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

- Source checkout: `cache/fauxcasa-cache/<library-digest>/`
- Frozen Windows build: `%LOCALAPPDATA%\\Fauxcasa\\cache/`
- Frozen other builds: `$XDG_CACHE_HOME/fauxcasa/`, or `~/.cache/fauxcasa/`
  when `XDG_CACHE_HOME` is not set

Pass `--cache-root <path>` to choose a different cache location. The per-library
cache currently contains files such as `catalog.json`, `thumbs.fcache`, and
`thumbs.fcache.json`.
The old preview cache `~/.cache/fauxcasa-tracer` is no longer read and can be
deleted.

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
