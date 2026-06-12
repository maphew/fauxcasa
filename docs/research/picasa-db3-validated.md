# db3 binary formats: validated reference (.pmp, thumbindex.db)

Status 2026-06-12: every claim below was either byte-verified against the
Wine-oracle database (real Picasa 3.9.141 indexing the synthetic library —
see `wine-oracle.md`) or re-derived from prior art with the source noted.
This is the normative reference for `scripts/picasa_db.py`; the executable
proof is `scripts/test_picasa_db.py` (oracle integration tests).

## .pmp column files

Header, 20 bytes, all little-endian (sbktech 2011; identical constants in
Chromium `pmp_constants.h`):

```
u32 magic 0x3fcccccd | u16 field-type | u16 0x1332 | u32 0x00000002
| u16 field-type (repeat, must match) | u16 0x1332 | u32 entry-count
```

Field types: 0x0/0x6 NUL-terminated strings, 0x1/0x7 uint32, 0x2 float64
OLE date, 0x3 uint8, 0x4 uint64, 0x5 uint16. Chromium only ever handled
0x0–0x4; 0x5/0x6/0x7 come from sbktech/picasa3meta. The oracle uses 0x7
for `imagedata_edit_width`/`edit_height` (plain uint32 behavior confirmed
by payload arithmetic); 0x5/0x6 remain unseen in oracle output.

Validation (Chromium's hardened reader, confirmed appropriate by the
oracle): all four constants checked; the two field-type copies must match;
payload length must EXACTLY equal `count × width` for fixed types (no
trailing bytes), and for strings exactly `count` NUL-terminated strings
must consume the payload. Chromium additionally caps files at 50 MB and
asserts the expected column type from the caller. Oracle sweep result:
all 22 column files pass exact-fit; strings are valid UTF-8 throughout.

`<table>_0` marker files are exactly the 4 magic bytes — table-existence
markers, not columns (Chromium requires the marker to exist before reading
a table).

**Variable-length columns are real**: oracle `albumdata_music.pmp` has 7
entries while its 9 sibling columns have 10. Reading past a short column =
"no value" (Chromium NULL-pads and skips rows whose required fields fail).

### OLE/VARIANT dates (type 0x2)

Days since 1899-12-30 00:00 **local wall-clock** (per Microsoft's DATE
docs). Negative values: whole part is signed days, fractional part is the
*absolute* time of day — -1.25 = 1899-12-29 06:00. Prior art diverges and
is wrong on negatives: Chromium converts linearly; picasa3meta applies
`t = 1.0 + t` to negative fractions (matches spec only at .0/.5). Healthy
Picasa data never goes negative (UI floor is 1903); treat negatives as
suspect rows. Oracle: `albumdata_date` rows are creation timestamps for
the built-in virtual albums and the earliest EXIF date for folder rows.

### albumdata semantics (from Chromium picasa_album_table_reader, BSD-3)

`category`: 0 = album (token must start `]album:`), 2 = folder on disk
(`filename` holds the path), 8 = people album (oracle: the stock
`]unknownface` row and per-person `]facealbum:<row>` rows created by face
tagging), 0xffff = invalid sentinel. Chromium — which consumes only
categories 0 and 2 — skips rows with empty name/uid as garbage
(deleted/auto-generated); do NOT apply that rule to people albums: the
oracle's person album carries no uid at all (the `uid` column is simply
not extended to its row — its identity lives in `albumcontactids`,
byte-equal to the ini `[Contacts2]` key and the contacts.xml contact id).
Oracle confirms: rows 0–6 are stock virtual albums (`]star`,
`]screensaver`, `]updated`, `]history:email`, `]history:upload`,
`]unknownface`, `]search`), rows 7–9 the watched folders with full `Z:\`
paths and `]album:<32hex>` tokens, row 10 a user-created category-0 album
(fixture sessions 003/011), row 11 the person album from session 014.

## thumbindex.db

```
u32 magic 0x40466666 | u32 entry-count
per entry:
  NUL-terminated path/name (UTF-8)
  u64 taken     FILETIME ── EXIF DateTimeOriginal (local→UTC), mtime
                   fallback; 0 on the face-crop record
  u64 modified  FILETIME ── file mtime (matched on-disk 28/28; face-crop 0)
  u32 size      ── byte size of the file (exact match 28/28; folders 0,
                   face-crop records 1 — not a real size)
  u8  ftype     ── 0x01/0x05 directory, 0x02 jpeg, 0x03 gif, 0x07 psd,
                   0x08 avi, 0x0d tiff, 0x0e png, 0x12 nikon-raw, 0x1e xml,
                   0x00 empty (table from xkikeg/PicasaDB), 0xe9 face-crop
                   (xkikeg said "empty"; on the oracle's face-crop record
                   it equals the low byte of imagedata filetype 0x3e9)
  u32 flags     ── opaque; 0 on every real entry, 3 on the oracle's
                   face-crop record
  u8  valid     ── 0 = invalidated (then parent must be none); 1 in oracle,
                   including the face-crop record
  u32 parent    ── entry index of containing folder; 0xffffffff = none
```

The 26 bytes between name and parent were "unknown" in picasa3meta; the
u64/u64/u32/u8/u32/u8 split is xkikeg/PicasaDB's (GPL-3, facts only), with
semantics refined by the oracle: the first FILETIME is **not** filesystem
creation time — for the 21 synthetic photos with EXIF DateTimeOriginal it
equals that tag exactly (converted local→UTC); for the 7 entries without
(folders + the photo00.jpg files, which carry EXIF but no DateTime tags)
it falls back to mtime. The `size` u32 is opaque in xkikeg but matched
stat() byte-for-byte on all 28 real oracle entries (the face-crop
record's size is 1 — not a real size).

Join model (validated 29/29): **record number in thumbindex == record
number in imagedata**. Folder entries store full absolute paths (always
`parent = 0xffffffff`, even for nested folders); file entries store bare
names and join through `parent` — a single level of indirection. The
oracle's `ftype` byte holds the **low byte** of `imagedata_filetype` at
every record (real entries have filetype ≤ 0x1e so the bytes agree
outright; the face-crop record is 1001 = 0x3e9 there and 0xe9 here).
Dimensions/sizes joined through this mapping match the real JPEGs 24/24.

### Empty-name records: face crops vs deletions

An empty name marks a record with no file of its own; `parent`
discriminates the two cases picasa3meta describes. **Face-crop records
(parent retained) are now reproduced in our oracle**: the manual face tag
of fixture session 014 created one on its lazy db flush. Observed shape —
empty name, `parent` = the photo the face was tagged on, `valid` 1,
`flags` 3, `size` 1, `ftype` 0xe9, both FILETIMEs 0. Its imagedata row is
a virtual face row: `filetype` 1001 (0x3e9), `crop64` == `facerect` ==
the rect64 written to the ini `faces=` line, `width`/`height` copied from
the parent photo's row, `personalbumid` -> the albumdata person-album row
(token `]facealbum:<row>`, category 8, `albumcontactids` = the contact
uid), `tagdate` = the tagging time as an OLE date. The parent photo's own
row gets the rect in `facerect` (was the 0x1 sentinel before). Sparse
pmps are padded up to the new row count with their type's default
(`crop64` uint64 0, `rotate` empty string).

True deletions (empty name, parent 0xffffffff, slot retained because
record numbers never reuse) remain unreproduced in our oracle.
picasa3meta's reader also accepted 0xff as a name terminator (unexplained
— we require NUL until evidence appears).

## Prior-art license notes (correction)

picasa3meta **is** GPL-3 (COPYING sits inside the package dir, per-file
headers say GPL v3+; GitHub's license detector misses it — our survey table
said "none"). xkikeg/PicasaDB GPL-3. Chromium BSD-3 (logic may be adapted
with attribution). Format layouts/constants are uncopyrightable facts;
implementations here are re-derived.
