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
    # Video transport strip (viewer.py _paint_video_chrome): these are
    # drawn on the CAPTION_BG strip, i.e. on chrome, not over the photo
    # — so unlike PLAY_WHITE they must follow the scheme
    # (fauxcasa-6y0 review). FG is the play/pause glyph and the played
    # part of the seek bar; TRACK is the unplayed remainder.
    "TRANSPORT_FG": QColor(235, 235, 235),
    "TRANSPORT_TRACK": QColor(255, 255, 255, 60),
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
    # QPalette.HighlightedText (build_palette): text drawn ON TOP of the
    # ACCENT highlight fill, e.g. a selected menu row. Plain white on
    # ACCENT is only 3.40:1 (Opus review, fauxcasa-6y0) — this near-black
    # clears that comfortably on both schemes.
    "HIGHLIGHT_TEXT": QColor(24, 24, 24),
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
    # PLAY is CHROME here (the group-header play button on HEADER_BG, the
    # toolbar Play action on SURFACE) — unlike PLAY_WHITE below, which is
    # drawn over a photo. The dark scheme's PLAY (80,200,80) only reaches
    # 1.67:1 against light HEADER_BG/SURFACE; this darker green clears
    # 4.2-4.4:1 (Opus review, fauxcasa-6y0).
    "PLAY": QColor(30, 120, 50),
    "PLAY_WHITE": _DARK["PLAY_WHITE"],  # drawn over photos, not chrome
    # Transport chrome on the light CAPTION_BG strip (white @ 180 over
    # the photo): the dark scheme's near-white glyphs/progress and
    # white@60 track vanished there (fauxcasa-6y0 review). Measured
    # against that strip over BOTH extremes of photo content — a white
    # photo (strip composites to 255) and a black one (strip
    # composites to 180): TRANSPORT_FG clears 8.4:1 (black photo) to
    # 17.4:1 (white photo), and TRANSPORT_TRACK — black @ 120, so the
    # strip still shows through as a track rather than a solid bar —
    # clears 3.07:1 and 3.63:1 respectively.
    "TRANSPORT_FG": QColor(28, 28, 28),
    "TRANSPORT_TRACK": QColor(0, 0, 0, 120),
    "STAR": _DARK["STAR"],
    "STAR_OUTLINE": _DARK["STAR_OUTLINE"],
    "GEOTAG": _DARK["GEOTAG"],
    # grid.py's _draw_error_tile fills this, then draws the broken-image
    # glyph outline and the filename label in theme.TEXT_MUTED/theme.TEXT
    # respectively (theme.TEXT for the filename since fauxcasa-6y0's Opus
    # review — the old TEXT_MUTED pairing only reached 2.94:1). This value
    # keeps >=3:1 against TEXT_MUTED (3.57:1) too, for the glyph outline.
    "ERROR_TILE": QColor(236, 200, 200),
    "PLACEHOLDER": QColor(204, 204, 204),
    "HIDDEN_VEIL": QColor(255, 255, 255, 120),
    "HEADER_BG": QColor(226, 226, 226),
    "HEADER_FG": QColor(56, 56, 56),
    "HEADER_RULE": QColor(200, 200, 200),
    "CAPTION_BG": QColor(255, 255, 255, 180),
    "CAPTION_FG": QColor(28, 28, 28),
    "HINT_FG": QColor(120, 120, 120),
    "FIELD_BORDER": QColor(130, 130, 130),
    "HIGHLIGHT_TEXT": QColor(28, 28, 28),
}

# INVARIANT: the two tables and the module-globals block below all name
# exactly the same keys. apply_scheme() repoints the constants with
# globals().update(), which — being dict.update — would happily ADD a
# name that only the tables know about; the module attribute would then
# raise AttributeError until the first apply_scheme() silently
# materialized it (fauxcasa-6y0 review). So the guard is not the update
# call: it is the explicit `NAME = _DARK["NAME"]` block below, which
# gives every key a module attribute at import time, plus _check_tables()
# just below that block, which enforces table<->table AND
# table<->globals parity. build_palette()'s src[key] lookups likewise
# assume every key it asks for exists in whichever table was picked.
# Checked with an explicit raise rather than `assert`, so `python -O`
# cannot strip the guard.

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
HOVER_OUTLINE = _DARK["HOVER_OUTLINE"]  # hover-only chrome; dark default is
# 40% white, but this differs per scheme (light is 40% black) — see _LIGHT

# ---- semantic ---------------------------------------------------------
PLAY = _DARK["PLAY"]          # actionable play glyph (group-header button)
PLAY_WHITE = _DARK["PLAY_WHITE"]  # passive "this is a video" badge/transport glyph
# Video transport strip chrome (viewer.py): scheme-variant, unlike
# PLAY_WHITE — see the _DARK/_LIGHT entries for the measured contrasts.
TRANSPORT_FG = _DARK["TRANSPORT_FG"]        # glyph + played seek bar
TRANSPORT_TRACK = _DARK["TRANSPORT_TRACK"]  # unplayed seek-bar remainder
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
HIGHLIGHT_TEXT = _DARK["HIGHLIGHT_TEXT"]  # QPalette.HighlightedText, on ACCENT


def _check_tables() -> None:
    """Enforce the parity invariant stated above _DARK/_LIGHT: the two
    scheme tables name the same keys, AND every one of those keys
    already exists as a module global. The second half is the one
    globals().update() cannot provide for itself — dict.update ADDS
    unknown keys, so without this check a key added to both tables but
    forgotten in the block above would leave theme.NEWKEY raising
    AttributeError until the first apply_scheme() call quietly created
    it (fauxcasa-6y0 review). Raises rather than asserts so `python -O`
    keeps the guard."""
    if set(_LIGHT) != set(_DARK):
        raise RuntimeError(
            "theme scheme tables must share every key; differ: "
            f"{sorted(set(_LIGHT) ^ set(_DARK))}")
    missing = sorted(k for k in _DARK if k not in globals())
    if missing:
        raise RuntimeError(
            "every theme scheme key needs a module-global default above: "
            f"{missing}")


_check_tables()


def current_scheme() -> str:
    """The scheme name ("dark"/"light") last applied by apply_scheme()."""
    return _current_scheme


def apply_scheme(name: str) -> None:
    """Repoint every named constant above at the named scheme's table
    (fauxcasa-6y0). Uses globals().update() rather than assigning new
    names, so it can only ever change the VALUE behind an existing
    `theme.X` — paint code and tests that read theme.X pick up the new
    color on their next paint/assert with no code change. (The
    both-tables-share-every-key invariant is asserted once, at module
    load, right after _LIGHT/_DARK are defined — see above.)"""
    global _current_scheme
    if name not in SCHEMES:
        raise ValueError(f"unknown scheme: {name!r}")
    globals().update(_DARK if name == "dark" else _LIGHT)
    _current_scheme = name


def build_palette(name: str | None = None) -> QPalette:
    """A Fusion-style QPalette, so stock Qt widgets (menus, the sidebar
    tree, toolbar buttons, tooltips) read as part of the same app as the
    custom-painted grid/viewer/tray (fauxcasa-ez2.4 UX audit; scheme
    switching added fauxcasa-6y0). PURE with respect to the CURRENTLY
    APPLIED scheme: given a name, it reads straight from that scheme's
    _DARK/_LIGHT table and does NOT call apply_scheme — so
    theme.build_palette("light") can preview a palette without silently
    flipping every theme.X global (and the tests/paint code reading
    them) out from under the caller. With no name, it builds from the
    CURRENT theme.X globals (today's applied scheme) — that reflects
    apply_scheme()'s last call, same as before. Only meaningful under
    QApplication.setStyle("Fusion") — other native styles largely ignore
    a custom QPalette. Sets the Active/Inactive roles the audit's
    screenshots actually exercise (Window/WindowText/Base/AlternateBase/
    Text/Button/ButtonText/Highlight/HighlightedText/Link/ToolTipBase/
    ToolTipText) plus the Disabled group, so a disabled action or an
    offline sidebar row dims instead of vanishing into the surface. Also
    sets PlaceholderText and Mid so the toolbar search box reads as an
    editable field rather than empty chrome (fauxcasa-e2y)."""
    if name is not None and name not in SCHEMES:
        raise ValueError(f"unknown scheme: {name!r}")
    src = (_DARK if name == "dark" else _LIGHT) if name is not None \
        else globals()

    def c(key: str) -> QColor:
        return src[key]

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, c("WINDOW"))
    pal.setColor(QPalette.ColorRole.WindowText, c("TEXT"))
    pal.setColor(QPalette.ColorRole.Base, c("BASE"))
    pal.setColor(QPalette.ColorRole.AlternateBase, c("ALT_BASE"))
    pal.setColor(QPalette.ColorRole.Text, c("TEXT"))
    pal.setColor(QPalette.ColorRole.Button, c("SURFACE"))
    pal.setColor(QPalette.ColorRole.ButtonText, c("TEXT"))
    pal.setColor(QPalette.ColorRole.Highlight, c("ACCENT"))
    # HIGHLIGHT_TEXT (fauxcasa-6y0 Opus review): plain white on ACCENT was
    # only 3.40:1; both schemes now name their own near-black value.
    pal.setColor(QPalette.ColorRole.HighlightedText, c("HIGHLIGHT_TEXT"))
    pal.setColor(QPalette.ColorRole.Link, c("TEAL"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, c("SURFACE"))
    pal.setColor(QPalette.ColorRole.ToolTipText, c("TEXT"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, c("TEXT_MUTED"))
    pal.setColor(QPalette.ColorRole.Mid, c("FIELD_BORDER"))

    disabled = QPalette.ColorGroup.Disabled
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        pal.setColor(disabled, role, c("TEXT_MUTED"))
    return pal


def dark_palette() -> QPalette:
    """NOT pure, unlike build_palette(name=...): applies the dark scheme
    (so every theme.X module global — read directly by grid/viewer/tray
    paint code and by tests such as test_theme_dark_palette_sets_
    expected_roles, which asserts pal.color(...) against theme.WINDOW
    etc. right after calling this) then returns its QPalette. Kept for
    those existing call sites/tests; main.py itself no longer calls this
    (theme.apply_mode is the app-startup/switch entry point)."""
    apply_scheme("dark")
    return build_palette()


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
