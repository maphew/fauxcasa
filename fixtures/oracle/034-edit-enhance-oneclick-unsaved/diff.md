# Fixture 034: edit-enhance-oneclick-unsaved

**Action:** I'm Feeling Lucky (auto-enhance one-click) on Garden Project/photo04.jpg (pristine, no prior ini section). Agent-driven headless :2. UI: double-click photo04 -> Edit room -> Basic Fixes tab -> 'I'm Feeling Lucky' (button is row2-col1 of the fixes grid, ~y193 in the 1016x734 window — NOT the tab row at y73) -> 'Back to Library' (NO File->Save). Undo button changed to 'Undo I'm Feeling Lucky' confirming the edit registered; image visibly deepened + histogram shifted, so auto-enhance is NOT a no-op even on a synthetic linear gradient. SYNCHRONOUS (ini-only, mirrors crop-unsaved 004): new [photo04.jpg] section gains filters=enhance=1; + backuphash=39773. KEY: 'enhance' is the FIRST non-crop filters= token captured in the corpus (001-033 only ever had crop64) — it is the bare '<op>=1;' flag form (no comma params), vs crop64's parametrized 'crop64=1,<hex>;'. enhance=1 = the I'm-Feeling-Lucky auto-contrast+auto-color one-click recipe marker (validates parse_filters in scripts/picasa_db.py). EXPECTED LAZY FLUSH (~2-6 min, NOT waited/captured here — synchronous fixture): imagedata_filters ''->'enhance=1;', imagedata_edited->1, imagedata_backuphash mirror. SESSION NOISE in this diff (NOT the edit): the recurring 'Search results' albumdata row reshuffle — row[19] 'Search results'/']search'/uid tombstoned to the 4501-01-01 date sentinel, re-appended as blank row[20] (description '16 results: No matches'); albumdata_* grew 20->21 rows. Documented wine-oracle.md session-activity gotcha.

**Captured:** 2026-06-20T22:54:05+00:00

```
baseline: 'manual' (2026-06-20T22:40:47+00:00)
23 file(s) differ (18 semantic, 5 blob/cache)

== CHANGED: db3/albumdata_category.pmp (100 -> 104 bytes)
   type uint32; rows 20 -> 21
   [20] (new) 0

== CHANGED: db3/albumdata_date.pmp (180 -> 188 bytes)
   type date(f64); rows 20 -> 21
   [19] 46193.64960648148 (2026-06-20 15:35:26) -> 949998.0 (4501-01-01 00:00:00)
   [20] (new) 949998.0 (4501-01-01 00:00:00)

== CHANGED: db3/albumdata_description.pmp (288 -> 311 bytes)
   type string; rows 20 -> 21
   [20] (new) '16 results: No matches'

== CHANGED: db3/albumdata_filename.pmp (199 -> 201 bytes)
   type string; rows 19 -> 21
   [19] (new) ''
   [20] (new) ''

== CHANGED: db3/albumdata_inisync.pmp (172 -> 172 bytes)
   type uint64; rows 19 -> 19
   [7] 0x01dcfd4ae3ccf406 -> 0x01dd010775f92aef

== CHANGED: db3/albumdata_location.pmp (40 -> 41 bytes)
   type string; rows 20 -> 21
   [20] (new) ''

== CHANGED: db3/albumdata_music.pmp (40 -> 41 bytes)
   type string; rows 20 -> 21
   [20] (new) ''

== CHANGED: db3/albumdata_name.pmp (189 -> 176 bytes)
   type string; rows 20 -> 21
   [19] 'Search results' -> ''
   [20] (new) ''

== CHANGED: db3/albumdata_token.pmp (204 -> 198 bytes)
   type string; rows 20 -> 21
   [19] ']search' -> ''
   [20] (new) ''

== CHANGED: db3/albumdata_uid.pmp (360 -> 329 bytes)
   type string; rows 20 -> 21
   [19] 'ebc015ff0824b9d171b4b36d9ad5604d' -> ''
   [20] (new) ''

== CHANGED: db3/bigthumbs_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x24
     before[0x24:]: 0784286b0784286b0784286b0784286b000000001764286b0764286b0724fdd0
     after [0x24:]: 124cb00c0784286b0784286b0784286b000000001764286b0764286b0724fdd0

== CHANGED: db3/imagedata_backuphash.pmp (70 -> 70 bytes)
   type uint16; rows 25 -> 25
   [6] 0 -> 39773

== CHANGED: db3/imagedata_edited.pmp (44 -> 44 bytes)
   type byte; rows 24 -> 24
   [6] 0 -> 1

== CHANGED: db3/imagedata_filters.pmp (44 -> 54 bytes)
   type string; rows 24 -> 24
   [6] '' -> 'enhance=1;'

== CHANGED: db3/previews_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x24
     before[0x24:]: 0784286b0784286b0784286b0784286b000000001764286b0764286b0724fdd0
     after [0x24:]: 124cb00c0784286b0784286b0784286b000000001764286b0764286b0724fdd0

== CHANGED: db3/thumbs2_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x9c
     before[0x9c:]: 4f5e00006b5b0000c11400007f18000000000000117300007b830000f4640000
     after [0x9c:]: 108a00006b5b0000c11400007f18000000000000117300007b830000f4640000

== CHANGED: db3/thumbs_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x9c
     before[0x9c:]: 11df0000002e0000043500003dd5000000000000fd360100a46901002f590000
     after [0x9c:]: 767a0100002e0000043500003dd5000000000000fd360100a46901002f590000

== CHANGED: library/2015-03-15 Garden Project/.picasa.ini (447 -> 500 bytes)
   --- before/library/2015-03-15 Garden Project/.picasa.ini
   +++ after/library/2015-03-15 Garden Project/.picasa.ini
   @@ -15,2 +15,5 @@
    [.album:0925e37243e77ab02c6835534d79ce09]
    token=0925e37243e77ab02c6835534d79ce09
   +[photo04.jpg]
   +filters=enhance=1;
   +backuphash=39773

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/bigthumbs_0.db — binary; 160493 -> 166348 bytes; common prefix; 5855 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/previews_0.db — binary; 439513 -> 458656 bytes; common prefix; 19143 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/thumbs2_0.db — binary; 35344 -> 36376 bytes; common prefix; 1032 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/thumbs_0.db — binary; 96886 -> 99629 bytes; common prefix; 2743 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/wordhash.dat — binary; 66912 -> 66916 bytes; first difference at offset 0x1038c
```

**CORRECTION (2026-08-12 salvage review, fauxcasa-nu9): the 'EXPECTED LAZY FLUSH ... NOT waited/captured here' claim is contradicted by this fixture's own diff — the db3 flush of THIS edit WAS captured: imagedata_filters[6] ''->'enhance=1;', imagedata_edited[6] 0->1, imagedata_backuphash[6] mirror. So 034 additionally pins that the lazy flush can land within ~14min of the baseline (22:40->22:54), and this snapshot is ini+db3, not ini-only.**
