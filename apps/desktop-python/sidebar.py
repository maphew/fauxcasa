"""The folder-tree/albums/people sidebar (fauxcasa-4tu stage 3).

`Sidebar` holds the tree-building/click/context-menu logic that used to
live directly on MainWindow. It does not own the tree widget or the
flat/tree checkbox itself — those stay MainWindow attributes (`win.tree`,
`win._flat_check`, `win._sidebar_panel`), reachable exactly as before, so
every other part of main.py (and every test) that reads them keeps
working unmodified. Sidebar takes the owning MainWindow (`win`) and
operates on its attributes instead; MainWindow keeps one-line delegating
methods with the original names (`_sidebar_clicked`, `_set_folder_sort`,
`_folders_root_menu`, `_folder_sort_menu`, `_refresh_recent_count`,
`_people_counts`, `_sidebar_menu`, `_build_sidebar`, `_rebuild_sidebar`,
`_new_sidebar_tree`)
so `win._x(...)` calls and Qt signal connections that name `self._x` still
resolve.

The module-level tooltip/offline-label helpers below (`_plain_tooltip`,
`_folder_tooltip`, `_offline_root_labels`, `_single_root_offline_message`,
`_OFFLINE_DRIVE_NAME_MAX_CHARS`) moved here too; main.py re-exports the
ones its own non-sidebar code and tests still reach as `main.<name>`.
`ElidingLabel` stays in main.py — it is a MainWindow status-bar widget
(activity/counts/progress/meta/decode-sandbox labels), not sidebar-only.

Preserves the "rebuild by swapping in a fresh tree" behavior exactly
(memory win-qt-tree-rebuild-crash exists for a reason: do not switch to
in-place clear()) — see _rebuild_sidebar's docstring.
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtGui import QActionGroup, QPalette
from PySide6.QtWidgets import (
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
)

from catalog import BACKFILL_COMPLETE, Catalog
import icons
from grid import DEFAULT_SORT_MODE, SORT_MODES, folder_key
from librarystate import save_sort_modes
import theme


def _plain_tooltip(text: str) -> str:
    """Tooltip text that renders LITERALLY (fauxcasa-6vk finding 7).

    QToolTip has no plain-text mode: it hands the string to a QLabel in
    AutoText, so Qt::mightBeRichText decides — and every tooltip below
    carries user-authored catalog text (folder/album descriptions, album
    names, on-disk paths, import-note details). A description of
    "<img src=http://…>" would be INTERPRETED: the markup swallowed, a
    broken-image icon shown, an external URL fetched on hover.

    Escaping alone is not the fix: mightBeRichText only notices "&lt;"
    BEFORE the first newline, and these tooltips are multi-line, so an
    escaped description on line 2 would show its raw entities instead.
    Emit explicit HTML with <br> breaks — unambiguously rich text, so
    every escaped character renders as itself. A string with neither
    '<' nor '&' can never trip mightBeRichText, so it passes through
    untouched (the common case: every path-only tooltip)."""
    if "<" not in text and "&" not in text:
        return text
    return "<html>" + "<br>".join(
        html.escape(line) for line in text.split("\n")) + "</html>"


def _folder_tooltip(path, description: str | None) -> str:
    """Sidebar folder-item tooltip text (fauxcasa-cam.14): the on-disk path
    (path on demand, per q6l.10) followed by the folder's .picasa.ini
    [Picasa] description= on its own line when non-empty. Kept as a pure
    function so all four sidebar shapes (single/multi-root x tree/flat)
    share one formatting rule instead of drifting."""
    tip = str(path)
    if description:
        tip += "\n" + description
    return _plain_tooltip(tip)


def _offline_root_labels(catalog: Catalog) -> list[str]:
    """Sidebar/status-bar badge labels for offline roots (design §12, bead
    .e). Only meaningful once a library actually HAS more than one root —
    a single-root library going offline has no sensible in-app badge (the
    grid/viewer would show nothing at all, and today's main.py never even
    opens a multi-root library — bead .d/.g), so this returns [] whenever
    `len(catalog.roots) <= 1` even if `offline_ids` happens to be
    populated. Kept as a pure function (no Qt types) so the offline-badge
    LOGIC is unit-testable without a QApplication or pixel-level check —
    per bead .e's brief, "test the logic (offline set) rather than
    pixels." Does NOT call refresh_offline_ids() itself: callers
    (load_catalog on open, _reconcile_online_roots on reconcile) already
    keep `offline_ids` current, and re-stat-ing on every sidebar rebuild
    would be surprising I/O in a UI-paint path.

    fauxcasa-hi2 item 4: the owner explicitly RATIFIED this single-root
    suppression on 2026-09-14 rather than lifting it — it is deliberate,
    not an oversight a future reviewer should "fix" by deleting the
    `len(catalog.roots) <= 1` guard. `_reconcile_online_roots` below
    reuses this same function (instead of building its own label list) so
    the two callers can never drift back out of sync. The real gap that
    prompted the review finding — a single-root offline library leaving
    an unexplained empty grid — is closed separately, by
    `_single_root_offline_message`'s status-bar text (hi2 item 5)."""
    if len(catalog.roots) <= 1:
        return []
    return [r.label or r.path.name or str(r.path)
            for r in catalog.offline_roots()]


# One sentence needs to stay readable in a status bar at ordinary window
# widths even when a drive's label/folder name is a long real-world path;
# see _single_root_offline_message's elision below (same middle-ellipsis
# choice ElidingLabel makes for path-shaped text — fauxcasa-a3m).
_OFFLINE_DRIVE_NAME_MAX_CHARS = 40


def _single_root_offline_message(catalog: Catalog) -> str | None:
    """The status-bar sentence for the gap `_offline_root_labels`
    deliberately leaves open (fauxcasa-hi2 item 5): a single-root library
    whose one root is offline shows an empty grid with no badge to
    explain it (item 4 keeps that badge suppressed on purpose — there is
    nothing in the grid to badge). This is the only in-app explanation
    such a user gets, so callers should prefer it over the raw
    `_offline_root_labels` badge text whenever it returns non-None.

    Returns None whenever there is nothing to explain this way: a
    multi-root library (badges are the right surface there — this
    function only ever covers the single-root case) or a single root
    that is not offline.

    Deliberately non-technical wording (matches docs/releases/v0.1.0.md's
    register, not developer phrasing) — never says "root", "catalog",
    "reconcile", or "offline_ids"."""
    if len(catalog.roots) > 1:
        return None
    offline = catalog.offline_roots()
    if not offline:
        return None
    r = offline[0]
    name = r.label or r.path.name or str(r.path)
    if len(name) > _OFFLINE_DRIVE_NAME_MAX_CHARS:
        keep = (_OFFLINE_DRIVE_NAME_MAX_CHARS - 1) // 2
        name = f"{name[:keep]}…{name[-keep:]}"
    return (f'The drive holding these photos, "{name}", isn\'t '
            f'connected right now; plug it back in and Fauxcasa will '
            f'pick up where it left off.')


class Sidebar:
    """Folder/albums/people sidebar tree logic for one MainWindow (`win`).
    A plain object, not a QObject — every widget it builds/wires belongs to
    `win` (win.tree, win._flat_check, win._sidebar_panel), so there is
    nothing here for Qt's object tree to own. See the module docstring for
    the delegating-method contract MainWindow keeps."""

    def __init__(self, win) -> None:
        self.win = win

    def _new_sidebar_tree(self) -> QTreeWidget:
        """Create and wire a fresh sidebar tree. Factored out so a rebuild can
        swap in a brand-new widget rather than clear() the live one."""
        win = self.win
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.itemClicked.connect(win._sidebar_clicked)
        # After the view applies: the tray readout's type/count follow the
        # sidebar selection (q6l.2). Connection order makes this run second.
        tree.itemClicked.connect(
            lambda *_a: win._refresh_tray_readout())
        # Folder context menu (fauxcasa-q6l.11): per-folder sort modes.
        # Wired HERE so every swapped-in rebuild tree (the gfz fresh-widget
        # pattern) carries the menu, not just the first one.
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(win._sidebar_menu)
        return tree

    def _rebuild_sidebar(self) -> None:
        """Repopulate the sidebar after a reveal toggle or reconcile reload.

        Swaps in a freshly built QTreeWidget and defers the old one's
        destruction via deleteLater(), instead of win.tree.clear()+repopulate.
        On the real Windows Qt platform, clear()-ing a QTreeWidget that has a
        current item set intermittently aborts with a native access violation
        in the in-place item teardown (fauxcasa-gfz) — reproducible in a single
        shown window with a running event loop, so it can crash the real app on
        a Show-hidden toggle or a post-reconcile reload, not just the tests.
        Building a new tree and letting the event loop free the old one at a
        safe point sidesteps that teardown path entirely."""
        win = self.win
        old = win.tree
        new = self._new_sidebar_tree()
        win.tree = new
        # Replace the tree inside the sidebar panel (not the splitter) so the
        # flat/tree toggle above it stays in place (fauxcasa-q6l.10).
        win._sidebar_panel.layout().replaceWidget(old, new)
        self._build_sidebar()
        old.deleteLater()

    def _build_sidebar(self) -> None:
        win = self.win
        cat = win.catalog
        reveal = win.grid.reveal
        t = win.tree

        def fcount(folder) -> int:
            return folder.total_count if reveal else folder.photo_count

        all_item = QTreeWidgetItem(
            t, [f"All photos  ({win._shown_count()})"])
        all_item.setData(0, Qt.ItemDataRole.UserRole, ("all", ""))
        starred = sum(
            1 for p in cat.photos if (p.visible or reveal) and p.star)
        star_item = QTreeWidgetItem(t, [f"Starred  ({starred})"])
        star_item.setData(0, Qt.ItemDataRole.UserRole, ("starred", ""))
        star_item.setIcon(0, icons.make_icon("star", theme.STAR))
        # Recently Updated auto-collection (fauxcasa-q6l.7): mtime recency,
        # semantics in recent_indices(). Live count like Starred — rebuilt
        # with the sidebar, plus a cold-build refresh once mtimes exist.
        recent_item = QTreeWidgetItem(t, [self._recent_label()])
        recent_item.setData(0, Qt.ItemDataRole.UserRole, ("recent", ""))
        recent_item.setIcon(0, icons.make_icon("clock", theme.TEXT_MUTED))

        folders_root = QTreeWidgetItem(t, ["Folders"])
        folders_root.setFlags(
            folders_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        folders_root.setIcon(0, icons.make_icon("folder", theme.TEXT))
        # Right-click affordance for the flat/tree toggle (ez2.14: replaces
        # the bare checkbox that used to sit above the tree); data marks
        # this item so _sidebar_menu can tell it apart from a real folder.
        folders_root.setData(0, Qt.ItemDataRole.UserRole, ("folders_root", ""))
        # Flat/tree toggle (fauxcasa-q6l.10): both branches below grow a flat
        # mode alongside their existing tree mode, and both modes now carry
        # a full on-disk-path tooltip (path on demand) on every folder item.
        flat = win._flat_check.isChecked()
        if len(cat.roots) <= 1:
            # Exact one-root passthrough: keep the historical tree shape,
            # labels, item data, and offline badge behavior unchanged.
            for label in _offline_root_labels(cat):
                badge = QTreeWidgetItem(folders_root, [f"{label} (offline)"])
                badge.setFlags(badge.flags() & ~Qt.ItemFlag.ItemIsSelectable
                               & ~Qt.ItemFlag.ItemIsEnabled)
                bf = badge.font(0)
                bf.setItalic(True)
                badge.setFont(0, bf)
            # cat.roots is populated for every load_catalog()/scan_library()
            # call (single-root gets the implicit LEGACY_ROOT_ID entry); the
            # cat.root fallback only matters for a bare test fixture that
            # never went through either.
            root_path = cat.roots[0].path if cat.roots else cat.root
            if flat:
                # Flat mode: alphabetical by leaf name, tooltip carries the
                # full on-disk path (path on demand).
                ordered = sorted(
                    ((rel, folder) for rel, folder in cat.folders.items()
                     if fcount(folder) > 0),
                    key=lambda rf: rf[0].rsplit("/", 1)[-1].lower(),
                )
                for rel, folder in ordered:
                    # rel == "" is the library root itself (photos directly
                    # in the root folder) — label it by on-disk folder name,
                    # matching what multiroot flat does for a root leaf.
                    leaf = rel.split("/")[-1] if rel else root_path.name
                    item = QTreeWidgetItem(
                        folders_root, [f"{leaf}  ({fcount(folder)})"])
                    item.setData(0, Qt.ItemDataRole.UserRole, ("folder", rel))
                    item.setToolTip(
                        0, _folder_tooltip(root_path / rel, folder.description))
                    item.setIcon(0, icons.make_icon("folder", theme.TEXT_MUTED))
            else:
                # Tree mode (default): hierarchical, with full-path tooltips.
                nodes: dict[str, QTreeWidgetItem] = {"": folders_root}

                def node_for(rel: str) -> QTreeWidgetItem:
                    if rel in nodes:
                        return nodes[rel]
                    parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
                    parent = node_for(parent_rel)
                    item = QTreeWidgetItem(parent, [rel.split("/")[-1]])
                    item.setData(0, Qt.ItemDataRole.UserRole, ("folder", rel))
                    item.setToolTip(0, _plain_tooltip(str(root_path / rel)))
                    item.setIcon(0, icons.make_icon("folder", theme.TEXT_MUTED))
                    nodes[rel] = item
                    return item

                for rel, folder in cat.folders.items():
                    if fcount(folder) == 0:
                        continue
                    item = node_for(rel)
                    # node_for() only sets a bare-path tooltip (it doesn't
                    # know the Folder object); recompute here with the
                    # description appended, whether rel is a real subfolder
                    # or "" (node_for("") is the preseeded "Folders" header
                    # standing in for the root folder — keep the path-on-
                    # demand promise there too).
                    item.setToolTip(0, _folder_tooltip(
                        root_path if not rel else root_path / rel,
                        folder.description))
                    item.setText(0, f"{folder.title}  ({fcount(folder)})")
        else:
            # Genuine multiroot: durable manifest order is display order in
            # tree mode; flat mode is alphabetical across every root. Child
            # identities use the catalog's root-qualified convention
            # (folder_key) so duplicate rels across roots never merge.
            roots_by_id = {r.id: r for r in cat.roots}
            if flat:
                # Flat mode: alphabetical by leaf name across all roots.
                # Offline roots remain browseable from cached thumbs (not
                # skipped) — styled with the same italic/dim cue tree mode
                # gives the offline root header, since a flat list has no
                # header row to carry that cue instead.
                def leaf_label(item: tuple[str, object]) -> str:
                    folder = item[1]
                    if folder.rel:
                        return folder.rel.split("/")[-1]
                    root = roots_by_id[folder.root_id]
                    return root.label or root.path.name

                ordered = sorted(
                    ((key, folder) for key, folder in cat.folders.items()
                     if fcount(folder) > 0
                     and folder.root_id in roots_by_id),
                    key=lambda kf: leaf_label(kf).lower(),
                )
                for key, folder in ordered:
                    root = roots_by_id[folder.root_id]
                    if folder.rel:
                        leaf = folder.rel.split("/")[-1]
                        text = f"{leaf}  ({fcount(folder)})"
                    else:
                        label = root.label or root.path.name
                        if folder.root_id in cat.offline_ids:
                            label += " (offline)"
                        text = f"{label}  ({fcount(folder)})"
                    item = QTreeWidgetItem(folders_root, [text])
                    item.setData(0, Qt.ItemDataRole.UserRole, ("folder", key))
                    item.setToolTip(0, _folder_tooltip(
                        root.path / folder.rel, folder.description))
                    item.setIcon(0, icons.make_icon("folder", theme.TEXT_MUTED))
                    if folder.root_id in cat.offline_ids:
                        font = item.font(0)
                        font.setItalic(True)
                        item.setFont(0, font)
                        item.setForeground(0, t.palette().brush(
                            QPalette.ColorGroup.Disabled,
                            QPalette.ColorRole.Text))
            else:
                # Tree mode (default): root labels are the top-level folder
                # nodes; child items carry full-path tooltips too.
                root_nodes: dict[str, QTreeWidgetItem] = {}
                nodes2: dict[tuple[str, str], QTreeWidgetItem] = {}
                for root in cat.roots:
                    label = root.label or root.path.name or str(root.path)
                    if root.id in cat.offline_ids:
                        label += " (offline)"
                    item = QTreeWidgetItem(folders_root, [label])
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                    item.setToolTip(0, _plain_tooltip(str(root.path)))
                    item.setIcon(0, icons.make_icon("folder", theme.TEXT))
                    if root.id in cat.offline_ids:
                        font = item.font(0)
                        font.setItalic(True)
                        item.setFont(0, font)
                        item.setForeground(0, t.palette().brush(
                            QPalette.ColorGroup.Disabled,
                            QPalette.ColorRole.Text))
                    root_nodes[root.id] = item
                    nodes2[(root.id, "")] = item

                def node_for_root(root_id: str, rel: str) -> QTreeWidgetItem:
                    ident = (root_id, rel)
                    if ident in nodes2:
                        return nodes2[ident]
                    parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
                    parent = node_for_root(root_id, parent_rel)
                    item = QTreeWidgetItem(parent, [rel.split("/")[-1]])
                    key = folder_key(cat, root_id, rel)
                    item.setData(0, Qt.ItemDataRole.UserRole, ("folder", key))
                    item.setToolTip(
                        0,
                        _plain_tooltip(str(roots_by_id[root_id].path / rel)))
                    item.setIcon(0, icons.make_icon("folder", theme.TEXT_MUTED))
                    nodes2[ident] = item
                    return item

                for key, folder in cat.folders.items():
                    if fcount(folder) == 0 or folder.root_id not in root_nodes:
                        continue
                    item = node_for_root(folder.root_id, folder.rel)
                    # node_for_root() only sets a bare-path tooltip (it
                    # doesn't know the Folder object, and the root header
                    # nodes created above never see one either); recompute
                    # here with the description appended, using the owning
                    # root's manifest path exactly as the bare-path tooltip
                    # already does.
                    root = roots_by_id[folder.root_id]
                    item.setToolTip(0, _folder_tooltip(
                        root.path if not folder.rel else root.path / folder.rel,
                        folder.description))
                    if folder.rel:
                        item.setText(0, f"{folder.title}  ({fcount(folder)})")
                    else:
                        label = (next(r for r in cat.roots
                                      if r.id == folder.root_id).label
                                 or next(r for r in cat.roots
                                         if r.id == folder.root_id).path.name)
                        if folder.root_id in cat.offline_ids:
                            label += " (offline)"
                        item.setText(0, f"{label}  ({fcount(folder)})")
                        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsSelectable)
                        item.setData(0, Qt.ItemDataRole.UserRole, ("folder", key))
                for item in root_nodes.values():
                    item.setExpanded(True)
        folders_root.setExpanded(True)

        if cat.albums:
            albums_root = QTreeWidgetItem(t, ["Albums"])
            albums_root.setFlags(
                albums_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            albums_root.setIcon(0, icons.make_icon("album", theme.TEXT))
            for uid, album in cat.albums.items():
                # §3: a placeholder (albums= uid with no definition) is
                # never dropped — shown dimmed/italic with a "?" suffix so
                # the gap is visible, not silent (import report has the
                # entry). A .pal-sourced album reads like a real one; its
                # provenance lives in the tooltip.
                suffix = " ?" if album.placeholder else ""
                item = QTreeWidgetItem(
                    albums_root,
                    [f"{album.name}{suffix}  ({len(album.members)})"])
                item.setData(0, Qt.ItemDataRole.UserRole, ("album", uid))
                item.setIcon(0, icons.make_icon("album", theme.TEXT_MUTED))
                if album.placeholder:
                    f = item.font(0)
                    f.setItalic(True)
                    item.setFont(0, f)
                    item.setForeground(0, t.palette().brush(
                        QPalette.ColorGroup.Disabled,
                        QPalette.ColorRole.Text))
                    item.setToolTip(
                        0, "Referenced by albums= lines but defined nowhere "
                           "— placeholder (see import notes)")
                elif album.pal_sourced:
                    item.setToolTip(
                        0, "Album definition from a Picasa2Albums .pal file")
                else:
                    # Regular album: show date and/or description if present.
                    tip_parts = []
                    if album.date:
                        tip_parts.append(album.date)
                    if album.description:
                        tip_parts.append(album.description)
                    if tip_parts:
                        item.setToolTip(
                            0, _plain_tooltip("\n".join(tip_parts)))
            albums_root.setExpanded(True)

        # People (read-only v1 slice, fauxcasa-cam.3): named people with
        # photo counts, clicking filters the grid like an album, plus an
        # explicit "Unnamed faces" affordance for photos carrying
        # suggested/unresolved face regions (N7: the gap is never silent).
        # Counts are live: rebuilt with the sidebar on reveal/reconcile.
        people, unnamed, name_cids = self._people_counts()
        if people or unnamed:
            # db3-rescued people are source-flagged (fauxcasa-cam.7): the
            # name exists only because the §4 rescue importer read it from
            # a machine-local db3 person album — provenance in the tooltip,
            # like a .pal-sourced album's. Rekeyed honestly by contact id,
            # not display name (fauxcasa-7aj.2): two DIFFERENT contacts can
            # share a display name (one ini-named, one db3-rescued), so a
            # name is flagged only when EVERY contact id that resolves to
            # it is db3-rescued — a name also known via a non-db3 contact
            # id must not be tarred with the rescue flag.
            db3_names = {name for name, cids in name_cids.items()
                         if cids and cids <= cat.db3_contacts}
            people_root = QTreeWidgetItem(t, ["People"])
            people_root.setFlags(
                people_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            people_root.setIcon(0, icons.make_icon("person", theme.TEXT))
            for person in sorted(people, key=str.lower):
                item = QTreeWidgetItem(
                    people_root, [f"{person}  ({people[person]})"])
                item.setData(0, Qt.ItemDataRole.UserRole, ("person", person))
                item.setIcon(0, icons.make_icon("person", theme.TEXT_MUTED))
                if person in db3_names:
                    item.setToolTip(
                        0, "Name rescued from the Picasa db3 database — "
                           "no .picasa.ini or contacts.xml names this "
                           "person (see import notes)")
            if unnamed:
                item = QTreeWidgetItem(
                    people_root, [f"Unnamed faces  ({unnamed})"])
                item.setData(0, Qt.ItemDataRole.UserRole, ("unnamed", ""))
            people_root.setExpanded(True)

    def _recent_label(self) -> str:
        """The sidebar text for Recently Updated. Honesty hint (the PR #41
        rider, fauxcasa-cam.12): while an adopt-mode backfill has not yet
        filled real mtimes, an empty collection says WHY it is empty
        instead of a bare 0 — the count appears once the backfill lands
        (or immediately, if some already-backfilled photos qualify)."""
        win = self.win
        n = len(win._recent_indices())
        if n == 0 and win.catalog.backfill_state != BACKFILL_COMPLETE:
            return "Recently Updated  (indexing metadata…)"
        return f"Recently Updated  ({n})"

    def _refresh_recent_count(self) -> None:
        """A COLD build fills Photo.mtime in-place only after the sidebar was
        first built (its count then read 0) — update just that item's label.
        setText on a live item is safe; only clear()+repopulate of a tree
        with a current item is the fauxcasa-gfz crash path."""
        win = self.win
        it = QTreeWidgetItemIterator(win.tree)
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole) == ("recent", ""):
                it.value().setText(0, self._recent_label())
                return
            it += 1

    def _people_counts(self) -> tuple[dict[str, int], int, dict[str, set]]:
        """Per-person photo tallies for the current reveal state: {name:
        photo count} over named faces, plus how many photos carry at least
        one unnamed (suggested/unresolved) face. Photo counts, not face
        counts — a photo with the same person tagged twice counts once.
        Also returns {name: {contact ids that resolved to it}} — the
        db3-rescued sidebar flag (fauxcasa-7aj.2) needs this to rekey by
        contact id rather than display name, since two DIFFERENT contacts
        can share a display name."""
        win = self.win
        reveal = win.grid.reveal
        people: dict[str, int] = {}
        name_cids: dict[str, set] = {}
        unnamed = 0
        for p in win.catalog.photos:
            if not (p.visible or reveal) or not p.faces:
                continue
            names = {n for _rect, _cid, n in p.faces if n}
            for n in names:
                people[n] = people.get(n, 0) + 1
            for _rect, cid, n in p.faces:
                if n:
                    name_cids.setdefault(n, set()).add(cid)
            if any(n is None for _rect, _cid, n in p.faces):
                unnamed += 1
        return people, unnamed, name_cids

    def _sidebar_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        win = self.win
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None:
            return
        if data[0] == "folders_root":
            # Header row (ez2.14: now carries data so the right-click menu
            # can find it) — unselectable, and a click on it must not reset
            # the active view to All photos the way a bare "unknown kind"
            # would via _apply_view's else branch.
            return
        # Track the active view so a Show-hidden toggle can preserve it
        # (fauxcasa-x1l). On a real click Qt has already made this the
        # current item; set it explicitly so a programmatic call agrees.
        win.tree.setCurrentItem(item)
        win.search.blockSignals(True)
        win.search.clear()
        win.search.blockSignals(False)
        win._apply_view(*data)
        win.grid.setFocus()

    # ---------- per-folder sort modes (fauxcasa-q6l.11) ----------

    def _sidebar_menu(self, point) -> None:
        """Right-click on the sidebar: FOLDER items get the sort-mode menu
        (spec §5 per-folder sort; the manual mode is blocked on the db3
        oracle fixture and absent). The Folders root gets the flat/tree
        toggle (ez2.14: moved off the bare checkbox that used to sit above
        the tree; the View menu's "Flat Folders" action is the other,
        kept in sync via the shared _flat_check state). Every other item
        kind — albums keep membership order, auto-collections keep catalog
        order — has no menu, which is the folder-scoped contract made
        visible."""
        win = self.win
        item = win.tree.itemAt(point)
        if item is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        if data[0] == "folders_root":
            menu = self._folders_root_menu()
            menu.exec(win.tree.viewport().mapToGlobal(point))
            return
        if data[0] != "folder":
            return
        menu = self._folder_sort_menu(data[1])
        menu.exec(win.tree.viewport().mapToGlobal(point))

    def _folders_root_menu(self) -> QMenu:
        """Build (without exec'ing — mirrors _folder_sort_menu's seam) the
        Folders-root context menu: one checkable Flat Folders action, kept
        in sync with the View menu's identical action via the shared
        win._flat_check state (both toggling it fires _toggle_folder_view
        exactly once each way)."""
        win = self.win
        menu = QMenu(win.tree)
        act = menu.addAction("Flat Folders")
        act.setCheckable(True)
        act.setChecked(win._flat_check.isChecked())
        act.toggled.connect(win._flat_check.setChecked)
        return menu

    def _folder_sort_menu(self, rel: str) -> QMenu:
        """Build (without exec'ing — the seam tests drive) the context menu
        for one folder: a checkable, mutually exclusive action per sort
        mode, the folder's current mode checked."""
        win = self.win
        menu = QMenu(win.tree)
        menu.addSection("Sort by")
        group = QActionGroup(menu)
        current = win.grid.sort_modes.get(rel, DEFAULT_SORT_MODE)
        for mode in SORT_MODES:
            act = menu.addAction(mode.capitalize())
            act.setCheckable(True)
            act.setChecked(mode == current)
            act.setData(mode)
            group.addAction(act)
            act.triggered.connect(
                lambda _checked=False, m=mode: self._set_folder_sort(rel, m))
        return menu

    def _set_folder_sort(self, rel: str, mode: str) -> None:
        """Apply + persist one folder's sort mode. The mode reshapes the
        folder-grouped default view only, so 'apply immediately' means:
        when that view is showing (no search, All-photos/folder selection),
        rebuild it and bring the re-sorted folder into view; an active
        search/album/starred view keeps its own order by design and picks
        the mode up on the next return to the folder view."""
        win = self.win
        if mode == DEFAULT_SORT_MODE:
            win.grid.sort_modes.pop(rel, None)
        else:
            win.grid.sort_modes[rel] = mode
        save_sort_modes(win.state_dir, win.grid.sort_modes)
        if win.search.text().strip():
            return
        kind, key = win._selected_view()
        if kind in ("all", "folder"):
            win._apply_view(kind, key)
            win.grid.scroll_to_folder(rel)
