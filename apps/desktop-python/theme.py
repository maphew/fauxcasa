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

from PySide6.QtGui import QColor, QPalette

# ---- surfaces ---------------------------------------------------------
WINDOW = QColor(24, 24, 24)       # app/grid background
BASE = QColor(18, 18, 18)         # list/tree/edit field backgrounds
SURFACE = QColor(34, 34, 34)      # raised panel: headers, tray, buttons
ALT_BASE = QColor(28, 28, 28)     # zebra/alternate rows
# Deliberately darker than WINDOW: the viewer/slideshow/peek surfaces are
# a photo-viewing context (no sibling chrome competing for attention), so
# they keep their own near-black backdrop rather than adopting the grid's
# lighter WINDOW gray — a named, intentional surface, not a stray literal.
VIEWER_BG = QColor(12, 12, 12)

# ---- text ---------------------------------------------------------
TEXT = QColor(220, 220, 220)
TEXT_MUTED = QColor(150, 150, 150)

# ---- brand accent (apps/desktop-python/assets/icon.svg) ---------------
TEAL = QColor(0x1E, 0x7A, 0x82)             # icon tile
ACCENT = QColor(0xF0, 0x59, 0x2E)           # icon sun: selection / current
ACCENT_SOFT = QColor(0xF0, 0x59, 0x2E, 60)  # translucent gutter fill
HOVER_OUTLINE = QColor(255, 255, 255, 102)  # 40% white — hover-only chrome

# ---- semantic ---------------------------------------------------------
PLAY = QColor(80, 200, 80)          # actionable play glyph (group-header button)
PLAY_WHITE = QColor(235, 235, 235)  # passive "this is a video" badge/transport glyph
STAR = QColor(255, 200, 40)
STAR_OUTLINE = QColor(40, 30, 0, 200)   # 1px dark outline so gold reads on gold/yellow photos
GEOTAG = QColor(64, 205, 175)
ERROR_TILE = QColor(96, 40, 40)
PLACEHOLDER = QColor(60, 60, 60)     # cache not built yet
HIDDEN_VEIL = QColor(0, 0, 0, 110)   # reveal mode: dim hidden/stash tiles

# ---- headers / chrome ---------------------------------------------------------
HEADER_BG = QColor(34, 34, 34)
HEADER_FG = QColor(200, 200, 200)
HEADER_RULE = QColor(58, 58, 58)     # 1px lighter top rule
CAPTION_BG = QColor(0, 0, 0, 170)
CAPTION_FG = QColor(220, 220, 220)
HINT_FG = QColor(120, 120, 120)


def dark_palette() -> QPalette:
    """A Fusion-style dark QPalette built from the constants above, so
    stock Qt widgets (menus, the sidebar tree, toolbar buttons, tooltips)
    read as part of the same app as the custom-painted grid/viewer/tray
    (fauxcasa-ez2.4 UX audit). Only meaningful under
    QApplication.setStyle("Fusion") — other native styles largely ignore
    a custom QPalette. Sets the Active/Inactive roles the audit's
    screenshots actually exercise (Window/WindowText/Base/AlternateBase/
    Text/Button/ButtonText/Highlight/HighlightedText/Link/ToolTipBase/
    ToolTipText) plus the Disabled group, so a disabled action or an
    offline sidebar row dims instead of vanishing into the dark surface."""
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, WINDOW)
    pal.setColor(QPalette.ColorRole.WindowText, TEXT)
    pal.setColor(QPalette.ColorRole.Base, BASE)
    pal.setColor(QPalette.ColorRole.AlternateBase, ALT_BASE)
    pal.setColor(QPalette.ColorRole.Text, TEXT)
    pal.setColor(QPalette.ColorRole.Button, SURFACE)
    pal.setColor(QPalette.ColorRole.ButtonText, TEXT)
    pal.setColor(QPalette.ColorRole.Highlight, ACCENT)
    pal.setColor(QPalette.ColorRole.HighlightedText, WINDOW)
    pal.setColor(QPalette.ColorRole.Link, TEAL)
    pal.setColor(QPalette.ColorRole.ToolTipBase, SURFACE)
    pal.setColor(QPalette.ColorRole.ToolTipText, TEXT)

    disabled = QPalette.ColorGroup.Disabled
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        pal.setColor(disabled, role, TEXT_MUTED)
    return pal
