# Fixture 024: unhide-photo

**Action:** Un-hide photo06 in '2010-12-25 Winter Holiday' (inverts 017-hide-photo). FIRST fully agent-driven fixture (xdotool mouse/keyboard on isolated headless weston :3, off the user's screen). UI: View menu > Hidden Pictures (toggle ON = view-only, ZERO disk write, verified) to reveal the dimmed hidden thumbnail; then right-click photo06 > Unhide. SYNCHRONOUS: ini DELETES the 'hidden=yes' line (NOT rewritten 'hidden=no'), leaving the now-empty '[photo06.jpg]' section header IN PLACE -- ini 126->114 bytes = -12 = len('hidden=yes')+CRLF; confirms CRLF endings, content-line-only deletion, and empty-header retention, exactly mirroring unstar 010's delete-key-not-=no rule. NO JPEG/XMP/backuphash touch. NO imagedata pmp mirror of the hidden field (hidden is ini-only state, no db column to revert). DB reacts INDIRECTLY and ~immediately (action forced a db write-out): albumdata_description[14] 'Search results' 16->17 (photo06 re-enters the searchable set; exact inverse of 017's -1) + albumdata_date[14] activity tick. TIMING: ini write LAGGED the db by several seconds -- diff at click+1s showed only the 2 db rows; the ini line-removal appeared ~8s later. (albumdata row[14] is the post-022/023 relocated Search-results row.)

**Captured:** 2026-06-13T22:16:06+00:00

```
baseline: 'manual' (2026-06-13T21:56:47+00:00)
5 file(s) differ (4 semantic, 1 blob/cache)

== CHANGED: db3/albumdata_date.pmp (140 -> 140 bytes)
   type date(f64); rows 15 -> 15
   [14] 46186.61934027778 (2026-06-13 14:51:51) -> 46186.635092592594 (2026-06-13 15:14:32)

== CHANGED: db3/albumdata_description.pmp (204 -> 204 bytes)
   type string; rows 15 -> 15
   [14] '16 results: No matches' -> '17 results: No matches'

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [8] 0x01dcfa86b7a7d4c1 -> 0x01dcfb8203dc1ecb

== CHANGED: library/2010-12-25 Winter Holiday/.picasa.ini (126 -> 114 bytes)
   --- before/library/2010-12-25 Winter Holiday/.picasa.ini
   +++ after/library/2010-12-25 Winter Holiday/.picasa.ini
   @@ -4,5 +4,4 @@
    backuphash=43584
    [photo06.jpg]
   -hidden=yes
    [photo01.jpg]
    backuphash=23770

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/wordhash.dat — binary; 66908 -> 66900 bytes; first difference at offset 0x101bc
```
