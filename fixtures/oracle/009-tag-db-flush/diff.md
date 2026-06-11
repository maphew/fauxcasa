# Fixture 009: tag-db-flush

**Action:** Deferred db flush of fixture 008's keyword (no UI action, ~6min later): imagedata_tags.pmp CREATED with field type 0x6 (csv-strings) - first type-6 sighting - row [2]='synthtag', only 3 rows (sparse field file, shorter than table); backuphash[2]=3563 mirrors ini; originslow[2] hash set; albumdata_inisync[7] (Garden Project row) FILETIME; thumbindex record for photo00.jpg updated (moddate FILETIME + size); wordhash indexed the tag. tags.txt STILL empty - keywords do not live there.

**Captured:** 2026-06-11T17:16:18+00:00

```
baseline: 'after 008-tag-photo' (2026-06-11T17:15:08+00:00)
7 file(s) differ (6 semantic, 1 blob/cache)

== CHANGED: db3/albumdata_date.pmp (108 -> 108 bytes)
   type date(f64); rows 11 -> 11
   [6] 46184.384791666664 (2026-06-11 09:14:06) -> 46184.4265625 (2026-06-11 10:14:15)

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [7] 0x01dcf9b818fe48de -> 0x01dcf9c5bbe24ec2

== CHANGED: db3/imagedata_backuphash.pmp (70 -> 70 bytes)
   type uint16; rows 25 -> 25
   [2] 0 -> 3563

== CHANGED: db3/imagedata_originslow.pmp (220 -> 220 bytes)
   type uint64; rows 25 -> 25
   [2] 0x0000000000000000 -> 0xc8dc4144d77bd593

== ADDED: db3/imagedata_tags.pmp (- -> 31 bytes)
   type string6; rows 0 -> 3
   [0] (new) ''
   [1] (new) ''
   [2] (new) 'synthtag'

== CHANGED: db3/thumbindex.db (1429 -> 1429 bytes)
   binary; 1429 -> 1429 bytes
   first difference at offset 0xe0
     before[0xe0:]: 4a5dd500b0f9dc016a3601000200000000010100000070686f746f30312e6a70
     after [0xe0:]: 804dacbac5f9dc01b83901000200000000010100000070686f746f30312e6a70

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/wordhash.dat — binary; 67240 -> 67264 bytes; first difference at offset 0x10
```
