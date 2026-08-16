#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6"]
# ///
"""TEMPORARY (fauxcasa-i92.3): bisect which env-block difference kills the
sandboxed worker on the GHA windows-latest runner.

Evidence so far: the Stage A spike (which passes the FULL parent
environment through to the AppContainer child) is fully viable on the
runner, while production spawns (allowlist env, PATH="") die exit-1 with
zero pipe bytes before any Python code runs. Same base interpreter, same
lockdown attributes, grants confirmed via icacls. The env block is the
last functional delta.

Monkeypatches decodesvc_win._build_env_block per variant and attempts a
real WinSandboxWorker.spawn() for each, printing VERDICT lines. Delete
this file once the culprit variable is identified and fixed.
"""

import ctypes
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "apps" / "desktop-python"))

import decodesvc_win as dw  # noqa: E402

if sys.platform != "win32":
    print("windows only")
    sys.exit(0)


def _pack(env: dict) -> ctypes.Array:
    parts = [f"{k}={v}" for k, v in env.items()]
    return ctypes.create_unicode_buffer("\x00".join(parts) + "\x00\x00")


def _prod_env(pythonpath, extra_env) -> dict:
    env = {}
    for key in ("SystemRoot", "SystemDrive", "windir",
                "USERPROFILE", "LOCALAPPDATA", "APPDATA"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    env["PATH"] = ""
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    for k, v in extra_env.items():
        if k.startswith("FAUXCASA_DECODESVC_"):
            env[k] = v
    return env


def _full_env(pythonpath, extra_env) -> dict:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    for k, v in extra_env.items():
        if k.startswith("FAUXCASA_DECODESVC_"):
            env[k] = v
    return env


def _passthrough(*keys):
    return {k: os.environ[k] for k in keys if k in os.environ}


VARIANTS = [
    ("prod-allowlist", None),                    # expected FAIL on GHA
    ("full-os-environ", "FULL"),                 # expected PASS (spike parity)
    ("prod+PATH", lambda: _passthrough("PATH")),
    ("prod+TEMP-TMP", lambda: _passthrough("TEMP", "TMP")),
    ("prod+ComSpec-PATHEXT", lambda: _passthrough("ComSpec", "PATHEXT")),
    ("prod+ProgramData-ProgramFiles", lambda: _passthrough(
        "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
        "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432")),
    ("prod+PROCESSOR-etc", lambda: _passthrough(
        "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL",
        "PROCESSOR_REVISION", "NUMBER_OF_PROCESSORS", "OS", "COMPUTERNAME",
        "USERNAME", "USERDOMAIN", "ALLUSERSPROFILE", "PUBLIC", "HOMEDRIVE",
        "HOMEPATH")),
    ("prod-sorted", "SORTED"),
]


def main() -> None:
    for name, extra in VARIANTS:
        def build(pythonpath, extra_env, _name=name, _extra=extra):
            if _extra == "FULL":
                env = _full_env(pythonpath, extra_env)
            else:
                env = _prod_env(pythonpath, extra_env)
                if callable(_extra):
                    env.update(_extra())
            if _extra == "SORTED":
                env = dict(sorted(env.items(), key=lambda kv: kv[0].upper()))
            return _pack(env)

        dw._build_env_block = build
        try:
            w = dw.WinSandboxWorker(arena_bytes=8 * 1024 * 1024, probe=True)
            w.spawn()
            print(f"VERDICT {name}: SPAWN-OK", flush=True)
            w.close()
        except Exception as e:
            print(f"VERDICT {name}: FAIL {repr(e)[:300]}", flush=True)


if __name__ == "__main__":
    main()
