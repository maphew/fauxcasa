"""Decode service facade (fauxcasa-ez2.9 Stage 1 -- plumbing only, no
call-site migration yet; thumbcache._index_one and viewer.
load_original_oriented keep decoding directly until Stage 2 wires them
onto this module).

`get_service()` returns a process-wide `DecodeService` singleton exposing
the call-site-facing shape from docs/design/decode-service.md sec 5:

    svc.state    -> "sandboxed" | "in-process" | "degraded"
    svc.reason   -> str (why -- empty string when state == "sandboxed")
    svc.decode(path, route="still", edge=0)                    -> QImage
    svc.index(path, top, crop=None, orientation=1, route="still") -> QImage
    svc.poster(path, edge=512)                                 -> QImage

DecodeService.decode() is ALWAYS display-upright for route="still",
regardless of which transport served it (fauxcasa-ez2.9 Stage 2 review
P2-2): WinSandboxTransport's worker applies EXIF autoTransform itself
(decodesvc_worker_win.py); InProcessTransport applies inmeta.
apply_orientation to its QImage.fromData decode (its pillow_qimage
fallback applies its own exif_transpose). Callers that need the
orientation value for crop/face math read it separately via
metareader.read_orientation(data) and must NOT re-apply it to the
returned QImage.

Every call returns a null QImage on ANY failure (open error, decode
error, sandbox unavailable) so callers keep their existing fail-soft
shape -- exactly like today's rawload/videoload/pillowload/QImageReader
call sites.

Two transports:

- `InProcessTransport` -- today's decode code paths, duplicated (per the
  lens plan's "duplicate the minimal calls" instruction, NOT moved: the
  real call sites migrate in Stage 2). Always available, every platform.
- `WinSandboxTransport` (win32 only) -- backed by
  `decodesvc_win.DecodePoolSet`. Stage 1 scope: only `decode()` for
  route="still" goes through the sandbox (the worker's `ops` list is
  `["decode"]` only, decodesvc_worker_win.py); `index()`, `poster()`, and
  every non-"still" `decode()` route ALWAYS fall back to InProcess in
  this stage, regardless of sandbox availability -- there is no sandboxed
  "index"/"poster" op yet to route them to.

Selection: `FAUXCASA_DECODE_SANDBOX=0|1|require` (env), default "1" on
win32 outside pytest, "0" everywhere else (`test_tracer.py` additionally
pins "0" via an autouse fixture, belt-and-suspenders with this default,
so its 86 build_cache / 29 load_original call sites never spawn a real
worker once Stage 2 wires them here). `require` means: a failed sandbox
startup is FATAL (raises `DecodeSandboxRequiredError` so `main()` can
exit non-zero with a clear message) -- see `main.py --require-sandbox`.

Error mapping (design doc sec migration plan item 2, "Error mapping"):
    OSError (file open)                -> null, no log (today's contract)
    DecodeServiceError (CORRUPT/...)   -> null + log.info (permanent, honest)
    ProtocolViolation                  -> null + log.error, and the sandbox
                                           transport marks the FILE permanently
                                           failed for the sandboxed path --
                                           it is NEVER retried in-process
                                           (that is the exact escape the
                                           threat model closes)
    RuntimeError from spawn            -> SESSION-level: state flips to
                                           "degraded", never a per-file
                                           fallback for files already routed
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("fauxcasa.decodefacade")

STATE_SANDBOXED = "sandboxed"
STATE_IN_PROCESS = "in-process"
STATE_DEGRADED = "degraded"


class DecodeSandboxRequiredError(RuntimeError):
    """Raised by ensure_started() when FAUXCASA_DECODE_SANDBOX=require and
    the sandbox failed to start -- main() should catch this, print a clear
    message, and exit non-zero (never silently degrade under require)."""


def under_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def sandbox_mode() -> str:
    """FAUXCASA_DECODE_SANDBOX=0|1|require. Default: "1" on win32 outside
    pytest, "0" otherwise."""
    env = os.environ.get("FAUXCASA_DECODE_SANDBOX")
    if env is not None:
        env = env.strip().lower()
        if env in ("0", "1", "require"):
            return env
        raise ValueError(f"FAUXCASA_DECODE_SANDBOX must be 0|1|require, got {env!r}")
    if sys.platform == "win32" and not under_pytest():
        return "1"
    return "0"


# ---------------------------------------------------------------------------
# Transports

class Transport:
    def decode(self, path: str, route: str = "still", edge: int = 0) -> Any:
        raise NotImplementedError

    def close(self) -> None:
        pass


class InProcessTransport(Transport):
    """Today's decode code paths, duplicated (fauxcasa-ez2.9 Stage 1 scope
    note: NOT moved -- thumbcache.py/viewer.py keep their own copies until
    Stage 2). Mirrors viewer.load_original_oriented's still/raw/tiff16/
    video routing and thumbcache._index_one's scaled-decode call, minus
    the crop/orientation/downscale/JPEG-encode steps that stay in the
    (not-yet-migrated) call sites themselves."""

    def decode(self, path: str, route: str = "still", edge: int = 0):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage

        path = str(path)
        if route == "video":
            from videoload import poster_qimage
            img = poster_qimage(path)
        else:
            try:
                data = Path(path).read_bytes()
            except OSError:
                return QImage()
            if route == "raw":
                from rawload import load_raw_qimage
                img = load_raw_qimage(data)
            elif route == "tiff16":
                from pillowload import pillow_qimage
                img = pillow_qimage(data)
            else:  # "still" (default) -- QImageReader-class formats first
                img = QImage.fromData(data)
                if not img.isNull():
                    # Orientation parity (fauxcasa-ez2.9 Stage 2 review
                    # P2-2): WinSandboxTransport's worker applies EXIF
                    # autoTransform itself (decodesvc_worker_win.py), so
                    # this branch must apply it too -- DecodeService.
                    # decode() is documented as ALWAYS display-upright,
                    # and both call sites (thumbcache._index_one, viewer.
                    # load_original_oriented) skip their own manual
                    # orientation step on the sandboxed branch. Mirrors
                    # viewer.load_original_oriented's own QImage.fromData
                    # branch. The pillow_qimage fallback below already
                    # applies orientation itself (exif_transpose) -- never
                    # composed with this.
                    from inmeta import apply_orientation
                    from metareader import read_orientation
                    img = apply_orientation(img, read_orientation(data))
                if img.isNull():
                    from pillowload import pillow_qimage
                    img = pillow_qimage(data)
        if img is None:
            from PySide6.QtGui import QImage as _QImage
            img = _QImage()
        if edge and not img.isNull() and max(img.width(), img.height()) > edge:
            img = img.scaled(edge, edge, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        return img


class NotSandboxed(Exception):
    """Raised by a sandbox transport's decode() for a route it does not
    implement yet (Stage 1: only route="still"); a MODULE-level exception
    (not nested in WinSandboxTransport) so DecodeService.decode()'s except
    clause still works when a test monkeypatches df.WinSandboxTransport to
    a stub class -- the stub raises decodefacade.NotSandboxed directly."""


class WinSandboxTransport(Transport):
    """decode() for route="still" only (Stage 1 scope), backed by a
    decodesvc_win.DecodePoolSet. Any other route raises NotSandboxed so
    the caller (DecodeService) falls back to InProcessTransport."""

    def __init__(self, n_batch: int) -> None:
        import decodesvc_win as dw

        self._dw = dw
        self._pool_set = dw.DecodePoolSet(n_batch=n_batch)

    def start(self) -> None:
        """Spawn the interactive worker (and warm the batch pool) --
        raises RuntimeError/DecodeServiceError on failure, same taxonomy
        as WinSandboxWorker.spawn()."""
        self._pool_set.warm()

    def decode(self, path: str, route: str = "still", edge: int = 0):
        if route != "still":
            raise NotSandboxed(f"route {route!r} has no sandboxed op yet")
        dw = self._dw
        lane = "interactive" if edge == 0 else "batch"
        pool = self._pool_set.lease(lane, timeout=30)
        try:
            result = pool.decode(Path(path), edge=edge)
        finally:
            self._pool_set.release(pool)
        from PySide6.QtGui import QImage
        buf = result.pixels
        return QImage(result.pixels_bytes, buf.w, buf.h, buf.stride,
                      QImage.Format.Format_RGBA8888).copy()

    def close(self) -> None:
        self._pool_set.close()


# ---------------------------------------------------------------------------
# DecodeService facade

class DecodeService:
    def __init__(self) -> None:
        self.state = STATE_IN_PROCESS
        self.reason = ""
        self._in_process = InProcessTransport()
        self._sandbox: WinSandboxTransport | None = None
        self._lock = threading.Lock()
        self._started = False
        # require-mode fail-closed latch (design doc sec "Fallback
        # policy" / require mode): once a MID-SESSION spawn-class failure
        # degrades the service under FAUXCASA_DECODE_SANDBOX=require, this
        # flips True and every later decode() call returns a null image
        # forever -- it must NEVER fall back to in-process under require.
        self._require_failed = False

    def ensure_started(self) -> None:
        """Idempotent session-start decision (design doc sec "Fallback
        policy"): try one interactive sandboxed worker; on failure set
        state="degraded" with the reason (or raise under require). Never
        called again after the first successful/failed attempt -- a later
        per-file sandbox failure is handled by decode()'s error mapping,
        never by re-running this."""
        with self._lock:
            if self._started:
                return
            # P3 finding: `_started` is only set True after a VALID
            # decision below -- previously it was set here, before
            # sandbox_mode() could raise ValueError on a bad env value,
            # so the first call raised and every later call silently
            # returned (fail-open on a typo, `_started` already True with
            # `self.state` still its in-process default). main() now
            # validates the env var at startup too (belt-and-suspenders),
            # but this method must never fail open either way.
            mode = sandbox_mode()
            if mode == "0" or sys.platform != "win32":
                self._started = True
                self.state = STATE_IN_PROCESS
                self.reason = (
                    "FAUXCASA_DECODE_SANDBOX=0" if mode == "0" else
                    f"no sandbox transport on {sys.platform!r}")
                return
            try:
                from thumbcache import INDEX_WORKERS
            except Exception:
                INDEX_WORKERS = 4
            sandbox = WinSandboxTransport(n_batch=INDEX_WORKERS)
            try:
                sandbox.start()
            except Exception as e:
                self._started = True
                self.reason = f"{type(e).__name__}: {e}"
                # P3 finding: a warm()/start() failure can leave up to
                # N-1 live AppContainer worker processes spawned before
                # the one that failed -- close() the transport instead of
                # just dropping the reference, or those processes leak
                # for the rest of the session.
                try:
                    sandbox.close()
                except Exception:
                    pass
                if mode == "require":
                    log.error("decode sandbox required but failed to start: %s", self.reason)
                    raise DecodeSandboxRequiredError(self.reason) from e
                log.error("decode sandbox failed to start, degrading to in-process: %s",
                          self.reason)
                self.state = STATE_DEGRADED
                return
            self._started = True
            self._sandbox = sandbox
            self.state = STATE_SANDBOXED
            self.reason = ""

    def decode(self, path: str, route: str = "still", edge: int = 0):
        self.ensure_started()
        from decodesvc import DecodeServiceError, ProtocolViolation

        if self._require_failed:
            # Require mode fail-closed latch: a prior mid-session
            # spawn-class failure already degraded the service and
            # FAUXCASA_DECODE_SANDBOX=require forbids ANY in-process
            # fallback -- every later call returns null forever.
            from PySide6.QtGui import QImage
            return QImage()

        # P3 finding "self._sandbox read twice": read the attribute ONCE
        # into a local under the lock. A concurrent degrade (another
        # thread's RuntimeError/OSError branch below, which reassigns
        # self._sandbox under the lock) must never be observed between a
        # "is not None" check and the subsequent `.decode()` call, or this
        # call raises AttributeError out of decode() instead of returning
        # a null image.
        with self._lock:
            sandbox = self._sandbox

        if sandbox is not None:
            try:
                return sandbox.decode(path, route=route, edge=edge)
            except NotSandboxed:
                pass  # route not sandboxed in Stage 1 -- fall through
            except ProtocolViolation as e:
                # Evidence of compromise: null + loud log, NEVER re-decode
                # this file in-process (design doc sec migration item 2).
                log.error("decode(%r): ProtocolViolation, refusing in-process "
                          "re-decode: %s", path, e)
                from PySide6.QtGui import QImage
                return QImage()
            except DecodeServiceError as e:
                log.info("decode(%r): %s", path, e)
                from PySide6.QtGui import QImage
                return QImage()
            except queue.Empty as e:
                # A contended lease (WinSandboxTransport.decode's 30s
                # lease wait on the interactive lane's single reserved
                # worker, or the batch lane) is NOT a spawn-class failure
                # -- the sandbox is fine, it is just busy. PER-FILE null
                # only; the session state/reason is untouched so the next
                # call still tries the sandbox.
                log.info("decode(%r): sandbox lease contended (timeout), "
                         "per-file null: %s", path, e)
                from PySide6.QtGui import QImage
                return QImage()
            except (RuntimeError, OSError) as e:
                # Spawn-class failure (RuntimeError), or a raw OSError
                # from create_or_derive_profile/CreateProcess -- P1/P2
                # findings: SESSION-level degrade, never per-file.
                # Critically (P1 finding "facade escape"), THIS call
                # returns a null QImage for the CURRENT path -- a file
                # that crashed/killed the sandbox worker must never be
                # decoded in-process; only SUBSEQUENT calls (after the
                # degrade below) may fall through to InProcessTransport
                # -- UNLESS require mode is active, in which case they
                # never fall through either (see _require_failed above).
                require_mode = sandbox_mode() == "require"
                with self._lock:
                    self.state = STATE_DEGRADED
                    if require_mode:
                        self.reason = "require: sandbox lost mid-session"
                        self._require_failed = True
                    else:
                        self.reason = f"{type(e).__name__}: {e}"
                    if self._sandbox is sandbox:
                        self._sandbox = None
                try:
                    sandbox.close()
                except Exception:
                    pass
                if require_mode:
                    log.error("decode sandbox required (FAUXCASA_DECODE_SANDBOX="
                              "require) but lost mid-session, refusing "
                              "in-process fallback for the rest of the "
                              "session: %s", e)
                else:
                    log.error("decode sandbox failure, degrading to "
                              "in-process for the rest of the session: %s", e)
                from PySide6.QtGui import QImage
                return QImage()
        try:
            return self._in_process.decode(path, route=route, edge=edge)
        except OSError:
            from PySide6.QtGui import QImage
            return QImage()

    def index(self, path: str, top: int, crop=None, orientation: int = 1,
              route: str = "still"):
        """Stage 1: always InProcess (no sandboxed "index" op yet, design
        doc migration plan item 1 -- lands with call-site migration)."""
        img = self._in_process.decode(path, route=route, edge=top)
        if crop is not None and not img.isNull():
            try:
                from cropmap import crop_qimage_upright
                img = crop_qimage_upright(img, crop, orientation)
            except Exception:
                pass
        return img

    def poster(self, path: str, edge: int = 512):
        """Stage 1: always InProcess (no sandboxed "poster" op yet)."""
        return self._in_process.decode(path, route="video", edge=edge)

    def close(self) -> None:
        if self._sandbox is not None:
            self._sandbox.close()
            self._sandbox = None


_service: DecodeService | None = None
_service_lock = threading.Lock()


def get_service() -> DecodeService:
    global _service
    with _service_lock:
        if _service is None:
            _service = DecodeService()
        return _service


def reset_service() -> None:
    """Test-only: drop the singleton (and close its sandbox transport, if
    any) so the next get_service() re-runs ensure_started() from scratch."""
    global _service
    with _service_lock:
        if _service is not None:
            _service.close()
        _service = None
