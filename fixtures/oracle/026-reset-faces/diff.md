# Fixture 026: reset-faces

**Action:** Remove face tag via right-click photo > Reset Faces, on Garden Project/photo00 (the only face-tagged photo, from 014). Agent-driven on headless :3; no confirm dialog. SYNCHRONOUS: photo00 ini 'faces=rect64(6800600097ff9fff),ca5c88ca60f42c0b' line REMOVED; backuphash bumped 600->22344 (the photo's ini was rewritten). The photo's own face rectangle is gone. PRESERVED -- this is NOT a full teardown of 014, it is a per-photo UNTAG: the ini '[Contacts2]' line (ca5c88...=synthetiic person 1200;;) REMAINS; contacts/contacts.xml UNCHANGED (contact 'synthetiic person 1200' id ca5c88ca60f42c0b, modified_time still 2026-06-11); the People/person tree entry (People (1)) REMAINS; and the VIRTUAL face imagedata row (imagedata_filetype[28]=1001, the face-crop record from 014) REMAINS. So Reset Faces strips a photo's face rect but keeps the contact + recognition infrastructure for reuse (a full contact teardown would be a separate 'delete person' action, not available from the photo's grid context menu). INCIDENTAL (not part of reset): recurring 'Search results' albumdata row reshuffle (row[15] tombstoned w/4501 sentinel -> new row[16]). NB: grid photo context menu offers Reset Faces but no per-person untag (that needs single-photo edit + People panel) and no delete-contact.

**Captured:** 2026-06-13T22:51:57+00:00

```
baseline: 'manual' (2026-06-13T22:40:41+00:00)
18 file(s) differ (17 semantic, 1 blob/cache)

== CHANGED: db3/albumdata_category.pmp (84 -> 88 bytes)
   type uint32; rows 16 -> 17
   [16] (new) 0

== CHANGED: db3/albumdata_date.pmp (148 -> 156 bytes)
   type date(f64); rows 16 -> 17
   [15] 46186.64570601852 (2026-06-13 15:29:49) -> 949998.0 (4501-01-01 00:00:00)
   [16] (new) 46186.66101851852 (2026-06-13 15:51:52)

== CHANGED: db3/albumdata_description.pmp (227 -> 250 bytes)
   type string; rows 16 -> 17
   [16] (new) '16 results: No matches'

== CHANGED: db3/albumdata_filename.pmp (195 -> 196 bytes)
   type string; rows 15 -> 16
   [15] (new) ''

== CHANGED: db3/albumdata_inisync.pmp (108 -> 108 bytes)
   type uint64; rows 11 -> 11
   [7] 0x01dcfb84265739a0 -> 0x01dcfb86f0e54240

== CHANGED: db3/albumdata_location.pmp (36 -> 37 bytes)
   type string; rows 16 -> 17
   [16] (new) ''

== CHANGED: db3/albumdata_music.pmp (36 -> 37 bytes)
   type string; rows 16 -> 17
   [16] (new) ''

== CHANGED: db3/albumdata_name.pmp (185 -> 186 bytes)
   type string; rows 16 -> 17
   [15] 'Search results' -> ''
   [16] (new) 'Search results'

== CHANGED: db3/albumdata_token.pmp (200 -> 201 bytes)
   type string; rows 16 -> 17
   [15] ']search' -> ''
   [16] (new) ']search'

== CHANGED: db3/albumdata_uid.pmp (323 -> 324 bytes)
   type string; rows 15 -> 16
   [15] (new) ''

== CHANGED: db3/imagedata_backuphash.pmp (70 -> 70 bytes)
   type uint16; rows 25 -> 25
   [2] 600 -> 22344

== CHANGED: db3/imagedata_facerect.pmp (252 -> 252 bytes)
   type uint64; rows 29 -> 29
   [2] 0x6800600097ff9fff -> 0x0000000000000001

== CHANGED: db3/imagedata_filetype.pmp (136 -> 136 bytes)
   type uint32; rows 29 -> 29
   [28] 1001 -> 0

== CHANGED: db3/imagedata_personalbumid.pmp (136 -> 136 bytes)
   type uint32; rows 29 -> 29
   [28] 11 -> 0

== CHANGED: db3/imagedata_tagdate.pmp (252 -> 252 bytes)
   type date(f64); rows 29 -> 29
   [28] 46184.861296296294 (2026-06-11 20:40:16) -> 46186.65957175926 (2026-06-13 15:49:47)

== CHANGED: db3/thumbindex.db (1301 -> 1301 bytes)
   binary; 1301 -> 1301 bytes
   first difference at offset 0x507
     before[0x507:]: 01000000e9030000000102000000
     after [0x507:]: 00000000000000000000ffffffff

== CHANGED: library/2015-03-15 Garden Project/.picasa.ini (393 -> 346 bytes)
   --- before/library/2015-03-15 Garden Project/.picasa.ini
   +++ after/library/2015-03-15 Garden Project/.picasa.ini
   @@ -2,6 +2,5 @@
    [photo02.jpg]
    [photo00.jpg]
   -backuphash=600
   -faces=rect64(6800600097ff9fff),ca5c88ca60f42c0b
   +backuphash=22344
    [Contacts2]
    ca5c88ca60f42c0b=synthetiic person 1200;;

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/wordhash.dat — binary; 66884 -> 66876 bytes; first difference at offset 0x101ec
```
