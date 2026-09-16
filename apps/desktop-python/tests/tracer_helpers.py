"""Shared helper pool for apps/desktop-python/tests/.

Every non-fixture top-level helper function, class, and module-level
constant from the pre-split test_tracer.py (fauxcasa-l09), in original
order (helpers reference each other and the product imports by name).
Fixtures live in conftest.py, not here."""

from __future__ import annotations

import json
import os
import re
import struct
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
REPO = APP_DIR.parents[1]

sys.path.insert(0, str(APP_DIR))

import inmeta
import library as libmod  # alias avoids shadowing the `library` Path fixture (conftest.py)
import thumbcache
import volumes as volmod
from catalog import (
    ScanFilter,
    load_catalog,
    reconcile_walk,
    save_catalog,
    save_catalog_retrying,
    scan_library,
    walk_library,
    BACKFILL_COMPLETE,
    BACKFILL_IN_PROGRESS,
    BACKFILL_NOT_STARTED,
)
import metareader


def _sweep_qt_widgets(app) -> None:
    """Retire GridView/ViewerPage workers, then delete every top-level Qt
    widget and flush the deferred deletion. Factored out of
    ``_isolate_qt_per_test`` (fauxcasa-xf2) so that fixture teardown and
    ``test_ready_poll_timer_dies_with_the_window`` — which needs this SAME
    sweep to run mid-test, not just at fixture teardown — share one
    implementation and cannot drift apart.

    1. Each GridView starts 4 daemon decode threads that block forever on
       jobs.get(). stop() retires them — and must run BEFORE widget deletion,
       so a worker can never emit tile_ready into a half-deleted notifier.
    2. QWidgets created in a test are never destroyed; they pile up as live
       Qt objects. Delete every top-level widget and flush the deferred
       deletions so the widget tree is actually clean afterward — the way
       the suite behaved before the loupe tests added this much widget
       churn.
    3. Same discipline for ViewerPage (and its SlideshowPage subclass):
       quiesce() ages out and joins any in-flight original-decode /
       prefetch thread. The LAST navigation's loader still holds a VALID
       serial when the sweep runs, so without this it can emit into the
       widget deletion below — the same gfz access-violation family, seen
       on Windows once the slideshow tests added rapid-navigation churn."""
    from PySide6.QtCore import QEvent
    from grid import GridView
    from viewer import ViewerPage

    for w in app.allWidgets():
        if isinstance(w, GridView):
            w.stop()                       # retire pools before any deletion
        elif isinstance(w, ViewerPage):
            w.quiesce()                    # reap decode/prefetch workers
    app.processEvents()                    # drain queued tile_ready -> update()
    for w in app.topLevelWidgets():
        w.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # actually free them
    app.processEvents()


def _raw_catalog(path: Path) -> dict:
    """Read a save_catalog-produced file back into its raw dict form for
    tests that inspect the serialized shape directly (fauxcasa-ed5.5: the
    file is zstd-compressed from CATALOG_VERSION 13 on, so a plain
    path.read_text()/json.loads() no longer works on it)."""
    import zstandard

    raw = path.read_bytes()
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        raw = zstandard.ZstdDecompressor().decompress(raw)
    return json.loads(raw)


def _write_raw_catalog(path: Path, data: dict) -> None:
    """The inverse of _raw_catalog: write a (possibly hand-mutated) raw
    catalog dict back out as a v13 zstd-compressed file, so a test that
    pokes at the parsed structure still exercises load_catalog's real
    on-disk format."""
    import zstandard

    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    path.write_bytes(zstandard.ZstdCompressor(level=3).compress(payload))


def _raw_photo_rows(data: dict) -> list[dict]:
    """Flatten a raw catalog dict's "photos" into the pre-ed5.5 flat-row
    shape ("r" rel path, hex "x", absent-means-roots[0] "R") that many
    tests are written against — transparently ungrouping the v13
    folder-grouped shape when present, and passing an already-flat legacy
    "photos" (a hand-built v11 fixture) through untouched."""
    import catalog as catmod

    roots_hdr = data.get("roots")
    first_root_id = roots_hdr[0]["id"] if roots_hdr else ""
    photos = data.get("photos", [])
    if photos and isinstance(photos[0], dict) and "ph" in photos[0]:
        return catmod._ungroup_photo_rows(photos, first_root_id)
    return photos


def make_jpeg(path: Path, w: int = 64, h: int = 48) -> None:
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(120, 160, 200))
    path.parent.mkdir(parents=True, exist_ok=True)
    assert img.save(str(path), "JPEG", 85)


def _jpeg_bytes(w: int = 64, h: int = 48) -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(120, 160, 200))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    assert img.save(buf, "JPEG", 90)
    return bytes(buf.data())


def _inject(jpeg: bytes, marker: int, payload: bytes) -> bytes:
    """Splice an APPn marker segment in right after SOI (FFD8)."""
    assert jpeg[:2] == b"\xff\xd8"
    seg = bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload
    return jpeg[:2] + seg + jpeg[2:]


def _xmp_app1(caption: str | None = None, keywords: tuple[str, ...] = ()) -> bytes:
    body = ""
    if caption is not None:
        body += ('<dc:description><rdf:Alt>'
                 f'<rdf:li xml:lang="x-default">{caption}</rdf:li>'
                 '</rdf:Alt></dc:description>')
    if keywords:
        lis = "".join(f"<rdf:li>{k}</rdf:li>" for k in keywords)
        body += f"<dc:subject><rdf:Bag>{lis}</rdf:Bag></dc:subject>"
    xml = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<rdf:Description rdf:about="">{body}</rdf:Description>'
        '</rdf:RDF></x:xmpmeta>'
    )
    return b"http://ns.adobe.com/xap/1.0/\x00" + xml.encode("utf-8")


def _iptc_app13(caption: str | None = None,
                keywords: tuple[str, ...] = ()) -> bytes:
    def ds(record: int, dataset: int, value: bytes) -> bytes:
        return bytes([0x1C, record, dataset]) + struct.pack(">H", len(value)) \
            + value

    iim = b""
    if caption is not None:
        iim += ds(2, 120, caption.encode("utf-8"))
    for k in keywords:
        iim += ds(2, 25, k.encode("utf-8"))
    # 8BIM block: id 0x0404 (IPTC-NAA), empty Pascal name padded to even,
    # 4-byte size, then the IIM stream padded to even.
    block = b"8BIM" + struct.pack(">H", 0x0404) + b"\x00\x00" \
        + struct.pack(">I", len(iim)) + iim
    if len(iim) % 2:
        block += b"\x00"
    return b"Photoshop 3.0\x00" + block


def _exif_orientation_app1(orientation: int) -> bytes:
    tiff = b"II" + struct.pack("<H", 42) + struct.pack("<I", 8)
    ifd = (struct.pack("<H", 1)
           + struct.pack("<HHI", 0x0112, 3, 1)  # Orientation, SHORT, count 1
           + struct.pack("<HH", orientation, 0)  # value in low 2 bytes, LE
           + struct.pack("<I", 0))               # next-IFD offset
    return b"Exif\x00\x00" + tiff + ifd


def write_jpeg_meta(path: Path, w: int = 64, h: int = 48, *,
                    xmp: bytes | None = None, iptc: bytes | None = None,
                    exif_orientation: int | None = None) -> None:
    data = _jpeg_bytes(w, h)
    if exif_orientation is not None:
        data = _inject(data, 0xE1, _exif_orientation_app1(exif_orientation))
    if xmp is not None:
        data = _inject(data, 0xE1, xmp)
    if iptc is not None:
        data = _inject(data, 0xED, iptc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


_EXT_GUID = b"AB" * 16  # 32 hex-valid chars — a fixture GUID, not a real MD5


def _main_xmp_with_note(guid: bytes, extra: str = "") -> bytes:
    """The main APP1 XMP packet declaring HasExtendedXMP -> guid."""
    xml = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:xmpNote="http://ns.adobe.com/xmp/note/">'
        f'<rdf:Description rdf:about="" '
        f'xmpNote:HasExtendedXMP="{guid.decode()}">{extra}'
        '</rdf:Description></rdf:RDF></x:xmpmeta>'
    )
    return b"http://ns.adobe.com/xap/1.0/\x00" + xml.encode("utf-8")


def _ext_app1(guid: bytes, total: int, offset: int, chunk: bytes) -> bytes:
    """One ExtendedXMP APP1 segment's PAYLOAD (caller injects the marker)."""
    return (b"http://ns.adobe.com/xmp/extension/\x00" + guid
            + struct.pack(">II", total, offset) + chunk)


def _seg(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def _jpeg_with_segments(*payloads: bytes, marker: int = 0xE1) -> bytes:
    """SOI + one APP1 segment per payload, IN THE GIVEN ORDER, + the rest
    of a real JPEG — so tests control exact on-disk segment order (needed
    for the out-of-order-chunks case, where _inject's insert-after-SOI
    behavior would silently reverse repeated calls)."""
    jpeg = _jpeg_bytes()
    assert jpeg[:2] == b"\xff\xd8"
    return jpeg[:2] + b"".join(_seg(marker, p) for p in payloads) + jpeg[2:]


def _face_region_xml(name: str, x: float, y: float, w: float, h: float) -> str:
    return ('<mwg-rs:Regions xmlns:mwg-rs='
            '"http://www.metadataworkinggroup.com/schemas/regions/" '
            'xmlns:stArea="http://ns.adobe.com/xmp/sType/Area#" '
            'rdf:parseType="Resource"><mwg-rs:RegionList><rdf:Bag>'
            '<rdf:li rdf:parseType="Resource">'
            f'<mwg-rs:Name>{name}</mwg-rs:Name><mwg-rs:Type>Face</mwg-rs:Type>'
            f'<mwg-rs:Area stArea:x="{x}" stArea:y="{y}" stArea:w="{w}" '
            f'stArea:h="{h}" stArea:unit="normalized"/>'
            '</rdf:li></rdf:Bag></mwg-rs:RegionList></mwg-rs:Regions>')


def _rect64(left: float, top: float, right: float, bottom: float) -> str:
    return "".join(format(round(v * 65536) & 0xFFFF, "04x")
                  for v in (left, top, right, bottom))


def _big_library(root: Path) -> None:
    """Sources larger than every test level so each level carries real,
    distinct pixels (a downscale, never an upscale)."""
    make_jpeg(root / "f" / "land.jpg", 600, 400)   # landscape
    make_jpeg(root / "f" / "port.jpg", 400, 600)   # portrait


def _offscreen_app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _bound_cache(tmp_path: Path, root: Path, levels=None):
    """Build + load + bind a cache for `root`; returns (catalog, cache)."""
    cat = scan_library(root)
    built = thumbcache.build_cache(cat, tmp_path / "c", levels=levels)
    cache = thumbcache.load_cache(built.path)
    thumbcache.bind(cache, cat)
    return cat, cache


def _spin(app, cond, timeout_s: float = 8.0) -> bool:
    """Pump the event loop until cond() (timers fire through
    processEvents) or the deadline passes; returns the final cond()."""
    import time
    deadline = time.monotonic() + timeout_s
    while not cond() and time.monotonic() < deadline:
        app.processEvents()
    return cond()


def _pump(app, seconds: float) -> None:
    """Pump the event loop for a fixed interval (to show something does
    NOT happen, e.g. no advance while paused)."""
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()


def _press(widget, key) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    widget.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))


def _show_library(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        make_jpeg(root / "show" / name)
    return root


def _header_click(g_widget, vx: float, vy: float) -> None:
    """Deliver a plain left-button press at viewport (vx, vy)."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    pos = QPointF(vx, vy)
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos, pos, pos,
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    g_widget.mousePressEvent(ev)


def _two_folder_library(root):
    """Synthetic library with two folders (2 + 1 photos) for play-glyph
    tests — no real Picasa data."""
    make_jpeg(root / "folder_a" / "img1.jpg")
    make_jpeg(root / "folder_a" / "img2.jpg")
    make_jpeg(root / "folder_b" / "img3.jpg")


def _search_win(library_root: Path):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None
    return MainWindow(scan_library(library_root), None,
                      cache_dir=None, build_dir=None)


def _hits(win) -> set:
    return {win.catalog.photos[i].name for i in win.grid.display}


def _selection_grid(tmp_path: Path):
    """A shown offscreen GridView over a synthetic two-folder library
    (6 + 3 photos), no thumb cache (selection never needs decoded tiles),
    sized to exactly 2 columns so row geometry is deterministic. Display
    order is p00..p08 (sorted rel paths)."""
    _offscreen_app()
    from grid import GridView

    root = tmp_path / "lib"
    k = 0
    for fi, count in ((0, 6), (1, 3)):
        for _ in range(count):
            make_jpeg(root / f"f{fi}" / f"p{k:02d}.jpg")
            k += 1
    cat = scan_library(root)
    g = GridView()
    g.resize(400, 640)   # viewport ~398 -> (398-8)//168 = 2 columns
    g.show()             # hidden widgets keep a stale default viewport size
    g.set_data(cat, None)
    assert len(g.display) == 9 and g.cols == 2
    return g


def _click(g, idx: int, modifiers=None, double: bool = False) -> None:
    """Deliver a left-button press (or double-click) at the center of the
    tile for catalog index idx, in viewport coordinates."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    gi, n = g.loc[idx]
    r = g._item_rect(g.groups[gi], n)
    pos = QPointF(r.center().x(),
                  r.center().y() - g.verticalScrollBar().value())
    mods = (modifiers if modifiers is not None
            else Qt.KeyboardModifier.NoModifier)
    kind = (QEvent.Type.MouseButtonDblClick if double
            else QEvent.Type.MouseButtonPress)
    ev = QMouseEvent(kind, pos, pos, pos, Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton, mods)
    (g.mouseDoubleClickEvent if double else g.mousePressEvent)(ev)


def _key(g, key, modifiers=None) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    mods = (modifiers if modifiers is not None
            else Qt.KeyboardModifier.NoModifier)
    g.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods))


def _write_faces_contacts_xml(path: Path) -> Path:
    """A synthetic machine-local contacts.xml (oracle-014 grammar): names
    the ini-conflict contact (xml must win) and the legacy-[Contacts]-only
    contact (nameable ONLY via contacts.xml)."""
    path.write_text(
        '<contacts>\n'
        ' <contact id="cccccccccccccccc" name="Carol Xml" '
        'modified_time="2026-01-01T00:00:00-07:00" local_contact="1"/>\n'
        ' <contact id="dddddddddddddddd" name="Dave Legacy" '
        'modified_time="2026-01-01T00:00:00-07:00" local_contact="1"/>\n'
        '</contacts>\n')
    return path


def _days_ago(n: float) -> float:
    import time
    return time.time() - n * 86400


def _jump_grid(tmp_path: Path):
    """A shown offscreen GridView over a 3-folder library (8 photos each),
    2 columns, viewport far shorter than the content so every jump has
    room to move. Returns the grid; group tops are read from g.groups."""
    _offscreen_app()
    from grid import GridView

    root = tmp_path / "lib"
    k = 0
    for fi in range(3):
        for _ in range(8):
            make_jpeg(root / f"f{fi}" / f"p{k:02d}.jpg")
            k += 1
    cat = scan_library(root)
    g = GridView()
    g.resize(400, 300)
    g.show()
    g.set_data(cat, None)
    assert len(g.groups) == 3 and g.content_h > g.viewport().height()
    assert g.verticalScrollBar().maximum() > g.groups[2].y
    return g


def _viewer_with_original(tmp_path: Path, w: int = 2560, h: int = 1600):
    """A 1280x800 ViewerPage showing photo 0 with a decoded `w`x`h` original
    already landed (via _on_loaded, no thread) — the common zoom case."""
    _offscreen_app()
    from PySide6.QtGui import QImage
    from viewer import ViewerPage
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "b.jpg")
    cat = scan_library(root)
    v = ViewerPage(cat, None)
    v.resize(1280, 800)
    v.show_photo(list(range(len(cat.photos))), 0)
    orig = QImage(w, h, QImage.Format.Format_RGB32)
    orig.fill(0x336699)
    v._on_loaded(v._serial, orig)
    assert v.image is orig and not v.zoomed
    return v, orig


def _mouse(widget, kind, x: float, y: float,
           button=None, modifiers=None) -> None:
    """Deliver a synthetic left-button mouse event to the widget handlers
    (press/move/release), offscreen-safe."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    pos = QPointF(x, y)
    btn = button if button is not None else Qt.MouseButton.LeftButton
    mods = (modifiers if modifiers is not None
            else Qt.KeyboardModifier.NoModifier)
    ev = QMouseEvent(kind, pos, pos, pos, btn, btn, mods)
    if kind == QEvent.Type.MouseButtonPress:
        widget.mousePressEvent(ev)
    elif kind == QEvent.Type.MouseMove:
        widget.mouseMoveEvent(ev)
    else:
        widget.mouseReleaseEvent(ev)


# -- the hover peek trigger state machine in the grid (fauxcasa-q6l.5) and
# the frameless full-screen surface MainWindow drives from it (peek.py) --


def _peek_move(g, idx: int | None, mods) -> None:
    """Deliver a button-free mouse move at the center of `idx`'s tile
    (or the top-left header/padding band for idx=None: no photo there)."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    if idx is None:
        pos = QPointF(2.0, 2.0)
    else:
        gi, n = g.loc[idx]
        r = g._item_rect(g.groups[gi], n)
        pos = QPointF(r.center().x(),
                      r.center().y() - g.verticalScrollBar().value())
    ev = QMouseEvent(QEvent.Type.MouseMove, pos, pos, pos,
                     Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, mods)
    g.mouseMoveEvent(ev)


def _key_up(g, key, modifiers=None) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    mods = (modifiers if modifiers is not None
            else Qt.KeyboardModifier.NoModifier)
    g.keyReleaseEvent(QKeyEvent(QEvent.Type.KeyRelease, key, mods))


def _peek_probes(g):
    """(requested, released) recorders wired to the grid's peek signals."""
    req: list[int] = []
    rel: list[bool] = []
    g.peek_requested.connect(req.append)
    g.peek_released.connect(lambda: rel.append(True))
    return req, rel


# Whitehorse YT — the western longitude exercises the hemisphere sign, and
# both coordinates convert to EXIF d/m/s rationals exactly (spike truth).
WHITEHORSE = (60.72125, -135.05685)


SYDNEY = (-33.8568, 151.2153)  # southern lat: the other sign branch


def _meta_jpeg(path: Path | None = None, **meta) -> bytes:
    """JPEG bytes carrying exactly the given in-file metadata; also written
    to `path` when given (embed_test_metadata raises on failure — a broken
    fixture must fail loudly)."""
    data = metareader.embed_test_metadata(_jpeg_bytes(), **meta)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return data


def _tiff_bytes(w: int = 40, h: int = 30) -> bytes:
    """A minimal TIFF via Qt's own encoder — no exiv2 involved yet, so the
    fixture stays privacy-safe synthetic pixels like _jpeg_bytes."""
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(20, 40, 60))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    assert img.save(buf, "TIFF")
    return bytes(buf.data())


def _synth_dng_module():
    """Load scripts/make-synthetic-dng.py once and cache it on this
    function (mirrors the make-thumbcache.py loader pattern used
    elsewhere in this file, e.g. test_raw_extensions_in_both_walkers)."""
    mod = getattr(_synth_dng_module, "_cached", None)
    if mod is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "synth_dng", REPO / "scripts" / "make-synthetic-dng.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _synth_dng_module._cached = mod
    return mod


def _make_dng(path: Path, w: int = 32, h: int = 24, orientation: int = 1,
              preview_jpeg: bytes | None = None,
              preview_size: tuple[int, int] = (0, 0),
              truncate: bool = False) -> Path:
    """A tiny synthetic DNG (see the section comment for provenance), built
    by scripts/make-synthetic-dng.py:_make_dng_bytes. With `preview_jpeg`,
    IFD0 is a JPEG-compressed preview (the layout real cameras use) and the
    CFA raw lives in a SubIFD; without, the raw IS IFD0 and the file
    carries no thumbnail at all (forces the demosaic fallback).
    `orientation` writes TIFF tag 274 so LibRaw bakes the flip during
    postprocess. `truncate` chops half the CFA strip off the end — a
    structurally-valid header whose pixel read fails (fail-soft test)."""
    out = _synth_dng_module()._make_dng_bytes(
        w=w, h=h, orientation=orientation,
        preview_jpeg=preview_jpeg, preview_size=preview_size)
    if truncate:
        strip_len = struct.calcsize(f"<{w * h}H")
        out = out[:len(out) - strip_len // 2]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out)
    return path


def _thumb_qimage(cache, idx: int):
    """Decode cache entry idx's primary-level blob, or None on error tile."""
    from PySide6.QtGui import QImage

    offset, length, _w, _h = cache.entries[idx]
    if length <= 0:
        return None
    with open(cache.path, "rb") as f:
        f.seek(offset)
        img = QImage.fromData(f.read(length), "JPEG")
    return None if img.isNull() else img


# 32-hex album uids: one ini-defined, one referenced-but-never-defined
# (the placeholder case), one that exists only as a .pal file.
UID_DEF = "1111222233334444555566667777888a"


UID_GHOST = "2222333344445555666677778888999b"


UID_PAL = "3333444455556666777788889999aaab"


def _write_pal(pal_dir: Path, uid: str, name: str, members: list[str],
               date: str | None = "39272.630035") -> Path:
    """One .pal file in the forensicir-documented picasa2album shape (the
    same XML the synthetic-corpus generator writes): property elements,
    then the <files> volume-token member list nested in the name property."""
    files = "\n".join(
        f" <filename>[C]\\{m.replace('/', chr(92))}</filename>"
        for m in members)
    date_prop = (f'<property name="date" type="real64" value="{date}"/>\n'
                 if date is not None else "")
    pal_dir.mkdir(parents=True, exist_ok=True)
    p = pal_dir / f"{uid}.pal"
    p.write_text(
        "<picasa2album>\n"
        f"<dbid>0164eaeacdd4046f5c1e44522fe44527</dbid>\n"
        f"<albumid>{uid}</albumid>\n"
        f'<property name="uid" type="string" value="{uid}"/>\n'
        f'<property name="category" type="num" value="0"/>\n'
        f"{date_prop}"
        f'<property name="token" type="string" value="]album:{uid}"/>\n'
        f'<property name="name" type="string" value="{name}">\n'
        "<files>\n"
        f"{files}\n"
        "</files>\n"
        "</property>\n"
        "</picasa2album>\n",
        encoding="utf-8")
    return p


CID_DB3 = "ca5c88ca60f42c0b"           # oracle-014's contact-id shape


PERSON_DB3 = "Synthetic Person 1200"


_PMP_TYPE_CODES = {"string": 0x0, "uint32": 0x1, "uint8": 0x3, "uint64": 0x4}


_PMP_PACK = {0x1: "<I", 0x3: "<B", 0x4: "<Q"}


def _write_pmp(path: Path, type_name: str, values: list) -> Path:
    """One .pmp column file to the documented layout (constants written
    literally from picasa-db3-validated.md, NOT taken from the reader, so
    the round-trip test proves the parser against independent bytes):
    u32 magic 0x3fcccccd | u16 type | u16 0x1332 | u32 2 | u16 type |
    u16 0x1332 | u32 count | payload (NUL-terminated strings or packed
    little-endian fixed-width values)."""
    code = _PMP_TYPE_CODES[type_name]
    out = bytearray(struct.pack("<IHHIHHI", 0x3FCCCCCD, code, 0x1332, 2,
                                code, 0x1332, len(values)))
    for v in values:
        if code == 0x0:
            out += v.encode("utf-8") + b"\x00"
        else:
            out += struct.pack(_PMP_PACK[code], v)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def _write_thumbindex(path: Path, entries: list) -> Path:
    """thumbindex.db to the documented layout: u32 magic 0x40466666 |
    u32 count | per entry NUL-terminated name + <u64 taken, u64 mtime,
    u32 size, u8 ftype, u32 flags, u8 valid, u32 parent>. `entries` is
    (name, ftype, parent-index-or-None); timestamps/sizes stay 0 (the
    oracle's face-crop records carry 0 there too) and valid is 1."""
    out = bytearray(struct.pack("<II", 0x40466666, len(entries)))
    for name, ftype, parent in entries:
        out += name.encode("utf-8") + b"\x00"
        out += struct.pack("<QQIBIBI", 0, 0, 0, ftype, 0, 1,
                           0xFFFFFFFF if parent is None else parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def _db3_machine_path(folder: Path, drive: str = "Q:") -> str:
    """The db3 spelling of `folder`: absolute, backslashes, trailing
    separator (folder thumbindex entries carry one), and a DIFFERENT
    drive letter than the live one — §8 says drive letters are
    translated, so every join in these tests must survive the swap."""
    parts = [c for c in str(folder).replace("\\", "/").split("/") if c]
    if len(parts[0]) == 2 and parts[0][1] == ":":
        parts = parts[1:]
    return drive + "\\" + "\\".join(parts) + "\\"


def _make_person_db3(db3: Path, folder_abs: str, photos: list[str],
                     contact_id: str = CID_DB3,
                     person_name: str = PERSON_DB3,
                     face_parent: int | None = None) -> Path:
    """A minimal synthetic db3 in the oracle-014 shape: thumbindex row 0
    is the folder (absolute machine path), rows 1..n the photos, plus —
    when face_parent (a photo's thumbindex row) is given — a virtual
    face-crop record whose imagedata row is filetype 1001 joined via
    personalbumid to a category-8 person album. albumdata carries the
    stock no-contact-id people bucket too, which the rescue must skip."""
    if not folder_abs.endswith(("\\", "/")):
        folder_abs += "\\"
    ti = [(folder_abs, 0x01, None)]
    for name in photos:
        ti.append((name, 0x02, 0))
    filetype = [1] + [2] * len(photos)
    personalbumid = [0] * (1 + len(photos))
    if face_parent is not None:
        ti.append(("", 0xE9, face_parent))    # ftype = low byte of 0x3e9
        filetype.append(1001)                 # the virtual face-crop row
        personalbumid.append(2)               # -> the person-album row
    _write_thumbindex(db3 / "thumbindex.db", ti)
    # albumdata rows: [0] the watched folder (category 2), [1] the stock
    # people bucket (category 8, NO contact id — skipped by the rescue),
    # [2] the person album (category 8, name + albumcontactids).
    _write_pmp(db3 / "albumdata_category.pmp", "uint32", [2, 8, 8])
    _write_pmp(db3 / "albumdata_name.pmp", "string",
               [folder_abs, "Unnamed people", person_name])
    _write_pmp(db3 / "albumdata_albumcontactids.pmp", "uint64",
               [0, 0, int(contact_id, 16)])
    _write_pmp(db3 / "albumdata_token.pmp", "string",
               ["", "]unknownface", "]facealbum:2"])
    _write_pmp(db3 / "imagedata_filetype.pmp", "uint32", filetype)
    _write_pmp(db3 / "imagedata_personalbumid.pmp", "uint32", personalbumid)
    return db3


def _make_caption_db3(db3: Path, folder_abs: str, photos: list[str],
                      captions: list[str]) -> Path:
    """Minimal imagedata.caption + thumbindex fixture for cam.20.

    Row 0 is the containing folder and rows 1..n are photo rows, exactly the
    validated db3 join shape. captions is parallel to photos; the folder's
    caption cell is the leading empty string.
    """
    assert len(photos) == len(captions)
    if not folder_abs.endswith(("\\", "/")):
        folder_abs += "\\"
    _write_thumbindex(db3 / "thumbindex.db", [
        (folder_abs, 0x01, None),
        *((name, 0x02, 0) for name in photos),
    ])
    _write_pmp(db3 / "imagedata_caption.pmp", "string", ["", *captions])
    return db3


def _two_root_library_cfg(tmp_path: Path):
    """Two explicit roots whose Trip/ folders carry the SAME rel — the
    duplicate-rel shape cam.21's root-qualified identity exists for."""
    root_a = tmp_path / "roots" / "alpha"
    root_b = tmp_path / "roots" / "beta"
    make_jpeg(root_a / "Trip" / "same.jpg")
    make_jpeg(root_b / "Trip" / "same.jpg")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    cfg = libmod.LibraryConfig(
        library_id=libmod.mint_library_id(), name="Two roots",
        roots=[libmod.LibraryRoot(id="aaaaaaaa", path=root_a, label="A"),
               libmod.LibraryRoot(id="bbbbbbbb", path=root_b, label="B")],
        home=home)
    return cfg, root_a, root_b


def _tray_window(tmp_path: Path, with_cache: bool = False):
    """A MainWindow over a synthetic two-folder library (2 + 2 photos,
    one single-member album), optionally with a built+bound fcache so
    tray thumbs have real pixels. Display order (sorted rels) is
    f0/a.jpg, f0/b.jpg, f1/c.jpg, f1/d.jpg -> catalog indices 0..3."""
    _offscreen_app()
    from main import MainWindow

    root = tmp_path / "lib"
    make_jpeg(root / "f0" / "a.jpg")
    make_jpeg(root / "f0" / "b.jpg")
    make_jpeg(root / "f1" / "c.jpg")
    make_jpeg(root / "f1" / "d.jpg")
    (root / "f0" / ".picasa.ini").write_text(
        "[a.jpg]\r\nalbums=cafecafecafecafecafecafecafecafe\r\n"
        "[.album:cafecafecafecafecafecafecafecafe]\r\nname=Best\r\n"
    )
    cat = scan_library(root)
    thumbs = None
    if with_cache:
        built = thumbcache.build_cache(cat, tmp_path / "c")
        thumbs = thumbcache.load_cache(built.path)
        thumbcache.bind(thumbs, cat)
    win = MainWindow(cat, thumbs, cache_dir=None, build_dir=None)
    assert [cat.photos[i].rel for i in win.grid.display] == [
        "f0/a.jpg", "f0/b.jpg", "f1/c.jpg", "f1/d.jpg"]
    return win


def _sidebar_click(win, kind: str, key: str) -> None:
    """Click the sidebar item carrying (kind, key) through the real
    itemClicked signal, so both connected slots (_sidebar_clicked and
    the tray-readout refresh) run in connection order."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItemIterator

    it = QTreeWidgetItemIterator(win.tree)
    while it.value():
        if it.value().data(0, Qt.ItemDataRole.UserRole) == (kind, key):
            win.tree.itemClicked.emit(it.value(), 0)
            return
        it += 1
    raise AssertionError(f"sidebar item {(kind, key)} not found")


def _make_clip(path: Path, color=(200, 60, 40), w: int = 64, h: int = 48,
               nframes: int = 8, rate: int = 8,
               creation_time: str | None = None) -> Path:
    """A tiny real video: solid-`color` frames, mpeg4, in whatever
    container the extension names (.mp4 muxes moov-at-end by default —
    exactly the shape that defeats pipe input and needs seekable reads).
    8 frames at 8 fps = 1 s; pass nframes=2 for a sub-second clip that
    forces the poster's seek-past-the-end fallback.
    Pass creation_time (ISO 8601 UTC, e.g. '2023-05-15T10:30:00.000000Z')
    to embed container creation_time metadata for probe_creation_time tests
    (synthetic data only — privacy rule)."""
    import av
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w") as container:
        if creation_time is not None:
            container.metadata["creation_time"] = creation_time
        stream = container.add_stream("mpeg4", rate=rate)
        stream.width, stream.height = w, h
        stream.pix_fmt = "yuv420p"
        img = Image.new("RGB", (w, h), color)
        for _ in range(nframes):
            for pkt in stream.encode(av.VideoFrame.from_image(img)):
                container.mux(pkt)
        for pkt in stream.encode():   # flush the encoder
            container.mux(pkt)
    return path


def _adopted_catalog(root: Path, tmp_path: Path):
    """main()'s --thumbs flow in miniature: build a prebuilt fcache from
    one walk, bind a FRESH scan (ini-only, signal-less) to it, mark the
    catalog NOT_STARTED and persist it — returns (catalog, catalog_path)
    ready for backfill_catalog."""
    built = thumbcache.build_cache(scan_library(root), tmp_path / "prebuilt")
    assert built is not None
    cat = scan_library(root)
    cache = thumbcache.load_cache(built.path)
    thumbcache.bind(cache, cat)
    cat.backfill_state = BACKFILL_NOT_STARTED
    cat_path = tmp_path / "catalog.json"
    save_catalog(cat, cat_path)
    return cat, cat_path


# Asymmetric on BOTH axes (margins differ left/right and top/bottom), so
# every mirror, turn, or axis-swap mix-up moves the patch and fails a probe.
_FACE_STORED_RECT = (0.25, 0.5, 0.5, 0.75)


def _marked_stored_image(w: int = 96, h: int = 64):
    """Gray stored-frame image with a red block filling exactly the
    _FACE_STORED_RECT fractions."""
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(96, 96, 96))
    left, top, right, bottom = _FACE_STORED_RECT
    for y in range(int(top * h), int(bottom * h)):
        for x in range(int(left * w), int(right * w)):
            img.setPixelColor(x, y, QColor(255, 0, 0))
    return img


def _qt_display_transform(img, orientation: int):
    """Qt's own pixel-level EXIF display transform — the independent
    reference: QTransform mirror/rotate combos straight from the EXIF 274
    definitions (2 mirror-H; 3 rotate 180; 4 mirror-V; 5 mirror-H + rotate
    270 CW = transpose; 6 rotate 90 CW; 7 mirror-H + rotate 90 CW =
    transverse; 8 rotate 270 CW), NOT map_face_fraction's algebra."""
    from PySide6.QtGui import QTransform

    rot90 = QTransform().rotate(90)
    mir_h = QTransform().scale(-1, 1)   # not QImage.mirrored: that bool
    mir_v = QTransform().scale(1, -1)   # overload is deprecated in PySide6
    if orientation == 2:
        return img.transformed(mir_h)
    if orientation == 3:
        return img.transformed(QTransform().rotate(180))
    if orientation == 4:
        return img.transformed(mir_v)
    if orientation == 5:
        return img.transformed(rot90).transformed(mir_h)
    if orientation == 6:
        return img.transformed(rot90)
    if orientation == 7:
        return img.transformed(rot90).transformed(mir_v)
    if orientation == 8:
        return img.transformed(QTransform().rotate(270))
    return img


def _face_viewer(tmp_path: Path):
    """A 1280x800 viewer over a 2-photo library where photo 0 carries two
    faces= tags (one named via [Contacts2], one an unconfirmed
    ffffffffffffffff suggestion) and photo 1 carries none; photo 0's
    original is landed via _on_loaded after staling the async decode job
    (serial bump), so the landed orientation can never be overwritten by
    the worker mid-test."""
    _offscreen_app()
    from PySide6.QtGui import QImage

    from viewer import ViewerPage

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "b.jpg")
    (root / "f" / ".picasa.ini").write_text(
        "[Contacts2]\r\nabcdef0123456789=Pat Named;;\r\n"
        "[a.jpg]\r\n"
        "faces=rect64(3f845bcb59418507),abcdef0123456789;"
        "rect64(ff),ffffffffffffffff\r\n")
    cat = scan_library(root)
    assert cat.photos[0].faces and not cat.photos[1].faces
    v = ViewerPage(cat, None)
    v.resize(1280, 800)
    v.show_photo([0, 1], 0)
    v._serial += 1                       # stale the async decode job
    orig = QImage(2560, 1600, QImage.Format.Format_RGB32)
    orig.fill(0x336699)
    v._on_loaded(v._serial, orig, 1)
    assert v.image is orig
    return v, orig


def _photo(name: str, folder: str = "f", **kw):
    from catalog import Photo
    return Photo(rel=f"{folder}/{name}", folder=folder, name=name, **kw)


def _load_mtc():
    """scripts/make-thumbcache.py as a module (hyphenated file name)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mtc", REPO / "scripts" / "make-thumbcache.py")
    mtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtc)
    return mtc


def _make_tga(path: Path, w: int = 64, h: int = 48,
              color: tuple[int, int, int] = (40, 200, 90)) -> Path:
    """A synthetic TGA via Pillow (Qt's qtga plugin reads, PIL writes)."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), color).save(path, "TGA")
    return path


def _make_psd(path: Path, w: int = 64, h: int = 48,
              color: tuple[int, int, int] = (200, 60, 120),
              truncate: bool = False) -> Path:
    """A synthetic 'maximize compatibility' PSD: hand-built to the
    documented framing (Pillow READS PSD but cannot write it) — 8BPS v1
    header, empty color-mode/resources/layer sections, then the raw-
    compression flattened composite as planar RGB. Privacy-safe like the
    hand-built APP segments above. `truncate=True` cuts the composite
    planes short: the synthetic stand-in for a PSD saved WITHOUT
    maximize-compatibility (no usable composite -> legitimately an
    error tile, never a crash)."""
    r, g, b = color
    out = (b"8BPS" + struct.pack(">H", 1) + b"\x00" * 6
           + struct.pack(">H", 3)            # channels
           + struct.pack(">II", h, w)        # rows, columns
           + struct.pack(">HH", 8, 3))       # 8-bit, mode 3 = RGB
    out += struct.pack(">I", 0)              # color mode data: empty
    out += struct.pack(">I", 0)              # image resources: empty
    out += struct.pack(">I", 0)              # layer & mask info: empty
    planes = (bytes([r]) * (w * h) + bytes([g]) * (w * h)
              + bytes([b]) * (w * h))
    data = struct.pack(">H", 0) + planes     # compression 0 = raw
    if truncate:
        data = data[: 2 + (w * h) // 2]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out + data)
    return path


# Committed synthetic fixture (96x64, four solid-color quadrants, EXIF
# Orientation=1) — see fixtures/heic-smoke/synthetic.heic.txt for
# provenance and how to regenerate it. Committed rather than generated
# in-test because Pillow itself cannot ENCODE HEIC without pillow-heif's
# x265 encoder, which the app deliberately does NOT depend on (GPLv2
# wheels, docs/research/heic-decode-decision.md); the app's own pi-heif
# is decode-only.
_HEIC_FIXTURE = REPO / "fixtures" / "heic-smoke" / "synthetic.heic"


def _heic_bytes(broken: bool = False) -> bytes:
    """The committed fixture's bytes, or the first half only — enough to
    break libheif's container parse (verified: raises
    UnidentifiedImageError, never decodes garbage) — the HEIC stand-in
    for _make_psd's truncate=True, a legitimately undecodable file that
    must error-tile, never crash the batch."""
    data = _HEIC_FIXTURE.read_bytes()
    return data[: len(data) // 2] if broken else data


def _assert_heic_quadrants(get_px) -> None:
    """`get_px(x, y) -> QColor`-shaped probe over the fixture's four
    solid-color quadrants (see synthetic.heic.txt), lossy-HEVC tolerant
    (+/-30, matching the PSD/TGA fixtures' tolerance above)."""
    tl, tr = get_px(5, 5), get_px(90, 5)
    bl, br = get_px(5, 58), get_px(90, 58)
    assert abs(tl.red() - 220) < 30 and abs(tl.green() - 40) < 30 \
        and abs(tl.blue() - 40) < 30                       # top-left: red
    assert abs(tr.red() - 40) < 30 and abs(tr.green() - 200) < 30 \
        and abs(tr.blue() - 60) < 30                       # top-right: green
    assert abs(bl.red() - 40) < 30 and abs(bl.green() - 80) < 30 \
        and abs(bl.blue() - 220) < 30                       # bottom-left: blue
    assert abs(br.red() - 230) < 30 and abs(br.green() - 210) < 30 \
        and abs(br.blue() - 30) < 30                        # bottom-right: yellow


class _StubSandboxedService:
    """A minimal decodefacade.get_service() stand-in that CLAIMS to be
    sandboxed and answers UNSUPPORTED (a null QImage) for .heic/.heif
    paths specifically, deciding purely from the extension — never a
    real sandbox spawn. Non-HEIC paths decode via plain QImage(path) so
    an unrelated neighbor file in the same test still gets a real tile
    (the generic top-level scaled/quality resize in _index_one/viewer
    still applies afterward regardless of edge).

    The point: thumbcache._index_one and viewer.load_original_oriented
    only ever reach decodefacade.get_service().decode() when a still
    ISN'T pre-routed by extension first (psd/16-bit-tiff/heic all skip
    it via elif branches evaluated in order). Patching get_service() to
    return this stub, then asserting a HEIC file still decodes, proves
    the .heic/.heif elif intercepted BEFORE this stub's decode() could
    ever run — delete that elif and the dispatch falls through to this
    stub instead, which answers null, exactly like a real sandboxed
    worker's UNSUPPORTED for a format its canRead() doesn't recognize
    (fauxcasa-ez2.9 Stage 2 review P1-1, the same risk documented for
    the PSD pre-route)."""

    state = None  # set to decodefacade.STATE_SANDBOXED by the fixture below
    reason = "test stub -- never a real sandbox"

    def decode(self, path: str, route: str = "still", edge: int = 0):
        from PySide6.QtGui import QImage

        if path.lower().endswith((".heic", ".heif")):
            return QImage()  # UNSUPPORTED, as a real sandboxed worker
                              # would answer for a format its canRead()
                              # rejects -- this must never be reached.
        return QImage(path)


def _patch_sandboxed_service(monkeypatch) -> None:
    """Make decodefacade.get_service() (as thumbcache.py/viewer.py call
    it, `import decodefacade` + `decodefacade.get_service()`) return
    _StubSandboxedService -- see its docstring for why this is the
    tightest way to prove a HEIC pre-route branch, not the generic
    post-Qt-null Pillow fallback, is what decoded the fixture (a plain
    green run without this patch can't tell the two apart: the generic
    fallback rescues HEIC too when the service is merely in-process/
    degraded, the default in this test process)."""
    import decodefacade

    stub = _StubSandboxedService()
    stub.state = decodefacade.STATE_SANDBOXED
    monkeypatch.setattr(decodefacade, "get_service", lambda: stub)


_HEIC_ROT6 = REPO / "fixtures" / "heic-smoke" / "synthetic-rot6.heic"


_HEIC_EXIF6 = REPO / "fixtures" / "heic-smoke" / "synthetic-exif6.heic"


_HEIC_XMP6 = REPO / "fixtures" / "heic-smoke" / "synthetic-xmp6.heic"


def _assert_heic_rotated_quadrants(img) -> None:
    """The displayed layout BOTH rotated fixtures must produce: the 96x64
    quadrant plane turned 90 deg clockwise into 64x96 -- TL blue, TR
    red, BL yellow, BR green (synthetic-rot6.heic.txt), +/-30 lossy
    tolerance like _assert_heic_quadrants."""
    assert (img.width(), img.height()) == (64, 96)
    tl, tr = img.pixelColor(5, 5), img.pixelColor(59, 5)
    bl, br = img.pixelColor(5, 91), img.pixelColor(59, 91)
    assert abs(tl.red() - 40) < 30 and abs(tl.blue() - 220) < 30   # blue
    assert abs(tr.red() - 220) < 30 and abs(tr.green() - 40) < 30  # red
    assert abs(bl.red() - 230) < 30 and abs(bl.green() - 210) < 30  # yellow
    assert abs(br.red() - 40) < 30 and abs(br.green() - 200) < 30  # green


# Oracle fixture 004's exact value: rect64(dc3369dc570a51e), zero-stripped
# 15-hex — the padded split is 0dc3 369d c570 a51e.
_FIXTURE_004_CROP = "rect64(dc3369dc570a51e)"


_FIXTURE_004_RECT = (0x0DC3 / 65536, 0x369D / 65536,
                     0xC570 / 65536, 0xA51E / 65536)


def _quadrant_jpeg(path: Path, edge: int = 1024) -> None:
    """A 4-quadrant marker image: TL red, TR green, BL blue, BR white."""
    from PySide6.QtGui import QColor, QImage, QPainter

    img = QImage(edge, edge, QImage.Format.Format_RGB32)
    half = edge // 2
    p = QPainter(img)
    p.fillRect(0, 0, half, half, QColor(255, 0, 0))
    p.fillRect(half, 0, half, half, QColor(0, 200, 0))
    p.fillRect(0, half, half, half, QColor(0, 0, 255))
    p.fillRect(half, half, half, half, QColor(255, 255, 255))
    p.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    assert img.save(str(path), "JPEG", 95)


def _promote_fixture(tmp_path: Path) -> tuple[Path, Path, "Catalog", Path]:
    """A one-photo legacy library with a real, already-built cache dir
    (catalog + fcache + sidecar) — the on-disk shape promote_library
    expects to find (Journey A always promotes an already-opened
    library). Returns (root, cache_root, catalog, old_dir)."""
    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    cache_root = tmp_path / "cache"
    cat = scan_library(root)
    old_dir = thumbcache.cache_dir_for(str(root.resolve()), cache_root)
    thumbcache.build_cache(cat, old_dir)
    save_catalog(cat, old_dir / "catalog.json")
    return root, cache_root, cat, old_dir


def _multiroot_ui_catalog(tmp_path: Path):
    from catalog import Catalog, Folder, Photo

    root_a, root_b = tmp_path / "root-a", tmp_path / "root-b"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)
    id_a, id_b = "aaaaaaaa", "bbbbbbbb"
    photos = [
        Photo(rel="2019/same.jpg", folder="2019", name="same.jpg",
              root_id=id_a),
        Photo(rel="2019/same.jpg", folder="2019", name="same.jpg",
              root_id=id_b),
    ]
    folders = {
        "2019": Folder(rel="2019", title="A 2019", photo_count=1,
                       total_count=1, root_id=id_a),
        f"{id_b}/2019": Folder(rel="2019", title="B 2019", photo_count=1,
                               total_count=1, root_id=id_b),
    }
    roots = [libmod.LibraryRoot(id=id_a, path=root_a, label="Primary"),
             libmod.LibraryRoot(id=id_b, path=root_b, label="Archive")]
    return Catalog(root=root_a, photos=photos, folders=folders, albums={},
                   roots=roots, library_id=libmod.mint_library_id())


def _inspector_text(panel) -> str:
    """VISIBLE label text under the panel, joined — a stand-in for
    reading a single text field (the panel is a form of many QLabels,
    unlike the status bar's one meta_label). The isVisibleTo filter
    matters: set_photo hides the state label without clearing it, so an
    unfiltered findChildren would still report a stale "N photos
    selected" after a multi -> single transition."""
    from PySide6.QtWidgets import QLabel
    return "\n".join(lbl.text() for lbl in panel.findChildren(QLabel)
                     if lbl.isVisibleTo(panel))


def _make_stream_clip(path: Path, w: int = 64, h: int = 48,
                      nframes: int = 8, rate: int = 8, gop: int | None = None,
                      audio: bool = True, audio_rate: int = 8000,
                      audio_seconds: float | None = None) -> Path:
    """_make_clip's shape plus an optional s16 mono 440 Hz sine track.
    mov container: carries pcm_s16le, so audio frames need no aac/fltp
    conversion and stay bit-exact s16 end to end. `audio_seconds`
    decouples the track length from the video duration (default: match) —
    the audio-outlives-video shape the backpressure test needs."""
    import math

    import av
    import numpy as np
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w") as container:
        vs = container.add_stream("mpeg4", rate=rate)
        vs.width, vs.height = w, h
        vs.pix_fmt = "yuv420p"
        if gop is not None:
            vs.codec_context.gop_size = gop
        ast = None
        if audio:
            ast = container.add_stream("pcm_s16le", rate=audio_rate)
            ast.codec_context.layout = "mono"
        img = Image.new("RGB", (w, h), (200, 60, 40))
        for _ in range(nframes):
            for pkt in vs.encode(av.VideoFrame.from_image(img)):
                container.mux(pkt)
        for pkt in vs.encode():
            container.mux(pkt)
        if ast is not None:
            total = int(audio_rate * (audio_seconds if audio_seconds
                                      is not None else nframes / rate))
            t = np.arange(total, dtype=np.float64)
            sine = (0.3 * 32767 * np.sin(
                2 * math.pi * 440 * t / audio_rate)).astype(np.int16)
            pos = 0
            while pos < total:
                n = min(1024, total - pos)
                fr = av.AudioFrame.from_ndarray(
                    sine[pos:pos + n].reshape(1, -1),
                    format="s16", layout="mono")
                fr.sample_rate = audio_rate
                fr.pts = pos
                for pkt in ast.encode(fr):
                    container.mux(pkt)
                pos += n
            for pkt in ast.encode():
                container.mux(pkt)
    return path


def _drain_stream(vs, deadline_s: float = 20.0):
    """Read a stream to eof/error, freeing each slot after copying the
    facts out; returns ([(pts_us, w, h, stride, nbytes)...], audio_bytes).
    Bounded: the deadline caps a wedged stream so a failure is a clean
    assert, never a hung suite."""
    import time as _time

    frames: list[tuple[int, int, int, int, int]] = []
    audio_bytes = 0
    deadline = _time.monotonic() + deadline_s
    while _time.monotonic() < deadline:
        while (chunk := vs.next_audio()) is not None:
            audio_bytes += len(chunk[0])
        got = vs.next_frame(timeout_ms=500)
        if got is None:
            if vs.at_eof or vs.error is not None or vs.closed:
                break
            continue
        slot, pts_us, w, h, stride, mv = got
        frames.append((pts_us, w, h, stride, len(mv)))
        vs.free_slot(slot)
    while (chunk := vs.next_audio()) is not None:
        audio_bytes += len(chunk[0])
    return frames, audio_bytes


def _video_viewer(tmp_path: Path, audio: bool = False, nframes: int = 8):
    """A shown ViewerPage on a synthetic clip (plus a still neighbour for
    navigation): (app, viewer, catalog, clip_idx)."""
    app = _offscreen_app()
    from viewer import ViewerPage

    root = tmp_path / "lib"
    _make_stream_clip(root / "clip.mov", audio=audio, nframes=nframes)
    make_jpeg(root / "still.jpg")
    cat = scan_library(root)
    v = ViewerPage(cat, None)
    v.resize(480, 360)
    v.show()
    idx = next(i for i, p in enumerate(cat.photos) if p.rel == "clip.mov")
    v.show_photo(list(range(len(cat.photos))), idx)
    return app, v, cat, idx


def _sidebar_text(win, kind: str, key: str) -> str:
    """The sidebar label text of the (kind, key) item — the counts a user
    reads, so a test can assert the view and the sidebar agree."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItemIterator

    it = QTreeWidgetItemIterator(win.tree)
    while it.value():
        if it.value().data(0, Qt.ItemDataRole.UserRole) == (kind, key):
            return it.value().text(0)
        it += 1
    raise AssertionError(f"sidebar item {(kind, key)} not found")


def _tooltip_literal(tip: str) -> str:
    """The text a tooltip actually SHOWS. QToolTip has no plain-text mode
    — it renders the string as HTML whenever Qt thinks it might be rich
    text — so a tooltip is only safe if HTML-rendering it still yields the
    original characters. That is exactly what this returns."""
    from PySide6.QtGui import QTextDocument

    doc = QTextDocument()
    doc.setHtml(tip)
    return doc.toPlainText()


VIEW_SPEC_ALBUM_UID = "deadbeefdeadbeefdeadbeefdeadbeef"


def _run_main_capturing_window(monkeypatch, argv_tail: list[str]):
    """In-process, offscreen main() run that captures the constructed
    MainWindow via a constructor spy — main() itself returns only an exit
    code. Asserts a clean exit and exactly one window built."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import main

    app = QApplication.instance() or QApplication([])
    assert app is not None
    captured: list = []
    real_init = main.MainWindow.__init__

    def spy_init(self, *a, **kw):
        real_init(self, *a, **kw)
        captured.append(self)

    monkeypatch.setattr(main.MainWindow, "__init__", spy_init)
    monkeypatch.setattr(sys, "argv", ["fauxcasa-tracer", *argv_tail])
    rc = main.main()
    assert rc == 0, rc
    assert len(captured) == 1
    return captured[0]


def _dated_starred_library(root: Path) -> None:
    """Synthetic multi-month library for the date-grouped Starred tests —
    no real Picasa data."""
    make_jpeg(root / "f" / "sep.jpg")
    make_jpeg(root / "f" / "aug.jpg")
    make_jpeg(root / "f" / "jul.jpg")
    make_jpeg(root / "f" / "nodate.jpg")


def _star_all_with_dates(cat: Catalog) -> dict[str, int]:
    """Star every photo in `cat` and assign _dated_starred_library's month
    spread via direct field mutation (mtime stays -1/unindexed and
    date_taken stays None on nodate.jpg, so it sinks to Undated exactly
    like a real never-backfilled photo would)."""
    by_name = {p.name: i for i, p in enumerate(cat.photos)}
    cat.photos[by_name["sep.jpg"]].date_taken = "2026-09-05T10:00:00"
    cat.photos[by_name["aug.jpg"]].date_taken = "2026-08-20T10:00:00"
    cat.photos[by_name["jul.jpg"]].date_taken = "2026-07-01T10:00:00"
    for i in by_name.values():
        cat.photos[i].star = 1
    return by_name
