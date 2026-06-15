#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6"]
# ///
"""Diagnostics channel for the tracer (fauxcasa-pqw).

A shipping double-click build sets ``console=False`` in the PyInstaller spec,
and a windowed PyInstaller process has ``sys.stdout``/``sys.stderr == None`` —
so every diagnostic the app would ``print(..., file=sys.stderr)`` silently
vanishes (a write to a ``None`` stream is a no-op, not a crash), and an
uncaught traceback disappears with no console to show it. This module is the
durable channel that survives ``console=False``:

  * a rotating log **file** in the app's cache/config dir (always written);
  * a **stderr mirror**, resolved per-emit so it follows a console when one
    exists (source runs, the console CI build, the ``bundle.yml`` stream
    greps) and no-ops cleanly when ``sys.stderr is None`` (the windowed
    build). pytest's ``capsys`` also swaps ``sys.stderr``, so the existing
    stderr assertions keep working;
  * ``qInstallMessageHandler`` -> the log (Qt's own qWarning/qCritical, the
    only survivor on a no-console Windows build is otherwise OutputDebugString);
  * ``sys.excepthook`` + ``threading.excepthook`` -> the log, plus a
    ``QMessageBox`` for a fatal error in a GUI build (there is no console to
    show a crash). Both degrade safely under a headless/offscreen platform so
    CI never blocks on a modal nobody can dismiss.

The MACHINE protocol the §7 harness/CI parse — ``READY`` and the
ready/exit/indexed JSON on real **stdout** — is deliberately NOT routed here;
main.py keeps printing those straight to stdout (a no-op in a windowed build,
which has no consumer for them anyway).
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import traceback
from pathlib import Path

# The single app logger. Everything (main.py, the Qt handler, the excepthook)
# logs through this name; setup() and this module wire its handlers.
log = logging.getLogger("fauxcasa")

_LOG_BYTES = 1_000_000   # ~1 MB per file …
_LOG_BACKUPS = 3         # … x (1 + 3) = ~4 MB ceiling for the rotating set

APP_NAME = "Fauxcasa"


class _StderrHandler(logging.Handler):
    """Mirror records to the *current* ``sys.stderr``, resolved per-emit.

    ``logging.StreamHandler`` binds its stream once at construction; we look it
    up on every emit so that (a) a windowed build whose ``sys.stderr is None``
    skips cleanly instead of raising, and (b) pytest's ``capsys`` — which swaps
    ``sys.stderr`` per test — still sees the messages the existing tests assert
    on. (This mirrors how ``logging.lastResort`` resolves its stream.)"""

    def emit(self, record: logging.LogRecord) -> None:
        stream = sys.stderr
        if stream is None:
            return
        try:
            stream.write(self.format(record) + "\n")
            stream.flush()
        except (OSError, ValueError):
            # A torn-down/closed stream at shutdown must never crash a worker
            # or the interpreter on its way out — and must not recurse into
            # handleError() (which writes to stderr again).
            pass


# --- eager stderr mirror -------------------------------------------------
# Attach the stderr mirror at import, before setup() runs, so the pre-setup
# diagnostics in main.py (and the direct-function unit tests that never call
# setup()) still reach a console / capsys. propagate=False keeps records off
# the root logger so the output is independent of whatever handler pytest may
# have installed on root.
log.setLevel(logging.DEBUG)
log.propagate = False
if not any(isinstance(h, _StderrHandler) for h in log.handlers):
    _sh = _StderrHandler()
    _sh.setLevel(logging.INFO)                      # match the old stderr UX
    _sh.setFormatter(logging.Formatter("%(message)s"))   # bare, like print()
    log.addHandler(_sh)


def _show_fatal_dialog(text: str) -> None:
    """Best-effort crash dialog for a GUI build — the only way to surface a
    fatal error when there is no console. No-op when no QApplication exists
    yet or the platform is headless (offscreen/minimal/""), so CI never hangs
    on a modal, and never lets the dialog itself raise."""
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except Exception:
        return
    app = QApplication.instance()
    if app is None or app.platformName() in ("offscreen", "minimal", ""):
        return
    try:
        lines = text.strip().splitlines()
        summary = lines[-1] if lines else "Unknown error"
        box = QMessageBox(
            QMessageBox.Icon.Critical,
            f"{APP_NAME} — unexpected error",
            f"{APP_NAME} hit an unexpected error and may be unstable.\n\n"
            f"{summary}\n\nDetails were written to the log file.",
        )
        box.exec()
    except Exception:
        pass


def _install_excepthook() -> None:
    """Route an uncaught exception to the log (and a console, if one exists,
    preserving Python's default traceback-to-stderr) plus a GUI dialog. A
    KeyboardInterrupt keeps the default behavior."""

    def hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.critical("uncaught exception\n%s", text)
        if sys.stderr is not None:      # console build: keep the usual trace
            try:
                sys.stderr.write(text)
                sys.stderr.flush()
            except (OSError, ValueError):
                pass
        _show_fatal_dialog(text)

    sys.excepthook = hook

    def thread_hook(args) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        text = "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback))
        log.critical("uncaught exception in thread %s\n%s",
                     getattr(args.thread, "name", "?"), text)

    threading.excepthook = thread_hook


def _install_qt_handler() -> None:
    """Forward Qt's own messages (qWarning/qCritical/qFatal, otherwise lost on
    a no-console build save for Windows OutputDebugString) into the log."""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        return

    level_of = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode, context, message) -> None:
        log.log(level_of.get(mode, logging.INFO), "Qt: %s", message)

    qInstallMessageHandler(handler)


def setup(cache_root: Path) -> Path | None:
    """Add the rotating log file under ``cache_root`` and install the Qt
    message handler + the exception hooks. Idempotent — tests (and a cache-root
    change) re-invoke it, so any prior file handler is repointed, never
    duplicated. The eager stderr mirror added at import is preserved.

    Returns the log file path, or ``None`` if a log file could not be opened
    (the stderr mirror still works, and the failure is reported — without a
    traceback — rather than aborting the launch)."""
    if not any(isinstance(h, _StderrHandler) for h in log.handlers):
        # Defensive: a test may have cleared handlers; keep the mirror.
        sh = _StderrHandler()
        sh.setLevel(logging.INFO)
        sh.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(sh)

    for h in list(log.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            log.removeHandler(h)
            h.close()

    log_path: Path | None = None
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        log_path = cache_root / "fauxcasa-tracer.log"
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=_LOG_BYTES, backupCount=_LOG_BACKUPS,
            delay=True, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s: %(message)s"))
        log.addHandler(fh)
    except OSError as e:
        # Best-effort, like the rest of the cache: a log we can't open must not
        # take down the app. Report it (no traceback) and rely on the mirror.
        log_path = None
        log.warning("could not open log file under %s: %s", cache_root, e)

    _install_qt_handler()
    _install_excepthook()
    return log_path
