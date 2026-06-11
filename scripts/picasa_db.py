#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Research-grade readers for Picasa 3.x database formats.

Covers the three on-disk formats Picasa uses for its catalog:

- ``.pmp``         one column of one table per file (``<table>_<column>.pmp``)
- ``thumbindex.db`` record-number -> file-path index (the join key for pmp rows)
- ``.picasa.ini``  per-folder sidecar with stars, captions, faces, edits, albums

Format references (see docs/research/ for the full survey):
- sbktech 2011 ".pmp format" writeup  (docs/research/sources/sbktech-2011-*)
- Chromium media_galleries pmp_column_reader.cc (BSD-3, hardening rules)
- fbuchinger ".picasa.ini decoded" gist (public domain, ini spec + rect64)
- picasa3meta / PicasaDBReader / xkikeg PicasaDB (thumbindex layout)
- Validated against a real Picasa 3.9.141 instance running under Wine
  (docs/research/wine-oracle.md) indexing a synthetic library.

Everything is little-endian. Strings are UTF-8 (decoded with surrogateescape
so undecodable bytes survive a round trip).

Usage as a library::

    from picasa_db import read_pmp, read_table, read_thumbindex, read_picasa_ini

Usage as a CLI (any of)::

    uv run scripts/picasa_db.py pmp DB3/imagedata_width.pmp
    uv run scripts/picasa_db.py table DB3 imagedata --limit 10
    uv run scripts/picasa_db.py thumbindex DB3/thumbindex.db --paths
    uv run scripts/picasa_db.py ini /photos/2009/.picasa.ini
    uv run scripts/picasa_db.py survey DB3 --redact

``--redact`` replaces every decoded string with ``<len=N sha1=8hex>`` so the
output is safe to share when run against private libraries (field types,
lengths, counts, hashes and pass/fail only — see the project privacy rules).

NOTE: Python here is research tooling per the repo script conventions; it
does not decide the application implementation language.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import struct
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

# --------------------------------------------------------------------------
# .pmp column files
# --------------------------------------------------------------------------

PMP_MAGIC = 0x3FCCCCCD  # bytes cd cc cc 3f == little-endian float 1.6
PMP_CONST1 = 0x1332
PMP_CONST2 = 0x00000002
PMP_HEADER_SIZE = 20

# field-type code -> (name, struct width in bytes; None = null-terminated
# string). 0x0-0x4 are confirmed by Chromium's pmp_constants.h; 0x5/0x6/0x7
# come from sbktech/picasa3meta (Chromium never handled them). The oracle
# uses 0x7 for imagedata edit_width/edit_height; 0x5/0x6 are so far unseen.
PMP_FIELD_TYPES: dict[int, tuple[str, int | None]] = {
    0x0: ("string", None),
    0x1: ("uint32", 4),
    0x2: ("date", 8),  # float64, OLE/VARIANT automation time
    0x3: ("uint8", 1),
    0x4: ("uint64", 8),
    0x5: ("uint16", 2),
    0x6: ("string6", None),  # second string type (csv-ish per sbktech)
    0x7: ("uint32_7", 4),  # second uint32 type
}

_PMP_STRUCT_FMT = {1: "<B", 2: "<H", 4: "<I", 8: "<Q"}

OLE_EPOCH = datetime(1899, 12, 30)  # naive: VARIANT time is local wall-clock

# albumdata.category semantics (re-derived from Chromium picasa_types.cc,
# BSD-3): 0 = album, 2 = folder-on-disk; albums carry a "]album:<32hex>"
# token, folders carry a filesystem path in albumdata.filename.
ALBUM_CATEGORY_ALBUM = 0
ALBUM_CATEGORY_FOLDER = 2
ALBUM_CATEGORY_INVALID = 0xFFFF
ALBUM_TOKEN_PREFIX = "]album:"


class PmpError(ValueError):
    """Raised when a .pmp file fails structural validation.

    Messages never embed decoded string content, so they are safe to show
    in redacted output.
    """


def ole_to_datetime(value: float) -> datetime:
    """Convert an OLE/VARIANT automation date to a naive datetime.

    The value is days since 1899-12-30 00:00 in *local wall-clock time*
    (per Microsoft's DATE docs), hence the naive result. The fractional
    part is the absolute time of day even for negative values: -1.25 is
    1899-12-29 06:00, not 18:00. Prior art diverges here — Chromium
    converts linearly (wrong for negative fractions), picasa3meta applies
    ``t = 1.0 + t`` to negative fractions (also wrong except at .0/.5);
    we follow the Microsoft definition. Negatives never occur in healthy
    Picasa data (the UI floor is 1903), so flag them as suspect upstream.
    """
    days = math.trunc(value)
    frac = abs(value - days)
    return OLE_EPOCH + timedelta(days=days) + timedelta(days=frac)


def datetime_to_ole(dt: datetime) -> float:
    """Inverse of ole_to_datetime (for building test fixtures).

    MS semantics: the whole part counts days (signed) from the epoch to the
    value's midnight; the fractional part is the time of day, applied away
    from zero — so 1899-12-29 06:00 encodes as -1.25, not -0.75. Aware
    datetimes are taken at face value as wall-clock (tzinfo ignored).
    """
    dt = dt.replace(tzinfo=None)
    day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    days = (day_start - OLE_EPOCH) / timedelta(days=1)
    time_of_day = (dt - day_start) / timedelta(days=1)
    return days + time_of_day if days >= 0 else days - time_of_day


@dataclass
class PmpColumn:
    """One parsed .pmp file: a single column of a table."""

    path: Path
    table: str  # from the filename, e.g. "imagedata"
    column: str  # from the filename, e.g. "width"
    field_type: int
    type_name: str
    count: int  # entry count declared in the header
    values: list[Any]
    trailing_bytes: int = 0  # payload bytes beyond the declared entries

    def get(self, index: int, default: Any = None) -> Any:
        """Value at record `index`, or `default` when this column is shorter.

        Columns of one table legitimately have different lengths; an index
        past the end means "no value" rather than an error.
        """
        if 0 <= index < len(self.values):
            return self.values[index]
        return default


def split_pmp_name(path: Path) -> tuple[str, str]:
    """('imagedata_width.pmp' -> ('imagedata', 'width'))."""
    stem = path.name
    if stem.endswith(".pmp"):
        stem = stem[: -len(".pmp")]
    table, _, column = stem.partition("_")
    return table, column


def is_pmp_marker(path: Path) -> bool:
    """True for `<table>_0` marker files (magic bytes only, no column data)."""
    if not path.name.endswith("_0"):
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return len(data) == 4 and struct.unpack("<I", data)[0] == PMP_MAGIC


def read_pmp(path: Path | str, *, strict: bool = True) -> PmpColumn:
    """Parse one .pmp column file.

    Header (20 bytes, all little-endian), per sbktech and confirmed against
    Chromium's PmpColumnReader and oracle-written files:

        u32 magic 0x3fcccccd | u16 field-type | u16 0x1332 | u32 0x00000002
        | u16 field-type (repeat) | u16 0x1332 | u32 entry-count

    With strict=True any structural deviation raises PmpError — including
    trailing bytes after the declared entries, mirroring Chromium's
    pmp_column_reader, which requires the payload length to EXACTLY equal
    the entries' size (Chromium also caps files at 50 MB; we don't, being
    a research tool on trusted data). With strict=False, short payloads
    yield the entries/strings that fit and trailing bytes are recorded in
    `trailing_bytes`; `count` still reports the declared count.
    """
    path = Path(path)
    data = path.read_bytes()
    if len(data) < PMP_HEADER_SIZE:
        raise PmpError(f"{path.name}: file too small for header ({len(data)} bytes)")
    magic, ftype, c1, c2, ftype2, c3, count = struct.unpack_from("<IHHIHHI", data, 0)
    if magic != PMP_MAGIC:
        raise PmpError(f"{path.name}: bad magic 0x{magic:08x}")
    if c1 != PMP_CONST1 or c3 != PMP_CONST1 or c2 != PMP_CONST2:
        raise PmpError(
            f"{path.name}: bad header constants 0x{c1:04x}/0x{c2:08x}/0x{c3:04x}"
        )
    if ftype != ftype2:
        raise PmpError(f"{path.name}: field type mismatch 0x{ftype:x} != 0x{ftype2:x}")
    if ftype not in PMP_FIELD_TYPES:
        raise PmpError(f"{path.name}: unknown field type 0x{ftype:x}")

    type_name, width = PMP_FIELD_TYPES[ftype]
    payload = memoryview(data)[PMP_HEADER_SIZE:]
    values: list[Any]
    trailing = 0

    if width is not None:
        need = count * width
        if len(payload) < need:
            if strict:
                raise PmpError(
                    f"{path.name}: payload {len(payload)} bytes < "
                    f"{count} x {width} = {need}"
                )
            count_avail = len(payload) // width
        else:
            count_avail = count
            trailing = len(payload) - need
            if trailing and strict:
                raise PmpError(
                    f"{path.name}: payload {len(payload)} bytes != "
                    f"{count} x {width} = {need} ({trailing} trailing)"
                )
        if ftype == 0x2:
            values = list(
                struct.unpack_from(f"<{count_avail}d", payload, 0) if count_avail else ()
            )
        else:
            fmt = _PMP_STRUCT_FMT[width][1]
            values = list(
                struct.unpack_from(f"<{count_avail}{fmt}", payload, 0)
                if count_avail
                else ()
            )
    else:
        values = []
        raw = bytes(payload)
        pos = 0
        for _ in range(count):
            end = raw.find(b"\x00", pos)
            if end < 0:
                if strict:
                    raise PmpError(
                        f"{path.name}: payload ended inside string "
                        f"{len(values)} of {count}"
                    )
                break
            values.append(raw[pos:end].decode("utf-8", "surrogateescape"))
            pos = end + 1
        else:
            trailing = len(raw) - pos
    if trailing and strict and width is None:
        # Like Chromium, the payload must be consumed exactly; for string
        # columns this also proves the NUL scan stayed in sync.
        raise PmpError(f"{path.name}: {trailing} unconsumed bytes after last string")

    table, column = split_pmp_name(path)
    return PmpColumn(
        path=path,
        table=table,
        column=column,
        field_type=ftype,
        type_name=type_name,
        count=count,
        values=values,
        trailing_bytes=trailing,
    )


@dataclass
class PmpTable:
    """All columns of one table (e.g. every imagedata_*.pmp in a db3 dir)."""

    name: str
    columns: dict[str, PmpColumn]

    @property
    def n_rows(self) -> int:
        """Largest column length; shorter columns read as None past their end."""
        return max((len(c.values) for c in self.columns.values()), default=0)

    def row(self, index: int) -> dict[str, Any]:
        return {name: col.get(index) for name, col in self.columns.items()}

    def rows(self) -> Iterator[dict[str, Any]]:
        for i in range(self.n_rows):
            yield self.row(i)


def read_table(db3_dir: Path | str, table: str, *, strict: bool = True) -> PmpTable:
    """Read every `<table>_<column>.pmp` in a db3 directory."""
    db3_dir = Path(db3_dir)
    columns: dict[str, PmpColumn] = {}
    for path in sorted(db3_dir.glob(f"{table}_*.pmp")):
        col = read_pmp(path, strict=strict)
        columns[col.column] = col
    return PmpTable(name=table, columns=columns)


# --------------------------------------------------------------------------
# thumbindex.db
# --------------------------------------------------------------------------

THUMBINDEX_MAGIC = 0x40466666
THUMBINDEX_NO_PARENT = 0xFFFFFFFF
FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

# File-type codes for ThumbIndexEntry.ftype, re-derived from xkikeg/PicasaDB
# (Data/PicasaDB.hs; layout facts only). '?' = that project's own uncertainty.
THUMBINDEX_FILE_TYPES = {
    0x00: "empty",
    0x01: "directory",
    0x02: "jpeg",
    0x03: "gif",
    0x05: "directory",
    0x06: "wbmp",
    0x07: "photoshop",
    0x08: "avi",
    0x09: "mpeg4?",
    0x0A: "h264?",
    0x0D: "tiff",
    0x0E: "png",
    0x12: "nikon-raw",
    0x1E: "xml",
    0xE9: "empty",
}


class ThumbIndexError(ValueError):
    """Raised when thumbindex.db fails structural validation."""


def filetime_to_datetime(ft: int) -> datetime | None:
    """Windows FILETIME (100ns ticks since 1601-01-01 UTC) -> datetime."""
    if ft == 0:
        return None
    return FILETIME_EPOCH + timedelta(microseconds=ft // 10)


@dataclass
class ThumbIndexEntry:
    """One record of thumbindex.db.

    Folder entries store a full path (e.g. ``C:\\photos\\2009\\``) and have
    ``parent == None``; file entries store a bare name and join to their
    folder through ``parent`` (an index into the same entry list — one level
    of indirection, folders always carry absolute paths). The record number
    here equals the record number in the ``imagedata`` table — this file is
    the join key between pmp rows and paths on disk.

    The 26 bytes between name and parent follow xkikeg/PicasaDB's
    decomposition (u64 + u64 + u32 + u8 + u32 + u8), with semantics
    refined against the Wine oracle (all 28 entries cross-checked against
    the synthetic library):

    - ``taken_filetime``: NOT the filesystem creation time (prior art's
      guess) — for photos with EXIF it matches DateTimeOriginal exactly,
      converted local->UTC; without EXIF it falls back to file mtime.
    - ``modified_filetime``: file mtime (matched on-disk mtime 28/28).
    - ``size``: byte size of the file (exact match 28/28; folders 0).
      xkikeg treats these 4 bytes as opaque; size fits our oracle.
    - ``ftype``: see THUMBINDEX_FILE_TYPES (1/5=directory, 2=jpeg, ...).
    - ``flags``: opaque u32, observed 0.
    - ``valid``: 0 = invalidated entry (xkikeg enforces that such entries
      carry no parent index); observed 1 everywhere in the oracle.

    An empty ``name`` marks a deleted record (per picasa3meta); record
    numbers are never reused, so deleted rows keep their slot.
    """

    index: int
    name: str
    taken_filetime: int
    modified_filetime: int
    size: int
    ftype: int
    flags: int
    valid: int
    parent: int | None  # entry index of containing folder; None for folders

    @property
    def is_folder(self) -> bool:
        return self.parent is None and not self.is_deleted

    @property
    def is_deleted(self) -> bool:
        return self.name == ""

    @property
    def ftype_name(self) -> str:
        return THUMBINDEX_FILE_TYPES.get(self.ftype, f"unknown-0x{self.ftype:x}")


def read_thumbindex(path: Path | str, *, strict: bool = True) -> list[ThumbIndexEntry]:
    """Parse thumbindex.db.

    Layout (confirmed against oracle-written files; corroborated by
    picasa3meta and xkikeg/PicasaDB):

        u32 magic 0x40466666 | u32 entry-count
        then per entry:
        null-terminated path/name | u64 taken | u64 mtime | u32 size
        | u8 ftype | u32 flags | u8 valid | u32 parent-index

    0xffffffff as parent-index means "no parent" (folder or deleted).
    picasa3meta also accepted 0xff as a name terminator; we have never
    observed that and require NUL.
    """
    path = Path(path)
    data = path.read_bytes()
    if len(data) < 8:
        raise ThumbIndexError(f"{path.name}: too small ({len(data)} bytes)")
    magic, count = struct.unpack_from("<II", data, 0)
    if magic != THUMBINDEX_MAGIC:
        raise ThumbIndexError(f"{path.name}: bad magic 0x{magic:08x}")
    entries: list[ThumbIndexEntry] = []
    pos = 8
    for i in range(count):
        end = data.find(b"\x00", pos)
        if end < 0 or end + 1 + 30 > len(data):
            raise ThumbIndexError(
                f"{path.name}: truncated in entry {i} of {count} at offset {pos}"
            )
        name = data[pos:end].decode("utf-8", "surrogateescape")
        taken, mtime, size, ftype, flags, valid, parent = struct.unpack_from(
            "<QQIBIBI", data, end + 1
        )
        entries.append(
            ThumbIndexEntry(
                index=i,
                name=name,
                taken_filetime=taken,
                modified_filetime=mtime,
                size=size,
                ftype=ftype,
                flags=flags,
                valid=valid,
                parent=None if parent == THUMBINDEX_NO_PARENT else parent,
            )
        )
        pos = end + 1 + 30
    if pos != len(data) and strict:
        raise ThumbIndexError(
            f"{path.name}: {len(data) - pos} unconsumed bytes after entry {count}"
        )
    return entries


def thumbindex_full_paths(entries: list[ThumbIndexEntry]) -> list[str]:
    """Resolve each entry to its full path.

    Folder entries already store one; deleted entries resolve to "".
    Parent chains are followed defensively even though Picasa only ever
    writes a single level (files -> folder with absolute path).
    """
    paths: list[str] = []
    for e in entries:
        if e.is_deleted:
            paths.append("")
            continue
        parts = [e.name]
        seen = {e.index}
        cur = e
        while cur.parent is not None:
            if cur.parent in seen or not 0 <= cur.parent < len(entries):
                parts.insert(0, f"<bad-parent:{cur.parent}>")
                break
            seen.add(cur.parent)
            cur = entries[cur.parent]
            parts.insert(0, cur.name)
        paths.append("".join(parts))
    return paths


# --------------------------------------------------------------------------
# .picasa.ini
# --------------------------------------------------------------------------


@dataclass
class IniSection:
    """One `[name]` section; items preserve order and duplicate keys."""

    name: str
    items: list[tuple[str, str]] = field(default_factory=list)

    def get(self, key: str, default: str | None = None) -> str | None:
        for k, v in self.items:
            if k.lower() == key.lower():
                return v
        return default


@dataclass
class PicasaIni:
    path: Path
    sections: list[IniSection]
    anomalies: list[str] = field(default_factory=list)  # structural notes only

    def section(self, name: str) -> IniSection | None:
        for s in self.sections:
            if s.name.lower() == name.lower():
                return s
        return None


def read_picasa_ini(path: Path | str) -> PicasaIni:
    """Parse a .picasa.ini (or legacy Picasa.ini / picasa.ini) file.

    Deliberately not configparser: real-world files contain duplicate
    sections AND duplicate keys (configparser merges or throws), dynamic
    key names with spaces/hyphens (`BKTag All Pictures-backuphash`),
    `[(null)]` garbage sections, even byte-reversed lines after crashes
    (per picasa2digikam's field experience and the fbuchinger-gist survey
    of ~800 real files). We preserve order, duplicates, and unknown
    sections verbatim; unparseable lines are recorded as anomalies by line
    number — never by content, so output stays redaction-safe. Section
    order is arbitrary in the wild; never assume [Picasa] comes first.
    `;` and `#` are NOT comment markers in Picasa files (`;` is data in
    faces/filters/Contacts2 values) — only whole junk lines are skipped.

    Encoding: Picasa 3.x writes UTF-8 and marks it with an `[encoding]`
    section (`utf8=1`, may appear anywhere); older versions wrote the
    system codepage, and mixed-encoding files exist in the wild (sections
    appended by different Picasa versions). We decode UTF-8 with
    surrogateescape so every byte survives a round trip either way.
    """
    path = Path(path)
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8", "surrogateescape")

    sections: list[IniSection] = []
    anomalies: list[str] = []
    current: IniSection | None = None
    for lineno, line in enumerate(text.split("\n"), 1):
        line = line.rstrip("\r").strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = IniSection(name=line[1:-1])
            sections.append(current)
            continue
        key, sep, value = line.partition("=")
        if not sep:
            anomalies.append(f"line {lineno}: no '=' separator")
            continue
        if current is None:
            current = IniSection(name="")
            sections.append(current)
            anomalies.append(f"line {lineno}: key before any section header")
        current.items.append((key.strip(), value))
    return PicasaIni(path=path, sections=sections, anomalies=anomalies)


# ---- value decoders ------------------------------------------------------

_RECT64_RE = re.compile(r"^rect64\(([0-9a-fA-F]{1,16})\)$")


def parse_rect64(value: str | int) -> tuple[float, float, float, float]:
    """Decode a rect64/crop64 value to (left, top, right, bottom) in [0, 1].

    The 64-bit number packs four 16-bit fixed-point values, each a fraction
    of the image dimension (value / 65536). Accepts the `rect64(hex)`
    wrapper, a bare hex string, or an int (the pmp crop64 column stores the
    same encoding as uint64). Picasa strips leading zeros from the hex, so
    the string must be left-padded to 16 digits before splitting — getting
    this wrong reads the rectangle shifted by whole fields.
    """
    if isinstance(value, int):
        packed = value
    else:
        s = value.strip()
        m = _RECT64_RE.match(s)
        if m:
            s = m.group(1)
        if not re.fullmatch(r"[0-9a-fA-F]{1,16}", s):
            raise ValueError("not a rect64 value")
        packed = int(s, 16)
    if packed < 0 or packed > 0xFFFFFFFFFFFFFFFF:
        raise ValueError("rect64 out of range")
    left = (packed >> 48) & 0xFFFF
    top = (packed >> 32) & 0xFFFF
    right = (packed >> 16) & 0xFFFF
    bottom = packed & 0xFFFF
    return (left / 65536.0, top / 65536.0, right / 65536.0, bottom / 65536.0)


# faces= contact id meaning "face detected, name suggestion unconfirmed"
UNKNOWN_CONTACT = "ffffffffffffffff"


def parse_faces(value: str) -> list[tuple[tuple[float, float, float, float], str]]:
    """Decode a `faces=` value: `rect64(...),<16-hex contact id>;...`.

    Returns [(rect, contact_id), ...]; contact ids join to [Contacts2]
    sections / contacts.xml. Ids are %llx-printed by Picasa (leading zeros
    stripped), so they are zero-padded back to 16 chars here — the
    [Contacts2] keys are stored padded. UNKNOWN_CONTACT marks a face whose
    suggested name was never confirmed; faces with no suggestion at all are
    simply absent from the ini.
    """
    out = []
    for part in value.split(";"):
        part = part.strip()
        if not part:
            continue
        rect_s, _, contact = part.partition(",")
        contact = contact.strip().lower()
        if re.fullmatch(r"[0-9a-f]{1,16}", contact):
            contact = contact.zfill(16)
        out.append((parse_rect64(rect_s), contact))
    return out


_ROTATE_RE = re.compile(r"^rotate\((\d+)\)$")


def parse_rotate(value: str) -> int:
    """Decode `rotate=rotate(N)` -> N quarter-turns clockwise (0-3)."""
    m = _ROTATE_RE.match(value.strip())
    if not m:
        raise ValueError("not a rotate(N) value")
    return int(m.group(1))


def parse_moddate(value: str) -> datetime:
    """Decode a `moddate=` value (16 hex chars) to a UTC datetime.

    Inferred format (validated by plausibility only — the community never
    cracked this key): the hex is a byte-for-byte little-endian dump of a
    Windows FILETIME, e.g. `8094e2826277cd01` -> 0x01cd776282e29480 ->
    2012-08-11 01:42:05 UTC. Verify against the oracle before treating as
    normative.
    """
    s = value.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{16}", s):
        raise ValueError("not a moddate value")
    ticks = int.from_bytes(bytes.fromhex(s), "little")
    dt = filetime_to_datetime(ticks)
    if dt is None:
        raise ValueError("moddate is zero")
    return dt


def parse_filters(value: str) -> list[tuple[str, list[str]]]:
    """Decode a `filters=` edit stack: `name=param1,param2,...;name=...;`.

    Each entry is one edit operation in application order; the first
    parameter is `1` (enabled) for every operation fbuchinger documented.
    Returns [(name, [params...]), ...] with params kept as strings —
    their meaning is per-filter (see the filters table in the gist).
    """
    out = []
    for part in value.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, params = part.partition("=")
        out.append((name, params.split(",") if params else []))
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _redact_str(s: str) -> str:
    digest = hashlib.sha1(s.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    return f"<len={len(s)} sha1={digest}>"


def _render(value: Any, redact: bool) -> Any:
    if isinstance(value, str):
        return _redact_str(value) if redact else value
    return value


def _print_json(obj: Any) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str)
    print()


def cmd_pmp(args: argparse.Namespace) -> int:
    for p in args.files:
        p = Path(p)
        if is_pmp_marker(p):
            print(f"{p.name}: table marker (magic only)")
            continue
        col = read_pmp(p, strict=not args.lax)
        head = (
            f"{p.name}: table={col.table} column={col.column} "
            f"type=0x{col.field_type:x} ({col.type_name}) count={col.count}"
        )
        if col.trailing_bytes:
            head += f" trailing_bytes={col.trailing_bytes}"
        print(head)
        shown = col.values if args.limit is None else col.values[: args.limit]
        if args.json:
            _print_json([_render(v, args.redact) for v in shown])
        else:
            for i, v in enumerate(shown):
                if col.field_type == 0x2:
                    v = f"{v!r} ({ole_to_datetime(v).isoformat()})"
                else:
                    v = _render(v, args.redact)
                print(f"  [{i}] {v}")
            if args.limit is not None and len(col.values) > args.limit:
                print(f"  ... {len(col.values) - args.limit} more")
    return 0


def cmd_table(args: argparse.Namespace) -> int:
    table = read_table(args.db3, args.table, strict=not args.lax)
    if not table.columns:
        print(f"no {args.table}_*.pmp files in {args.db3}", file=sys.stderr)
        return 1
    counts = {name: len(c.values) for name, c in table.columns.items()}
    print(f"table {args.table}: {table.n_rows} rows, columns: {counts}")
    rows = []
    for i, row in enumerate(table.rows()):
        if args.limit is not None and i >= args.limit:
            print(f"... {table.n_rows - args.limit} more rows")
            break
        rows.append({k: _render(v, args.redact) for k, v in row.items()})
        if not args.json:
            print(f"[{i}] {rows[-1]}")
    if args.json:
        _print_json(rows)
    return 0


def cmd_thumbindex(args: argparse.Namespace) -> int:
    entries = read_thumbindex(args.file, strict=not args.lax)
    print(f"{Path(args.file).name}: {len(entries)} entries")
    paths = thumbindex_full_paths(entries) if args.paths else None
    out = []
    for e in entries:
        shown = paths[e.index] if paths else e.name
        rec = {
            "index": e.index,
            "path" if args.paths else "name": _render(shown, args.redact),
            "folder": e.is_folder,
            "deleted": e.is_deleted,
            "parent": e.parent,
            "taken": str(filetime_to_datetime(e.taken_filetime)),
            "modified": str(filetime_to_datetime(e.modified_filetime)),
            "size": e.size,
            "ftype": e.ftype_name,
            "flags": e.flags,
            "valid": e.valid,
        }
        out.append(rec)
        if not args.json:
            print(f"  {rec}")
    if args.json:
        _print_json(out)
    return 0


# Known-structural ini key vocabulary: safe to print under --redact. Keys
# outside this set are redacted too, because Picasa generates dynamic key
# names embedding account names (IIDLIST_<user>_lh, <user>_lh) and backup
# set names (<set name>-backuphash).
_INI_KNOWN_KEYS = frozenset(
    """star caption keywords rotate flipped crop crop64 filters redo faces
    albums backuphash moddate originhash width height textactive text geotag
    screensaver name token date description location link category p2category
    utf8 album""".split()
)


def cmd_ini(args: argparse.Namespace) -> int:
    ini = read_picasa_ini(args.file)
    out: list[dict[str, Any]] = []
    for s in ini.sections:
        sec: dict[str, Any] = {"section": _render(s.name, args.redact), "items": []}
        for k, v in s.items:
            redact_key = args.redact and k.lower() not in _INI_KNOWN_KEYS
            item: dict[str, Any] = {
                "key": _redact_str(k) if redact_key else k,
                "value": _render(v, args.redact),
            }
            try:
                kl = k.lower()
                if kl == "faces":
                    item["decoded"] = [
                        {"rect": r, "contact": c} for r, c in parse_faces(v)
                    ]
                elif kl in ("filters", "redo"):
                    item["decoded"] = parse_filters(v)
                elif kl == "rotate":
                    item["decoded"] = parse_rotate(v)
                elif kl == "moddate":
                    item["decoded"] = str(parse_moddate(v))
                elif kl == "date" and s.name.lower() == "picasa":
                    # [Picasa] date= is an OLE double; [.album:*] date= is
                    # ISO 8601 text and needs no decoding.
                    item["decoded"] = str(ole_to_datetime(float(v)))
                elif kl in ("crop", "crop64") or v.startswith("rect64("):
                    item["decoded"] = parse_rect64(v)
            except ValueError:
                item["decoded"] = None
            sec["items"].append(item)
        out.append(sec)
    if args.json:
        _print_json({"sections": out, "anomalies": ini.anomalies})
    else:
        for sec in out:
            print(f"[{sec['section']}]")
            for item in sec["items"]:
                line = f"  {item['key']} = {item['value']}"
                if "decoded" in item and item["decoded"] is not None:
                    line += f"   -> {item['decoded']}"
                print(line)
        for a in ini.anomalies:
            print(f"anomaly: {a}")
    return 0


def cmd_survey(args: argparse.Namespace) -> int:
    """Structural survey of a db3 directory: types, counts, validity.

    Output contains no decoded string values regardless of --redact; this
    subcommand is meant to be safe on private databases by construction.
    """
    db3 = Path(args.db3)
    report: list[dict[str, Any]] = []
    for p in sorted(db3.iterdir()):
        if p.name.endswith("_0"):
            rec = {"file": p.name, "kind": "marker", "ok": is_pmp_marker(p)}
        elif p.suffix == ".pmp":
            try:
                col = read_pmp(p, strict=False)
                rec = {
                    "file": p.name,
                    "kind": "pmp",
                    "type": f"0x{col.field_type:x}",
                    "type_name": col.type_name,
                    "count": col.count,
                    "parsed": len(col.values),
                    "trailing_bytes": col.trailing_bytes,
                    "ok": len(col.values) == col.count and col.trailing_bytes == 0,
                }
            except (PmpError, OSError) as e:
                rec = {"file": p.name, "kind": "pmp", "ok": False, "error": str(e)}
        elif p.name == "thumbindex.db":
            try:
                entries = read_thumbindex(p)
                rec = {
                    "file": p.name,
                    "kind": "thumbindex",
                    "entries": len(entries),
                    "folders": sum(1 for e in entries if e.is_folder),
                    "ok": True,
                }
            except (ThumbIndexError, OSError) as e:
                rec = {"file": p.name, "kind": "thumbindex", "ok": False, "error": str(e)}
        else:
            rec = {"file": p.name, "kind": "other", "bytes": p.stat().st_size}
        report.append(rec)
    if args.json:
        _print_json(report)
    else:
        for rec in report:
            print(rec)
    return 0


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="JSON output")
    common.add_argument(
        "--redact",
        action="store_true",
        help="replace decoded strings with <len/sha1> placeholders",
    )
    common.add_argument(
        "--lax", action="store_true", help="tolerate truncated/corrupt files"
    )
    common.add_argument(
        "--limit", type=int, default=None, help="max values/rows to print"
    )
    ap = argparse.ArgumentParser(
        description="Readers for Picasa 3.x database formats (.pmp, thumbindex.db, .picasa.ini)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pmp", help="dump .pmp column file(s)", parents=[common])
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_pmp)

    p = sub.add_parser("table", help="join all columns of a table", parents=[common])
    p.add_argument("db3")
    p.add_argument("table")
    p.set_defaults(func=cmd_table)

    p = sub.add_parser("thumbindex", help="dump thumbindex.db", parents=[common])
    p.add_argument("file")
    p.add_argument("--paths", action="store_true", help="resolve full paths")
    p.set_defaults(func=cmd_thumbindex)

    p = sub.add_parser("ini", help="dump a .picasa.ini file", parents=[common])
    p.add_argument("file")
    p.set_defaults(func=cmd_ini)

    p = sub.add_parser(
        "survey", help="structural survey of a db3 directory", parents=[common]
    )
    p.add_argument("db3")
    p.set_defaults(func=cmd_survey)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
