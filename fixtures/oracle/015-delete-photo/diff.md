# Fixture 015: delete-photo

**Action:** Delete photo, synchronous phase. UI: in '2009-07-04 Beach Day' selected 5th grid photo (= photo05.jpg on disk this time), pressed Delete; 'are you sure' dialog -> checked 'don't ask again' (delete-confirm suppressed for rest of session) -> [Delete Image]. Sync phase is minimal: photo05.jpg unlinked from library folder and moved BYTE-EXACT (sha256 fb912160...d3a7f447 verified vs baseline) to the Wine flatpak's freedesktop trash: ~/.var/app/org.winehq.Wine/data/Trash/files/photo05.jpg + info/photo05.jpg.trashinfo (Path= original full path URL-encoded, DeletionDate=2026-06-12T08:11:49 local) - i.e. Wine maps SHFileOperation FOF_ALLOWUNDO to XDG trash; on real Windows this would be the Recycle Bin. Sync phase wrote only that + one in-place change inside albums_0.db blob at 0x9300. Db flush followed within ~1 min and IS included in this fixture: imagedata_filetype[25] 2 -> 0 (photo row TOMBSTONED in place - table stays 29 rows, no compaction of pmp files), photo05's thumbindex.db entry zeroed out (1460 -> 1449 bytes), albumdata_description[6] 'Search results' count 25 -> 24 + date[6] tick, wordhash.dat shrank. NO ini change in either phase. Also: Picasa sat idle overnight (~8h) and wrote NOTHING - idle noise floor is zero.

**Captured:** 2026-06-12T15:13:15+00:00

```
baseline: 'after 014-face-tag-new-person' (2026-06-12T03:43:48+00:00)
8 file(s) differ (6 semantic, 2 blob/cache)

== CHANGED: db3/albumdata_date.pmp (116 -> 116 bytes)
   type date(f64); rows 12 -> 12
   [6] 46184.86140046296 (2026-06-11 20:40:25) -> 46185.34155092593 (2026-06-12 08:11:50)

== CHANGED: db3/albumdata_description.pmp (54 -> 54 bytes)
   type string; rows 12 -> 12
   [6] '25 results: No matches' -> '24 results: No matches'

== CHANGED: db3/albums_index.db (164 -> 164 bytes)
   binary; 164 -> 164 bytes
   first difference at offset 0x30
     before[0x30:]: 2dd50c8a84c10000000000000c00000000000000000000001862020000000000
     after [0x30:]: 25d9167384c10000000000000c00000000000000000000001862020000000000

== CHANGED: db3/imagedata_filetype.pmp (136 -> 136 bytes)
   type uint32; rows 29 -> 29
   [25] 2 -> 0

== CHANGED: db3/thumbindex.db (1460 -> 1449 bytes)
   binary; 1460 -> 1449 bytes
   first difference at offset 0x517
     before[0x517:]: 70686f746f30352e6a70670000c8c564eafcc9019cce2f00b0f9dc01e3f70000
     after [0x517:]: 000000000000000000000000000000000000000000000000000000ffffffff70

== REMOVED: library/2009-07-04 Beach Day/photo05.jpg (63459 -> - bytes)
   image; 63459 -> - bytes (see fixture copies)

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/albums_0.db — binary; 210792 -> 210792 bytes; first difference at offset 0x9300
   changed: db3/wordhash.dat — binary; 67296 -> 67248 bytes; first difference at offset 0x10060
```
