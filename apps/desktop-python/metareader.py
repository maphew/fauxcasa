"""In-file metadata reader for the tracer: capture date, GPS, XMP Rating.

THE exiv2 isolation seam. Per the metadata-library decision
(docs/research/metadata-library-decision.md, fauxcasa-cam.16): the app wraps
the exiv2 engine via **python-exiv2** (PyPI package ``exiv2``), and every
call into that library lives in THIS module, behind a typed,
library-neutral interface — bytes in, plain data out — so the owner can
reverse the library choice by swapping this one file (pyexiv2 is the named
fallback binding: same engine, same flattened key strings).

BYTES-MODE ONLY, by decision: both exiv2 bindings fail to open non-ASCII
Windows paths *by filename* but read the same file's BYTES perfectly, and
bytes-in-hand is exactly the shape the indexer (thumbcache._index_one holds
the bytes for hashing + decode) and the future decode-sandbox broker
(docs/decode-threat-model.md: workers are handed bytes, never open paths)
already have. No function here takes a path.

FAIL-SOFT, same contract as inmeta.py: garbage/truncated/empty bytes, an
unsupported container, an unparseable field, or even a missing exiv2 wheel
yield ``FileMeta()`` (all None) — ``read_file_meta`` never raises, because
one corrupt photo must not abort an index over the whole library.

Scope: exactly the fields inmeta.py does NOT read — capture date
(fauxcasa-cam.9), GPS (fauxcasa-cam.10), XMP Rating (fauxcasa-cam.11).
inmeta.py remains the caption/keywords reader; the two run side by side on
the same bytes at index time. Containers: whatever exiv2 sniffs from the
bytes (JPEG/TIFF/PNG/WebP for the tracer's walk set; GIF/BMP fail soft).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

log = logging.getLogger("fauxcasa")


@dataclass(frozen=True)
class FileMeta:
    """The in-file fields this module reads. All-None = nothing usable."""

    # Canonical "YYYY-MM-DDTHH:MM:SS" from EXIF DateTimeOriginal (fallback
    # DateTime). The EXIF string is parsed HERE, by hand, with NO year floor:
    # scanned photos legally predate 1903 (spec §6 footgun 16 — Picasa's own
    # UI floor is a designed-away failure), so "1899-03-02T14:00:00" is a
    # first-class value. None when absent or garbage (e.g. the all-zeros
    # placeholder some cameras write).
    date_taken: str | None = None
    # Signed decimal degrees (lat, lon): EXIF GPS rationals + hemisphere
    # refs converted here (S/W negative). None when absent, unparseable, or
    # out of range (|lat| > 90, |lon| > 180 is treated as garbage).
    gps: tuple[float, float] | None = None
    # xmp:Rating as an int clamped to 0..5 (§3 star authority: stars are
    # 0-5 and map losslessly to Rating 1-5). None when the packet carries no
    # Rating at all — callers must distinguish "no opinion" (None) from an
    # explicit 0. A negative Rating (XMP -1 = rejected) clamps to 0 for now;
    # the M2 reverse-star work owns the -1 mapping.
    rating: int | None = None


EMPTY = FileMeta()

# One lock around every exiv2 call. The engine's XMP-SDK initialization is
# not thread-safe and the sibling binding (pyexiv2) documents global C++
# state outright; the indexer calls us from a thread pool. Serializing reads
# costs ~0.3 ms per photo (spike measurement) against a >= 30 photos/s §7
# budget — noise. The product's sandbox design uses a pool of *processes*
# (one job at a time each), where this lock is moot by construction.
_LOCK = threading.Lock()

_exiv2 = None
_import_failed = False


def _module():
    """Import python-exiv2 lazily, once; on failure warn ONCE and disable
    (N7: a library-wide capability loss is surfaced, not silent — but it
    degrades the fields, never the index)."""
    global _exiv2, _import_failed
    if _exiv2 is None and not _import_failed:
        try:
            import exiv2  # noqa: PLC0415 (deliberate lazy import)

            try:  # mute the engine's stderr chatter about odd-but-readable files
                exiv2.LogMsg.setLevel(exiv2.LogMsg.Level.mute)
            except Exception:  # noqa: BLE001 — best-effort across versions
                pass
            _exiv2 = exiv2
        except Exception as e:  # noqa: BLE001 — ImportError or binder faults
            _import_failed = True
            log.warning("python-exiv2 unavailable (%s) — in-file dates/GPS/"
                        "Rating will not be ingested this run", e)
    return _exiv2


# ---- field parsing (library output -> plain data) -------------------------

def _parse_exif_datetime(value: str | None) -> str | None:
    """EXIF 'YYYY:MM:DD HH:MM:SS' -> canonical 'YYYY-MM-DDTHH:MM:SS'.

    Hand-parsed on purpose: datetime.strptime would impose library year
    bounds and reject the pre-1903 dates footgun 16 protects; here the year
    is any non-negative integer (zero-padded to >= 4 digits so 4-digit years
    sort lexicographically). Lenient about '-' as the date separator (some
    writers deviate); garbage — including the all-zeros placeholder — is
    None, never an exception."""
    if not value:
        return None
    try:
        date_s, time_s = value.strip().split(None, 1)
        y_s, mo_s, dy_s = date_s.replace("-", ":").split(":")
        h_s, mi_s, s_s = time_s.strip().split(":")
        y, mo, dy = int(y_s), int(mo_s), int(dy_s)
        h, mi, s = int(h_s), int(mi_s), int(s_s)
    except (ValueError, TypeError):
        return None
    if y < 0 or not (1 <= mo <= 12) or not (1 <= dy <= 31):
        return None  # "0000:00:00 00:00:00" placeholders land here (month 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 60):  # 60: leap sec
        return None
    return f"{y:04d}-{mo:02d}-{dy:02d}T{h:02d}:{mi:02d}:{s:02d}"


def _rational_to_degrees(text: str | None, ref: str | None) -> float | None:
    """EXIF GPS '60/1 43/1 1650/100' (+ ref N/S/E/W) -> signed decimal
    degrees, S/W negative. None on any malformation."""
    if not text:
        return None
    try:
        parts = []
        for tok in text.split():
            num, _, den = tok.partition("/")
            parts.append(float(num) / float(den or 1))
        while len(parts) < 3:
            parts.append(0.0)
        deg = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
    except (ValueError, ZeroDivisionError):
        return None
    if ref and ref.strip().upper()[:1] in ("S", "W"):
        deg = -deg
    return round(deg, 6)  # ~0.1 m — beyond any camera's GPS precision


def _parse_rating(value: str | None) -> int | None:
    """xmp:Rating string -> int clamped to 0..5 (None = no Rating present).
    Accepts the float form ('3.0') some writers emit."""
    if value is None:
        return None
    try:
        r = int(float(value.strip()))
    except (ValueError, TypeError):
        return None
    return max(0, min(5, r))


# ---- the reader ------------------------------------------------------------

_EXIF_KEYS = frozenset((
    "Exif.Photo.DateTimeOriginal",
    "Exif.Image.DateTime",
    "Exif.GPSInfo.GPSLatitude", "Exif.GPSInfo.GPSLatitudeRef",
    "Exif.GPSInfo.GPSLongitude", "Exif.GPSInfo.GPSLongitudeRef",
))
_XMP_RATING_KEY = "Xmp.xmp.Rating"


def read_file_meta(data: bytes) -> FileMeta:
    """date_taken / gps / rating from one file's bytes. Never raises."""
    exiv2 = _module()
    if exiv2 is None or not data:
        return EMPTY
    exif: dict[str, str] = {}
    rating_raw: str | None = None
    try:
        with _LOCK:
            img = exiv2.ImageFactory.open(data)  # bytes-mode: sniffs the type
            img.readMetadata()
            for d in img.exifData():
                k = d.key()
                if k in _EXIF_KEYS:
                    exif[k] = d.toString()
            for d in img.xmpData():
                if d.key() == _XMP_RATING_KEY:
                    rating_raw = d.toString()
                    break
    except Exception:  # noqa: BLE001 — hostile bytes: exiv2 raises, we shrug
        return EMPTY

    date_taken = _parse_exif_datetime(
        exif.get("Exif.Photo.DateTimeOriginal")
        or exif.get("Exif.Image.DateTime"))

    gps: tuple[float, float] | None = None
    lat = _rational_to_degrees(exif.get("Exif.GPSInfo.GPSLatitude"),
                               exif.get("Exif.GPSInfo.GPSLatitudeRef"))
    lon = _rational_to_degrees(exif.get("Exif.GPSInfo.GPSLongitude"),
                               exif.get("Exif.GPSInfo.GPSLongitudeRef"))
    if lat is not None and lon is not None \
            and abs(lat) <= 90.0 and abs(lon) <= 180.0:
        gps = (lat, lon)

    return FileMeta(date_taken=date_taken, gps=gps,
                    rating=_parse_rating(rating_raw))


# ---- test-fixture support ---------------------------------------------------

def _degrees_to_rational(value: float) -> str:
    """|decimal degrees| -> EXIF 'D/1 M/1 SS*100/100' rational string."""
    v = abs(value)
    d = int(v)
    m = int((v - d) * 60.0)
    s100 = round(((v - d) * 60.0 - m) * 60.0 * 100.0)
    return f"{d}/1 {m}/1 {s100}/100"


def embed_test_metadata(data: bytes, *,
                        date_time_original: str | None = None,
                        date_time: str | None = None,
                        gps: tuple[float, float] | None = None,
                        rating: object | None = None) -> bytes:
    """Write date/GPS/Rating INTO image bytes — TEST-FIXTURE SUPPORT ONLY.

    This is NOT the §5 P1 product writer (no round-trip verification, no
    MakerNote care, no sidecar staging): it exists so test_tracer.py can
    synthesize privacy-safe fixtures with exactly-known metadata, and it
    lives here so every exiv2 call stays inside this one module (the
    library-swap seam). Raises on failure — a broken fixture must fail the
    test loudly, the opposite of read_file_meta's fail-soft contract.

    date_time_original/date_time are raw EXIF strings (caller controls the
    grammar, so tests can plant garbage); gps is signed decimal (lat, lon);
    rating is written as str(rating) so tests can plant out-of-range values.
    """
    exiv2 = _module()
    if exiv2 is None:
        raise RuntimeError("python-exiv2 is not importable")
    with _LOCK:
        img = exiv2.ImageFactory.open(data)
        img.readMetadata()
        exif = img.exifData()
        if date_time_original is not None:
            exif["Exif.Photo.DateTimeOriginal"] = date_time_original
        if date_time is not None:
            exif["Exif.Image.DateTime"] = date_time
        if gps is not None:
            lat, lon = gps
            exif["Exif.GPSInfo.GPSLatitude"] = _degrees_to_rational(lat)
            exif["Exif.GPSInfo.GPSLatitudeRef"] = "N" if lat >= 0 else "S"
            exif["Exif.GPSInfo.GPSLongitude"] = _degrees_to_rational(lon)
            exif["Exif.GPSInfo.GPSLongitudeRef"] = "E" if lon >= 0 else "W"
        if rating is not None:
            img.xmpData()[_XMP_RATING_KEY] = str(rating)
        img.writeMetadata()
        bio = img.io()
        view = bio.mmap()
        try:
            out = bytes(view)
        finally:
            bio.munmap()
    return out
