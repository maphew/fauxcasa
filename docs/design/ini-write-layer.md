# Sidecar-first `.picasa.ini` write layer

**Status:** design deliverable for fauxcasa-lgg.1 (blocks lgg.2 stars
writer and lgg.4 writer implementation; lgg.3 journal, lgg.5 to lgg.8 build
on it). Written 2026-09-16. **Decision state: proposed**, owner
review-by-argument; the calls most worth fighting over carry **⚖ argue**
markers. Parent commitments: spec §3 (three tiers, star authority,
concurrency), spec §4 (write the durable layer Picasa-acceptably; acceptance
is differential), spec §5 P1 (staged in sidecars, explicit make-permanent),
N3, N5, N7, §7 ("star/caption → durable" row), and the architecture
review's section 8.1 (the write layer is the spine). File and line
references are to `apps/desktop-python/` and `scripts/` at main `40b89b7`.
The skeleton that accompanies this note is `apps/desktop-python/inisidecar.py`
with `apps/desktop-python/test_inisidecar.py`.

## Plain-language summary (for the owner)

Today nothing in Fauxcasa writes to a photo library. The next year of
features (stars, captions, keywords, hide, albums, sort, edits) all need
one thing first: a safe way to change a `.picasa.ini` file so that Picasa
still reads it, nothing the user or Picasa put there is lost, and a failed
write is impossible to miss. This note pins that one path. The calls, in
plain words:

1. **We change only the lines we mean to change.** Every other byte of the
   file, including junk Picasa left behind, comes back out exactly as it
   went in. This is checked after every write by re-reading the file.
2. **A write is all-or-nothing.** We write a temporary file next to the
   original, flush it to disk, then swap it in. A crash leaves either the
   old file or the new file, never a half-written one.
3. **We never overwrite what we have not just read.** If the file changed
   under us since we read it (Picasa was open, a sync client touched it),
   the write is refused and reported, not forced.
4. **A read-only file is refused, not worked around.** Picasa silently
   stopped writing forever in that case; we say so within one action and
   keep the action queued so it retries when the file is writable again.
5. **What Picasa has no line for lives in our own file beside the ini**,
   never in the cache: exact star counts, reject flags, per-folder sort.
   Deleting every cache and rescanning loses nothing.
6. **Every action is written to a journal before the UI says "done".** If
   the app dies between the journal and the ini, the next start finishes
   the job.

## 1. Where this sits

```
UI action (star, caption, hide, album, sort, ...)
   |
   v
librarystate.py           the only caller of the writer; owns the journal,
   |                      the in-memory catalog update and the status marks
   |-- journal.append()   (lgg.3, <home>/.fauxcasa/journal.jsonl, fsync)
   |-- inisidecar.write_edits()      Picasa-compatible keys   (this note)
   |-- nativesidecar.write_records() tier-2 keys              (lgg.4)
   |-- catalog.ini_sigs[...] = result.sig ; photo.<field> = value
   '-- journal.mark_applied()
```

`inisidecar.py` is pure stdlib, no Qt, no catalog import. It knows how to
read, edit and verify one ini file. It does not know what a star is. The
feature writers (lgg.2, lgg.5, lgg.7) map a user action to a list of
`IniEdit`s and a list of native records; `librarystate.py` sequences them.

## 2. The document model: lines, not sections

`read_picasa_ini` (`scripts/picasa_db.py:643`) is the right *reader* and
stays the reader for ingest and for verification. It cannot be the writer's
model, because it drops what a writer must keep: blank lines are skipped
(`picasa_db.py:709`), CRLF is stripped per line (`:707`) and not recorded,
junk lines survive only as line-number anomalies.

The writer therefore keeps a `list[Line]`, one entry per physical line,
each holding the line's text and its own end-of-line bytes (`\r\n`, `\n`,
or nothing at end of file). A line is classified once, with the same rules
the reader uses, as a `header` (`[name]`), a `pair` (`key=value`, split on
the first `=`, key stripped, value verbatim), or `other` (blank, junk,
byte-reversed garbage, `[(null)]` contents). Unmodified lines are emitted
byte-for-byte. A rewritten line (an existing key's value changed) is
emitted as `key=value` plus **that line's own original end-of-line
bytes** -- a rewrite changes only the value, nothing else, so a
mixed-EOL file changes no bytes outside the target line. An added line
(a new key, a new section's header, or the one EOL the "no trailing EOL"
case adds so an appended line has something to follow) uses the
document's end-of-line style, since it has no original EOL to keep.

Decisions:

- **EOL style** is the file's majority style; a new file uses `\r\n`
  because Picasa does (`docs/research/picasa-ini-format.md:11`, every
  oracle fixture). A file whose last line has no EOL gets one added when
  we append after it; this is the one byte we change outside our own
  lines, and the verification step allows exactly it.
- **Duplicate sections and duplicate keys** (real, from crashed Picasa
  writes; format doc "Robustness") are edited *everywhere*: `set` rewrites
  every `key=` pair in every section of that name, `remove` deletes every
  one. Readers disagree on first-wins versus last-wins, so the only edit
  that every reader agrees on is one that leaves no disagreement. **⚖
  argue**: the alternative, edit the last occurrence only, is smaller but
  leaves a first-wins reader (Windows `GetPrivateProfileString`) seeing the
  old value.
- **Placement**: after a `set`, *every* run of the named section carries
  exactly that `key=value`: runs that had the key are rewritten in place,
  runs that lacked it get the pair inserted after their last `pair` line,
  before any trailing blank or junk lines. This keeps the duplicate rule
  honest for a first-wins reader too (a key added only to the last run
  would be invisible to it). A new section is appended at the end of the
  file. Section order is arbitrary to Picasa (format doc line 12), so
  appending is compatible and keeps diffs small.
- **No-op writes are free**: if the emitted bytes equal the bytes read,
  `write_edits` returns success without creating a temp file, touching
  the mtime, or disturbing `ini_sigs`. Starring an already-starred photo
  and every journal replay of an entry that already landed cost nothing
  and trigger no rescan.
- **Values are literal**: no quoting, no escaping, `=` allowed in values.
  A value containing `\r` or `\n`, a key containing `=` or starting with
  `[`, or a section name containing `]` is refused before any file is
  touched (`IniWriteError(kind="value")`).
- **Empty sections are fine to leave behind**: `remove` of the last key in
  a `[photo.jpg]` section leaves the header; Picasa leaves stale sections
  routinely (format doc "Stale sections are normal") and the reader
  tolerates them. Removing the header would be a second guess about
  Picasa's own file.

## 3. Encoding

The reader's three-way policy (`picasa_db.py:686-697`, landed by cam.14)
is the writer's too, and the writer records which branch it took:

| Read branch | Write with | Notes |
|---|---|---|
| strict UTF-8 succeeded | `utf-8` | byte-exact; a BOM is recorded as a document flag and re-emitted, never treated as part of the first line |
| UTF-8 failed, `[encoding] utf8=1` present | `utf-8` + `surrogateescape` | every original byte survives; our new values are plain UTF-8 |
| UTF-8 failed, no marker | `cp1252` | byte-exact where cp1252's map is total; when even cp1252 rejects a byte the document is tagged `legacy-surrogateescape`, distinct from the marked branch above, and any non-ASCII new value is refused (`kind="encoding"`) |

Two edge rules:

- A new value that `cp1252` cannot encode in a legacy file **upgrades the
  file to UTF-8**: the whole file is transcoded and an `[encoding]` section
  with `utf8=1` is added as the first lines. This is the one case where
  every line changes. It is reported in the result (`upgraded_encoding=True`)
  so the caller can log it, and it is refused (`kind="encoding"`) when the
  legacy file had undecodable bytes (the `surrogateescape` fallback inside
  the cp1252 branch), because those bytes have no UTF-8 meaning to
  transcode. **⚖ argue**: the alternative is to write the value
  `cp1252`-lossy (replace characters); Picasa would show `?` where the
  user typed an emoji, and the verification step would fail its own
  round-trip. Refusing is honest; upgrading is what Picasa 3 itself does to
  a Picasa 2 file it rewrites.
- A **new file** is UTF-8. It gets the `[encoding]` `utf8=1` section only
  when its bytes are not all ASCII (fixture 001 shows Picasa writes a bare
  `[photo02.jpg]` `star=yes` for the ASCII case).

## 4. The write itself

```python
def write_edits(folder: Path, edits: Sequence[IniEdit], *,
                expected_sig: tuple[int, int] | None = None,
                ) -> WriteResult
```

Steps, in order, each with its failure kind:

1. **Pick the file.** The same `INI_NAMES` selection `catalog._select_ini_variant`
   uses (`catalog.py:648`), so a folder with a legacy `Picasa.ini` gets its
   edits in the file the reader and Picasa read. No ini at all: the target
   is `.picasa.ini` and `created=True`.
2. **Refuse read-only up front** (`kind="readonly"`). `os.access(path, W_OK)`
   is false for a Windows read-only attribute and for a POSIX file without
   owner write; the folder must also be writable for the temp file, and
   for a to-be-created ini only the folder is checked. A Windows DACL
   denial is not visible to `os.access` and surfaces at step 5 or 7 as
   `kind="io"`; the status mark treats both kinds the same way.
   Measured on the dev box: `os.replace` onto a read-only target fails with
   `WinError 5`, while on POSIX it would silently succeed because the
   directory permits it. Refusing on both makes N7's test ("make a sidecar
   read-only, star the photo, the failure is visible within one action")
   behave the same on every platform.
3. **Stat, then read** (`stat_sig` order, `catalog.py:724-736`): the
   signature is taken before the bytes are read, never after. If
   `expected_sig` is given and differs from the fresh stat, refuse with
   `kind="drift"` before reading further. `librarystate` passes the
   catalog's `ini_sigs` entry for the folder for a live user action, so a
   Picasa-side edit since the last scan is caught here, not overwritten.
   A journal replay passes `expected_sig=None`: a journal entry is a
   desired end state, not a compare-and-swap, and the line model already
   confines the write to our own keys (the spec's per-key last-writer-wins
   merge, §3 Concurrency). Otherwise a crash between the ini write and
   the next `save_catalog` would leave a stale `ini_sigs` that refuses
   every replay forever.
4. **Decode, classify, apply** (sections 2 and 3), then **check the
   payload in memory before any file is touched**: every line not in the
   edit set is byte-identical to the original (the only allowed extra
   byte is the trailing EOL of section 2); `read_picasa_ini` over the
   payload bytes shows every edit landed in every run of its section (or
   absent, for removals) and the anomaly count did not grow. A failure
   here is `kind="verify"` and costs nothing, which is the point: a
   placement or encoding bug becomes a refusal, never a corrupt file. If
   the payload equals the original bytes, return success now (no-op).
5. **Write the temp file** `<name>.<pid>.tmp` in the same directory
   (the `_write_library_config` idiom, `main.py:450`), `flush`, then
   `os.fsync` on the file. The §7 row says durable means fsync'd; every
   existing writer in the tree skips this (none of `starstore.py:83`,
   `library.py:436`, `catalog.py:2044` fsync), which is fine for caches and
   wrong for the user's own state.
6. **Re-check the file** against step 3 immediately before the swap,
   using `st_mtime_ns` and size (the catalog's second-granularity `stat_sig`
   is too coarse for a same-second rewrite) and, when they match, a byte
   compare against the bytes read; a change means someone wrote in the
   last few milliseconds: remove the temp, refuse with `kind="drift"`. The
   window between this check and the swap is accepted under the spec's
   alternation protocol (§3 Concurrency, `product-spec.md:330-340`).
7. **Swap** with `os.replace`. On Windows the target's attributes are
   captured before the swap and re-applied after it: measured on the dev
   box, replacing a hidden+system `.picasa.ini` succeeds but leaves the
   result with only the archive attribute, which would make every ini
   Picasa had hidden pop into view in Explorer. On POSIX the directory is
   fsync'd after the swap; Windows has no directory fsync (measured:
   `EACCES`), and `MoveFileEx` with replace is the journaling filesystem's
   own atomic rename.
8. **Read back** (`kind="verify"`): the bytes on disk must equal the
   payload (the parse-level checks already ran in step 4 on the same
   bytes). The result carries the post-swap `stat_sig`, which is the
   value the caller stores in `catalog.ini_sigs`.
9. Any `OSError` on the way is `kind="io"`; the temp file is removed on
   every failure path. `IniWriteError` carries `kind`, `path`, and a
   `detail` string that is safe to show in the UI and to log: paths and
   kinds, never values (privacy rule for real libraries).

`WriteResult`: `path`, `sig`, `created`, `upgraded_encoding`,
`bytes_written`. No return value on a refusal, only the exception, so a
caller cannot forget to check.

## 5. Keeping the catalog honest about our own writes

Two things go wrong if the writer and the catalog do not agree, both
listed in the bead: a full re-ingest after every star, or a stale read of
our own write on the next warm start.

- The catalog's freshness signal for a folder's ini is `ini_sigs[(root_id,
  folder_rel)]` (`catalog.py:458`), compared by `reconcile_walk`
  (`catalog.py:2405-2411`); any mismatch anywhere under a root sets
  `Drift.ini_changed`, which `main.py:2784-2815` answers with a full
  background `scan_library` rebuild for a single-root library. So after a
  verified write, `librarystate` must (a) set `ini_sigs[...] = result.sig`
  and (b) update the in-memory `Photo` field the same way ingest would
  (`catalog.py:1414-1418` for star, caption, keywords). Then the next
  reconcile sees no drift and the grid shows the new state without a
  rescan.
- The updated `ini_sigs` reach disk with the next `save_catalog`. A crash
  before that costs one background rebuild on the next start, not data:
  the ini on disk already holds the write. That is the right trade;
  saving a 100k-photo catalog after every star is not.
- Ordering inside one action: journal append, ini write, native write,
  in-memory catalog update, journal applied mark. The in-memory update
  happens only after the write verifies, so the grid never shows a star
  the disk does not have (N7).
- **Drift recovery is folder-local**, not a library rebuild. `main.py`'s
  answer to `Drift.ini_changed` is a full rescan only for a single-root,
  non-adopt library; for multi-root and adopt-mode catalogs it keeps the
  indexed snapshot and only posts a notice (`main.py:2803-2818`), so
  `ini_sigs` would never refresh and a pending entry would stay wedged.
  On a `drift` refusal `librarystate` therefore re-reads that one
  folder's ini through `catalog._read_folder_ini`, refreshes its
  `ini_sigs` entry and the in-memory `Photo` fields (per-key
  last-writer-wins, with the surfaced reconciliation note §3 asks for),
  and retries the pending entry once with the fresh signature.

## 6. Tier-2 native state placement

The spec's §3 table puts per-photo app state in "per-folder sidecars for
per-photo state (per-photo records within the folder file, merged per
record), a library-home directory for library-level state"
(`product-spec.md:276-323`). Today's `library_state_dir` (`main.py:641`)
is a cache directory, so `stars.json` there is the N3 violation the review
names (section 6.1). Placement:

| State | File | Notes |
|---|---|---|
| exact star count 0..5, reject flag, per-photo sort key, ignored faces | `<folder>/.fauxcasa.json`, one record per photo, keyed by file name and carrying the photo's content hash | travels with a folder copy; `star=yes` in the ini stays authoritative for zero versus non-zero (§3 star authority); the hash lets reconcile follow an OS-level rename or move (N6 gate a) the way the catalog already does |
| album order, manual folder sort mode, people registry, watch config | `<home>/.fauxcasa/<name>.json` | library-level; `library.py:40` already defines the directory |
| window geometry, view mode, star threshold, remembered library | `library_state_dir` (cache root) | machine preferences, disposable |

The native sidecar is written by the same steps as section 4 (stat before
read, temp, fsync, drift check, swap, read-back), through a JSON document
instead of a line model; the atomic-swap core is one shared function. The
record for a photo is replaced, never the file's other records, so a
Picasa-era folder copy that carries two different `.fauxcasa.json`s merges
by file name, and a record whose hash matches a photo now living under
another name is re-attached to it on reconcile. Format: `{"format": 1,
"photos": {"IMG_0001.jpg": {"hash": "<hex>", "stars": 3}}}`, sorted keys,
indent 1, UTF-8, `\n`. The catalog keeps a freshness signature for the
native sidecar exactly as it does for the ini (`native_sigs` beside
`ini_sigs`, same stat-before-read rule), so a folder copied in with its
own `.fauxcasa.json` is noticed on reconcile; without that the placement
would buy nothing. The file joins the release-notes inventory too; of
everything Fauxcasa writes it is the one that lands in the user's own
photo folders. **⚖ argue**: the file name
hard-codes the provisional project name, against the `project-name-provisional`
rule; it is one constant (`NATIVE_SIDECAR_NAME`) next to `LIBRARY_DIR`,
and a rename is a one-line migration plus a rescan. Picking a neutral name
now (`.photostate.json`) would avoid the migration; the owner's call.

`stars.json` migrates into the library in lgg.5: on first open after the
upgrade, every override is written through this layer (star count to the
native sidecar, `star=yes` to the ini), then the cache copy is renamed
`stars.json.migrated`.

## 7. The journal (contract for lgg.3)

`<home>/.fauxcasa/journal.jsonl`, append-only, one JSON object per line,
`os.fsync` after every append, before the UI acknowledges the action.
Entry: `{"seq": 17, "ts": "2026-09-16T21:04:11Z", "action": "star",
"root_id": "1a2b3c4d", "rel": "2010/IMG_0001.jpg", "value": 3}`. Applied
marks are their own appended lines, `{"seq": 17, "applied": true}`, never
an edit of an earlier line. On start, `librarystate` replays every entry
without an applied mark, in `seq` order, through the same writer with
`expected_sig=None` (section 4 step 3), so an entry that already landed
is a free no-op and one that did not is applied per key; a `readonly`
or `io` refusal leaves the entry pending and marks the
folder's health (section 8), so the journal is also the retry queue the
review asks for. When every entry is applied and the file exceeds 256 KiB,
it is rotated by writing an empty journal with the atomic swap. The
journal file joins the release-notes inventory ("What Fauxcasa writes on
your computer").

A legacy single-root library has no home (`library.py:67-75`,
`is_legacy`). **The first write promotes it** with `promote_library`
(`library.py:776`, a rename, not a re-index) to the in-root default home,
so the journal has a place before the first action lands. **⚖ argue**:
this creates `<root>/.fauxcasa/` on the first star, which a user who chose
Fauxcasa for its read-only past may not expect; the alternative, journal
in the cache until promotion, loses pending actions on a cache wipe, which
is exactly the N5 failure. Surface it once ("Fauxcasa keeps its notes in
`<root>/.fauxcasa`"), and honour the existing read-only-root path (home
outside the root).

## 8. Status surface (N7)

`librarystate` keeps a per-folder health mark, `dict[(root_id,
folder_rel), FolderHealth]`, with `kind` in `readonly`, `drift`, `io`,
`encoding`, `verify` and the refusal's `detail`. The sidebar folder row
and the status bar show the mark; the import-notes dialog lists the
refusals. A pending journal entry for the folder keeps the mark alive
until a replay succeeds. This note only fixes the vocabulary; the UI is
lgg.5's.

## 9. Test strategy

Three layers, each mechanical to run:

1. **Unit, `test_inisidecar.py`** (ships with the skeleton, most tests
   `xfail(strict=True)` until lgg.4 lands the bodies): byte round-trip on
   every oracle fixture ini (`fixtures/oracle/*/after/library/**/.picasa.ini`)
   with zero edits produces identical bytes; each edit kind on CRLF and LF
   files; duplicate sections and keys; BOM; `utf8=1` with a stray high
   byte; cp1252 legacy; the encoding upgrade; the missing-trailing-EOL
   case; refusal on read-only (chmod on POSIX, attribute on Windows);
   refusal on drift (touch the file between read and write via a hook);
   verify failure when a hook corrupts the temp; temp-file cleanup on every
   failure; Windows attribute preservation; new-file creation with and
   without the encoding section; value and key validation.
2. **Golden fixtures**: for each write feature, a `fixtures/oracle/NNN-*`
   entry captured from real Picasa is the *expected* ini for the same
   action performed through Fauxcasa (fixture 001 star, 017 hide, 003
   album). `scripts/check-ingest-parity.py` gains a `--writes` mode that
   performs the action through the writer on the fixture's `before/`
   library and diffs against `after/`, key by key, order-insensitive.
3. **Oracle differential** (spec §4, the M2 gate, Linux box with the Wine
   oracle): write through Fauxcasa, launch Picasa, snapshot with
   `scripts/oracle-diff.py`, confirm Picasa shows the state and neither
   rewrites nor rejects the file. Recipe in `docs/research/wine-oracle.md`.
   One entry per write feature, recorded in the bead that lands it.

And the two spec gates this layer serves: N7 (read-only sidecar, star,
visible within one action) is a `test_tracer` GUI test once lgg.5 wires the
status mark; N5 (kill-fuzzer over journal and writer) is lgg.8.

## 10. What this note does not decide

The exact wire shape of albums in the ini beyond what fixture 003 shows
(lgg.7); how a Fauxcasa-only edit operation is spelled when Picasa has no
`filters=` grammar for it (the review's 8.2, the differential harness
decides); in-file XMP/IPTC writes (§5 P1 make-permanent, M4); anything to
do with db3, which is never written (§4).
