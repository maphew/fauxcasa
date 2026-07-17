"""In-file JPEG metadata reader for the tracer app: captions and keywords
stored *inside* the photo (XMP + IPTC), not in the .picasa.ini sidecar.

Why this exists: real Picasa 3.9 writes a JPEG's caption to XMP
``dc:description`` + an IPTC 8BIM block (dataset 2:120) and its keywords to
XMP ``dc:subject`` + IPTC 2:25 — the ini ``caption=``/``keywords=`` keys are
used *only* for formats with no IPTC/XMP home (non-JPEG). See
``docs/research/picasa-ini-format.md`` (per-file keys table) and the
``Caption``/``Keyword`` rows of ``docs/research/wine-oracle.md`` (fixtures
002/003). A reader that looks only at the ini is therefore blind to every
JPEG caption — the gap this module closes for the §4 tier-1 ingest contract.

Scope and stance (a tracer, not the product writer):
- READ ONLY. Two fields only — caption and keywords — the tier-1 data the
  grid/search/viewer surface. Faces-in-XMP, geotags, dates, and the full
  XMP graph are out of tracer scope (the product wraps a mature metadata
  library, spec §5 P1 — "wrap, don't reimplement"). This is a small, self-
  contained pure-Python segment walker: no new dependencies, matching the
  tracer's minimalism, and privacy-safe to test with synthetic fixtures.
- FAIL SOFT. Any malformed segment, truncation, or undecodable text yields
  no metadata for that field, never an exception — a corrupt APP block in
  one photo must not abort an index over the whole library.
- EXIF *orientation* is deliberately NOT read here: it is applied at decode
  by Qt's QImageReader.setAutoTransform / PIL's exif_transpose (which handle
  all eight orientations, mirrors included), baked into the thumbnail and
  applied to the viewed original, then composed with the Picasa ``rotate=``
  user quarter-turns. See apps/desktop-python/README.md "EXIF orientation".

Precedence within the file: XMP wins over IPTC when both are present. Picasa
writes the two together and they agree, but XMP is its primary store (the
IPTC 8BIM block is the legacy mirror), so XMP is authoritative here.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

# JPEG markers we care about (all big-endian, per JFIF/EXIF).
_SOI = 0xD8
_EOI = 0xD9
_SOS = 0xDA
_APP1 = 0xE1  # EXIF or XMP
_APP13 = 0xED  # Photoshop Image Resource Block (carries IPTC-NAA)

_XMP_PREFIX = b"http://ns.adobe.com/xap/1.0/\x00"
_PHOTOSHOP_PREFIX = b"Photoshop 3.0\x00"
_IPTC_NAA_ID = 0x0404  # 8BIM resource type holding the IPTC IIM stream

# IPTC IIM datasets (record 2, the "application" record).
_IIM_CAPTION = 120  # 2:120 Caption/Abstract
_IIM_KEYWORDS = 25  # 2:25 Keywords (repeatable)

# XMP / RDF / Dublin Core namespaces.
_NS_RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
_NS_DC = "{http://purl.org/dc/elements/1.1/}"


@dataclass(frozen=True)
class InMeta:
    """In-file caption + keywords. Empty when the file is not a JPEG, has no
    in-file metadata, or could not be parsed."""

    caption: str | None = None
    keywords: tuple[str, ...] = ()


EMPTY = InMeta()


def apply_orientation(img, orientation: int):
    """Apply an EXIF Orientation value (1..8) to a decoded QImage, returning
    the display-upright result (the same image object, unchanged, for 1 or
    any value outside 1..8).

    Why this lives here instead of QImageReader.setAutoTransform: a
    proven GIL <-> Qt-mutex lock inversion (fauxcasa-5dk, live py-spy
    native dump). QImageReader's format-probe loop holds Qt's image-plugin
    factory mutex and calls device.read()/peek() per candidate plugin; when
    the device is a Python-created QBuffer, every virtual read() call goes
    through shiboken's Sbk_GetPyOverride, which blocks acquiring the GIL --
    WHILE HOLDING THE QT MUTEX. A concurrent QImage.fromData on another
    thread (PySide does not release the GIL for it) can then block on that
    same factory mutex, and the two threads deadlock. The fix is to keep
    the invariant "no thread ever needs the GIL while holding Qt's
    image-plugin factory mutex": decode with QImage.fromData (an internal
    C++ buffer, no Python device, no plugin-mutex GIL reentry) and apply
    EXIF orientation ourselves afterward, here.

    This function MUST stay in lockstep with viewer._ORIENT_MAP, which
    documents (and the face-overlay math depends on) the exact same eight
    transforms applied to fractional coordinates rather than pixels."""
    from PySide6.QtGui import QTransform

    if orientation == 2:
        # QImage.mirrored(bool, bool) is deprecated in PySide6; an
        # equivalent QTransform scale keeps the same pixel semantics.
        return img.transformed(QTransform().scale(-1, 1))
    if orientation == 3:
        return img.transformed(QTransform().rotate(180))
    if orientation == 4:
        return img.transformed(QTransform().scale(1, -1))
    if orientation == 5:
        return img.transformed(QTransform(0, 1, 1, 0, 0, 0))
    if orientation == 6:
        # Qt's y-down coordinate space makes a positive rotate() angle a
        # clockwise turn.
        return img.transformed(QTransform().rotate(90))
    if orientation == 7:
        return img.transformed(QTransform(0, -1, -1, 0, 0, 0))
    if orientation == 8:
        return img.transformed(QTransform().rotate(-90))
    return img  # 1, or any out-of-range value: identity


def _decode(raw: bytes) -> str:
    """IPTC strings are UTF-8 in modern Picasa output (XMP confirms it);
    fall back to latin-1 so legacy bytes still round-trip rather than vanish."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


def _iter_segments(data: bytes):
    """Yield (marker, payload) for each JPEG marker segment up to the start
    of compressed data (SOS) or EOI. Bails silently on anything that isn't a
    well-formed marker stream — the caller treats a bail as 'no metadata'."""
    if len(data) < 2 or data[0] != 0xFF or data[1] != _SOI:
        return
    pos = 2
    n = len(data)
    while pos + 1 < n:
        if data[pos] != 0xFF:
            return  # desynchronized; refuse to guess
        marker = data[pos + 1]
        pos += 2
        # Fill bytes: any run of 0xFF before the real marker byte.
        while marker == 0xFF and pos < n:
            marker = data[pos]
            pos += 1
        if marker in (_EOI, _SOS):
            return
        # Standalone markers (RSTn, TEM) carry no length field; none appear
        # before SOS in practice, but skip defensively rather than misread.
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > n:
            return
        seg_len = int.from_bytes(data[pos:pos + 2], "big")
        if seg_len < 2:
            return  # a segment length includes its own 2 bytes; <2 is corrupt
        start = pos + 2
        end = pos + seg_len
        if end > n:
            return  # truncated segment
        yield marker, data[start:end]
        pos = end


def _parse_iptc(payload: bytes) -> InMeta:
    """Pull caption (2:120) and keywords (2:25) from an APP13 Photoshop IRB.
    Walks 8BIM resource blocks to the IPTC-NAA (0x0404) block, then the IIM
    dataset stream inside it."""
    if not payload.startswith(_PHOTOSHOP_PREFIX):
        return EMPTY
    i = len(_PHOTOSHOP_PREFIX)
    n = len(payload)
    iim = b""
    while i + 12 <= n:
        if payload[i:i + 4] != b"8BIM":
            break
        res_id = int.from_bytes(payload[i + 4:i + 6], "big")
        # Pascal name: length byte + name, padded so (1+len) is even.
        name_len = payload[i + 6]
        name_field = 1 + name_len
        if name_field % 2:
            name_field += 1
        j = i + 6 + name_field
        if j + 4 > n:
            break
        size = int.from_bytes(payload[j:j + 4], "big")
        data_start = j + 4
        data_end = data_start + size
        if data_end > n:
            break
        if res_id == _IPTC_NAA_ID:
            iim = payload[data_start:data_end]
            break
        # Resource data is padded to an even length.
        i = data_end + (size & 1)
    if not iim:
        return EMPTY

    caption: str | None = None
    keywords: list[str] = []
    k = 0
    m = len(iim)
    while k + 5 <= m:
        if iim[k] != 0x1C:
            break  # not a tag marker; stop rather than resync
        record = iim[k + 1]
        dataset = iim[k + 2]
        length = int.from_bytes(iim[k + 3:k + 5], "big")
        k += 5
        if length & 0x8000:  # extended-length form: low 15 bits = #octets
            nbytes = length & 0x7FFF
            if k + nbytes > m:
                break
            length = int.from_bytes(iim[k:k + nbytes], "big")
            k += nbytes
        if k + length > m:
            break
        value = iim[k:k + length]
        k += length
        if record == 2 and dataset == _IIM_CAPTION and caption is None:
            # Normalize like the XMP and keyword paths: an empty (or
            # whitespace/NUL-padded) 2:120 means "no caption", not "".
            caption = _decode(value).strip().strip("\x00").strip() or None
        elif record == 2 and dataset == _IIM_KEYWORDS:
            kw = _decode(value).strip()
            if kw:
                keywords.append(kw)
    return InMeta(caption=caption, keywords=tuple(keywords))


def _xmp_text(elem) -> str | None:
    """Text of a dc:* property: an rdf:Alt/rdf:li (language alternatives,
    prefer x-default), an rdf:Bag/rdf:Seq first item, or a plain text node."""
    alt = elem.find(f"{_NS_RDF}Alt")
    if alt is not None:
        lis = alt.findall(f"{_NS_RDF}li")
        for li in lis:
            if li.get("{http://www.w3.org/XML/1998/namespace}lang") == \
                    "x-default":
                return (li.text or "").strip() or None
        if lis:
            return (lis[0].text or "").strip() or None
    return (elem.text or "").strip() or None


def _xmp_bag(elem) -> tuple[str, ...]:
    """Items of a dc:subject rdf:Bag/rdf:Seq (keywords)."""
    for container in (f"{_NS_RDF}Bag", f"{_NS_RDF}Seq"):
        box = elem.find(container)
        if box is not None:
            out = [(li.text or "").strip()
                   for li in box.findall(f"{_NS_RDF}li")]
            return tuple(k for k in out if k)
    text = (elem.text or "").strip()
    return (text,) if text else ()


def _parse_xmp(payload: bytes) -> InMeta:
    """Pull caption (dc:description) and keywords (dc:subject) from an APP1
    XMP packet. Tolerant: a malformed packet yields nothing, never raises."""
    if not payload.startswith(_XMP_PREFIX):
        return EMPTY
    xml = payload[len(_XMP_PREFIX):]
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return EMPTY
    caption: str | None = None
    keywords: tuple[str, ...] = ()
    # dc:* properties live on rdf:Description elements; scan all of them so
    # we don't depend on a particular nesting depth or attribute layout.
    for desc in root.iter(f"{_NS_RDF}Description"):
        if caption is None:
            d = desc.find(f"{_NS_DC}description")
            if d is not None:
                caption = _xmp_text(d)
        if not keywords:
            s = desc.find(f"{_NS_DC}subject")
            if s is not None:
                keywords = _xmp_bag(s)
    return InMeta(caption=caption, keywords=keywords)


def read_jpeg_metadata(data: bytes) -> InMeta:
    """Caption + keywords from a JPEG's in-file metadata, the way Picasa 3.9
    stores them (XMP dc:description/dc:subject, mirrored to IPTC 2:120/2:25).
    XMP wins over IPTC per field. Returns EMPTY for non-JPEG bytes or when no
    in-file metadata is present. Never raises (fail-soft per photo).

    `data` is the file's bytes — the indexer already holds them (for hashing
    and the scaled decode), so reading metadata adds no extra I/O.
    """
    xmp = EMPTY
    iptc = EMPTY
    try:
        for marker, payload in _iter_segments(data):
            if marker == _APP1 and payload.startswith(_XMP_PREFIX):
                if xmp is EMPTY:
                    xmp = _parse_xmp(payload)
            elif marker == _APP13 and payload.startswith(_PHOTOSHOP_PREFIX):
                if iptc is EMPTY:
                    iptc = _parse_iptc(payload)
    except Exception:
        # The segment walker is defensive, but a reader over a whole library
        # must survive even an unforeseen malformation: degrade to whatever
        # was parsed before the fault.
        pass
    caption = xmp.caption if xmp.caption is not None else iptc.caption
    keywords = xmp.keywords if xmp.keywords else iptc.keywords
    if caption is None and not keywords:
        return EMPTY
    return InMeta(caption=caption, keywords=keywords)
