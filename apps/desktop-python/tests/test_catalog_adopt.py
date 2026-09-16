"""Tests for thumbcache.py/main.py adopted-catalog cache binding.

Split from test_tracer.py (fauxcasa-l09); originally lines 10709-11520 of the monolith."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
import pytest
import library as libmod
import thumbcache
from catalog import (
    load_catalog,
    save_catalog,
    scan_library,
    BACKFILL_COMPLETE,
    BACKFILL_IN_PROGRESS,
    BACKFILL_NOT_STARTED,
)
from tracer_helpers import (
    WHITEHORSE,
    _adopted_catalog,
    _days_ago,
    _meta_jpeg,
    _offscreen_app,
    _raw_catalog,
    _write_raw_catalog,
    _xmp_app1,
    make_jpeg,
    write_jpeg_meta,
)


# ---------------------------------------------------------------------------
# Adopt-mode backfill (fauxcasa-cam.12): --thumbs binds a prebuilt fcache
# without running the indexer, so the catalog starts with no identity
# signals (N6 — reconcile blind to in-place edits), ini-only metadata (§4
# tier-1 never applied), and no mtimes (Recently Updated honestly 0).
# thumbcache.backfill_catalog is the read side of the indexer without the
# thumbnail work — read_photo_meta + apply_photo_meta, the SAME factored
# functions build_cache runs — applied in catalog order behind a bounded
# two-reader window so the persisted cursor is a contiguous frontier and a
# killed pass resumes exactly where it left off.
# ---------------------------------------------------------------------------


def test_backfill_matches_indexer_read_side(tmp_path: Path) -> None:
    """The core parity claim: a backfilled adopt-mode catalog is
    indistinguishable from an indexed one across every read-side field —
    identity signals AND the §4 tier-1 precedence merge (in-file caption/
    keywords beat ini, ini survives where the file carries none, EXIF GPS
    beats geotag=, XMP Rating beats bare star=yes) — and the merged result
    persists for the next warm start."""
    root = tmp_path / "lib"
    # a: ini caption/keywords + in-file XMP -> in-file wins
    write_jpeg_meta(root / "f" / "a.jpg",
                    xmp=_xmp_app1("in-file cap", ("ifkw",)))
    # b: ini caption only -> survives the pass untouched
    make_jpeg(root / "f" / "b.jpg")
    # c: in-file EXIF date + GPS + XMP Rating over ini star=yes/geotag=
    _meta_jpeg(root / "f" / "c.jpg",
               date_time_original="1899:03:02 14:00:00",
               gps=WHITEHORSE, rating=3)
    # d: nothing anywhere (signals only)
    make_jpeg(root / "f" / "d.jpg")
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\ncaption=ini cap a\r\nkeywords=inikw\r\n"
        "[b.jpg]\r\ncaption=ini cap b\r\n"
        "[c.jpg]\r\nstar=yes\r\ngeotag=-33.856800,151.215300\r\n")

    ref = scan_library(root)                     # reference: the indexer
    assert thumbcache.build_cache(ref, tmp_path / "ref") is not None

    cat, cat_path = _adopted_catalog(root, tmp_path)
    a = next(p for p in cat.photos if p.name == "a.jpg")
    assert a.caption == "ini cap a"              # the gap: ini tier showing
    assert a.sha256 is None and a.mtime < 0 and a.size < 0

    result = thumbcache.backfill_catalog(cat, cat_path)
    assert result is not None
    assert result.photos == len(cat.photos) and result.workers == 2
    assert cat.backfill_state == BACKFILL_COMPLETE

    for got, want in zip(cat.photos, ref.photos):
        assert got.rel == want.rel
        assert (got.size, got.mtime, got.sha256) == \
            (want.size, want.mtime, want.sha256)
        assert got.caption == want.caption
        assert got.keywords == want.keywords
        assert got.date_taken == want.date_taken
        assert got.geotag == want.geotag
        assert got.star == want.star
    by = {p.name: p for p in cat.photos}
    assert by["a.jpg"].caption == "in-file cap"          # tier-1 applied
    assert by["a.jpg"].keywords == ("ifkw",)
    assert by["b.jpg"].caption == "ini cap b"            # ini fallback kept
    assert by["c.jpg"].star == 3                          # Rating over star=yes
    assert by["c.jpg"].geotag == pytest.approx(WHITEHORSE)  # GPS over geotag=
    assert by["c.jpg"].date_taken == "1899-03-02T14:00:00"  # no year floor
    assert len(by["d.jpg"].sha256) == 64 and by["d.jpg"].mtime >= 0

    # the merged result is durable: the next launch warm-loads it complete
    loaded = load_catalog(cat_path, root)
    assert loaded is not None
    assert loaded.backfill_state == BACKFILL_COMPLETE
    assert next(p for p in loaded.photos
                if p.name == "a.jpg").caption == "in-file cap"
    # ...and a complete catalog's file shape carries no backfill key
    assert "backfill" not in _raw_catalog(cat_path)


def test_backfill_interrupt_resume_and_periodic_persist(
        tmp_path: Path, monkeypatch) -> None:
    """Kill-safety: the pass persists every persist_every photos AND on
    cancel, recording IN_PROGRESS + a contiguous cursor; a relaunch loads
    that catalog and resumes from the cursor, never re-reading the photos
    already applied."""
    import threading

    root = tmp_path / "lib"
    for n in range(6):
        make_jpeg(root / "f" / f"p{n}.jpg")
    cat, cat_path = _adopted_catalog(root, tmp_path)

    saves: list[int] = []
    real_save = thumbcache.save_catalog
    real_save_retrying = thumbcache.save_catalog_retrying

    def counting_save(c, p):
        saves.append(c.backfill_cursor)
        real_save(c, p)

    def counting_save_retrying(c, p, attempts=5, backoff=0.1):
        # terminal (must=True) saves now go through save_catalog_retrying
        saves.append(c.backfill_cursor)
        real_save_retrying(c, p, attempts=attempts, backoff=backoff)

    monkeypatch.setattr(thumbcache, "save_catalog", counting_save)
    monkeypatch.setattr(thumbcache, "save_catalog_retrying", counting_save_retrying)

    stop = threading.Event()
    assert thumbcache.backfill_catalog(
        cat, cat_path, cancel=stop, persist_every=2,
        progress=lambda done, total: stop.set() if done >= 3 else None,
    ) is None                                    # cancelled mid-pass
    # results apply IN ORDER, so the checkpoints are deterministic: the
    # periodic save at 2, then the cancel checkpoint at 3
    assert saves == [2, 3]

    disk = load_catalog(cat_path, root)          # what a relaunch loads
    assert disk is not None
    assert disk.backfill_state == BACKFILL_IN_PROGRESS
    assert disk.backfill_cursor == 3
    assert all(p.sha256 and p.mtime >= 0 for p in disk.photos[:3])
    assert all(p.sha256 is None and p.mtime < 0 for p in disk.photos[3:])

    read: list[str] = []
    real_read = thumbcache.read_photo_meta

    def recording_read(r, photo):
        read.append(photo.rel)
        return real_read(r, photo)

    monkeypatch.setattr(thumbcache, "read_photo_meta", recording_read)
    result = thumbcache.backfill_catalog(disk, cat_path)
    assert result is not None and result.photos == 3   # the tail only
    assert sorted(read) == ["f/p3.jpg", "f/p4.jpg", "f/p5.jpg"]
    assert disk.backfill_state == BACKFILL_COMPLETE
    assert all(p.sha256 for p in disk.photos)
    again = load_catalog(cat_path, root)
    assert again is not None and again.backfill_state == BACKFILL_COMPLETE


def test_backfill_survives_transient_checkpoint_failure(
        tmp_path: Path, monkeypatch) -> None:
    """Windows regression (caught live at photo 96,500 of the 100k
    benchmark run): os.replace onto a catalog.json some reader momentarily
    holds open without FILE_SHARE_DELETE (antivirus, the search indexer,
    any tool peeking at the file) raises a transient PermissionError. A
    PERIODIC checkpoint failing must not abort the multi-minute pass — it
    retries at the next photo — and the terminal save still lands."""
    root = tmp_path / "lib"
    for n in range(6):
        make_jpeg(root / "f" / f"p{n}.jpg")
    cat, cat_path = _adopted_catalog(root, tmp_path)

    calls = [0]
    real_save = thumbcache.save_catalog

    def flaky_save(c, p):
        calls[0] += 1
        if calls[0] == 1:                        # first periodic checkpoint
            raise PermissionError(5, "Access is denied")
        real_save(c, p)

    monkeypatch.setattr(thumbcache, "save_catalog", flaky_save)
    result = thumbcache.backfill_catalog(cat, cat_path, persist_every=2)
    assert result is not None and result.photos == 6   # pass not aborted
    assert calls[0] >= 2                         # ...and saves resumed
    disk = load_catalog(cat_path, root)
    assert disk is not None and disk.backfill_state == BACKFILL_COMPLETE
    assert all(p.sha256 for p in disk.photos)


def test_backfill_worker_cap_two(tmp_path: Path, monkeypatch) -> None:
    """Rate limiting is structural: BACKFILL_WORKERS is 2 (deliberately far
    below INDEX_WORKERS) and the pass never has more than that many reads
    in flight — a bounded submission window, not the indexer's
    fire-everything pool."""
    import threading
    import time as _time

    assert thumbcache.BACKFILL_WORKERS == 2
    root = tmp_path / "lib"
    for n in range(10):
        make_jpeg(root / "f" / f"p{n}.jpg")
    cat, cat_path = _adopted_catalog(root, tmp_path)

    lock = threading.Lock()
    active, peak = [0], [0]
    real_read = thumbcache.read_photo_meta

    def tracking_read(r, photo):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            _time.sleep(0.01)                    # widen the overlap window
            return real_read(r, photo)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(thumbcache, "read_photo_meta", tracking_read)
    assert thumbcache.backfill_catalog(cat, cat_path) is not None
    assert 1 <= peak[0] <= thumbcache.BACKFILL_WORKERS


def test_backfill_pause_parks_readers(tmp_path: Path, monkeypatch) -> None:
    """The low-priority hook: a set pause event parks the pass before any
    read is submitted (and between photos); clearing it lets the pass run
    to completion."""
    import threading
    import time as _time

    root = tmp_path / "lib"
    for n in range(4):
        make_jpeg(root / "f" / f"p{n}.jpg")
    cat, cat_path = _adopted_catalog(root, tmp_path)

    reads: list[str] = []
    real_read = thumbcache.read_photo_meta

    def recording_read(r, photo):
        reads.append(photo.rel)
        return real_read(r, photo)

    monkeypatch.setattr(thumbcache, "read_photo_meta", recording_read)
    pause = threading.Event()
    pause.set()                                  # paused before the start
    out: list = []
    t = threading.Thread(
        target=lambda: out.append(
            thumbcache.backfill_catalog(cat, cat_path, pause=pause)),
        daemon=True)
    t.start()
    _time.sleep(0.3)
    assert reads == [] and t.is_alive()          # parked: nothing read yet
    pause.clear()
    t.join(timeout=15)
    assert not t.is_alive()
    assert out and out[0] is not None and out[0].photos == 4
    assert len(reads) == 4
    assert cat.backfill_state == BACKFILL_COMPLETE


def test_backfill_flips_recently_updated_from_zero(tmp_path: Path) -> None:
    """The PR #41 rider: adopt-mode mtimes are -1 so Recently Updated is
    honestly empty; the backfill fills REAL file mtimes and the collection
    populates through the exact same recent_indices seam."""
    from main import recent_indices

    root = tmp_path / "lib"
    make_jpeg(root / "T" / "fresh.jpg")
    make_jpeg(root / "T" / "stale.jpg")
    os.utime(root / "T" / "fresh.jpg", (_days_ago(1),) * 2)
    os.utime(root / "T" / "stale.jpg", (_days_ago(90),) * 2)
    cat, cat_path = _adopted_catalog(root, tmp_path)

    assert recent_indices(cat, reveal=False) == []     # honest pre-backfill 0
    assert thumbcache.backfill_catalog(cat, cat_path) is not None
    got = [cat.photos[i].rel for i in recent_indices(cat, reveal=False)]
    assert got == ["T/fresh.jpg"]                      # window, not fallback


def test_backfill_state_roundtrip_and_version_gate(tmp_path: Path) -> None:
    """backfill_state/cursor persistence: NOT_STARTED and IN_PROGRESS(cursor)
    round-trip (cursor clamped against hand-edits), COMPLETE writes no key at
    all (an indexer-built catalog's file shape is unchanged), garbage in the
    key degrades to a cold walk, and the v7 version gate rejects a v6 catalog
    (which cannot say whether an adopt catalog was ever backfilled)."""
    import catalog as catmod

    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "b.jpg")
    cat = scan_library(root)
    path = tmp_path / "catalog.json"

    cat.backfill_state = BACKFILL_NOT_STARTED
    save_catalog(cat, path)
    loaded = load_catalog(path, root)
    assert loaded is not None
    assert loaded.backfill_state == BACKFILL_NOT_STARTED
    assert loaded.backfill_cursor == 0

    cat.backfill_state = BACKFILL_IN_PROGRESS
    cat.backfill_cursor = 1
    save_catalog(cat, path)
    loaded = load_catalog(path, root)
    assert loaded is not None
    assert loaded.backfill_state == BACKFILL_IN_PROGRESS
    assert loaded.backfill_cursor == 1
    data = _raw_catalog(path)
    data["backfill"]["cursor"] = 99              # hand-edited overshoot
    _write_raw_catalog(path, data)
    assert load_catalog(path, root).backfill_cursor == 2   # clamped to count

    cat.backfill_state = BACKFILL_COMPLETE
    save_catalog(cat, path)
    assert "backfill" not in _raw_catalog(path)
    loaded = load_catalog(path, root)
    assert loaded is not None
    assert loaded.backfill_state == BACKFILL_COMPLETE

    data = _raw_catalog(path)
    data["backfill"] = {"state": "banana"}       # unknown state string
    _write_raw_catalog(path, data)
    assert load_catalog(path, root) is None
    data["backfill"] = "not-an-object"
    _write_raw_catalog(path, data)
    assert load_catalog(path, root) is None

    # v9 (TGA/PSD stills, fauxcasa-v46.4) through v10 (edit recipes,
    # fauxcasa-cam.15): the exact current value is pinned by the newest
    # version-gate test (test_multiroot_two_root_save_load_roundtrip_v13).
    assert catmod.CATALOG_VERSION >= 9
    cat.backfill_state = BACKFILL_COMPLETE
    save_catalog(cat, path)
    data = _raw_catalog(path)
    data["version"] = 6                          # pre-backfill-state format
    path.write_text(json.dumps(data))  # plain JSON: a version this old never zstd-wrapped
    assert load_catalog(path, root) is None


def test_backfill_persists_report_across_resume(tmp_path: Path) -> None:
    """A cancel/resume cycle must not lose infile_override report entries
    for photos already applied before the cursor (review finding: the
    cursor skips re-processing them, so a report dropped at cancel time is
    gone forever). With report_path supplied, checkpoint() persists
    catalog.report alongside the catalog on both the periodic and the
    terminal save — the entry recorded for photo 0 survives a simulated
    relaunch (fresh catalog load + report re-attach) and the finished
    resume pass."""
    import threading

    from catalog import REPORT_NAME, load_report

    root = tmp_path / "lib"
    # p0: in-file caption conflicts with ini -> one infile_override entry
    write_jpeg_meta(root / "f" / "p0.jpg",
                    xmp=_xmp_app1(caption="file cap"))
    (root / "f" / ".picasa.ini").write_text(
        "[p0.jpg]\r\ncaption=ini cap\r\n")
    make_jpeg(root / "f" / "p1.jpg")
    cat, cat_path = _adopted_catalog(root, tmp_path)
    report_path = tmp_path / REPORT_NAME

    stop = threading.Event()
    assert thumbcache.backfill_catalog(
        cat, cat_path, cancel=stop, persist_every=1, report_path=report_path,
        progress=lambda done, total: stop.set() if done >= 1 else None,
    ) is None  # cancelled after photo 0's checkpoint

    # The checkpoint after photo 0 must have persisted the override entry —
    # not just the catalog.
    on_disk = load_report(report_path)
    overrides = [e for e in on_disk.entries if e.kind == "infile_override"]
    assert len(overrides) == 1 and "caption" in overrides[0].detail

    # Simulate the next launch: fresh catalog load + report re-attach
    # (mirrors main()'s warm-start path), exactly as main.py does.
    disk = load_catalog(cat_path, root)
    assert disk is not None
    disk.report = load_report(report_path)
    assert len(disk.report.entries) == 1  # survived the "relaunch"

    result = thumbcache.backfill_catalog(disk, cat_path,
                                         report_path=report_path)
    assert result is not None and result.photos == 1  # the tail only (p1)
    # p0's entry is still there after the resumed pass completes — it was
    # never re-derived (p0 wasn't re-processed) and never lost.
    final = load_report(report_path)
    final_overrides = [e for e in final.entries if e.kind == "infile_override"]
    assert len(final_overrides) == 1 and "caption" in final_overrides[0].detail


def test_backfill_report_path_none_preserves_old_behavior(
        tmp_path: Path) -> None:
    """The default report_path=None never writes a report file — existing
    callers that don't pass it see no behavior change."""
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    cat, cat_path = _adopted_catalog(root, tmp_path)
    result = thumbcache.backfill_catalog(cat, cat_path)
    assert result is not None
    assert not (tmp_path / "import-report.json").exists()


def test_recent_empty_state_hint_while_backfill_pending(
        tmp_path: Path) -> None:
    """§1 modes-not-modals honesty (the PR #41 rider): while an adopt-mode
    catalog's backfill has not yet filled mtimes, the sidebar's Recently
    Updated says WHY it is empty ('indexing metadata…', not a bare 0) and
    clicking it explains the empty view in the status bar; a COMPLETE
    catalog's empty collection is a bare, final 0 again. cache_dir=None
    keeps the pass itself from starting, so the label logic is tested
    deterministically."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    def item_for(win, kind, key):
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == (kind, key):
                return it.value()
            it += 1
        return None

    root = tmp_path / "lib"
    make_jpeg(root / "T" / "a.jpg")
    cat = scan_library(root)
    cat.backfill_state = BACKFILL_NOT_STARTED
    win = MainWindow(cat, None, cache_dir=None, build_dir=None, adopt=True)
    assert win._backfill_thread is None          # nowhere to persist into

    item = item_for(win, "recent", "")
    assert "indexing metadata" in item.text(0)   # the hint, not (0)
    win._sidebar_clicked(item, 0)
    assert "backfill" in win.statusBar().currentMessage()

    cat.backfill_state = BACKFILL_COMPLETE
    win.statusBar().clearMessage()
    win._refresh_recent_count()
    assert item_for(win, "recent", "").text(0).endswith("(0)")
    win._apply_view("recent", "")
    assert win.statusBar().currentMessage() == ""


def test_mainwindow_adopt_backfill_end_to_end(tmp_path: Path) -> None:
    """The wiring: an adopt-mode MainWindow starts the backfill thread
    (NOT reconcile — a warm start's reconcile is deferred behind it), the
    pass runs against cache_dir/catalog.json, and on completion the sidebar
    refreshes (Recently Updated flips from the indexing hint to a real
    count), the catalog persists COMPLETE with full signals, and the
    deferred reconcile then starts."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import time as _time
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    def item_for(win, kind, key):
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == (kind, key):
                return it.value()
            it += 1
        return None

    root = tmp_path / "lib"
    make_jpeg(root / "T" / "fresh.jpg")
    make_jpeg(root / "T" / "old.jpg")
    os.utime(root / "T" / "fresh.jpg", (_days_ago(1),) * 2)
    os.utime(root / "T" / "old.jpg", (_days_ago(90),) * 2)

    built = thumbcache.build_cache(scan_library(root), tmp_path / "prebuilt")
    cat = scan_library(root)
    cache = thumbcache.load_cache(built.path)
    thumbcache.bind(cache, cat)
    cat.backfill_state = BACKFILL_NOT_STARTED
    cache_dir = tmp_path / "cachedir"
    save_catalog(cat, cache_dir / "catalog.json")  # what main()'s adopt saves

    win = MainWindow(cat, cache, cache_dir=cache_dir, build_dir=None,
                     warm=True, adopt=True)
    assert win._backfill_thread is not None      # backfill, not reconcile
    assert win._reconcile_thread is None
    assert win._reconcile_after_backfill
    assert "indexing metadata" in item_for(win, "recent", "").text(0)

    deadline = _time.time() + 20
    while win._backfill_thread.is_alive() and _time.time() < deadline:
        app.processEvents()
        _time.sleep(0.01)
    assert not win._backfill_thread.is_alive()
    app.processEvents()                          # deliver backfill_done

    assert cat.backfill_state == BACKFILL_COMPLETE
    assert item_for(win, "recent", "").text(0).endswith("(1)")
    disk = load_catalog(cache_dir / "catalog.json", root)
    assert disk is not None
    assert disk.backfill_state == BACKFILL_COMPLETE
    assert all(p.sha256 and p.mtime >= 0 for p in disk.photos)
    assert win._reconcile_thread is not None     # the deferred reconcile ran
    win.shutdown()


def test_cold_scan_nonblocking(tmp_path: Path, monkeypatch) -> None:
    """Non-blocking first run (fauxcasa-q6l.13): main()'s cold non-adopt
    branch hands MainWindow an EMPTY cfg-shaped catalog and calls
    _start_cold_scan after show() — the window must stay responsive/
    paintable while the walk is in flight, index_busy() must read True for
    the whole time _cold_scan_pending is set (this is what keeps a
    scripted --finish-build run from quitting mid-walk), and landing the
    walk (_on_scan_done) must populate the real catalog via reload_data and
    chain straight into the cold BUILD — the scan -> reload -> build gate
    design."""
    app = _offscreen_app()
    import threading
    import time as _time

    import main
    from catalog import Catalog

    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    make_jpeg(root / "b.jpg")
    cfg = libmod.legacy_config(root)

    real_catalog = scan_library(root)
    release = threading.Event()
    calls = []

    def fake_scan_library_config(cfg_, scan_filter, contacts, pal_dir,
                                 exts, db3_dir):
        calls.append(cfg_)
        assert release.wait(5.0), "test never released the fake scan"
        return real_catalog

    monkeypatch.setattr(main, "_scan_library_config",
                        fake_scan_library_config)

    empty = Catalog(root=cfg.roots[0].path, photos=[], folders={},
                    albums={}, roots=list(cfg.roots),
                    library_id=cfg.library_id)
    cache_dir = tmp_path / "cachedir"
    win = main.MainWindow(empty, None, cache_dir=cache_dir, build_dir=None,
                          cfg=cfg)
    win.show()

    # Constructing/showing the window must NOT itself have started a scan
    # or build — main() calls _start_cold_scan explicitly, after show().
    assert not win.index_busy()
    assert win._scan_thread is None

    win._start_cold_scan(cache_dir)
    assert win._cold_scan_pending
    assert win.index_busy()          # folded into index_busy from the start
    assert win.catalog is empty      # not yet swapped — the walk is blocked

    # The window stays responsive while the walk (blocked on `release`) is
    # in flight: the event loop keeps pumping and the window is still the
    # empty placeholder's — nothing here waits on the walk thread.
    for _ in range(5):
        app.processEvents()
    assert win.isVisible()
    assert win._cold_scan_pending     # still pending — release() not yet set
    assert win.catalog is empty

    release.set()
    deadline = _time.time() + 5.0
    while win._cold_scan_pending and _time.time() < deadline:
        app.processEvents()
        _time.sleep(0.01)
    app.processEvents()  # deliver the queued bridge.scan_done signal

    assert not win._cold_scan_pending
    assert win.catalog is real_catalog          # reload_data swapped it in
    assert len(win.catalog.photos) == 2
    assert win._build_thread is not None        # the cold BUILD was chained
    assert calls == [cfg]                       # _scan_library_config args

    win.shutdown()


def test_cold_scan_reapplies_star_overrides(tmp_path: Path,
                                            monkeypatch) -> None:
    """Codex cross-vendor review finding 1: the ctor's own
    apply_star_overrides call only ever touches the EMPTY placeholder
    catalog on the deferred cold path — a stars.json from an earlier run
    (e.g. --rebuild, or an invalidated cache) must still land on the REAL
    catalog once _on_scan_done gets it, same as the old synchronous cold
    path applied it in __init__ directly against the just-scanned catalog."""
    app = _offscreen_app()
    import time as _time

    import starstore
    import main
    from catalog import Catalog

    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    make_jpeg(root / "b.jpg")
    cfg = libmod.legacy_config(root)

    # A prior run's Fauxcasa-local star choice, persisted beside the cache
    # (main()'s cold non-adopt branch never re-walks this on --rebuild —
    # only the photos change, the overlay file survives).
    pre_scan = scan_library(root)
    key = starstore.photo_key(pre_scan.photos[0])
    cache_dir = tmp_path / "cachedir"
    starstore.save_star_overrides(cache_dir, {key: 4})

    real_catalog = scan_library(root)
    assert all(p.star == 0 for p in real_catalog.photos)  # no ini stars

    def fake_scan_library_config(cfg_, scan_filter, contacts, pal_dir,
                                 exts, db3_dir):
        return real_catalog

    monkeypatch.setattr(main, "_scan_library_config",
                        fake_scan_library_config)

    empty = Catalog(root=cfg.roots[0].path, photos=[], folders={},
                    albums={}, roots=list(cfg.roots),
                    library_id=cfg.library_id)
    win = main.MainWindow(empty, None, cache_dir=cache_dir, build_dir=None,
                          cfg=cfg)
    assert win.star_overrides == {key: 4}    # loaded in __init__ as usual
    win.show()
    win._start_cold_scan(cache_dir)

    deadline = _time.time() + 5.0
    while win._cold_scan_pending and _time.time() < deadline:
        app.processEvents()
        _time.sleep(0.01)
    app.processEvents()

    assert win.catalog is real_catalog
    starred = {starstore.photo_key(p): p.star for p in win.catalog.photos
              if p.star}
    assert starred == {key: 4}               # reapplied onto the REAL scan
    win.shutdown()


def test_cold_scan_failure_scripted_run_exits_nonzero(
        tmp_path: Path, monkeypatch, capsys) -> None:
    """Codex cross-vendor review finding 2: the old synchronous cold path
    let a scan exception propagate out of main() and crash the process —
    never a fake READY, always a nonzero exit. A background-thread scan
    failure on the deferred path must reproduce that contract for a
    scripted run: no READY line, nonzero exit — driven in-process the same
    way test_main_reuses_existing_qapplication drives main() (a real
    subprocess can't monkeypatch _scan_library_config to force a
    deterministic, portable failure)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import main

    app = QApplication.instance() or QApplication([])
    assert app is not None

    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")

    def fake_scan_library_config(cfg_, scan_filter, contacts, pal_dir,
                                 exts, db3_dir):
        raise RuntimeError("synthetic scan failure (test)")

    monkeypatch.setattr(main, "_scan_library_config",
                        fake_scan_library_config)

    cache_root = tmp_path / "cr"
    monkeypatch.setattr(sys, "argv", [
        "fauxcasa-tracer", str(root), "--cache-root", str(cache_root),
        "--quit-after-ready", "--timeout", "10"])
    rc = main.main()
    out = capsys.readouterr().out
    assert rc != 0
    assert "READY" not in out
    assert '"event": "ready"' not in out


def test_cold_scan_orphan_ignored_after_shutdown(
        tmp_path: Path, monkeypatch) -> None:
    """Codex cross-vendor review round 3, working repro: a cold scan that
    outlives both a scripted --timeout and shutdown()'s own 5 s join
    leaves _scan_thread alive after main() has already returned this
    window. main() supports reusing the same QApplication across an
    in-process run (test_ready_poll_timer_dies_with_the_window) — if a later
    run restarts the event loop, the orphaned worker's queued scan_done
    can fire into this now-stale window and reload/rebuild against
    deleted Qt objects. shutdown() must set _shut_down (whether its join
    reaped the thread or gave up on it — this test exercises the giving-up
    shape without paying the real 5 s wait, since a monkeypatched join
    timeout would diverge from the actual code path being fixed) and
    _on_scan_done must check it FIRST and be a strict no-op once set —
    this is exactly the guard a signal delivered after shutdown() would
    hit, whether or not the belt-and-braces disconnect also won its race."""
    app = _offscreen_app()
    import main
    from catalog import Catalog

    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    cfg = libmod.legacy_config(root)

    def fake_scan_library_config(cfg_, scan_filter, contacts, pal_dir,
                                 exts, db3_dir):
        raise AssertionError("must never run once shut down")

    monkeypatch.setattr(main, "_scan_library_config",
                        fake_scan_library_config)

    empty = Catalog(root=cfg.roots[0].path, photos=[], folders={},
                    albums={}, roots=list(cfg.roots),
                    library_id=cfg.library_id)
    cache_dir = tmp_path / "cachedir"
    win = main.MainWindow(empty, None, cache_dir=cache_dir, build_dir=None,
                          cfg=cfg)
    win.show()
    assert not win._shut_down

    win.shutdown()          # no scan ever started here: the join is instant,
    assert win._shut_down   # same as the "gave up on a stuck join" outcome —
                             # shutdown() sets the flag unconditionally either way

    # Belt and braces: the signal itself is disconnected too, so a SECOND
    # disconnect attempt returns False (PySide6's signal for "not
    # connected", a RuntimeWarning rather than a raise) — proving
    # shutdown() actually severed it, not just set the flag.
    assert win._bridge.scan_done.disconnect(win._on_scan_done) is False

    # Simulate the orphaned worker's queued scan_done landing AFTER
    # shutdown (the exact race the flag exists for, independent of
    # whether Qt's queued delivery would even still reach a disconnected
    # slot): a real, non-empty catalog arrives — _on_scan_done must still
    # be a strict no-op.
    real_catalog = scan_library(root)
    win._on_scan_done(real_catalog)

    assert win.catalog is empty          # never reloaded
    assert not win._cold_scan_pending    # untouched by the no-op guard's
                                          # early return (shutdown() itself
                                          # never toggled it — no scan ran)
    app.processEvents()                  # nothing queued; must not raise
    win.deleteLater()


def test_mainwindow_backfill_persists_report_and_updates_notes(
        tmp_path: Path) -> None:
    """Review findings (fauxcasa-nu9): _start_backfill threads a real
    report_path into backfill_catalog (reusing the same cache_dir/
    REPORT_NAME the adopt path already saves to at scan time), so an
    infile_override entry survives the pass on disk; and _on_backfill_done
    calls _update_import_notes so the status-bar note appears without
    requiring a relaunch."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import time as _time

    from catalog import REPORT_NAME, load_report
    from PySide6.QtWidgets import QApplication
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    root = tmp_path / "lib"
    # in-file caption conflicts with ini -> one infile_override entry
    write_jpeg_meta(root / "f" / "p.jpg", xmp=_xmp_app1(caption="file cap"))
    (root / "f" / ".picasa.ini").write_text(
        "[p.jpg]\r\ncaption=ini cap\r\n")

    built = thumbcache.build_cache(scan_library(root), tmp_path / "prebuilt")
    cat = scan_library(root)
    cache = thumbcache.load_cache(built.path)
    thumbcache.bind(cache, cat)
    cat.backfill_state = BACKFILL_NOT_STARTED
    cache_dir = tmp_path / "cachedir"
    save_catalog(cat, cache_dir / "catalog.json")

    win = MainWindow(cat, cache, cache_dir=cache_dir, build_dir=None,
                     warm=True, adopt=True)
    assert win._backfill_thread is not None
    assert win.notes_label.text() == ""  # nothing yet: backfill hasn't run

    deadline = _time.time() + 20
    while win._backfill_thread.is_alive() and _time.time() < deadline:
        app.processEvents()
        _time.sleep(0.01)
    assert not win._backfill_thread.is_alive()
    app.processEvents()  # deliver backfill_done -> _on_backfill_done

    # _update_import_notes ran without a relaunch (item A/B of the review).
    assert win.notes_label.isVisibleTo(win)
    assert "1 import note" in win.notes_label.text()

    # report_path was threaded through: the entry is on disk, not just
    # in the live catalog's report.
    on_disk = load_report(cache_dir / REPORT_NAME)
    overrides = [e for e in on_disk.entries if e.kind == "infile_override"]
    assert len(overrides) == 1 and "caption" in overrides[0].detail
    win.shutdown()
