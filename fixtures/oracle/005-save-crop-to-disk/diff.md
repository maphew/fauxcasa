# Fixture 005: save-crop-to-disk

**Action:** File->Save after the unsaved crop of photo03.jpg ('do not ask again' checked in save dialog - future saves are silent). Bake architecture revealed: main ini drops crop=/filters= lines (keeps new backuphash=39767); .picasaoriginals/.picasa.ini created with the undo recipe (filters=, crop=, original width=800/height=600, moddate=FILETIME hex - same bytes appear in thumbindex.db record = file moddate); original JPEG stashed byte-exact in .picasaoriginals/; main JPEG rewritten 26634->17220 (574x259). Save also flushed the whole edit-state pmp family (crop64, edited, filters, flipped, redo, revertable, rotate, text, textactive, colorspace, onlinechecksum): for photo03 row 23 revertable=1, width/height updated, crop64=0/filters='' - db edit state is CLEARED after baking; the recipe lives only in .picasaoriginals ini.

**Captured:** 2026-06-11T16:11:05+00:00

```
baseline: 'after 004-crop-unsaved' (2026-06-11T16:07:43+00:00)
31 file(s) differ (26 semantic, 5 blob/cache)

== CHANGED: db3/albumdata_date.pmp (108 -> 108 bytes)
   type date(f64); rows 11 -> 11
   [6] 46184.358773148146 (2026-06-11 08:36:38) -> 46184.38159722222 (2026-06-11 09:09:30)

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [9] 0x01dcf9b7404a6929 -> 0x01dcf9bcaf21305a

== CHANGED: db3/bigthumbs_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x6a
     before[0x6a:]: 286b0744b56c0764286b0764286b0764286b1c00000000000000000000000400
     after [0x6a:]: d16d0744b56c0764286b0764286b0764286b1c00000000000000000000000400

== CHANGED: db3/imagedata_backuphash.pmp (70 -> 70 bytes)
   type uint16; rows 25 -> 25
   [23] 0 -> 39767

== ADDED: db3/imagedata_colorspace.pmp (- -> 44 bytes)
   type byte; rows 0 -> 24
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

== ADDED: db3/imagedata_crop64.pmp (- -> 212 bytes)
   type uint64; rows 0 -> 24
   [0] (new) 0x0000000000000000
   [1] (new) 0x0000000000000000
   [2] (new) 0x0000000000000000
   [3] (new) 0x0000000000000000
   [4] (new) 0x0000000000000000
   [5] (new) 0x0000000000000000
   [6] (new) 0x0000000000000000
   [7] (new) 0x0000000000000000
   [8] (new) 0x0000000000000000
   [9] (new) 0x0000000000000000
   [10] (new) 0x0000000000000000
   [11] (new) 0x0000000000000000
   [12] (new) 0x0000000000000000
   [13] (new) 0x0000000000000000
   [14] (new) 0x0000000000000000
   [15] (new) 0x0000000000000000
   [16] (new) 0x0000000000000000
   [17] (new) 0x0000000000000000
   [18] (new) 0x0000000000000000
   [19] (new) 0x0000000000000000
   [20] (new) 0x0000000000000000
   [21] (new) 0x0000000000000000
   [22] (new) 0x0000000000000000
   [23] (new) 0x0000000000000000

== ADDED: db3/imagedata_edited.pmp (- -> 44 bytes)
   type byte; rows 0 -> 24
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

== ADDED: db3/imagedata_filters.pmp (- -> 44 bytes)
   type string; rows 0 -> 24
   [0] (new) ''
   [1] (new) ''
   [2] (new) ''
   [3] (new) ''
   [4] (new) ''
   [5] (new) ''
   [6] (new) ''
   [7] (new) ''
   [8] (new) ''
   [9] (new) ''
   [10] (new) ''
   [11] (new) ''
   [12] (new) ''
   [13] (new) ''
   [14] (new) ''
   [15] (new) ''
   [16] (new) ''
   [17] (new) ''
   [18] (new) ''
   [19] (new) ''
   [20] (new) ''
   [21] (new) ''
   [22] (new) ''
   [23] (new) ''

== ADDED: db3/imagedata_flipped.pmp (- -> 44 bytes)
   type string; rows 0 -> 24
   [0] (new) ''
   [1] (new) ''
   [2] (new) ''
   [3] (new) ''
   [4] (new) ''
   [5] (new) ''
   [6] (new) ''
   [7] (new) ''
   [8] (new) ''
   [9] (new) ''
   [10] (new) ''
   [11] (new) ''
   [12] (new) ''
   [13] (new) ''
   [14] (new) ''
   [15] (new) ''
   [16] (new) ''
   [17] (new) ''
   [18] (new) ''
   [19] (new) ''
   [20] (new) ''
   [21] (new) ''
   [22] (new) ''
   [23] (new) ''

== CHANGED: db3/imagedata_height.pmp (132 -> 132 bytes)
   type uint32; rows 28 -> 28
   [23] 600 -> 259

== ADDED: db3/imagedata_onlinechecksum.pmp (- -> 116 bytes)
   type uint32; rows 0 -> 24
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

== CHANGED: db3/imagedata_originslow.pmp (220 -> 220 bytes)
   type uint64; rows 25 -> 25
   [23] 0x0000000000000000 -> 0x9c453041ea059c81

== ADDED: db3/imagedata_redo.pmp (- -> 44 bytes)
   type string; rows 0 -> 24
   [0] (new) ''
   [1] (new) ''
   [2] (new) ''
   [3] (new) ''
   [4] (new) ''
   [5] (new) ''
   [6] (new) ''
   [7] (new) ''
   [8] (new) ''
   [9] (new) ''
   [10] (new) ''
   [11] (new) ''
   [12] (new) ''
   [13] (new) ''
   [14] (new) ''
   [15] (new) ''
   [16] (new) ''
   [17] (new) ''
   [18] (new) ''
   [19] (new) ''
   [20] (new) ''
   [21] (new) ''
   [22] (new) ''
   [23] (new) ''

== ADDED: db3/imagedata_revertable.pmp (- -> 44 bytes)
   type byte; rows 0 -> 24
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
   [23] (new) 1

== ADDED: db3/imagedata_rotate.pmp (- -> 44 bytes)
   type string; rows 0 -> 24
   [0] (new) ''
   [1] (new) ''
   [2] (new) ''
   [3] (new) ''
   [4] (new) ''
   [5] (new) ''
   [6] (new) ''
   [7] (new) ''
   [8] (new) ''
   [9] (new) ''
   [10] (new) ''
   [11] (new) ''
   [12] (new) ''
   [13] (new) ''
   [14] (new) ''
   [15] (new) ''
   [16] (new) ''
   [17] (new) ''
   [18] (new) ''
   [19] (new) ''
   [20] (new) ''
   [21] (new) ''
   [22] (new) ''
   [23] (new) ''

== ADDED: db3/imagedata_text.pmp (- -> 44 bytes)
   type string; rows 0 -> 24
   [0] (new) ''
   [1] (new) ''
   [2] (new) ''
   [3] (new) ''
   [4] (new) ''
   [5] (new) ''
   [6] (new) ''
   [7] (new) ''
   [8] (new) ''
   [9] (new) ''
   [10] (new) ''
   [11] (new) ''
   [12] (new) ''
   [13] (new) ''
   [14] (new) ''
   [15] (new) ''
   [16] (new) ''
   [17] (new) ''
   [18] (new) ''
   [19] (new) ''
   [20] (new) ''
   [21] (new) ''
   [22] (new) ''
   [23] (new) ''

== ADDED: db3/imagedata_textactive.pmp (- -> 44 bytes)
   type byte; rows 0 -> 24
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

== CHANGED: db3/imagedata_width.pmp (132 -> 132 bytes)
   type uint32; rows 28 -> 28
   [23] 800 -> 574

== CHANGED: db3/previews_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x6a
     before[0x6a:]: 286b0744b56c0764286b0764286b0764286b1c00000000000000000000000400
     after [0x6a:]: d16d0744b56c0764286b0764286b0764286b1c00000000000000000000000400

== CHANGED: db3/thumbindex.db (1429 -> 1429 bytes)
   binary; 1429 -> 1429 bytes
   first difference at offset 0x4d7
     before[0x4d7:]: e2ad1000b0f9dc010a6800000200000000011300000070686f746f30342e6a70
     after [0x4d7:]: 5ec020afbcf9dc01444300000200000000011300000070686f746f30342e6a70

== CHANGED: db3/thumbs2_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x68
     before[0x68:]: 7e8df446038efe51360335d57c46e06e237c40af1c0000000000000000000000
     after [0x68:]: 981863e6038efe51360335d57c46e06e237c40af1c0000000000000000000000

== CHANGED: db3/thumbs_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x68
     before[0x68:]: 7e8df446038efe51360335d57c46e06e237c40af1c0000000000000000000000
     after [0x68:]: 981863e6038efe51360335d57c46e06e237c40af1c0000000000000000000000

== CHANGED: library/2009-07-04 Beach Day/.picasa.ini (130 -> 65 bytes)
   --- before/library/2009-07-04 Beach Day/.picasa.ini
   +++ after/library/2009-07-04 Beach Day/.picasa.ini
   @@ -2,5 +2,3 @@
    backuphash=3224
    [photo03.jpg]
   -backuphash=64082
   -crop=rect64(dc3369dc570a51e)
   -filters=crop64=1,dc3369dc570a51e;
   +backuphash=39767

== ADDED: library/2009-07-04 Beach Day/.picasaoriginals/.picasa.ini (- -> 143 bytes)
   --- before/library/2009-07-04 Beach Day/.picasaoriginals/.picasa.ini
   +++ after/library/2009-07-04 Beach Day/.picasaoriginals/.picasa.ini
   @@ -0,0 +1,7 @@
   +[photo03.jpg]
   +filters=crop64=1,dc3369dc570a51e;
   +crop=rect64(dc3369dc570a51e)
   +moddate=5ec020afbcf9dc01
   +width=800
   +height=600
   +textactive=0

== ADDED: library/2009-07-04 Beach Day/.picasaoriginals/photo03.jpg (- -> 26634 bytes)
   image; - -> 26634 bytes (see fixture copies)

== CHANGED: library/2009-07-04 Beach Day/photo03.jpg (26634 -> 17220 bytes)
   image; 26634 -> 17220 bytes (see fixture copies)

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/bigthumbs_0.db — binary; 116637 -> 121951 bytes; common prefix; 5314 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/previews_0.db — binary; 360844 -> 377376 bytes; common prefix; 16532 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/thumbs2_0.db — binary; 28483 -> 29457 bytes; common prefix; 974 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/thumbs_0.db — binary; 76755 -> 79613 bytes; common prefix; 2858 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/wordhash.dat — binary; 67216 -> 67236 bytes; first difference at offset 0x10
```
