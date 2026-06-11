#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Snapshot/diff/capture harness for the Wine Picasa oracle.

Differential fixture workflow (docs/research/wine-oracle.md, bead fauxcasa-dcc):

  oracle-diff.py snapshot                 # baseline db3/ + albums + library
  <perform ONE action in the Picasa UI>
  oracle-diff.py diff                     # decoded report of what changed
  oracle-diff.py capture star-photo \
      --note "Starred Beach Day/photo00.jpg via toolbar"
                                          # writes fixtures/oracle/NNN-star-photo/
                                          # {before,after,diff.md,meta.json},
                                          # then re-baselines
  oracle-diff.py pmp <file.pmp ...>       # decode/dump .pmp files (debug)

Everything under fixtures/oracle/ is synthetic and committable. Thumbnail and
preview caches are derived data: their changes are reported in diff.md but the
blobs are not copied unless --include-blobs is given.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import shutil
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SNAPDIR = REPO / "cache" / "oracle-snapshots"
BASELINE = SNAPDIR / "baseline"
MANIFEST = SNAPDIR / "manifest.json"
FIXTURES = REPO / "fixtures" / "oracle"

# Derived caches: reported in diffs, but not copied into fixtures by default.
BLOB_RE = re.compile(r"^db3/((big)?thumbs2?_\d+\.db|previews_\d+\.db|wordhash\.dat)$")

PMP_MAGIC = 0x3FCCCCCD
PMP_TYPE_NAMES = {
    0x0: "string",
    0x1: "uint32",
    0x2: "date(f64)",
    0x3: "byte",
    0x4: "uint64",
    0x5: "uint16",
    0x6: "string6",
    0x7: "uint32(7)",
}
MAX_DIFF_ROWS = 100  # per-file cap on printed row deltas


# ---------------------------------------------------------------- roots/scan

def find_roots() -> dict[str, Path]:
    library = REPO / "cache" / "synthetic-library"
    hits = sorted(
        (REPO / "cache" / "wine-oracle" / "drive_c" / "users").glob(
            "*/AppData/Local/Google"
        )
    )
    google = next((g for g in hits if (g / "Picasa2" / "db3").is_dir()), None)
    if google is None:
        sys.exit("error: no Picasa2/db3 found under cache/wine-oracle — run Picasa once first")
    if not library.is_dir():
        sys.exit("error: cache/synthetic-library missing — run scripts/make-synthetic-library.py")
    return {
        "db3": google / "Picasa2" / "db3",
        "albums": google / "Picasa2Albums",
        "library": library,
    }


def scan(roots: dict[str, Path]) -> dict[str, Path]:
    """Map of snapshot-relative path -> absolute file path."""
    files: dict[str, Path] = {}
    for prefix, root in roots.items():
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file():
                files[f"{prefix}/{p.relative_to(root).as_posix()}"] = p
    return files


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- pmp decode

def parse_pmp(data: bytes):
    """Return (ftype, declared_count, entries, notes). Raises ValueError if not pmp."""
    notes: list[str] = []
    if len(data) == 4 and struct.unpack("<I", data)[0] == PMP_MAGIC:
        return None, 0, [], ["marker file (magic only)"]
    if len(data) < 20:
        raise ValueError("too short for pmp header")
    magic, ftype, c1, c2, ftype2, c3, count = struct.unpack_from("<IHHIHHI", data, 0)
    if magic != PMP_MAGIC:
        raise ValueError(f"bad magic 0x{magic:08x}")
    if (c1, c2, c3) != (0x1332, 0x2, 0x1332) or ftype != ftype2:
        notes.append(f"unexpected header constants: {c1:#x} {c2:#x} {c3:#x} type2={ftype2:#x}")
    entries: list = []
    off = 20
    try:
        if ftype in (0x0, 0x6):
            for _ in range(count):
                end = data.index(b"\x00", off)
                entries.append(data[off:end].decode("utf-8", errors="backslashreplace"))
                off = end + 1
        elif ftype in (0x1, 0x7):
            for _ in range(count):
                entries.append(struct.unpack_from("<I", data, off)[0])
                off += 4
        elif ftype == 0x2:
            for _ in range(count):
                entries.append(struct.unpack_from("<d", data, off)[0])
                off += 8
        elif ftype == 0x3:
            for _ in range(count):
                entries.append(data[off])
                off += 1
        elif ftype == 0x4:
            for _ in range(count):
                entries.append(struct.unpack_from("<Q", data, off)[0])
                off += 8
        elif ftype == 0x5:
            for _ in range(count):
                entries.append(struct.unpack_from("<H", data, off)[0])
                off += 2
        else:
            notes.append(f"unknown field type {ftype:#x}; raw payload kept as hex")
            entries = [data[20:].hex()]
            off = len(data)
    except (ValueError, struct.error, IndexError):
        notes.append(f"truncated: only {len(entries)} of {count} entries decodable")
    if off < len(data):
        notes.append(f"{len(data) - off} trailing bytes after entries: {data[off:off + 32].hex()}")
    return ftype, count, entries, notes


def fmt_entry(ftype, v) -> str:
    if ftype == 0x2 and isinstance(v, float):
        try:
            dt = datetime(1899, 12, 30) + timedelta(days=v)
            return f"{v!r} ({dt.isoformat(sep=' ', timespec='seconds')})"
        except OverflowError:
            return repr(v)
    if ftype == 0x4:
        return f"0x{v:016x}"
    return repr(v)


def pmp_dump_lines(rel: str, data: bytes) -> list[str]:
    try:
        ftype, count, entries, notes = parse_pmp(data)
    except ValueError as e:
        return [f"{rel}: not a valid pmp ({e})"]
    tname = PMP_TYPE_NAMES.get(ftype, "?") if ftype is not None else "marker"
    lines = [f"{rel}: type={ftype:#x} ({tname}), {count} entries" if ftype is not None
             else f"{rel}: {notes[0]}"]
    lines += [f"  note: {n}" for n in notes if ftype is not None]
    lines += [f"  [{i}] {fmt_entry(ftype, v)}" for i, v in enumerate(entries)]
    return lines


def pmp_diff_lines(before: bytes | None, after: bytes | None) -> list[str]:
    """Row-level delta between two pmp payloads."""
    try:
        bt, bcount, be, bnotes = parse_pmp(before) if before is not None else (None, 0, [], [])
        at, acount, ae, anotes = parse_pmp(after) if after is not None else (None, 0, [], [])
    except ValueError as e:
        return [f"(pmp parse failed: {e}; falling back to binary summary)",
                *binary_summary_lines(before or b"", after or b"")]
    lines: list[str] = []
    ftype = at if at is not None else bt
    tname = PMP_TYPE_NAMES.get(ftype, f"{ftype:#x}") if ftype is not None else "marker"
    if bt is not None and at is not None and bt != at:
        lines.append(f"FIELD TYPE CHANGED: {bt:#x} -> {at:#x}")
    lines.append(f"type {tname}; rows {len(be)} -> {len(ae)}"
                 + (f" (declared {bcount} -> {acount})" if (bcount, acount) != (len(be), len(ae)) else ""))
    for n in set(bnotes + anotes):
        lines.append(f"note: {n}")
    shown = 0
    for i in range(max(len(be), len(ae))):
        if shown >= MAX_DIFF_ROWS:
            lines.append(f"... (more row deltas suppressed, cap {MAX_DIFF_ROWS})")
            break
        b = be[i] if i < len(be) else None
        a = ae[i] if i < len(ae) else None
        if b == a:
            continue
        if b is None:
            lines.append(f"[{i}] (new) {fmt_entry(ftype, a)}")
        elif a is None:
            lines.append(f"[{i}] (removed) was {fmt_entry(ftype, b)}")
        else:
            lines.append(f"[{i}] {fmt_entry(ftype, b)} -> {fmt_entry(ftype, a)}")
        shown += 1
    if shown == 0:
        lines.append("(no row-level differences — header/trailing bytes only)")
    return lines


# ------------------------------------------------------------- other formats

TEXT_SUFFIXES = {".txt", ".ini", ".pal", ".xml", ".log"}


def decode_text(data: bytes) -> str | None:
    if b"\x00" in data:
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def text_diff_lines(rel: str, before: bytes | None, after: bytes | None) -> list[str]:
    bt = decode_text(before) if before is not None else ""
    at = decode_text(after) if after is not None else ""
    if bt is None or at is None:
        return binary_summary_lines(before or b"", after or b"")
    diff = list(
        difflib.unified_diff(
            bt.splitlines(keepends=False), at.splitlines(keepends=False),
            fromfile=f"before/{rel}", tofile=f"after/{rel}", lineterm="", n=2,
        )
    )
    return diff[:MAX_DIFF_ROWS] + (["... (truncated)"] if len(diff) > MAX_DIFF_ROWS else []) \
        if diff else ["(byte change with no visible line diff — line endings/encoding?)"]


def binary_summary_lines(before: bytes, after: bytes) -> list[str]:
    lines = [f"binary; {len(before)} -> {len(after)} bytes"]
    n = min(len(before), len(after))
    first = next((i for i in range(n) if before[i] != after[i]), None)
    if first is None:
        if len(before) == len(after):
            lines.append("contents identical")
        else:
            longer, who = (after, "appended") if len(after) > len(before) else (before, "truncated")
            lines.append(f"common prefix; {abs(len(after) - len(before))} bytes {who}: "
                         f"{longer[n:n + 64].hex()}{'...' if abs(len(after) - len(before)) > 64 else ''}")
    else:
        lines.append(f"first difference at offset {first:#x}")
        lines.append(f"  before[{first:#x}:]: {before[first:first + 32].hex()}")
        lines.append(f"  after [{first:#x}:]: {after[first:first + 32].hex()}")
    return lines


def file_diff_lines(rel: str, before: bytes | None, after: bytes | None) -> list[str]:
    suffix = Path(rel).suffix.lower()
    if suffix == ".pmp" or re.search(r"/(albumdata|catdata|imagedata)_0$", rel):
        return pmp_diff_lines(before, after)
    if suffix in TEXT_SUFFIXES:
        return text_diff_lines(rel, before, after)
    if suffix in (".jpg", ".jpeg", ".png"):
        return [f"image; {len(before) if before is not None else '-'} -> "
                f"{len(after) if after is not None else '-'} bytes (see fixture copies)"]
    return binary_summary_lines(before or b"", after or b"")


# ----------------------------------------------------------- snapshot / diff

def take_snapshot(label: str) -> None:
    roots = find_roots()
    files = scan(roots)
    if BASELINE.exists():
        shutil.rmtree(BASELINE)
    manifest = {
        "label": label,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "roots": {k: str(v) for k, v in roots.items()},
        "files": {},
    }
    for rel, path in files.items():
        data = path.read_bytes()
        dest = BASELINE / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        manifest["files"][rel] = {"sha256": sha256(data), "size": len(data)}
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=1))
    print(f"snapshot '{label}': {len(files)} files baselined -> {BASELINE.relative_to(REPO)}")


def load_manifest() -> dict:
    if not MANIFEST.exists():
        sys.exit("error: no baseline — run `oracle-diff.py snapshot` first")
    return json.loads(MANIFEST.read_text())


def compute_changes(manifest: dict) -> list[tuple[str, str]]:
    """[(rel, status)] where status in added/removed/changed, sorted by rel."""
    current = scan(find_roots())
    old = manifest["files"]
    changes = []
    for rel in sorted(set(old) | set(current)):
        if rel not in old:
            changes.append((rel, "added"))
        elif rel not in current:
            changes.append((rel, "removed"))
        elif sha256(current[rel].read_bytes()) != old[rel]["sha256"]:
            changes.append((rel, "changed"))
    return changes


def build_report(manifest: dict, changes: list[tuple[str, str]]) -> str:
    current = scan(find_roots())
    out: list[str] = []
    semantic = [(r, s) for r, s in changes if not BLOB_RE.match(r)]
    blobs = [(r, s) for r, s in changes if BLOB_RE.match(r)]
    out.append(f"baseline: '{manifest['label']}' ({manifest['created']})")
    out.append(f"{len(changes)} file(s) differ ({len(semantic)} semantic, {len(blobs)} blob/cache)")
    for rel, status in semantic:
        before = (BASELINE / rel).read_bytes() if status != "added" else None
        after = current[rel].read_bytes() if status != "removed" else None
        bsize = "-" if before is None else len(before)
        asize = "-" if after is None else len(after)
        out.append("")
        out.append(f"== {status.upper()}: {rel} ({bsize} -> {asize} bytes)")
        out += ["   " + line for line in file_diff_lines(rel, before, after)]
    if blobs:
        out.append("")
        out.append("-- blob/cache changes (not copied into fixtures by default):")
        for rel, status in blobs:
            before = (BASELINE / rel).read_bytes() if status != "added" else None
            after = current[rel].read_bytes() if status != "removed" else None
            summary = "; ".join(binary_summary_lines(before or b"", after or b"")[:2])
            out.append(f"   {status}: {rel} — {summary}")
    return "\n".join(out)


# ------------------------------------------------------------------ capture

def slugify(s: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    if not slug:
        sys.exit("error: slug is empty after normalization")
    return slug


def next_fixture_number() -> int:
    if not FIXTURES.exists():
        return 1
    nums = [int(m.group(1)) for d in FIXTURES.iterdir()
            if d.is_dir() and (m := re.match(r"^(\d{3})-", d.name))]
    return max(nums, default=0) + 1


def capture(slug: str, note: str, include_blobs: bool) -> None:
    manifest = load_manifest()
    changes = compute_changes(manifest)
    if not changes:
        sys.exit("error: nothing changed since baseline — no fixture to capture")
    current = scan(find_roots())
    report = build_report(manifest, changes)

    nnn = next_fixture_number()
    fixdir = FIXTURES / f"{nnn:03d}-{slug}"
    if fixdir.exists():
        sys.exit(f"error: {fixdir} already exists")

    meta = {
        "fixture": f"{nnn:03d}-{slug}",
        "note": note,
        "captured": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline": {"label": manifest["label"], "created": manifest["created"]},
        "files": {},
    }
    for rel, status in changes:
        is_blob = bool(BLOB_RE.match(rel))
        copied = include_blobs or not is_blob
        entry = {"status": status, "blob": is_blob, "copied": copied}
        if status != "added":
            data = (BASELINE / rel).read_bytes()
            entry["before"] = {"sha256": sha256(data), "size": len(data)}
            if copied:
                dest = fixdir / "before" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
        if status != "removed":
            data = current[rel].read_bytes()
            entry["after"] = {"sha256": sha256(data), "size": len(data)}
            if copied:
                dest = fixdir / "after" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
        meta["files"][rel] = entry

    fixdir.mkdir(parents=True, exist_ok=True)
    (fixdir / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    (fixdir / "diff.md").write_text(
        f"# Fixture {nnn:03d}: {slug}\n\n"
        f"**Action:** {note}\n\n"
        f"**Captured:** {meta['captured']}\n\n"
        "```\n" + report + "\n```\n"
    )
    print(f"fixture written: {fixdir.relative_to(REPO)} ({len(changes)} files)")
    take_snapshot(f"after {nnn:03d}-{slug}")


# --------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("snapshot", help="baseline the oracle state")
    sp.add_argument("--label", default="manual", help="label recorded in the manifest")

    sub.add_parser("diff", help="report changes vs baseline")

    cp = sub.add_parser("capture", help="write a before/after fixture pair, then re-baseline")
    cp.add_argument("slug", help="short action name, e.g. star-photo")
    cp.add_argument("--note", required=True, help="exact action performed in the UI")
    cp.add_argument("--include-blobs", action="store_true",
                    help="also copy thumbnail/preview caches into the fixture")

    pp = sub.add_parser("pmp", help="decode and dump .pmp files")
    pp.add_argument("files", nargs="+", type=Path)

    args = ap.parse_args()
    if args.cmd == "snapshot":
        take_snapshot(args.label)
    elif args.cmd == "diff":
        manifest = load_manifest()
        changes = compute_changes(manifest)
        if not changes:
            print(f"no changes since baseline '{manifest['label']}' ({manifest['created']})")
        else:
            print(build_report(manifest, changes))
    elif args.cmd == "capture":
        capture(slugify(args.slug), args.note, args.include_blobs)
    elif args.cmd == "pmp":
        for f in args.files:
            print("\n".join(pmp_dump_lines(str(f), f.read_bytes())))


if __name__ == "__main__":
    main()
