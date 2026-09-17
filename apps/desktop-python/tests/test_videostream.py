"""Tests for videostream.py video playback worker and viewer integration.

Split from test_tracer.py (fauxcasa-l09); originally lines 18017-18507 of the monolith."""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path
import pytest
from tracer_helpers import (
    APP_DIR,
    _drain_stream,
    _make_clip,
    _make_stream_clip,
    _press,
    _pump,
    _spin,
    _video_viewer,
)


# ---------------------------------------------------------------------------
# Video playback streaming seam (fauxcasa-v46.3, decode-service §3c ruled
# option A): videostream.py spawns a separate worker PROCESS that decodes
# with PyAV from a handed-over open descriptor (never a path) and streams
# rgb24 frames through a shared-memory ring; audio crosses as PCM s16
# chunks through a dedicated sub-ring. The broker validates every buffer
# description against the §2.4 checklist before exposing it and treats
# any violation as evidence of compromise: kill the worker, latch an
# error, never retry. QtMultimedia decoding is DECLINED and never
# imported.
#
# Fixture provenance (privacy rule: NEVER real family data): clips are
# synthesized in-test with PyAV — _make_stream_clip extends _make_clip
# with an optional pcm_s16le mono sine track (mov container, which
# carries PCM audio; the numpy import rides the rawpy dependency).
# ---------------------------------------------------------------------------


def test_videostream_open_info_sane(tmp_path: Path) -> None:
    """stream_open on a 1 s clip with a mono sine track: info reports a
    ~1 s duration, the true dims and fps, and the audio parameters the
    worker will resample to (source rate kept, channels capped at 2)."""
    import videostream

    clip = _make_stream_clip(tmp_path / "clip.mov")   # 8 frames @ 8 fps
    vs = videostream.VideoStream(clip, max_edge=256)
    try:
        info = vs.info()
        assert 400_000 < info.duration_us < 2_500_000
        assert (info.w, info.h) == (64, 48)
        assert 6.0 < info.fps < 10.0
        assert info.has_audio is True
        assert info.audio_rate == 8000
        assert info.audio_channels == 1
    finally:
        vs.close()


def test_videostream_frames_monotonic_audio_flows(tmp_path: Path) -> None:
    """Frames arrive with strictly increasing pts, the clip's dims, a
    %4-padded stride, and a memoryview of exactly stride*h bytes; the
    sine track arrives as s16 PCM totalling ~1 s at the source rate."""
    import videostream

    clip = _make_stream_clip(tmp_path / "clip.mov")
    vs = videostream.VideoStream(clip, max_edge=256)
    try:
        frames, audio_bytes = _drain_stream(vs)
        assert vs.error is None
        assert len(frames) == 8
        pts = [f[0] for f in frames]
        assert pts == sorted(pts) and len(set(pts)) == len(pts)
        for _pts, w, h, stride, nbytes in frames:
            assert (w, h) == (64, 48)
            assert stride % 4 == 0 and stride >= w * 3
            assert nbytes == stride * h
        # 1 s of mono s16 at 8 kHz = 16000 bytes (allow container slack)
        assert 12_000 <= audio_bytes <= 20_000
    finally:
        vs.close()


def test_videostream_ring_recycles_long_stream(tmp_path: Path) -> None:
    """free_slot recycling: a 24-frame clip (3x the 8-slot ring) streams
    to completion, so backpressure + stream_free round-trips work."""
    import videostream

    clip = _make_stream_clip(tmp_path / "long.mov", nframes=24, audio=False)
    vs = videostream.VideoStream(clip, max_edge=256)
    try:
        frames, _ = _drain_stream(vs)
        assert vs.error is None
        assert len(frames) == 24
        assert vs.at_eof
    finally:
        vs.close()


def test_videostream_seek_lands_near_target(tmp_path: Path) -> None:
    """seek(mid) flushes the ring and resumes at the nearest keyframe at
    or before the target: with gop=4 @ 8 fps (0.5 s keyframe interval),
    the first post-seek frame is within one interval below 1.5 s, and
    the stream still runs to its 3 s end."""
    import videostream

    clip = _make_stream_clip(tmp_path / "seek.mov", nframes=24, gop=4,
                             audio=False)
    vs = videostream.VideoStream(clip, max_edge=256)
    try:
        first = vs.next_frame(timeout_ms=5000)
        assert first is not None
        vs.free_slot(first[0])
        vs.seek(1_500_000)
        assert vs.error is None
        frames, _ = _drain_stream(vs)
        assert vs.error is None
        assert frames, "no frames after seek"
        assert frames[0][0] >= 1_500_000 - 500_000   # target - one keyframe gap
        assert frames[0][0] <= 1_600_000             # never past the target+1fr
        pts = [f[0] for f in frames]
        assert pts == sorted(pts)
        assert pts[-1] >= 2_500_000                  # ran on to the clip's end
    finally:
        vs.close()


def test_videostream_close_idempotent_reaps_and_unlinks(tmp_path: Path) -> None:
    """close() twice is safe; the worker process is reaped (poll() is
    not None), the shm segment is unlinked (attaching by name fails),
    and a held frame view is invalidated rather than left dangling."""
    from multiprocessing import shared_memory

    import videostream

    clip = _make_stream_clip(tmp_path / "clip.mov", audio=False)
    vs = videostream.VideoStream(clip, max_edge=256)
    got = vs.next_frame(timeout_ms=5000)
    assert got is not None
    mv = got[5]
    name = vs.shm_name
    proc = vs._proc
    vs.close()
    vs.close()                                       # idempotent
    assert vs.closed
    assert proc is not None and proc.poll() is not None
    with pytest.raises(FileNotFoundError):
        shared_memory.SharedMemory(name=name)
    with pytest.raises(ValueError):
        mv[0]                                        # released, not dangling


def test_videostream_audio_backpressure_bounds_broker_memory(
        tmp_path: Path) -> None:
    """The 32-slot audio ring is REAL backpressure: the broker acks a
    chunk (audio_free) only when next_audio pops it, so with the consumer
    not draining audio the worker blocks and the broker never holds more
    than AUDIO_SLOTS chunks — a hostile file whose audio track far
    outlives its video (the shape that defeated a receipt-time ack: the
    video ring stops braking at video EOF) cannot balloon the trusted
    process. Draining afterwards releases the worker through to EOF."""
    import time as _time

    import videostream

    # 0.5 s of video but 30 s of 48 kHz mono PCM (~2.9 MB — well past the
    # ring's 32 x 64 KiB = 2 MiB) in one file.
    clip = _make_stream_clip(tmp_path / "longaudio.mov", nframes=4,
                             audio=True, audio_rate=48000,
                             audio_seconds=30.0)
    vs = videostream.VideoStream(clip, max_edge=256)
    try:
        # Consume video only; audio stays unpopped.
        deadline = _time.monotonic() + 15
        frames = 0
        while _time.monotonic() < deadline and frames < 4:
            got = vs.next_frame(timeout_ms=200)
            if got is not None:
                vs.free_slot(got[0])
                frames += 1
        assert frames == 4
        _time.sleep(0.5)                       # let the worker run ahead
        assert vs.error is None
        assert vs._audioq.qsize() <= videostream.AUDIO_SLOTS
        assert not vs.at_eof                   # worker blocked mid-track
        # Now drain: consumption acks slots back and the stream finishes.
        audio_bytes = 0
        deadline = _time.monotonic() + 20
        while _time.monotonic() < deadline and not vs.at_eof \
                and vs.error is None:
            chunk = vs.next_audio()
            if chunk is not None:
                audio_bytes += len(chunk[0])
            else:
                _time.sleep(0.01)
        while (chunk := vs.next_audio()) is not None:
            audio_bytes += len(chunk[0])
        assert vs.error is None
        assert vs.at_eof
        assert audio_bytes >= 2_700_000        # ~30 s of 48 kHz mono s16
    finally:
        vs.close()


def test_videostream_no_audio_clip(tmp_path: Path) -> None:
    """A clip without an audio track streams video fine: has_audio False,
    zero audio chunks, all frames delivered."""
    import videostream

    clip = _make_clip(tmp_path / "mute.mp4")         # v46.2 fixture, no audio
    vs = videostream.VideoStream(clip, max_edge=256)
    try:
        info = vs.info()
        assert info.has_audio is False
        assert info.audio_rate == 0 and info.audio_channels == 0
        frames, audio_bytes = _drain_stream(vs)
        assert vs.error is None
        assert len(frames) == 8
        assert audio_bytes == 0
        assert vs.next_audio() is None
    finally:
        vs.close()


def test_main_dispatches_worker_before_argparse() -> None:
    """main.py hands --worker to videostream._worker_main BEFORE its own
    argparse (which would exit 2 with usage text on the unknown flag): a
    FROZEN bundle spawns its playback worker as [sys.executable,
    "--worker", ...] where sys.executable IS the app, so this dispatch is
    what keeps video playback alive in the shipped artifact. With stdin
    at EOF the worker exits 2 through the WIRE protocol — a framed
    PROTOCOL error on stdout, never argparse's usage text."""
    import subprocess

    main_py = str(APP_DIR / "main.py")
    out = subprocess.run(
        [sys.executable, main_py, "--worker", "--shm", "nosuch",
         "--slots", "8", "--max-edge", "256"],
        input=b"", capture_output=True, timeout=120)
    assert out.returncode == 2
    assert b"usage:" not in out.stderr and b"usage:" not in out.stdout
    n = struct.unpack("<I", out.stdout[:4])[0]
    msg = json.loads(out.stdout[4:4 + n].decode("utf-8"))
    assert msg["type"] == "error"
    assert msg["code"] == "PROTOCOL"


def test_videostream_protocol_violation_kills_worker(tmp_path: Path) -> None:
    """A frame message whose len != stride*h (injected via the broker's
    test hook, i.e. exactly what a lying worker would send) is treated
    as evidence of compromise: the worker is killed, an error state is
    latched, next_frame yields nothing, and there is no retry."""
    import time as _time

    import videostream

    clip = _make_stream_clip(tmp_path / "evil.mov", audio=False)
    vs = videostream.VideoStream(
        clip, max_edge=256,
        _test_frame_hook=lambda m: {**m, "len": m["len"] + 4})
    try:
        deadline = _time.monotonic() + 10
        while vs.error is None and _time.monotonic() < deadline:
            assert vs.next_frame(timeout_ms=200) is None
        assert vs.error is not None and "PROTOCOL" in vs.error
        assert vs._proc is not None and vs._proc.poll() is not None
        assert vs.next_frame(timeout_ms=50) is None
    finally:
        vs.close()


# ---------------------------------------------------------------------------
# Viewer playback integration (fauxcasa-v46.3, task 2): the ViewerPage
# plays videos through the videostream seam. The poster stays the pre-play
# stand-in with a painted centered play affordance + transport strip; P
# plays/pauses; Ctrl+Left/Right seek ±5 s; playback stops cleanly on
# navigation / hide / quiesce (worker process reaped, shm unlinked); a
# stream failure reverts to the poster with an honest note. Audio rides a
# GUARDED QAudioSink (None on headless CI -> wall clock), so nothing here
# requires an audio device. Offscreen-safe: ticks are pumped via _spin.
# ---------------------------------------------------------------------------


def test_viewer_video_shows_poster_and_play_affordance(tmp_path: Path) -> None:
    """Pre-play, a video photo paints its poster with the centered play
    affordance (white triangle over the disc — checked by pixel at the
    widget center) and the transport strip; no playback session exists."""
    app, v, cat, idx = _video_viewer(tmp_path)
    assert _spin(app, lambda: v.image is not None)     # poster landed
    assert v._video is None                            # nothing auto-plays
    img = v.grab().toImage()
    c = img.pixelColor(v.width() // 2, v.height() // 2)
    assert c.red() > 180 and c.green() > 180 and c.blue() > 180  # triangle
    strip = v._transport_rect()
    assert strip.width() == v.width() and strip.height() > 0
    sc = img.pixelColor(strip.center())                # strip painted dark
    assert sc.red() < 120 and sc.blue() < 120
    v.quiesce()


def test_viewer_video_play_streams_frames(tmp_path: Path) -> None:
    """P starts a playback session over the sandboxed stream; at least one
    streamed frame is presented (viewer state swaps from the poster to
    pb.frame), the info bar shows position/duration, and the audio path
    (guarded QAudioSink or wall-clock fallback) never crashes headless."""
    from PySide6.QtCore import Qt

    app, v, cat, idx = _video_viewer(tmp_path, audio=True, nframes=16)
    assert _spin(app, lambda: v.image is not None)
    _press(v, Qt.Key.Key_P)                            # keymap viewer.play_pause
    # The stream opens ASYNCHRONOUSLY (worker spawn off the GUI thread —
    # pressing P must never freeze the window), so spin for the session.
    assert _spin(app, lambda: v._video is not None, 20.0)   # session opened
    assert _spin(app, lambda: v._video is not None
                 and v._video.frame is not None, 20.0)  # a frame painted
    assert v._video_note is None
    assert v._video.stream.error is None
    txt = v._info_text(cat.photos[idx])
    assert "video" in txt and "/" in txt               # position / duration
    assert not v.grab().isNull()                       # frame + chrome paint
    v.quiesce()


def test_viewer_video_pause_holds_position_and_seek_keys(tmp_path: Path) -> None:
    """P again pauses: the position freezes (pumping the loop does not
    advance it) and the shown frame stays. Ctrl+Right seeks +5 s (clamped
    to the clip) WITHOUT resuming — seek preserves the pause state — and
    P resumes."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    app, v, cat, idx = _video_viewer(tmp_path, nframes=24)   # 3 s clip
    assert _spin(app, lambda: v.image is not None)
    v._video_play_pause()                              # programmatic play
    assert _spin(app, lambda: v._video is not None
                 and v._video.frame is not None, 20.0)
    _press(v, Qt.Key.Key_P)                            # pause
    pb = v._video
    assert pb is not None and not pb.playing
    pos = pb.position_us
    _pump(app, 0.6)                                    # ticks keep firing...
    assert pb.position_us == pos                       # ...position frozen
    assert pb.frame is not None                        # frame held
    v.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right,
                              Qt.KeyboardModifier.ControlModifier))
    # Seek is fire-and-forget (never blocks the GUI thread); the position
    # updates when the worker's ack lands in a tick — spin for it.
    expected = min(pos + 5_000_000, pb.info.duration_us)  # ±5 s, clamped
    assert _spin(app, lambda: pb.position_us == expected, 10.0)
    assert not pb.playing                              # still paused
    _press(v, Qt.Key.Key_P)                            # resume
    assert pb.playing
    v.quiesce()


def test_viewer_video_navigation_stops_stream(tmp_path: Path) -> None:
    """Navigating next mid-playback stops the session cleanly: the stream
    closes, the worker child is reaped, and the next photo (a still)
    shows normally."""
    app, v, cat, idx = _video_viewer(tmp_path, nframes=24)
    assert _spin(app, lambda: v.image is not None)
    v._video_play_pause()
    assert _spin(app, lambda: v._video is not None
                 and v._video.frame is not None, 20.0)
    stream = v._video.stream
    proc = stream._proc
    v._step(1)                                         # -> still.jpg
    assert v._video is None
    assert stream.closed
    assert proc is not None and proc.poll() is not None  # child reaped
    assert _spin(app, lambda: v.image is not None)     # the still loads
    v.quiesce()


def test_viewer_video_quiesce_reaps_everything(tmp_path: Path) -> None:
    """quiesce() mid-playback leaks nothing: session gone, consumer timer
    stopped, worker process reaped, shm arena unlinked — the same
    test-safe teardown discipline as the decode worker."""
    from multiprocessing import shared_memory

    app, v, cat, idx = _video_viewer(tmp_path, audio=True, nframes=24)
    assert _spin(app, lambda: v.image is not None)
    v._video_play_pause()
    assert _spin(app, lambda: v._video is not None
                 and v._video.frame is not None, 20.0)
    pb = v._video
    stream, proc, name = pb.stream, pb.stream._proc, pb.stream.shm_name
    v.quiesce()
    assert v._video is None
    assert not pb._timer.isActive()                    # consumer loop stopped
    assert stream.closed
    assert proc is not None and proc.poll() is not None
    with pytest.raises(FileNotFoundError):
        shared_memory.SharedMemory(name=name)          # arena unlinked
    v.quiesce()                                        # idempotent


def test_viewer_video_star_toggle_still_works(tmp_path: Path) -> None:
    """Space keeps meaning star toggle on a video photo — before AND
    during playback (the v46.3 bindings claim P and Ctrl+arrows only;
    Space/arrows are untouched)."""
    from PySide6.QtCore import Qt

    app, v, cat, idx = _video_viewer(tmp_path)
    assert _spin(app, lambda: v.image is not None)
    requested: list[int] = []
    v.star_toggle_requested.connect(requested.append)
    _press(v, Qt.Key.Key_Space)                        # on the poster
    assert requested == [idx]
    v._video_play_pause()
    assert _spin(app, lambda: v._video is not None, 20.0)  # async open lands
    _press(v, Qt.Key.Key_Space)                        # while playing
    assert requested == [idx, idx]
    v.quiesce()
