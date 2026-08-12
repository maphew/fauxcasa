"""Video detection + poster-frame seam for the tracer app (fauxcasa-v46.2).

Detection: Picasa's documented video extension list (spec §5 Formats;
docs/research/sources/picasaresources/files-supported-by-picasa3.md).
Poster extraction: PyAV — a maintained, *updatable* libav wheel — chosen
over an imageio-ffmpeg subprocess by the merged decode-service design
(docs/design/decode-service.md §3c): a grandchild ffmpeg process punches
an exec hole in the future sandbox exactly where it must be tightest, and
pipe input defeats moov-at-end MP4s, which real family archives are full
of. PyAV reads a *seekable* source in-process, so when the sandboxed
worker pool lands (fauxcasa-i92.3) this module's poster path becomes the
in-worker `op: "poster"` implementation — a move, not a rewrite.

ROUTING IS BY EXTENSION, ahead of any content sniff, exactly like
rawload (fauxcasa-v46.1): QImageReader/PIL must never be handed video
bytes. Every decode path checks the extension FIRST and hands video
files here: thumbcache._index_one, viewer.load_original (which the
slideshow prefetch shares), and scripts/make-thumbcache.py (which
mirrors this strategy in PIL terms; VIDEO_EXTS there must match).

The seam shape (decode-service §5): ONE poster_frame(bytes-or-path) ->
pixels function — a packed RGB888 buffer plus dimensions, the same
fixed-shape "pixels out, never encodings" contract the sandboxed pool
speaks — with poster_qimage as the thin Qt adapter for today's
in-process callers. Poster policy per the design's `op: "poster"`: the
first decodable frame at ~POSTER_SEEK_S seconds in (seek is
keyframe-backward, so short clips degrade to an early frame), falling
back to the first decodable frame, fail-soft None/null on anything else
— a corrupt video yields an error tile, never a crash.

Metadata note: exiv2's video support is patchy (docs/research/
metadata-library-decision.md), so the indexer deliberately SKIPS the
metareader/inmeta piggyback for video bytes — ini sidecar values
(star/caption/albums/geotag, and width=/height= dim seeds) are the
tier-1 sources for video in M1. Playback is fauxcasa-v46.3, gated on
the decode-service §3c sandbox-valve ruling — not this module's scope.
"""

from __future__ import annotations

import io
from datetime import datetime

# Picasa 3's documented video list ("For playback in Picasa",
# files-supported-by-picasa3.md): mpg, mod, mmv, tod, wmv, asf, avi,
# divx, mov, m4v, 3gp, 3g2, mp4, m2t, m2ts, mts, mkv — PLUS .mpeg, the
# four-letter alias of .mpg (same precedent as .jpeg/.jpg in the stills
# set; the doc's own stills list spells only ".jpeg"). The doc's trailing
# .wma/.mp3 entries are audio and deliberately excluded. Must match
# scripts/make-thumbcache.py VIDEO_EXTS exactly (walk/cache-order parity
# — test_tracer.py asserts the full EXTS sets are equal).
VIDEO_EXTS = frozenset({
    ".3g2", ".3gp", ".asf", ".avi", ".divx", ".m2t", ".m2ts", ".m4v",
    ".mkv", ".mmv", ".mod", ".mov", ".mp4", ".mpeg", ".mpg", ".mts",
    ".tod", ".wmv",
})

# Poster grab point: ~1 s in (decode-service §2.2 `at`), past the black/
# fade-in first frames of typical camera clips; av seeks to the nearest
# keyframe at-or-before it, so sub-second clips land on an early frame.
POSTER_SEEK_S = 1.0


def is_video_suffix(name) -> bool:
    """True when `name` (a str/Path file name or path) has a video
    extension. Pure string routing — no file I/O, no content sniff."""
    s = str(name)
    dot = s.rfind(".")
    return dot >= 0 and s[dot:].lower() in VIDEO_EXTS


def _open_container(source):
    """An av container over bytes (via a seekable BytesIO — moov-at-end
    MP4s need seekable input, the §3c reason pipes were rejected) or a
    filesystem path. Raises on unparseable input; callers fail soft."""
    import av

    if isinstance(source, (bytes, bytearray, memoryview)):
        return av.open(io.BytesIO(bytes(source)))
    return av.open(str(source))


def _frame_rgb(frame) -> tuple[bytes, int, int]:
    """One decoded frame -> packed RGB888 rows (stride padding stripped,
    so the buffer is exactly w*3*h bytes — the fixed-shape pixel contract
    the decode-service boundary will validate)."""
    rgb = frame.reformat(format="rgb24")
    plane = rgb.planes[0]
    w, h = rgb.width, rgb.height
    raw = bytes(plane)
    row = w * 3
    stride = plane.line_size
    if stride != row:
        raw = b"".join(raw[y * stride:y * stride + row] for y in range(h))
    else:
        raw = raw[:row * h]
    return raw, w, h


def poster_frame(source, at_s: float = POSTER_SEEK_S) \
        -> tuple[bytes, int, int] | None:
    """The poster pixels for a video: (packed RGB888 bytes, width,
    height), or None when the source has no decodable video frame, is
    corrupt, or PyAV is unavailable. `source` is raw bytes (the indexer
    already holds them for sha256) or a path (the viewer — PyAV reads
    the file seekably, no whole-file load). First decodable frame at
    ~`at_s` seconds in, falling back to the first decodable frame."""
    try:
        import av

        with _open_container(source) as container:
            stream = next(
                (s for s in container.streams if s.type == "video"), None)
            if stream is None:
                return None
            frame = None
            try:
                container.seek(int(at_s * av.time_base))
                frame = next(container.decode(stream), None)
            except Exception:  # noqa: BLE001 — a failed seek is not fatal
                frame = None
            if frame is None:
                # Seek landed past the last packet (or failed outright):
                # rewind and take the first decodable frame.
                container.seek(0)
                frame = next(container.decode(stream), None)
            if frame is None:
                return None
            return _frame_rgb(frame)
    except Exception:  # noqa: BLE001 — fail-soft per file (error tile)
        return None


def poster_qimage(source):
    """poster_frame adapted to a QImage (null on any failure) — the shape
    today's in-process callers (thumbcache._index_one, viewer
    .load_original) consume. The Picasa rotate= quarter-turns compose on
    top at the caller exactly as for stills; video frames carry no EXIF
    Orientation, so no auto-transform question arises."""
    from PySide6.QtGui import QImage

    got = poster_frame(source)
    if got is None:
        return QImage()
    data, w, h = got
    img = QImage(data, w, h, 3 * w, QImage.Format.Format_RGB888)
    return img.copy()  # detach from the transient Python buffer


def probe_duration(source) -> float | None:
    """The video's duration in seconds (container-level, falling back to
    the video stream's own clock), or None when unknown/undecodable. A
    v46.3 consumer surface; indexed here so duration lives behind the
    same seam the sandboxed `op: "poster"` response will carry it in."""
    try:
        import av

        with _open_container(source) as container:
            if container.duration is not None and container.duration > 0:
                return container.duration / av.time_base
            for s in container.streams:
                if s.type == "video" and s.duration is not None \
                        and s.time_base is not None:
                    return float(s.duration * s.time_base)
    except Exception:  # noqa: BLE001 — fail-soft: duration is decoration
        pass
    return None


def _parse_creation_time(value: str | None) -> str | None:
    """ISO 8601 container creation_time -> 'YYYY-MM-DDTHH:MM:SS', or None.
    Accepts 'YYYY-MM-DDTHH:MM:SS[.frac][Z|±HH:MM]' and the rare
    space-separated variant; ignores unparseable values (all-zeros
    placeholders, out-of-range fields, etc.)."""
    if not value:
        return None
    s = value.strip()
    # Normalise the space-separated ISO 8601 variant to 'T'.
    if len(s) >= 11 and s[10] == " ":
        s = s[:10] + "T" + s[11:]
    # Strip timezone suffix: 'Z', or the first '+'/'-' at position >= 19.
    for i, c in enumerate(s):
        if i >= 19 and c in ("Z", "+", "-"):
            s = s[:i]
            break
    # Strip sub-second fraction at position >= 19.
    dot = s.find(".")
    if dot >= 19:
        s = s[:dot]
    # Validate the canonical 19-char shape 'YYYY-MM-DDTHH:MM:SS', then the
    # actual calendar: cameras emit all-zero placeholders (0000-00-00...)
    # and garbage fields, which must fall back to mtime, not sort as dates.
    if (len(s) == 19 and s[4] == "-" and s[7] == "-"
            and s[10].upper() == "T" and s[13] == ":" and s[16] == ":"):
        canon = s[:10] + "T" + s[11:19]
        try:
            datetime.strptime(canon, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
        return canon
    return None


def probe_creation_time(source) -> str | None:
    """The container's creation_time as 'YYYY-MM-DDTHH:MM:SS', or None.
    Tries container-level metadata first, then the video stream's own
    metadata dict; returns None when absent, undecodable, or unparseable.
    Same fail-soft shape as probe_duration — a missing timestamp is
    decoration, never a reason to abort an index."""
    try:
        with _open_container(source) as container:
            dt = _parse_creation_time(container.metadata.get("creation_time"))
            if dt is not None:
                return dt
            for s in container.streams:
                if s.type == "video":
                    dt = _parse_creation_time(
                        s.metadata.get("creation_time"))
                    if dt is not None:
                        return dt
    except Exception:  # noqa: BLE001 — fail-soft: creation_time is decoration
        pass
    return None
