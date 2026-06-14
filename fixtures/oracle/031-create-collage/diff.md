# Fixture 031: create-collage

**Action:** Create a Picture Collage on the DISPOSABLE clone (live oracle untouched). Selected the whole '2015-03-15 Garden Project' folder (9 photos) -> menu Create > Picture Collage... -> default 'Picture Pile' theme, 10x15 landscape, Draw Shadows + Show Captions on -> 'Create Collage' button. CONTRAST WITH EXPORT (030): a collage IS tracked in db3 (synchronous; collage creation forced an immediate flush), even though its output lands in the same unwatched clone Pictures tree where the export left no db3 trace. db3: (1) NEW albumdata row[17] for the output collection: category=1 (= catdata 'Projects (internal)'), name='Collages', filename='C:\users\matt\Pictures\Picasa\Collages\', fresh ']album:<uid>' token + uid, description='', inisync flag word, hascollage[17]=0; albumdata grew 17->18 rows (intermediate album columns backfilled to defaults). (2) NEW imagedata rows: row[29]='Collages' FOLDER (filetype=1, dims 0); row[30]=the rendered collage JPEG (filetype=2, width=5120 height=3413 full-res, avgcolor+originfast set, facerect/facerectdata scanned-no-face=1, fileflags=1). (3) NEW pmp column materialized: imagedata_fileflags.pmp (first appearance in the corpus; fileflags=1 on the collage row[30], 0 elsewhere). (4) Search results +1 (albumdata_description '16 results'->'17 results'; albumdata_date[16] activity tick). Caches grew (albums_index/bigthumbs_index/previews_index, blobs). OUT-OF-ROOT on-disk artifacts (clone Pictures\Picasa\Collages\, outside the harness's watched roots; .picasa.ini + .cxf copied into after/collage-folder/ by hand, the 5120x3413 715KB rendered JPEG documented but not committed): .picasa.ini = '[Picasa] / P2category=Projects (internal)' (cf export 030 'Exported Pictures', folder-desc 020 'Folders on Disk'); and a NEW artifact type '<name>.cxf' = the editable collage-recipe XML (<collage version=2 format=15:10 orientation=landscape theme=picturepile shadows=1 captions=1 albumUID=...>, per-photo <node x/y/w/h/theta/scale><theme><src>[Z]\...portable-drive-token...<uid>>) -- each node <uid> ENDS IN that photo's originslow hash from 030, tying originslow = the content-hash photo identity.

**Captured:** 2026-06-14T08:50:46+00:00

```
baseline: 'clone-baseline-D (in collage editor, pre Create-Collage)' (2026-06-14T08:46:55+00:00)
32 file(s) differ (26 semantic, 6 blob/cache)

== CHANGED: db3/albumdata_category.pmp (88 -> 92 bytes)
   type uint32; rows 17 -> 18
   [17] (new) 1

== CHANGED: db3/albumdata_date.pmp (156 -> 164 bytes)
   type date(f64); rows 17 -> 18
   [16] 46187.062002314815 (2026-06-14 01:29:17) -> 46187.074837962966 (2026-06-14 01:47:46)
   [17] (new) 46187.07483796296 (2026-06-14 01:47:46)

== CHANGED: db3/albumdata_description.pmp (250 -> 251 bytes)
   type string; rows 17 -> 18
   [16] '16 results: No matches' -> '17 results: No matches'
   [17] (new) ''

== CHANGED: db3/albumdata_filename.pmp (196 -> 237 bytes)
   type string; rows 16 -> 18
   [16] (new) ''
   [17] (new) 'C:\\users\\matt\\Pictures\\Picasa\\Collages\\'

== CHANGED: db3/albumdata_hascollage.pmp (30 -> 38 bytes)
   type byte; rows 10 -> 18
   [10] (new) 0
   [11] (new) 0
   [12] (new) 0
   [13] (new) 0
   [14] (new) 0
   [15] (new) 0
   [16] (new) 0
   [17] (new) 0

== CHANGED: db3/albumdata_inisync.pmp (108 -> 164 bytes)
   type uint64; rows 11 -> 18
   [11] (new) 0x0000000000000000
   [12] (new) 0x0000000000000000
   [13] (new) 0x0000000000000000
   [14] (new) 0x0000000000000000
   [15] (new) 0x0000000000000000
   [16] (new) 0x0000000000000000
   [17] (new) 0x01dcfbda3d001de6

== CHANGED: db3/albumdata_location.pmp (37 -> 38 bytes)
   type string; rows 17 -> 18
   [17] (new) ''

== CHANGED: db3/albumdata_name.pmp (186 -> 195 bytes)
   type string; rows 17 -> 18
   [17] (new) 'Collages'

== CHANGED: db3/albumdata_token.pmp (201 -> 241 bytes)
   type string; rows 17 -> 18
   [17] (new) ']album:3ba3f7b5a7c4cefe0844aae918ee7e75'

== CHANGED: db3/albumdata_uid.pmp (357 -> 390 bytes)
   type string; rows 17 -> 18
   [17] (new) '3ba3f7b5a7c4cefe0844aae918ee7e75'

== CHANGED: db3/albums_index.db (164 -> 236 bytes)
   binary; 164 -> 236 bytes
   first difference at offset 0x8
     before[0x8:]: 0c00000000000000000000008205850000000000000000000000000000000000
     after [0x8:]: 1200000000000000000000008205850000000000000000000000000000000000

== CHANGED: db3/bigthumbs_index.db (368 -> 392 bytes)
   binary; 368 -> 392 bytes
   first difference at offset 0x8
     before[0x8:]: 1d000000000000000000000007e4ba6f0784286b0784286b0784286b0784286b
     after [0x8:]: 1f000000000000000000000007e4ba6f0784286b0784286b0784286b0784286b

== CHANGED: db3/imagedata_avgcolor.pmp (132 -> 144 bytes)
   type uint32; rows 28 -> 31
   [28] (new) 0
   [29] (new) 0
   [30] (new) 4286217083

== CHANGED: db3/imagedata_edit_height.pmp (132 -> 144 bytes)
   type uint32(7); rows 28 -> 31
   [28] (new) 0
   [29] (new) 0
   [30] (new) 0

== CHANGED: db3/imagedata_edit_width.pmp (132 -> 144 bytes)
   type uint32(7); rows 28 -> 31
   [28] (new) 0
   [29] (new) 0
   [30] (new) 0

== CHANGED: db3/imagedata_facerect.pmp (252 -> 268 bytes)
   type uint64; rows 29 -> 31
   [29] (new) 0x0000000000000000
   [30] (new) 0x0000000000000001

== CHANGED: db3/imagedata_facerectdata.pmp (72 -> 76 bytes)
   type string; rows 28 -> 31
   [28] (new) ''
   [29] (new) ''
   [30] (new) '1'

== ADDED: db3/imagedata_fileflags.pmp (- -> 144 bytes)
   type uint32; rows 0 -> 31
   [0] (new) 0
   [1] (new) 0
   [2] (new) 0
   [3] (new) 0
   [4] (new) 0
   [5] (new) 0
   [6] (new) 0
   [7] (new) 0
   [8] (new) 0
   [9] (new) 0
   [10] (new) 0
   [11] (new) 0
   [12] (new) 0
   [13] (new) 0
   [14] (new) 0
   [15] (new) 0
   [16] (new) 0
   [17] (new) 0
   [18] (new) 0
   [19] (new) 0
   [20] (new) 0
   [21] (new) 0
   [22] (new) 0
   [23] (new) 0
   [24] (new) 0
   [25] (new) 0
   [26] (new) 0
   [27] (new) 0
   [28] (new) 0
   [29] (new) 0
   [30] (new) 1

== CHANGED: db3/imagedata_filetype.pmp (136 -> 144 bytes)
   type uint32; rows 29 -> 31
   [29] (new) 1
   [30] (new) 2

== CHANGED: db3/imagedata_height.pmp (136 -> 144 bytes)
   type uint32; rows 29 -> 31
   [29] (new) 0
   [30] (new) 3413

== CHANGED: db3/imagedata_originfast.pmp (244 -> 268 bytes)
   type uint64; rows 28 -> 31
   [28] (new) 0x0000000000000000
   [29] (new) 0x0000000000000000
   [30] (new) 0xe9002a88d397983b

== CHANGED: db3/imagedata_width.pmp (136 -> 144 bytes)
   type uint32; rows 29 -> 31
   [29] (new) 0
   [30] (new) 5120

== CHANGED: db3/previews_index.db (356 -> 392 bytes)
   binary; 356 -> 392 bytes
   first difference at offset 0x8
     before[0x8:]: 1c000000000000000000000007e4ba6f0784286b0784286b0784286b0784286b
     after [0x8:]: 1f000000000000000000000007e4ba6f0784286b0784286b0784286b0784286b

== CHANGED: db3/thumbindex.db (1301 -> 1431 bytes)
   binary; 1301 -> 1431 bytes
   first difference at offset 0x4
     before[0x4:]: 1d0000005a3a5c7661725c686f6d655c6d6174745c6465765c66617578636173
     after [0x4:]: 1f0000005a3a5c7661725c686f6d655c6d6174745c6465765c66617578636173

== CHANGED: db3/thumbs2_index.db (368 -> 392 bytes)
   binary; 368 -> 392 bytes
   first difference at offset 0x8
     before[0x8:]: 1d00000000000000000000001aecd89c26b5d363fc27b44c76eab57b6316cb3a
     after [0x8:]: 1f00000000000000000000001aecd89c26b5d363fc27b44c76eab57b6316cb3a

== CHANGED: db3/thumbs_index.db (368 -> 392 bytes)
   binary; 368 -> 392 bytes
   first difference at offset 0x8
     before[0x8:]: 1d00000000000000000000001aecd89c26b5d363fc27b44c76eab57b6316cb3a
     after [0x8:]: 1f00000000000000000000001aecd89c26b5d363fc27b44c76eab57b6316cb3a

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/albums_0.db — binary; 210792 -> 234560 bytes; common prefix; 23768 bytes appended: 5a000000420000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000...
   changed: db3/bigthumbs_0.db — binary; 155537 -> 162815 bytes; common prefix; 7278 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/previews_0.db — binary; 424420 -> 447951 bytes; common prefix; 23531 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/thumbs2_0.db — binary; 34415 -> 36115 bytes; common prefix; 1700 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/thumbs_0.db — binary; 94424 -> 98382 bytes; common prefix; 3958 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/wordhash.dat — binary; 66992 -> 67100 bytes; first difference at offset 0x4
```

## Out-of-root artifacts (the collage output, captured by hand)

The collage rendered into the clone's `C:\users\matt\Pictures\Picasa\Collages\`
— outside the harness's watched roots, so preserved under
`after/collage-folder/Collages/`:

- **`.picasa.ini`** (42 B) — folder-identity stub: `[Picasa]` /
  `P2category=Projects (internal)`. Confirms `albumdata_category=1` ⇒ catdata
  category **"Projects (internal)"** (cf export 030 `Exported Pictures`,
  folder-desc 020 `Folders on Disk`).
- **`2015-03-15 Garden Project.cxf`** (2972 B) — a NEW artifact type: the editable
  collage **recipe** (XML). Header + a representative `<node>`:
  ```xml
  <?xml version="1.0" encoding="utf-8" ?>
  <collage version="2" format="15:10" orientation="landscape" theme="picturepile" shadows="1" captions="1" albumUID="b65d79ab3e5ebdb99c67f4fa671c6a2f">
   <albumTitle>2015-03-15 Garden Project</albumTitle>
   <albumDate>March 2015</albumDate>
   <background type="solid" color="FFFFFFFF"/>
   <spacing value="0.000000"/>
   <node x="0.161824" y="0.305894" w="0.246826" h="0.494135" theta="-0.071720" scale="337.000000">
    <theme>noborder</theme>
    <src>[Z]\var\home\matt\dev\fauxcasa\cache\synthetic-library\2015-03-15 Garden Project\photo01.jpg</src>
    <uid>fe751dfa20848685f24babcdec09dee2</uid>
   </node>
  ```
  - `version="2" format="15:10" orientation="landscape" theme="picturepile"
    shadows="1" captions="1" albumUID=...` mirrors the editor settings.
  - One `<node>` per source photo: layout (`x y w h theta scale`), `<theme>`,
    `<src>` using the portable **`[Z]`** drive token, and a `<uid>`.
  - **Each node `<uid>` ends in that photo's `originslow` hash** from fixture 030
    (e.g. photo01 `…f24babcdec09dee2` = `imagedata_originslow[3]`): `originslow`
    is the content-hash photo identity Picasa keys the recipe on.
- **`2015-03-15 Garden Project.jpg`** — the rendered collage, 5120×3413 (10×15 @
  ~341 dpi), 715 KB. Derived pixels; metadata in `_rendered-jpg.txt`, bytes not
  committed.

## Findings

- **A collage is a tracked "Project"; an export is not.** Both write into the same
  unwatched `Pictures\Picasa\…` tree, but the collage gets a full db3 footprint
  (a category-1 `Collages` albumdata row + two imagedata rows + an indexed image)
  while the export (030) left only an ini. Resolves the bead's "Projects
  albumdata row" question: **yes** — `category=1`, `name='Collages'`,
  `filename='C:\users\matt\Pictures\Picasa\Collages\'`.
- **The rendered collage is indexed like any photo**: imagedata row[30] gets
  `filetype=2`, real `width/height` (5120×3413), `avgcolor`, `originfast`, and the
  scanned-no-face `facerect=1`/`facerectdata='1'` sentinels — Picasa scanned its
  own output. A folder row[29] (`filetype=1`) precedes it for the `Collages` dir.
- **First sighting of `imagedata_fileflags.pmp`** in the corpus — the column
  materializes with `fileflags=1` on the collage image and `0` on every prior row.
- **The `.cxf` recipe + `.picasa.ini` are the durable project record**; the JPEG is
  re-renderable from the recipe ("Edit Collage" reopens it).
