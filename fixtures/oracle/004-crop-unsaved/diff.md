# Fixture 004: crop-unsaved

**Action:** Crop WITHOUT saving: in '2009-07-04 Beach Day' double-clicked photo03.jpg, Crop, drag rectangle, Apply, Back to Library (no File->Save). Artifacts: .picasa.ini ONLY - [photo03.jpg] gains crop=rect64(dc3369dc570a51e) + filters=crop64=1,dc3369dc570a51e; + backuphash=64082. rect64 = 4x16-bit fixed-point fractions of image dims (l=0x0dc3 t=0x369d r=0xc570 b=0xa51e). NO db3 change, JPEG untouched, no .picasaoriginals - fully non-destructive edit.

**Captured:** 2026-06-11T16:07:43+00:00

```
baseline: 'after 003-create-album' (2026-06-11T15:37:27+00:00)
1 file(s) differ (1 semantic, 0 blob/cache)

== CHANGED: library/2009-07-04 Beach Day/.picasa.ini (32 -> 130 bytes)
   --- before/library/2009-07-04 Beach Day/.picasa.ini
   +++ after/library/2009-07-04 Beach Day/.picasa.ini
   @@ -1,2 +1,6 @@
    [photo04.jpg]
    backuphash=3224
   +[photo03.jpg]
   +backuphash=64082
   +crop=rect64(dc3369dc570a51e)
   +filters=crop64=1,dc3369dc570a51e;
```
