"""Tests for main.py Recent view.

Split from test_tracer.py (fauxcasa-l09); originally lines 6158-6381 of the monolith."""

from __future__ import annotations

import os
from pathlib import Path
import thumbcache
from catalog import (
    scan_library,
)
from tracer_helpers import (
    _days_ago,
    _jump_grid,
    make_jpeg,
)


# ---------------------------------------------------------------------------
# M1 browse: the Recently Updated auto-collection (fauxcasa-q6l.7) and the
# jump-to-folder/end buttons beside the grid scrollbar (fauxcasa-q6l.9).
# Recency = FILE MTIME (the read-only app's honest proxy for "updated");
# semantics live on main.recent_indices. Jump stepping uses the grid's
# group y-offsets: prev from mid-group snaps to the CURRENT group's top
# first (Picasa behavior), then to the prior group.
# ---------------------------------------------------------------------------


def test_recent_indices_mtime_window_from_disk(tmp_path: Path) -> None:
    """End to end: os.utime sets controlled file mtimes, the indexer
    (build_cache) captures them into Photo.mtime, and recent_indices
    selects exactly the photos modified within the RECENT_DAYS window —
    hidden ones only under reveal."""
    from main import recent_indices

    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "fresh.jpg")
    make_jpeg(root / "Trip" / "stale.jpg")
    make_jpeg(root / "Picnic" / "fresh2.jpg")
    make_jpeg(root / "Picnic" / "secret.jpg")
    (root / "Picnic" / ".picasa.ini").write_text(
        "[secret.jpg]\r\nhidden=yes\r\n")
    os.utime(root / "Trip" / "fresh.jpg", (_days_ago(1),) * 2)
    os.utime(root / "Trip" / "stale.jpg", (_days_ago(90),) * 2)
    os.utime(root / "Picnic" / "fresh2.jpg", (_days_ago(2),) * 2)
    os.utime(root / "Picnic" / "secret.jpg", (_days_ago(3),) * 2)

    cat = scan_library(root)
    assert all(p.mtime < 0 for p in cat.photos)   # unindexed: no signal yet
    assert recent_indices(cat, reveal=False) == []  # ...and honestly empty
    assert thumbcache.build_cache(cat, tmp_path / "cache") is not None

    rels = lambda idxs: sorted(cat.photos[i].rel for i in idxs)  # noqa: E731
    assert rels(recent_indices(cat, reveal=False)) == [
        "Picnic/fresh2.jpg", "Trip/fresh.jpg"]
    assert rels(recent_indices(cat, reveal=True)) == [
        "Picnic/fresh2.jpg", "Picnic/secret.jpg", "Trip/fresh.jpg"]


def test_recent_indices_empty_window_falls_back_to_most_recent_k(
        monkeypatch, tmp_path: Path) -> None:
    """A library untouched for months still gets a useful collection: with
    nothing inside the window, the K most recently modified photos stand
    in (catalog order), never photos without an mtime signal."""
    import main as main_mod
    from main import recent_indices

    root = tmp_path / "lib"
    for n in range(4):
        make_jpeg(root / "Old" / f"p{n}.jpg")
    cat = scan_library(root)
    # Direct signal injection (the disk->mtime path is covered above):
    # all far older than RECENT_DAYS, distinct, newest NOT in index order.
    for p, days in zip(cat.photos, (90, 40, 70, 60)):
        p.mtime = int(_days_ago(days))

    monkeypatch.setattr(main_mod, "RECENT_FALLBACK_K", 2)
    got = recent_indices(cat, reveal=False)
    assert got == sorted(got)                       # catalog order
    assert sorted(cat.photos[i].rel for i in got) == [
        "Old/p1.jpg", "Old/p3.jpg"]                 # the 2 newest by mtime
    # An entirely unindexed catalog (mtime -1 everywhere, e.g. adopt-mode)
    # yields an honest empty set even through the fallback.
    for p in cat.photos:
        p.mtime = -1
    assert recent_indices(cat, reveal=False) == []


def test_recent_sidebar_item_click_filters_and_counts(tmp_path: Path) -> None:
    """The sidebar's Recently Updated item carries a live count and clicking
    it filters the grid via set_filter, like Starred; reveal recomputes the
    view in place (x1l) and _refresh_recent_count updates the label once a
    cold build fills mtimes in."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
    from main import MainWindow

    app = QApplication.instance() or QApplication([])
    assert app is not None

    def item_for(win, kind: str, key: str):
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == (kind, key):
                return it.value()
            it += 1
        return None

    root = tmp_path / "lib"
    make_jpeg(root / "Trip" / "fresh.jpg")
    make_jpeg(root / "Trip" / "stale.jpg")
    make_jpeg(root / "Trip" / "secret.jpg")
    (root / "Trip" / ".picasa.ini").write_text(
        "[secret.jpg]\r\nhidden=yes\r\n")
    cat = scan_library(root)
    by_rel = {p.rel: p for p in cat.photos}
    by_rel["Trip/fresh.jpg"].mtime = int(_days_ago(1))
    by_rel["Trip/stale.jpg"].mtime = int(_days_ago(90))
    by_rel["Trip/secret.jpg"].mtime = int(_days_ago(2))

    win = MainWindow(cat, None, cache_dir=None, build_dir=None)
    item = item_for(win, "recent", "")
    assert item is not None and item.text(0).endswith("(1)")

    win._sidebar_clicked(item, 0)
    assert win.grid.filter_label == "Recently Updated"
    assert [cat.photos[i].rel for i in win.grid.display] == ["Trip/fresh.jpg"]

    # Reveal keeps the active view and recomputes it: the hidden-but-recent
    # photo joins; the rebuilt sidebar's count text follows.
    win.reveal_box.setChecked(True)
    assert win.grid.filter_label == "Recently Updated"
    assert sorted(cat.photos[i].rel for i in win.grid.display) == [
        "Trip/fresh.jpg", "Trip/secret.jpg"]
    assert item_for(win, "recent", "").text(0).endswith("(2)")
    win.reveal_box.setChecked(False)
    assert [cat.photos[i].rel for i in win.grid.display] == ["Trip/fresh.jpg"]

    # Cold-build label refresh: a catalog indexed AFTER the sidebar was
    # built (counts read as-of-construction) gets its count fixed in place —
    # setText on the live item, never a clear()+repopulate (fauxcasa-gfz).
    by_rel["Trip/stale.jpg"].mtime = int(_days_ago(0.5))
    win._refresh_recent_count()
    assert item_for(win, "recent", "").text(0).endswith("(2)")


def test_grid_current_group_index_bisect(tmp_path: Path) -> None:
    g = _jump_grid(tmp_path)
    sb = g.verticalScrollBar()
    tops = [grp.y for grp in g.groups]
    assert tops[0] == 0 and tops[0] < tops[1] < tops[2]
    for gi, top in enumerate(tops):
        sb.setValue(top)                      # exactly at a header
        assert g.current_group_index() == gi
        sb.setValue(top + 5)                  # a bit into the group
        assert g.current_group_index() == gi
    sb.setValue(tops[1] - 1)                  # last row of the group before
    assert g.current_group_index() == 0


def test_grid_jump_folder_stepping_across_boundaries(tmp_path: Path) -> None:
    """next walks group top -> group top; prev from MID-group first snaps
    to the current group's own top (Picasa behavior), THEN to the prior
    group; both are no-ops at their respective ends."""
    g = _jump_grid(tmp_path)
    sb = g.verticalScrollBar()
    tops = [grp.y for grp in g.groups]

    sb.setValue(0)
    g.jump_next_folder()
    assert sb.value() == tops[1]
    g.jump_next_folder()
    assert sb.value() == tops[2]
    g.jump_next_folder()                      # inside the last group: no-op
    assert sb.value() == tops[2]

    sb.setValue(tops[2] + 40)                 # mid-group...
    g.jump_prev_folder()
    assert sb.value() == tops[2]              # ...its own top first
    g.jump_prev_folder()
    assert sb.value() == tops[1]              # then the prior group
    g.jump_prev_folder()
    assert sb.value() == tops[0] == 0
    g.jump_prev_folder()                      # top of the first group: no-op
    assert sb.value() == 0


def test_grid_jump_top_end_and_buttons(tmp_path: Path) -> None:
    """jump_to_end/top hit the scrollbar's extremes, and the scrollbar-side
    button cluster drives the same primitives (clicked wiring)."""
    g = _jump_grid(tmp_path)
    sb = g.verticalScrollBar()
    tops = [grp.y for grp in g.groups]

    g.jump_to_end()
    assert sb.value() == sb.maximum() > 0
    g.jump_to_top()
    assert sb.value() == 0

    g.btn_end.click()
    assert sb.value() == sb.maximum()
    g.btn_prev_folder.click()                 # end is mid-last-group here
    assert sb.value() == tops[2]
    g.btn_next_folder.click()                 # no next group: no-op
    assert sb.value() == tops[2]
    g.btn_top.click()
    assert sb.value() == 0
    g.btn_next_folder.click()
    assert sb.value() == tops[1]
    # The cluster must never steal the grid's keyboard focus (triage keys).
    from PySide6.QtCore import Qt as _Qt
    for b in (g.btn_top, g.btn_prev_folder, g.btn_next_folder, g.btn_end):
        assert b.focusPolicy() == _Qt.FocusPolicy.NoFocus
