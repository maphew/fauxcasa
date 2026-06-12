# Picasa 3.9 binary static-analysis notes

Findings from a strings/resources pass over the final Picasa for Windows
release, for interoperability research (file formats, registry locations,
runtime behavior). No code was decompiled; everything below comes from
printable strings in the shipped binaries.

## Provenance

- Installer: `picasa39-setup.exe`, retrieved 2026-06-11 from the Wayback
  Machine capture of `http://dl.google.com/picasa/picasa39-setup.exe`
  (`https://web.archive.org/web/2016id_/http://dl.google.com/picasa/picasa39-setup.exe`)
- SHA-256: `482c1a547d8d3aa25ee446d30ea986de63ef8c8d68b8d1109dd3d9b714e73e08`
- NSIS self-extracting archive; main binary `Picasa3.exe` (10 MB, file date
  2015-10-13 — the final 3.9.141 build). Also ships `PicasaPhotoViewer.exe`,
  `Picasa3i18n.dll` (27 MB of localized strings), `MovieThumb.exe`.
- Cached (not committed): `cache/installers/` holds the installer, extracted
  tree, and full `strings` dumps (`strings-ascii.txt`, 85k lines).

## Database schema vocabulary

The `.pmp` column-per-file database (see `sources/sbktech-2011-picasa-pmp-format.md`)
stores one table per filename prefix. The binary contains the schema string
table; field lists appear in this order:

**`imagedata`** (one row per indexed file):
`parent, filetype, fileflags, size, creation, modified, updated, width,
height, rotate, crop64, flipped, edit_width, edit_height, filters, text,
textactive, tags, edited, revertable, originslow, originfast, uid64,
aliasparents, colorspace, personalbumid, suggestionpersonalbumid,
facequality, facerect, deferredface, deferredregion, facerectdata,
personalbumrecs, personalbumrecvalues, personalbumrecs2,
personalbumrecvalues2, peoplealbumchecksum, tagdate, fdbhash, backuphash`

**`catdata`** (categories): `catpri` (+ `name`, `state` per sbktech)

**`albumdata`** (albums/virtual collections):
`token, filename, date, category, description, location, hascollage, inisync`

Version-sentinel strings adjacent to the schema: `dbVersion, P2category,
contactsversion, frversion, gpsversion, colorspaceversion, rawversion`.

Code-xref follow-up (fauxcasa-5kl disassembly of the repository accessors at
0x4141d0 get / 0x4143c0 set): the keys actually read through the
repository.dat accessor are the nine the oracle writes **plus `DBID` and
`syncversion`** (replication id and Picasaweb-sync version — `DBID` is
corroborated by the discovery string
`server=picasa,computer=%s,user=%s,httpport=%d,repl=1,dbid=%s`). `dbVersion`
and `P2category`, despite sitting in the same string table, do *not* flow
through the repository accessor — `dbVersion` is a registry
`Preferences\dbVersion` value, `P2category` is category/INI code.
`repository.dat` is class `ytRepository`, member `m_repo`;
`usernames.dat` is the same class, member `m_usernameRepository` — same
on-disk layout. No literal usernames.dat key strings exist in the binary:
the ~9 variable-key accessor call sites build keys at runtime from account
identifiers and compare values against `"1"`, so a populated usernames.dat
keys on the account identity itself (statically uncharacterizable). The
shared magic `0x3fcccccd` is the IEEE-754 float **1.6** — read it as a
format-version constant.

## Database directory contents

- Directory literals: `#db3\` and `#db3_d\` (the `_d` variant appears to be a
  staging/dirty copy used during persistence), `#contacts\`.
- Fixed-format sidecar DBs (thumbs.db-container format per sbktech):
  `thumbs.db, thumbs2.db, bigthumbs.db, previews.db, albums.db,
  profilephotos.db, facetemplatesV2.db, facetemplates_0.db,
  facetemplates_index.db, thumbindex.db / thumbindex.tid`
- Other data files: `repository.dat`, `usernames.dat`, `contacts.xml`,
  `Contacts2`, IO queues `ioqueue\slingshot.ioq / filesafe.ioq / albumsafe.ioq`.
- **Locking/persistence**: `_lock.lck` / `_lock` / `.lck`, plus the message
  *"The presence of this file indicates that the database has been persisted
  successfully but there was a failure copying these files back to the active
  db directory."* — i.e. Picasa persists to a staging dir and copies back,
  with a lock/sentinel protocol. Directly relevant to multi-machine sharing
  (and to why PicasaStarter had to serialize access).
- Database maintenance: `compacting` / `"Always Compact"` strings — the DB
  has an explicit compaction pass.

## `.picasa.ini` vocabulary

Keys present as exact strings: `faces, crop, crop64, filters, star, caption,
rotate, keywords, albums, backuphash, width, height, textactive, moddate,
redo, originhash`. Section/marker strings with a `]` prefix: `]album`,
`]album:%d`, `]face`, `]ignoreface`, `]unknownface`, `]facealbum:%d`,
`]expeople:%d` — these look like internal markers for ini section names
(`[.album:<id>]` style sections are known from community docs).

Confirmed edit-filter operation names (the ops that appear in `filters=`
stacks): `autocolor, autolight, crop64, enhance, fill, finetune2, glow2,
grain2, radblur, redeye, retouch, sepia, tilt, unsharp2`. (Community docs
list more, e.g. `bw`, `warmify`; those strings may be stored differently —
treat this list as confirmed-minimum, not exhaustive.)

## Faces / geotags

- Face template metadata format string:
  `conf(%.3f),pan(%.3f),leye(%.3f,%.3f),reye(%.3f,%.3f),mouth(%.3f,%.3f)` —
  per-face confidence, pan angle, and eye/mouth landmark coordinates.
- Geotag export uses a KML `LookAt` template:
  `<LookAt><longitude>%f</longitude><latitude>%f</latitude>...`.

## Registry

- Active root: `SOFTWARE\Google\Picasa\Picasa2` (Picasa 3.x kept the
  "Picasa2" key name) with subkeys `Preferences\`, `Runtime\`, `Update`.
- Watched-folder config: `Preferences\HotFolders`; also `RootPath`,
  `skipinitialscan`, `initialscan`, view-state keys (`LastViewRoot`,
  `LastAlbumSelected`, `flat`, ...).
- Legacy roots still checked: `Software\Lifescape Solutions Inc.\Picasa` /
  `\Picasa2` / `\downloader` (Picasa's pre-Google publisher).
- No `AppDataPath` string found in the ASCII/UTF-16 dumps — the database
  relocation mechanism used by PicasaStarter needs confirmation from its
  source (tracked separately).

## Leads for later

- `Picasa3i18n.dll` (27 MB) holds the localized UI strings — useful for
  reconstructing complete menu/dialog inventories.
- Format support flags: `SupportBMP, SupportPSD, SupportTIF, SupportWEBP,
  SupportPNG, SupportGIF, SupportMovies`.
- `editpanel/picnik` — the cloud-edit integration point.
- `usefileloadcache`, `ReportStats`, memory telemetry keys (`AppMem`,
  `AppPeakMem`, ...) hint at the performance instrumentation culture.
