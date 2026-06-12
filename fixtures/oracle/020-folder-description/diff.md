# Fixture 020: folder-description

**Action:** Edit FOLDER description (the album-description variant is a separate fixture - user edited the folder '2015-03-15 Garden Project', via the folder header/edit-description UI). Sync phase: folder ini gets the full [Picasa] folder-identity block materialized, not just the edited field: name= (folder name), description=description edit in garden project: done!, date=42078.416667 (Picasa epoch days float = 2015-03-15 10:00, the folder's date), P2category=Folders on Disk. Flush (+~3min, included): albumdata_description[7] = description VERBATIM (row 7 = Garden Project folder row, confirmed by matching inisync[7] FILETIME tick), albumdata_date[6] activity tick, thumbnail index/blob churn (bigthumbs +4156 - folder thumb re-render). Key for writer: editing ONE field of folder metadata writes the WHOLE [Picasa] block.

**Captured:** 2026-06-12T16:22:54+00:00

```
baseline: 'after 019-save-text-to-disk' (2026-06-12T16:17:02+00:00)
12 file(s) differ (8 semantic, 4 blob/cache)

== CHANGED: db3/albumdata_date.pmp (116 -> 116 bytes)
   type date(f64); rows 12 -> 12
   [6] 46185.38590277778 (2026-06-12 09:15:42) -> 46185.38835648148 (2026-06-12 09:19:14)

== CHANGED: db3/albumdata_description.pmp (54 -> 95 bytes)
   type string; rows 12 -> 12
   [7] '' -> 'description edit in garden project: done!'

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [7] 0x01dcfa1d2fff950b -> 0x01dcfa8737491438

== CHANGED: db3/bigthumbs_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x3c
     before[0x3c:]: fa0591080764286b0764286b0764286b0764286b0784286b0784286b00000000
     after [0x3c:]: 07e433980764286b0764286b0764286b0764286b0784286b0784286b00000000

== CHANGED: db3/previews_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x3c
     before[0x3c:]: fa0591080764286b0764286b0764286b0764286b0784286b0784286b00000000
     after [0x3c:]: 07e433980764286b0764286b0764286b0764286b0784286b0784286b00000000

== CHANGED: db3/thumbs2_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x3c
     before[0x3c:]: 0f7bc97381a5f8aa695a1272fbe3f233248d4019f62ab7c2a4ac37f700000000
     after [0x3c:]: f1cdec7981a5f8aa695a1272fbe3f233248d4019f62ab7c2a4ac37f700000000

== CHANGED: db3/thumbs_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x3c
     before[0x3c:]: 0f7bc97381a5f8aa695a1272fbe3f233248d4019f62ab7c2a4ac37f700000000
     after [0x3c:]: f1cdec7981a5f8aa695a1272fbe3f233248d4019f62ab7c2a4ac37f700000000

== CHANGED: library/2015-03-15 Garden Project/.picasa.ini (387 -> 531 bytes)
   --- before/library/2015-03-15 Garden Project/.picasa.ini
   +++ after/library/2015-03-15 Garden Project/.picasa.ini
   @@ -12,2 +12,7 @@
    [Contacts2]
    ca5c88ca60f42c0b=synthetiic person 1200;;
   +[Picasa]
   +name=2015-03-15 Garden Project
   +description=description edit in garden project: done!
   +date=42078.416667
   +P2category=Folders on Disk

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/bigthumbs_0.db — binary; 151381 -> 155537 bytes; common prefix; 4156 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/previews_0.db — binary; 424420 -> 424420 bytes; first difference at offset 0x643a0
   changed: db3/thumbs2_0.db — binary; 34415 -> 34415 bytes; first difference at offset 0x8434
   changed: db3/thumbs_0.db — binary; 94424 -> 94424 bytes; first difference at offset 0x16a6e
```
