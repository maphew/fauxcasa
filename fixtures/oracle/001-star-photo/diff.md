# Fixture 001: star-photo

**Action:** Star a photo from the photo-edit view: opened folder '2010-12-25 Winter Holiday', double-clicked the 2nd thumbnail in the grid (which is photo02.jpg, not photo01 - grid order != filename order), pressed spacebar (add star), clicked Back to Library. Immediate artifacts: .picasa.ini [photo02.jpg] star=yes, db3/starlist.txt gains full Z:\ path line. NO .pmp change within 3+ min (lazy flush - to be tested at session-end Picasa exit).

**Captured:** 2026-06-11T15:28:51+00:00

```
baseline: 'pre-session-clean' (2026-06-11T15:21:13+00:00)
2 file(s) differ (2 semantic, 0 blob/cache)

== CHANGED: db3/starlist.txt (0 -> 93 bytes)
   --- before/db3/starlist.txt
   +++ after/db3/starlist.txt
   @@ -0,0 +1 @@
   +Z:\var\home\matt\dev\fauxcasa\cache\synthetic-library\2010-12-25 Winter Holiday\photo02.jpg

== ADDED: library/2010-12-25 Winter Holiday/.picasa.ini (- -> 25 bytes)
   --- before/library/2010-12-25 Winter Holiday/.picasa.ini
   +++ after/library/2010-12-25 Winter Holiday/.picasa.ini
   @@ -0,0 +1,2 @@
   +[photo02.jpg]
   +star=yes
```
