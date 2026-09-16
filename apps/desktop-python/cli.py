"""Argument parsing and standalone CLI subcommands for main.py (fauxcasa-4tu).

build_parser() holds the ArgumentParser construction and all --flag
definitions verbatim; it takes the app's identity (prog/description/
version text) as parameters rather than importing main.py for them, so
this module never imports main (main.py imports this module, so the
reverse would be circular at load time).

_cmd_promote, _cmd_add_root, _cmd_import_picasa_watched, and their small
helpers are the --promote/--add-root/--import-picasa-watched standalone
management actions (design §10): each validates its arguments, performs
one on-disk change via library.py, prints a one-line summary, and returns
an exit code. _cmd_promote and _cmd_add_root call main._resolve_library
via a LAZY import inside the function body (not a module-level import) —
same reasoning as librarystate._remembered_library's lazy import of
main._is_filesystem_root: main.py imports this module near its own top,
before _resolve_library is defined further down, so a module-level
`from main import _resolve_library` would be a circular import that fails
at load time. _resolve_library itself is never monkeypatched by tests, so
resolving it at call time (once main.py has fully loaded) is safe.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import applog
import library

log = applog.log


def build_parser(*, prog: str, description: str,
                 version_text: str) -> argparse.ArgumentParser:
    """The tracer CLI's full flag surface, verbatim from main.py's former
    in-line construction (fauxcasa-4tu stage 2). `--help` output must stay
    byte-identical, so nothing here should change wording, defaults, or
    argument order without also updating this docstring's caller."""
    ap = argparse.ArgumentParser(
        prog=prog,
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=version_text,
                    help="print the version and exit")
    ap.add_argument("library", nargs="?", default=None,
                    help="library root to browse (read-only). Default: the "
                         "bundled synthetic library in a source checkout; in "
                         "a frozen build, the library you last opened, or one "
                         "you pick on first run")
    ap.add_argument("--thumbs", type=Path, default=None,
                    help="adopt an existing .fcache instead of building "
                         "one (e.g. cache/benchmark-thumbs.fcache)")
    ap.add_argument("--cache-root", type=Path, default=None,
                    help="where the app keeps its own disposable caches "
                         "(default: <repo>/cache/fauxcasa-cache in a checkout, "
                         "a per-user cache dir when run as a frozen bundle)")
    ap.add_argument("--rebuild", action="store_true",
                    help="ignore any existing tracer cache and rebuild")
    ap.add_argument("--promote", action="store_true",
                    help="promote the legacy library given as the "
                         "positional argument to a multi-root-capable "
                         "library-home in place (design §10: mint "
                         "identity, rename cache dir, header-upgrade "
                         "catalog.json — no re-walk/re-hash), print the "
                         "resulting home, and exit")
    ap.add_argument("--add-root", type=Path, default=None, metavar="PATH",
                    help="add PATH as a new watched root to the "
                         "already-promoted library-home given as the "
                         "positional argument, then exit")
    ap.add_argument("--import-picasa-watched", type=str, default=None,
                    metavar="LISTFILE",
                    help="create a fresh library-home at the positional "
                         "argument from a Picasa watched-folders list: "
                         "LISTFILE is a text file (one path per line, "
                         "'#' comments), or the literal 'registry' to "
                         "read Picasa's own watched-folders list from the "
                         "Windows registry, then exit")
    ap.add_argument("--contacts", type=Path, default=None,
                    help="Picasa contacts.xml for face names (read-only; "
                         "default: the machine-local copy under "
                         "%%LocalAppData%%\\Google\\Picasa2 when present)")
    ap.add_argument("--pal-dir", type=Path, default=None,
                    help="Picasa2Albums directory of .pal album files "
                         "(read-only; merged per spec §4 — ini wins "
                         "membership, .pal fills gaps; default: the "
                         "machine-local Picasa2Albums under "
                         "%%LocalAppData%%\\Google\\Picasa2 when present)")
    ap.add_argument("--db3", type=Path, default=None,
                    help="Picasa db3 directory for the §4 rescue import "
                         "(read-only; person-album names gap-fill contacts "
                         "the ini/contacts.xml never named; default: the "
                         "machine-local db3 under "
                         "%%LocalAppData%%\\Google\\Picasa2 when present — "
                         "use this flag for PicasaStarter relocations)")
    ap.add_argument("--min-image-size", type=_parse_image_size_arg,
                    metavar="WIDTHxHEIGHT",
                    help="ignore images smaller than WIDTHxHEIGHT during "
                         "catalog scan (for icons and thumbnails)")
    ap.add_argument("--max-image-size", type=_parse_image_size_arg,
                    metavar="WIDTHxHEIGHT",
                    help="ignore images larger than WIDTHxHEIGHT during "
                         "catalog scan (for huge source or GIS images)")
    ap.add_argument("--zoom", type=int, default=160)
    ap.add_argument("--screenshot", type=Path, default=None,
                    help="save a PNG once the viewport is fully decoded, "
                         "then quit (pairs with QT_QPA_PLATFORM=offscreen)")
    ap.add_argument("--scroll-to", type=float, default=None, metavar="FRAC",
                    help="after ready, jump to this scroll fraction (0-1)")
    ap.add_argument("--open", type=int, default=None, metavar="N",
                    help="after ready, open the viewer on the Nth photo "
                         "of the current view (screenshot testing)")
    ap.add_argument("--view", type=str, default=None, metavar="SPEC",
                    help="after ready, select a sidebar view exactly as a "
                         "click would: 'all', 'starred', 'recent', "
                         "'unnamed', 'album:<name-or-uid>', "
                         "'person:<name>', or 'folder:<rel-path>' "
                         "(screenshot testing)")
    ap.add_argument("--search", type=str, default=None, metavar="QUERY",
                    help="after ready (and --view, if given), type QUERY "
                         "into the search box (screenshot testing; "
                         "distinct from --search-probe, which is a "
                         "latency probe that quits)")
    ap.add_argument("--select", type=int, default=None, metavar="N",
                    help="after ready/--view/--search, make the Nth photo "
                         "of the current view the grid's current/selected "
                         "item (clamped like --open; screenshot testing)")
    ap.add_argument("--info", action="store_true",
                    help="after ready, open the metadata inspector panel "
                         "(screenshot testing)")
    ap.add_argument("--play", action="store_true",
                    help="after ready, start the slideshow over the "
                         "current view; --screenshot then captures the "
                         "slideshow surface instead of the main window "
                         "(screenshot testing)")
    ap.add_argument("--faces", action="store_true",
                    help="after --open N: show the viewer's face boxes "
                         "(the F key) before the screenshot; exits 1 if the "
                         "viewer is not on a face-tagged photo (screenshot "
                         "testing)")
    ap.add_argument("--window-size", type=_parse_image_size_arg,
                    metavar="WIDTHxHEIGHT",
                    help="resize the window to exactly WIDTHxHEIGHT "
                         "before showing it, for a screenshot size that "
                         "doesn't depend on the machine's screen")
    ap.add_argument("--quit-after-ready", action="store_true",
                    help="exit right after the READY line (perf probe)")
    ap.add_argument("--search-probe", type=str, default=None, metavar="TERMS",
                    help="after READY, run each comma-separated query "
                         "through the search box, print a machine-readable "
                         '{"event":"search",...} line per query, then quit '
                         "(§7 latency probe; offscreen-safe; a query may "
                         "contain spaces and -negations)")
    ap.add_argument("--finish-build", action="store_true",
                    help="scripted runs: wait for an in-flight cache "
                         "build before quitting (warm-run scripting)")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="scripted runs (--screenshot/--quit-after-ready) "
                         "abort with exit 1 after this many seconds")
    ap.add_argument("--bundle-self-check", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--require-sandbox", action="store_true",
                    help="fail loud (nonzero exit) instead of degrading "
                         "to in-process decoding if the Windows decode "
                         "sandbox cannot start (fauxcasa-ez2.9); maps to "
                         "FAUXCASA_DECODE_SANDBOX=require")
    return ap


def _parse_image_size_arg(value: str) -> tuple[int, int]:
    raw = value.strip().lower()
    sep = "x" if "x" in raw else ","
    parts = raw.split(sep, 1)
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT")
    try:
        width, height = (int(parts[0]), int(parts[1]))
    except ValueError as e:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT") from e
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("WIDTH and HEIGHT must be positive")
    return width, height


# ---------------------------------------------------------------------------
# Multi-root management CLI actions (fauxcasa-ed5.7.4, bead .d, design §10)
# ---------------------------------------------------------------------------
#
# --promote / --add-root / --import-picasa-watched are management
# operations, not the normal open path: each validates its arguments,
# performs the on-disk change via library.py, prints a one-line summary,
# and returns an exit code WITHOUT proceeding to the normal warm/cold-walk
# open below — opening an explicit multi-root library through the grid/tree
# is bead .g's scope (the "tree per root" UI); today's open flow only knows
# how to browse a single Path. Re-running the app afterwards (implicit
# legacy open for an un-promoted root, or a future explicit-open path once
# .g lands) picks up the change.

def _cmd_promote(library_arg: str | None, cache_root: Path) -> int:
    """--promote: promote the legacy library named by the positional
    `library` argument in place (design §10). Reuses _resolve_library for
    the same existence/filesystem-root validation every other invocation
    gets, so a bad path fails exactly the same way it would on a normal
    open. An explicit `library` is REQUIRED (same rule
    _cmd_import_picasa_watched enforces): without one, _resolve_library
    would silently fall through to _default_library() and promote the
    built-in sample library in place."""
    if not library_arg:
        log.error("--promote requires a library path as the positional "
                  "argument")
        return 2
    from main import _resolve_library  # lazy: see module docstring

    root = _resolve_library(library_arg, cache_root)
    if root is None:
        return 2
    try:
        cfg = library.promote_library(root, cache_root=cache_root)
    except Exception as e:
        log.error("promotion failed (rolled back, %s is still a plain "
                  "legacy library): %s", root, e)
        return 2
    print(f"promoted: library home at {cfg.home}")
    return 0


def _cmd_add_root(library_arg: str | None, cache_root: Path,
                  new_root: Path) -> int:
    """--add-root PATH: add PATH as a new watched root to the
    already-promoted library-home named by the positional `library`
    argument. Mint + save + fail-soft marker only — no indexing runs here;
    the new root's empty/mismatched fcache rides the existing bind()
    mismatch -> reindex path the next time this library is actually
    opened (design §10: "mint id, append to roots, save library.json, mint
    a new empty thumbs-<root_id>.fcache -> bind mismatch -> incremental
    reindex for the new root only")."""
    from main import _resolve_library  # lazy: see module docstring

    home = _resolve_library(library_arg, cache_root)
    if home is None:
        return 2
    cfg = library.resolve_open_path(home)
    if cfg.is_legacy:
        log.error("%s is not a library-home (no .fauxcasa/library.json) — "
                  "run --promote first", home)
        return 2
    try:
        added = library.add_root(cfg, new_root)
    except ValueError as e:
        log.error("cannot add root: %s", e)
        return 2
    library.write_root_marker(Path(new_root).resolve(), added.id)  # fail-soft
    library.save_library(cfg)
    print(f"added root {added.id} ({added.label}) to {home}")
    return 0


def _read_watched_list_file(path: Path) -> list[Path]:
    """One folder path per line; blank lines and '#'-prefixed comments are
    skipped. Raises OSError if `path` can't be read — the caller turns
    that into a friendly CLI error."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Path(s) for s in (line.strip() for line in lines)
            if s and not s.startswith("#")]


def _cmd_import_picasa_watched(library_arg: str | None,
                               listfile: str) -> int:
    """--import-picasa-watched LISTFILE: create a FRESH library-home at the
    positional `library` argument from a Picasa watched-folders list.
    `listfile` is a text file (one path per line, '#' comments), or the
    literal 'registry' to read Picasa's own HKCU watched-folders list
    (Windows only; library.picasa_watched_from_registry fails soft with a
    clear RuntimeError when absent/unsupported, caught here)."""
    if not library_arg:
        log.error("--import-picasa-watched requires a library-home path "
                  "as the positional argument")
        return 2
    home = Path(library_arg).expanduser().resolve()

    if listfile == "registry":
        try:
            folders = library.picasa_watched_from_registry()
        except RuntimeError as e:
            log.error(str(e))
            return 2
    else:
        try:
            folders = _read_watched_list_file(Path(listfile))
        except OSError as e:
            log.error("cannot read %s: %s", listfile, e)
            return 2

    skipped: list[str] = []
    cfg = library.import_picasa_watched(folders, home, name=home.name,
                                        skipped=skipped)
    for msg in skipped:
        log.warning("picasa import: skipped %s", msg)
    if not cfg.roots:
        log.error("no usable watched folders found — nothing imported")
        return 2
    library.save_library(cfg)
    for r in cfg.roots:
        library.write_root_marker(r.path.resolve(), r.id)  # fail-soft
    print(f"imported {len(cfg.roots)} root(s) into {cfg.home}")
    return 0
