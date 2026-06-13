# Fixture 022: delete-folder

**Action:** Right-click folder '2009-07-04 Beach Day' in ToC -> Delete Folder. Whole folder + its 7 remaining live photos removed from disk; NO OS-trash stash found (searched flatpak wine Trash, host XDG Trash, wine prefix recycle bin, per-fs .Trash-1000) -- contrasts with single-photo delete 015 which trashed photo05.jpg byte-exact. DB rewrite was synchronous (pmps already updated at diff time). albumdata row[9] blanked in place (uid/name/token/filename->'' ) + date[9]->949998.0 (year-4501 sentinel); all member imagedata rows tombstoned filetype->0 (folder row 1->0, 7 photos 2->0; row already-0 from 015 delete); search-results 23->16 (-7 live); thumbindex.db compacted (shrank 1453->1301); wordhash shrank; repository.dat key order swapped. Side effect: sparse albumdata_uid densified -- face album (014) uid backfilled at row[11] ']facealbum:11' = 'synthetiic person 1200'.

**Captured:** 2026-06-13T20:20:28+00:00

```
baseline: 'after 021-album-description' (2026-06-12T19:44:08+00:00)
21 file(s) differ (20 semantic, 1 blob/cache)

== CHANGED: db3/albumdata_date.pmp (116 -> 116 bytes)
   type date(f64); rows 12 -> 12
   [6] 46185.38835648148 (2026-06-12 09:19:14) -> 46186.553148148145 (2026-06-13 13:16:32)
   [9] 39998.416666666664 (2009-07-04 10:00:00) -> 949998.0 (4501-01-01 00:00:00)

== CHANGED: db3/albumdata_description.pmp (135 -> 135 bytes)
   type string; rows 12 -> 12
   [6] '23 results: No matches' -> '16 results: No matches'

== CHANGED: db3/albumdata_filename.pmp (265 -> 190 bytes)
   type string; rows 10 -> 10
   [9] 'Z:\\var\\home\\matt\\dev\\fauxcasa\\cache\\synthetic-library\\2009-07-04 Beach Day\\' -> ''

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [9] 0x01dcf9d4a2e50f82 -> 0xfbb88259a2e50f82

== CHANGED: db3/albumdata_name.pmp (218 -> 198 bytes)
   type string; rows 12 -> 12
   [9] '2009-07-04 Beach Day' -> ''

== CHANGED: db3/albumdata_token.pmp (274 -> 235 bytes)
   type string; rows 12 -> 12
   [9] ']album:cc0858737c08a7889250a1c557e9b606' -> ''

== CHANGED: db3/albumdata_uid.pmp (383 -> 384 bytes)
   type string; rows 11 -> 12
   [9] 'cc0858737c08a7889250a1c557e9b606' -> ''
   [11] (new) '26d74ed367e89d9698b9d9b01d73f773'

== CHANGED: db3/imagedata_filetype.pmp (136 -> 136 bytes)
   type uint32; rows 29 -> 29
   [19] 1 -> 0
   [20] 2 -> 0
   [21] 2 -> 0
   [22] 2 -> 0
   [23] 2 -> 0
   [24] 2 -> 0
   [26] 2 -> 0
   [27] 2 -> 0

== CHANGED: db3/repository.dat (140 -> 140 bytes)
   binary; 140 -> 140 bytes
   first difference at offset 0x79
     before[0x79:]: 494450657273697374003200666c6174003100
     after [0x79:]: 666c6174003100494450657273697374003200

== CHANGED: db3/thumbindex.db (1453 -> 1301 bytes)
   binary; 1453 -> 1301 bytes
   first difference at offset 0xb6
     before[0xb6:]: d50cb1bac5f9dc0100000000010000000001ffffffff70686f746f30302e6a70
     after [0xb6:]: 65a1b2a17ffadc0100000000010000000001ffffffff70686f746f30302e6a70

== REMOVED: library/2009-07-04 Beach Day/.picasa.ini (66 -> - bytes)
   --- before/library/2009-07-04 Beach Day/.picasa.ini
   +++ after/library/2009-07-04 Beach Day/.picasa.ini
   @@ -1,4 +0,0 @@
   -[photo04.jpg]
   -backuphash=14043
   -[photo03.jpg]
   -backuphash=39767

== REMOVED: library/2009-07-04 Beach Day/.picasaoriginals/.picasa.ini (143 -> - bytes)
   --- before/library/2009-07-04 Beach Day/.picasaoriginals/.picasa.ini
   +++ after/library/2009-07-04 Beach Day/.picasaoriginals/.picasa.ini
   @@ -1,7 +0,0 @@
   -[photo03.jpg]
   -filters=crop64=1,dc3369dc570a51e;
   -crop=rect64(dc3369dc570a51e)
   -moddate=5ec020afbcf9dc01
   -width=800
   -height=600
   -textactive=0

== REMOVED: library/2009-07-04 Beach Day/.picasaoriginals/photo03.jpg (26634 -> - bytes)
   image; 26634 -> - bytes (see fixture copies)

== REMOVED: library/2009-07-04 Beach Day/photo00.jpg (76421 -> - bytes)
   image; 76421 -> - bytes (see fixture copies)

== REMOVED: library/2009-07-04 Beach Day/photo01.jpg (64413 -> - bytes)
   image; 64413 -> - bytes (see fixture copies)

== REMOVED: library/2009-07-04 Beach Day/photo02.jpg (111387 -> - bytes)
   image; 111387 -> - bytes (see fixture copies)

== REMOVED: library/2009-07-04 Beach Day/photo03.jpg (17220 -> - bytes)
   image; 17220 -> - bytes (see fixture copies)

== REMOVED: library/2009-07-04 Beach Day/photo04.jpg (78165 -> - bytes)
   image; 78165 -> - bytes (see fixture copies)

== REMOVED: library/2009-07-04 Beach Day/photo06.jpg (112393 -> - bytes)
   image; 112393 -> - bytes (see fixture copies)

== REMOVED: library/2009-07-04 Beach Day/photo07.jpg (26545 -> - bytes)
   image; 26545 -> - bytes (see fixture copies)

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/wordhash.dat — binary; 67280 -> 66908 bytes; first difference at offset 0x204
```
