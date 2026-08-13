"""Sandboxed decode worker entry point (fauxcasa-i92.3, Stage B).

Spawned directly as `<base_interpreter> decodesvc_worker_win.py` inside a
Windows AppContainer + job object by decodesvc_win.WinSandboxWorker.spawn()
(never via `uv` or a venv trampoline python.exe -- gotcha 1). Speaks the
same length-prefixed JSON control-channel framing as decodesvc_win.py, but
the framing is INLINED here rather than imported: this file's import list
IS the sandboxed process's attack surface budget, so it stays minimal and
does not pull in decodesvc_win.py (which imports subprocess, ACL/job-object
ctypes bindings, etc. -- none of which the worker itself ever needs).

Two-phase per docs/design/decode-service.md sec 4:

  PHASE 1 (full-ish authority, still able to do things a stripped-down
  process could not): import everything the job loop will ever need --
  PySide6.QtGui (QImageReader/QImage) with QT_QPA_PLATFORM=offscreen set
  BEFORE the import -- then say `hello`, then adopt the arena mapping
  handle the broker hands over. Python's lazy imports would otherwise
  fail post-lockdown, so nothing decode-related is imported later.

  PHASE 2: report `{"locked": true, "is_appcontainer": <bool>}`. On
  Windows the authority stripping itself is applied by the PARENT at
  spawn time (AppContainer token + job object) -- this phase mostly
  ASSERTS that state via TokenIsAppContainer (gotcha: read the token,
  do not trust the environment).

Then the job loop: `decode` (real work) and `probe` (test-only, requires
FAUXCASA_DECODESVC_PROBE=1 in the environment -- production spawns never
set this, so the hostile-attempt corpus below is dead code in prod).

ctypes + stdlib only, plus PySide6 (the decode stack itself). No pywin32.
"""

from __future__ import annotations

import os

# Must be set BEFORE importing anything from PySide6.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import base64
import ctypes
import json
import struct
import sys
from ctypes import wintypes

PROTO = 1  # decodesvc.PROTO, mirrored here to avoid importing decodesvc.py
MAX_CONTROL_MSG = 64 * 2**10  # decodesvc.MAX_CONTROL_MSG, mirrored
MAX_PIXELS = 64 * 2**20  # decodesvc.MAX_PIXELS, mirrored
MAX_EDGE = 32_768  # decodesvc.MAX_EDGE, mirrored -- per-axis cap

ARENA_BYTES = int(os.environ.get("FAUXCASA_DECODESVC_ARENA_BYTES", str(256 * 2**20)))
PROBE_ENABLED = os.environ.get("FAUXCASA_DECODESVC_PROBE") == "1"

# Same deterministic formula as decodesvc_win.ARENA_PROBE_PATTERN, recomputed
# independently rather than imported (keeps this file's import list minimal).
ARENA_PROBE_PATTERN = bytes((i * 7 + 11) % 256 for i in range(256))


# ---------------------------------------------------------------------------
# Length-prefixed JSON framing (inlined; mirrors decodesvc_win._read_frame /
# _write_frame exactly, minus the broker-only exception types).

def _read_frame(fh):
    header = fh.read(4)
    if not header or len(header) < 4:
        return None
    (length,) = struct.unpack("<I", header)
    if length > MAX_CONTROL_MSG:
        return None
    payload = b""
    while len(payload) < length:
        chunk = fh.read(length - len(payload))
        if not chunk:
            return None
        payload += chunk
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _write_frame(fh, obj) -> None:
    data = json.dumps(obj).encode("utf-8")
    if len(data) > MAX_CONTROL_MSG:
        # FIX 9 (nit, review fix pass): symmetric to the broker's own
        # _write_frame guard (decodesvc_win.py) -- never even attempt to
        # put an outgoing frame past the wire cap on the pipe. Raising
        # (rather than truncating or silently sending it anyway) lets the
        # job loop's own exception handling fall back to a small,
        # in-cap error response instead.
        raise ValueError(
            f"outgoing control frame {len(data)} bytes exceeds MAX_CONTROL_MSG {MAX_CONTROL_MSG}")
    fh.write(struct.pack("<I", len(data)))
    fh.write(data)
    fh.flush()


# ---------------------------------------------------------------------------
# Token introspection (own copy of the spike's is_appcontainer(), not shared
# with decodesvc_win.py -- see module docstring).

def _is_appcontainer():
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        TOKEN_QUERY = 0x0008
        TokenIsAppContainer = 29
        tok = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(tok)):
            return f"err:OpenProcessToken {ctypes.get_last_error()}"
        val = wintypes.DWORD(0)
        rl = wintypes.DWORD(0)
        ok = advapi32.GetTokenInformation(tok, TokenIsAppContainer, ctypes.byref(val), 4, ctypes.byref(rl))
        kernel32.CloseHandle(tok)
        if not ok:
            return f"err:GetTokenInformation {ctypes.get_last_error()}"
        return bool(val.value)
    except Exception as e:
        return f"err:{e!r}"


# ---------------------------------------------------------------------------
# Arena mapping (adopt the duplicated file-mapping handle the broker sends
# in the attach_arena control message -> MapViewOfFile via ctypes).

def _map_arena(handle_val: int, arena_bytes: int):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    FILE_MAP_ALL_ACCESS = 0x000F001F
    kernel32.MapViewOfFile.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
    kernel32.MapViewOfFile.restype = ctypes.c_void_p
    addr = kernel32.MapViewOfFile(wintypes.HANDLE(handle_val), FILE_MAP_ALL_ACCESS, 0, 0, arena_bytes)
    return addr  # int address, or None on failure


# ---------------------------------------------------------------------------
# op: decode

def _handle_decode(req: dict, out_file, arena_addr: int, qt) -> dict:
    rid = req.get("id")
    try:
        import msvcrt
        handle_val = req["handle"]
        edge = int(req.get("edge", 0))
        fd = msvcrt.open_osfhandle(handle_val, os.O_RDONLY)
        with os.fdopen(fd, "rb") as fh:
            data = fh.read()

        buf = qt.QBuffer()
        buf.setData(qt.QByteArray(data))
        buf.open(qt.QIODevice.ReadOnly)
        reader = qt.QImageReader(buf)
        reader.setAutoTransform(True)  # EXIF orientation -> display-upright

        # FIX 6 (P2, review fix pass): UNSUPPORTED vs CORRUPT taxonomy
        # (design doc sec 2.5). canRead() is Qt's own "does any bundled
        # decoder plugin claim this format" check -- false here means no
        # decoder engaged at all, which is UNSUPPORTED, not a decode
        # failure.
        if not reader.canRead():
            return {"id": rid, "ok": False, "error": "UNSUPPORTED",
                     "detail": f"no bundled decoder claims this format: {reader.errorString()}"}

        src_size = reader.size()
        source_w = source_h = 0
        if src_size.isValid() and src_size.width() > 0 and src_size.height() > 0:
            source_w, source_h = src_size.width(), src_size.height()
            if edge > 0:
                w0, h0 = source_w, source_h
                if w0 >= h0:
                    new_w, new_h = edge, max(1, round(h0 * edge / w0))
                else:
                    new_h, new_w = edge, max(1, round(w0 * edge / h0))
                reader.setScaledSize(qt.QSize(new_w, new_h))

        img = reader.read()
        if img.isNull():
            # canRead() passed (a plugin claimed the format) but the actual
            # read still failed -- engaged-but-failed is CORRUPT, EXCEPT
            # Qt can still report UnsupportedFormatError post-hoc for some
            # plugins/codepaths (e.g. an unsupported sub-variant sniffed
            # only once decoding starts); honor that the same way.
            if reader.error() == qt.QImageReader.UnsupportedFormatError:
                return {"id": rid, "ok": False, "error": "UNSUPPORTED", "detail": reader.errorString()}
            return {"id": rid, "ok": False, "error": "CORRUPT", "detail": reader.errorString()}

        if not source_w or not source_h:
            source_w, source_h = img.width(), img.height()

        # FIX 5 (P2, review fix pass): per-axis MAX_EDGE check, ahead of the
        # convertToFormat/constBits work below. An image like 32769x1 is
        # under MAX_PIXELS and under the arena but over MAX_EDGE per-axis
        # (design doc sec 2.4 checklist item 1); reporting it as ok here
        # would make the broker's PixelBuffer.validate() reject it as a
        # PROTOCOL violation -- killing a legitimately-behaving worker for
        # an honestly-oversized file. Report the documented TOO_LARGE file
        # error instead.
        if img.width() > MAX_EDGE or img.height() > MAX_EDGE:
            return {"id": rid, "ok": False, "error": "TOO_LARGE",
                     "detail": f"{img.width()}x{img.height()} exceeds per-axis "
                               f"MAX_EDGE {MAX_EDGE}"}

        img = img.convertToFormat(qt.QImage.Format_RGBA8888)
        pixel_bytes = bytes(img.constBits())
        stride = img.bytesPerLine()
        length = len(pixel_bytes)
        if img.width() * img.height() > MAX_PIXELS or length > ARENA_BYTES:
            return {"id": rid, "ok": False, "error": "TOO_LARGE",
                     "detail": f"{img.width()}x{img.height()} ({length} bytes) exceeds caps"}

        ctypes.memmove(arena_addr, pixel_bytes, length)
        return {
            "id": rid, "ok": True,
            "source": {"w": source_w, "h": source_h},
            "pixels": {"w": img.width(), "h": img.height(), "stride": stride,
                       "pixfmt": "RGBA8", "off": 0, "len": length},
        }
    except Exception as e:
        return {"id": rid, "ok": False, "error": "CORRUPT", "detail": repr(e)}


# ---------------------------------------------------------------------------
# op: probe (test-only hostile-attempt corpus; design doc sec 7 gate 1)

def _probe_open_read(target: str):
    with open(target, "rb") as fh:
        fh.read(16)
    return True, "read succeeded"


def _probe_user_file_read(req):
    target = req.get("target")
    if not target:
        return False, "no target given"
    try:
        return _probe_open_read(target)
    except Exception as e:
        return False, repr(e)


def _probe_rel_path_read(req):
    target = req.get("target")
    if not target:
        return False, "no target given"
    try:
        return _probe_open_read(target)
    except Exception as e:
        return False, repr(e)


def _probe_dotdot_traversal(req):
    target = req.get("target")
    if not target:
        return False, "no target given"
    try:
        return _probe_open_read(target)
    except Exception as e:
        return False, repr(e)


def _probe_qmark_path(req):
    target = req.get("target")
    if not target:
        return False, "no target given"
    try:
        return _probe_open_read(target)
    except Exception as e:
        return False, repr(e)


def _probe_device_path(req):
    target = req.get("target", r"\\.\PhysicalDrive0")
    try:
        return _probe_open_read(target)
    except Exception as e:
        return False, repr(e)


def _probe_temp_write(req):
    target = req.get("target")
    if not target:
        return False, "no target given"
    try:
        with open(target, "wb") as fh:
            fh.write(b"x")
        return True, "write succeeded"
    except Exception as e:
        return False, repr(e)


def _probe_registry_hklm_read(req):
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        HKEY_LOCAL_MACHINE = ctypes.c_void_p(0x80000002)
        KEY_READ = 0x20019
        hkey = wintypes.HANDLE()
        subkey = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        rc = advapi32.RegOpenKeyExW(HKEY_LOCAL_MACHINE, subkey, 0, KEY_READ, ctypes.byref(hkey))
        if rc != 0:
            return False, f"RegOpenKeyExW rc={rc}"
        try:
            buf = ctypes.create_unicode_buffer(256)
            size = wintypes.DWORD(ctypes.sizeof(buf))
            rc2 = advapi32.RegQueryValueExW(hkey, "ProductName", None, None, buf, ctypes.byref(size))
            if rc2 != 0:
                return False, f"RegQueryValueExW rc={rc2}"
            return True, f"read {buf.value!r}"
        finally:
            advapi32.RegCloseKey(hkey)
    except Exception as e:
        return False, repr(e)


def _probe_registry_hkcu_write(req):
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        HKEY_CURRENT_USER = ctypes.c_void_p(0x80000001)
        KEY_WRITE = 0x20006
        REG_OPTION_NON_VOLATILE = 0
        hkey = wintypes.HANDLE()
        subkey = r"Software\FauxcasaDecodeSvcProbe"
        rc = advapi32.RegCreateKeyExW(
            HKEY_CURRENT_USER, subkey, 0, None, REG_OPTION_NON_VOLATILE, KEY_WRITE,
            None, ctypes.byref(hkey), None)
        if rc != 0:
            return False, f"RegCreateKeyExW rc={rc}"
        try:
            REG_SZ = 1
            value = "probe"
            data = ctypes.create_unicode_buffer(value)
            size = (len(value) + 1) * 2
            rc2 = advapi32.RegSetValueExW(hkey, "probe", 0, REG_SZ, data, size)
            if rc2 != 0:
                return False, f"RegSetValueExW rc={rc2}"
            return True, "write succeeded"
        finally:
            advapi32.RegCloseKey(hkey)
    except Exception as e:
        return False, repr(e)


def _probe_socket_create(req):
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.close()
        return True, "socket() succeeded"
    except Exception as e:
        return False, repr(e)


def _probe_socket_tcp_connect(req):
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        try:
            s.connect(("127.0.0.1", 445))
            return True, "connect succeeded"
        finally:
            s.close()
    except Exception as e:
        return False, repr(e)


def _probe_dns_lookup(req):
    try:
        import socket
        addrs = socket.getaddrinfo("example.com", 80)
        return True, f"resolved {len(addrs)} addrs"
    except Exception as e:
        return False, repr(e)


# FIX 8 (should-fix, review fix pass): network-probe coverage backing the
# socket_create "document, don't assert" decision (see
# test_socket_create_documented_not_asserted) -- socket_create's inertness
# is only meaningful evidence of containment if actual I/O is separately
# proven denied. These three cover UDP send, listen/accept, and a
# non-loopback TCP connect distinct from the existing loopback:445 gate.

def _probe_udp_sendto(req):
    """UDP to a routable non-loopback address (8.8.8.8:53) -- must
    fail/timeout at the socket layer (no AppContainer network
    capability). Distinct from TCP: UDP is connectionless, so a bare
    sendto() succeeding is not by itself proof of exfiltration, but the
    zero-capability AppContainer's WFP filters are documented (design doc
    sec 4) to block ALL outbound I/O, TCP or UDP alike, at the socket
    layer -- so a raised error here is the expected signal."""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.5)
        try:
            s.sendto(b"fauxcasa-decodesvc-probe", ("8.8.8.8", 53))
            return True, "sendto() succeeded"
        finally:
            s.close()
    except Exception as e:
        return False, repr(e)


def _probe_socket_bind_listen(req):
    """Bind a TCP listener + accept -- must fail. Without any
    AppContainer network capability (internetClientServer /
    privateNetworkClientServer), the process must not be able to open a
    listening socket at all."""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", 0))
            s.listen(1)
            s.settimeout(1.0)
            try:
                conn, _addr = s.accept()
                conn.close()
                return True, "bind+listen+accept succeeded (a client connected)"
            except TimeoutError:
                # bind()/listen() themselves did not raise -- that alone is
                # the containment question this probe asks (nothing
                # connecting within the timeout is not itself a denial).
                return True, "bind()+listen() succeeded (no client connected within timeout)"
        finally:
            s.close()
    except Exception as e:
        return False, repr(e)


def _probe_tcp_connect_nonloopback(req):
    """TCP connect to a routable non-loopback address:443 -- must
    fail/timeout, distinct from the existing loopback:445
    (socket_tcp_connect) gate: containment must hold for reachable
    external hosts, not just prove loopback is denied."""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        try:
            s.connect(("8.8.8.8", 443))
            return True, "connect succeeded"
        finally:
            s.close()
    except Exception as e:
        return False, repr(e)


def _probe_memory_limit_exceeded(req):
    """Attempt an allocation past the 2 GiB job ProcessMemoryLimit
    (design doc sec 4) -- must never succeed. Either Python raises
    MemoryError here (a graceful commit failure) or the job object kills
    the process outright before this function can return at all (the
    caller must handle that as WORKER_CRASHED, not as this function's
    return value)."""
    try:
        target_bytes = 3 * 1024 * 1024 * 1024  # 3 GiB, past the 2 GiB job limit
        chunk = bytearray(target_bytes)
        # Touch pages to force actual commit, not just a reserved range.
        touched = 0
        for i in range(0, len(chunk), 4096):
            chunk[i] = 1
            touched += 1
        return True, f"allocated+touched {touched} pages ({len(chunk)} bytes) without hitting the job memory limit"
    except MemoryError as e:
        return False, f"MemoryError as expected: {e!r}"
    except Exception as e:
        return False, repr(e)


def _probe_subprocess_spawn(req):
    try:
        import subprocess
        p = subprocess.run(["cmd", "/c", "exit", "0"], capture_output=True, timeout=5)
        return True, f"rc={p.returncode}"
    except Exception as e:
        return False, repr(e)


def _probe_open_clipboard(req):
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        ok = user32.OpenClipboard(None)
        if not ok:
            return False, f"OpenClipboard failed err={ctypes.get_last_error()}"
        user32.CloseClipboard()
        return True, "OpenClipboard succeeded"
    except Exception as e:
        return False, repr(e)


def _probe_handed_fd_read(req):
    """The ONE probe that must succeed -- proves the handed-file mechanism
    itself works even though everything else is denied."""
    handle_val = req.get("handle")
    if handle_val is None:
        return False, "no handle given"
    try:
        import msvcrt
        fd = msvcrt.open_osfhandle(handle_val, os.O_RDONLY)
        with os.fdopen(fd, "rb") as fh:
            data = fh.read()
        return True, base64.b64encode(data).decode("ascii")
    except Exception as e:
        return False, repr(e)


def _probe_arena_write(req, arena_addr: int):
    if arena_addr is None:
        return False, "arena not mapped"
    try:
        ctypes.memmove(arena_addr, ARENA_PROBE_PATTERN, len(ARENA_PROBE_PATTERN))
        return True, f"wrote {len(ARENA_PROBE_PATTERN)} bytes"
    except Exception as e:
        return False, repr(e)


_PROBES = {
    "user_file_read": _probe_user_file_read,
    "rel_path_read": _probe_rel_path_read,
    "dotdot_traversal": _probe_dotdot_traversal,
    "qmark_path": _probe_qmark_path,
    "device_path": _probe_device_path,
    "temp_write": _probe_temp_write,
    "registry_hklm_read": _probe_registry_hklm_read,
    "registry_hkcu_write": _probe_registry_hkcu_write,
    "socket_create": _probe_socket_create,
    "socket_tcp_connect": _probe_socket_tcp_connect,
    "dns_lookup": _probe_dns_lookup,
    "subprocess_spawn": _probe_subprocess_spawn,
    "open_clipboard": _probe_open_clipboard,
    "handed_fd_read": _probe_handed_fd_read,
    # FIX 8 (should-fix): network-I/O-denial coverage backing the
    # socket_create "document, don't assert" decision.
    "udp_sendto": _probe_udp_sendto,
    "socket_bind_listen": _probe_socket_bind_listen,
    "tcp_connect_nonloopback": _probe_tcp_connect_nonloopback,
    "memory_limit_exceeded": _probe_memory_limit_exceeded,
    # arena_write handled specially (needs arena_addr) -- see _handle_probe
}


def _handle_probe(req: dict, arena_addr: int) -> dict:
    rid = req.get("id")
    attempt = req.get("attempt")
    if not PROBE_ENABLED:
        return {"id": rid, "ok": False, "error": "UNSUPPORTED", "detail": "probe not enabled"}
    if attempt == "arena_write":
        allowed, detail = _probe_arena_write(req, arena_addr)
    else:
        fn = _PROBES.get(attempt)
        if fn is None:
            return {"id": rid, "ok": False, "error": "UNSUPPORTED",
                     "detail": f"unknown probe attempt {attempt!r}"}
        try:
            allowed, detail = fn(req)
        except Exception as e:
            allowed, detail = False, repr(e)
    out = {"id": rid, "op": "probe", "attempt": attempt, "allowed": bool(allowed), "ok": True}
    if attempt == "handed_fd_read" and allowed:
        out["data_b64"] = detail
        out["detail"] = "ok"
    else:
        out["detail"] = str(detail)
    return out


# ---------------------------------------------------------------------------
# Main

def main() -> None:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    # PHASE 1: import everything up front (design doc sec 4 -- Python's lazy
    # imports would otherwise fail post-lockdown).
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize
    from PySide6.QtGui import QImage, QImageReader
    import PySide6

    class _Qt:
        pass

    qt = _Qt()
    qt.QBuffer, qt.QByteArray, qt.QIODevice = QBuffer, QByteArray, QIODevice
    qt.QSize, qt.QImage, qt.QImageReader = QSize, QImage, QImageReader

    _write_frame(stdout, {
        "hello": 1,
        "proto": PROTO,
        "bundle": {"id": "fauxcasa-decoders", "version": "2026-08-12",
                   "components": {"qt-imageformats": PySide6.__version__}},
        "ops": ["decode", "probe"] if PROBE_ENABLED else ["decode"],
        "arena_bytes": ARENA_BYTES,
        "max_pixels": MAX_PIXELS,
    })

    # Wait for the broker's attach_arena control message.
    msg = _read_frame(stdin)
    if not isinstance(msg, dict) or msg.get("op") != "attach_arena":
        _write_frame(stdout, {"op": "attach_arena", "ok": False,
                                "error": f"expected attach_arena first, got {msg!r}"})
        return
    arena_addr = _map_arena(msg.get("handle"), ARENA_BYTES)
    if not arena_addr:
        _write_frame(stdout, {"op": "attach_arena", "ok": False,
                                "error": f"MapViewOfFile failed err={ctypes.get_last_error()} "
                                          f"handle={msg.get('handle')!r} arena_bytes={ARENA_BYTES}"})
        return
    _write_frame(stdout, {"op": "attach_arena", "ok": True})

    # PHASE 2: report lockdown state. The authority stripping itself was
    # applied by the parent at spawn (AppContainer token + job object); this
    # mostly asserts it via TokenIsAppContainer rather than trusting env.
    is_appc = _is_appcontainer()
    _write_frame(stdout, {"locked": True, "is_appcontainer": is_appc is True})

    # Job loop.
    while True:
        req = _read_frame(stdin)
        if req is None:
            return  # control pipe closed -- broker is tearing us down
        try:
            op = req.get("op")
            if op == "decode":
                resp = _handle_decode(req, stdout, arena_addr, qt)
            elif op == "probe":
                resp = _handle_probe(req, arena_addr)
            else:
                resp = {"id": req.get("id"), "ok": False, "error": "UNSUPPORTED",
                         "detail": f"unknown op {op!r}"}
            _write_frame(stdout, resp)
        except Exception as e:
            try:
                _write_frame(stdout, {"id": req.get("id") if isinstance(req, dict) else None,
                                        "ok": False, "error": "CORRUPT", "detail": repr(e)})
            except Exception:
                return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never let an uncaught exception print a traceback to a pipe the
        # broker is reading as JSON frames; just exit non-zero. The broker
        # sees this as EOF -> WORKER_CRASHED, which is the correct signal.
        sys.exit(1)
