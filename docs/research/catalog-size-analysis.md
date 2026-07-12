# fauxcasa-ed5.5 — Catalog size: meet ~50 B/photo honestly

Decision-grade measurement for the M1 catalog-size budget. All numbers measured
on the real 100k-photo benchmark catalog
(`A:\dev\fauxcasa\cache\benchmark-thumbs.fcache.catalog.json`, 5,229,874 B,
100,000 rows) or on synthesized fully-indexed rows built over the **same 100k
real rel paths** — every synthesized/modeled number is labelled. Measurement
script: `scripts/measure-catalog-size.py` (PEP 723 uv script, re-runnable
against the local benchmark cache); raw output:
the script prints the results JSON to stdout.

## 1. What a row actually is

`_photo_to_row` (catalog.py:1005) writes absent-means-default single-char keys:
`r` (rel path, always) plus optional `s c k o h a f d g wh z m x`. The identity
substrate the indexer fills is `z` (size), `m` (mtime), `x` (64-char hex
sha256); the indexer also fills `d` (date_taken, 19-char ISO).
`save_catalog` uses `json.dumps` **default separators** (`", "` / `": "`).
Multiroot-in-flight rider: optional per-row `"R"` root id — absent for
single-root, **+8 B/photo per extra-root row** (`, "R": 1`) when present;
treated as a rider throughout, it does not change any ranking below.

## 2. Measured reality (real file)

| quantity | value |
|---|---|
| **Adopt-mode catalog (measured)** | **52.30 B/photo** |
| …of which the `r` field | 48.30 B (path avg 41.3 B + quotes/key/colon) |
| …row punctuation | 4.0 B |
| avg basename | 11.25 B; avg folder prefix 29.05 B; 1,548 folders |

**The previously-quoted 52.3 B/photo is adopt-mode: rows are literally
`{"r": "…"}` — no `z/m/x/d` at all.** It is not a valid green, and note it is
*already* 105% of the 50 B budget. **The rel path alone (48.3 B) consumes 97%
of the budget before a single byte of identity exists.**

**Fully-indexed shape (synthesized on the same 100k paths** — realistic
sha256-of-path digests, log-uniform 60 KB–12 MB sizes, 2001–2026 mtimes,
1996–2026 dates):

| quantity | value | per-field |
|---|---|---|
| identity substrate only (`r z m x`) | 155.7 B/photo *(synth)* | — matches the bead's "~150 B" |
| fully indexed (`r d z m x`) | **183.7 B/photo** *(synth)* | r 48.3 · x 71.0 · d 26.0 · m 15.0 · z 11.4 · punct 12 |

The hex sha256 costs 71 B/row serialized (64 hex + `"x": ""` + separator) —
the single biggest field, but even deleting it entirely leaves 112.7 B/photo.

## 3. Options (all measured on the same 100k rows unless marked MODEL)

| # | option | B/photo | % of 50 B budget | saved vs 183.7 |
|---|---|---:|---:|---:|
| a | current: hex sha256, default seps *(synth)* | 183.7 | 367% | — |
| b | base85 sha256 (b85encode, 32 B→40 ch) *(synth)* | 159.7 | 319% | 24.0 |
| b2 | base64 sha256 (unpadded, 43 ch) *(synth)* | 162.7 | 325% | 21.0 |
| c | truncate 24 B → b85 (30 ch) *(synth)* | 149.7 | 299% | 34.0 |
| c | truncate 20 B → b85 (25 ch) *(synth)* | 144.7 | 289% | 39.0 |
| c | truncate 16 B → b85 (20 ch) *(synth)* | 139.7 | 279% | 44.0 |
| d | field-name shortening alone | (no-op) | — | 0 — keys are already 1 char |
| d' | compact JSON separators, hex hash *(synth)* | 173.7 | 347% | 10.0 |
| d2 | compact seps + trunc-16 b85 *(synth)* | 129.7 | 259% | 54.0 |
| g | + folder-grouped rows (folder once/group, basename rows), trunc-16, compact *(synth)* | 100.3 | 201% | 83.4 |
| e | binary catalog, full 32 B sha (MODEL) | 60.0 | 120% | 123.7 |
| e | binary catalog, 16 B truncated sha (MODEL) | **44.0** | **88%** | 139.7 |
| f | gzip -9 whole file (hex JSON) *(synth)* | 64.9 | 130% | 118.8 |
| f | zstd-3 whole file (hex JSON) *(synth)* | 62.5 | 125% | 121.2 |
| f | zstd-19 whole file (hex JSON) *(synth)* | 57.7 | 115% | 126.0 |
| f2 | zstd-3 over option-g JSON *(synth)* | **41.8** | **84%** | 141.9 |
| — | perspective: zstd-3 over the real adopt-mode file (measured) | 2.4 | 5% | — |

Compression floor: 32 B of the row is a cryptographic digest — pure entropy no
compressor removes; truncating the hash is the only way under it. (Real
mtimes/dates cluster more than my synth values, so the f-rows are mildly
pessimistic; the uncompressed rows are digit-count-accurate.)

### Collision risk of truncation (birthday bound, p ≈ n²/2^(bits+1))

| kept | bits | p @ 100k | p @ 1M |
|---|---|---:|---:|
| 8 B | 64 | 2.7e-10 | 2.7e-8 |
| **16 B** | **128** | **1.5e-29** | **1.5e-27** |
| 20 B | 160 | 3.4e-39 | 3.4e-37 |
| 24 B | 192 | 8.0e-49 | 8.0e-47 |

At 16 B the accidental-collision probability at 1M photos is ~10²⁰× below
"struck by lightning while winning the lottery." Truncation only weakens
*adversarial* collision resistance, which is irrelevant for a local photo
catalog's identity/dedup role; the full hash is always recoverable by
re-hashing the file.

### Binary model (e) — assumed row layout, spelled out

Header (O(1)); folder table (u16 len + utf8 path, amortized 0.47 B/photo);
per row: folder_id varint (1.92 B avg @ 1,548 folders) · name u8-len+utf8
(12.25 B avg) · flags u8 (hidden/visible/star/has-geotag/has-dims/has-R…)
· mtime u32 (4) · size varint (3.33 B avg) · date_taken i40 signed seconds
(5 — scanned photos predate 1903, u32 epoch won't do) · sha 32 or 16 B raw.
Sparse fields (caption/keywords/albums/faces/geotag, mostly absent) ride a
side section, ~0 B/photo on this benchmark. Total: 59.97 (full sha) /
43.97 (16 B sha). **Even the binary format misses 50 B with the full sha**
— the budget is only met binary+truncated.

## 4. Engineering cost, one line each

- **(a) hex, status quo** — zero cost; human-greppable; matches `sha256sum` output directly.
- **(b) b85** — one-line encode/decode (`base64.b85encode`; alphabet verified JSON-safe, no `"` or `\`); loses direct `sha256sum` comparability; version bump, no migration (cache is regenerable); `R`-orthogonal.
- **(c) trunc-16 b85** — same one-liner + `[:16]`; collision note above; still eyeball-able in the JSON; `R`-orthogonal; catalog can no longer *emit* a full sha (must re-hash the file if ever needed externally).
- **(d') compact separators** — one argument to `json.dumps`; free; file is already one line so no debuggability change.
- **(g) folder-grouped rows** — moderate: reader/writer restructure; safe only because scan order is folder-contiguous (album `members` are photo indices — flattening order must be preserved exactly); folder-level `R` actually gets *cheaper* (per group, not per row).
- **(e) binary** — highest: custom format, versioning/endianness/corruption story, zero human-debuggability, and **git-diffability is a red herring — `cache/` is gitignored** (`.gitignore:13`), the catalog is regenerable runtime state; absent-means-default becomes flag bits (fine, but every new field is a format rev instead of a new JSON key).
- **(f) whole-file zstd/gzip** — small: wrap read/write (gzip is stdlib; zstd needs a dep); kills text-editor debuggability unless a `--dump` helper exists; random access was never used anyway — `load_catalog` slurps the whole file — so the "random-access story" changes only in principle; `R` unaffected.

## 5. Recommendation and verdict

**Verdict: ARGUE, with a cheap meet-halfway.** The ~50 B/photo budget is not
honestly meetable by any *uncompressed JSON* encoding — the rel path alone is
48.3 B/photo, 97% of the budget, before identity exists at all. Even the
adopt-mode catalog that inspired the 52.3 figure is over budget. Chasing 50 B
uncompressed forces a bespoke binary format *with a truncated hash* (44.0
B/photo modeled) — maximal engineering cost for a gitignored, regenerable
cache file whose real-world size at 1M photos is ~14 MB (139.7 B/photo) vs
~4.4 MB (44 B): both trivial for a desktop app that reads the file once at
startup.

Concretely:

1. **Do now (cheap, honest):** truncate-16 → b85 for `x` plus compact
   separators — two one-line changes, **183.7 → 129.7 B/photo** (−29%),
   collision risk 1.5e-27 at 1M. Optionally folder-grouping later for
   **100.3 B/photo** if the number still rankles (it touches ordering
   invariants, so do it as its own bead).
2. **Re-baseline the spec:** state the budget as two honest numbers —
   **~52 B/photo adopt-mode (paths only)** and **~130 B/photo fully
   indexed** (option d2), with a note that ~100 B is reachable via folder
   grouping and ~42 B via zstd-of-JSON if disk size ever actually matters.
3. **If 50 B must stand as written:** the only sane reading is *on-disk
   compressed* — zstd-3 over the option-g JSON measures **41.8 B/photo**
   (84% of budget) with stdlib-adjacent effort, and beats the binary
   format's 44.0 while keeping a JSON debug path (`zstd -d | jq`). Choose
   this over building a binary format; recommend against binary outright.

*Rider note:* multiroot's `"R"` adds 8 B only on non-default-root rows in
every JSON option, flag-bit-free in (e), and one key per group in (g) — it
does not change any conclusion above.
