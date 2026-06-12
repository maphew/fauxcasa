# The Wine Picasa oracle

Real Picasa 3.9 running under Wine on the dev machine, indexing the synthetic
photo library — our ground-truth generator for differential format testing.
Stood up and validated 2026-06-11.

## Why

Instead of trusting 2011-era format writeups, we make the real binary produce
fresh artifacts on demand: change something in Picasa, diff the resulting
`.pmp`/`.picasa.ini` bytes; later, feed Fauxcasa-written files back to real
Picasa and confirm it accepts them. All data is synthetic
(`scripts/make-synthetic-library.py`), so none of this touches the privacy
rules.

## Setup (reproducible)

1. Wine flatpak, user-level:
   `flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo`
   `flatpak install --user -y flathub app/org.winehq.Wine/x86_64/stable-25.08`
2. Synthetic library: `scripts/make-synthetic-library.py` →
   `cache/synthetic-library/` (3 folders × 8 EXIF-dated JPEGs).
3. Install Picasa silently into a dedicated prefix (installer cached, see
   `picasa-binary-notes.md` for provenance):

   ```sh
   flatpak run --filesystem=home \
     --env=WINEPREFIX=$PWD/cache/wine-oracle \
     --env=WINEDLLOVERRIDES="mscoree,mshtml=" \
     org.winehq.Wine cache/installers/picasa39-setup.exe /S
   ```
4. Pre-seed first-run scan suppression (the PicasaStarter recipe — see
   `picasastarter-notes.md`) under
   `cache/wine-oracle/drive_c/users/<user>/AppData/Local/Google/`:
   - `Picasa2Albums/watchedfolders.txt` — CRLF line:
     `Z:\...\cache\synthetic-library` (Wine maps `/` as `Z:`)
   - `Picasa2Albums/frexcludefolders.txt` — empty
   - `Picasa2/db3/thumbs_index.db` — 356-byte seed file (fetched from the
     PicasaStarter mirror's embedded resource)

## Launch

```sh
flatpak run --filesystem=home \
  --env=WINEPREFIX=$PWD/cache/wine-oracle \
  --env=WINEDLLOVERRIDES="mscoree,mshtml=" \
  org.winehq.Wine "C:\\Program Files (x86)\\Google\\Picasa3\\Picasa3.exe"
```

First interactive run asks about usage feedback and backup (decline both);
after that it shows the standard Picasa window and works. Headless runs (no
interaction) still perform the folder scan and write the database, then exit.

## Validation results (2026-06-11)

- Watched-folder seeding works: `scanlist.txt` = `+C:\` `+Z:\`;
  `thumbindex.db` contains the three synthetic folders (full paths, UTF-8/
  ASCII) and all 24 photos (stored name-only — files join to parent folders
  by index, matching the `imagedata.parent` field design).
- Database appears in `db3/` within ~20s of first launch: the full
  `.pmp` set for `albumdata`/`catdata`/`imagedata` plus `thumbs2_0.db`,
  `bigthumbs_0.db`, `previews_*`, `repository.dat`, `facetemplatesV2_0.db` —
  matching the schema vocabulary recovered from the binary
  (`picasa-binary-notes.md`).
- **Round-trip parse confirmed**: a ~30-line Python reader using the sbktech
  header layout (magic `0x3fcccccd`, type, `0x1332`, `0x00000002`, type,
  `0x1332`, count) correctly read oracle-written files:
  - `imagedata_width/height.pmp` (type 0x1 uint32) → exactly our synthetic
    dimensions; first rows are 0 (folder rows; `imagedata_filetype` = 1 for
    folders, 2 for JPEG photos; 28 rows = 24 photos + folder entries)
  - `catdata_name.pmp` (type 0x0 string) → Picasa's internal categories:
    `Labels, Projects (internal), Folders on Disk, Web Albums, Web Drive,
    Exported Pictures, Other Stuff, Hidden Folders, People`
  - `albumdata_name.pmp` → built-in virtual albums (`Starred Photos,
    Screensaver, Recently Updated, Emailed, Uploaded, ...`) followed by the
    three watched folders — folders and albums share the albumdata table.

## Differential-testing recipe

Harness: `scripts/oracle-diff.py` (stdlib-only; watches `db3/`,
`Picasa2Albums/`, and the synthetic library tree).

1. `uv run scripts/oracle-diff.py snapshot` — baseline.
2. Perform ONE action in the Picasa UI (star a photo, caption, crop, tag a
   face, make an album).
3. `uv run scripts/oracle-diff.py diff` — decoded report: row-level `.pmp`
   deltas (all sbktech field types), unified diffs for `.picasa.ini`/`.txt`/
   `.pal`, first-difference hexdump for other binaries.
4. `uv run scripts/oracle-diff.py capture <slug> --note "exact UI action"` —
   writes `fixtures/oracle/NNN-<slug>/{before,after,diff.md,meta.json}` and
   re-baselines for the next action. Thumbnail/preview caches are reported
   but not copied (`--include-blobs` overrides).
5. `uv run scripts/oracle-diff.py pmp <file.pmp>` — ad-hoc decode of any pmp.

Fixture pairs are synthetic → committable; the accumulated
`fixtures/oracle/` corpus is the ground truth for the Fauxcasa parser/writer
test suite. Session log: bead `fauxcasa-dcc`.

## Differential findings (2026-06-11 session, fixtures 001–013)

Harvested corpus: `fixtures/oracle/001-star-photo` … `013-face-tag-manual`
(each has `diff.md` with decoded deltas). Action→storage map:

| Action | Synchronous (at action time) | Lazy flush (~2–6 min cycle) |
|---|---|---|
| Star | ini `star=yes` + `db3/starlist.txt` (full `Z:\` path) | nothing observed |
| Unstar | ini line deleted (never `star=no`); starlist entry removed | `inisync` tick |
| Caption | JPEG XMP `dc:description` + new IPTC 8BIM block; ini gets `backuphash` only | `imagedata_caption.pmp` (flushed immediately this time — action forced a global db write-out) |
| Keyword | JPEG XMP `dc:subject` + IPTC 2:25 Keywords; ini `backuphash` | `imagedata_tags.pmp` (type 0x6, sparse), wordhash. `tags.txt` stays empty |
| New album | `albumdata_*` row (uid, `]album:<uid>` token, date = *photo* date) + ini `[.album:<uid>]` + per-photo `albums=<uid>`. No `.pal` file | — |
| Rename album | ini `name=` rewritten in place; **uid/token stable** | `albumdata_name` row in place; album's `inisync` row = flag word (1-bit change) |
| Crop (unsaved) | ini only: `crop=rect64(...)` + `filters=crop64=1,...` (4×16-bit fixed-point fractions) | — |
| File→Save | recipe moves to `.picasaoriginals/.picasa.ini` (filters/crop + orig dims + `moddate` FILETIME); original stashed byte-exact; JPEG rewritten | edit-state pmp family (`revertable=1`, new width/height, `crop64`/`filters` **cleared** after bake) |
| Rotate (unsaved) | ini `rotate=rotate(1)` | `imagedata_rotate` `'rotate(1)'` string, `edited=2` |
| Manual face tag | name into JPEG XMP `dc:subject` + ini `backuphash` | `imagedata_tags` row. **Face geometry never reached disk** (no facerect/facetemplates/contacts.xml, even at exit) — manual-region flow needs a retry |

General write model:

- **Two-phase**: `.picasa.ini` + the photo file (XMP/IPTC) are written
  synchronously at action time; pmp mirrors arrive on a lazy flush cycle
  (observed 2–6 min), occasionally immediately when an action forces a db
  write-out. A clean exit produced **no additional flush** — no shutdown dump.
- `backuphash=<u16>` lands in the ini for every touched photo and mirrors
  `imagedata_backuphash.pmp`.
- `albumdata_inisync`: folder rows = FILETIME of last ini sync; album rows =
  a flag word. `albumdata_date[6]` ('Search results' row) ticks on every
  action — an activity timestamp.
- Field files are sparse (`imagedata_tags.pmp` had 3 rows in a 28-row table)
  — confirms sbktech's variable-length note.
- UI grid order ≠ filename order (our synthetic photos share EXIF
  timestamps): identify the acted-on file from the diff, never from grid
  position. One caption keystroke was dropped under Wine ('aption') —
  always read back the stored text.
- Save dialog has "do not ask again" checked since fixture 005.

## Scale runs

For indexing-at-scale experiments, clone the prefix and point the clone's
`watchedfolders.txt` at a bigger generated library
(`scripts/make-synthetic-library.py --scale N`) — full recipe and the
1000-photo results are in `oracle-db3-survey.md`. Never repoint or reset the
oracle prefix itself: it is the live baseline for differential fixtures.
