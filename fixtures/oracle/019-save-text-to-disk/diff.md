# Fixture 019: save-text-to-disk

**Action:** File->Save the text edit from 018 (bake phase; pairs with 018 like 005 pairs with 004). UI: selected photo01.jpg in '2010-12-25 Winter Holiday', File->Save (dialog suppressed since 005). Sync phase, same architecture as 005: recipe MOVED from library ini (text=/textactive= deleted, backuphash 24332->23770) to NEW .picasaoriginals/.picasa.ini (moddate=a240a7b786fadc01 FILETIME + width=1200 height=1600 original dims + verbatim text= + textactive=1); original stashed BYTE-EXACT (sha256 8fe06860... verified) as .picasaoriginals/photo01.jpg; library JPEG rewritten 66033->55612 with text rendered in. Flush (+~3min, included): imagedata_text[12] cleared, textactive[12] 1->0, edited[12] 1->0, revertable[12] 0->1, backuphash mirror, NEW FIELD imagedata_originslow[12] 0 -> 0x80a4a73eb9f4a17f (uint64, set at bake - revert-tracking key for the stashed original?). width/height pmps UNTOUCHED (text doesn't resize; 005's crop did). thumbindex row decode confirmed: the photo's entry updates in place with new mtime FILETIME (== originals-ini moddate value) followed by u32 file size (0x000101f1=66033 -> 0xd93c=55612) - thumbindex rows carry (name, mtime, size, flags). albumdata_date[6] + folder inisync[8] ticks; wordhash same-size churn.

**Captured:** 2026-06-12T16:17:02+00:00

```
baseline: 'after 018-text-on-photo-unsaved' (2026-06-12T16:13:58+00:00)
14 file(s) differ (13 semantic, 1 blob/cache)

== CHANGED: db3/albumdata_date.pmp (116 -> 116 bytes)
   type date(f64); rows 12 -> 12
   [6] 46185.37708333333 (2026-06-12 09:03:00) -> 46185.38590277778 (2026-06-12 09:15:42)

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [8] 0x01dcfa862889f27c -> 0x01dcfa86b7a7d4c1

== CHANGED: db3/imagedata_backuphash.pmp (70 -> 70 bytes)
   type uint16; rows 25 -> 25
   [12] 24332 -> 23770

== CHANGED: db3/imagedata_edited.pmp (44 -> 44 bytes)
   type byte; rows 24 -> 24
   [12] 1 -> 0

== CHANGED: db3/imagedata_originslow.pmp (220 -> 220 bytes)
   type uint64; rows 25 -> 25
   [12] 0x0000000000000000 -> 0x80a4a73eb9f4a17f

== CHANGED: db3/imagedata_revertable.pmp (44 -> 44 bytes)
   type byte; rows 24 -> 24
   [12] 0 -> 1

== CHANGED: db3/imagedata_text.pmp (183 -> 44 bytes)
   type string; rows 24 -> 24
   [12] '1;132;11;oracle text;Times;0.342500,0.685625,0.033333,0.000000;v1,4294967295,4278190080,128.000000,1.000000,0.000000,1.000000,400,0,49152;;' -> ''

== CHANGED: db3/imagedata_textactive.pmp (44 -> 44 bytes)
   type byte; rows 24 -> 24
   [12] 1 -> 0

== CHANGED: db3/thumbindex.db (1453 -> 1453 bytes)
   binary; 1453 -> 1453 bytes
   first difference at offset 0x2c9
     before[0x2c9:]: 4f3b6c00b0f9dc01f10101000200000000010a00000070686f746f30322e6a70
     after [0x2c9:]: a240a7b786fadc013cd900000200000000010a00000070686f746f30322e6a70

== CHANGED: library/2010-12-25 Winter Holiday/.picasa.ini (286 -> 126 bytes)
   --- before/library/2010-12-25 Winter Holiday/.picasa.ini
   +++ after/library/2010-12-25 Winter Holiday/.picasa.ini
   @@ -6,5 +6,3 @@
    hidden=yes
    [photo01.jpg]
   -text=1;132;11;oracle text;Times;0.342500,0.685625,0.033333,0.000000;v1,4294967295,4278190080,128.000000,1.000000,0.000000,1.000000,400,0,49152;;
   -backuphash=24332
   -textactive=1
   +backuphash=23770

== ADDED: library/2010-12-25 Winter Holiday/.picasaoriginals/.picasa.ini (- -> 226 bytes)
   --- before/library/2010-12-25 Winter Holiday/.picasaoriginals/.picasa.ini
   +++ after/library/2010-12-25 Winter Holiday/.picasaoriginals/.picasa.ini
   @@ -0,0 +1,6 @@
   +[photo01.jpg]
   +moddate=a240a7b786fadc01
   +width=1200
   +height=1600
   +text=1;132;11;oracle text;Times;0.342500,0.685625,0.033333,0.000000;v1,4294967295,4278190080,128.000000,1.000000,0.000000,1.000000,400,0,49152;;
   +textactive=1

== ADDED: library/2010-12-25 Winter Holiday/.picasaoriginals/photo01.jpg (- -> 66033 bytes)
   image; - -> 66033 bytes (see fixture copies)

== CHANGED: library/2010-12-25 Winter Holiday/photo01.jpg (66033 -> 55612 bytes)
   image; 66033 -> 55612 bytes (see fixture copies)

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/wordhash.dat — binary; 67280 -> 67280 bytes; first difference at offset 0x100b4
```
