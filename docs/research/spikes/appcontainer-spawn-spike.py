#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
r"""AppContainer + job-object spawn spike (fauxcasa-i92.3, Stage A de-risk).

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
    FAUXCASA_SPIKE_KEEPENV  -- if set, do not revoke ACLs / delete artifacts.

======================================================================
NOTES / gotchas found (2026-08-12 run, Windows 11 Pro, Python 3.13.1) --
these MUST inform Stage B (apps/desktop-python/decodesvc_win.py):
======================================================================

VERDICT: AppContainer is VIABLE. A zero-capability AppContainer child,
nested in a KILL_ON_JOB_CLOSE job (ActiveProcessLimit=1, 2 GiB, UILIMIT_ALL),
with full mitigation policy AND child-process-restricted policy, starts in
~31 ms, imports PySide6.QtGui offscreen (~72 ms) and decodes a PNG. Token
IsAppContainer == true is confirmed from inside.

1. uv venv python.exe is a TRAMPOLINE. The venv's Scripts\python.exe
   re-launches the base interpreter as a CHILD process. Under
   ActiveProcessLimit=1 that child hits ERROR_NOT_ENOUGH_QUOTA; under
   child-process-restricted it is blocked -> the worker never starts. FIX:
   spawn the *base* interpreter (sys.base_prefix\python.exe) directly and put
   PySide6 on PYTHONPATH (the venv site-packages). Never spawn via uv or the
   venv python for a sandboxed worker.

2. child-process policy vs the console. PROC_THREAD_ATTRIBUTE_CHILD_PROCESS_
   POLICY = RESTRICTED + CREATE_NO_WINDOW kills the child at loader init with
   STATUS_DLL_INIT_FAILED (0xC0000142): CREATE_NO_WINDOW still spins up a
   conhost.exe, which is a child process the policy forbids. FIX: launch the
   worker with DETACHED_PROCESS (no console at all) instead of
   CREATE_NO_WINDOW. Then child-process policy can stay ON (belt with the
   job's ActiveProcessLimit=1 braces). Verified in appc_diagnostics below.

3. Window station + desktop grant is MANDATORY. Without it the AppContainer
   child also dies 0xC0000142 (user32 init cannot open the station/desktop).
   Grant the container SID access to GetProcessWindowStation() and
   GetThreadDesktop() -- and use the USER-object APIs
   GetUserObjectSecurity/SetUserObjectSecurity, NOT advapi32
   GetSecurityInfo(SE_WINDOW_OBJECT) (that route access-violates via ctypes).

4. ACL grants that proved necessary (read+execute, inheritable, for the
   container SID): the base interpreter runtime dir (uv keeps it under
   %APPDATA%\Roaming\uv\python\... -- AppContainer cannot read it by default),
   the venv site-packages (PySide6 import root), the PySide6 package dir, and
   the worker-script dir. Grant is idempotent; revoke helper included.

5. AppContainer is NOT a filesystem jail for world/app-package-readable
   files. The worker READ C:\Windows\win.ini fine (system files carry an ALL
   APPLICATION PACKAGES ACE). Real containment held where it matters: a
   user-profile file -> PermissionError; a %TEMP% write -> PermissionError;
   DNS getaddrinfo -> gaierror 11001; loopback connect -> blocked; subprocess
   -> "process creation has been blocked" (err 367). Stage B's probe corpus
   must test USER files, not system files, for the filesystem gate.

6. Sequencing footguns proven here: (a) resume the main thread only AFTER
   AssignProcessToJobObject (never a moment outside the job); (b) when reading
   the child's pipe from a background thread, TerminateProcess FIRST on
   teardown, else close() deadlocks in ReadFile on a still-open pipe;
   (c) ERROR_ALREADY_EXISTS from CreateAppContainerProfile is HRESULT
   0x800700B7 (183 == 0xB7) -> derive the SID instead.

7. Numbers (this box): plain spawn-to-ready ~37 ms, job-only ~36 ms,
   AppContainer+job ~31 ms (faster: DETACHED_PROCESS skips the conhost that
   CREATE_NO_WINDOW spawns). QtGui import inside the container ~72 ms. These
   are the crash-refill latencies design sec 6 asked for.
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
HRESULT_ALREADY_EXISTS = 0x800700B7  # HRESULT_FROM_WIN32(183); 183 == 0xB7

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
user32 = ctypes.WinDLL("user32", use_last_error=True)

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

# Window stations and desktops are USER objects -- they must be secured with
# user32 GetUserObjectSecurity/SetUserObjectSecurity, NOT advapi32
# GetSecurityInfo(SE_WINDOW_OBJECT) (that route access-violates via ctypes).
user32.GetProcessWindowStation.restype = wintypes.HANDLE
user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
user32.GetThreadDesktop.restype = wintypes.HANDLE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

user32.GetUserObjectSecurity.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), LPVOID,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
user32.GetUserObjectSecurity.restype = wintypes.BOOL

user32.SetUserObjectSecurity.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), LPVOID]
user32.SetUserObjectSecurity.restype = wintypes.BOOL

advapi32.GetSecurityDescriptorDacl.argtypes = [
    LPVOID, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(LPVOID),
    ctypes.POINTER(wintypes.BOOL)]
advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL

advapi32.InitializeSecurityDescriptor.argtypes = [LPVOID, wintypes.DWORD]
advapi32.InitializeSecurityDescriptor.restype = wintypes.BOOL

advapi32.SetSecurityDescriptorDacl.argtypes = [
    LPVOID, wintypes.BOOL, LPVOID, wintypes.BOOL]
advapi32.SetSecurityDescriptorDacl.restype = wintypes.BOOL

GENERIC_ALL = 0x10000000
NO_INHERITANCE = 0x0
SECURITY_DESCRIPTOR_REVISION = 1
ERROR_INSUFFICIENT_BUFFER = 122


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


def _grant_user_object(handle, sid: LPVOID, inherit: int) -> None:
    """Merge a GRANT (GENERIC_ALL) ACE for `sid` into a USER object's DACL
    (window station / desktop) via Get/SetUserObjectSecurity."""
    si = wintypes.DWORD(DACL_SECURITY_INFORMATION)
    needed = wintypes.DWORD(0)
    # First call: discover required buffer length.
    ok = user32.GetUserObjectSecurity(handle, ctypes.byref(si), None, 0,
                                      ctypes.byref(needed))
    err = ctypes.get_last_error()
    if ok or err != ERROR_INSUFFICIENT_BUFFER:
        raise OSError(f"GetUserObjectSecurity(size) ok={ok} err={err}")
    buf = (ctypes.c_byte * needed.value)()
    if not user32.GetUserObjectSecurity(handle, ctypes.byref(si), buf,
                                        needed.value, ctypes.byref(needed)):
        raise OSError(f"GetUserObjectSecurity err={ctypes.get_last_error()}")

    present = wintypes.BOOL(0)
    defaulted = wintypes.BOOL(0)
    old_dacl = LPVOID()
    if not advapi32.GetSecurityDescriptorDacl(
            ctypes.cast(buf, LPVOID), ctypes.byref(present),
            ctypes.byref(old_dacl), ctypes.byref(defaulted)):
        raise OSError(f"GetSecurityDescriptorDacl err={ctypes.get_last_error()}")

    ea = EXPLICIT_ACCESS_W()
    ea.grfAccessPermissions = GENERIC_ALL
    ea.grfAccessMode = GRANT_ACCESS
    ea.grfInheritance = inherit
    ea.Trustee.pMultipleTrustee = None
    ea.Trustee.MultipleTrusteeOperation = NO_MULTIPLE_TRUSTEE
    ea.Trustee.TrusteeForm = TRUSTEE_IS_SID
    ea.Trustee.TrusteeType = TRUSTEE_IS_GROUP
    ea.Trustee.ptstrName = sid
    new_dacl = LPVOID()
    rc = advapi32.SetEntriesInAclW(
        1, ctypes.byref(ea),
        old_dacl if present.value else None, ctypes.byref(new_dacl))
    if rc != 0:
        raise OSError(f"SetEntriesInAclW(userobj) err={rc}")
    try:
        new_sd = (ctypes.c_byte * 64)()  # >= SECURITY_DESCRIPTOR_MIN_LENGTH
        new_sd_p = ctypes.cast(new_sd, LPVOID)
        if not advapi32.InitializeSecurityDescriptor(
                new_sd_p, SECURITY_DESCRIPTOR_REVISION):
            raise OSError(f"InitializeSD err={ctypes.get_last_error()}")
        if not advapi32.SetSecurityDescriptorDacl(new_sd_p, True, new_dacl, False):
            raise OSError(f"SetSDDacl err={ctypes.get_last_error()}")
        si2 = wintypes.DWORD(DACL_SECURITY_INFORMATION)
        if not user32.SetUserObjectSecurity(handle, ctypes.byref(si2), new_sd_p):
            raise OSError(f"SetUserObjectSecurity err={ctypes.get_last_error()}")
    finally:
        if new_dacl:
            kernel32.LocalFree(new_dacl)


def grant_winsta_desktop(sid: LPVOID) -> dict:
    """Grant the AppContainer SID access to the broker's window station and
    desktop. Without this, an AppContainer child dies at loader init with
    STATUS_DLL_INIT_FAILED (0xC0000142): user32 init cannot open the
    station/desktop."""
    out = {}
    winsta = user32.GetProcessWindowStation()
    desktop = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
    try:
        _grant_user_object(winsta, sid,
                           CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE)
        out["window_station"] = "granted"
    except OSError as e:
        out["window_station"] = str(e)
    try:
        _grant_user_object(desktop, sid, NO_INHERITANCE)
        out["desktop"] = "granted"
    except OSError as e:
        out["desktop"] = str(e)
    return out


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


# Set in main(): the site-packages dir the base interpreter needs on
# PYTHONPATH to import PySide6 (we spawn the *base* interpreter directly, not
# the venv trampoline python.exe -- see NOTES in the module docstring).
WORKER_PYTHONPATH: str | None = None


def build_env_block() -> ctypes.Array:
    env = {k: v for k, v in os.environ.items()}
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    if WORKER_PYTHONPATH:
        env["PYTHONPATH"] = WORKER_PYTHONPATH
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
        # Terminate the child FIRST. Otherwise the reader thread is blocked in
        # ReadFile on a still-open pipe and closing _out_file deadlocks until
        # the child (which may be hung) closes its write end.
        if self.pi and self.pi.hProcess:
            kernel32.TerminateProcess(self.pi.hProcess, 1)
            kernel32.WaitForSingleObject(self.pi.hProcess, 2000)
            kernel32.CloseHandle(self.pi.hProcess)
            kernel32.CloseHandle(self.pi.hThread)
            self.pi = None
        if self.job:
            kernel32.CloseHandle(self.job)
            self.job = None
        if self._reader is not None:
            self._reader.join(2.0)
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


# Attribute toggles (defaults = full lockdown). The diagnostic path flips
# these to isolate which attribute, if any, is fatal to the AppContainer child.
APPC_USE_CHILD_POLICY = True
APPC_USE_HANDLE_LIST = True
APPC_USE_MITIGATION = True
APPC_MITIGATION_VALUE = MITIGATION_FULL


DETACHED_PROCESS = 0x00000008


def spawn(kind: str, worker_python: str, worker_script: str, mode: str,
          sid: LPVOID | None, mem_limit=2 * 1024 * 1024 * 1024,
          use_job: bool = True, raw_cmdline: str | None = None,
          console_flag: int | None = None) -> Child:
    """kind in {'plain','job','appc'}.

    console_flag defaults to DETACHED_PROCESS for appc children and
    CREATE_NO_WINDOW otherwise. This matters: CREATE_NO_WINDOW still spawns a
    conhost.exe for a console app, which PROC_THREAD_ATTRIBUTE_CHILD_PROCESS_
    POLICY (restricted) forbids -> the child dies at init with
    STATUS_DLL_INIT_FAILED (0xC0000142). DETACHED_PROCESS gives the worker no
    console at all, so no conhost child, so child-process-policy can stay on.
    """
    if console_flag is None:
        console_flag = DETACHED_PROCESS if kind == "appc" else CREATE_NO_WINDOW
    stdin_r, stdin_w, stdout_r, stdout_w = make_pipe_pair()
    env_block = build_env_block()
    cmdline = raw_cmdline or f'"{worker_python}" "{worker_script}" {mode}'
    cmd_buf = ctypes.create_unicode_buffer(cmdline)

    pi = PROCESS_INFORMATION()
    job = None
    flags = console_flag | CREATE_UNICODE_ENVIRONMENT

    # keep-alive refs for attribute list values
    _keep = []

    if kind == "appc":
        assert sid is not None
        if use_job:
            job = make_job(mem_limit)

        # Build the attribute set dynamically so the count matches.
        attrs = []  # (attr_id, ptr, size)
        sec_cap = SECURITY_CAPABILITIES()
        sec_cap.AppContainerSid = sid
        sec_cap.Capabilities = None
        sec_cap.CapabilityCount = 0
        sec_cap.Reserved = 0
        _keep.append(sec_cap)
        attrs.append((PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                      ctypes.byref(sec_cap), ctypes.sizeof(sec_cap)))

        if APPC_USE_CHILD_POLICY:
            child_policy = ctypes.c_uint32(PROCESS_CREATION_CHILD_PROCESS_RESTRICTED)
            _keep.append(child_policy)
            attrs.append((PROC_THREAD_ATTRIBUTE_CHILD_PROCESS_POLICY,
                          ctypes.byref(child_policy), ctypes.sizeof(child_policy)))

        if APPC_USE_HANDLE_LIST:
            handle_arr = (wintypes.HANDLE * 2)(stdin_r, stdout_w)
            _keep.append(handle_arr)
            attrs.append((PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                          handle_arr, ctypes.sizeof(handle_arr)))

        if APPC_USE_MITIGATION:
            mitigation = ctypes.c_uint64(APPC_MITIGATION_VALUE)
            _keep.append(mitigation)
            attrs.append((PROC_THREAD_ATTRIBUTE_MITIGATION_POLICY,
                          ctypes.byref(mitigation), ctypes.sizeof(mitigation)))

        size = SIZE_T(0)
        kernel32.InitializeProcThreadAttributeList(
            None, len(attrs), 0, ctypes.byref(size))
        attr_buf = (ctypes.c_byte * size.value)()
        attr_list = ctypes.cast(attr_buf, LPVOID)
        if not kernel32.InitializeProcThreadAttributeList(
                attr_list, len(attrs), 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        for attr_id, ptr, sz in attrs:
            if not kernel32.UpdateProcThreadAttribute(
                    attr_list, 0, attr_id, ptr, sz, None, None):
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

    created_suspended = bool(flags & CREATE_SUSPENDED)
    if job:
        if not kernel32.AssignProcessToJobObject(job, pi.hProcess):
            raise ctypes.WinError(ctypes.get_last_error())
    if created_suspended:
        # Resume only AFTER job assignment so the child never runs outside the
        # job (design sec 4). With no job it still needs the resume.
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
        k.GetCurrentProcess.restype = wintypes.HANDLE
        adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.HANDLE)]
        adv.OpenProcessToken.restype = wintypes.BOOL
        adv.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        adv.GetTokenInformation.restype = wintypes.BOOL
        tok = wintypes.HANDLE()
        if not adv.OpenProcessToken(k.GetCurrentProcess(), 0x0008,
                                    ctypes.byref(tok)):
            return "err:OpenProcessToken " + str(ctypes.get_last_error())
        val = wintypes.DWORD(0)
        rl = wintypes.DWORD(0)
        ok = adv.GetTokenInformation(tok, 29, ctypes.byref(val), 4,
                                     ctypes.byref(rl))
        k.CloseHandle(tok)
        if not ok:
            return "err:GetTokenInformation " + str(ctypes.get_last_error())
        return bool(val.value)
    except Exception as e:
        return "err:" + repr(e)

# 2x2 red RGBA PNG (valid CRCs; generated with zlib/struct)
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAEUlEQVR42mP4z8DwH4QZYAwAR8oH+Rq28akAAAAASUVORK5CYII=")

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

    # (b) open a system file outside grant (task-specified). NOTE: C:\Windows
    # files are readable by ALL APPLICATION PACKAGES, so this MAY succeed -- it
    # documents an AppContainer characteristic, not a containment breach.
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

    # (b2) read a USER-profile secret the broker wrote (NOT granted) -- this is
    # the real filesystem-containment test; must FAIL.
    b2 = {}
    secret = os.environ.get("FAUXCASA_SECRET", "")
    try:
        with open(secret, "rb") as fh:
            fh.read(16)
        b2["allowed"] = True
        b2["error"] = None
    except Exception as e:
        b2["allowed"] = False
        b2["error"] = repr(e)
    result["user_secret_read"] = {"path": secret, **b2}

    # (b3) write a file into %TEMP% -- must FAIL (no write authority anywhere
    # but the arena, which this spike does not wire up).
    b3 = {}
    wtarget = os.environ.get("FAUXCASA_TEMPWRITE", "")
    try:
        with open(wtarget, "wb") as fh:
            fh.write(b"x")
        b3["allowed"] = True
        b3["error"] = None
    except Exception as e:
        b3["allowed"] = False
        b3["error"] = repr(e)
    result["temp_write"] = {"path": wtarget, **b3}

    # (c) socket connect to loopback -- must fail (no network capability).
    c = {}
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect(("127.0.0.1", 445))
        c["allowed"] = True
        c["error"] = None
        s.close()
    except Exception as e:
        c["allowed"] = False
        c["error"] = repr(e)
    result["socket_connect"] = c

    # (c2) DNS resolution -- decisive + fast; must fail with no network cap.
    c2 = {}
    try:
        import socket
        addrs = socket.getaddrinfo("example.com", 80)
        c2["allowed"] = True
        c2["error"] = None
        c2["n"] = len(addrs)
    except Exception as e:
        c2["allowed"] = False
        c2["error"] = repr(e)
    result["dns_lookup"] = c2

    # (d) subprocess spawn -- must fail (child-process policy).
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


def _try_appc(worker_python, worker_script, sid, label, *, use_job,
              child_policy, handle_list, mitigation, mitigation_value,
              raw_cmdline=None, tag="SPIKE_READY", timeout=12.0,
              console_flag=CREATE_NO_WINDOW):
    """Spawn one appc child with a specific attribute combination and report
    ready/exit evidence. Mutates the module toggles around the call."""
    global APPC_USE_CHILD_POLICY, APPC_USE_HANDLE_LIST, APPC_USE_MITIGATION
    global APPC_MITIGATION_VALUE
    APPC_USE_CHILD_POLICY = child_policy
    APPC_USE_HANDLE_LIST = handle_list
    APPC_USE_MITIGATION = mitigation
    APPC_MITIGATION_VALUE = mitigation_value
    rec = {"label": label, "use_job": use_job, "child_policy": child_policy,
           "handle_list": handle_list, "mitigation": mitigation,
           "console_flag": hex(console_flag)}
    try:
        child = spawn("appc", worker_python, worker_script, "ready", sid,
                      use_job=use_job, raw_cmdline=raw_cmdline,
                      console_flag=console_flag)
    except OSError as e:
        rec["spawn_error"] = str(e)
        return rec
    ready = child.read_tagged(tag, timeout)
    if ready is None:
        rec["ready"] = False
        rec["exit_code_hex"] = f"0x{child.exit_code() & 0xFFFFFFFF:08X}"
        rec["noise"] = child._noise[:10]
    else:
        rec["ready"] = True
        rec["ready_payload"] = ready[:200]
    child.close()
    return rec


def diagnose_appc(worker_python, worker_script, sid):
    """Isolation matrix (all with the job object, which is known-good): which
    of child-policy / handle-list / mitigation is fatal to the child?"""
    diag = []
    diag.append(_try_appc(worker_python, worker_script, sid,
                          "caps_job_only", use_job=True, child_policy=False,
                          handle_list=False, mitigation=False,
                          mitigation_value=0))
    diag.append(_try_appc(worker_python, worker_script, sid,
                          "caps_job_childpolicy", use_job=True,
                          child_policy=True, handle_list=False,
                          mitigation=False, mitigation_value=0))
    diag.append(_try_appc(worker_python, worker_script, sid,
                          "caps_job_handlelist", use_job=True,
                          child_policy=False, handle_list=True,
                          mitigation=False, mitigation_value=0))
    diag.append(_try_appc(worker_python, worker_script, sid,
                          "caps_job_mitigation_full", use_job=True,
                          child_policy=False, handle_list=False,
                          mitigation=True, mitigation_value=MITIGATION_FULL))
    diag.append(_try_appc(worker_python, worker_script, sid,
                          "caps_job_mitigation_aslr_only", use_job=True,
                          child_policy=False, handle_list=False,
                          mitigation=True,
                          mitigation_value=MITIGATION_FORCE_RELOCATE_ALWAYS_ON))
    diag.append(_try_appc(worker_python, worker_script, sid,
                          "caps_job_mitigation_dep_heap", use_job=True,
                          child_policy=False, handle_list=False,
                          mitigation=True,
                          mitigation_value=MITIGATION_DEP_ENABLE
                          | MITIGATION_HEAP_TERMINATE_ALWAYS_ON))
    diag.append(_try_appc(worker_python, worker_script, sid,
                          "caps_job_childpolicy_handlelist", use_job=True,
                          child_policy=True, handle_list=True,
                          mitigation=False, mitigation_value=0))
    # Does the child-process policy fail because a console app needs conhost
    # (a child process the policy forbids)? Retry it with DETACHED_PROCESS
    # (0x8) and with no console flag (0) instead of CREATE_NO_WINDOW.
    diag.append(_try_appc(worker_python, worker_script, sid,
                          "caps_job_childpolicy_DETACHED", use_job=True,
                          child_policy=True, handle_list=False,
                          mitigation=False, mitigation_value=0,
                          console_flag=DETACHED_PROCESS))
    diag.append(_try_appc(worker_python, worker_script, sid,
                          "caps_job_childpolicy_noconsoleflag", use_job=True,
                          child_policy=True, handle_list=False,
                          mitigation=False, mitigation_value=0,
                          console_flag=0))
    # The intended production config: full lockdown MINUS child-process
    # policy (rely on the job ActiveProcessLimit=1 for no-child-processes).
    diag.append(_try_appc(worker_python, worker_script, sid,
                          "caps_job_handlelist_mitigation_NOchildpolicy",
                          use_job=True, child_policy=False, handle_list=True,
                          mitigation=True, mitigation_value=MITIGATION_FULL))
    # restore full lockdown defaults
    global APPC_USE_CHILD_POLICY, APPC_USE_HANDLE_LIST, APPC_USE_MITIGATION
    global APPC_MITIGATION_VALUE
    APPC_USE_CHILD_POLICY = True
    APPC_USE_HANDLE_LIST = True
    APPC_USE_MITIGATION = True
    APPC_MITIGATION_VALUE = MITIGATION_FULL
    return diag


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

    global WORKER_PYTHONPATH
    venv_python, wmeta = resolve_worker_python(worktree)
    dirs = probe_worker_dirs(venv_python)
    result["worker_dirs"] = dirs

    # CRITICAL: spawn the *base* interpreter directly, never the venv
    # python.exe -- uv's venv python.exe is a trampoline that CreateProcess()es
    # the base interpreter as a CHILD, which ActiveProcessLimit=1 and
    # child-process-restricted policy both forbid (ERROR_NOT_ENOUGH_QUOTA /
    # STATUS_DLL_INIT_FAILED). The base interpreter starts in-process; PySide6
    # rides in on PYTHONPATH pointed at the venv site-packages.
    worker_python = os.path.join(dirs["base_prefix"], "python.exe")
    WORKER_PYTHONPATH = dirs.get("site")
    result["worker_python"] = worker_python
    result["worker_python_meta"] = wmeta
    result["venv_python_probe"] = venv_python
    result["worker_pythonpath"] = WORKER_PYTHONPATH

    # AppContainer profile
    sid = create_or_derive_profile(PROFILE_NAME)
    result["appcontainer_sid"] = sid_to_string(sid)

    # Grants: every dir the container may need to read+execute.
    grant_targets = []
    for key in ("base_prefix", "site", "pyside6"):
        d = dirs.get(key)
        if d and d not in grant_targets:
            grant_targets.append(d)
    if work_dir not in grant_targets:
        grant_targets.append(work_dir)

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

    # Window station + desktop grant (required for AppContainer loader init).
    result["winsta_desktop_grant"] = grant_winsta_desktop(sid)

    # A user-profile secret the worker must NOT be able to read, and a
    # %TEMP% path it must NOT be able to write -- the containment probes.
    secret_path = os.path.join(os.path.expanduser("~"),
                               "fauxcasa_spike_secret.txt")
    try:
        with open(secret_path, "w", encoding="utf-8") as fh:
            fh.write("top secret - the sandbox must not read this\n")
    except OSError:
        pass
    os.environ["FAUXCASA_SECRET"] = secret_path
    os.environ["FAUXCASA_TEMPWRITE"] = os.path.join(
        tempfile.gettempdir(), "fauxcasa_spike_wtest.txt")

    try:
        # 5. spawn-to-ready measurements
        result["spawn"] = {
            "plain": measure_spawn_to_ready("plain", worker_python, worker_script, None, N_SPAWN),
            "job_only": measure_spawn_to_ready("job", worker_python, worker_script, None, N_SPAWN),
            "appcontainer_job": measure_spawn_to_ready("appc", worker_python, worker_script, sid, N_SPAWN),
        }

        appc_ok = not result["spawn"]["appcontainer_job"].get("failed")

        # Always record the attribute-isolation matrix -- it is the evidence
        # for WHICH proc-thread attribute is (in)compatible with the child.
        result["appc_diagnostics"] = diagnose_appc(
            worker_python, worker_script, sid)

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
            try:
                os.remove(secret_path)
            except OSError:
                pass
            try:
                os.remove(os.environ["FAUXCASA_TEMPWRITE"])
            except OSError:
                pass
        advapi32.FreeSid(sid)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
