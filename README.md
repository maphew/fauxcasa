# Fauxcasa

Fauxcasa shows you your Picasa photo library again: the same folders,
albums, people, stars and captions, on your own computer. It is a free
program that reads the photo folders and Picasa files you already have.
It never changes your photos or Picasa's files.

![The Fauxcasa window: folders, albums and people down the left, photos grouped by folder on the right, with stars and place markers on the thumbnails](docs/releases/gallery/gallery.jpg)

The [release notes](docs/releases/v0.1.0.md#what-it-looks-like) have more
pictures: the photo viewer, face boxes, an album, a person, search, the
Info panel and the slideshow.

Fauxcasa is early software. Version 0.1 is for looking, not changing: you
can browse, search and view your photos, but not edit, tag or export them
yet. It is not a replacement for Picasa in daily use, but it is safe to
try on your own photos and easy to remove again.

## Try it

1. Download the current version, `0.1.0-rc2`, from its
   [release page](https://github.com/maphew/fauxcasa/releases/tag/v0.1.0-rc2).
   Windows 10 or 11: the file ending in `windows-x64.zip`. Linux: the file
   ending in `linux-x64.tar.gz`. (The "x64" means a 64-bit computer,
   which is almost every PC made in the last decade.)
2. **Windows:** right-click the zip, choose **Extract All**, open the new
   folder, and double-click `fauxcasa.exe`. Keep the folder together; the
   program needs the other files next to it.
   **Linux:** extract the archive and run the `fauxcasa` program inside
   the extracted folder. The folder can live anywhere you like.
3. Choose the folder that holds your photos. If Picasa is installed on the
   same Windows PC, Fauxcasa also offers to open the folders Picasa
   watched.

The window opens right away. Thumbnails fill in while Fauxcasa looks
through your folders. A big library takes a while the first time and is
fast after that.

The [release notes](docs/releases/v0.1.0.md) explain what works, what does
not work yet, the keyboard shortcuts, and exactly which files Fauxcasa
keeps on your computer.

### Windows will warn you the first time

Windows shows a blue "Windows protected your PC" screen for programs that
are not signed with a paid publisher certificate, which this one is not
yet. That is normal for a small free program. Click **More info**, then
**Run anyway**. Do this only for a copy downloaded from the releases page
above.

If you prefer, right-click the zip before extracting, open
**Properties**, tick **Unblock**, and Windows will not ask.

## Your photos stay untouched

- Fauxcasa only reads your photos and Picasa's files. Browsing, searching
  and viewing never change, move or delete anything in your photo
  folders.
- Because it only reads, a crash cannot damage your photos. At worst
  Fauxcasa has to rebuild its own thumbnails.
- It has no online features. It never connects to the internet: no
  accounts, no uploads, no update checks, no usage reports. A firewall
  that asks per program will never ask about it.
- Everything it makes for itself (a list of your photos and their
  thumbnails) lives in one folder of its own, away from your photos. On
  Windows, paste `%LOCALAPPDATA%\Fauxcasa` into the File Explorer address
  bar to see it; on Linux it is `~/.cache/fauxcasa`. You can delete that
  folder at any time.
- One small exception: if you choose **Use Picasa's watched folders** on
  the first start, Fauxcasa leaves a tiny bookkeeping file called
  `.fauxcasa-root` in each of those folders so it can recognise them
  later. It never touches the photos themselves.
- To remove Fauxcasa completely, delete the extracted program folder and
  the Fauxcasa folder above.

One caution. Any photo viewer can be crashed or misled by a deliberately
malformed image file. On Windows, Fauxcasa opens the common photo formats
in a locked-down helper process to limit what such a file could do. That
protection does not yet cover camera RAW files, Photoshop files or videos,
and does not exist on Linux yet. Open your own photo collections, not
folders of files from people you do not know.

Later versions will let you change things. The plan is to save your
changes next to your photos, in files Picasa itself understands, never in
a private database that locks you in. The
[product spec](docs/product-spec.md) has the details.

## Help and bug reports

Report problems on the project's
[issue page](https://github.com/maphew/fauxcasa/issues). A free GitHub
account is needed to post. Please do not attach photos, Picasa files,
names or real folder paths; the release notes list what a useful report
contains.

## For developers

Fauxcasa is written in Python with Qt (PySide6). The app lives in
`apps/desktop-python/`, and its [README](apps/desktop-python/README.md)
describes the architecture and the measured performance numbers.
`scripts/` holds tooling and research utilities. `docs/` holds the
[product spec](docs/product-spec.md), design notes, research and release
notes.

To run from source, install [uv](https://docs.astral.sh/uv/), then:

```
uv run apps/desktop-python/main.py ~/Pictures     # run against a folder
uv run apps/desktop-python/test_tracer.py          # the app's test suite
uv run scripts/preflight.py                        # every check CI runs
```

If you have [just](https://just.systems/) installed, `just py ~/Pictures`
and `just py-test` are shortcuts for the first two; `just --list` shows
the rest. Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull
request.

## License

Fauxcasa is free software and will stay that way. The code is licensed
under the [GNU AGPL v3 or later](LICENSE) and the documentation under
[CC BY-SA 4.0](docs/LICENSE.md). You may use, study, share and improve
it. If you distribute a changed version, or let others use one over a
network, you must share your changes under the same terms. The Fauxcasa
name and logo are reserved as trademarks. Archived research material under
`docs/research/` keeps its original sources' terms; see
[docs/research/NOTICE.md](docs/research/NOTICE.md).
