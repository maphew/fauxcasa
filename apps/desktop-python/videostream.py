"""Video playback streaming seam (fauxcasa-v46.3): broker + worker.

The ruled design (docs/design/decode-service.md §3c, owner ruling
2026-08-11 in fauxcasa-i92.1, PR #96): playback is option A — a separate,
to-be-sandboxed worker PROCESS decodes with PyAV (in-worker libav) and
streams fixed-shape frames out through a shared-memory ring. QtMultimedia
in-process decoding is DECLINED and never imported here. Scope is
play/pause/seek/position only. Audio leaves the worker as PCM s16 chunks
through the same shared memory (a QAudioSink consumes them on the trusted
side — its input is our sandbox's *output*, not attacker bytes).

This one module holds both sides of the boundary:

- Broker side: class `VideoStream` (the StreamHandle-shaped API from
  decodesvc). It opens the file, spawns the worker, passes the OPEN
  DESCRIPTOR (never a path — a hijacked worker holds exactly one file),
  creates the shared-memory arena, and VALIDATES every frame/audio
  message against the §2.4 checklist before any trusted code touches
  arena bytes. Any violation is treated as evidence of compromise: kill
  the worker, surface an error state, never retry.
- Worker side: `--worker` entrypoint, spawned as
  `[sys.executable, videostream.py, "--worker", ...]`. It imports only
  what it needs (PyAV + stdlib), re-opens nothing by path, demuxes and
  decodes ahead until the ring is full (backpressure = block until a
  stream_free arrives), and interleaves resampled s16 audio chunks.

Wire protocol (design §2): 4-byte little-endian length prefix, UTF-8
JSON, hard cap 65536 bytes per message; oversized or malformed input is
a protocol violation. Requests (broker -> worker): stream_open
{slots, max_edge, pixfmt, fd|handle}, stream_seek {pts_us}, stream_free
{slot}, audio_free {slot}, stream_close. Responses (worker -> broker):
info {duration_us, fps, w, h, has_audio, audio_rate, audio_channels},
frame {slot, pts_us, w, h, stride, off, len}, audio {off, len, pts_us},
seek_ack {pts_us}, eof, error {code, detail}.

SECURITY RESIDUAL (flagged): the arena is a *name-based*
multiprocessing.shared_memory segment passed to the worker by name at
spawn. The ruled design (§2 item 3) wants an anonymous capability-style
mapping (memfd / anonymous file mapping + handle duplication) so no name
exists for a compromised worker to guess or reuse; that lands with
fauxcasa-i92.3's anonymous-capability arena and the sandbox launcher.
Same residual: the worker is not yet authority-stripped (§4) — this
module is the seam those i92.3 pieces slot into.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time

from decodesvc import (
    DecodeServiceError,
    ErrorCode,
    ProtocolViolation,
    StreamInfo,
)

# Control-channel framing (design §2): 4-byte LE length + UTF-8 JSON.
MSG_CAP = 65_536

# Ring geometry. Frame slots default to StreamOpenRequest's 8; the audio
# sub-region is its own simple ring of fixed-size PCM chunk slots with
# its own slot accounting (broker acks each consumed chunk back with an
# audio_free message; the worker blocks when all audio slots are
# outstanding, mirroring the video ring's backpressure).
DEFAULT_SLOTS = 8
DEFAULT_MAX_EDGE = 3_840
AUDIO_SLOTS = 32
AUDIO_SLOT_BYTES = 64 * 1024  # multiple of 4: keeps every off % 4 == 0

# Per design §1: 5 s per-frame progress deadline for streaming; on
# expiry the broker kills the worker outright (never a polite request).
FRAME_DEADLINE_S = 5.0

_RGB_BPP = 3  # frames cross the boundary as rgb24, stride padded to %4


def _align4(n: int) -> int:
    return (n + 3) & ~3


class _Layout:
    """Arena layout, computed identically on both sides from
    (slots, max_edge) so no offsets ever travel on the control channel
    except inside validated buffer descriptions."""

    def __init__(self, slots: int, max_edge: int) -> None:
        self.slots = slots
        self.max_edge = max_edge
        # Worst-case rgb24 frame: stride = align4(max_edge*3), h = max_edge.
        self.frame_slot_bytes = _align4(max_edge * _RGB_BPP) * max_edge
        self.frame_base = 0
        self.audio_slots = AUDIO_SLOTS
        self.audio_slot_bytes = AUDIO_SLOT_BYTES
        self.audio_base = self.frame_base + slots * self.frame_slot_bytes
        self.total_bytes = self.audio_base + AUDIO_SLOTS * AUDIO_SLOT_BYTES

    def frame_slot_base(self, slot: int) -> int:
        return self.frame_base + slot * self.frame_slot_bytes

    def audio_slot_base(self, slot: int) -> int:
        return self.audio_base + slot * self.audio_slot_bytes


# ---------------------------------------------------------------------------
# Framing helpers (both sides)


def _send_msg(fp, obj: dict) -> None:
    data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    if len(data) > MSG_CAP:
        raise ValueError(f"control message {len(data)} bytes exceeds cap")
    fp.write(struct.pack("<I", len(data)) + data)
    fp.flush()


def _recv_msg(fp) -> dict | None:
    """One framed message, None on clean EOF (no bytes at the header).
    Raises ProtocolViolation on oversize, mid-message EOF, non-JSON, or
    a non-object payload — the §1 'malformed = evidence of compromise'
    semantics on the broker side; on the worker side the same strictness
    just makes a buggy broker fail loudly."""
    hdr = fp.read(4)
    if len(hdr) == 0:
        return None
    if len(hdr) < 4:
        raise ProtocolViolation("EOF inside message header")
    n = struct.unpack("<I", hdr)[0]
    if n > MSG_CAP:
        raise ProtocolViolation(f"message length {n} exceeds cap {MSG_CAP}")
    data = fp.read(n)
    if len(data) < n:
        raise ProtocolViolation("EOF inside message body")
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolViolation(f"malformed control JSON: {e}") from None
    if not isinstance(obj, dict):
        raise ProtocolViolation("control message is not a JSON object")
    return obj


def _as_int(msg: dict, key: str) -> int:
    v = msg.get(key)
    if not isinstance(v, int) or isinstance(v, bool):
        raise ProtocolViolation(f"field {key!r} not an integer: {v!r}")
    return v


# ---------------------------------------------------------------------------
# Broker side


_LIVE: set["VideoStream"] = set()


def _close_all_live() -> None:
    for s in list(_LIVE):
        try:
            s.close()
        except Exception:  # noqa: BLE001 — atexit best effort
            pass


atexit.register(_close_all_live)


class VideoStream:
    """A live playback stream: the broker side of the §3c seam, shaped
    like decodesvc.StreamHandle (next_frame returns the raw validated
    tuple rather than a PixelBuffer because frames cross as rgb24, which
    is deliberately not in the stills PixelFormat enum).

    Threading: a reader thread drains worker stdout into thread-safe
    queues; next_frame/next_audio/free_slot/seek/close are called from
    the playback thread. Every frame/audio description is validated
    against the design §2.4 checklist BEFORE being exposed; any failure
    kills the worker and latches an error state — no retry (per §1 a
    protocol violation marks the file suspect; the viewer shows the
    poster plus an honest error note).

    Callers must copy pixels they keep out of a frame's memoryview
    before free_slot(slot) — after freeing, the worker may rewrite that
    memory (§2.4 item 6). A seek() invalidates ALL outstanding frame
    views (ring-flush semantics: every slot returns to the worker when
    the seek is acknowledged)."""

    def __init__(self, path, max_edge: int = DEFAULT_MAX_EDGE,
                 slots: int = DEFAULT_SLOTS, *,
                 open_timeout_s: float = 15.0,
                 _test_frame_hook=None) -> None:
        from multiprocessing import shared_memory

        if not (1 <= slots <= 64 and 16 <= max_edge <= 16_384):
            raise ValueError("unreasonable ring geometry")
        self._slots = slots
        self._max_edge = max_edge
        self._layout = _Layout(slots, max_edge)
        self._test_frame_hook = _test_frame_hook

        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._frames: queue.Queue = queue.Queue()
        self._audioq: queue.Queue = queue.Queue()
        self._epoch = 0            # bumped on every seek_ack (ring flush)
        self._eof_epoch = -1       # epoch whose stream reached EOF
        self._error: str | None = None
        self._closed = False
        self._info: StreamInfo | None = None
        self._info_evt = threading.Event()
        self._fillable = set(range(slots))     # slots the worker may fill
        self._owned: set[int] = set()          # slots held broker-side
        self._audio_fillable = set(range(AUDIO_SLOTS))
        self._views: dict[int, memoryview] = {}
        self._last_progress = time.monotonic()
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._shm = None

        # SECURITY RESIDUAL: name-based shm segment (see module docstring)
        # until fauxcasa-i92.3's anonymous-capability arena.
        self._shm = shared_memory.SharedMemory(
            create=True, size=self._layout.total_bytes)
        self.shm_name = self._shm.name

        try:
            self._spawn(str(path))
        except BaseException:
            self.close()
            raise
        _LIVE.add(self)

        if not self._info_evt.wait(open_timeout_s) or self._info is None:
            detail = self._error or "worker produced no stream info"
            self.close()
            code = ErrorCode.PROTOCOL if "PROTOCOL" in detail else ErrorCode.CORRUPT
            raise DecodeServiceError(code, detail)

    # -- lifecycle ----------------------------------------------------------

    def _spawn(self, path: str) -> None:
        argv = [sys.executable, os.path.abspath(__file__), "--worker",
                "--shm", self.shm_name,
                "--slots", str(self._slots),
                "--max-edge", str(self._max_edge)]
        # The broker opens the file; the worker NEVER sees a path — only
        # the already-open, read-only descriptor (design §2 item 2).
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            if os.name == "posix":
                # pass_fds preserves the fd number across exec, so the
                # number itself is the reference in the open request.
                self._proc = subprocess.Popen(
                    argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    pass_fds=(fd,), close_fds=True)
                file_ref = {"fd": fd}
            else:
                # Windows: inherit the OS handle; the child re-opens it
                # via msvcrt.open_osfhandle + os.fdopen("rb").
                import msvcrt

                handle = msvcrt.get_osfhandle(fd)
                os.set_handle_inheritable(handle, True)
                self._proc = subprocess.Popen(
                    argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    close_fds=False)
                file_ref = {"handle": handle}
        finally:
            # The child holds its own copy now (or spawn failed).
            os.close(fd)
        self._reader = threading.Thread(
            target=self._read_loop, name="videostream-reader", daemon=True)
        self._reader.start()
        self._send_req({"op": "stream_open", "slots": self._slots,
                        "max_edge": self._max_edge, "pixfmt": "RGB24",
                        **file_ref})

    def _send_req(self, obj: dict) -> None:
        with self._send_lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.stdin.closed:
                return
            try:
                _send_msg(proc.stdin, obj)
            except (OSError, ValueError):
                pass  # worker death is surfaced by the reader thread

    def _kill_worker(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def _latch_error(self, msg: str) -> None:
        with self._state_lock:
            if self._error is None and not self._closed:
                self._error = msg

    def close(self) -> None:
        """Idempotent teardown: always reaps the child and unlinks the
        shm segment; safe to call twice and from atexit."""
        with self._state_lock:
            already = self._closed
            self._closed = True
        _LIVE.discard(self)
        if already:
            return
        self._send_req({"op": "stream_close"})
        self._kill_worker()
        proc = self._proc
        if proc is not None:
            for fp in (proc.stdin, proc.stdout):
                try:
                    if fp is not None:
                        fp.close()
                except OSError:
                    pass
        if self._reader is not None and self._reader.is_alive() \
                and threading.current_thread() is not self._reader:
            self._reader.join(timeout=2)
        # Invalidate every view handed out so shm can unmap; a caller
        # still holding one gets ValueError on access, never stale reads.
        for mv in list(self._views.values()):
            try:
                mv.release()
            except BufferError:
                pass
        self._views.clear()
        if self._shm is not None:
            try:
                self._shm.close()
            except BufferError:
                pass  # a caller-exported sub-view pins the map; unlink anyway
            try:
                self._shm.unlink()
            except FileNotFoundError:
                pass
        self._info_evt.set()

    # -- reader thread ------------------------------------------------------

    def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                msg = _recv_msg(proc.stdout)
                if msg is None:  # clean EOF: worker exited
                    if not self._closed:
                        self._latch_error(
                            "WORKER_CRASHED: worker exited mid-stream")
                    return
                self._last_progress = time.monotonic()
                self._dispatch(msg)
        except (ProtocolViolation, DecodeServiceError) as e:
            self._latch_error(f"PROTOCOL: {getattr(e, 'detail', e)}")
            self._kill_worker()
        except (OSError, ValueError):
            if not self._closed:
                self._latch_error("WORKER_CRASHED: control channel lost")
        finally:
            self._info_evt.set()

    def _dispatch(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "frame":
            if self._test_frame_hook is not None:
                msg = self._test_frame_hook(msg)
            self._accept_frame(msg)
        elif t == "audio":
            self._accept_audio(msg)
        elif t == "eof":
            with self._state_lock:
                self._eof_epoch = self._epoch
        elif t == "seek_ack":
            # §3c ring flush: after the ack every slot returns to the
            # worker; anything queued or held from before is stale.
            with self._state_lock:
                self._epoch += 1
                self._eof_epoch = -1
                self._fillable = set(range(self._slots))
                self._owned.clear()
                self._audio_fillable = set(range(AUDIO_SLOTS))
                views, self._views = self._views, {}
            for mv in views.values():
                try:
                    mv.release()
                except BufferError:
                    pass
        elif t == "info":
            self._accept_info(msg)
        elif t == "error":
            code = msg.get("code")
            detail = msg.get("detail")
            if not isinstance(code, str) or not isinstance(detail, str):
                raise ProtocolViolation("malformed error message")
            self._latch_error(f"{code[:32]}: {detail[:512]}")
            self._info_evt.set()
        else:
            raise ProtocolViolation(f"unknown message type {t!r}")

    def _accept_info(self, msg: dict) -> None:
        w, h = _as_int(msg, "w"), _as_int(msg, "h")
        if not (0 < w <= self._max_edge and 0 < h <= self._max_edge):
            raise ProtocolViolation(f"info dims {w}x{h} out of range")
        duration_us = _as_int(msg, "duration_us")
        rate = _as_int(msg, "audio_rate")
        channels = _as_int(msg, "audio_channels")
        fps = msg.get("fps")
        if duration_us < 0 or not isinstance(fps, (int, float)) \
                or isinstance(fps, bool) or not (0 <= fps <= 100_000) \
                or not (0 <= rate <= 1_000_000) or not (0 <= channels <= 2):
            raise ProtocolViolation("info fields out of range")
        self._info = StreamInfo(
            duration_us=duration_us, fps=float(fps), w=w, h=h,
            has_audio=bool(msg.get("has_audio")),
            audio_rate=rate, audio_channels=channels)
        self._info_evt.set()

    def _accept_frame(self, msg: dict) -> None:
        """Design §2.4 checklist, applied before ANY trusted code sees
        the buffer. Overflow-safe by Python ints."""
        slot = _as_int(msg, "slot")
        pts_us = _as_int(msg, "pts_us")
        w, h = _as_int(msg, "w"), _as_int(msg, "h")
        stride, off, ln = (_as_int(msg, "stride"), _as_int(msg, "off"),
                           _as_int(msg, "len"))
        lay = self._layout
        if not (0 <= slot < self._slots):
            raise ProtocolViolation(f"slot {slot} out of range")
        if not (0 < w <= self._max_edge and 0 < h <= self._max_edge):
            raise ProtocolViolation(f"frame dims {w}x{h} out of range")
        if stride < w * _RGB_BPP or stride % 4 != 0:
            raise ProtocolViolation(f"stride {stride} invalid for w={w}")
        if ln != stride * h:
            raise ProtocolViolation(f"len {ln} != stride*h {stride * h}")
        if off < 0 or off % 4 != 0 or off + ln > lay.total_bytes:
            raise ProtocolViolation(f"buffer [{off},{off + ln}) outside arena")
        base = lay.frame_slot_base(slot)
        if off < base or off + ln > base + lay.frame_slot_bytes:
            raise ProtocolViolation(
                f"buffer [{off},{off + ln}) outside claimed slot {slot}")
        with self._state_lock:
            if slot not in self._fillable:
                raise ProtocolViolation(
                    f"slot {slot} written while not worker-owned")
            self._fillable.discard(slot)
            self._owned.add(slot)
            epoch = self._epoch
        self._frames.put((epoch, slot, pts_us, w, h, stride, off, ln))

    def _accept_audio(self, msg: dict) -> None:
        pts_us = _as_int(msg, "pts_us")
        off, ln = _as_int(msg, "off"), _as_int(msg, "len")
        lay = self._layout
        if ln <= 0 or ln % 2 != 0 or ln > lay.audio_slot_bytes:
            raise ProtocolViolation(f"audio len {ln} invalid")
        if off % 4 != 0 or off < lay.audio_base \
                or off + ln > lay.audio_base + AUDIO_SLOTS * lay.audio_slot_bytes:
            raise ProtocolViolation(f"audio chunk [{off},{off + ln}) outside "
                                    "audio region")
        if (off - lay.audio_base) % lay.audio_slot_bytes != 0:
            raise ProtocolViolation(f"audio off {off} not slot-aligned")
        slot = (off - lay.audio_base) // lay.audio_slot_bytes
        with self._state_lock:
            if slot not in self._audio_fillable:
                raise ProtocolViolation(
                    f"audio slot {slot} written while not worker-owned")
            self._audio_fillable.discard(slot)
            epoch = self._epoch
        # Copy the PCM out of the arena immediately (§2.4 item 6), then
        # hand the slot straight back — the audio ring is a staging
        # buffer, its accounting purely worker backpressure.
        data = bytes(self._shm.buf[off:off + ln])
        self._audioq.put((epoch, data, pts_us))
        with self._state_lock:
            self._audio_fillable.add(slot)
        self._send_req({"op": "audio_free", "slot": slot})

    # -- StreamHandle-shaped API ---------------------------------------------

    def info(self) -> StreamInfo:
        assert self._info is not None
        return self._info

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def at_eof(self) -> bool:
        with self._state_lock:
            return self._eof_epoch == self._epoch and self._frames.empty()

    def next_frame(self, timeout_ms: int = 1000):
        """The next decoded frame as (slot, pts_us, w, h, stride,
        memoryview-of-rgb24-rows), or None on eof / timeout / error /
        closed (check .at_eof / .error to tell which). The memoryview is
        a window into shared memory: copy what you keep, then
        free_slot(slot). Enforces the 5 s per-frame progress deadline:
        a worker that owes a frame and shows no progress is killed."""
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        while True:
            if self._closed or self._error is not None:
                return None
            with self._state_lock:
                epoch = self._epoch
                eof = self._eof_epoch == epoch
            if eof and self._frames.empty():
                return None
            wait = min(0.1, max(0.0, deadline - time.monotonic()))
            try:
                item = self._frames.get(timeout=wait) if wait > 0 \
                    else self._frames.get_nowait()
            except queue.Empty:
                item = None
            if item is not None:
                iepoch, slot, pts_us, w, h, stride, off, ln = item
                if iepoch != epoch:
                    continue  # stale pre-seek frame: slot already flushed
                mv = self._shm.buf[off:off + ln]
                self._views[slot] = mv
                return (slot, pts_us, w, h, stride, mv)
            self._check_stall()
            if time.monotonic() >= deadline:
                return None

    def next_audio(self):
        """The next PCM s16 chunk as (bytes, pts_us), or None when no
        audio is queued (non-blocking; chunks are already copied out of
        the arena, so no free call exists or is needed)."""
        while True:
            try:
                epoch, data, pts_us = self._audioq.get_nowait()
            except queue.Empty:
                return None
            with self._state_lock:
                if epoch == self._epoch:
                    return (data, pts_us)
            # stale pre-seek audio: drop and keep draining

    def free_slot(self, slot: int) -> None:
        """Return a frame slot to the worker. The frame's memoryview is
        invalidated (release()) — copy first. Idempotent per slot; slots
        flushed by a seek are already back with the worker and ignored."""
        with self._state_lock:
            if self._closed or slot not in self._owned:
                return
            self._owned.discard(slot)
            self._fillable.add(slot)
            mv = self._views.pop(slot, None)
        if mv is not None:
            try:
                mv.release()
            except BufferError:
                pass
        # Fresh demand starts now; don't count ring-full backpressure
        # time against the 5 s progress deadline.
        self._last_progress = time.monotonic()
        self._send_req({"op": "stream_free", "slot": int(slot)})

    def seek(self, pts_us: int, timeout_ms: int = 5000) -> None:
        """Seek to the nearest keyframe at or before pts_us. Blocks
        until the worker acknowledges (then ALL ring slots are flushed
        back to the worker and every outstanding frame view is
        invalidated — §3c semantics); a worker that never acks within
        the deadline is killed."""
        with self._state_lock:
            if self._closed or self._error is not None:
                return
            target = self._epoch + 1
        self._last_progress = time.monotonic()
        self._send_req({"op": "stream_seek", "pts_us": max(0, int(pts_us))})
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            with self._state_lock:
                if self._epoch >= target:
                    return
            if self._closed or self._error is not None:
                return
            time.sleep(0.005)
        self._latch_error("TIMEOUT: seek not acknowledged")
        self._kill_worker()

    # -- timeouts -----------------------------------------------------------

    def _check_stall(self) -> None:
        """§1 streaming deadline: the worker owes a frame (it holds
        fillable slots, the stream is not at EOF) and has made no
        progress for 5 s -> kill, error state, no retry."""
        with self._state_lock:
            owes = (bool(self._fillable)
                    and self._eof_epoch != self._epoch
                    and self._error is None and not self._closed)
        if not owes:
            return
        proc = self._proc
        if proc is not None and proc.poll() is not None:
            self._latch_error("WORKER_CRASHED: worker exited mid-stream")
            return
        if time.monotonic() - self._last_progress > FRAME_DEADLINE_S:
            self._latch_error("TIMEOUT: no frame progress within deadline")
            self._kill_worker()


# ---------------------------------------------------------------------------
# Worker side. Everything below runs in the child process only; imports
# beyond stdlib happen inside functions so the broker side never pays
# for (or accidentally leans on) them.


class _SeekRequested(Exception):
    def __init__(self, pts_us: int) -> None:
        super().__init__(pts_us)
        self.pts_us = pts_us


class _CloseRequested(Exception):
    pass


class _WorkerIO:
    """Worker-side control + arena state: slot accounting for both
    rings, a control-reader thread feeding a queue, framed sends."""

    def __init__(self, stdin, stdout, shm, layout: _Layout) -> None:
        self.stdout = stdout
        self.shm = shm
        self.layout = layout
        self.free_slots = set(range(layout.slots))
        self.audio_outstanding = 0
        self.audio_next = 0
        self.ctlq: queue.Queue = queue.Queue()

        def _pump_loop() -> None:
            try:
                while True:
                    msg = _recv_msg(stdin)
                    if msg is None:
                        break
                    self.ctlq.put(msg)
            except Exception:  # noqa: BLE001 — any control failure = close
                pass
            finally:
                # Control EOF means the broker is gone: exit with it
                # (the §1 lifetime tie, pre-sandbox edition).
                self.ctlq.put({"op": "stream_close"})

        self._thread = threading.Thread(target=_pump_loop, daemon=True)
        self._thread.start()

    def send(self, obj: dict) -> None:
        try:
            _send_msg(self.stdout, obj)
        except (OSError, ValueError):
            os._exit(1)  # broker gone: die with it (lifetime tie, §1)

    def _handle(self, msg: dict) -> None:
        op = msg.get("op")
        if op == "stream_free":
            slot = msg.get("slot")
            if isinstance(slot, int) and 0 <= slot < self.layout.slots:
                self.free_slots.add(slot)
        elif op == "audio_free":
            # Clamped: acks for pre-seek chunks may straggle in after a
            # seek reset zeroed the outstanding count.
            self.audio_outstanding = max(0, self.audio_outstanding - 1)
        elif op == "stream_seek":
            raise _SeekRequested(max(0, int(msg.get("pts_us", 0))))
        elif op == "stream_close":
            raise _CloseRequested()
        else:
            raise _CloseRequested()  # unknown op from our own broker

    def poll_control(self, block: bool = False) -> None:
        """Drain pending control messages; with block=True wait for at
        least one (used while backpressure-blocked on a full ring).
        Raises _SeekRequested/_CloseRequested out to the decode loop."""
        first = block
        while True:
            try:
                msg = self.ctlq.get(block=first, timeout=0.5 if first else None) \
                    if first else self.ctlq.get_nowait()
            except queue.Empty:
                return
            first = False
            self._handle(msg)

    def acquire_frame_slot(self) -> int:
        while not self.free_slots:
            self.poll_control(block=True)
        return self.free_slots.pop()

    def acquire_audio_slot(self) -> int:
        while self.audio_outstanding >= self.layout.audio_slots:
            self.poll_control(block=True)
        slot = self.audio_next
        self.audio_next = (self.audio_next + 1) % self.layout.audio_slots
        self.audio_outstanding += 1
        return slot

    def reset_after_seek(self) -> None:
        # §3c ring flush: after the ack the broker treats every slot as
        # worker-owned again, so mirror that here.
        self.free_slots = set(range(self.layout.slots))
        self.audio_outstanding = 0


def _fit_dims(w: int, h: int, max_edge: int) -> tuple[int, int]:
    if w <= max_edge and h <= max_edge:
        return w, h
    scale = max_edge / max(w, h)
    return max(1, int(w * scale)), max(1, int(h * scale))


def _frame_to_padded_rgb(frame, max_edge: int) -> tuple[bytes, int, int, int]:
    """Decoded VideoFrame -> (rgb24 rows padded to stride%4==0, w, h,
    stride), scaled down if a side exceeds max_edge. The stride-stripping
    core mirrors videoload._frame_rgb; the %4 pad is the §2.4 stride
    invariant the broker validates."""
    w, h = _fit_dims(frame.width, frame.height, max_edge)
    if (w, h) != (frame.width, frame.height):
        rgb = frame.reformat(width=w, height=h, format="rgb24")
    else:
        rgb = frame.reformat(format="rgb24")
    plane = rgb.planes[0]
    w, h = rgb.width, rgb.height
    src = bytes(plane)
    src_stride = plane.line_size
    dst_stride = _align4(w * _RGB_BPP)
    if src_stride == dst_stride and len(src) >= dst_stride * h:
        return src[:dst_stride * h], w, h, dst_stride
    row = w * _RGB_BPP
    out = bytearray(dst_stride * h)
    for y in range(h):
        out[y * dst_stride:y * dst_stride + row] = \
            src[y * src_stride:y * src_stride + row]
    return bytes(out), w, h, dst_stride


def _pts_us(frame) -> int:
    t = frame.time
    return int(round(t * 1_000_000)) if t is not None else -1


def _emit_video(ws: _WorkerIO, frame, max_edge: int) -> None:
    buf, w, h, stride = _frame_to_padded_rgb(frame, max_edge)
    slot = ws.acquire_frame_slot()  # backpressure: blocks on a full ring
    base = ws.layout.frame_slot_base(slot)
    ws.shm.buf[base:base + len(buf)] = buf
    ws.send({"type": "frame", "slot": slot, "pts_us": _pts_us(frame),
             "w": w, "h": h, "stride": stride, "off": base, "len": len(buf)})


def _emit_audio(ws: _WorkerIO, frame, channels: int) -> None:
    n = frame.samples * channels * 2  # s16 interleaved
    data = bytes(frame.planes[0])[:n]
    pts = _pts_us(frame)
    lay = ws.layout
    for i in range(0, len(data), lay.audio_slot_bytes):
        chunk = data[i:i + lay.audio_slot_bytes]
        if len(chunk) < 2:
            break
        if len(chunk) % 2:
            chunk = chunk[:-1]
        slot = ws.acquire_audio_slot()
        base = lay.audio_slot_base(slot)
        ws.shm.buf[base:base + len(chunk)] = chunk
        ws.send({"type": "audio", "off": base, "len": len(chunk),
                 "pts_us": pts})


def _attach_shm(name: str):
    """Attach by name without letting this process's resource tracker
    unlink the broker's segment when the worker dies (track=False on
    3.13+, manual unregister before that)."""
    from multiprocessing import shared_memory

    try:
        return shared_memory.SharedMemory(name=name, track=False)
    except TypeError:
        seg = shared_memory.SharedMemory(name=name)
        try:
            from multiprocessing import resource_tracker

            resource_tracker.unregister(seg._name, "shared_memory")
        except Exception:  # noqa: BLE001 — best effort
            pass
        return seg


def _open_handed_file(msg: dict):
    """The open request's descriptor -> a seekable binary file object.
    The worker NEVER opens by path."""
    if os.name == "posix":
        fd = msg.get("fd")
        if not isinstance(fd, int) or fd < 3:
            raise ValueError(f"bad fd in stream_open: {fd!r}")
        return os.fdopen(fd, "rb")
    import msvcrt

    handle = msg.get("handle")
    if not isinstance(handle, int):
        raise ValueError(f"bad handle in stream_open: {handle!r}")
    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    return os.fdopen(fd, "rb")


def _worker_main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", action="store_true", required=True)
    ap.add_argument("--shm", required=True)
    ap.add_argument("--slots", type=int, required=True)
    ap.add_argument("--max-edge", type=int, required=True)
    args = ap.parse_args(argv)

    if os.name == "nt":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer

    layout = _Layout(args.slots, args.max_edge)

    def fail(code: str, detail: str) -> int:
        try:
            _send_msg(stdout, {"type": "error", "code": code,
                               "detail": detail[:512]})
        except OSError:
            pass
        return 2

    try:
        open_msg = _recv_msg(stdin)
    except ProtocolViolation as e:
        return fail("PROTOCOL", str(e))
    if open_msg is None or open_msg.get("op") != "stream_open" \
            or open_msg.get("slots") != args.slots \
            or open_msg.get("max_edge") != args.max_edge \
            or open_msg.get("pixfmt") != "RGB24":
        return fail("PROTOCOL", f"bad stream_open: {open_msg!r}")

    try:
        fileobj = _open_handed_file(open_msg)
    except (OSError, ValueError) as e:
        return fail("PROTOCOL", f"cannot adopt descriptor: {e}")

    shm = _attach_shm(args.shm)

    import av  # the one heavy dependency this worker exists to contain

    try:
        container = av.open(fileobj)  # seekable file object, never a path
    except Exception as e:  # noqa: BLE001 — hostile bytes land here
        return fail("CORRUPT", f"container open failed: {e}")

    vstream = next((s for s in container.streams if s.type == "video"), None)
    if vstream is None:
        return fail("UNSUPPORTED", "no video stream")
    astream = next((s for s in container.streams if s.type == "audio"), None)

    cc = astream.codec_context if astream is not None else None
    src_channels = 0
    src_rate = 0
    if cc is not None:
        src_channels = getattr(cc, "channels", None) \
            or getattr(getattr(cc, "layout", None), "nb_channels", 0) or 0
        src_rate = cc.sample_rate or 0
    if astream is not None and (src_channels <= 0 or src_rate <= 0):
        astream = None  # unusable audio: stream video-only, honestly
    out_channels = min(src_channels, 2) if astream is not None else 0

    duration_us = 0
    if container.duration is not None and container.duration > 0:
        duration_us = int(container.duration)  # av.time_base is 1/1e6
    elif vstream.duration is not None and vstream.time_base is not None:
        duration_us = int(vstream.duration * vstream.time_base * 1_000_000)
    fps = 0.0
    rate = vstream.average_rate or vstream.guessed_rate
    if rate:
        fps = float(rate)
    w, h = _fit_dims(vstream.codec_context.width or 1,
                     vstream.codec_context.height or 1, args.max_edge)

    ws = _WorkerIO(stdin, stdout, shm, layout)
    ws.send({"type": "info", "duration_us": max(0, duration_us), "fps": fps,
             "w": w, "h": h, "has_audio": astream is not None,
             "audio_rate": src_rate if astream is not None else 0,
             "audio_channels": out_channels})

    streams = [vstream] + ([astream] if astream is not None else [])
    pending_seek: int | None = None
    try:
        while True:
            if pending_seek is not None:
                try:
                    # Nearest keyframe at or before pts (av.time_base us).
                    container.seek(pending_seek, backward=True)
                except Exception:  # noqa: BLE001 — a failed seek: rewind
                    container.seek(0)
                vstream.codec_context.flush_buffers()
                if astream is not None:
                    astream.codec_context.flush_buffers()
                ws.reset_after_seek()
                ws.send({"type": "seek_ack", "pts_us": pending_seek})
                pending_seek = None
            try:
                resampler = None
                if astream is not None:
                    resampler = av.AudioResampler(
                        format="s16",
                        layout="stereo" if out_channels == 2 else "mono",
                        rate=src_rate)
                for packet in container.demux(*streams):
                    ws.poll_control()  # seeks/frees between packets
                    for frame in packet.decode():
                        if packet.stream.type == "video":
                            _emit_video(ws, frame, args.max_edge)
                        elif resampler is not None:
                            for out in resampler.resample(frame):
                                _emit_audio(ws, out, out_channels)
                if resampler is not None:
                    for out in resampler.resample(None):
                        _emit_audio(ws, out, out_channels)
                ws.send({"type": "eof"})
                while True:  # parked at EOF: only a seek resumes decode
                    ws.poll_control(block=True)
            except _SeekRequested as s:
                pending_seek = s.pts_us
    except _CloseRequested:
        pass
    except Exception as e:  # noqa: BLE001 — decode failure mid-stream
        try:
            _send_msg(stdout, {"type": "error", "code": "CORRUPT",
                               "detail": str(e)[:512]})
        except OSError:
            pass
        return 3
    finally:
        try:
            container.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            shm.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    if "--worker" in sys.argv[1:]:
        sys.exit(_worker_main(sys.argv[1:]))
    sys.stderr.write("videostream.py is a library plus a --worker "
                     "entrypoint; it has no other CLI.\n")
    sys.exit(2)
