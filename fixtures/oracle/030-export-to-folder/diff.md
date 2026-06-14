# Fixture 030: export-to-folder

**Action:** Export Pictures to Folder on a DISPOSABLE clone of the oracle prefix (dosdevices/z: staged library copy; live oracle prefix + live library untouched). Selected whole '2015-03-15 Garden Project' folder (9 photos) -> bottom Export button -> Export-to-Folder dialog (default location C:\users\matt\(My )Pictures\Picasa\Exports, exported-folder name defaulted to source folder name, 'Resize to 800px', quality Automatic, location field is read-only/Browse-only) -> Export. KEY RESULT: an export to the DEFAULT (unwatched) location leaves NO db3 catalog row and NO tree node — verified by ~3 min idle watch + UI screenshot after the action; the only persistent "Exported Pictures" record is the export folder's own .picasa.ini (ini-only, like hide-photo 017). SYNCHRONOUS db3: imagedata_originslow set 0->nonzero u64 for the 8 exported SOURCE photos (rows 3-9,18) — the per-photo slow content hash (same column populated at bake in 019); export reads each source fully to resize it, which computes it. NOISE: albumdata_date[16] 'Search results' activity tick; wordhash grew (search index, blob). OUT-OF-ROOT on-disk artifacts (clone Pictures, not under the harness's db3/albums/library roots, so documented by hand below + ini copied in): a NEW export folder with 9 resized 800px JPEGs, where a filename collision (two source photos both named photo07.jpg — the second is the moved-in Winter Holiday #07 from fixture 016) auto-deduped to photo07-001.jpg; AND that folder gets a .picasa.ini folder-identity stub '[Picasa] / P2category=Exported Pictures / date=<export epoch-day>' (cf the [Picasa] block in folder-description 020, but category 'Exported Pictures').

**Captured:** 2026-06-14T08:32:11+00:00

```
baseline: 'clone-baseline-B (pre-export, noise absorbed)' (2026-06-14T08:21:16+00:00)
3 file(s) differ (2 semantic, 1 blob/cache)

== CHANGED: db3/albumdata_date.pmp (156 -> 156 bytes)
   type date(f64); rows 17 -> 17
   [16] 46187.054085648146 (2026-06-14 01:17:53) -> 46187.062002314815 (2026-06-14 01:29:17)

== CHANGED: db3/imagedata_originslow.pmp (220 -> 220 bytes)
   type uint64; rows 25 -> 25
   [3] 0x0000000000000000 -> 0xf24babcdec09dee2
   [4] 0x0000000000000000 -> 0x9515c8981e1e0736
   [5] 0x0000000000000000 -> 0x98b0bf8e89d33381
   [6] 0x0000000000000000 -> 0x58f17391f8281542
   [7] 0x0000000000000000 -> 0xfdad861c28cbcaba
   [8] 0x0000000000000000 -> 0x5249dd5e750656a8
   [9] 0x0000000000000000 -> 0xdba26f67a339243c
   [18] 0x0000000000000000 -> 0xd7c13171973d9eda

-- blob/cache changes (not copied into fixtures by default):
   changed: db3/wordhash.dat — binary; 66904 -> 66992 bytes; first difference at offset 0x10
```

## Out-of-root artifacts (the export output, captured by hand)

The export landed in the clone's `C:\users\matt\Pictures\Picasa\Exports\` — outside
the harness's watched roots (`db3/`, `Picasa2Albums/`, the staged library), so it is
**not** in the auto-captured `before/after/` diff above. It is preserved under
`after/export-folder/2015-03-15 Garden Project/`:

- **`.picasa.ini`** (59 B) — the export folder's identity stub, written synchronously:
  ```ini
  [Picasa]
  P2category=Exported Pictures
  date=46187.061979
  ```
  Same `[Picasa]` folder-identity block as folder-description **020**, but
  `P2category=Exported Pictures` (vs `Folders on Disk`). `date=` is the export
  instant as an epoch-days float (46187.06 ≈ 2026-06-14). No per-photo sections.
- **`_filelist.txt`** — the 9 exported JPEGs (derived/resized copies, bytes not
  committed). All 9 source photos were resized to 800px. Filename collision:
  the folder held two photos named `photo07.jpg` (the original + the Winter
  Holiday #07 moved in by fixture **016**); the second was auto-deduped to
  `photo07-001.jpg` — same `-NNN` rename rule as the move-duplicate dialog in 016.

## Findings

- **Export = new files + an ini-only category mark; no db3 catalog mirror** when the
  destination is unwatched (Picasa's default `Pictures\Picasa\Exports`). The bead's
  open question ("possible Projects/Exported-Pictures albumdata row") resolves to
  **no row** for the default target — the membership lives solely in the export
  folder's `.picasa.ini` (`P2category=Exported Pictures`), which would only
  materialize a db3 row if that folder were later added to a watched location.
- **`imagedata_originslow` is computed as an export side effect.** Exporting reads
  each source photo in full to resize it, and Picasa stamps the per-photo slow
  content hash (`originslow`, u64) for every source it touched — here all 8 photos
  of the source folder that physically live in it (rows 3-9, 18). Previously seen
  set only at File→Save bake (019); this shows a *read-only* full-decode also sets it.
- **Folder name collisions in the export get `-001` suffixes**, the same dedup
  Picasa applies to in-place move/rename collisions (016).

> Geotag (the other half of bead fauxcasa-zve) is a documented dead end on this
> oracle: View ▸ Places renders no map (mshtml disabled + Picasa 3.9's Maps v3
> tile endpoint retired ~2013), so GPS-via-map cannot be exercised. See the bead's
> feasibility comment. This export fixture is the viable Create/output differential.
