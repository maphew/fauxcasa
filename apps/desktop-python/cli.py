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
import json
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QTreeWidgetItemIterator

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


# ---------------------------------------------------------------------------
# Scripted run (fauxcasa-4tu stage 2, commit 3): the check_ready() state
# machine used by --screenshot/--quit-after-ready/--search-probe/... CI
# runs, and by the ordinary interactive run's own READY-instrumentation gate
# (every run polls check_ready() once per 50ms tick until READY fires).
# ---------------------------------------------------------------------------

def run_search_probe(win, spec: str) -> list[dict]:
    """§7 search-latency probe (fauxcasa-ed5.4): drive each comma-separated
    query through the live search box exactly as its final keystroke would
    (setText -> textChanged -> _search_changed, synchronously) and print one
    machine-readable {"event": "search", "query", "ms", "hits"} line per
    query for the CI/dev harness. `ms` is _search_changed end to end (see
    its docstring comment for what that covers). The box is cleared between
    queries — a repeated identical query would otherwise not re-fire
    textChanged — and left empty afterwards. Blank segments are skipped, so
    a trailing comma is harmless."""
    events: list[dict] = []
    for q in (t.strip() for t in spec.split(",")):
        if not q:
            continue
        win.search.setText("")
        win.search.setText(q)
        ev = {"event": "search", "query": q,
              "ms": round(win.last_search_ms, 3),
              "hits": win.last_search_hits}
        print(json.dumps(ev), flush=True)
        events.append(ev)
    win.search.setText("")
    return events


def select_sidebar_view(win, spec: str) -> bool:
    """Select a sidebar view exactly as a click on its tree item would
    (scripted-run screenshot flag --view). SPEC is 'all', 'starred',
    'recent', 'unnamed', 'album:<name-or-uid>', 'person:<name>', or
    'folder:<rel-path>' — the part before ':' (or the whole string, for
    the colon-less kinds) is matched against a tree item's (kind, key)
    UserRole payload set up in _rebuild_sidebar/_build_sidebar. An album
    matches by UID OR by its resolved display name (win.catalog.albums
    [uid].name), since a human would rather type "Best Of" than a hex
    token; person/folder match the key exactly (case-sensitive). Drives
    win._sidebar_clicked(item, 0) so the tree highlights too, exactly
    like a real click — not just win._apply_view, which leaves the tree
    selection stale. Returns False for an unknown kind or a spec with no
    matching item; the caller (check_ready) decides how to fail a
    scripted run on that."""
    kind, _, key = spec.partition(":")
    if kind not in ("all", "starred", "recent", "unnamed",
                    "album", "person", "folder"):
        return False
    matches = []
    it = QTreeWidgetItemIterator(win.tree)
    while it.value():
        item = it.value()
        it += 1
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None or data[0] != kind:
            continue
        item_key = data[1]
        if kind == "album":
            album = win.catalog.albums.get(item_key)
            if item_key != key and not (album is not None
                                        and album.name == key):
                continue
        elif item_key != key:
            continue
        matches.append(item)
    if len(matches) != 1:
        # Picasa permits two albums with one display name, and a name
        # could equal another album's uid: refuse to guess which the
        # caller meant rather than screenshot the wrong one.
        if matches:
            log.error("--view %r is ambiguous: %d sidebar items match",
                      spec, len(matches))
        return False
    win._sidebar_clicked(matches[0], 0)
    return True


class ScriptedRun:
    """The check_ready() state machine main() used to build as a closure
    over its own locals, now an object that receives explicitly what that
    closure used: the window, the QApplication, args, the READY-
    instrumentation values only main() knows (T0, prep_ms, cat_path, warm,
    the version/git_sha identity, OPEN_WAIT_MS, read_rss_mb), and the
    decode-sandbox service. start()/stop() own the three QTimers'
    lifecycle (poll, decode_sandbox_poll, and — for a scripted run only —
    hard_stop), parented to `win` for the same reason the original
    closure's comments give (fauxcasa-q6l.15, fauxcasa-9pr): an orphaned
    timer with no parent can outlive main()'s return and fire into an
    already-deleted window."""

    def __init__(self, win, app, args, *, decode_svc, t0: float,
                prep_ms: float, cat_path: Path, warm: bool, version: str,
                git_sha: str, open_wait_ms: int, read_rss_mb):
        self.win = win
        self.app = app
        self.args = args
        self.decode_svc = decode_svc
        self.t0 = t0
        self.prep_ms = prep_ms
        self.cat_path = cat_path
        self.warm = warm
        self.version = version
        self.git_sha = git_sha
        self.open_wait_ms = open_wait_ms
        self.read_rss_mb = read_rss_mb
        # READY instrumentation (§7 cold start): poll until every visible
        # tile is decoded, then report cold start + RSS on stdout.
        self.state = {"scrolled": False, "shot": False, "opened": False,
                     "probed": False, "scan_failure_handled": False,
                     "viewed": False, "searched": False, "selected": False,
                     "info_set": False, "played": False, "faced": False}
        # A scripted probe (any of the three) implies quit — same set the
        # hard timeout below arms on; reused by check_ready's scan-failure
        # gate (fauxcasa-q6l.13, Codex cross-vendor review finding 2).
        self.scripted_run = (args.screenshot is not None
                             or args.quit_after_ready
                             or args.search_probe is not None)
        self.poll: QTimer | None = None
        self.hard_stop: QTimer | None = None
        self.decode_sandbox_poll: QTimer | None = None

    def sync_decode_sandbox_label(self) -> None:
        # P3 finding: a MID-SESSION degrade (decodefacade.DecodeService.
        # decode() flips state -> "degraded" on a per-file spawn/OSError/
        # queue.Empty failure, well after the READY-time snapshot below) must
        # still update the status-bar label -- N7 "never silent" applies for
        # the whole session, not just at startup. check_ready's scripted
        # `poll` timer is stopped once an interactive run's instrumentation
        # is done, so this uses its OWN long-lived timer instead.
        win, decode_svc = self.win, self.decode_svc
        if decode_svc.state == "degraded" and not win.decode_sandbox_label.isVisible():
            log.error("decode sandbox degraded: %s", decode_svc.reason)
            win.decode_sandbox_label.setText(
                f"Decoding is not sandboxed on this machine: "
                f"{decode_svc.reason}  ")
            win.decode_sandbox_label.setToolTip(decode_svc.reason)
            win.decode_sandbox_label.setVisible(True)

    def may_quit(self) -> bool:
        win, args, app = self.win, self.args, self.app
        if not args.finish_build:
            return True
        if win.index_busy():
            return False
        if win.build_failed:
            # --finish-build promised a cache; a failed build must not
            # masquerade as a green run.
            log.error("cache build failed under --finish-build")
            app.exit(1)
            return False
        return True

    def check_ready(self) -> None:
        win, args, app = self.win, self.args, self.app
        state, decode_svc = self.state, self.decode_svc
        if win._scan_failed:
            # Codex cross-vendor review finding 2: never let a failed
            # deferred walk report a fake READY against the still-empty
            # placeholder catalog — _on_scan_done already surfaced the
            # failure via the activity row/status bar/log. A scripted
            # probe gets an immediate nonzero exit here (mirrors the crash
            # an unhandled scan exception caused on the old synchronous
            # path, just without waiting out the full --timeout); a plain
            # interactive run simply stays open with the window it already
            # has — not killed outright, since unlike the old path the
            # window exists and is otherwise usable.
            if self.scripted_run and not state["scan_failure_handled"]:
                state["scan_failure_handled"] = True
                log.error("cold scan failed — exiting nonzero, no READY")
                app.exit(1)
            return
        # fauxcasa-q6l.13: while the deferred cold scan is still in flight
        # the grid is genuinely empty (0 items), so all_visible_decoded()
        # would trivially read True and fire READY against the EMPTY
        # placeholder catalog — never once the real one lands. Hold READY
        # until the walk lands (this does NOT block the event loop; the
        # walk runs on its own thread and the window is already painted
        # and responsive), same as the pre-existing "wait for visible
        # tiles to decode" gate this joins for the async BUILD that
        # follows.
        # The "visible tiles decoded" gate is about the GRID page. Once a
        # scripted --open has switched to the viewer, the grid is hidden and
        # never paints, so it never requests decodes; if the cold build
        # lands AFTER the viewer opened (READY can fire before "indexed"),
        # set_thumbs() makes every visible tile undecoded again and this
        # gate would hold the poll forever (a bare TIMEOUT, seen ~1 in 5
        # runs locally and on CI's native-smoke leg). Apply it only while
        # the grid page is current.
        grid_current = win.pages.currentWidget() is not win.viewer
        if win._cold_scan_pending or (
                grid_current and not win.grid.all_visible_decoded()):
            return
        if not win.ready_reported:
            win.ready_reported = True
            cold_ms = (time.perf_counter() - self.t0) * 1000.0
            rss, hwm = self.read_rss_mb()
            # §7 catalog-size row (fauxcasa-ed5.3): the persisted
            # catalog.json's on-disk bytes, normalized per photo. 0 = not
            # yet persisted (a cold build writes it when the index lands).
            # fauxcasa-ed5.5 re-baselined the budget to <=100 B/photo fully
            # indexed (spec §10 item 20) and met it honestly: catalog.json
            # is now zstd level 3 over folder-grouped compact JSON (see
            # catalog.save_catalog and docs/research/catalog-size-analysis.md)
            # — st_size already reflects that compression, so this field
            # continues to measure the file honestly, just a smaller one.
            try:
                cat_bytes = self.cat_path.stat().st_size
            except OSError:
                cat_bytes = 0
            print("READY", flush=True)
            # fauxcasa-ez2.9 Stage 1, item 6: one line right after READY,
            # every run (CI's --require-sandbox smoke asserts state ==
            # "sandboxed" on this exact key). Logged via applog too, and
            # "degraded" additionally gets a persistent status-bar note
            # (N7: never silent) -- "in-process" (non-Windows, or
            # FAUXCASA_DECODE_SANDBOX=0) is a softer log-only note.
            print(json.dumps({"event": "decode-sandbox",
                              "state": decode_svc.state,
                              "reason": decode_svc.reason}), flush=True)
            if decode_svc.state == "degraded":
                self.sync_decode_sandbox_label()
            elif decode_svc.state == "in-process":
                log.info("decode sandbox: in-process (%s)", decode_svc.reason)
            else:
                log.info("decode sandbox: sandboxed")
            # fauxcasa-q6l.13: win.catalog, not the outer `catalog` closed
            # over above — on the non-blocking cold-scan path that outer
            # binding is the EMPTY placeholder forever, while win.catalog
            # is swapped in place by reload_data once the deferred walk
            # lands (_on_scan_done). Reading win.catalog means READY
            # reports whatever is actually live at the moment first paint
            # settled — 0 on a still-scanning NAS-scale library (honest:
            # the walk isn't done yet), the real counts when the walk
            # already landed by then (small/warm-adjacent libraries).
            # "version" (+ "git_sha" only on a stamped build) rides the
            # existing keys so a perf/CI record says WHICH build produced
            # the numbers (rel-0.1 identity). Consumers read keys by name
            # (scripts/perf-canary.py), so an added key is additive.
            print(json.dumps({
                "event": "ready",
                "version": self.version,
                **({"git_sha": self.git_sha} if self.git_sha else {}),
                "cold_start_ms": round(cold_ms),
                "prep_ms": round(self.prep_ms),
                "warm": self.warm,
                "photos": len(win.catalog.photos),
                "visible_photos": win.catalog.visible_count,
                "folders": len(win.catalog.folders),
                "albums": len(win.catalog.albums),
                "catalog_bytes": cat_bytes,
                "catalog_bytes_per_photo": round(
                    cat_bytes / max(1, len(win.catalog.photos)), 1),
                "vm_rss_mb": round(rss, 1),
                "vm_hwm_mb": round(hwm, 1),
            }), flush=True)
            if args.quit_after_ready and args.screenshot is None \
                    and args.scroll_to is None and args.open is None \
                    and args.search_probe is None and args.view is None \
                    and args.search is None and args.select is None \
                    and not args.info and not args.play \
                    and not args.faces and self.may_quit():
                app.quit()
                return
        if args.search_probe is not None and not state["probed"]:
            # §7 search probe (fauxcasa-ed5.4): run once, right after READY,
            # then fall through to the normal scripted-quit path below —
            # --search-probe implies quit (see the bottom of check_ready).
            state["probed"] = True
            run_search_probe(win, args.search_probe)
        # Scripted-run screenshot flags: view -> search -> select -> info ->
        # scroll_to -> open -> faces -> play -> (loading waits) -> screenshot. Each
        # step sets its state flag and returns once so the viewport gets a
        # poll cycle to decode (same pattern scroll_to always used).
        if args.view is not None and not state["viewed"]:
            state["viewed"] = True
            if not select_sidebar_view(win, args.view):
                log.error("--view: no sidebar item matches %r", args.view)
                print(json.dumps({"event": "view", "ok": False,
                                  "spec": args.view}), flush=True)
                app.exit(1)
                return
            kind, _, key = args.view.partition(":")
            print(json.dumps({
                "event": "view", "ok": True, "kind": kind, "key": key,
                "shown": len(win.grid.display),
            }), flush=True)
            return  # let the new view's viewport decode
        if args.search is not None and not state["searched"]:
            state["searched"] = True
            win.search.setText(args.search)
            print(json.dumps({
                "event": "view", "ok": True, "kind": "search",
                "key": args.search, "shown": len(win.grid.display),
            }), flush=True)
            return
        if args.select is not None and not state["selected"]:
            state["selected"] = True
            display = win.grid.display
            if display:
                pos = max(0, min(len(display) - 1, args.select))
                win.grid._select(display[pos])
                win.grid._ensure_visible(display[pos])
            print(json.dumps({
                "event": "view", "ok": True, "kind": "select",
                "key": str(args.select), "shown": len(display),
            }), flush=True)
            return
        if args.info and not state["info_set"]:
            state["info_set"] = True
            win.info_action.setChecked(True)  # opens the inspector
            return
        if args.scroll_to is not None and not state["scrolled"]:
            state["scrolled"] = True
            win.grid.scroll_to_fraction(args.scroll_to)
            return  # wait for the new viewport to decode
        if args.open is not None and not state["opened"]:
            state["opened"] = True
            display = win.grid.display
            if display:
                pos = max(0, min(len(display) - 1, args.open))
                win._open_viewer(display[pos], display, pos)
            return
        if args.faces and not state["faced"]:
            # Face boxes come from the catalog, not the decoded original,
            # so this need not wait for the viewer's load; toggle_faces is
            # a no-op unless the viewer is current on a face-tagged photo.
            state["faced"] = True
            win.viewer.toggle_faces()
            if not win.viewer.faces_visible:
                # Same contract as a --view miss: a screenshot promised to
                # show face boxes must not quietly come out without them.
                log.error("--faces: the viewer is not on a face-tagged photo "
                          "(pair it with --open N on one that is)")
                print(json.dumps({"event": "view", "ok": False,
                                  "kind": "faces", "key": ""}), flush=True)
                app.exit(1)
                return
            print(json.dumps({"event": "view", "ok": True, "kind": "faces",
                              "key": "", "shown": 1}), flush=True)
            return
        if args.play and not state["played"]:
            state["played"] = True
            win._start_slideshow()  # no-op (no _slideshow surface) if empty
            return
        if state["opened"] and win.viewer.loading:
            # Let the original finish loading before the shot — but never
            # wait forever: a wedged decode used to turn a scripted
            # --open/--screenshot run into a bare TIMEOUT with no diagnosis.
            # Bounded wait, then shoot whatever is painted and say why.
            state["open_wait"] = state.get("open_wait", 0) + 1
            if state["open_wait"] * self.poll.interval() < self.open_wait_ms:
                return
            dec = getattr(win.viewer, "_decoder", None)
            log.error(
                "viewer original still loading after %d ms (decoder alive=%s,"
                " queued jobs=%d) — taking the screenshot anyway",
                self.open_wait_ms, bool(dec is not None and dec.is_alive()),
                win.viewer._jobs.qsize())
            win.viewer.loading = False
        if state["played"] and win._slideshow is not None \
                and win._slideshow.loading:
            # Same bounded-wait pattern as the viewer original above, for
            # the slideshow's own first-slide decode.
            state["play_wait"] = state.get("play_wait", 0) + 1
            if state["play_wait"] * self.poll.interval() < self.open_wait_ms:
                return
            dec = getattr(win._slideshow, "_decoder", None)
            log.error(
                "slideshow original still loading after %d ms (decoder "
                "alive=%s, queued jobs=%d) — taking the screenshot anyway",
                self.open_wait_ms, bool(dec is not None and dec.is_alive()),
                win._slideshow._jobs.qsize())
            win._slideshow.loading = False
        if not self.may_quit():
            return  # --finish-build: hold the quit for the cache build
        if args.screenshot is not None and not state["shot"]:
            state["shot"] = True
            # --play's surface is its own top-level window (slideshow.py
            # module docstring) — grab THAT, not the main window it sits
            # on top of, once it is actually up.
            shot_target = win
            if state["played"] and win._slideshow is not None \
                    and win._slideshow.isVisible():
                shot_target = win._slideshow
            if shot_target.grab().save(str(args.screenshot)):
                log.info("screenshot: %s", args.screenshot)
            else:
                log.error("FAILED to save screenshot to %s", args.screenshot)
                app.exit(1)
                return
            app.quit()
        elif args.quit_after_ready or args.search_probe is not None:
            app.quit()
        else:
            self.poll.stop()  # interactive run: instrumentation is done

    def start(self) -> None:
        """Arm the decode-sandbox-degrade poll and the ready-poll timer (and,
        for a scripted run, the hard-stop timeout). Timers are parented to
        `win` and stopped explicitly by stop() — see the class docstring for
        why an unparented/unstopped timer is a real (previously-hit) bug,
        not just tidiness."""
        win, app, args = self.win, self.app, self.args
        # Parented to the window it polls (fauxcasa-q6l.15): check_ready
        # dereferences win.grid, and the timeout connection forms a Python
        # reference cycle (poll -> check_ready -> poll) that kept the orphan
        # timer alive past main()'s return — an in-process caller (the test
        # suite) that later deleted the window got a stale fire into a
        # deleted GridView (rediscovered independently three times before
        # the fix).
        self.decode_sandbox_poll = QTimer(win)
        self.decode_sandbox_poll.setInterval(1000)
        self.decode_sandbox_poll.timeout.connect(self.sync_decode_sandbox_label)
        self.decode_sandbox_poll.start()

        self.poll = QTimer(win)
        self.poll.setObjectName("ready-poll")
        self.poll.setInterval(50)
        self.poll.timeout.connect(self.check_ready)
        self.poll.start()

        # Hard stop for scripted runs: a stuck decode must fail loudly, not
        # hang CI or masquerade as success.
        #
        # Parented and stopped below for the same reason as the poll timer
        # above (fauxcasa-9pr, the case q6l.15 missed): a bare
        # QTimer.singleShot belongs to no object and nothing cancels it, so a
        # run that finishes INSIDE its own deadline leaves the deadline armed
        # in the shared QApplication. Whichever later in-process run happened
        # to be in app.exec() when it fired was killed with exit 1 and a log
        # line quoting the dead run's timeout and state — an ubuntu-only
        # tracer failure on main, and a latent flake everywhere else.
        if self.scripted_run:
            def on_timeout() -> None:
                log.error("TIMEOUT after %ss — ready=%s state=%s",
                          args.timeout, win.ready_reported, self.state)
                app.exit(1)

            self.hard_stop = QTimer(win)
            self.hard_stop.setObjectName("scripted-hard-stop")
            self.hard_stop.setSingleShot(True)
            self.hard_stop.timeout.connect(on_timeout)
            self.hard_stop.start(int(args.timeout * 1000))

    def stop(self) -> None:
        """Stop every timer this run started — belt to the parenting
        suspenders: never fire post-exec."""
        if self.poll is not None:
            self.poll.stop()
        if self.decode_sandbox_poll is not None:
            self.decode_sandbox_poll.stop()
        if self.hard_stop is not None:
            self.hard_stop.stop()
