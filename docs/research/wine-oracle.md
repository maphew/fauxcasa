# The Wine Picasa oracle

Real Picasa 3.9 running under Wine on the dev machine, indexing the synthetic
photo library — our ground-truth generator for differential format testing.
Stood up and validated 2026-06-11.

## Why

Instead of trusting 2011-era format writeups, we make the real binary produce
fresh artifacts on demand: change something in Picasa, diff the resulting
`.pmp`/`.picasa.ini` bytes; later, feed Fauxcasa-written files back to real
Picasa and confirm it accepts them. All data is synthetic
(`scripts/make-synthetic-library.py`), so none of this touches the privacy
rules.

## Setup (reproducible)

1. Wine flatpak, user-level:
   `flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo`
   `flatpak install --user -y flathub app/org.winehq.Wine/x86_64/stable-25.08`
2. Synthetic library: `scripts/make-synthetic-library.py` →
   `cache/synthetic-library/` (3 folders × 8 EXIF-dated JPEGs).
3. Install Picasa silently into a dedicated prefix (installer cached, see
   `picasa-binary-notes.md` for provenance):

   ```sh
   flatpak run --filesystem=home \
     --env=WINEPREFIX=$PWD/cache/wine-oracle \
     --env=WINEDLLOVERRIDES="mscoree,mshtml=" \
     org.winehq.Wine cache/installers/picasa39-setup.exe /S
   ```
4. Pre-seed first-run scan suppression (the PicasaStarter recipe — see
   `picasastarter-notes.md`) under
   `cache/wine-oracle/drive_c/users/<user>/AppData/Local/Google/`:
   - `Picasa2Albums/watchedfolders.txt` — CRLF line:
     `Z:\...\cache\synthetic-library` (Wine maps `/` as `Z:`)
   - `Picasa2Albums/frexcludefolders.txt` — empty
   - `Picasa2/db3/thumbs_index.db` — 356-byte seed file (fetched from the
     PicasaStarter mirror's embedded resource)

## Launch

```sh
flatpak run --filesystem=home \
  --env=WINEPREFIX=$PWD/cache/wine-oracle \
  --env=WINEDLLOVERRIDES="mscoree,mshtml=" \
  org.winehq.Wine "C:\\Program Files (x86)\\Google\\Picasa3\\Picasa3.exe"
```

First interactive run asks about usage feedback and backup (decline both);
after that it shows the standard Picasa window and works. Headless runs (no
interaction) still perform the folder scan and write the database, then exit.

> **Agents driving the oracle: do NOT use the launch above.** It puts Picasa
> windows on the user's live `:0` desktop and shares their real mouse/keyboard.
> Use the **headless isolated** method below so the user's session stays free
> (windows off-screen, no input contention).

### Driving headlessly (isolated display — agent-safe)

Validated 2026-06-13 (`fauxcasa-dcc`). Lets an agent see + drive Picasa with no
windows on the user's screen and no input contention. It is also effectively
*required* on GNOME/Wayland: the main session's Xwayland `:0` runs with
`-enable-ei-portal`, so `xdotool` synthetic input is forced through GNOME's
RemoteDesktop **consent dialog** (mouse warp is silently blocked, no persistent
token). A separate headless weston Xwayland has no portal, so `xdotool`
mouse+keyboard work natively (exact 1:1 px).

1. **Start a dedicated headless compositor.** `weston` lives in the `benchbox`
   toolbox; it spawns its own Xwayland on the next free display (e.g. `:3`):

   ```sh
   toolbox run -c benchbox weston --backend=headless --xwayland \
     --width=1680 --height=1120 --socket=wayland-oracle --idle-time=0 \
     >/tmp/weston-oracle.log 2>&1 &
   grep -i 'listening on display' /tmp/weston-oracle.log   # -> the DISPLAY, e.g. :3
   ```

2. **Launch Picasa onto it.** Keep `--socket=x11` (winex11 needs that plumbing),
   but override `DISPLAY` *inside* a shell wrapper and **disable winewayland**
   (otherwise Wine grabs the user's `wayland-0` and renders there / crashes):

   ```sh
   flatpak run --filesystem=home \
     --env=WINEPREFIX=$PWD/cache/wine-oracle \
     --command=sh org.winehq.Wine -c \
     'unset WAYLAND_DISPLAY; export DISPLAY=:3; \
      export WINEDLLOVERRIDES="mscoree,mshtml=;winewayland.drv="; \
      exec wine "C:\\Program Files (x86)\\Google\\Picasa3\\Picasa3.exe"'
   ```

   `scripts/launch-oracle-picasa.sh` wraps this exact invocation
   (display via `ORACLE_DISPLAY`, default `:2`); run it detached so Picasa
   survives across agent tool calls:
   `tmux new-session -d -s oracle scripts/launch-oracle-picasa.sh`.

3. **See + drive** (all on `:3`, 1:1 device px, no HiDPI scaling):

   ```sh
   ID=$(DISPLAY=:3 xdotool search --name 'Picasa 3' | tail -1)
   DISPLAY=:3 import -window $ID /tmp/p.png                 # screenshot (ImageMagick)
   DISPLAY=:3 xdotool mousemove --window $ID X Y click 1    # left click (window-relative)
   DISPLAY=:3 xdotool click 3                               # right click (context menu)
   DISPLAY=:3 xdotool key alt+v                             # keyboard
   ```
   Dropdowns/context menus are **separate override-redirect windows**: after a
   click, list them with `xdotool search --all --maxdepth 2 ""` (pick the small
   tall geometry), `import -window <popup>` to read the items, then click an
   absolute `:3` coordinate on the chosen row. Hover the row and re-capture to
   confirm the highlight *before* committing — avoids misclicks.

**Critical gotchas**
- Do **not** pass `--nosocket=x11`: it unsets `DISPLAY` and disables winex11
  plumbing → Picasa exits silently with an empty log.
- Must disable `winewayland.drv`, or Wine renders on the user's `wayland-0`.
- The flatpak reaches the nested X via the **abstract** socket
  `@/tmp/.X11-unix/X3` (works only because the flatpak shares the host network
  namespace; a filesystem bind of the socket into the sandbox `/tmp` does not).
- **One Picasa per prefix.** Kill any other instance first
  (`pkill -f Picasa3.exe`); killing an *idle* Picasa writes nothing to the
  prefix. Never run two instances on the live oracle prefix.
- `ydotool` is a **dead end for isolation** — it injects at `/dev/uinput`
  (global), moving the user's real cursor. Use `xdotool` on the headless
  Xwayland instead.
- **Keyboard needs `xdotool key --window <id>` (XSendEvent), not plain
  `key`/`type` (XTEST).** Under headless weston, Xwayland often has no
  wayland keyboard focus, so XTEST key events (`xdotool type 'foo'`,
  `xdotool key ctrl+a`) are silently dropped while *mouse* XTEST still works —
  you see the search field's caret blink but nothing types, and `Ctrl+A`
  selects only one photo. Target the Picasa window directly:
  `xdotool key --window <picasaid> --delay 150 p h o t o 0 5`. (Mouse +
  keyboard *modifiers held during a click* — e.g. `mousemove … keydown ctrl
  click 1 keyup ctrl` in one invocation — do work via XTEST.)
- **Input dies intermittently; re-arm it.** Clicks/keys stop registering when
  (a) the Picasa surface loses focus (`xdotool getwindowfocus` returns `1`),
  or (b) a stale **drag-preview overlay** (a ~370×93 child window of Picasa)
  gets stuck over the grid — `getmouselocation` over a photo then returns the
  overlay's id, not the main window. Recover with
  `xdotool mouseup 1; xdotool mouseup 3; xdotool keyup ctrl shift alt;
  xdotool windowactivate --sync <id>; xdotool windowfocus <id>` (each a
  separate `xdotool` call — `mouseup 1 3` in one invocation errors). If that
  fails, **restart Picasa** — the prefix
  state (and any user album you built) persists on disk, so you lose only the
  in-memory selection/hold. Avoid drag gestures (slider drags, photo drags),
  which are what leave the overlay stuck.
- The **left folder/album tree and the bottom selection tray** only accept
  clicks while the window is focused — re-`windowfocus` before each, or they
  no-op silently (looks identical to a wrong coordinate).
- Capture timing: the `.picasa.ini` write can lag the `db3/` write by several
  seconds — re-run `diff` if the ini delta is missing. Re-`snapshot` right
  before each action: a "Search results" `albumdata` row reshuffle
  (tombstone+append, year-4501 date sentinel) fires on session activity after a
  folder delete and will otherwise contaminate the next diff.

Cross-session copy of this recipe: `bd recall oracle-headless-isolation`.

## Validation results (2026-06-11)

- Watched-folder seeding works: `scanlist.txt` = `+C:\` `+Z:\`;
  `thumbindex.db` contains the three synthetic folders (full paths, UTF-8/
  ASCII) and all 24 photos (stored name-only — files join to parent folders
  by index, matching the `imagedata.parent` field design).
- Database appears in `db3/` within ~20s of first launch: the full
  `.pmp` set for `albumdata`/`catdata`/`imagedata` plus `thumbs2_0.db`,
  `bigthumbs_0.db`, `previews_*`, `repository.dat`, `facetemplatesV2_0.db` —
  matching the schema vocabulary recovered from the binary
  (`picasa-binary-notes.md`).
- **Round-trip parse confirmed**: a ~30-line Python reader using the sbktech
  header layout (magic `0x3fcccccd`, type, `0x1332`, `0x00000002`, type,
  `0x1332`, count) correctly read oracle-written files:
  - `imagedata_width/height.pmp` (type 0x1 uint32) → exactly our synthetic
    dimensions; first rows are 0 (folder rows; `imagedata_filetype` = 1 for
    folders, 2 for JPEG photos; 28 rows = 24 photos + folder entries)
  - `catdata_name.pmp` (type 0x0 string) → Picasa's internal categories:
    `Labels, Projects (internal), Folders on Disk, Web Albums, Web Drive,
    Exported Pictures, Other Stuff, Hidden Folders, People`
  - `albumdata_name.pmp` → built-in virtual albums (`Starred Photos,
    Screensaver, Recently Updated, Emailed, Uploaded, ...`) followed by the
    three watched folders — folders and albums share the albumdata table.

## Differential-testing recipe

Harness: `scripts/oracle-diff.py` (stdlib-only; watches `db3/`,
`Picasa2Albums/`, and the synthetic library tree).

1. `uv run scripts/oracle-diff.py snapshot` — baseline.
2. Perform ONE action in the Picasa UI (star a photo, caption, crop, tag a
   face, make an album).
3. `uv run scripts/oracle-diff.py diff` — decoded report: row-level `.pmp`
   deltas (all sbktech field types), unified diffs for `.picasa.ini`/`.txt`/
   `.pal`, first-difference hexdump for other binaries.
4. `uv run scripts/oracle-diff.py capture <slug> --note "exact UI action"` —
   writes `fixtures/oracle/NNN-<slug>/{before,after,diff.md,meta.json}` and
   re-baselines for the next action. Thumbnail/preview caches are reported
   but not copied (`--include-blobs` overrides).
5. `uv run scripts/oracle-diff.py pmp <file.pmp>` — ad-hoc decode of any pmp.

Fixture pairs are synthetic → committable; the accumulated
`fixtures/oracle/` corpus is the ground truth for the Fauxcasa parser/writer
test suite. Session log: bead `fauxcasa-dcc`.

## Differential findings (2026-06-11/12 sessions, fixtures 001–021; + 033 cross-folder 2026-06-16)

Harvested corpus: `fixtures/oracle/001-star-photo` … `021-album-description`,
plus `033-cross-folder-batch-star` (each has `diff.md` with decoded deltas).
Action→storage map:

| Action | Synchronous (at action time) | Lazy flush (~2–6 min cycle) |
|---|---|---|
| Star | ini `star=yes` + `db3/starlist.txt` (full `Z:\` path) | nothing observed |
| Unstar | ini line deleted (never `star=no`); starlist entry removed | `inisync` tick |
| Caption | JPEG XMP `dc:description` + new IPTC 8BIM block; ini gets `backuphash` only | `imagedata_caption.pmp` (flushed immediately this time — action forced a global db write-out) |
| Keyword | JPEG XMP `dc:subject` + IPTC 2:25 Keywords; ini `backuphash` | `imagedata_tags.pmp` (type 0x6, sparse), wordhash. `tags.txt` stays empty |
| New album | `albumdata_*` row (uid, `]album:<uid>` token, date = *photo* date) + ini `[.album:<uid>]` + per-photo `albums=<uid>`. No `.pal` file | — |
| Rename album | ini `name=` rewritten in place; **uid/token stable** | `albumdata_name` row in place; album's `inisync` row = flag word (1-bit change) |
| Crop (unsaved) | ini only: `crop=rect64(...)` + `filters=crop64=1,...` (4×16-bit fixed-point fractions) | — |
| File→Save | recipe moves to `.picasaoriginals/.picasa.ini` (filters/crop + orig dims + `moddate` FILETIME); original stashed byte-exact; JPEG rewritten | edit-state pmp family (`revertable=1`, new width/height, `crop64`/`filters` **cleared** after bake) |
| Rotate (unsaved) | ini `rotate=rotate(1)` | `imagedata_rotate` `'rotate(1)'` string, `edited=2` |
| Manual face tag (013, People-panel flow) | name into JPEG XMP `dc:subject` + ini `backuphash` | `imagedata_tags` row. **Face geometry never reached disk** (no facerect/facetemplates/contacts.xml, even at exit) |
| Manual face tag (014, New Person dialog flow — the retry that worked) | ini `faces=rect64(...),<uid>` + `[Contacts2]` section; **no JPEG XMP at all** | `contacts/contacts.xml`; person album row (`]facealbum:<row>`, category 8, `albumcontactids`=uid); virtual imagedata row (`filetype=1001`) + own thumbindex entry holding the rect — see `picasa-db3-validated.md` "Empty-name records" |
| Delete photo | file moved byte-exact to OS trash (Wine: XDG trash w/ `.trashinfo`; Windows: Recycle Bin via `FOF_ALLOWUNDO`); one in-place `albums_0.db` change. **No ini change in either phase** | imagedata row **tombstoned in place** (`filetype` 2→0; table stays same row count, no pmp compaction); thumbindex entry zeroed; 'Search results' count decrements; wordhash shrinks |
| Move photo between folders (016) | file relocated byte-exact; name collision → Rename Duplicates dialog → `-001` suffix; **one thumbindex entry rewritten in place** (new name+parent). No imagedata pmp change, no ini — thumbindex is the file-identity/folder-membership table | only `albumdata_date[6]` tick + an `albums_index` stamp |
| Hide photo (017) | ini `hidden=yes` (no `backuphash` line added) | **no pmp mirror — ini-only state.** Db reacts indirectly: 'Search results' −1, folder `inisync` tick |
| Text on photo, unsaved (018) | ini `text=count;payload-len;str-len;string;font;x,y,size,rot fractions;v1,fill-ARGB,outline-ARGB,…,weight,…;;` + `textactive=1`. **No `filters=` entry** — text is its own key, outside the filter chain (crop wrote both) | `imagedata_text` = ini string **verbatim**; `textactive`=1, `edited`=1 (byte; rotate had used 2); thumbs/previews re-rendered with the overlay |
| File→Save text (019) | bake per 005: recipe → `.picasaoriginals/.picasa.ini` (`moddate` FILETIME + orig dims + verbatim recipe), original stashed byte-exact, JPEG rewritten | `text`/`textactive`/`edited` cleared, `revertable`=1, **`imagedata_originslow` uint64 set at bake** (revert-tracking key?); width/height untouched (text doesn't resize). Thumbindex row updated in place — pins row layout as (name, mtime FILETIME, u32 size, flags) |
| Folder description (020) | **whole `[Picasa]` folder-identity block** materialized in the folder's own ini — `name=`, `description=`, `date=` (epoch-days float), `P2category=` — from a single-field edit | folder row `albumdata_description` verbatim |
| Album description (021) | `description=` line inserted in the `[.album:uid]` section, which lives in the **member photos' folder ini** (albums own no file; no `.pal`). UI gotcha: the header field **silently drops input** without Enter/click-away — first attempt left zero disk trace | album row `albumdata_description` verbatim; album-row `inisync` flag word churns (low half, high half stable) |
| Cross-folder batch star (033) | one star action over **two photos in two folders** → `star=yes` added under `[photo05.jpg]` in **both** folders' `.picasa.ini` + **both** full `Z:\` paths appended to `db3/starlist.txt`, atomically. Pure N×(single-photo case 001) — **no batch-only artifact**. The first star of a session also creates the 'Starred Photos' virtual album (`albumdata_uid` +1 row). **The selection had to be made via a flat album + Ctrl+A** (see note below) | none beyond the star (star has no pmp mirror) |

General write model:

- **Cross-folder multi-select is single-folder-scoped for metadata edits.**
  Picasa 3.9 scopes a metadata-edit selection (star/tag/rotate/hide) to ONE
  source folder: `Ctrl+click` and `Shift+click` across a folder boundary
  **reset** the selection to the clicked photo's folder, in the folder view,
  the search-results view, **and even inside a flat album** that already holds
  photos from two folders. The "hold"/green-pin tray keeps cross-folder photos
  visible across navigation but per-photo edits (the bottom Star button, the
  right-click `Add to Album → Starred Photos`) apply only to the *active*
  photo, never the held set. **The only way to batch-edit across folders:**
  build an album spanning the folders (add each photo separately — one active
  photo per add), open the album (flat, no folder headers), then **`Ctrl+A`**
  — that selects all members across folders. A single edit then fans out to
  each member's source-folder ini (fixture 033). So two `.picasa.ini` files
  *are* written atomically by one action, but only through the album+`Ctrl+A`
  vehicle. (bead `fauxcasa-ezn`)

- **Two-phase**: `.picasa.ini` + the photo file (XMP/IPTC) are written
  synchronously at action time; pmp mirrors arrive on a lazy flush cycle
  (observed 2–6 min), occasionally immediately when an action forces a db
  write-out. A clean exit produced **no additional flush** — no shutdown dump.
- `backuphash=<u16>` lands in the ini for every touched photo and mirrors
  `imagedata_backuphash.pmp`.
- `albumdata_inisync`: folder rows = FILETIME of last ini sync; album rows =
  a flag word. `albumdata_date[6]` ('Search results' row) ticks on every
  action — an activity timestamp.
- Field files are sparse (`imagedata_tags.pmp` had 3 rows in a 28-row table)
  — confirms sbktech's variable-length note.
- UI grid order ≠ filename order (our synthetic photos share EXIF
  timestamps): identify the acted-on file from the diff, never from grid
  position. Wine drops/doubles keystrokes routinely — 'aption' (002),
  'synthetiic' (014), 'descriptin' (021) — always read back the stored text.
- **Idle noise floor is zero**: Picasa left running idle ~8h (overnight,
  2026-06-12) wrote nothing — any diff vs baseline is attributable to the
  last action.
- Save dialog has "do not ask again" checked since fixture 005; delete
  confirmation likewise suppressed since fixture 015.

## Scale runs

For indexing-at-scale experiments, clone the prefix and point the clone's
`watchedfolders.txt` at a bigger generated library
(`scripts/make-synthetic-library.py --scale N`) — full recipe and the
1000-photo results are in `oracle-db3-survey.md`. Never repoint or reset the
oracle prefix itself: it is the live baseline for differential fixtures.

## Capturing differentials on a disposable clone (fauxcasa-zve)

Some actions create **new files + permanent db3 rows in the watched tree**
(collage/export, and anything that registers a new album/category). Capturing
those on the live oracle would contaminate the baseline forever, so they run on
a **disposable clone** of the prefix — same `dosdevices/z:`-staging trick as the
sentinel experiment (`oracle-db3-survey.md`), which keeps the `Z:\` paths baked
into db3 resolving to clone-local copies and makes the live prefix/library
**unreachable** from the clone's drive mappings:

```sh
CLONE=cache/wine-oracle-export; ZROOT=$CLONE-z
STAGED=$ZROOT/var/home/matt/dev/fauxcasa/cache/synthetic-library
cp -a --reflink=auto cache/wine-oracle "$CLONE"          # instant CoW
mkdir -p "$(dirname "$STAGED")"
cp -a --reflink=auto cache/synthetic-library "$STAGED"
ln -sfn "$PWD/$ZROOT" "$CLONE/dosdevices/z:"             # Z: -> staged tree
for d in Desktop Documents Downloads Pictures; do        # exports land IN the clone
  rm -f "$CLONE/drive_c/users/matt/$d" && mkdir -p "$CLONE/drive_c/users/matt/$d"; done
```

Then point the **diff harness** at the clone with three env vars (defaults
reproduce the live-oracle behavior exactly), so the live baseline cache is
untouched:

```sh
export ORACLE_PREFIX=$PWD/cache/wine-oracle-export
export ORACLE_LIBRARY=$PWD/cache/wine-oracle-export-z/var/home/matt/dev/fauxcasa/cache/synthetic-library
export ORACLE_SNAPDIR=$PWD/cache/oracle-snapshots-clone
uv run scripts/oracle-diff.py snapshot   # ... act in Picasa ... then diff / capture
```

Launch Picasa with `WINEPREFIX=$CLONE` via the headless recipe above.

**Gotcha — the dead Places map busy-loops and blocks all input.** If the cloned
prefix's `active_metadata_tab` (registry `Software\Google\Picasa\Picasa2\
Preferences`) is `thumbui/places_toggle`, Picasa opens Places on launch and
spins forever on the retired Maps endpoint via `ieframe` (the `wine.log` fills
with `ieframe:bind_to_object failed` at tens of KB/s) — the UI thread never
processes clicks or keys. Fix before relaunching: set that key to `""` in
`user.reg` while Picasa is down, **and** add `ieframe` to the disabled DLLs
(`WINEDLLOVERRIDES="mscoree,mshtml,ieframe=;winewayland.drv="`) so the map
control fails fast instead of looping. `ieframe` is browser-only — orthogonal to
the photo catalog, so disabling it doesn't affect export/collage db3 writes.

**Gotcha — coordinates.** `import -window <id>` screenshots are *window-relative*;
so are `xdotool mousemove --window <id> X Y` clicks. Use `--window` (or add the
window's screen offset) — feeding raw image pixels to a screen-absolute
`mousemove` lands the click in empty space and it silently does nothing.

First findings — the Create/output differentials this clone enabled:

- **Export to folder (030):** to the **default (unwatched)** `Pictures\Picasa\
  Exports` location, Picasa writes the resized JPEGs + a `.picasa.ini` folder stub
  (`[Picasa]` / `P2category=Exported Pictures` / `date=`) but **no db3 row and no
  tree node** — the category mark is ini-only (cf hide-photo 017).
  `imagedata_originslow` (content-hash photo id) gets stamped for every source
  photo the export read.
- **Picture collage (031):** by contrast, a collage written to the *same* unwatched
  `Pictures\Picasa\Collages\` tree **is** tracked in db3 — a category-1 `Collages`
  albumdata row (= catdata "Projects (internal)") + two imagedata rows (the folder
  + the indexed 5120×3413 render), and the column `imagedata_fileflags.pmp`
  materializes. On disk it leaves `.picasa.ini` (`P2category=Projects (internal)`),
  a `.cxf` collage-recipe XML (layout `<node>`s keyed by each photo's `originslow`
  hash, `[Z]` portable drive token), and the rendered JPEG. So "Projects" are
  catalogued; plain exports are not.
