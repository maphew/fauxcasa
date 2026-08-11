"""Photo metadata inspector panel (fauxcasa-q6l.25) — the acceptance
criteria's "panel associated with the selected/current photo" that shows
keywords plus useful available EXIF/XMP and file fields, handles missing
metadata cleanly, updates on selection/navigation, and is accessible from
both the gallery and the individual-photo (viewer) surfaces.

Scope decision (spec §"do not expand"): CATALOG-ONLY data. This panel
reads fields already resident on the in-memory `Photo` record
(catalog.py:172-242) — nothing here opens a file. The grid surface never
reads originals on selection (N4); the inspector keeps that contract.
On-demand full-EXIF dumps (reading the original bytes for fields the
catalog does not carry) are a follow-up, out of scope for this bead.

`InspectorPanel` is a pure DISPLAY widget — no catalog mutation, no
timers, no threads, no file I/O. The owner (MainWindow) decides WHEN to
call `set_photo()` / `set_many()` / `set_none()`; the panel only renders
whatever it is handed. Album-UID -> display-name resolution also happens
in the owner (it already has the catalog's album map for the sidebar,
main.py:1889-1917) so this module stays catalog-shape-agnostic.

Rows are OMITTED entirely when their backing value is absent (no dashes,
no placeholders) — that is the "handles missing metadata cleanly"
behavior the bead asks for; see `_rows_for` for the exact per-row rule.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from catalog import Photo, format_date_taken, format_geotag

PANEL_WIDTH = 280  # sizeHint only — the owning splitter owns actual size.

_NAME_STYLE = "color: #888;"
_STATE_STYLE = "color: #888; padding: 12px;"


def _human_size(n: int) -> str:
    """KB/MB/GB with one decimal (spec); plain bytes below 1 KB. Videos
    are in scope (Photo.media), so the GB tier is real, not theoretical.
    Caller guarantees n >= 0 (negative = unindexed, filtered out before
    here)."""
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024:.1f} GB"


def _rows_for(photo: Photo, album_names: list[str]) -> list[tuple[str, str]]:
    """The ordered (label, value) pairs to render for one photo — spec's
    "Rows to show, in order, omitting any row whose value is absent"."""
    rows: list[tuple[str, str]] = []
    rows.append(("Name", photo.name))

    folder_val = photo.folder
    if photo.root_id:
        # Multiroot only (LEGACY_ROOT_ID == "" is the default single-root
        # id, never shown) — disambiguates two roots' same-rel folders.
        folder_val = f"[{photo.root_id}] {folder_val}".rstrip()
    if folder_val:
        # Library-root photos have folder == "" (catalog.py) — omit the
        # row rather than render an empty value (same omit rule as the
        # metadata rows below).
        rows.append(("Folder", folder_val))

    ext = Path(photo.name).suffix.lstrip(".").upper()
    rows.append(("Type", f"{photo.media} ({ext})" if ext else photo.media))

    if photo.dims is not None:
        w, h = photo.dims
        rows.append(("Dimensions", f"{w} × {h}"))

    if photo.size >= 0:  # -1 = unindexed (spec)
        rows.append(("Size", _human_size(photo.size)))

    if photo.mtime >= 0:  # -1 = unindexed (spec)
        stamp = datetime.fromtimestamp(photo.mtime)
        rows.append(("Modified", stamp.strftime("%Y-%m-%d %H:%M:%S")))

    if photo.date_taken:
        rows.append(("Taken", format_date_taken(photo.date_taken)))

    if photo.star:
        rows.append(("Star", "★" * min(photo.star, 5)))

    if photo.caption:
        rows.append(("Caption", photo.caption))

    if photo.keywords:
        # Plain comma-joined text (spec) — deliberately NOT the status
        # bar's "#hashtag" spelling (main._photo_selected); this is a
        # values panel, not a search-affordance readout.
        rows.append(("Keywords", ", ".join(photo.keywords)))

    if photo.geotag is not None:
        rows.append(("Location", format_geotag(photo.geotag)))

    if photo.faces:
        named = [name for _rect, _cid, name in photo.faces if name]
        unnamed = sum(1 for _rect, _cid, name in photo.faces if not name)
        parts = list(named)
        if unnamed:
            parts.append(f"+{unnamed} unnamed")
        rows.append(("People", ", ".join(parts)))

    if album_names:
        rows.append(("Albums", ", ".join(album_names)))

    edit_parts = []
    if photo.has_edits:
        edit_parts.append("edited")
    if photo.stashed_original is not None:
        edit_parts.append("Picasa-saved original kept")
    if edit_parts:
        rows.append(("Edited", "; ".join(edit_parts)))

    return rows


class InspectorPanel(QWidget):
    """Pure display widget: `set_photo()` / `set_many()` / `set_none()`
    are the whole API — the owner drives all three from its existing
    selection/navigation plumbing (main.py, no new signals needed)."""

    def __init__(self, parent=None):
        super().__init__(parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)

        body = QWidget()
        scroll.setWidget(body)
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(10, 10, 10, 10)
        body_lay.setSpacing(6)

        # Empty/multi state text (set_none / set_many) — a single label,
        # shown instead of the row form.
        self._state_label = QLabel("No photo selected")
        self._state_label.setStyleSheet(_STATE_STYLE)
        self._state_label.setWordWrap(True)
        body_lay.addWidget(self._state_label)

        # Row form (set_photo) — name-label + word-wrapped, selectable
        # value-label pairs, one row per present field (_rows_for).
        self._form_widget = QWidget()
        self._form = QFormLayout(self._form_widget)
        self._form.setContentsMargins(0, 0, 0, 0)
        self._form.setSpacing(4)
        self._form.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._form.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapAllRows)
        body_lay.addWidget(self._form_widget)
        self._form_widget.hide()

        body_lay.addStretch(1)

    def sizeHint(self) -> QSize:  # noqa: N802 — Qt override
        return QSize(PANEL_WIDTH, 400)

    # ---------- API ----------

    def set_none(self) -> None:
        """Empty state: nothing selected."""
        self._show_state("No photo selected")

    def set_many(self, count: int) -> None:
        """Multi-select state: no per-photo rows, just the count."""
        self._show_state(f"{count} photos selected")

    def set_photo(self, photo: Photo, *, album_names: list[str],
                  position: str | None = None) -> None:
        """Populate from a single Photo. `album_names` are resolved
        display names for `photo.albums` UIDs — resolution is the
        owner's job (it already holds the catalog's album map for the
        sidebar); this module never looks a UID up itself. `position`
        (e.g. a future "N/M" readout) is accepted for signature
        stability but not yet rendered — the row list above is
        exhaustive for this bead."""
        self._state_label.hide()
        self._clear_rows()
        for name, value in _rows_for(photo, album_names):
            name_label = QLabel(name)
            name_label.setStyleSheet(_NAME_STYLE)
            value_label = QLabel(value)
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            self._form.addRow(name_label, value_label)
        self._form_widget.show()

    # ---------- internals ----------

    def _show_state(self, text: str) -> None:
        self._clear_rows()
        self._form_widget.hide()
        self._state_label.setText(text)
        self._state_label.show()

    def _clear_rows(self) -> None:
        while self._form.rowCount():
            self._form.removeRow(0)
