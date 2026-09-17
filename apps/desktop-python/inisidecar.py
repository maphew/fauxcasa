"""Sidecar-first `.picasa.ini` write layer (fauxcasa-lgg.1).

`inisidecar.py` is pure stdlib, no Qt, no catalog import. It knows how to
read, edit and verify one ini file; it does not know what a star is. The
feature writers (lgg.2, lgg.5, lgg.7) map a user action to a list of
`IniEdit`s; `librarystate.py` (lgg.3) is the only intended caller of
`write_edits` and sequences it with the journal and the in-memory catalog
update. Full design: `docs/design/ini-write-layer.md`.

The one-path rule (design note, plain-language summary): every action
that changes a `.picasa.ini` goes through this module's `write_edits`,
which (1) touches only the lines it means to change and verifies that by
re-reading the file, (2) writes all-or-nothing via a temp file + atomic
swap, (3) refuses instead of clobbering when the file drifted since it
was last read, and (4) refuses instead of silently no-op'ing when the
file is read-only. There is no second way to write a `.picasa.ini` in
this codebase; a future writer must extend this module, not bypass it.

`read_picasa_ini` (`scripts/picasa_db.py`) stays the reader for ingest
and for this module's own verification step, but it cannot be the
writer's document model: it drops blank lines, strips CRLF without
recording it, and keeps junk lines only as line-number anomalies. This
module instead keeps a `list[Line]`, one entry per physical line, each
holding its own end-of-line bytes, so unmodified lines round-trip
byte-for-byte and only the lines an edit actually touches change.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts"))

import picasa_db  # noqa: E402


# INI_NAMES is a deliberate local duplicate of catalog.py:134's constant
# of the same name (and select_ini_variant below duplicates
# catalog._select_ini_variant, catalog.py:648-676), NOT an import from
# catalog.py: the module docstring's "no catalog import" is load-bearing
# here -- catalog.py sits above this module in the dependency direction
# design §1 draws (librarystate calls both catalog and inisidecar;
# inisidecar must not call back up into catalog). catalog.py can switch
# to this copy in lgg.4 once the two are proven identical by the shared
# oracle fixtures in test_inisidecar.py.
INI_NAMES = (".picasa.ini", "Picasa.ini", "picasa.ini")


def select_ini_variant(folder: Path) -> Path | None:
    """Pick whichever INI_NAMES variant `folder` carries, the same way
    catalog._select_ini_variant does (catalog.py:648): the first name
    that both stats as a file AND opens for read without an OSError, not
    just the first that stats -- an exists-but-permission-denied earlier
    candidate must not shadow a later, actually-readable one. See
    write_edits (design §4 step 1) for why the writer needs this same
    selection the reader uses."""
    for name in INI_NAMES:
        p = folder / name
        if not p.is_file():
            continue
        try:
            with open(p, "rb"):
                pass
        except OSError:
            continue
        return p
    return None


class IniWriteError(Exception):
    """Raised for every refusal on the write path (design §4, §8).

    `kind` is one of "value", "readonly", "drift", "encoding", "verify",
    "io" -- the vocabulary librarystate's per-folder health mark uses
    (design §8). `detail` is safe to show in the UI and to log: paths,
    kinds and counts only, NEVER a value string -- captions, keywords and
    other user text must never end up in an exception message or a log
    line (privacy rule for real libraries).
    """

    def __init__(self, kind: str, detail: str, path: Path | None = None):
        self.kind = kind
        self.path = path
        self.detail = detail
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


@dataclass(frozen=True)
class IniEdit:
    """One edit to one `key=` in one `[section]`. `value=None` means
    remove; any other value means set (design §2)."""

    section: str
    key: str
    value: str | None  # None = remove

    def validate(self) -> None:
        """Refuse anything that could corrupt the line grammar or be
        misread by Picasa/GetPrivateProfileString, before any file is
        touched (design §2 "Values are literal")."""
        if self.value is not None and ("\r" in self.value or "\n" in self.value):
            raise IniWriteError("value", "value contains a line break")
        if (not self.key or "=" in self.key or "\r" in self.key
                or "\n" in self.key or self.key.startswith("[")):
            raise IniWriteError(
                "value",
                "key is empty, contains '=' or a line break, or starts with '['")
        # Surrounding whitespace is REFUSED, not silently stripped:
        # `classify_line` stores every existing pair's key stripped, while
        # `_set` compares the edit's key as given, so a padded key
        # (" star ") matches no existing line -- pass 2 would insert a
        # SECOND `" star =v"` pair, and a first-wins reader
        # (`picasa_db.IniSection.get`, Windows GetPrivateProfileString)
        # would keep resolving `star` to the OLD value. That shadowed
        # duplicate is exactly the reader disagreement design §2's
        # "edited everywhere" rule exists to prevent, and step 4 would
        # catch it only late, as a misleading `kind="verify"`. A
        # whitespace-only key (" ") is worse: it is truthy, so the
        # emptiness check above passes it, and the inserted `" =v"` line
        # re-parses with key "" and can never match anything.
        if self.key != self.key.strip():
            raise IniWriteError(
                "value", "key has leading or trailing whitespace")
        if (not self.section or "]" in self.section
                or "\r" in self.section or "\n" in self.section):
            raise IniWriteError(
                "value",
                "section is empty or contains ']' or a line break")
        # The same shadowing argument one level up: `_set` compares the
        # edit's section against each header Line's own `name`, so a
        # padded edit section ("a.jpg ") matches no existing `[a.jpg]`
        # header and appends a duplicate SECTION instead of editing the
        # existing run. (The mirror case -- a FILE whose header is spelled
        # `[ a.jpg ]`, whose `name` keeps that padding and so never
        # matches a clean edit section -- is deliberately NOT handled
        # here: changing which existing lines a write rewrites is a design
        # §2 call for lgg.4, not one validation may make on its own.)
        if self.section != self.section.strip():
            raise IniWriteError(
                "value", "section has leading or trailing whitespace")


@dataclass(frozen=True)
class WriteResult:
    """What a successful `write_edits` call reports (design §4)."""

    path: Path
    sig: tuple[int, int]
    created: bool
    upgraded_encoding: bool
    bytes_written: int


@dataclass
class Line:
    """One physical line of an ini file: its content (EOL bytes already
    peeled off into `eol`) plus a classification using exactly
    `read_picasa_ini`'s own rules (picasa_db.py:704-719).

    `kind` is "header" (`[name]`), "pair" (`key=value`, split on the
    first "="), or "other" (blank, junk, byte-reversed garbage,
    `[(null)]`-style content that still parses as a header, anything
    without an "="). `name` is set for "header" lines; `key`/`value` are
    set for "pair" lines. An unmodified Line's `text + eol` reproduces
    its original bytes exactly; `IniDocument.apply` is the only thing
    that mutates a Line's fields on a real edit.
    """

    text: str
    eol: str
    kind: str  # "header" | "pair" | "other"
    name: str | None = None
    key: str | None = None
    value: str | None = None


def classify_line(text: str) -> Line:
    """Classify one physical line's content (its own EOL already
    stripped by the caller) using exactly `read_picasa_ini`'s rules: strip
    for blank/header detection, split on the FIRST "=" for pair lines
    (key stripped, value kept verbatim), everything else is "other". The
    returned Line's `.eol` is "" -- the caller (IniDocument.from_bytes)
    fills it in per physical line."""
    stripped = text.strip()
    if not stripped:
        return Line(text=text, eol="", kind="other")
    if stripped.startswith("[") and stripped.endswith("]"):
        return Line(text=text, eol="", kind="header", name=stripped[1:-1])
    key, sep, value = text.partition("=")
    if not sep:
        return Line(text=text, eol="", kind="other")
    return Line(text=text, eol="", kind="pair", key=key.strip(), value=value)


def _split_physical_lines(text: str) -> list[tuple[str, str]]:
    """Split decoded text into (content, eol) pairs using
    `read_picasa_ini`'s own line boundaries (`text.split("\\n")`, per-line
    trailing "\\r" run stripped) so every physical line's content matches
    what the reader would classify, while the stripped "\\r" run plus the
    "\\n" (when one followed) is preserved verbatim in `eol` -- the two
    together always reproduce the original bytes exactly. The very last
    physical line has no trailing "\\n" in `eol` when the file itself had
    none (the "no trailing EOL" case)."""
    if text == "":
        return []
    parts = text.split("\n")
    result: list[tuple[str, str]] = []
    if text.endswith("\n"):
        for part in parts[:-1]:
            stripped = part.rstrip("\r")
            num_cr = len(part) - len(stripped)
            result.append((stripped, "\r" * num_cr + "\n"))
    else:
        last = len(parts) - 1
        for i, part in enumerate(parts):
            stripped = part.rstrip("\r")
            num_cr = len(part) - len(stripped)
            eol = "\r" * num_cr + ("\n" if i < last else "")
            result.append((stripped, eol))
    return result


def _majority_eol(lines: list[Line]) -> str:
    """The file's majority EOL style, "\\r\\n" or "\\n"; "\\r\\n" is also
    the default for an empty/new file (design §2 "EOL style"), since
    every oracle fixture and Picasa itself writes CRLF."""
    crlf = sum(1 for ln in lines if ln.eol == "\r\n")
    lf = sum(1 for ln in lines if ln.eol == "\n")
    if crlf == 0 and lf == 0:
        return "\r\n"
    return "\r\n" if crlf >= lf else "\n"


@dataclass
class IniDocument:
    """The writer's document model: a `list[Line]` plus the encoding/BOM/
    EOL bookkeeping needed to re-emit every unmodified byte exactly
    (design §2, §3). `appended_eol` records whether `apply()` had to add
    an EOL to a previously-EOL-less last line in order to append after
    it (design §2 "A file whose last line has no EOL gets one added...");
    this is the one byte changed outside the edited lines themselves.

    `encoding` is one of FOUR tags, not three -- the reader's cp1252
    branch has its own inner fallback, and that fallback reaches the
    same codec (`utf-8` + `surrogateescape`) as the `utf8=1`-marked
    branch but for a completely different reason, so the two must not
    share a tag (design §3):
    - "utf-8": strict UTF-8 decode succeeded.
    - "utf-8-marked": UTF-8 failed, `[encoding] utf8=1` is present --
      the file is legitimately mixed-encoding; surrogateescape keeps
      every byte.
    - "cp1252": UTF-8 failed, no marker, cp1252 decoded cleanly.
    - "legacy-surrogateescape": UTF-8 failed, no marker, AND cp1252
      itself rejected at least one byte (one of the handful cp1252
      leaves undefined, e.g. 0x81/0x8D/0x8F/0x90/0x9D) -- this file's
      true encoding is unknown, not just "old cp1252"; `_upgrade_encoding`
      treats it much more conservatively than plain "cp1252" (see its
      docstring)."""

    lines: list[Line]
    encoding: str  # "utf-8" | "utf-8-marked" | "cp1252" | "legacy-surrogateescape"
    bom: bool
    eol: str  # majority EOL, "\r\n" default for empty/new (see _majority_eol)
    appended_eol: bool = False

    @classmethod
    def from_bytes(cls, raw: bytes) -> "IniDocument":
        """Decode `raw` with `read_picasa_ini`'s own three-way policy
        (picasa_db.py:686-697: strict UTF-8, else utf8=1-marked
        surrogateescape, else cp1252 falling back to surrogateescape) --
        four distinct OUTCOMES though, since the cp1252 branch's own
        inner fallback is tagged separately from the utf8=1-marked one
        (see the `IniDocument.encoding` field comment above) -- and split
        into classified physical lines. Reuses `picasa_db._has_utf8_marker`
        (rather than a second copy of its ASCII-safe `[encoding]` scan) so
        the two decode policies can never drift apart -- the same reuse
        rationale as `PicasaIni`/`IniSection` elsewhere in this file.
        `IniDocument.to_bytes()` is this method's exact inverse; that
        round-trip is the module's core invariant (design §9)."""
        bom = raw.startswith(b"\xef\xbb\xbf")
        if bom:
            raw = raw[3:]
        try:
            text = raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            if picasa_db._has_utf8_marker(raw):
                text = raw.decode("utf-8", "surrogateescape")
                encoding = "utf-8-marked"
            else:
                try:
                    text = raw.decode("cp1252")
                    encoding = "cp1252"
                except UnicodeDecodeError:
                    text = raw.decode("utf-8", "surrogateescape")
                    encoding = "legacy-surrogateescape"

        lines: list[Line] = []
        for content, eol in _split_physical_lines(text):
            line = classify_line(content)
            line.eol = eol
            lines.append(line)

        return cls(lines=lines, encoding=encoding, bom=bom,
                    eol=_majority_eol(lines))

    def to_bytes(self) -> bytes:
        """Re-encode every line with the codec `from_bytes` recorded and
        re-add the BOM if one was present. Must be the exact inverse of
        `from_bytes` for every input (design §9's round-trip invariant);
        an edit that changes a value the current codec cannot encode is
        `_upgrade_encoding`'s job (design §3), not this method's -- a
        plain `UnicodeEncodeError` here is a caller bug, not a value
        this method silently papers over."""
        text = "".join(ln.text + ln.eol for ln in self.lines)
        if self.encoding == "utf-8":
            raw = text.encode("utf-8")
        elif self.encoding in ("utf-8-marked", "legacy-surrogateescape"):
            # Both decoded via utf-8+surrogateescape (from_bytes), just
            # for different reasons -- same codec re-encodes both exactly.
            raw = text.encode("utf-8", "surrogateescape")
        elif self.encoding == "cp1252":
            raw = text.encode("cp1252")
        else:
            raise IniWriteError("io", f"unknown IniDocument.encoding {self.encoding!r}")
        if self.bom:
            raw = b"\xef\xbb\xbf" + raw
        return raw

    def _ensure_trailing_eol(self) -> None:
        """Before appending/inserting at the very end of `self.lines`,
        give a last line whose `eol` is not a real terminator one, so the
        new line does not get glued onto it with no separator (design
        §2's one exception to "we change only the lines we mean to
        change"). A terminator ends in "\\n"; a bare "" (no EOL at all)
        or a bare "\\r" run with no following "\\n" (a lone trailing CR --
        `read_picasa_ini` splits only on "\\n", so a lone CR is NOT a line
        break to the reader either) both need fixing, not just "" --
        checking `eol == ""` alone missed the lone-CR case and glued the
        next line's `key=value` directly onto the previous value.

        ADD-ONLY, never replace. A bare-CR run keeps its own byte(s) and
        gains only the missing "\\n" ("\\r" -> "\\r\\n"); it is NOT
        swapped for `self.eol`, which would DESTROY the original 0x0D in
        an LF-majority document (`b"[a]\\nk=v\\r"` would emit
        `b"[a]\\nk=v\\n..."`), and design §4 step 4 allows the trailing
        EOL only as an ADDED byte -- a REPLACED one becomes a
        `kind="verify"` refusal once lgg.4 lands `_check_payload`. Only
        the genuinely EOL-less case ("") has no original bytes to keep,
        so only it uses `self.eol`. In a CRLF-majority document the two
        rules coincide ("\\r" -> "\\r\\n" either way), which is why the
        CRLF-only lone-CR test never caught this."""
        if self.lines and not self.lines[-1].eol.endswith("\n"):
            last = self.lines[-1]
            last.eol = (last.eol + "\n") if last.eol else self.eol
            self.appended_eol = True

    def _set(self, section: str, key: str, value: str) -> bool:
        # Every run of a header matching `section`, in file order, as
        # (header_index, end_index_exclusive) -- `end` is the next
        # header's index (of ANY name) or len(self.lines). Captured
        # up front, before any mutation, so indices stay valid through
        # pass 1 (mutates Line objects in place, never the list) and
        # through pass 2 as long as that pass walks runs in REVERSE
        # order (an insertion into a later run can only shift indices
        # >= that run's own header index, which is always >= every
        # earlier run's `end`, by file order -- so earlier runs' indices
        # are never disturbed by a later run's insertion).
        header_indices = [i for i, ln in enumerate(self.lines)
                           if ln.kind == "header"]
        runs: list[tuple[int, int]] = []
        for pos, hi in enumerate(header_indices):
            ln = self.lines[hi]
            if ln.name is not None and ln.name.lower() == section.lower():
                end = (header_indices[pos + 1] if pos + 1 < len(header_indices)
                       else len(self.lines))
                runs.append((hi, end))

        if not runs:
            # Section absent entirely: append a brand new section at EOF
            # (design §2 "Placement").
            self._ensure_trailing_eol()
            self.lines.append(Line(text=f"[{section}]", eol=self.eol,
                                    kind="header", name=section))
            self.lines.append(Line(text=f"{key}={value}", eol=self.eol,
                                    kind="pair", key=key, value=value))
            return True

        changed = False

        # Pass 1: rewrite every matching key in every run (design §2
        # "Duplicate sections and duplicate keys are edited everywhere").
        # A rewrite keeps the line's OWN key spelling/whitespace and its
        # OWN eol -- only the value bytes change (mixed-EOL files change
        # no bytes outside the target line); only lines pass 2 ADDS use
        # the document's majority eol, since they have no original to
        # keep. A rewrite whose new text is byte-identical to the old
        # (setting a value that is already set) reports no change.
        runs_missing_key: list[tuple[int, int]] = []
        for hi, end in runs:
            found_in_run = False
            for i in range(hi + 1, end):
                ln = self.lines[i]
                if (ln.kind == "pair" and ln.key is not None
                        and ln.key.lower() == key.lower()):
                    head, sep, _ = ln.text.partition("=")
                    new_text = head + sep + value
                    if new_text != ln.text:
                        ln.text = new_text
                        ln.value = value
                        changed = True
                    found_in_run = True
            if not found_in_run:
                runs_missing_key.append((hi, end))

        # Pass 2: insert the key into every run that lacked it -- EVERY
        # run of the section's name ends up carrying key=value, not just
        # the last one (design §2 "edited everywhere" applies to
        # insertion, not only to rewriting existing occurrences).
        # Reverse order: see the indices note above.
        for hi, end in reversed(runs_missing_key):
            insert_at = hi + 1  # default: right after the header
            for i in range(hi + 1, end):
                if self.lines[i].kind == "pair":
                    insert_at = i + 1
            if insert_at == len(self.lines):
                self._ensure_trailing_eol()
            self.lines.insert(insert_at, Line(text=f"{key}={value}", eol=self.eol,
                                               kind="pair", key=key, value=value))
            changed = True

        return changed

    def _remove(self, section: str, key: str) -> bool:
        current: str | None = None
        kept: list[Line] = []
        removed_any = False
        for ln in self.lines:
            if ln.kind == "header":
                current = ln.name
                kept.append(ln)
                continue
            if (ln.kind == "pair" and current is not None
                    and current.lower() == section.lower()
                    and ln.key is not None and ln.key.lower() == key.lower()):
                removed_any = True
                continue  # drop this line; an empty section's header stays
            kept.append(ln)
        self.lines = kept
        return removed_any

    def apply(self, edits: Sequence[IniEdit]) -> list[tuple[str, str]]:
        """Apply every edit in order (design §2). `set` (value is not
        None) rewrites every matching key in every run of every matching
        section, inserts the key into any run of that section that
        lacked it, and appends a new section when the name is absent
        entirely -- EVERY run of the name ends up carrying key=value, not
        just the last one; `remove` (value is None) deletes every
        matching pair and leaves the header(s) behind. Returns the
        `(section, key)` pairs whose bytes actually changed -- a `set`
        whose value already matches every occurrence, or a `remove` that
        matched nothing, contributes no entry."""
        changed: list[tuple[str, str]] = []
        for edit in edits:
            edit.validate()
            if edit.value is None:
                did_change = self._remove(edit.section, edit.key)
            else:
                did_change = self._set(edit.section, edit.key, edit.value)
            if did_change:
                changed.append((edit.section, edit.key))
        return changed


def _before_swap(path: Path) -> None:
    """Test seam only, a no-op in production. `write_edits` calls this
    immediately before step 6's pre-swap signature re-check (right after
    the temp file from step 5 exists on disk). A test can monkeypatch
    this to rewrite `path` in place, simulating something else (Picasa,
    a sync client) writing during our own read-to-swap window, without
    needing to monkeypatch `_atomic_replace` itself -- exercises the same
    race step 6 exists to catch."""
    pass


def write_edits(folder: Path, edits: Sequence[IniEdit], *,
                 expected_sig: tuple[int, int] | None = None) -> WriteResult:
    """Design §4 steps 1-9, the writer's only public entry point.

    1. Pick the file via `select_ini_variant` (same INI_NAMES selection
       the reader uses); no ini at all targets `.picasa.ini` with
       `created=True`.
    2. Refuse read-only up front, `kind="readonly"` (`os.access(path,
       os.W_OK)` false for a Windows read-only attribute or a POSIX file
       without owner write; the folder must be writable too, for the
       temp file -- and for a to-be-created ini, only the folder is
       checked, since there is no `path` yet). A Windows DACL denial is
       not visible to `os.access` and instead surfaces at step 5 or 7 as
       `kind="io"`; the status mark (design §8) treats both kinds the
       same way.
    3. Stat before read (`stat_sig` order, catalog.py:724-736); if
       `expected_sig` is given and the fresh stat differs, refuse
       `kind="drift"` before reading further. `expected_sig=None` means
       the caller has no prior signature to compare against -- the
       journal-replay path (design §7): a journal entry is a desired end
       state, not a compare-and-swap (the line model already confines
       the write to our own keys, the spec's per-key last-writer-wins
       merge), and a crash between the ini write and the next
       `save_catalog` would otherwise leave a stale `ini_sigs` that
       refuses every replay forever. `None` skips ONLY this step-3 check;
       step 6 below still always runs, since it protects against a write
       racing OUR OWN read-to-swap window, not against a mismatch with
       the caller's belief.
    4. Decode, classify, apply (`IniDocument.from_bytes` + `.apply`); no
       file is touched yet. Run `_upgrade_encoding` first when an edit's
       value cannot be encoded by the document's current codec. Then
       `_check_payload(doc_before, doc_after, payload, edits)` checks the
       result IN MEMORY, still before any file is touched: every
       untouched line is byte-identical between `doc_before` and
       `doc_after`, every edit landed (present with its new value in
       every section of that name for a `set`, absent for a `remove`,
       re-parsed via `picasa_db.read_picasa_ini` on the in-memory
       `payload`), and the anomaly count did not grow. A mismatch is
       `kind="verify"` -- raised before step 5, so there is no temp file
       to clean up for this failure. **No-op fast path**: if `payload ==`
       the original bytes read at step 3 (design §2 "No-op writes are
       free"), return success now -- `created=False`,
       `upgraded_encoding=False`, `sig` the step-3 stat unchanged -- with
       no temp file, no mtime touch, and no `ini_sigs`/journal-health
       change for the caller to make beyond marking the entry applied.
    5. Write the temp file via `_atomic_replace`'s temp-write half:
       `<name>.<pid>.tmp` in the same directory, flush, `os.fsync`.
    6. Call `_before_swap(path)` (a no-op in production, a test seam),
       then re-check the file against step 3's read: `os.stat(path).
       st_mtime_ns` and size, not the coarser whole-second `stat_sig`
       tuple step 3 itself used (a same-second rewrite needs
       nanosecond resolution to catch), and when THOSE match too, a byte
       compare against the bytes step 3/4 actually read (belt-and-braces
       for a filesystem whose mtime resolution is coarser than a
       nanosecond in practice). A change on either check means someone
       wrote in the last few milliseconds: remove the temp, refuse with
       `kind="drift"`. The window between this check and the swap is
       accepted under the spec's alternation protocol (§3 Concurrency,
       `product-spec.md:330-340`).
    7. Swap via `_atomic_replace`'s `os.replace` half, preserving Windows
       attributes and fsync'ing the parent directory on POSIX.
    8. `_read_back(path, payload)`: read the bytes actually on disk and
       compare them to `payload` byte-for-byte -- ONLY a byte comparison,
       since step 4's `_check_payload` already established the edits are
       semantically correct on these same bytes; a mismatch here means
       the swap/write itself corrupted bytes (a partial write despite
       fsync, a filesystem oddity), still `kind="verify"`.
    9. Any `OSError` on the way is `kind="io"`; the temp file is removed
       on every failure path. Returns `WriteResult` with the post-swap
       `stat_sig`, which the caller stores in `catalog.ini_sigs`.
    """
    raise NotImplementedError("design/ini-write-layer.md §4 steps 1-9")


def _atomic_replace(path: Path, payload: bytes) -> tuple[int, int]:
    """Design §4 steps 5 and 7, the shared atomic-swap core (also meant
    to back the tier-2 native sidecar writer, design §6).

    - Write `payload` to `<path.name>.<pid>.tmp` in `path`'s own
      directory (the `_remember_library` idiom, `main.py:450`),
      then `flush()` and `os.fsync()` the temp file's descriptor before
      closing it -- the §7 row's "durable means fsync'd", which every
      existing writer in the tree (`starstore.py`, `library.py`,
      `catalog.py`) currently skips.
    - On Windows, capture `path`'s file attributes before the swap
      (measured: a bare `os.replace` onto a hidden+system `.picasa.ini`
      leaves only `FILE_ATTRIBUTE_ARCHIVE`) and re-apply them to the new
      file after it via `SetFileAttributesW`.
    - `os.replace(tmp, path)` for the atomic swap itself.
    - On POSIX, `os.fsync` the parent directory's file descriptor after
      the swap (Windows has no directory fsync -- measured `EACCES` --
      and `MoveFileEx` with replace is the journaling filesystem's own
      atomic rename, so nothing further is needed there).
    - Removes the temp file on any failure path, before re-raising as
      `IniWriteError(kind="io")`.
    - Returns the post-swap `stat_sig` (size, mtime) of `path`.
    """
    raise NotImplementedError("design/ini-write-layer.md §4 steps 5, 7")


def _check_payload(doc_before: IniDocument, doc_after: IniDocument,
                    payload: bytes, edits: Sequence[IniEdit]) -> None:
    """Design §4 step 4's in-memory check, BEFORE any file is touched
    (no temp file exists yet at this point, so there is nothing to clean
    up on a failure here beyond simply not proceeding to step 5).

    - Every line `doc_before`/`doc_after` agree neither `apply()` nor
      `_upgrade_encoding` meant to touch is byte-identical between the
      two documents (`text` and `eol` both) -- the "we change only the
      lines we mean to change" invariant, checked against the in-memory
      model directly rather than by re-reading a file that does not
      exist yet.
    - `payload` (== `doc_after.to_bytes()`) parses via
      `picasa_db.read_picasa_ini` and every edit landed: present with
      its new value in every section of that name for a `set`, absent
      from every section of that name for a `remove`.
    - `len(parsed.anomalies)` did not grow relative to parsing
      `doc_before`'s own bytes.

    Raises `IniWriteError(kind="verify")` on any mismatch.
    """
    raise NotImplementedError("design/ini-write-layer.md §4 step 4 (in-memory check)")


def _read_back(path: Path, payload: bytes) -> None:
    """Design §4 step 8, AFTER the swap: read `path`'s bytes back and
    compare them to `payload` byte-for-byte. This is ONLY a byte
    comparison -- `_check_payload` (step 4) already established the
    edits are semantically correct before the temp file was even
    written, so a mismatch here means the swap/write itself corrupted
    bytes (a partial write despite fsync, a filesystem oddity), not that
    an edit failed to land. Raises `IniWriteError(kind="verify")` on any
    mismatch; the caller (`write_edits`) treats it the same as any other
    failure path -- the temp file is already gone by this point (the
    swap already completed), so there is nothing left to clean up on
    `path` itself; the on-disk file is left as-is and the refusal is
    surfaced so `librarystate` can mark the folder unhealthy (design
    §8)."""
    raise NotImplementedError("design/ini-write-layer.md §4 step 8 (post-swap read-back)")


def _upgrade_encoding(doc: IniDocument) -> None:
    """Design §3's encoding-upgrade edge rule.

    - `doc.encoding == "cp1252"` (clean legacy file): when a new edit's
      value cannot be encoded by cp1252, transcode the whole document to
      UTF-8 in place -- retag `doc.encoding = "utf-8"`, and insert an
      `[encoding]` section with `utf8=1` as the file's first lines (a
      header Line plus a pair Line, both using `doc.eol`) unless one is
      already present. An ASCII value never triggers this (cp1252 is a
      superset of ASCII), regardless of tag.
    - `doc.encoding == "legacy-surrogateescape"` (the file already
      contains at least one byte cp1252 itself rejects, e.g.
      0x81/0x8D/0x8F/0x90/0x9D -- see the `IniDocument.encoding` field
      comment): refuses with `IniWriteError(kind="encoding")` for ANY
      non-ASCII value, not merely one cp1252 cannot encode. This file's
      true encoding is already unknown -- transcoding it under an
      assumed encoding would risk corrupting the undecoded byte(s)
      silently, so no non-ASCII value is safe to write into it, even one
      that happens to be cp1252-encodable.
    - `doc.encoding in ("utf-8", "utf-8-marked")`: never upgrades: both
      already accept any value via `surrogateescape`/plain UTF-8 encode.

    The caller (`write_edits`) is responsible for setting
    `WriteResult.upgraded_encoding=True` when this function actually
    changes `doc.encoding`, and for logging it (design §3: "this is the
    one case where every line changes")."""
    raise NotImplementedError("design/ini-write-layer.md §3")
