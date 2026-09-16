"""Tests for main.py sidebar and star overrides.

Split from test_tracer.py (fauxcasa-l09); originally lines 18510-18994 of the monolith."""

from __future__ import annotations

import json
from pathlib import Path
import pytest
import library as libmod
import thumbcache
from catalog import (
    scan_library,
)
from tracer_helpers import (
    _inspector_text,
    _offscreen_app,
    _press,
    _sidebar_click,
    _sidebar_text,
    _tooltip_literal,
    make_jpeg,
)


# ---------------------------------------------------------------------------
# Post-merge review findings (fauxcasa-6vk): seven small correctness bugs
# found by a cross-vendor pass over the 0.1 release candidate. Each test
# below fails on the pre-fix code and names its finding number.
# ---------------------------------------------------------------------------


def test_unstar_inside_starred_view_drops_the_photo(library: Path) -> None:
    """Finding 1: Space inside the live Starred view must remove the
    unstarred photo from the grid's materialized display — not leave a
    stale tile that contradicts the sidebar count."""
    from PySide6.QtCore import Qt
    from main import MainWindow

    _offscreen_app()
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    idx_a = next(i for i, p in enumerate(cat.photos)
                 if p.rel.endswith("Trip/a.jpg"))       # star=yes in the ini
    idx_c = next(i for i, p in enumerate(cat.photos) if p.name == "c.jpg")

    win.grid._select(idx_c)
    _press(win.grid, Qt.Key.Key_Space)                  # star the second one
    assert cat.photos[idx_c].star == 1
    _sidebar_click(win, "starred", "")
    assert sorted(win.grid.display) == sorted([idx_a, idx_c])
    assert _sidebar_text(win, "starred", "") == "Starred  (2)"

    win.grid._select(idx_a)
    _press(win.grid, Qt.Key.Key_Space)                  # unstar from inside
    assert cat.photos[idx_a].star == 0
    assert idx_a not in win.grid.display_pos
    assert win.grid.display == [idx_c]
    assert _sidebar_text(win, "starred", "") == "Starred  (1)"
    assert win.grid.current == idx_c                    # sensible landing spot
    assert "Starred: 1 photos" in win.counts_label.text()


def test_unstar_in_viewer_keeps_its_frozen_display_list(library: Path) -> None:
    """Finding 1 boundary: the viewer's display list stays frozen, so a
    photo unstarred while viewing remains reachable with Left/Right."""
    from main import MainWindow

    _offscreen_app()
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    idx_a = next(i for i, p in enumerate(cat.photos)
                 if p.rel.endswith("Trip/a.jpg"))
    idx_c = next(i for i, p in enumerate(cat.photos) if p.name == "c.jpg")
    win.grid._select(idx_c)
    from PySide6.QtCore import Qt
    _press(win.grid, Qt.Key.Key_Space)
    _sidebar_click(win, "starred", "")
    display = list(win.grid.display)
    win._open_viewer(idx_a, display, display.index(idx_a))

    win._toggle_stars([idx_a])                          # unstar while viewing
    assert cat.photos[idx_a].star == 0
    assert win.viewer.display == display                # untouched
    assert win.grid.display == display                  # grid left alone too
    win.viewer.quiesce()


@pytest.mark.parametrize("bad", ["null", "3", '"x"', '{"a": 1}'])
def test_star_overrides_survive_a_non_list_stars_field(tmp_path: Path,
                                                       bad: str) -> None:
    """Finding 3: load_star_overrides is a fail-soft loader, so a
    hand-edited stars.json whose "stars" is not a list must read as "no
    overrides" — not raise TypeError out of MainWindow.__init__."""
    import starstore

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / starstore.STAR_OVERRIDES_NAME).write_text(
        '{"version": 1, "stars": %s}' % bad)
    assert starstore.load_star_overrides(cache_dir) == {}


def test_star_overrides_skip_bad_rows_but_keep_good_ones(
        tmp_path: Path) -> None:
    """Finding 3 boundary: a LIST with junk entries keeps its valid rows
    — the per-row validation is unchanged by the type guard above."""
    import starstore

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / starstore.STAR_OVERRIDES_NAME).write_text(json.dumps({
        "version": 1,
        "stars": [
            "not a dict",
            {"root_id": "", "rel": "a.jpg", "star": 1},
            {"root_id": "", "rel": "b.jpg", "star": 9},     # out of range
            {"root_id": "", "rel": "c.jpg", "star": True},  # bool is not int
        ],
    }))
    assert starstore.load_star_overrides(cache_dir) == {("", "a.jpg"): 1}


def test_star_and_sort_state_is_shared_across_cache_variants(
        library: Path, tmp_path: Path) -> None:
    """Finding 2: stars and sort modes are USER CHOICES keyed on the
    library, not on the walk variant. Two windows over the same library
    with different scan filters (hence different cache_dirs) see the same
    state dir, so a star set under one variant is live under the other."""
    from PySide6.QtCore import Qt
    from main import MainWindow, library_state_dir, load_sort_modes
    from starstore import STAR_OVERRIDES_NAME

    _offscreen_app()
    cache_root = tmp_path / "cache"
    key = str(library.resolve())
    state_dir = library_state_dir(key, cache_root)
    plain_dir = thumbcache.cache_dir_for(key, cache_root)
    small_dir = thumbcache.cache_dir_for(key, cache_root, "scan:min=200x200")
    assert state_dir == plain_dir != small_dir   # variant moves the cache

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=plain_dir, build_dir=None,
                     state_dir=state_dir)
    idx = next(i for i, p in enumerate(cat.photos) if p.name == "c.jpg")
    win.grid._select(idx)
    _press(win.grid, Qt.Key.Key_Space)             # star it under variant A
    win._set_folder_sort("2021-05-05 Picnic", "date")
    assert (state_dir / STAR_OVERRIDES_NAME).is_file()
    assert load_sort_modes(state_dir) == {"2021-05-05 Picnic": "date"}

    # Variant B: a different cache dir, the SAME state dir.
    other = scan_library(library)
    win2 = MainWindow(other, None, cache_dir=small_dir, build_dir=None,
                      state_dir=state_dir)
    assert other.photos[idx].star == 1            # star survived the variant
    assert win2.grid.sort_modes == {"2021-05-05 Picnic": "date"}
    assert not (small_dir / STAR_OVERRIDES_NAME).exists()


def test_library_state_migrates_out_of_a_variant_cache_dir(
        library: Path, tmp_path: Path) -> None:
    """Finding 2 migration: stars/sort modes written by an older build
    into the VARIANT cache dir are lifted into the state dir on the next
    open — and the old file is left in place, never deleted."""
    import main as mainmod
    import starstore
    from main import MainWindow, load_sort_modes
    from starstore import STAR_OVERRIDES_NAME

    _offscreen_app()
    cache_root = tmp_path / "cache"
    key = str(library.resolve())
    state_dir = mainmod.library_state_dir(key, cache_root)
    variant_dir = thumbcache.cache_dir_for(key, cache_root, "exts:no=.png")
    cat = scan_library(library)
    idx = next(i for i, p in enumerate(cat.photos) if p.name == "c.jpg")
    key_c = starstore.photo_key(cat.photos[idx])
    starstore.save_star_overrides(variant_dir, {key_c: 3})
    mainmod.save_sort_modes(variant_dir, {"2021-05-05 Picnic": "size"})

    win = MainWindow(cat, None, cache_dir=variant_dir, build_dir=None,
                     state_dir=state_dir)
    assert cat.photos[idx].star == 3                     # applied at open
    assert (state_dir / STAR_OVERRIDES_NAME).is_file()   # migrated
    assert (variant_dir / STAR_OVERRIDES_NAME).is_file() # old copy kept
    assert load_sort_modes(state_dir) == {"2021-05-05 Picnic": "size"}
    assert win.grid.sort_modes == {"2021-05-05 Picnic": "size"}

    # A state dir that already has its own file wins — no re-migration.
    starstore.save_star_overrides(state_dir, {key_c: 1})
    fresh = scan_library(library)
    win2 = MainWindow(fresh, None, cache_dir=variant_dir, build_dir=None,
                      state_dir=state_dir)
    assert fresh.photos[idx].star == 1
    assert win2 is not None


def test_library_state_migration_merges_variant_only_choices(
        library: Path, tmp_path: Path) -> None:
    """Finding 2, Codex cross-vendor follow-up: a tester with state in BOTH
    the base dir and a variant dir must keep the variant-only choices.
    Base keys win on conflict (that is where this build has been
    writing); keys only the variant has are adopted, per photo for stars
    and per folder for sort modes. A second open adopts nothing more."""
    import main as mainmod
    import starstore
    from main import MainWindow, load_sort_modes
    from starstore import STAR_OVERRIDES_NAME

    _offscreen_app()
    cache_root = tmp_path / "cache"
    key = str(library.resolve())
    state_dir = mainmod.library_state_dir(key, cache_root)
    variant_dir = thumbcache.cache_dir_for(key, cache_root, "exts:no=.png")
    cat = scan_library(library)
    idx_c = next(i for i, p in enumerate(cat.photos) if p.name == "c.jpg")
    idx_o = next(i for i, p in enumerate(cat.photos) if p.name != "c.jpg")
    key_c = starstore.photo_key(cat.photos[idx_c])
    key_o = starstore.photo_key(cat.photos[idx_o])
    # Base: c starred once; folder Picnic sorted by date. (The DEFAULT
    # mode, name, is never persisted — save_sort_modes drops it — so a
    # base folder left at the default cannot "win" over a variant choice;
    # only an explicit non-default base choice can.)
    starstore.save_star_overrides(state_dir, {key_c: 1})
    mainmod.save_sort_modes(state_dir, {"2021-05-05 Picnic": "date"})
    # Variant: c starred DIFFERENTLY (must lose) plus a photo and a folder
    # the base never saw (must be adopted).
    starstore.save_star_overrides(variant_dir, {key_c: 3, key_o: 2})
    mainmod.save_sort_modes(
        variant_dir, {"2021-05-05 Picnic": "size", "2020-01-01 Other": "date"})

    win = MainWindow(cat, None, cache_dir=variant_dir, build_dir=None,
                     state_dir=state_dir)
    assert cat.photos[idx_c].star == 1          # base wins the conflict
    assert cat.photos[idx_o].star == 2          # variant-only star adopted
    assert starstore.load_star_overrides(state_dir) == {key_c: 1, key_o: 2}
    assert load_sort_modes(state_dir) == {
        "2021-05-05 Picnic": "date",             # base wins
        "2020-01-01 Other": "date",              # variant-only adopted
    }
    assert win.grid.sort_modes == load_sort_modes(state_dir)
    # Copy, never move: the variant file is untouched.
    assert starstore.load_star_overrides(variant_dir) == {key_c: 3, key_o: 2}

    # Idempotent: a second open finds nothing left to adopt and rewrites
    # neither state file.
    stars_path = state_dir / STAR_OVERRIDES_NAME
    cfg_path = mainmod._library_config_path(state_dir)
    before = (stars_path.read_bytes(), cfg_path.read_bytes())
    fresh = scan_library(library)
    win2 = MainWindow(fresh, None, cache_dir=variant_dir, build_dir=None,
                      state_dir=state_dir)
    assert (stars_path.read_bytes(), cfg_path.read_bytes()) == before
    assert fresh.photos[idx_o].star == 2
    assert win2 is not None


def test_reload_data_retires_the_viewer_gallery_action(library: Path,
                                                       tmp_path: Path) -> None:
    """Finding 4: a reconcile swap forces the page back to the browser, so
    it must also retire the viewer-only Gallery/Esc action — otherwise it
    sits on the toolbar over the grid doing nothing."""
    from main import MainWindow

    _offscreen_app()
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    display = list(win.grid.display)
    win._open_viewer(display[0], display, 0)
    assert win.back_action.isVisible()
    assert win.pages.currentWidget() is win.viewer

    other = tmp_path / "other-lib"
    make_jpeg(other / "2022 Aurora" / "borealis.jpg")
    win.reload_data(scan_library(other), None)

    assert win.pages.currentWidget() is win.pages.widget(0)
    assert not win.back_action.isVisible()
    win.viewer.quiesce()


def test_terminal_notice_takes_the_activity_spinner_down(library: Path) -> None:
    """Finding 5: a TERMINAL reconcile notice must not leave the busy
    indicator running. `status` keeps meaning "still working" and raises
    it; `notice` means "nothing more is coming" and lowers it while still
    reporting the text."""
    from main import MainWindow

    _offscreen_app()
    win = MainWindow(scan_library(library), None,
                     cache_dir=None, build_dir=None)
    win._on_status("reindexing…")                  # genuine progress
    assert not win.activity_row.isHidden()
    assert win.activity_progress.maximum() == 0    # indeterminate

    win._on_notice("library unchanged — offline, skipped: Archive")
    assert win.activity_row.isHidden()
    assert win.progress_label.text() == \
        "   library unchanged — offline, skipped: Archive"
    assert win.statusBar().currentMessage() == \
        "library unchanged — offline, skipped: Archive"


def test_offline_only_reconcile_leaves_no_spinner_running(
        tmp_path: Path) -> None:
    """Finding 5 end-to-end: a warm start whose online root is unchanged
    but whose second root is offline reports that and STOPS — the reconcile
    thread emits nothing else, so the activity row must not be up when it
    exits."""
    import time as _time

    from main import MainWindow
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    root_a = tmp_path / "root-a"
    make_jpeg(root_a / "a.jpg")
    cat = scan_library(root_a)
    # A second root whose directory does not exist -> offline (the
    # test_multiroot_flat_folder_listing fixture pattern).
    cat.roots = [libmod.LibraryRoot(id=cat.roots[0].id if cat.roots else "",
                                    path=root_a, label="A"),
                 libmod.LibraryRoot(id="bbbbbbbb", path=tmp_path / "root-b",
                                    label="Archive")]
    cat.refresh_offline_ids()
    assert cat.offline_ids == {"bbbbbbbb"}

    win = MainWindow(cat, None, cache_dir=tmp_path / "cachedir",
                     build_dir=None, warm=True)
    win._start_reconcile()
    assert win._reconcile_thread is not None
    deadline = _time.time() + 20
    while win._reconcile_thread.is_alive() and _time.time() < deadline:
        app.processEvents()
        _time.sleep(0.01)
    assert not win._reconcile_thread.is_alive()
    app.processEvents()                       # deliver the queued signal

    assert "offline, skipped: Archive" in win.progress_label.text()
    assert win.activity_row.isHidden()        # the spinner never started
    win.shutdown()


def test_inspector_rederives_after_in_place_cold_build(
        library: Path, tmp_path: Path) -> None:
    """Finding 6: the cold-build branch of _on_index_finished merges
    in-file metadata into the SAME Photo objects, so no selection signal
    follows and an open panel would keep rendering the pre-build values."""
    from main import MainWindow

    _offscreen_app()
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.info_action.setChecked(True)
    idx = next(i for i, p in enumerate(cat.photos)
               if p.rel.endswith("Trip/a.jpg"))
    win.grid._select(idx)
    assert "the beach" in _inspector_text(win.inspector)

    # What the cold build does: merge in-file metadata into the LIVE Photo
    # objects, then report through the same-catalog branch.
    result = thumbcache.build_cache(cat, tmp_path / "build")
    cat.photos[idx].caption = "in-file caption wins"
    win._on_index_finished(result, cat, False)

    text = _inspector_text(win.inspector)
    assert "in-file caption wins" in text
    assert "the beach" not in text


def test_inspector_rederives_after_metadata_backfill(library: Path) -> None:
    """Finding 6, the adopt-mode twin: backfill_catalog mutates the bound
    catalog in place, so _on_backfill_done must refresh an open panel."""
    from main import MainWindow

    _offscreen_app()
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None, adopt=True)
    win.info_action.setChecked(True)
    idx = next(i for i, p in enumerate(cat.photos)
               if p.rel.endswith("Trip/a.jpg"))
    win.grid._select(idx)
    assert "the beach" in _inspector_text(win.inspector)

    cat.photos[idx].caption = "backfilled caption"
    win._on_backfill_done(True)

    text = _inspector_text(win.inspector)
    assert "backfilled caption" in text
    assert "the beach" not in text


def test_plain_tooltip_passes_plain_text_through_and_escapes_markup() -> None:
    """Finding 7 unit: _plain_tooltip leaves ordinary text (every
    path-only tooltip) byte-identical, and renders anything Qt could
    mistake for markup as explicit, escaped HTML."""
    import main

    plain = "C:/photos/2020 Trip\nSummer holiday"
    assert main._plain_tooltip(plain) is plain          # untouched

    risky = "C:/photos/2020 Trip\n<img src=http://x/y>"
    out = main._plain_tooltip(risky)
    assert out.startswith("<html>") and "&lt;img" in out
    assert _tooltip_literal(out) == risky               # shown verbatim
    # The pre-fix string would have lost the tag entirely.
    assert "<img" not in _tooltip_literal(risky)


def test_catalog_markup_renders_literally(tmp_path: Path) -> None:
    """Finding 7: user-authored catalog text — a caption, a folder
    description, an album description — must be SHOWN, never interpreted,
    in the inspector, the status-bar readout and the sidebar tooltips."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel, QTreeWidgetItemIterator
    from main import MainWindow

    _offscreen_app()
    uid = "abcdabcdabcdabcd" * 2
    root = tmp_path / "lib"
    make_jpeg(root / "trip" / "a.jpg")
    (root / "trip" / ".picasa.ini").write_text(
        "[Picasa]\r\nname=Trip\r\ndescription=<img src=http://evil/x>\r\n"
        "[a.jpg]\r\ncaption=<b>beach</b>\r\n"
        f"albums={uid}\r\n"
        f"[.album:{uid}]\r\n"
        "name=Best Of\r\n"
        "description=<i>favourites</i>\r\n"
        f"token=]album:{uid}\r\n")
    cat = scan_library(root)
    assert cat.folders["trip"].description == "<img src=http://evil/x>"
    assert cat.albums[uid].description == "<i>favourites</i>"

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.info_action.setChecked(True)
    idx = next(i for i, p in enumerate(cat.photos) if p.name == "a.jpg")
    win.grid._select(idx)

    # Status-bar readout and the view counts are plain-text sinks.
    assert "<b>beach</b>" in win.meta_label.text()
    assert win.meta_label.textFormat() == Qt.TextFormat.PlainText
    assert win.counts_label.textFormat() == Qt.TextFormat.PlainText

    # Inspector: the VALUE label carrying the caption renders it literally.
    caption_label = next(
        lbl for lbl in win.inspector.findChildren(QLabel)
        if lbl.text() == "<b>beach</b>")
    assert caption_label.textFormat() == Qt.TextFormat.PlainText

    # Sidebar tooltips: folder description and album description.
    def item_for(kind: str, key: str):
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == (kind, key):
                return it.value()
            it += 1
        return None

    folder_tip = item_for("folder", "trip").toolTip(0)
    assert "<img src=http://evil/x>" in _tooltip_literal(folder_tip)
    assert str(root / "trip") in _tooltip_literal(folder_tip)

    album_tip = item_for("album", uid).toolTip(0)
    assert "<i>favourites</i>" in _tooltip_literal(album_tip)


def test_import_note_markup_renders_literally(tmp_path: Path) -> None:
    """Finding 7, the import-notes tooltip: report details quote album and
    file names straight from the library, so they are user-authored too."""
    from main import MainWindow
    from catalog import ReportEntry

    _offscreen_app()
    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    cat = scan_library(root)
    cat.report.entries.append(ReportEntry(
        source="ini", kind="unknown_album", subject="<b>Best Of</b>",
        detail="<b>Best Of</b> is referenced but never defined"))
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    tip = win.notes_label.toolTip()
    assert "<b>Best Of</b>" in _tooltip_literal(tip)
