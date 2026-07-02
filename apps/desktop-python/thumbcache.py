"""Thumbnail cache layer for the tracer app.

Reads the project's packed fcache format (fcthumbs v1, written by
scripts/make-thumbcache.py) and can build one in-app for small
libraries. The cache is a disposable machine-local artifact (N3): it
lives under cache/tracer-cache/<digest>/, never inside the library
(N1). A catalog.json beside it records per-file (sha256, size, mtime)
so identity is content-hash + library-relative path (N6) and staleness
is checked by cheap signals (size + mtime) before trusting the cache.

The in-app builder is sequential and unoptimized — fine for fixture
libraries; the real indexer with its >= 30 photos/s budget (spec §7) is
deliberately out of tracer scope. Big pre-built caches (the 100k
benchmark fcache) are adopted via an explicit --thumbs path instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from catalog import Catalog
from inmeta import read_jpeg_metadata
from metareader import read_file_meta
from rawload import is_raw_suffix, raw_demosaic_qimage, raw_preview_jpeg

MAGIC = b"FCTC"
THUMB_EDGE = 256
JPEG_QUALITY = 80

# A multi-resolution (fcache v2) cache stores several long-edge levels per
# photo, declared in the header (largest first). The single-level default is
# byte-identical to the legacy v1 layout; pass an explicit level set to build
# v2. The grid reads the PRIMARY level — the 256 px tile it scales in paint —
# so a v2 cache that includes 256 leaves the grid and §7 behaviour unchanged
# and only adds larger levels for a future hi-DPI / loupe consumer (the
# product requirement behind fauxcasa-gtr; the viewer still reads originals).
RECOMMENDED_LEVELS = (512, THUMB_EDGE, 128)


class CacheError(Exception):
    pass


def _normalize_levels(levels: Sequence[int] | None) -> list[int]:
    """Canonical level set: unique positive long-edges, largest first. None or
    empty -> the single 256 px level, i.e. a v1 cache."""
    if not levels:
        return [THUMB_EDGE]
    out = sorted({int(e) for e in levels if int(e) > 0}, reverse=True)
    if not out:
        raise ValueError("levels must contain at least one positive edge")
    if out[0] > 0xFFFF:
        raise ValueError("a level edge must fit in a u16 (<= 65535)")
    if len(out) > 64:  # fail fast at build: load_cache caps nlevels at 64
        raise ValueError("at most 64 levels")
    return out


def _primary_level(levels: list[int]) -> int:
    """Index of the level the grid reads: exactly 256 px if present, else the
    largest level <= 256 (never hand the grid an oversized tile it just
    shrinks), else — every level is > 256 — the smallest. Levels are largest
    first, so the first <= 256 is the largest such."""
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
    records the true resolution; a v1 file is always read back as [256]."""
    return levels == [THUMB_EDGE]


@dataclass
class ThumbCache:
    path: Path
    count: int
    # PRIMARY level, ONE record per photo: (blob offset, blob length, thumb
    # width, thumb height); length 0 = the original failed to decode at build
    # time (error tile). A v1 cache has one level so this is the only level; a
    # v2 cache exposes every level via `level_entries` / `entry()`.
    entries: list[tuple[int, int, int, int]]
    files: list[str]  # library-relative POSIX paths, ONE per photo (entry order)
    library: str
    # Long-edge of each cached level, largest first (a v1 cache -> [256]).
    levels: list[int] = field(default_factory=lambda: [THUMB_EDGE])
    primary: int = 0  # index into `levels` that `entries` mirrors
    # Full per-level index: level_entries[level_idx][photo_idx]. Defaults to
    # the single primary level so v1 constructors need not supply it.
    level_entries: list[list[tuple[int, int, int, int]]] | None = None

    def __post_init__(self) -> None:
        if self.level_entries is None:
            self.level_entries = [self.entries]

    def best_level(self, min_edge: int) -> int:
        """Index of the cheapest cached level whose long edge is >= min_edge
        (the smallest sufficient level), or the largest level (index 0) when
        none is big enough. Lets a hi-DPI / loupe consumer ask for "at least
        N px" without knowing the level set."""
        best = 0
        for i, edge in enumerate(self.levels):
            if edge >= min_edge:
                best = i  # descending order: keep the smallest sufficient one
        return best

    def entry(self, photo_idx: int,
              level_idx: int | None = None) -> tuple[int, int, int, int]:
        """The (offset, length, w, h) record for a photo at a level (default:
        the primary level, == entries[photo_idx])."""
        li = self.primary if level_idx is None else level_idx
        return self.level_entries[li][photo_idx]


def load_cache(path: Path) -> ThumbCache:
    """Read a packed fcache. v1 (single 256 level) and v2 (multi-resolution,
    levels declared in the header) both load through here — the version field
    selects the layout, so the shipped v1 benchmark cache keeps loading with
    no rebuild."""
    with open(path, "rb") as f:
        hdr = f.read(16)
    if len(hdr) < 16 or hdr[:4] != MAGIC:
        raise CacheError(f"{path}: not an fcache file")
    # Word 3 is reserved (0) in v1 and packs nlevels in its low u16 in v2; the
    # version field disambiguates, so the same <III> read serves both.
    version, count, word3 = struct.unpack("<III", hdr[4:16])
    if version == 1:
        levels = [THUMB_EDGE]
        index_off = 16
    elif version == 2:
        nlevels = word3 & 0xFFFF
        if not 1 <= nlevels <= 64:
            raise CacheError(f"{path}: v2 header declares {nlevels} levels")
        with open(path, "rb") as f:
            f.seek(16)
            ltbl = f.read(2 * nlevels)
        if len(ltbl) != 2 * nlevels:
            raise CacheError(f"{path}: truncated level table")
        levels = list(struct.unpack(f"<{nlevels}H", ltbl))
        index_off = 16 + 2 * nlevels
    else:
        raise CacheError(f"{path}: unsupported fcache version {version}")

    nlevels = len(levels)
    with open(path, "rb") as f:
        f.seek(index_off)
        raw = f.read(count * nlevels * 16)
    if len(raw) != count * nlevels * 16:
        raise CacheError(f"{path}: truncated index")
    recs = [struct.unpack_from("<QIHH", raw, i * 16)
            for i in range(count * nlevels)]
    # Photo-major on disk: level l of photo p is record p*nlevels + l.
    level_entries = [[recs[p * nlevels + li] for p in range(count)]
                     for li in range(nlevels)]
    primary = _primary_level(levels)

    sidecar = path.with_suffix(".fcache.json")
    try:
        meta = json.loads(sidecar.read_text())
    except (OSError, ValueError) as e:
        raise CacheError(f"{sidecar}: unreadable sidecar ({e})") from e
    if not isinstance(meta, dict):
        raise CacheError(f"{sidecar}: sidecar is not a JSON object")
    files = meta.get("files")
    # files[] is ONE per photo (levels never multiply it), so a v2 cache binds
    # to the catalog exactly like a v1 one.
    if not isinstance(files, list) or len(files) != count:
        raise CacheError(
            f"{sidecar}: no per-entry 'files' array (old sidecar?) — "
            f"regenerate with scripts/make-thumbcache.py --sidecar-only"
        )
    return ThumbCache(
        path=path,
        count=count,
        entries=level_entries[primary],
        files=[str(f) for f in files],
        library=str(meta.get("library", "")),
        levels=levels,
        primary=primary,
        level_entries=level_entries,
    )


def bind(cache: ThumbCache, catalog: Catalog) -> None:
    """Entry i of the cache must be photo i of the catalog. Both sides
    derive their order from the same walk rule, so a mismatch means the
    library changed since the cache was built."""
    cat_files = [p.rel for p in catalog.photos]
    if cache.files != cat_files:
        if cache.count != len(cat_files):
            why = f"{cache.count} entries cached vs {len(cat_files)} found"
        else:
            i = next(i for i, (a, b) in enumerate(zip(cache.files, cat_files))
                     if a != b)
            why = (f"first mismatch at entry {i}: "
                   f"cached {cache.files[i]!r} vs on disk {cat_files[i]!r}")
        raise CacheError(
            f"cache {cache.path.name} does not match the library walk "
            f"({why}) — the library changed since the cache was built"
        )


def cache_dir_for(library: Path, cache_root: Path, variant: str = "") -> Path:
    key = str(library.resolve()).encode()
    if variant:
        key += b"\0" + variant.encode()
    digest = hashlib.sha256(key).hexdigest()[:16]
    return cache_root / digest


# Index workers: read + hash + scaled-decode + encode each photo on a
# thread pool. The load-bearing detail is SCALED decode (QImageReader
# .setScaledSize → libjpeg DCT-scaled decode straight to ~256 px): a
# naive full decode of a 24 MP original is the entire cost and holds the
# GIL the whole time, so threads gave zero speedup (~14 photos/s on 3 MB
# JPEGs, measured). Decoding directly at thumb resolution is ~30× cheaper
# AND releases the GIL enough that threads scale — ~430 photos/s on one
# core, ~1500 with 8, far past the §7 ≥30/s budget (fauxcasa-hw0). No
# multiprocessing or Pillow needed. Capped to keep the UI pump sane.
INDEX_WORKERS = min(8, (os.cpu_count() or 4))


@dataclass
class IndexResult:
    path: Path
    photos: int
    elapsed_s: float
    workers: int

    @property
    def rate(self) -> float:
        """Photos per second, including content hashing (the §7 metric)."""
        return self.photos / self.elapsed_s if self.elapsed_s > 0 else 0.0


def _index_one(root: Path, photo, idx: int, levels: list[int]):
    """One photo: read bytes, sha256 (N6 identity), SCALED-decode ONCE to the
    top (largest) level's box (libjpeg DCT decode — see INDEX_WORKERS), then
    downscale that decoded image to each lower level and JPEG-encode every
    level. Also reads in-file metadata from the bytes already in hand:
    caption/keywords (inmeta, XMP/IPTC, JPEG only) plus capture date / GPS /
    XMP Rating (metareader, the exiv2 seam, all carriers — fauxcasa-
    cam.9/.10/.11). Returns the per-level (blob, w, h) records the fcache
    needs, the catalog signals, and the PRIMARY-level QImage for the live grid
    feed. A null/corrupt image yields a zero-length blob per level (error
    tile). With a single 256 level this is byte-identical to the legacy path.

    EXIF orientation IS applied here (setAutoTransform): the thumbnail is
    baked display-upright, so the grid, the viewer (which auto-transforms the
    original), and both cache builders agree. The Picasa rotate= user
    quarter-turns are a separate, live display transform composed on top (see
    grid/viewer); a rotate change never invalidates the cache.

    RAW files are routed BY EXTENSION before QImageReader ever sniffs the
    bytes (TIFF-based RAW containers would fool it — rawload module doc):
    the embedded JPEG preview, when present, simply becomes the bytes the
    scaled-JPEG path below decodes (its own EXIF tag auto-applied, once);
    otherwise a half-size demosaic yields the image directly, flip already
    baked by LibRaw, so no second orientation pass ever runs."""
    from PySide6.QtCore import QBuffer, QIODevice, QSize, Qt
    from PySide6.QtGui import QImageReader

    top = levels[0]  # largest edge (levels are descending)
    primary_li = _primary_level(levels)

    src = root / photo.rel
    try:
        data = src.read_bytes()
        st = src.stat()
        size, mtime = st.st_size, int(st.st_mtime)
    except OSError:
        data, size, mtime = b"", -1, -1
    sha = hashlib.sha256(data).hexdigest()
    meta = read_jpeg_metadata(data)  # in-file caption/keywords (JPEG only)
    fmeta = read_file_meta(data)     # in-file date/GPS/Rating (all carriers)

    img = None
    if data and is_raw_suffix(photo.rel):
        jpeg = raw_preview_jpeg(data)
        if jpeg is not None:
            data = jpeg  # ride the ordinary scaled-JPEG decode below
        else:
            # No embedded preview: real demosaic at half size (plenty for
            # a <= 512 px thumb). Null on failure -> the error tile below.
            img = raw_demosaic_qimage(data, half_size=True)
    if img is None:
        # setData copies into the buffer's own QByteArray; passing a
        # temporary QByteArray to QBuffer(...) instead leaves a dangling
        # pointer (PySide6 frees the temporary) and every read silently
        # fails.
        buf = QBuffer()
        buf.setData(data)
        buf.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buf)
        # Apply EXIF orientation at decode (handles all 8 cases, mirrors
        # too); the thumbnail is stored display-upright. setScaledSize is in
        # pre-transform pixels — for a 90/270 image the box edges swap but
        # both stay <= top, and the post-read clamp below covers any header
        # that couldn't be pre-sized.
        reader.setAutoTransform(True)
        sz = reader.size()  # header-only; full pixels not decoded yet
        if sz.isValid() and (sz.width() > top or sz.height() > top):
            s = min(top / sz.width(), top / sz.height())
            reader.setScaledSize(QSize(max(1, round(sz.width() * s)),
                                       max(1, round(sz.height() * s))))
        img = reader.read()
    if img.isNull():
        return (idx, [(b"", 0, 0) for _ in levels], size, mtime, sha,
                meta, fmeta, None)
    # formats whose header size was unreadable skip the scaled decode above
    if img.width() > top or img.height() > top:
        img = img.scaled(
            top, top,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    level_blobs: list[tuple[bytes, int, int]] = []
    primary_img = None
    for li, edge in enumerate(levels):
        # Downscale the top image to each level; never upscale (a small
        # source caps every level at its native size, as v1 did at 256).
        if img.width() <= edge and img.height() <= edge:
            lvl = img
        else:
            lvl = img.scaled(
                edge, edge,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        out = QBuffer()
        out.open(QIODevice.OpenModeFlag.WriteOnly)
        lvl.save(out, "JPEG", JPEG_QUALITY)
        level_blobs.append((bytes(out.data()), lvl.width(), lvl.height()))
        if li == primary_li:
            primary_img = lvl
    return idx, level_blobs, size, mtime, sha, meta, fmeta, primary_img


def _write_fcache(out: Path, levels: list[int],
                  photo_levels: list[list[tuple[bytes, int, int]]]) -> None:
    """Atomically write a packed fcache. `photo_levels[i]` is photo i's list
    of (blob, w, h), one per level in `levels` order. A single level emits the
    legacy v1 layout byte-for-byte; multiple levels emit v2: a level table
    after the header, then a photo-major count*nlevels index, then the blobs
    photo-major (a photo's levels are contiguous)."""
    count = len(photo_levels)
    nlevels = len(levels)
    single = _is_v1(levels)
    header_len = 16 if single else 16 + 2 * nlevels
    index = bytearray()
    offset = header_len + 16 * count * nlevels
    for plist in photo_levels:
        for blob, w, h in plist:
            index += struct.pack("<QIHH", offset, len(blob), w, h)
            offset += len(blob)
    tmp = out.with_suffix(".tmp")
    with tmp.open("wb") as f:
        if single:
            f.write(MAGIC + struct.pack("<III", 1, count, 0))
        else:
            f.write(MAGIC + struct.pack("<IIHH", 2, count, nlevels, 0))
            f.write(struct.pack(f"<{nlevels}H", *levels))
        f.write(index)
        for plist in photo_levels:
            for blob, _w, _h in plist:
                f.write(blob)
    tmp.replace(out)


def build_cache(
    catalog: Catalog,
    cache_dir: Path,
    progress: Callable[[int, int, object], None] | None = None,
    cancel: threading.Event | None = None,
    levels: Sequence[int] | None = None,
) -> IndexResult | None:
    """Build thumbs.fcache for a (small) library and fill each photo's
    identity/staleness signals (size, mtime, sha256) into the catalog in
    place; the caller persists the catalog. Returns an IndexResult with
    the measured throughput, or None if cancelled.

    `levels` is the set of thumbnail long-edges to cache (default: the single
    256 px level, i.e. a v1 cache byte-identical to the legacy format). Pass a
    set such as RECOMMENDED_LEVELS = (512, 256, 128) for a multi-resolution v2
    cache (include 256 so the grid's primary level is unaffected).

    A thread pool runs the per-photo index work; `progress(i, total,
    qimage_or_None)` fires as each completes (out of order) so the grid
    shows tiles while indexing — the library stays browsable during the
    initial index (spec §7). A set `cancel` event abandons the build
    cleanly (nothing written)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    levels = _normalize_levels(levels)
    primary = _primary_level(levels)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / "thumbs.fcache"
    total = len(catalog.photos)
    photo_levels: list[list[tuple[bytes, int, int]]] = \
        [[(b"", 0, 0)] * len(levels) for _ in range(total)]

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=INDEX_WORKERS) as pool:
        futs = [pool.submit(_index_one, catalog.root, p, i, levels)
                for i, p in enumerate(catalog.photos)]
        for fut in as_completed(futs):
            if cancel is not None and cancel.is_set():
                pool.shutdown(wait=False, cancel_futures=True)
                return None
            idx, level_blobs, size, mtime, sha, meta, fmeta, img = \
                fut.result()
            photo_levels[idx] = level_blobs
            photo = catalog.photos[idx]
            photo.size, photo.mtime, photo.sha256 = size, mtime, sha
            # §4 precedence: in-file metadata wins over the ini for tier-1
            # data (captions, keywords) on JPEGs. scan_library already set
            # any ini value; override only when the file actually carries
            # one (truthy), so non-JPEGs, untagged JPEGs, and a JPEG with an
            # empty in-file caption all keep their ini caption/keywords.
            if meta.caption:
                photo.caption = meta.caption
            if meta.keywords:
                photo.keywords = meta.keywords
            # Same §4 tier-1 rule for the metareader fields
            # (fauxcasa-cam.9/.10/.11):
            if fmeta.date_taken:
                # the ini has no per-photo date key, so in-file is the only
                # source; None leaves the field for a consumer-side mtime
                # fallback (q6l.11 owns date grouping/sort)
                photo.date_taken = fmeta.date_taken
            if fmeta.gps is not None:
                # in-file EXIF GPS wins over the ini geotag= value the scan
                # set — §4 tier-1: in-file wins for standard metadata; the
                # ini key remains the source for files with no GPS in them.
                # The oracle check of this precedence is fauxcasa-ed5.9's.
                photo.geotag = fmeta.gps
            if fmeta.rating:
                # §3 star authority: XMP Rating 1-5 maps losslessly to the
                # star count and WINS over a bare ini star=yes (which
                # carries no count and imported as 1 at scan). An explicit
                # Rating 0 does NOT unstar an ini-starred photo: the ini
                # star= line stays authoritative for zero-vs-nonzero (§3).
                photo.star = fmeta.rating
            if progress:
                progress(idx, total, img)
    elapsed = time.perf_counter() - t0

    if cancel is not None and cancel.is_set():
        return None
    _write_fcache(out, levels, photo_levels)

    sidecar = {
        "count": total,
        "library": str(catalog.root),
        "thumb_edge": levels[primary],
        "files": [p.rel for p in catalog.photos],
    }
    if len(levels) > 1:  # informational; the header is authoritative
        sidecar["levels"] = levels
    out.with_suffix(".fcache.json").write_text(json.dumps(sidecar, indent=1))
    return IndexResult(path=out, photos=total, elapsed_s=elapsed,
                       workers=INDEX_WORKERS)
