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
sys.path.insert(0, str(_REPO / "scripts"))

import picasa_db  # noqa: E402

_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows file-attribute preservation only applies on win32",
)


# --------------------------------------------------------------------------
# Round-trip: oracle fixtures
# --------------------------------------------------------------------------

# Every INI_NAMES variant (.picasa.ini, Picasa.ini, picasa.ini), under
# both before/ and after/ library trees -- not just the after/.picasa.ini
# slice a first pass might reach for.
_ORACLE_INIS = sorted({
    p
    for tree in ("before", "after")
    for name in ini.INI_NAMES
    for p in _REPO.glob(f"fixtures/oracle/*/{tree}/library/**/{name}")
})


def test_oracle_fixtures_present():
    # SKIP only when fixtures/oracle/ itself is absent (e.g. a sparse
    # checkout); FAIL when the directory exists but the glob found
    # nothing -- that is a glob or fixture-layout regression, not an
    # absent-fixtures situation, and must not silently vanish as zero
    # collected parametrize cases.
    oracle_dir = _REPO / "fixtures" / "oracle"
    if not oracle_dir.is_dir():
        pytest.skip(f"{oracle_dir} not present in this checkout")
    assert _ORACLE_INIS, (
        f"{oracle_dir} exists but no {{.picasa.ini,Picasa.ini,picasa.ini}} "
        "under before/ or after/ library trees matched")


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
    "lone_cr_at_eof": b"[a.jpg]\r\nstar=yes\r",
    "cr_only_file": b"[a]\rfoo\rbar\r",
    "bom_only_file": b"\xef\xbb\xbf",
    "empty_file": b"",
    "cp1252_undefined_byte_no_marker": b"[a]\r\nc=Stra\xdfe\r\nd=\x81\r\n",
}


@pytest.mark.parametrize(
    "raw", list(SYNTHETIC_ROUND_TRIP_CASES.values()),
    ids=list(SYNTHETIC_ROUND_TRIP_CASES.keys()))
def test_round_trip_synthetic(raw: bytes):
    doc = ini.IniDocument.from_bytes(raw)
    assert doc.to_bytes() == raw


def test_round_trip_records_expected_encoding_branch():
    # Four distinct tags, not three: the cp1252 branch's own inner
    # fallback (a byte cp1252 itself rejects) must NOT share a tag with
    # the utf8=1-marked branch, even though both decode via the same
    # utf-8+surrogateescape codec (design §3, Opus review finding 2).
    utf8_doc = ini.IniDocument.from_bytes(b"[a]\r\nk=v\r\n")
    assert utf8_doc.encoding == "utf-8"
    marker_doc = ini.IniDocument.from_bytes(
        b"[encoding]\r\nutf8=1\r\n[a]\r\ncaption=caf\xe9\r\n")
    assert marker_doc.encoding == "utf-8-marked"
    cp1252_doc = ini.IniDocument.from_bytes(b"[a]\r\ncaption=Stra\xdfe\r\n")
    assert cp1252_doc.encoding == "cp1252"
    # The fourth branch: no [encoding] marker AND cp1252 itself rejects a
    # byte (0x81 is undefined in cp1252) -- falls back to utf-8+
    # surrogateescape same as the marked branch, but for a different
    # reason, so it gets its own tag.
    legacy_doc = ini.IniDocument.from_bytes(
        b"[a]\r\nc=Stra\xdfe\r\nd=\x81\r\n")
    assert legacy_doc.encoding == "legacy-surrogateescape"
    assert legacy_doc.to_bytes() == b"[a]\r\nc=Stra\xdfe\r\nd=\x81\r\n"
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
                   and ln.value == "yes" and ln.eol == "\n"]  # sanity: exists

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


def test_apply_set_duplicate_sections_key_missing_in_every_run_inserts_in_each():
    # NEITHER run carries the key, so both take the insert path (pass 2)
    # -- every run of the name ends up carrying key=value, not just the
    # last one (Opus review finding 4). The mixed rewrite-in-one-run +
    # insert-in-the-other case is the next test.
    raw = b"[a.jpg]\r\nx=1\r\n[a.jpg]\r\ny=2\r\n"
    doc = ini.IniDocument.from_bytes(raw)
    changed = doc.apply([ini.IniEdit("a.jpg", "star", "yes")])
    assert changed == [("a.jpg", "star")]
    assert doc.to_bytes() == (
        b"[a.jpg]\r\nx=1\r\nstar=yes\r\n[a.jpg]\r\ny=2\r\nstar=yes\r\n")


def test_apply_set_duplicate_sections_rewrites_one_run_and_inserts_in_other():
    # Both mechanisms in ONE apply() call: run 1 already has `star` and
    # gets it REWRITTEN (pass 1, in place), run 2 lacks it and gets it
    # INSERTED (pass 2) -- the pass-1/pass-2 interaction behind the
    # insertion rule, which the all-insert fixture above never reaches.
    raw = b"[a.jpg]\r\nstar=yes\r\n[a.jpg]\r\ny=2\r\n"
    doc = ini.IniDocument.from_bytes(raw)
    changed = doc.apply([ini.IniEdit("a.jpg", "star", "no")])
    assert changed == [("a.jpg", "star")]
    assert doc.to_bytes() == (
        b"[a.jpg]\r\nstar=no\r\n[a.jpg]\r\ny=2\r\nstar=no\r\n")
    # Asserted separately, so a regression that turned the rewrite into a
    # second insertion (or vice versa) cannot hide behind the byte
    # comparison: exactly one line was added, and both runs now resolve
    # `star` to the new value for a first-wins reader as well as a
    # last-wins one.
    assert len(doc.lines) == len(ini.IniDocument.from_bytes(raw).lines) + 1
    assert [ln.value for ln in doc.lines
            if ln.kind == "pair" and ln.key == "star"] == ["no", "no"]


def test_apply_set_no_op_when_value_already_matches():
    # Setting a value that is already set everywhere reports no change
    # and leaves every byte untouched (Opus review finding 5).
    raw = b"[a.jpg]\r\nstar=yes\r\n"
    doc = ini.IniDocument.from_bytes(raw)
    changed = doc.apply([ini.IniEdit("a.jpg", "star", "yes")])
    assert changed == []
    assert doc.to_bytes() == raw


def test_apply_set_preserves_original_key_spelling_and_whitespace():
    # A rewrite touches only the bytes at/after the first "=" (the
    # reader's own definition of "value"); the key's original spelling
    # and surrounding whitespace survive untouched (Opus review finding 3).
    raw = b"[a.jpg]\r\n  Star = yes \r\n"
    doc = ini.IniDocument.from_bytes(raw)
    changed = doc.apply([ini.IniEdit("a.jpg", "star", "no")])
    assert changed == [("a.jpg", "star")]
    assert doc.to_bytes() == b"[a.jpg]\r\n  Star =no\r\n"


def test_apply_set_on_lf_file():
    raw = b"[a.jpg]\nstar=yes\ncaption=hi\n"
    doc = ini.IniDocument.from_bytes(raw)
    assert doc.eol == "\n"
    doc.apply([ini.IniEdit("a.jpg", "star", "no")])
    assert doc.to_bytes() == b"[a.jpg]\nstar=no\ncaption=hi\n"


def test_apply_set_new_key_and_new_section_after_lone_cr_at_eof():
    # A file whose last line ends in a bare "\r" (no "\n") is NOT
    # terminated as far as read_picasa_ini is concerned (it splits only
    # on "\n"); appending after it must not glue the new line onto the
    # old value (Opus review finding 1).
    raw = b"[a.jpg]\r\nstar=yes\r"

    doc = ini.IniDocument.from_bytes(raw)
    assert doc.to_bytes() == raw  # sanity: unmodified round-trip first
    doc.apply([ini.IniEdit("a.jpg", "caption", "hi")])
    out = doc.to_bytes()
    assert out == b"[a.jpg]\r\nstar=yes\r\ncaption=hi\r\n"

    doc2 = ini.IniDocument.from_bytes(raw)
    doc2.apply([ini.IniEdit("b.jpg", "star", "yes")])
    out2 = doc2.to_bytes()
    assert out2 == b"[a.jpg]\r\nstar=yes\r\n[b.jpg]\r\nstar=yes\r\n"


def test_apply_after_lone_cr_at_eof_in_an_lf_file_keeps_the_cr_byte():
    # LF-majority twin of the CRLF case above, the one the old
    # `_ensure_trailing_eol` got wrong: with `doc.eol == "\n"`, assigning
    # the majority EOL to a last line whose own eol is a bare "\r"
    # REPLACED the file's final 0x0D instead of adding to it. Adding the
    # missing "\n" keeps every original byte, which is what design §4
    # step 4 allows (the trailing EOL is an ADDED byte, never a swapped
    # one).
    raw = b"[a]\nk=v\r"
    doc = ini.IniDocument.from_bytes(raw)
    assert doc.eol == "\n"  # majority is LF, and the lone CR is not one
    assert doc.to_bytes() == raw  # sanity: unmodified round-trip first
    doc.apply([ini.IniEdit("a", "caption", "hi")])
    out = doc.to_bytes()
    assert doc.appended_eol is True
    assert out == b"[a]\nk=v\r\ncaption=hi\n"
    # The strongest form of the invariant: the original bytes survive
    # untouched and in order, with the write only appending after them.
    assert out.startswith(raw)


def test_apply_lone_cr_at_eof_reparses_cleanly_via_the_reader(tmp_path):
    # The bytes inisidecar.py produces must actually parse correctly
    # through read_picasa_ini, not merely look right -- this is the
    # regression the Opus review caught: the OLD code produced
    # b"...star=yes\rcaption=hi\r\n" which the reader parsed as
    # star="yes\rcaption=hi" with zero anomalies (silently wrong, not a
    # crash).
    raw = b"[a.jpg]\r\nstar=yes\r"
    doc = ini.IniDocument.from_bytes(raw)
    doc.apply([ini.IniEdit("a.jpg", "caption", "hi")])
    path = tmp_path / ".picasa.ini"
    path.write_bytes(doc.to_bytes())
    parsed = picasa_db.read_picasa_ini(path)
    sec = parsed.section("a.jpg")
    assert sec is not None
    assert sec.get("star") == "yes"
    assert sec.get("caption") == "hi"
    assert parsed.anomalies == []


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
    pytest.param(ini.IniEdit("a", " star ", "v"), id="key-padded"),
    pytest.param(ini.IniEdit("a", "star\t", "v"), id="key-trailing-tab"),
    pytest.param(ini.IniEdit("a", " ", "v"), id="key-whitespace-only"),
    pytest.param(ini.IniEdit(" a ", "k", "v"), id="section-padded"),
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
    # Inner whitespace is data, not padding -- only the edges are refused.
    ini.IniEdit("a b.jpg", "my key", "v").validate()


def test_apply_refuses_padded_key_instead_of_inserting_a_shadowed_duplicate():
    # A padded key can never match the stripped key `classify_line`
    # stores for an existing `star=old` line, so accepting it would
    # insert a SECOND pair and leave a first-wins reader
    # (GetPrivateProfileString, picasa_db.IniSection.get) resolving
    # `star` to the OLD value -- an edit that silently never lands, and
    # that step 4 could only report late as a misleading kind="verify".
    # It is refused up front, and apply() validates BEFORE mutating, so
    # the document is left byte-identical.
    raw = b"[a.jpg]\r\nstar=old\r\n"
    doc = ini.IniDocument.from_bytes(raw)
    with pytest.raises(ini.IniWriteError) as excinfo:
        doc.apply([ini.IniEdit("a.jpg", " star ", "new")])
    assert excinfo.value.kind == "value"
    assert doc.to_bytes() == raw


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


# Root ignores the 0o444 mode bits `_make_readonly` sets on POSIX:
# `os.access(path, os.W_OK)` returns True for uid 0 and the write
# succeeds, so step 2's refusal cannot be provoked this way and
# pytest.raises would report DID NOT RAISE. Harmless while the xfail
# below masks everything, but containers run test suites as root often
# enough that the guard belongs here now, not in lgg.4. Windows has no
# geteuid and needs no guard (the READONLY attribute binds administrators
# too).
_SKIP_IF_ROOT = pytest.mark.skipif(
    getattr(os, "geteuid", lambda: -1)() == 0,
    reason="root bypasses the mode bits os.access(W_OK) checks",
)


@_SKIP_IF_ROOT
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
def test_write_edits_refuses_on_drift_raced_before_swap(tmp_path, monkeypatch):
    # design §4 step 6's race: something else rewrites the file in the
    # gap between our own read (step 3) and the swap (step 7). Hooks
    # `_before_swap`, the seam write_edits calls right before step 6's
    # signature re-check, rather than monkeypatching `_atomic_replace`
    # itself (Opus review finding 6b).
    path = tmp_path / ".picasa.ini"
    path.write_bytes(b"[a.jpg]\r\nstar=yes\r\n")

    def _race(raced_path):
        raced_path.write_bytes(b"[a.jpg]\r\nstar=yes\r\ncaption=raced\r\n")

    monkeypatch.setattr(ini, "_before_swap", _race)
    with pytest.raises(ini.IniWriteError) as excinfo:
        ini.write_edits(tmp_path, [ini.IniEdit("a.jpg", "star", "no")])
    assert excinfo.value.kind == "drift"


@pytest.mark.xfail(strict=True, raises=NotImplementedError)
def test_write_edits_cleans_up_temp_file_on_failure(tmp_path, monkeypatch):
    path = tmp_path / ".picasa.ini"
    path.write_bytes(b"[a.jpg]\r\nstar=yes\r\n")

    def _boom(raced_path):
        # Assert the temp file EXISTS before failing. `_before_swap` is
        # documented to fire after step 5 has written the temp
        # (inisidecar.py's `_before_swap` docstring), and without this
        # assertion an implementation that called the seam too early
        # would satisfy the empty-glob check below vacuously: "temp
        # created and then cleaned up" would be indistinguishable from
        # "temp never created at all".
        assert list(tmp_path.glob(".picasa.ini.*.tmp")), (
            "_before_swap fired before step 5 created the temp file")
        raise OSError("synthetic failure injected by the test")

    # Fails AFTER step 5 creates the temp file but BEFORE the swap (step
    # 7) -- the case that actually leaves a temp file to clean up; a
    # post-swap failure has no temp file left by definition (Opus review
    # note accompanying finding 6a).
    monkeypatch.setattr(ini, "_before_swap", _boom)
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
    # Monkeypatch _atomic_replace (not _verify/_read_back) so DIFFERENT
    # bytes actually land on disk than the ones write_edits computed --
    # the REAL post-swap _read_back must catch that itself by re-reading
    # the file, rather than the test just proving _verify raises when
    # told to (Opus review finding 6a: the old version of this test
    # proved nothing).
    path = tmp_path / ".picasa.ini"
    path.write_bytes(b"[a.jpg]\r\nstar=yes\r\n")

    def _write_wrong_bytes(target_path: Path, payload: bytes):
        corrupted = payload.replace(b"star=no", b"star=CORRUPTED")
        target_path.write_bytes(corrupted)
        st = target_path.stat()
        return (st.st_size, int(st.st_mtime))

    monkeypatch.setattr(ini, "_atomic_replace", _write_wrong_bytes)
    with pytest.raises(ini.IniWriteError) as excinfo:
        ini.write_edits(tmp_path, [ini.IniEdit("a.jpg", "star", "no")])
    assert excinfo.value.kind == "verify"


# --------------------------------------------------------------------------
# select_ini_variant vs. catalog._select_ini_variant parity
# --------------------------------------------------------------------------

def test_select_ini_variant_agrees_with_catalog(tmp_path):
    # inisidecar.select_ini_variant is a deliberate duplicate of
    # catalog._select_ini_variant (see the INI_NAMES comment in
    # inisidecar.py) -- the two must never drift apart. catalog.py is
    # Qt-free at import time (rawload.py/videoload.py defer their
    # PySide6 imports inside functions), so this imports cleanly under
    # this file's own pytest-only PEP 723 dependencies; if that ever
    # changes, skip rather than fail the whole suite.
    try:
        import catalog
    except ImportError as exc:
        pytest.skip(f"catalog.py not importable here: {exc}")

    # Each variant present alone. Folder names are index-based, not
    # derived from `name`: INI_NAMES's three spellings differ only by a
    # leading dot / letter case (".picasa.ini", "Picasa.ini",
    # "picasa.ini"), which collide on a case-insensitive filesystem
    # (Windows) if used directly as directory names.
    for i, name in enumerate(ini.INI_NAMES):
        folder = tmp_path / f"alone_{i}"
        folder.mkdir()
        (folder / name).write_bytes(b"[a]\r\nk=v\r\n")
        assert (ini.select_ini_variant(folder)
                == catalog._select_ini_variant(folder))

    # Two variants present together: INI_NAMES priority order must pick
    # the same one on both sides.
    both = tmp_path / "two_together"
    both.mkdir()
    (both / ini.INI_NAMES[0]).write_bytes(b"[a]\r\nk=v\r\n")
    (both / ini.INI_NAMES[1]).write_bytes(b"[a]\r\nk=v\r\n")
    assert (ini.select_ini_variant(both)
            == catalog._select_ini_variant(both))

    # An unreadable first candidate -- cheap via chmod on POSIX only;
    # Windows needs a heavier ACL dance not worth it for this parity
    # check, so skip there.
    if sys.platform != "win32":
        unreadable = tmp_path / "unreadable_first"
        unreadable.mkdir()
        first = unreadable / ini.INI_NAMES[0]
        first.write_bytes(b"[a]\r\nk=v\r\n")
        second = unreadable / ini.INI_NAMES[1]
        second.write_bytes(b"[a]\r\nk=v\r\n")
        os.chmod(first, 0o000)
        try:
            assert (ini.select_ini_variant(unreadable)
                    == catalog._select_ini_variant(unreadable))
        finally:
            os.chmod(first, 0o644)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"] + sys.argv[1:]))
