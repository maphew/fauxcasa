"""Tests for theme.py itself: the light/dark scheme tables, the module
globals they repoint, and the live aliases grid/viewer keep onto them.

Ported into the split tree (fauxcasa-l09) from the fauxcasa-6y0 light/dark
theme work, which landed them in the monolithic test_tracer.py. Window-level
theme tests (the View > Theme menu, persistence, the OS colorSchemeChanged
slot) live in test_main_polish.py instead."""

from __future__ import annotations

import pytest
from tracer_helpers import (
    _offscreen_app,
)


def test_dark_palette_search_affordance_roles() -> None:
    """dark_palette() sets PlaceholderText/Mid so the toolbar search box
    has visible placeholder text and a frame that reads against WINDOW —
    FIELD_BORDER itself must stay clearly lighter than WINDOW
    (fauxcasa-e2y: the field was reading as empty toolbar space)."""
    _offscreen_app()
    from PySide6.QtGui import QPalette

    import theme

    pal = theme.dark_palette()
    R = QPalette.ColorRole
    assert pal.color(R.PlaceholderText) == theme.TEXT_MUTED
    assert pal.color(R.Mid) == theme.FIELD_BORDER
    # A contrast floor, not a direction: a light palette flips the sign.
    assert abs(theme.FIELD_BORDER.lightness() - theme.WINDOW.lightness()) >= 60


def test_light_palette_roles() -> None:
    """apply_scheme("light") repoints every theme.X module global at the
    light table (fauxcasa-6y0); build_palette() reads those live, so a
    palette built right after picks up light colors, and one built after
    switching back to "dark" returns to today's values. Restores "dark"
    in a finally so later tests see today's colors regardless of
    ordering/failure."""
    _offscreen_app()
    import theme
    from PySide6.QtGui import QPalette

    try:
        theme.apply_scheme("light")
        pal = theme.build_palette()
        R = QPalette.ColorRole
        assert pal.color(R.Window).getRgb()[:3] == (240, 240, 240)
        assert theme.WINDOW.getRgb()[:3] == (240, 240, 240)
        assert theme.TEXT.getRgb()[:3] == (28, 28, 28)
        assert theme.TEXT.lightness() < theme.WINDOW.lightness()

        theme.apply_scheme("dark")
        pal2 = theme.build_palette()
        assert pal2.color(R.Window).getRgb()[:3] == (24, 24, 24)
        assert theme.WINDOW.getRgb()[:3] == (24, 24, 24)
        assert theme.TEXT.getRgb()[:3] == (220, 220, 220)
    finally:
        theme.apply_scheme("dark")


def test_grid_aliases_follow_scheme() -> None:
    """grid.BACKGROUND (module __getattr__, fauxcasa-6y0) tracks
    theme.WINDOW live under both schemes — the fix that replaced the old
    import-time snapshot (and the same fix applied to tray.py/viewer.py,
    which had the identical bug)."""
    _offscreen_app()
    import theme
    import grid

    try:
        assert grid.BACKGROUND.getRgb() == theme.WINDOW.getRgb()
        theme.apply_scheme("light")
        assert grid.BACKGROUND.getRgb() == theme.WINDOW.getRgb()
        assert grid.BACKGROUND.getRgb()[:3] == (240, 240, 240)
    finally:
        theme.apply_scheme("dark")


def _wcag_contrast(fg, bg) -> float:
    """WCAG 2.x contrast ratio between two OPAQUE (r, g, b) triples."""
    def lum(c):
        chan = []
        for v in c:
            v /= 255.0
            chan.append(v / 12.92 if v <= 0.03928
                        else ((v + 0.055) / 1.055) ** 2.4)
        return 0.2126 * chan[0] + 0.7152 * chan[1] + 0.0722 * chan[2]
    l1, l2 = sorted((lum(fg), lum(bg)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def _over(top, bottom) -> tuple[int, int, int]:
    """Source-over composite of a QColor onto an opaque (r, g, b)."""
    a = top.alpha() / 255.0
    return tuple(round(a * t + (1 - a) * b)
                 for t, b in zip(top.getRgb()[:3], bottom))


def test_transport_colors_follow_scheme() -> None:
    """The video transport strip is CHROME — it fills with
    theme.CAPTION_BG — so its play/pause glyph, seek progress and
    unplayed track take the scheme-variant theme.TRANSPORT_FG/
    TRANSPORT_TRACK, not the photo-facing PLAY_WHITE (fauxcasa-6y0
    review: near-white glyphs and a white@60 track vanished on the light
    white@180 strip). viewer's module aliases follow theme live, and both
    light values clear a 3:1 floor against that strip composited over
    BOTH extremes of photo content — a white photo and a black one."""
    _offscreen_app()
    import theme
    import viewer

    try:
        assert viewer.TRANSPORT_FG.getRgb() == theme.PLAY_WHITE.getRgb()
        assert viewer.TRANSPORT_TRACK.getRgb() == (255, 255, 255, 60)

        theme.apply_scheme("light")
        assert viewer.TRANSPORT_FG.getRgb() == theme.TRANSPORT_FG.getRgb()
        assert (viewer.TRANSPORT_TRACK.getRgb()
                == theme.TRANSPORT_TRACK.getRgb())
        # No longer the photo-facing near-white the review flagged.
        assert theme.TRANSPORT_FG.getRgb() != theme.PLAY_WHITE.getRgb()
        for photo in ((255, 255, 255), (0, 0, 0)):
            strip = _over(theme.CAPTION_BG, photo)
            assert _wcag_contrast(
                _over(theme.TRANSPORT_FG, strip), strip) >= 3.0, photo
            assert _wcag_contrast(
                _over(theme.TRANSPORT_TRACK, strip), strip) >= 3.0, photo
        # The face-name chip rides the same CAPTION_BG, so its label is
        # CAPTION_FG now (viewer._paint_faces) — also a 3:1 floor.
        for photo in ((255, 255, 255), (0, 0, 0)):
            strip = _over(theme.CAPTION_BG, photo)
            assert _wcag_contrast(
                _over(theme.CAPTION_FG, strip), strip) >= 3.0, photo
    finally:
        theme.apply_scheme("dark")


def test_theme_check_tables_guards_globals_parity() -> None:
    """theme._check_tables() enforces BOTH halves of the invariant
    (fauxcasa-6y0 review): the two scheme tables share every key, and
    every such key already exists as a module global. The second half is
    what globals().update() cannot give itself — dict.update ADDS unknown
    keys, so a key present in both tables but missing from the
    module-globals block would otherwise raise AttributeError until the
    first apply_scheme() quietly created it. A plain `raise`, not an
    `assert`, so `python -O` keeps the guard."""
    _offscreen_app()
    from PySide6.QtGui import QColor

    import theme

    theme._check_tables()  # the real tables pass

    probe = QColor(1, 2, 3)
    theme._DARK["ZZ_PROBE"] = probe
    try:
        with pytest.raises(RuntimeError, match="share every key"):
            theme._check_tables()
        theme._LIGHT["ZZ_PROBE"] = probe
        with pytest.raises(RuntimeError, match="module-global"):
            theme._check_tables()
    finally:
        theme._DARK.pop("ZZ_PROBE", None)
        theme._LIGHT.pop("ZZ_PROBE", None)
    theme._check_tables()


def test_resolve_scheme_matrix() -> None:
    """resolve_scheme's mode x os-scheme table (fauxcasa-6y0): "light"/
    "dark" are explicit overrides and ignore the OS scheme entirely;
    "system" maps Qt.ColorScheme.Light -> "light", Dark -> "dark", and
    Unknown -> "dark" (today's look, so offscreen tests — which never
    report a real OS scheme — are unchanged)."""
    from PySide6.QtCore import Qt

    import theme

    for os_scheme in (Qt.ColorScheme.Light, Qt.ColorScheme.Dark,
                      Qt.ColorScheme.Unknown):
        assert theme.resolve_scheme("light", os_scheme) == "light"
        assert theme.resolve_scheme("dark", os_scheme) == "dark"
    assert theme.resolve_scheme("system", Qt.ColorScheme.Light) == "light"
    assert theme.resolve_scheme("system", Qt.ColorScheme.Dark) == "dark"
    assert theme.resolve_scheme("system", Qt.ColorScheme.Unknown) == "dark"
