#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""AppContainer + job-object spawn spike (fauxcasa-i92.3, Stage A de-risk).

Purpose: prove on the actual Windows dev box that a decode worker can be
launched inside a zero-capability **AppContainer** nested in a **job
object**, after an ACL grant, speak on its pipes, and (attempt) import
PySide6.QtGui offscreen and decode a PNG -- the load-bearing assumption of
`docs/design/decode-service.md` sec 4 (Windows column). If AppContainer is a
hard wall, this script proves it with exit-code evidence instead of guessing.

This is stdlib-only ctypes (no pywin32). The *broker* (this script) runs on
any interpreter; the *worker* child must be an interpreter that can import
PySide6, so we point it at a uv-managed venv that has PySide6 (resolved or
provisioned below), and we spawn that python.exe DIRECTLY -- never via `uv`.

Measures (JSON emitted at the end, like decode-worker-spawn-spike.py):
  - plain spawn-to-ready            (N=15)
  - job-only spawn-to-ready         (N=15)
  - appcontainer+job spawn-to-ready (N=15)
  - QtGui import cost inside the container (N=5)
  - a hostile-attempt report from inside the container (file/socket/subproc)
  - a KILL_ON_JOB_CLOSE lifetime check.

Run (Git Bash preferred; PowerShell mangles some argv):
    uv run docs/research/spikes/appcontainer-spawn-spike.py

Env knobs:
    FAUXCASA_WORKER_PYTHON  -- path to a python.exe with PySide6 (skip auto
                               provisioning).
    FAUXCASA_SPIKE_KEEPENV  -- if set, do not delete the provisioned venv.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes

if sys.platform != "win32":
    print(json.dumps({"skipped": "windows-only spike", "platform": sys.platform}))
    sys.exit(0)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
N_SPAWN = 15
N_QT = 5

PROFILE_NAME = "fauxcasa.decode.spike"

# CreateProcess flags
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400

STARTF_USESTDHANDLES = 0x00000100

HANDLE_FLAG_INHERIT = 0x00000001

# ProcThreadAttribute values
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_MITIGATION_POLICY = 0x00020007
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
PROC_THREAD_ATTRIBUTE_CHILD_PROCESS_POLICY = 0x0002000E

PROCESS_CREATION_CHILD_PROCESS_RESTRICTED = 0x01

# Mitigation policy bits (winbase.h): 2-bit fields, ALWAYS_ON == 1
MITIGATION_DEP_ENABLE = 0x01
MITIGATION_FORCE_RELOCATE_ALWAYS_ON = 0x01 << 8
MITIGATION_HEAP_TERMINATE_ALWAYS_ON = 0x01 << 12
MITIGATION_FULL = (
    MITIGATION_DEP_ENABLE
    | MITIGATION_FORCE_RELOCATE_ALWAYS_ON
    | MITIGATION_HEAP_TERMINATE_ALWAYS_ON
)

# Job object
JobObjectBasicUIRestrictions = 4
JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_UILIMIT_ALL = 0x000000FF

# ACLs
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
GENERIC_READ = 0x80000000
GENERIC_EXECUTE = 0x20000000
GRANT_ACCESS = 1
REVOKE_ACCESS = 4
NO_MULTIPLE_TRUSTEE = 0
TRUSTEE_IS_SID = 0
TRUSTEE_IS_GROUP = 2
OBJECT_INHERIT_ACE = 0x1
CONTAINER_INHERIT_ACE = 0x2
SUB_CONTAINERS_AND_OBJECTS_INHERIT = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE

ERROR_ALREADY_EXISTS = 183
HRESULT_ALREADY_EXISTS = 0x800700B1  # HRESULT_FROM_WIN32(183)

INFINITE = 0xFFFFFFFF
WAIT_TIMEOUT = 0x00000102
STILL_ACTIVE = 259

TOKEN_QUERY = 0x0008
TokenIsAppContainer = 29

# --------------------------------------------------------------------------
# Win32 handles
# --------------------------------------------------------------------------
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
userenv = ctypes.WinDLL("userenv", use_last_error=True)

LPVOID = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
ULONG_PTR = ctypes.c_size_t


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", LPVOID),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", LPVOID),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", LPVOID),
        ("Capabilities", LPVOID),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in
                ("ReadOperationCount", "WriteOperationCount",
                 "OtherOperationCount", "ReadTransferCount",
                 "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]


class TRUSTEE_W(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", LPVOID),
        ("MultipleTrusteeOperation", ctypes.c_int),
        ("TrusteeForm", ctypes.c_int),
        ("TrusteeType", ctypes.c_int),
        ("ptstrName", LPVOID),
    ]


class EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", ctypes.c_int),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", TRUSTEE_W),
    ]


# --------------------------------------------------------------------------
# ctypes prototypes
# --------------------------------------------------------------------------
kernel32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE),
                                ctypes.POINTER(wintypes.HANDLE),
                                ctypes.POINTER(SECURITY_ATTRIBUTES),
                                wintypes.DWORD]
kernel32.CreatePipe.restype = wintypes.BOOL

kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
kernel32.SetHandleInformation.restype = wintypes.BOOL

kernel32.InitializeProcThreadAttributeList.argtypes = [
    LPVOID, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SIZE_T)]
kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL

kernel32.UpdateProcThreadAttribute.argtypes = [
    LPVOID, wintypes.DWORD, ctypes.c_size_t, LPVOID, SIZE_T,
    LPVOID, ctypes.POINTER(SIZE_T)]
kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL

kernel32.DeleteProcThreadAttributeList.argtypes = [LPVOID]
kernel32.DeleteProcThreadAttributeList.restype = None

kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(SECURITY_ATTRIBUTES),
    ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.BOOL, wintypes.DWORD,
    LPVOID, wintypes.LPCWSTR, LPVOID, ctypes.POINTER(PROCESS_INFORMATION)]
kernel32.CreateProcessW.restype = wintypes.BOOL

kernel32.CreateJobObjectW.argtypes = [ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE

kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, LPVOID, wintypes.DWORD]
kernel32.SetInformationJobObject.restype = wintypes.BOOL

kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
kernel32.ResumeThread.restype = wintypes.DWORD

kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateProcess.restype = wintypes.BOOL

kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD

kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

advapi32.GetNamedSecurityInfoW.argtypes = [
    wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
    ctypes.POINTER(LPVOID), ctypes.POINTER(LPVOID),
    ctypes.POINTER(LPVOID), ctypes.POINTER(LPVOID),
    ctypes.POINTER(LPVOID)]
advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD

advapi32.SetEntriesInAclW.argtypes = [
    wintypes.ULONG, ctypes.POINTER(EXPLICIT_ACCESS_W), LPVOID,
    ctypes.POINTER(LPVOID)]
advapi32.SetEntriesInAclW.restype = wintypes.DWORD

advapi32.SetNamedSecurityInfoW.argtypes = [
    wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
    LPVOID, LPVOID, LPVOID, LPVOID]
advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD

advapi32.ConvertSidToStringSidW.argtypes = [LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

advapi32.FreeSid.argtypes = [LPVOID]
advapi32.FreeSid.restype = LPVOID

kernel32.LocalFree.argtypes = [LPVOID]
kernel32.LocalFree.restype = LPVOID

userenv.CreateAppContainerProfile.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
    LPVOID, wintypes.DWORD, ctypes.POINTER(LPVOID)]
userenv.CreateAppContainerProfile.restype = ctypes.c_long  # HRESULT

userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
    wintypes.LPCWSTR, ctypes.POINTER(LPVOID)]
userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long

userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
userenv.DeleteAppContainerProfile.restype = ctypes.c_long


# --------------------------------------------------------------------------
# AppContainer profile
# --------------------------------------------------------------------------
def create_or_derive_profile(name: str) -> LPVOID:
    """Idempotent: create profile, else derive its SID. Returns PSID."""
    sid = LPVOID()
    hr = userenv.CreateAppContainerProfile(
        name, name, "fauxcasa decode spike sandbox",
        None, 0, ctypes.byref(sid))
    hr &= 0xFFFFFFFF
    if hr == 0:
        return sid
    if hr == HRESULT_ALREADY_EXISTS:
        hr2 = userenv.DeriveAppContainerSidFromAppContainerName(
            name, ctypes.byref(sid))
        hr2 &= 0xFFFFFFFF
        if hr2 != 0:
            raise OSError(f"DeriveAppContainerSid failed HRESULT=0x{hr2:08X}")
        return sid
    raise OSError(f"CreateAppContainerProfile failed HRESULT=0x{hr:08X}")


def sid_to_string(sid: LPVOID) -> str:
    ptr = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(ptr)):
        return "<unknown>"
    s = ptr.value
    kernel32.LocalFree(ptr)
    return s


# --------------------------------------------------------------------------
# ACL grant / revoke
# --------------------------------------------------------------------------
def _set_file_dacl(path: str, sid: LPVOID, mode: int) -> None:
    p_owner = LPVOID()
    p_group = LPVOID()
    p_dacl = LPVOID()
    p_sacl = LPVOID()
    p_sd = LPVOID()
    err = advapi32.GetNamedSecurityInfoW(
        path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
        ctypes.byref(p_owner), ctypes.byref(p_group),
        ctypes.byref(p_dacl), ctypes.byref(p_sacl), ctypes.byref(p_sd))
    if err != 0:
        raise OSError(f"GetNamedSecurityInfoW({path}) err={err}")
    try:
        ea = EXPLICIT_ACCESS_W()
        ea.grfAccessPermissions = GENERIC_READ | GENERIC_EXECUTE
        ea.grfAccessMode = mode
        ea.grfInheritance = SUB_CONTAINERS_AND_OBJECTS_INHERIT
        ea.Trustee.pMultipleTrustee = None
        ea.Trustee.MultipleTrusteeOperation = NO_MULTIPLE_TRUSTEE
        ea.Trustee.TrusteeForm = TRUSTEE_IS_SID
        ea.Trustee.TrusteeType = TRUSTEE_IS_GROUP
        ea.Trustee.ptstrName = sid
        new_dacl = LPVOID()
        err = advapi32.SetEntriesInAclW(1, ctypes.byref(ea), p_dacl,
                                        ctypes.byref(new_dacl))
        if err != 0:
            raise OSError(f"SetEntriesInAclW({path}) err={err}")
        try:
            err = advapi32.SetNamedSecurityInfoW(
                path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
                None, None, new_dacl, None)
            if err != 0:
                raise OSError(f"SetNamedSecurityInfoW({path}) err={err}")
        finally:
            if new_dacl:
                kernel32.LocalFree(new_dacl)
    finally:
        if p_sd:
            kernel32.LocalFree(p_sd)


def grant_read_execute(path: str, sid: LPVOID) -> None:
    _set_file_dacl(path, sid, GRANT_ACCESS)


def revoke(path: str, sid: LPVOID) -> None:
    try:
        _set_file_dacl(path, sid, REVOKE_ACCESS)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Job object
# --------------------------------------------------------------------------
def make_job(mem_limit_bytes: int = 2 * 1024 * 1024 * 1024) -> wintypes.HANDLE:
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_PROCESS_MEMORY)
    info.BasicLimitInformation.ActiveProcessLimit = 1
    info.ProcessMemoryLimit = mem_limit_bytes
    if not kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    ui = JOBOBJECT_BASIC_UI_RESTRICTIONS()
    ui.UIRestrictionsClass = JOB_OBJECT_UILIMIT_ALL
    if not kernel32.SetInformationJobObject(
            job, JobObjectBasicUIRestrictions,
            ctypes.byref(ui), ctypes.sizeof(ui)):
        raise ctypes.WinError(ctypes.get_last_error())
    return job


# --------------------------------------------------------------------------
# Pipes + environment
# --------------------------------------------------------------------------
def make_pipe_pair():
    """Returns (child_stdin_read, broker_stdin_write,
                broker_stdout_read, child_stdout_write). Child ends are
    inheritable; broker ends are not."""
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(sa)
    sa.lpSecurityDescriptor = None
    sa.bInheritHandle = True

    stdin_r = wintypes.HANDLE()
    stdin_w = wintypes.HANDLE()
    if not kernel32.CreatePipe(ctypes.byref(stdin_r), ctypes.byref(stdin_w),
                               ctypes.byref(sa), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    stdout_r = wintypes.HANDLE()
    stdout_w = wintypes.HANDLE()
    if not kernel32.CreatePipe(ctypes.byref(stdout_r), ctypes.byref(stdout_w),
                               ctypes.byref(sa), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    # Broker ends must NOT be inherited by the child.
    kernel32.SetHandleInformation(stdin_w, HANDLE_FLAG_INHERIT, 0)
    kernel32.SetHandleInformation(stdout_r, HANDLE_FLAG_INHERIT, 0)
    return stdin_r, stdin_w, stdout_r, stdout_w


def build_env_block() -> ctypes.Array:
    env = {k: v for k, v in os.environ.items()}
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    parts = [f"{k}={v}" for k, v in env.items()]
    block = "\x00".join(parts) + "\x00\x00"
    return ctypes.create_unicode_buffer(block)


# --------------------------------------------------------------------------
# Spawn
# --------------------------------------------------------------------------
class Child:
    def __init__(self, pi, stdin_w, stdout_r, job=None):
        self.pi = pi
        self.stdin_w = stdin_w
        self.stdout_r = stdout_r
        self.job = job
        self._out_fd = None
        self._out_file = None
        self._in_fd = None
        self._in_file = None
        self._q: "queue.Queue[str|None]" = queue.Queue()
        self._noise: list[str] = []
        self._reader = None

    def start_reader(self):
        import msvcrt
        self._out_fd = msvcrt.open_osfhandle(int(self.stdout_r.value), os.O_RDONLY)
        self._out_file = os.fdopen(self._out_fd, "rb", buffering=0)
        self.stdout_r = None  # ownership moved to fd
        self._in_fd = msvcrt.open_osfhandle(int(self.stdin_w.value), 0)
        self._in_file = os.fdopen(self._in_fd, "wb", buffering=0)
        self.stdin_w = None

        def _pump():
            try:
                buf = b""
                while True:
                    chunk = self._out_file.read(1)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self._q.put(line.decode("utf-8", "replace").rstrip("\r"))
            except OSError:
                pass
            finally:
                self._q.put(None)

        self._reader = threading.Thread(target=_pump, daemon=True)
        self._reader.start()

    def send(self, text: str):
        self._in_file.write((text + "\n").encode("utf-8"))
        self._in_file.flush()

    def read_tagged(self, tag: str, timeout: float):
        """Read lines until one starts with tag; collect others as noise."""
        deadline = time.perf_counter() + timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            try:
                line = self._q.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:  # EOF
                return None
            if line.startswith(tag):
                return line[len(tag):].strip()
            self._noise.append(line)

    def exit_code(self):
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(self.pi.hProcess, ctypes.byref(code))
        return code.value

    def wait(self, timeout_ms: int):
        return kernel32.WaitForSingleObject(self.pi.hProcess, timeout_ms)

    def close(self):
        try:
            if self._in_file:
                self._in_file.close()
        except OSError:
            pass
        try:
            if self._out_file:
                self._out_file.close()
        except OSError:
            pass
        if self.pi and self.pi.hProcess:
            kernel32.TerminateProcess(self.pi.hProcess, 1)
            kernel32.WaitForSingleObject(self.pi.hProcess, 2000)
            kernel32.CloseHandle(self.pi.hProcess)
            kernel32.CloseHandle(self.pi.hThread)
            self.pi = None
        if self.job:
            kernel32.CloseHandle(self.job)
            self.job = None


def spawn(kind: str, worker_python: str, worker_script: str, mode: str,
          sid: LPVOID | None, mem_limit=2 * 1024 * 1024 * 1024) -> Child:
    """kind in {'plain','job','appc'}."""
    stdin_r, stdin_w, stdout_r, stdout_w = make_pipe_pair()
    env_block = build_env_block()
    cmdline = f'"{worker_python}" "{worker_script}" {mode}'
    cmd_buf = ctypes.create_unicode_buffer(cmdline)

    pi = PROCESS_INFORMATION()
    job = None
    flags = CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT

    # keep-alive refs for attribute list values
    _keep = []

    if kind == "appc":
        assert sid is not None
        job = make_job(mem_limit)
        # attribute list with 4 attributes
        size = SIZE_T(0)
        kernel32.InitializeProcThreadAttributeList(None, 4, 0, ctypes.byref(size))
        attr_buf = (ctypes.c_byte * size.value)()
        attr_list = ctypes.cast(attr_buf, LPVOID)
        if not kernel32.InitializeProcThreadAttributeList(
                attr_list, 4, 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())

        sec_cap = SECURITY_CAPABILITIES()
        sec_cap.AppContainerSid = sid
        sec_cap.Capabilities = None
        sec_cap.CapabilityCount = 0
        sec_cap.Reserved = 0
        _keep.append(sec_cap)
        if not kernel32.UpdateProcThreadAttribute(
                attr_list, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                ctypes.byref(sec_cap), ctypes.sizeof(sec_cap), None, None):
            raise ctypes.WinError(ctypes.get_last_error())

        child_policy = ctypes.c_uint32(PROCESS_CREATION_CHILD_PROCESS_RESTRICTED)
        _keep.append(child_policy)
        if not kernel32.UpdateProcThreadAttribute(
                attr_list, 0, PROC_THREAD_ATTRIBUTE_CHILD_PROCESS_POLICY,
                ctypes.byref(child_policy), ctypes.sizeof(child_policy), None, None):
            raise ctypes.WinError(ctypes.get_last_error())

        handle_arr = (wintypes.HANDLE * 2)(stdin_r, stdout_w)
        _keep.append(handle_arr)
        if not kernel32.UpdateProcThreadAttribute(
                attr_list, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                handle_arr, ctypes.sizeof(handle_arr), None, None):
            raise ctypes.WinError(ctypes.get_last_error())

        mitigation = ctypes.c_uint64(MITIGATION_FULL)
        _keep.append(mitigation)
        if not kernel32.UpdateProcThreadAttribute(
                attr_list, 0, PROC_THREAD_ATTRIBUTE_MITIGATION_POLICY,
                ctypes.byref(mitigation), ctypes.sizeof(mitigation), None, None):
            raise ctypes.WinError(ctypes.get_last_error())

        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        si.StartupInfo.hStdInput = stdin_r
        si.StartupInfo.hStdOutput = stdout_w
        si.StartupInfo.hStdError = stdout_w
        si.lpAttributeList = attr_list
        flags |= EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED
        ok = kernel32.CreateProcessW(
            None, cmd_buf, None, None, True, flags,
            env_block, None, ctypes.byref(si.StartupInfo), ctypes.byref(pi))
        err = ctypes.get_last_error()
        kernel32.DeleteProcThreadAttributeList(attr_list)
    else:
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(STARTUPINFOW)
        si.dwFlags = STARTF_USESTDHANDLES
        si.hStdInput = stdin_r
        si.hStdOutput = stdout_w
        si.hStdError = stdout_w
        if kind == "job":
            job = make_job(mem_limit)
            flags |= CREATE_SUSPENDED
        ok = kernel32.CreateProcessW(
            None, cmd_buf, None, None, True, flags,
            env_block, None, ctypes.byref(si), ctypes.byref(pi))
        err = ctypes.get_last_error()

    # close child-end handles in broker regardless
    kernel32.CloseHandle(stdin_r)
    kernel32.CloseHandle(stdout_w)

    if not ok:
        kernel32.CloseHandle(stdin_w)
        kernel32.CloseHandle(stdout_r)
        if job:
            kernel32.CloseHandle(job)
        raise ctypes.WinError(err)

    if job:
        if not kernel32.AssignProcessToJobObject(job, pi.hProcess):
            raise ctypes.WinError(ctypes.get_last_error())
        kernel32.ResumeThread(pi.hThread)

    child = Child(pi, stdin_w, stdout_r, job)
    child.start_reader()
    return child


# --------------------------------------------------------------------------
# Worker script
# --------------------------------------------------------------------------
WORKER_SRC = r'''
import sys, os, json, time, base64, ctypes
from ctypes import wintypes

def emit(tag, obj):
    sys.stdout.write(tag + " " + json.dumps(obj) + "\n")
    sys.stdout.flush()

def is_appcontainer():
    try:
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        tok = wintypes.HANDLE()
        adv.OpenProcessToken(k.GetCurrentProcess(), 0x0008, ctypes.byref(tok))
        val = wintypes.DWORD(0)
        rl = wintypes.DWORD(0)
        adv.GetTokenInformation(tok, 29, ctypes.byref(val), 4, ctypes.byref(rl))
        return bool(val.value)
    except Exception as e:
        return "err:" + repr(e)

# 1x1 red PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "ready"
    emit("SPIKE_READY", {"pid": os.getpid(), "is_appcontainer": is_appcontainer(),
                         "mode": mode})
    if mode == "ready":
        return
    # attempts mode: wait for a command
    line = sys.stdin.readline().strip()
    if line != "ATTEMPTS":
        return
    result = {}

    # (a) QtGui import + PNG decode
    a = {}
    try:
        t0 = time.perf_counter()
        from PySide6.QtGui import QImage
        a["import_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        img = QImage()
        ok = img.loadFromData(PNG, "PNG")
        a["decode_ok"] = bool(ok) and not img.isNull()
        a["size"] = [img.width(), img.height()]
        a["error"] = None
    except Exception as e:
        a["import_ms"] = None
        a["decode_ok"] = False
        a["error"] = repr(e)
    result["qtgui"] = a

    # (b) open a file outside grant
    b = {}
    try:
        with open(r"C:\Windows\win.ini", "rb") as fh:
            fh.read(16)
        b["allowed"] = True
        b["error"] = None
    except Exception as e:
        b["allowed"] = False
        b["error"] = repr(e)
    result["win_ini"] = b

    # (c) socket connect
    c = {}
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", 445))
        c["allowed"] = True
        c["error"] = None
        s.close()
    except Exception as e:
        c["allowed"] = False
        c["error"] = repr(e)
    result["socket"] = c

    # (d) subprocess spawn
    d = {}
    try:
        import subprocess
        p = subprocess.run(["cmd", "/c", "exit", "0"], capture_output=True, timeout=5)
        d["allowed"] = True
        d["rc"] = p.returncode
        d["error"] = None
    except Exception as e:
        d["allowed"] = False
        d["error"] = repr(e)
    result["subprocess"] = d

    emit("SPIKE_RESULT", result)

if __name__ == "__main__":
    main()
'''


# --------------------------------------------------------------------------
# Worker python resolution
# --------------------------------------------------------------------------
def resolve_worker_python(worktree: str) -> tuple[str, dict]:
    """Return (python_exe, meta) for an interpreter that can import PySide6.
    Prefers FAUXCASA_WORKER_PYTHON; else provisions a venv via uv."""
    meta = {}
    env_py = os.environ.get("FAUXCASA_WORKER_PYTHON")
    if env_py and os.path.exists(env_py):
        meta["source"] = "env"
        return env_py, meta

    venv_dir = os.path.join(
        tempfile.gettempdir(), "fauxcasa-spike-venv")
    py = os.path.join(venv_dir, "Scripts", "python.exe")
    if not os.path.exists(py):
        meta["source"] = "provisioned"
        subprocess.run(["uv", "venv", venv_dir], cwd=worktree, check=True,
                       capture_output=True)
        subprocess.run(["uv", "pip", "install", "--python", py, "PySide6"],
                       cwd=worktree, check=True, capture_output=True)
    else:
        meta["source"] = "cached-venv"
    meta["venv_dir"] = venv_dir
    return py, meta


def probe_worker_dirs(worker_python: str) -> dict:
    """Ask the worker interpreter for base_prefix and PySide6 dir."""
    code = ("import sys, json;"
            "d={'base_prefix': sys.base_prefix, 'prefix': sys.prefix,"
            "'executable': sys.executable};"
            "\ntry:\n import PySide6, os;"
            " d['pyside6']=os.path.dirname(PySide6.__file__);"
            " d['site']=os.path.dirname(d['pyside6'])\nexcept Exception as e:"
            " d['pyside6_err']=repr(e)\nprint(json.dumps(d))")
    out = subprocess.run([worker_python, "-c", code], capture_output=True,
                         text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------
def _stats(samples: list[float]) -> dict:
    return {
        "min_ms": round(min(samples) * 1000, 3),
        "median_ms": round(statistics.median(samples) * 1000, 3),
        "max_ms": round(max(samples) * 1000, 3),
        "n": len(samples),
    }


def measure_spawn_to_ready(kind, worker_python, worker_script, sid, n, ready_timeout=15.0):
    samples = []
    failures = []
    for _ in range(n):
        t0 = time.perf_counter()
        child = spawn(kind, worker_python, worker_script, "ready", sid)
        ready = child.read_tagged("SPIKE_READY", ready_timeout)
        elapsed = time.perf_counter() - t0
        if ready is None:
            code = child.exit_code()
            failures.append({
                "exit_code_hex": f"0x{code & 0xFFFFFFFF:08X}",
                "noise": child._noise[:20],
            })
            child.close()
            break  # a failure invalidates the run; report evidence
        samples.append(elapsed)
        child.close()
    if failures:
        return {"failed": True, "detail": failures, "n_ok": len(samples)}
    return _stats(samples)


def main():
    worktree = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    result = {
        "platform": sys.platform,
        "broker_python": sys.version.split()[0],
        "profile_name": PROFILE_NAME,
    }

    # worker script into its own dir (granted, revocable)
    work_dir = os.path.join(tempfile.gettempdir(), "fauxcasa-spike-worker")
    os.makedirs(work_dir, exist_ok=True)
    worker_script = os.path.join(work_dir, "spike_worker.py")
    with open(worker_script, "w", encoding="utf-8") as fh:
        fh.write(WORKER_SRC)

    worker_python, wmeta = resolve_worker_python(worktree)
    result["worker_python"] = worker_python
    result["worker_python_meta"] = wmeta
    dirs = probe_worker_dirs(worker_python)
    result["worker_dirs"] = dirs

    # AppContainer profile
    sid = create_or_derive_profile(PROFILE_NAME)
    result["appcontainer_sid"] = sid_to_string(sid)

    # Grants: every dir the container may need to read+execute.
    grant_targets = []
    for key in ("base_prefix", "prefix", "pyside6", "site"):
        d = dirs.get(key)
        if d and d not in grant_targets:
            grant_targets.append(d)
    # venv Scripts dir (holds python.exe launcher + pyvenv.cfg parent)
    venv_scripts = os.path.dirname(worker_python)
    venv_root = os.path.dirname(venv_scripts)
    for d in (venv_root, venv_scripts, work_dir):
        if d and d not in grant_targets:
            grant_targets.append(d)

    granted = []
    grant_errors = {}
    for d in grant_targets:
        try:
            grant_read_execute(d, sid)
            granted.append(d)
        except OSError as e:
            grant_errors[d] = str(e)
    result["grants_applied"] = granted
    if grant_errors:
        result["grant_errors"] = grant_errors

    try:
        # 5. spawn-to-ready measurements
        result["spawn"] = {
            "plain": measure_spawn_to_ready("plain", worker_python, worker_script, None, N_SPAWN),
            "job_only": measure_spawn_to_ready("job", worker_python, worker_script, None, N_SPAWN),
            "appcontainer_job": measure_spawn_to_ready("appc", worker_python, worker_script, sid, N_SPAWN),
        }

        appc_ok = not result["spawn"]["appcontainer_job"].get("failed")

        # 4 + 5: QtGui import cost + hostile attempts inside container
        if appc_ok:
            qt_samples = []
            attempts = None
            for i in range(N_QT):
                child = spawn("appc", worker_python, worker_script, "attempts", sid)
                ready = child.read_tagged("SPIKE_READY", 15.0)
                if ready is None:
                    child.close()
                    continue
                child.send("ATTEMPTS")
                res = child.read_tagged("SPIKE_RESULT", 60.0)
                if res is not None:
                    try:
                        parsed = json.loads(res)
                        if attempts is None:
                            attempts = parsed
                        imp = parsed.get("qtgui", {}).get("import_ms")
                        if imp is not None:
                            qt_samples.append(imp / 1000.0)
                    except json.JSONDecodeError:
                        pass
                child.close()
            result["qtgui_import"] = _stats(qt_samples) if qt_samples else {"failed": True}
            result["attempts"] = attempts
        else:
            result["qtgui_import"] = {"skipped": "appcontainer spawn failed"}
            result["attempts"] = {"skipped": "appcontainer spawn failed"}

        # 6. KILL_ON_JOB_CLOSE lifetime check
        if appc_ok:
            child = spawn("appc", worker_python, worker_script, "ready", sid)
            ready = child.read_tagged("SPIKE_READY", 15.0)
            proc = child.pi.hProcess
            job = child.job
            child.pi = None  # detach so close() doesn't terminate the process
            child.job = None
            # close only the job handle -> KILL_ON_JOB_CLOSE must kill child
            kernel32.CloseHandle(job)
            waited = kernel32.WaitForSingleObject(proc, 3000)
            died = waited == 0  # WAIT_OBJECT_0
            code = wintypes.DWORD()
            kernel32.GetExitCodeProcess(proc, ctypes.byref(code))
            result["kill_on_job_close"] = {
                "child_died": bool(died),
                "exit_code_hex": f"0x{code.value & 0xFFFFFFFF:08X}",
            }
            kernel32.CloseHandle(proc)
            try:
                child._out_file.close()
                child._in_file.close()
            except OSError:
                pass
        else:
            result["kill_on_job_close"] = {"skipped": "appcontainer spawn failed"}

        result["verdict"] = "appcontainer_viable" if appc_ok else "appcontainer_hard_wall"
    finally:
        # cleanup grants + profile unless asked to keep
        if not os.environ.get("FAUXCASA_SPIKE_KEEPENV"):
            for d in granted:
                revoke(d, sid)
        advapi32.FreeSid(sid)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
