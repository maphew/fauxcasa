# Fixture 003: create-album

**Action:** Create album from selection: in '2015-03-15 Garden Project' selected photo01.jpg + photo02.jpg (ctrl-click), right-click -> Add to Album -> New Album, named 'Synthetic Album A'. Artifacts: new row [10] in all albumdata_*.pmp (name, uid=f07f69266e7760526d5b1553c7f451fa, token=']album:<uid>', category=0, date=2015-03-15T09:59:59 = photo date not creation date, empty description/location/music); folder .picasa.ini gains [.album:<uid>] section (name/token/date) + 'albums=<uid>' under each member photo's section. NO .pal file in Picasa2Albums. albumdata_inisync[7] (folder row) set to FILETIME = ini-sync timestamp hypothesis. albums_0.db +75KB appended; 4x *_index.db few-byte changes.

**Captured:** 2026-06-11T15:37:27+00:00

```
baseline: 'after 002-caption-photo' (2026-06-11T15:32:02+00:00)
17 file(s) differ (16 semantic, 1 blob/cache)

== CHANGED: db3/albumdata_category.pmp (60 -> 64 bytes)
   type uint32; rows 10 -> 11
   [10] (new) 0

== CHANGED: db3/albumdata_date.pmp (100 -> 108 bytes)
   type date(f64); rows 10 -> 11
   [6] 46184.35083333333 (2026-06-11 08:25:12) -> 46184.358773148146 (2026-06-11 08:36:38)
   [10] (new) 42078.416655092595 (2015-03-15 09:59:59)

== CHANGED: db3/albumdata_description.pmp (52 -> 53 bytes)
   type string; rows 10 -> 11
   [10] (new) ''

== CHANGED: db3/albumdata_inisync.pmp (100 -> 108 bytes)
   type uint64; rows 10 -> 11
   [7] 0x0000000000000000 -> 0x01dcf9b818fe48de
   [10] (new) 0x9cf208f20000c184

== CHANGED: db3/albumdata_location.pmp (30 -> 31 bytes)
   type string; rows 10 -> 11
   [10] (new) ''

== CHANGED: db3/albumdata_music.pmp (27 -> 31 bytes)
   type string; rows 7 -> 11
   [7] (new) ''
   [8] (new) ''
   [9] (new) ''
   [10] (new) ''

== CHANGED: db3/albumdata_name.pmp (177 -> 195 bytes)
   type string; rows 10 -> 11
   [10] (new) 'Synthetic Album A'

== CHANGED: db3/albumdata_token.pmp (220 -> 260 bytes)
   type string; rows 10 -> 11
   [10] (new) ']album:f07f69266e7760526d5b1553c7f451fa'

== CHANGED: db3/albumdata_uid.pmp (350 -> 383 bytes)
   type string; rows 10 -> 11
   [10] (new) 'f07f69266e7760526d5b1553c7f451fa'

== CHANGED: db3/albums_0.db (81200 -> 156184 bytes)
   binary; 81200 -> 156184 bytes
   common prefix; 74984 bytes appended: 610000006a0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000...

== CHANGED: db3/albums_index.db (140 -> 152 bytes)
   binary; 140 -> 152 bytes
   first difference at offset 0x8
     before[0x8:]: 0a00000000000000000000000000000000000000000000000000000000000000
     after [0x8:]: 0b00000000000000000000000000000000000000000000000000000000000000

== CHANGED: db3/bigthumbs_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x6d
     before[0x6d:]: 64286b0764286b0764286b0764286b1c000000000000000000000004000000f0
     after [0x6d:]: 44b56c0764286b0764286b0764286b1c000000000000000000000004000000f0

== CHANGED: db3/previews_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x6d
     before[0x6d:]: 64286b0764286b0764286b0764286b1c000000000000000000000004000000cd
     after [0x6d:]: 44b56c0764286b0764286b0764286b1c000000000000000000000004000000cd

== CHANGED: db3/thumbs2_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x6c
     before[0x6c:]: d2691b41360335d57c46e06e237c40af1c0000000000000000000000f6530000
     after [0x6c:]: 038efe51360335d57c46e06e237c40af1c0000000000000000000000f6530000

== CHANGED: db3/thumbs_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x6c
     before[0x6c:]: d2691b41360335d57c46e06e237c40af1c000000000000000000000004000000
     after [0x6c:]: 038efe51360335d57c46e06e237c40af1c000000000000000000000004000000

== ADDED: library/2015-03-15 Garden Project/.picasa.ini (- -> 251 bytes)
   --- before/library/2015-03-15 Garden Project/.picasa.ini
   +++ after/library/2015-03-15 Garden Project/.picasa.ini
   @@ -0,0 +1,8 @@
   +[.album:f07f69266e7760526d5b1553c7f451fa]
   +name=Synthetic Album A
   +token=f07f69266e7760526d5b1553c7f451fa
   +date=2015-03-15T09:59:59-07:00
   +[photo01.jpg]
   +albums=f07f69266e7760526d5b1553c7f451fa
   +[photo02.jpg]
   +albums=f07f69266e7760526d5b1553c7f451fa

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/wordhash.dat — binary; 67184 -> 67216 bytes; first difference at offset 0x10
```
