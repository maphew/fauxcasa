# Fixture 033: cross-folder-batch-star

**Action:** Cross-folder batch star: built album 'xfbatch' from Garden Project/photo05.jpg + Winter Holiday/photo05.jpg (added one at a time), opened the album (flat, no folder headers), Ctrl+A to select BOTH members across folders, then bottom-tray Add/Remove Star. One action -> star=yes written to BOTH folders' .picasa.ini AND both full Z:\ paths appended to db3/starlist.txt atomically. NB: cross-folder multi-select is ONLY reachable via a flat album + Ctrl+A; Ctrl+click/Shift+click/hold-pin/context-menu all scope to the photo's source folder even inside the album.

**Captured:** 2026-06-16T04:05:52+00:00

```
baseline: 'pre 033 xfbatch album star-all (both photo05 unstarred)' (2026-06-16T03:56:45+00:00)
8 file(s) differ (7 semantic, 1 blob/cache)

== CHANGED: db3/albumdata_date.pmp (180 -> 180 bytes)
   type date(f64); rows 20 -> 20
   [19] 46188.86988425926 (2026-06-15 20:52:38) -> 46188.877905092595 (2026-06-15 21:04:11)

== CHANGED: db3/albumdata_inisync.pmp (172 -> 172 bytes)
   type uint64; rows 19 -> 19
   [7] 0x01dcfd42fd307ee5 -> 0x01dcfd4532fac818
   [8] 0x01dcfd4362682098 -> 0x01dcfd4532faef28

== CHANGED: db3/albumdata_uid.pmp (359 -> 392 bytes)
   type string; rows 19 -> 20
   [19] (new) 'ebc015ff0824b9d171b4b36d9ad5604d'

== CHANGED: db3/repository.dat (140 -> 140 bytes)
   binary; 140 -> 140 bytes
   first difference at offset 0x79
     before[0x79:]: 494450657273697374003200666c6174003100
     after [0x79:]: 666c6174003100494450657273697374003200

== CHANGED: db3/starlist.txt (0 -> 186 bytes)
   --- before/db3/starlist.txt
   +++ after/db3/starlist.txt
   @@ -0,0 +1,2 @@
   +Z:\var\home\matt\dev\fauxcasa\cache\synthetic-library\2015-03-15 Garden Project\photo05.jpg
   +Z:\var\home\matt\dev\fauxcasa\cache\synthetic-library\2010-12-25 Winter Holiday\photo05.jpg

== CHANGED: library/2010-12-25 Winter Holiday/.picasa.ini (390 -> 400 bytes)
   --- before/library/2010-12-25 Winter Holiday/.picasa.ini
   +++ after/library/2010-12-25 Winter Holiday/.picasa.ini
   @@ -13,4 +13,5 @@
    [photo05.jpg]
    albums=0925e37243e77ab02c6835534d79ce09
   +star=yes
    [.album:0925e37243e77ab02c6835534d79ce09]
    name=xfbatch

== CHANGED: library/2015-03-15 Garden Project/.picasa.ini (549 -> 559 bytes)
   --- before/library/2015-03-15 Garden Project/.picasa.ini
   +++ after/library/2015-03-15 Garden Project/.picasa.ini
   @@ -19,2 +19,3 @@
    [photo05.jpg]
    albums=0925e37243e77ab02c6835534d79ce09
   +star=yes

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/wordhash.dat — binary; 66936 -> 66952 bytes; first difference at offset 0x101d8
```
