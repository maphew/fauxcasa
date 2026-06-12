# Fixture 021: album-description

**Action:** Edit VIRTUAL album description (differential vs 020's folder description). UI: clicked 'Add a description' under the 'Synthetic Album B' header. ATTEMPT 1 left ZERO disk trace across ~10min - the field apparently never committed (needs Enter/click-away; silently dropped under Wine; user later found the field empty). ATTEMPT 2: retyped (intended 'description to renamed album, 2nd attempt'; stored 'descriptin...' - Wine dropped the 'o'), followed by a left-tree selection change; ini write landed within the next 30s poll. Storage model: album description = description= line inside the [.album:f07f6926...] section, which lives in the .picasa.ini of the FOLDER holding the album's member photos ('2015-03-15 Garden Project') - albums have no file of their own (no .pal). Inserted after token=/date= lines. Flush (+~3min, included): albumdata_description[10] (Synthetic Album B row) = text VERBATIM; albumdata_inisync[7] (Garden Project folder row) FILETIME tick; albumdata_inisync[10] (album row) flag-word LOW 32 bits churned, high half stable - more albumrow-flag evidence atop fixture 011's 1-bit change. No thumbnail churn, no activity-date tick.

**Captured:** 2026-06-12T19:44:08+00:00

```
baseline: 'after 020-folder-description' (2026-06-12T16:22:54+00:00)
3 file(s) differ (3 semantic, 0 blob/cache)

== CHANGED: db3/albumdata_description.pmp (95 -> 135 bytes)
   type string; rows 12 -> 12
   [10] '' -> 'descriptin to renamed album, 2nd attempt'

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [7] 0x01dcfa8737491438 -> 0x01dcfaa36ab1a24c
   [10] 0x9cf208d20000c184 -> 0xf2e0ab7d0000c184

== CHANGED: library/2015-03-15 Garden Project/.picasa.ini (531 -> 585 bytes)
   --- before/library/2015-03-15 Garden Project/.picasa.ini
   +++ after/library/2015-03-15 Garden Project/.picasa.ini
   @@ -3,4 +3,5 @@
    token=f07f69266e7760526d5b1553c7f451fa
    date=2015-03-15T09:59:59-07:00
   +description=descriptin to renamed album, 2nd attempt
    [photo01.jpg]
    albums=f07f69266e7760526d5b1553c7f451fa
```
