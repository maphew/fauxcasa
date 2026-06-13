# Fixture 028: revert-db-flush

**Action:** Lazy db edit-state flush ~90s after revert 027 (Winter Holiday/photo01). KEY: imagedata_revertable[12] 1->0 -- the db 'has a saved edit / can revert' flag CLEARED; photo01 is pristine again. imagedata_backuphash[12] 23770->3079 (db mirror of the ini backuphash change from 027). thumbindex.db row for photo01: mtime/FILETIME updated to the restored original. thumbs2/thumbs/bigthumbs/previews _index.db re-stamped for the re-rendered un-baked thumbnails. NOT changed: imagedata_originslow (so originslow, which was SET at bake 019, does NOT clear on revert -- revertable is the operative flag, not originslow) and imagedata width/height (text bake didn't resize, so revert doesn't either). INCIDENTAL: albumdata_inisync[8] (WH folder row) tick + albumdata_date[16] (Search results) activity tick. CONCLUSION: revert's db side = clear revertable + sync backuphash + re-index thumbs (file restore itself was synchronous in 027). NEGATIVE RESULT: no reset-faces (026) lazy db teardown ever appeared in this window -- the virtual face imagedata row (filetype[28]=1001) and contact persist, confirming Reset Faces is a pure per-photo untag with no contact/recognition teardown.

**Captured:** 2026-06-13T23:02:19+00:00

```
baseline: 'after 027-revert-baked-edit' (2026-06-13T22:59:04+00:00)
10 file(s) differ (9 semantic, 1 blob/cache)

== CHANGED: db3/albumdata_date.pmp (156 -> 156 bytes)
   type date(f64); rows 17 -> 17
   [16] 46186.66101851852 (2026-06-13 15:51:52) -> 46186.66726851852 (2026-06-13 16:00:52)

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [8] 0x01dcfb8203dc1ecb -> 0x01dcfb8812c76c4d

== CHANGED: db3/bigthumbs_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x3d
     before[0x3d:]: e433980764286b0764286b0764286b0764286b0784286b0784286b0000000007
     after [0x3d:]: 64286b0764286b0764286b0764286b0764286b0784286b0784286b0000000007

== CHANGED: db3/imagedata_backuphash.pmp (70 -> 70 bytes)
   type uint16; rows 25 -> 25
   [12] 23770 -> 3079

== CHANGED: db3/imagedata_revertable.pmp (44 -> 44 bytes)
   type byte; rows 24 -> 24
   [12] 1 -> 0

== CHANGED: db3/previews_index.db (356 -> 356 bytes)
   binary; 356 -> 356 bytes
   first difference at offset 0x3d
     before[0x3d:]: e433980764286b0764286b0764286b0764286b0784286b0784286b0000000007
     after [0x3d:]: 64286b0764286b0764286b0764286b0764286b0784286b0784286b0000000007

== CHANGED: db3/thumbindex.db (1301 -> 1301 bytes)
   binary; 1301 -> 1301 bytes
   first difference at offset 0x275
     before[0x275:]: 6f63a7b786fadc0100000000010000000001ffffffff70686f746f30302e6a70
     after [0x275:]: 29cac41288fbdc0100000000010000000001ffffffff70686f746f30302e6a70

== CHANGED: db3/thumbs2_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x3c
     before[0x3c:]: f1cdec7981a5f8aa695a1272fbe3f233248d4019f62ab7c2a4ac37f700000000
     after [0x3c:]: 0f7bc97381a5f8aa695a1272fbe3f233248d4019f62ab7c2a4ac37f700000000

== CHANGED: db3/thumbs_index.db (368 -> 368 bytes)
   binary; 368 -> 368 bytes
   first difference at offset 0x3c
     before[0x3c:]: f1cdec7981a5f8aa695a1272fbe3f233248d4019f62ab7c2a4ac37f700000000
     after [0x3c:]: 0f7bc97381a5f8aa695a1272fbe3f233248d4019f62ab7c2a4ac37f700000000

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/wordhash.dat — binary; 66876 -> 66872 bytes; first difference at offset 0x1007c
```
