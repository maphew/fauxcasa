"""Typed interface skeleton for the sandboxed decode service (fauxcasa-i92.2).

This module is the *shape* of the boundary designed in
docs/design/decode-service.md — the request/response dataclasses, the
closed enums, the response-validation invariant, and the abstract
transport — with no transport implementation (that is fauxcasa-i92.3).
Nothing in the app imports it yet; it exists so the design is expressed
in the one language a review can't argue with.

Boundary rules it encodes (see the design doc, sections 2-3):

- Workers receive an OPEN FILE (fd on POSIX via SCM_RIGHTS, duplicated
  handle on Windows) and return ONLY raw pixel buffers + plain fields.
  No encoded image authored inside the sandbox is ever re-parsed by the
  trusted side; the broker does its own JPEG encoding for the fcache.
- Every buffer a worker describes is validated against the fixed-shape
  checklist (PixelBuffer.validate) BEFORE any trusted code touches the
  shared-memory arena. Validation failure is treated as evidence of
  compromise, not as an error result.
- The wire protocol is versioned (PROTO major, exact match) and the
  decoder bundle is a versioned artifact reported in the hello
  handshake, so decoder security updates ship without app releases.

Stdlib-only and import-clean by design: importing this module must never
pull Qt, decoders, or anything else a broker-side unit test would trip
over.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Sequence

# Wire-protocol major version: the broker requires an exact match with a
# worker's hello and refuses the worker otherwise (fail loud, N7). Fields
# are additive within a major; anything structural bumps it.
PROTO = 1

# Trusted-side caps (validation checklist, design doc section 2.4).
# MAX_PIXELS is a revisit knob: larger panoramas fail honestly with
# TOO_LARGE (never a silent cap) until a tiled-decode follow-up exists.
MAX_EDGE = 32_768          # per-axis pixel cap
MAX_PIXELS = 64 * 2**20    # 64 MP -> 256 MiB RGBA, the default arena size
ARENA_BYTES = 256 * 2**20  # per-worker shared-memory response arena
MAX_CONTROL_MSG = 64 * 2**10  # control-channel JSON hard cap (bytes)

MAX_CAPTION_BYTES = 8 * 2**10
MAX_KEYWORDS = 64
MAX_FACES = 128


class PixelFormat(Enum):
    """Closed set of pixel layouts a worker may return (v1 may implement
    RGBA8 only; the enum exists so adding one is additive, never ad hoc)."""

    RGBA8 = "RGBA8"
    BGRA8 = "BGRA8"
    GRAY8 = "GRAY8"

    @property
    def bytes_per_pixel(self) -> int:
        return 1 if self is PixelFormat.GRAY8 else 4


class ErrorCode(Enum):
    """The error taxonomy (design doc section 2.5). Broker retry policy:
    TIMEOUT / WORKER_CRASHED retry once on a fresh worker; PROTOCOL never
    retries (kill worker, loud log); the rest are permanent for the file
    until the decoder-bundle version changes."""

    UNSUPPORTED = "UNSUPPORTED"      # no bundled decoder claims the format
    CORRUPT = "CORRUPT"              # decoder engaged and failed
    TOO_LARGE = "TOO_LARGE"          # beyond MAX_EDGE / MAX_PIXELS caps
    TIMEOUT = "TIMEOUT"              # deadline hit; worker was killed
    WORKER_CRASHED = "WORKER_CRASHED"  # process died mid-job
    PROTOCOL = "PROTOCOL"            # malformed/malicious response
    CANCELLED = "CANCELLED"          # broker withdrew the job


class DecodeServiceError(Exception):
    """A job's terminal failure, carrying its taxonomy code."""

    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        super().__init__(f"{code.value}: {detail}" if detail else code.value)
        self.code = code
        self.detail = detail


class ProtocolViolation(DecodeServiceError):
    """Validation failure = evidence of compromise (kill worker, no retry,
    loud log). Distinct type so broker code cannot accidentally handle it
    like an ordinary decode failure."""

    def __init__(self, detail: str) -> None:
        super().__init__(ErrorCode.PROTOCOL, detail)


# ---------------------------------------------------------------------------
# Handshake


@dataclass(frozen=True)
class BundleInfo:
    """The versioned decoder bundle a worker is running (hello handshake).
    Recorded into the artifacts it produced (fcache sidecar / catalog
    `decoded_by`) so a bundle security update can re-try past failures and
    a bad bundle has an exact re-index blast radius."""

    id: str
    version: str
    components: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Hello:
    """First message on a worker's control channel. The broker enforces
    nothing from it except the proto match: arena_bytes must equal what
    the broker allocated (mismatch is a ProtocolViolation) and max_pixels
    is clamped to the broker's own caps."""

    proto: int
    bundle: BundleInfo
    ops: tuple[str, ...]
    arena_bytes: int
    max_pixels: int


# ---------------------------------------------------------------------------
# Requests (broker -> worker; the open file rides OS-natively beside them)


@dataclass(frozen=True)
class DecodeRequest:
    """One-shot decode: stills, RAW (edge=0 -> full resolution; edge=N ->
    scaled decode to fit an N px long edge). EXIF orientation is applied
    inside the worker; output is display-upright."""

    edge: int = 0
    pixfmt: PixelFormat = PixelFormat.RGBA8
    deadline_ms: int = 20_000


@dataclass(frozen=True)
class IndexRequest:
    """Index-time batch: read the fd once, sha256 those bytes, parse
    in-file metadata from them, scaled-decode, downscale to each level.
    Levels come back as RAW PIXELS; the broker encodes the fcache JPEGs."""

    levels: tuple[int, ...] = (512, 256, 128)
    want_hash: bool = True
    want_meta: bool = True
    pixfmt: PixelFormat = PixelFormat.RGBA8
    deadline_ms: int = 30_000


@dataclass(frozen=True)
class PosterRequest:
    """Video poster frame: interface-identical to decode. `at` is a
    fraction of the stream duration; workers fall back to the first
    decodable frame."""

    edge: int = 512
    at: float = 0.1
    pixfmt: PixelFormat = PixelFormat.RGBA8
    deadline_ms: int = 30_000


@dataclass(frozen=True)
class StreamOpenRequest:
    """Video playback: pins a worker for the stream's life; frames arrive
    through a ring of `slots` arena slots, audio as PCM chunks (design doc
    section 3c). Per-frame progress deadline, not a whole-job one."""

    max_edge: int = 3_840
    slots: int = 8
    pixfmt: PixelFormat = PixelFormat.RGBA8
    frame_deadline_ms: int = 5_000


# ---------------------------------------------------------------------------
# Responses (worker -> broker; bulk bytes live in the arena)


@dataclass(frozen=True)
class PixelBuffer:
    """A fixed-shape pixel buffer at (off, len) inside one worker's arena.

    THE core boundary invariant: no trusted code touches arena bytes
    before validate() passes. This is the one piece of this skeleton with
    real logic, because the design's security reduces to it."""

    w: int
    h: int
    stride: int
    pixfmt: PixelFormat
    off: int
    len: int

    def validate(self, arena_bytes: int = ARENA_BYTES,
                 max_edge: int = MAX_EDGE,
                 max_pixels: int = MAX_PIXELS) -> None:
        """Raise ProtocolViolation unless this description is internally
        consistent and inside the arena. Overflow-safe by Python ints."""
        if not (0 < self.w <= max_edge and 0 < self.h <= max_edge):
            raise ProtocolViolation(f"dims {self.w}x{self.h} out of range")
        if self.w * self.h > max_pixels:
            raise ProtocolViolation(
                f"{self.w * self.h} px exceeds max_pixels {max_pixels}")
        bpp = self.pixfmt.bytes_per_pixel
        if self.stride < self.w * bpp or self.stride % 4 != 0:
            raise ProtocolViolation(
                f"stride {self.stride} invalid for w={self.w} {self.pixfmt.value}")
        if self.len != self.stride * self.h:
            raise ProtocolViolation(
                f"len {self.len} != stride*h {self.stride * self.h}")
        if self.off < 0 or self.off % 4 != 0:
            raise ProtocolViolation(f"offset {self.off} invalid")
        if self.off + self.len > arena_bytes:
            raise ProtocolViolation(
                f"buffer [{self.off}, {self.off + self.len}) outside "
                f"arena of {arena_bytes}")

    @staticmethod
    def validate_disjoint(buffers: Sequence["PixelBuffer"]) -> None:
        """Buffers within one response must not overlap (checklist item 4)."""
        spans = sorted((b.off, b.off + b.len) for b in buffers)
        for (_, end_a), (start_b, _) in zip(spans, spans[1:]):
            if start_b < end_a:
                raise ProtocolViolation("overlapping buffers in one response")


@dataclass(frozen=True)
class FaceRegion:
    """Normalized [0,1] geometry; trusted side re-checks range/finiteness."""

    name: str
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class MetaFields:
    """In-file metadata parsed inside the sandbox (i92.4 wrapper,
    bytes-mode python-exiv2 + ExtendedXMP shim). All fields optional and
    clamped by the broker (MAX_CAPTION_BYTES / MAX_KEYWORDS / MAX_FACES).
    `taken` stays a string: we parse dates ourselves, keeping footgun 16
    dead (no year floor)."""

    caption: str = ""
    keywords: tuple[str, ...] = ()
    taken: str = ""
    gps: tuple[float, float] | None = None
    rating: int = 0
    faces: tuple[FaceRegion, ...] = ()


@dataclass(frozen=True)
class DecodeResult:
    """decode/poster response: one validated buffer + source dimensions."""

    pixels: PixelBuffer
    source_w: int
    source_h: int


@dataclass(frozen=True)
class IndexResult:
    """index response: identity hash, metadata, and one raw-pixel buffer
    per requested level (broker encodes these into the fcache)."""

    sha256: str
    source_w: int
    source_h: int
    meta: MetaFields
    levels: tuple[PixelBuffer, ...]


@dataclass(frozen=True)
class StreamInfo:
    """stream_open response; frames then arrive as (slot, pts) events."""

    duration_us: int
    fps: float
    w: int
    h: int
    has_audio: bool
    audio_rate: int = 0
    audio_channels: int = 0


# ---------------------------------------------------------------------------
# Transport seam


class Transport(ABC):
    """How jobs reach workers. Two implementations planned (i92.3):

    - InProcessTransport: today's behaviour behind the new interface —
      the migration vehicle, no sandbox, so call sites can move first.
    - SandboxedPoolTransport: the broker/worker pool of the design doc
      (persistent sandboxed workers, fd/handle passing, shm arenas,
      per-platform authority stripping).

    submit() blocks the calling (worker-side of the app, never the GUI)
    thread; concurrency and queueing live in DecodeService/broker, which
    also owns retry policy — a Transport reports failures, it never
    retries."""

    @abstractmethod
    def start(self) -> None:
        """Bring up whatever the transport needs (lazily, cheaply)."""

    @abstractmethod
    def submit(self, path: Path,
               request: DecodeRequest | IndexRequest | PosterRequest,
               ) -> DecodeResult | IndexResult:
        """Run one job to completion; raise DecodeServiceError on failure."""

    @abstractmethod
    def open_stream(self, path: Path,
                    request: StreamOpenRequest) -> "StreamHandle":
        """Pin a worker for playback; raise DecodeServiceError on failure."""

    @abstractmethod
    def close(self) -> None:
        """Tear down workers; safe to call twice."""


class StreamHandle(ABC):
    """A live playback stream (design doc section 3c): frames via an
    arena ring, control via these methods. Freeing a slot is what returns
    it to the worker (backpressure = ring full)."""

    @abstractmethod
    def info(self) -> StreamInfo: ...

    @abstractmethod
    def next_frame(self, timeout_ms: int) -> tuple[int, int, PixelBuffer]:
        """Block for the next decoded frame: (slot, pts_us, buffer)."""

    @abstractmethod
    def free_slot(self, slot: int) -> None: ...

    @abstractmethod
    def seek(self, pts_us: int) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


class DecodeService:
    """The facade every call site uses (viewer, slideshow, thumbcache
    indexer, video poster/playback). Owns the transport, the two queue
    lanes (interactive vs batch, one worker always reserved interactive),
    and the retry policy. Skeleton only: construction wires a transport;
    the methods are the agreed call-site surface."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def decode(self, path: Path, edge: int = 0) -> DecodeResult:
        """Viewer / slideshow one-shot original decode (interactive lane)."""
        raise NotImplementedError("fauxcasa-i92.3")

    def index(self, path: Path,
              levels: tuple[int, ...] = (512, 256, 128)) -> IndexResult:
        """Index-time batch: hash + metadata + per-level pixels (batch lane)."""
        raise NotImplementedError("fauxcasa-i92.3")

    def poster(self, path: Path, edge: int = 512,
               at: float = 0.1) -> DecodeResult:
        """Video poster frame for the index (batch lane)."""
        raise NotImplementedError("fauxcasa-i92.3")

    def open_stream(self, path: Path, max_edge: int = 3_840) -> StreamHandle:
        """Video playback stream (pins the interactive worker)."""
        raise NotImplementedError("fauxcasa-i92.3")

    def close(self) -> None:
        self._transport.close()
