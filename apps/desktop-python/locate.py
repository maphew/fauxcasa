"""Locate on Disk (fauxcasa-q6l.8) — open the OS file manager with a
given file SELECTED, Picasa's Ctrl+Enter / "Locate on Disk" menu action
(docs/research/picasa-ui-inventory.md). One function behind the keymap's
grid.locate / viewer.locate bindings; named after Picasa's menu wording
because "reveal" already means show-hidden mode in the grid.

Per-platform, best native selection first, honest fallback after:

* Windows: ``explorer /select,"<native path>"`` — launched as a single
  command-line STRING so the exact ``/select,"..."`` form reaches
  CreateProcess verbatim (list argv would re-quote and explorer's comma
  parsing is famously brittle). Explorer always returns a nonzero exit
  code by design, so launch success is the only signal.
* macOS: ``open -R <path>`` (reveal in Finder).
* Linux/BSD: the org.freedesktop.FileManager1 D-Bus interface
  (``ShowItems``) via ``dbus-send`` — the portable file-manager-neutral
  protocol — falling back to ``xdg-open`` of the CONTAINING FOLDER when
  the bus/interface is missing (no selection, but the user still lands
  next to the file).

No new threads: the D-Bus round-trip is bounded by a short timeout and
the launchers are fire-and-forget Popen.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_DBUS_TIMEOUT = 3.0  # bounded: a hung session bus must not wedge a key press


def reveal_in_file_manager(path: str | os.PathLike,
                           platform: str | None = None) -> bool:
    """Show `path` selected in the platform file manager. Returns True
    when a launcher was started (fire-and-forget beyond that), False when
    nothing could be launched. `platform` overrides sys.platform so every
    branch is testable from any host."""
    p = Path(path)
    plat = platform if platform is not None else sys.platform
    try:
        if plat.startswith("win"):
            subprocess.Popen(f'explorer /select,"{os.path.normpath(p)}"')
            return True
        if plat == "darwin":
            subprocess.Popen(["open", "-R", str(p)])
            return True
        # Linux/BSD: FileManager1 ShowItems, else open the folder.
        try:
            r = subprocess.run(
                ["dbus-send", "--session", "--print-reply",
                 "--dest=org.freedesktop.FileManager1",
                 "/org/freedesktop/FileManager1",
                 "org.freedesktop.FileManager1.ShowItems",
                 f"array:string:{p.absolute().as_uri()}", "string:"],
                capture_output=True, timeout=_DBUS_TIMEOUT)
            if r.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        subprocess.Popen(["xdg-open", str(p.parent)])
        return True
    except OSError:
        return False
