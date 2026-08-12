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


def test_pmp_fixed_width_trailing_garbage_strict(tmp_path):
    # Chromium requires payload length to EXACTLY equal count*width
    p = write_pmp(tmp_path, "t_c.pmp", 0x1, struct.pack("<II", 1, 2) + b"xx", 2)
    with pytest.raises(pdb.PmpError, match="trailing"):
        pdb.read_pmp(p)
    col = pdb.read_pmp(p, strict=False)
    assert col.values == [1, 2]
    assert col.trailing_bytes == 2


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
    # naive datetimes: VARIANT time is local wall-clock per the MS DATE docs
    assert pdb.ole_to_datetime(0.0) == datetime(1899, 12, 30)
    # sbktech's example: 3.25 is 6:00 AM on January 2, 1900
    assert pdb.ole_to_datetime(3.25) == datetime(1900, 1, 2, 6, 0)


def test_ole_negative_fraction_is_time_of_day():
    # MS DATE semantics: -1.25 is 1899-12-29 06:00 (time always runs forward);
    # MS's own table: -0.25 = 30 Dec 1899 6 A.M., -2.5 = 28 Dec 1899 noon
    assert pdb.ole_to_datetime(-1.25) == datetime(1899, 12, 29, 6, 0)
    assert pdb.ole_to_datetime(-0.25) == datetime(1899, 12, 30, 6, 0)
    assert pdb.ole_to_datetime(-2.5) == datetime(1899, 12, 28, 12, 0)


@pytest.mark.parametrize(
    "dt",
    [
        datetime(1899, 12, 30),
        datetime(1900, 1, 2, 6, 0),
        datetime(1899, 12, 29, 6, 0),
        datetime(2009, 7, 4, 10, 0),
        datetime(1903, 12, 31, 23, 59, 59),
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


def test_parse_faces_short_contact_id_zero_padded():
    # contact ids are %llx-printed (leading zeros stripped in the wild)
    faces = pdb.parse_faces("rect64(ffff),632e71e2ffd6c6d")
    assert faces[0][1] == "0632e71e2ffd6c6d"


def test_parse_filters():
    # finetune2 shape per oracle fixture 037: slot [4] is 8-hex-digit ARGB
    # (neutral-color picker, 00000000 = unset), NOT a float — the fields
    # around it are floats, so a consumer must not float() them all.
    v = "crop64=1,3f845bcb59418507;enhance=1;finetune2=1,0.2,0.2,0.3,00000000,0.5;"
    ops = pdb.parse_filters(v)
    assert [name for name, _ in ops] == ["crop64", "enhance", "finetune2"]
    assert ops[0][1] == ["1", "3f845bcb59418507"]
    assert ops[2][1][3] == "0.3"
    assert ops[2][1][4] == "00000000"


def test_parse_filters_oracle_captured():
    # Verbatim filters= values captured at the live Wine oracle
    # (fixtures/oracle/034..037): the first non-crop tokens in the corpus.
    cases = {
        "enhance=1;": ("enhance", ["1"]),                      # 034 one-click
        "fill=1,0.186916;": ("fill", ["1", "0.186916"]),       # 035 Basic Fixes
        "tilt=1,0.565155,0.000000;":                           # 036 straighten
            ("tilt", ["1", "0.565155", "0.000000"]),
        "finetune2=1,0.280702,0.261053,0.345263,00000000,0.555556;":  # 037 Tuning
            ("finetune2",
             ["1", "0.280702", "0.261053", "0.345263", "00000000", "0.555556"]),
    }
    for raw, expected in cases.items():
        assert pdb.parse_filters(raw) == [expected]


def test_parse_filters_real_world_crop():
    # real crop from the fbuchinger-gist survey: 10000000f1ddff49
    ops = pdb.parse_filters("crop64=1,10000000f1ddff49;")
    rect = pdb.parse_rect64(ops[0][1][1])
    assert rect == pytest.approx(
        (0x1000 / 65536, 0.0, 0xF1DD / 65536, 0xFF49 / 65536)
    )


def test_parse_rotate():
    assert pdb.parse_rotate("rotate(1)") == 1
    assert pdb.parse_rotate("rotate(3)") == 3
    with pytest.raises(ValueError):
        pdb.parse_rotate("1")


def test_parse_moddate_le_filetime():
    # worked example from the format research (inferred LE FILETIME dump)
    dt = pdb.parse_moddate("8094e2826277cd01")
    assert dt == datetime(2012, 8, 11, 1, 42, 5, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        pdb.parse_moddate("xyz")


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
    # A file with [encoding] utf8=1 but non-UTF-8 bytes (mixed-encoding
    # file from different Picasa versions): surrogateescape keeps every byte
    # intact — the UTF-8-path round-trip guarantee is untouched.
    p = tmp_path / ".picasa.ini"
    p.write_bytes(b"[encoding]\nutf8=1\n[photo.jpg]\ncaption=caf\xe9 latin1\n")
    ini = pdb.read_picasa_ini(p)
    cap = ini.sections[1].get("caption")  # sections[0] = [encoding]
    assert cap is not None
    assert cap.encode("utf-8", "surrogateescape") == b"caf\xe9 latin1"


def test_ini_cp1252_caption(tmp_path):
    """Without [encoding] utf8=1 and with non-strict-UTF-8 bytes, decode
    falls through to cp1252 (pre-UTF8 Picasa was Windows-only, so cp1252 —
    not the running machine's locale — is always the fallback codepage;
    no monkeypatch needed, the real path is portable by construction)."""
    p = tmp_path / ".picasa.ini"
    # "Straße" in cp1252: ß = 0xDF; no [encoding] utf8=1 marker
    p.write_bytes(b"[photo.jpg]\ncaption=Stra\xdfe\n")
    ini = pdb.read_picasa_ini(p)
    assert ini.sections[0].get("caption") == "Straße"


def test_ini_key_before_section(tmp_path):
    p = tmp_path / ".picasa.ini"
    p.write_text("orphan=1\n[ok]\nk=v\n", encoding="utf-8")
    ini = pdb.read_picasa_ini(p)
    assert ini.sections[0].name == ""
    assert ini.sections[0].get("orphan") == "1"
    assert any("before any section" in a for a in ini.anomalies)


def test_cli_ini_redact_hides_strings_and_unknown_keys(tmp_path, capsys):
    p = tmp_path / ".picasa.ini"
    p.write_text(
        "[holiday.jpg]\ncaption=secret words\nIIDLIST_joedoe_lh=4dfe636c9cf4c302\n"
        "star=yes\n",
        encoding="utf-8",
    )
    assert pdb.main(["ini", str(p), "--redact"]) == 0
    out = capsys.readouterr().out
    # values, section (filename), and dynamic key names must not appear
    assert "secret" not in out and "holiday" not in out and "joedoe" not in out
    assert "star" in out  # known-structural keys stay readable


# --------------------------------------------------------------------------
# thumbindex.db
# --------------------------------------------------------------------------


def make_thumbindex(entries: list[tuple]) -> bytes:
    out = struct.pack("<II", pdb.THUMBINDEX_MAGIC, len(entries))
    for name, taken, mtime, size, ftype, flags, valid, parent in entries:
        out += name.encode("utf-8") + b"\x00"
        out += struct.pack("<QQIBIBI", taken, mtime, size, ftype, flags, valid, parent)
    return out


SAMPLE_TI = [
    ("C:\\pics\\", 10, 20, 0, 0x01, 0, 1, 0xFFFFFFFF),
    ("C:\\pics\\2009\\", 11, 21, 0, 0x01, 0, 1, 0xFFFFFFFF),
    ("a.jpg", 12, 22, 999, 0x02, 0, 1, 1),
    ("b.jpg", 13, 23, 888, 0x02, 0, 1, 1),
]


def test_thumbindex_roundtrip(tmp_path):
    p = tmp_path / "thumbindex.db"
    p.write_bytes(make_thumbindex(SAMPLE_TI))
    entries = pdb.read_thumbindex(p)
    assert len(entries) == 4
    assert entries[0].is_folder and entries[0].parent is None
    assert entries[2].name == "a.jpg" and entries[2].parent == 1
    assert entries[2].size == 999 and entries[2].ftype == 0x02
    assert entries[2].ftype_name == "jpeg" and entries[0].ftype_name == "directory"
    assert entries[2].valid == 1 and entries[2].flags == 0
    paths = pdb.thumbindex_full_paths(entries)
    assert paths[2] == "C:\\pics\\2009\\a.jpg"
    assert paths[0] == "C:\\pics\\"


def test_thumbindex_deleted_entry(tmp_path):
    p = tmp_path / "thumbindex.db"
    rows = SAMPLE_TI + [("", 0, 0, 0, 0x00, 0, 0, 0xFFFFFFFF)]
    p.write_bytes(make_thumbindex(rows))
    entries = pdb.read_thumbindex(p)
    assert entries[4].is_deleted
    assert not entries[4].is_folder  # deleted, not a folder, despite no parent
    assert not entries[4].is_facecrop  # no parent retained -> true deletion
    assert pdb.thumbindex_full_paths(entries)[4] == ""


def test_thumbindex_facecrop_entry(tmp_path):
    # empty name + retained valid parent = face-crop virtual record
    # (shape as observed in the oracle: ftype 0xe9, flags 3, valid 1, size 1)
    p = tmp_path / "thumbindex.db"
    rows = SAMPLE_TI + [("", 0, 0, 1, 0xE9, 3, 1, 2)]
    p.write_bytes(make_thumbindex(rows))
    entries = pdb.read_thumbindex(p)
    e = entries[4]
    assert e.is_facecrop
    assert not e.is_deleted and not e.is_folder
    assert e.ftype_name == "face-crop"
    assert pdb.thumbindex_full_paths(entries)[4] == ""


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
    p.write_bytes(make_thumbindex([("x.jpg", 0, 0, 1, 0x02, 0, 1, 7)]))
    entries = pdb.read_thumbindex(p)
    paths = pdb.thumbindex_full_paths(entries)
    assert "bad-parent" in paths[0]


# --------------------------------------------------------------------------
# corrupt-value guards (decoders must raise ValueError or return None,
# never OverflowError, on adversarial inputs)
# --------------------------------------------------------------------------


def test_ole_rejects_nonfinite_and_out_of_range():
    for bad in [float("nan"), float("inf"), -float("inf"), 1e9, 1e300, -1e9]:
        with pytest.raises(ValueError):
            pdb.ole_to_datetime(bad)


def test_ole_half_boundary_canonical_encoding():
    # +0.5 and -0.5 decode identically (MS DATE is non-injective near zero);
    # the canonical encoding of that instant is the positive form
    noon = datetime(1899, 12, 30, 12, 0)
    assert pdb.ole_to_datetime(0.5) == pdb.ole_to_datetime(-0.5) == noon
    assert pdb.datetime_to_ole(noon) == 0.5


def test_datetime_to_ole_aware_input_treated_as_wall_clock():
    naive = datetime(2009, 7, 4, 10, 0)
    aware = datetime(2009, 7, 4, 10, 0, tzinfo=timezone.utc)
    assert pdb.datetime_to_ole(aware) == pdb.datetime_to_ole(naive)


def test_filetime_overflow_returns_none():
    assert pdb.filetime_to_datetime(2**64 - 1) is None
    assert pdb.filetime_to_datetime(0) is None


def test_parse_moddate_overflow_raises_valueerror():
    for bad in ["ffffffffffffffff", "7fffffffffffffff", "0000000000000000"]:
        with pytest.raises(ValueError):
            pdb.parse_moddate(bad)


def test_parse_filters_edges():
    assert pdb.parse_filters("") == []
    assert pdb.parse_filters("enhance") == [("enhance", [])]
    assert pdb.parse_filters("enhance=") == [("enhance", [])]


def test_parse_rect64_int_range():
    assert pdb.parse_rect64(0) == (0.0, 0.0, 0.0, 0.0)
    assert pdb.parse_rect64(2**64 - 1) == (0xFFFF / 65536,) * 4
    for bad in (-1, 2**64):
        with pytest.raises(ValueError):
            pdb.parse_rect64(bad)


def test_pmp_column_get_default(tmp_path):
    col = pdb.read_pmp(write_pmp(tmp_path, "t_c.pmp", 0x1, struct.pack("<I", 7), 1))
    assert col.get(5, default=0) == 0


def test_read_table_missing(tmp_path):
    table = pdb.read_table(tmp_path, "nothere")
    assert table.columns == {} and table.n_rows == 0
    assert pdb.main(["table", str(tmp_path), "nothere"]) == 1


def test_is_pmp_marker_missing_file(tmp_path):
    assert not pdb.is_pmp_marker(tmp_path / "missing_0")
    assert not pdb.is_pmp_marker(tmp_path / "missing.pmp")


def test_ini_value_whitespace_preserved(tmp_path):
    p = tmp_path / ".picasa.ini"
    p.write_bytes(b"[x.jpg]\r\ncaption=  pad me  \r\n")
    sec = pdb.read_picasa_ini(p).sections[0]
    assert sec.items == [("caption", "  pad me  ")]


# --------------------------------------------------------------------------
# CLI contract tests (JSON purity, --lax plumbing, redaction)
# --------------------------------------------------------------------------

import json as _json


def _json_after_headers(out: str, header_lines: int):
    return _json.loads("\n".join(out.splitlines()[header_lines:]))


def _cli_fixture_dir(tmp_path: Path) -> Path:
    write_pmp(tmp_path, "img_width.pmp", 0x1, struct.pack("<II", 10, 20), 2)
    write_pmp(tmp_path, "img_name.pmp", 0x0, b"a\x00b\x00", 2)
    write_pmp(
        tmp_path, "img_date.pmp", 0x2, struct.pack("<2d", 39998.5, 40000.25), 2
    )
    (tmp_path / "img_0").write_bytes(struct.pack("<I", pdb.PMP_MAGIC))
    (tmp_path / "thumbindex.db").write_bytes(
        make_thumbindex(SAMPLE_TI + [("", 0, 0, 1, 0xE9, 3, 1, 2)])
    )
    return tmp_path


def test_cli_thumbindex_facecrop_field(tmp_path, capsys):
    d = _cli_fixture_dir(tmp_path)
    assert pdb.main(["thumbindex", str(d / "thumbindex.db"), "--json"]) == 0
    recs = _json_after_headers(capsys.readouterr().out, 1)
    assert recs[-1]["facecrop"] is True and recs[-1]["deleted"] is False
    assert recs[0]["facecrop"] is False


def test_cli_json_outputs_are_parseable(tmp_path, capsys):
    d = _cli_fixture_dir(tmp_path)
    cases = [
        (["pmp", str(d / "img_width.pmp"), "--json"], 1),
        (["pmp", str(d / "img_date.pmp"), "--json", "--redact"], 1),
        (["table", str(d), "img", "--json"], 1),
        (["table", str(d), "img", "--json", "--limit", "1"], 1),  # no stray note
        (["thumbindex", str(d / "thumbindex.db"), "--json"], 1),
        (["survey", str(d), "--json"], 0),
    ]
    for argv, headers in cases:
        assert pdb.main(argv) == 0, argv
        _json_after_headers(capsys.readouterr().out, headers)  # must not raise


def test_cli_ini_json_parseable(tmp_path, capsys):
    p = tmp_path / ".picasa.ini"
    p.write_text("[x.jpg]\nstar=yes\n", encoding="utf-8")
    assert pdb.main(["ini", str(p), "--json"]) == 0
    data = _json_after_headers(capsys.readouterr().out, 0)
    assert data["sections"][0]["items"][0]["key"] == "star"


def test_cli_lax_plumbing(tmp_path, capsys):
    bad_pmp = write_pmp(tmp_path, "t_c.pmp", 0x1, struct.pack("<I", 1), 5)
    with pytest.raises(pdb.PmpError):
        pdb.main(["pmp", str(bad_pmp)])
    assert pdb.main(["pmp", str(bad_pmp), "--lax"]) == 0
    bad_ti = tmp_path / "thumbindex.db"
    bad_ti.write_bytes(make_thumbindex(SAMPLE_TI) + b"junk")
    with pytest.raises(pdb.ThumbIndexError):
        pdb.main(["thumbindex", str(bad_ti)])
    assert pdb.main(["thumbindex", str(bad_ti), "--lax"]) == 0
    capsys.readouterr()


def test_cli_redact_hides_timestamps(tmp_path, capsys):
    d = _cli_fixture_dir(tmp_path)
    assert pdb.main(["pmp", str(d / "img_date.pmp"), "--redact"]) == 0
    out = capsys.readouterr().out
    assert "39998" not in out and "2009" not in out
    assert "<redacted-date>" in out
    assert pdb.main(["table", str(d), "img", "--redact", "--json"]) == 0
    rows = _json_after_headers(capsys.readouterr().out, 1)
    assert all(r["date"] == "<redacted-date>" for r in rows)
    assert pdb.main(["thumbindex", str(d / "thumbindex.db"), "--redact"]) == 0
    out = capsys.readouterr().out
    assert "<redacted-date>" in out and "1601" not in out


def test_cli_pmp_corrupt_date_does_not_crash(tmp_path, capsys):
    p = write_pmp(
        tmp_path, "t_d.pmp", 0x2, struct.pack("<2d", float("nan"), 1e300), 2
    )
    assert pdb.main(["pmp", str(p)]) == 0
    out = capsys.readouterr().out
    assert out.count("invalid date") == 2


def test_cli_ini_redact_decoded_is_structural_only(tmp_path, capsys):
    p = tmp_path / ".picasa.ini"
    p.write_text(
        "[Picasa]\ndate=39621.924444\n"
        "[x.jpg]\nfaces=rect64(4a8e8e6b),632e71e2ffd6c6d\n"
        "filters=crop64=1,10000000f1ddff49;enhance=1;\n"
        "moddate=8094e2826277cd01\ncrop=rect64(3f845bcb59418507)\n",
        encoding="utf-8",
    )
    assert pdb.main(["ini", str(p), "--redact"]) == 0
    out = capsys.readouterr().out
    # decoded summaries keep format vocabulary only
    assert "crop64" in out and "enhance" in out and "'faces': 1" in out
    # ...but no params, rects, contact ids, or timestamps
    for leak in ["10000000f1ddff49", "632e71e2", "0.2481", "2012", "2008", "4a8e"]:
        assert leak not in out, leak


def test_cli_survey_never_raises_and_redacts_foreign_names(tmp_path, capsys):
    d = _cli_fixture_dir(tmp_path)
    (d / "t_c.pmp").write_bytes(b"\x00bad")  # corrupt pmp
    (d / "Aunt Edna Holiday.txt").write_text("x")  # foreign file name
    (d / "repository.dat").write_bytes(b"\x00" * 8)  # known sidecar name
    assert pdb.main(["survey", str(d), "--json"]) == 0
    out = capsys.readouterr().out
    assert "Aunt Edna" not in out
    assert "repository.dat" in out
    report = _json.loads(out)
    assert any(r.get("ok") is False for r in report["files"])  # corrupt pmp
    assert report["sentinels"]["repository.dat"]["ok"] is False  # bad magic
    assert str(d) not in out  # no absolute paths anywhere


# --------------------------------------------------------------------------
# repository.dat / usernames.dat
# --------------------------------------------------------------------------


def make_repository(pairs: list[tuple[str, str]]) -> bytes:
    out = struct.pack("<II", pdb.PMP_MAGIC, len(pairs))
    for k, v in pairs:
        out += k.encode() + b"\x00" + v.encode() + b"\x00"
    return out


def test_repository_roundtrip(tmp_path):
    p = tmp_path / "repository.dat"
    pairs = [("KeywordVersion", "1"), ("gpsversion", "1.0")]
    p.write_bytes(make_repository(pairs))
    assert pdb.read_repository(p) == pairs


def test_repository_empty(tmp_path):
    p = tmp_path / "usernames.dat"
    p.write_bytes(make_repository([]))
    assert pdb.read_repository(p) == []


def test_repository_bad_magic(tmp_path):
    p = tmp_path / "repository.dat"
    p.write_bytes(b"\x00" * 8)
    with pytest.raises(pdb.RepositoryError, match="magic"):
        pdb.read_repository(p)


def test_repository_truncated(tmp_path):
    p = tmp_path / "repository.dat"
    p.write_bytes(make_repository([("a", "1")])[:-1])
    with pytest.raises(pdb.RepositoryError, match="truncated"):
        pdb.read_repository(p)


def test_repository_trailing_bytes(tmp_path):
    p = tmp_path / "repository.dat"
    p.write_bytes(make_repository([("a", "1")]) + b"x")
    with pytest.raises(pdb.RepositoryError, match="unconsumed"):
        pdb.read_repository(p)


def test_encode_repository_matches_oracle_layout():
    pairs = [("KeywordVersion", "1"), ("gpsversion", "1.0")]
    assert pdb.encode_repository(pairs) == make_repository(pairs)
    assert pdb.encode_repository([]) == make_repository([])


def test_write_repository_roundtrip(tmp_path):
    p = tmp_path / "repository.dat"
    pairs = [("KeywordVersion", "1"), ("a", "a"), ("a", "a")]  # dupes preserved
    pdb.write_repository(p, pairs)
    assert pdb.read_repository(p) == pairs


def test_encode_repository_rejects_nul():
    with pytest.raises(ValueError, match="NUL"):
        pdb.encode_repository([("a\x00b", "1")])


# --------------------------------------------------------------------------
# survey rollups (tables / sentinels / scale / features / --library)
# --------------------------------------------------------------------------


def _survey_rollup_fixture(tmp_path: Path) -> Path:
    """A db3 dir whose every user-data value contains the SECRET marker."""
    db3 = tmp_path / "db3"
    db3.mkdir()
    write_pmp(db3, "imagedata_caption.pmp", 0x0, b"SECRETCAP\x00\x00\x00", 3)
    write_pmp(db3, "imagedata_rotate.pmp", 0x1, struct.pack("<3I", 0, 1, 0), 3)
    write_pmp(
        db3, "albumdata_category.pmp", 0x1, struct.pack("<3I", 0, 2, 0xFFFF), 3
    )
    (db3 / "thumbindex.db").write_bytes(
        make_thumbindex(
            [
                ("C:\\SECRETDIR\\", 10, 20, 0, 0x01, 0, 1, 0xFFFFFFFF),
                ("SECRETFILE.jpg", 12, 22, 999, 0x02, 0, 1, 0),
                ("", 0, 0, 1, 0xE9, 3, 1, 1),  # face crop of SECRETFILE
                ("", 0, 0, 5000, 0x00, 0, 0, 0xFFFFFFFF),  # deleted slot
            ]
        )
    )
    (db3 / "starlist.txt").write_bytes(b"C:\\SECRETDIR\\SECRETFILE.jpg\r\n\r\n")
    # the four sentinel quadrants: known key + version value (prints),
    # known key + non-version value, unknown-but-word-shaped key (the
    # signed-in usernames.dat shape), junk key + junk value (all redact)
    (db3 / "repository.dat").write_bytes(
        make_repository(
            [
                ("KeywordVersion", "1"),
                ("contactsversion", "1244102400"),
                ("username", "SECRETUSER"),
                ("odd key!", "SECRETVAL"),
            ]
        )
    )
    return db3


def test_survey_rollups(tmp_path, capsys):
    db3 = _survey_rollup_fixture(tmp_path)
    assert pdb.main(["survey", str(db3), "--json"]) == 0
    report = _json.loads(capsys.readouterr().out)
    assert report["tables"]["imagedata"]["columns"]["caption"] == {
        "type": "string",
        "rows": 3,
        "populated": 1,
        "distinct": 2,
    }
    assert report["features"]["starlist_entries"] == 1
    assert report["features"]["albumdata_categories"] == {
        "album": 1,
        "folder": 1,
        "other": 1,
    }
    ti = report["scale"]["thumbindex"]
    assert ti["entries"] == 4 and ti["folders"] == 1 and ti["files"] == 1
    assert ti["deleted"] == 1 and ti["face_crops"] == 1
    assert ti["by_ftype"] == {"jpeg": 1} and ti["file_bytes"] == 999
    pairs = dict(map(tuple, report["sentinels"]["repository.dat"]["pairs"]))
    assert pairs["KeywordVersion"] == "1"
    # known key, non-version-shaped value: key prints, value redacted
    assert pairs["contactsversion"].startswith("<len=10 ")


def test_survey_rollups_never_leak_values(tmp_path, capsys):
    db3 = _survey_rollup_fixture(tmp_path)
    for argv in ([], ["--json"], ["--redact"]):
        assert pdb.main(["survey", str(db3)] + argv) == 0
        out = capsys.readouterr().out
        assert "SECRET" not in out  # captions, paths, odd sentinel values
        assert "odd key!" not in out  # sentinel key outside vocabulary
        # word-shaped key outside vocabulary is redacted (quoted: the
        # legitimate "usernames.dat" file name contains the substring)
        assert '"username"' not in out
        assert "1244102400" not in out  # timestamp under a known key


def test_survey_usernames_dat_fully_redacted(tmp_path, capsys):
    """The populated usernames.dat format is unobserved account data:
    every key and value must redact, however machine-ish they look."""
    db3 = tmp_path / "db3"
    db3.mkdir()
    (db3 / "usernames.dat").write_bytes(
        make_repository(
            [("maphew_gmail_com", "20091225"), ("IIDLIST_maphew_lh", "1.0")]
        )
    )
    assert pdb.main(["survey", str(db3), "--json"]) == 0
    out = capsys.readouterr().out
    for leak in ("maphew", "gmail", "IIDLIST", "20091225"):
        assert leak not in out, leak
    report = _json.loads(out)
    assert len(report["sentinels"]["usernames.dat"]["pairs"]) == 2


def test_survey_filename_gates(tmp_path, capsys):
    db3 = tmp_path / "db3"
    db3.mkdir()
    # known vocabulary prints
    (db3 / "facetemplatesV2_0.db").write_bytes(b"\x00" * 4)
    (db3 / "thumbs2_index.db").write_bytes(b"\x00" * 4)
    # space-free personal names must still redact (no shape heuristics)
    (db3 / "MomsTherapyNotes.txt").write_text("x")
    (db3 / "Passwords.dat").write_text("x")
    # stray *_*.pmp: name redacted in files AND tables sections
    (db3 / "Aunt Edna Wedding_notes.pmp").write_bytes(b"junk")
    # valid pmp under a known table but non-machine column name
    write_pmp(db3, "imagedata_MomSecret.pmp", 0x1, struct.pack("<I", 7), 1)
    # marker-shaped file of an unknown table
    (db3 / "secrets_0").write_bytes(struct.pack("<I", pdb.PMP_MAGIC))
    assert pdb.main(["survey", str(db3), "--json"]) == 0
    out = capsys.readouterr().out
    assert "facetemplatesV2_0.db" in out and "thumbs2_index.db" in out
    for leak in ("MomsTherapyNotes", "Passwords", "Aunt Edna", "Edna Wedding",
                 "MomSecret", "secrets"):
        assert leak not in out, leak
    report = _json.loads(out)
    assert "imagedata" in report["tables"]  # known table key survives


def test_survey_library_missing_dir_fails_loudly(tmp_path, capsys):
    db3 = tmp_path / "db3"
    db3.mkdir()
    missing = tmp_path / "no-such-library"
    assert pdb.main(["survey", str(db3), "--library", str(missing)]) == 2
    err = capsys.readouterr().err
    assert "not a directory" in err
    assert str(missing) not in err  # error path stays redacted too


def test_survey_file_bytes_excludes_deleted_entries(tmp_path, capsys):
    db3 = tmp_path / "db3"
    db3.mkdir()
    rows = SAMPLE_TI + [("", 0, 0, 5000, 0x00, 0, 0, 0xFFFFFFFF)]  # stale size
    (db3 / "thumbindex.db").write_bytes(make_thumbindex(rows))
    assert pdb.main(["survey", str(db3), "--json"]) == 0
    ti = _json.loads(capsys.readouterr().out)["scale"]["thumbindex"]
    assert ti["deleted"] == 1
    assert ti["face_crops"] == 0
    assert ti["file_bytes"] == 999 + 888  # a.jpg + b.jpg, not the stale 5000


def test_survey_face_crops_counted_separately(tmp_path, capsys):
    db3 = tmp_path / "db3"
    db3.mkdir()
    rows = SAMPLE_TI + [("", 0, 0, 1, 0xE9, 3, 1, 2)]  # oracle facecrop shape
    (db3 / "thumbindex.db").write_bytes(make_thumbindex(rows))
    assert pdb.main(["survey", str(db3), "--json"]) == 0
    ti = _json.loads(capsys.readouterr().out)["scale"]["thumbindex"]
    assert ti["face_crops"] == 1 and ti["deleted"] == 0
    assert ti["files"] == 2  # the virtual record is not a file on disk
    assert ti["file_bytes"] == 999 + 888  # its size=1 is not a byte count
    assert "face-crop" not in ti["by_ftype"]


def test_survey_distinct_collapses_nan_dates(tmp_path, capsys):
    db3 = tmp_path / "db3"
    db3.mkdir()
    nan = struct.pack("<d", float("nan"))
    write_pmp(db3, "imagedata_date.pmp", 0x2, nan * 3, 3)
    assert pdb.main(["survey", str(db3), "--json"]) == 0
    col = _json.loads(capsys.readouterr().out)["tables"]["imagedata"]["columns"][
        "date"
    ]
    assert col["distinct"] == 1  # one corrupt constant, not three uniques


def test_survey_ini_tree(tmp_path, capsys):
    lib = tmp_path / "lib"
    (lib / "f1").mkdir(parents=True)
    (lib / "f1" / ".picasa.ini").write_text(
        "[SECRETPHOTO.jpg]\n"
        "star=yes\n"
        "caption=SECRETCAPTION\n"
        "keywords=SECRETKW\n"
        "geotag=48.2,16.3\n"
        "faces=rect64(1234567890abcdef),ffffffffffffffff;"
        "rect64(fedcba0987654321),1234567890abcdef\n"
        "albums=deadbeefdeadbeefdeadbeefdeadbeef\n"
        "rotate=rotate(1)\n"
        "SECRETKEY=x\n"
        "[.album:deadbeefdeadbeefdeadbeefdeadbeef]\n"
        "name=SECRETALBUM\n"
        "token=]album:deadbeefdeadbeefdeadbeefdeadbeef\n"
        "[Contacts2]\n"
        "1234567890abcdef=SECRETNAME;;\n",
        encoding="utf-8",
    )
    (lib / "f2").mkdir()
    (lib / "f2" / "Picasa.ini").write_text("[p.jpg]\nstar=yes\n", encoding="utf-8")
    db3 = tmp_path / "db3"
    db3.mkdir()
    assert pdb.main(["survey", str(db3), "--library", str(lib), "--json"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out  # no filenames, captions, keys, or contacts
    report = _json.loads(out)["library"]
    assert report["ini_files"] == 2
    assert report["read_errors"] == 0 and report["anomalies"] == 0
    assert report["sections"] == {"album": 1, "contacts2": 1, "file": 2}
    assert report["keys"]["<other>"] == 1  # SECRETKEY counted, not named
    feats = report["features"]
    assert feats["starred"] == 2
    assert feats["captioned"] == 1
    assert feats["keyworded"] == 1
    assert feats["geotagged"] == 1
    assert feats["face_tags"] == 2 and feats["face_tags_named"] == 1
    assert feats["album_memberships"] == 1
    assert feats["edit_keys"] == 1  # rotate; faces/star/caption are not edits
    assert feats["contacts"] == 1


# --------------------------------------------------------------------------
# survey rollup helpers: media counts, .pal, contacts.xml (fauxcasa-ed5.11)
# --------------------------------------------------------------------------


def test_survey_ini_tree_media_counts(tmp_path):
    """_survey_ini_tree with image_exts/video_exts returns survey-owned
    media file, video, and folder counts; backward-compat: callers that
    omit image_exts get no 'media' key."""
    lib = tmp_path / "lib"
    (lib / "f1").mkdir(parents=True)
    (lib / "f1" / "photo00.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    (lib / "f1" / "clip00.avi").write_bytes(b"RIFF")
    (lib / "f1" / "note.txt").write_bytes(b"not media")
    (lib / "f2").mkdir()
    (lib / "f2" / "photo00.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    # f3: dir with no media — must NOT count toward media_folders
    (lib / "f3").mkdir()
    (lib / "f3" / "readme.txt").write_bytes(b"x")
    image_exts = frozenset({".jpg", ".jpeg", ".avi", ".mp4"})
    video_exts = frozenset({".avi", ".mp4"})
    result = pdb._survey_ini_tree(lib, image_exts=image_exts,
                                  video_exts=video_exts)
    assert result["media"]["files"] == 3   # photo00.jpg x2 + clip00.avi
    assert result["media"]["videos"] == 1  # clip00.avi only
    assert result["media"]["folders"] == 2  # f1 and f2 each have media
    # backward-compat: no image_exts → no "media" key
    result_basic = pdb._survey_ini_tree(lib)
    assert "media" not in result_basic
    # video_exts=None → videos count is None
    result_no_vext = pdb._survey_ini_tree(lib, image_exts=image_exts)
    assert result_no_vext["media"]["files"] == 3
    assert result_no_vext["media"]["videos"] is None


def test_count_pal_files(tmp_path):
    """_count_pal_files: counts *.pal files in a directory; 0 for absent."""
    albums = tmp_path / "albums"
    albums.mkdir()
    (albums / "uid1.pal").write_text("<picasa2album/>")
    (albums / "uid2.pal").write_text("<picasa2album/>")
    (albums / "readme.txt").write_text("not a pal")
    assert pdb._count_pal_files(albums) == 2
    assert pdb._count_pal_files(tmp_path / "nonexistent") == 0


def test_count_contacts_xml(tmp_path):
    """_count_contacts_xml: counts <contact> elements; 0 for absent file."""
    xml = tmp_path / "contacts.xml"
    xml.write_text(
        "<contacts>\n"
        ' <contact id="a" name="Alice"/>\n'
        ' <contact id="b" name="Bob"/>\n'
        "</contacts>\n",
        encoding="utf-8",
    )
    assert pdb._count_contacts_xml(xml) == 2
    assert pdb._count_contacts_xml(tmp_path / "nonexistent.xml") == 0


def test_count_contacts_xml_empty_file(tmp_path):
    """An empty contacts.xml (no <contact> elements) returns 0."""
    xml = tmp_path / "contacts.xml"
    xml.write_text("<contacts/>\n", encoding="utf-8")
    assert pdb._count_contacts_xml(xml) == 0


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
        assert e.is_folder == (e.ftype == 1)
        if e.valid != 1:
            # delete tombstone: zeroed in place, row count preserved, and
            # the imagedata filetype row is zeroed too (fixture 015)
            assert (e.name, e.size, e.ftype, e.parent) == ("", 0, 0, None)
            assert filetype.values[e.index] == 0
            continue
        if e.is_facecrop:
            # the face-crop row's u8 holds the low byte of its imagedata
            # filetype (1001 = 0x3e9 -> 0xe9); observed-shape pin (014
            # face-tag fixture session), relax if future evidence diverges
            assert filetype.values[e.index] == 1001
            assert e.ftype == filetype.values[e.index] & 0xFF
            assert e.flags == 3 and e.size == 1
            parent = entries[e.parent]
            assert not parent.is_folder and parent.name  # crops a photo
        else:
            # the thumbindex ftype byte mirrors imagedata.filetype
            # exactly on real entries (1=dir, 2=jpeg)
            assert e.ftype == filetype.values[e.index]
            assert e.flags == 0


@needs_oracle
def test_oracle_facecrop_virtual_rows():
    """Face-crop records carry a virtual imagedata row: crop64 == facerect
    == the rect64 Picasa wrote to the folder ini faces= line, photo dims
    copied from the parent, personalbumid -> the person album (token
    ]facealbum:<row>, category 8)."""
    entries = pdb.read_thumbindex(ORACLE_DB3 / "thumbindex.db")
    paths = pdb.thumbindex_full_paths(entries)
    crops = [e for e in entries if e.is_facecrop]
    assert crops  # the 014 face-tag fixture session left one
    img = pdb.read_table(ORACLE_DB3, "imagedata")
    token = pdb.read_pmp(ORACLE_DB3 / "albumdata_token.pmp")
    category = pdb.read_pmp(ORACLE_DB3 / "albumdata_category.pmp")
    for e in crops:
        row, parent_row = img.row(e.index), img.row(e.parent)
        assert row["crop64"] == row["facerect"] != 0
        l, t, r, b = pdb.parse_rect64(row["facerect"])
        assert 0 <= l < r <= 1 and 0 <= t < b <= 1  # plausible geometry
        # the same rect sits in the parent folder's ini faces= line
        local = _oracle_path_to_local(paths[e.parent])
        ini = pdb.read_picasa_ini(local.parent / ".picasa.ini")
        faces = pdb.parse_faces(ini.section(local.name).get("faces"))
        assert (l, t, r, b) in [rect for rect, _ in faces]
        assert (row["width"], row["height"]) == (
            parent_row["width"],
            parent_row["height"],
        )
        pid = row["personalbumid"]
        assert token.values[pid] == f"]facealbum:{pid}"
        assert category.values[pid] == 8  # people album
        assert 2000 < pdb.ole_to_datetime(row["tagdate"]).year < 2100


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
        assert e.size == local.stat().st_size  # thumbindex u32 is the byte size
        checked += 1
    assert checked >= 20  # the synthetic library has 24 photos


@needs_oracle
def test_oracle_album_dates_plausible():
    col = pdb.read_pmp(ORACLE_DB3 / "albumdata_date.pmp")
    for v in col.values:
        dt = pdb.ole_to_datetime(v)
        assert 2000 < dt.year < 2100


@needs_oracle
def test_oracle_repository_sentinels():
    pairs = dict(pdb.read_repository(ORACLE_DB3 / "repository.dat"))
    assert pairs["KeywordVersion"] == "1"
    assert pairs["frversion"] == "1.5"
    assert pairs["gpsversion"] == "1.0"
    assert pdb.read_repository(ORACLE_DB3 / "usernames.dat") == []


@needs_oracle
def test_oracle_survey_runs_clean(capsys):
    argv = ["survey", str(ORACLE_DB3), "--library", str(SYNTH_LIB), "--json"]
    assert pdb.main(argv) == 0
    report = _json.loads(capsys.readouterr().out)
    bad = [r for r in report["files"] if not r.get("ok", True)]
    assert not bad, bad
    ti = report["scale"]["thumbindex"]
    # the oracle is a growing baseline: deletes tombstone rows in place
    # (fixture 015), so live + tombstoned never drops below the original 24
    assert ti["files"] + ti["deleted"] >= 24
    assert ti["face_crops"] >= 1  # the 014 fixture session's manual face tag
    assert "face-crop" not in ti["by_ftype"]  # virtual records aren't files
    # every imagedata column row count is bounded by the thumbindex join key
    assert report["tables"]["imagedata"]["rows"] == ti["entries"]
    assert report["sentinels"]["repository.dat"]["ok"] is True
    assert report["library"]["ini_files"] >= 3


# --------------------------------------------------------------------------
# integration: committed differential fixtures (fixtures/oracle/NNN-*)
# --------------------------------------------------------------------------

FIXTURES = REPO / "fixtures" / "oracle"

needs_fixtures = pytest.mark.skipif(
    not FIXTURES.is_dir(), reason="no oracle differential fixtures present"
)


@needs_fixtures
def test_fixture_every_pmp_parses_strict():
    pmps = sorted(FIXTURES.rglob("*.pmp"))
    assert pmps
    for p in pmps:
        col = pdb.read_pmp(p)
        assert len(col.values) == col.count


@needs_fixtures
def test_fixture_every_thumbindex_parses_strict():
    files = sorted(FIXTURES.rglob("thumbindex.db"))
    assert files
    for p in files:
        assert pdb.read_thumbindex(p)


@needs_fixtures
def test_fixture_every_picasa_ini_parses_clean():
    inis = sorted(FIXTURES.rglob(".picasa.ini"))
    assert inis  # real Picasa-written ini files (CRLF, no [encoding] yet)
    for p in inis:
        ini = pdb.read_picasa_ini(p)
        # An empty (0-byte) .picasa.ini is real Picasa output, not a parse
        # failure: reverting a baked edit consumes the .picasaoriginals
        # stash and leaves the originals .picasa.ini emptied to 0 bytes
        # rather than unlinking it (fixture 027-revert-baked-edit). Such a
        # file parses cleanly to zero sections — non-empty inis must still
        # carry sections.
        assert not ini.anomalies
        assert ini.sections or p.stat().st_size == 0
        for s in ini.sections:
            assert s.name  # every section is a [filename]


@needs_fixtures
def test_fixture_star_lands_in_ini():
    p = (
        FIXTURES
        / "001-star-photo/after/library/2010-12-25 Winter Holiday/.picasa.ini"
    )
    if not p.is_file():
        pytest.skip("fixture 001 layout changed")
    sec = pdb.read_picasa_ini(p).section("photo02.jpg")
    assert sec is not None and sec.get("star") == "yes"


@needs_fixtures
def test_fixture_caption_column_is_variable_length():
    db3 = FIXTURES / "002-caption-photo/after/db3"
    if not (db3 / "imagedata_caption.pmp").is_file():
        pytest.skip("fixture 002 layout changed")
    cap = pdb.read_pmp(db3 / "imagedata_caption.pmp")
    # the captioned photo is the last record; earlier rows are empty and
    # rows past the column's end simply don't exist (25 entries vs 28 rows)
    assert cap.values[-1] != ""
    assert all(v == "" for v in cap.values[:-1])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"] + sys.argv[1:]))
