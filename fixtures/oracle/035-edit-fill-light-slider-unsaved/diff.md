# Fixture 035: edit-fill-light-slider-unsaved

**Action:** Fill Light slider (Basic Fixes) on Garden Project/photo03.jpg (pristine). Agent-driven headless :2. UI: select photo03 -> Edit room -> Basic Fixes -> set Fill Light via a SINGLE CLICK on the slider track (window-rel ~128,329, right of the thumb) -> Back to Library (NO File->Save). Undo button -> 'Undo Fill Light' confirmed. SYNCHRONOUS (ini-only): new [photo03.jpg] filters=fill=1,0.186916; + backuphash=4142. KEY: 'fill' = the Fill Light token; param shape pins the FIRST parametrized non-crop filter -> 'fill=1,<f>' where the leading 1 is the enabled-flag (same slot as crop64=1,...) and f=0.186916 is the normalized fill amount as a %f 6-decimal float (the single track-click landed ~0.19). Contrast 034 enhance=1 (bare flag, no comma params). DRAG-GESTURE MITIGATION VALIDATED: a single click on the slider track jumps the thumb and sets the value with NO drag -> no stuck drag-overlay (the wine-oracle.md hazard for sliders/pucks); this is the technique for all slider/puck FLAG items (035/036/037/043/048). EXPECTED LAZY FLUSH (~2-6min, not waited): imagedata_filters ''->'fill=1,0.186916;', imagedata_edited->1, imagedata_backuphash mirror. SESSION NOISE (not the edit): albumdata Search-results reshuffle rows 21->22 (4501 date sentinel tombstone+append). HARNESS GOTCHAS this session: (a) clicks need throwaway-focus-click-then-real (first click after a gap is swallowed because weston keyboard focus stays on its internal window; mouse XTEST still hits window-under-pointer); (b) the 'Back to Library' button center is window-rel ~y33 (between the y16 menu bar and the panel) -- y22 lands in a dead strip; (c) the .picasa.ini write can lag the click by several seconds -- re-diff.

**Captured:** 2026-06-20T23:02:11+00:00

```
baseline: 'after 034-edit-enhance-oneclick-unsaved' (2026-06-20T22:54:05+00:00)
9 file(s) differ (8 semantic, 1 blob/cache)

== CHANGED: db3/albumdata_category.pmp (104 -> 108 bytes)
   type uint32; rows 21 -> 22
   [21] (new) 0

== CHANGED: db3/albumdata_date.pmp (188 -> 196 bytes)
   type date(f64); rows 21 -> 22
   [21] (new) 46193.66357638889 (2026-06-20 15:55:33)

== CHANGED: db3/albumdata_description.pmp (311 -> 334 bytes)
   type string; rows 21 -> 22
   [21] (new) '16 results: No matches'

== CHANGED: db3/albumdata_location.pmp (41 -> 42 bytes)
   type string; rows 21 -> 22
   [21] (new) ''

== CHANGED: db3/albumdata_music.pmp (41 -> 42 bytes)
   type string; rows 21 -> 22
   [21] (new) ''

== CHANGED: db3/albumdata_name.pmp (176 -> 191 bytes)
   type string; rows 21 -> 22
   [21] (new) 'Search results'

== CHANGED: db3/albumdata_token.pmp (198 -> 206 bytes)
   type string; rows 21 -> 22
   [21] (new) ']search'

== CHANGED: library/2015-03-15 Garden Project/.picasa.ini (500 -> 558 bytes)
   --- before/library/2015-03-15 Garden Project/.picasa.ini
   +++ after/library/2015-03-15 Garden Project/.picasa.ini
   @@ -18,2 +18,5 @@
    filters=enhance=1;
    backuphash=39773
   +[photo03.jpg]
   +filters=fill=1,0.186916;
   +backuphash=4142

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/thumbs_0.db — binary; 99629 -> 99629 bytes; first difference at offset 0xe9b2
```

**CORRECTION (2026-08-12 salvage review, fauxcasa-nu9): the ini-only/'flush not waited' claim is accurate for this fixture — no imagedata_* rows changed in this diff. Note the flush of THIS edit (imagedata_filters[5] ''->'fill=1,0.186916;', edited[5] 0->1) appears as carry-over in fixture 036's snapshot.**
