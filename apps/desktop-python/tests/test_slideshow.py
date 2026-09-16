"""Tests for slideshow.py play button and full-screen timed loop.

Split from test_tracer.py (fauxcasa-l09); originally lines 4610-4991 of the monolith."""

from __future__ import annotations

from pathlib import Path
import thumbcache
from catalog import (
    scan_library,
)
from tracer_helpers import (
    _offscreen_app,
    _press,
    _pump,
    _show_library,
    _spin,
    make_jpeg,
)


# ---- M1 slideshow: play button + full-screen timed loop (fauxcasa-q6l.3) --
# SlideshowPage rides ViewerPage's rendering (instant preview + async
# original) and adds the playback loop: timer advance with wrap-around,
# Space pause/resume, manual nav that keeps playing, Esc exit, and a
# dwell-time prefetch of the next original. MainWindow's ▶ Play action
# plays the CURRENT display set full-screen and Esc returns to exactly
# the prior grid/viewer state. All offscreen-safe: timers are driven with
# short test delays through processEvents, never wall-clock sleeps alone.


def test_slideshow_starts_fullscreen_and_plays(tmp_path: Path) -> None:
    """start() goes full-screen over the given display set at the given
    position, playing (timer running, not paused), with the transient
    control hint up."""
    app = _offscreen_app()
    from slideshow import SLIDE_DELAY_MS, SlideshowPage
    cat = scan_library(_show_library(tmp_path))
    show = SlideshowPage(cat, None, delay_ms=60_000)   # no advance in-test
    assert show._timer.interval() == 60_000            # test delay honored
    assert SlideshowPage(cat, None)._timer.interval() == SLIDE_DELAY_MS
    display = list(range(len(cat.photos)))
    show.start(display, 1)
    assert show.isFullScreen()
    assert show.display == display and show.pos == 1
    assert show._timer.isActive() and not show.paused
    assert show._hint_visible
    _ = app


def test_slideshow_advances_on_timer_and_wraps(tmp_path: Path) -> None:
    """The dwell timer steps through the display set in order and WRAPS at
    the end (the corpus documents no Picasa end-of-show behavior, so the
    loop is the chosen convention — slideshow.py module docstring)."""
    app = _offscreen_app()
    from slideshow import SlideshowPage
    cat = scan_library(_show_library(tmp_path))
    show = SlideshowPage(cat, None, delay_ms=20)
    display = list(range(len(cat.photos)))
    seen: list[int] = []
    show.photo_shown.connect(seen.append)
    show.start(display, 0)
    assert _spin(app, lambda: len(seen) >= 5), seen
    # initial show + 4 timer advances: a b c -> wrap -> a b
    assert seen[:5] == [display[0], display[1], display[2],
                        display[0], display[1]]


def test_slideshow_space_pauses_and_resumes(tmp_path: Path) -> None:
    """Space stops the dwell timer (no advance while paused); a second
    Space resumes and the show advances again."""
    app = _offscreen_app()
    from PySide6.QtCore import Qt
    from slideshow import SlideshowPage
    cat = scan_library(_show_library(tmp_path))
    show = SlideshowPage(cat, None, delay_ms=20)
    seen: list[int] = []
    show.photo_shown.connect(seen.append)
    show.start(list(range(len(cat.photos))), 0)
    _press(show, Qt.Key.Key_Space)
    assert show.paused and not show._timer.isActive()
    shown_while_paused = len(seen)
    _pump(app, 0.15)                     # several delays' worth of time
    assert len(seen) == shown_while_paused    # ...and no advance happened
    _press(show, Qt.Key.Key_Space)
    assert not show.paused and show._timer.isActive()
    assert _spin(app, lambda: len(seen) > shown_while_paused)


def test_slideshow_manual_nav_wraps_and_keeps_playing(tmp_path: Path) -> None:
    """Left/Right and J/K navigate with wrap-around WITHOUT stopping
    playback (the dwell restarts); while paused they navigate but stay
    paused."""
    app = _offscreen_app()
    from PySide6.QtCore import Qt
    from slideshow import SlideshowPage
    cat = scan_library(_show_library(tmp_path))
    show = SlideshowPage(cat, None, delay_ms=60_000)   # manual nav only
    show.start(list(range(len(cat.photos))), 0)
    last = len(cat.photos) - 1
    _press(show, Qt.Key.Key_Right)
    assert show.pos == 1 and show._timer.isActive() and not show.paused
    _press(show, Qt.Key.Key_J)                         # J = forward, as viewer
    assert show.pos == 2
    _press(show, Qt.Key.Key_Right)                     # wrap forward
    assert show.pos == 0
    _press(show, Qt.Key.Key_Left)                      # wrap backward
    assert show.pos == last
    _press(show, Qt.Key.Key_K)                         # K = back, as viewer
    assert show.pos == last - 1
    assert show._timer.isActive() and not show.paused  # still playing
    _press(show, Qt.Key.Key_Space)                     # pause...
    _press(show, Qt.Key.Key_Right)                     # ...nav while paused
    assert show.pos == last and show.paused
    assert not show._timer.isActive()                  # stays paused
    _ = app


def test_slideshow_esc_exits_and_stops(tmp_path: Path) -> None:
    """Esc stops the timer, hides the surface, and emits closed with the
    current catalog index (the ViewerPage closed contract)."""
    app = _offscreen_app()
    from PySide6.QtCore import Qt
    from slideshow import SlideshowPage
    cat = scan_library(_show_library(tmp_path))
    show = SlideshowPage(cat, None, delay_ms=60_000)
    closed: list[int] = []
    show.closed.connect(closed.append)
    display = list(range(len(cat.photos)))
    show.start(display, 2)
    _press(show, Qt.Key.Key_Escape)
    assert closed == [display[2]]
    assert not show._timer.isActive()
    assert show.isHidden()
    _ = app


def test_slideshow_prefetch_makes_advance_a_pure_swap(tmp_path: Path) -> None:
    """During the dwell the NEXT photo's original decodes off-thread; the
    advance then swaps it in instantly — image present, no loading state,
    no preview flash — instead of starting a fresh async load."""
    app = _offscreen_app()
    from PySide6.QtCore import Qt
    from slideshow import SlideshowPage
    cat = scan_library(_show_library(tmp_path))
    show = SlideshowPage(cat, None, delay_ms=60_000)   # advance manually
    display = list(range(len(cat.photos)))
    show.start(display, 0)

    def prefetched_next() -> bool:
        with show._prefetch_lock:
            got = show._prefetched
        key = (id(show.catalog), display[1])
        return got is not None and got[0] == key and got[1] is not None

    assert _spin(app, prefetched_next)
    _press(show, Qt.Key.Key_Right)
    # The swap is synchronous: the original is up BEFORE any event pumping.
    assert show.pos == 1
    assert show.image is not None and not show.image.isNull()
    assert not show.loading and show.preview is None


def test_slideshow_prefetch_failure_falls_back_to_async(
        tmp_path: Path) -> None:
    """An undecodable next photo is remembered as a FAILED prefetch (None);
    the advance then takes the normal ViewerPage async path and degrades to
    the viewer's could-not-decode state — never a crash, never a stall."""
    app = _offscreen_app()
    from PySide6.QtCore import Qt
    from slideshow import SlideshowPage
    root = tmp_path / "lib"
    make_jpeg(root / "show" / "a.jpg")
    (root / "show" / "bad.jpg").write_bytes(b"not a jpeg at all")
    make_jpeg(root / "show" / "c.jpg")
    cat = scan_library(root)
    by_rel = {p.rel: i for i, p in enumerate(cat.photos)}
    display = [by_rel["show/a.jpg"], by_rel["show/bad.jpg"],
               by_rel["show/c.jpg"]]
    show = SlideshowPage(cat, None, delay_ms=60_000)
    show.start(display, 0)
    assert _spin(app, lambda: show._prefetched is not None)
    key = (id(show.catalog), display[1])
    assert show._prefetched == (key, None)              # tried, failed
    _press(show, Qt.Key.Key_Right)
    assert show.pos == 1 and show.image is None        # async path taken
    assert _spin(app, lambda: not show.loading)        # decode fails soft
    assert show.image is None                          # "could not decode"


def test_slideshow_prefetch_ignored_after_catalog_swap(
        tmp_path: Path) -> None:
    """slideshow._prefetched (fauxcasa-ez2.12 finding 4): a reconcile
    catalog swap must not let a stale decoded image from the OLD catalog
    be served for an index in the NEW catalog. Prefetch for index 1, swap
    self.catalog to a new Catalog object, and confirm _take_prefetched no
    longer serves the pre-swap decode."""
    import copy
    app = _offscreen_app()
    from slideshow import SlideshowPage
    cat = scan_library(_show_library(tmp_path))
    show = SlideshowPage(cat, None, delay_ms=60_000)
    display = list(range(len(cat.photos)))
    show.start(display, 0)

    def prefetched_next() -> bool:
        with show._prefetch_lock:
            got = show._prefetched
        key = (id(show.catalog), display[1])
        return got is not None and got[0] == key and got[1] is not None

    assert _spin(app, prefetched_next)

    # Simulate a reconcile swap: a new Catalog object, same indices.
    show.catalog = copy.copy(cat)

    assert show._take_prefetched(display[1]) is None


def test_slideshow_start_and_exit_clear_stale_prefetch(
        tmp_path: Path) -> None:
    """start() and _exit() drop any leftover _prefetched entry (and age out
    an in-flight job via the serial) so a decode queued before a catalog
    swap can never land in the fresh session."""
    app = _offscreen_app()
    from slideshow import SlideshowPage
    cat = scan_library(_show_library(tmp_path))
    show = SlideshowPage(cat, None, delay_ms=60_000)
    display = list(range(len(cat.photos)))

    with show._prefetch_lock:
        show._prefetched = ((id(cat), display[1]), None)
    show.start(display, 0)
    with show._prefetch_lock:
        assert show._prefetched is None

    def prefetched_next() -> bool:
        with show._prefetch_lock:
            got = show._prefetched
        key = (id(show.catalog), display[1])
        return got is not None and got[0] == key and got[1] is not None

    assert _spin(app, prefetched_next)
    show._exit()
    with show._prefetch_lock:
        assert show._prefetched is None


def test_mainwindow_play_action_plays_current_view_and_esc_restores(
        library: Path) -> None:
    """The toolbar ▶ Play action starts a full-screen slideshow over the
    grid's CURRENT display set from the selected photo; Esc tears the
    surface down and the browser beneath is exactly as it was."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    display_before = list(win.grid.display)
    win.grid._select(display_before[1])                # current photo
    win.play_action.trigger()
    show = win._slideshow
    assert show is not None and show.isFullScreen()
    assert show.display == display_before              # the current view...
    assert show.pos == 1                               # ...from the selection
    assert show._timer.isActive()
    _press(show, Qt.Key.Key_Escape)
    assert show.isHidden() and not show._timer.isActive()    # stopped
    assert win.pages.currentWidget() is win.pages.widget(0)   # still browser
    assert list(win.grid.display) == display_before    # view untouched
    assert win.grid.current == display_before[1]       # selection untouched


def test_mainwindow_play_from_search_album_and_starred_views(
        library: Path) -> None:
    """Play acts on whatever the grid is showing: a search result set, an
    album's members, and the Starred set — not always All photos — and an
    EMPTY view is a no-op (no blank show, no crash)."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)

    def play_and_close() -> list[int]:
        win.play_action.trigger()
        assert win._slideshow is not None and win._slideshow.isFullScreen()
        shown = list(win._slideshow.display)
        _press(win._slideshow, Qt.Key.Key_Escape)
        assert win._slideshow.isHidden()
        return shown

    win.search.setText("beach")                        # a.jpg's caption
    filtered = list(win.grid.display)
    assert len(filtered) == 1
    assert play_and_close() == filtered
    assert win.search.text() == "beach"                # search state survives

    win.search.clear()
    uid = "deadbeefdeadbeefdeadbeefdeadbeef"
    win._apply_view("album", uid)                      # album view
    members = list(cat.albums[uid].members)
    assert list(win.grid.display) == members
    assert play_and_close() == members

    win._apply_view("starred", "")                     # starred view
    starred = list(win.grid.display)
    assert len(starred) == 1
    assert play_and_close() == starred

    win.search.setText("no-such-photo-anywhere")       # empty display set
    assert win.grid.display == []
    win.play_action.trigger()                          # no-op: nothing to show
    assert win._slideshow is None or win._slideshow.isHidden()


def test_mainwindow_play_from_viewer_returns_to_viewer(
        library: Path) -> None:
    """Play while the single-photo viewer is up starts at the viewer's
    photo, and Esc returns to the viewer page (the exact prior state), not
    the grid."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    display = list(win.grid.display)
    win._open_viewer(display[1], display, 1)
    assert win.pages.currentWidget() is win.viewer
    win.play_action.trigger()
    show = win._slideshow
    assert show is not None and show.pos == 1          # the viewer's photo
    _press(show, Qt.Key.Key_Escape)
    assert show.isHidden()
    assert win.pages.currentWidget() is win.viewer     # back to the viewer
    assert win.viewer.current_index() == display[1]    # on the same photo


def test_mainwindow_slideshow_surface_reused_and_repointed(
        library: Path, tmp_path: Path) -> None:
    """The slideshow surface is ONE lasting instance (never deleted
    mid-run — its decode threads must not race a widget teardown,
    fauxcasa-gfz): a later Play reuses it, re-pointed at the catalog/cache
    a reconcile swapped in; and a reconcile landing DURING a show closes
    it (its display indices belong to the old catalog, as with the viewer
    page)."""
    _offscreen_app()
    from PySide6.QtCore import Qt
    from main import MainWindow
    cat = scan_library(library)
    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    win.play_action.trigger()
    first = win._slideshow
    assert first is not None and first.catalog is cat
    _press(first, Qt.Key.Key_Escape)

    cat2 = scan_library(library)                       # the reconcile swap
    cache2 = thumbcache.load_cache(
        thumbcache.build_cache(cat2, tmp_path / "c2").path)
    thumbcache.bind(cache2, cat2)
    win.reload_data(cat2, cache2)
    win.play_action.trigger()
    assert win._slideshow is first                     # reused, not recreated
    assert first.catalog is cat2 and first.thumbs is cache2   # re-pointed
    assert first.isFullScreen()

    cat3 = scan_library(library)                       # reconcile mid-show...
    win.reload_data(cat3, cache2)
    assert first.isHidden()                            # ...closes the show
    assert not first._timer.isActive()
