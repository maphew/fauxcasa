# Fixture 036: edit-tilt-straighten-unsaved

**Action:** Straighten (Basic Fixes) on Garden Project/photo05.jpg (pristine, portrait 1200x1600). Agent-driven headless :2. UI: select photo05 -> Edit room -> Basic Fixes -> 'Straighten' (row1-col2, ~130,124) enters a SUB-MODE: dashed grid overlay on the image + a horizontal ANGLE SLIDER at the image bottom (~y577) with APPLY/CANCEL buttons -> set angle via a SINGLE CLICK on the slider track right-of-center (no drag) -> APPLY -> Back to Library (NO File->Save). Undo button -> 'Undo Straighten'; image visibly rotated (corner triangles exposed). SYNCHRONOUS (ini-only): new [photo05.jpg] filters=tilt=1,0.565155,0.000000; + backuphash=53094. KEY: 'tilt' pins the TWO-value param shape -> tilt=1,<angle>,<persist>: leading 1=enabled-flag, 0.565155=the normalized straighten angle (slider position, click landed right-of-center), 0.000000=the 2nd slot (backward-compat slider1 / persist, zero here). PROVES tilt is part of the filters= chain and is DISTINCT from the rotate= ini key + imagedata.rotate (fixtures 006/007, which are 90-degree library rotations stored as rotate=rotate(1), NOT in filters=). So: Straighten = fine free-angle rotation serialized as a filter op; Rotate = 90-degree step as its own key. Straighten is a 2-STAGE tool (sub-mode + Apply), unlike one-click enhance (034) or single-slider fill (035). EXPECTED LAZY FLUSH (~2-6min, not waited): imagedata_filters ''->'tilt=1,0.565155,0.000000;', imagedata_edited->1, backuphash mirror. SESSION NOISE (not the edit): albumdata Search-results reshuffle (4501 date sentinel tombstone+append).

**Captured:** 2026-06-20T23:06:49+00:00

```
baseline: 'after 035-edit-fill-light-slider-unsaved' (2026-06-20T23:02:11+00:00)
22 file(s) differ (18 semantic, 4 blob/cache)

== CHANGED: db3/albumdata_category.pmp (108 -> 112 bytes)
   type uint32; rows 22 -> 23
   [22] (new) 0

== CHANGED: db3/albumdata_date.pmp (196 -> 204 bytes)
   type date(f64); rows 22 -> 23
   [21] 46193.66357638889 (2026-06-20 15:55:33) -> 949998.0 (4501-01-01 00:00:00)
   [22] (new) 949998.0 (4501-01-01 00:00:00)

== CHANGED: db3/albumdata_description.pmp (334 -> 357 bytes)
   type string; rows 22 -> 23
   [22] (new) '16 results: No matches'

== CHANGED: db3/albumdata_filename.pmp (201 -> 203 bytes)
   type string; rows 21 -> 23
   [21] (new) ''
   [22] (new) ''

== CHANGED: db3/albumdata_inisync.pmp (172 -> 172 bytes)
   type uint64; rows 19 -> 19
   [7] 0x01dd010775f92aef -> 0x01dd0108bfce0ac5

== CHANGED: db3/albumdata_location.pmp (42 -> 43 bytes)
   type string; rows 22 -> 23
   [22] (new) ''

== CHANGED: db3/albumdata_music.pmp (42 -> 43 bytes)
   type string; rows 22 -> 23
   [22] (new) ''

== CHANGED: db3/albumdata_name.pmp (191 -> 178 bytes)
   type string; rows 22 -> 23
   [21] 'Search results' -> ''
   [22] (new) ''

== CHANGED: db3/albumdata_token.pmp (206 -> 200 bytes)
   type string; rows 22 -> 23
   [21] ']search' -> ''
   [22] (new) ''

== CHANGED: db3/albumdata_uid.pmp (329 -> 331 bytes)
   type string; rows 21 -> 23
   [21] (new) ''
   [22] (new) ''

== CHANGED: db3/bigthumbs_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x20
     before[0x20:]: 0784286b124cb00c0784286b0784286b0784286b000000001764286b0764286b
     after [0x20:]: 2954c898124cb00c0784286b0784286b0784286b000000001764286b0764286b

== CHANGED: db3/imagedata_backuphash.pmp (70 -> 70 bytes)
   type uint16; rows 25 -> 25
   [5] 0 -> 4142

== CHANGED: db3/imagedata_edited.pmp (44 -> 44 bytes)
   type byte; rows 24 -> 24
   [5] 0 -> 1

== CHANGED: db3/imagedata_filters.pmp (54 -> 70 bytes)
   type string; rows 24 -> 24
   [5] '' -> 'fill=1,0.186916;'

== CHANGED: db3/previews_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x20
     before[0x20:]: 0784286b124cb00c0784286b0784286b0784286b000000001764286b0764286b
     after [0x20:]: 2954c898124cb00c0784286b0784286b0784286b000000001764286b0764286b

== CHANGED: db3/thumbs2_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x98
     before[0x98:]: 770a0000108a00006b5b0000c11400007f18000000000000117300007b830000
     after [0x98:]: 188e0000108a00006b5b0000c11400007f18000000000000117300007b830000

== CHANGED: db3/thumbs_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x110
     before[0x110:]: 97090000b70a0000c7060000a7090000d409000000000000740a0000a0060000
     after [0x110:]: 85090000b70a0000c7060000a7090000d409000000000000740a0000a0060000

== CHANGED: library/2015-03-15 Garden Project/.picasa.ini (558 -> 626 bytes)
   --- before/library/2015-03-15 Garden Project/.picasa.ini
   +++ after/library/2015-03-15 Garden Project/.picasa.ini
   @@ -21,2 +21,5 @@
    filters=fill=1,0.186916;
    backuphash=4142
   +[photo05.jpg]
   +filters=tilt=1,0.565155,0.000000;
   +backuphash=53094

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/bigthumbs_0.db — binary; 166348 -> 171386 bytes; common prefix; 5038 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/previews_0.db — binary; 458656 -> 473614 bytes; first difference at offset 0x13dbd
   changed: db3/thumbs2_0.db — binary; 36376 -> 37301 bytes; common prefix; 925 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/wordhash.dat — binary; 66916 -> 66920 bytes; first difference at offset 0x1038c
```
