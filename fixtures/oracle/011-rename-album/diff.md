# Fixture 011: rename-album

**Action:** Rename album 'Synthetic Album A' -> 'Synthetic Album B' via left panel. Synchronous phase: ini [.album:uid] name= line rewritten in place; uid and token UNCHANGED (album identity is the uid, name is display-only). albumdata_name.pmp NOT yet updated (lazy). wordhash shrank 8 bytes (re-indexed name). STOWAWAY: albumdata_inisync[8] (Winter Holiday row) tick belongs to fixture 010's unstar, arriving on its lazy cycle - not part of the rename.

**Captured:** 2026-06-11T18:32:38+00:00

```
baseline: 'after 010-unstar-photo' (2026-06-11T18:30:35+00:00)
4 file(s) differ (3 semantic, 1 blob/cache)

== CHANGED: db3/albumdata_date.pmp (108 -> 108 bytes)
   type date(f64); rows 11 -> 11
   [6] 46184.4265625 (2026-06-11 10:14:15) -> 46184.479317129626 (2026-06-11 11:30:13)

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [8] 0x01dcf9bd4fcb86a1 -> 0x01dcf9d05ae63272

== CHANGED: library/2015-03-15 Garden Project/.picasa.ini (283 -> 283 bytes)
   --- before/library/2015-03-15 Garden Project/.picasa.ini
   +++ after/library/2015-03-15 Garden Project/.picasa.ini
   @@ -1,4 +1,4 @@
    [.album:f07f69266e7760526d5b1553c7f451fa]
   -name=Synthetic Album A
   +name=Synthetic Album B
    token=f07f69266e7760526d5b1553c7f451fa
    date=2015-03-15T09:59:59-07:00

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/wordhash.dat — binary; 67264 -> 67256 bytes; first difference at offset 0x10250
```
