#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6"]
# ///
"""Throttle-probe gate for the §7 scroll validation (fauxcasa-ncv).

Run FIRST, before trusting any paint-interval number. A trivial QWidget
calls update() from a fixed-rate QTimer; we count paintEvents/s on THIS
compositor. Run it at several timer rates:

    for hz in 60 240 1000; do
      WAYLAND_DISPLAY=wayland-0 QT_QPA_PLATFORM=wayland \
        uv run tracer/vsync_probe.py --hz $hz
    done

Decision rule (per the methodology pre-mortem):
  * paint cadence INVARIANT to timer rate and ≈ refresh  -> Qt throttles
    raster paints to the compositor frame callback here; paint-to-paint
    INTERVAL is a trustworthy vsync-cadence signal.
  * paint cadence TRACKS the timer rate (≈ timer Hz, not refresh) -> Qt is
    UNTHROTTLED; interval measures Qt's render-offer cadence, not on-screen
    smoothness. The honest §7 metric is then FRAME PRODUCTION TIME (time
    inside paintEvent), corroborated by the compositor's own commit /
    frame-callback cadence (WAYLAND_DEBUG). This is the case on the dev
    machine (GNOME/Mutter, AMD 780M, 165 Hz): see the §7-validation report.
"""
import argparse
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QWidget


class Probe(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(1280, 800)
        self.last = None
        self.iv: list[float] = []
        self.frame = 0

    def paintEvent(self, _e) -> None:
        now = time.perf_counter()
        if self.last is not None:
            self.iv.append((now - self.last) * 1000.0)
        self.last = now
        self.frame += 1
        p = QPainter(self)
        y = (self.frame * 13) % 800  # scroll a band so content changes
        p.fillRect(self.rect(), QColor(20, 20, 20))
        p.fillRect(0, y, 1280, 40, QColor(200, 120, 40))
        p.end()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hz", type=float, default=240.0,
                    help="timer rate that calls update()")
    ap.add_argument("--seconds", type=float, default=3.0)
    args = ap.parse_args()

    app = QApplication([])
    w = Probe()
    w.setWindowTitle("vsync-probe")
    w.show()
    w.raise_()
    w.activateWindow()
    scr = w.screen() or app.primaryScreen()
    refresh = scr.refreshRate() or 60.0

    t = QTimer()
    t.setTimerType(Qt.TimerType.PreciseTimer)
    t.setInterval(max(1, round(1000.0 / args.hz)))
    t.timeout.connect(w.update)
    t.start()

    def done() -> None:
        iv = sorted(w.iv)
        n = len(iv)
        pct = lambda q: iv[min(int((n - 1) * q), n - 1)] if n else 0.0
        period = 1000.0 / refresh if refresh else 0.0
        paint_hz = n / args.seconds
        print(f"platform={app.platformName()} screen={scr.name()} "
              f"refresh={refresh:.2f}Hz frame_period={period:.2f}ms "
              f"timer_hz={args.hz:g}")
        print(f"paints={n} paint_hz={paint_hz:.1f} min={iv[0]:.2f} "
              f"p50={pct(.5):.2f} p99={pct(.99):.2f} max={iv[-1]:.2f} ms")
        # A vsync-throttled surface caps paint_hz at ~refresh regardless of
        # timer; only a timer well ABOVE refresh can tell them apart.
        if args.hz >= refresh * 1.2:
            if paint_hz > refresh * 1.3:
                v = ("paint_hz exceeds refresh -> UNTHROTTLED (interval is "
                     "NOT a vsync signal; use frame-production time + the "
                     "WAYLAND_DEBUG commit/callback cadence)")
            else:
                v = ("paint_hz pinned at ~refresh, INVARIANT to timer -> "
                     "THROTTLED (interval is a valid vsync signal)")
        else:
            v = (f"inconclusive: timer ({args.hz:g}Hz) <= refresh; paint_hz "
                 f"≈ timer just means paints follow the driver. Re-run with "
                 f"--hz > {refresh:.0f} to decide throttling.")
        print("verdict: " + v)
        app.quit()

    QTimer.singleShot(int(args.seconds * 1000), done)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
