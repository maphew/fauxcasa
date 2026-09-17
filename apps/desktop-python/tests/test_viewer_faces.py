"""Tests for viewer.py faces overlay and keymap.

Split from test_tracer.py (fauxcasa-l09); originally lines 11523-12060 of the monolith."""

from __future__ import annotations

from pathlib import Path
import pytest
from catalog import (
    scan_library,
)
from tracer_helpers import (
    _FACE_STORED_RECT,
    _exif_orientation_app1,
    _face_viewer,
    _inject,
    _jpeg_bytes,
    _marked_stored_image,
    _offscreen_app,
    _press,
    _qt_display_transform,
    make_jpeg,
)


# ---------------------------------------------------------------------------
# Face-region overlay (fauxcasa-cam.4): faces= rect64 fractions are relative
# to the STORED pixels (picasa-ini-format.md "faces=": rotate= does NOT
# transform them; EXIF orientation handling is the consumer's job), while
# every display path shows EXIF-upright + rotate= composed — so the overlay
# maps stored-frame rects through that SAME composed transform, then through
# the live _shown_rect (fit / panned 1:1). Verification strategy: the 8x4
# orientation x rotate matrix is checked against Qt's OWN pixel transforms
# (mirrored()/rotate() on a marked synthetic image — an independent
# reference, not a re-derivation of the mapping algebra), and end-to-end
# cases go through REAL EXIF Orientation bytes + the actual
# load_original_oriented decode. Orientation is read at VIEW time from the
# original's bytes (metareader.read_orientation, the exiv2 seam) — no
# catalog schema change. Fixtures are synthetic (privacy rule).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("orientation", range(1, 9))
@pytest.mark.parametrize("rotate", range(4))
def test_face_map_matrix_orientation_x_rotate(orientation: int,
                                              rotate: int) -> None:
    """All 32 EXIF-orientation x rotate= compositions: the mapped rect's
    center must land ON the red patch in the actually-transformed image,
    and a probe just past each mapped edge must land OFF it — pinning all
    four edges against Qt's own pixel transforms."""
    _offscreen_app()
    from PySide6.QtGui import QTransform

    from viewer import map_face_fraction

    disp = _qt_display_transform(_marked_stored_image(), orientation)
    if rotate:
        disp = disp.transformed(QTransform().rotate(90 * rotate))
    left, top, right, bottom = map_face_fraction(
        _FACE_STORED_RECT, orientation, rotate)
    assert 0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0
    w, h = disp.width(), disp.height()
    cx, cy = (left + right) / 2 * w, (top + bottom) / 2 * h

    def red(x: float, y: float) -> bool:
        c = disp.pixelColor(int(x), int(y))
        return c.red() > 180 and c.green() < 80 and c.blue() < 80

    assert red(cx, cy)                       # center ON the patch
    pad = 4                                  # min patch-to-edge margin is 16
    assert not red(left * w - pad, cy)       # each mapped edge is pinned:
    assert not red(right * w + pad, cy)      # just outside must be OFF
    assert not red(cx, top * h - pad)
    assert not red(cx, bottom * h + pad)


def test_face_overlay_end_to_end_exif_bytes(tmp_path: Path) -> None:
    """Full-pipeline probes with REAL EXIF Orientation bytes: exiv2 writes
    tag 274, load_original_oriented decodes (autoTransform + rotate=,
    bytes read once) and reports the stored value, and face_widget_rect
    at the image's own 1:1 rect lands on the marked patch — confirming
    Qt's autoTransform composition IS the transform the pure math models,
    on real files. Cases cover a plain turn, a mirror, and a mirror-turn
    composed with rotate=."""
    _offscreen_app()
    from PySide6.QtCore import QBuffer, QIODevice, QRect

    import metareader
    from viewer import face_widget_rect, load_original_oriented

    for orientation, rotate in ((6, 0), (2, 0), (5, 1)):
        img = _marked_stored_image()
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        assert img.save(buf, "JPEG", 95)
        data = metareader.embed_test_metadata(
            bytes(buf.data()), orientation=orientation)
        p = tmp_path / f"o{orientation}r{rotate}.jpg"
        p.write_bytes(data)
        shown, got = load_original_oriented(str(p), rotate)
        assert not shown.isNull() and got == orientation
        wr = face_widget_rect(_FACE_STORED_RECT, got, rotate,
                              QRect(0, 0, shown.width(), shown.height()))
        c = shown.pixelColor(int(wr.center().x()), int(wr.center().y()))
        assert c.red() > 150 and c.green() < 100 and c.blue() < 100


def test_metareader_read_orientation() -> None:
    """read_orientation: each stored value 1..8 round-trips from real EXIF
    bytes; an absent tag, out-of-range values, and garbage/empty bytes all
    fail soft to 1 (never an exception) — the wrong-but-bounded contract a
    paint path needs."""
    import metareader

    base = _jpeg_bytes()
    assert metareader.read_orientation(base) == 1        # no tag at all
    for o in range(1, 9):
        assert metareader.read_orientation(
            metareader.embed_test_metadata(base, orientation=o)) == o
    for bad in (0, 9):
        assert metareader.read_orientation(
            metareader.embed_test_metadata(base, orientation=bad)) == 1
    assert metareader.read_orientation(b"") == 1
    assert metareader.read_orientation(b"\xff\xd8 not really a jpeg") == 1


# ---------------------------------------------------------------------------
# fauxcasa-5dk: a proven GIL <-> Qt-mutex lock inversion (live py-spy native
# dump) made QImageReader-over-a-Python-QBuffer unsafe for decode, so
# inmeta.apply_orientation now applies EXIF orientation manually after a
# QImage.fromData decode (viewer.load_original_oriented, rawload.
# load_raw_qimage) instead of via QImageReader.setAutoTransform. These tests
# are the regression net: apply_orientation must keep producing the exact
# same pixels the old setAutoTransform path did.
# ---------------------------------------------------------------------------


def test_apply_orientation_pixel_transforms() -> None:
    """Unit-level check of all 8 EXIF Orientation values on a small,
    doubly-asymmetric (both axes) 2x3 image with a distinct color in each
    corner. Cross-checked against _qt_display_transform — the independent
    Qt-transform reference already used to validate the face-overlay math
    (map_face_fraction) — for a full pixel-exact image comparison at every
    orientation, plus explicit corner-pixel and dimension-swap assertions
    per orientation."""
    _offscreen_app()
    from PySide6.QtGui import QColor, QImage

    from inmeta import apply_orientation

    w, h = 2, 3
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(10, 10, 10))
    tl, tr = QColor(255, 0, 0), QColor(0, 255, 0)
    bl, br = QColor(0, 0, 255), QColor(255, 255, 0)
    img.setPixelColor(0, 0, tl)
    img.setPixelColor(w - 1, 0, tr)
    img.setPixelColor(0, h - 1, bl)
    img.setPixelColor(w - 1, h - 1, br)

    fmt = QImage.Format.Format_ARGB32  # rotate()/transformed() promote to
    # this (or its premultiplied twin) while mirrored() keeps RGB32 — settle
    # on one common format so `==` compares pixels, not incidental storage.
    for orientation in range(1, 9):
        expected = _qt_display_transform(img, orientation)
        got = apply_orientation(img, orientation)
        assert (got.width(), got.height()) == \
            (expected.width(), expected.height())
        assert got.convertToFormat(fmt) == expected.convertToFormat(fmt)

    assert (apply_orientation(img, 1).width(),
            apply_orientation(img, 1).height()) == (w, h)

    o2 = apply_orientation(img, 2)         # mirror-H: left/right swap
    assert o2.pixelColor(0, 0) == tr and o2.pixelColor(w - 1, 0) == tl
    assert o2.pixelColor(0, h - 1) == br and o2.pixelColor(w - 1, h - 1) == bl

    o3 = apply_orientation(img, 3)         # 180: diagonal swap
    assert o3.pixelColor(0, 0) == br and o3.pixelColor(w - 1, h - 1) == tl
    assert o3.pixelColor(w - 1, 0) == bl and o3.pixelColor(0, h - 1) == tr

    o4 = apply_orientation(img, 4)         # mirror-V: top/bottom swap
    assert o4.pixelColor(0, 0) == bl and o4.pixelColor(0, h - 1) == tl
    assert o4.pixelColor(w - 1, 0) == br and o4.pixelColor(w - 1, h - 1) == tr

    o6 = apply_orientation(img, 6)         # rotate 90 CW: dims swap
    assert (o6.width(), o6.height()) == (h, w)
    o8 = apply_orientation(img, 8)         # rotate 90 CCW: dims swap
    assert (o8.width(), o8.height()) == (h, w)

    o5 = apply_orientation(img, 5)         # transpose (main diagonal)
    assert (o5.width(), o5.height()) == (h, w)
    assert o5.pixelColor(0, 0) == tl       # the main-diagonal fixed corner

    o7 = apply_orientation(img, 7)         # transverse (anti-diagonal)
    assert (o7.width(), o7.height()) == (h, w)
    assert o7.pixelColor(0, 0) == br       # the anti-diagonal fixed corner


def test_load_original_oriented_matches_qimagereader_autotransform(
        tmp_path: Path) -> None:
    """load_original_oriented's new QImage.fromData + apply_orientation
    decode must still produce pixel-identical output to the OLD
    QBuffer + QImageReader.setAutoTransform decode, for every EXIF
    Orientation value — built here as a reference, on the main thread
    (safe; the deadlock is a worker-thread/main-thread race, fauxcasa-5dk).
    The fixture is _marked_stored_image() (distinct content in each
    corner), NOT a solid color: a solid fixture would let this pass even
    if a mirror axis or a rotation direction were swapped, since every
    pixel is identical either way — the marked corners are what actually
    pin the pixel ARRANGEMENT, not just the dimensions."""
    _offscreen_app()
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage, QImageReader

    from viewer import load_original_oriented

    fmt = QImage.Format.Format_ARGB32  # settle rotate()-vs-untouched format
    # differences (premultiplied ARGB32 vs RGB32) before comparing pixels.
    for orientation in range(1, 9):
        img = _marked_stored_image()
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        assert img.save(buf, "JPEG", 95)
        data = _inject(bytes(buf.data()), 0xE1,
                       _exif_orientation_app1(orientation))
        p = tmp_path / f"o{orientation}.jpg"
        p.write_bytes(data)

        ref_buf = QBuffer()
        ref_buf.setData(data)
        ref_buf.open(QIODevice.OpenModeFlag.ReadOnly)
        ref_reader = QImageReader(ref_buf)
        ref_reader.setAutoTransform(True)
        reference = ref_reader.read()
        assert not reference.isNull()

        shown, got = load_original_oriented(str(p), 0)
        assert not shown.isNull() and got == orientation
        assert shown.convertToFormat(fmt) == reference.convertToFormat(fmt)


def test_viewer_face_toggle_only_with_faces(tmp_path: Path) -> None:
    """F toggles the overlay on a face-bearing photo — rects for BOTH tags,
    the named one carrying its name and the unconfirmed one None (dashed +
    "Unnamed" in paint) — and the overlaid paint is clean offscreen. On a
    photo with NO faces the same key is a no-op and no rects are produced
    (no dead mode switch); the toggle itself is session-sticky across
    navigation."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    v, _ = _face_viewer(tmp_path)
    assert not v.faces_visible and v._face_rects() == []
    _press(v, Qt.Key.Key_F)
    assert v.faces_visible
    rects = v._face_rects()
    assert len(rects) == 2
    assert {name for _r, name in rects} == {"Pat Named", None}
    assert not v.grab().isNull()         # overlay paint is clean (offscreen)
    _press(v, Qt.Key.Key_F)
    assert not v.faces_visible and v._face_rects() == []
    _press(v, Qt.Key.Key_F)              # back on, then navigate away
    v.show_photo([0, 1], 1)              # the no-faces photo
    v._serial += 1
    orig = QImage(800, 600, QImage.Format.Format_RGB32)
    orig.fill(0x111111)
    v._on_loaded(v._serial, orig, 1)
    assert v.faces_visible               # sticky across navigation...
    assert v._face_rects() == []         # ...but nothing to draw here
    got: list[str] = []
    v.notice.connect(got.append)
    _press(v, Qt.Key.Key_F)
    assert v.faces_visible               # F on a faceless photo: state kept
    # ...but never silent (fauxcasa-s6i): the user hears there are no
    # Picasa tags here and that naming faces is still to come.
    assert len(got) == 1
    assert "no picasa face tags" in got[0].lower()
    assert "not implemented yet" in got[0].lower()
    assert "M4" in got[0]


def test_planned_keys_notice_instead_of_silence(tmp_path: Path) -> None:
    """A Picasa chord Fauxcasa knows but has not built (keymap.PLANNED_KEYS)
    raises a notice from BOTH surfaces, and the window shows it in the
    status bar (fauxcasa-s6i) — never a silent nothing. Live bindings are
    untouched: the same press path still runs them first."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    import keymap

    def chord(widget, key, mods=Qt.KeyboardModifier.NoModifier):
        widget.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods))

    _offscreen_app()
    import main
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "b.jpg")
    win = main.MainWindow(scan_library(root), None,
                          cache_dir=None, build_dir=None)
    ctrl = Qt.KeyboardModifier.ControlModifier
    # grid: Ctrl+3 (Picasa edit mode) -> notice names the feature + M3
    win.grid.setFocus()
    chord(win.grid, Qt.Key.Key_3, ctrl)
    msg = win.statusBar().currentMessage()
    assert "edit mode" in msg.lower() and "M3" in msg, msg
    # grid: bare digit (M2 star-set) — the grid has no '1' binding, so
    # the notice fires there; X (reject) likewise
    chord(win.grid, Qt.Key.Key_1)
    assert "1 star" in win.statusBar().currentMessage()
    chord(win.grid, Qt.Key.Key_X)
    assert "reject" in win.statusBar().currentMessage().lower()
    # viewer: Ctrl+R (rotate) -> notice; '1' is the live zoom toggle
    # there and must keep winning over the planned star-set entry
    win.viewer.show_photo([0, 1], 0)
    got: list[str] = []
    win.viewer.notice.connect(got.append)
    chord(win.viewer, Qt.Key.Key_R, ctrl)
    assert got and "rotate" in got[-1].lower() and "M3" in got[-1]
    assert "rotate" in win.statusBar().currentMessage().lower()
    before = len(got)
    chord(win.viewer, Qt.Key.Key_1)
    assert len(got) == before            # live binding, no notice
    # an unknown key still falls through quietly (no notice spam)
    chord(win.viewer, Qt.Key.Key_Q)
    assert len(got) == before
    # main-row '+' keeps its Shift (exact matching) and the platform
    # picks the key: Key_Equal on Windows/macOS ("Shift+="), Key_Plus on
    # X11 ("Shift++"). Both spellings must reach the viewer's zoom-step
    # notice — Picasa's "+/- (not the numeric keypad)"; the grid says
    # nothing about video keys
    shift = Qt.KeyboardModifier.ShiftModifier
    chord(win.viewer, Qt.Key.Key_Plus, shift)
    assert "zoom in" in got[-1].lower()
    before = len(got)
    chord(win.viewer, Qt.Key.Key_Equal, shift)
    assert len(got) == before + 1 and "zoom in" in got[-1].lower(), got[-1:]
    gg: list[str] = []
    win.grid.notice.connect(gg.append)
    chord(win.grid, Qt.Key.Key_Comma)
    assert gg == []
    # PgDown in the grid still reaches QAbstractScrollArea's paging (a
    # notice there would eat a working key): no notice
    chord(win.grid, Qt.Key.Key_PageDown)
    assert gg == []
    # the one status-bar wording
    assert keymap.notice("Edit mode", "planned for M3 (edit room)") == \
        "Edit mode is not implemented yet — planned for M3 (edit room)."


def test_keymap_planned_keys_never_shadow_live_bindings(tmp_path: Path) -> None:
    """PLANNED_KEYS[scope] is consulted only after every live binding on
    that surface, so a chord the surface ALSO binds would be dead text.
    Live means: DEFAULT_SCHEME exact chords (StandardKey ones compiled
    through Qt, e.g. Ctrl+A), key_only bare keys under ANY modifier, and
    every window-level QAction shortcut (those intercept BEFORE the
    widget's keyPressEvent). Any overlap means a notice that can never
    show — fail here, not in the field."""
    from PySide6.QtGui import QAction, QKeySequence

    import keymap
    import main

    _offscreen_app()
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    win = main.MainWindow(scan_library(root), None,
                          cache_dir=None, build_dir=None)
    window_chords = {sc.toString() for a in win.findChildren(QAction)
                     for sc in a.shortcuts() if not sc.isEmpty()}
    for scope, table in keymap.PLANNED_KEYS.items():
        exact: set[str] = set()
        bare: set[int] = set()
        for action, b in keymap.DEFAULT_SCHEME.items():
            if action.split(".", 1)[0] not in (scope, "app"):
                continue
            if b.key_only:
                bare |= {int(k) for k in keymap._bare_keys(b)}
            else:
                exact |= {c.toString() for c in keymap._compiled(b)}
        for chords in table:
            assert len(chords) >= 1
            for c in chords:
                seq = QKeySequence(c)
                assert seq.count() == 1, (scope, c)   # a typo never matches
                assert seq.toString() not in exact, (scope, c)
                assert seq.toString() not in window_chords, (scope, c)
                assert int(seq[0].key()) not in bare, (scope, c)


def test_viewer_face_rects_wait_for_original(tmp_path: Path) -> None:
    """The overlay waits for the ORIGINAL: the stored orientation rides in
    with the decode, so while only the preview stand-in is up _face_rects
    is empty (a mis-mapped box is worse than a fraction-of-a-second wait),
    and the orientation delivered with the image is adopted and applied to
    the mapping."""
    _offscreen_app()
    from PySide6.QtGui import QImage

    from viewer import ViewerPage, face_widget_rect

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\nfaces=rect64(4a8e8e6b),ffffffffffffffff\r\n")
    cat = scan_library(root)
    v = ViewerPage(cat, None)
    v.resize(1280, 800)
    v.show_photo([0], 0)
    v._serial += 1                       # keep the async decode out of it
    v.faces_visible = True
    assert v.image is None
    assert v._face_rects() == []         # orientation unknown: no boxes yet
    orig = QImage(1600, 2400, QImage.Format.Format_RGB32)
    orig.fill(0x445566)
    v._on_loaded(v._serial, orig, 6)     # a 90-CW-stored original lands
    assert v._orientation == 6
    (rect, _name), = v._face_rects()
    shown = v._shown_rect(1280, 800, orig)
    assert rect == face_widget_rect(cat.photos[0].faces[0][0], 6, 0, shown)
    # the doc's worked example: a stored top-LEFT face reads top-RIGHT
    # once a 90-CW-stored (orientation 6) photo is displayed upright
    assert rect.right() == pytest.approx(shown.x() + shown.width())
    assert rect.top() == pytest.approx(shown.y())


def test_viewer_face_rects_track_zoom_and_pan(tmp_path: Path,
                                              monkeypatch) -> None:
    """The widget-space face rect is exactly face_widget_rect over the LIVE
    _shown_rect: at fit, at 1:1 (the box scales with the zoom), and after a
    pan the box moves by exactly the shown-rect delta — the overlay never
    drifts off its image pixels."""
    from viewer import face_widget_rect

    v, orig = _face_viewer(tmp_path)
    monkeypatch.setattr(v, "devicePixelRatioF", lambda: 1.0)
    v.faces_visible = True
    stored = v.catalog.photos[0].faces[0][0]
    fit = v._shown_rect(1280, 800, orig)
    at_fit = v._face_rects()[0][0]
    assert at_fit == face_widget_rect(stored, 1, 0, fit)
    v.toggle_zoom()
    z1 = v._shown_rect(1280, 800, orig)
    r1 = v._face_rects()[0][0]
    assert r1 == face_widget_rect(stored, 1, 0, z1)
    assert r1.width() > at_fit.width()          # the box scales with zoom
    v._pan_by(-120, -80)
    z2 = v._shown_rect(1280, 800, orig)
    r2 = v._face_rects()[0][0]
    assert (z2.x() - z1.x(), z2.y() - z1.y()) == (-120, -80)
    assert r2.x() - r1.x() == pytest.approx(-120)
    assert r2.y() - r1.y() == pytest.approx(-80)


def test_face_overlay_hidden_on_peek_and_slideshow(tmp_path: Path) -> None:
    """Peek and slideshow are glance surfaces: face_overlay_allowed is
    False there, toggle_faces cannot enable it, and even a forced
    faces_visible produces no rects — the overlay is viewer-only by
    policy (viewer.py module doc). No show_photo here on purpose: state
    is set directly so no decode worker ever spawns for a policy test."""
    _offscreen_app()
    from PySide6.QtGui import QImage

    from peek import PeekPage
    from slideshow import SlideshowPage

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\nfaces=rect64(4a8e8e6b),ffffffffffffffff\r\n")
    cat = scan_library(root)
    orig = QImage(400, 300, QImage.Format.Format_RGB32)
    orig.fill(0x222222)
    for cls in (PeekPage, SlideshowPage):
        s = cls(cat)
        s.resize(640, 480)
        s.display, s.pos = [0], 0
        s.image = orig
        assert not s.face_overlay_allowed
        s.toggle_faces()
        assert not s.faces_visible           # F could never switch it on
        s.faces_visible = True               # even forced...
        assert s._face_rects() == []         # ...the paint gate holds
