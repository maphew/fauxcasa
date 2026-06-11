# PicasaStarter: mechanisms and lessons

PicasaStarter was a community C# wrapper enabling multiple databases,
multi-machine sharing, and portable libraries — everything stock Picasa
couldn't do. Its source encodes hard-won knowledge of Picasa's internals and
real-world failure modes. Surveyed 2026-06-11.

Sources: code mirror https://github.com/maphew/picasastarter (maphew's own
mirror of the CodePlex original — first-party access); docs at
https://sites.google.com/site/picasastartersite (FAQ, Users Guide). Key
files: `2-BusinessLogic/PicasaRunner.cs` (+ `.cs.bak` legacy),
`3-HelperClasses/IOHelper.cs`, `Settings.cs`, `2-BusinessLogic/PicasaButton.cs`.

## How Picasa's database gets relocated

**Picasa ≥ 3.9 — a registry value, swapped around the process lifetime:**

- `HKCU\Software\Google\Picasa\Picasa2\Preferences\AppLocalDataPath`
  (REG_SZ, **must end with `\`**) → Picasa puts its data under
  `<path>\Google\Picasa2\` (db3) and `<path>\Google\Picasa2Albums\` (albums).
- There is **no command-line flag** for this; PicasaStarter sets the value,
  launches `picasa3.exe`, `WaitForExit()`, then deletes the value to restore
  the default `%LocalAppData%` location.
- Crash safety: a sentinel value (`AppLocalDataPathSaved`) is written before
  launch; if found on next start, the previous run died mid-swap and the user
  is offered recovery. Picasa's own experimental "Move database" command uses
  `AppLocalDataPathCopy`.
- Picasa ≤ 3.8 had no override at all: PicasaStarter v1.x built a **fake
  Windows user profile** tree (`<base>\Local Settings\Application Data\Google\...`)
  and redirected via symlinks — it even had to create a `Desktop` folder or
  Picasa's Export errored. Picasa derived paths from the profile wholesale.
- Install dir auto-detected from `HKCU\...\Picasa2\Runtime\appPath`; version
  sniffed via `FileVersionInfo` on picasa3.exe (3.9 behavior gate).

## Multi-machine sharing (the NAS scenario)

- Central `PicasaStarterSettings.xml` on the share lists the databases;
  per-machine variance (exe paths) keyed by `Environment.MachineName`.
- **No real concurrency** — an advisory marker file `PicasaRunning.txt`
  ("started by <user> on <machine> at <time>") with a warning the user can
  override: two Picasas on one DB ⇒ corruption. Sequential access only.
- **Drive-letter problem**: Picasa stores absolute paths in its DB, so every
  machine must see photos at the identical path. PicasaStarter maps a
  configurable **virtual drive letter** before launch (`DefineDosDevice` =
  programmatic `subst` for local dirs; `WNetGetConnection`/`WNetAddConnection2`
  for UNC), and unmaps on exit. Optional relative re-rooting handles external
  disks that mount as different letters on different PCs.
- Automation: CLI `/autorun <db>`, `/backup <db>`; `Pre_RunPicasa.bat` /
  `Post_RunPicasa.bat` hooks with env vars.

## First-run suppression (directly useful for our Wine oracle)

Writing these into a fresh DB root makes Picasa skip the "scan the whole
computer" first-run wizard:

- `Picasa2Albums\watchedfolders.txt` — empty file (watched-folder list)
- `Picasa2Albums\frexcludefolders.txt` — empty file (face-rec exclude list)
- `Picasa2\db3\thumbs_index.db` — seed file (PicasaStarter embeds one as a
  resource)

Then watched folders can be configured explicitly — exactly what a controlled
differential-testing oracle wants.

## Custom buttons (developer-mindset evidence)

PicasaStarter fully decoded Picasa's button extension format: a `.pbz` is a
zip dropped into `%LocalAppData%\Google\Picasa2\buttons`, containing a `.pbf`
XML manifest + `.psd` icon. The manifest's `action verb='trayexec'` launches
an arbitrary exe (path literal or resolved from a registry key), optionally
per-selected-photo (`foreach`) or after auto-exporting the selection
(`export`). Two tells about how the Picasa team thought:

1. **The selection tray is the extension point** — third-party actions
   consume the tray, like every built-in output action. One central object.
2. **Export-then-act** — plugins get safe flattened copies (post-edit JPEGs)
   rather than touching originals or the database: the non-destructive
   invariant extends to the plugin API.

## Pain points a rebuild must design away

1. One DB per Windows user, hardwired to `%LocalAppData%` (relocation was
   undocumented registry surgery) → libraries must be first-class, N per user,
   user-visible locations.
2. Absolute drive-letter paths in the DB → store relative / root-token paths;
   a library must survive its volume changing mount points.
3. Zero concurrency control (corruption on double-open) → real locking with
   holder identity at minimum; ideally safe multi-reader.
4. State leaks outside the DB root even when redirected (collages/movies to
   My Pictures, buttons dir, Desktop dependency) → ALL per-library state
   inside the library root.
5. Fragile relocation (registry swap strands DB pointer on crash) → no global
   mutable pointer; open-by-path like a document.
6. Moving photo folders outside the app silently orphans metadata → tolerate
   external moves (content hashing, reconciliation), not just app-internal Move.
7. First-run "scan everything" hostility → scanning scope is opt-in,
   configurable before first index.
