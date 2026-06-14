# Fixture 029: multi-photo-batch-tag

**Action:** Multi-photo batch keyword, SAME-folder: selected Winter Holiday photo02+photo03 (click + shift-click), opened Tags panel (Ctrl+T), typed 'batchtag', Enter -> applied to BOTH in ONE action. Agent-driven on headless :3 (worktree oracle-batch3). SYNCHRONOUS: both JPEGs rewritten +846B each (identical XMP dc:subject + IPTC keyword, per 008). NEAR-IMMEDIATE flush (tag forced a db write-out, like caption 002): WH folder ini gained 'backuphash=60086' under BOTH [photo02] and [photo03] (ONE ini, two sections, one action); imagedata_backuphash[13],[14] 0->60086 (db mirror); imagedata_tags[13],[14] ''->'batchtag'; imagedata_originslow[13],[14] 0->nonzero. thumbindex photo03 row updated (size grew). albumdata_inisync[8] WH tick + date[16] Search-results churn. KEY FINDINGS: (1) batch = N atomic per-photo writes in one action; deltas = N copies of single-photo 008/009. (2) backuphash is SHARED -- same 60086 for both -> it is derived from the EDIT (the keyword), NOT per-photo content. (3) originslow is PER-PHOTO (0x4a95fdae5554f628 vs 0x52fce680cde37a90) -> a content hash set whenever the JPEG is rewritten; refines 028 (originslow is NOT revert-specific, it is a general 'content changed' marker, set here on tag-write). tags.txt stays empty (per 008). NOTE: imagedata rows [13]/[14] = photo02/photo03. STILL UNTESTED (fauxcasa-ezn): CROSS-folder batch (photos in 2 folders in one action -> do 2 separate .picasa.ini files get written atomically?).

**Captured:** 2026-06-14T00:34:46+00:00

```
baseline: 'manual' (2026-06-14T00:27:41+00:00)
10 file(s) differ (9 semantic, 1 blob/cache)

== CHANGED: db3/albumdata_date.pmp (156 -> 156 bytes)
   type date(f64); rows 17 -> 17
   [16] 46186.66726851852 (2026-06-13 16:00:52) -> 46186.73158564815 (2026-06-13 17:33:29)

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [8] 0x01dcfb8812c76c4d -> 0x01dcfb956d2416d4

== CHANGED: db3/imagedata_backuphash.pmp (70 -> 70 bytes)
   type uint16; rows 25 -> 25
   [13] 0 -> 60086
   [14] 0 -> 60086

== CHANGED: db3/imagedata_originslow.pmp (220 -> 220 bytes)
   type uint64; rows 25 -> 25
   [13] 0x0000000000000000 -> 0x4a95fdae5554f628
   [14] 0x0000000000000000 -> 0x52fce680cde37a90

== CHANGED: db3/imagedata_tags.pmp (69 -> 85 bytes)
   type string6; rows 25 -> 25
   [13] '' -> 'batchtag'
   [14] '' -> 'batchtag'

== CHANGED: db3/thumbindex.db (1301 -> 1301 bytes)
   binary; 1301 -> 1301 bytes
   first difference at offset 0x2f3
     before[0x2f3:]: 9a688500b0f9dc0122bd01000200000000010a00000070686f746f30332e6a70
     after [0x2f3:]: 8012b56b95fbdc0170c001000200000000010a00000070686f746f30332e6a70

== CHANGED: library/2010-12-25 Winter Holiday/.picasa.ini (139 -> 190 bytes)
   --- before/library/2010-12-25 Winter Holiday/.picasa.ini
   +++ after/library/2010-12-25 Winter Holiday/.picasa.ini
   @@ -1,3 +1,4 @@
    [photo02.jpg]
   +backuphash=60086
    [photo00.jpg]
    rotate=rotate(1)
   @@ -7,2 +8,4 @@
    backuphash=3079
    moddate=4f3b6c00b0f9dc01
   +[photo03.jpg]
   +backuphash=60086

== CHANGED: library/2010-12-25 Winter Holiday/photo02.jpg (113954 -> 114800 bytes)
   image; 113954 -> 114800 bytes (see fixture copies)

== CHANGED: library/2010-12-25 Winter Holiday/photo03.jpg (27636 -> 28482 bytes)
   image; 27636 -> 28482 bytes (see fixture copies)

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/wordhash.dat — binary; 66872 -> 66904 bytes; first difference at offset 0x10
```
