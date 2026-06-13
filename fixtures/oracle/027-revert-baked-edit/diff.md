# Fixture 027: revert-baked-edit

**Action:** Revert a baked/saved edit to original, on Winter Holiday/photo01 (text bake from 019). Agent-driven on :3: right-click photo01 > Revert (menu item ENABLED only for baked photos, greyed otherwise) > confirm dialog 'Revert to original version of file? This cannot be undone and all changes will be lost. To undo the last save and keep edits click Undo Save.' with 3 buttons [Revert][Undo Save][Cancel] (Cancel is default-focused; 'Undo Save' is a DISTINCT option = undo the save but keep the edit recipe -- NOT used here). SYNCHRONOUS artifacts: (1) working photo01.jpg RESTORED from stash BYTE-EXACT (55612B baked -> 66033B, == the .picasaoriginals original). (2) stash CONSUMED: .picasaoriginals/photo01.jpg removed; .picasaoriginals/.picasa.ini EMPTIED 226->0 bytes (recipe section [photo01.jpg] moddate/width/height/text/textactive deleted; the ini file is left as 0 bytes, NOT unlinked). (3) main folder ini [photo01.jpg]: backuphash 23770->3079 + NEW 'moddate=4f3b6c00b0f9dc01' line added (the restored original's FILETIME). (4) thumbs2_0/thumbs_0/bigthumbs_0/previews_0 all re-rendered at photo01's offset, text overlay gone [blob/cache, not copied]. NOT in this synchronous diff (expect on a lazy flush): the imagedata pmp edit-state clear (revertable 1->0, originslow, width/height) -- watch for a -db-flush variant. Inverts 019-save-text-to-disk / the 005 bake architecture.

**Captured:** 2026-06-13T22:59:04+00:00

```
baseline: 'manual' (2026-06-13T22:52:53+00:00)
8 file(s) differ (4 semantic, 4 blob/cache)

== CHANGED: library/2010-12-25 Winter Holiday/.picasa.ini (114 -> 139 bytes)
   --- before/library/2010-12-25 Winter Holiday/.picasa.ini
   +++ after/library/2010-12-25 Winter Holiday/.picasa.ini
   @@ -5,3 +5,4 @@
    [photo06.jpg]
    [photo01.jpg]
   -backuphash=23770
   +backuphash=3079
   +moddate=4f3b6c00b0f9dc01

== CHANGED: library/2010-12-25 Winter Holiday/.picasaoriginals/.picasa.ini (226 -> 0 bytes)
   --- before/library/2010-12-25 Winter Holiday/.picasaoriginals/.picasa.ini
   +++ after/library/2010-12-25 Winter Holiday/.picasaoriginals/.picasa.ini
   @@ -1,6 +0,0 @@
   -[photo01.jpg]
   -moddate=a240a7b786fadc01
   -width=1200
   -height=1600
   -text=1;132;11;oracle text;Times;0.342500,0.685625,0.033333,0.000000;v1,4294967295,4278190080,128.000000,1.000000,0.000000,1.000000,400,0,49152;;
   -textactive=1

== REMOVED: library/2010-12-25 Winter Holiday/.picasaoriginals/photo01.jpg (66033 -> - bytes)
   image; 66033 -> - bytes (see fixture copies)

== CHANGED: library/2010-12-25 Winter Holiday/photo01.jpg (55612 -> 66033 bytes)
   image; 55612 -> 66033 bytes (see fixture copies)

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/bigthumbs_0.db — binary; 155537 -> 155537 bytes; first difference at offset 0x2501e
   changed: db3/previews_0.db — binary; 424420 -> 424420 bytes; first difference at offset 0x64388
   changed: db3/thumbs2_0.db — binary; 34415 -> 34415 bytes; first difference at offset 0x8434
   changed: db3/thumbs_0.db — binary; 94424 -> 94424 bytes; first difference at offset 0x16a58
```
