# Fixture 014: face-tag-new-person

**Action:** Face geometry retry (batch 2) - SUCCESS where 013 stalled. UI: 2x-clicked photo00.jpg in '2015-03-15 Garden Project' -> 'Add a person manually' -> drew box, typed 'synthetic person 1200' in the name field under the box -> confirmed the New Person dialog. Wine doubled a keystroke: stored name is 'synthetiic person 1200' everywhere. Phase 1 (sync, ~1min): ini faces=rect64(6800600097ff9fff),ca5c88ca60f42c0b + new [Contacts2] section 'uid=name;;'; backuphash 3563->600; albums_0.db append. Phase 2 (lazy flush +4min, merged into this capture): contacts/contacts.xml created (contact id=uid, local_contact=1, ISO timestamp w/ TZ); new albumdata row [11] = person album (name, token ']facealbum:11', category=8, albumcontactids NEW pmp uint64=uid); NEW VIRTUAL imagedata row [28] filetype=1001 carrying the face: crop64=facerect=rect64 value, width/height=photo dims (1600x1200), personalbumid NEW pmp=11 (-> person album row), tagdate NEW pmp=action datetime, gets its own thumbindex entry (28->29); photo's own row [2] facerect 0x1 -> rect. Sparse pmps (crop64/rotate) zero-filled to 29 rows. NO JPEG XMP write at all (unlike 013's panel flow which wrote dc:subject synchronously) - contact-dialog flow is ini/db/contacts.xml-side only.

**Captured:** 2026-06-12T03:43:48+00:00

```
baseline: 'after 013-face-tag-manual' (2026-06-11T19:03:45+00:00)
31 file(s) differ (26 semantic, 5 blob/cache)

== ADDED: contacts/contacts.xml (- -> 150 bytes)
   --- before/contacts/contacts.xml
   +++ after/contacts/contacts.xml
   @@ -0,0 +1,3 @@
   +<contacts>
   + <contact id="ca5c88ca60f42c0b" name="synthetiic person 1200" modified_time="2026-06-11T20:40:16-07:00" local_contact="1"/>
   +</contacts>

== ADDED: db3/albumdata_albumcontactids.pmp (- -> 116 bytes)
   type uint64; rows 0 -> 12
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
   [11] (new) 0xca5c88ca60f42c0b

== CHANGED: db3/albumdata_category.pmp (64 -> 68 bytes)
   type uint32; rows 11 -> 12
   [11] (new) 8

== CHANGED: db3/albumdata_date.pmp (108 -> 116 bytes)
   type date(f64); rows 11 -> 12
   [6] 46184.502592592595 (2026-06-11 12:03:44) -> 46184.86140046296 (2026-06-11 20:40:25)
   [11] (new) 46184.861296296294 (2026-06-11 20:40:16)

== CHANGED: db3/albumdata_description.pmp (53 -> 54 bytes)
   type string; rows 11 -> 12
   [6] '24 results: No matches' -> '25 results: No matches'
   [11] (new) ''

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [7] 0x01dcf9d0972f30ab -> 0x01dcfa1d2fff950b

== CHANGED: db3/albumdata_location.pmp (31 -> 32 bytes)
   type string; rows 11 -> 12
   [11] (new) ''

== CHANGED: db3/albumdata_music.pmp (31 -> 32 bytes)
   type string; rows 11 -> 12
   [11] (new) ''

== CHANGED: db3/albumdata_name.pmp (195 -> 218 bytes)
   type string; rows 11 -> 12
   [11] (new) 'synthetiic person 1200'

== CHANGED: db3/albumdata_token.pmp (260 -> 274 bytes)
   type string; rows 11 -> 12
   [11] (new) ']facealbum:11'

== CHANGED: db3/albums_index.db (152 -> 164 bytes)
   binary; 152 -> 164 bytes
   first difference at offset 0x8
     before[0x8:]: 0b00000000000000000000000000000000000000000000000000000000000000
     after [0x8:]: 0c00000000000000000000008205850000000000000000000000000000000000

== CHANGED: db3/bigthumbs_index.db (356 -> 368 bytes)
   binary; 356 -> 368 bytes
   first difference at offset 0x8
     before[0x8:]: 1c000000000000000000000007e4ba6f0784286b0784286b0784286b0784286b
     after [0x8:]: 1d000000000000000000000007e4ba6f0784286b0784286b0784286b0784286b

== CHANGED: db3/imagedata_backuphash.pmp (70 -> 70 bytes)
   type uint16; rows 25 -> 25
   [2] 3563 -> 600

== CHANGED: db3/imagedata_crop64.pmp (212 -> 252 bytes)
   type uint64; rows 24 -> 29
   [24] (new) 0x0000000000000000
   [25] (new) 0x0000000000000000
   [26] (new) 0x0000000000000000
   [27] (new) 0x0000000000000000
   [28] (new) 0x6800600097ff9fff

== CHANGED: db3/imagedata_facerect.pmp (244 -> 252 bytes)
   type uint64; rows 28 -> 29
   [2] 0x0000000000000001 -> 0x6800600097ff9fff
   [28] (new) 0x6800600097ff9fff

== CHANGED: db3/imagedata_filetype.pmp (132 -> 136 bytes)
   type uint32; rows 28 -> 29
   [28] (new) 1001

== CHANGED: db3/imagedata_height.pmp (132 -> 136 bytes)
   type uint32; rows 28 -> 29
   [28] (new) 1200

== ADDED: db3/imagedata_personalbumid.pmp (- -> 136 bytes)
   type uint32; rows 0 -> 29
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
   [24] (new) 0
   [25] (new) 0
   [26] (new) 0
   [27] (new) 0
   [28] (new) 11

== CHANGED: db3/imagedata_rotate.pmp (53 -> 58 bytes)
   type string; rows 24 -> 29
   [24] (new) ''
   [25] (new) ''
   [26] (new) ''
   [27] (new) ''
   [28] (new) ''

== ADDED: db3/imagedata_tagdate.pmp (- -> 252 bytes)
   type date(f64); rows 0 -> 29
   [0] (new) 0.0 (1899-12-30 00:00:00)
   [1] (new) 0.0 (1899-12-30 00:00:00)
   [2] (new) 0.0 (1899-12-30 00:00:00)
   [3] (new) 0.0 (1899-12-30 00:00:00)
   [4] (new) 0.0 (1899-12-30 00:00:00)
   [5] (new) 0.0 (1899-12-30 00:00:00)
   [6] (new) 0.0 (1899-12-30 00:00:00)
   [7] (new) 0.0 (1899-12-30 00:00:00)
   [8] (new) 0.0 (1899-12-30 00:00:00)
   [9] (new) 0.0 (1899-12-30 00:00:00)
   [10] (new) 0.0 (1899-12-30 00:00:00)
   [11] (new) 0.0 (1899-12-30 00:00:00)
   [12] (new) 0.0 (1899-12-30 00:00:00)
   [13] (new) 0.0 (1899-12-30 00:00:00)
   [14] (new) 0.0 (1899-12-30 00:00:00)
   [15] (new) 0.0 (1899-12-30 00:00:00)
   [16] (new) 0.0 (1899-12-30 00:00:00)
   [17] (new) 0.0 (1899-12-30 00:00:00)
   [18] (new) 0.0 (1899-12-30 00:00:00)
   [19] (new) 0.0 (1899-12-30 00:00:00)
   [20] (new) 0.0 (1899-12-30 00:00:00)
   [21] (new) 0.0 (1899-12-30 00:00:00)
   [22] (new) 0.0 (1899-12-30 00:00:00)
   [23] (new) 0.0 (1899-12-30 00:00:00)
   [24] (new) 0.0 (1899-12-30 00:00:00)
   [25] (new) 0.0 (1899-12-30 00:00:00)
   [26] (new) 0.0 (1899-12-30 00:00:00)
   [27] (new) 0.0 (1899-12-30 00:00:00)
   [28] (new) 46184.861296296294 (2026-06-11 20:40:16)

== CHANGED: db3/imagedata_width.pmp (132 -> 136 bytes)
   type uint32; rows 28 -> 29
   [28] (new) 1600

== CHANGED: db3/repository.dat (140 -> 140 bytes)
   binary; 140 -> 140 bytes
   first difference at offset 0x79
     before[0x79:]: 666c6174003100494450657273697374003200
     after [0x79:]: 494450657273697374003200666c6174003100

== CHANGED: db3/thumbindex.db (1429 -> 1460 bytes)
   binary; 1429 -> 1460 bytes
   first difference at offset 0x4
     before[0x4:]: 1c0000005a3a5c7661725c686f6d655c6d6174745c6465765c66617578636173
     after [0x4:]: 1d0000005a3a5c7661725c686f6d655c6d6174745c6465765c66617578636173

== CHANGED: db3/thumbs2_index.db (356 -> 368 bytes)
   binary; 356 -> 368 bytes
   first difference at offset 0x8
     before[0x8:]: 1c00000000000000000000001aecd89c26b5d363fc27b44c76eab57b6316cb3a
     after [0x8:]: 1d00000000000000000000001aecd89c26b5d363fc27b44c76eab57b6316cb3a

== CHANGED: db3/thumbs_index.db (356 -> 368 bytes)
   binary; 356 -> 368 bytes
   first difference at offset 0x8
     before[0x8:]: 1c00000000000000000000001aecd89c26b5d363fc27b44c76eab57b6316cb3a
     after [0x8:]: 1d00000000000000000000001aecd89c26b5d363fc27b44c76eab57b6316cb3a

== CHANGED: library/2015-03-15 Garden Project/.picasa.ini (283 -> 387 bytes)
   --- before/library/2015-03-15 Garden Project/.picasa.ini
   +++ after/library/2015-03-15 Garden Project/.picasa.ini
   @@ -8,3 +8,6 @@
    albums=f07f69266e7760526d5b1553c7f451fa
    [photo00.jpg]
   -backuphash=3563
   +backuphash=600
   +faces=rect64(6800600097ff9fff),ca5c88ca60f42c0b
   +[Contacts2]
   +ca5c88ca60f42c0b=synthetiic person 1200;;

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/albums_0.db — binary; 156184 -> 210792 bytes; common prefix; 54608 bytes appended: 5c0000005c0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000...
   changed: db3/bigthumbs_0.db — binary; 138121 -> 147229 bytes; common prefix; 9108 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb00430006040506050406060506070706080a100a0a09090a140e0f0c1017141818171416161a1d251f1a...
   changed: db3/thumbs2_0.db — binary; 31446 -> 33659 bytes; common prefix; 2213 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/thumbs_0.db — binary; 87277 -> 92580 bytes; common prefix; 5303 bytes appended: ffd8ffe000104a46494600010100000100010000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d...
   changed: db3/wordhash.dat — binary; 67280 -> 67296 bytes; first difference at offset 0x4
```
