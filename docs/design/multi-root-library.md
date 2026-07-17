# Multi-root library model

**Status:** design deliverable for fauxcasa-ed5.7 (blocks .a through .g,
the implementation sub-beads). Written 2026-07-03. **Decision state:
proposed** — owner review-by-argument; the calls most worth fighting over
carry **⚖ argue** markers. Parent commitments: spec §3 (Library definition,
File identity N6, Volume remounting), spec §7 (rebuild gate N3). Recon
inventory is in the worktree at this branch; file/line references below are
to `apps/desktop-python/` and `scripts/` in the
`docs/multiroot-design` worktree.

## Plain-language summary (for the owner)

Today Fauxcasa works with one folder at a time. A photo archive that lives
across two drives — or a family's Picasa library that spans four watched
folders — cannot be opened as one library. This document is the blueprint
for fixing that, with one design constraint above all others: **opening a
library you already have today must cost nothing**, and **adding a second
root must never re-scan or re-hash what already exists**.

The important calls, in plain words:

1. **An existing single-root library is untouched until the user does
   something that requires a home.** Day one after the update: the app
   behaves byte-identically to the version before it. No migration runs,
   no prompts appear, no caches are rebuilt. The old path-keyed cache
   digest is preserved exactly.
2. **Promotion is a rename, not a re-index.** When the user adds a
   second root for the first time, Fauxcasa mints a library identity, renames
   one cache directory, and additively rewrites only the header lines of
   `catalog.json`. Photo rows are untouched. No re-walk, no re-hash, no
   thumbnail rebuild. Worst case on any failure: delete `.fauxcasa/` and
   the app falls back to the original cold walk.
3. **Picasa users have a direct onboarding path.** Fauxcasa can read the
   Picasa watched-folders list from the Windows registry
   (`HKCU\Software\Google\Picasa\...`) or from a manually-selected list,
   create a new library-home, add each watched folder as a root, and scan.
   Because `.picasa.ini` data lives beside the photos, per-root scanning
   needs no changes to ini parsing.
4. **Each root gets its own thumb cache file.** Adding or removing a root
   never touches another root's cache. The parity invariant (entry *i* in
   the fcache = photo *i* in the catalog) becomes N independent
   per-root invariants, each testable in isolation, exactly like today's
   single-root invariant. ⚖ This is the highest-value graft from the
   competing designs; it is argued fully in §3.
5. **Unplugging a drive does not lose data.** Offline roots' photos stay
   in the catalog and their thumbnails still render from the per-root
   fcache. Reconcile skips offline roots and never treats their entries
   as deletions. This is the single most important rule for multi-volume
   safety.
6. **Volume UUIDs self-heal drive-letter changes.** A root on a
   removable drive that remounts at a different letter is found
   automatically. Volume UUID support is deferred past the minimal slice
   (see §8 and phasing §14), but the resolution chain is designed so UUID
   support slots in without changing any other format.
7. **The binary thumb cache format is frozen forever.** Version 1 and
   version 2 read paths are unchanged. The shipped benchmark cache remains
   byte-reproducible. This is a hard constraint, not a preference.

## Scope

Designed here: the library model, library-home, `library.json` format,
per-root fcache files, the identity key extension (`root_id`), walk rule
with N roots, catalog and sidecar format changes, cache-dir keying,
promotion journey, offline-root semantics, volume UUID resolution chain,
reconcile with N roots, the adopt-mode script extension, UI implications,
and a phased bead-sized implementation plan.

Explicitly not here: the cross-root move-detection algorithm (M2, flagged
in §9 and the open questions), album order and view-prefs migration to the
library home (M2 REVISIT already noted at `main.py:250-263`), flat view
across roots (out of brief scope), hardware-accelerated volume enumeration
beyond what `volumes.py` provides, and any implementation code — this
bead is design-first, no code changes.

## 1. The two journeys (design drivers)

**Journey A — upgrade in place.** User has `D:/Photos` open today; caches
live at `cache_root/<sha256(D:/Photos)[:16]>/` containing `catalog.json`,
`thumbs.fcache`, `thumbs.fcache.json`, `config.json`, `import-report.json`.
They click "Add folder to library…" for the first time. This triggers
**promotion** (§10): mint a library-home, rename the cache dir, additively
rewrite `catalog.json`'s header. No re-walk, no re-hash, no thumbnail
rebuild. Reversible by deleting `.fauxcasa/` — worst case is a cold walk,
which N3 guarantees is lossless.

**Journey B — Picasa import.** User points Fauxcasa at their Picasa
watched-folders list (read from the Windows registry or picked manually).
Fauxcasa creates a fresh library-home, adds each watched folder as a root,
and scans. Because `.picasa.ini` data is folder-relative, per-root scanning
needs zero changes to ini parsing — Picasa's own model is already "state
lives beside the photos," which is exactly why multi-root composes cleanly.

Every format and code decision below is derived from what these two
journeys require, and nothing more for M1.

## 2. Library model and library-home

**A library = one library-home directory + N watched roots** (spec §3).
Concretely:

- The library-home is a directory containing `.fauxcasa/library.json`
  (plus, from M2, tier-2 state: album order file, etc.). It is the
  open-by-path handle: `fauxcasa <path>` where `<path>` is the home dir,
  any watched root containing `.fauxcasa/`, or (legacy) a bare photo dir.
- **Location rules:**
  - Default on creation or promotion: **inside the first watched root** —
    `<root>/.fauxcasa/library.json`. This satisfies N3 ("durable state in
    the library"): back up the root, you back up the library definition.
  - If the first root is not writable (read-only NAS family archive — a
    first-class case), the home may live **outside all roots**; the user
    picks a location. `library.json` then carries the roots by reference
    exactly the same way.
  - The walk rule must exclude `.fauxcasa/` — add it to the
    stash/exclusion filter next to `_is_stashed` in `catalog.py` and the
    twin filter in `scripts/make-thumbcache.py`. **These two changes must
    land in the same commit** (§4 twin-rule invariant).
- **First-run creation: never implicit.** A bare dir opened by path is an
  **implicit legacy library** (see §10) and stays that way until the user
  does something that requires a home (adds a second root, or explicitly
  "New Library…"). This keeps Journey A's day-zero behavior bit-identical.

**`library.json` (format 1):**

```json
{
  "format": 1,
  "library_id": "9f3c1a2e-...-uuid4",
  "name": "Family Archive",
  "roots": [
    {"id": "a1b2c3d4", "path": "D:/Photos",
     "volume_uuid": "\\\\?\\Volume{...}", "vol_rel": "Photos",
     "label": "Photos (D:)"},
    {"id": "5e6f7a8b", "path": "E:/Archive/Scans",
     "volume_uuid": null, "vol_rel": null,
     "label": "Scans"}
  ]
}
```

Written with the same write-temp-rename discipline as `save_catalog`. The
roots list order is durable and load-bearing (§4). New module:
`apps/desktop-python/library.py` — `LibraryRoot`, `LibraryConfig`,
`load_library(path)`, `save_library(cfg)`, `mint_root_id()`,
`resolve_open_path(path)`.

**Root-id minting:** 8 lowercase hex chars from `uuid4`, minted when a
root is added, unique within the library, **never derived from the path**
(N6: nothing keyed to drive letters or mount points), never reused even
after root removal so stale sidecars cannot alias. `library_id` is a full
uuid4 minted at home creation.

**In-tree root marker (optional, for id recovery):** each root may carry a
`.fauxcasa-root` file containing its `root_id`. This is precedented by
Picasa's per-folder markers and enables id recovery if `library.json` is
lost or a root is re-adopted after removal. The marker is written on root
addition, read on adopt-on-remount, and never required — its absence is
not an error.

**Roots snapshot inside catalog.json:** the catalog header (§5) also
records `roots` as a redundancy against `library.json` loss. The snapshot
is informational: `library.json` is the resolution authority; the snapshot
lets recovery tooling reconstruct `library.json` from a catalog alone.

## 3. Per-root fcache files ⚖

**Design choice: `thumbs-<root_id>.fcache` per root, not one concatenated
`thumbs.fcache`.** ⚖ This is grafted from the competing designs and is the
highest-value structural change from the original winning design.

Arguments for per-root files:

- The parity invariant (entry *i* = photo *i*) decomposes into N
  independent copies of today's exact invariant. Each is testable exactly
  as today's single-root invariant is tested — no new testing concepts.
- Root add/remove/reorder never invalidates another root's cache. Adding a
  root appends a new `thumbs-<id>.fcache`; removing a root leaves the
  others untouched. Design 3's "reorder = full rebuild" risk item (its
  risk 6) disappears entirely.
- The N-mmap cold-start concern (open questions §13) is per-root file, not
  global pages — the OS maps only the roots actually opened, and an offline
  root's fcache is opened read-only for browse, not memory-mapped into the
  walk.
- `bind()` in `thumbcache.py:208-224` runs per root independently: compare
  `cache.files` against `[p.rel for p in catalog.photos_for_root(root_id)]`.
  Mismatch on one root triggers that root's reconcile/rebuild without
  touching the others.

The argue side: the existing fcache test and benchmark infrastructure and
the frozen format description (`thumbcache.py:150-179`) assume one file per
library. Migration: the legacy single-root fcache `thumbs.fcache` is the
fcache for the implicit root (id `""`). Promotion (§10) renames it to
`thumbs-<root_id>.fcache`. The benchmark cache is keyed on the implicit
legacy root — binding it in a test uses the `""` id convention, which is
already defined.

**Binary format: unchanged.** Versions 1 and 2 read paths are frozen. The
file is pure order-indexed records with no paths — zero changes to the
binary schema. Per-root isolation is a naming convention on top of the
unchanged format.

## 4. Walk rule and the parity invariant across N roots

**"Catalog order" with N roots is defined as: the frozen per-root walk,
independently, in `library.json` roots-list order.** Because each root has
its own fcache, the per-root parity invariant is identical in shape to
today's: entry *i* in `thumbs-<root_id>.fcache` = photo *i* in the
catalog's slice for that root.

- `catalog.py:309` `walk_library(root, ...)` is renamed in role to the
  per-root primitive `walk_root(root, ...)` — the frozen rule untouched
  (path-component sort, extension filter). New
  `walk_roots(cfg) = itertools.chain(walk_root(r) for r in cfg.roots)`
  tags each hit with the root's id. The twin in
  `scripts/make-thumbcache.py:297` gets the same two-layer structure.
- **The twin walk-rule change in `catalog.py` and
  `scripts/make-thumbcache.py` must land in the same commit**, enforced
  by a parity test that runs both walks against the same tree and asserts
  identical ordering. The warning already in the code at `catalog.py:312`
  ("caches stop binding") now covers two layers instead of one.
- `.fauxcasa/` is excluded from both walk twins (§2). The `.fauxcasa-root`
  marker file is also excluded.
- **Determinism note for later parallelism:** per-root walks may run
  concurrently, but each root's results are independently ordered (the
  per-root invariant is the unit), so parallel execution is a drop-in
  later with no format risk.

**`bind()` changes (`thumbcache.py:208-224`):** per root — compare
`cat_files = [p.rel for p in catalog.photos_for_root(root_id)]` against
the sidecar's `files[]` for that root. The sidecar is also per-root (§5).

## 5. Catalog + sidecar format changes, versioning

### catalog.json — additive, rows unchanged

Bump `CATALOG_VERSION` by one. Changes to `save_catalog`
(`catalog.py:1040`):

- `"library": str(catalog.root)` → kept **only** for implicit legacy
  libraries; explicit libraries write `"library_id": <uuid>` and
  `"roots": [{"id","path"}]` (paths are informational/debug — resolution
  authority is `library.json`). The roots snapshot also serves as
  redundancy against `library.json` loss.
- Per-photo row (`_photo_to_row`, `catalog.py:1005`): new optional key
  `"R": root_id`, **absent means roots[0]**. Consequence: a promoted
  single-root catalog's photo rows are *byte-identical* to before —
  promotion rewrites only the header. Folder keys (`"folders"`,
  `"hidden_folders"`, `catalog.py:1047-1053`) use `"<root_id>/<rel>"` for
  non-first roots, bare `rel` for roots[0] — same absent-means-first
  convention.
- `load_catalog(path, root)` (`catalog.py:1079`) → `load_catalog(path, cfg:
  LibraryConfig)`; validates `library_id` matches (fixing the existing gap
  where the stored `"library"` field is never checked, line 1095).

**Compat rule of record:** old format (previous `CATALOG_VERSION`) is
accepted only when the library has exactly one root. At load, photo rows
without `"R"` are assigned to roots[0]. The absent-means-roots[0] default
is centralized in one expand helper (`catalog.py`) so there is one place
to audit.

**Migration for existing users:** the version gate at `catalog.py:1093`
already means "old version → return None → cold walk." That is the N3
escape hatch. But a cold walk at 100k re-hashes everything (backfilled
sha256s live only in the catalog), so promotion (§10) does a
**header-only in-place upgrade**: load old JSON, add `library_id`/`roots`,
bump version, atomic write with the old file preserved as
`catalog.json.bak`. Photo rows are untouched — sha256 backfill is
preserved. Any parse anomaly → skip the rewrite, cold walk. The
acceptance test for this migrator staying header-only: it must preserve
every per-row sha256 without touching row bytes. ⚖ This is the one piece
of migration code N3 says we could skip (degrading to cold walk is safe);
the migrator exists solely to preserve expensive sha256 backfill. A bug in
it degrades to cold walk (safe) but costs a 100k re-hash (annoying).
Mitigation: `.bak`, atomic write, fixture round-trip test on a real-shaped
catalog (synthetic fixture, per privacy rules).

**Album members (`catalog.py:193`, persisted raw at
`save_catalog:1059`):** album members are catalog photo *indices*, not
rel strings. Index-based membership makes global catalog order load-bearing
for album identity within a root. **Decision: freeze index semantics within
a root's slice.** Each root's photos form a contiguous slice in the
catalog; album indices are root-local. Any future root reorder must remap
album indices or be forbidden — this is explicitly M2 territory and must
not be silently broken by M1 regroup operations. M1 does not offer root
reorder; this is a documented constraint.

**config.json sort modes** (`main.py:271-289`) are keyed by bare folder
rel. These are machine-local per-cache-dir and are not migrated. Existing
sort prefs are silently dropped on promotion (the cache dir is renamed,
so the old `config.json` moves with it). The folder-rel keys under the
first root remain valid; prefs for non-first roots start fresh. The loss is
explicitly accepted (sort prefs are ephemeral view state, not library
content). If this is unacceptable, it becomes a separate bead.

### thumbs-\<root_id\>.fcache sidecar — per root, typed entries

Each per-root fcache gets its own sidecar `thumbs-<root_id>.fcache.json`.
Sidecar gains `"sidecar_version": 2` semantics, additively:

- `"files"` entries: a **plain string** means a root-relative path (the
  file belongs to this root); a **two-element array** `["<root_id>",
  "<rel>"]` is reserved for future cross-root references — unused in M1
  but defined now to prevent delimiter hacks. No `:` delimiter parsing.
- `"library"` (`thumbcache.py:527`) → `"library_id"` for explicit
  libraries; legacy keeps the path string.
- The legacy single-root sidecar `thumbs.fcache.json` is valid under the
  new reader for the implicit-legacy root (id `""`): every string entry is
  a root-relative path, no rewrite needed.

## 6. Identity key and catalog plumbing

**Identity = `(sha256, root_id, rel)`** — the N6 "(hash, path)" pair where
"library-relative path" generalizes to *root-qualified* path. `Photo` grows
one field:

- `catalog.py:126` — `Photo.root_id: str = ""` alongside the existing
  `rel`. `rel` stays **root-relative POSIX**, untouched — this keeps every
  existing consumer, serializer, and the frozen ini semantics intact.
- The empty string `""` is the reserved id of the **implicit legacy root**.
  Promoted and new libraries always use minted ids; `""` never appears
  inside a `library.json`. The shipped benchmark cache sidecar binds under
  id `""` — there is no ambiguity about which root_id an unsuffixed
  `thumbs.fcache` belongs to.
- New accessor `Catalog.abs(photo) -> Path | None` (root lookup by
  `photo.root_id`, join `rel`, return None if root is offline) replaces the
  three raw compositions: `thumbcache.py:271` (`src = root / photo.rel`),
  `slideshow.py:183` (`path = str(self.catalog.root / photo.rel)`),
  `main.py:1677` (`parts = [p.rel]`). Every abs-path consumer must go
  through this single choke point — it is the only place that can return
  `None` for offline roots and trigger the offline placeholder. A grep
  audit for raw `/ photo.rel` compositions is part of bead .b's acceptance
  criteria.
- Reconciliation key: `catalog.py:1232` becomes
  `old = {(p.root_id, p.rel): (p.size, p.mtime) for p in catalog.photos}`.
  Duplicate `rel` across roots (guaranteed in real archives:
  `2019/IMG_0001.JPG` on two drives) is cleanly disambiguated, and
  byte-identical twins each keep their own per-location state per N6.
  **This is M1-load-bearing, not deferrable** — opening any real
  multi-root archive without it produces incorrect reconcile results.

## 7. cache_dir_for — keyed on library, not root path

`thumbcache.py:227-232` becomes:

```python
def cache_dir_for(library_key: str, cache_root: Path, variant: str = "") -> Path:
    # library_key: library_id (uuid) for explicit libraries;
    # str(path.resolve()) for implicit legacy single-root opens.
    key = library_key.encode()
    if variant:
        key += b"\0" + variant.encode()
    digest = hashlib.sha256(key).hexdigest()[:16]
    return cache_root / digest
```

- **Implicit legacy libraries keep the old key** (`str(path.resolve())`) —
  digests, and therefore every existing cache dir, `config.json`
  (`main.py:263-268`), and import-report, are found exactly where they are
  today. Day-zero upgrade cost for Journey A users who never add a root:
  **zero**.
- **Explicit libraries key on `library_id`.** The library is now
  open-by-path-to-home; the roots can move volumes without orphaning caches.
- **Promotion carries caches across by a single rename** of
  `cache_root/<digest(path)>` → `cache_root/<digest(library_id)>` — catalog,
  all per-root fcaches and sidecars, `config.json`, import-report all move
  atomically. If the rename fails (cross-device `cache_root`, unlikely),
  fall back to copy-then-delete, and beyond that to N3 rebuild.

## 8. Volume UUID binding and offline roots

New module `apps/desktop-python/volumes.py`:

- `volume_uuid_for(path) -> str | None` — Windows: ctypes
  `GetVolumePathNameW` + `GetVolumeNameForVolumeMountPointW` →
  `\\?\Volume{GUID}\`; Linux: match mountpoint from `/proc/mounts`, UUID
  via `/dev/disk/by-uuid/*` symlinks; macOS: `diskutil info -plist`. **Fail-soft
  `None`** for network shares, exFAT without a UUID, WSL, and cloud-synced
  folders. These paths will exercise the null-UUID fallback heavily — see
  open question §13 item 3.
- `mount_for_uuid(uuid) -> Path | None` — inverse enumeration.

Each `LibraryRoot` stores `volume_uuid` + `vol_rel` (path relative to the
volume mount) + last-known absolute `path`. **Resolution order at library
open:**

1. Stored absolute `path` exists and (uuid unknown or uuid matches) → use
   it.
2. `home_rel` check: if the root's path is relative to the library home
   (a whole-library move, e.g. copied to another machine), join and use it.
   This is the cheapest remount case and covers the common "move the whole
   folder" scenario with zero UUID support.
3. `mount_for_uuid(volume_uuid)` found → join `vol_rel`, use it, **update
   `path` in library.json** (self-healing remount — drive letter changed,
   spec §3 satisfied).
4. Else the root is **offline**.

**Offline semantics (the load-bearing part):**

- Offline roots' photos **stay in the catalog** — reconcile runs **per
  online root only** and never treats an offline root's entries as
  deletions. This is the single most important rule for not destroying
  state when a drive is unplugged. It needs an explicit regression test:
  unplug a root, reconcile, assert zero catalog entries removed for that
  root.
- `Catalog.abs(photo)` returns `None` for offline photos — the choke-point
  accessor (§6) is the enforcement. Every call site that consumes the
  result must handle `None` (offline placeholder, grey badge). A missed
  call site is an unhandled `OSError` against an unplugged drive.
- Because catalog entries persist, `bind()` still passes and the per-root
  fcache still renders thumbnails for offline photos — a free, useful
  read-only feature.
- Opening or slideshowing an offline photo shows a "volume offline"
  placeholder; the tree greys the root with a badge.

**Deferral note:** volume UUIDs are deferred past the minimal M1 slice.
Until bead .f lands, a multi-root library on a removable drive breaks
(recovers to offline, not data loss) on drive-letter change. This is
accepted and documented; the `.fauxcasa-root` marker provides an
adopt-on-remount fallback that does not require UUID support.

## 9. Reconcile and cross-root move detection

- `reconcile_walk(catalog, root, ...)` (`catalog.py:1208-1242`) → driven
  per root: for each **online** root, walk it and diff against the catalog
  subset with that `root_id`, keying on `(root_id, rel)` as established
  in §6.
- The N6 matrix applies over the **union of online roots**: same hash +
  different `(root_id, rel)` = moved (state follows) — this naturally
  covers cross-root moves ("photo moved from laptop root to archive root"),
  the same code path as an intra-root rename since the key is uniform.
  Guard: a move is only inferred when the source root is **online and the
  file is absent there**; an offline source never yields moves.
- **Deferral:** hash-based cross-root move detection with state-follow is
  M2 (the rebuild gate, CI, from spec §7). M1 reconcile keeps today's
  semantics per root: size/mtime staleness, adds/removes, no cross-root
  inference. The M2 algorithm: aggregate gone/appeared sets, size-gated
  candidate hashing, basename+(size,mtime) tie-breaks for duplicate hashes,
  explicit pre-backfill fallback. **Pre-backfill files that move between
  roots lose per-location state** — spec-consistent but user-visible;
  needs a status-bar warning while backfill is pending. This is documented
  in §13 open question 5.
- **Per-root backfill state** is keyed by `root_id`, so an adopted root
  backfills independently of a fresh root's index.

## 10. Promotion (Journey A, concretely)

`library.py: promote_library(path, cache_root) -> LibraryConfig`, triggered
by first "Add folder to library…" or explicit "Convert to library…":

1. Mint `library_id`, mint a root id for `path`, capture volume UUID
   (fail-soft), write `<path>/.fauxcasa/library.json` (or user-chosen home
   if `path` is unwritable). Write the optional `.fauxcasa-root` marker.
2. Rename cache dir `digest(str(path))` → `digest(library_id)` (§7). Also
   rename `thumbs.fcache` → `thumbs-<root_id>.fcache` and
   `thumbs.fcache.json` → `thumbs-<root_id>.fcache.json` within that dir.
3. Header-upgrade `catalog.json` in place, `.bak` kept (§5). Per-root
   fcaches and sidecars: untouched beyond the rename above.
4. On any failure at any step: delete the partial `.fauxcasa/`, leave
   caches where the failure found them — the app falls back to
   implicit-legacy open or cold walk. **Reversal instruction for users:
   delete `.fauxcasa/`** — worst case is one rebuild (N3). A crash
   mid-promotion must land in either the legacy layout or the new layout,
   never a hybrid. The `.bak` and the atomic write sequence enforce this.

**Promoted-file-equals-old-file property test:** a synthetic catalog
fixture (matching the real shape but synthetic content per privacy rules)
is promoted and the resulting photo rows are asserted byte-identical to the
input rows. This test ships with bead .d.

Then "add second root" is: mint id, append to `roots`, save
`library.json`, mint a new empty `thumbs-<root_id>.fcache` → bind mismatch
→ incremental reindex for the new root only, other roots untouched.

## 11. Adopt-mode (`scripts/make-thumbcache.py --thumbs`) story

- Existing positional single-root invocation: **byte-frozen forever**
  (benchmark cache). CI regression test pins this. This is a hard
  constraint — the benchmark cache is the parity ground truth.
- New `--library <home-path>` mode: reads `library.json`, walks roots in
  list order via the two-layer walk, writes per-root sidecars with typed
  entries (§5). The `--thumbs` adopt path composes unchanged per root —
  Picasa's own thumb DBs are per-machine anyway, so adoption is naturally
  per-root.
- The in-app twin (`catalog.py:309` walk) and the script walk must land
  the per-root change in the **same commit** — the "caches stop binding"
  warning at `catalog.py:312` now covers two layers instead of one.

## 12. UI implications (M1, read-only)

- `main.py` folder tree: when `len(roots) > 1`, insert one top-level node
  per root (label: `root.label`, default = volume label + last path
  segment), children are today's folder nodes under that root. Single-root
  libraries render exactly as today (no gratuitous extra level).
- Offline roots: greyed node + badge; grid thumbs still render from the
  per-root fcache (§3 and §8); reveal-in-file-manager and slideshow use
  `Catalog.abs()` and show the offline placeholder when it returns `None`.
- Flat view across roots: **out of scope** (per brief).
- `config.json` view prefs: unchanged, still per-cache-dir; the M2
  REVISIT at `main.py:250-263` (move to library-home tier-2) becomes
  *possible* now that a home exists, but is not done in M1.

## 13. Open questions

The following risks were raised by the design judges and are unresolved as
of this writing. They are carried forward explicitly so they are not silently
deferred into implementation.

**1. Nested or overlapping roots.** One root inside another, or a root
containing the library-home, causes the walk to double-count files. The
parity invariant breaks. **Resolution needed before .a ships:** either
forbid overlapping roots at add-root time (validate that no root is an
ancestor or descendant of any existing root, and that no root contains
`.fauxcasa/`) or define the de-duplication rule explicitly.

**2. Root removed then re-added.** Removal mints a fresh id on re-add,
orphaning any durable state under the old id unless the N6 hash matrix
recovers it (M2). The `.fauxcasa-root` marker mitigates this: if the
marker is found on re-add, the old id is resurrected rather than a new one
minted. This needs an explicit decision and a test. Without the marker,
id resurrection via the move pass is M2 — documented gap, not data loss.

**3. Volume UUID null-coverage.** Network shares, some exFAT, WSL, and
cloud-synced folders will return null UUIDs. The resolution chain (path →
home_rel → UUID → last-known) must be tested in every degradation order or
remount bugs masquerade as mass deletion of the family archive. Specifically:
a null-UUID root that goes offline and remounts at a different path must
be recognized as offline (not as a delete event). Regression test required
before bead .e ships.

**4. Absent-means-roots[0] first-root removal/demotion.** Photo rows,
folder keys, and sidecar strings that omit `root_id` implicitly point at
roots[0]. Removing or reordering the first root without an explicit rewrite
would rebind all bare entries to a different root — the one failure mode
worse than losing state. **Guard required:** any operation that would
change which root is roots[0] must either rewrite bare entries to make
them explicit, or be forbidden until the entries are explicit. M1 does not
offer root reorder, which sidesteps this; but remove-and-re-add of the
first root must be validated.

**5. Pre-backfill cross-root moves.** Files that move between roots before
their sha256 is backfilled lose per-location state under every design (the
hash-based move pass has nothing to match on). Spec-consistent but
user-visible. A status-bar warning is needed while backfill is pending
for any root with incomplete coverage. Explicitly M2 behavior, documented
here so it is not shipped silently.

**6. Offline-ness threading.** Every abs-path consumer (`slideshow`,
`reveal-in-file-manager`, full-res open, `read_photo_meta`) must handle
`Catalog.abs()` returning `None`. A missed call site is an unhandled
`OSError` against an unplugged drive. Acceptance criterion for bead .e:
a grep audit confirms no raw `/ photo.rel` composition exists outside
`Catalog.abs()`.

**7. Catalog regroup on manifest reorder.** If any future operation
regroups per-root catalog slices (e.g. root reorder in M2), a stable-sort
bug silently corrupts per-root bind — the fcache entry for photo N is now
paired with the wrong photo. A per-root bind parity test (§4 twin test)
catches this if it runs after every regroup operation. The test must be CI,
not optional.

**8. Cold-start budget with N per-root fcaches and volume enumeration.**
All designs promise a 2-root startup benchmark against the <2s @ 100k
budget (spec §7), but no numbers exist. The 2-root benchmark must land with
the M1 slice (bead .g acceptance criterion), not after. Sequential walks of
N roots at 100k each could pressure the budget; per-root parallel walking
is a drop-in later (§4), but the budget must be measured before it is
needed.

## 14. Phased plan (bead-sized, under fauxcasa-ed5.7)

| Bead | Scope | Key files | M1? |
|---|---|---|---|
| **.a — library model core** | `library.py` (config, minting, load/save, `resolve_open_path`, `.fauxcasa-root` marker), `.fauxcasa/` and marker exclusion in both walk twins, nested-root validation | `apps/desktop-python/library.py`, `catalog.py`, `scripts/make-thumbcache.py` | M1 |
| **.b — catalog plumbing** | `Photo.root_id`, `Catalog.roots` + `abs()` returning `Path\|None`, `walk_roots`, CATALOG_VERSION bump, absent-`"R"` convention, save/load, album-index freeze doc, grep audit for raw `/ photo.rel` compositions, tests including duplicate-rel-across-roots | `catalog.py`, `slideshow.py`, `main.py:1677` | M1 |
| **.c — cache binding** | per-root fcache naming (`thumbs-<root_id>.fcache`), sidecar typed entries, `bind()` per root, `cache_dir_for(library_key)`, `--library` mode; **frozen-v1 byte-identity regression test** vs shipped benchmark cache | `thumbcache.py`, `scripts/make-thumbcache.py` | M1 |
| **.d — journeys** | `promote_library()` (rename + header upgrade + `.bak` + rollback + promoted-rows-equal-old-rows test), add-root flow, Picasa watched-folders import | `library.py`, `main.py` | M1 |
| **.e — offline tolerance** | per-online-root reconcile, offline-skip semantics + regression test (no entries deleted for offline root on unplug), abs-path grep audit, UI badges/placeholder | `catalog.py`, `main.py` | M1 |
| **.f — volume UUIDs** | `volumes.py`, resolution order (path → home_rel → UUID → offline), self-heal path update, null-UUID degradation tests in every order, remount simulation test | `apps/desktop-python/volumes.py`, `library.py` | post-M1 |
| **.g — tree per root** | top-level root nodes, labels, single-root passthrough, 2-root cold-start benchmark vs <2s budget | `main.py` | M1 |

**Minimal M1 slice: .a + .b + .c + .g** — open a multi-root library
read-only, tree grouped by root, per-root caches bound on `(root_id, rel)`,
legacy single-root untouched. **.d and .e complete M1's user story**
(upgrade in place + unplug safety). **.f can trail** (until then, remounts
rely on last-known path and `.fauxcasa-root` marker — acceptable,
documented in §8 and §13 item 3).

**M2 scope (out of this bead):** cross-root hash-based move detection with
state-follow, rebuild-gate CI over N roots (spec §7), tier-2 library-home
state (album order, view prefs migration from cache-dir to home), root
reorder with album-index remapping, and hardware video decode.

## 15. Risks this design takes on

1. **Two open modes live forever** (implicit legacy path-keyed vs explicit
   library-id-keyed). Mitigation: in code there is exactly one model —
   legacy is a degenerate `LibraryConfig` with one root of id `""` and no
   home — so divergence is a data difference, not a code branch.
2. **The header-only migrator** departs from the reject-and-rebuild house
   style (N3) and must stay correct against a frozen legacy format
   indefinitely. Mitigation: `.bak`, atomic write, acceptance test that
   rows are byte-identical before and after, and the escape hatch of cold
   walk if the migrator hits any anomaly.
3. **Absent-means-roots[0] conventions** (row `"R"`, folder keys, sidecar
   strings) buy byte-compat at the price of an implicit default in three
   formats. Mitigation: centralize expand/collapse in two helpers
   (`catalog.py`, `thumbcache.py`) and test the promoted-file-equals-old-file
   property directly.
4. **Cache-dir rename and fcache rename on promotion** couples us to the
   old digest and naming scheme once. Both renames have N3 fallbacks;
   acceptable.
5. **Deferring volume UUIDs past the minimal slice** means an M1
   multi-root library on a removable drive breaks (recovers to offline,
   not data loss) on drive-letter change until .f lands — accepted and
   documented; state is not lost, only inconvenient.
6. **Cold-start budget at N roots** is unbenchmarked. The benchmark must
   land with .g (M1 requirement), not afterward.
7. **Per-root contiguous catalog slices** enable future incremental
   rebuild (a root's slice can be rebuilt independently). This is a layout
   guarantee worth preserving in any M2 regroup operation — root reorder
   must maintain contiguous slices or album-index semantics break silently.
