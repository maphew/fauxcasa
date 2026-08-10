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
    / , .                 Video pause/rew/ff         v46.3 (playback is gated)

Fauxcasa-only bindings (no Picasa equivalent):

    F11                   app.play — slideshow of the current view (q6l.3)
    F                     viewer.faces — face-region overlay (cam.4; Picasa
                                         documents no view-mode key for boxes)
    Ctrl+Alt+0            viewer.zoom_toggle — conflict-free 100% spelling
    Space                 slideshow.pause (playback context only)

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
    "viewer.next": Binding(
        ("Right", "J"), key_only=True,
        note="Picasa: Right/J = next. Checked AFTER pan, so Ctrl+Right "
             "pans while zoomed."),
    "viewer.prev": Binding(
        ("Left", "K"), key_only=True,
        note="Picasa: Left/K = prev."),

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
                    for sa in _compiled(ba):
                        for so in _compiled(bo):
                            if sa == so:
                                found.append(
                                    (a, other,
                                     f"both bind {sa.toString()} in scope "
                                     f"{scope!r}"))
    return found
