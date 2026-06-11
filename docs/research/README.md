# Research material

Reference material for reverse-engineering Picasa, gathered from the links in
`begin.md`. Committed here so it survives link rot and is available to every
session/machine without re-fetching.

## transcripts/

Auto-generated English captions for the Picasa tutorial videos, as raw
`<youtube-id>.en.vtt` (timestamps preserved) plus deduplicated `<youtube-id>.txt`
for easy reading.

| YouTube id    | Title                                                            |
| ------------- | ---------------------------------------------------------------- |
| `drf6OTywF6E` | Picasa 3.5 Instructional Video - Part 1: Organizing              |
| `PuNjY6Zq6Cw` | Picasa 3.5 - Part 2: Basic Fixes and More                        |
| `4L7IV_3xKKM` | Importing Photos in Picasa 3.5                                   |
| `sOOkONjzcss` | Backup photos with Picasa                                        |
| `c1_Tbs5e6Ag` | Google Picasa 3 - Video 2 - Running Picasa 3 for the First Time (no captions available) |
| `ArB400dG0sU` | Simple Photo Slideshow with Picasa                               |
| `De1U5mAqCTM` | Google Picasa (overview)                                         |
| `YfqThYqDs-8` | Picasa 3.5: Managing Thousands of Pictures on External Drives    |

The videos themselves and extracted stills are NOT committed (size/copyright);
they live in the gitignored `cache/videos/<id>/` and can be rebuilt on any
machine with `scripts/fetch-videos.py`.

## sources/

Archived text copies of the file-format / reverse-engineering articles, each
with source attribution in its header:

- `sbktech-2011-picasa-pmp-format.md` — the key writeup of the `.pmp`
  column-per-file database format (Picasa 3.9), including header layout and
  field types.
- `stackoverflow-1467004-access-picasa-database.md` — same author's summary:
  `.pmp` files are the database (one file per field, prefix = table); `.db`
  files are Windows `thumbs.db`-format thumbnail containers.
- `superuser-151146-picasa-file-format.md` — community knowledge on Picasa's
  data locations and formats.
- `forensicir-2007-picasa-analysis.md` — forensic analysis of Picasa's on-disk
  artifacts (2007, older Picasa 2-era perspective).
- `picasaresources/` — 24 key pages archived from the community-maintained
  Picasa Resources site (how-picasa-works, database internals, network
  sharing, name tags, custom buttons API, release notes, ...).

## prior-art-parsers.md

Survey of existing open-source Picasa format parsers (Chromium's unit-tested
.pmp reader, picasa3meta, PicasaDBReader, picasa2digikam, the .picasa.ini
spec gist, ...) with licenses and recommended primary references.

## wine-oracle.md

Real Picasa 3.9 running under Wine against the synthetic library: setup,
launch commands, validation results (round-trip .pmp parse confirmed), and
the differential-testing recipe.

## picasastarter-notes.md

Mechanisms and lessons from PicasaStarter (multi-database/multi-machine
wrapper): the AppLocalDataPath registry relocation, virtual-drive path
workarounds, advisory locking, first-run scan suppression, and the pain
points a rebuild must design away.

## picasa-video-notes.md

Synthesized observations (features, UI anatomy, behaviors, workflows) extracted
from the tutorial videos via transcripts + frame analysis.

## picasa-binary-notes.md

Static-analysis (strings) findings from the final Picasa 3.9 Windows binary:
database schema vocabulary, `.picasa.ini` keys, filter op names, registry
locations, locking protocol. Installer + extracted tree + full dumps cached in
`cache/installers/` (gitignored; provenance and SHA-256 in the notes).
