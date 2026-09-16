"""Tests for main.py/thumbcache.py/viewer.py metadata_library integration.

Split from test_tracer.py (fauxcasa-l09); originally lines 7353-7576 of the monolith."""

from __future__ import annotations

import json
from pathlib import Path
import pytest
import thumbcache
from catalog import (
    load_catalog,
    save_catalog,
    scan_library,
)
from tracer_helpers import (
    SYDNEY,
    WHITEHORSE,
    _click,
    _offscreen_app,
    _raw_catalog,
    _selection_grid,
)


def test_index_precedence_infile_beats_ini(
        metadata_library: Path, tmp_path: Path) -> None:
    cat = scan_library(metadata_library)
    by = {p.name: p for p in cat.photos}
    # scan-level state before the index: ini only, in-file not read yet
    assert by["a.jpg"].geotag == pytest.approx(SYDNEY)
    assert by["a.jpg"].star == 1 and by["a.jpg"].date_taken is None
    assert by["c.jpg"].star == 0

    assert thumbcache.build_cache(cat, tmp_path / "c") is not None
    # a: in-file EXIF GPS beats ini geotag=; Rating 3 beats bare star=yes
    assert by["a.jpg"].geotag == pytest.approx(WHITEHORSE)
    assert by["a.jpg"].star == 3
    assert by["a.jpg"].date_taken == "1899-03-02T14:00:00"  # footgun 16
    # b: no in-file values -> the ini fallback survives the index pass
    assert by["b.jpg"].geotag == pytest.approx(SYDNEY)
    assert by["b.jpg"].star == 1 and by["b.jpg"].date_taken is None
    # c: Rating alone sets the count
    assert by["c.jpg"].star == 2
    # d: an explicit Rating 0 does NOT unstar an ini-starred photo (§3:
    # ini star= is authoritative for zero-vs-nonzero)
    assert by["d.jpg"].star == 1


def test_metadata_catalog_roundtrip_and_version_gate(
        metadata_library: Path, tmp_path: Path) -> None:
    """date_taken / geotag / star count survive save_catalog/load_catalog
    (the warm-load path), and a pre-v5 catalog is rejected so a warm start
    can never silently drop the new fields."""
    import catalog as catmod

    cat = scan_library(metadata_library)
    assert thumbcache.build_cache(cat, tmp_path / "c") is not None
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    loaded = load_catalog(path, metadata_library)
    assert loaded is not None
    for orig, back in zip(cat.photos, loaded.photos):
        assert back.star == orig.star and isinstance(back.star, int)
        assert back.date_taken == orig.date_taken
        assert back.geotag == orig.geotag  # exact: rounded before persist
    a = next(p for p in loaded.photos if p.name == "a.jpg")
    assert a.star == 3 and a.geotag == pytest.approx(WHITEHORSE)
    assert a.date_taken == "1899-03-02T14:00:00"

    # the version gate: a v4 (pre-metadata) catalog cold-rebuilds
    assert catmod.CATALOG_VERSION >= 5   # exact value pinned by the v7 test
    data = _raw_catalog(path)
    data["version"] = 4
    path.write_text(json.dumps(data))  # plain JSON: a version this old never zstd-wrapped
    assert load_catalog(path, metadata_library) is None


def test_grid_geotag_badge_paint_smoke(tmp_path: Path) -> None:
    """The geotag corner badge paints without incident alongside the star
    badge and selection chrome (offscreen render through paintEvent), in
    a corner of its own (bottom-right vs the star's top-right)."""
    from PySide6.QtGui import QImage

    from grid import GEO_TEAL, STAR_GOLD, _pin_polygon

    g = _selection_grid(tmp_path)
    cat = g.catalog
    d = g.display
    cat.photos[d[0]].geotag = WHITEHORSE            # pin only
    cat.photos[d[1]].geotag = SYDNEY                # pin + star together
    cat.photos[d[1]].star = 4
    _click(g, d[1])                                 # selection chrome on top
    shot = g.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
    assert not shot.isNull()

    # both badges actually hit the viewport: their colors appear in the shot
    found = {"geo": False, "star": False}
    for y in range(0, shot.height(), 2):
        for x in range(0, shot.width(), 2):
            c = shot.pixelColor(x, y)
            if (abs(c.red() - GEO_TEAL.red()) < 30
                    and abs(c.green() - GEO_TEAL.green()) < 30
                    and abs(c.blue() - GEO_TEAL.blue()) < 30):
                found["geo"] = True
            elif (abs(c.red() - STAR_GOLD.red()) < 30
                    and abs(c.green() - STAR_GOLD.green()) < 30
                    and abs(c.blue() - STAR_GOLD.blue()) < 30):
                found["star"] = True
        if all(found.values()):
            break
    assert found["geo"] and found["star"]

    # shape sanity: a closed teardrop — the tip plus a 13-point head arc
    poly = _pin_polygon(10.0, 10.0, 8.0)
    assert poly.size() == 14
    assert poly.at(0).y() > poly.at(7).y()  # tip below the head's top arc


def test_status_readout_date_coords_and_star_count(library: Path) -> None:
    """Single-photo status-bar mode (§5 dual mode) reads out capture date,
    coordinates (§3 geotag v1 display), and the star COUNT (one ★ per
    star); the Starred view still treats any count >= 1 as starred
    (backward-compatible truthiness)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None
    cat = scan_library(library)
    a = next(p for p in cat.photos if p.rel.endswith("Trip/a.jpg"))
    a.star = 3
    a.date_taken = "1899-03-02T14:00:00"
    a.geotag = WHITEHORSE
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    idx = cat.photos.index(a)
    win.grid._select(idx)
    text = win.meta_label.text()
    assert "★★★" in text and "★★★★" not in text   # exactly three
    assert "1899-03-02 14:00:00" in text            # unbounded year, displayed
    assert "60.72125, -135.05685" in text           # signed decimal readout
    assert "the beach" in text                      # caption still present

    # Starred view: count >= 1 keeps every existing truthy consumer working
    win._apply_view("starred", "")
    assert idx in win.grid.display
    assert "Starred: 1 photos" in win.counts_label.text()


def test_status_readout_stashed_original(library: Path) -> None:
    """Status bar shows 'Picasa-saved original kept' chip for a photo whose
    stash copy exists, and nothing for one without (fauxcasa-cam.19)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None
    cat = scan_library(library)
    a = next(p for p in cat.photos if p.rel == "2020-01-01 Trip/a.jpg")
    assert a.stashed_original is not None  # fixture guarantees the pair
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    idx_a = cat.photos.index(a)
    win.grid._select(idx_a)
    assert "Picasa-saved original kept" in win.meta_label.text()

    c = next(p for p in cat.photos if p.rel == "2021-05-05 Picnic/c.jpg")
    idx_c = cat.photos.index(c)
    win.grid._select(idx_c)
    assert "Picasa-saved original kept" not in win.meta_label.text()


def test_status_readout_dims_and_size(library: Path) -> None:
    """Single-photo status bar includes image dimensions and human file size
    when present, and omits both cleanly when dims is None (pre-backfill)
    or size is -1 (unindexed) — fauxcasa-q6l.12."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None
    cat = scan_library(library)
    a = next(p for p in cat.photos if p.rel.endswith("Trip/a.jpg"))
    a.dims = (3648, 2736)
    a.size = 2_200_000
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    idx = cat.photos.index(a)

    # present: dims and size both populated
    win._photo_selected(idx)
    text = win.meta_label.text()
    assert "3648 × 2736" in text
    assert "2.1 MB" in text

    # absent: dims=None, size=-1 — omit cleanly
    a.dims = None
    a.size = -1
    win._photo_selected(idx)
    text2 = win.meta_label.text()
    assert "3648 × 2736" not in text2
    assert " MB" not in text2
    assert " KB" not in text2


def test_viewer_info_line_paints_metadata(library: Path) -> None:
    """The viewer's info bar composes star count + date + coordinates
    without incident (offscreen paint smoke; the text path is the shared
    catalog.format_* formatting the status bar test asserts on)."""
    _offscreen_app()
    from viewer import ViewerPage

    cat = scan_library(library)
    a = next(p for p in cat.photos if p.rel.endswith("Trip/a.jpg"))
    a.star = 5
    a.date_taken = "2009-07-04T13:00:00"
    a.geotag = SYDNEY
    v = ViewerPage(cat, None)
    v.resize(320, 240)
    v.show()
    v.show_photo([cat.photos.index(a)], 0)
    assert not v.grab().isNull()   # paints the bar with all fields present
    v.quiesce()


def test_viewer_info_line_stashed_original(library: Path) -> None:
    """Viewer info line shows 'Picasa-saved original kept' chip for a photo
    with a stash copy, and not for one without (fauxcasa-cam.19)."""
    _offscreen_app()
    from viewer import ViewerPage

    cat = scan_library(library)
    a = next(p for p in cat.photos if p.rel == "2020-01-01 Trip/a.jpg")
    assert a.stashed_original is not None  # fixture guarantees the pair
    c = next(p for p in cat.photos if p.rel == "2021-05-05 Picnic/c.jpg")
    v = ViewerPage(cat, None)
    v.resize(320, 240)
    v.show()
    v.show_photo([cat.photos.index(a)], 0)
    assert "Picasa-saved original kept" in v._info_text(a)
    assert "Picasa-saved original kept" not in v._info_text(c)
    v.quiesce()
