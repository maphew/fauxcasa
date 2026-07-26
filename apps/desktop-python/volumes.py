"""Fail-soft platform volume identity and mount-point probes.

Volume identity is an optional aid to library-root resolution, never a
precondition for using the application.  Every public probe therefore returns
``None`` when the host platform, filesystem, permissions, or command output do
not provide a trustworthy answer.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from xml.parsers.expat import ExpatError


def _unescape_mount_field(value: str) -> str:
    """Decode the octal escapes used by /proc/mounts fields."""
    for encoded, decoded in (("\\040", " "), ("\\011", "\t"),
                             ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(encoded, decoded)
    return value


def _linux_mounts(path: Path = Path("/proc/mounts")) -> list[tuple[Path, str]]:
    """Return ``(mountpoint, source)`` pairs, longest mountpoint first."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, RuntimeError):
        return []
    mounts: list[tuple[Path, str]] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 2:
            continue
        source = _unescape_mount_field(fields[0])
        mountpoint = Path(_unescape_mount_field(fields[1]))
        mounts.append((mountpoint, source))
    mounts.sort(key=lambda item: len(item[0].parts), reverse=True)
    return mounts


def _containing_mount(path: Path,
                      mounts: list[tuple[Path, str]]) -> tuple[Path, str] | None:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_dir() and not resolved.is_file():
        return None
    for mountpoint, source in mounts:
        try:
            if resolved == mountpoint or resolved.is_relative_to(mountpoint):
                return mountpoint, source
        except (OSError, RuntimeError, ValueError):
            continue
    return None


def _linux_uuid_devices(
        directory: Path = Path("/dev/disk/by-uuid")) -> dict[str, Path]:
    """Map UUID labels to resolved block devices, skipping broken links."""
    result: dict[str, Path] = {}
    try:
        entries = list(directory.iterdir())
    except OSError:
        return result
    for entry in entries:
        try:
            result[entry.name] = entry.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
    return result


def _windows_volume_uuid(path: Path) -> str | None:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        mount = ctypes.create_unicode_buffer(32768)
        if not kernel32.GetVolumePathNameW(str(path.resolve()), mount, len(mount)):
            return None
        name = ctypes.create_unicode_buffer(32768)
        if not kernel32.GetVolumeNameForVolumeMountPointW(
                mount.value, name, len(name)):
            return None
        return name.value or None
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _windows_mount_for_uuid(volume_uuid: str) -> Path | None:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        needed = ctypes.c_uint32()
        # First call obtains the required multi-string buffer size.
        kernel32.GetVolumePathNamesForVolumeNameW(
            volume_uuid, None, 0, ctypes.byref(needed))
        if needed.value == 0:
            return None
        paths = ctypes.create_unicode_buffer(needed.value)
        if not kernel32.GetVolumePathNamesForVolumeNameW(
                volume_uuid, paths, len(paths), ctypes.byref(needed)):
            return None
        first = paths[:].split("\0", 1)[0]
        return Path(first) if first else None
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _diskutil_info(argument: str) -> dict | None:
    try:
        proc = subprocess.run(
            ["diskutil", "info", "-plist", argument],
            capture_output=True, check=False, timeout=5)
        if proc.returncode != 0:
            return None
        data = plistlib.loads(proc.stdout)
        return data if isinstance(data, dict) else None
    except (OSError, subprocess.SubprocessError, ValueError, ExpatError):
        # ExpatError: diskutil can exit 0 with truncated/corrupt XML;
        # plistlib.loads then raises through its expat backend, and
        # ExpatError is not a ValueError. (InvalidFileException IS one,
        # so ValueError already covers the empty/non-plist case.)
        return None


def volume_uuid_for(path: Path) -> str | None:
    """Return the containing volume's stable identifier, when available."""
    path = Path(path)
    if sys.platform == "win32":
        return _windows_volume_uuid(path)
    if sys.platform.startswith("linux"):
        match = _containing_mount(path, _linux_mounts())
        if match is None:
            return None
        _mountpoint, source = match
        try:
            source_path = Path(source).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        for volume_uuid, device in _linux_uuid_devices().items():
            if device == source_path:
                return volume_uuid
        return None
    if sys.platform == "darwin":
        data = _diskutil_info(os.fspath(path))
        value = data.get("VolumeUUID") if data is not None else None
        return value if isinstance(value, str) and value else None
    return None


def mount_for_uuid(volume_uuid: str) -> Path | None:
    """Return a current mountpoint for *volume_uuid*, when discoverable."""
    if not isinstance(volume_uuid, str) or not volume_uuid:
        return None
    if sys.platform == "win32":
        return _windows_mount_for_uuid(volume_uuid)
    if sys.platform.startswith("linux"):
        devices = _linux_uuid_devices()
        wanted = next((device for label, device in devices.items()
                       if label.casefold() == volume_uuid.casefold()), None)
        if wanted is None:
            return None
        for mountpoint, source in _linux_mounts():
            try:
                if Path(source).resolve(strict=True) == wanted:
                    return mountpoint
            except (OSError, RuntimeError):
                continue
        return None
    if sys.platform == "darwin":
        data = _diskutil_info(volume_uuid)
        value = data.get("MountPoint") if data is not None else None
        return Path(value) if isinstance(value, str) and value else None
    return None
