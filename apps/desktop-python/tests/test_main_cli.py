"""Tests for main.py view spec and run_main CLI wiring.

Split from test_tracer.py (fauxcasa-l09); originally lines 19473-19797 of the monolith."""

from __future__ import annotations

import os
import sys
from pathlib import Path
import pytest
from catalog import (
    scan_library,
)
from tracer_helpers import (
    VIEW_SPEC_ALBUM_UID,
    _inspector_text,
    _offscreen_app,
    _run_main_capturing_window,
    make_jpeg,
)


# ---------------------------------------------------------------------------
# Scripted-run screenshot flags (--view/--search/--select/--info/--play/
# --window-size): main.py CLI additions that let a headless --screenshot
# run capture every major surface (albums, people, search, the inspector,
# the slideshow) for documentation screenshots.
# ---------------------------------------------------------------------------


def test_select_sidebar_view_matches_each_kind(
        view_spec_library: Path) -> None:
    """select_sidebar_view walks the sidebar tree and applies a view
    exactly like a real click, for every SPEC kind --view can name
    (album accepts either its uid or its display name); an unknown kind
    or a spec with no matching item returns False and leaves the grid
    untouched."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow, select_sidebar_view

    cat = scan_library(view_spec_library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    def rels() -> list[str]:
        return [cat.photos[i].rel for i in win.grid.display]

    assert select_sidebar_view(win, "all")
    assert len(win.grid.display) == 4

    assert select_sidebar_view(win, "starred")
    assert rels() == ["Trip/d.jpg"]

    assert select_sidebar_view(win, f"album:{VIEW_SPEC_ALBUM_UID}")
    assert rels() == ["Trip/a.jpg"]

    assert select_sidebar_view(win, "album:Best Of")  # by display name too
    assert rels() == ["Trip/a.jpg"]

    assert select_sidebar_view(win, "person:Ada Example")
    assert rels() == ["Trip/a.jpg"]

    assert select_sidebar_view(win, "unnamed")
    assert rels() == ["Trip/b.jpg"]

    assert select_sidebar_view(win, "folder:Trip")
    assert win.tree.currentItem().data(0, Qt.ItemDataRole.UserRole) \
        == ("folder", "Trip")

    before = list(win.grid.display)
    assert not select_sidebar_view(win, "album:no-such-album")
    assert not select_sidebar_view(win, "person:Nobody Here")
    assert not select_sidebar_view(win, "bogus:xyz")
    assert win.grid.display == before  # no match -> no state change


def test_window_size_flag_resizes(
        monkeypatch, library: Path, tmp_path: Path) -> None:
    """--window-size WxH resizes the window to exactly that size before
    show() — a stable screenshot size regardless of the machine's screen.
    Asserted on a WARM run: a COLD run's window briefly grows past the
    requested size while the deferred first-scan build lands (a
    pre-existing Qt/offscreen relayout quirk unrelated to this flag — the
    exact same drift happens with no --window-size at all), but a warm
    run — the realistic case for a documentation --screenshot pass
    against an already-built library — holds the requested size exactly."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import main

    app = QApplication.instance() or QApplication([])
    assert app is not None
    cache_root = tmp_path / "cr"

    monkeypatch.setattr(sys, "argv", [
        "fauxcasa-tracer", str(library), "--cache-root", str(cache_root),
        "--quit-after-ready", "--finish-build", "--timeout", "30"])
    assert main.main() == 0  # cold: build the cache once

    captured: list = []
    real_init = main.MainWindow.__init__

    def spy_init(self, *a, **kw):
        real_init(self, *a, **kw)
        captured.append(self)

    monkeypatch.setattr(main.MainWindow, "__init__", spy_init)
    monkeypatch.setattr(sys, "argv", [
        "fauxcasa-tracer", str(library), "--cache-root", str(cache_root),
        "--window-size", "900x600", "--quit-after-ready",
        "--finish-build", "--timeout", "30"])
    assert main.main() == 0  # warm: the actual assertion run
    assert len(captured) == 1
    assert (captured[0].width(), captured[0].height()) == (900, 600)


def test_scripted_select_and_info_populate_inspector(
        monkeypatch, library: Path, tmp_path: Path) -> None:
    """--select 1 --info leaves the inspector panel visible and populated
    for the 2nd displayed photo, end to end through main()'s scripted-run
    state machine."""
    win = _run_main_capturing_window(monkeypatch, [
        str(library), "--cache-root", str(tmp_path / "cr"),
        "--select", "1", "--info", "--quit-after-ready",
        "--finish-build", "--timeout", "30"])
    assert win.info_action.isChecked()
    assert not win.inspector.isHidden()
    assert win.grid.current == win.grid.display[1]
    assert win.inspector._form.rowCount() > 0
    assert _inspector_text(win.inspector) \
        .find(win.catalog.photos[win.grid.display[1]].name) >= 0


def test_scripted_play_starts_slideshow_offscreen(
        monkeypatch, library: Path, tmp_path: Path) -> None:
    """--play (screenshot testing) starts the slideshow over the current
    view, offscreen-safe — no real display needed to prove the state
    machine kicked it off."""
    win = _run_main_capturing_window(monkeypatch, [
        str(library), "--cache-root", str(tmp_path / "cr"),
        "--play", "--quit-after-ready", "--finish-build", "--timeout", "30"])
    assert win._slideshow is not None
    assert win._slideshow.isFullScreen()


def test_scripted_faces_flag_shows_viewer_face_boxes(
        monkeypatch, view_spec_library: Path, tmp_path: Path) -> None:
    """--faces after --open toggles the viewer's face overlay on (the F
    key) when the opened photo carries face tags, so a documentation
    screenshot can show Picasa's face boxes without a keypress."""
    win = _run_main_capturing_window(monkeypatch, [
        str(view_spec_library), "--cache-root", str(tmp_path / "cr"),
        "--view", "person:Ada Example", "--open", "0", "--faces",
        "--quit-after-ready", "--finish-build", "--timeout", "30"])
    assert win.pages.currentWidget() is win.viewer
    assert win.catalog.photos[win.viewer.current_index()].faces
    assert win.viewer.faces_visible


def test_window_size_arg_rejects_junk(monkeypatch) -> None:
    """--window-size reuses --min-image-size's WIDTHxHEIGHT parser, so
    junk is rejected at argparse time (SystemExit 2), before any window
    is ever constructed."""
    import main

    monkeypatch.setattr(sys, "argv", [
        "fauxcasa-tracer", "--window-size", "junk"])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2


def test_scripted_hard_stop_timer_is_owned_and_disarmed(
        monkeypatch, library: Path, tmp_path: Path) -> None:
    """fauxcasa-9pr: the scripted-run hard stop must be a QTimer owned by
    the window and explicitly disarmed once main() returns, exactly like
    the READY poll timer (fauxcasa-q6l.15). A bare QTimer.singleShot is
    owned by nobody and survives the run that armed it."""
    from PySide6.QtCore import QTimer

    win = _run_main_capturing_window(monkeypatch, [
        str(library), "--cache-root", str(tmp_path / "cr"),
        "--quit-after-ready", "--finish-build", "--timeout", "30"])
    hard_stop = win.findChild(QTimer, "scripted-hard-stop")
    assert hard_stop is not None, \
        "scripted run armed its hard stop on no owner"
    assert not hard_stop.isActive(), \
        "hard stop still armed after main() returned"
    poll = win.findChild(QTimer, "ready-poll")
    assert poll is not None and not poll.isActive(), \
        "the READY poll outlived the run that armed it (fauxcasa-q6l.15)"


def test_abandoned_hard_stop_does_not_fire_after_its_run(
        monkeypatch, library: Path, tmp_path: Path, capsys) -> None:
    """fauxcasa-9pr, the behaviour the shape above protects: a scripted
    run that finishes BEFORE its own --timeout used to leave the deadline
    armed in the shared QApplication, so it fired into whatever later run
    happened to be inside app.exec() and killed it with exit 1 — quoting
    the dead run's timeout and state dict. Seen on main as an ubuntu-only
    tracer failure (run 34885536050): test_window_size_flag_resizes died
    on 'TIMEOUT after 10.0s ... scan_failure_handled: True' borrowed from
    a scan-failure test ten seconds earlier.

    A rc of 0 is itself proof the run beat its own deadline, so anything
    logging TIMEOUT after that is by definition a deadline that outlived
    its run. Asserts on capsys' stderr mirror (applog's _StderrHandler),
    NOT caplog: applog sets `log.propagate = False` on the 'fauxcasa'
    logger on purpose, and a caplog form of this assertion was measured passing
    against the UNFIXED main.py when run alone — vacuous. Same convention,
    and the same reason, as the note on
    test_cmd_promote_requires_explicit_library: caplog cannot reliably see
    this logger. (It is not that it never can — a direct probe does capture
    from it — so the mechanism is narrower than 'never propagates' and
    looks ordering-dependent. fauxcasa-47f tracks the sibling test that
    still uses the caplog form.)"""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    import main

    app = QApplication.instance() or QApplication([])
    assert app is not None
    monkeypatch.setattr(sys, "argv", [
        "fauxcasa-tracer", str(library), "--cache-root", str(tmp_path / "cr"),
        "--quit-after-ready", "--finish-build", "--timeout", "2"])
    assert main.main() == 0  # rc 0 => it finished inside its own 2 s deadline
    capsys.readouterr()      # discard the run's own output

    # Spin the reused QApplication past that abandoned deadline. A slow
    # runner cannot make this vacuous: it would fail the rc above, loudly,
    # rather than quietly skip the race.
    #
    # This must drive app.exec(), NOT a bare QEventLoop() (see the note on
    # test_ready_poll_timer_dies_with_the_window, whose own spin loop this
    # was originally copied from): after main.main() has called
    # QCoreApplication.quit() once (the --quit-after-ready path), Qt leaves
    # QThreadData::quitNow set, and every later bare QEventLoop().exec() on
    # this thread returns in well under a millisecond WITHOUT servicing any
    # pending timer, forever — only QCoreApplication::exec() resets that
    # flag on entry. A bare-QEventLoop() version of this spin is silently a
    # no-op no matter how many rounds or how long each singleShot is, so it
    # stays green whether or not the abandoned hard-stop is actually
    # disarmed — vacuous against the very bug this test guards.
    for _ in range(45):  # ~2.7 s
        QTimer.singleShot(60, app.quit)
        app.exec()
    err = capsys.readouterr().err
    assert "TIMEOUT after" not in err, \
        f"the abandoned --timeout fired after its run: {err}"


# ---------------------------------------------------------------------------
# Scoped star views (fauxcasa-q6l.20), clause (b): the Starred collection is
# date-grouped (month buckets, newest first, pinned headers/jump/play like
# the main grid) instead of one flat unbroken run. set_filter grows an
# optional `grouper` hook for this; the default per-folder grouping is
# unchanged (guarded by the pre-existing sort/default-view tests above).
# ---------------------------------------------------------------------------


def test_set_filter_custom_grouper_overrides_default_folder_grouping(
        tmp_path: Path) -> None:
    """The `grouper` hook fully replaces folder grouping: a caller-defined
    key/title/description collapses two folders into one group, and group
    ORDER follows first appearance in the caller's `indices` list — the
    caller controls order, not some grouper-internal iteration."""
    _offscreen_app()
    from grid import GridView

    root = tmp_path / "lib"
    make_jpeg(root / "folder_a" / "img1.jpg")
    make_jpeg(root / "folder_b" / "img2.jpg")
    cat = scan_library(root)
    g = GridView()
    g.set_data(cat, None)
    assert len(g.groups) == 2   # default: one group per folder, unchanged

    def one_group(cat, i):
        return "all", "Everything", "a description"

    g.set_filter(list(range(len(cat.photos))), "custom", grouper=one_group)
    assert len(g.groups) == 1
    assert g.groups[0].folder == "all"
    assert g.groups[0].title == "Everything"
    assert g.groups[0].description == "a description"
    assert sorted(g.groups[0].items) == list(range(len(cat.photos)))

    def by_parity(cat, i):
        key = "odd" if i % 2 else "even"
        return key, key, None

    g.set_filter([1, 0], "parity", grouper=by_parity)   # odd's index first
    assert [grp.folder for grp in g.groups] == ["odd", "even"]
