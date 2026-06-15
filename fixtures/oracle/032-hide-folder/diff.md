# Fixture 032: hide-folder

**Action:** Hide a folder (move it to Picasa's built-in 'Hidden Folders' collection) on the DISPOSABLE clone (live oracle untouched). UI: selected folder '2010-12-25 Winter Holiday' -> Folder menu -> Hide -> 'Add Password' dialog ('The "Hidden Folders" collection is not currently password protected. Would you like to add a password now?') -> clicked 'Don't Add Password'. REAL SIGNATURE: the folder's own .picasa.ini gains a [Picasa] identity block with P2category=Hidden Folders (190->227 B). The category mark appears in BOTH the folder .picasa.ini (string P2category=Hidden Folders, cf export 030 'Exported Pictures' / folder-desc 020 'Folders on Disk') AND db3 -- the folder's EXISTING albumdata_category row[8] flips 2 ('Folders on Disk') -> 7 ('Hidden Folders'), the integer mirror of the ini mark; NO new album/catdata row is created ('Hidden Folders' is built-in catdata category index 7: Labels/Projects(internal)/Folders on Disk/Web Albums/Web Drive/Exported Pictures/Other Stuff/Hidden Folders/People), and NO per-photo hidden=yes is added (contrast hide-PHOTO 017, which writes ini hidden=yes). So folder-hide == one ini [Picasa] section + the folder's albumdata_category[8] flipping to 7 (no new rows). SESSION NOISE in this diff (NOT the hide action): albumdata_* grew 17->18 via the 'Search results' row reshuffle (row[16] tombstoned to the 4501-01-01 date sentinel, re-appended as row[17]) per the harness's documented session-activity gotcha; thumb/preview/index caches (bigthumbs/previews/thumbs/thumbs2 _0+_index, thumbindex, wordhash, repository.dat) re-rendered. VALIDATES fauxcasa-r42: catalog._is_folder_hidden matches [Picasa] P2category=='Hidden Folders' (case-insensitive) -- confirmed correct, no tracer code change needed.

**Captured:** 2026-06-15T01:05:50+00:00

```
baseline: 'manual' (2026-06-15T00:53:49+00:00)
22 file(s) differ (17 semantic, 5 blob/cache)

== CHANGED: db3/albumdata_category.pmp (88 -> 92 bytes)
   type uint32; rows 17 -> 18
   [8] 2 -> 7
   [17] (new) 0

== CHANGED: db3/albumdata_date.pmp (156 -> 164 bytes)
   type date(f64); rows 17 -> 18
   [16] 46186.73158564815 (2026-06-13 17:33:29) -> 949998.0 (4501-01-01 00:00:00)
   [17] (new) 46187.75027777778 (2026-06-14 18:00:24)

== CHANGED: db3/albumdata_description.pmp (250 -> 273 bytes)
   type string; rows 17 -> 18
   [17] (new) '16 results: No matches'

== CHANGED: db3/albumdata_filename.pmp (196 -> 197 bytes)
   type string; rows 16 -> 17
   [16] (new) ''

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [8] 0x01dcfb956d2416d4 -> 0x01dcfc62af9b9c96

== CHANGED: db3/albumdata_location.pmp (37 -> 38 bytes)
   type string; rows 17 -> 18
   [17] (new) ''

== CHANGED: db3/albumdata_music.pmp (37 -> 38 bytes)
   type string; rows 17 -> 18
   [17] (new) ''

== CHANGED: db3/albumdata_name.pmp (186 -> 187 bytes)
   type string; rows 17 -> 18
   [16] 'Search results' -> ''
   [17] (new) 'Search results'

== CHANGED: db3/albumdata_token.pmp (201 -> 202 bytes)
   type string; rows 17 -> 18
   [16] ']search' -> ''
   [17] (new) ']search'

== CHANGED: db3/albumdata_uid.pmp (324 -> 325 bytes)
   type string; rows 16 -> 17
   [16] (new) ''

== CHANGED: db3/bigthumbs_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x41
     before[0x41:]: 64286b0764286b0764286b0764286b0784286b0784286b000000000744286b07
     after [0x41:]: 24fdd00724fdd00764286b0764286b0784286b0784286b000000000744286b07

== CHANGED: db3/previews_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x41
     before[0x41:]: 64286b0764286b0764286b0764286b0784286b0784286b000000000744286b07
     after [0x41:]: 24fdd00724fdd00764286b0764286b0784286b0784286b000000000744286b07

== CHANGED: db3/repository.dat (140 -> 140 bytes)
   binary; 140 -> 140 bytes
   first difference at offset 0x79
     before[0x79:]: 494450657273697374003200666c6174003100
     after [0x79:]: 666c6174003100494450657273697374003200

== CHANGED: db3/thumbindex.db (1301 -> 1301 bytes)
   binary; 1301 -> 1301 bytes
   first difference at offset 0x275
     before[0x275:]: 29cac41288fbdc0100000000010000000001ffffffff70686f746f30302e6a70
     after [0x275:]: 4402f36b95fbdc0100000000010000000001ffffffff70686f746f30302e6a70

== CHANGED: db3/thumbs2_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x40
     before[0x40:]: 81a5f8aa695a1272fbe3f233248d4019f62ab7c2a4ac37f70000000079d34401
     after [0x40:]: e6e8b9540eb777e8fbe3f233248d4019f62ab7c2a4ac37f70000000079d34401

== CHANGED: db3/thumbs_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x40
     before[0x40:]: 81a5f8aa695a1272fbe3f233248d4019f62ab7c2a4ac37f70000000079d34401
     after [0x40:]: e6e8b9540eb777e8fbe3f233248d4019f62ab7c2a4ac37f70000000079d34401

== CHANGED: library/2010-12-25 Winter Holiday/.picasa.ini (190 -> 227 bytes)
   --- before/library/2010-12-25 Winter Holiday/.picasa.ini
   +++ after/library/2010-12-25 Winter Holiday/.picasa.ini
   @@ -10,2 +10,4 @@
    [photo03.jpg]
    backuphash=60086
   +[Picasa]
   +P2category=Hidden Folders

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/bigthumbs_0.db — binary; 155537 -> 160493 bytes; first difference at offset 0x124de
   changed: db3/previews_0.db — binary; 424420 -> 439513 bytes; first difference at offset 0x384c6
   changed: db3/thumbs2_0.db — binary; 34415 -> 35344 bytes; first difference at offset 0x65a8
   changed: db3/thumbs_0.db — binary; 94424 -> 96886 bytes; first difference at offset 0x5a00
   changed: db3/wordhash.dat — binary; 66904 -> 66904 bytes; first difference at offset 0x4
```
