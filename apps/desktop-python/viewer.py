"""Single-photo viewer page for the tracer app.

Loading the original file here is the deliberate, explicit exception to
N4 (the grid itself never reads originals): the user asked to see this
one photo, and it loads asynchronously so the UI never blocks on a slow
volume. Navigation walks the grid's current display order.

While that original decodes off-thread, the viewer paints an INSTANT
stand-in read from the same thumbnail-cache pair the grid uses — the
fcache v2 hi-DPI / loupe consumer (fauxcasa-9pp). On a large or hi-DPI
window it pulls — via ThumbCache.best_level()/entry() — the smallest
cached level that covers the viewport's device pixels, or the largest
level available when none is big enough (the 512 px level of a v2 cache
on a typical window); a single-level v1 cache yields its 256 px level.
The preview fills the box the original will occupy, so for the common
case — a photo larger than the window — the hand-off is a pure
sharpen-in-place (see _display_rect for the small-original caveat).
Reading the cache is well within the grid budget (N4) — it is the
originals the grid must never touch, not the cache it owns.

Explicit 1:1 zoom (fauxcasa-q6l.4), the OTHER named N4 exception: a
fit <-> 1:1 toggle where 1:1 means one image pixel per DEVICE pixel
(devicePixelRatio-correct, so a hi-DPI display shows native pixels, not
a 2x blowup). Bindings: `1` — Picasa Photo Viewer's own "Toggle 100%
zoom" (docs/research/sources/picasaresources/keyboard-shortcuts.md) —
plus Ctrl+Alt+0 as the conflict-free spelling (the M2 triage loop will
claim bare digits 0–5 for star-set keys, at which point `1` cedes and
Ctrl+Alt+0 remains — that arbitration is now ENCODED in keymap.py, the
default-scheme table all key handling here looks bindings up from,
fauxcasa-q6l.8), plus a plain click, anchored so the clicked image
point stays put under the cursor. While at 1:1, drag pans (and
Ctrl+arrows pan a quarter-viewport) — PLAIN arrows keep meaning
next/prev, the triage loop's key priority. Zoom state resets to fit on
every photo change (Picasa's behavior; nothing in the evidence corpus
documents zoom persisting across navigation). Until the original lands
the cached preview stands in at ITS OWN native pixels (paint whatever
is available — the pan anchor is fractional, so the view deepens in
place around the same image point when the original arrives).

Face-region overlay (fauxcasa-cam.4): `F` toggles rounded boxes + name
labels over the photo's ini faces= regions (Photo.faces). The math is
the point — rect64 fractions are relative to the STORED pixels, while
this pipeline displays EXIF-upright + rotate= composed, so the overlay
pushes each rect through that same composed transform and then through
whatever _shown_rect says (fit / panned 1:1), tracking zoom and pan.
See map_face_fraction / face_widget_rect and the metareader.
read_orientation seam. Viewer-only by policy: the peek and slideshow
subclasses are glance surfaces and set face_overlay_allowed False.

Crop-applied display (fauxcasa-cam.15): a photo with an unsaved Picasa
crop recipe (Photo.crop) shows CROPPED — the M1 "see your library again"
bar, since Picasa itself rendered unsaved recipes applied. The crop is
applied to the decoded original in load_original_oriented, composing
crop -> EXIF orientation -> rotate= (crop coords are STORED-frame; see
cropmap.py for the order's evidence); the cached preview stand-in is
already crop-baked by the indexer, so the hand-off stays a sharpen in
place. Face rects on a cropped photo rebase through the crop first
(_face_rects), and the info bar carries an honest "edited" chip
(has_edits) — the unsaved-vs-baked state cue is M3.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPolygonF
from PySide6.QtWidgets import QWidget

import keymap
from catalog import Catalog, format_date_taken, format_geotag
from cropmap import (
    crop_qimage_upright,
    map_fraction_rect,
    rebase_fraction_rect,
)
from locate import reveal_in_file_manager
from thumbcache import THUMB_EDGE, ThumbCache

BACKGROUND = QColor(12, 12, 12)
CAPTION_BG = QColor(0, 0, 0, 170)
# A press that travels less than this (logical px, Manhattan) before its
# release is a CLICK (zoom toggle); at or past it, a DRAG (pan at 1:1).
CLICK_SLOP = 6
# Face-overlay chrome (fauxcasa-cam.4): subtle, non-blocking outlines.
FACE_PEN = QColor(255, 255, 255, 215)
FACE_RADIUS = 6  # rounded-rect corner, logical px (chrome, so zoom-invariant)
# Video transport chrome (fauxcasa-v46.3): painted-in-widget, like every
# other viewer overlay — no child widget tree, keyboard keeps working.
TRANSPORT_H = 34            # transport strip height, above the 30 px info bar
TRANSPORT_FG = QColor(235, 235, 235)   # glyphs + seek progress (grid PLAY_WHITE)
TRANSPORT_TRACK = QColor(255, 255, 255, 60)  # unplayed seek-bar track
AFFORDANCE_BG = QColor(0, 0, 0, 140)   # centered play badge disc
SEEK_STEP_US = 5_000_000    # Ctrl+Left / Ctrl+Right skip (±5 s)


def _play_triangle(cx: float, cy: float, r: float) -> QPolygonF:
    """Right-pointing play triangle, grid._play_polygon's proportions
    ((cx, cy) center, r half-height, width trimmed 0.75r each way) so the
    viewer's play affordance and the grid's corner badge read as a set."""
    return QPolygonF([QPointF(cx - r * 0.75, cy - r),
                      QPointF(cx + r * 0.75, cy),
                      QPointF(cx - r * 0.75, cy + r)])


def _format_us(us: int) -> str:
    """Microseconds -> m:ss for the transport / info bar (video durations
    in a family archive are minutes, not hours; hours roll into minutes)."""
    s = max(0, int(us)) // 1_000_000
    return f"{s // 60}:{s % 60:02d}"


def _make_audio_sink(rate: int, channels: int):
    """A QAudioSink for the worker-produced PCM s16 stream, or None when
    audio output is unusable (headless CI, no output device, unsupported
    format, QtMultimedia absent) — playback then paces on the wall clock.

    Import ruling (owner, 2026-08-11, fauxcasa-i92.1 / PR #96): what is
    DECLINED is QtMultimedia *decoding* attacker bytes in-process
    (QMediaPlayer / QVideoWidget). QAudioSink consuming PCM that OUR
    sandboxed worker produced is exactly what decode-service §3c
    prescribes — its input is the sandbox's output, not attacker bytes."""
    try:
        from PySide6.QtMultimedia import (
            QAudioFormat,
            QAudioSink,
            QMediaDevices,
        )
    except Exception:  # noqa: BLE001 — bundle without QtMultimedia: no audio
        return None
    try:
        fmt = QAudioFormat()
        fmt.setSampleRate(int(rate))
        fmt.setChannelCount(int(channels))
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        dev = QMediaDevices.defaultAudioOutput()
        if dev.isNull() or not dev.isFormatSupported(fmt):
            return None
        return QAudioSink(dev, fmt)
    except Exception:  # noqa: BLE001 — any audio-stack failure: silent video
        return None


def _pan_delta(event) -> tuple[int, int] | None:
    """(dx, dy) unit for a viewer.pan_* chord, None when the event is no
    pan key. Positive means the image moves right/down — the historical
    sign convention of _pan_by (pan Left reveals what lies to the left)."""
    for action, delta in (("viewer.pan_left", (1, 0)),
                          ("viewer.pan_right", (-1, 0)),
                          ("viewer.pan_up", (0, 1)),
                          ("viewer.pan_down", (0, -1))):
        if keymap.matches(event, action):
            return delta
    return None


def load_original_oriented(path: str, rotate: int,
                           crop: tuple[float, float, float, float] | None
                           = None) -> tuple[QImage, int]:
    """Decode a full original AND report the stored file's EXIF Orientation
    value (1..8; 1 on anything unusable). The image comes back EXIF-upright
    (auto-orientation on read, so it matches the EXIF-baked grid thumbnails)
    with the Picasa rotate= user quarter-turns composed on top — see
    apps/desktop-python/README.md "EXIF orientation". The ONE full-image
    decode path, shared by the viewer's async load and the slideshow's dwell
    prefetch (slideshow.py), so every consumer orients identically. Returns
    (null QImage, 1) on failure. Thread-safe: QImage (unlike QPixmap) may be
    built off the GUI thread, and callers do call this from worker threads.

    `crop` is the photo's unsaved Picasa crop recipe (Photo.crop,
    fauxcasa-cam.15), fractions of the STORED pixels. It is applied to
    the decoded pixels here so every original consumer shows the crop,
    composing crop -> EXIF orientation -> rotate= — the crop selects
    stored pixels first (rect64 grammar; rotate= does not transform crop
    coords), and since the decode below hands back EXIF-upright pixels
    the rect maps through the reported orientation instead of
    re-orienting (cropmap.crop_qimage_upright, where the order's
    evidence lives). Fail-soft: a degenerate rect leaves the image whole.

    The file's bytes are read ONCE and shared by the decode and the
    orientation read (metareader.read_orientation, the exiv2 seam) — the
    orientation is what the face overlay needs to un-map faces= rect64
    fractions, which are relative to the STORED pixels, into the upright
    frame this function bakes (fauxcasa-cam.4). It is read at view time,
    on the decode worker, precisely so the catalog needs no orientation
    column (no CATALOG_VERSION bump). Bytes-in-hand also matches the
    decode-sandbox direction (docs/decode-threat-model.md: workers are
    handed bytes, never open paths).

    RAW files route by extension to rawload BEFORE QImageReader can sniff
    the TIFF-based container (rawload module doc): embedded JPEG preview
    first — usually full-size, so the viewer stays responsive — else a
    full demosaic; orientation lands exactly once on either path, and the
    rotate= turns compose on top exactly as for any other format. The
    reported orientation for a RAW is the container tag exiv2 reads —
    Picasa's faces=-on-RAW frame is unobserved (no oracle fixture), so
    this is the documented best guess, fail-soft by construction.

    Video files route by extension to the videoload poster seam
    (fauxcasa-v46.2): the poster frame IS the decoded "original" for a
    video — an honest still that stands in until the user PLAYS it
    (fauxcasa-v46.3: playback streams frames from a sandboxed worker
    process via videostream.py, per the ruled decode-service §3c seam;
    this function never starts playback). PyAV opens the path seekably,
    so a multi-GB video is never read whole here; a decoded poster
    carries no EXIF orientation, so it reports 1."""
    from inmeta import apply_orientation
    from metareader import read_orientation
    from rawload import is_raw_suffix, load_raw_qimage
    from videoload import is_video_suffix, poster_qimage

    if is_video_suffix(path):
        img = poster_qimage(path)  # null on any failure (error text)
        orientation = 1
    else:
        try:
            data = Path(path).read_bytes()
        except OSError:
            return QImage(), 1
        orientation = read_orientation(data)
        if is_raw_suffix(path):
            img = load_raw_qimage(data)
        else:
            from pillowload import pillow_qimage, tiff_is_16bit

            # Pre-route 16-bit TIFFs to Pillow on ALL platforms: Qt's
            # tiff plugin silently clips 16-bit grayscale to white on
            # Linux (fauxcasa-v46.7). Header sniff only — no pixel decode.
            _is_tiff = path.lower().endswith((".tif", ".tiff"))
            if _is_tiff and tiff_is_16bit(data):
                img = pillow_qimage(data)
            else:
                # QImage.fromData decodes into an internal C++ buffer — no
                # Python-created QIODevice for QImageReader to probe, so no
                # thread can be caught needing the GIL while holding Qt's
                # image-plugin factory mutex (the fauxcasa-5dk deadlock).
                # Orientation is therefore applied manually, once, right
                # here, instead of via QImageReader.setAutoTransform.
                img = QImage.fromData(data)
                if not img.isNull():
                    img = apply_orientation(img, orientation)
                if img.isNull():
                    # Qt has no decoder for this still (PSD — Pillow reads
                    # the flattened composite, fauxcasa-v46.4): same bytes,
                    # one Pillow attempt, orientation applied exactly once
                    # by exif_transpose there (pillowload module doc).
                    img = pillow_qimage(data)
    if not img.isNull() and crop is not None:
        # crop FIRST (stored-frame rect mapped through the orientation the
        # upright decode already applied — incl. the Pillow fallback's
        # exif_transpose), THEN the rotate= turns below — the composition
        # order cropmap.py documents.
        img = crop_qimage_upright(img, crop, orientation)
    if not img.isNull() and rotate:
        from PySide6.QtGui import QTransform

        img = img.transformed(QTransform().rotate(90 * rotate))
    return img, orientation


def load_original(path: str, rotate: int,
                  crop: tuple[float, float, float, float] | None
                  = None) -> QImage:
    """load_original_oriented for callers that need only the pixels (the
    slideshow prefetch — its surface never shows the face overlay)."""
    return load_original_oriented(path, rotate, crop)[0]


# ---- face-overlay coordinate math (fauxcasa-cam.4) -------------------------
#
# faces= rect64 fractions are relative to the STORED pixels; rotate= does
# NOT transform them and EXIF orientation handling is the consumer's job
# (picasa-ini-format.md "faces="). This app displays stored pixels through
# EXIF orientation (autoTransform at decode) THEN rotate= quarter-turns
# (load_original_oriented / _load_preview / the grid bake), so the overlay
# must push the stored-frame rect through the SAME composed transform. The
# fraction-rect algebra (incl. the 8-entry orientation map) now lives in
# cropmap.py, single-sourced with the crop bake (fauxcasa-cam.15).


def map_face_fraction(rect: tuple[float, float, float, float],
                      orientation: int,
                      rotate: int) -> tuple[float, float, float, float]:
    """A stored-pixel fractional rect (left, top, right, bottom — the
    catalog FaceTag shape) -> the SAME face's fractional rect in the
    DISPLAYED frame: EXIF orientation first (all 8 cases incl. mirrors),
    then rotate= quarter-turns clockwise, i.e. exactly the transform the
    pixels get. Pure and fail-soft (see cropmap.map_fraction_rect, which
    this delegates to — kept as the overlay's named seam)."""
    return map_fraction_rect(rect, orientation, rotate)


def face_widget_rect(rect: tuple[float, float, float, float],
                     orientation: int, rotate: int,
                     shown_rect: QRect | QRectF) -> QRectF:
    """The widget-space rect for one stored-frame face rect: the composed
    orientation x rotate mapping (map_face_fraction) scaled into
    `shown_rect` — wherever the displayed image paints right now, which is
    _shown_rect's job (centered fit, or the panned/clamped 1:1 rect while
    zoomed), so the overlay tracks pan and zoom for free. Pure function:
    (stored rect, exif orientation, rotate, shown rect) -> widget rect."""
    left, top, right, bottom = map_face_fraction(rect, orientation, rotate)
    return QRectF(shown_rect.x() + left * shown_rect.width(),
                  shown_rect.y() + top * shown_rect.height(),
                  (right - left) * shown_rect.width(),
                  (bottom - top) * shown_rect.height())


class _Playback:
    """One live video playback session (fauxcasa-v46.3): owns the
    sandboxed VideoStream (a separate decode PROCESS streaming rgb24
    frames + PCM through shared memory — the ruled §3c seam; QtMultimedia
    never decodes here), a QTimer consumer loop on the GUI thread, and
    the presentation clock.

    Clock: when the file has audio AND a QAudioSink is available, the
    audio clock — media pts = _pts0 + (sink.processedUSecs() - _proc0),
    which freezes across suspend (pause) and re-bases across seek (sink
    reset + restart). The anchor pts0 is the pts of the FIRST PCM chunk
    actually written to the sink (not the requested seek target): the
    worker resumes at the nearest keyframe AT OR BEFORE the target and
    that pre-target audio plays, so anchoring on the target would leave
    video permanently ahead of audio by the keyframe gap. Otherwise a
    monotonic wall clock anchored at every play/resume/seek (_pts0 at
    _t0). Late frames drop: each tick presents the NEWEST decoded frame
    whose pts <= clock; earlier due frames are overwritten unpainted.
    Frames are copied out of the shm view and the slot freed immediately
    (§2.4 item 6 — the broker side never holds a view the worker could
    rewrite), so seeks can flush the ring freely.

    Nothing here blocks the GUI thread: the VideoStream is opened OFF
    the GUI thread (ViewerPage._start_video) and handed in ready-made;
    seek() is fire-and-forget (stream.seek_async) with the ack picked up
    by _tick, which also pulls the landing frame while paused.

    GUI-thread only, like the ViewerPage that owns it. stop() is
    idempotent and reaps everything: timer, audio sink, worker process
    (VideoStream.close joins its reader thread and unlinks the arena)."""

    def __init__(self, page: "ViewerPage", stream) -> None:
        self.page = page
        # An already-open VideoStream (spawned off-thread by
        # ViewerPage._start_video so pressing P never freezes the window
        # on worker spawn / a slow or corrupt file).
        self.stream = stream
        self.info = self.stream.info()
        self.playing = False
        self.at_end = False
        self.position_us = 0
        self.frame: QImage | None = None      # last presented frame
        self._ahead: tuple[int, QImage] | None = None  # decoded, not yet due
        self._pending = b""                   # PCM copied out, not yet sunk
        self._stopped = False
        self._pts0 = 0                        # media pts at the clock anchor
        self._t0 = time.monotonic()           # wall anchor (no-audio clock)
        self._proc0 = 0                       # processedUSecs at the anchor
        self._last_ui = 0.0                   # ~4 Hz position repaints
        # (target, resume, epoch goal, deadline) of an in-flight seek —
        # sent fire-and-forget, settled by _tick when the ack's ring
        # flush lands (VideoStream.seek_async).
        self._seek: tuple[int, bool, int, float] | None = None
        self._want_frame = False              # paused: present next frame
        self._anchor_audio = True             # re-anchor clock on next PCM
        self._clock_seen = (-1, time.monotonic())  # stalled-sink watchdog
        self._sink = None
        self._sink_dev = None
        try:
            if self.info.has_audio:
                self._sink = _make_audio_sink(self.info.audio_rate,
                                              self.info.audio_channels)
                if self._sink is not None:
                    try:
                        self._sink_dev = self._sink.start()
                    except Exception:  # noqa: BLE001 — audio never blocks video
                        self._sink_dev = None
                    if self._sink_dev is None:
                        self._sink = None
                    else:
                        self._sink.suspend()  # created paused
            self._timer = QTimer(page)
            self._timer.setInterval(15)       # consumer budget: pts vs clock
            self._timer.timeout.connect(self._tick)
            self._timer.start()
        except BaseException:
            self.stream.close()               # never leak the worker process
            raise

    # -- clock ---------------------------------------------------------------

    def _clock_us(self) -> int:
        if self._sink is not None:
            return self._pts0 + int(self._sink.processedUSecs()) - self._proc0
        if self.playing:
            return self._pts0 + int((time.monotonic() - self._t0) * 1_000_000)
        return self._pts0

    def _drop_sink(self) -> None:
        """Fall back to the wall clock mid-flight (sink died or drained at
        EOF), re-anchored so the position is continuous."""
        cur = self._clock_us()
        sink, self._sink, self._sink_dev = self._sink, None, None
        self._pts0, self._t0 = cur, time.monotonic()
        if sink is not None:
            try:
                sink.stop()
            except Exception:  # noqa: BLE001 — teardown best effort
                pass

    # -- transport -----------------------------------------------------------

    def pause(self) -> None:
        if not self.playing or self._stopped:
            return
        cur = self._clock_us()
        self.playing = False
        if self._sink is not None:
            self._sink.suspend()              # processedUSecs freezes with it
        else:
            self._pts0 = cur                  # freeze the wall clock
        self.page.update()

    def resume(self) -> None:
        if self._stopped:
            return
        self.at_end = False
        self.playing = True
        self._want_frame = False
        self._t0 = time.monotonic()           # re-anchor the wall clock
        self._clock_seen = (-1, time.monotonic())  # watchdog: fresh window
        if self._sink is not None \
                and self._sink.state().name == "SuspendedState":
            self._sink.resume()
        self.page.update()

    def seek(self, target_us: int, resume: bool | None = None) -> None:
        """Seek, resuming in the prior play/pause state (or the explicit
        `resume` override); while paused the sought frame is pulled and
        shown when it lands. Fire-and-forget: the request is sent here,
        the worker's ack (the ring-flush guarantee — every later frame is
        post-seek) is picked up by _tick, so key auto-repeat can never
        stack GUI-thread waits."""
        if self._stopped:
            return
        dur = self.info.duration_us
        target = max(0, min(int(target_us), dur) if dur > 0 else int(target_us))
        if resume is None:
            # A seek issued while one is pending keeps the ORIGINAL
            # play/pause intent (the first seek already paused us).
            resume = self._seek[1] if self._seek is not None else self.playing
        if self.playing:
            self.pause()
        goal = self.stream.seek_async(target)
        if goal is None:
            return                            # closed/errored: tick surfaces it
        self._seek = (target, bool(resume), goal, time.monotonic() + 5.0)
        self.at_end = False
        self.page.update()

    def _finish_seek(self, target: int, resume: bool) -> None:
        """The worker acked (ring flushed): apply the post-seek state."""
        self._ahead = None                    # pre-seek frame: stale
        self._pending = b""                   # pre-seek PCM: stale
        self.at_end = False
        self.position_us = target
        self._pts0 = target
        self._anchor_audio = True             # re-anchor on first PCM chunk
        if self._sink is not None:
            try:
                self._sink.reset()            # drop buffered pre-seek audio
                self._sink_dev = self._sink.start()
                self._proc0 = int(self._sink.processedUSecs())
                self._sink.suspend()
            except Exception:  # noqa: BLE001 — audio dies, video plays on
                self._drop_sink()
        if resume or self.playing:
            self.resume()
        else:
            # Paused: present the landing frame (the seek target's
            # keyframe) when it arrives, so the seek bar shows where it
            # landed — pulled by _tick, never a blocking wait here.
            self._want_frame = True
        self.page.update()

    def stop(self) -> None:
        """Idempotent teardown: timer, sink, worker process + reader
        thread + shm (VideoStream.close). Safe from the tick itself."""
        if self._stopped:
            return
        self._stopped = True
        self.playing = False
        self._timer.stop()
        self._timer.deleteLater()  # one QTimer per session, not per page-life
        if self._sink is not None:
            try:
                self._sink.stop()
            except Exception:  # noqa: BLE001 — teardown best effort
                pass
            self._sink = None
            self._sink_dev = None
        self.stream.close()

    # -- consumer loop ---------------------------------------------------------

    def _pull_ahead(self) -> bool:
        """Copy the next decoded frame out of the ring (freeing its slot
        promptly) into _ahead; False when none is queued right now."""
        got = self.stream.next_frame(timeout_ms=0)
        if got is None:
            return False
        slot, pts, w, h, stride, mv = got
        # One copy, not two: wrap the shm view directly (no bytes()
        # detour), then .copy() detaches before free_slot lets the
        # worker rewrite that memory.
        img = QImage(mv, w, h, stride, QImage.Format.Format_RGB888).copy()
        self.stream.free_slot(slot)
        self._ahead = (pts, img)
        return True

    def _feed_audio(self) -> None:
        """Move broker-queued PCM into the sink, bounded by bytesFree —
        never blocking the GUI thread on audio. Popping a chunk is what
        releases its ring slot back to the worker (backpressure), so this
        pulls only what the sink can absorb plus one staged chunk."""
        st = self.stream
        if self._sink_dev is None:
            while st.next_audio() is not None:
                pass                          # no sink: wall clock, PCM dropped
            return
        while True:
            if not self._pending:
                got = st.next_audio()
                if got is None:
                    return
                data, pts = got
                if self._anchor_audio and pts >= 0:
                    # A/V sync anchor: clock = pts of the first chunk
                    # actually sunk (see the class docstring — after a
                    # seek this is the keyframe's audio, not the target).
                    self._pts0 = int(pts)
                    self._proc0 = int(self._sink.processedUSecs())
                    self._anchor_audio = False
                self._pending = data
            free = int(self._sink.bytesFree())
            if free <= 0:
                return
            try:
                n = self._sink_dev.write(self._pending[:free])
            except Exception:  # noqa: BLE001 — device died: wall fallback
                self._drop_sink()
                return
            if not n or n <= 0:
                return
            self._pending = self._pending[int(n):]

    def _tick(self) -> None:
        if self._stopped:
            return
        st = self.stream
        if st.error is not None:
            # Stream/protocol/timeout failure: stop playback, revert to
            # the poster, short honest note — never a crash (§1 semantics
            # live in VideoStream; this is the UI's shrug).
            self.page._video_failed("video decode failed")
            return
        if self._seek is not None:
            target, resume, goal, deadline = self._seek
            if st.epoch >= goal:              # ack landed: ring flushed
                self._seek = None
                self._finish_seek(target, resume)
            elif time.monotonic() > deadline:
                # No ack within the deadline: the worker is wedged; stop()
                # (via _video_failed) kills and reaps it.
                self.page._video_failed("video decode failed")
                return
            else:
                return                        # waiting on the flush
        if not self.playing:
            if self._want_frame and self._pull_ahead():
                # Paused post-seek: present the landing frame once.
                _pts, img = self._ahead
                self._ahead = None
                self.frame = img
                self._want_frame = False
                self.page.update()
            return
        self._feed_audio()
        if self._sink is not None and st.at_eof and not self._pending \
                and self._sink.state().name == "IdleState":
            # Audio track drained at EOF: the audio clock has stopped, so
            # flush the remaining video tail on the wall clock.
            self._drop_sink()
        if self._sink is not None:
            # Stalled-sink watchdog: a device that accepts the format but
            # never consumes (bytesFree stuck, write()->0) freezes the
            # audio clock and playback with it. If the clock has not
            # advanced for ~1 s while PCM is queued, fall back to the
            # wall clock (the mechanism _drop_sink already provides).
            cur = self._clock_us()
            val, since = self._clock_seen
            now = time.monotonic()
            if cur != val:
                self._clock_seen = (cur, now)
            elif now - since > 1.0 and (self._pending or st.audio_pending):
                self._drop_sink()
        clock = self._clock_us()
        presented = False
        while True:
            if self._ahead is None and not self._pull_ahead():
                break
            pts, img = self._ahead
            if pts > clock:
                break                          # not due yet: hold it
            # Present-newest: a frame already due is overwritten by any
            # LATER frame also <= clock before the single repaint below —
            # late frames drop without ever painting.
            self.frame = img
            self.position_us = pts if pts >= 0 else clock
            self._ahead = None
            presented = True
        now = time.monotonic()
        if presented:
            self.page.update()
            self._last_ui = now
        elif self._ahead is None and st.at_eof and not self._pending:
            dur = self.info.duration_us
            self.position_us = dur if dur > 0 else self.position_us
            self.playing = False
            self.at_end = True                # play again = seek(0) + resume
            if self._sink is not None:
                self._sink.suspend()
            self.page.update()
        elif now - self._last_ui > 0.25:
            self.page.update()                # ~4 Hz position label refresh
            self._last_ui = now


class _Loader(QObject):
    # serial, image (null = failed), stored EXIF orientation (1..8; 1 =
    # unknown/none — the face overlay's un-mapping input, fauxcasa-cam.4)
    loaded = Signal(int, QImage, int)
    # serial, duration_us (0 = unknown): the video's probed duration, so
    # the info bar / transport can show it BEFORE playback starts (v46.3).
    video_meta = Signal(int, int)
    # serial, VideoStream-or-None: an off-thread stream open finished
    # (None = open failed). The GUI slot builds the _Playback session, so
    # pressing P never blocks the window on worker spawn (design §1/§6:
    # the poster keeps painting until the stream is live).
    video_stream = Signal(int, object)


class ViewerPage(QWidget):
    closed = Signal(int)  # catalog index in view when closed
    photo_shown = Signal(int)
    # Ctrl+H — Picasa's hold key (fauxcasa-q6l.2): hold the photo on
    # screen in the selection tray, so triage can stage outputs without
    # bouncing back to the grid. Payload: the catalog index shown.
    hold_requested = Signal(int)
    # Space — Picasa's library shortcut. MainWindow owns persistence and
    # refreshes every surface after changing this catalog photo.
    star_toggle_requested = Signal(int)
    # Bare I — metadata inspector toggle (fauxcasa-q6l.25), same contract
    # as the grid's info_toggle_requested. SlideshowPage overrides
    # keyPressEvent entirely (its own scheme, slideshow.*), so this never
    # reaches playback.
    info_toggle_requested = Signal()

    def __init__(self, catalog: Catalog, thumbs: ThumbCache | None = None,
                 parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.thumbs = thumbs
        self.display: list[int] = []
        self.pos = 0
        self.image: QImage | None = None
        # Instant low-res stand-in from the thumb cache, painted while the
        # full original decodes off-thread (loupe preview, fauxcasa-9pp).
        self.preview: QImage | None = None
        self.loading = False
        self._serial = 0
        # Explicit 1:1 zoom state (fauxcasa-q6l.4). The pan is a FRACTIONAL
        # image point (0..1 of width/height) pinned at the viewport center,
        # not a pixel offset: the stand-in preview and the original differ in
        # pixel size, so a fractional anchor keeps the SAME image point
        # centered when the async original replaces the preview — the zoom
        # deepens in place instead of jumping. Clamping to the viewport
        # happens per-paint in _zoom_rect against whatever is shown.
        self.zoomed = False
        self._zoom_cx = 0.5
        self._zoom_cy = 0.5
        # Face overlay (fauxcasa-cam.4). `face_overlay_allowed` is the
        # per-SURFACE policy: True here (the viewer is the inspection
        # surface); the peek and slideshow subclasses set it False — they
        # are glance surfaces, and boxes over a timed show or a hover
        # flash would be noise. `faces_visible` is the user's F toggle,
        # session-sticky across navigation (a display mode, like reveal).
        # `_orientation` is the CURRENT photo's stored EXIF Orientation
        # (1..8), read from the original's bytes on the decode worker and
        # delivered with the image — 1 (identity) until the original
        # lands, which is why the overlay waits for self.image.
        self.face_overlay_allowed = True
        self.faces_visible = False
        self._orientation = 1
        # Video playback (fauxcasa-v46.3). `video_playback_allowed` is the
        # per-SURFACE policy, exactly like face_overlay_allowed: True here
        # (the viewer is the inspection surface); the peek and slideshow
        # subclasses set it False — glance surfaces keep showing the
        # honest poster still. `_video` is the live _Playback session
        # (None until the user plays), `_video_note` a short honest error
        # ("video decode failed"), `_video_duration_us` the async-probed
        # duration for the pre-play transport/info bar.
        self.video_playback_allowed = True
        self._video: _Playback | None = None
        self._video_note: str | None = None
        self._video_duration_us: int | None = None
        # Async stream open (never blocks the GUI thread): `_video_opening`
        # gates duplicate opens while the worker spawns off-thread;
        # `_video_open_play` is the play/pause intent to apply when the
        # stream lands; `_video_open_seek` an initial seek target (a seek
        # issued from the poster before any session exists).
        self._video_opening = False
        self._video_open_play = False
        self._video_open_seek: int | None = None
        # Click-vs-drag disambiguation: where the left press landed, and
        # whether it traveled past CLICK_SLOP (then it pans, never toggles).
        self._press_pos = None
        self._dragging = False
        # ONE persistent, lazily started decode worker fed by a job queue —
        # NOT a thread per navigation. Short-lived Qt-touching threads exit
        # through Qt's per-thread native cleanup, and on offscreen Windows
        # that churn is exactly the cumulative state that tips the
        # fauxcasa-gfz access violations; the grid's long-lived pool is the
        # same discipline. Stale jobs cost one queue hop and bail on the
        # serial guard, so the queue stays bounded under key-repeat.
        self._jobs: queue.Queue = queue.Queue()
        self._decoder: threading.Thread | None = None
        self._loader = _Loader()
        self._loader.loaded.connect(self._on_loaded)
        self._loader.video_meta.connect(self._on_video_meta)
        self._loader.video_stream.connect(self._on_video_stream)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _submit(self, job) -> None:
        """Queue a decode job on the persistent worker (started on first
        use, so a viewer that never shows a photo never owns a thread)."""
        if self._decoder is None or not self._decoder.is_alive():
            self._decoder = threading.Thread(
                target=self._decode_loop, daemon=True)
            self._decoder.start()
        self._jobs.put(job)

    def _decode_loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return  # quiesce() sentinel: retire the worker
            job()

    def quiesce(self, timeout: float = 5.0) -> None:
        """Retire the decode worker BEFORE this widget is destroyed: bump
        the stale-guard serial so any queued job bails instead of emitting,
        then send the sentinel and join. A job that already passed its final
        serial check could otherwise emit into a receiver being torn down
        concurrently — the fauxcasa-gfz Windows access-violation family
        (grid.stop() is the same discipline for the grid's pool).
        Deliberately NOT called on ordinary navigation or viewer close: the
        serial guards handle staleness there, and joining a decode mid-read
        could stall the UI on a slow volume — this is for teardown paths
        (tests delete widgets aggressively).

        Also stops any live video playback FIRST (fauxcasa-v46.3): the
        session's QTimer must stop and its worker process / reader thread
        / shm arena be reaped before the widget goes away — the same
        no-leaked-threads discipline as the decode worker below."""
        self._stop_video()
        self._serial += 1
        t = self._decoder
        if t is not None and t.is_alive():
            self._jobs.put(None)
            t.join(timeout)

    def set_thumbs(self, thumbs: ThumbCache | None) -> None:
        """Adopt a freshly built or reconciled cache (cold-build finish or the
        reconcile swap in main), so the next photo shown gets an instant
        preview. Previews are decoded per-show, so there is no stale tile state
        to invalidate — just re-point at the new cache."""
        self.thumbs = thumbs

    def show_photo(self, display: list[int], pos: int) -> None:
        self.display = display
        self.pos = max(0, min(len(display) - 1, pos))
        self._load_current()

    def current_index(self) -> int:
        if not self.display:
            return -1
        return self.display[self.pos]

    def _take_prefetched(self, idx: int) -> QImage | None:
        """Subclass hook: hand back an already-decoded original for `idx`,
        or None. The base viewer never prefetches; the slideshow decodes the
        NEXT photo's original during the current dwell and surrenders it
        here, making a timed advance a pure swap (slideshow.py)."""
        return None

    def _load_current(self) -> None:
        idx = self.current_index()
        if idx < 0:
            return
        # Any live video playback stops on EVERY photo change (v46.3):
        # navigation away must reap the streaming worker cleanly, and a
        # new video starts from its poster (the pre-play stand-in).
        self._stop_video()
        self._video_note = None
        self._video_duration_us = None
        # Zoom state resets to fit on EVERY photo change (Picasa behavior) —
        # this is the one funnel all changes pass through: show_photo, the
        # viewer's _step, the slideshow's wrap _step and timed advance.
        self._reset_zoom()
        # Orientation is per-photo and unknown until this photo's original
        # decodes (the overlay gates on self.image, so a stale value could
        # never paint — this reset just keeps the state honest, including
        # for the prefetched path below, whose surface never overlays).
        self._orientation = 1
        self._serial += 1
        serial = self._serial
        ready = self._take_prefetched(idx)
        if ready is not None:
            # A prefetcher already decoded this original during the previous
            # dwell: show it NOW — no preview flash, no redundant decode. The
            # serial bump above stales any in-flight load (_on_loaded guard),
            # exactly as a normal navigation would.
            self.loading = False
            self.image = ready
            self.preview = None
            self.photo_shown.emit(idx)
            self.update()
            return
        self.image = None
        self.loading = True
        # Catalog.abs() is the single choke point for absolute-path
        # composition (multiroot .b, design §6). An offline/unresolvable
        # root (None, bead .e's offline-tolerance semantics, design §8)
        # degrades to the existing unreadable-file fail-soft path below
        # (empty path -> OSError -> null image, error tile) — a working
        # "volume offline" stand-in without a dedicated overlay for M1.
        abs_path = self.catalog.abs(self.catalog.photos[idx])
        path = str(abs_path) if abs_path is not None else ""
        rotate = self.catalog.photos[idx].rotate
        # Unsaved Picasa crop recipe (fauxcasa-cam.15): applied to the
        # decoded original (crop -> EXIF -> rotate). The cached preview
        # below is already crop-baked by the v10 indexer, so both stand-in
        # and original show the same sub-rect (a prebuilt adopt-mode cache
        # may predate the bake — accepted: its corpus carries no recipes).
        crop = self.catalog.photos[idx].crop
        # Show a cached stand-in NOW — synchronous, but cheap (a <= 512 px
        # JPEG decodes in a couple of ms) — so the viewport is never blank
        # while the worker below decodes the full original. This is the
        # fcache v2 loupe consumer: best_level() reads the >256 level on a
        # large/hi-DPI window, the 256 level from a v1 cache (fauxcasa-9pp).
        # Holding an arrow key pays this once per step inline, but it is bounded
        # by the key-repeat rate (one cheap decode per shown photo), not a
        # growing backlog — unlike the original, which needs the stale guards.
        self.preview = self._load_preview(idx, rotate)

        is_video = self.catalog.photos[idx].media == "video"

        def work() -> None:
            # Stale checks bound the decode backlog when the user holds
            # an arrow key: superseded loads bail before the expensive
            # decode, and the emit is guarded against Qt teardown.
            if serial != self._serial:
                return
            img, orientation = load_original_oriented(path, rotate, crop)
            if serial != self._serial:
                return
            try:
                self._loader.loaded.emit(serial, img, orientation)
            except RuntimeError:
                return  # loader deleted at shutdown
            if is_video:
                # Probe the duration off-thread for the pre-play
                # transport / info bar (videoload's declared v46.3
                # consumer seam); 0 = unknown, fail-soft.
                from videoload import probe_duration

                d = probe_duration(path)
                if serial != self._serial:
                    return
                try:
                    self._loader.video_meta.emit(
                        serial, int(d * 1_000_000) if d else 0)
                except RuntimeError:
                    pass  # loader deleted at shutdown

        self._submit(work)
        self.photo_shown.emit(idx)
        self.update()

    def _preview_min_edge(self) -> int:
        """Long edge, in DEVICE pixels, the preview should cover so best_level()
        picks a level that is sharp at this window's DPI. Floored at the grid's
        256 px tile (never preview below the grid); a large or hi-DPI window
        clears 256 and selects a larger v2 level."""
        longest = max(self.width(), self.height())
        return max(THUMB_EDGE, round(longest * self.devicePixelRatioF()))

    def _load_preview(self, idx: int, rotate: int) -> QImage | None:
        """Decode the nearest cached level for `idx` as an instant stand-in, or
        None when there is no usable cached pixel: no cache, an out-of-range
        index, an error-tile entry (zero-length blob), or an unreadable/corrupt
        blob — the caller then falls back to the loading text. The cached thumb
        is EXIF-upright; the Picasa rotate= quarter-turns compose on top exactly
        as the grid and the original path do, so every display path agrees."""
        cache = self.thumbs
        if cache is None or not (0 <= idx < cache.count):
            return None
        level = cache.best_level(self._preview_min_edge())
        offset, length, _w, _h = cache.entry(idx, level)
        if length <= 0:
            return None  # error tile: the original failed to decode at build
        # Read + decode + rotate under ONE broad guard, exactly as the grid's
        # decode worker does (grid.py): a corrupt index can hand us a multi-GiB
        # `length` (the read then raises MemoryError, not an OSError), a dying
        # volume an EIO, a bad blob a null decode. ANY of these must degrade to
        # "no preview" (the loading text) — never an exception escaping into the
        # synchronous Qt event handler that called us and aborting the UI.
        # A plain buffered "rb" + seek + read (NOT os.pread) keeps this portable:
        # os.pread is Unix-only — absent on Windows — and os.open there defaults
        # to text mode, which would mangle the JPEG bytes. One synchronous read,
        # so the atomic-offset reason the threaded grid uses os.pread for is moot.
        try:
            with open(cache.path, "rb") as f:
                f.seek(offset)
                buf = f.read(length)
            img = QImage.fromData(buf, "JPEG")
            if img.isNull():
                return None
            if rotate:
                from PySide6.QtGui import QTransform

                img = img.transformed(QTransform().rotate(90 * rotate))
            return img
        except Exception:  # noqa: BLE001 — match grid.py: degrade, never crash
            return None

    def _on_loaded(self, serial: int, img: QImage,
                   orientation: int = 1) -> None:
        if serial != self._serial:
            return  # user already moved on
        self.loading = False
        if img.isNull():
            self.image = None     # keep painting the preview, if we have one
        else:
            self.image = img
            self.preview = None   # the full original supersedes the preview
            self._orientation = orientation  # arrives WITH its image
        self.update()

    # ---------- video playback (fauxcasa-v46.3) ----------
    #
    # The poster stays the pre-play stand-in; playing opens a _Playback
    # session over the sandboxed videostream seam (§3c, ruled option A).
    # Everything here is painted-in-widget (play affordance + transport
    # strip in paintEvent) and lifecycle-tied: _load_current, hideEvent
    # and quiesce all stop the session and reap the worker process.

    def _on_video_meta(self, serial: int, duration_us: int) -> None:
        if serial != self._serial:
            return  # user already moved on
        self._video_duration_us = duration_us or None
        self.update()

    def _current_is_video(self) -> bool:
        idx = self.current_index()
        return idx >= 0 and self.catalog.photos[idx].media == "video"

    def _video_max_edge(self) -> int:
        """Decode-scale cap for streamed frames: the screen's device-pixel
        long edge — frames are painted fit, never 1:1, so decoding beyond
        the screen is pure waste (and ring slots are sized by this).
        Capped at 1920 for v1: ring slots are sized worst-case from this
        edge and Windows charges the whole arena to commit at creation,
        so a hi-DPI 4K cap would cost hundreds of MB per playback session
        for no visible win while frames paint fit (arena negotiation from
        the stream's real dims is the follow-up if 1:1 video ever lands)."""
        scr = self.screen()
        if scr is None:
            from PySide6.QtGui import QGuiApplication

            scr = QGuiApplication.primaryScreen()
        edge = 1920
        if scr is not None:
            g = scr.geometry()
            edge = int(max(g.width(), g.height()) * self.devicePixelRatioF())
        return max(256, min(edge, 1920))

    def _start_video(self, play: bool) -> None:
        """Open the stream ASYNCHRONOUSLY: the worker spawn + container
        open (up to VideoStream's 15 s open timeout on a corrupt or slow
        file) runs on a short-lived helper thread while the poster keeps
        painting; _on_video_stream builds the session when it lands.
        The thread touches no Qt objects (the signal emit is queued), so
        the fauxcasa-gfz short-lived-Qt-thread hazard does not apply."""
        if self._video is not None or self._video_opening \
                or not self.video_playback_allowed:
            return
        idx = self.current_index()
        abs_path = self.catalog.abs(self.catalog.photos[idx])
        if abs_path is None:
            self._video_note = "volume offline"
            self.update()
            return
        self._video_opening = True
        self._video_open_play = play
        serial = self._serial
        path = str(abs_path)
        max_edge = self._video_max_edge()

        def work() -> None:
            from videostream import VideoStream

            vs = None
            try:
                vs = VideoStream(path, max_edge=max_edge)
            except Exception:  # noqa: BLE001 — corrupt file / worker refused
                vs = None
            if serial != self._serial:
                if vs is not None:
                    vs.close()                 # user moved on: reap quietly
                return
            try:
                self._loader.video_stream.emit(serial, vs)
            except RuntimeError:
                if vs is not None:
                    vs.close()                 # loader deleted at shutdown

        threading.Thread(target=work, daemon=True,
                         name="viewer-video-open").start()
        self.update()

    def _on_video_stream(self, serial: int, vs) -> None:
        """GUI-thread half of the async open: adopt the stream into a
        _Playback session, or close it if the user has moved on."""
        if serial != self._serial or not self._video_opening \
                or self._video is not None:
            if vs is not None:
                vs.close()
            return
        self._video_opening = False
        seek_to, self._video_open_seek = self._video_open_seek, None
        if vs is None:
            self._video_note = "video decode failed"
            self.update()
            return
        try:
            pb = _Playback(self, vs)
        except Exception:  # noqa: BLE001 — sink/timer failure: no session
            vs.close()
            self._video_note = "video decode failed"
            self.update()
            return
        self._video = pb
        self._video_note = None
        if seek_to is not None:
            pb.seek(seek_to, resume=self._video_open_play)
        elif self._video_open_play:
            pb.resume()
        self.update()

    def _stop_video(self) -> None:
        """Stop + reap any live playback session; the poster (self.image)
        resumes painting. Idempotent; never touches _video_note (an error
        note must survive the stop that raised it). Also cancels the
        INTENT of any in-flight async open — the helper thread's stream,
        if one lands, is closed by _on_video_stream's gate."""
        self._video_opening = False
        self._video_open_seek = None
        pb, self._video = self._video, None
        if pb is not None:
            pb.stop()
            self.update()

    def _video_failed(self, note: str) -> None:
        """Stream/system failure during playback: stop, revert to the
        poster, show a short honest note in the info bar — never crash."""
        self._video_note = note
        self._stop_video()

    def _video_play_pause(self) -> None:
        if not self._current_is_video() or not self.video_playback_allowed:
            return
        pb = self._video
        if pb is None:
            if self._video_opening:
                # P during the async open toggles the intent the session
                # will start with (press-press = open paused).
                self._video_open_play = not self._video_open_play
            else:
                self._start_video(play=True)  # failed earlier try retries here
            return
        if pb.at_end:
            pb.seek(0, resume=True)       # play again from the top
        elif pb.playing:
            pb.pause()
        else:
            pb.resume()
        self.update()

    def _video_seek_to(self, pts_us: int) -> None:
        if self._video is not None:
            self._video.seek(pts_us)
            self.update()
            return
        if not self._current_is_video() or not self.video_playback_allowed:
            return
        # Seek from the poster: remember the target, open async, land
        # paused on it (_on_video_stream applies _video_open_seek).
        self._video_open_seek = max(0, int(pts_us))
        self._start_video(play=False)
        self.update()

    def _video_seek_by(self, delta_us: int) -> None:
        if self._video is not None:
            self._video_seek_to(self._video.position_us + delta_us)

    # -- transport strip geometry (hit-testing + painting share these) --

    def _transport_rect(self) -> QRect:
        return QRect(0, self.height() - 30 - TRANSPORT_H,
                     self.width(), TRANSPORT_H)

    def _transport_seek_rect(self) -> QRect:
        r = self._transport_rect()
        left, right = r.x() + 150, r.right() - 14
        if right - left < 40:               # tiny window: bar over the text
            left = r.x() + 8
        return QRect(left, r.center().y() - 2, max(1, right - left), 4)

    def _video_click(self, posf) -> None:
        """A clean click on a video photo: the seek bar seeks (click-to-
        seek), anywhere else toggles play/pause — the centered affordance
        is the visual anchor, but the whole surface is the target."""
        bar = self._transport_seek_rect()
        if bar.adjusted(-4, -10, 4, 10).contains(posf.toPoint()):
            pb = self._video
            dur = pb.info.duration_us if pb is not None \
                else (self._video_duration_us or 0)
            if dur > 0:
                frac = (posf.x() - bar.x()) / max(1, bar.width())
                self._video_seek_to(int(max(0.0, min(1.0, frac)) * dur))
            return
        self._video_play_pause()

    # ---------- explicit 1:1 zoom + pan (fauxcasa-q6l.4) ----------

    def _shown_now(self) -> QImage | None:
        """Whatever paintEvent would draw right now: the full original once
        it has arrived, else the instant cached preview, else None."""
        return self.image if self.image is not None else self.preview

    def _reset_zoom(self) -> None:
        self.zoomed = False
        self._zoom_cx = self._zoom_cy = 0.5
        self.unsetCursor()

    def toggle_zoom(self, anchor=None) -> None:
        """Fit <-> 1:1. `anchor` (a QPointF in widget coords, e.g. the click
        position) picks the image point that stays PUT under the cursor
        across the toggle; None (the key toggle) anchors at the center.

        The anchor solves for the fractional center: with the shown image
        drawn dw x dh logical px at 1:1, pinning image fraction (u, v) at
        widget point (ax, ay) means the drawn origin is ax - u*dw, and the
        fractional center — the point at the viewport middle — is
        cx = (w/2 - ax)/dw + u. Deliberately stored UNCLAMPED: against the
        small stand-in preview the clamp would collapse to center and lose
        the anchor the user meant for the original; _zoom_rect clamps
        per-paint instead."""
        if self._current_is_video():
            return  # 1:1 is unsupported for video in v1 (fauxcasa-v46.3)
        if self.zoomed:
            self._reset_zoom()
            self.update()
            return
        self.zoomed = True
        self._zoom_cx = self._zoom_cy = 0.5
        shown = self._shown_now()
        if anchor is not None and shown is not None:
            w, h = self.width(), self.height()
            r = self._display_rect(w, h, shown.width(), shown.height(),
                                   cap=self.image is not None)
            if r.width() > 0 and r.height() > 0:
                u = max(0.0, min(1.0, (anchor.x() - r.x()) / r.width()))
                v = max(0.0, min(1.0, (anchor.y() - r.y()) / r.height()))
                dpr = self.devicePixelRatioF()
                dw = max(1, round(shown.width() / dpr))
                dh = max(1, round(shown.height() / dpr))
                self._zoom_cx = (w / 2 - anchor.x()) / dw + u
                self._zoom_cy = (h / 2 - anchor.y()) / dh + v
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()

    def _pan_by(self, dx: float, dy: float) -> None:
        """Pan the 1:1 view by a widget-space delta (logical px): dragging
        the photo right moves the fractional center left. Each axis snaps to
        the RANGE the viewport can actually pan ([box/(2*drawn), 1 - that],
        where _zoom_rect's edge clamp bites), so a fling past an edge leaves
        no dead travel to wind back through. An axis with no pan freedom
        (drawn <= box) is left alone — snapping it would erase a click
        anchor meant for the original while only the small preview is up."""
        shown = self._shown_now()
        if shown is None or not self.zoomed:
            return
        dpr = self.devicePixelRatioF()
        dw = max(1, round(shown.width() / dpr))
        dh = max(1, round(shown.height() / dpr))
        if dw > self.width():
            lo = self.width() / (2 * dw)
            self._zoom_cx = max(lo, min(1 - lo, self._zoom_cx - dx / dw))
        if dh > self.height():
            lo = self.height() / (2 * dh)
            self._zoom_cy = max(lo, min(1 - lo, self._zoom_cy - dy / dh))
        self.update()

    @staticmethod
    def _clamp_offset(off: int, drawn: int, box: int) -> int:
        """One axis of the 1:1 pan clamp: an image smaller than the box
        centers (no pan freedom); a larger one may never expose background
        past either edge, so the origin stays within [box - drawn, 0]."""
        if drawn <= box:
            return (box - drawn) // 2
        return max(box - drawn, min(0, off))

    @staticmethod
    def _zoom_rect(box_w: int, box_h: int, src_w: int, src_h: int,
                   dpr: float, cx: float, cy: float) -> QRect:
        """The 1:1 display rect for a `src` painted in a `box`: one image
        pixel per DEVICE pixel, so the drawn size in LOGICAL px is src/dpr
        (at dpr 1 the blit is pixel-exact; at dpr 2 the half-size logical
        rect covers exactly src device pixels). (cx, cy) is the fractional
        image point at the box center, clamped per axis."""
        dw = max(1, round(src_w / dpr))
        dh = max(1, round(src_h / dpr))
        x = ViewerPage._clamp_offset(round(box_w / 2 - cx * dw), dw, box_w)
        y = ViewerPage._clamp_offset(round(box_h / 2 - cy * dh), dh, box_h)
        return QRect(x, y, dw, dh)

    def _shown_rect(self, w: int, h: int, shown: QImage) -> QRect:
        """Where the current stand-in paints: the centered aspect-fit rect
        normally, the DPR-correct panned 1:1 rect while zoomed. At 1:1 the
        rect comes from the SHOWN image's own pixels — until the original
        lands that is the preview at its native (small) size, "paint
        whatever is available"; the fractional pan then re-centers the same
        image point when the original's pixels arrive."""
        if self.zoomed:
            return self._zoom_rect(w, h, shown.width(), shown.height(),
                                   self.devicePixelRatioF(),
                                   self._zoom_cx, self._zoom_cy)
        return self._display_rect(w, h, shown.width(), shown.height(),
                                  cap=self.image is not None)

    # ---------- face-region overlay (fauxcasa-cam.4) ----------

    def toggle_faces(self) -> None:
        """Flip the face overlay. Bound to `F` — Picasa documents no
        view-mode key for face boxes (the shortcut corpus's face keys are
        all People-editor keys), so F ("faces") is this app's own pick,
        documented in the README key table. Deliberately a no-op on a
        surface that never overlays (peek/slideshow) and on a photo with
        no face tags: a mode toggle that could never show anything would
        just leave invisible state behind."""
        if not self.face_overlay_allowed:
            return
        idx = self.current_index()
        if idx < 0 or not self.catalog.photos[idx].faces:
            return
        self.faces_visible = not self.faces_visible
        self.update()

    def _face_rects(self) -> list[tuple[QRectF, str | None]]:
        """Widget-space (rect, name-or-None) for the CURRENT photo's face
        tags, or [] whenever the overlay must not paint: surface policy
        off, toggle off, no faces, or the ORIGINAL not yet landed — the
        stored orientation rides in with the original's decode, and until
        it is known a mis-mapped box is worse than none (the preview
        stand-in window is a fraction of a second). Rects are computed
        against _shown_rect, so fit, 1:1, and every pan land the boxes on
        the same image pixels the photo paints at."""
        if not (self.face_overlay_allowed and self.faces_visible):
            return []
        if self.image is None:
            return []
        idx = self.current_index()
        if idx < 0:
            return []
        photo = self.catalog.photos[idx]
        if not photo.faces:
            return []
        shown = self._shown_rect(self.width(), self.height(), self.image)
        out: list[tuple[QRectF, str | None]] = []
        for rect, _cid, name in photo.faces:
            if photo.crop is not None:
                # Faces on a cropped photo still reference STORED pixels
                # (fauxcasa-cam.15): rebase into the crop sub-rect FIRST
                # (the displayed image IS that sub-rect), then the same
                # EXIF x rotate mapping as the pixels. A face the crop
                # cut out entirely has no on-screen pixels — skipped; one
                # straddling the edge shows its visible part (clamped).
                rect = rebase_fraction_rect(rect, photo.crop)
                if rect is None:
                    continue
            out.append((face_widget_rect(rect, self._orientation,
                                         photo.rotate, shown), name))
        return out

    def _paint_faces(self, painter: QPainter) -> None:
        """Rounded outline + name chip per face: solid for a named face,
        dashed + "Unnamed" for a suggested/unresolved one. Chrome (pen
        width, corner radius, label) stays at UI scale; the box geometry
        itself scales/pans with the image because it derives from
        _shown_rect. The label flips above the box when the box bottom
        runs off the widget, and everything else clips naturally."""
        rects = self._face_rects()
        if not rects:
            return
        from PySide6.QtGui import QPen

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fm = painter.fontMetrics()
        for rect, name in rects:
            pen = QPen(FACE_PEN, 2)
            if name is None:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, FACE_RADIUS, FACE_RADIUS)
            label = name or "Unnamed"
            tw = fm.horizontalAdvance(label)
            th = fm.height()
            ly = rect.bottom() + 4
            if ly + th + 4 > self.height():
                ly = rect.top() - th - 8
            chip = QRectF(rect.left(), ly, tw + 12, th + 4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(CAPTION_BG)
            painter.drawRoundedRect(chip, 4, 4)
            painter.setPen(QColor(235, 235, 235))
            painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, label)

    def _step(self, delta: int) -> None:
        if not self.display:
            return
        self.pos = max(0, min(len(self.display) - 1, self.pos + delta))
        self._load_current()

    def keyPressEvent(self, event) -> None:
        # Every binding is a keymap lookup (fauxcasa-q6l.8); ordering is
        # the contract: pan is checked before prev/next so Ctrl+arrows pan
        # while zoomed, and PLAIN arrows always mean next/prev (the triage
        # loop owns them, module docstring).
        if keymap.matches(event, "viewer.close"):
            self.closed.emit(self.current_index())
        elif keymap.matches(event, "viewer.zoom_toggle"):
            # `1` = Picasa Photo Viewer's "Toggle 100% zoom"; Ctrl+Alt+0 is
            # the conflict-free spelling that survives the M2 star-set keys
            # claiming bare digits (keymap arbitration + module docstring).
            self.toggle_zoom()
        elif keymap.matches(event, "viewer.hold"):
            # Ctrl+H holds the shown photo in the tray (fauxcasa-q6l.2).
            idx = self.current_index()
            if idx >= 0:
                self.hold_requested.emit(idx)
        elif keymap.matches(event, "viewer.locate"):
            # Ctrl+Enter: Picasa's Locate on Disk, from the viewer too
            # (q6l.8) — reveal the shown photo in the OS file manager.
            idx = self.current_index()
            if idx >= 0:
                # Catalog.abs() is the single choke point for absolute-path
                # composition (multiroot .b, design §6); None means the
                # photo's root is missing/offline — nothing to reveal.
                target = self.catalog.abs(self.catalog.photos[idx])
                if target is not None:
                    reveal_in_file_manager(target)
        elif keymap.matches(event, "viewer.faces"):
            # F = face overlay (toggle_faces docstring: our own binding —
            # Picasa documents no view-mode key for face boxes).
            self.toggle_faces()
        elif keymap.matches(event, "viewer.star_toggle"):
            idx = self.current_index()
            if idx >= 0:
                self.star_toggle_requested.emit(idx)
        elif self._current_is_video() \
                and keymap.matches(event, "viewer.play_pause"):
            # P = play/pause the shown video (v46.3). Gated on media kind,
            # so P on a still falls through to the default handler.
            self._video_play_pause()
        elif self._video is not None \
                and keymap.matches(event, "viewer.seek_back"):
            # Ctrl+Left/Right = ±5 s ONLY while a playback session is
            # live — the keymap's _CONTEXT_LAYERED pair with pan (which
            # needs zoomed, unsupported for video); with no session the
            # chord falls through exactly as before.
            self._video_seek_by(-SEEK_STEP_US)
        elif self._video is not None \
                and keymap.matches(event, "viewer.seek_fwd"):
            self._video_seek_by(SEEK_STEP_US)
        elif self.zoomed and (pan := _pan_delta(event)) is not None:
            # Ctrl+arrows pan a quarter-viewport at 1:1.
            self._pan_by((self.width() // 4) * pan[0],
                         (self.height() // 4) * pan[1])
        elif keymap.matches(event, "viewer.prev"):
            self._step(-1)
        elif keymap.matches(event, "viewer.next"):
            self._step(1)
        elif keymap.matches(event, "app.info"):
            # Bare I toggles the metadata inspector (q6l.25). key_only
            # matching ignores modifiers; placed LAST, after every exact
            # Ctrl-chord check above (pan/hold/locate), per the keymap
            # dispatch-order contract.
            self.info_toggle_requested.emit()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position()
            self._dragging = False
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        # Only arrives while a button is held (no mouse tracking): past the
        # click slop this press is a DRAG — pan at 1:1, and never toggle on
        # the release. Incremental (press_pos walks with the cursor), so a
        # long drag pans smoothly rather than jumping from the press point.
        if self._press_pos is None:
            super().mouseMoveEvent(event)
            return
        d = event.position() - self._press_pos
        if not self._dragging and d.manhattanLength() < CLICK_SLOP:
            return
        self._dragging = True
        if self.zoomed:
            self._pan_by(d.x(), d.y())
        self._press_pos = event.position()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton \
                and self._press_pos is not None:
            if not self._dragging:
                if self._current_is_video() and self.video_playback_allowed:
                    # Video (v46.3): the seek bar seeks, anywhere else
                    # toggles play/pause — click-to-zoom is a still-photo
                    # gesture (1:1 is unsupported for video in v1).
                    self._video_click(event.position())
                else:
                    # A clean click: toggle fit <-> 1:1 anchored at the
                    # click point (Picasa's click-to-zoom muscle memory).
                    self.toggle_zoom(event.position())
            self._press_pos = None
            self._dragging = False
        else:
            super().mouseReleaseEvent(event)

    def hideEvent(self, event) -> None:
        # Leaving the viewer (the pages stack switched away, or teardown)
        # stops playback and reaps the streaming worker (v46.3): a hidden
        # page must never keep a decode process or audio sink alive.
        self._stop_video()
        super().hideEvent(event)

    def mouseDoubleClickEvent(self, _event) -> None:
        # Note the first press/release of a double-click already toggled the
        # zoom — a transient frame we accept: the viewer closes right here,
        # and zoom state resets with the next photo shown anyway.
        self.closed.emit(self.current_index())

    def _info_text(self, photo) -> str:
        """The info-bar line for `photo` under the current position/zoom
        state (extracted from paintEvent so tests can assert the text)."""
        parts = [f"{self.pos + 1}/{len(self.display)}", photo.rel]
        if photo.media == "video":
            # Video chip (fauxcasa-v46.3): position/duration while a
            # playback session is live, the probed duration + play hint
            # before one, a short honest note after a failure. Replaces
            # v46.2's "playback pending" placeholder now playback ships.
            if self._video_note:
                parts.append(f"video — {self._video_note}")
            elif self._video is not None:
                pb = self._video
                parts.append(f"video · {_format_us(pb.position_us)} / "
                             f"{_format_us(pb.info.duration_us)}")
            elif self._video_duration_us:
                parts.append(f"video · {_format_us(self._video_duration_us)}"
                             " — P plays")
            else:
                parts.append("video — P plays")
        if self.zoomed:
            # The mode chip doubles as the binding hint (drag pans;
            # 1 / Ctrl+Alt+0 / click return to fit).
            parts.append("1:1 — drag to pan, click/1 to fit")
        if photo.faces and self.face_overlay_allowed:
            # The face count doubles as the F-binding hint (like the 1:1
            # chip above): subtle discoverability, no extra chrome.
            n = len(photo.faces)
            parts.append(f"{n} face{'s' if n != 1 else ''} (F)")
        if photo.has_edits:
            # Honest M1 marker (fauxcasa-cam.15): this photo carries a
            # Picasa edit recipe (only its crop is rendered; the rest of
            # the chain — and the unsaved-vs-baked state cue — is M3).
            parts.append("edited")
        if photo.star:
            parts.append("★" * min(photo.star, 5))  # 0-5 count (cam.11)
        if photo.date_taken:
            parts.append(format_date_taken(photo.date_taken))
        if photo.geotag is not None:
            parts.append(format_geotag(photo.geotag))  # §3 geotag readout
        if photo.caption:
            parts.append(f"“{photo.caption}”")
        if photo.stashed_original is not None:
            parts.append("Picasa-saved original kept")
        return "   ·   ".join(parts)

    @staticmethod
    def _display_rect(box_w: int, box_h: int, src_w: int, src_h: int,
                      cap: bool) -> QRect:
        """Centered, aspect-fit rect for `src` painted in a `box`. `cap=True`
        clamps the scale to 1:1 — the ORIGINAL never upscales past native, so a
        small photo stays crisp. The preview passes `cap=False`, filling the box
        the original will occupy: when the original is at least window-sized
        (the common photo case) both compute the SAME rect, so the hand-off is a
        pure sharpen-in-place. (An original SMALLER than the window draws capped
        at native, smaller than the filled preview — a one-off downward pop we
        accept: the cache stores thumb dims, not the original's, so the preview
        cannot predict that fit.)"""
        fit = min(box_w / src_w, box_h / src_h)
        if cap:
            fit = min(fit, 1.0)
        dw = max(1, round(src_w * fit))
        dh = max(1, round(src_h * fit))
        return QRect((box_w - dw) // 2, (box_h - dh) // 2, dw, dh)

    def _paint_video_chrome(self, painter: QPainter, w: int, h: int) -> None:
        """Painted video controls (fauxcasa-v46.3): a centered play
        affordance whenever the video is NOT playing (poster, paused, or
        ended — it doubles as the paused indicator), and a minimal
        transport strip above the info bar: play/pause glyph, elapsed /
        total time, and a thin click-to-seek bar. All chrome, no child
        widgets — keyboard focus and every key binding keep working."""
        pb = self._video
        playing = pb is not None and pb.playing
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not playing:
            cx, cy = w / 2.0, h / 2.0
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(AFFORDANCE_BG)
            painter.drawEllipse(QPointF(cx, cy), 34.0, 34.0)
            painter.setBrush(TRANSPORT_FG)
            # +3 px: optically center the triangle inside the disc.
            painter.drawPolygon(_play_triangle(cx + 3, cy, 16))
        strip = self._transport_rect()
        painter.fillRect(strip, CAPTION_BG)
        gy = strip.center().y()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(TRANSPORT_FG)
        if playing:                               # pause: two bars
            painter.fillRect(QRect(14, gy - 7, 4, 14), TRANSPORT_FG)
            painter.fillRect(QRect(22, gy - 7, 4, 14), TRANSPORT_FG)
        else:                                     # play triangle
            painter.drawPolygon(_play_triangle(20, gy, 8))
        dur = pb.info.duration_us if pb is not None \
            else (self._video_duration_us or 0)
        posn = pb.position_us if pb is not None else 0
        painter.setPen(QColor(220, 220, 220))
        painter.drawText(QRect(36, strip.y(), 110, strip.height()),
                         Qt.AlignmentFlag.AlignVCenter,
                         f"{_format_us(posn)} / {_format_us(dur)}")
        bar = self._transport_seek_rect()
        painter.fillRect(bar, TRANSPORT_TRACK)
        if dur > 0:
            frac = max(0.0, min(1.0, posn / dur))
            painter.fillRect(QRect(bar.x(), bar.y(),
                                   round(bar.width() * frac), bar.height()),
                             TRANSPORT_FG)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), BACKGROUND)
        w, h = self.width(), self.height()
        idx = self.current_index()
        if idx < 0:
            painter.end()
            return
        # A live playback session's latest streamed frame wins (painted
        # into the same fit rect the poster used — cap=True, video never
        # upscales past native, matching the poster's own paint); then
        # the full original once it has arrived, else the instant cached
        # preview; only when none exists do we fall back to text.
        vframe = self._video.frame if self._video is not None else None
        shown = self._shown_now()
        if vframe is not None and not vframe.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(self._display_rect(
                w, h, vframe.width(), vframe.height(), cap=True), vframe)
        elif shown is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(self._shown_rect(w, h, shown), shown)
            self._paint_faces(painter)   # no-op unless toggled on + faces
        else:
            painter.setPen(QColor(150, 150, 150))
            msg = "loading…" if self.loading else "could not decode this file"
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, msg)

        if self.video_playback_allowed \
                and self.catalog.photos[idx].media == "video":
            self._paint_video_chrome(painter, w, h)

        bar = self._info_text(self.catalog.photos[idx])
        painter.fillRect(0, h - 30, w, 30, CAPTION_BG)
        painter.setPen(QColor(220, 220, 220))
        painter.drawText(10, h - 30, w - 20, 30,
                         Qt.AlignmentFlag.AlignVCenter, bar)
        painter.end()
