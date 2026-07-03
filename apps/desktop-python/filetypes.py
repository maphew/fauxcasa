"""Per-extension include/exclude for the library walk (fauxcasa-v46.4).

Spec §5 Formats: "per-extension include/exclude panel" — Picasa's
Tools > Options > File Types, distilled. Three pieces live here so
main.py only grows the menu entry and the wiring:

- Persistence: the user's choice is stored as the EXCLUDED set (not the
  included one), per library, under the "exclude_exts" key of the same
  config.json that remembers the last-opened library (main._config_path
  — one file, beside the per-library cache dirs). Storing exclusions
  means newly supported extensions default to INCLUDED when the app
  grows its EXTS set, which is what a user who never opened the panel
  expects.

- Cache identity: exts_cache_key folds the choice into the cache-dir
  variant EXACTLY like ScanFilter.cache_key (catalog.py) — "" when
  nothing is excluded (so default configs keep their existing cache
  dirs and warm starts), a deterministic string otherwise. The walk
  rule is load-bearing for cache-order parity (catalog.py module doc),
  so a changed extension set MUST mean a different cache dir; and
  because the key is derived from the (sorted) excluded names alone,
  toggling an extension off and back on returns to the SAME warm cache.

- The panel: FileTypesDialog, a plain modal QDialog. JUDGMENT under the
  modes-not-modals principle (§1): this is one-shot CONFIGURATION with
  a cache-identity consequence, not a browsing workflow — a dialog that
  asks once and gets out of the way is the honest shape, like the
  first-run library picker. Applying a change relaunches the app (the
  Open... pattern) so main() stays the only scan/cache orchestrator.

scripts/make-thumbcache.py mirrors the exclusion with --exclude-exts so
an adopt-mode (--thumbs) cache can be built to match a panel choice.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from catalog import EXTS
from rawload import RAW_EXTS
from videoload import VIDEO_EXTS

# Display groups for the panel, in EXTS-lockstep by construction: the
# stills group is whatever EXTS carries beyond the RAW and video mirrors.
STILL_EXTS = frozenset(EXTS) - RAW_EXTS - VIDEO_EXTS

# The config.json key. The file itself is main._config_path's — this
# module only reads/updates this one key, preserving everything else.
CONFIG_KEY = "exclude_exts"
CONFIG_NAME = "config.json"


def effective_exts(excluded: set[str] | frozenset[str]) -> frozenset[str]:
    """The extension set the walk should use: EXTS minus the exclusions.
    Unknown names in `excluded` (a stale config after an EXTS rename)
    subtract nothing and are ignored everywhere, fail-soft."""
    return frozenset(EXTS) - set(excluded)


def exts_cache_key(excluded: set[str] | frozenset[str]) -> str:
    """Cache-identity fragment for an extension choice — the
    ScanFilter.cache_key shape: "" when the walk is the default (every
    supported extension included), so existing cache dirs keep binding;
    else a deterministic string over the sorted, EXTS-known exclusions,
    so the same choice always maps to the same cache dir and toggling
    off-and-back-on returns to the SAME warm cache."""
    ex = sorted(set(excluded) & set(EXTS))
    if not ex:
        return ""
    return "exts:no=" + ",".join(ex)


def _config_file(cache_root: Path) -> Path:
    # Mirror of main._config_path (kept trivial there; a shared import
    # would be circular — main.py imports this module).
    return cache_root / CONFIG_NAME


def load_excluded_exts(cache_root: Path, library: Path) -> set[str]:
    """The persisted exclusions for `library`, or an empty set when the
    config file, the key, or this library's entry is absent or garbage —
    the panel is a convenience, never a gate (the _remembered_library
    posture). Names are normalized to lowercase dotted form and filtered
    to EXTS so a stale entry can never exclude an unknown extension."""
    try:
        data = json.loads(_config_file(cache_root).read_text())
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    per_lib = data.get(CONFIG_KEY)
    if not isinstance(per_lib, dict):
        return set()
    entry = per_lib.get(str(library.resolve()))
    if not isinstance(entry, list):
        return set()
    out = set()
    for e in entry:
        if isinstance(e, str):
            e = e.strip().lower()
            if e and not e.startswith("."):
                e = "." + e
            if e in EXTS:
                out.add(e)
    return out


def save_excluded_exts(cache_root: Path, library: Path,
                       excluded: set[str] | frozenset[str]) -> bool:
    """Persist `library`'s exclusions into config.json, preserving every
    other key (the remembered library, other libraries' entries). Same
    atomic per-process-temp + os.replace discipline as
    main._remember_library, and the same best-effort posture: False on a
    write failure (the caller decides how loud to be), never an
    exception — a garbage config file on disk just gets rebuilt."""
    cfg = _config_file(cache_root)
    try:
        data = json.loads(cfg.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    per_lib = data.get(CONFIG_KEY)
    if not isinstance(per_lib, dict):
        per_lib = {}
    key = str(library.resolve())
    ex = sorted(set(excluded) & set(EXTS))
    if ex:
        per_lib[key] = ex
    else:
        per_lib.pop(key, None)  # default choice = no entry at all
    if per_lib:
        data[CONFIG_KEY] = per_lib
    else:
        data.pop(CONFIG_KEY, None)
    tmp = cfg.with_name(f"{cfg.name}.{os.getpid()}.tmp")
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data))
        os.replace(tmp, cfg)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


class FileTypesDialog:
    """Tools > File Types…: one checkbox per supported extension, grouped
    Stills / RAW / Video, checked = included in the walk. Constructed as
    a plain QDialog (see the module doc's modes-not-modals judgment).
    The class defers every Qt import so importing this module stays free
    of widget machinery (catalog-level tests import it headless).

    Usage: dlg = FileTypesDialog(excluded, parent); exec_() Accepted ->
    dlg.excluded() is the new exclusion set. Unchecking everything is
    allowed — the walk is then empty, which the grid shows honestly and
    the panel can undo; a guard here would just be a second modal."""

    GROUPS = (("Stills", STILL_EXTS), ("RAW", RAW_EXTS),
              ("Video", VIDEO_EXTS))

    def __init__(self, excluded: set[str] | frozenset[str], parent=None):
        from PySide6.QtWidgets import (
            QCheckBox,
            QDialog,
            QDialogButtonBox,
            QGridLayout,
            QGroupBox,
            QLabel,
            QVBoxLayout,
        )

        self.dialog = QDialog(parent)
        self.dialog.setWindowTitle("File Types")
        lay = QVBoxLayout(self.dialog)
        lay.addWidget(QLabel(
            "Choose which file types belong in the library walk.\n"
            "Changing this re-indexes into its own cache; switching a "
            "type back restores the previous one."))
        self.boxes: dict[str, QCheckBox] = {}
        per_row = 6
        for title, group in self.GROUPS:
            gb = QGroupBox(title)
            grid = QGridLayout(gb)
            for i, ext in enumerate(sorted(group)):
                box = QCheckBox(ext)
                box.setChecked(ext not in excluded)
                self.boxes[ext] = box
                grid.addWidget(box, i // per_row, i % per_row)
            lay.addWidget(gb)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.dialog.accept)
        buttons.rejected.connect(self.dialog.reject)
        lay.addWidget(buttons)

    def exec_(self) -> bool:
        from PySide6.QtWidgets import QDialog

        return self.dialog.exec() == QDialog.DialogCode.Accepted

    def excluded(self) -> set[str]:
        """The exclusion set the checkboxes currently express."""
        return {ext for ext, box in self.boxes.items()
                if not box.isChecked()}
