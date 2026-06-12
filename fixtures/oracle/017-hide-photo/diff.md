# Fixture 017: hide-photo

**Action:** Hide photo. UI: right-click photo06.jpg in '2010-12-25 Winter Holiday' grid -> Hide. Sync phase: ini-only - new [photo06.jpg] section with hidden=yes (note: no backuphash line added for this photo). Flush (+~5min, included): NO imagedata pmp mirror - hidden state is INI-ONLY, one of the few states with no db-side field. Db reaction is indirect: albumdata_description[6] 'Search results' 24 -> 23 (hidden photos excluded from search), albumdata_inisync[8] (Winter Holiday folder row) FILETIME tick, albumdata_date[6] activity tick, albums_index 8-byte stamp, wordhash +16. JPEG untouched.

**Captured:** 2026-06-12T16:08:56+00:00

```
baseline: 'after 016-move-photo-rename-dup' (2026-06-12T15:30:41+00:00)
6 file(s) differ (5 semantic, 1 blob/cache)

== CHANGED: db3/albumdata_date.pmp (116 -> 116 bytes)
   type date(f64); rows 12 -> 12
   [6] 46185.35068287037 (2026-06-12 08:24:59) -> 46185.37708333333 (2026-06-12 09:03:00)

== CHANGED: db3/albumdata_description.pmp (54 -> 54 bytes)
   type string; rows 12 -> 12
   [6] '24 results: No matches' -> '23 results: No matches'

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [8] 0x01dcf9d05ae63272 -> 0x01dcfa84f2c020d8

== CHANGED: db3/albums_index.db (164 -> 164 bytes)
   binary; 164 -> 164 bytes
   first difference at offset 0x2c
     before[0x2c:]: 7b7d1cfb25d9167384c10000000000000c000000000000000000000018620200
     after [0x2c:]: e338f4e125d9167384c10000000000000c000000000000000000000018620200

== CHANGED: library/2010-12-25 Winter Holiday/.picasa.ini (66 -> 93 bytes)
   --- before/library/2010-12-25 Winter Holiday/.picasa.ini
   +++ after/library/2010-12-25 Winter Holiday/.picasa.ini
   @@ -3,2 +3,4 @@
    rotate=rotate(1)
    backuphash=43584
   +[photo06.jpg]
   +hidden=yes

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/wordhash.dat — binary; 67260 -> 67276 bytes; first difference at offset 0x10
```
