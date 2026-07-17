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

No new threads: the D-Bus round-trip is now an async QProcess whose
3 s deadline kill triggers the xdg-open fallback without blocking the
key handler (fauxcasa-q6l.21). Windows and macOS paths remain
fire-and-forget Popen, unchanged.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QTimer

_DBUS_TIMEOUT = 3.0  # bounded: a hung session bus must not wedge a key press

_pending: set[QProcess] = set()   # keep async probes alive until they settle


def _dbus_show_items_cmd(p: Path) -> list[str]:
    """Return the dbus-send argv for FileManager1.ShowItems.
    Extracted as a named function so tests can monkeypatch the program."""
    return [
        "dbus-send", "--session", "--print-reply",
        "--dest=org.freedesktop.FileManager1",
        "/org/freedesktop/FileManager1",
        "org.freedesktop.FileManager1.ShowItems",
        f"array:string:{p.absolute().as_uri()}", "string:",
    ]


def _open_folder(p: Path) -> None:
    """Late async fallback: open the containing folder via xdg-open.
    Errors are swallowed — a late async fallback can no longer report
    False into the long-returned key handler."""
    try:
        subprocess.Popen(["xdg-open", str(p.parent)])
    except OSError:
        pass


def _reveal_linux_async(p: Path) -> None:
    """Start a QProcess probe for FileManager1.ShowItems and return
    immediately. The key handler is free before dbus-send replies.

    Settle path (pitfalls are load-bearing — keep all guards):
    - finished  (NormalExit + code==0): success, discard proc.
    - finished  (any other): xdg-open folder fallback.
    - errorOccurred (FailedToStart): xdg-open fallback; finished may
      never fire for FailedToStart, so this signal is the only trigger.
    - QTimer deadline: kill() the probe -> CrashExit -> _settle(False)
      -> xdg-open, same 3 s bound as the old synchronous call, but
      without blocking.
    """
    proc = QProcess()
    _pending.add(proc)
    done = False   # finished AND errorOccurred can both fire (kill -> Crashed + finished)

    def _settle(ok: bool) -> None:
        nonlocal done
        if done:
            return
        done = True
        if not ok:
            _open_folder(p)
        _pending.discard(proc)
        proc.deleteLater()

    # finished signature: (exitCode: int, status: QProcess.ExitStatus)
    proc.finished.connect(
        lambda code, status: _settle(
            status == QProcess.ExitStatus.NormalExit and code == 0))
    # FailedToStart never emits finished — errorOccurred is the only trigger
    proc.errorOccurred.connect(lambda _err: _settle(False))
    # done-guard: proc may be deleteLater'd before the timer fires
    QTimer.singleShot(int(_DBUS_TIMEOUT * 1000),
                      lambda: None if done else proc.kill())
    cmd = _dbus_show_items_cmd(p)
    proc.start(cmd[0], cmd[1:])


def reveal_in_file_manager(path: str | os.PathLike,
                           platform: str | None = None) -> bool:
    """Show `path` selected in the platform file manager. Returns True
    when a launcher was started (fire-and-forget beyond that), False when
    nothing could be launched. `platform` overrides sys.platform so every
    branch is testable from any host.

    On Linux, True means the async probe was started; whether selection
    (ShowItems) or the xdg-open folder fallback is used resolves later in
    the probe's settle path."""
    p = Path(path)
    plat = platform if platform is not None else sys.platform
    try:
        if plat.startswith("win"):
            subprocess.Popen(f'explorer /select,"{os.path.normpath(p)}"')
            return True
        if plat == "darwin":
            subprocess.Popen(["open", "-R", str(p)])
            return True
        # Linux/BSD: FileManager1 ShowItems, else open the folder — probed
        # ASYNCHRONOUSLY (q6l.21): a slow session bus must not wedge the
        # key handler, so QProcess signals replace the old synchronous
        # subprocess.run round-trip; the xdg-open fallback fires from the
        # probe's settle path instead of inline.
        _reveal_linux_async(p)
        return True
    except OSError:
        return False
