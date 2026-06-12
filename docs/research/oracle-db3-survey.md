# Structural survey of a real-Picasa-written database (fauxcasa-ok6)

Snapshot date: 2026-06-11. Surveyed database: the Wine oracle
(`docs/research/wine-oracle.md`) — real Picasa 3.9.141 indexing the synthetic
library, after the first six differential fixture sessions
(`fixtures/oracle/001`–`006`). The oracle is a live, growing baseline; the
numbers below are a snapshot and will drift as more fixtures are captured.

The bead originally targeted a read-only copy of the real family DB from the
local network; with Picasa now running locally under Wine, the survey runs
against an oracle-written database instead — same binary, same formats, zero
privacy exposure. The tooling stays counts-only by construction
(`picasa_db.py survey` emits no decoded values, paths, or timestamps), so the
same command remains safe to point at a private library later if real-scale
numbers are ever wanted.

Reproduce:

```sh
uv run scripts/picasa_db.py survey \
  cache/wine-oracle/drive_c/users/<user>/AppData/Local/Google/Picasa2/db3 \
  --library cache/synthetic-library --json
```

## db3 file census

61 files: 37 `.pmp` column files across 3 tables (`albumdata` 11 columns,
`catdata` 3, `imagedata` 23), 3 table markers (`<table>_0`), `thumbindex.db`,
and 20 "other" (thumbnail/preview caches and their `*_index.db` files,
`repository.dat`, `usernames.dat`, list sidecars `starlist.txt`,
`scanlist.txt`, `saverlist.txt`, `tags.txt`, `facetags.txt`,
`facetemplatesV2_0.db`, `wordhash.dat`, `albums_0.db`, `profilephotos_0.db`).

**Every structured file parses strictly clean**: all 37 pmp columns
(`parsed == count`, no trailing bytes), thumbindex, both key/value stores.
That is the parser-validation half of this bead: nothing real Picasa wrote —
including mid-session, with the UI open — violates the formats documented in
`picasa-db3-validated.md`.

## Table shapes (rows / populated / distinct)

- `imagedata`: 28 rows = 24 photos + 4 folder rows (3 watched folders + the
  library root). Column lengths legitimately vary (28 / 25 / 24): Picasa
  extends a column only when a row past its end gets a non-default value.
  Defaults dominate: of 23 columns, only the identity/scan columns
  (`width`, `height`, `avgcolor`, `originfast`, `filetype`, `facerect`,
  `facerectdata`) are populated for every photo; the edit columns
  (`caption`, `rotate`, `edited`, `revertable`, `originslow`, `backuphash`)
  carry exactly the handful of values the fixture sessions created, and
  `crop64`, `filters`, `flipped`, `redo`, `text`, `textactive`,
  `colorspace`, `onlinechecksum`, `edit_width`, `edit_height` are 100 %
  default on this library.
- `albumdata`: 11 rows = 7 virtual albums (Starred Photos, Screensaver, …
  category 0 with `]album:`/`]star`-style tokens), 3 watched folders
  (category 2, filesystem path in `filename`), 1 invalid-category row.
  `name`/`token`/`uid` populated on all 11; `hascollage`, `location`,
  `music` never populated.
- `catdata`: 9 fixed internal categories, fully populated, `state` constant.

Sentinel semantics found via `distinct`: `facerect`/`facerectdata` read as
"populated" on all 24 photos but hold only the constant `1` / `"1"` —
a *scanned, no face found* marker (folder rows hold the type default `0`).
Populated-count ≠ feature-usage for face columns; real face tags will need a
differential fixture to pin the encoding.

## Version sentinels (`repository.dat`)

Same `0x3fcccccd` magic as pmp, then `u32 pair-count` and NUL-terminated
key/value pairs (reader: `read_repository`):

| key | value |
|---|---|
| KeywordVersion | 1 |
| contactsversion | 1.0 |
| rawversion | 1.1 |
| colorspaceversion | 1.1 |
| frversion | 1.5 |
| Folders | 1 |
| gpsversion | 1.0 |
| flat | 1 |
| IDPersist | 2 |

`usernames.dat` shares the layout, 0 pairs (no Google sign-in). Disassembly
of the repository accessors (fauxcasa-5kl, see `picasa-binary-notes.md`)
settled what static analysis can: the binary reads two more repository.dat
keys it never wrote at the oracle — `DBID` (replication/database id) and
`syncversion` (Picasaweb sync) — while `dbVersion`, despite sitting in the
same string table, is a registry `Preferences\` value, not a repository
key. usernames.dat is the same `ytRepository` class (member
`m_usernameRepository`), and **no literal key strings for it exist in the
binary**: its keys are built at runtime from account identifiers (values
compared against `"1"`), so the populated format keys on the account
identity itself and cannot be characterized without a live Google sign-in —
which the long-retired Picasaweb/ClientLogin endpoints no longer offer. The
survey accordingly keeps redacting unknown keys unconditionally. A Fauxcasa
writer should emit the same nine key/value pairs; pair *order* is not
stable — fixture 002 recorded Picasa itself reordering the block during an
ordinary caption flush. Whether Picasa tolerates missing or different
sentinels is the subject of the acceptance experiment below.

## Feature usage

db3 side: `starlist.txt` 1 entry (001), 1 caption (002), 1 rotate (006),
1 edited (also the 006 rotate; the flag value is 2, not 1) and 1 revertable
(the 005 baked crop) — note `edited` and `revertable` are set on *different*
photos here. `crop64` is 0-populated — not because no crop happened, but
because the fixture-005 File→Save *baked* the crop and cleared the db3 edit
state; the undo recipe (`crop=`, `filters=`, original dimensions) lives in
`.picasaoriginals/.picasa.ini` (see `fixtures/oracle/005-*/diff.md`).
Library side (4 `.picasa.ini` files counting the `.picasaoriginals` one,
0 anomalies, 0 read errors): 8 sections (7 per-file + 1 album), keys all
within the known vocabulary, features: 1 starred, 2 album memberships,
4 edit keys. These counts line up one-for-one with the six fixture
actions — the survey independently re-derives what the differential
sessions did, which is exactly the cross-check the tool should provide on
a real library.

## Scale

Current oracle: 24 photos / 1.7 MB of images → db3 866 KB total, of which
793 KB is regenerable `.db` caches (637 KB thumbnail/preview tiers,
156 KB album-cover cache `albums_0.db`) versus ~4 KB of pmp catalog and
1.4 KB of thumbindex. The catalog is tiny; the caches dominate and are
safely regenerable.

### 1000-photo indexing run

To validate the parsers at scale without real data, a 1000-photo / 40-folder
synthetic library (`scripts/make-synthetic-library.py --scale 1000`) was
indexed by the same Picasa binary in a disposable clone of the oracle
prefix. Recipe (the oracle prefix itself stays untouched):

```sh
cp -a cache/wine-oracle cache/wine-oracle-scale
G=cache/wine-oracle-scale/drive_c/users/<user>/AppData/Local/Google
rm -rf "$G"/Picasa2/{db3,cache,ioqueue,tmp,runtime}
mkdir -p "$G/Picasa2/db3"   # re-seed per the PicasaStarter recipe:
cp .../wine-oracle/.../db3/thumbs_index.db "$G/Picasa2/db3/"
printf 'Z:\\...\\cache\\synthetic-library-scale\r\n' > "$G/Picasa2Albums/watchedfolders.txt"
printf '' > "$G/Picasa2Albums/frexcludefolders.txt"
# launch Picasa with WINEPREFIX=$PWD/cache/wine-oracle-scale, poll db3,
# then: flatpak run --command=wineserver --env=WINEPREFIX=... org.winehq.Wine -k
```

Results (run 2026-06-11, ~4 minutes wall clock):

- Scan and thumbnailing finished within ~90 s; Picasa then idled and
  flushed the pmp catalog in one batch ≈ 2.5 min later — the catalog is
  written lazily, so a reader can encounter a db3 whose caches are minutes
  ahead of its pmp columns.
- `thumbindex.db`: 1041 entries = 1000 jpegs + 40 folders + the watched
  root, 0 deleted — exact match with the generated library.
- **Every structured file parses strictly clean**: 22 pmp columns, 3 table
  markers, `thumbindex.db`, `repository.dat`, `usernames.dat` — 28 of the
  45 db3 files; the other 17 are thumbnail/preview caches and txt sidecars
  the survey reports by size only. Row counts agree with the library
  everywhere (thumbindex 1041; every pmp `parsed == count` ≤ 1041, no
  trailing bytes).
- Column sparseness confirmed at scale: a freshly indexed library gets only
  9 of the 23 `imagedata` columns seen in the oracle (`width`, `height`,
  `avgcolor`, `originfast`, `filetype`, `facerect`, `facerectdata`,
  `edit_width`, `edit_height`) — every edit column appears only after the
  first edit. `originfast` is unique per photo (1001 distinct over 1041
  rows: 1000 photos + the folder-row default 0), consistent with a
  per-file content hash.
- No `.picasa.ini` is written anywhere in a library that has only been
  indexed — ini files appear on first user action, so a reader must not
  assume their presence.
- Size: db3 totals 20.96 MB for 1000 photos (12.2 MB of source JPEGs) —
  20.74 MB of that is regenerable `.db` caches (almost entirely the
  thumbnail/preview tiers), 53 KB pmp catalog (~50 bytes/photo) and 48 KB
  thumbindex. Extrapolated to a 100k-photo real library: catalog ≈ 5 MB
  plus a ~5 MB thumbindex, trivially parseable; the caches, not the
  catalog, are the storage cost.

## Product-spec implications (feeds fauxcasa-8g7)

- The pmp catalog is column-sparse: a writer only needs to emit columns it
  has values for, at the lengths it has values for. Readers must treat
  missing columns and short columns as "default for every row" (already the
  `PmpTable.row()` contract).
- Identity columns Picasa always fills (`width`, `height`, `avgcolor`,
  `originfast`, `filetype`) are the minimum a compatible writer must produce.
  Edit state lives redundantly in db3 *and* `.picasa.ini`; the fixture
  sessions show the ini written first (004) and db3 flushed later (005), so
  the ini side is the durable record (hypothesis to confirm: Picasa rebuilds
  db3 edit columns from ini on rescan — `albumdata_inisync` exists but its
  semantics are unconfirmed).
- `repository.dat` holds nine version sentinels — trivial to write; emit
  the same pairs (order varies between Picasa's own flushes; whether Picasa
  accepts foreign or differing sentinel blocks is untested).
- Cache files (`thumbs*`, `previews*`, `bigthumbs*`, `albums_0.db`) are the
  bulk of db3 by bytes and are regenerable; Fauxcasa can ignore their
  contents and let Picasa rebuild them.
