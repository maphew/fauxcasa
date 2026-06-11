# Picasa tutorial video research notes

**What this is.** Consolidated observations from eight public tutorial videos about Google
Picasa (versions 2 through 3.5), gathered as raw material for the Fauxcasa product spec.
This is an evidence record, not the spec itself: it describes what the videos actually show,
reconciled and deduplicated across all eight.

**How it was produced.** Each video listed in `begin.md` was analyzed by extracting still
frames (scene changes plus sampled frames) and pairing them with the spoken/captioned
transcript. Features, UI layout, behaviors, and workflows were recorded per video, then
merged here. Where the narration and the on-screen pixels disagree, the pixels win (noted
inline). Date: 2026-06-11.

**Source videos** (codes used as evidence markers throughout):

| Code | Video ID | Title | Picasa version shown |
|------|-------------|-------|----------------------|
| V1 | drf6OTywF6E | Picasa 3.5 Instructional Video - Part 1: Organizing | 3.5 |
| V2 | PuNjY6Zq6Cw | Picasa 3.5 - Part 2: Basic Fixes and More | 3.5 |
| V3 | 4L7IV_3xKKM | Importing Photos in Picasa 3.5 | 3.5 |
| V4 | sOOkONjzcss | Backup photos with Picasa | 2 |
| V5 | c1_Tbs5e6Ag | Google Picasa 3 - Video 2 - Running Picasa 3 for the First Time | 3 |
| V6 | ArB400dG0sU | Simple Photo Slideshow with Picasa | 2 |
| V7 | De1U5mAqCTM | Google Picasa | 3 |
| V8 | YfqThYqDs-8 | Picasa 3.5: Managing Thousands of Pictures on External Drives | 3.5 |

Two videos (V4, V6) show Picasa 2; chrome details differ slightly (window title "Picasa2",
toolbar has Slideshow/Timeline/Gift CD buttons) but the core model is the same.

---

## 1. Consolidated feature inventory

### 1.1 Library and organizing

- **Folders collection (disk mirror).** The sidebar "Folders" section lists every folder on
  disk that contains photos; on install Picasa scans the machine and reproduces the existing
  folder layout and names automatically, each with a photo count in parentheses
  (e.g. "27 Atwood (14)"). [V1, V5, V7, V8]
- **Flat vs tree folder views.** Two toolbar icons toggle the Folders list between a flat
  list (alphabetical in V1; date-sorted with plain-text year heading rows like "2007",
  "2006" in V5/V7/V8 — both flat modes hide drive locations) and a hierarchical tree rooted
  at My Computer / drive letters (C:, P:, S:) with expansion triangles and aggregate counts
  per drive node. Because flat view flattens distinct disk paths, duplicate folder names
  can coexist in it (two "442a" entries, two "Bridget" groups, multiple "AddingAdmin"; V1).
  Tooltips: "Set view to show flat folder structure" / "Set view to show
  folder tree structure". [V1, V5, V8]
- **Folder sort dropdown.** Next to the view toggles: Sort by Date / Recent Changes / Size /
  Name, plus people-sort options (by Name / Amount / Top 10), a Shortcuts submenu, and view
  options (Show Thumbnails in Library, Simplified Tree View) all in one menu. [V1]
- **Albums (virtual collections).** User-created groupings that reference photos without
  copying them or creating anything on disk. System auto-albums: "Recently Updated" exists
  from the first scan (V5 shows it immediately after first run); "Starred Photos" is seen
  in every established library but was not observed at first run. [V1, V2, V3, V5, V7]
- **Album creation (multiple entry points).** File > New Album, the tray's blue
  "Add selected items to an Album" button dropdown, and a toolbar icon all open the same
  Album Properties dialog (name defaulting to "Untitled" pre-selected, date defaulting to
  today, optional slideshow/movie music with a Browse... button that stays disabled until
  the Music checkbox is ticked, optional place taken and description). [V1, V2]
- **Drag-and-drop into albums.** Drag one thumbnail or a Ctrl+click (Cmd on Mac)
  multi-selection onto an album row in the sidebar; the row highlights before drop. Photos
  remain in their folders. [V2]
- **Remove from album vs delete from folder.** Delete key in an album asks to "remove the
  selected image from the current album" (button "Remove Image", with don't-ask-again);
  the file stays on disk and in its folder. Deleting from a folder removes the photo from
  the folder *and* every album. [V1, V2]
- **Starring.** Star button in the bottom tray toggles a yellow star badge on the thumbnail's
  lower-right corner; starred photos automatically join the "Starred Photos" album, which
  spans the whole library. Stars can also be set during import. [V2, V3]
- **Selection tray (photo tray).** Persistent bottom-left well showing the current selection
  — either thumbnails of selected photos or a green chip whose label is keyed to the
  selection type: "Folder Selected - 14 photos" or "Album Selected - 4 photos" (V2) —
  with Hold (green pushpin), Clear (red circle), and add-to-album controls. All output actions
  consume the tray contents. Hold/Clear/Add are grayed when only a folder is selected (V6).
  [V1, V2, V4, V6, V7, V8]
- **Context-sensitive status bar.** Blue strip above the tray: for a collection it shows
  count, date or date range, and size on disk ("14 pictures   Sep 4, 2009   1.2MB on disk";
  "84 pictures   Apr 26, 2004 to Sep 4, 2009   44.0MB on disk"); for a single photo it shows
  filename, capture timestamp, pixel dimensions, file size ("IMG_3591.jpg   8/29/2009
  3:09:26 PM   800x533 pixels   83KB"). In V8 the folder mode additionally shows a tag
  histogram ("Tags: solomons (41), castle (41), river (27)..."). [V1, V2, V4, V6, V8]
- **Filters bar and search.** Toolbar cluster labeled "Filters": star, uploaded, faces,
  movie, and geotag toggle icons plus a small date slider, with a free-text search box at
  the far right. [V1, V2, V5, V8]
- **Thumbnail zoom slider.** Bottom-right slider rescales the whole grid live; tooltip
  "click and drag over photos to magnify them". [V1, V5, V6]
- **Custom scrollbar with folder-jump buttons.** The grid's scrollbar thumb recenters rather
  than tracking position (narrator flags it as confusing); extra buttons jump to the top of
  a folder or the bottom of the entire library. Scroll wheel scrolls the continuous library
  smoothly. [V1]
- **Folder context menu.** Right-click on a folder: Expand All, Collapse All, Edit Folder
  Description..., Select All Pictures (Ctrl+A), Clear Selection (Ctrl+D), Invert Selection
  (Ctrl+I), Move to Collection >, Refresh Thumbnails, Sort Folder By >, Hide Folder,
  Locate on Disk (Ctrl+Enter), Remove from Picasa..., Move Folder... [V8]
- **Folder Manager (referenced).** Tools-menu utility for choosing which folders Picasa
  displays/watches; named by the first-run dialog but not demonstrated. [V5]
- **Timeline and Gift CD** (Picasa 2 toolbar buttons; not demonstrated). [V4, V6]

### 1.2 Editing

- **Edit view ("darkroom").** Double-click a thumbnail to replace the library with a
  single-photo edit view: "Back to Library" button, filmstrip of the folder's siblings with
  prev/next arrows and a Play button along the top, photo on a gray canvas, "Make a caption!"
  field under the photo, and a status line "folder > filename  timestamp  WxH pixels  size
  (n of m)". [V2, V7]
- **Basic Fixes tab.** Crop, Straighten ("Fix a crooked photo"), Redeye, I'm Feeling Lucky
  (one-click auto fix), Auto Contrast, Auto Color, Retouch, Text, plus a Fill Light slider.
  [V2, V7]
- **Tuning tab.** Sliders for Fill Light, Highlights, Shadows, Color Temperature; a Neutral
  Color Picker eyedropper; one-click auto (magic-wand) buttons beside the slider groups.
  [V2, V7]
- **Effects tab.** Twelve effect tiles with live previews rendered from the current photo:
  Sharpen, Sepia, B&W, Warmify, Film Grain, Tint, Saturation, Soft Focus, Glow, Filtered
  B&W, Focal B&W, Graduated Tint. One-click effects carry a blue "1" corner badge; others
  open a slider sub-panel with Apply/Cancel. The evidence conflicts on which tiles are
  which: V7 lists Sharpen among the "1"-badged one-click effects yet also shows Sharpen
  opening an Amount-slider sub-panel, and V2's narration calls all twelve tiles one-click —
  unresolved. [V2, V7]
- **Crop tool.** Selecting Crop swaps the left panel for a "Crop Photo" panel with an
  aspect-ratio dropdown: Manual, "Current ratio: W x H", "4 x 6: Small print", "5 x 7:
  Large print", "8.5 x 11: Letter paper", "8 x 10", "Square: CD Cover", "4:3: Standard
  screen", "16:10: Widescreen monitor", "16:9: HDTV". Presets constrain proportion only,
  not print size. Outside the crop box is dimmed; Manual mode offers three auto-suggested
  crop thumbnails. Buttons: Rotate, Preview, Reset, Apply (green check), Cancel (red X).
  Preview flashes the result for ~2 seconds and reverts. After Apply, the tool relabels
  "Recrop". [V2]
- **Operation-labeled, persistent undo/redo.** Undo/Redo buttons at the panel bottom name
  the exact operation ("Undo Crop", "Undo Auto Contrast", "Undo Sepia", "Redo I'm Feeling
  Lucky"); the full stack can be unwound to the original photo, and it survives closing
  Picasa or rebooting — the per-photo history is stored durably. [V2, V7]
- **Histogram & Camera Information panel.** Live RGB histogram (updates as effects apply,
  shows "Loading…" on photo switch) plus EXIF readout ("No EXIF data available" when
  absent), at the bottom of the edit panel. [V2]

### 1.3 Import

- **Import tab.** Import is a workspace tab next to "Library" (with a close X), not a
  dialog; it opens automatically when a camera or memory card is attached. [V1, V3]
- **Source selector & acquisition.** "Import from:" device dropdown (e.g. "Removable
  Drive(F:\)"); files are acquired asynchronously with a live counter ("Acquired 0 files" →
  "Acquired 15 files") and gray placeholder thumbnails that fill in one by one; the empty
  grid reads "No photos available". [V3]
- **Exclude Duplicates.** Checkbox, on by default — skips photos already in the library. [V3]
- **Per-photo star and exclude in the import tray.** A star button (yellow badge; stars
  persist into the library) and a red circle-with-X exclude button sit under the thumbnail
  grid; the right pane shows a large preview with a metadata header (filename, capture
  date/time, size) and rotate buttons. Exclusion is per-photo and overrides Import All —
  excluded photos are skipped even by it, not just by Import Selected. [V3]
- **Destination controls.** "Import to:" (default My Pictures) and "Folder title:" (defaults
  to the import date in YYYY-MM-DD form). The folder title also names the resulting web
  album. [V3]
- **After Copying policy.** Dropdown: "Leave card alone" (default), "Delete only copied
  photos", "Delete everything on card". [V3]
- **Upload during import.** "Upload" checkbox enables an Options dropdown — privacy (Public
  ✓ / Unlisted / Sign-In Required to View), upload size (640 / 1024 / 1600 ✓ Max. Width /
  Original Size Images), "Starred Images Only" toggle — plus "Share with: Nobody" and
  "+ Add". Upload starts automatically after import completes. [V3]
- **Action buttons.** "Import All" (green check), "Import Selected (N)" with a live count,
  "Cancel" (red X); both import buttons stay disabled until photos are acquired. [V3]

### 1.4 Sharing, export, and output actions

- **Output action row.** Persistent bottom-bar buttons acting on the selection tray:
  Upload, Email, Print, Export, Shop, BlogThis!, Collage, Movie, Geo-Tag (Google Earth
  icon); V8 (3.5) also shows Facebook. The Picasa 2 equivalent row is Web Album, Email,
  Print, Order Prints, BlogThis!, Collage, Export. [V1, V2, V4, V5, V6, V7, V8]
- **Picasa Web Albums sign-in.** Account state lives in the window chrome top-right
  (email | Web Albums | Sign Out, or "Sign In to Web Albums" when signed out); a modal
  sign-in dialog takes a Google account. [V1, V3, V5, V7, V8]
- **Sync to Web.** Per-folder/per-album toggle in each grid group header. The "Sync Album
  to Web" dialog confirms upload with current settings (1600px max width, Public Album)
  and offers Change Settings... and don't-ask-again. Once enabled, edits in Picasa mirror
  to the online album immediately; synced thumbnails carry a green up-arrow badge, and
  synced folders show a "View <album>: public (N)" link in their header. [V7, V8]
- **Share button.** Each grid group header also has a Share split-button. [V1, V2, V5, V7, V8]
- **Picasa Web Albums (web side).** Album list with counts and sort (Album date / Upload
  date); per-photo page with caption, comments + Subscribe, tags, photo information
  (date, dimensions, size), location, and a rights/reuse setting. [V3, V7]

### 1.5 Backup

- **Backup Pictures (Tools menu).** Switches the whole main window into a backup mode: a
  "Backup your photos" banner replaces the toolbar, checkboxes appear inline next to every
  folder in the Library tree, and the photo tray is replaced by a two-step wizard. [V4]
- **Backup Sets.** Named, persistent configurations recording the destination *and* which
  files have already been backed up, so re-runs show only new files (incremental backup).
  Managed via a dropdown with Edit Set / Delete Set. [V4]
- **Live dual-unit disc estimator.** A strip totals the checked selection as folders +
  files + size and converts to media in both units at once ("10 folders 1299 files (3.3GB)
  6 CDs or 1 DVD"), recomputing on every checkbox toggle. [V4]
- **Burn with multi-disc spanning.** Burn / Eject / Cancel / Help action column; when one
  disc fills, Picasa prompts for the next. Eject is grayed until a burn is underway. [V4]

### 1.6 Slideshow and movie creation

- **Slideshow.** Green Play button on every folder/album header and atop the edit view
  starts a slideshow; Picasa 2 also has a toolbar Slideshow button. [V1, V4, V5, V6, V7]
- **Create > Movie (Picasa 2).** Minimal dialog: "Delay between pictures" dropdown and a
  three-option size radio (Small 320x240, Large 640x480, "Widescreen" 960x720 — actually
  4:3). Acts on the current selection; a second confirmation precedes rendering. Output is
  auto-named after the source folder, gets a generated title slide (folder name + date) and
  pan/zoom transitions, no audio; on completion Picasa opens the OS file manager at the
  output file. Observed output: 6 photos / 2s delay → 14s, 320x240, 95.5 MB. [V6]
- **Movie Maker (Picasa 3).** Full-window mode with Movie / Slide / Clips tabs: Audio Track
  with Load.../Clear, "Fit photos into audio" option (auto-computes and locks slide
  duration), Transition Style (default Dissolve), Slide Duration (default 4.0s), Overlap
  (30%), Dimensions (640x480), Show Captions and Full frame photo crop checkboxes; preview
  player at right; clip tray with auto-generated title text slide; YouTube upload, Close,
  and Create Movie buttons. [V7]

### 1.7 People / faces

- **Background face scanning.** A "People" sidebar collection shows a live inline progress
  item ("Scanning, 17% complete", advancing over time) while face detection runs in the
  background with the UI fully interactive. People sorting options (Name / Amount / Top 10)
  appear in the sort dropdown. People panel toggle button at bottom right. [V1, V2]
  (Face tagging itself is deferred in the tutorials and not demonstrated.)

### 1.8 Geotagging / places

- **Geo-Tag output button** (Google Earth icon) in the action row; **Places panel** toggle
  at bottom right; geotagged photos get a green diamond corner badge on thumbnails; a
  geotag filter exists in the Filters bar; the web side shows photo location. [V1, V2, V7, V8]
  (No end-to-end geotagging workflow shown.)

### 1.9 External drives and folder management

- **Move Folder...** Right-click command relocating a folder (with photos and subfolders)
  anywhere, including another physical drive, while keeping all Picasa metadata (albums,
  people, places, tags) intact — the explicit reason to move inside Picasa instead of
  Explorer. Destination chosen in an OS "Browse For Folder" dialog augmented with a
  live-updating caption "Move Folder to S:\Pictures\2006\" and Make New Folder. [V8]
- **Move progress popup.** Cross-drive moves are copy-then-delete with a compact overlay:
  Picasa logo, progress bar, "Moving 75 of 318 (10.5MB/s)". Same-drive moves are
  near-instant renames with no visible progress. Cross-filesystem moves (FAT32 P: to NTFS
  S:) are handled transparently, with no warnings or behavior differences. [V8]
- **Live count recalculation.** Drive, folder, collection, and status-bar counts all update
  in real time as moves complete (P: 1,276 → 968 → 128 across two moves). [V8]
- **Empty-parent limitation.** Picasa only manages folders that *directly* contain media;
  a parent holding only subfolders gets a plain manila icon (vs the photo-thumbnail icon of
  managed folders) and cannot be moved. Demonstrated workaround: copy one photo into it via
  Explorer, after which Picasa picks it up automatically and the move (carrying all
  subfolders) works. [V8]
- **Watched filesystem.** Files added via Explorer appear in Picasa automatically, with no
  manual rescan (a Refresh Thumbnails command also exists). [V8]

### 1.10 Installation and first run

- **Lightweight installer.** ~9.6 MB download from picasa.google.com; site pitches four
  pillars (Organize / Edit / Create / Share); requirements Windows XP/Vista or Linux,
  256MB RAM, 100MB disk. [V7]
- **First-run scan dialog.** Two radio options: "Completely scan my computer for pictures"
  (default) vs "Only scan My Documents, My Pictures, and the Desktop"; footer states
  "Scanning for pictures never moves or copies files to new locations" and points to the
  Folder Manager (Tools menu); single Continue button. [V5, V7]
- **Picasa Photo Viewer registration.** Second first-run dialog: opt-in per file type
  (.JPG, .TIF/.TIFF, .BMP, .GIF, .PNG, .TGA, RAW), each annotated with the currently
  registered handler; Default / Select All / Select None; or "Don't use Picasa Photo
  Viewer". The viewer is a separate component installed alongside the library app. The
  dialog itself is shown only in V7; V5's burned-in caption merely notes that a viewer is
  installed into Windows Explorer — the dialog and viewer are never on screen there. [V7]
- **Background scan with toast.** The library is browsable while scanning continues; a
  dismissible gray toast streams the folder and file currently being indexed, with a
  thumbnail — in the lower-right of the grid in V5; V7's "Folder Scanned" toast appears at
  the lower-left. [V5, V7]

---

## 2. UI anatomy

Reconciled screen layout of the main window (Picasa 3/3.5; Picasa 2 differences noted).

**Window chrome.** Title "Picasa 3" (Picasa 2: "Picasa2"). Menu bar: File, Edit, View,
Folder, Picture, Create, Tools, Help — the "Folder" menu is contextually replaced by
"Album" when an album is selected (V2). Top-right account strip: signed-in email |
Web Albums | Sign Out (or "Sign In to Web Albums"; V3 adds a Feedback link; V7's signed-in
strip also includes a Screensaver link: email | Screensaver | Web Albums | Sign Out).

**Tab strip** (3.x): "Library" and "Import" tabs below the menu bar; the Import tab has a
close X. Other full-window modes (edit view, Movie Maker, backup mode) replace the library
pane rather than adding tabs.

**Top toolbar.** Import button (camera icon + dropdown) at far left; add-album icon;
flat-list / tree-list view toggle icons with a sort dropdown arrow; then a "Filters"
cluster (star, uploaded, face, movie, geotag toggles + small date slider); wide search box
with magnifier at far right; activity/spinner icon at the very end. Picasa 2 instead has
Import, Slideshow, Timeline, Gift CD and a search field.

**Left sidebar.** Collapsible collections, each header with a green disclosure triangle and
a live count, every child row with an icon and "(photo count)":
- **Albums (N)** — auto-albums (Recently Updated, Starred Photos) plus user albums; blue
  book icons. The Albums list is itself grouped under year headers in V3 (2009/2008) and
  V6 — year grouping is not exclusive to Folders.
- **People (N)** — includes the inline "Scanning, n% complete" progress row during face
  detection.
- **Projects (N)** — e.g. Captured Videos, Screen Captures. (3.5)
- **Folders (N)** — flat or tree mode. Flat mode hides drives and groups under plain-text
  year header rows; tree mode roots at My Computer / drive letters with aggregate counts.
  Managed-folder rows use miniature photo thumbnails as icons; photo-less parents show a
  plain manila folder.
- Trailing collections seen variously: **Other Stuff** (collects folders found outside the
  standard photo locations, V5), **Web Albums**, **Downloaded Albums**.
The Folders header count differs between flat and tree modes (252 vs 619 in V1; 418 vs 561
in V8) because flat mode lists only photo-containing folders. Selected folder rows carry a
small up-arrow badge button at their right edge (V8) — present in the UI but unexplained
in the source videos.

**Main pane (lightbox).** One continuous vertical scroll across all groups (not per-folder
pages). Each group header: folder/album icon, name, date ("Sep 4, 2009" / "10 January
2007"), green Play (slideshow) button plus small create/action icons, star and save icons
(V8), a "Share" split button, a right-aligned "Sync to Web" toggle, an "Add a description"
inline placeholder, and for synced folders a "View <album>: public (N)" link. Picasa 2
headers also carry a "Select Starred" control (a greyed button alongside "Save Changes
(100+)" in V4; a link in V6). The header of
the group at the current scroll position stays pinned at the top. Selected thumbnails get a
blue border; thumbnails carry corner badges (yellow star = starred, green up-arrow =
synced/uploaded, green diamond = geotagged). While thumbnails hover/load, a floating
identification panel appears at the grid's bottom-right showing the photo's album and
filename ("Orchha Fall / DSC05725.JPG"; V7). Right edge: the custom scrollbar with
folder-jump buttons.

**Status bar.** Thin blue strip between grid and tray; dual mode (collection aggregate vs
single-photo metadata; see §1.1), plus tag histogram in folder mode (V8).

**Bottom tray bar** (left to right). Selection tray well (thumbnails or "Folder Selected -
N photos" chip) with Hold (green pushpin), Clear (red circle), and blue add-to-album
controls; small star / rotate-left / rotate-right (and tag, V7) buttons; the large output
action buttons (§1.4); thumbnail zoom slider; People / Places / Tags panel toggle buttons
at far right.

**Edit view.** "Back to Library" arrow top-left; top strip with Play button, sibling
filmstrip with prev/next arrows, and an external-edit icon; left panel with three rounded
tabs (Basic Fixes / Tuning / Effects), operation-labeled Undo/Redo buttons, and the
Histogram & Camera Information section at the bottom; photo on gray canvas with
"Make a caption!" bar; status line with breadcrumb, timestamp, dimensions, size, "(n of m)";
zoom slider plus 1:1 / fit toggles bottom-right.

**Import screen.** Header row: "Import from:" device dropdown, "Exclude Duplicates"
checkbox, "Acquired N files" counter. Body: scrollable thumbnail grid (left) with star and
exclude icon buttons beneath it; large preview (right) with metadata header, prev/next
arrows, rotate buttons. Bottom options bar reads like a sentence: Import to [location] /
[Folder title] | After Copying [policy] | Upload [Options, Share with + Add]. Bottom-right:
Import All (green check) / Import Selected (N) / Cancel (red X).

**Backup mode** (V4, Picasa 2). Green "Backup your photos" banner replaces the toolbar;
checkboxes appear beside every folder in the tree; live total strip; two numbered wizard
cards ("1 Create a Set or use an existing one" with set dropdown/Edit Set/Delete Set;
"2 Choose Folders & Albums to Backup" with Select All / Select None); vertical action
column at right: Burn (green check), Eject, Cancel (red X), Help (blue ?).

**Movie Maker** (V7, Picasa 3). Full-window mode: left settings panel with Movie / Slide /
Clips tabs; right preview player with timecode, volume, fullscreen; bottom clip tray with
slide thumbnails, add-text-slide (green +) and remove (red X); YouTube, Close, Create Movie
buttons.

**Dialogs observed.** First-run scan dialog; Photo Viewer file-type dialog; Album
Properties; album-removal confirmation; Create Movie (Picasa 2); Web Albums sign-in;
Sync Album to Web; Browse For Folder (move destination, with live path caption).

---

## 3. Key product behaviors

The recurring traits that define how Picasa feels — each observed in at least one video,
most in several.

1. **The filesystem is the source of truth; the database is an index.** Scanning "never
   moves or copies files to new locations" (first-run dialog, V5). Folders in the sidebar
   mirror disk exactly; folder names, dates and locations come from disk. [V1, V5, V8]
2. **Albums are a virtual layer over folders.** Albums reference photos; nothing appears on
   disk. Removing from an album never touches the file; deleting from a folder cascades to
   every album, because the folder is where the photo actually lives. A "Recently Updated"
   auto-album exists from the first scan (V5); "Starred Photos" appears in every
   established library but was not observed at first run. [V1, V2, V5]
3. **Editing is non-destructive with a durable, per-photo, named edit log.** Every
   operation can be undone/redone in order at any time — including after quitting or
   rebooting — and the undo/redo buttons are labeled with the specific operation. Tool
   buttons reflect history (Crop → "Recrop"; Auto Contrast disables once applied). [V2, V7]
4. **Watched folders / live indexing.** Changes made outside Picasa (a file copied in via
   Explorer) show up automatically (V8); scanning and face detection run in the background while
   the UI stays fully interactive, with progress surfaced inline (sidebar item, dismissible
   toast) rather than in modal dialogs. [V1, V2, V5, V8]
5. **Selection is a first-class object.** The bottom-left tray persists the selection
   across navigation (Hold/Clear), shows exactly what is selected ("Folder Selected - N
   photos" / "Album Selected - N photos" vs photo thumbnails), and every output action (Email, Print, Upload, Export,
   Movie...) consumes it. Action buttons gray out when the selection can't feed them.
   [V1, V2, V5, V6, V7, V8]
6. **Counts everywhere, always live.** Every collection header, album, folder, and drive
   node shows a parenthesized count; counts recompute immediately on star, move, import,
   or delete (V8 shows drive counts dropping within seconds of a cross-drive move).
   [V1, V2, V4, V5, V7, V8]
7. **Non-destructive defaults.** Import defaults to "Leave card alone"; Exclude Duplicates
   is on; upload is off; scan is index-only; album removal asks for confirmation. The
   destructive paths (delete from card, delete from folder) require explicit choice.
   [V3, V5]
8. **Metadata travels with Picasa-managed operations.** Move Folder preserves albums,
   people, places, and tags across drives; moving via Explorer loses them. Same-volume
   moves are instant renames; cross-volume moves are copy-verify-delete with per-file
   progress and MB/s. [V8]
9. **Modes, not dialog mazes.** Import, edit, backup, and Movie Maker each take over the
   main window (tab or full-window mode) while reusing the same chrome, rather than opening
   stacks of modal dialogs. [V2, V3, V4, V7]
10. **Lightweight and fast.** ~10 MB installer, 256MB RAM requirement; a first-run scan
    populates a browsable 14-folder library with the taskbar clock advancing only one
    minute (22:53 → 22:54 — though the video may be edited, so this is indicative, not a
    measured fact) on XP-era hardware; thumbnail zoom reflows instantly; cross-drive moves
    report ~10MB/s. [V5, V7, V8]
11. **Context-sensitivity as a design language.** The status bar, the "Folder"/"Album"
    menu, the selection chip, button enablement, and even folder icons (thumbnail = managed,
    manila = unmanaged) all change with context. [V1, V2, V6, V8]
12. **Web integration is opt-in and album-centric.** Sign-in lives in the chrome; Sync to
    Web is a per-folder toggle with size/visibility settings; sync state is shown as
    thumbnail badges; once enabled, desktop edits mirror online immediately. [V3, V7, V8]

Data cautions noted during analysis: narration occasionally contradicts the pixels
(V1: "69 photos" vs sidebar "Bday 09 (62)"; V4: "299 pictures" vs strip "1299 files") —
the on-screen values were taken as authoritative. V4 also shows a tree count of (241) vs a
backup strip count of 244 files for the same folder, suggesting photo counts and backup
file counts are computed over different file sets.

---

## 4. Demonstrated workflows

End-to-end flows actually performed on camera.

**W1. First-run setup (V5, V7).**
1. Launch Picasa for the first time; main window opens fully chromed but empty.
2. First-run dialog: choose scan scope (whole computer [default] vs My Documents +
   My Pictures + Desktop); Continue.
3. (V7) Second dialog: choose which file types Picasa Photo Viewer should open; Finish.
4. Library populates as the background scan indexes photos in place; a dismissible toast
   shows the current folder/file; sidebar fills with year-grouped folders and counts; a
   "Recently Updated" auto-album already exists.

**W2. Import from camera/card (V3).**
1. Attach camera or insert card; the Import tab opens automatically.
2. Pick the device in "Import from:"; leave Exclude Duplicates checked; wait for
   acquisition ("Acquired 15 files").
3. Optionally star photos (for later starred-only upload), select specific photos
   ("Import Selected (N)" live-updates), or exclude photos with the red exclude button.
4. Set "Import to:" (default My Pictures) and "Folder title:" (defaults to import date).
5. Choose After Copying policy (default: Leave card alone).
6. Optionally check Upload and set privacy / max size / Starred Images Only in Options.
7. Click Import All or Import Selected (N); if Upload was checked, the web upload starts
   automatically, with the folder title as the album name.

**W3. Create and populate an album (V1, V2).**
1. File > New Album, the tray's blue add-to-album dropdown, or the toolbar icon — all open
   the Album Properties dialog.
2. Name the album (over the pre-selected "Untitled"); optionally set date, slideshow music
   (Browse... enables only after ticking the Music checkbox), place, description; OK.
   Album appears in the sidebar with count (0).
3. Drag a photo onto the album row (wait for highlight), or Ctrl/Cmd+click several photos
   and drag the multi-selection. Photos stay in their folders.
4. To remove: select the photo in the album, press Delete, confirm "Remove Image" — the
   file remains in its folder.

**W4. Star photos and review favorites (V2).**
1. Select a photo; click the star button in the tray (yellow badge appears; second click
   un-stars).
2. Open Albums > Starred Photos to see every starred photo across the library, with the
   spanning date range in the status bar.

**W5. Edit a photo (V2, V7).**
1. Double-click a thumbnail to enter the edit view.
2. Apply Basic Fixes (Crop, Straighten, Redeye, I'm Feeling Lucky, Auto Contrast, Auto
   Color, Retouch, Text, Fill Light), fine-tune in Tuning, and/or apply Effects.
3. Crop flow: pick an aspect preset or Manual; drag the box (outside dims); Preview flashes
   the result ~2s and reverts; adjust; Apply commits (or Reset/Cancel).
4. Unwind anything with the operation-named Undo button, step forward with Redo — at any
   time, including after restart.
5. "Back to Library" when done.

**W6. Share via Web Albums / Sync to Web (V7).**
1. Select photos/album, click Upload (or the folder header's Sync to Web toggle).
2. Sign in with a Google account if needed.
3. Confirm Sync Album to Web (defaults: 1600px max, Public; Change Settings... available).
4. Thumbnails gain green up-arrow badges; subsequent Picasa edits mirror online
   immediately; viewers caption/comment/tag on picasaweb.

**W7. Back up to CD/DVD (V4).**
1. Tools > Backup Pictures; window enters backup mode (checkboxes on every folder).
2. Pick or create a Backup Set (records destination + already-backed-up files).
3. Check folders to include (or Select All); watch the live strip total
   ("...3.3GB — 6 CDs or 1 DVD").
4. Click Burn, insert a disc; Picasa prompts for additional discs as each fills.
5. Re-running later shows only files not previously backed up.

**W8. Make a slideshow movie (V6 — Picasa 2; V7 — Picasa 3).**
- V6: select photos → Create > Movie → set delay (2s) and size (320x240/640x480/960x720) →
  OK, then OK again → Picasa renders and opens Explorer at the output file (auto-named after
  the folder, with generated title slide and pan/zoom transitions, no audio); hand off to an
  external editor for music.
- V7: open Movie Maker → auto-generated title slide → Load an audio track → "Fit photos
  into audio" (locks slide duration) → choose transition/overlap/dimensions → preview →
  Create Movie or upload straight to YouTube.

**W9. Migrate folders to a new external drive (V8).**
1. Switch the folder pane to tree view to see drives.
2. Select the folder (e.g. P:\Pictures\2006\200612), right-click > Move Folder...
3. In Browse For Folder pick the destination (live caption "Move Folder to
   S:\Pictures\2006\"); OK.
4. Cross-drive: progress popup ("Moving 75 of 318 (10.5MB/s)"), copy-then-delete; the
   folder visibly leaves P: and appears under S:; counts update live; all
   albums/people/places/tags survive.
5. Parent folders that directly contain photos move with all their subfolders. A
   photo-less parent can't be moved — workaround: drop one photo into it via Explorer
   (Picasa picks it up automatically), then move it.
6. Same-drive moves complete near-instantly (location change only).

---

## 5. Rebuild implications

What a faithful rebuild must get right, consolidated from per-video spec notes.

**Data model**
- Two-layer model: folders mirror disk (source of truth); albums are many-to-many photo
  references. Delete semantics branch on context: album = remove reference (with
  confirmation naming "the current album"); folder = delete file, cascading from all albums.
- The edit system is a persisted, ordered, per-photo operation log with human-readable
  operation names (used verbatim as Undo/Redo labels), plus retained original pixels.
  Not session undo.
- Backup Sets need per-set bookkeeping of already-backed-up files (incremental), not
  one-shot export.
- Only folders directly containing media are "managed"; decide deliberately whether to keep
  the empty-parent limitation (V8 treats it as a wart with a workaround). Subfolder-carrying
  moves must be preserved either way.
- Move Folder must rewrite the folder's location in the metadata DB so albums/people/
  places/tags survive — that is the entire point of the feature. Same-volume = rename
  (instant); cross-volume = copy-verify-delete with per-file progress + MB/s readout.
- Denormalized live counts everywhere (collection headers, drive nodes, folder rows,
  status bar) recomputed synchronously on every mutation.
- Folder dates (from photo/folder metadata, not names) drive year grouping in flat view;
  the Albums section is year-grouped too (V3, V6), not just Folders.

**Library UI**
- Single continuous virtual scroll across all groups, with the in-view group's header
  pinned at the top; per-group headers carry their own Play, Share, Sync-to-Web, and
  description controls.
- One folder-pane component with two render modes (flat / tree) sharing selection, context
  menu, and tray behavior; flat and tree headers report different counts (photo-containing
  folders vs full hierarchy). Flat-view rows are not name-unique — duplicate names from
  distinct disk paths coexist — so identity must come from the path, not the display name.
- The selection tray with Hold/Clear/Add-to-album is the universal input to all output
  actions; it needs distinct enabled/disabled states keyed to selection type.
- Dual-mode status bar with exact formats: collection = "N pictures  date-range  size on
  disk" (+ tag histogram); photo = "filename  M/D/YYYY h:mm:ss AM  WxH pixels  NKB".
- The custom scrollbar (recentering thumb + jump-to-folder-top / jump-to-library-bottom
  buttons) is a signature behavior the tutorials explicitly flag; reproduce it, or at
  minimum the folder-jump buttons.
- Thumbnail corner badges (star, sync, geotag) must render in every grid context.
- One combined sort dropdown mixing folder sort, people sort, shortcuts, and view options.
- Folder icon = mini photo thumbnail iff the folder directly contains media; this signals
  what operations are available.

**Editing**
- Edit mode replaces the library in-window ("Back to Library"), keeps a sibling filmstrip,
  shows "(n of m)" and the breadcrumb status line.
- Crop is a stateful sub-mode swapping the whole left panel; Manual mode generates three
  auto-suggested crops; Preview-then-revert (~2s) is distinct from Apply; Rotate/Reset/
  Cancel are separate actions. Preset list mixes print sizes and screen ratios with
  descriptive suffixes plus a computed "Current ratio" entry.
- Tool state reflects history: Crop → "Recrop", one-shot fixes disable after applying.
- Fill Light appears in both Basic Fixes (quick slider) and Tuning — same parameter, two
  surfaces. Exact tool sets and Effects order are documented in §1.2.
- Effects tiles render live previews from the current photo; one-click effects carry a "1"
  badge, parametric ones open Apply/Cancel sub-panels (the sources conflict on which
  effects are one-click vs parametric — see §1.2).
- Live histogram + EXIF panel in the edit view.

**Import**
- Import is a closable tab, not a dialog; opens on device attach.
- Acquisition (async, counter, placeholder thumbnails) is a separate phase from import;
  import buttons disabled until acquisition yields photos.
- Selection (blue border, feeds "Import Selected (N)") and star state (yellow badge, feeds
  the Starred Images Only upload filter) are independent per-photo states; exclude is a
  third.
- The bottom bar groups destination / card policy / upload as one labeled sentence; the
  Options menu deliberately mixes privacy, resize, and starred-filter as one flat checkable
  menu.
- Folder title is the single name for both the on-disk folder and the web album.
- Defaults to honor: Exclude Duplicates on, Leave card alone, My Pictures, date-named
  folder, upload off, Public + 1600px when upload is enabled.

**Other modes**
- Backup is a mode of the main window reusing the tree/grid (checkboxes injected in place),
  with numbered wizard cards and a Burn/Eject/Cancel/Help action column; the disc estimator
  is dual-unit (CDs *and* DVDs) and live.
- Picasa 2's Create Movie is deliberately minimal (delay + 3 fixed sizes, two-step
  confirm, hand-off to the OS file manager afterward); Picasa 3's Movie Maker adds audio
  fitting, transitions, captions, and YouTube upload as a full-window mode. Note the
  original "Widescreen (960x720)" label is mislabeled (it's 4:3) — decide whether to
  replicate or correct.
- First-run needs: a fully-chromed empty state, the two-scope scan dialog (single Continue),
  index-only scanning, and a dismissible scan toast while the library stays browsable.
- Photo Viewer is a separate, optionally-registered component with per-extension
  granularity.

**General design language**
- Buttons carry status glyphs (green check = commit, red X = cancel) and live counts in
  labels ("Import Selected (3)", "Save Changes (100+)").
- Background work (scan, face detection, sync, moves) reports progress inline (sidebar
  rows, toasts, compact popups) — never blocking modals.
- Exact strings worth preserving where feasible: tooltips "Add selected items to an Album",
  "Set view to show flat folder structure", "Set view to show folder tree structure",
  "click and drag over photos to magnify them"; the first-run promise "Scanning for
  pictures never moves or copies files to new locations".
- Keyboard shortcuts observed: Ctrl+A select all in folder, Ctrl+D clear selection,
  Ctrl+I invert, Ctrl+Enter locate on disk.
- Stay lightweight: the original ran in 256MB RAM with a ~10MB installer; performance
  (instant zoom, browsable-while-scanning, fast moves) is part of the product's identity.

---

## 6. Per-video appendix

### V1 — "Picasa 3.5 Instructional Video - Part 1: Organizing" (drf6OTywF6E)
Tour of the Library screen, left to right: sidebar collections (Albums, People, Projects,
Folders), how Picasa mirrors on-disk folders, album creation and folder-vs-album deletion
semantics, the selection tray, flat vs tree folder views, folder sorting, the thumbnail
zoom slider, and the unconventional recentering scrollbar with folder-jump buttons.
Editing and People/Projects deferred to later parts. Best single source for library layout
and the Albums/Folders distinction.

### V2 — "Picasa 3.5 - Part 2: Basic Fixes and More" (PuNjY6Zq6Cw)
Starring and the auto-maintained Starred Photos album; album creation, drag-and-drop
population, and removal semantics; the edit view with Basic Fixes / Tuning / Effects tabs;
a detailed crop walkthrough (presets, manual crop, ~2s Preview, Apply); and the persistent,
per-photo, operation-labeled undo/redo stack that survives restarts. Best source for the
editing model.

### V3 — "Importing Photos in Picasa 3.5" (4L7IV_3xKKM)
Official 2-minute Google tutorial of the redesigned import flow: auto-opening Import tab,
async acquisition with counter and placeholder thumbnails, star/select/exclude per photo,
destination + folder title (date default), After Copying card policy, and optional
auto-upload to Picasa Web Albums with privacy / size / starred-only options. Best source
for import defaults and the import screen layout.

### V4 — "Backup photos with Picasa" (sOOkONjzcss)
95-second Geeks on Tour tutorial (Picasa 2, May 2008): Tools > Backup Pictures turns the
main window into a backup mode with checkboxes on every folder, named incremental Backup
Sets, a live dual-unit disc estimator ("6 CDs or 1 DVD"), and multi-disc-spanning burns.
Best source for the backup model.

### V5 — "Google Picasa 3 - Video 2 - Running Picasa 3 for the First Time" (c1_Tbs5e6Ag)
24-second captioned capture of first launch on Windows XP: the empty chromed window, the
two-scope first-run scan dialog (with the "never moves or copies files" promise and the
Folder Manager reference), and the library populating with the taskbar clock advancing
only one minute (22:53 → 22:54; the video may be edited), including the background-scan
toast and year-grouped sidebar. Best source for first-run behavior.

### V6 — "Simple Photo Slideshow with Picasa" (ArB400dG0sU)
83-second HelpMeRick.com tutorial (Picasa 2): select six photos, Create > Movie, choose
delay and one of three fixed sizes, render, and Picasa opens Explorer at the output — a
folder-named clip with an auto title slide, pan/zoom transitions, and no audio (95.5 MB for
14 seconds at 320x240). Best source for the era's minimal movie feature and
selection-reactive tray/status behavior.

### V7 — "Google Picasa" (De1U5mAqCTM)
4-minute full-lifecycle overview (Picasa 3 on XP): download/install (~9.6 MB), both
first-run dialogs (scan scope, Photo Viewer file types), library UI, the three edit tabs
with named-undo, Web Albums sign-in, Sync to Web (1600px/Public defaults, immediate edit
mirroring, green badges), the web-side album experience, and Movie Maker with audio fitting
and YouTube upload. Broadest single source; best for sharing and Movie Maker.

### V8 — "Picasa 3.5: Managing Thousands of Pictures on External Drives" (YfqThYqDs-8)
Geeks on Tour (October 2009): migrating photos from a nearly full 250GB drive to a new
500GB drive using right-click Move Folder... so albums/people/places/tags survive
(Explorer moves lose them). Shows flat vs tree views over drives, cross-drive
copy-then-delete with a progress popup, subfolder-carrying moves, the empty-parent-folder
limitation and its workaround, instant same-drive moves, live count recalculation, and
automatic pickup of files added outside Picasa. Best source for the folder/metadata
relationship and watched-folder behavior.
