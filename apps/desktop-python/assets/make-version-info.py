#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Generate PyInstaller's Windows VERSIONINFO resource from main.py."""

from __future__ import annotations

import re
from pathlib import Path


HERE = Path(__file__).resolve().parent
MAIN = HERE.parent / "main.py"
OUT = HERE.parent / "version_info.txt"


def main() -> None:
    source = MAIN.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([0-9]+(?:\.[0-9]+)*)"',
                      source, re.MULTILINE)
    if match is None:
        raise SystemExit(f"could not find __version__ in {MAIN}")
    version = match.group(1)
    parts = [int(part) for part in version.split(".")]
    if len(parts) > 4:
        raise SystemExit(f"version has more than four components: {version}")
    numeric = tuple(parts + [0] * (4 - len(parts)))
    dotted = ".".join(map(str, numeric))
    OUT.write_text(f'''# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numeric},
    prodvers={numeric},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Fauxcasa project'),
        StringStruct('FileDescription', 'Fauxcasa photo browser'),
        StringStruct('FileVersion', '{dotted}'),
        StringStruct('LegalCopyright', 'AGPL-3.0-or-later'),
        StringStruct('ProductName', 'Fauxcasa'),
        StringStruct('ProductVersion', '{dotted}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
''', encoding="utf-8")


if __name__ == "__main__":
    main()
