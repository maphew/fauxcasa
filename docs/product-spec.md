# Product Spec v1 — scope, soul, and non-goals

**Status:** draft for argument (fauxcasa-8g7 — tracker IDs are beads issues,
`bd show <id>`). This is the arguing-about-it doc: it makes a decisive call on
everything, and marks the calls most worth fighting over with **⚖ argue**.
Change it by arguing, not by silently diverging. A glossary at the end defines
the project jargon (oracle, fixture, differential acceptance, …).

**Name note:** "Fauxcasa" is a provisional working name. Nothing in this spec —
file formats, identifiers, user-visible strings — may bake the name in; the app
name is a single swappable constant.

**Evidence base.** Distilled from three inputs. (1) The research corpus:
`docs/research/picasa-video-notes.md` (the tutorial corpus — what the product
actually was), `picasastarter-notes.md` (the failure modes a community of
power users paid to work around), `picasa-ini-format.md` /
`picasa-db3-validated.md` / `oracle-db3-survey.md` / `wine-oracle.md` (the
byte-level truth, validated against real Picasa 3.9.141 under Wine),
`prior-art-parsers.md`, and the archived community sources under
`docs/research/sources/` (Picasa Resources pages, the sbktech .pmp writeup and
its comment thread, the tutorial-video transcripts). (2) The real-DB survey
(`oracle-db3-survey.md`, per the fauxcasa-ok6 redirection). (3) **Owner
priorities** (argue these with maphew, not with the evidence): the project
exists to succeed a treasured multi-decade family Picasa archive on a home
network; the owner runs Linux daily; `begin.md` is the founding motivation.
Calls driven by (3) are tagged *owner priority* below.

---

## 1. Soul

> "Picasa's main goal is in making it as easy and fast as possible for anyone
> to organize and edit their pictures." — Picasa Resources

Fauxcasa is a local-first desktop photo manager that resurrects the best of
Google Picasa 3.9 and designs away its documented failures. It is for the
"95% of the pictures of 95% of people" (Picasa Resources' own RAW-philosophy
phrase): people with decades of family photos in folders, who want to find,
fix, tag, and share them — not develop RAW files or run a DAM workflow.
Google killed Picasa in 2016; a decade later people still run the Windows
binary under Wine because nothing replaced its particular combination of
speed, safety, and respect for the user's own folders.

The emotional core, in the users' own words from the tutorial corpus:

- **"Scanning for pictures never moves or copies files to new locations."**
  Your folder structure *is* the organization. Zero-effort setup; the library
  mirrors disk from the first scan.
- **"No harm done."** Every action is reversible and is *seen* to be
  reversible. The narrator's advice for the edit tools was "just experiment" —
  safe advice only because of the next point.
- **"You can come back anytime — and I say anytime, I mean anytime after you
  close Picasa … — and still undo something you've done."** The per-photo,
  operation-named, durable edit history was the single strongest emotional
  peak in the entire tutorial corpus.
- **It's fast.** ~10 MB installer, 256 MB RAM, instant grid, instant search,
  browsable while scanning. The speed was not an implementation detail; it was
  the product.

One design rule binds every surface: **modes, not modals.** Import, edit, and
backup take over the main window; background work (scan, faces, moves)
reports progress inline — sidebar rows, toasts, compact popups — never in
blocking dialogs. (`picasa-video-notes.md` §3, behavior 9.)

Everything in v1 is tested against that sentence, those four feelings, and
that rule.

### What Fauxcasa is NOT (non-goals, permanent)

- **Not a cloud service.** No accounts, no sign-in, no hosted backend, no
  telemetry. Picasa's release history is a graveyard of cloud-coupled features
  that decayed as services churned; the durable core was 100% local (face
  tagging outlived every service around it). Nothing in Fauxcasa may *depend*
  on a hosted service.
- **Not Photoshop, not Lightroom, not darktable.** Editing depth stays at
  Picasa's bar: crop, straighten, one-clicks, tuning sliders, simple effects.
  RAW files render automatically "as if JPEG"; real raw development is
  explicitly someone else's job (and externally produced JPEGs dropped into a
  watched folder just appear — that's the interop contract).
- **Not a managed-library importer.** Never copy-into-app-library. Install and
  uninstall are side-effect-free on user files.
- **Not security software.** No password-protected "hidden" folders defeated
  by deleting a file (Picasa's was). Hiding is cosmetic and labeled as such.
- **Not a social network, not a pushy AI curator.** No feeds, no
  auto-generated collections pushed at the user, no required cloud ML.
  (Picasa's own *mechanical* auto-collections — Starred Photos, Recently
  Updated — are beloved, deterministic, and in scope; see §5. And the
  non-goal bans curation-by-default, not assistance: local, opt-in ML that
  helps you *search and tag* — object/colour/sentiment recognition — is on
  the medium-range roadmap, §5 LATER, *owner priority*.)

---

## 2. Non-negotiables

The constitution. Every feature, milestone, and architecture decision is
subordinate to these. Each carries an acceptance test that becomes an
executable CI gate at the milestone noted in §9 ("Gate coverage" map).

### N1. Folders on disk are the truth

The sidebar's folder list *is* the filesystem. Folder operations are real file
operations and the UI says so. Albums are pure references layered on top.
Delete semantics branch on context and the confirmation states the file's fate
in context ("remove from this album — the file stays in `<folder>`" vs "delete
from disk and from N albums").

*Test (CI, from M2):* for a generated corpus of folders, the state Fauxcasa
displays is byte-derivable from folder contents + sidecars alone; a folder
`cp -r`'d to a second library carries its photos, originals, and per-photo
state intact.

### N2. Non-destructive by default

Edits are an ordered, named, per-photo operation log, durable across restarts,
applied to a retained original. Pixels are touched only on explicit Save —
which stashes the byte-exact original (Picasa's `.picasaoriginals` mechanism,
confirmed by oracle fixture 005) and remains reversible via both "Undo Save"
(un-bake the file, keep the edits live) and "Revert" (discard everything).
Metadata writes into originals follow the in-file write policy (§5 P1):
guilty until round-trip-proven.

*Test (CI, from M3):* apply N edits, kill -9, relaunch — full named undo stack
intact. Save, then Undo Save — original restored byte-exact.

### N3. All state lives in the library

**Zero database-only user state.** Picasa's documented rebuild-loss list —
ignored faces, manual sort order in folders *and* albums, video date/geotag
edits, contacts-only face names — is our regression checklist: every one of
those must survive in Fauxcasa. Albums (with member *order*), people, watch
config, and edit state are serialized in the library; the index, thumbnails,
and search index are disposable caches.

*Test (the rebuild gate, CI, from M2):* delete every cache, rescan, diff the
full user-visible state — zero loss, including the four things Picasa lost.

### N4. Instant feel at 100k+ photos

The corpus documents multi-hundred-GB, multi-volume libraries on slow
removable drives as the normal power-user shape (tutorial V8; sbktech comment
thread; the community rebuild guide plans in units of 10,000 photos).
**100k photos is our design target with headroom above the documented
shape** — and roughly the size of the owner's archive (*owner priority*).
Instant feel comes from Picasa's proven architecture: precomputed catalog +
multi-resolution thumbnail cache + per-photo word index. Grid scrolling,
search, and selection never read an original file; explicit 1:1 zoom and the
hover full-screen peek are the deliberate exceptions and load asynchronously.
Numeric budgets in §7.

*Test (CI, phased):* the §7 budget table, measured on the 100k synthetic
library on the §7 reference hardware. Each row gates at the milestone where
its feature lands; the M1 slice is the read-only rows (cold start, scroll,
search, initial index, catalog size).

### N5. Crash-safe, transactional, always

What the oracle showed about Picasa: user state (ini + in-file metadata) was
written synchronously at action time, but the central db3 was a lazy mirror —
flushed minutes later, with **no flush at all on clean exit** — while
community lore ("Exit Picasa to make sure the database is written") papered
over a store that corrupted on unexpected close (CBlockFile errors, wrong
thumbnails, endless rescans) and ran an interruptible-at-peril compaction
pass. Fauxcasa keeps the good half and fixes the rest: durable state commits
synchronously at action time as a small append/journal write; caches use a
WAL-style store. Cache corruption can never prevent startup or cost user
data — at worst it costs a background re-index.

*Test (CI, from M2):* the kill-fuzzer — N seeded runs over the user-action
vocabulary, kill -9 at random points; relaunch loses at most the in-flight
action, never corrupts, never blocks startup. (The fuzz harness is a named M2
deliverable.)

### N6. The library survives other people's tools

Picasa's #1 documented footgun: metadata was keyed to absolute paths, so a
move done in Explorer silently orphaned albums, faces, and places. The
operational rule "only move files inside the app" contradicted its own
folders-are-truth model. Fauxcasa instead: file identity is content-derived
with path disambiguation (§3 "File identity"); paths are stored
library-relative; nothing is keyed to drive letters or mount points; external
moves/renames/restores — including those made while the app wasn't running —
are reconciled on rescan. (Precedent: Picasa itself kept a per-file
identifier, `originfast` — unique per photo in the oracle at scale and
consistent with a content hash, though its exact semantics are unconfirmed.)

*Test (CI, from M4):* (a) move/rename folders and files with the OS file
manager while the app is closed; relaunch; albums, faces, stars, edits all
intact. (b) Foreign-write variant: while Fauxcasa is running, sidecars are
modified externally — replaying oracle-fixture diffs, i.e. exactly what
Picasa would write — and the changes are merged with no user state lost on
either side. (Simultaneous dual-open of both apps stays unsupported; §3
"Concurrency".)

### N7. No silent failure

The community archive is a catalog of silent no-ops: a read-only `.picasa.ini`
silently stops all writes forever; a UNC watched path without a trailing
backslash silently does nothing (drive-letter paths worked without one — the
inconsistency is its own indictment); five documented invisible mechanisms
hide photos (small-image size filter, OS hidden attributes, app-level hide,
dot-prefix exclusion, name-based scan exclusions) and generate "my photos are
missing" support threads;
XMP backfill silently skips read-only files. In Fauxcasa every write is
verified and surfaced (per-folder health status), every exclusion/filter is
inspectable, and there is a first-class "why is this photo not shown?" /
"what does the app think about this file?" diagnostic.

*Test (CI, from M2):* make a sidecar read-only, star the photo — the failure
is visible in the UI within one action.

---

## 3. Data model

Two layers, exactly as Picasa had them, because the model was right:

- **Folders** mirror disk. Watch policy per root: watch / scan-once /
  ignore ("ignore never deletes" is a trust guarantee). Flat and tree views
  are pure views; flat view must surface the on-disk path on demand and a
  folder delete must disclose its full nested blast radius regardless of view.
- **Albums** are references: stable UID, name, optional date/description,
  member list **with persistent manual order** (Picasa stored membership in
  the ini but order only in the DB — order jumbling was a top
  trust-destroying bug; order is first-class data here). Removing from an
  album never touches files; album-context operations never mutate files
  implicitly (Picasa's rename-photos-from-an-album renamed the source files
  on disk — designed away).
- Per-photo user state: **star** (binary flag, a cross-cutting selection
  predicate other features filter on), **caption**, **keywords**,
  **faces** (region + person ref, with suggested/confirmed/ignored states —
  in v1 the *suggested* state is populated solely by imported Picasa data;
  the recognition engine that generates new suggestions is v1.5), **geotag**
  (v1: read, preserve, display — badge + coordinates readout; authoring waits
  for the v1.5 map panel), **edit stack**, **manual sort position**,
  **video date/geotag overrides** (v1: imported Picasa values preserved
  losslessly per N3; authoring new ones arrives with the post-v1 date-edit
  and map surfaces).
- **People** are first-class: a person registry living in the library (not a
  machine-local contacts.xml), referenced by face tags.

### File identity

Identity = **content hash, with library-relative path as the disambiguator**.
Byte-identical duplicates are normal in family archives (the import flow has
duplicate exclusion precisely because of them); each copy keeps its own
per-location state, keyed by the (hash, path) pair. The hash is a *recorded*
property, transactionally re-keyed whenever Fauxcasa itself rewrites a file
(Save, metadata write-back) — the app's own writes never orphan state.
Reconciliation on rescan covers the full matrix:

| | same path | different path |
|---|---|---|
| **same hash** | unchanged | moved/renamed → state follows |
| **different hash** | edited externally → keep state, refresh hash, surface notice | new file (or moved+edited: offer match by name/EXIF, surface) |

Cost model: cheap signals first (size + mtime), full hash only for
new/changed/candidate-moved files; the §7 index budget includes hashing.

### Where state lives (the three tiers, corrected)

Picasa's stated policy was right and its implementation leaked. Fauxcasa
adopts the policy with the leaks fixed:

| Tier | What | Where | Notes |
|---|---|---|---|
| 1. Standard metadata | captions, keywords, date, geotags, face regions | in-file XMP/IPTC, governed by the write policy (§5 P1: sidecar-first in v1, deliberate write-back) | so the data outlives Fauxcasa too |
| 2. App state | edit stacks, stars, albums+order, people, watch config, ignored faces, video metadata overrides | human-readable files inside the library: per-folder sidecars for per-photo state (per-photo *records* within the folder file, merged per-record — never whole-file clobber; travels with a folder copy), a library-home directory for library-level state | Picasa leaked albums/contacts/watch-lists into the user profile — the exact reason libraries weren't portable. Fixed. |
| 3. Cache | catalog index, thumbnails, search index | machine-local by default, location configurable | disposable by N3; ~1–2% of library size at full tier depth |

**Album state authority, precisely:** the ini layer is authoritative for
*membership* (per-photo `albums=` lines, Picasa-compatible); the library-home
order file is authoritative for *order only* (the ini grammar has no order
field). Members the order file doesn't list append in discovery order — same
rule as new files in a manually sorted folder. A copied-in folder whose ini
references unknown album UIDs gets placeholder albums, surfaced in the import
diagnostics, never dropped.

**Star authority:** the sidecar star line is authoritative. When in-file
write-back is enabled (§5 P1), stars mirror to XMP `Rating` (5 ↔ starred)
asynchronously; foreign `Rating` changes detected on rescan are *offered* as
star changes (transitions to/from 5) and logged; ratings 1–4 display
read-only. Binary star stays — it's the Picasa soul (*owner priority*;
**⚖ argue** for 0–5 ratings instead).

**⚖ argue — cache location.** Machine-local default (fast local disk even when
the library is on NAS/USB; nothing portable lives there) vs in-library default
(one dir to back up… but backups don't need regenerable thumbnails, and NAS
round-trips would hurt the instant-feel budget). Spec says machine-local,
configurable.

**A "library"** = one library-home directory (the root for tier-2 library-level
state) plus N watched roots, possibly on other volumes. Libraries are
first-class documents: open-by-path, N per user, no global mutable pointer
(PicasaStarter existed because Picasa had one DB per Windows user behind an
undocumented registry value). Multi-volume state binds to volume UUIDs so
drives can remount anywhere.

### Concurrency (v1)

- **Fauxcasa vs Fauxcasa:** single writer, advisory lock *with holder
  identity* ("opened by matt@host since 14:02"), safe multi-reader. Two
  Picasas on one DB meant corruption; we make that failure impossible to hit
  silently.
- **Fauxcasa vs Picasa (the cohabitation protocol):** Picasa honors no lock
  of ours, so the rule is **alternation, not simultaneity** — in plain
  words: switch between the two apps as often as you like, but only one
  open at a time on a given library. Simultaneous open of both apps is
  unsupported and detected where possible (Picasa's lock artifacts:
  `_lock.lck` family). Sidecars Picasa
  touched while we weren't looking are first-class external changes: detected
  by mtime/content on rescan and folder-open, merged per-key
  (last-writer-wins) with a surfaced reconciliation note. Fauxcasa never
  holds a sidecar open and never rewrites one it hasn't re-read. This is the
  live half of the N6 test.
- **⚖ argue** if the multi-machine NAS scenario needs more than
  single-writer-alternation in v1 (§10 item 10).

---

## 4. Picasa compatibility contract

The family archive is a Picasa 3.9 library (*owner priority*). Compatibility
is not nostalgia — it's the migration path, and it's the most de-risked part
of the project: the formats are documented (`picasa-ini-format.md`,
`picasa-db3-validated.md`), implemented (`scripts/picasa_db.py`), and
validated against the live Wine oracle with a growing differential-fixture
corpus (`fixtures/oracle/`).

**Read: everything.** `.picasa.ini` (and legacy `Picasa.ini`), `.pal` album
files, `contacts.xml`, the db3 catalog (.pmp + thumbindex — the *rescue
importer* for the four DB-only data classes), `.picasaoriginals`, and in-file
XMP/IPTC/EXIF. The legacy stores overlap and disagree: album membership
appears in ini, .pal, *and* db3; face names in ini, contacts.xml, db3, and
XMP. The importer reads all of them and merges with this provisional
precedence, to be hardened empirically against the oracle before M1 exit:
in-file metadata wins for tier-1 data (captions, keywords, faces-in-XMP);
ini wins for app state (stars, albums, edit recipes); db3 fills gaps only
(the rescue classes); every conflict is surfaced in the import report, never
silently resolved. Robustness rules per `picasa-ini-format.md` ("fail soft
per-line, never per-file"; preserve unknown keys/sections byte-faithfully).

**Write: the durable layer, Picasa-acceptably.** The fixture sessions
established Picasa's own write model: ini + in-file metadata are written
synchronously at action time; db3 is a lazy mirror (2–6 min flush, no
shutdown flush). So the ini layer is the durable record, and that's what
Fauxcasa writes: organize/edit actions in v1 are persisted as
Picasa-compatible `.picasa.ini` (in-file metadata per §5 P1), byte-faithful
to the observed grammar (zero-stripped rect64, CRLF, key vocabulary per the
format doc). We do **not** write `.pal` files: oracle fixture 003 shows
Picasa 3.9 itself creates albums as db3 row + ini sections with no `.pal`;
the differential tests are the arbiter if that proves insufficient.

**Acceptance is differential, and the harness is a deliverable.** Point real
Picasa 3.9 at a Fauxcasa-written library in the oracle; Picasa must read our
stars/captions/albums/edits, and "must not rebuild or reject" is made
machine-checkable (db3 diff classes + ini/sidecar diffs after a scripted
Picasa session, synchronized past the lazy-flush window the fixtures
characterized). Honestly costed: this means driving a proprietary,
non-redistributable 2011 GUI binary (installer cached locally with Wayback
provenance — it cannot ship in a public CI image) deterministically under
Wine. M2 therefore owns: a reproducible oracle-harness recipe (obtain,
install, drive, synchronize, assert), its licensing position documented, and
a **recorded golden-fixture fallback** so contributors and public CI verify
against committed oracle outputs while full differential runs happen on
machines that have the binary.

**db3 writing is *not* a v1 goal.** We read db3 (rescue import); we let Picasa
rebuild its own caches. Whether Picasa tolerates a foreign-written
`repository.dat` sentinel block (fauxcasa-5kl) is an open experiment that
could enable deeper coexistence later — it gates nothing in v1.

**⚖ argue — how long does write-compat rule?** Writing Picasa's ini grammar
forever freezes us to an undocumented 2011 format; dropping it breaks the
migrate-gradually story. Spec says: v1 is **a handoff with a safety period**
("succession via alternation"): Fauxcasa's goal is to *replace* Picasa, and
during the trust-building period full ini write-compat means the user can
switch between the two apps freely — a week in Fauxcasa, fall back to real
Picasa anytime, nothing lost in either direction — provided only one app is
open at a time (§3 cohabitation protocol). Fauxcasa-native state (album order,
ignored faces, people registry, video overrides — things Picasa has no home
for) goes in clearly-marked *additional* files Picasa ignores. Post-v1, if
the sidecar format needs to evolve, ini stays as an export/sync target rather
than the native store.

---

## 5. Feature scope

### v1 — IN

Library & navigation

- Scan/index in place; watched roots with 3-state policy; live change pickup
  (FS events + periodic rescan + on-open revalidation — events alone
  demonstrably fail on NAS); library browsable during initial scan.
- Continuous-scroll thumbnail grid across all groups, pinned group headers,
  live counts everywhere, thumbnail zoom slider, conventional scrollbar
  **plus** Picasa's jump-to-folder/end buttons (the recentering thumb was an
  admitted footgun — dropped). Thumbnail corner badges (star, geotag) in
  every grid context.
- Flat + tree folder views (tree shows volumes; flat shows path on demand);
  per-folder sort modes (date / name / size / manual — manual is one mode
  among them, durable per N3).
- Picasa-native auto-collections: **Starred Photos** (with scoped views per
  footgun 17) and **Recently Updated** (plus the Exports collection, under
  In & out below).
- Selection tray: persistent cross-folder selection with Hold/Clear, live
  type-aware readout ("Folder Selected — 14 photos"), the universal input to
  every output action, with enablement keyed to selection type.
- Instant as-you-type search over filenames, captions, keywords, people,
  folder names; negation (`-term`); results are a selectable, bulk-operable
  set; a native All Photos view (Picasa users needed a magic negation-search
  hack to get one).
- Keyboard triage loop: star, next/prev (incl. J/K), select/hold, 100% zoom
  toggle, hover full-screen peek, reveal-in-file-manager, platform-correct
  Ctrl/Cmd. Picasa-compatible bindings as the default scheme.
- Status bar: collection aggregate / single-photo metadata dual mode.

Organize

- Stars (one keystroke, instant, reversible), captions, keywords.
- Albums: create/fill both ways (create-then-drag and select-then-add),
  drag-to-sidebar drop targets, persistent manual order, year grouping.
- Persistent manual sort order in folders (survives rescans; new files get an
  explicit insertion position — no F2-rename-everything workaround).
- People v1: import *all* existing face data (ini + contacts.xml + db3 + XMP),
  people albums, manual face tagging (region + name),
  suggested → confirmed / ignored state machine (v1 suggestions come from
  import only), per-folder face opt-out, one-action "remove all face data",
  and an honest People surface: an explicit unnamed-faces affordance and a
  visible count of photos not yet face-scanned (N7 spirit) so the
  pre-recognition gap is never silent.
- Hidden photos/folders: cosmetic hide with a single inspectable filter UI.
- Batch rename (pattern + counter; dots in names are legal).

Edit (the Picasa bar, no more)

- Edit room one double-click from the grid: filmstrip, breadcrumb, histogram
  + EXIF panel, caption field.
- Basic fixes: crop (aspect presets labeled as ratios, preview-then-revert),
  straighten, rotate/flip (lossless), redeye, I'm Feeling Lucky, auto
  contrast/color, retouch, text, fill light.
- Tuning: fill light (yes, in both places — mirrors Picasa's UI) /
  highlights / shadows / color temperature + neutral picker.
- Effects: the 12 classic tiles with live previews. ⏳ resolve-by-M3: the
  one-click vs parametric split is documented ambiguous in the video notes —
  settle it against the oracle.
- Operation-named, durable, per-photo undo/redo; Save (bake + stash
  original), Undo Save, Revert; tool state reflects history (Crop → Recrop).

In & out

- Import from device: opens on device attach (toggleable), linear one-screen
  flow, async acquisition, per-photo star/exclude (X key), duplicate
  exclusion on by default, card policy defaulting to "leave card alone",
  date-named destination, cross-platform filename hygiene.
- Export with edits applied: original-size option, optional order-preserving
  numbering, JPEG conversion by default, Exports collection, plus a
  "for email" preset (resize + JPEG, hand off to `mailto:`/xdg-email — the
  family sharing path, kept without any Google integration). Export is also
  the "open with external tool" path — pending edits always render unless the
  user explicitly asks for raw originals (Picasa's silent
  unedited-file-passthrough confused everyone).
- Move Folder (incl. cross-volume copy-verify-delete with per-file progress;
  same-volume = instant rename; photo-less parents are movable — Picasa's
  empty-parent wart is designed away).
- Delete → OS trash everywhere (freedesktop trash on Linux, Recycle Bin on
  Windows); where no trash exists (NAS), a library-local trash, never direct
  unlink; restored files re-index automatically.
- Minimal incremental backup: named Backup Sets targeting a path/drive
  (folder checkboxes, already-backed-up bookkeeping, live size estimate) —
  the "rest easy" feature with discs replaced by drives. (Until it ships, N3
  is the interim story: the library root is complete, so any file-level
  backup tool backs up everything.)
- Slideshow (basic, per-folder/album play button).
- Minimal movie/slideshow renderer (*owner priority* — both halves of the V6
  tutorial are loved): Picasa-2 level — acts on the selection/album, slide
  delay + output size, auto title slide (name + date), pan/zoom transitions,
  no audio; output lands in the Exports collection and the file manager
  opens on completion. Rides the bundled decoders. The full Movie Maker
  (audio fitting, captions, transitions) stays LATER.
- Locate on disk / reveal original.

Formats

- Stills: JPEG, PNG, TIFF, GIF, BMP, PSD, TGA, WebP; per-extension
  include/exclude panel.
- RAW: detect at least Picasa's documented 16-vendor extension list
  (`sources/picasaresources/files-supported-by-picasa3.md`); auto-render via
  a maintained, *updatable* decode library (LibRaw-class) — never a frozen
  in-binary table (Picasa's 2013 freeze was its single biggest functional
  decay).
- Video: index + playback with **bundled** decoders (ffmpeg-class; Picasa
  delegated to system codecs — if Windows Media Player or QuickTime couldn't
  play it, Picasa couldn't either — the anti-pattern); video metadata edits
  live in tier-2 library state (Picasa lost them on every rebuild).
- Decoding untrusted files is the primary attack surface (Picasa's final
  years saw three decoder-vulnerability patch rounds). Requirement: decode of
  untrusted input runs with no ambient authority; the mechanism (process
  isolation and/or memory-safe decoders) is chosen with the stack
  (fauxcasa-6hf) against a written threat model, at M0 exit — before M1 work
  begins.

Maintenance & trust

- One-click verify/rebuild index; per-folder health status; the "what does
  the app think about this file?" inspector; first-run = two questions max
  (scan scope; nothing else), library usable immediately.
- Updates: opt-in version check only, never auto-install (**⚖ argue**:
  fully offline instead — even a check-only ping is a network dependency;
  but Picasa's broken updater stranded users on vulnerable builds).
- i18n string externalization from day one (full localization is post-v1).

### P1 — the in-file metadata write policy (**⚖ argue**, the genuinely hard call)

The evidence pulls both ways. The storage-tiering principle and the face-tag
lock-in disaster say *write standard metadata into files by default* — it's
the only storage that outlives every app. The MakerNote corruption bug
(Picasa corrupted Olympus/Kodak vendor EXIF when writing metadata) says
*never touch originals you can't round-trip*. And the trust ladder (§9) says
the scariest right — mutating decades of originals — should not be granted in
the same milestone as the app's first write of any kind. Resolution:

This policy governs *metadata* writes into originals. Pixel writes happen
only via explicit, user-invoked Save, with its own original-stashing
guarantee (N2) — that is a different act and is not gated here.

- **v1 default: sidecar-first.** Metadata operations do not write originals.
  Captions, keywords, and confirmed face regions live in sidecars/tier-2,
  fully exportable.
- **Write-back is deliberate, supported, and honest:** a per-library "write
  standard metadata into my files" switch plus a resumable, progress- and
  failure-reporting batch operation (both directions). Enabling the switch
  offers retroactive backfill — Picasa's non-retroactive opt-in was the
  lock-in trap, and footgun 13 is honored by making backfill one explicit
  action, not an experimental afterthought.
- **The writer must be proven before it's default:** round-trip verification
  that re-parses offset-sensitive structures (MakerNotes embed absolute
  offsets — byte-comparing non-target segments is *not* sufficient; never
  relocate the EXIF APP1 segment, or re-parse MakerNotes after write), built
  on a mature metadata library (exiv2/exiftool-class — wrap, don't
  hand-roll), fuzz-tested against vendor samples. Files failing verification
  fall back to sidecar with a surfaced notice (N7).
- **Flipping the default on** (for JPEG) is a post-v1 decision, after the
  verified writer has soak time on the dogfood archive. The machinery itself
  (switch, verified writer, batch op) ships in v1 — it is an M4 deliverable.
- RAW and video originals are never written, ever; XMP sidecar files serve
  interop for them.

### v1.5 / v2 — LATER (in spirit, not yet)

- **Face recognition + clustering** — cluster-then-bulk-name with threshold
  sliders is the loved workflow for the archive's untagged decades. Deferred
  because it is the single largest engineering line-item in the corpus, and
  bound: **it is the sole headline of the release immediately after v1; no
  other LATER item may queue ahead of it.** Sequencing confirmed by the
  owner (2026-06-11): highly desirable, but pointless before browsing,
  search, organization, and non-destructive edits exist. Privacy-gated:
  explicit opt-in, fully local.
- Geotag map panel (swappable tile provider, offline-degrading — Picasa's
  Maps-API dependency broke twice via deprecation).
- Timeline view.
- Print + contact sheets.
- Static-gallery export — the serverless heir to Sync to Web's durable
  concept (per-folder/album opt-in mirroring of *edited renditions* to a
  share target).
- Full Movie Maker (audio track + fit-photos-into-audio, captions,
  transition styles, per-slide control). The minimal renderer is already in
  v1 (§5 In & out — owner settled the V6 question: both halves are loved).
- Collage, screensaver.
- **Content recognition — the medium-range roadmap** (*owner priority*,
  v2+): object, colour, scene, and sentiment recognition via local ML —
  the modern capability Picasa classic never had — to power search and
  bulk keywording. Soul constraints: local models by default; any remote
  LLM assist is explicit per-batch opt-in, never required, never a
  dependency (§1 non-goals); results land as ordinary, reviewable
  keywords/attributes in tier-2 state (N3) — searchable suggestions the
  user accepts or ignores, never self-applied curation. Sequenced after
  face recognition.
- Custom buttons / external-tool API (tray-consuming, export-then-act, with
  a consent gate Picasa lacked).
- Standalone fast viewer + OS file associations.
- Full localization (string externalization is already day-one).

### OUT — dead, replaced, or against the soul

- Picasa Web Albums, Google sign-in, Google email integration, BlogThis!,
  Shop, print-ordering, YouTube upload, Picnik, Gift CD.
- "Backup to CD/DVD" as discs (the *idea* survives as backup-to-path, in v1).
- iPhoto / Apple-Photos-container import (revisit only if macOS becomes
  first-class).
- Password-protected hiding (security theater), any cloud sync of face data,
  auto-install updates / phone-home of any kind (the opt-in version check in
  Maintenance is the entire network surface, and it's off by default).

---

## 6. Designed-away footguns

The PicasaStarter survey and the community archive are, jointly, a paid-for
catalog of what broke in the field. These are spec commitments, each traceable
to a documented failure:

| # | Picasa failure (documented) | Fauxcasa rule |
|---|---|---|
| 1 | One DB per Windows user behind `%LocalAppData%` + undocumented registry surgery to relocate | Libraries are documents: open-by-path, N per user, no global pointer |
| 2 | Absolute drive-letter paths; library breaks when a drive remounts | Library-relative paths + volume UUIDs everywhere |
| 3 | Zero concurrency control; double-open corrupts | Advisory lock with holder identity; safe multi-reader; cohabitation protocol (§3) |
| 4 | State leaks outside the DB root (collages to My Pictures, contacts in profile, Desktop dependency) | All state in the library (N3); generated artifacts get a defined in-library home |
| 5 | db3 lazy mirror + no shutdown flush + corruption on unexpected close | Transactional writes at action time (N5) |
| 6 | External moves orphan albums/faces/places | Content identity + rescan reconciliation (N6) |
| 7 | First-run "scan everything" hostility | Scan scope opt-in before first index; exclusions visible and overridable |
| 8 | Folder merge destroys edits (per-folder sidecar clobber); transplant requires identical paths/usernames | Per-file state records, merge-safe; libraries relocatable by copying |
| 9 | Read-only sidecar silently stops all writes; UNC watch path without trailing backslash silently ignored | Verified writes, surfaced errors, canonicalized config (N7) |
| 10 | Five invisible hide mechanisms ("my photos are missing") | One inspectable filter/diagnostic surface |
| 11 | Flat-view folder delete hides nested blast radius | Destructive ops always disclose full scope |
| 12 | Manual sort order jumbles "at some undetermined time" | Order is first-class, durable, tested |
| 13 | Face data locked in DB/sidecar by default; XMP opt-in non-retroactive | Durable face storage by default; write-back/backfill is a real, resumable batch op (§5 P1) |
| 14 | Frozen RAW table; OS-codec dependency | Updatable decode libraries, bundled codecs |
| 15 | MakerNote corruption on metadata write | In-file writes round-trip-verified or not made (§5 P1) |
| 16 | UI floor of year 1903 on dates | Unbounded dates (scanned photos predate 1903) |
| 17 | Starred Photos accretes forever (the V2 tutorial narrator wades through 84 stale stars), degrading the triage loop | Scoped star views + easy bulk-unstar |
| 18 | Positional row joins across dozens of per-column .pmp files with no cross-file integrity check — documented failures: thumbnails joined to wrong photos after corruption; a hand-edited column file bricking every category | Catalog cache is a single transactional store with explicit keys; durable state is per-file records |
| 19 | Users hand-edit state files and brick categories | Tier-2 files human-readable and validated on load; unparseable content is quarantined and surfaced, never rewritten (preserves the §4 byte-faithful rule) |

---

## 7. Performance identity

"Instant feel at 100k+" becomes numbers. **Reference library:** 100k photos /
~500 GB across two volumes. **Reference hardware:** a mid-range 2020s laptop —
8 hardware threads, 16 GB RAM, NVMe system disk (cache lives here), library
split between a SATA-class internal volume and one slow volume (USB-3 spinning
disk, or SMB over gigabit with ~1 ms RTT). Budgets are per volume class where
they differ, and each row becomes a CI gate at the milestone where its
feature lands (§9). Picasa-era baselines appear in parentheses — they are
*reported or derived* from the corpus (community planning guidance, tutorial
observations), not measurements, except the catalog-size row, which is
oracle-measured.

| Operation | Budget |
|---|---|
| Cold start, already-indexed library → interactive grid | < 2 s (community-reported: effectively instant) |
| Scroll the full library | 60 fps target; the CI check: p99 frame time ≤ 32 ms, no frame > 100 ms, during a scripted flick-scroll at ≤ 3 screens/s; zero blank tiles; grid never reads originals (1:1 zoom/peek load async, N4) |
| Search keystroke → filtered grid | < 50 ms |
| Star/caption → durable | UI acknowledgment < 100 ms; durable = fsync'd journal/sidecar append: < 100 ms local, < 1 s NAS. In-file metadata mirroring, when enabled, is async and queued (§5 P1) |
| Initial index (no faces), including content hashing | library browsable immediately; ≥ 30 photos/s sustained on the local volume, ≥ 10/s on the slow volume (Picasa planning lore: ~3/s *including* face recognition — not like-for-like; the honest bar is "browsable immediately" plus the absolute rates) |
| Full cache rebuild | background, lossless (N3), UI responsive throughout, ≥ initial-index rate — never an event users plan around |
| Catalog size | ~50 bytes/photo core catalog (oracle-measured); cache total ≤ 2% of library |
| External change → visible in UI | < 5 s on local volumes (FS events); ≤ 10 min on NAS at the 5-min default poll interval |

Resource austerity is part of the identity, honestly restated for 2026:
Picasa's 10 MB installer is not reachable with bundled decoders (ffmpeg-class
+ LibRaw-class are the price of a deterministic format matrix — accepted);
the soul constraint is **resident memory and cold start, not installer
megabytes**. Hard RAM/installer budgets are set by the stack decision
(fauxcasa-6hf) *at M0 exit*, and any candidate stack must demonstrate the
scroll and cold-start budgets in a prototype before the language/framework is
locked. "Electron + 1.5 GB resident" fails the soul test regardless.

---

## 8. Target platforms

- **Linux: first in line** (*owner priority* — the dev/dogfood platform, and
  an unserved market that still runs Picasa under Wine in 2026). First in
  line means it leads development, not that the others are lesser tiers.
- **Windows: first-class in v1** (*owner priority*, settled 2026-06-11:
  "that's where the people who need the most help live" — the legacy
  libraries and the family machines are Windows). Consequences owned
  honestly: the N-gates and §7 budgets run in Windows CI, the
  trash/hidden-attribute/path/keymap items below are v1 work, and the known
  risk — first-class without daily dogfooding — is mitigated by Windows CI
  gate runs, the oracle's Windows-format corpus, and family beta testers as
  the dogfood proxy.
- **macOS: first-class intent, gated on hardware** (*owner priority*: Mac
  users equally deserve the help; the owner has no Mac). Nothing may
  preclude it — Mac quirks like the `Picasa3` db path are already in the
  import scope, keymaps and trash semantics are designed per-platform from
  day one — and it ships as soon as there is hardware to test on (CI
  cross-builds + community testers wanted; §10 item 5). Until then it is
  blocked on access, not demoted by intent.

Cross-platform consequences owned in v1: trash semantics per platform +
library-local trash on trashless storage; hidden-file conventions (legacy
dot-files are hidden on Unix and need the hidden attribute on Windows); paths
(drive letters in imported data are translated, never stored); Ctrl/Cmd
keymaps; bundled decoders so the format matrix is identical on every platform.

**NAS/network libraries: supported, best-effort, honestly.** Picasa was
defeatist about networks; we do better but stay honest: polling freshness (no
event dependency), library-local trash, reconnect tolerance, and a documented
caveat list (e.g. dot-file-hostile NAS firmware vs our Picasa-compatible
sidecar names — compat wins in v1, revisit if it bites). NAS is not just
prose: M4's gates include a NAS-profile run (§9).

---

## 9. v1 milestones

Sequenced by the trust ladder: each milestone earns the right to the next by
passing its gate. Family-archive gates always have a synthetic-corpus proxy
that any contributor and CI can run; the archive itself (read-only copies
until M2 is proven) is the owner's additional confidence check, never the
only check. The build order mirrors the tutorial corpus's own pedagogy:
organize → edit → faces.

**M0 — Ground truth.** *Done:* format research, validated parsers
(`picasa_db.py`), Wine oracle + fixtures 001–013, this spec. *Remaining:*
stack decision (fauxcasa-6hf, gated on §7 prototype evidence); the **100k
synthetic library generator** (extend `make-synthetic-library.py`: defined
composition — file-size and EXIF-date distributions, folder shapes, duplicate
files, mixed formats — so §7 numbers are reproducible).

**M1 — See your library again.** Read-only browser over an existing Picasa
library: full ingest (ini, .pal, contacts.xml, db3 rescue, XMP) under the §4
precedence (pinned by M1 exit), instant grid, folders/albums/stars/captions/
faces displayed, search, selection tray, slideshow, stills + RAW rendering,
video indexing/playback, decode isolation per the §5 threat-model
requirement. *Gate:* N4 budgets green on the 100k synthetic library;
`picasa_db.py survey` cross-check shows zero ingest loss on synthetic
corpora; owner confirms the same on the family archive.

**M2 — Trust it with changes.** Writes: stars, captions, keywords, albums
(+order), manual sort, hide — persisted sidecar-first (§5 P1) in
Picasa-compatible ini plus tier-2 native state. Deliverables: the
reproducible oracle harness + golden-fixture fallback (§4), the N5
kill-fuzzer. *Gate:* oracle differential acceptance — real Picasa reads
everything Fauxcasa wrote, machine-checked; N1 (folder-copy), N3 (rebuild),
N5 (crash), N7 (silent-failure) gates green in CI.

**M3 — Edit without fear.** The edit room: full non-destructive stack, named
durable undo, Save / Undo Save / Revert with `.picasaoriginals` compat,
export-with-edits (incl. the email preset). *Gate:* N2 green; fixture-replay
equivalence — Fauxcasa reproduces the oracle corpus's edit semantics
(004/005 crop-save round-trip, etc.).

**M4 — Live in it.** Watching + external-change reconciliation, device
import, Move Folder, trash-everywhere, library relocation, minimal Backup
Sets, manual face tagging + people registry + face-data import, batch
rename, maintenance surface, the P1 write-back machinery (per-library
switch, offset-aware verified writer, resumable batch op — default stays
off), the minimal movie renderer, first-class Windows builds (N-gates + §7
budgets green in Windows CI). *Gate:* N6 green in CI —
both halves: app-closed external moves and the foreign-write variant;
N5/N6/§7 gates additionally pass under a NAS profile (simulated latency
acceptable); the P1 writer's round-trip verification corpus green; plus the
owner soak: one month of daily-driver use on the family archive with
measurable exit criteria — zero data-loss incidents, zero N1–N7 violations,
weekly Picasa-opens-the-library differential checks green, owner signs off.

**v1 = M1–M4.** Face recognition is the sole headline of the next release
(§5 LATER); maps, printing, gallery export follow.

**Gate coverage:** N1→M2 · N2→M3 · N3→M2 · N4→M1 (read-only rows; write/watch
rows at M2/M4) · N5→M2 (+M4 NAS) · N6→M4 · N7→M2. Formats + decode
isolation→M1. Backup, cohabitation, NAS profile, P1 write-back machinery,
movie renderer, first-class Windows CI→M4.

---

## 10. Open questions — the argument agenda

Decisions this spec makes that most deserve a fight, plus genuinely open
items. Argue here, then edit the spec.

1. **P1 in-file write policy** (§5 P1) — v1 sidecar-first with deliberate,
   verified write-back; flipping default-on is post-v1. The counter-argument:
   sidecar-first delays the interop everyone migrating *out* of Picasa wants.
2. ~~Face recognition in v1.5, not v1~~ — **settled (owner, 2026-06-11):**
   sequencing confirmed — highly desirable, pointless before
   browse/search/organize/edit exist. Stays the bound v1.5 headline.
3. **Cache location default** (§3) — machine-local vs in-library.
4. ~~Windows tier in v1~~ — **settled (owner, 2026-06-11):** first-class in
   v1; Windows and Mac users are where the people who need the most help
   live. The dogfood-gap risk is owned in §8.
5. **macOS** — intent settled (first-class as soon as testable, owner
   2026-06-11); the *open* question is access: who has the Mac hardware or
   testers, and when?
6. **Write-compat horizon** (§4) — succession via alternation (ini-compat
   through v1, native evolution later) vs indefinite cohabitation.
7. **Star model** (§3) — binary star forever vs 0–5 ratings.
8. **db3 sentinel acceptance** (fauxcasa-5kl) — experiment, may unlock deeper
   coexistence; gates nothing.
9. **Updates** (§5 Maintenance) — spec says opt-in version check, never
   auto-install; argue for fully-offline instead.
10. **Concurrency model** (§3) — single-writer + alternation vs something
    stronger for the multi-machine NAS scenario.
11. ~~V6 slideshow question~~ — **settled (owner, 2026-06-11):** both halves
    are desirable. Playback is M1; the minimal Picasa-2-level renderer rides
    M4 (§5 In & out); the full Movie Maker stays LATER.
12. **Implementation stack** — explicitly *not* this document (fauxcasa-6hf),
    but constrained by §7: any candidate must demo the scroll + cold-start
    budgets in a prototype, and set the RAM/installer budgets, at M0 exit.
    (Windows first-class in v1 — item 4 — now also weighs on the choice:
    the stack must build and test cleanly on Windows CI from the start.)
13. **Content-recognition boundaries** (§5 LATER, medium-range) — local
    models only, or also per-batch opt-in remote LLM assist? And where is
    the line between assistive suggestions (in) and auto-curation (banned by
    §1)? Needs argument before any v2 design work.

---

## Glossary

- **The oracle / Wine oracle** — real Picasa 3.9.141 running under Wine
  against a synthetic photo library on the dev machine; our ground-truth
  generator. Setup, launch, and recipes: `docs/research/wine-oracle.md`.
- **Fixture / differential fixture** — a committed before/after snapshot pair
  of the oracle's on-disk state around exactly one UI action, with a decoded
  diff (`fixtures/oracle/NNN-*/`). Thirteen exist as of this writing.
- **Differential acceptance** — the M2 gate: Fauxcasa writes a library, real
  Picasa reads it in the oracle, and the resulting diffs are machine-checked
  against expected classes (no rebuild, no rejection, state visible).
- **Sentinel acceptance** (fauxcasa-5kl) — open experiment: does Picasa accept
  a `repository.dat` version-sentinel block it didn't write itself?
- **db3** — Picasa's machine-local catalog directory (`.pmp` column files +
  caches); **ini** — the per-folder `.picasa.ini` sidecars. Formats:
  `picasa-db3-validated.md`, `picasa-ini-format.md`.
- **Succession via alternation** — v1's coexistence stance: Fauxcasa aims to
  *replace* Picasa, with a safety period where the user can switch freely
  between the two apps on one library (either direction, anytime, losslessly)
  as long as only one is open at a time. Simultaneous dual-open is
  unsupported (§3, §4).
- **Tracker IDs** (`fauxcasa-XXX`) — beads issues; `bd show <id>`.
- **The tutorial corpus** — the eight archived Picasa tutorial videos
  analyzed in `picasa-video-notes.md` (V1–V8).
