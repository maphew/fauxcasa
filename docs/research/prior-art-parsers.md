# Prior art: open-source parsers for Picasa data formats

Survey of existing reverse-engineering work (2026-06-11), so Fauxcasa builds
on prior art instead of redoing it. See also `picasa-binary-notes.md` (our own
strings pass) and `sources/sbktech-2011-picasa-pmp-format.md` (the canonical
`.pmp` format writeup).

## Recommended primary references

1. **Chromium's Picasa importer** — the highest-quality `.pmp` code in
   existence: production C++, BSD-3-Clause, unit-tested, written to run on
   untrusted data in a sandboxed process. Removed from Chromium ~2016-17 when
   the Media Galleries API died; browse via historical tags:
   `https://chromium.googlesource.com/chromium/src/+/refs/tags/44.0.2400.0/chrome/utility/media_galleries/`
   and `.../chrome/common/media_galleries/` — `pmp_column_reader.cc`,
   `picasa_album_table_reader.cc`, `picasa_albums_indexer.cc`,
   `pmp_constants.h`, `picasa_types.h`.
2. **fbuchinger's ".picasa.ini decoded" gist** (public domain, validated
   against ~800 real ini files): the most complete `.picasa.ini` spec —
   sections, keys, rect64/crop64 fixed-point encoding (beware: leading zeros
   are stripped from the hex), full filter-parameter table, 3.8→3.9
   differences. https://gist.github.com/fbuchinger/1073823
3. **picasa2digikam** (GPL-3.0, Python, still maintained — last push
   2026-03) — tested `rect64.py`, contacts.xml handling, real-world gotchas
   (inconsistent ini after crashes, orphaned contact IDs, conflicting names
   for one region). GPL: re-derive, don't copy, unless Fauxcasa goes GPL.
   https://github.com/Philipp91/picasa2digikam

## The format in one paragraph

`.pmp` files are a column-per-file database: magic `0xcd 0xcc 0xcc 0x3f`
(little-endian float 1.6), then field-type (2 bytes), `0x1332`, `0x00000002`,
field-type again, `0x1332`, 4-byte entry count. Field types: 0x0/0x6
null-terminated strings, 0x1/0x7 uint32, 0x2 double (OLE/Variant time), 0x3
byte, 0x4 uint64, 0x5 uint16. Filename = `table_column.pmp`; rows join across
files by record number; `table_0` marker files hold only magic. Columns are
variable-length — an index valid in one column may be out of range in another
(treat as empty). OLE time epoch is 1899-12-30; negative fractional times
need `t = 1.0 + t` adjustment (picasa3meta). `thumbindex.db` maps record
numbers to full file paths — the join key between `imagedata` rows and disk.
Everything little-endian.

## Full survey

| Project | Lang / License | State | Covers |
|---|---|---|---|
| sbktech blog `Read.java` (in-post code; verbatim copy inside github.com/SaumyaSoman/MiniJarvis) | Java / none | 2011, canonical | Generic .pmp container, all 8 field types |
| [picasa3meta](https://github.com/vosbergw/picasa3meta) | Python / none | dead 2013 | Broadest single lib: .pmp (40+ imagedata cols), .picasa.ini, thumbindex.db, exiv2 |
| Chromium media_galleries (see above) | C++ / BSD-3 | frozen ~2016 | Hardened .pmp reader, albumdata, .picasa.ini album indexer, unit tests |
| [PicasaDBReader](https://github.com/skisoo/PicasaDBReader) | Java / MIT | 2017, 70★ | All .pmp + thumbindex → CSV; face data export + crop via ImageMagick; path translation |
| [picasa2digikam](https://github.com/Philipp91/picasa2digikam) | Python / GPL-3 | **active 2026** | ini stars/albums/faces, rect64 (with tests), contacts.xml |
| [fbuchinger gist](https://gist.github.com/fbuchinger/1073823) | doc / PD | ~2022 | The .picasa.ini spec (keys, crop64/rect64, filters table) |
| [sydp/picasaparser](https://github.com/sydp/picasaparser) | Python / Apache-2 | 2017 | Forensics: imagedata, index, thumbs db dump |
| [xkikeg/PicasaDB](https://github.com/xkikeg/PicasaDB) | Haskell / GPL-3 | 2012 | thumbindex.db; extracts images from thumbs/bigthumbs/previews dbs |
| [jabacrack/Picasa-database-reader](https://github.com/jabacrack/Picasa-database-reader) | C# / MIT | 2019 | Minimal .pmp example |
| [ashaduri/embed_picasa_tags](https://github.com/ashaduri/embed_picasa_tags) | PHP / Zlib | 2022 | ini faces → embedded metadata |
| [dgtombs/picasa2shotwell](https://github.com/dgtombs/picasa2shotwell) | Python / GPL-3 | 2024 | ini → Shotwell DB |
| [extractpicasainfo](https://github.com/alazhar-shamshuddin-picprocessing/extractpicasainfo) | Perl / none | 2025 | Albums + contacts → JSON/SQLite |
| [belugame/picasainiparser](https://github.com/belugame/picasainiparser) | Python / none | 2016 | ini → IPTC |
| [Forensics Wiki](https://forensics.wiki/google_picasa/) | doc | n/a | Independent format documentation |

Smaller face-rect viewers: mrgloom/picasa_ini_reader, okoteogl/PicasaViewer,
AddisMap/PicasaFaceBlur, and gists by JMalysiak, asherber, egore (2013-15).
