"""Tests for main.py application icon and visual polish.

Split from test_tracer.py (fauxcasa-l09); originally lines 18995-19470 of the monolith."""

from __future__ import annotations

import sys
from pathlib import Path
import pytest
from catalog import (
    scan_library,
)
from tracer_helpers import (
    APP_DIR,
    _click,
    _offscreen_app,
    _press,
    _search_win,
    _selection_grid,
    make_jpeg,
)


# ---------- application icon (rel-0.1) ----------


def test_asset_path_resolves_committed_icon_set() -> None:
    """The runtime icon is assembled from the committed output of
    assets/make-icons.py: every size app_icon() consumes, plus the SVG
    source and the .ico the PyInstaller spec embeds, must exist in a
    source checkout — a missing raster would silently drop that size."""
    import main

    for px in main.ICON_SIZES:
        p = main.asset_path("icon.png" if px == 256 else f"icon-{px}.png")
        assert p.is_file(), p
    assert main.asset_path("icon.svg").is_file()
    assert main.asset_path("icon.ico").is_file()


def test_asset_path_resolves_inside_bundle_when_frozen(
        monkeypatch, tmp_path: Path) -> None:
    """Frozen: the spec's datas land under sys._MEIPASS/assets, so the
    resolver must switch its base there (PyInstaller's documented contract)
    rather than trust APP_DIR, which only coincides by __file__ convention."""
    import main

    monkeypatch.setattr(main, "FROZEN", True)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert main.asset_path("icon.png") == tmp_path / "assets" / "icon.png"
    monkeypatch.setattr(main, "FROZEN", False)
    assert main.asset_path("icon.png") == main.APP_DIR / "assets" / "icon.png"


def test_app_icon_carries_every_size_and_windows_get_it(library: Path) -> None:
    """app_icon() is non-null, offers each pre-rendered size as its own
    pixmap (no downscale at the taskbar's 16/32), and MainWindow — plus the
    separate top-level slideshow and peek surfaces it creates lazily —
    carry it after construction (offscreen-safe: nothing needs a display)."""
    _offscreen_app()
    from PySide6.QtCore import QSize, Qt
    import main

    icon = main.app_icon()
    assert not icon.isNull()
    sizes = {(s.width(), s.height()) for s in icon.availableSizes()}
    assert sizes >= {(px, px) for px in main.ICON_SIZES}
    assert not icon.pixmap(QSize(16, 16)).isNull()   # decodes, not just listed

    cat = scan_library(library)
    win = main.MainWindow(cat, None, cache_dir=None, build_dir=None)
    assert not win.windowIcon().isNull()
    win.grid.peek_requested.emit(0)                   # lazily creates the peek
    assert win._peek_page is not None
    assert not win._peek_page.windowIcon().isNull()
    win._hide_peek()
    win._play_group(win.grid.groups[0].folder)        # lazily creates the show
    assert win._slideshow is not None
    assert not win._slideshow.windowIcon().isNull()
    _press(win._slideshow, Qt.Key.Key_Escape)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shell API only")
def test_windows_app_user_model_id_derives_from_app_name(monkeypatch) -> None:
    """The taskbar identity string is built from APP_NAME (the provisional
    name's single source of truth), never a second hard-coded copy, and
    obeys the AppUserModelID rules (no spaces, at most 128 chars)."""
    import ctypes
    import main

    seen: list[str] = []
    monkeypatch.setattr(ctypes.windll.shell32,
                        "SetCurrentProcessExplicitAppUserModelID",
                        lambda s: seen.append(s) or 0)
    main._set_windows_app_user_model_id()
    assert seen == [f"{main.APP_NAME}.Desktop"]
    assert " " not in seen[0] and len(seen[0]) <= 128


def test_windows_app_user_model_id_is_noop_elsewhere(monkeypatch) -> None:
    """Off Windows the helper must return without touching ctypes.windll
    (which does not exist there) — never raise."""
    import main

    monkeypatch.setattr(sys, "platform", "linux")
    main._set_windows_app_user_model_id()


# ---------- visual polish (fauxcasa-ez2.4) ----------


def test_theme_dark_palette_sets_expected_roles() -> None:
    """dark_palette() sets every role the audit's screenshots exercise —
    Window/WindowText/Base/AlternateBase/Text/Button/ButtonText/Highlight/
    HighlightedText/Link/ToolTipBase/ToolTipText — plus the Disabled
    group, all sourced from theme's own named constants, so a
    Fusion-styled QApplication reads as part of the same app as the
    custom-painted grid/viewer/tray."""
    _offscreen_app()
    from PySide6.QtGui import QPalette

    import theme

    pal = theme.dark_palette()
    R = QPalette.ColorRole
    assert pal.color(R.Window) == theme.WINDOW
    assert pal.color(R.WindowText) == theme.TEXT
    assert pal.color(R.Base) == theme.BASE
    assert pal.color(R.AlternateBase) == theme.ALT_BASE
    assert pal.color(R.Text) == theme.TEXT
    assert pal.color(R.Button) == theme.SURFACE
    assert pal.color(R.ButtonText) == theme.TEXT
    assert pal.color(R.Highlight) == theme.ACCENT
    assert pal.color(R.HighlightedText) == theme.WINDOW
    assert pal.color(R.Link) == theme.TEAL
    assert pal.color(R.ToolTipBase) == theme.SURFACE
    assert pal.color(R.ToolTipText) == theme.TEXT
    disabled = QPalette.ColorGroup.Disabled
    for role in (R.WindowText, R.Text, R.ButtonText):
        assert pal.color(disabled, role) == theme.TEXT_MUTED


# ---------- light/dark theme, window level (fauxcasa-6y0) ----------


def test_theme_menu_and_persistence(search_library: Path, monkeypatch) -> None:
    """View > Theme (fauxcasa-6y0): an exclusive System/Light/Dark radio
    group, "system" checked by default, driving theme.apply_mode plus
    persistence through _save_theme_mode/_load_theme_mode — monkeypatched
    here to a plain dict so the test never touches the real per-user
    config.json. _set_theme_mode/_toggle_theme keep the radio group in
    sync and apply the QApplication palette; restores "dark" in a
    finally."""
    import main
    import theme
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication

    store: dict[str, str] = {}
    monkeypatch.setattr(main, "_load_theme_mode",
                        lambda cache_root: store.get("theme", "system"))
    monkeypatch.setattr(
        main, "_save_theme_mode",
        lambda cache_root, mode: store.__setitem__("theme", mode))
    win = None
    try:
        win = _search_win(search_library)
        assert win.theme_mode == "system"
        assert win.theme_menu is not None
        assert len(win.theme_actions) == 3
        assert [a.data() for a in win.theme_actions] == \
            ["system", "light", "dark"]
        assert [a.data() for a in win.theme_actions if a.isChecked()] == \
            ["system"]

        win.show()  # grab() below needs an actual paint
        QApplication.instance().processEvents()

        win._set_theme_mode("light")
        QApplication.instance().processEvents()
        assert QApplication.instance().palette().color(
            QPalette.ColorRole.Window).getRgb()[:3] == (240, 240, 240)
        assert theme.current_scheme() == "light"
        assert store["theme"] == "light"
        assert [a.data() for a in win.theme_actions if a.isChecked()] == \
            ["light"]
        # Qt resolves a QSS palette(…) function against the QApplication
        # palette AT setStyleSheet() time and CACHES it (Opus review
        # finding): the search box's border would stay the OLD scheme's
        # FIELD_BORDER forever without _refresh_theme's clear-then-
        # reset-the-same-sheet trick. Probe the actual rendered pixel at
        # the box's left edge, not just the sheet text, so a regression
        # that re-breaks the cache (e.g. skipping the clear step) fails
        # this test even though the sheet STRING never changes.
        h = win.search.height()
        border_px = win.search.grab().toImage().pixelColor(0, h // 2)
        assert border_px.getRgb()[:3] == theme.FIELD_BORDER.getRgb()[:3]

        win._toggle_theme()
        assert theme.current_scheme() == "dark"
        assert store["theme"] == "dark"
        assert [a.data() for a in win.theme_actions if a.isChecked()] == \
            ["dark"]
    finally:
        theme.apply_mode(QApplication.instance(), "dark")
        if win is not None:
            win.hide()


def test_theme_toggle_shortcut_from_keymap(search_library: Path) -> None:
    """The View > Theme > Toggle Light/Dark action's real QAction
    shortcuts come from the keymap (fauxcasa-6y0) — never hard-coded,
    the same rule app.play/app.info follow."""
    import keymap

    win = _search_win(search_library)
    assert win.theme_toggle_menu_action.shortcuts() == \
        keymap.shortcuts("app.theme_toggle")


def test_refresh_theme_preserves_selected_view(search_library: Path,
                                               tmp_path: Path) -> None:
    """_refresh_theme()'s _rebuild_sidebar() call must be bracketed with
    _selected_view()/_reselect_view() like every other rebuild call site
    (_toggle_reveal, _toggle_folder_view, ...) — Opus review blocker:
    without the bracket, switching theme (or the OS flipping scheme at
    sunset via colorSchemeChanged) silently reset the current view back
    to All photos (fauxcasa-6y0).

    Built with an explicit tmp cache_root because _set_theme_mode below
    persists the mode to self.cache_root, exactly as _remember_library
    does (main.py, _change_library) — a MainWindow method writing the
    user's choice to its own cache root is the established behavior, so
    the fix is to give the TEST a cache root of its own instead of
    making persistence conditional. Without it this test merge-wrote the
    theme key into the developer's real config.json (fauxcasa-6y0
    review)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator

    import theme

    try:
        win = _search_win(search_library, cache_root=tmp_path / "cr")
        it = QTreeWidgetItemIterator(win.tree)
        folder_item = None
        while it.value():
            data = it.value().data(0, Qt.ItemDataRole.UserRole)
            if data is not None and data[0] == "folder":
                folder_item = it.value()
                break
            it += 1
        assert folder_item is not None, "search_library has no folder item"
        win._sidebar_clicked(folder_item, 0)
        before = win._selected_view()
        assert before[0] == "folder"

        win._refresh_theme()
        assert win._selected_view() == before

        win._set_theme_mode("light")
        assert win._selected_view() == before
    finally:
        theme.apply_mode(QApplication.instance(), "dark")


def test_on_os_color_scheme_changed_respects_mode(
        search_library: Path) -> None:
    """MainWindow._on_os_color_scheme_changed (fauxcasa-6y0), the
    QStyleHints.colorSchemeChanged slot: in "system" mode it re-resolves
    against the real app.styleHints().colorScheme() (offscreen always
    reports Unknown -> "dark" — see resolve_scheme), so calling it from
    "light" flips to "dark"; in an explicit "light"/"dark" mode it must
    be a complete no-op, leaving the scheme untouched. Called directly
    (a bound-method slot, not a closure) rather than faking a real
    QStyleHints signal emission."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    import theme

    try:
        win = _search_win(search_library)

        win.theme_mode = "system"
        theme.apply_scheme("light")
        win._on_os_color_scheme_changed(Qt.ColorScheme.Dark)
        assert theme.current_scheme() == "dark"

        win.theme_mode = "light"
        theme.apply_scheme("light")
        win._on_os_color_scheme_changed(Qt.ColorScheme.Dark)
        assert theme.current_scheme() == "light"  # untouched: not "system"
    finally:
        theme.apply_mode(QApplication.instance(), "dark")


def test_grid_empty_text_paints_only_when_set(tmp_path: Path) -> None:
    """An empty display paints nothing extra when empty_text is unset
    (the default — main.py picks per-view copy) and the given text,
    centered in TEXT_MUTED, once it is set (fauxcasa-ez2.4 UX audit: an
    empty grid used to paint literally nothing at all)."""
    _offscreen_app()
    from PySide6.QtGui import QImage
    from grid import BACKGROUND, GridView

    root = tmp_path / "lib"
    root.mkdir()
    cat = scan_library(root)          # no photos: an empty catalog
    g = GridView()
    g.resize(300, 200)
    g.show()
    g.set_data(cat, None)
    assert g.display == [] and g.empty_text == ""

    shot = g.viewport().grab().toImage().convertToFormat(
        QImage.Format.Format_RGB32)
    assert all(shot.pixelColor(x, y) == BACKGROUND
              for x in (0, shot.width() // 2, shot.width() - 1)
              for y in (0, shot.height() // 2, shot.height() - 1))

    g.empty_text = "No photos here"
    g.viewport().update()
    shot2 = g.viewport().grab().toImage().convertToFormat(
        QImage.Format.Format_RGB32)
    cy = shot2.height() // 2
    found_text = any(shot2.pixelColor(x, y) != BACKGROUND
                     for x in range(0, shot2.width(), 2)
                     for y in range(max(0, cy - 8), cy + 8))
    assert found_text


def test_grid_selection_wash_never_tints_the_photo_pixels(
        tmp_path: Path) -> None:
    """A selected tile keeps its own true colors: the translucent ACCENT
    halo paints only in the ring between the tile rect and its margin
    (QRegion subtraction, behind the pixmap) — never over the photo's own
    pixels. The margin just outside the tile DOES pick up chrome
    (coordinator follow-up on fauxcasa-ez2.4: painting the fill over/
    after the image used to tint every selected thumbnail — e.g. a blue
    tile reading mauve in 02-grid-multi-select.png)."""
    from PySide6.QtGui import QColor, QImage

    from grid import BACKGROUND

    g = _selection_grid(tmp_path)
    d = g.display
    idx = d[0]
    solid = QColor(90, 140, 190)
    img = QImage(g.tile, g.tile, QImage.Format.Format_RGB32)  # fills `r` exactly
    img.fill(solid)
    gen = g.generation
    g.done.put((gen, idx, img))
    g._pump_decoded()

    gi, n = g.loc[idx]
    r = g._item_rect(g.groups[gi], n)
    top = g.verticalScrollBar().value()
    cx, cy = r.center().x(), r.center().y() - top
    ring_y = cy
    ring_pts = [(r.x() - o, ring_y) for o in (1, 2, 3, 4)]

    shot0 = g.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
    assert shot0.pixelColor(cx, cy) == solid   # sanity: the fed tile painted

    _click(g, idx)   # select it (also current: the strongest chrome)
    shot1 = g.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
    assert shot1.pixelColor(cx, cy) == solid   # untouched by selection

    found_chrome = any(shot1.pixelColor(x, y) != BACKGROUND
                       for x, y in ring_pts)
    assert found_chrome   # the halo/outline still shows — just not on the photo


def test_grid_tooltip_shows_name_caption_date_html_escaped(
        tmp_path: Path, monkeypatch) -> None:
    """viewportEvent(QEvent.ToolTip) hit-tests the item under the cursor
    and shows name/caption/date; a caption containing markup renders
    LITERALLY (html.escape'd) rather than being interpreted, and hovering
    empty background falls back to the existing Ctrl+Alt peek hint
    (fauxcasa-ez2.4 UX audit: one static grid-wide tooltip used to hide
    every per-photo name)."""
    import html as html_mod

    from PySide6.QtCore import QEvent, QPoint
    from PySide6.QtGui import QHelpEvent

    import grid

    g = _selection_grid(tmp_path)
    d = g.display
    cat = g.catalog
    photo = cat.photos[d[0]]
    photo.caption = "<b>hi</b>"
    photo.date_taken = "2020-01-02T03:04:05"

    calls: list[tuple] = []
    monkeypatch.setattr(grid.QToolTip, "showText",
                        lambda *a, **kw: calls.append((a, kw)))

    gi, n = g.loc[d[0]]
    r = g._item_rect(g.groups[gi], n)
    center = r.center() - QPoint(0, g.verticalScrollBar().value())
    ev = QHelpEvent(QEvent.Type.ToolTip, center,
                    g.viewport().mapToGlobal(center))
    assert g.viewportEvent(ev) is True
    assert len(calls) == 1
    text = calls[0][0][1]
    assert html_mod.escape(photo.name) in text
    assert "&lt;b&gt;hi&lt;/b&gt;" in text
    assert "<b>hi</b>" not in text
    assert "2020-01-02 03:04:05" in text

    calls.clear()
    off_pos = QPoint(2, 2)   # header band, no photo
    ev2 = QHelpEvent(QEvent.Type.ToolTip, off_pos,
                     g.viewport().mapToGlobal(off_pos))
    assert g.viewportEvent(ev2) is True
    assert calls[0][0][1] == g.toolTip()   # peek-hint fallback


def test_grid_header_elides_long_and_empty_descriptions(
        tmp_path: Path) -> None:
    """_elide (group-header description) truncates text too wide for its
    budget and returns "" for an empty/absent one; Folder.description
    (catalog.py's .picasa.ini [Picasa] description= parse) reaches
    _Group.description end to end, and the header paints without
    incident for both a described and a bare folder (fauxcasa-ez2.4 UX
    audit: headers used to show only "title · N" and silently drop the
    description)."""
    _offscreen_app()
    from PySide6.QtGui import QFont, QFontMetrics

    from grid import GridView, _elide

    fm = QFontMetrics(QFont())
    assert _elide(fm, "", 200) == ""
    assert _elide(fm, "short", 0) == ""
    assert _elide(fm, "short", 200) == "short"
    long_text = "a very long folder description " * 10
    elided = _elide(fm, long_text, 60)
    assert elided != long_text and elided != ""
    assert fm.horizontalAdvance(elided) <= 60

    root = tmp_path / "lib"
    make_jpeg(root / "described" / "a.jpg")
    make_jpeg(root / "bare" / "b.jpg")
    (root / "described" / ".picasa.ini").write_text(
        "[Picasa]\r\ndescription=" + long_text + "\r\n")
    cat = scan_library(root)
    g = GridView()
    g.resize(400, 300)
    g.show()
    g.set_data(cat, None)
    by_folder = {grp.folder: grp for grp in g.groups}
    assert by_folder["described"].description == long_text
    assert by_folder["bare"].description is None
    assert not g.grab().isNull()      # paints both without incident


def test_viewer_fit_rect_excludes_caption_bar(tmp_path: Path) -> None:
    """paintEvent fits the photo into `height - CAPTION_H` whenever the
    caption bar is visible, so the bar never covers the bottom of the
    image (fauxcasa-ez2.4 UX audit: the fit rect used to use the FULL
    widget height — a native-capped original centered in that too-tall
    box left a slice of the photo under the semi-transparent bar instead
    of leaving that slice to the bar alone)."""
    _offscreen_app()
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QImage

    from viewer import CAPTION_H, ViewerPage

    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    cat = scan_library(root)
    v = ViewerPage(cat, None)
    v.resize(400, 300)
    v.show()
    assert v._caption_visible() and v._caption_h() == CAPTION_H

    # A tall, narrow ORIGINAL exactly `height - CAPTION_H` px tall caps at
    # native size (cap=True): flush to the top in the FIXED box; 15px down
    # (letterboxed under the bar) in the old, too-tall one.
    orig = QImage(100, 270, QImage.Format.Format_RGB32)
    orig.fill(0xFFFFFF)
    fit = v._shown_rect(v.width(), v.height() - v._caption_h(), orig)
    assert fit == QRect(150, 0, 100, 270)

    v.show_photo(list(range(len(cat.photos))), 0)
    v._on_loaded(v._serial, orig)
    shot = v.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
    assert shot.pixelColor(200, 5).red() > 200     # photo, not letterboxed
    v.quiesce()


# ---------- polish day 2 (fauxcasa-ez2.14): icons, welcome dialog, chevrons ----------


def test_icons_make_icon_every_glyph_has_1x_and_2x() -> None:
    """make_icon() paints a real (non-null) pixmap at both 16px (1x) and
    32px (2x) for every glyph name the toolbar/sidebar use, and raises
    KeyError — loud, not a blank tile — on an unknown name."""
    _offscreen_app()
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QColor

    import icons

    for name in ("library", "back", "play", "info", "folder", "album",
                "person", "star", "clock", "zoom_small", "zoom_large"):
        icon = icons.make_icon(name, QColor(220, 220, 220))
        sizes = set(icon.availableSizes())
        assert QSize(16, 16) in sizes and QSize(32, 32) in sizes, name
        pm = icon.pixmap(16, 16)
        assert not pm.isNull()
        # The real content check is that SOME pixel carries alpha — proof
        # the glyph actually painted something onto the transparent ground.
        img = pm.toImage()
        painted = any(
            img.pixelColor(x, y).alpha() > 0
            for x in range(16) for y in range(16))
        assert painted, f"{name}: pixmap is fully transparent"

    with pytest.raises(KeyError):
        icons.make_icon("not-a-glyph", QColor(0, 0, 0))


def test_toolbar_actions_carry_icons(tmp_path: Path) -> None:
    """The Library/Gallery/Play/Info toolbar actions (ez2.14) each carry a
    non-null QIcon, and text labels stay (ToolButtonTextBesideIcon) —
    icons are a scan aid, not a replacement for the label."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    win = MainWindow(scan_library(root), None, cache_dir=None, build_dir=None)
    for action in (win.open_action, win.back_action, win.play_action,
                  win.info_action):
        assert not action.icon().isNull(), action.text()
        assert action.text()   # label kept


def test_sidebar_items_carry_icons(tmp_path: Path) -> None:
    """Starred/Recently-Updated/Folders/Albums/People root rows and their
    Folder/Album/Person children all carry a non-null icon (ez2.14)."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItemIterator
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "Animals" / "cat.jpg")
    cat = scan_library(root)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    kinds_seen = set()
    it = QTreeWidgetItemIterator(win.tree)
    while it.value():
        item = it.value()
        d = item.data(0, Qt.ItemDataRole.UserRole)
        if d is not None and d[0] in ("starred", "recent", "folders_root",
                                      "folder"):
            assert not item.icon(0).isNull(), d
            kinds_seen.add(d[0])
        it += 1
    assert {"starred", "recent", "folders_root", "folder"} <= kinds_seen


def test_flat_checkbox_removed_from_sidebar_panel(tmp_path: Path) -> None:
    """The bare 'Flat' QCheckBox no longer sits above the tree (ez2.14) —
    only the tree fills the sidebar panel; the View menu's Flat Folders
    action and the Folders-root context menu are the two surfaces left."""
    _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    win = MainWindow(scan_library(root), None, cache_dir=None, build_dir=None)

    layout = win._sidebar_panel.layout()
    widgets = [layout.itemAt(i).widget() for i in range(layout.count())]
    assert win._flat_check not in widgets
    assert win.tree in widgets
    # The state holder still exists and still drives _build_sidebar/menus.
    assert win._flat_check.isChecked() is False


def test_folders_root_context_menu_toggles_flat_and_stays_in_sync(
        tmp_path: Path) -> None:
    """Right-clicking the Folders root (ez2.14) gets a checkable 'Flat
    Folders' action mirroring the View menu's identical action — both
    read/write the same self._flat_check state, so toggling either one
    rebuilds the sidebar and leaves the other in sync."""
    _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "Animals" / "cat.jpg")
    win = MainWindow(scan_library(root), None, cache_dir=None, build_dir=None)

    menu = win._folders_root_menu()
    acts = [a for a in menu.actions() if a.isCheckable()]
    assert len(acts) == 1 and acts[0].text() == "Flat Folders"
    assert not acts[0].isChecked()

    acts[0].trigger()   # toggles ON via the context-menu action
    assert win._flat_check.isChecked() is True

    # The View menu action was built from the same _flat_check and stays
    # in sync going the other way too.
    win._flat_check.setChecked(False)
    remenu = win._folders_root_menu()
    assert not remenu.actions()[0].isChecked()


def test_sidebar_menu_ignores_folders_root_click_for_view_selection(
        tmp_path: Path) -> None:
    """Clicking (not right-clicking) the Folders header must not reset the
    active grid view to All photos — only real folder/album/etc rows do
    that (regression guard for ez2.14's header now carrying UserRole
    data so the context menu can find it)."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItemIterator
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "Animals" / "cat.jpg")
    make_jpeg(root / "Zebra" / "z.jpg")
    win = MainWindow(scan_library(root), None, cache_dir=None, build_dir=None)

    # Select a specific folder first, so we can detect an unwanted reset.
    it = QTreeWidgetItemIterator(win.tree)
    folder_item = None
    while it.value():
        d = it.value().data(0, Qt.ItemDataRole.UserRole)
        if d is not None and d == ("folder", "Zebra"):
            folder_item = it.value()
            break
        it += 1
    assert folder_item is not None
    win._sidebar_clicked(folder_item, 0)
    before = list(win.grid.display)

    headers = [win.tree.topLevelItem(i)
               for i in range(win.tree.topLevelItemCount())]
    folders_header = next(
        h for h in headers
        if h.data(0, Qt.ItemDataRole.UserRole) == ("folders_root", ""))
    win._sidebar_clicked(folders_header, 0)
    assert win.grid.display == before
