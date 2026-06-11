# Fixture 008: tag-photo

**Action:** Add keyword/tag: selected photo00.jpg in '2015-03-15 Garden Project' (not an album member), Ctrl+T tags panel, typed 'synthtag', Enter. Immediate artifacts: tag written INTO THE JPEG twice - XMP dc:subject rdf:Bag + IPTC 8BIM dataset 2:25 Keywords (+846 bytes, also adds xmp MetadataDate); ini gets only backuphash=3563. db3/tags.txt still empty, no pmp change yet (lazy flush expected - watching for it as a follow-up fixture).

**Captured:** 2026-06-11T17:15:08+00:00

```
baseline: 'after 007-rotate-db-flush' (2026-06-11T17:07:23+00:00)
2 file(s) differ (2 semantic, 0 blob/cache)

== CHANGED: library/2015-03-15 Garden Project/.picasa.ini (251 -> 283 bytes)
   --- before/library/2015-03-15 Garden Project/.picasa.ini
   +++ after/library/2015-03-15 Garden Project/.picasa.ini
   @@ -7,2 +7,4 @@
    [photo02.jpg]
    albums=f07f69266e7760526d5b1553c7f451fa
   +[photo00.jpg]
   +backuphash=3563

== CHANGED: library/2015-03-15 Garden Project/photo00.jpg (79466 -> 80312 bytes)
   image; 79466 -> 80312 bytes (see fixture copies)
```
