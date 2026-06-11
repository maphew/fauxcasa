# Fixture 012: rename-album-db-flush

**Action:** Deferred db flush of fixture 011's album rename (~2min): albumdata_name[10] 'Synthetic Album A' -> 'Synthetic Album B' rewritten in place; albumdata_inisync[7] (Garden Project folder, ini host) FILETIME; albumdata_inisync[10] (the album's own row) changed by ONE BIT (0x9cf208f2.. -> 0x9cf208d2..) - album rows in inisync are not FILETIMEs but some state/flag word.

**Captured:** 2026-06-11T18:34:35+00:00

```
baseline: 'after 011-rename-album' (2026-06-11T18:32:38+00:00)
3 file(s) differ (3 semantic, 0 blob/cache)

== CHANGED: db3/albumdata_date.pmp (108 -> 108 bytes)
   type date(f64); rows 11 -> 11
   [6] 46184.479317129626 (2026-06-11 11:30:13) -> 46184.480520833335 (2026-06-11 11:31:57)

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [7] 0x01dcf9c5bbe24ec2 -> 0x01dcf9d0972f30ab
   [10] 0x9cf208f20000c184 -> 0x9cf208d20000c184

== CHANGED: db3/albumdata_name.pmp (195 -> 195 bytes)
   type string; rows 11 -> 11
   [10] 'Synthetic Album A' -> 'Synthetic Album B'
```
