# Fixture 025: delete-album

**Action:** Delete virtual album 'Synthetic Album B' (inverts 003-create-album/011-rename/021-album-description). Agent-driven on headless :3: tree right-click album > Delete Album > confirm dialog ('Are you sure you want to delete the album "Synthetic Album B"?', buttons [Delete Album][No], No is default). NO photo files touched. PERSISTENCE IS LAZY: UI updated instantly (Albums 2->1) but disk flush lagged ~3-4 min (contrast folder-delete 022 which wrote synchronously). FLUSH ARTIFACTS: (1) albumdata row[10] TOMBSTONED IN PLACE - name/token/uid -> '', date[10] -> 949998.0 (year-4501 dead-row sentinel, same as 022 folder + 023 search-reshuffle); row count NOT reduced (tombstone-and-grow). (2) member-folder ini (2015-03-15 Garden Project/.picasa.ini, 585->393B): REMOVED the rich '[.album:uid]' definition block (name/token/date(ISO-8601!)/description) from the top AND removed the per-photo 'albums=uid' membership lines from [photo01]/[photo02]; BUT LEFT a minimal token-only '[.album:uid]\ntoken=uid' STUB at the bottom (album ghost) -- so the ini still grep-matches '[.album:'. (3) albumdata_inisync[10] (album row flag word) + [7] (Garden folder row) churned; wordhash shrank 16B (blob). INCIDENTAL (not part of delete): the recurring 'Search results' albumdata row reshuffle fired again (row[14] tombstoned w/4501 sentinel -> new row[15]) -- session-activity churn documented in 023.

**Captured:** 2026-06-13T22:39:41+00:00

```
baseline: 'manual' (2026-06-13T22:25:07+00:00)
12 file(s) differ (11 semantic, 1 blob/cache)

== CHANGED: db3/albumdata_category.pmp (80 -> 84 bytes)
   type uint32; rows 15 -> 16
   [15] (new) 0

== CHANGED: db3/albumdata_date.pmp (140 -> 148 bytes)
   type date(f64); rows 15 -> 16
   [10] 42078.416655092595 (2015-03-15 09:59:59) -> 949998.0 (4501-01-01 00:00:00)
   [14] 46186.635092592594 (2026-06-13 15:14:32) -> 949998.0 (4501-01-01 00:00:00)
   [15] (new) 46186.64570601852 (2026-06-13 15:29:49)

== CHANGED: db3/albumdata_description.pmp (204 -> 227 bytes)
   type string; rows 15 -> 16
   [15] (new) '17 results: No matches'

== CHANGED: db3/albumdata_filename.pmp (194 -> 195 bytes)
   type string; rows 14 -> 15
   [14] (new) ''

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [7] 0x01dcfaa36ab1a24c -> 0x01dcfb84265739a0
   [10] 0xf2e0ab7d0000c184 -> 0xec1775cf0000c184

== CHANGED: db3/albumdata_location.pmp (35 -> 36 bytes)
   type string; rows 15 -> 16
   [15] (new) ''

== CHANGED: db3/albumdata_music.pmp (35 -> 36 bytes)
   type string; rows 15 -> 16
   [15] (new) ''

== CHANGED: db3/albumdata_name.pmp (201 -> 185 bytes)
   type string; rows 15 -> 16
   [10] 'Synthetic Album B' -> ''
   [14] 'Search results' -> ''
   [15] (new) 'Search results'

== CHANGED: db3/albumdata_token.pmp (238 -> 200 bytes)
   type string; rows 15 -> 16
   [10] ']album:f07f69266e7760526d5b1553c7f451fa' -> ''
   [14] ']search' -> ''
   [15] (new) ']search'

== CHANGED: db3/albumdata_uid.pmp (387 -> 323 bytes)
   type string; rows 15 -> 15
   [10] 'f07f69266e7760526d5b1553c7f451fa' -> ''
   [14] '638c4c7f2a8091e200e18a43531d4414' -> ''

== CHANGED: library/2015-03-15 Garden Project/.picasa.ini (585 -> 393 bytes)
   --- before/library/2015-03-15 Garden Project/.picasa.ini
   +++ after/library/2015-03-15 Garden Project/.picasa.ini
   @@ -1,11 +1,4 @@
   -[.album:f07f69266e7760526d5b1553c7f451fa]
   -name=Synthetic Album B
   -token=f07f69266e7760526d5b1553c7f451fa
   -date=2015-03-15T09:59:59-07:00
   -description=descriptin to renamed album, 2nd attempt
    [photo01.jpg]
   -albums=f07f69266e7760526d5b1553c7f451fa
    [photo02.jpg]
   -albums=f07f69266e7760526d5b1553c7f451fa
    [photo00.jpg]
    backuphash=600
   @@ -18,2 +11,4 @@
    date=42078.416667
    P2category=Folders on Disk
   +[.album:f07f69266e7760526d5b1553c7f451fa]
   +token=f07f69266e7760526d5b1553c7f451fa

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/wordhash.dat — binary; 66900 -> 66884 bytes; first difference at offset 0x251
```
