"""Default keyboard scheme for the tracer (fauxcasa-q6l.8) — the single
source of truth the product spec (§5 "Keyboard triage loop", §8 Ctrl/Cmd)
calls the Picasa-compatible default scheme. Surfaces (grid / viewer /
slideshow / main window) look their bindings up here via matches() /
shortcuts() instead of hard-coding key comparisons. No user rebinding UI
in v1 (post-v1 work): this table IS the scheme.

Platform correctness rides Qt: QKeySequence portable strings ("Ctrl+…")
and QKeySequence.StandardKey both map Ctrl to Cmd on macOS, so every
chord below is platform-correct without per-OS tables. Where Qt defines
a StandardKey for an action (SelectAll) the table uses it, so the
platform's own binding list applies.

Picasa's documented bindings and their Fauxcasa disposition
===========================================================

Enumerated from the research corpus:
docs/research/sources/picasaresources/keyboard-shortcuts.md (the
community shortcut list, Windows + Mac columns), cross-checked against
docs/research/picasa-ui-inventory.md (menu accelerators) and
docs/research/picasa-video-notes.md §"Keyboard shortcuts observed".

Library (grid) surface:

    Picasa binding        Picasa action              Fauxcasa (this table)
    --------------------  -------------------------  --------------------------
    Ctrl+A / Cmd+A        Select all in folder       grid.select_all (StandardKey.SelectAll — shipped)
    Ctrl+D                Deselect photos            grid.deselect (ed5.12)
    Ctrl+I                Invert selection           grid.invert (ed5.12)
    Home / End            First / last photo         grid.first / grid.last (ed5.12)
    Ctrl+H                Hold selection in tray     grid.hold (shipped, fauxcasa-q6l.2)
    Space                 Add / remove star          grid.star_toggle / viewer.star_toggle
    Ctrl+Enter            Locate on Disk             grid.locate / viewer.locate (NEW here)
    Ctrl+Alt (hover)      Full-screen photo preview  PEEK_MODS chord (shipped, fauxcasa-q6l.5)
    Ctrl+1 / Ctrl+2       Small / large thumbnails   superseded by the thumbnail zoom slider
    Ctrl+4                Start a slideshow          app.play Ctrl+4 alias (ed5.12)
    F11                   Full-screen mode           REPURPOSED: app.play = slideshow of the
                                                     current view (fauxcasa-q6l.3's call)
    Ctrl+3                Edit mode                  M3 (edit room)
    Ctrl+R / Ctrl+Shift+R Rotate CW / CCW            M3 (edit)
    Ctrl+8                Add / remove star          M2 (star machinery)
    Ctrl+O/M/N/T/E/P...   File/import/album/print    later surfaces (see ui-inventory ranks)

Picasa Photo Viewer / edit-view surface (our single-photo viewer):

    Picasa binding        Picasa action              Fauxcasa (this table)
    --------------------  -------------------------  --------------------------
    Left / Right          Prev / next image          viewer.prev / viewer.next (shipped)
    J / K                 Next / prev image          viewer.next / viewer.prev (shipped);
                                                     NEW in the grid (grid.next / grid.prev)
    1                     Toggle 100% zoom           viewer.zoom_toggle — see ARBITRATION below
    Esc                   Close the viewer           viewer.close (shipped; Backspace is our
                                                     added alias, matching the slideshow)
    8                     Toggle star                M2 (reserved — see RESERVED_KEYS)
    X                     Exclude (import)           M2 reject (reserved — see RESERVED_KEYS)
    PgUp / PgDn, +/-,     Zoom in / out steps        follow-up rider from PR #45 (not v1-owed)
    mousewheel
    Space                 Add / remove star          viewer.star_toggle; slideshow.pause is
                                                     context-specific during playback
    / , .                 Video pause/rew/ff         v46.3 ships viewer.play_pause on P and
                                                     viewer.seek_back/seek_fwd on Ctrl+Left/
                                                     Ctrl+Right instead — Space is taken by
                                                     star_toggle (the triage loop's key), and
                                                     / , . remain unclaimed for a follow-up

Fauxcasa-only bindings (no Picasa equivalent):

    F11                   app.play — slideshow of the current view (q6l.3)
    F                     viewer.faces — face-region overlay (cam.4; Picasa
                                         documents no view-mode key for boxes)
    Ctrl+Alt+0            viewer.zoom_toggle — conflict-free 100% spelling
    Space                 slideshow.pause (playback context only)
    I                     app.info — metadata inspector toggle (q6l.25;
                                     Lightroom-style, no Picasa equivalent)

Every "M2"/"M3"/"later" row above is ALSO a PLANNED_KEYS entry: pressing
one shows a status-bar notice naming the feature and its milestone
(fauxcasa-s6i) rather than doing nothing, and Help > Keyboard Shortcuts
lists them under "Not yet available".

ARBITRATIONS (encoded in the table below):

* '1' = 1:1 zoom TODAY, but the digit row 0–5 is RESERVED for the M2
  star-set keys (spec §5: "star (taps stack; 0–5 set keys)"). When those
  land, '1' cedes to star-set and the zoom toggle keeps Ctrl+Alt+0 plus
  click (PR #45 rider; viewer.py module doc). viewer.zoom_toggle's bare
  '1' is the ONE grandfathered tenant of a reserved key (_GRANDFATHERED);
  conflicts() fails the suite if any other binding claims a bare digit.
* 'X' is RESERVED for the M2 reverse-star/reject key (Picasa's
  import-exclude muscle memory, spec §5) — nothing may bind it.
* Ctrl+H = hold-in-tray (shipped, q6l.2). Picasa's edit-room Ctrl+Shift+H
  (flip horizontal) is an M3 concern and does not collide (exact-chord
  matching: Ctrl+Shift+H is not Ctrl+H).
* Esc is context-layered inside the grid's handler: peek.dismiss wins
  while a peek is up (chord modifiers are physically held, hence
  key_only), then grid.clear. Different scopes, so conflicts() allows it.

Match semantics: exact chord equality with the keypad modifier stripped
(keypad Enter/1/0 count as their main-row keys, as the pre-keymap code
accepted), EXCEPT key_only bindings, which match the bare key under any
modifiers — that looseness is load-bearing where flagged (Esc while the
peek chord is held; nav keys staying nav during Shift-extension).
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QKeyCombination, Qt
from PySide6.QtGui import QKeySequence

# The hover-peek trigger chord (fauxcasa-q6l.5): Picasa's "Hover over a
# photo and use Ctrl-Alt" (the shortcut corpus). Not a QKeySequence — it
# is a modifier-only chord over a hover — so it lives beside the table as
# a constant. Qt's standard modifiers keep it platform-correct (Cmd
# reports as ControlModifier on macOS).
PEEK_MODS = (Qt.KeyboardModifier.ControlModifier
             | Qt.KeyboardModifier.AltModifier)


@dataclass(frozen=True)
class Binding:
    """One action's default chords. `chords` are QKeySequence portable
    strings; `standard` (when set) defers to the platform's own binding
    list for a Qt StandardKey; `key_only` matches the bare key ignoring
    modifiers (each use is deliberate and documented in `note`)."""
    chords: tuple[str, ...] = ()
    standard: QKeySequence.StandardKey | None = None
    key_only: bool = False
    note: str = ""


# Action names are "<scope>.<action>"; the scope is the surface whose
# keyPressEvent (or QAction) owns the binding. "app" chords are global
# window shortcuts and therefore conflict-checked against EVERY scope.
DEFAULT_SCHEME: dict[str, Binding] = {
    # ---- app: main-window QAction shortcuts ----
    "app.play": Binding(
        ("F11", "Ctrl+4"),
        note="Slideshow of the current view (q6l.3). Repurposes Picasa's "
             "F11 full-screen; Ctrl+4 is Picasa's own slideshow chord (ed5.12)."),
    "app.search": Binding(
        ("Ctrl+F", "/"),
        note="Focus the search box and select its text (ez2.6 main-window "
             "polish) — Ctrl+F is the conventional find accelerator, '/' "
             "is the common browser/mail quick-search key. Dispatched from "
             "the grid's keyPressEvent (not a window-level QAction "
             "shortcut) so a bare '/' typed while the search box ALREADY "
             "has focus keeps inserting the character instead of re-"
             "triggering; Space still reaches grid.star_toggle at launch "
             "since neither chord is Space."),
    "app.info": Binding(
        ("I",), key_only=True,
        note="Toggle the metadata inspector panel (q6l.25) — this app's "
             "own pick (Lightroom-style info toggle); the shortcut corpus "
             "documents no Picasa equivalent. Dispatched PER-SURFACE "
             "(grid.py / viewer.py keyPressEvent), never as a window-level "
             "QAction shortcut — a bare-letter global shortcut would fire "
             "while typing in the search box. key_only ignores modifiers, "
             "so it is checked AFTER each surface's exact Ctrl-chord "
             "actions (grid.invert's Ctrl+I above all) — same key_only-"
             "layering convention as grid.star_toggle's Space vs "
             "Shift+Space."),
    "app.theme_toggle": Binding(
        ("Ctrl+Shift+D",),
        note="Toggle light/dark (fauxcasa-6y0) — a modifier chord, so a "
             "window-level QAction shortcut is safe while typing in the "
             "search box (same reasoning as app.play's F11/Ctrl+4). "
             "Ctrl+D is grid.deselect and Ctrl+Shift+R is the M3 "
             "counter-clockwise rotate reservation above, so this picks "
             "the next free Ctrl+Shift chord rather than colliding with "
             "either."),

    # ---- grid (library) ----
    "grid.select_all": Binding(
        standard=QKeySequence.StandardKey.SelectAll,
        note="Picasa Ctrl+A; StandardKey = Cmd+A on macOS for free."),
    "grid.hold": Binding(
        ("Ctrl+H",),
        note="Picasa: Hold selected photos in Photo Tray (q6l.2)."),
    "grid.star_toggle": Binding(
        ("Space",), key_only=True,
        note="Picasa: add/remove star on the selected/current photo(s)."),
    "grid.star_clear": Binding(
        ("Shift+Space",),
        note="Bulk-unstar (fauxcasa-q6l.20 clause c, spec §3): clears "
             "stars on the WHOLE current selection (or the current photo "
             "alone) in one gesture — Space's natural sibling, no Picasa "
             "equivalent documented. An EXACT chord checked BEFORE "
             "grid.star_toggle's key_only Space (conflicts() allows this "
             "layering — same convention as app.info vs Ctrl-chords)."),
    "grid.deselect": Binding(
        ("Ctrl+D",),
        note="Picasa: Deselect photos (ed5.12). Clears the selection set; "
             "the current item keeps keyboard focus but is deselected."),
    "grid.invert": Binding(
        ("Ctrl+I",),
        note="Picasa: Invert selection (ed5.12). Flips membership for every "
             "photo in the current view — selected become unselected and vice "
             "versa; the current item stays as keyboard focus."),
    "grid.locate": Binding(
        ("Ctrl+Return", "Ctrl+Enter"),
        note="Picasa: Locate on Disk (Ctrl+Enter). Both Return and keypad "
             "Enter spellings. NEW in q6l.8."),
    "grid.open": Binding(
        ("Return", "Enter"), key_only=True,
        note="Open the CURRENT item in the viewer. key_only predates the "
             "keymap; grid.locate is matched first so Ctrl+Enter reveals."),
    "grid.clear": Binding(
        ("Esc",), key_only=True,
        note="Collapse the selection to the current item (clears all when "
             "none). key_only: Esc during leftover modifiers still clears."),
    "grid.next": Binding(
        ("Right", "J"), key_only=True,
        note="J is Picasa's viewer next, NEW in the grid (q6l.8). key_only "
             "so Shift+Right/Shift+J extend the selection (handler logic)."),
    "grid.prev": Binding(
        ("Left", "K"), key_only=True,
        note="K = prev, as grid.next."),
    "grid.row_down": Binding(
        ("Down",), key_only=True,
        note="One visual row down; Shift extends (handler logic)."),
    "grid.row_up": Binding(
        ("Up",), key_only=True,
        note="One visual row up; Shift extends (handler logic)."),
    "grid.first": Binding(
        ("Home",), key_only=True,
        note="Jump to the first photo in the current view (ed5.12). "
             "key_only so Shift+Home extends the selection from the anchor."),
    "grid.last": Binding(
        ("End",), key_only=True,
        note="Jump to the last photo in the current view (ed5.12). "
             "key_only so Shift+End extends the selection from the anchor."),

    # ---- peek overlay (handled by the grid while a peek is up) ----
    "peek.dismiss": Binding(
        ("Esc",), key_only=True,
        note="Esc dismisses the peek. key_only is load-bearing: the "
             "Ctrl+Alt trigger chord is HELD when Esc arrives (q6l.5)."),

    # ---- viewer (single photo) ----
    "viewer.close": Binding(
        ("Esc", "Backspace"), key_only=True,
        note="Picasa: Esc closes the Photo Viewer; Backspace is our alias."),
    "viewer.zoom_toggle": Binding(
        ("1", "Ctrl+Alt+0"),
        note="Picasa Photo Viewer: 1 = toggle 100% zoom. ARBITRATION: bare "
             "'1' is grandfathered on a RESERVED key and cedes to the M2 "
             "star-set (0-5); Ctrl+Alt+0 (and click) remain (PR #45)."),
    "viewer.hold": Binding(
        ("Ctrl+H",),
        note="Hold the shown photo in the tray (q6l.2)."),
    "viewer.locate": Binding(
        ("Ctrl+Return", "Ctrl+Enter"),
        note="Picasa: Locate on Disk, from the viewer too. NEW in q6l.8."),
    "viewer.faces": Binding(
        ("F",),
        note="Face-region overlay (cam.4) — our binding; Picasa has none."),
    "viewer.pan_left": Binding(
        ("Ctrl+Left",),
        note="Quarter-viewport pan, ONLY while zoomed (handler gate); "
             "plain arrows keep meaning next/prev (triage loop priority)."),
    "viewer.pan_right": Binding(("Ctrl+Right",), note="As viewer.pan_left."),
    "viewer.pan_up": Binding(("Ctrl+Up",), note="As viewer.pan_left."),
    "viewer.pan_down": Binding(("Ctrl+Down",), note="As viewer.pan_left."),
    "viewer.star_toggle": Binding(
        ("Space",), key_only=True,
        note="Picasa library shortcut: add/remove star. Fauxcasa stores "
             "the override in its own cache; originals remain read-only."),
    "viewer.star_clear": Binding(
        ("Shift+Space",),
        note="Clear the star on the shown photo (fauxcasa-q6l.20 clause "
             "c) — same chord as grid.star_clear; checked before "
             "viewer.star_toggle's key_only Space."),
    "viewer.next": Binding(
        ("Right", "J"), key_only=True,
        note="Picasa: Right/J = next. Checked AFTER pan, so Ctrl+Right "
             "pans while zoomed."),
    "viewer.prev": Binding(
        ("Left", "K"), key_only=True,
        note="Picasa: Left/K = prev."),
    "viewer.play_pause": Binding(
        ("P",),
        note="Video playback (fauxcasa-v46.3): play/pause the shown video. "
             "Our own pick — Picasa's documented video keys are / (pause) "
             "and , . (rew/ff), but Space is claimed by star_toggle (the "
             "triage loop's key, which must keep working on videos), so P "
             "carries play/pause; / , . stay unclaimed (module docstring)."),
    "viewer.seek_back": Binding(
        ("Ctrl+Left",),
        note="Skip back 5 s during video playback (v46.3). CONTEXT-LAYERED "
             "on viewer.pan_left's chord deliberately: pan fires only while "
             "zoomed and 1:1 is unsupported for video in v1, seek only while "
             "a playback session is live — mutually exclusive by handler "
             "gates, encoded in _CONTEXT_LAYERED so conflicts() stays green."),
    "viewer.seek_fwd": Binding(
        ("Ctrl+Right",),
        note="Skip forward 5 s during video playback. As viewer.seek_back "
             "(context-layered on viewer.pan_right)."),

    # ---- slideshow ----
    "slideshow.pause": Binding(
        ("Space",), key_only=True,
        note="Pause/resume the dwell (conventional; corpus documents no "
             "Picasa slideshow keys)."),
    "slideshow.next": Binding(
        ("Right", "J"), key_only=True, note="As the viewer."),
    "slideshow.prev": Binding(
        ("Left", "K"), key_only=True, note="As the viewer."),
    "slideshow.exit": Binding(
        ("Esc", "Backspace"), key_only=True, note="Exit to the grid."),
}

# Plain-English labels for every DEFAULT_SCHEME action (fauxcasa-ez2.6):
# the single source Help > Keyboard shortcuts… reads to build its runtime
# table, so the on-screen list and this module's own docs never drift
# apart the way a second, hand-maintained copy would.
ACTION_LABELS: dict[str, str] = {
    "app.play": "Start slideshow of the current view",
    "app.search": "Jump to search",
    "app.info": "Toggle info panel",
    "app.theme_toggle": "Toggle light/dark theme",
    "grid.select_all": "Select all",
    "grid.hold": "Hold selection in tray",
    "grid.star_toggle": "Add / remove star",
    "grid.star_clear": "Clear star(s) on selection",
    "grid.deselect": "Deselect",
    "grid.invert": "Invert selection",
    "grid.locate": "Locate on disk",
    "grid.open": "Open photo",
    "grid.clear": "Clear selection",
    "grid.next": "Next photo",
    "grid.prev": "Previous photo",
    "grid.row_down": "Move down one row",
    "grid.row_up": "Move up one row",
    "grid.first": "Jump to first photo",
    "grid.last": "Jump to last photo",
    "peek.dismiss": "Dismiss full-screen peek",
    "viewer.close": "Close viewer",
    "viewer.zoom_toggle": "Toggle 100% zoom",
    "viewer.hold": "Hold photo in tray",
    "viewer.locate": "Locate on disk",
    "viewer.faces": "Toggle face overlay",
    "viewer.pan_left": "Pan left (while zoomed)",
    "viewer.pan_right": "Pan right (while zoomed)",
    "viewer.pan_up": "Pan up (while zoomed)",
    "viewer.pan_down": "Pan down (while zoomed)",
    "viewer.star_toggle": "Add / remove star",
    "viewer.star_clear": "Clear star on this photo",
    "viewer.next": "Next photo",
    "viewer.prev": "Previous photo",
    "viewer.play_pause": "Play / pause video",
    "viewer.seek_back": "Skip back 5s (video)",
    "viewer.seek_fwd": "Skip forward 5s (video)",
    "slideshow.pause": "Pause / resume slideshow",
    "slideshow.next": "Next photo",
    "slideshow.prev": "Previous photo",
    "slideshow.exit": "Exit slideshow",
}

# M2 reservations (spec §5): the star-set digits and the reject key. No
# binding may claim these as a BARE key (modified chords like Ctrl+Alt+0
# are fine) — conflicts() enforces it, so a future binding colliding with
# the digit scheme fails the suite instead of shipping.
RESERVED_KEYS: dict[str, str] = {
    "0": "M2 star-set: clear stars",
    "1": "M2 star-set: set 1 star (zoom_toggle is grandfathered until then)",
    "2": "M2 star-set: set 2 stars",
    "3": "M2 star-set: set 3 stars",
    "4": "M2 star-set: set 4 stars",
    "5": "M2 star-set: set 5 stars",
    "X": "M2 reverse star / reject (Picasa's import-exclude key)",
}
# The one binding allowed to sit on a reserved key until M2 claims it.
_GRANDFATHERED: frozenset[tuple[str, str]] = frozenset(
    {("viewer.zoom_toggle", "1")})

# Deliberate same-chord pairs resolved by mutually exclusive handler gates
# (the exact-vs-key_only layering rule's sibling for two EXACT chords):
# viewer.pan_* fires only while zoomed — and 1:1 is unsupported for video
# in v1 — while viewer.seek_* fires only while a video playback session is
# live (fauxcasa-v46.3), so the same Ctrl+arrow can never reach both.
# conflicts() skips exactly these pairs; any OTHER exact duplicate still
# fails the suite.
_CONTEXT_LAYERED: frozenset[frozenset[str]] = frozenset({
    frozenset({"viewer.pan_left", "viewer.seek_back"}),
    frozenset({"viewer.pan_right", "viewer.seek_fwd"}),
})

# Picasa chords Fauxcasa KNOWS but has not implemented (fauxcasa-s6i).
# Pressing one must never be a silent nothing: the owning surface's
# keyPressEvent falls through to planned(event, scope) and the window
# shows the notice in the status bar, so the user learns the feature is
# missing (and roughly when it is due) instead of wondering whether the
# key worked. Keyed by SCOPE (the surface, as in DEFAULT_SCHEME's
# "<scope>.<action>"), because the honest wording differs per surface:
# ",": "rewind video" is true in the viewer and nonsense in the grid, and
# PgUp/PgDown page-scroll the grid through Qt's base handler (a notice
# there would EAT a working key — reviewer finding, PR for s6i). Each
# entry: (chord aliases, (feature label, availability note)); aliases are
# QKeySequence portable strings, listed because exact matching keeps
# Shift and the platform decides the key: main-row '+' arrives as
# "Shift+=" on Windows/macOS (the '=/+' key is Key_Equal there) but as
# "Shift++" on X11 (shifted XK_plus), while keypad '+' (keypad modifier
# stripped) arrives as "+".
#
# Not a scheme: a live binding always wins (planned() runs after every
# matches() call), so a chord here that the same scope also binds — via
# DEFAULT_SCHEME, a StandardKey, a key_only bare key under ANY modifier,
# or a window-level QAction — would be dead text.
# test_keymap_planned_keys_never_shadow_live_bindings fails on any such
# overlap; that is why bare '1' is grid-only (viewer.zoom_toggle owns it
# there), '/' is absent (app.search) and Ctrl+D is absent (grid.deselect).
_M2_STARS = "planned for M2 (star-set keys 0-5)"
_M3 = "planned for M3 (edit room)"
_WRITES = "planned for M2 (library writes)"
_EVERYWHERE: dict[tuple[str, ...], tuple[str, str]] = {
    ("Ctrl+3",): ("Edit mode", _M3),
    ("Ctrl+R",): ("Rotate clockwise", _M3),
    ("Ctrl+Shift+R",): ("Rotate counter-clockwise", _M3),
    ("Ctrl+8",): ("Add / remove star (Picasa's Ctrl+8)",
                  "use Space for now; Ctrl+8 lands with M2 star machinery"),
    ("0",): ("Clear stars", _M2_STARS),
    ("2",): ("Set 2 stars", _M2_STARS),
    ("3",): ("Set 3 stars", _M2_STARS),
    ("4",): ("Set 4 stars", _M2_STARS),
    ("5",): ("Set 5 stars", _M2_STARS),
    ("X",): ("Reject / reverse star", "planned for M2"),
    ("Ctrl+O",): ("Add folder to library", "planned for a later milestone"),
    ("Ctrl+M",): ("Move to new folder", _WRITES),
    ("Ctrl+N",): ("New album", _WRITES),
    ("Ctrl+T",): ("Add tag", _WRITES),
    ("Ctrl+E",): ("Export", "planned for a later milestone"),
    ("Ctrl+P",): ("Print", "planned for a later milestone"),
}
PLANNED_KEYS: dict[str, dict[tuple[str, ...], tuple[str, str]]] = {
    "grid": {
        **_EVERYWHERE,
        ("1",): ("Set 1 star", _M2_STARS),
        ("Ctrl+1",): ("Small thumbnails",
                      "use the thumbnail zoom slider instead"),
        ("Ctrl+2",): ("Large thumbnails",
                      "use the thumbnail zoom slider instead"),
    },
    "viewer": {
        **_EVERYWHERE,
        ("8",): ("Toggle star (Picasa viewer's 8)",
                 "use Space for now; 8 lands with M2 star machinery"),
        ("PgUp", "+", "Shift++", "Shift+="): ("Zoom in one step",
                                              "not yet available; 1 toggles 100%"),
        ("PgDown", "-"): ("Zoom out one step",
                          "not yet available; 1 toggles 100%"),
        (",",): ("Rewind video", "not yet available; Ctrl+Left skips back 5 s"),
        (".",): ("Fast-forward video",
                 "not yet available; Ctrl+Right skips forward 5 s"),
    },
}

# Features with no key at all yet, surfaced by the shipped action whose
# no-op path the user would otherwise hit: label -> availability note.
# Kept beside PLANNED_KEYS so Help > Keyboard Shortcuts lists both under
# one "Not yet available" heading. Callers index by the named constant,
# never by position.
FACE_TAGGING = "Name a face / manual face tagging"
PLANNED_FEATURES: dict[str, str] = {
    FACE_TAGGING:
        "planned for M4 (people registry); today F only shows the boxes "
        "Picasa already drew",
}


def notice(label: str, note: str) -> str:
    """The one status-bar wording for a not-yet-implemented feature."""
    return f"{label} is not implemented yet — {note}."


# Modifiers stripped before exact matching: keypad Enter/digits count as
# their main-row keys (the pre-keymap handlers accepted KeypadModifier).
_IGNORED_MODS = Qt.KeyboardModifier.KeypadModifier


def _compiled(binding: Binding) -> tuple[QKeySequence, ...]:
    """The binding's chords as QKeySequence objects — the StandardKey's
    platform binding list when one is set, plus the portable strings."""
    seqs = []
    if binding.standard is not None:
        seqs.extend(QKeySequence.keyBindings(binding.standard))
    seqs.extend(QKeySequence(c) for c in binding.chords)
    return tuple(seqs)


def _bare_keys(binding: Binding) -> tuple[Qt.Key, ...]:
    """The Qt.Key of each UNMODIFIED single-chord sequence (for key_only
    matching and reserved-key checks). A modified chord (Ctrl+Alt+0) is
    not bare: it neither matches key_only nor trips a key reservation."""
    return tuple(
        seq[0].key() for seq in _compiled(binding)
        if seq.count() == 1
        and seq[0].keyboardModifiers() == Qt.KeyboardModifier.NoModifier)


def _event_sequence(event) -> QKeySequence:
    mods = event.modifiers() & ~_IGNORED_MODS
    return QKeySequence(QKeyCombination(mods, Qt.Key(event.key())))


def matches(event, action: str, scheme: dict[str, Binding] | None = None,
            ) -> bool:
    """True when the QKeyEvent is this action's binding. StandardKey
    entries defer to QKeyEvent.matches (platform lists); key_only
    bindings compare the bare key ignoring modifiers; everything else is
    exact chord equality (keypad modifier stripped)."""
    b = (scheme or DEFAULT_SCHEME)[action]
    if b.standard is not None and event.matches(b.standard):
        return True
    if b.key_only:
        return event.key() in {int(k) for k in _bare_keys(b)}
    seq = _event_sequence(event)
    return any(seq == c for c in _compiled(b))


# scope -> [(compiled chord, notice text)], built once (mirrors the
# _compiled() caching style; a keypress must not re-parse the table).
_PLANNED_COMPILED: dict[str, list[tuple[QKeySequence, str]]] = {
    scope: [(QKeySequence(chord), notice(label, note))
            for chords, (label, note) in table.items()
            for chord in chords]
    for scope, table in PLANNED_KEYS.items()
}


def planned(event, scope: str) -> str | None:
    """The status-bar notice for a PLANNED_KEYS[scope] chord, or None
    when the event is not one. Exact chord equality, keypad stripped,
    same as matches() — callers put this LAST in keyPressEvent, after
    every live binding, so a shipped action on the same key can never be
    shadowed."""
    seq = _event_sequence(event)
    for compiled, text in _PLANNED_COMPILED[scope]:
        if seq == compiled:
            return text
    return None


def shortcuts(action: str) -> list[QKeySequence]:
    """The action's chords for QAction.setShortcuts."""
    return list(_compiled(DEFAULT_SCHEME[action]))


def conflicts(scheme: dict[str, Binding] | None = None,
              ) -> list[tuple[str, str, str]]:
    """Integrity check over one scheme: (action_a, action_b, why) for
    every collision. Within one scope (plus "app.*", which is global and
    joins every scope):

    * two exact chords that compare equal collide;
    * two key_only bindings sharing a bare key collide;
    * an exact chord vs a key_only binding on the same key is ALLOWED —
      that is deliberate layering, resolved by handler order (e.g.
      viewer.pan_left Ctrl+Left before viewer.prev's key_only Left);
    * an exact-chord pair listed in _CONTEXT_LAYERED is ALLOWED — the
      same deliberate layering between two exact chords whose handler
      gates are mutually exclusive (pan-while-zoomed vs seek-while-
      video-plays);
    * any binding claiming a RESERVED_KEYS bare key collides with the
      reservation, except the _GRANDFATHERED tenant.

    This is the check that would have caught bare '1' (zoom) colliding
    with a future digit binding: a second bare-digit binding fails both
    the duplicate-chord rule and the reservation rule.
    """
    scheme = scheme if scheme is not None else DEFAULT_SCHEME
    found: list[tuple[str, str, str]] = []
    scopes = {a.split(".", 1)[0] for a in scheme}

    def in_scope(action: str, scope: str) -> bool:
        s = action.split(".", 1)[0]
        return s == scope or s == "app"

    reserved = {int(QKeySequence(k)[0].key()): k for k in RESERVED_KEYS}
    for action, b in scheme.items():
        for key in _bare_keys(b):
            name = reserved.get(int(key))
            if name is not None and (action, name) not in _GRANDFATHERED:
                found.append((action, "RESERVED",
                              f"{name!r} is reserved: {RESERVED_KEYS[name]}"))

    for scope in sorted(scopes - {"app"}) + (["app"] if "app" in scopes
                                             else []):
        actions = [a for a in scheme if in_scope(a, scope)]
        for i, a in enumerate(actions):
            for other in actions[i + 1:]:
                ba, bo = scheme[a], scheme[other]
                if a.startswith("app.") and other.startswith("app."):
                    if scope != "app":   # report the app/app pair once
                        continue
                if ba.key_only and bo.key_only:
                    shared = {int(k) for k in _bare_keys(ba)} \
                        & {int(k) for k in _bare_keys(bo)}
                    for k in sorted(shared):
                        found.append((a, other,
                                      f"both bind key {QKeySequence(k).toString()}"
                                      f" (key_only) in scope {scope!r}"))
                elif ba.key_only == bo.key_only:  # both exact
                    if frozenset({a, other}) in _CONTEXT_LAYERED:
                        continue  # deliberate gate-resolved layering
                    for sa in _compiled(ba):
                        for so in _compiled(bo):
                            if sa == so:
                                found.append(
                                    (a, other,
                                     f"both bind {sa.toString()} in scope "
                                     f"{scope!r}"))
    return found
