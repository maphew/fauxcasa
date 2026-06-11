# Fixture 007: rotate-db-flush

**Action:** Deferred db flush of fixture 006's rotate (no new UI action; flush landed ~4min later, observed after parallel-agent investigation). The db-side representation of an unsaved rotate: imagedata_rotate[11]='rotate(1)' (string, matches ini value), imagedata_edited[11]=2, imagedata_backuphash[11]=43584 (matches ini), albumdata_inisync[8] (Winter Holiday folder row) FILETIME updated, rotated thumbnails appended to caches. Row 11 = photo00.jpg in Winter Holiday. Confirms: ini is written synchronously on the action, pmp mirrors arrive on a lazy flush cycle.

**Captured:** 2026-06-11T17:07:23+00:00

```
baseline: 'after 006-rotate-photo' (2026-06-11T16:14:35+00:00)
14 file(s) differ (9 semantic, 5 blob/cache)

== CHANGED: db3/albumdata_date.pmp (108 -> 108 bytes)
   type date(f64); rows 11 -> 11
   [6] 46184.382060185184 (2026-06-11 09:10:10) -> 46184.384791666664 (2026-06-11 09:14:06)

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [8] 0x01dcf9b6790dd706 -> 0x01dcf9bd4fcb86a1

== CHANGED: db3/bigthumbs_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x38
     before[0x38:]: 0764286b0764286b0764286b0764286b0764286b0764286b0784286b0784286b
     after [0x38:]: 1764286b0764286b0764286b0764286b0764286b0764286b0784286b0784286b

== CHANGED: db3/imagedata_backuphash.pmp (70 -> 70 bytes)
   type uint16; rows 25 -> 25
   [11] 0 -> 43584

== CHANGED: db3/imagedata_edited.pmp (44 -> 44 bytes)
   type byte; rows 24 -> 24
   [11] 0 -> 2

== CHANGED: db3/imagedata_rotate.pmp (44 -> 53 bytes)
   type string; rows 24 -> 24
   [11] '' -> 'rotate(1)'

== CHANGED: db3/previews_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x38
     before[0x38:]: 0764286b0764286b0764286b0764286b0764286b0764286b0784286b0784286b
     after [0x38:]: 1764286b0764286b0764286b0764286b0764286b0764286b0784286b0784286b

== CHANGED: db3/thumbs2_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0xac
     before[0xac:]: 471c000017200000f4640000be260000632a00001562000004310000cb340000
     after [0xac:]: 1173000017200000f4640000be260000632a00001562000004310000cb340000

== CHANGED: db3/thumbs_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0xac
     before[0xac:]: 5e4800005d5200002f5900006206010073fc00002a760000187d000095f20000
     after [0xac:]: fd3601005d5200002f5900006206010073fc00002a760000187d000095f20000

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/bigthumbs_0.db — binary; 121951 -> 127652 bytes; common prefix; 5701 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/previews_0.db — binary; 377376 -> 394955 bytes; common prefix; 17579 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/thumbs2_0.db — binary; 29457 -> 30506 bytes; common prefix; 1049 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/thumbs_0.db — binary; 79613 -> 82289 bytes; common prefix; 2676 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/wordhash.dat — binary; 67236 -> 67240 bytes; first difference at offset 0x10464
```
