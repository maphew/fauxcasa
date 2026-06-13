# Fixture 023: delete-folder-db-flush

**Action:** Lazy db reorganization following folder-delete 022, surfacing ~35min later when the Picasa window regained focus/activity (no UI action; keyboard menu test that preceded it writes nothing). Picasa RELOCATED the 'Search results' virtual album from row[6] to a freshly-appended row[13] (name/token=']search'/uid/date 2026-06-13 13:55/description '16 results' all re-created at [13]); old row[6] tombstoned IN PLACE (name/token/uid->'', date->949998.0 year-4501 sentinel, same sentinel as the 022 folder row). Also appended a fully-blank placeholder row[12] (all fields '', category 0, date 4501 sentinel). albumdata tables grew 12->14 rows (uid 12->13); category/location/music/filename columns extended with empty cells. Confirms the tombstone-and-grow model: Picasa never compacts row counts -- it blanks dead rows in place (4501-date sentinel as the 'dead' marker) and APPENDS replacements, so the table monotonically grows. Reorg was deferred from the 022 delete and triggered by session activity, not an immediate flush.

**Captured:** 2026-06-13T20:58:28+00:00

```
baseline: 'after 022-delete-folder' (2026-06-13T20:20:28+00:00)
9 file(s) differ (9 semantic, 0 blob/cache)

== CHANGED: db3/albumdata_category.pmp (68 -> 76 bytes)
   type uint32; rows 12 -> 14
   [12] (new) 0
   [13] (new) 0

== CHANGED: db3/albumdata_date.pmp (116 -> 132 bytes)
   type date(f64); rows 12 -> 14
   [6] 46186.553148148145 (2026-06-13 13:16:32) -> 949998.0 (4501-01-01 00:00:00)
   [12] (new) 949998.0 (4501-01-01 00:00:00)
   [13] (new) 46186.58011574074 (2026-06-13 13:55:22)

== CHANGED: db3/albumdata_description.pmp (135 -> 181 bytes)
   type string; rows 12 -> 14
   [12] (new) '16 results: No matches'
   [13] (new) '16 results: No matches'

== CHANGED: db3/albumdata_filename.pmp (190 -> 193 bytes)
   type string; rows 10 -> 13
   [10] (new) ''
   [11] (new) ''
   [12] (new) ''

== CHANGED: db3/albumdata_location.pmp (32 -> 34 bytes)
   type string; rows 12 -> 14
   [12] (new) ''
   [13] (new) ''

== CHANGED: db3/albumdata_music.pmp (32 -> 34 bytes)
   type string; rows 12 -> 14
   [12] (new) ''
   [13] (new) ''

== CHANGED: db3/albumdata_name.pmp (198 -> 200 bytes)
   type string; rows 12 -> 14
   [6] 'Search results' -> ''
   [12] (new) ''
   [13] (new) 'Search results'

== CHANGED: db3/albumdata_token.pmp (235 -> 237 bytes)
   type string; rows 12 -> 14
   [6] ']search' -> ''
   [12] (new) ''
   [13] (new) ']search'

== CHANGED: db3/albumdata_uid.pmp (384 -> 353 bytes)
   type string; rows 12 -> 13
   [6] '6a852a01b4161f61db41b705386a592b' -> ''
   [12] (new) ''
```
