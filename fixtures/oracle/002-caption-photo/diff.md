# Fixture 002: caption-photo

**Action:** Caption a photo from edit view: in '2009-07-04 Beach Day' double-clicked a photo (= photo04.jpg), typed caption in the strip under the photo, Enter, Back to Library. Stored text is 'synthetic aption one' (requested 'synthetic caption one' - one keystroke dropped at input; stored consistently everywhere). Artifacts: caption goes to imagedata_caption.pmp + INTO THE JPEG (XMP dc:description + new IPTC/8BIM block, +899 bytes) - NOT into .picasa.ini; ini only gets backuphash=3224 (= imagedata_backuphash.pmp row 24). Action also triggered a global db flush: new pmps (caption, backuphash, originslow, albumdata_inisync), albums_0.db 4->81200 bytes, albums_index.db created, repository.dat entries reordered, thumbindex.db timestamp updated. Star from fixture 001 still has no pmp representation.

**Captured:** 2026-06-11T15:32:02+00:00

```
baseline: 'after 001-star-photo' (2026-06-11T15:28:51+00:00)
16 file(s) differ (11 semantic, 5 blob/cache)

== CHANGED: db3/albumdata_date.pmp (100 -> 100 bytes)
   type date(f64); rows 10 -> 10
   [6] 46184.322222222225 (2026-06-11 07:44:00) -> 46184.35083333333 (2026-06-11 08:25:12)

== ADDED: db3/albumdata_inisync.pmp (- -> 100 bytes)
   type uint64; rows 0 -> 10
   [0] (new) 0x0000000000000000
   [1] (new) 0x0000000000000000
   [2] (new) 0x0000000000000000
   [3] (new) 0x0000000000000000
   [4] (new) 0x0000000000000000
   [5] (new) 0x0000000000000000
   [6] (new) 0x0000000000000000
   [7] (new) 0x0000000000000000
   [8] (new) 0x01dcf9b6790dd706
   [9] (new) 0x01dcf9b7404a6929

== CHANGED: db3/albums_0.db (4 -> 81200 bytes)
   binary; 4 -> 81200 bytes
   common prefix; 81196 bytes appended: 5f000000630000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000...

== ADDED: db3/albums_index.db (- -> 140 bytes)
   binary; 0 -> 140 bytes
   common prefix; 140 bytes appended: cdcccc3f000000000a00000000000000000000000000000000000000000000000000000000000000000000007b7d1cfb2dd50c8a0a0000000000000000000000...

== ADDED: db3/imagedata_backuphash.pmp (- -> 70 bytes)
   type uint16; rows 0 -> 25
   [0] (new) 0
   [1] (new) 0
   [2] (new) 0
   [3] (new) 0
   [4] (new) 0
   [5] (new) 0
   [6] (new) 0
   [7] (new) 0
   [8] (new) 0
   [9] (new) 0
   [10] (new) 0
   [11] (new) 0
   [12] (new) 0
   [13] (new) 0
   [14] (new) 0
   [15] (new) 0
   [16] (new) 0
   [17] (new) 0
   [18] (new) 0
   [19] (new) 0
   [20] (new) 0
   [21] (new) 0
   [22] (new) 0
   [23] (new) 0
   [24] (new) 3224

== ADDED: db3/imagedata_caption.pmp (- -> 65 bytes)
   type string; rows 0 -> 25
   [0] (new) ''
   [1] (new) ''
   [2] (new) ''
   [3] (new) ''
   [4] (new) ''
   [5] (new) ''
   [6] (new) ''
   [7] (new) ''
   [8] (new) ''
   [9] (new) ''
   [10] (new) ''
   [11] (new) ''
   [12] (new) ''
   [13] (new) ''
   [14] (new) ''
   [15] (new) ''
   [16] (new) ''
   [17] (new) ''
   [18] (new) ''
   [19] (new) ''
   [20] (new) ''
   [21] (new) ''
   [22] (new) ''
   [23] (new) ''
   [24] (new) 'synthetic aption one'

== ADDED: db3/imagedata_originslow.pmp (- -> 220 bytes)
   type uint64; rows 0 -> 25
   [0] (new) 0x0000000000000000
   [1] (new) 0x0000000000000000
   [2] (new) 0x0000000000000000
   [3] (new) 0x0000000000000000
   [4] (new) 0x0000000000000000
   [5] (new) 0x0000000000000000
   [6] (new) 0x0000000000000000
   [7] (new) 0x0000000000000000
   [8] (new) 0x0000000000000000
   [9] (new) 0x0000000000000000
   [10] (new) 0x0000000000000000
   [11] (new) 0x0000000000000000
   [12] (new) 0x0000000000000000
   [13] (new) 0x0000000000000000
   [14] (new) 0x0000000000000000
   [15] (new) 0x0000000000000000
   [16] (new) 0x0000000000000000
   [17] (new) 0x0000000000000000
   [18] (new) 0x0000000000000000
   [19] (new) 0x0000000000000000
   [20] (new) 0x0000000000000000
   [21] (new) 0x0000000000000000
   [22] (new) 0x0000000000000000
   [23] (new) 0x0000000000000000
   [24] (new) 0xaa3f9d1f987c54f0

== CHANGED: db3/repository.dat (140 -> 140 bytes)
   binary; 140 -> 140 bytes
   first difference at offset 0x79
     before[0x79:]: 494450657273697374003200666c6174003100
     after [0x79:]: 666c6174003100494450657273697374003200

== CHANGED: db3/thumbindex.db (1429 -> 1429 bytes)
   binary; 1429 -> 1429 bytes
   first difference at offset 0x430
     before[0x430:]: dd4d00b0f9dc0100000000010000000001ffffffff70686f746f30302e6a7067
     after [0x430:]: fa8df0b5f9dc0100000000010000000001ffffffff70686f746f30302e6a7067

== ADDED: library/2009-07-04 Beach Day/.picasa.ini (- -> 32 bytes)
   --- before/library/2009-07-04 Beach Day/.picasa.ini
   +++ after/library/2009-07-04 Beach Day/.picasa.ini
   @@ -0,0 +1,2 @@
   +[photo04.jpg]
   +backuphash=3224

== CHANGED: library/2009-07-04 Beach Day/photo04.jpg (77164 -> 78063 bytes)
   image; 77164 -> 78063 bytes (see fixture copies)

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/bigthumbs_0.db — binary; 116637 -> 116637 bytes; first difference at offset 0x17fa2
   changed: db3/previews_0.db — binary; 360844 -> 360844 bytes; first difference at offset 0x4a00a
   changed: db3/thumbs2_0.db — binary; 28483 -> 28483 bytes; first difference at offset 0x6c6b
   changed: db3/thumbs_0.db — binary; 76755 -> 76755 bytes; first difference at offset 0x11a11
   changed: db3/wordhash.dat — binary; 67132 -> 67184 bytes; first difference at offset 0x10
```
