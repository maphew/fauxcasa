"""Tests for grid.py header play glyph and search upgrades (multi-word AND, -term negation, folder names).

Split from test_tracer.py (fauxcasa-l09); originally lines 4994-5484 of the monolith."""

from __future__ import annotations

import os
import sys
from pathlib import Path
import thumbcache
from catalog import (
    scan_library,
)
from tracer_helpers import (
    APP_DIR,
    _big_library,
    _bound_cache,
    _header_click,
    _hits,
    _offscreen_app,
    _press,
    _search_win,
    _two_folder_library,
    make_jpeg,
)


# ---- grid header play glyph (fauxcasa-q6l.16) --------------------------------


def test_grid_header_glyph_click_emits_play_group(tmp_path: Path) -> None:
    """Click on the play triangle in a group header emits play_group with
    the matching folder key; clicking the label area of the same header
    does NOT emit the signal (fauxcasa-q6l.16)."""
    _offscreen_app()
    from grid import GridView, HEADER_H, HEADER_PLAY_W, PAD

    root = tmp_path / "lib"
    _two_folder_library(root)
    cat = scan_library(root)

    g = GridView()
    g.resize(400, 640)
    g.show()
    g.set_data(cat, None)
    assert len(g.groups) == 2

    emitted: list[str] = []
    g.play_group.connect(emitted.append)

    w = g.viewport().width()
    top = g.verticalScrollBar().value()   # 0 (not scrolled)

    # First group header: viewport y = 0
    glyph0 = g._header_glyph_rect(g.groups[0].y - top, w)
    _header_click(g, glyph0.center().x(), glyph0.center().y())
    assert emitted == [g.groups[0].folder], "glyph click must emit play_group"

    # Click the label side of the SAME header — must NOT emit
    emitted.clear()
    _header_click(g, float(PAD + 4), float(HEADER_H // 2))
    assert emitted == [], "label-area click must not emit play_group"

    # Second group header
    emitted.clear()
    y_vp_g1 = g.groups[1].y - top
    glyph1 = g._header_glyph_rect(y_vp_g1, w)
    _header_click(g, glyph1.center().x(), glyph1.center().y())
    assert emitted == [g.groups[1].folder]


def test_grid_header_glyph_click_pinned_sticky_header(tmp_path: Path) -> None:
    """The other half of _header_glyph_at's two-path hit-test: when the
    viewport is scrolled PAST a group's own in-flow header, that header
    pins to the viewport top (_sticky) and the glyph click must still be
    recognized via the sticky branch, not the in-flow-headers loop —
    fauxcasa-q6l.16 / fauxcasa-wqi.4 (the existing
    test_grid_header_glyph_click_emits_play_group never scrolls, so top
    stays 0 and _sticky(0) always returns None; the sticky branch of
    _header_glyph_at was therefore never exercised)."""
    _offscreen_app()
    from grid import GridView

    root = tmp_path / "lib"
    for i in range(12):                     # enough rows to scroll within
        make_jpeg(root / "folder_a" / f"img{i}.jpg")
    make_jpeg(root / "folder_b" / "img_b.jpg")
    cat = scan_library(root)

    g = GridView()
    g.resize(400, 640)
    g.show()
    g.set_data(cat, None)
    assert len(g.groups) == 2
    g0, g1 = g.groups

    # Scroll well past group 0's own header, but nowhere near group 1's —
    # group 0's header must be the pinned/sticky one, fully pushed to the
    # viewport top (push == 0).
    top = g0.y + 100
    assert top < g1.y - 1  # still safely inside group 0's content, not g1's
    g.verticalScrollBar().setValue(top)
    st = g._sticky(top)
    assert st is not None and st[0] is g0 and st[1] == 0, \
        "test setup must actually produce a pinned header to exercise " \
        "the sticky branch"

    emitted: list[str] = []
    g.play_group.connect(emitted.append)

    w = g.viewport().width()
    glyph = g._header_glyph_rect(st[1], w)   # y_vp = push = 0
    _header_click(g, glyph.center().x(), glyph.center().y())
    assert emitted == [g0.folder], \
        "glyph click on the PINNED header must emit play_group"


def test_mainwindow_play_group_starts_slideshow_for_group(
        tmp_path: Path) -> None:
    """_play_group starts the slideshow over THAT group's items from
    position 0; a second call with a different key replays that group;
    an unknown key is a no-op (fauxcasa-q6l.16)."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow

    root = tmp_path / "lib"
    _two_folder_library(root)
    cat = scan_library(root)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    ga = next(g for g in win.grid.groups if "folder_a" in g.folder)
    gb = next(g for g in win.grid.groups if "folder_b" in g.folder)

    # Play folder_a
    win._play_group(ga.folder)
    assert win._slideshow is not None and win._slideshow.isFullScreen()
    assert list(win._slideshow.display) == ga.items
    assert win._slideshow.pos == 0
    _press(win._slideshow, Qt.Key.Key_Escape)

    # Play folder_b — same surface, different display set
    win._play_group(gb.folder)
    assert win._slideshow.isFullScreen()
    assert list(win._slideshow.display) == gb.items
    assert win._slideshow.pos == 0
    _press(win._slideshow, Qt.Key.Key_Escape)

    # Unknown folder key is a silent no-op
    win._play_group("no-such-folder")
    assert win._slideshow is None or win._slideshow.isHidden()


# ---- search upgrades: multi-word AND, -term negation, folder names --------
# (fauxcasa-q6l.6) §5: instant search over filenames, captions, keywords and
# folder names, with '-term' negation. Positive terms AND together (each may
# match a different field of the same photo); any '-term' hit excludes the
# photo; a lone '-' (a negation still being typed) is ignored. People-name
# search joins the same haystack once faces land (see the haystack() parts
# list in main.MainWindow._search_changed).


def test_search_multi_word_and(search_library: Path) -> None:
    """Multiple terms AND together — each must match somewhere on the same
    photo, fields may differ per term — instead of the old single-substring
    reading where 'sunset ocean' had to appear verbatim, space included."""
    win = _search_win(search_library)

    win.search.setText("sunset ocean")         # filename AND keyword
    assert _hits(win) == {"sunset.jpg"}
    win.search.setText("beach golden")         # folder AND caption
    assert _hits(win) == {"sunset.jpg"}
    assert "Search" in win.counts_label.text()
    win.search.setText("sunset neon")          # terms hit different photos
    assert _hits(win) == set()


def test_search_negation(search_library: Path) -> None:
    """'-term' excludes any photo it matches, whatever the field; a
    negation-only query stands alone (Picasa's all-photos hack was exactly
    a match-nothing negation search)."""
    win = _search_win(search_library)

    win.search.setText("beach -dunes")         # folder hits minus a filename
    assert _hits(win) == {"sunset.jpg"}
    win.search.setText("beach -ocean")         # ...minus a keyword hit
    assert _hits(win) == {"dunes.jpg"}
    win.search.setText("-city")                # negation-only: the rest
    assert _hits(win) == {"sunset.jpg", "dunes.jpg"}
    win.search.setText("-nosuchterm")          # excludes nothing -> all
    assert _hits(win) == {"sunset.jpg", "dunes.jpg", "market.jpg",
                          "street.jpg"}


def test_search_folder_name(search_library: Path) -> None:
    """A term matching a folder's display title or any rel-path segment
    pulls that folder's photos into the flat result set — a nested folder's
    photos are also reached through their parent's segment."""
    win = _search_win(search_library)

    win.search.setText("beach")
    assert _hits(win) == {"sunset.jpg", "dunes.jpg"}
    win.search.setText("osaka")                # the nested folder's own name
    assert _hits(win) == {"street.jpg"}
    win.search.setText("city")                 # parent segment: nested too
    assert _hits(win) == {"market.jpg", "street.jpg"}


def test_search_negated_folder(search_library: Path) -> None:
    win = _search_win(search_library)

    win.search.setText("-beach")
    assert _hits(win) == {"market.jpg", "street.jpg"}
    win.search.setText(".jpg -city")           # everything minus a subtree
    assert _hits(win) == {"sunset.jpg", "dunes.jpg"}
    win.search.setText("sun -beach")           # positive vetoed by folder
    assert _hits(win) == set()


def test_search_case_insensitive(search_library: Path) -> None:
    """Both positive and negative terms match case-insensitively against
    every field (filename, caption, keyword, folder)."""
    win = _search_win(search_library)

    win.search.setText("BEACH Golden")
    assert _hits(win) == {"sunset.jpg"}
    win.search.setText("OcEaN")
    assert _hits(win) == {"sunset.jpg"}
    win.search.setText("beach -DUNES")
    assert _hits(win) == {"sunset.jpg"}
    win.search.setText("OSAKA")
    assert _hits(win) == {"street.jpg"}


def test_search_degenerate_queries(search_library: Path) -> None:
    """Empty/whitespace queries and a lone '-' (a negation still being
    typed) fall back to the unfiltered All-photos view instead of blanking
    the grid; a trailing lone '-' inside a real query is simply ignored."""
    win = _search_win(search_library)
    all_names = {"sunset.jpg", "dunes.jpg", "market.jpg", "street.jpg"}

    for q in ("", "   ", "-", " - ", "- -"):
        win.search.setText("beach")            # a real filter first...
        win.search.setText(q)                  # ...then the degenerate query
        assert _hits(win) == all_names, repr(q)
        assert "All photos" in win.counts_label.text(), repr(q)

    win.search.setText("beach -")              # half-typed negation: ignored
    assert _hits(win) == {"sunset.jpg", "dunes.jpg"}
    assert "Search" in win.counts_label.text()


def test_grid_decodes_dpr_scaled_v2_level(tmp_path: Path) -> None:
    """fauxcasa-q7m: the grid's decode worker reads the v2 level chosen by the
    DPR-scaled native edge, then caps the tile to that edge. At native 256
    (devicePixelRatio 1) it reads the 256 level — the legacy primary, a no-op;
    at native 512 (a 2x display) it reads the 512 level so a hi-DPI tile is
    sharp. The cap holds the tile at exactly the device footprint."""
    import queue as _queue
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    sys.path.insert(0, str(APP_DIR))
    from grid import GridView

    root = tmp_path / "lib"
    _big_library(root)               # land.jpg 600x400 (idx 0), port.jpg (idx 1)
    cat = scan_library(root)
    v2 = thumbcache.load_cache(thumbcache.build_cache(
        cat, tmp_path / "c", levels=[512, 256, 128]).path)
    assert v2.levels == [512, 256, 128]

    app = QApplication.instance() or QApplication([])
    assert app is not None
    g = GridView()
    g.set_data(cat, v2)

    def decode(idx: int, native: int):
        g._tile_native = native
        g.generation += 1
        g.wanted = frozenset({idx})
        with g.pending_lock:
            g.pending.discard(idx)
        g.tiles.pop(idx, None)
        g._request(idx)              # a daemon worker decodes onto g.done
        for _ in range(200):         # bounded wait (~10s worst case)
            try:
                gen, di, img = g.done.get(timeout=0.05)
            except _queue.Empty:
                continue
            if gen == g.generation and di == idx:
                return img
        raise AssertionError("decode did not complete")

    # idx 0 == land.jpg 600x400: 512 level caps the long edge to 512x341, the
    # 256 level to 256x171. The long edge equals the chosen level -> proves
    # which level the worker read. rotate=0, so dims are not transposed.
    big = decode(0, 512)
    assert big is not None and max(big.width(), big.height()) == 512
    small = decode(0, 256)
    assert small is not None and max(small.width(), small.height()) == 256


def test_grid_v1_cache_falls_back_to_only_level(tmp_path: Path) -> None:
    """fauxcasa-q7m: a v1 cache has only the 256 level, so even a hi-DPI native
    edge (512) reads it via best_level's largest-available fallback — the grid
    never asks a v1 cache for a level it doesn't have, it just stays soft."""
    import queue as _queue
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    sys.path.insert(0, str(APP_DIR))
    from grid import GridView

    root = tmp_path / "lib"
    _big_library(root)
    cat = scan_library(root)
    v1 = thumbcache.load_cache(thumbcache.build_cache(cat, tmp_path / "c").path)
    assert v1.levels == [256]

    app = QApplication.instance() or QApplication([])
    g = GridView()
    g.set_data(cat, v1)
    g._tile_native = 512             # pretend a 2x display
    g.generation += 1
    g.wanted = frozenset({0})
    with g.pending_lock:
        g.pending.discard(0)
    g.tiles.pop(0, None)
    g._request(0)
    img = None
    for _ in range(200):
        try:
            gen, di, im = g.done.get(timeout=0.05)
        except _queue.Empty:
            continue
        if gen == g.generation and di == 0:
            img = im
            break
    assert img is not None and max(img.width(), img.height()) == 256


def test_refresh_tile_native_dpr_and_invalidation(monkeypatch) -> None:
    """fauxcasa-q7m: _refresh_tile_native scales the native edge by
    devicePixelRatio, floors at TILE_NATIVE, and invalidates (forcing a
    re-decode at the new level) ONLY when the edge changes — a steady ratio
    costs an int compare, moving to a 2x monitor drops stale 256 tiles."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    sys.path.insert(0, str(APP_DIR))
    import grid as gridmod
    from grid import GridView

    app = QApplication.instance() or QApplication([])
    assert app is not None
    g = GridView()

    monkeypatch.setattr(g, "devicePixelRatioF", lambda: 1.0)
    g._tile_native = 0               # force the first refresh to set it
    g._refresh_tile_native()
    assert g._tile_native == gridmod.TILE_NATIVE          # 256, the v1/dpr1 base

    # steady DPR -> no invalidation: a seeded tile and generation survive
    g.tiles[7] = [None, 0, 0]
    gen = g.generation
    g._refresh_tile_native()
    assert g._tile_native == gridmod.TILE_NATIVE
    assert g.generation == gen and 7 in g.tiles

    # move to a 2x display -> native 512, tiles invalidated for re-decode
    monkeypatch.setattr(g, "devicePixelRatioF", lambda: 2.0)
    g._refresh_tile_native()
    assert g._tile_native == 2 * gridmod.TILE_NATIVE      # 512
    assert g.generation == gen + 1 and 7 not in g.tiles

    # fractional ratio rounds; sub-1 (rare) stays floored at TILE_NATIVE
    monkeypatch.setattr(g, "devicePixelRatioF", lambda: 1.5)
    g._refresh_tile_native()
    assert g._tile_native == round(gridmod.TILE_NATIVE * 1.5)   # 384
    monkeypatch.setattr(g, "devicePixelRatioF", lambda: 0.5)
    g._refresh_tile_native()
    assert g._tile_native == gridmod.TILE_NATIVE          # never below the base


def test_pick_decode_edge_bands_by_zoom(monkeypatch) -> None:
    """fauxcasa-q6l.27: pick_decode_edge is the pure rule behind
    _refresh_tile_native's banded pick. Headline case: min zoom (64) at
    the box's native dpr 1.25 needs only 80 device px/tile, so the 128
    level suffices -- not the always-512 dpr*TILE_NATIVE edge the old
    zoom-independent rule picked for every zoom (the measured 10x
    first-paint slowdown this bead fixes)."""
    import grid as gridmod
    from grid import pick_decode_edge

    levels = [512, 256, 128]

    # min zoom, native dpr 1.25 -> 80 device px -> smallest sufficient
    # level is 128 (today's bug picked 512 here).
    assert pick_decode_edge(64, 1.25, levels, None) == 128
    # mid zoom -> 200 device px -> 256.
    assert pick_decode_edge(160, 1.25, levels, None) == 256
    # default/max zoom -> 320 device px -> 512, unchanged from today.
    assert pick_decode_edge(256, 1.25, levels, None) == 512
    # v1 cache (single level): always that level, at every zoom/dpr --
    # the fauxcasa-q7m no-op invariant this bead must not disturb.
    assert pick_decode_edge(64, 1.0, [256], None) == 256
    assert pick_decode_edge(256, 1.0, [256], None) == 256
    # no cache bound yet (thumbs is None): falls back to the old DPR-only
    # cap, exactly reproducing pre-bead _refresh_tile_native behavior.
    assert pick_decode_edge(64, 1.25, [], None) == max(
        gridmod.TILE_NATIVE, round(gridmod.TILE_NATIVE * 1.25))

    # --- hysteresis around the 128/256 boundary ---------------------
    # Currently on the 256 band; want_px eases down to just above the 128
    # boundary (within DECODE_EDGE_HYSTERESIS, 6%) -- stays on 256 so a
    # single zoom-slider pixel can't flip the decode level back and forth.
    assert pick_decode_edge(100, 1.25, levels, 256) == 256   # want=125, boundary=128, 125>=128*0.94
    # Comfortably below the boundary (more than 6%) -> flips down to 128.
    assert pick_decode_edge(90, 1.25, levels, 256) == 128    # want=113 < 128*0.94=120.32
    # No current band recorded (e.g. first paint): no hysteresis to apply,
    # picks the natural (smallest sufficient) level directly.
    assert pick_decode_edge(100, 1.25, levels, None) == 128
    # Zooming back UP always takes the larger level immediately -- hysteresis
    # only holds the larger band on the way DOWN, never delays picking up
    # detail on the way up.
    assert pick_decode_edge(160, 1.25, levels, 128) == 256


def test_grid_set_zoom_reads_banded_v2_level(tmp_path: Path, monkeypatch) -> None:
    """fauxcasa-q6l.27, widget-level: set_zoom on a v2 cache must move the
    decode band with the new zoom (not just DPR) -- min zoom at the box's
    native dpr 1.25 now reads the 128 level, proven by the ACTUAL decoded
    image size (same technique as test_grid_decodes_dpr_scaled_v2_level)."""
    import queue as _queue
    _offscreen_app()
    from grid import GridView

    root = tmp_path / "lib"
    _big_library(root)                    # land.jpg 600x400 (idx 0)
    cat, v2 = _bound_cache(tmp_path, root, levels=[512, 256, 128])
    assert v2.levels == [512, 256, 128]

    g = GridView()
    monkeypatch.setattr(g, "devicePixelRatioF", lambda: 1.25)
    g.set_data(cat, v2)

    g.set_zoom(64)                        # min zoom
    assert g._tile_native == 128          # banded pick, not the old 512 edge

    g.generation += 1
    g.wanted = frozenset({0})
    with g.pending_lock:
        g.pending.discard(0)
    g.tiles.pop(0, None)
    g._request(0)
    img = None
    for _ in range(200):                  # bounded wait (~10s worst case)
        try:
            gen, di, im = g.done.get(timeout=0.05)
        except _queue.Empty:
            continue
        if gen == g.generation and di == 0:
            img = im
            break
    assert img is not None and max(img.width(), img.height()) == 128
