# Changelog

All notable user-facing changes to Fauxcasa are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Fauxcasa is pre-1.0 preview software; version numbers below refer to
GitHub release tags, not a semantic-versioning contract.

## [Unreleased]

Changes since the `tracer-test-v1` preview (2026-08-10, commit `553d31a`).
The `v0.1.0-rc2` release candidate (2026-09-13) contains everything in
this section; see `docs/releases/v0.1.0.md` for the full user-facing notes.

### Added (2026-09-15)

- Starred Photos is now date-grouped (month buckets, newest first, with
  the same pinned headers, jump buttons and per-group play as the main
  grid); undated photos land in a trailing "Undated" group
  (fauxcasa-q6l.20).
- A star-threshold filter (**View > Stars**: Any / 1-5 stars or more)
  composes with any view — folder, album, search, Starred — instead of
  needing a dedicated surface for "starred in this folder/search". The
  status line shows the active threshold (e.g. "Starred ≥3★") so a
  filtered-empty grid is never a silent no-op (fauxcasa-q6l.20).
- Bulk-unstar: Shift+Space (or **View > Clear Star(s)**) clears stars on
  the whole current selection in one gesture — pair it with Ctrl+A to
  clear an entire folder, search or Starred scope at once; in the viewer
  it clears just the shown photo (fauxcasa-q6l.20).
- Faces named inside a photo's own XMP metadata (mwg-rs face regions) are
  now read and merged with any Picasa `.picasa.ini` face regions by
  geometry: a face Picasa already knew about keeps its Picasa identity
  but takes the in-file name when the file has one; a face Picasa never
  saw is added and shows up under **People** like any other. Captions and
  keywords stored inside RAW and TIFF files (not just JPEG) are read too.
  A caption or keyword set too long for a single JPEG XMP block
  (ExtendedXMP) is reassembled and read as well (fauxcasa-cam.5).
- HEIC/HEIF photos (what recent iPhones save by default) now decode for
  thumbnails and the viewer, via the same Pillow fallback path already
  used for Photoshop PSD and 16-bit TIFF files (fauxcasa-y5b). Licensing
  and sandbox-posture notes: `docs/research/heic-decode-decision.md`.

### Added (2026-09-14)

- Screenshot gallery in the README and release notes, made from a demo
  library of freely licensed photos with realistic dates, places,
  captions, keywords, faces and albums (`scripts/make-demo-library.py`,
  `scripts/make-gallery.py`).
- Scripted-run options for screenshots and testing: `--view`, `--search`,
  `--select`, `--info`, `--faces`, `--play`, `--window-size`.
- The public metadata test corpora adopted in June (IPTC reference images,
  exif-samples) are now exercised by a dataset-gated test suite, with the
  small IPTC set fetched in CI.

### Added (0.1 release push, 2026-09-13)

- Still images decode inside a Windows AppContainer sandbox for thumbnails
  and the viewer, on by default (PRs 129, 131, 132, 135).
- App icon, taskbar identity, version identity and product names for the
  cache directory, log and executables (PRs 114, 116).
- Dark theme, toolbar and sidebar icons, selection/hover feedback, group
  headers with descriptions, empty states, menus with a generated
  shortcuts dialog and About box, remembered geometry, first-run welcome
  with Picasa's watched folders, viewer chevrons (PRs 121, 124, 134).
- Import-notes dialog; rescue of another library's Picasa database no
  longer runs for unrelated folders (PR 130).
- Release workflow with checksums, build-provenance attestation and a
  draft release; native Windows CI rendering check; third-party notices,
  SECURITY.md and a bug-report form (PRs 117, 120, 122, 123, 126).

### Fixed (0.1 release push, 2026-09-13)

- Seven post-merge review findings: Starred-view desync, stars keyed on
  the cache variant, malformed stars.json, stale Gallery action, stuck
  activity spinner, stale inspector, rich-text rendering of catalog
  strings (PR 115).
- Reconcile rebuild now lands on Windows while thumbnails are open; a
  library switch no longer wipes File Types; XMP rating overflow; slideshow
  prefetch after a catalog swap (PR 118).
- A raising decoder no longer wedges the viewer; scripted runs no longer
  stall when the build finishes after the viewer opens (PRs 127, 128).
- Minimum-zoom scrolling on high-DPI displays reads a smaller thumbnail
  level (PR 125).

### Changed (0.1 release push, 2026-09-13)

- README and the 0.1 release notes rewritten for readers who are not
  programmers: plain-language install, safety and removal answers up
  front, technical material (download verification, command-line options,
  provenance, gate status) moved to an appendix, and a fresh screenshot.


### Added

- Video playback with audio, via a sandboxed PyAV streaming seam (PR 99,
  fauxcasa-v46.3).
- Info panel: a photo metadata inspector showing EXIF/IPTC/XMP and catalog
  fields for the selected photo (PR 95, fauxcasa-q6l.25).
- Non-blocking first run: the window appears immediately and the initial
  library walk continues in the background instead of blocking startup
  (PR 108, fauxcasa-q6l.13).
- Flat/tree folder view toggle in the sidebar, with path-on-demand tooltips
  (PR 105, fauxcasa-q6l.10).
- Folder descriptions surfaced in sidebar tooltips across all four folder
  display shapes (PR 106, fauxcasa-cam.14).
- Catalog files are now written as a zstd-compressed store instead of
  grouped JSON, keeping the on-disk catalog within its size budget
  (PR 98, fauxcasa-ed5.5).
- Windows AppContainer decode pool: a sandboxed worker/broker transport for
  original-media decoding, with escape-resistance gates
  (`apps/desktop-python/test_decodesvc_win.py`), deadline enforcement, and
  a `TIMEOUT` taxonomy (PRs 110, 111, 113, fauxcasa-i92.3.x). This is
  CI-gated and validated but **not yet wired into the running app** — the
  desktop UI does not use the sandboxed pool for decoding yet.
- Application icon: a wordless "lens horizon" photo-mark icon, wired into
  the taskbar, window chrome, and the PyInstaller build, plus Windows
  AppUserModelID identity (PR 114, fauxcasa-ez2.2).
- `scripts/daily-report.py`: a one-shot repo status/health report
  (PR 112).

### Changed

- Windows 4K min-zoom: a scaled-paint cache and byte-budgeted "want band"
  reduce min-zoom cost at 4K; re-measured as an improvement, not a full
  close of the underlying performance bead (PRs 100, 109,
  fauxcasa-q6l.14 / q6l.26).
- Catalog gained ini/contacts freshness signatures and a new
  `CATALOG_VERSION` (14), so stale sidecar data is detected and refreshed
  (PR 107, fauxcasa-cam.14).

### Fixed

Seven post-merge findings from earlier Codex reviews, all user-visible in
the shipped preview (PR 115, fauxcasa-ez2.1; **pending merge** at the time
of writing):

- Unstarring a photo inside the Starred view now re-materializes the grid
  instead of leaving it stale.
- Star and sort state (`stars.json`, per-library `config.json`) now live in
  a variant-free per-library state directory, so changing File Types or
  scan-size settings no longer hides existing stars.
- A `stars.json` file with a non-list `stars` field no longer raises at
  startup.
- The gallery action stays visible after a reconcile swap leaves the
  viewer.
- The activity spinner now stops on terminal reconcile notices (offline
  root unchanged, adopt/multiroot drift) instead of spinning forever.
- The Info panel is re-derived after an in-place cold build or adopt-mode
  backfill finishes, instead of showing stale metadata.
- Inspector and status-bar labels render as plain text, and sidebar/
  import-note tooltips escape catalog strings explicitly, avoiding
  accidental rich-text interpretation of captions or paths.

## [tracer-test-v1] - 2026-08-10

Read-only friend preview for Windows 10/11 x64. See the [release
notes](https://github.com/maphew/fauxcasa/releases/tag/tracer-test-v1) for
full scope. Scans a photo library in place; writes only a rebuildable
catalog and thumbnail cache. Video files show poster images but did not
play yet. Original-media decoding was not sandboxed.

## [tracer-test-v0] - 2026-06-15

Earlier Windows tracer preview build. See the [release
notes](https://github.com/maphew/fauxcasa/releases/tag/tracer-test-v0) for
scope at that time.

[Unreleased]: https://github.com/maphew/fauxcasa/compare/tracer-test-v1...HEAD
[tracer-test-v1]: https://github.com/maphew/fauxcasa/releases/tag/tracer-test-v1
[tracer-test-v0]: https://github.com/maphew/fauxcasa/releases/tag/tracer-test-v0
