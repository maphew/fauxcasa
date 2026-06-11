# Fixture 010: unstar-photo

**Action:** Remove star: selected starred photo02.jpg in '2010-12-25 Winter Holiday', toggled star off via toolbar. Inverse of 001: ini star=yes line DELETED (no star=no; empty [photo02.jpg] section header remains), starlist.txt line removed (back to 0 bytes). Both synchronous.

**Captured:** 2026-06-11T18:30:35+00:00

```
baseline: 'after 009-tag-db-flush' (2026-06-11T17:16:18+00:00)
2 file(s) differ (2 semantic, 0 blob/cache)

== CHANGED: db3/starlist.txt (93 -> 0 bytes)
   --- before/db3/starlist.txt
   +++ after/db3/starlist.txt
   @@ -1 +0,0 @@
   -Z:\var\home\matt\dev\fauxcasa\cache\synthetic-library\2010-12-25 Winter Holiday\photo02.jpg

== CHANGED: library/2010-12-25 Winter Holiday/.picasa.ini (76 -> 66 bytes)
   --- before/library/2010-12-25 Winter Holiday/.picasa.ini
   +++ after/library/2010-12-25 Winter Holiday/.picasa.ini
   @@ -1,4 +1,3 @@
    [photo02.jpg]
   -star=yes
    [photo00.jpg]
    rotate=rotate(1)
```
