#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pillow",
#   "rawpy",
#   "av",
# ]
# ///
"""Pre-build the packed thumbnail cache for the stack trial balloons
(fauxcasa-6hf / fauxcasa-rqe). Every balloon renders the 100k grid from
this one file; the format is deliberately trivial so any candidate
language reads it in a few lines.

    uv run scripts/make-thumbcache.py [--library DIR] [--out FILE]
                                      [--levels 512,256,128]

    uv run scripts/make-thumbcache.py --library-home HOME [--out-dir DIR]
                                      [--levels 512,256,128]

--library-home HOME (multiroot .c, fauxcasa-ed5.7.3, design §11) reads
HOME/.fauxcasa/library.json and builds one thumbs-<root_id>.fcache (+ typed
sidecar, sidecar_version 2 — design §5) per watched root, walked in
roots-list order via the same frozen per-root walk rule as --library. Named
--library-home rather than --library to avoid colliding with the existing
single-directory flag above; mutually exclusive with --library/
--sidecar-only. Output defaults to cache/<sha256(library_id)[:16]>, the same
digest apps/desktop-python/thumbcache.py.cache_dir_for derives for an
explicit library, so the app can warm-start from a script-built cache.

Format (fcthumbs, all little-endian). Two versions share one reader; the
version field selects the layout, so the shipped v1 benchmark cache keeps
loading with no rebuild.

  v1 (single level — the default; byte-identical to the legacy format):
    header, 16 bytes:  magic b"FCTC" | u32 version=1 | u32 count | u32 0
    index, count * 16: u64 blob offset (from file start) | u32 blob length
                       | u16 thumb width | u16 thumb height
    blobs:             JPEG bytes, quality 80, 256 px long edge

  v2 (multi-resolution — built with --levels):
    header, 16 bytes:  magic b"FCTC" | u32 version=2 | u32 count
                       | u16 nlevels | u16 0
    level table:       nlevels * u16  long-edge per level, LARGEST FIRST
                       (e.g. 512, 256, 128)
    index, count*nlevels * 16: the same record, PHOTO-MAJOR — level l of
                       photo i is record i*nlevels + l
    blobs:             JPEG q80, photo-major (a photo's levels are contiguous)

The level set is declared in the header, so a reader locates any level without
the sidecar. Every photo carries exactly nlevels blobs; each lower level is
the top level decoded once and downscaled (never upscaled — a small source
caps every level at its native size). The "files" array stays ONE per photo so
a v2 cache binds to a catalog exactly like a v1 one.

Entry order = sorted(Path) over the library walk, i.e. path COMPONENT
order — NOT a sort of the joined POSIX string (the two differ when one
folder name is a prefix of another that continues past a '/' boundary,
e.g. "2020" vs "2020-01 Trip"; the shipped benchmark cache uses
component order, so this rule is frozen). A JSON sidecar
(<out>.json) records count, the source library, the folder groups
(name, start index, count) and the per-entry library-relative file paths
("files", same order as the index records) in entry order, so a balloon
can draw group headers — and a tracer app can map any tile back to its
source photo — without ever touching the library itself. The grid budget
rule (N4) is that scrolling never reads originals — balloons may open
ONLY this cache pair.

--sidecar-only rewrites just the JSON sidecar from a fresh walk of the
library (validated against the existing .fcache entry count), upgrading
old caches to carry "files" without re-encoding the blobs. It is only
safe if the library has not changed since the cache was built.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import struct
import sys
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "cache"
MAGIC = b"FCTC"
THUMB_EDGE = 256
# Picasa's documented 16-vendor RAW extension list. Mirror of
# apps/desktop-python/rawload.py RAW_EXTS (fauxcasa-v46.1) — must match
# exactly, like EXTS below, or cache-order parity breaks (test_tracer.py
# asserts the sets are equal).
RAW_EXTS = frozenset({
    ".3fr", ".arw", ".cr2", ".crw", ".dcr", ".dng", ".kdc", ".mrw",
    ".nef", ".nrw", ".orf", ".pef", ".raf", ".raw", ".rw2", ".sr2",
    ".srf", ".x3f",
})
# Picasa's documented video list. Mirror of apps/desktop-python/
# videoload.py VIDEO_EXTS (fauxcasa-v46.2) — must match exactly, like
# RAW_EXTS above, or cache-order parity breaks (test_tracer.py asserts
# the sets are equal).
VIDEO_EXTS = frozenset({
    ".3g2", ".3gp", ".asf", ".avi", ".divx", ".m2t", ".m2ts", ".m4v",
    ".mkv", ".mmv", ".mod", ".mov", ".mp4", ".mpeg", ".mpg", ".mts",
    ".tod", ".wmv",
})
# Poster grab point, seconds — mirror of videoload.POSTER_SEEK_S.
POSTER_SEEK_S = 1.0
# Must match apps/desktop-python/catalog.py EXTS exactly (cache-order parity).
# TGA/PSD joined the stills set with fauxcasa-v46.4; PIL reads both natively
# (PSD = the flattened composite, matching the app's Pillow fallback).
EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
        ".webp", ".tga", ".psd"} | RAW_EXTS | VIDEO_EXTS
# Mirror of apps/desktop-python/library.py LIBRARY_DIR / ROOT_MARKER — the
# library-state dir and per-root id marker are excluded from the walk in
# BOTH twins (multiroot .a, fauxcasa-ed5.7.1) or caches stop binding.
LIBRARY_DIR = ".fauxcasa"
ROOT_MARKER = ".fauxcasa-root"
# Mirror of apps/desktop-python/library.py LIBRARY_FILE (multiroot .c).
LIBRARY_FILE = "library.json"
# Mirror of apps/desktop-python/thumbcache.py.RECOMMENDED_LEVELS — 512 is the
# hi-DPI/loupe payload, 256 is the grid's primary, 128 a cheap low-DPI level.
RECOMMENDED_LEVELS = (512, THUMB_EDGE, 128)


def walk_library(root: Path, exts) -> list[Path]:
    """Twin of apps/desktop-python/catalog.py walk_library: the frozen
    path-component-sort walk rule, minus .fauxcasa/ and .fauxcasa-root.
    Any change lands in both files in the same commit (multiroot .a,
    fauxcasa-ed5.7.1) or caches stop binding."""
    nroot = len(root.parts)
    return sorted(
        p for p in root.rglob("*")
        if p.suffix.lower() in exts and p.is_file()
        and LIBRARY_DIR not in p.parts[nroot:]
        and p.name != ROOT_MARKER
    )


def _normalize_levels(levels) -> list[int]:
    """Canonical level set: unique positive long-edges, largest first. None or
    empty -> the single 256 px level (a v1 cache). Mirrors
    apps/desktop-python/thumbcache.py._normalize_levels so both builders agree."""
    if not levels:
        return [THUMB_EDGE]
    out = sorted({int(e) for e in levels if int(e) > 0}, reverse=True)
    if not out:
        raise ValueError("levels must contain at least one positive edge")
    if out[0] > 0xFFFF:
        raise ValueError("a level edge must fit in a u16 (<= 65535)")
    if len(out) > 64:  # fail fast: load_cache caps nlevels at 64
        raise ValueError("at most 64 levels")
    return out


def _primary_level(levels: list[int]) -> int:
    """Index of the level the grid reads (exactly 256 if present, else the
    largest <= 256, else the smallest). Mirrors thumbcache._primary_level."""
    for i, edge in enumerate(levels):
        if edge == THUMB_EDGE:
            return i
    for i, edge in enumerate(levels):
        if edge <= THUMB_EDGE:
            return i
    return len(levels) - 1


def _is_v1(levels: list[int]) -> bool:
    """The legacy v1 layout (no level table; readers assume [256]) is reserved
    for the default single 256 px level ONLY. Any other set — including a lone
    non-256 level such as [512] — is written as v2 so the header's level table
    records the true resolution. Mirrors thumbcache._is_v1."""
    return levels == [THUMB_EDGE]


def _parse_levels(spec: str | None) -> list[int]:
    if not spec:
        return [THUMB_EDGE]
    if spec.strip().lower() == "recommended":
        return _normalize_levels(RECOMMENDED_LEVELS)
    try:
        raw = [int(p) for p in spec.replace(" ", "").split(",") if p]
    except ValueError:
        raise SystemExit(f"--levels: not a comma-separated int list: {spec!r}")
    return _normalize_levels(raw)


def _video_poster(path: Path):
    """PIL mirror of apps/desktop-python/videoload.py poster_frame
    (fauxcasa-v46.2): PyAV over the file BYTES via a seekable BytesIO
    (moov-at-end MP4s need seekable input — decode-service §3c), first
    decodable frame at ~POSTER_SEEK_S in, fallback the first frame.
    Raises on failure; _make_thumb's broad except is the error tile.
    Video frames carry no EXIF Orientation, so no transpose runs."""
    import av

    with av.open(io.BytesIO(path.read_bytes())) as container:
        stream = next(s for s in container.streams if s.type == "video")
        try:
            container.seek(int(POSTER_SEEK_S * av.time_base))
            frame = next(container.decode(stream), None)
        except Exception:
            frame = None
        if frame is None:
            container.seek(0)
            frame = next(container.decode(stream))
        return frame.to_image().convert("RGB")


def _open_source(path: Path):
    """Decoded, orientation-applied RGB image for `path`. RAW files route
    BY EXTENSION to rawpy before PIL can content-sniff the TIFF-based
    container (mirror of apps/desktop-python/rawload.py, same strategy as
    thumbcache._index_one): embedded JPEG preview first — decoded like any
    JPEG, its own EXIF tag applied by exif_transpose, once — else a
    half-size demosaic whose flip LibRaw already baked (so NO
    exif_transpose on that branch: orientation lands exactly once).

    PREVIEW-ORIENTATION EDGE (fauxcasa-v46.5), mirrored PIL-side: some
    vendors store the flip ONLY in the container (LibRaw sizes.flip) and
    omit EXIF Orientation from the embedded JPEG preview, so a plain
    exif_transpose silently no-ops and this script's cache would show the
    preview sideways while the app (which injects a synthetic EXIF tag in
    rawload.raw_preview_jpeg, then relies on QImageReader.setAutoTransform
    to apply it) is corrected. Mirror strategy: when the preview JPEG
    carries NO EXIF Orientation tag of its own AND sizes.flip is nonzero,
    apply the equivalent PIL transpose directly — no synthetic-tag
    injection needed here since there is no downstream EXIF-reading
    consumer, only this script's own encode step. _LIBRAW_FLIP_TO_PIL_
    TRANSPOSE mirrors rawload.py's _LIBRAW_FLIP_TO_EXIF_ORIENTATION mapping
    (TIFF/EXIF Orientation 3/6/8 <-> flip 3/6/5), translated to the
    matching Image.Transpose op instead of an EXIF tag value. Deliberately
    NOT importing rawload here — this script stays self-contained (PEP 723).

    Video files route BY EXTENSION to the PyAV poster path (never PIL).
    Raises on failure; _make_thumb's broad except is the error tile."""
    from PIL import Image, ImageOps

    _LIBRAW_FLIP_TO_PIL_TRANSPOSE = {
        3: Image.Transpose.ROTATE_180,   # flip 3 -> EXIF 3 (180 deg)
        5: Image.Transpose.ROTATE_90,    # flip 5 -> EXIF 8 (90 deg CCW)
        6: Image.Transpose.ROTATE_270,   # flip 6 -> EXIF 6 (90 deg CW)
    }

    if path.suffix.lower() in VIDEO_EXTS:
        return _video_poster(path)
    if path.suffix.lower() in RAW_EXTS:
        import rawpy

        # Bytes in, pixels out (like rawload): sidesteps libraw-vs-Windows
        # path-encoding differences and matches the future sandbox seam.
        with rawpy.imread(io.BytesIO(path.read_bytes())) as raw:
            try:
                thumb = raw.extract_thumb()
                jpeg = (thumb.data
                        if thumb.format == rawpy.ThumbFormat.JPEG else None)
            except rawpy.LibRawError:
                jpeg = None  # no/unsupported preview -> demosaic below
            if jpeg is None:
                return Image.fromarray(raw.postprocess(half_size=True))
            flip = raw.sizes.flip  # read before the rawpy context closes
        with Image.open(io.BytesIO(jpeg)) as img:
            if img.getexif().get(0x0112):  # 274: Orientation, own tag wins
                return ImageOps.exif_transpose(img).convert("RGB")
            op = _LIBRAW_FLIP_TO_PIL_TRANSPOSE.get(flip)
            if op is not None:
                img = img.transpose(op)
            return img.convert("RGB")
    with Image.open(path) as img:
        # Bake EXIF orientation (all 8 cases, mirrors too) so this builder
        # agrees with the in-app one (apps/desktop-python/thumbcache.py) and
        # the viewer; the Picasa rotate= user turns are composed live at
        # display, never baked. No-op without an Orientation tag.
        return ImageOps.exif_transpose(img).convert("RGB")


def _make_thumb(path: Path, levels: list[int]) -> list[tuple[bytes, int, int]]:
    try:
        img = _open_source(path)
        # Decode once to the top (largest) level, then downscale to each lower
        # level — never upscale (PIL.thumbnail caps at the source size, as the
        # legacy single-256 path did). A single level reproduces that path
        # byte-for-byte.
        img.thumbnail((levels[0], levels[0]))
        out: list[tuple[bytes, int, int]] = []
        for edge in levels:
            if img.width <= edge and img.height <= edge:
                lvl = img
            else:
                lvl = img.copy()
                lvl.thumbnail((edge, edge))
            buf = io.BytesIO()
            lvl.save(buf, "JPEG", quality=80)
            out.append((buf.getvalue(), lvl.width, lvl.height))
        return out
    except Exception:
        # A corrupt/unreadable source yields a zero-length blob at every level —
        # the same error tile the in-app builder emits when its decode returns a
        # null image (thumbcache._index_one) — so one bad file never aborts the
        # whole batch build.
        return [(b"", 0, 0) for _ in levels]


def fcache_name(root_id: str) -> str:
    """Mirror of apps/desktop-python/thumbcache.py.fcache_name (multiroot
    .c, design §3): the implicit legacy root (id "") keeps the unsuffixed
    'thumbs.fcache' name; every explicit root gets its own
    'thumbs-<root_id>.fcache'."""
    return "thumbs.fcache" if root_id == "" else f"thumbs-{root_id}.fcache"


def _load_library_home(home: Path) -> tuple[str, list[tuple[str, Path]]]:
    """Minimal library.json reader (mirror of apps/desktop-python/
    library.py load_library, multiroot .c, design §2/§11): returns
    (library_id, [(root_id, root_path), ...]) in roots-list order. Unlike
    the app's fail-soft loader (a bare dir degrades to an implicit legacy
    library), this script has no legacy fallback to degrade to, so any
    parse/format problem is a hard SystemExit."""
    p = home / LIBRARY_DIR / LIBRARY_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"cannot read {p}: {e}")
    if not isinstance(data, dict) or data.get("format") != 1:
        raise SystemExit(f"{p}: not a format-1 library.json")
    library_id = data.get("library_id")
    if not library_id or not isinstance(library_id, str):
        raise SystemExit(f"{p}: missing library_id")
    raw_roots = data.get("roots")
    if not isinstance(raw_roots, list) or not raw_roots:
        raise SystemExit(f"{p}: missing/empty roots")
    roots: list[tuple[str, Path]] = []
    for r in raw_roots:
        rid, rpath = r.get("id"), r.get("path")
        if not rid or not rpath:
            raise SystemExit(f"{p}: malformed root entry {r!r}")
        roots.append((rid, Path(rpath)))
    return library_id, roots


def _build_one_cache(files: list[Path], walk_root: Path, out: Path,
                     levels: list[int], jobs: int | None,
                     header: dict) -> dict:
    """Build one packed fcache + sidecar for an already-walked `files`
    list (relative to `walk_root`) — the --library-home per-root worker.
    `header` supplies the sidecar's identity fields (sidecar_version,
    library_id, root_id), inserted before 'count'/'thumb_edge'/'files'.
    Binary layout is identical to the single-root path below (same v1/v2
    encoder, same ProcessPoolExecutor loop) — only the sidecar header and
    the per-root file selection differ. Returns the sidecar dict written."""
    primary = _primary_level(levels)
    nlevels = len(levels)
    single = _is_v1(levels)
    header_len = 16 if single else 16 + 2 * nlevels
    index = bytearray()
    offset = header_len + 16 * len(files) * nlevels
    worker = partial(_make_thumb, levels=levels)
    tmp = out.with_suffix(".tmp")
    with tmp.open("wb") as f:
        f.write(b"\x00" * offset)  # header (+ level table) + index placeholder
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            for n, plist in enumerate(pool.map(worker, files, chunksize=32)):
                for blob, w, h in plist:  # photo-major: every level of photo n
                    index += struct.pack("<QIHH", offset, len(blob), w, h)
                    f.write(blob)
                    offset += len(blob)
                if n % 10000 == 0:
                    print(f"  {n}/{len(files)}", file=sys.stderr)
        f.seek(0)
        if single:
            f.write(MAGIC + struct.pack("<III", 1, len(files), 0))
        else:
            f.write(MAGIC + struct.pack("<IIHH", 2, len(files), nlevels, 0))
            f.write(struct.pack(f"<{nlevels}H", *levels))
        f.write(index)
    tmp.replace(out)

    # Key order mirrors the app writer (thumbcache.build_cache explicit
    # branch) — no byte-identity constraint on the v2 shape, but the
    # script/app twins stay in lockstep everywhere else (EXTS, walk rule),
    # so keep the sidecars diff-identical too.
    sidecar = {"sidecar_version": header["sidecar_version"],
               "count": len(files)}
    sidecar.update((k, v) for k, v in header.items()
                   if k != "sidecar_version")
    sidecar["thumb_edge"] = levels[primary]
    sidecar["files"] = [p.relative_to(walk_root).as_posix() for p in files]
    if len(levels) > 1:  # informational; the header is authoritative
        sidecar["levels"] = levels
    out.with_suffix(".fcache.json").write_text(json.dumps(sidecar, indent=1))
    return sidecar


def _default_out_dir(library_id: str) -> Path:
    """Default --library-home output dir when --out-dir is omitted: matches
    apps/desktop-python/thumbcache.py.cache_dir_for(library_id, CACHE) with
    no variant, so a script-built cache lands exactly where the app's
    warm-start path looks for it."""
    digest = hashlib.sha256(library_id.encode()).hexdigest()[:16]
    return CACHE / digest


def _main_library_home(args) -> int:
    """--library-home mode (multiroot .c, design §11): read library.json,
    walk each root in roots-list order (the two-layer walk — the in-app
    twin is catalog.walk_roots), and write one thumbs-<root_id>.fcache (+
    typed sidecar, sidecar_version 2, design §5) per root. Entirely
    separate code path from the single-directory --library mode below —
    it never touches that mode's byte-frozen output."""
    library_id, roots = _load_library_home(args.library_home)

    exts = set(EXTS)
    if args.exclude_exts:
        drop = {e if e.startswith(".") else "." + e
                for e in (s.strip().lower()
                          for s in args.exclude_exts.split(",")) if e}
        unknown = drop - exts
        if unknown:
            print("--exclude-exts: unsupported extension(s): "
                  + ", ".join(sorted(unknown)), file=sys.stderr)
            return 2
        exts -= drop

    levels = _parse_levels(args.levels)
    out_dir = args.out_dir if args.out_dir is not None \
        else _default_out_dir(library_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    built_any = False
    for root_id, root_path in roots:
        if not root_path.is_dir():
            print(f"root not found: {root_path} (id {root_id})",
                  file=sys.stderr)
            return 2
        files = walk_library(root_path, exts)
        if not files:
            print(f"root {root_id} ({root_path}): no images found",
                  file=sys.stderr)
            continue
        out = out_dir / fcache_name(root_id)
        header = {
            "sidecar_version": 2,
            "library_id": library_id,
            "root_id": root_id,
        }
        _build_one_cache(files, root_path, out, levels, args.jobs, header)
        built_any = True
        print(f"root {root_id}: {len(files)} thumbs -> {out}")
    if not built_any:
        print("no images found in any root", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--library", type=Path, default=CACHE / "benchmark-library")
    ap.add_argument("--out", type=Path, default=CACHE / "benchmark-thumbs.fcache")
    ap.add_argument(
        "--library-home",
        type=Path,
        default=None,
        help="path to a library-home dir with .fauxcasa/library.json "
        "(multiroot .c, design §11): build one thumbs-<root_id>.fcache "
        "per watched root instead of a single-root cache. Overrides "
        "--library/--out/--sidecar-only.",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="output directory for --library-home mode (default: "
        "cache/<sha256(library_id)[:16]>, matching thumbcache.cache_dir_for "
        "so the app can warm-start from a script-built cache)",
    )
    ap.add_argument("--jobs", type=int, default=None)
    ap.add_argument(
        "--levels",
        type=str,
        default=None,
        help="comma-separated thumbnail long-edges for a multi-resolution "
        "(v2) cache, e.g. 512,256,128, or the word 'recommended' "
        f"({','.join(map(str, RECOMMENDED_LEVELS))}). Omit for the single-level "
        "v1 cache (256 px) that is byte-identical to the legacy format. A "
        "level set should include 256 so the tracer grid is unaffected.",
    )
    ap.add_argument(
        "--exclude-exts",
        type=str,
        default=None,
        metavar="EXTS",
        help="comma-separated extensions to leave OUT of the walk, e.g. "
        "'.psd,.tga' — the adopt-mode mirror of the app's File Types "
        "panel (fauxcasa-v46.4): a cache built with the same exclusions "
        "binds to a catalog walked with them",
    )
    ap.add_argument(
        "--sidecar-only",
        action="store_true",
        help="rewrite the JSON sidecar from a fresh library walk; "
        "requires an existing .fcache whose count matches",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="with --sidecar-only: overwrite even if the existing "
        "sidecar's files array disagrees with the fresh walk",
    )
    args = ap.parse_args(argv)
    if args.library_home is not None:
        return _main_library_home(args)
    levels = _parse_levels(args.levels)
    primary = _primary_level(levels)
    if not args.library.is_dir():
        print(f"library not found: {args.library}", file=sys.stderr)
        return 2

    exts = set(EXTS)
    if args.exclude_exts:
        drop = {e if e.startswith(".") else "." + e
                for e in (s.strip().lower()
                          for s in args.exclude_exts.split(",")) if e}
        unknown = drop - exts
        if unknown:
            print("--exclude-exts: unsupported extension(s): "
                  + ", ".join(sorted(unknown)), file=sys.stderr)
            return 2
        exts -= drop

    files = walk_library(args.library, exts)
    if not files:
        print("no images found", file=sys.stderr)
        return 2

    groups: list[dict] = []
    for i, p in enumerate(files):
        folder = p.parent.relative_to(args.library).as_posix()
        if not groups or groups[-1]["name"] != folder:
            groups.append({"name": folder, "start": i, "count": 0})
        groups[-1]["count"] += 1

    sidecar = {
        "count": len(files),
        # resolved, so consumers comparing against their own resolved
        # library root don't get CWD-dependent spurious mismatches
        "library": str(args.library.resolve()),
        "thumb_edge": levels[primary],
        "groups": groups,
        "files": [p.relative_to(args.library).as_posix() for p in files],
    }
    if len(levels) > 1:  # informational; the header is authoritative
        sidecar["levels"] = levels

    if args.sidecar_only:
        if not args.out.is_file():
            print(f"no existing cache at {args.out} — drop --sidecar-only "
                  f"to build one", file=sys.stderr)
            return 2
        try:
            with args.out.open("rb") as f:
                hdr = f.read(16)
                if len(hdr) < 16 or hdr[:4] != MAGIC:
                    raise ValueError("not an fcache file")
                version, cached, word3 = struct.unpack("<III", hdr[4:16])
                if version == 1:
                    cache_levels = [THUMB_EDGE]
                elif version == 2:
                    nlevels = word3 & 0xFFFF
                    ltbl = f.read(2 * nlevels)
                    if len(ltbl) != 2 * nlevels:
                        raise ValueError("truncated level table")
                    cache_levels = list(struct.unpack(f"<{nlevels}H", ltbl))
                else:
                    raise ValueError(f"unsupported fcache version {version}")
        except (OSError, ValueError) as e:
            print(f"cannot read {args.out}: {e}", file=sys.stderr)
            return 2
        # The cache header is authoritative on a rewrite — reflect ITS levels
        # in the sidecar, not whatever --levels says (which builds a new cache).
        sidecar["thumb_edge"] = cache_levels[_primary_level(cache_levels)]
        if len(cache_levels) > 1:
            sidecar["levels"] = cache_levels
        else:
            sidecar.pop("levels", None)
        if cached != len(files):
            print(
                f"library walk found {len(files)} files but {args.out} holds "
                f"{cached} thumbs — library changed since build, rebuild instead",
                file=sys.stderr,
            )
            return 2
        # Count alone can't catch equal-count drift, and a wrong rewrite
        # silently mislabels every thumb — require whatever ordering
        # evidence the old sidecar carries (files array on new ones,
        # groups on v1 sidecars) to match the fresh walk.
        sidecar_path = args.out.with_suffix(".fcache.json")
        if sidecar_path.is_file() and not args.force:
            try:
                old = json.loads(sidecar_path.read_text())
            except (OSError, ValueError):
                old = None
            if not isinstance(old, dict):
                old = {}
            old_files = old.get("files")
            old_groups = old.get("groups")
            why = None
            if old_files is not None and old_files != sidecar["files"]:
                if len(old_files) != len(sidecar["files"]):
                    why = (f"old sidecar lists {len(old_files)} files vs "
                           f"{len(files)} in the fresh walk")
                else:
                    diffs = sum(1 for a, b in zip(old_files, sidecar["files"])
                                if a != b)
                    why = f"{diffs} of {len(files)} file entries differ"
            elif old_files is None and old_groups is not None \
                    and old_groups != groups:
                why = "folder groups disagree with the fresh walk"
            if why is not None:
                print(
                    f"existing sidecar disagrees with the fresh walk "
                    f"({why}) — library changed since build; rebuild, or "
                    f"--force to overwrite anyway", file=sys.stderr,
                )
                return 2
        sidecar_path.write_text(json.dumps(sidecar, indent=1))
        print(f"sidecar rewritten for {len(files)} thumbs ({len(groups)} groups)")
        return 0

    nlevels = len(levels)
    single = _is_v1(levels)
    header_len = 16 if single else 16 + 2 * nlevels
    index = bytearray()
    offset = header_len + 16 * len(files) * nlevels
    worker = partial(_make_thumb, levels=levels)
    tmp = args.out.with_suffix(".tmp")
    with tmp.open("wb") as f:
        f.write(b"\x00" * offset)  # header (+ level table) + index placeholder
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            for n, plist in enumerate(pool.map(worker, files, chunksize=32)):
                for blob, w, h in plist:  # photo-major: every level of photo n
                    index += struct.pack("<QIHH", offset, len(blob), w, h)
                    f.write(blob)
                    offset += len(blob)
                if n % 10000 == 0:
                    print(f"  {n}/{len(files)}", file=sys.stderr)
        f.seek(0)
        if single:
            f.write(MAGIC + struct.pack("<III", 1, len(files), 0))
        else:
            f.write(MAGIC + struct.pack("<IIHH", 2, len(files), nlevels, 0))
            f.write(struct.pack(f"<{nlevels}H", *levels))
        f.write(index)
    tmp.replace(args.out)
    args.out.with_suffix(".fcache.json").write_text(json.dumps(sidecar, indent=1))
    lvl_note = "" if single else f", levels {','.join(map(str, levels))}"
    print(f"{len(files)} thumbs -> {args.out} "
          f"({args.out.stat().st_size / 1e6:.0f} MB, {len(groups)} groups{lvl_note})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
