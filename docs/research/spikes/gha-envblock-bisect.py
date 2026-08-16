#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6"]
# ///
"""TEMPORARY (fauxcasa-i92.3): bisect the GHA windows-latest worker death.

Round 2. Round 1 exonerated the env block (all variants, including the
full parent environment, die identically) and the CWD. The Stage A spike
succeeds on this runner with the SAME base exe and lockdown attributes --
its remaining deltas are the worker script's content and location (C:
temp stub vs D: checkout decodesvc_worker_win.py). Run the PRODUCTION
spawn machinery (_spawn_appcontainer) over a matrix of scripts/locations
and report which combination boots.

Delete this file once the culprit is identified and fixed.
"""

import ctypes
import os
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
APPDIR = REPO / "apps" / "desktop-python"
sys.path.insert(0, str(APPDIR))

import decodesvc_win as dw  # noqa: E402

if sys.platform != "win32":
    print("windows only")
    sys.exit(0)

STUB = (
    "import sys\n"
    "sys.stdout.buffer.write(b'STUBOK!!')\n"
    "sys.stdout.buffer.flush()\n"
    "sys.stdin.buffer.read(1)\n"
)


def try_spawn(label: str, worker_python: str, script: str, pythonpath, sid) -> None:
    try:
        child = dw._spawn_appcontainer(
            worker_python, script, sid, pythonpath,
            {"FAUXCASA_DECODESVC_ARENA_BYTES": str(8 * 1024 * 1024)},
            2 * 1024 * 1024 * 1024)
    except Exception as e:
        print(f"VERDICT {label}: SPAWN-RAISED {repr(e)[:200]}", flush=True)
        return
    got = {}

    def _read():
        got["data"] = child.out_file.read(8)

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout=8.0)
    code = ctypes.wintypes.DWORD(0)
    dw.kernel32.GetExitCodeProcess(child.pi.hProcess, ctypes.byref(code))
    print(f"VERDICT {label}: read={got.get('data')!r} exit={code.value:#010x}",
          flush=True)
    child.close()


def main() -> None:
    worker_python, site = dw.resolve_worker_python()
    base_dir = os.path.dirname(worker_python)

    tmp_c = Path(tempfile.mkdtemp(prefix="fauxcasa-bisect-"))
    stub_c = tmp_c / "stub.py"
    stub_c.write_text(STUB)
    stub_d = APPDIR / "gha_bisect_stub_tmp.py"
    stub_d.write_text(STUB)

    sid = dw.create_or_derive_profile("fauxcasa.decode.bisect")
    for d in (base_dir, site, str(APPDIR), str(tmp_c)):
        dw.grant_read_execute(d, sid)
    print("winsta:", dw.grant_winsta_desktop(sid), flush=True)

    try:
        try_spawn("stub-Ctemp-nopath", worker_python, str(stub_c), None, sid)
        try_spawn("stub-Ctemp-Dsite", worker_python, str(stub_c), site, sid)
        try_spawn("stub-Drepo-nopath", worker_python, str(stub_d), None, sid)
        try_spawn("stub-Drepo-Dsite", worker_python, str(stub_d), site, sid)
        try_spawn("realworker-prod", worker_python,
                  str(APPDIR / "decodesvc_worker_win.py"), site, sid)
    finally:
        stub_d.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
