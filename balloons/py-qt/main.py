#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6"]
# ///
# Trial balloon: Python + PySide6. See balloons/README.md for the fixed
# task, the fcache format, and the output contract. Mirrors the phase
# machine, metrics definitions, prefetch and bounded-LRU design of the
# reference implementation (balloons/rust-egui/src/main.rs).

import json
import math
import os
import queue
import struct
import sys
import threading
import time

T0 = time.perf_counter()  # process-start reference for cold_start_ms

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QWidget

CELL = 168
TILE = 160
WIN = (1280, 800)
# Decoded-tile RAM cache bound: 1200 entries. Tiles are pre-scaled to fit
# the 160 px box at decode time, so worst case 1200 * 160*160*4 B ~= 120 MB
# -- comfortably under the suggested 512 MB bound.
CACHE_CAP = 1200
PREFETCH_SCREENS = 2.0
DWELL_AFTER_FILL_S = 2.0
WORKERS = 4
TELEPORT_STOPS = (0.25, 0.50, 0.75, 0.99)


def read_index(path):
    """fcache header: magic FCTC, u32 version=1, u32 count, u32 reserved;
    then count 16-byte records: u64 offset, u32 length, u16 w, u16 h."""
    with open(path, "rb") as f:
        hdr = f.read(16)
        if hdr[:4] != b"FCTC":
            sys.exit("bad magic")
        version, count, _ = struct.unpack("<III", hdr[4:16])
        if version != 1:
            sys.exit("bad version")
        raw = f.read(count * 16)
    return [struct.unpack_from("<QI", raw, i * 16) for i in range(count)]


def decode_worker(path, entries, jobs, done):
    """Pull indices off the job queue, pread + JPEG-decode + pre-scale to
    the 160 px tile box, push (idx, QImage) back. QImage decode happens in
    Qt's C++ libjpeg; whether the GIL is released is the question under
    test -- we just measure the result honestly."""
    fd = os.open(path, os.O_RDONLY)
    while True:
        idx = jobs.get()
        offset, length = entries[idx]
        buf = os.pread(fd, length, offset)
        img = QImage.fromData(buf, "JPEG")
        if img.isNull():
            continue
        img = img.scaled(
            TILE,
            TILE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ).convertToFormat(QImage.Format.Format_RGB32)
        done.put((idx, img))


def read_rss_mb():
    rss = hwm = 0.0
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = float(line.split()[1]) / 1024.0
                elif line.startswith("VmHWM:"):
                    hwm = float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return rss, hwm


class Grid(QWidget):
    """Custom-painted virtualized grid over a virtual scroll offset.
    A ~240 Hz QTimer calls update(); the Wayland compositor's frame
    callbacks throttle actual paints (vsync-equivalent)."""

    def __init__(self, entries, path, seconds, speed):
        super().__init__()
        self.entries = entries
        self.seconds = seconds
        self.speed = speed
        self.jobs = queue.SimpleQueue()
        self.done = queue.SimpleQueue()
        for _ in range(WORKERS):
            threading.Thread(
                target=decode_worker,
                args=(path, entries, self.jobs, self.done),
                daemon=True,
            ).start()
        # tiles: idx -> None (queued) | [QImage, last_used_frame_no]
        self.tiles = {}
        self.phase = ("cold",)
        self.phase_start = T0
        self.fill_start = None
        self.last_frame = None
        self.frame_no = 0
        self.offset_y = 0.0
        self.finished = False
        self.m_cold = 0.0
        self.m_frames = []
        self.m_blank = 0
        self.m_fills = []
        self.setFixedSize(*WIN)
        self.setWindowTitle("balloon-py-qt")

    # ----- phase machine -----

    def advance(self, now):
        ph = self.phase
        if ph[0] == "cold":
            self.phase = ("flick_a",)
        elif ph[0] == "flick_a":
            self.phase = ("teleport", 0)
        elif ph[0] == "teleport":
            self.phase = ("dwell", ph[1])
        elif ph[0] == "dwell":
            self.phase = ("teleport", ph[1] + 1) if ph[1] < 3 else ("flick_b",)
        elif ph[0] == "flick_b":
            self.phase = ("done",)
        self.phase_start = now
        self.fill_start = None
        if os.environ.get("BALLOON_DEBUG"):
            print(f"phase -> {self.phase}", file=sys.stderr)

    # ----- cache plumbing -----

    def pump_decoded(self):
        while True:
            try:
                idx, img = self.done.get_nowait()
            except queue.Empty:
                break
            self.tiles[idx] = [img, self.frame_no]

    def request_range(self, a, b):
        tiles = self.tiles
        for i in range(a, b):
            if i not in tiles:
                tiles[i] = None
                self.jobs.put(i)

    def evict_lru(self):
        ready = [(i, v[1]) for i, v in self.tiles.items() if v is not None]
        excess = len(ready) - CACHE_CAP
        if excess <= 0:
            return
        ready.sort(key=lambda t: t[1])
        for i, _ in ready[:excess]:
            del self.tiles[i]

    def all_ready(self, a, b):
        tiles = self.tiles
        return all(tiles.get(i) is not None for i in range(a, b))

    # ----- the frame -----

    def paintEvent(self, _event):
        now = time.perf_counter()
        frame_ms = None
        if self.last_frame is not None:
            frame_ms = (now - self.last_frame) * 1000.0
        self.last_frame = now
        self.frame_no += 1

        self.pump_decoded()

        # The interval just measured belongs to the phase that was active
        # while the frame was produced — record it BEFORE the state machine
        # advances, so a stall on a phase-deadline frame is not dropped.
        if self.phase[0] in ("flick_a", "flick_b") and frame_ms is not None:
            self.m_frames.append(frame_ms)

        n = len(self.entries)
        w, h = float(self.width()), float(self.height())
        cols = max(1, int(w // CELL))
        rows = (n + cols - 1) // cols
        max_off = max(0.0, rows * CELL - h)

        # phase state machine drives the offset
        elapsed = now - self.phase_start
        ph = self.phase[0]
        if ph in ("flick_a", "flick_b"):
            anchor = 0.0 if ph == "flick_a" else 0.5 * max_off
            self.offset_y = min(anchor + elapsed * self.speed, max_off)
            if elapsed >= self.seconds:
                self.advance(now)
        elif ph == "teleport":
            self.offset_y = TELEPORT_STOPS[self.phase[1]] * max_off
        elif ph == "dwell":
            if elapsed >= DWELL_AFTER_FILL_S:
                self.advance(now)

        first_row = max(0, int(self.offset_y // CELL))
        last_row = math.ceil((self.offset_y + h) / CELL)
        a = min(first_row * cols, n)
        b = min((last_row + 1) * cols, n)
        a = min(a, b)

        # prefetch ~2 screens beyond the viewport in scroll direction
        pre_rows = math.ceil(h * PREFETCH_SCREENS / CELL)
        pb = min(b + pre_rows * cols, n)
        self.request_range(a, pb)
        self.evict_lru()

        # draw visible tiles
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(20, 20, 20))
        placeholder = QColor(60, 60, 60)
        any_blank = False
        for i in range(a, b):
            row, col = divmod(i, cols)
            x = col * CELL + 4
            y = row * CELL - self.offset_y + 4
            t = self.tiles.get(i)
            if t is not None:
                t[1] = self.frame_no
                img = t[0]
                ix = x + (TILE - img.width()) / 2
                iy = y + (TILE - img.height()) / 2
                painter.drawImage(round(ix), round(iy), img)
            else:
                any_blank = True
                painter.fillRect(int(x), round(y), TILE, TILE, placeholder)
        painter.end()

        # metrics by phase
        ph = self.phase[0]
        if ph == "cold":
            if self.all_ready(a, b):
                self.m_cold = (now - T0) * 1000.0
                print("READY", flush=True)
                self.advance(now)
        elif ph in ("flick_a", "flick_b"):
            if any_blank:
                self.m_blank += 1
        elif ph == "teleport":
            if self.fill_start is None:
                self.fill_start = now
            elif self.all_ready(a, b):
                self.m_fills.append((now - self.fill_start) * 1000.0)
                self.fill_start = None
                self.advance(now)

        if self.phase[0] == "done" and not self.finished:
            self.finished = True
            self.finish()
            QTimer.singleShot(0, QApplication.instance().quit)

    def finish(self):
        ms = sorted(self.m_frames)

        def pct(p):
            if not ms:
                return 0.0
            return ms[min(int(len(ms) * p), len(ms) - 1)]

        rss, hwm = read_rss_mb()
        out = {
            "candidate": "py-qt",
            "photos": len(self.entries),
            "cold_start_ms": round(self.m_cold),
            "frames": len(ms),
            "p50_ms": round(pct(0.50), 2),
            "p99_ms": round(pct(0.99), 2),
            "max_ms": round(ms[-1], 2) if ms else 0.0,
            "blank_tile_frames": self.m_blank,
            "fill_ms": [round(f) for f in self.m_fills],
            "vm_rss_mb": round(rss, 1),
            "vm_hwm_mb": round(hwm, 1),
        }
        print(json.dumps(out), flush=True)


def main():
    path = None
    seconds = 45.0
    speed = 2000.0
    it = iter(sys.argv[1:])
    for arg in it:
        if arg == "--seconds":
            seconds = float(next(it))
        elif arg == "--speed":
            speed = float(next(it))
        else:
            path = arg
    if path is None:
        sys.exit("usage: main.py <fcache> [--seconds S] [--speed PX_S]")

    entries = read_index(path)

    os.environ.setdefault("QT_QPA_PLATFORM", "wayland")
    os.environ.setdefault("QT_WAYLAND_DISABLE_WINDOWDECORATION", "1")
    # no text input in a benchmark; the session's ibus IM module spams
    # Gdk-CRITICAL on a headless seat, so force it off
    os.environ["QT_IM_MODULE"] = ""
    os.environ.setdefault("QT_QPA_PLATFORMTHEME", "generic")
    app = QApplication([])
    print(f"platform: {app.platformName()}", file=sys.stderr)

    grid = Grid(entries, path, seconds, speed)
    grid.show()

    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.setInterval(4)  # ~240 Hz; actual paints throttled by compositor
    timer.timeout.connect(grid.update)
    timer.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
