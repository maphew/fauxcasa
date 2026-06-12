# Fixture 018: text-on-photo-unsaved

**Action:** Text on photo, UNSAVED (edit-recipe phase; File->Save bake is next fixture). UI: 2x-clicked photo01.jpg in '2010-12-25 Winter Holiday' -> Text tool (ABC tab) -> clicked on image, typed 'oracle text' (11 chars, no Wine mangling this time) -> Apply -> Back to Library, NO File->Save. Sync phase: ini [photo01.jpg] gets text=1;132;11;oracle text;Times;0.342500,0.685625,0.033333,0.000000;v1,4294967295,4278190080,128.000000,1.000000,0.000000,1.000000,400,0,49152;; + backuphash=24332 + textactive=1. Format reading: count;payload-len;string-len;string;font;x,y,size,rot fractions;v1,fill-ARGB(0xFFFFFFFF white),outline-ARGB(0xFF000000 black),...,weight 400,...;; - and NO filters= entry (unlike crop-unsaved which wrote filters=crop64; text is its own key, not in the filter chain). Flush (+~4min, included): imagedata_text.pmp[12] = ini string VERBATIM, imagedata_textactive[12]=1 (byte), imagedata_edited[12]=1 (byte; rotate had set edited=2), backuphash mirror, folder inisync[8] tick, thumbs/bigthumbs/previews re-rendered with text overlay (entries appended), wordhash +4 ('oracle'/'text' indexed?). NO albumdata_date[6] activity tick this time, no search-results change. JPEG untouched.

**Captured:** 2026-06-12T16:13:58+00:00

```
baseline: 'after 017-hide-photo' (2026-06-12T16:08:56+00:00)
15 file(s) differ (10 semantic, 5 blob/cache)

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [8] 0x01dcfa84f2c020d8 -> 0x01dcfa862889f27c

== CHANGED: db3/bigthumbs_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x3c
     before[0x3c:]: 0764286b0764286b0764286b0764286b0764286b0784286b0784286b00000000
     after [0x3c:]: fa0591080764286b0764286b0764286b0764286b0784286b0784286b00000000

== CHANGED: db3/imagedata_backuphash.pmp (70 -> 70 bytes)
   type uint16; rows 25 -> 25
   [12] 0 -> 24332

== CHANGED: db3/imagedata_edited.pmp (44 -> 44 bytes)
   type byte; rows 24 -> 24
   [12] 0 -> 1

== CHANGED: db3/imagedata_text.pmp (44 -> 183 bytes)
   type string; rows 24 -> 24
   [12] '' -> '1;132;11;oracle text;Times;0.342500,0.685625,0.033333,0.000000;v1,4294967295,4278190080,128.000000,1.000000,0.000000,1.000000,400,0,49152;;'

== CHANGED: db3/imagedata_textactive.pmp (44 -> 44 bytes)
   type byte; rows 24 -> 24
   [12] 0 -> 1

== CHANGED: db3/previews_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x3c
     before[0x3c:]: 0764286b0764286b0764286b0764286b0764286b0784286b0784286b00000000
     after [0x3c:]: fa0591080764286b0764286b0764286b0764286b0784286b0784286b00000000

== CHANGED: db3/thumbs2_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0xb4
     before[0xb4:]: 17200000f4640000be260000632a00001562000004310000cb34000000000000
     after [0xb4:]: 7b830000f4640000be260000632a00001562000004310000cb34000000000000

== CHANGED: db3/thumbs_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0xb4
     before[0xb4:]: 5d5200002f5900006206010073fc00002a760000187d000095f2000000000000
     after [0xb4:]: a46901002f5900006206010073fc00002a760000187d000095f2000000000000

== CHANGED: library/2010-12-25 Winter Holiday/.picasa.ini (93 -> 286 bytes)
   --- before/library/2010-12-25 Winter Holiday/.picasa.ini
   +++ after/library/2010-12-25 Winter Holiday/.picasa.ini
   @@ -5,2 +5,6 @@
    [photo06.jpg]
    hidden=yes
   +[photo01.jpg]
   +text=1;132;11;oracle text;Times;0.342500,0.685625,0.033333,0.000000;v1,4294967295,4278190080,128.000000,1.000000,0.000000,1.000000,400,0,49152;;
   +backuphash=24332
   +textactive=1

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/bigthumbs_0.db — binary; 147229 -> 151381 bytes; common prefix; 4152 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/previews_0.db — binary; 410319 -> 424420 bytes; common prefix; 14101 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/thumbs2_0.db — binary; 33659 -> 34415 bytes; common prefix; 756 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/thumbs_0.db — binary; 92580 -> 94424 bytes; common prefix; 1844 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/wordhash.dat — binary; 67276 -> 67280 bytes; first difference at offset 0x10470
```
