#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pytest",
#   "pillow",
# ]
# ///
"""Tests for picasa_db.py.

Run:  uv run scripts/test_picasa_db.py

Two layers:
- Unit tests on synthetic in-memory bytes (always run; cover every field
  type, header corruption, encodings, rect64/faces/filters/ini grammar).
- Integration tests against the Wine-oracle database written by real
  Picasa 3.9 over the synthetic library (docs/research/wine-oracle.md).
  Skipped automatically when the machine-local cache/ is absent.
"""

from __future__ import annotations

import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import picasa_db as pdb

REPO = Path(__file__).resolve().parent.parent
ORACLE_DB3 = (
    REPO
    / "cache/wine-oracle/drive_c/users/matt/AppData/Local/Google/Picasa2/db3"
)
SYNTH_LIB = REPO / "cache/synthetic-library"

needs_oracle = pytest.mark.skipif(
    not ORACLE_DB3.is_dir(), reason="wine-oracle cache not present on this machine"
)

# --------------------------------------------------------------------------
# .pmp unit tests
# --------------------------------------------------------------------------


def make_pmp(ftype: int, payload: bytes, count: int) -> bytes:
    return (
        struct.pack(
            "<IHHIHHI", pdb.PMP_MAGIC, ftype, 0x1332, 0x2, ftype, 0x1332, count
        )
        + payload
    )


def write_pmp(tmp_path: Path, name: str, ftype: int, payload: bytes, count: int) -> Path:
    p = tmp_path / name
    p.write_bytes(make_pmp(ftype, payload, count))
    return p


@pytest.mark.parametrize(
    "ftype,packfmt,values",
    [
        (0x1, "<I", [0, 1, 0xFFFFFFFF]),
        (0x3, "<B", [0, 7, 255]),
        (0x4, "<Q", [0, 2**63, 2**64 - 1]),
        (0x5, "<H", [0, 256, 65535]),
        (0x7, "<I", [42, 0, 99]),
    ],
)
def test_pmp_fixed_width_roundtrip(tmp_path, ftype, packfmt, values):
    payload = b"".join(struct.pack(packfmt, v) for v in values)
    col = pdb.read_pmp(write_pmp(tmp_path, "t_c.pmp", ftype, payload, len(values)))
    assert col.values == values
    assert col.count == len(values)
    assert col.trailing_bytes == 0


@pytest.mark.parametrize("ftype", [0x0, 0x6])
def test_pmp_string_roundtrip(tmp_path, ftype):
    strings = ["Labels", "", "Ünïcôde — ⛰", "a,b,c"]
    payload = b"".join(s.encode("utf-8") + b"\x00" for s in strings)
    col = pdb.read_pmp(write_pmp(tmp_path, "t_s.pmp", ftype, payload, len(strings)))
    assert col.values == strings


def test_pmp_string_invalid_utf8_survives(tmp_path):
    payload = b"ok\x00\xff\xfebad\x00"
    col = pdb.read_pmp(write_pmp(tmp_path, "t_s.pmp", 0x0, payload, 2))
    # surrogateescape keeps the original bytes recoverable
    assert col.values[1].encode("utf-8", "surrogateescape") == b"\xff\xfebad"


def test_pmp_date_column(tmp_path):
    vals = [0.0, 3.25, 39998.416666666664]
    payload = struct.pack("<3d", *vals)
    col = pdb.read_pmp(write_pmp(tmp_path, "t_d.pmp", 0x2, payload, 3))
    assert col.values == pytest.approx(vals)


def test_pmp_filename_split(tmp_path):
    payload = struct.pack("<I", 5)
    col = pdb.read_pmp(write_pmp(tmp_path, "imagedata_edit_width.pmp", 0x1, payload, 1))
    assert (col.table, col.column) == ("imagedata", "edit_width")


def test_pmp_marker_detection(tmp_path):
    p = tmp_path / "imagedata_0"
    p.write_bytes(struct.pack("<I", pdb.PMP_MAGIC))
    assert pdb.is_pmp_marker(p)
    p.write_bytes(struct.pack("<I", pdb.PMP_MAGIC) + b"x")
    assert not pdb.is_pmp_marker(p)


def test_pmp_header_rejects(tmp_path):
    good = make_pmp(0x1, struct.pack("<I", 1), 1)

    def corrupt(data: bytes) -> Path:
        p = tmp_path / "bad_x.pmp"
        p.write_bytes(data)
        return p

    with pytest.raises(pdb.PmpError, match="too small"):
        pdb.read_pmp(corrupt(good[:10]))
    with pytest.raises(pdb.PmpError, match="magic"):
        pdb.read_pmp(corrupt(b"\x00" + good[1:]))
    with pytest.raises(pdb.PmpError, match="constants"):
        pdb.read_pmp(corrupt(good[:6] + b"\x00\x00" + good[8:]))
    mismatched = good[:12] + struct.pack("<H", 0x3) + good[14:]
    with pytest.raises(pdb.PmpError, match="mismatch"):
        pdb.read_pmp(corrupt(mismatched))
    unknown = good[:4] + struct.pack("<H", 0x9) + good[6:12] + struct.pack("<H", 0x9) + good[14:]
    with pytest.raises(pdb.PmpError, match="unknown field type"):
        pdb.read_pmp(corrupt(unknown))


def test_pmp_truncated_payload_strict_vs_lax(tmp_path):
    p = write_pmp(tmp_path, "t_c.pmp", 0x1, struct.pack("<II", 1, 2), 5)
    with pytest.raises(pdb.PmpError, match="payload"):
        pdb.read_pmp(p)
    col = pdb.read_pmp(p, strict=False)
    assert col.values == [1, 2]
    assert col.count == 5  # declared count is preserved for diagnostics


def test_pmp_truncated_string_strict_vs_lax(tmp_path):
    p = write_pmp(tmp_path, "t_s.pmp", 0x0, b"one\x00two", 2)  # no final NUL
    with pytest.raises(pdb.PmpError, match="ended inside string"):
        pdb.read_pmp(p)
    col = pdb.read_pmp(p, strict=False)
    assert col.values == ["one"]


def test_pmp_string_trailing_garbage_strict(tmp_path):
    p = write_pmp(tmp_path, "t_s.pmp", 0x0, b"one\x00extra\x00", 1)
    with pytest.raises(pdb.PmpError, match="unconsumed"):
        pdb.read_pmp(p)
    col = pdb.read_pmp(p, strict=False)
    assert col.values == ["one"]
    assert col.trailing_bytes == len(b"extra\x00")


def test_pmp_column_get_out_of_range(tmp_path):
    col = pdb.read_pmp(write_pmp(tmp_path, "t_c.pmp", 0x1, struct.pack("<I", 7), 1))
    assert col.get(0) == 7
    assert col.get(1) is None
    assert col.get(-1) is None  # negative indexes must not wrap around


def test_read_table_variable_length_columns(tmp_path):
    write_pmp(tmp_path, "alb_name.pmp", 0x0, b"a\x00b\x00c\x00", 3)
    write_pmp(tmp_path, "alb_music.pmp", 0x0, b"m\x00", 1)
    table = pdb.read_table(tmp_path, "alb")
    assert table.n_rows == 3
    assert table.row(0) == {"name": "a", "music": "m"}
    assert table.row(2) == {"name": "c", "music": None}


# --------------------------------------------------------------------------
# OLE / VARIANT time
# --------------------------------------------------------------------------


def test_ole_epoch_and_positive():
    assert pdb.ole_to_datetime(0.0) == datetime(1899, 12, 30, tzinfo=timezone.utc)
    # sbktech's example: 3.25 is 6:00 AM on January 2, 1900
    assert pdb.ole_to_datetime(3.25) == datetime(1900, 1, 2, 6, 0, tzinfo=timezone.utc)


def test_ole_negative_fraction_is_time_of_day():
    # MS DATE semantics: -1.25 is 1899-12-29 06:00 (time always runs forward)
    assert pdb.ole_to_datetime(-1.25) == datetime(
        1899, 12, 29, 6, 0, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    "dt",
    [
        datetime(1899, 12, 30, tzinfo=timezone.utc),
        datetime(1900, 1, 2, 6, 0, tzinfo=timezone.utc),
        datetime(1899, 12, 29, 6, 0, tzinfo=timezone.utc),
        datetime(2009, 7, 4, 10, 0, tzinfo=timezone.utc),
        datetime(1903, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
    ],
)
def test_ole_roundtrip(dt):
    assert pdb.ole_to_datetime(pdb.datetime_to_ole(dt)) == dt


# --------------------------------------------------------------------------
# rect64 / faces / filters
# --------------------------------------------------------------------------


def test_rect64_full_width():
    l, t, r, b = pdb.parse_rect64("rect64(3f845bcb59418507)")
    assert (l, t, r, b) == pytest.approx(
        (0x3F84 / 65536, 0x5BCB / 65536, 0x5941 / 65536, 0x8507 / 65536)
    )


def test_rect64_leading_zeros_stripped():
    # Picasa strips leading zeros: an 8-digit value fills only right+bottom
    l, t, r, b = pdb.parse_rect64("rect64(ff4effff)")
    assert (l, t) == (0.0, 0.0)
    assert r == pytest.approx(0xFF4E / 65536)
    assert b == pytest.approx(0xFFFF / 65536)


def test_rect64_accepts_int_from_pmp_crop64_column():
    assert pdb.parse_rect64(0xFFFF) == (0.0, 0.0, 0.0, 0xFFFF / 65536)


def test_rect64_rejects_junk():
    for bad in ["", "rect64()", "0x1234", "rect64(12345678901234567)", "g000"]:
        with pytest.raises(ValueError):
            pdb.parse_rect64(bad)


def test_parse_faces():
    v = "rect64(3f845bcb59418507),abcdef0123456789;rect64(ffff),ffffffffffffffff"
    faces = pdb.parse_faces(v)
    assert len(faces) == 2
    assert faces[0][1] == "abcdef0123456789"
    assert faces[1][1] == pdb.UNKNOWN_CONTACT
    assert faces[1][0][3] == pytest.approx(0xFFFF / 65536)


def test_parse_filters():
    v = "crop64=1,3f845bcb59418507;enhance=1;finetune2=1,0.0,0.0,0.3,0.0,0.0;"
    ops = pdb.parse_filters(v)
    assert [name for name, _ in ops] == ["crop64", "enhance", "finetune2"]
    assert ops[0][1] == ["1", "3f845bcb59418507"]
    assert ops[2][1][3] == "0.3"


# --------------------------------------------------------------------------
# .picasa.ini
# --------------------------------------------------------------------------

INI_SAMPLE = "\r\n".join(
    [
        "[Picasa]",
        "name=Beach Day",
        "[photo00.jpg]",
        "star=yes",
        "caption=Sunset = lovely",  # '=' inside a value must survive
        "keywords=beach,summer",
        "rotate=rotate(1)",
        "faces=rect64(3f845bcb59418507),ffffffffffffffff",
        "filters=crop64=1,3f845bcb59418507;enhance=1;",
        "backuphash=12345",
        "[photo00.jpg]",  # duplicate section (seen after crashes)
        "star=no",
        "[.album:8e8a2a7d3c9bb6e188ac4146a4b5da41]",
        "name=Holiday",
        "token=8e8a2a7d3c9bb6e188ac4146a4b5da41",
        "",
        "stray line without separator",
    ]
)


def test_ini_sections_and_values(tmp_path):
    p = tmp_path / ".picasa.ini"
    p.write_text(INI_SAMPLE, encoding="utf-8")
    ini = pdb.read_picasa_ini(p)
    names = [s.name for s in ini.sections]
    assert names == [
        "Picasa",
        "photo00.jpg",
        "photo00.jpg",
        ".album:8e8a2a7d3c9bb6e188ac4146a4b5da41",
    ]
    photo = ini.sections[1]
    assert photo.get("star") == "yes"
    assert photo.get("caption") == "Sunset = lovely"
    assert ini.sections[2].get("star") == "no"  # duplicates preserved in order
    assert ini.anomalies == ["line 17: no '=' separator"]


def test_ini_bom_and_unicode(tmp_path):
    p = tmp_path / ".picasa.ini"
    p.write_bytes("﻿[photo.jpg]\ncaption=Çödé ☀\n".encode("utf-8"))
    ini = pdb.read_picasa_ini(p)
    assert ini.sections[0].name == "photo.jpg"
    assert ini.sections[0].get("caption") == "Çödé ☀"


def test_ini_undecodable_bytes_survive(tmp_path):
    p = tmp_path / ".picasa.ini"
    p.write_bytes(b"[photo.jpg]\ncaption=caf\xe9 latin1\n")  # not valid UTF-8
    ini = pdb.read_picasa_ini(p)
    cap = ini.sections[0].get("caption")
    assert cap is not None
    assert cap.encode("utf-8", "surrogateescape") == b"caf\xe9 latin1"


def test_ini_key_before_section(tmp_path):
    p = tmp_path / ".picasa.ini"
    p.write_text("orphan=1\n[ok]\nk=v\n", encoding="utf-8")
    ini = pdb.read_picasa_ini(p)
    assert ini.sections[0].name == ""
    assert ini.sections[0].get("orphan") == "1"
    assert any("before any section" in a for a in ini.anomalies)


# --------------------------------------------------------------------------
# thumbindex.db
# --------------------------------------------------------------------------


def make_thumbindex(entries: list[tuple[str, int, int, int, int, int, int]]) -> bytes:
    out = struct.pack("<II", pdb.THUMBINDEX_MAGIC, len(entries))
    for name, ft1, ft2, n1, n2, n3, parent in entries:
        out += name.encode("utf-8") + b"\x00"
        out += struct.pack("<QQIIHI", ft1, ft2, n1, n2, n3, parent)
    return out


SAMPLE_TI = [
    ("C:\\pics\\", 10, 20, 0, 1, 256, 0xFFFFFFFF),
    ("C:\\pics\\2009\\", 11, 21, 0, 1, 256, 0xFFFFFFFF),
    ("a.jpg", 12, 22, 999, 2, 256, 1),
    ("b.jpg", 13, 23, 888, 2, 256, 1),
]


def test_thumbindex_roundtrip(tmp_path):
    p = tmp_path / "thumbindex.db"
    p.write_bytes(make_thumbindex(SAMPLE_TI))
    entries = pdb.read_thumbindex(p)
    assert len(entries) == 4
    assert entries[0].is_folder and entries[0].parent is None
    assert entries[2].name == "a.jpg" and entries[2].parent == 1
    assert entries[2].num1 == 999 and entries[2].num2 == 2
    paths = pdb.thumbindex_full_paths(entries)
    assert paths[2] == "C:\\pics\\2009\\a.jpg"
    assert paths[0] == "C:\\pics\\"


def test_thumbindex_bad_magic(tmp_path):
    p = tmp_path / "thumbindex.db"
    p.write_bytes(b"\x00\x00\x00\x00\x00\x00\x00\x00")
    with pytest.raises(pdb.ThumbIndexError, match="magic"):
        pdb.read_thumbindex(p)


def test_thumbindex_truncated(tmp_path):
    p = tmp_path / "thumbindex.db"
    p.write_bytes(make_thumbindex(SAMPLE_TI)[:-4])
    with pytest.raises(pdb.ThumbIndexError, match="truncated"):
        pdb.read_thumbindex(p)


def test_thumbindex_trailing_bytes(tmp_path):
    p = tmp_path / "thumbindex.db"
    p.write_bytes(make_thumbindex(SAMPLE_TI) + b"xx")
    with pytest.raises(pdb.ThumbIndexError, match="unconsumed"):
        pdb.read_thumbindex(p)
    assert len(pdb.read_thumbindex(p, strict=False)) == 4


def test_thumbindex_bad_parent_does_not_crash(tmp_path):
    p = tmp_path / "thumbindex.db"
    p.write_bytes(make_thumbindex([("x.jpg", 0, 0, 1, 2, 256, 7)]))
    entries = pdb.read_thumbindex(p)
    paths = pdb.thumbindex_full_paths(entries)
    assert "bad-parent" in paths[0]


# --------------------------------------------------------------------------
# integration: the Wine oracle database
# --------------------------------------------------------------------------


@needs_oracle
def test_oracle_every_pmp_parses_strict():
    pmps = sorted(ORACLE_DB3.glob("*.pmp"))
    assert pmps, "oracle db3 contains no .pmp files?"
    for p in pmps:
        col = pdb.read_pmp(p)  # strict: any deviation raises
        assert len(col.values) == col.count
        assert col.trailing_bytes == 0


@needs_oracle
def test_oracle_markers():
    for name in ("imagedata_0", "catdata_0", "albumdata_0"):
        assert pdb.is_pmp_marker(ORACLE_DB3 / name)


@needs_oracle
def test_oracle_catdata_names():
    col = pdb.read_pmp(ORACLE_DB3 / "catdata_name.pmp")
    assert col.values[:3] == ["Labels", "Projects (internal)", "Folders on Disk"]
    assert "People" in col.values


@needs_oracle
def test_oracle_albumdata_tokens():
    col = pdb.read_pmp(ORACLE_DB3 / "albumdata_token.pmp")
    assert any(v.startswith("]star") for v in col.values)


@needs_oracle
def test_oracle_variable_length_columns():
    table = pdb.read_table(ORACLE_DB3, "albumdata")
    counts = {len(c.values) for c in table.columns.values()}
    # albumdata_music is shorter than its siblings in the oracle snapshot —
    # the variable-length behavior the format is documented to have.
    assert len(counts) > 1
    short = min(counts)
    assert table.row(table.n_rows - 1 if short < table.n_rows else 0) is not None


@needs_oracle
def test_oracle_thumbindex_matches_imagedata():
    entries = pdb.read_thumbindex(ORACLE_DB3 / "thumbindex.db")
    filetype = pdb.read_pmp(ORACLE_DB3 / "imagedata_filetype.pmp")
    assert len(entries) == filetype.count
    for e in entries:
        # thumbindex num2 mirrors imagedata.filetype (1=folder, 2=image)
        assert e.num2 == filetype.values[e.index]
        assert e.is_folder == (e.num2 == 1)


def _oracle_path_to_local(win_path: str) -> Path | None:
    # The oracle maps / as Z:, so Z:\var\home\... -> /var/home/...
    if not win_path.startswith("Z:\\"):
        return None
    return Path("/" + win_path[3:].replace("\\", "/"))


@needs_oracle
def test_oracle_join_dimensions_and_sizes():
    from PIL import Image

    entries = pdb.read_thumbindex(ORACLE_DB3 / "thumbindex.db")
    paths = pdb.thumbindex_full_paths(entries)
    width = pdb.read_pmp(ORACLE_DB3 / "imagedata_width.pmp")
    height = pdb.read_pmp(ORACLE_DB3 / "imagedata_height.pmp")
    checked = 0
    for e in entries:
        if e.is_folder:
            continue
        local = _oracle_path_to_local(paths[e.index])
        if local is None or not local.is_file():
            continue
        with Image.open(local) as img:
            assert img.size == (width.get(e.index), height.get(e.index)), paths[
                e.index
            ]
        assert e.num1 == local.stat().st_size  # thumbindex u32 is the byte size
        checked += 1
    assert checked >= 20  # the synthetic library has 24 photos


@needs_oracle
def test_oracle_album_dates_plausible():
    col = pdb.read_pmp(ORACLE_DB3 / "albumdata_date.pmp")
    for v in col.values:
        dt = pdb.ole_to_datetime(v)
        assert 2000 < dt.year < 2100


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"] + sys.argv[1:]))
