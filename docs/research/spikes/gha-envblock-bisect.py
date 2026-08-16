#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6"]
# ///
"""TEMPORARY (fauxcasa-i92.3): bisect the GHA windows-latest worker death.

Round 3. Round 2 proved the production spawn machinery boots plain-python
stubs from every location on the runner; only the real worker dies -- and
the real worker is the only one that imports PySide6. A native-level
death during the Qt DLL load (ExitProcess-style, no Python exception)
would explain the silent exit-1 that the phase-1 reporter cannot catch.
These stubs import PySide6 WITHOUT redirecting stderr first, so whatever
the loader prints arrives on the control pipe for us to read.

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

STUB_QTCORE = (
    "import sys\n"
    "sys.stdout.write('PRE-IMPORT\\n')\n"
    "sys.stdout.flush()\n"
    "from PySide6.QtCore import QSize\n"
    "sys.stdout.write('QTCORE-OK\\n')\n"
    "sys.stdout.flush()\n"
    "from PySide6.QtGui import QImage\n"
    "sys.stdout.write('QTGUI-OK\\n')\n"
    "sys.stdout.flush()\n"
    "sys.stdin.buffer.read(1)\n"
)

STUB_LISTDIR = (
    "import sys, os\n"
    "site = os.environ.get('PYTHONPATH', '')\n"
    "p = os.path.join(site, 'PySide6')\n"
    "try:\n"
    "    names = os.listdir(p)\n"
    "    sys.stdout.write('LISTDIR-OK %d entries\\n' % len(names))\n"
    "    ok = err = 0\n"
    "    first_err = ''\n"
    "    for n in names:\n"
    "        f = os.path.join(p, n)\n"
    "        if not os.path.isfile(f):\n"
    "            continue\n"
    "        try:\n"
    "            with open(f, 'rb') as fh:\n"
    "                fh.read(16)\n"
    "            ok += 1\n"
    "        except OSError as e:\n"
    "            err += 1\n"
    "            if not first_err:\n"
    "                first_err = '%s: %r' % (n, e)\n"
    "    sys.stdout.write('READ ok=%d err=%d first_err=%s\\n' % (ok, err, first_err))\n"
    "except OSError as e:\n"
    "    sys.stdout.write('LISTDIR-FAIL %r\\n' % (e,))\n"
    "sys.stdout.flush()\n"
    "sys.stdin.buffer.read(1)\n"
)


def try_spawn(label: str, worker_python: str, script: str, pythonpath, sid) -> None:
    try:
        child = dw._spawn_appcontainer(
            worker_python, script, sid, pythonpath, {}, 2 * 1024 * 1024 * 1024)
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
    while time.monotonic() < deadline and t.is_alive() and len(buf) < 3900:
        time.sleep(0.25)
    code = ctypes.wintypes.DWORD(0)
    dw.kernel32.GetExitCodeProcess(child.pi.hProcess, ctypes.byref(code))
    print(f"VERDICT {label}: exit={code.value:#010x} bytes={len(buf)}", flush=True)
    print(f"OUTPUT {label}: {bytes(buf[:3900])!r}", flush=True)
    child.close()


def main() -> None:
    worker_python, site = dw.resolve_worker_python()
    base_dir = os.path.dirname(worker_python)

    tmp_c = Path(tempfile.mkdtemp(prefix="fauxcasa-bisect-"))
    qt_stub = tmp_c / "qt_stub.py"
    qt_stub.write_text(STUB_QTCORE)
    ls_stub = tmp_c / "ls_stub.py"
    ls_stub.write_text(STUB_LISTDIR)

    sid = dw.create_or_derive_profile("fauxcasa.decode.bisect")
    for d in (base_dir, site, str(APPDIR), str(tmp_c)):
        dw.grant_read_execute(d, sid)
    print("winsta:", dw.grant_winsta_desktop(sid), flush=True)

    try_spawn("listdir-site", worker_python, str(ls_stub), site, sid)
    try_spawn("qt-import-noredirect", worker_python, str(qt_stub), site, sid)


if __name__ == "__main__":
    main()
