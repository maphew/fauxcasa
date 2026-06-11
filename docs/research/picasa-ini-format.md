# .picasa.ini format reference

Synthesized 2026-06-11 from the fbuchinger gist ("Picasa.ini files decoded",
public domain, validated against ~800 real files, plus its comment thread),
the Picasa 3.9 binary string vocabulary (`picasa-binary-notes.md`), Chromium's
`picasa_albums_indexer.cc` (BSD-3), picasa2digikam's field experience (GPL-3,
facts only), and live samples found on the web. Normative reference for the
ini reader in `scripts/picasa_db.py`.

One `.picasa.ini` per folder. Plain Windows INI: `[section]` headers,
`key=value` lines, CRLF endings, no quoting or escaping; `=` may appear in
values (split on first `=` only). **Section order is arbitrary** — real files
interleave per-photo sections, `[encoding]`, and `[Picasa]` in any order.
Picasa never writes comment lines; `;` and `#` are NOT comment markers (`;`
is data inside faces/filters/Contacts2/text values).

## Naming history

- Picasa 1.x/2.x wrote `Picasa.ini` and backed up originals to `Originals/`.
- Picasa 3.0 build 57.52 (2008) switched new files to `.picasa.ini` and
  `.picasaoriginals/` "to be compatible with their Mac counterparts".
- Upgrades never deleted old files: a tree can contain both names, even both
  in one folder. Read `.picasa.ini` first, fall back to `Picasa.ini`.
- On Windows the file carries hidden(+system) attributes.

## Section types

| Section | Purpose |
|---|---|
| `[Picasa]` | folder-level: `name=`, `date=` (OLE double!), `category=` / `P2category=` (newer; `Hidden Folders`, `Downloaded Albums~<uid>`), `description=`, `location=`, `<account>_lh=<web album id>` (dynamic key), `link=` |
| `[encoding]` | exactly `utf8=1`; may appear anywhere; absent in Picasa-2-era files (locale codepage) |
| `[Contacts]` | ≤3.8: `<16-hex id>=<account>_lh,<web id hex>` — no names; need contacts.xml |
| `[Contacts2]` | 3.9: `<16-hex id>=<Display Name>;;` (split on `;`, field 0). Both tables can coexist; ids inherited downward from ancestor folders' inis |
| `[<filename>]` | one per media file, keys below |
| `[.album:<32hex>]` | 3.9+ album definition: `name=` (may be absent), `token=` (equals section id), `date=` (**ISO 8601 with offset** — unlike `[Picasa]`'s OLE double), `description=`, `location=`, `<account>_lh=` |
| `[photoid]` | rare; `<decimal web photo id>=<filename>` |
| `[(null)]` | corruption artifact (printf of NULL); usually empty; don't crash |

The 3.9 binary also holds marker strings `]album`, `]face`, `]ignoreface`,
`]unknownface`, `]facealbum:%d`, `]expeople:%d` — preserve unknown sections.

## Per-file keys

| Key | Format | Notes |
|---|---|---|
| `star=yes` | literal | presence-only; never `star=no` |
| `caption=` | text | only for formats with no IPTC/XMP home; JPEG captions go to IPTC |
| `keywords=` | CSV | only non-JPEG (JPEG → IPTC) |
| `rotate=rotate(N)` | N∈0..3 | quarter-turns CW; does NOT transform faces/crop coords |
| `crop=rect64(...)` | rect64 | current crop; history lives in `filters=` as `crop64` op |
| `filters=` | op stack | see below; `redo=` uses the same grammar (undone ops) |
| `faces=` | `rect64(..),<id>;...` | see below |
| `albums=` | CSV of 32-hex tokens | 3.9+; tokens may not match any album (ini/DB drift) — skip those |
| `backuphash=` | decimal | also `<backup set name>-backuphash=` (keys contain spaces/hyphens!) |
| `IIDLIST_<account>_lh=` | 16-hex | web-upload id; dynamic key name |
| `moddate=` | 16 hex | **inferred**: byte-for-byte LE dump of a Windows FILETIME (`8094e2826277cd01` → 2012-08-11 01:42:05 UTC); community never cracked it; verify against oracle |
| `originhash=` | 32 hex | pairs with `.picasaoriginals` |
| `width=`/`height=` | decimal | cached dims (mostly RAW/video) |
| `textactive=` | 0/1 | text overlay visible |
| `text=` | `;`-delimited record | text-tool annotation (enable;int;int;string;font;4 floats;`v1,`+colors/style;;) |
| `geotag=` | `lat,lon` floats | |
| `screensaver=yes` | literal | |

## rect64 / crop64

Four fractions of the stored-on-disk image dimensions — **left, top, right,
bottom** — each `u16 = round(frac * 65536)`, concatenated MSB-first into a
u64, printed `%llx` (lowercase, **leading zeros stripped**). Wrapped as
`rect64(<hex>)` in `faces=`/`crop=`; bare in `crop64=1,<hex>`. The pmp
`imagedata.crop64` column stores the same u64.

Decode: strip wrapper → **left-pad to 16 chars** → slice 4×4 → each
`int(g,16)/65536`. Never slice the unpadded string.

Worked examples:
- `rect64(3f845bcb59418507)` → (0.24810791, 0.35856628, 0.34864807, 0.51963806)
- `rect64(4a8e8e6b)` → (0.0, 0.0, 0.29122925, 0.55632019) — face touching
  the top-left corner; on 2304×1296 = pixel box x=0, y=0, w=670, h=720
- `crop64=1,10000000f1ddff49` → (0.0625, 0.0, 0.94477844, 0.99720764)

Byte-faithful writing requires the zero-stripped form (Picasa reads padded
too). Floats elsewhere are printf'd at 6 decimals; keep raw strings to
round-trip.

## faces=

`rect64(<hex>),<contact id>` joined by `;` (tolerate trailing `;`).
Contact ids are `%llx`-printed → can be <16 chars (`632e71e2ffd6c6d`);
**zero-pad to 16** before joining `[Contacts2]`/contacts.xml (stored padded
there). `ffffffffffffffff` = face with unconfirmed name suggestion; faces
with no suggestion are absent entirely. Gotchas: orphaned ids (no name
anywhere), conflicting names across folders (contacts.xml wins), "Write
faces to XMP" *removes* `faces=` from the ini. Coordinates are relative to
stored pixels: `rotate=` doesn't transform them, EXIF orientation handling
is the consumer's job.

## filters=

`<op>=1,<p1>,...,<pN>;` repeated; ordered edit history (ops can repeat);
`=1` is an enabled flag, always 1. Param types: floats (6 decimals, usually
[0,1]), AARRGGBB hex colors (leading zeros stripped — `ffff` observed),
CROP = bare rect64. Ops confirmed in the 3.9 binary: autocolor, autolight,
crop64, enhance, fill, finetune2, glow2, grain2, radblur, redeye, retouch,
sepia, tilt, unsharp2. Gist-only (3.8-era, wild-validated): bw, warm, tint,
sat, ansel, radsat, dir_tint; plus legacy `unsharp` (seen in `redo=`).
Treat unknown ops as opaque and preserve them.

Examples: `tilt=1,0.280632,0.000000;` ·
`finetune2=1,0.333333,0.176842,0.193684,00000000,0.000000;` ·
`dir_tint=1,0.306743,0.401515,0.250000,0.250000,ff5bfff3;`

## Robustness requirements (from the wild)

- Duplicate sections AND duplicate keys occur (crashes mid-write); preserve
  both, don't configparser-merge.
- Mixed encodings inside one file even with `utf8=1` (picasa2digikam issue
  14: stray 0xd8 byte mid-file). Decode UTF-8 with a byte-preserving
  fallback (surrogateescape).
- Garbage seen: `[(null)]` sections; byte-reversed lines
  (`05292=hsahpukcab` = `backuphash=29250` reversed).
- Stale `[<filename>]` sections for deleted files are normal.
- Two date formats in one file (OLE double vs ISO 8601, see table).
- Dynamic key names: never whitelist exact keys only.
- Fail soft per-line/per-section, never per-file.
