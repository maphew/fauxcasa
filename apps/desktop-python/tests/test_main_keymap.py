"""Tests for keymap.py action table, menu bar, and help dialogs.

Split from test_tracer.py (fauxcasa-l09); originally lines 13644-14183 of the monolith."""

from __future__ import annotations

import os
from pathlib import Path
from catalog import (
    scan_library,
)
from tracer_helpers import (
    _click,
    _key,
    _offscreen_app,
    _selection_grid,
    _viewer_with_original,
    make_jpeg,
)


# ---------------------------------------------------------------------------
# Keymap layer (fauxcasa-q6l.8): keymap.py is the single action->QKeySequence
# default-scheme table (the spec's Picasa-compatible scheme) that grid /
# viewer / slideshow / main look bindings up from. Tests here cover the table
# integrity check (duplicate chords within a surface scope + the M2 digit/X
# reservations — the check that would have caught bare '1' colliding with a
# future star-set key), the NEW bindings the bead owes (J/K in the GRID,
# Ctrl+Enter reveal-in-file-manager from grid and viewer), and the per-
# platform launcher commands behind locate.reveal_in_file_manager (Windows
# verified for real during development; macOS/Linux structurally; Linux probe
# is async as of q6l.21). Existing key tests above pin that the refactor
# changed only the lookup mechanism.
# ---------------------------------------------------------------------------


def test_keymap_default_scheme_has_no_conflicts() -> None:
    """The shipped scheme is collision-free: no duplicate chords within a
    surface scope, and nothing squats on an M2-reserved key (digits 0-5
    star-set, X reject) beyond the one grandfathered tenant — bare '1' on
    viewer.zoom_toggle, the PR #45 arbitration.

    fauxcasa-l09: needs a live QApplication before keymap.conflicts() ->
    QKeySequence.keyBindings() -- in the pre-split monolith an earlier GUI
    test always had one running by this point; standalone, this is the
    first test in the process, so it must build its own (access violation
    otherwise)."""
    _offscreen_app()
    import keymap

    assert keymap.conflicts() == []


def test_keymap_conflict_checker_is_real() -> None:
    """The integrity check catches what it exists for. A future binding
    claiming bare '1' (the M2 star-set) fails BOTH ways: reserved-key
    violation and duplicate chord against the grandfathered zoom toggle —
    exactly the collision the '1' zoom binding would have shipped into a
    digit scheme unnoticed. X (M2 reject) and plain same-scope duplicates
    are caught too; cross-scope reuse (viewer Space vs slideshow Space)
    is not a conflict."""
    _offscreen_app()  # fauxcasa-l09: see test_keymap_default_scheme_has_no_conflicts
    import keymap

    scheme = dict(keymap.DEFAULT_SCHEME)
    scheme["viewer.star_1"] = keymap.Binding(("1",))
    found = keymap.conflicts(scheme)
    assert ("viewer.star_1", "RESERVED") in [f[:2] for f in found]
    assert any({a, b} == {"viewer.star_1", "viewer.zoom_toggle"}
               for a, b, _ in found)

    scheme = dict(keymap.DEFAULT_SCHEME)
    scheme["grid.reject"] = keymap.Binding(("X",))
    assert any(f[:2] == ("grid.reject", "RESERVED")
               for f in keymap.conflicts(scheme))

    scheme = dict(keymap.DEFAULT_SCHEME)
    scheme["grid.flip"] = keymap.Binding(("Ctrl+H",))     # dupes grid.hold
    assert any({a, b} == {"grid.flip", "grid.hold"}
               for a, b, _ in keymap.conflicts(scheme))

    # an "app." global chord conflicts INTO every surface scope
    scheme = dict(keymap.DEFAULT_SCHEME)
    scheme["app.flip"] = keymap.Binding(("Ctrl+H",))
    assert any({a, b} >= {"app.flip"} and "hold" in a + b
               for a, b, _ in keymap.conflicts(scheme))


def test_keymap_platform_correctness_rides_qt() -> None:
    """Ctrl/Cmd correctness (spec §8) comes from Qt, not per-OS tables:
    grid.select_all defers to StandardKey.SelectAll (Cmd+A on macOS from
    Qt's own binding list), and app.play resolves F11 + Ctrl+4 (the
    Picasa slideshow chord added in ed5.12) for QAction.setShortcuts."""
    _offscreen_app()  # fauxcasa-l09: see test_keymap_default_scheme_has_no_conflicts
    import keymap
    from PySide6.QtGui import QKeySequence

    assert (keymap.DEFAULT_SCHEME["grid.select_all"].standard
            == QKeySequence.StandardKey.SelectAll)
    play_strs = [s.toString() for s in keymap.shortcuts("app.play")]
    assert "F11" in play_strs
    assert "Ctrl+4" in play_strs


def test_play_tooltip_derives_from_keymap(library: Path) -> None:
    """The ▶ Play tooltip is built from keymap.shortcuts, not a hard-coded
    string — every chord in the scheme appears in the tooltip text, so
    adding or removing a chord keeps the UI self-consistent (ed5.12)."""
    import keymap
    _offscreen_app()
    from main import MainWindow
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    tip = win.play_action.toolTip()
    for seq in keymap.shortcuts("app.play"):
        assert seq.toString() in tip, (
            f"{seq.toString()!r} missing from play tooltip: {tip!r}")


def test_menu_bar_has_file_view_help_reusing_toolbar_actions(
        library: Path) -> None:
    """fauxcasa-ez2.6 §5: File/View/Help menus exist alongside the
    existing Tools menu, and every item that already has a toolbar
    QAction (Library…, Info, Play) is the SAME action object in the
    menu — never a second one with its own state."""
    from PySide6.QtWidgets import QMenu
    _offscreen_app()
    from main import MainWindow

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    # The window holds durable references to its menus (like tools_menu).
    # Do NOT re-discover them through findChildren(QMenu) / QAction.menu():
    # those transient wrappers proved timing-sensitive under the script
    # runner (uv run test_tracer.py) — "Internal C++ object already
    # deleted" on the File menu, while python -m pytest passed.
    menus = {"File": win.file_menu, "View": win.view_menu,
             "Help": win.help_menu, "Tools": win.tools_menu}
    for name, menu in menus.items():
        assert isinstance(menu, QMenu), name
        assert menu.title().replace("&", "") == name

    # fauxcasa-ez2.9: reading order in the bar itself -- File, View,
    # Tools, Help (Tools is created first in __init__, v46.4, so without
    # the ez2.9 reposition it lands leftmost instead). Menu actions carry
    # the menu title as their text, so no QMenu wrapper is needed here.
    bar_order = [a.text().replace("&", "") for a in win.menuBar().actions()
                 if a.text().replace("&", "") in menus]
    assert bar_order == ["File", "View", "Tools", "Help"], bar_order

    file_actions = menus["File"].actions()
    assert win.open_action in file_actions        # reused, not duplicated
    assert win.open_action.text() == "Library…"    # renamed from "Open..."

    view_actions = menus["View"].actions()
    assert win.info_action in view_actions
    assert win.play_action in view_actions


def test_view_menu_zoom_in_out_steps_the_slider(library: Path) -> None:
    """Zoom In/Out (View menu) step the SAME slider the toolbar drags,
    clamped to its range — one source of truth for tile size."""
    _offscreen_app()
    from main import MainWindow

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    start = win.zoom.value()
    win._step_zoom(16)
    assert win.zoom.value() == start + 16
    win._step_zoom(-16)
    assert win.zoom.value() == start

    win.zoom.setValue(win.zoom.maximum())
    win._step_zoom(16)
    assert win.zoom.value() == win.zoom.maximum()   # clamped, no overshoot
    win.zoom.setValue(win.zoom.minimum())
    win._step_zoom(-16)
    assert win.zoom.value() == win.zoom.minimum()


def test_view_menu_show_hidden_and_flat_folders_sync_checkboxes(
        library: Path) -> None:
    """The View menu's Show Hidden / Flat Folders checkable actions stay
    in lockstep with the toolbar/sidebar QCheckBoxes that actually own
    the state — driving either one moves the other, and the underlying
    view behavior (reveal_box's own handler) still fires."""
    from PySide6.QtWidgets import QMenu
    _offscreen_app()
    from main import MainWindow

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    view_menu = next(m for m in win.menuBar().findChildren(QMenu)
                     if m.title().replace("&", "") == "View")
    show_hidden = next(a for a in view_menu.actions()
                       if a.text().replace("&", "") == "Show Hidden")
    flat_folders = next(a for a in view_menu.actions()
                        if a.text().replace("&", "") == "Flat Folders")

    assert show_hidden.isChecked() == win.reveal_box.isChecked() is False
    show_hidden.trigger()
    assert win.reveal_box.isChecked() is True       # action -> checkbox
    win.reveal_box.setChecked(False)
    assert show_hidden.isChecked() is False          # checkbox -> action

    assert flat_folders.isChecked() == win._flat_check.isChecked() is False
    flat_folders.trigger()
    assert win._flat_check.isChecked() is True
    win._flat_check.setChecked(False)
    assert flat_folders.isChecked() is False


def test_help_keyboard_shortcuts_dialog_lists_every_action(
        library: Path) -> None:
    """Help > Keyboard shortcuts… builds its table at runtime from
    keymap.DEFAULT_SCHEME + ACTION_LABELS (fauxcasa-ez2.6 §5): every
    action's plain-English label appears exactly once."""
    import keymap
    _offscreen_app()
    from main import MainWindow

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    captured = {}

    def fake_exec(dlg):
        table = dlg.findChildren(__import__(
            "PySide6.QtWidgets", fromlist=["QTableWidget"]).QTableWidget)[0]
        captured["labels"] = [table.item(r, 0).text()
                              for r in range(table.rowCount())]
        return 0

    import PySide6.QtWidgets as qtw
    orig = qtw.QDialog.exec
    qtw.QDialog.exec = lambda self: fake_exec(self)
    try:
        win._show_shortcuts_dialog()
    finally:
        qtw.QDialog.exec = orig

    labels = captured["labels"]
    # live actions, then a heading, then every planned key and keyless
    # planned feature (fauxcasa-s6i) — one table, one source
    distinct = {(label, chords[0])
                for table in keymap.PLANNED_KEYS.values()
                for chords, (label, _n) in table.items()}
    assert len(labels) == (len(keymap.DEFAULT_SCHEME) + 1 + len(distinct)
                           + len(keymap.PLANNED_FEATURES))
    for action in keymap.DEFAULT_SCHEME:
        assert keymap.ACTION_LABELS[action] in labels
    heading = labels.index("— Not yet available —")
    assert heading == len(keymap.DEFAULT_SCHEME)
    assert any("Edit mode" in lbl for lbl in labels[heading:])
    assert any("face tagging" in lbl for lbl in labels[heading:])


def test_help_about_shows_icon_version_and_license(library: Path) -> None:
    """Help > About: app icon, version_string() (release identity — same
    formatter as --version/READY), the tagline, AGPL license, and the
    project URL, via a real QMessageBox so the icon reliably shows
    (about()'s convenience function leaves that to the platform)."""
    import main as mainmod
    _offscreen_app()
    from main import MainWindow

    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    captured = {}

    def fake_exec(self):
        captured["icon"] = self.iconPixmap()
        captured["text"] = self.text()
        return 0

    import PySide6.QtWidgets as qtw
    orig = qtw.QMessageBox.exec
    qtw.QMessageBox.exec = fake_exec
    try:
        win._show_about()
    finally:
        qtw.QMessageBox.exec = orig

    assert not captured["icon"].isNull()
    assert mainmod.version_string() in captured["text"]
    assert "AGPL-3.0-or-later" in captured["text"]
    assert "github.com/maphew/fauxcasa" in captured["text"]


def test_action_labels_cover_every_scheme_entry() -> None:
    """keymap.ACTION_LABELS is the single source Help > Keyboard shortcuts…
    reads (fauxcasa-ez2.6) — a scheme entry with no label would silently
    drop out of that dialog, so pin 1:1 coverage both ways."""
    import keymap

    assert set(keymap.ACTION_LABELS) == set(keymap.DEFAULT_SCHEME)


def test_keymap_app_search_focuses_and_selects_search_box(
        tmp_path: Path) -> None:
    """Ctrl+F or '/' on the grid (keymap.app.search) jumps focus to the
    search box and selects any existing text, ready to be replaced by
    typing (fauxcasa-ez2.6)."""
    from PySide6.QtCore import Qt
    app = _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    cat = scan_library(root)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.show()
    win.activateWindow()               # offscreen platform: hasFocus()
    app.processEvents()                # needs an active window (Qt quirk)
    win.search.setText("stale")
    win.grid.setFocus()

    _key(win.grid, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert win.search.hasFocus()
    assert win.search.selectedText() == "stale"

    win.search.clearFocus()
    win.grid.setFocus()
    _key(win.grid, Qt.Key.Key_Slash)
    assert win.search.hasFocus()


def test_grid_has_focus_after_construction_so_space_stars_not_types(
        tmp_path: Path) -> None:
    """MainWindow.__init__ lands keyboard focus on the grid (fauxcasa-
    ez2.6) — without it Qt's default tab order can leave the search box
    focused at launch, so a Space keystroke meant for star_toggle would
    instead type a literal space into the query. Drive the actual
    keyPressEvent through the grid (the same path a real keystroke takes
    once focus is correct) and confirm the star flipped, the search box
    stayed empty, and it is indeed the grid holding focus."""
    from PySide6.QtCore import Qt
    app = _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    cat = scan_library(root)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.show()
    win.activateWindow()               # offscreen platform: hasFocus()
    app.processEvents()                # needs an active window (Qt quirk)
    assert win.grid.hasFocus()

    win.grid._select(0)
    assert not cat.photos[0].star
    _key(win.grid, Qt.Key.Key_Space)
    assert cat.photos[0].star
    assert win.search.text() == ""


def test_grid_jk_navigate_next_prev(tmp_path: Path) -> None:
    """J/K step the grid's CURRENT item forward/back exactly like
    Right/Left (Picasa's viewer J/K, extended to the grid per q6l.8):
    plain taps collapse the selection to the target, and Shift extends
    from the anchor because they ride the same nav path as the arrows."""
    from PySide6.QtCore import Qt

    g = _selection_grid(tmp_path)
    d = g.display
    _click(g, d[0])
    _key(g, Qt.Key.Key_J)
    assert g.current == d[1] and g.selection == {d[1]}
    _key(g, Qt.Key.Key_J)
    assert g.current == d[2]
    _key(g, Qt.Key.Key_K)
    assert g.current == d[1] and g.selection == {d[1]}
    _key(g, Qt.Key.Key_J, Qt.KeyboardModifier.ShiftModifier)
    assert g.selection == {d[1], d[2]}          # Shift+J extends, as arrows
    assert g.current == d[2] and g.anchor == d[1]
    _key(g, Qt.Key.Key_K)                       # plain key: collapse again
    assert g.selection == {d[1]}


def test_grid_ctrl_enter_reveals_current(tmp_path: Path,
                                         monkeypatch) -> None:
    """Ctrl+Enter (Picasa: Locate on Disk) reveals the CURRENT item in the
    OS file manager and never activates it — plain Enter still opens the
    viewer — and with no current item the chord is a swallowed no-op. Both
    the main-row Return and keypad Enter spellings fire."""
    from PySide6.QtCore import Qt

    import grid as gridmod

    g = _selection_grid(tmp_path)
    d = g.display
    calls: list[Path] = []
    monkeypatch.setattr(gridmod, "reveal_in_file_manager",
                        lambda p: calls.append(Path(p)) or True)
    opened: list[int] = []
    g.photo_activated.connect(lambda idx, _d, _p: opened.append(idx))
    _key(g, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert calls == [] and opened == []         # no current item: no-op
    _click(g, d[1])
    _key(g, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert opened == []                         # reveal, never activate
    assert calls == [g.catalog.root / g.catalog.photos[d[1]].rel]
    _key(g, Qt.Key.Key_Enter, Qt.KeyboardModifier.ControlModifier
         | Qt.KeyboardModifier.KeypadModifier)  # keypad Enter spelling
    assert len(calls) == 2
    _key(g, Qt.Key.Key_Return)                  # plain Enter still opens
    assert opened == [d[1]]


def test_viewer_ctrl_enter_reveals_shown_photo(tmp_path: Path,
                                               monkeypatch) -> None:
    """Ctrl+Enter in the viewer reveals the photo ON SCREEN — the same
    keymap action, the same one launcher function as the grid."""
    from PySide6.QtCore import Qt

    import viewer as viewermod

    v, _ = _viewer_with_original(tmp_path)
    calls: list[Path] = []
    monkeypatch.setattr(viewermod, "reveal_in_file_manager",
                        lambda p: calls.append(Path(p)) or True)
    _key(v, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    idx = v.current_index()
    assert idx >= 0
    assert calls == [v.catalog.root / v.catalog.photos[idx].rel]
    _key(v, Qt.Key.Key_Right)                   # nav still navigates
    _key(v, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert calls[-1] == v.catalog.root / v.catalog.photos[
        v.current_index()].rel


def test_reveal_in_file_manager_per_platform(tmp_path: Path,
                                             monkeypatch) -> None:
    """The exact launcher per platform. Windows: ONE command-line string,
    explorer /select,"<native path>" (list argv would re-quote and break
    explorer's comma parsing) — this exact form was verified for real on
    Windows (explorer opens with the file selected, checked via the Shell
    COM automation API). macOS: open -R argv. Linux: FileManager1
    ShowItems probed ASYNCHRONOUSLY (q6l.21) via QProcess; settle path
    drives an xdg-open folder fallback when dbus-send fails or is missing.
    True on Linux means the async probe was started; a launcher OSError
    reports False instead of raising into a key handler."""
    import locate
    from PySide6.QtCore import QProcess

    target = tmp_path / "sub dir" / "photo one.jpg"    # spaces on purpose
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    popens: list[object] = []
    monkeypatch.setattr(locate.subprocess, "Popen",
                        lambda cmd, *a, **k: popens.append(cmd))
    assert locate.reveal_in_file_manager(target, platform="win32")
    assert popens == [f'explorer /select,"{os.path.normpath(target)}"']

    popens.clear()
    assert locate.reveal_in_file_manager(target, platform="darwin")
    assert popens == [["open", "-R", str(target)]]

    # --------------- Linux legs: fake QProcess + QTimer seam ----------------
    # The Linux path is now an async QProcess probe — we swap in a fake so
    # the test runs identically on Windows CI (no session bus needed) and
    # exercises all settle-path branches synchronously by driving signals.

    class _Sig:
        def __init__(self): self.cbs = []
        def connect(self, cb): self.cbs.append(cb)
        def emit(self, *a):
            for cb in list(self.cbs): cb(*a)

    class _FakeProc:
        # Expose the real enums: locate compares against QProcess.ExitStatus
        # through the (monkeypatched) module global, so the fake must carry them.
        ExitStatus = QProcess.ExitStatus
        ProcessError = QProcess.ProcessError
        instances: list["_FakeProc"] = []
        def __init__(self):
            self.finished = _Sig(); self.errorOccurred = _Sig()
            self.cmd = None; self.killed = False; self.deleted = False
            _FakeProc.instances.append(self)
        def start(self, program, args): self.cmd = [program, *args]
        def kill(self): self.killed = True
        def deleteLater(self): self.deleted = True

    shots: list[tuple[int, object]] = []

    class _FakeTimer:
        @staticmethod
        def singleShot(ms, cb): shots.append((ms, cb))

    monkeypatch.setattr(locate, "QProcess", _FakeProc)
    monkeypatch.setattr(locate, "QTimer", _FakeTimer)
    monkeypatch.setattr(locate, "_pending", set())

    # Leg 1: happy path — ShowItems answers OK -> no fallback at all
    popens.clear()
    _FakeProc.instances.clear()
    shots.clear()
    assert locate.reveal_in_file_manager(target, platform="linux")  # returns IMMEDIATELY
    (proc,) = _FakeProc.instances
    assert proc.cmd[0] == "dbus-send"
    assert "--dest=org.freedesktop.FileManager1" in proc.cmd
    assert proc.cmd[-2] == f"array:string:{target.absolute().as_uri()}"
    assert proc.cmd[-1] == "string:"                 # empty startup id
    assert popens == []                              # no fallback yet
    ms, _ = shots[0]
    assert ms == 3000                                # deadline matches _DBUS_TIMEOUT
    proc.finished.emit(0, QProcess.ExitStatus.NormalExit)
    assert popens == []                              # success: no folder fallback
    assert proc not in locate._pending              # reaped from set
    assert proc.deleted                             # deleteLater called

    # Leg 2: ShowItems answers nonzero -> xdg-open folder fallback
    _FakeProc.instances.clear()
    shots.clear()
    locate.reveal_in_file_manager(target, platform="linux")
    (proc,) = _FakeProc.instances
    proc.finished.emit(1, QProcess.ExitStatus.NormalExit)
    assert popens == [["xdg-open", str(target.parent)]]

    # Leg 3: dbus-send missing (FailedToStart) -> folder fallback; then a
    # late finished signal must NOT trigger a second fallback (double-settle guard)
    popens.clear()
    _FakeProc.instances.clear()
    shots.clear()
    locate.reveal_in_file_manager(target, platform="linux")
    (proc,) = _FakeProc.instances
    proc.errorOccurred.emit(QProcess.ProcessError.FailedToStart)
    assert popens == [["xdg-open", str(target.parent)]]
    proc.finished.emit(1, QProcess.ExitStatus.CrashExit)
    assert popens == [["xdg-open", str(target.parent)]]  # still exactly one

    # Leg 4: deadline fires before any signal -> kill() -> CrashExit -> fallback
    # CrashExit with code 0 must NOT count as success (pins NormalExit term)
    popens.clear()
    _FakeProc.instances.clear()
    shots.clear()
    locate.reveal_in_file_manager(target, platform="linux")
    (proc,) = _FakeProc.instances
    assert shots                                     # deadline shot registered
    deadline_ms, deadline_cb = shots[0]
    assert deadline_ms == 3000
    deadline_cb()                                    # simulate timer firing
    assert proc.killed                               # kill() called on hung probe
    proc.finished.emit(0, QProcess.ExitStatus.CrashExit)  # code=0 but CrashExit
    assert popens == [["xdg-open", str(target.parent)]]   # fallback fired

    # Leg 5: fallback Popen raising OSError is swallowed, no exception escapes
    popens.clear()
    _FakeProc.instances.clear()
    shots.clear()
    monkeypatch.setattr(locate.subprocess, "Popen",
                        lambda cmd, *a, **k: (_ for _ in ()).throw(OSError("xdg gone")))
    locate.reveal_in_file_manager(target, platform="linux")
    (proc,) = _FakeProc.instances
    proc.finished.emit(1, QProcess.ExitStatus.NormalExit)  # triggers fallback -> OSError swallowed

    # restore Popen for the final OSError->False leg below
    monkeypatch.setattr(locate.subprocess, "Popen",
                        lambda cmd, *a, **k: popens.append(cmd))

    # launcher failure -> False, never an exception into the key handler
    def _boom(cmd, *a, **k):
        raise OSError("no explorer")

    monkeypatch.setattr(locate.subprocess, "Popen", _boom)
    assert not locate.reveal_in_file_manager(target, platform="win32")
