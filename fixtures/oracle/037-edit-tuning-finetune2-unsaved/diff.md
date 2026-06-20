# Fixture 037: edit-tuning-finetune2-unsaved

**Action:** Tuning tab (entire tab = one filter op) on Garden Project/photo06.jpg (pristine). Agent-driven headless :2. UI: select photo06 -> Edit room -> 2nd tab 'Tuning' (~82,73) -> set Fill Light / Highlights / Shadows / Color Temperature sliders to 4 DISTINCT non-zero values via single track-clicks; LEFT the Neutral Color Picker UNSET (black swatch) -> Back to Library (NO File->Save). Undo -> 'Undo Tuning'. SYNCHRONOUS (ini-only): new [photo06.jpg] filters=finetune2=1,0.280702,0.261053,0.345263,00000000,0.555556; + backuphash=49282. KEY -- this is the highest single-token information density in the corpus: the WHOLE Tuning tab serializes as ONE 'finetune2' op with 6 comma fields: [1]=enabled-flag, [2]=Fill Light 0.280702, [3]=Highlights 0.261053, [4]=Shadows 0.345263, [5]=Neutral-Color-Picker ARGB = 00000000 (8 HEX digits, =unset since no color picked), [6]=Color Temperature 0.555556. PINS the 5-float layout AND the interleaved ARGB color slot sitting BETWEEN shadows and colortemp (so a parser must NOT treat all fields as floats -- field [5] is hex). Slider anchoring: Fill/Highlights/Shadows are LEFT-anchored (0..1, thumb starts left); Color Temperature is CENTER-anchored (0.5=neutral, 0.555556=my slightly-warm right-of-center click). NOTE: the Tuning Fill Light is bundled INTO finetune2 -- it is NOT the standalone Basic-Fixes 'fill=' op of fixture 035. Both Fill Light controls exist (Basic Fixes -> fill=1,<f>; Tuning -> finetune2 slot [2]). EXPECTED LAZY FLUSH (~2-6min, not waited): imagedata_filters mirror, imagedata_edited->1, backuphash mirror. SESSION NOISE: albumdata Search-results reshuffle.

**Captured:** 2026-06-20T23:08:55+00:00

```
baseline: 'after 036-edit-tilt-straighten-unsaved' (2026-06-20T23:06:49+00:00)
21 file(s) differ (16 semantic, 5 blob/cache)

== CHANGED: db3/albumdata_category.pmp (112 -> 116 bytes)
   type uint32; rows 23 -> 24
   [23] (new) 0

== CHANGED: db3/albumdata_date.pmp (204 -> 212 bytes)
   type date(f64); rows 23 -> 24
   [23] (new) 46193.67240740741 (2026-06-20 16:08:16)

== CHANGED: db3/albumdata_description.pmp (357 -> 380 bytes)
   type string; rows 23 -> 24
   [23] (new) '16 results: No matches'

== CHANGED: db3/albumdata_inisync.pmp (172 -> 172 bytes)
   type uint64; rows 19 -> 19
   [7] 0x01dd0108bfce0ac5 -> 0x01dd0109ae722eb6

== CHANGED: db3/albumdata_location.pmp (43 -> 44 bytes)
   type string; rows 23 -> 24
   [23] (new) ''

== CHANGED: db3/albumdata_music.pmp (43 -> 44 bytes)
   type string; rows 23 -> 24
   [23] (new) ''

== CHANGED: db3/albumdata_name.pmp (178 -> 193 bytes)
   type string; rows 23 -> 24
   [23] (new) 'Search results'

== CHANGED: db3/albumdata_token.pmp (200 -> 208 bytes)
   type string; rows 23 -> 24
   [23] (new) ']search'

== CHANGED: db3/bigthumbs_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x28
     before[0x28:]: 0784286b0784286b0784286b000000001764286b0764286b0724fdd00724fdd0
     after [0x28:]: dd9a489bfb2c3def0784286b000000001764286b0764286b0724fdd00724fdd0

== CHANGED: db3/imagedata_backuphash.pmp (70 -> 70 bytes)
   type uint16; rows 25 -> 25
   [7] 0 -> 53094
   [8] 0 -> 49282

== CHANGED: db3/imagedata_edited.pmp (44 -> 44 bytes)
   type byte; rows 24 -> 24
   [7] 0 -> 1
   [8] 0 -> 1

== CHANGED: db3/imagedata_filters.pmp (70 -> 152 bytes)
   type string; rows 24 -> 24
   [7] '' -> 'tilt=1,0.565155,0.000000;'
   [8] '' -> 'finetune2=1,0.280702,0.261053,0.345263,00000000,0.555556;'

== CHANGED: db3/previews_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x28
     before[0x28:]: 0784286b0784286b0784286b000000001764286b0764286b0724fdd00724fdd0
     after [0x28:]: dd9a489bfb2c3def0784286b000000001764286b0764286b0724fdd00724fdd0

== CHANGED: db3/thumbs2_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0xa0
     before[0xa0:]: 6b5b0000c11400007f18000000000000117300007b830000f46400006f860000
     after [0xa0:]: b5910000359500007f18000000000000117300007b830000f46400006f860000

== CHANGED: db3/thumbs_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0xa0
     before[0xa0:]: 002e0000043500003dd5000000000000fd360100a46901002f590000d8700100
     after [0xa0:]: 2d850100d58d01003dd5000000000000fd360100a46901002f590000d8700100

== CHANGED: library/2015-03-15 Garden Project/.picasa.ini (626 -> 726 bytes)
   --- before/library/2015-03-15 Garden Project/.picasa.ini
   +++ after/library/2015-03-15 Garden Project/.picasa.ini
   @@ -24,2 +24,5 @@
    filters=tilt=1,0.565155,0.000000;
    backuphash=53094
   +[photo06.jpg]
   +filters=finetune2=1,0.280702,0.261053,0.345263,00000000,0.555556;
   +backuphash=49282

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/bigthumbs_0.db — binary; 171386 -> 182018 bytes; common prefix; 10632 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/previews_0.db — binary; 473614 -> 495057 bytes; common prefix; 21443 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/thumbs2_0.db — binary; 37301 -> 39235 bytes; common prefix; 1934 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/thumbs_0.db — binary; 99629 -> 104791 bytes; common prefix; 5162 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/wordhash.dat — binary; 66920 -> 66928 bytes; first difference at offset 0x1038c
```
