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

Scope: capture date (fauxcasa-cam.9), GPS (fauxcasa-cam.10), XMP Rating
(fauxcasa-cam.11), the raw EXIF Orientation value (read_orientation,
fauxcasa-cam.4 — a VIEW-time read for the face overlay, not an index-time
field), plus, since fauxcasa-cam.5, faces-in-XMP (mwg-rs RegionInfo) and a
library-neutral caption/keywords pair read from XMP dc:description/
dc:subject. The caption/keywords fields here exist so non-JPEG containers
(TIFF/RAW, PNG, WebP) get tier-1 ingest too — inmeta.py remains the
JPEG-specific reader (XMP + its IPTC mirror) and stays authoritative for
JPEG at the merge site (thumbcache.apply_photo_meta): these fields are the
fallback for the containers inmeta.py cannot parse. Containers: whatever
exiv2 sniffs from the bytes (JPEG/TIFF/PNG/WebP for the tracer's walk set;
GIF/BMP fail soft).

Faces-in-XMP (mwg-rs RegionInfo, per the MWG Metadata Guidelines): each
region is a normalized (0..1) center x/y + width/height rect inside
``Xmp.mwg-rs.Regions/mwg-rs:RegionList[N]/...``; this module converts to
the same (left, top, right, bottom) STORED-pixel-fraction rect the ini
``faces=`` grammar uses (picasa-ini-format.md), clamped to [0, 1], so a
downstream merge (catalog/thumbcache) can compare XMP faces and ini faces
by rect geometry without knowing which store a photo came from. No
contact ids here — those are a Picasa-catalog concept; a bare (rect,
name-or-None) pair is the library-neutral shape.
"""

from __future__ import annotations

import logging
import math
import struct
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
    # dc:description (LangAlt: x-default, else the first alternative).
    # None when absent or blank — same "truthy wins" contract inmeta.py
    # uses, so a merge site can treat "" and None identically.
    caption: str | None = None
    # dc:subject (Bag), in packet order. Empty tuple when absent.
    keywords: tuple[str, ...] = ()
    # mwg-rs Face regions (fauxcasa-cam.5): ((left, top, right, bottom),
    # name-or-None) pairs, rect fractions of the STORED pixels (same frame
    # as ini faces=, clamped to [0, 1]). Only mwg-rs:Type == "Face" (or a
    # region with no Type at all — MWG's documented default) and
    # stArea:unit == "normalized" (or absent) regions are included; other
    # types (Pet, BarCode, ...) and pixel-unit regions are skipped. Capped
    # at _FACE_CAP entries so a hostile packet cannot balloon the catalog.
    faces: tuple[tuple[tuple[float, float, float, float], str | None], ...] \
        = ()


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
    Accepts the float form ('3.0') some writers emit. A non-finite writer
    value ('inf', '-inf', '1e999' — the last parses to a Python float
    'inf') must not raise: int() on an infinite float raises
    OverflowError, which would otherwise escape read_file_meta's
    never-raises contract and abort the whole index build."""
    if value is None:
        return None
    try:
        f = float(value.strip())
        if not math.isfinite(f):
            return None
        r = int(f)
    except (ValueError, OverflowError, TypeError):
        return None
    return max(0, min(5, r))


def _parse_caption(value: str | None) -> str | None:
    """dc:description toString() -> the x-default (or only) alternative's
    text. python-exiv2 renders a LangAlt as ``lang="<code>" <text>``
    (confirmed against jpeg_full's XMP: docs/research/spikes/
    metadata-lib-spike.py read_exiv2); a plain (non-LangAlt) value passes
    through unchanged. Blank -> None."""
    if not value:
        return None
    if value.startswith("lang="):
        value = value.split(" ", 1)[-1] if " " in value else ""
    value = value.strip()
    return value or None


def _parse_subject(value: str | None) -> tuple[str, ...]:
    """dc:subject toString() -> its comma-joined Bag items, split back out
    (same rendering as _parse_caption's LangAlt: exiv2's Value::toString()
    joins XMP arrays with ", "). A keyword containing a literal comma is
    not distinguishable from two keywords — the same limitation the ini
    keywords= reader (catalog.py) already has."""
    if not value:
        return ()
    return tuple(k.strip() for k in value.split(",") if k.strip())


def _parse_mwg_faces(xmp: dict[str, str]
                     ) -> tuple[tuple[tuple[float, float, float, float],
                                      str | None], ...]:
    """mwg-rs RegionList (already filtered into `xmp` by read_file_meta) ->
    (rect, name-or-None) pairs. See FileMeta.faces for the field contract."""
    faces: list[tuple[tuple[float, float, float, float], str | None]] = []
    for i in range(1, _REGION_INDEX_CAP + 1):
        if len(faces) >= _FACE_CAP:
            break
        prefix = f"{_MWG_REGIONLIST}[{i}]/mwg-rs:Area/stArea:"
        if prefix + "x" not in xmp:
            break  # RegionList is a contiguous Bag: no gaps, so this ends it
        region_type = xmp.get(f"{_MWG_REGIONLIST}[{i}]/mwg-rs:Type")
        if region_type is not None and region_type.strip().lower() not in \
                ("", "face"):
            continue
        unit = xmp.get(prefix + "unit")
        if unit is not None and unit.strip().lower() not in \
                ("", "normalized"):
            continue
        try:
            x = float(xmp[prefix + "x"])
            y = float(xmp[prefix + "y"])
            w = float(xmp[prefix + "w"])
            h = float(xmp[prefix + "h"])
        except (KeyError, ValueError, TypeError):
            continue
        if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0:
            continue
        left = max(0.0, min(1.0, x - w / 2.0))
        top = max(0.0, min(1.0, y - h / 2.0))
        right = max(0.0, min(1.0, x + w / 2.0))
        bottom = max(0.0, min(1.0, y + h / 2.0))
        if right <= left or bottom <= top:
            continue  # degenerate after clamping (e.g. entirely off-frame)
        name = (xmp.get(f"{_MWG_REGIONLIST}[{i}]/mwg-rs:Name") or "").strip()
        faces.append(((left, top, right, bottom), name or None))
    return tuple(faces)


def _xmp_bytes_to_tiff_shell(xmp: bytes) -> bytes:
    """Wrap a raw (unprefixed) XMP packet in a minimal single-IFD TIFF so
    it can be handed to exiv2.ImageFactory.open/readMetadata — the same
    machinery every other read in this module already uses.

    Why a TIFF shell and not a JPEG one: a JPEG APP1 segment's length is a
    16-bit field (max 65533 bytes of payload), which is exactly the
    constraint that forces ExtendedXMP to exist in the first place — an
    ExtendedXMP packet is BY DEFINITION over that ceiling, so it could
    never round-trip through a synthetic single-segment JPEG. TIFF's XMP
    tag (0x02BC) is a plain byte array with a 32-bit offset/count, no such
    limit. This is also why this module doesn't call exiv2.XmpParser
    directly: the installed python-exiv2 binding (0.19.2) exposes
    XmpParser.encode but not a decode-to-XmpData entry point, so the
    "open something exiv2 already knows how to read" trick is the
    available seam, not a preference."""
    ifd_off = 8
    entry = struct.pack("<HHI", 0x02BC, 1, len(xmp)) + struct.pack(
        "<I", ifd_off + 2 + 12 + 4)
    ifd = struct.pack("<H", 1) + entry + struct.pack("<I", 0)
    return b"II" + struct.pack("<H", 42) + struct.pack("<I", ifd_off) \
        + ifd + xmp


def _read_extended_xmp(exiv2, extended_xmp: bytes) -> dict[str, str]:
    """Parse a reassembled ExtendedXMP packet (inmeta.extended_xmp's
    output — raw, unprefixed) into the same filtered key->toString() dict
    read_file_meta builds for the main packet. Fail-soft: any failure
    (including a still-oversized packet the TIFF shell itself can't hold,
    though that would need a >4 GiB packet) yields an empty dict, never an
    exception — an unusable extension must not cost the main packet's
    already-parsed fields."""
    out: dict[str, str] = {}
    try:
        shell = _xmp_bytes_to_tiff_shell(extended_xmp)
        img = exiv2.ImageFactory.open(shell)
        img.readMetadata()
        for d in img.xmpData():
            k = d.key()
            if k == _XMP_RATING_KEY or k == _CAPTION_KEY or k == _SUBJECT_KEY \
                    or k.startswith(_MWG_REGIONLIST):
                out[k] = d.toString()
    except Exception:  # noqa: BLE001 — malformed extension: degrade quietly
        return {}
    return out


# ---- the reader ------------------------------------------------------------

_EXIF_KEYS = frozenset((
    "Exif.Photo.DateTimeOriginal",
    "Exif.Image.DateTime",
    "Exif.GPSInfo.GPSLatitude", "Exif.GPSInfo.GPSLatitudeRef",
    "Exif.GPSInfo.GPSLongitude", "Exif.GPSInfo.GPSLongitudeRef",
))
_XMP_RATING_KEY = "Xmp.xmp.Rating"
_ORIENTATION_KEY = "Exif.Image.Orientation"
_CAPTION_KEY = "Xmp.dc.description"
_SUBJECT_KEY = "Xmp.dc.subject"
_MWG_REGIONLIST = "Xmp.mwg-rs.Regions/mwg-rs:RegionList"

# Faces-in-XMP caps (fauxcasa-cam.5): _FACE_CAP bounds the OUTPUT (a
# hostile packet cannot balloon the catalog); _REGION_INDEX_CAP bounds how
# far the 1-based mwg-rs:RegionList[N] scan looks before giving up, even
# when every region is skipped as non-Face/non-normalized (so a packet
# full of *rejected* regions cannot spin the loop unboundedly either).
_FACE_CAP = 64
_REGION_INDEX_CAP = 4096


def read_orientation(data: bytes) -> int:
    """EXIF Orientation (tag 274) from one file's bytes: 1..8, fail-soft 1.

    Exists for the viewer's face overlay (fauxcasa-cam.4): faces= rect64
    fractions are relative to the STORED pixels, but every display path
    decodes EXIF-upright (autoTransform), so the overlay must know the
    stored file's orientation value to map stored-pixel rects into the
    upright frame. Read at VIEW time from the bytes the viewer already
    holds — deliberately NOT persisted in the catalog (no schema bump).
    Anything unusable — missing exiv2 wheel, garbage/empty bytes, an
    absent tag, a value outside 1..8 — is 1 ("display == stored"), the
    same fail-soft contract as read_file_meta: a wrong-but-bounded box
    beats an exception in a paint path."""
    exiv2 = _module()
    if exiv2 is None or not data:
        return 1
    try:
        with _LOCK:
            img = exiv2.ImageFactory.open(data)  # bytes-mode: sniffs the type
            img.readMetadata()
            for d in img.exifData():
                if d.key() == _ORIENTATION_KEY:
                    v = int(d.toString().strip())
                    return v if 1 <= v <= 8 else 1
    except Exception:  # noqa: BLE001 — hostile bytes: exiv2 raises, we shrug
        return 1
    return 1


def read_file_meta(data: bytes, extended_xmp: bytes | None = None) -> FileMeta:
    """date_taken / gps / rating / caption / keywords / faces from one
    file's bytes. Never raises.

    `extended_xmp`: the reassembled ExtendedXMP packet (inmeta.
    extended_xmp(data)'s output — raw, unprefixed), when the caller has
    one. exiv2 does NOT reassemble ExtendedXMP itself (confirmed: neither
    writing nor reading a split packet round-trips through python-exiv2
    0.19.2 without this), so the fields it carries are read via a small
    TIFF-shell trick (_read_extended_xmp) and used to FILL keys the main
    packet lacks — a field (or a single mwg-rs region, keyed by its own
    index) present in the main packet always wins over the extension."""
    exiv2 = _module()
    if exiv2 is None or not data:
        return EMPTY
    exif: dict[str, str] = {}
    xmp: dict[str, str] = {}
    try:
        with _LOCK:
            img = exiv2.ImageFactory.open(data)  # bytes-mode: sniffs the type
            img.readMetadata()
            for d in img.exifData():
                k = d.key()
                if k in _EXIF_KEYS:
                    exif[k] = d.toString()
            for d in img.xmpData():
                k = d.key()
                # Filtered, not a full dump: an XMP graph can carry
                # arbitrary unrelated data (thumbnails, editor history)
                # that this module has no business holding in memory.
                if k == _XMP_RATING_KEY or k == _CAPTION_KEY \
                        or k == _SUBJECT_KEY \
                        or k.startswith(_MWG_REGIONLIST):
                    xmp[k] = d.toString()
            if extended_xmp:
                for k, v in _read_extended_xmp(exiv2, extended_xmp).items():
                    xmp.setdefault(k, v)  # main packet wins per key
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
                    rating=_parse_rating(xmp.get(_XMP_RATING_KEY)),
                    caption=_parse_caption(xmp.get(_CAPTION_KEY)),
                    keywords=_parse_subject(xmp.get(_SUBJECT_KEY)),
                    faces=_parse_mwg_faces(xmp))


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
                        rating: object | None = None,
                        orientation: int | None = None,
                        caption: str | None = None,
                        keywords: list[str] | None = None,
                        faces: list[tuple[str | None, float, float,
                                          float, float]] | None = None,
                        ) -> bytes:
    """Write date/GPS/Rating/caption/keywords/faces INTO image bytes —
    TEST-FIXTURE SUPPORT ONLY.

    This is NOT the §5 P1 product writer (no round-trip verification, no
    MakerNote care, no sidecar staging): it exists so test_tracer.py can
    synthesize privacy-safe fixtures with exactly-known metadata, and it
    lives here so every exiv2 call stays inside this one module (the
    library-swap seam). Raises on failure — a broken fixture must fail the
    test loudly, the opposite of read_file_meta's fail-soft contract.

    date_time_original/date_time are raw EXIF strings (caller controls the
    grammar, so tests can plant garbage); gps is signed decimal (lat, lon);
    rating is written as str(rating) so tests can plant out-of-range values;
    orientation writes EXIF tag 274 verbatim (tests plant all 8 cases —
    and out-of-range values to prove read_orientation's fail-soft).

    caption -> dc:description (exiv2 renders it as a LangAlt automatically,
    per its built-in XMP property schema); keywords -> dc:subject, one
    bracket-assignment per item (confirmed to APPEND to the existing Bag
    rather than overwrite — exiv2's XmpData::operator[] semantics).

    faces -> mwg-rs:RegionList, one Bag entry per (name, x, y, w, h) tuple
    (x/y/w/h already normalized center+dims, the MWG/XMP wire shape;
    name=None omits mwg-rs:Name entirely, matching a real unnamed/
    suggested region). The Bag container itself must exist before any
    indexed sub-key can be assigned (XMP Toolkit error 102 "Indexing
    applied to non-array" otherwise) — built once, on first use.
    """
    exiv2 = _module()
    if exiv2 is None:
        raise RuntimeError("python-exiv2 is not importable")
    with _LOCK:
        img = exiv2.ImageFactory.open(data)
        img.readMetadata()
        exif = img.exifData()
        xmp = img.xmpData()
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
            xmp[_XMP_RATING_KEY] = str(rating)
        if orientation is not None:
            exif[_ORIENTATION_KEY] = str(orientation)
        if caption is not None:
            xmp[_CAPTION_KEY] = caption
        if keywords:
            for kw in keywords:
                xmp[_SUBJECT_KEY] = kw
        if faces:
            base = exiv2.XmpTextValue()
            base.setXmpArrayType(exiv2.XmpValue.XmpArrayType.xaBag)
            xmp.add(exiv2.XmpKey(_MWG_REGIONLIST), base)
            for i, (name, x, y, w, h) in enumerate(faces, start=1):
                prefix = f"{_MWG_REGIONLIST}[{i}]/mwg-rs:"
                if name is not None:
                    xmp[prefix + "Name"] = name
                xmp[prefix + "Type"] = "Face"
                xmp[prefix + "Area/stArea:x"] = str(x)
                xmp[prefix + "Area/stArea:y"] = str(y)
                xmp[prefix + "Area/stArea:w"] = str(w)
                xmp[prefix + "Area/stArea:h"] = str(h)
                xmp[prefix + "Area/stArea:unit"] = "normalized"
        img.writeMetadata()
        bio = img.io()
        view = bio.mmap()
        try:
            out = bytes(view)
        finally:
            bio.munmap()
    return out
