# Fixture 006: rotate-photo

**Action:** Rotate clockwise in library view (no save): selected photo00.jpg in '2010-12-25 Winter Holiday', Ctrl+R once. Artifacts: ini [photo00.jpg] rotate=rotate(1) + backuphash=43584. JPEG untouched; imagedata_rotate.pmp NOT updated (exists since 005 flush but live edit is ini-only); albumdata_date[6] ('Search results' row) ticked again - it updates on every action, an activity timestamp.

**Captured:** 2026-06-11T16:14:35+00:00

```
baseline: 'after 005-save-crop-to-disk' (2026-06-11T16:11:05+00:00)
2 file(s) differ (2 semantic, 0 blob/cache)

== CHANGED: db3/albumdata_date.pmp (108 -> 108 bytes)
   type date(f64); rows 11 -> 11
   [6] 46184.38159722222 (2026-06-11 09:09:30) -> 46184.382060185184 (2026-06-11 09:10:10)

== CHANGED: library/2010-12-25 Winter Holiday/.picasa.ini (25 -> 76 bytes)
   --- before/library/2010-12-25 Winter Holiday/.picasa.ini
   +++ after/library/2010-12-25 Winter Holiday/.picasa.ini
   @@ -1,2 +1,5 @@
    [photo02.jpg]
    star=yes
   +[photo00.jpg]
   +rotate=rotate(1)
   +backuphash=43584
```
