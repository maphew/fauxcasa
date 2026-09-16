#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest"]
# ///
"""Tests for inisidecar.py (fauxcasa-lgg.1): the sidecar-first
`.picasa.ini` write layer. Design: docs/design/ini-write-layer.md, test
strategy in its §9.

Run: `uv run apps/desktop-python/test_inisidecar.py -q`
(inisidecar.py is pure stdlib, no Qt -- QT_QPA_PLATFORM is harmless but
unnecessary here, unlike the PySide6-backed test_*.py files in this dir.)

Two groups:
- Green now: byte round-trip (the module's core invariant, design §9),
  IniDocument.apply's edit placement/rewrite/remove rules, and
  IniEdit.validate's refusals. All of these must pass, no xfail.
- xfail(strict=True, raises=NotImplementedError) placeholders for the
  design §4 write_edits behaviours that land in lgg.4; a placeholder that
  starts passing (because someone half-implemented the stub without
  finishing it) is a hard failure here, not a silent xpass.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import inisidecar as ini  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]

_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows file-attribute preservation only applies on win32",
)


# --------------------------------------------------------------------------
# Round-trip: oracle fixtures
# --------------------------------------------------------------------------

_ORACLE_INIS = sorted(
    _REPO.glob("fixtures/oracle/*/after/library/**/.picasa.ini"))


def test_oracle_fixtures_present_or_skip():
    if not _ORACLE_INIS:
        pytest.skip(
            "no fixtures/oracle/*/after/library/**/.picasa.ini found")


@pytest.mark.parametrize(
    "ini_path", _ORACLE_INIS,
    ids=[str(p.relative_to(_REPO)) for p in _ORACLE_INIS])
def test_round_trip_oracle_fixture(ini_path: Path):
    raw = ini_path.read_bytes()
    doc = ini.IniDocument.from_bytes(raw)
    assert doc.to_bytes() == raw


# --------------------------------------------------------------------------
# Round-trip: synthetic cases
# --------------------------------------------------------------------------

SYNTHETIC_ROUND_TRIP_CASES = {
    "crlf": b"[a]\r\nk=v\r\n",
    "lf": b"[a]\nk=v\n",
    "mixed_eol": b"[a]\r\nk=v\n[b]\r\nk2=v2\r\n",
    "no_trailing_eol": b"[a]\r\nk=v",
    "bom_plus_utf8": b"\xef\xbb\xbf[a]\r\nk=v\r\n",
    "utf8_marker_with_stray_byte": (
        b"[encoding]\r\nutf8=1\r\n[a]\r\ncaption=caf\xe9\r\n"),
    "cp1252_legacy": b"[a]\r\ncaption=Stra\xdfe\r\n",
    "null_section": b"[(null)]\r\nfoo=bar\r\n",
    "byte_reversed_junk_line": b"[a]\r\n=oof\r\nk=v\r\n",
    "duplicate_sections_and_keys": (
        b"[a]\r\nk=1\r\n[a]\r\nk=2\r\nk=3\r\n"),
    "blank_lines_between_sections": (
        b"[a]\r\n\r\nk=v\r\n\r\n[b]\r\nk=v\r\n"),
}


@pytest.mark.parametrize(
    "raw", list(SYNTHETIC_ROUND_TRIP_CASES.values()),
    ids=list(SYNTHETIC_ROUND_TRIP_CASES.keys()))
def test_round_trip_synthetic(raw: bytes):
    doc = ini.IniDocument.from_bytes(raw)
    assert doc.to_bytes() == raw


def test_round_trip_records_expected_encoding_branch():
    utf8_doc = ini.IniDocument.from_bytes(b"[a]\r\nk=v\r\n")
    assert utf8_doc.encoding == "utf-8"
    marker_doc = ini.IniDocument.from_bytes(
        b"[encoding]\r\nutf8=1\r\n[a]\r\ncaption=caf\xe9\r\n")
    assert marker_doc.encoding == "utf-8-surrogateescape"
    cp1252_doc = ini.IniDocument.from_bytes(b"[a]\r\ncaption=Stra\xdfe\r\n")
    assert cp1252_doc.encoding == "cp1252"
    bom_doc = ini.IniDocument.from_bytes(b"\xef\xbb\xbf[a]\r\nk=v\r\n")
    assert bom_doc.bom is True


# --------------------------------------------------------------------------
# IniDocument.apply
# --------------------------------------------------------------------------

def test_apply_set_existing_key_only_that_line_changes():
    raw = b"[a.jpg]\r\nstar=yes\r\ncaption=hi\r\n"
    doc = ini.IniDocument.from_bytes(raw)
    changed = doc.apply([ini.IniEdit("a.jpg", "star", "no")])
    assert changed == [("a.jpg", "star")]
    assert doc.to_bytes() == b"[a.jpg]\r\nstar=no\r\ncaption=hi\r\n"


def test_apply_rewrite_keeps_the_line_own_eol_in_a_mixed_eol_file():
    # Mostly-CRLF file with one bare-LF line (majority eol is still
    # "\r\n"); rewriting the LF line's key must change ONLY its value
    # bytes -- not its eol to the document's majority style, and not any
    # other line's bytes (design §2: a rewrite is a minimal byte change).
    raw = b"[a.jpg]\r\nstar=yes\ncaption=hi\r\n[b.jpg]\r\nstar=yes\r\n"
    doc = ini.IniDocument.from_bytes(raw)
    assert doc.eol == "\r\n"  # majority is still CRLF
    [star_line] = [ln for ln in doc.lines if ln.kind == "pair" and ln.key == "star"
                   and ln.value == "yes" and ln.eol == "\n"]
    assert star_line is not None  # sanity: the LF line is the one we target

    doc.apply([ini.IniEdit("a.jpg", "star", "no")])
    out = doc.to_bytes()
    assert out == b"[a.jpg]\r\nstar=no\ncaption=hi\r\n[b.jpg]\r\nstar=yes\r\n"
    [rewritten] = [ln for ln in doc.lines if ln.kind == "pair" and ln.key == "star"
                   and ln.value == "no"]
    assert rewritten.eol == "\n"  # kept its own original eol, not self.eol


def test_apply_set_new_key_inserted_before_trailing_blank():
    raw = b"[a.jpg]\r\nstar=yes\r\n\r\n[b.jpg]\r\nstar=yes\r\n"
    doc = ini.IniDocument.from_bytes(raw)
    doc.apply([ini.IniEdit("a.jpg", "caption", "hi")])
    assert doc.to_bytes() == (
        b"[a.jpg]\r\nstar=yes\r\ncaption=hi\r\n\r\n[b.jpg]\r\nstar=yes\r\n")


def test_apply_set_new_section_appended():
    raw = b"[a.jpg]\r\nstar=yes\r\n"
    doc = ini.IniDocument.from_bytes(raw)
    changed = doc.apply([ini.IniEdit("b.jpg", "star", "yes")])
    assert changed == [("b.jpg", "star")]
    assert doc.to_bytes() == b"[a.jpg]\r\nstar=yes\r\n[b.jpg]\r\nstar=yes\r\n"


def test_apply_set_duplicate_sections_and_keys_rewrites_all():
    raw = b"[a.jpg]\r\nk=1\r\n[a.jpg]\r\nk=2\r\nk=3\r\n"
    doc = ini.IniDocument.from_bytes(raw)
    doc.apply([ini.IniEdit("a.jpg", "k", "9")])
    assert doc.to_bytes() == b"[a.jpg]\r\nk=9\r\n[a.jpg]\r\nk=9\r\nk=9\r\n"


def test_apply_remove_deletes_all_occurrences_leaves_header():
    raw = b"[a.jpg]\r\nk=1\r\n[a.jpg]\r\nk=2\r\nother=stay\r\n"
    doc = ini.IniDocument.from_bytes(raw)
    changed = doc.apply([ini.IniEdit("a.jpg", "k", None)])
    assert changed == [("a.jpg", "k")]
    assert doc.to_bytes() == b"[a.jpg]\r\n[a.jpg]\r\nother=stay\r\n"


def test_apply_remove_nothing_found_reports_no_change():
    raw = b"[a.jpg]\r\nstar=yes\r\n"
    doc = ini.IniDocument.from_bytes(raw)
    changed = doc.apply([ini.IniEdit("a.jpg", "caption", None)])
    assert changed == []
    assert doc.to_bytes() == raw


def test_apply_no_trailing_eol_gets_one_added_before_append():
    raw = b"[a.jpg]\r\nstar=yes"
    doc = ini.IniDocument.from_bytes(raw)
    assert doc.to_bytes() == raw  # sanity: unmodified round-trip first
    assert doc.appended_eol is False
    doc.apply([ini.IniEdit("b.jpg", "star", "yes")])
    assert doc.appended_eol is True
    assert doc.to_bytes() == (
        b"[a.jpg]\r\nstar=yes\r\n[b.jpg]\r\nstar=yes\r\n")


def test_apply_value_verbatim_with_equals_sign():
    raw = b"[a.jpg]\r\nk=v\r\n"
    doc = ini.IniDocument.from_bytes(raw)
    doc.apply([
        ini.IniEdit("a.jpg", "filters", "BRIGHTNESS=1.0,CONTRAST=2.0")])
    out = doc.to_bytes()
    assert b"filters=BRIGHTNESS=1.0,CONTRAST=2.0\r\n" in out
    # The written value survives an independent re-parse unmangled too.
    reparsed = ini.IniDocument.from_bytes(out)
    [line] = [ln for ln in reparsed.lines
              if ln.kind == "pair" and ln.key == "filters"]
    assert line.value == "BRIGHTNESS=1.0,CONTRAST=2.0"


# --------------------------------------------------------------------------
# IniEdit.validate
# --------------------------------------------------------------------------

_INVALID_EDITS = [
    pytest.param(ini.IniEdit("a", "k", "bad\rvalue"), id="value-cr"),
    pytest.param(ini.IniEdit("a", "k", "bad\nvalue"), id="value-lf"),
    pytest.param(ini.IniEdit("a", "", "v"), id="key-empty"),
    pytest.param(ini.IniEdit("a", "k=x", "v"), id="key-equals"),
    pytest.param(ini.IniEdit("a", "k\r", "v"), id="key-cr"),
    pytest.param(ini.IniEdit("a", "k\n", "v"), id="key-lf"),
    pytest.param(ini.IniEdit("a", "[k", "v"), id="key-leading-bracket"),
    pytest.param(ini.IniEdit("", "k", "v"), id="section-empty"),
    pytest.param(ini.IniEdit("a]", "k", "v"), id="section-bracket"),
    pytest.param(ini.IniEdit("a\r", "k", "v"), id="section-cr"),
    pytest.param(ini.IniEdit("a\n", "k", "v"), id="section-lf"),
]


@pytest.mark.parametrize("edit", _INVALID_EDITS)
def test_ini_edit_validate_rejects_bad_input(edit: ini.IniEdit):
    with pytest.raises(ini.IniWriteError) as excinfo:
        edit.validate()
    assert excinfo.value.kind == "value"


def test_ini_edit_validate_accepts_well_formed_edits():
    ini.IniEdit("a.jpg", "star", "yes").validate()
    ini.IniEdit("a.jpg", "caption", None).validate()  # remove is fine
    ini.IniEdit("a.jpg", "filters", "K=V,OTHER=1").validate()  # "=" in value ok


# --------------------------------------------------------------------------
# write_edits and friends: xfail placeholders (design §4, lgg.4)
# --------------------------------------------------------------------------

def _make_readonly(path: Path) -> None:
    if sys.platform == "win32":
        FILE_ATTRIBUTE_READONLY = 0x1
        ctypes.windll.kernel32.SetFileAttributesW(str(path),
                                                    FILE_ATTRIBUTE_READONLY)
    else:
        os.chmod(path, 0o444)


def _restore_writable(path: Path) -> None:
    if sys.platform == "win32":
        FILE_ATTRIBUTE_NORMAL = 0x80
        ctypes.windll.kernel32.SetFileAttributesW(str(path),
                                                    FILE_ATTRIBUTE_NORMAL)
    else:
        os.chmod(path, 0o644)


@pytest.mark.xfail(strict=True, raises=NotImplementedError)
def test_write_edits_happy_path_returns_matching_sig_and_bytes(tmp_path):
    path = tmp_path / ".picasa.ini"
    path.write_bytes(b"[a.jpg]\r\nstar=yes\r\n")
    result = ini.write_edits(tmp_path, [ini.IniEdit("a.jpg", "caption", "hi")])
    st = path.stat()
    assert result.sig == (st.st_size, int(st.st_mtime))
    assert result.path == path
    assert result.created is False
    on_disk = path.read_bytes()
    doc = ini.IniDocument.from_bytes(on_disk)
    assert on_disk == doc.to_bytes()
    assert result.bytes_written == len(on_disk)


@pytest.mark.xfail(strict=True, raises=NotImplementedError)
def test_write_edits_refuses_on_readonly(tmp_path):
    path = tmp_path / ".picasa.ini"
    path.write_bytes(b"[a.jpg]\r\nstar=yes\r\n")
    _make_readonly(path)
    try:
        with pytest.raises(ini.IniWriteError) as excinfo:
            ini.write_edits(tmp_path, [ini.IniEdit("a.jpg", "star", "no")])
        assert excinfo.value.kind == "readonly"
    finally:
        _restore_writable(path)


@pytest.mark.xfail(strict=True, raises=NotImplementedError)
def test_write_edits_refuses_on_drift(tmp_path):
    path = tmp_path / ".picasa.ini"
    path.write_bytes(b"[a.jpg]\r\nstar=yes\r\n")
    stale_sig = (0, 0)
    with pytest.raises(ini.IniWriteError) as excinfo:
        ini.write_edits(tmp_path, [ini.IniEdit("a.jpg", "star", "no")],
                         expected_sig=stale_sig)
    assert excinfo.value.kind == "drift"


@pytest.mark.xfail(strict=True, raises=NotImplementedError)
def test_write_edits_cleans_up_temp_file_on_failure(tmp_path, monkeypatch):
    path = tmp_path / ".picasa.ini"
    path.write_bytes(b"[a.jpg]\r\nstar=yes\r\n")

    def _boom(*args, **kwargs):
        raise OSError("synthetic failure injected by the test")

    monkeypatch.setattr(ini, "_verify", _boom)
    with pytest.raises(ini.IniWriteError):
        ini.write_edits(tmp_path, [ini.IniEdit("a.jpg", "star", "no")])
    assert list(tmp_path.glob(".picasa.ini.*.tmp")) == []


@pytest.mark.xfail(strict=True, raises=NotImplementedError)
@pytest.mark.parametrize("value,expect_encoding_section", [
    ("summit", False),
    ("café", True),
])
def test_write_edits_new_file_encoding_section(tmp_path, value,
                                                expect_encoding_section):
    result = ini.write_edits(tmp_path, [ini.IniEdit("a.jpg", "caption", value)])
    assert result.created is True
    text = (tmp_path / ".picasa.ini").read_bytes()
    assert (b"[encoding]" in text) == expect_encoding_section


@_WINDOWS_ONLY
@pytest.mark.xfail(strict=True, raises=NotImplementedError)
def test_write_edits_preserves_windows_hidden_system_attributes(tmp_path):
    FILE_ATTRIBUTE_HIDDEN = 0x2
    FILE_ATTRIBUTE_SYSTEM = 0x4
    path = tmp_path / ".picasa.ini"
    path.write_bytes(b"[a.jpg]\r\nstar=yes\r\n")
    ctypes.windll.kernel32.SetFileAttributesW(
        str(path), FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
    ini.write_edits(tmp_path, [ini.IniEdit("a.jpg", "star", "no")])
    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    assert attrs & FILE_ATTRIBUTE_HIDDEN
    assert attrs & FILE_ATTRIBUTE_SYSTEM


@pytest.mark.xfail(strict=True, raises=NotImplementedError)
def test_write_edits_upgrades_cp1252_to_utf8_when_value_not_encodable(tmp_path):
    path = tmp_path / ".picasa.ini"
    path.write_bytes(b"[a.jpg]\r\ncaption=Stra\xdfe\r\n")
    result = ini.write_edits(
        tmp_path, [ini.IniEdit("a.jpg", "caption", "写真")])
    assert result.upgraded_encoding is True
    text = path.read_bytes()
    assert b"[encoding]" in text
    assert b"utf8=1" in text


@pytest.mark.xfail(strict=True, raises=NotImplementedError)
def test_write_edits_verify_failure_surfaces_as_kind_verify(tmp_path, monkeypatch):
    path = tmp_path / ".picasa.ini"
    path.write_bytes(b"[a.jpg]\r\nstar=yes\r\n")

    def _corrupt_verify(*args, **kwargs):
        raise ini.IniWriteError("verify", "synthetic corruption injected by the test")

    monkeypatch.setattr(ini, "_verify", _corrupt_verify)
    with pytest.raises(ini.IniWriteError) as excinfo:
        ini.write_edits(tmp_path, [ini.IniEdit("a.jpg", "star", "no")])
    assert excinfo.value.kind == "verify"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"] + sys.argv[1:]))
