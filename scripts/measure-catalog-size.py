#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["zstandard"]
# ///
"""fauxcasa-ed5.5: catalog bytes/photo measurement + encoding prototypes.

Measures the REAL 100k benchmark catalog (adopt-mode, rows = {"r": rel}),
then SYNTHESIZES the fully-indexed row shape (identity substrate z/m/x
plus indexer-filled d) on the same 100k rel paths and prototypes every
candidate encoding on those rows. All synthesized numbers are labelled.

The benchmark catalog itself is a machine-local, untracked artifact (it
lives under <repo-root>/cache/, gitignored) — pass --catalog to point at a
different one. Output goes to stdout as a JSON blob; pass --out to also
write it to a file (fauxcasa-7aj.5: writing results.json unconditionally
was a footgun for a repo-tooling script with no fixture of its own).
"""
import argparse
import base64
import gzip
import hashlib
import json
import math
import random
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

import zstandard

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = REPO_ROOT / "cache" / "benchmark-thumbs.fcache.catalog.json"

parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
parser.add_argument(
    "--catalog", type=Path, default=DEFAULT_CATALOG,
    help=f"Path to the benchmark catalog.json to measure "
         f"(default: {DEFAULT_CATALOG})")
parser.add_argument(
    "--out", type=Path, default=None,
    help="Also write the results JSON to this path (default: stdout only, "
         "nothing written to disk)")
args = parser.parse_args()

CATALOG = args.catalog
OUT = args.out

random.seed(0xED55)


# ---------------------------------------------------------------- helpers
def dumps(obj) -> str:
    """Match save_catalog: json.dumps default separators (', ', ': ')."""
    return json.dumps(obj)


def dumps_compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def photos_array_bytes(rows, compact=False) -> int:
    """UTF-8 bytes of the serialized photos array (the per-photo cost
    center; version/library/folders/... header is O(1), not per-photo)."""
    s = dumps_compact(rows) if compact else dumps(rows)
    return len(s.encode("utf-8"))


def per_field_breakdown(rows):
    """Average per-row byte cost attributed to each field: key quotes +
    key + ': ' + serialized value + ', ' separator share. Row braces
    ('{}') counted under 'row-punctuation'."""
    n = len(rows)
    field_total: dict[str, int] = {}
    punct = 0
    for row in rows:
        punct += 2  # braces
        punct += 2  # ", " between array elements (last row: off by 2 total)
        for k, v in row.items():
            cost = len(k) + 4 + len(dumps(v))  # "k": v
            field_total[k] = field_total.get(k, 0) + cost
        punct += 2 * (len(row) - 1)  # ", " between fields
        field_total_adjust = 0
    return ({k: t / n for k, t in field_total.items()}, punct / n)


# ---------------------------------------------------------------- load real
raw = CATALOG.read_bytes()
data = json.loads(raw)
rows_real = data["photos"]
n = len(rows_real)
rels = [r["r"] for r in rows_real]

real_photos_bytes = photos_array_bytes(rows_real)
real_breakdown, real_punct = per_field_breakdown(rows_real)

folders = sorted({rel.rpartition("/")[0] for rel in rels})
names = [rel.rpartition("/")[2] for rel in rels]
avg_rel = statistics.mean(len(r.encode()) for r in rels)
avg_name = statistics.mean(len(nm.encode()) for nm in names)
avg_folder = statistics.mean(len(rel.rpartition("/")[0].encode()) for rel in rels)

# ------------------------------------------------------- synthesize indexed
def synth_date():
    t0 = datetime(1996, 1, 1)
    d = t0 + timedelta(seconds=random.randrange(0, 30 * 365 * 86400))
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def synth_size():
    # log-uniform 60 KB .. 12 MB (typical camera JPEG spread)
    return int(math.exp(random.uniform(math.log(60_000), math.log(12_000_000))))


def synth_mtime():
    # 2001..2026 -> 10-digit epoch seconds
    return random.randrange(978_307_200, 1_767_225_600)


digests = [hashlib.sha256(rel.encode()).digest() for rel in rels]  # realistic 32B
sizes = [synth_size() for _ in rels]
mtimes = [synth_mtime() for _ in rels]
dates = [synth_date() for _ in rels]


def make_rows(hash_field):
    """hash_field: fn(digest)->(key, json-value) or None to omit."""
    out = []
    for rel, dg, z, m, d in zip(rels, digests, sizes, mtimes, dates):
        row = {"r": rel, "d": d, "z": z, "m": m}
        if hash_field:
            k, v = hash_field(dg)
            row[k] = v
        out.append(row)
    return out


rows_indexed_hex = make_rows(lambda dg: ("x", dg.hex()))
rows_identity_only = [  # z/m/x without d, in case ~150 B figure = substrate only
    {"r": rel, "z": z, "m": m, "x": dg.hex()}
    for rel, dg, z, m in zip(rels, digests, sizes, mtimes)
]

indexed_bytes = photos_array_bytes(rows_indexed_hex)
identity_only_bytes = photos_array_bytes(rows_identity_only)
indexed_breakdown, indexed_punct = per_field_breakdown(rows_indexed_hex)

# sanity: b85 alphabet is JSON-safe (no '"' or '\\')
b85_alphabet = bytes(sorted(set(base64.b85encode(bytes(range(256))))))
assert b'"' not in b85_alphabet and b"\\" not in b85_alphabet

results = {}
results["file_total_bytes"] = len(raw)
results["n"] = n
results["real"] = {
    "photos_array_bytes": real_photos_bytes,
    "bytes_per_photo": real_photos_bytes / n,
    "file_bytes_per_photo": len(raw) / n,
    "per_field": real_breakdown,
    "punct_per_row": real_punct,
    "n_folders": len(folders),
    "avg_rel_bytes": avg_rel,
    "avg_name_bytes": avg_name,
    "avg_folder_bytes": avg_folder,
}
results["synth_identity_only_bpp"] = identity_only_bytes / n
results["synth_indexed"] = {
    "bytes_per_photo": indexed_bytes / n,
    "per_field": indexed_breakdown,
}

# ------------------------------------------------------------ (a-d) options
options = {}

def opt(name, rows, compact=False, note=""):
    b = photos_array_bytes(rows, compact=compact)
    options[name] = {"bytes_per_photo": b / n, "note": note}
    return b / n

opt("a_hex_sha256", rows_indexed_hex)
opt("b_b85_sha256", make_rows(lambda dg: ("x", base64.b85encode(dg).decode())))
opt("b2_b64_sha256", make_rows(
    lambda dg: ("x", base64.b64encode(dg).decode().rstrip("="))))
opt("c_trunc16_b85", make_rows(lambda dg: ("x", base64.b85encode(dg[:16]).decode())))
opt("c_trunc20_b85", make_rows(lambda dg: ("x", base64.b85encode(dg[:20]).decode())))
opt("c_trunc24_b85", make_rows(lambda dg: ("x", base64.b85encode(dg[:24]).decode())))
# (d) field names are already 1-char; the only "naming" fat left is the
# default json.dumps separators (', '/': ') -> compact separators.
opt("d_compact_separators_hex", rows_indexed_hex, compact=True)
opt("d2_compact_plus_trunc16_b85",
    make_rows(lambda dg: ("x", base64.b85encode(dg[:16]).decode())), compact=True)

# grouped-by-folder JSON (bonus): folder path stored once per folder,
# rows keep only the basename.
by_folder: dict[str, list] = {}
for rel, dg, z, m, d in zip(rels, digests, sizes, mtimes, dates):
    fol, _, nm = rel.rpartition("/")
    by_folder.setdefault(fol, []).append(
        {"n": nm, "d": d, "z": z, "m": m,
         "x": base64.b85encode(dg[:16]).decode()})
grouped = [{"p": f, "ph": ph} for f, ph in by_folder.items()]
gb = len(dumps_compact(grouped).encode())
options["g_grouped_folders_trunc16_compact"] = {
    "bytes_per_photo": gb / n,
    "note": "folder stored once per group; rows carry basename only",
}
# grouped, full hex hash (grouping alone, no hash change)
by_folder2: dict[str, list] = {}
for rel, dg, z, m, d in zip(rels, digests, sizes, mtimes, dates):
    fol, _, nm = rel.rpartition("/")
    by_folder2.setdefault(fol, []).append(
        {"n": nm, "d": d, "z": z, "m": m, "x": dg.hex()})
grouped2 = [{"p": f, "ph": ph} for f, ph in by_folder2.items()]
options["g2_grouped_folders_hex_default_sep"] = {
    "bytes_per_photo": len(dumps(grouped2).encode()) / n,
    "note": "grouping alone, hash untouched, default separators",
}

# ------------------------------------------------- (e) binary catalog model
# Fixed layout, spelled out (variable-length strings length-prefixed):
#   header: magic 4 + ver 2 + counts/offsets 24            (O(1), ignored /photo)
#   folder table: per folder u16 len + utf8 path
#   per row:
#     folder_id  varint (100k photos / 1,548 real benchmark folders,
#                measured below as len(folders) -> 2 B)
#     name       u8 len + utf8 bytes (avg measured)
#     flags      u8  (hidden/visible/star-count/has-geotag/has-dims/R-rider...)
#     mtime      u32 (4)   [epoch seconds; 2106 horizon]
#     size       varint (avg ~3.6 B for 60KB..12MB)
#     date_taken i40 (5)   [signed seconds; scanned photos predate 1903]
#     sha256     32 raw (or 16 truncated)
# Star/caption/keywords/albums/faces/geotag ride a sparse side-section
# (most rows have none) — charged here at the measured real-catalog rate
# of ~0 for the benchmark; noted in the report.
def varint_len(v):
    b = 1
    while v >= 0x80:
        v >>= 7
        b += 1
    return b

fid_b = statistics.mean(varint_len(i) for i in range(len(folders)))
size_b = statistics.mean(varint_len(z) for z in sizes)
folder_table_pp = sum(2 + len(f.encode()) for f in folders) / n
bin_common = fid_b + 1 + avg_name + 1 + 4 + size_b + 5 + folder_table_pp
options["e_binary_full_sha"] = {
    "bytes_per_photo": bin_common + 32,
    "note": "MODEL (not built): layout above, 32B raw sha",
}
options["e_binary_trunc16"] = {
    "bytes_per_photo": bin_common + 16,
    "note": "MODEL (not built): layout above, 16B truncated sha",
}
results["binary_model_parts"] = {
    "folder_id_varint": fid_b, "name_len+name": 1 + avg_name, "flags": 1,
    "mtime_u32": 4, "size_varint": size_b, "date_i40": 5,
    "folder_table_amortized": folder_table_pp,
}

# --------------------------------------------- (f) whole-file compression
indexed_json = dumps(rows_indexed_hex).encode()
gz = gzip.compress(indexed_json, 9)
z3 = zstandard.ZstdCompressor(level=3).compress(indexed_json)
z19 = zstandard.ZstdCompressor(level=19).compress(indexed_json)
options["f_gzip9_hex_json"] = {"bytes_per_photo": len(gz) / n,
                               "note": "whole-file; no random access"}
options["f_zstd3_hex_json"] = {"bytes_per_photo": len(z3) / n,
                               "note": "whole-file; no random access"}
options["f_zstd19_hex_json"] = {"bytes_per_photo": len(z19) / n,
                                "note": "whole-file; no random access"}
# compression on the best-JSON variant too
best_json = dumps_compact(grouped).encode()
options["f2_zstd3_grouped_trunc16"] = {
    "bytes_per_photo": len(zstandard.ZstdCompressor(level=3)
                           .compress(best_json)) / n,
    "note": "zstd3 over grouped+trunc16 compact JSON",
}
# adopt-mode real file compressed, for perspective
options["f3_zstd3_real_adopt"] = {
    "bytes_per_photo": len(zstandard.ZstdCompressor(level=3)
                           .compress(raw)) / n,
    "note": "zstd3 over the real adopt-mode file",
}

# ------------------------------------------------------ collision math
def collision_p(bits, nn):
    return nn * (nn - 1) / 2 / (2 ** bits)

results["collisions"] = {
    f"{by*8}bit": {"p_100k": collision_p(by * 8, 100_000),
                   "p_1M": collision_p(by * 8, 1_000_000)}
    for by in (8, 16, 20, 24, 32)
}

# R rider cost: ', "R": 1' with default separators
results["R_rider_bytes_when_present"] = len(', "R": 1')

results["options"] = options
if OUT is not None:
    OUT.write_text(json.dumps(results, indent=1))
print(json.dumps(results, indent=1))
