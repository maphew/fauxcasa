# Fixture 013: face-tag-manual

**Action:** Manual face tag: View->People, typed 'Synthetic Person' in right panel, 'add manually', drew box on photo04.jpg in '2009-07-04 Beach Day' (image is synthetic gradient - manual tagging works without a detectable face). Synchronous phase: person name written into JPEG XMP as dc:subject keyword (+ ModifyDate bump, +102 bytes); ini backuphash 3224->14043. NO face geometry in the JPEG (no mwg-rs RegionInfo); rect/template/contacts expected db-side on lazy flush. Thumbnail caches churned.

**Captured:** 2026-06-11T19:03:45+00:00

```
baseline: 'after 012-rename-album-db-flush' (2026-06-11T18:34:35+00:00)
16 file(s) differ (11 semantic, 5 blob/cache)

== CHANGED: db3/albumdata_date.pmp (108 -> 108 bytes)
   type date(f64); rows 11 -> 11
   [6] 46184.480520833335 (2026-06-11 11:31:57) -> 46184.502592592595 (2026-06-11 12:03:44)

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [9] 0x01dcf9bcaf21305a -> 0x01dcf9d4a2e50f82

== CHANGED: db3/bigthumbs_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x15
     before[0x15:]: 84286b0784286b0784286b0784286b0784286b0784286b0784286b0784286b00
     after [0x15:]: e4ba6f0784286b0784286b0784286b0784286b0784286b0784286b0784286b00

== CHANGED: db3/imagedata_backuphash.pmp (70 -> 70 bytes)
   type uint16; rows 25 -> 25
   [24] 3224 -> 14043

== CHANGED: db3/imagedata_tags.pmp (31 -> 69 bytes)
   type string6; rows 3 -> 25
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
   [24] (new) 'Synthetic Person'

== CHANGED: db3/previews_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x15
     before[0x15:]: 84286b0784286b0784286b0784286b0784286b0784286b0784286b0784286b00
     after [0x15:]: e4ba6f0784286b0784286b0784286b0784286b0784286b0784286b0784286b00

== CHANGED: db3/thumbindex.db (1429 -> 1429 bytes)
   binary; 1429 -> 1429 bytes
   first difference at offset 0x501
     before[0x501:]: 0009ab3eb7f9dc01ef3001000200000000011300000070686f746f30352e6a70
     after [0x501:]: 80cd5ea1d4f9dc01553101000200000000011300000070686f746f30352e6a70

== CHANGED: db3/thumbs2_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x14
     before[0x14:]: 55bb638126b5d363fc27b44c76eab57b6316cb3a8f8e9f1a9f802ddd01f29dfe
     after [0x14:]: 1aecd89c26b5d363fc27b44c76eab57b6316cb3a8f8e9f1a9f802ddd01f29dfe

== CHANGED: db3/thumbs_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x14
     before[0x14:]: 55bb638126b5d363fc27b44c76eab57b6316cb3a8f8e9f1a9f802ddd01f29dfe
     after [0x14:]: 1aecd89c26b5d363fc27b44c76eab57b6316cb3a8f8e9f1a9f802ddd01f29dfe

== CHANGED: library/2009-07-04 Beach Day/.picasa.ini (65 -> 66 bytes)
   --- before/library/2009-07-04 Beach Day/.picasa.ini
   +++ after/library/2009-07-04 Beach Day/.picasa.ini
   @@ -1,4 +1,4 @@
    [photo04.jpg]
   -backuphash=3224
   +backuphash=14043
    [photo03.jpg]
    backuphash=39767

== CHANGED: library/2009-07-04 Beach Day/photo04.jpg (78063 -> 78165 bytes)
   image; 78063 -> 78165 bytes (see fixture copies)

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/bigthumbs_0.db — binary; 127652 -> 138121 bytes; common prefix; 10469 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/previews_0.db — binary; 394955 -> 410319 bytes; first difference at offset 0xcb
   changed: db3/thumbs2_0.db — binary; 30506 -> 31446 bytes; first difference at offset 0x54aa
   changed: db3/thumbs_0.db — binary; 82289 -> 87277 bytes; common prefix; 4988 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/wordhash.dat — binary; 67256 -> 67280 bytes; first difference at offset 0x10
```
