# Fixture 016: move-photo-rename-dup

**Action:** Move photo between folders, with name-collision rename. UI: dragged photo07.jpg from '2010-12-25 Winter Holiday' onto '2015-03-15 Garden Project' in the left folder tree. Confirm dialog: 'Moving file(s) to Z:\...\Garden Project\. This folder already contains files with the same name. Would you like to rename or skip these files?' [Rename Duplicates] [Skip Duplicates] [Cancel] -> user pressed Rename Duplicates (note: every synthetic folder holds photo00-07, so ANY cross-folder move collides). File side: source unlinked; byte-exact copy (sha256 4d0a200e... verified vs baseline) appears as photo07-001.jpg - Picasa dedupe suffix is '-001'. Sync db: the photo's thumbindex.db entry rewritten IN PLACE with new name+parent (+4 bytes for longer name); wordhash +12. NO imagedata pmp change at all - file identity & folder membership live in thumbindex (name + parent-by-index), so move+rename = one thumbindex rewrite. Flush (+~10min, included here): only albumdata_date[6] activity tick + one 8-byte stamp in albums_index. Search-results count stays 24 (move doesn't change photo count). NO ini change in either folder.

**Captured:** 2026-06-12T15:30:41+00:00

```
baseline: 'after 015-delete-photo' (2026-06-12T15:13:15+00:00)
6 file(s) differ (5 semantic, 1 blob/cache)

== CHANGED: db3/albumdata_date.pmp (116 -> 116 bytes)
   type date(f64); rows 12 -> 12
   [6] 46185.34155092593 (2026-06-12 08:11:50) -> 46185.35068287037 (2026-06-12 08:24:59)

== CHANGED: db3/albums_index.db (164 -> 164 bytes)
   binary; 164 -> 164 bytes
   first difference at offset 0x28
     before[0x28:]: aa14e4697b7d1cfb25d9167384c10000000000000c0000000000000000000000
     after [0x28:]: 27550a727b7d1cfb25d9167384c10000000000000c0000000000000000000000

== CHANGED: db3/thumbindex.db (1449 -> 1453 bytes)
   binary; 1449 -> 1453 bytes
   first difference at offset 0x3b8
     before[0x3b8:]: 2e6a70670000d8f57487a4cb01dda5c500b0f9dc013b6c00000200000000010a
     after [0x3b8:]: 2d3030312e6a70670000d8f57487a4cb01dda5c500b0f9dc013b6c0000020000

== REMOVED: library/2010-12-25 Winter Holiday/photo07.jpg (27707 -> - bytes)
   image; 27707 -> - bytes (see fixture copies)

== ADDED: library/2015-03-15 Garden Project/photo07-001.jpg (- -> 27707 bytes)
   image; - -> 27707 bytes (see fixture copies)

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/wordhash.dat — binary; 67248 -> 67260 bytes; first difference at offset 0x10
```
