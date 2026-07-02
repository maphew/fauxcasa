#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6"]
# ///
"""Spawn/IPC cost measurements grounding the decode-service design
(fauxcasa-i92.2, docs/design/decode-service.md).

The design's process model hangs on four numbers, measured here on the
actual dev box rather than asserted:

  1. bare interpreter spawn      -- `python -c pass` round-trip: the floor
     for ANY per-job-process scheme on Windows.
  2. decode-ready worker spawn   -- child imports PySide6.QtGui (the
     QImageReader decode stack) and reports ready: the real cost of
     (re)filling a worker-pool slot after a crash/timeout kill.
  3. persistent-worker round-trip -- tiny request/response over pipes to
     an already-warm child: the per-job IPC floor for a reused worker.
  4. shared-memory copy bandwidth -- one 24 MP RGBA frame written into a
     multiprocessing.shared_memory segment: the per-image cost of the
     pixels-over-shm response channel.

Run:  uv run docs/research/spikes/decode-worker-spawn-spike.py
Prints a small JSON blob; the design doc cites the 2026-07-02 run on the
Windows dev machine.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time

N_SPAWN = 15
N_ROUNDTRIP = 200


def _stats(samples: list[float]) -> dict:
    return {
        "min_ms": round(min(samples) * 1000, 3),
        "median_ms": round(statistics.median(samples) * 1000, 3),
        "max_ms": round(max(samples) * 1000, 3),
        "n": len(samples),
    }


def bare_spawn() -> dict:
    """Full CreateProcess + interpreter start + exit, no imports."""
    samples = []
    for _ in range(N_SPAWN):
        t0 = time.perf_counter()
        subprocess.run([sys.executable, "-c", "pass"], check=True)
        samples.append(time.perf_counter() - t0)
    return _stats(samples)


def decode_ready_spawn() -> dict:
    """Spawn to 'worker can decode': child imports QImageReader and
    prints a ready line; we time until that line arrives (the child may
    then exit off the clock)."""
    child = "from PySide6.QtGui import QImageReader; print('R', flush=True)"
    samples = []
    for _ in range(N_SPAWN):
        t0 = time.perf_counter()
        p = subprocess.Popen([sys.executable, "-c", child],
                             stdout=subprocess.PIPE)
        assert p.stdout is not None and p.stdout.readline().strip() == b"R"
        samples.append(time.perf_counter() - t0)
        p.wait()
    return _stats(samples)


def persistent_roundtrip() -> dict:
    """Per-job IPC floor: echo a small line to a warm child N times."""
    child = ("import sys\n"
             "for line in sys.stdin.buffer:\n"  # binary: no \r\n mangling
             "    sys.stdout.buffer.write(line)\n"
             "    sys.stdout.buffer.flush()\n")
    p = subprocess.Popen([sys.executable, "-c", child],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert p.stdin is not None and p.stdout is not None
    samples = []
    for i in range(N_ROUNDTRIP):
        msg = f"job {i}\n".encode()
        t0 = time.perf_counter()
        p.stdin.write(msg)
        p.stdin.flush()
        assert p.stdout.readline() == msg
        samples.append(time.perf_counter() - t0)
    p.stdin.close()
    p.wait()
    return _stats(samples)


def shm_frame_copy() -> dict:
    """Write one 6000x4000 RGBA frame (~91 MiB) into a fresh shm segment,
    then re-copy into an existing one (steady-state pool behaviour)."""
    from multiprocessing import shared_memory

    frame = bytes(6000 * 4000 * 4)  # 24 MP RGBA
    t0 = time.perf_counter()
    seg = shared_memory.SharedMemory(create=True, size=len(frame))
    create_s = time.perf_counter() - t0

    copies = []
    for _ in range(5):
        t0 = time.perf_counter()
        seg.buf[: len(frame)] = frame
        copies.append(time.perf_counter() - t0)
    seg.close()
    seg.unlink()
    best = min(copies)
    return {
        "frame_mib": round(len(frame) / 2**20, 1),
        "segment_create_ms": round(create_s * 1000, 1),
        "copy_min_ms": round(best * 1000, 1),
        "copy_gib_per_s": round(len(frame) / best / 2**30, 1),
    }


def main() -> None:
    result = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "bare_spawn": bare_spawn(),
        "decode_ready_spawn": decode_ready_spawn(),
        "persistent_roundtrip": persistent_roundtrip(),
        "shm_frame_copy": shm_frame_copy(),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
