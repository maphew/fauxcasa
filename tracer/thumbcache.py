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
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from catalog import Catalog

MAGIC = b"FCTC"
THUMB_EDGE = 256
JPEG_QUALITY = 80


class CacheError(Exception):
    pass


@dataclass
class ThumbCache:
    path: Path
    count: int
    # per entry: (blob offset, blob length, thumb width, thumb height);
    # length 0 = the original failed to decode at build time (error tile)
    entries: list[tuple[int, int, int, int]]
    files: list[str]  # library-relative POSIX paths, entry order
    library: str


def load_cache(path: Path) -> ThumbCache:
    with open(path, "rb") as f:
        hdr = f.read(16)
    if len(hdr) < 16 or hdr[:4] != MAGIC:
        raise CacheError(f"{path}: not an fcache file")
    version, count, _ = struct.unpack("<III", hdr[4:16])
    if version != 1:
        raise CacheError(f"{path}: unsupported fcache version {version}")
    with open(path, "rb") as f:
        f.seek(16)
        raw = f.read(count * 16)
    if len(raw) != count * 16:
        raise CacheError(f"{path}: truncated index")
    entries = [struct.unpack_from("<QIHH", raw, i * 16) for i in range(count)]

    sidecar = path.with_suffix(".fcache.json")
    try:
        meta = json.loads(sidecar.read_text())
    except (OSError, ValueError) as e:
        raise CacheError(f"{sidecar}: unreadable sidecar ({e})") from e
    if not isinstance(meta, dict):
        raise CacheError(f"{sidecar}: sidecar is not a JSON object")
    files = meta.get("files")
    if not isinstance(files, list) or len(files) != count:
        raise CacheError(
            f"{sidecar}: no per-entry 'files' array (old sidecar?) — "
            f"regenerate with scripts/make-thumbcache.py --sidecar-only"
        )
    return ThumbCache(
        path=path,
        count=count,
        entries=entries,
        files=[str(f) for f in files],
        library=str(meta.get("library", "")),
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


def cache_dir_for(library: Path, cache_root: Path) -> Path:
    digest = hashlib.sha256(str(library.resolve()).encode()).hexdigest()[:16]
    return cache_root / digest


def _cheap_signals(catalog: Catalog) -> list[dict]:
    out = []
    for p in catalog.photos:
        try:
            st = (catalog.root / p.rel).stat()
            size, mtime = st.st_size, int(st.st_mtime)
        except OSError:
            size, mtime = -1, -1
        out.append({"rel": p.rel, "size": size, "mtime": mtime})
    return out


def stale(cache_dir: Path, catalog: Catalog) -> bool:
    """Cheap-signal check (size + mtime, no hashing) per N6."""
    meta_path = cache_dir / "catalog.json"
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return True
    if not isinstance(meta, dict):
        return True
    cached = [
        {"rel": f["rel"], "size": f["size"], "mtime": f["mtime"]}
        for f in meta.get("files", [])
    ]
    return cached != _cheap_signals(catalog)


def build_cache(
    catalog: Catalog,
    cache_dir: Path,
    progress: Callable[[int, int, object], None] | None = None,
    cancel: threading.Event | None = None,
) -> Path | None:
    """Build thumbs.fcache + catalog.json for a (small) library.

    Decodes with Qt (QImage) so the GUI needs no extra image deps; runs
    on a background thread. `progress(i, total, qimage_or_None)` fires
    per photo so the grid can show tiles while indexing — the library
    stays browsable during the initial index (spec §7). A set `cancel`
    event abandons the build cleanly (returns None, nothing written).
    """
    from PySide6.QtCore import QBuffer, QIODevice, Qt
    from PySide6.QtGui import QImage

    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / "thumbs.fcache"
    total = len(catalog.photos)

    blobs: list[bytes] = []
    dims: list[tuple[int, int]] = []
    hashes: list[str] = []
    for i, photo in enumerate(catalog.photos):
        if cancel is not None and cancel.is_set():
            return None
        src = catalog.root / photo.rel
        try:
            data = src.read_bytes()
        except OSError:
            data = b""
        hashes.append(hashlib.sha256(data).hexdigest())
        img = QImage.fromData(data)
        if img.isNull():
            blobs.append(b"")
            dims.append((0, 0))
            if progress:
                progress(i, total, None)
            continue
        if img.width() > THUMB_EDGE or img.height() > THUMB_EDGE:
            img = img.scaled(
                THUMB_EDGE, THUMB_EDGE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "JPEG", JPEG_QUALITY)
        blobs.append(bytes(buf.data()))
        dims.append((img.width(), img.height()))
        if progress:
            progress(i, total, img)

    if cancel is not None and cancel.is_set():
        return None
    index = bytearray()
    offset = 16 + 16 * total
    tmp = out.with_suffix(".tmp")
    with tmp.open("wb") as f:
        f.write(MAGIC + struct.pack("<III", 1, total, 0))
        for blob, (w, h) in zip(blobs, dims):
            index += struct.pack("<QIHH", offset, len(blob), w, h)
            offset += len(blob)
        f.write(index)
        for blob in blobs:
            f.write(blob)
    tmp.replace(out)

    out.with_suffix(".fcache.json").write_text(json.dumps({
        "count": total,
        "library": str(catalog.root),
        "thumb_edge": THUMB_EDGE,
        "files": [p.rel for p in catalog.photos],
    }, indent=1))
    signals = _cheap_signals(catalog)
    for sig, sha in zip(signals, hashes):
        sig["sha256"] = sha
    (cache_dir / "catalog.json").write_text(json.dumps({
        "library": str(catalog.root),
        "files": signals,
    }, indent=1))
    return out
