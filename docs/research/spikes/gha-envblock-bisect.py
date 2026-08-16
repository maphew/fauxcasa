#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6"]
# ///
"""TEMPORARY (fauxcasa-i92.3): bisect the GHA windows-latest worker death.

Round 4. Round 3 proved site-packages reads and the QtGui import all work
under the production sandbox on the runner. The real worker's entry point
swallows every main() exception into a silent sys.exit(1) -- the exact
observed signature -- so the death is an ordinary exception in main()'s
prologue, before the phase-1 import reporter. Beacon every prologue step
(sys.stdin.buffer, os.dup(1), os.open(NUL) -- the one syscall no stub has
exercised -- the dup2s, fdopen) to see exactly which one raises.

Delete this file once the culprit is identified and fixed.
"""

import ctypes
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
APPDIR = REPO / "apps" / "desktop-python"
sys.path.insert(0, str(APPDIR))

import decodesvc_win as dw  # noqa: E402

if sys.platform != "win32":
    print("windows only")
    sys.exit(0)

STUB_BEACONS = (
    "import sys, os\n"
    "os.write(1, b'[A:fd1-raw-write]')\n"
    "stdin = sys.stdin.buffer\n"
    "os.write(1, b'[B:stdin-buffer]')\n"
    "try:\n"
    "    _ctrl_fd = os.dup(1)\n"
    "    os.write(_ctrl_fd, b'[C:dup1]')\n"
    "    _nul = os.open(os.devnull, os.O_WRONLY)\n"
    "    os.write(_ctrl_fd, b'[D:open-nul]')\n"
    "    os.dup2(_nul, 1)\n"
    "    os.write(_ctrl_fd, b'[E:dup2-1]')\n"
    "    os.dup2(_nul, 2)\n"
    "    os.write(_ctrl_fd, b'[F:dup2-2]')\n"
    "    os.close(_nul)\n"
    "    os.write(_ctrl_fd, b'[G:close-nul]')\n"
    "    f = os.fdopen(_ctrl_fd, 'wb', buffering=0)\n"
    "    f.write(b'[H:fdopen]')\n"
    "    import base64, json, struct\n"
    "    import ctypes\n"
    "    from ctypes import wintypes\n"
    "    f.write(b'[I:stdlib-imports]')\n"
    "    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')\n"
    "    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize\n"
    "    f.write(b'[J:qtcore]')\n"
    "    from PySide6.QtGui import QImage, QImageReader\n"
    "    f.write(b'[K:qtgui]')\n"
    "    import PySide6\n"
    "    f.write(b'[L:pyside6-root]')\n"
    "except BaseException as e:\n"
    "    import traceback\n"
    "    msg = ('[EXC:%r]' % (e,)) + traceback.format_exc()[-800:]\n"
    "    try:\n"
    "        os.write(_ctrl_fd, msg.encode())\n"
    "    except Exception:\n"
    "        os.write(1, msg.encode())\n"
    "stdin.read(1)\n"
)


def try_spawn(label: str, worker_python: str, script: str, pythonpath, sid, extra_env) -> None:
    try:
        child = dw._spawn_appcontainer(
            worker_python, script, sid, pythonpath, extra_env,
            2 * 1024 * 1024 * 1024)
    except Exception as e:
        print(f"VERDICT {label}: SPAWN-RAISED {repr(e)[:200]}", flush=True)
        return
    buf = bytearray()

    def _read():
        while True:
            b = child.out_file.read(4096)
            if not b:
                return
            buf.extend(b)

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and t.is_alive() and len(buf) < 3000:
        time.sleep(0.25)
    code = ctypes.wintypes.DWORD(0)
    dw.kernel32.GetExitCodeProcess(child.pi.hProcess, ctypes.byref(code))
    print(f"VERDICT {label}: exit={code.value:#010x} bytes={len(buf)}", flush=True)
    print(f"OUTPUT {label}: {bytes(buf[:3000])!r}", flush=True)
    child.close()


def main() -> None:
    worker_python, site = dw.resolve_worker_python()
    base_dir = os.path.dirname(worker_python)

    tmp_c = Path(tempfile.mkdtemp(prefix="fauxcasa-bisect-"))
    stub = tmp_c / "beacon_stub.py"
    stub.write_text(STUB_BEACONS)

    sid = dw.create_or_derive_profile("fauxcasa.decode.bisect")
    for d in (base_dir, site, str(APPDIR), str(tmp_c)):
        dw.grant_read_execute(d, sid)
    print("winsta:", dw.grant_winsta_desktop(sid), flush=True)

    prod_env = {"FAUXCASA_DECODESVC_ARENA_BYTES": str(8 * 1024 * 1024),
                "FAUXCASA_DECODESVC_PROBE": "1"}
    try_spawn("beacons-prodenv", worker_python, str(stub), site, sid, prod_env)


if __name__ == "__main__":
    main()
