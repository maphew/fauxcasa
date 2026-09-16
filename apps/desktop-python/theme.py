"""Shared color palette for the tracer desktop app (fauxcasa-ez2.4).

Before this module, grid.py/tray.py/viewer.py each hard-coded their own
near-identical dark grays as module-level QColor literals (a 2026-09 UX
audit's first finding), and no Qt widget outside the custom-painted
surfaces (menus, the sidebar tree, buttons) knew the app was dark at all
— they stayed in whatever native/light style Qt picked. This module is
the single source for both: the named constants below back every custom
paintEvent (grid/tray/viewer/slideshow; peek.py inherits viewer's
unchanged), and dark_palette() feeds QApplication.setPalette() so the
ordinary Qt chrome (menus, tree, buttons, tooltips) matches under the
Fusion style — the one built-in style that honors every QPalette role on
every platform (native styles ignore most of them). Wiring
setStyle("Fusion") + setPalette(dark_palette()) into main.py is a
follow-up (main.py is out of this bead's scope).

Light/dark switching (fauxcasa-6y0): every named constant is still a
plain module global, but its value now comes from apply_scheme() picking
between the _DARK/_LIGHT tables below via globals().update() — so a
scheme switch is visible to already-imported call sites (paint code,
tests) without re-importing anything. main.py follows the OS scheme by
default (theme.apply_mode()) and offers an explicit View > Theme
override; see resolve_scheme()/apply_mode() at the bottom of this file.

ACCENT decision: the app icon (assets/icon.svg — "lens horizon") is a
teal tile (#1E7A82) with a cream lens, a warm orange sun (#F0592E), and a
green hill (#3F9B6E). Selection/current-item chrome takes the sun orange
rather than the old generic UI blue (64,140,255) — checked in the polish
scratch driver against warm (orange/yellow) photo content specifically,
the risk case for a warm accent. A selected photo keeps its own true
colors: grid.py paints ACCENT_SOFT's translucent wash only in the ring
between the halo rect and the tile rect itself (QRegion subtraction,
behind the pixmap), never over the thumbnail's own pixels, so the check
was of the OUTLINE and the star badge's contrast against warm/yellow
tiles — both hold up: the ACCENT outline reads as a crisper, more
saturated edge than the photo's own warm fill, and the star keeps its
own 1px dark outline regardless. PLAY stays a plain green — it names an
action (playback), not brand identity, and doesn't compete with ACCENT
since the two are never adjacent chrome on the same element.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette

# ---- scheme tables (fauxcasa-6y0) --------------------------------------
# Every named constant below is a module global, kept in sync with one of
# these two tables by apply_scheme() (globals().update(table)) so existing
# `theme.X` reads — in paint code and in tests — need no change; only the
# VALUE behind the name changes when the scheme switches. _DARK is the
# original (and still default) palette, verbatim. _LIGHT is a light
# reflection of it: most semantic/brand colors (TEAL, ACCENT, ACCENT_SOFT,
# PLAY, PLAY_WHITE, STAR, STAR_OUTLINE, GEOTAG) are kept identical between
# schemes — they were picked for contrast against photo content, not
# against the chrome, and PLAY_WHITE in particular is drawn over photos.
_DARK: dict[str, QColor] = {
    "WINDOW": QColor(24, 24, 24),       # app/grid background
    "BASE": QColor(18, 18, 18),         # list/tree/edit field backgrounds
    "SURFACE": QColor(34, 34, 34),      # raised panel: headers, tray, buttons
    "ALT_BASE": QColor(28, 28, 28),     # zebra/alternate rows
    # Deliberately darker than WINDOW: the viewer/slideshow/peek surfaces
    # are a photo-viewing context (no sibling chrome competing for
    # attention), so they keep their own near-black backdrop rather than
    # adopting the grid's lighter WINDOW gray — a named, intentional
    # surface, not a stray literal.
    "VIEWER_BG": QColor(12, 12, 12),
    "TEXT": QColor(220, 220, 220),
    "TEXT_MUTED": QColor(150, 150, 150),
    "TEAL": QColor(0x1E, 0x7A, 0x82),             # icon tile
    "ACCENT": QColor(0xF0, 0x59, 0x2E),           # icon sun: selection / current
    "ACCENT_SOFT": QColor(0xF0, 0x59, 0x2E, 60),  # translucent gutter fill
    "HOVER_OUTLINE": QColor(255, 255, 255, 102),  # 40% white — hover-only chrome
    "PLAY": QColor(80, 200, 80),          # actionable play glyph (group-header button)
    "PLAY_WHITE": QColor(235, 235, 235),  # passive "this is a video" badge/transport glyph
    "STAR": QColor(255, 200, 40),
    "STAR_OUTLINE": QColor(40, 30, 0, 200),   # 1px dark outline so gold reads on gold/yellow photos
    "GEOTAG": QColor(64, 205, 175),
    "ERROR_TILE": QColor(96, 40, 40),
    "PLACEHOLDER": QColor(60, 60, 60),     # cache not built yet
    "HIDDEN_VEIL": QColor(0, 0, 0, 110),   # reveal mode: dim hidden/stash tiles
    "HEADER_BG": QColor(34, 34, 34),
    "HEADER_FG": QColor(200, 200, 200),
    "HEADER_RULE": QColor(58, 58, 58),     # 1px lighter top rule
    "CAPTION_BG": QColor(0, 0, 0, 170),
    "CAPTION_FG": QColor(220, 220, 220),
    "HINT_FG": QColor(120, 120, 120),
    "FIELD_BORDER": QColor(96, 96, 96),    # border for text fields; must stay clearly apart from WINDOW
}

_LIGHT: dict[str, QColor] = {
    "WINDOW": QColor(240, 240, 240),
    "BASE": QColor(255, 255, 255),
    "SURFACE": QColor(228, 228, 228),
    "ALT_BASE": QColor(247, 247, 247),
    "VIEWER_BG": QColor(214, 214, 214),
    "TEXT": QColor(28, 28, 28),
    "TEXT_MUTED": QColor(105, 105, 105),
    "TEAL": _DARK["TEAL"],
    "ACCENT": _DARK["ACCENT"],
    "ACCENT_SOFT": _DARK["ACCENT_SOFT"],
    "HOVER_OUTLINE": QColor(0, 0, 0, 102),
    "PLAY": _DARK["PLAY"],
    "PLAY_WHITE": _DARK["PLAY_WHITE"],  # drawn over photos, not chrome
    "STAR": _DARK["STAR"],
    "STAR_OUTLINE": _DARK["STAR_OUTLINE"],
    "GEOTAG": _DARK["GEOTAG"],
    "ERROR_TILE": QColor(226, 178, 178),
    "PLACEHOLDER": QColor(204, 204, 204),
    "HIDDEN_VEIL": QColor(255, 255, 255, 120),
    "HEADER_BG": QColor(226, 226, 226),
    "HEADER_FG": QColor(56, 56, 56),
    "HEADER_RULE": QColor(200, 200, 200),
    "CAPTION_BG": QColor(255, 255, 255, 180),
    "CAPTION_FG": QColor(28, 28, 28),
    "HINT_FG": QColor(140, 140, 140),
    "FIELD_BORDER": QColor(160, 160, 160),
}

SCHEMES = ("dark", "light")

_current_scheme = "dark"

# ---- surfaces ---------------------------------------------------------
WINDOW = _DARK["WINDOW"]       # app/grid background
BASE = _DARK["BASE"]         # list/tree/edit field backgrounds
SURFACE = _DARK["SURFACE"]      # raised panel: headers, tray, buttons
ALT_BASE = _DARK["ALT_BASE"]     # zebra/alternate rows
# Deliberately darker than WINDOW: the viewer/slideshow/peek surfaces are
# a photo-viewing context (no sibling chrome competing for attention), so
# they keep their own near-black backdrop rather than adopting the grid's
# lighter WINDOW gray — a named, intentional surface, not a stray literal.
VIEWER_BG = _DARK["VIEWER_BG"]

# ---- text ---------------------------------------------------------
TEXT = _DARK["TEXT"]
TEXT_MUTED = _DARK["TEXT_MUTED"]

# ---- brand accent (apps/desktop-python/assets/icon.svg) ---------------
TEAL = _DARK["TEAL"]             # icon tile
ACCENT = _DARK["ACCENT"]           # icon sun: selection / current
ACCENT_SOFT = _DARK["ACCENT_SOFT"]  # translucent gutter fill
HOVER_OUTLINE = _DARK["HOVER_OUTLINE"]  # 40% white — hover-only chrome

# ---- semantic ---------------------------------------------------------
PLAY = _DARK["PLAY"]          # actionable play glyph (group-header button)
PLAY_WHITE = _DARK["PLAY_WHITE"]  # passive "this is a video" badge/transport glyph
STAR = _DARK["STAR"]
STAR_OUTLINE = _DARK["STAR_OUTLINE"]   # 1px dark outline so gold reads on gold/yellow photos
GEOTAG = _DARK["GEOTAG"]
ERROR_TILE = _DARK["ERROR_TILE"]
PLACEHOLDER = _DARK["PLACEHOLDER"]     # cache not built yet
HIDDEN_VEIL = _DARK["HIDDEN_VEIL"]   # reveal mode: dim hidden/stash tiles

# ---- headers / chrome ---------------------------------------------------------
HEADER_BG = _DARK["HEADER_BG"]
HEADER_FG = _DARK["HEADER_FG"]
HEADER_RULE = _DARK["HEADER_RULE"]     # 1px lighter top rule
CAPTION_BG = _DARK["CAPTION_BG"]
CAPTION_FG = _DARK["CAPTION_FG"]
HINT_FG = _DARK["HINT_FG"]
# Border for text fields on chrome; must stay clearly apart (in either
# direction) from WINDOW — lighter in dark mode, darker in light mode.
FIELD_BORDER = _DARK["FIELD_BORDER"]


def current_scheme() -> str:
    """The scheme name ("dark"/"light") last applied by apply_scheme()."""
    return _current_scheme


def apply_scheme(name: str) -> None:
    """Repoint every named constant above at the named scheme's table
    (fauxcasa-6y0). Uses globals().update() rather than assigning new
    names, so it can only ever change the VALUE behind an existing
    `theme.X` — paint code and tests that read theme.X pick up the new
    color on their next paint/assert with no code change, and a stray key
    typo in a scheme table can never silently create a new module
    attribute."""
    global _current_scheme
    if name not in SCHEMES:
        raise ValueError(f"unknown scheme: {name!r}")
    table = _DARK if name == "dark" else _LIGHT
    assert set(table) <= set(globals()), "scheme table defines an unknown name"
    globals().update(table)
    _current_scheme = name


def build_palette(name: str | None = None) -> QPalette:
    """A Fusion-style QPalette built from the CURRENT constants above (or
    from `name`'s scheme, if given — apply_scheme(name) runs first so the
    two never disagree), so stock Qt widgets (menus, the sidebar tree,
    toolbar buttons, tooltips) read as part of the same app as the
    custom-painted grid/viewer/tray (fauxcasa-ez2.4 UX audit; scheme
    switching added fauxcasa-6y0). Only meaningful under
    QApplication.setStyle("Fusion") — other native styles largely ignore
    a custom QPalette. Sets the Active/Inactive roles the audit's
    screenshots actually exercise (Window/WindowText/Base/AlternateBase/
    Text/Button/ButtonText/Highlight/HighlightedText/Link/ToolTipBase/
    ToolTipText) plus the Disabled group, so a disabled action or an
    offline sidebar row dims instead of vanishing into the surface. Also
    sets PlaceholderText and Mid so the toolbar search box reads as an
    editable field rather than empty chrome (fauxcasa-e2y)."""
    if name is not None:
        apply_scheme(name)
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, WINDOW)
    pal.setColor(QPalette.ColorRole.WindowText, TEXT)
    pal.setColor(QPalette.ColorRole.Base, BASE)
    pal.setColor(QPalette.ColorRole.AlternateBase, ALT_BASE)
    pal.setColor(QPalette.ColorRole.Text, TEXT)
    pal.setColor(QPalette.ColorRole.Button, SURFACE)
    pal.setColor(QPalette.ColorRole.ButtonText, TEXT)
    pal.setColor(QPalette.ColorRole.Highlight, ACCENT)
    # Dark keeps WINDOW (near-black) as HighlightedText, matching the
    # original dark_palette() exactly. Light would put near-white WINDOW
    # text on the ACCENT orange highlight — too close in luminance to
    # ACCENT's own warm fill — so light uses plain white instead
    # (fauxcasa-6y0).
    pal.setColor(QPalette.ColorRole.HighlightedText,
                 WINDOW if _current_scheme == "dark" else QColor(255, 255, 255))
    pal.setColor(QPalette.ColorRole.Link, TEAL)
    pal.setColor(QPalette.ColorRole.ToolTipBase, SURFACE)
    pal.setColor(QPalette.ColorRole.ToolTipText, TEXT)
    pal.setColor(QPalette.ColorRole.PlaceholderText, TEXT_MUTED)
    pal.setColor(QPalette.ColorRole.Mid, FIELD_BORDER)

    disabled = QPalette.ColorGroup.Disabled
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        pal.setColor(disabled, role, TEXT_MUTED)
    return pal


def dark_palette() -> QPalette:
    """Thin wrapper kept for existing call sites/tests: apply the dark
    scheme and return its QPalette."""
    return build_palette("dark")


def light_palette() -> QPalette:
    """Apply the light scheme and return its QPalette (fauxcasa-6y0)."""
    return build_palette("light")


MODES = ("system", "light", "dark")


def resolve_scheme(mode: str, os_scheme) -> str:
    """Map a user-facing MODE ("system"/"light"/"dark") plus the current
    Qt.ColorScheme reported by the OS to one of SCHEMES. "light"/"dark"
    are explicit overrides and ignore os_scheme entirely. "system" maps
    Qt.ColorScheme.Light -> "light", Dark -> "dark", and Unknown -> "dark"
    — the last matching today's look (the only scheme that existed before
    fauxcasa-6y0), so offscreen tests and platforms that never report a
    scheme are unchanged."""
    if mode in ("light", "dark"):
        return mode
    if mode != "system":
        raise ValueError(f"unknown theme mode: {mode!r}")
    if os_scheme == Qt.ColorScheme.Light:
        return "light"
    return "dark"  # Dark or Unknown


def apply_mode(app, mode: str) -> str:
    """Resolve MODE against the OS's current color scheme
    (app.styleHints().colorScheme()), apply that scheme's constants, set
    it as the QApplication palette, and return the scheme name actually
    applied. Does not touch the OS-reported scheme itself — only the
    palette changes here."""
    scheme = resolve_scheme(mode, app.styleHints().colorScheme())
    apply_scheme(scheme)
    app.setPalette(build_palette())
    return scheme
