"""RAW detection + decode seam for the tracer app (fauxcasa-v46.1).

Detection: Picasa's documented 16-vendor RAW extension list (spec §5
Formats; docs/research/sources/picasaresources/files-supported-by-
picasa3.md). Decode: rawpy — a maintained, *updatable* LibRaw wheel —
never a frozen in-binary table (§6 footgun 14, Picasa's single biggest
functional decay). RAW originals are never written (§5: XMP sidecars
serve interop for them); everything here is read-only.

ROUTING IS BY EXTENSION, ahead of any content sniff. Most vendor RAW
containers are TIFF-based (NEF/CR2/ORF/PEF/ARW/DNG), so a generic TIFF
decoder (QImageReader, PIL) can "succeed" by decoding the tiny embedded
preview IFD — a wrong, low-res image presented as the photo — or fail
oddly partway. Every decode path checks the extension FIRST and hands
RAW files here: thumbcache._index_one, viewer.load_original (which the
slideshow prefetch shares), and scripts/make-thumbcache.py (which
mirrors this strategy in PIL terms; RAW_EXTS there must match).

Decode strategy, both thumbs and viewer: the embedded JPEG preview
first (LibRaw extract_thumb — most RAWs carry a full-size JPEG preview;
extraction is cheap and the result rides the existing scaled-JPEG decode
path), falling back to a real demosaic (postprocess) when there is no
usable preview. Orientation is applied exactly ONCE per path: an
embedded preview carries its own EXIF Orientation tag, honored by the
caller's normal JPEG decode (QImageReader.setAutoTransform / PIL
exif_transpose) — while postprocess() bakes the raw's flip into the
returned pixels (LibRaw user_flip default), so demosaiced output gets
no further transform. The two sources are never composed.

Threat-model note (docs/decode-threat-model.md): RAW decode is
in-process for now, like all tracer decode, but flows through this one
module so the sandboxed decode service the product requires (§5 "no
ambient authority") can slot in behind the same seam: bytes in, pixels
out, nothing else. Fail-soft: every helper degrades to None / a null
QImage on any decode error — a corrupt RAW yields an error tile, never
a crash (the existing error-tile pattern).
"""

from __future__ import annotations

import io

# Picasa 3's documented RAW support: 18 extensions across 16 vendors
# (files-supported-by-picasa3.md): Adobe DNG (also Leica/Ricoh/Samsung),
# Canon CRW/CR2, Casio RAW (also Leica/Panasonic), Fuji RAF, Hasselblad
# 3FR, Kodak DCR/KDC, Minolta MRW, Nikon NEF/NRW, Olympus ORF, Panasonic
# RW2, Pentax PEF, Sigma X3F, Sony ARW/SRF/SR2. Must match
# scripts/make-thumbcache.py RAW_EXTS exactly (walk/cache-order parity —
# test_tracer.py asserts the full EXTS sets are equal).
RAW_EXTS = frozenset({
    ".3fr", ".arw", ".cr2", ".crw", ".dcr", ".dng", ".kdc", ".mrw",
    ".nef", ".nrw", ".orf", ".pef", ".raf", ".raw", ".rw2", ".sr2",
    ".srf", ".x3f",
})


def is_raw_suffix(name) -> bool:
    """True when `name` (a str/Path file name or path) has a RAW extension.
    Pure string routing — no file I/O, no content sniff (see module doc)."""
    s = str(name)
    dot = s.rfind(".")
    return dot >= 0 and s[dot:].lower() in RAW_EXTS


def raw_preview_jpeg(data: bytes) -> bytes | None:
    """The RAW file's embedded JPEG preview bytes, or None when the file
    has no JPEG preview (bitmap-only thumbs fall through to demosaic),
    cannot be parsed, or rawpy is unavailable. The returned JPEG carries
    its own EXIF Orientation tag when the camera wrote one — callers
    decode it exactly like any JPEG original (auto-transform ONCE)."""
    try:
        import rawpy

        with rawpy.imread(io.BytesIO(data)) as raw:
            thumb = raw.extract_thumb()
        if thumb.format == rawpy.ThumbFormat.JPEG:
            return bytes(thumb.data)
    except Exception:  # noqa: BLE001 — fail-soft per file (error tile)
        pass
    return None


def raw_demosaic_qimage(data: bytes, half_size: bool):
    """Demosaic a RAW to a QImage (null on any failure). LibRaw applies
    the raw's orientation flip during postprocess, so the pixels come
    back display-upright — callers must NOT auto-transform again.
    half_size=True quarters the pixel count (thumbnail path); the viewer
    passes False for the full-resolution render."""
    from PySide6.QtGui import QImage

    try:
        import rawpy

        with rawpy.imread(io.BytesIO(data)) as raw:
            rgb = raw.postprocess(half_size=half_size)
    except Exception:  # noqa: BLE001 — fail-soft per file (error tile)
        return QImage()
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype.name != "uint8":
        return QImage()  # unexpected shape: treat as undecodable
    h, w = rgb.shape[:2]
    img = QImage(rgb.tobytes(), w, h, 3 * w, QImage.Format.Format_RGB888)
    return img.copy()  # detach from the transient Python buffer


def load_raw_qimage(data: bytes):
    """Full RAW decode for the viewer path (viewer.load_original and the
    slideshow prefetch riding it): embedded JPEG preview first — instant
    and usually full-size — with its own EXIF orientation auto-applied
    (once), else a full demosaic (flip already baked by LibRaw). Returns
    a null QImage when neither path can decode the bytes."""
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImageReader

    jpeg = raw_preview_jpeg(data)
    if jpeg is not None:
        buf = QBuffer()
        buf.setData(jpeg)  # setData copies; see thumbcache._index_one
        buf.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buf)
        reader.setAutoTransform(True)  # the preview's OWN tag, applied once
        img = reader.read()
        if not img.isNull():
            return img
    return raw_demosaic_qimage(data, half_size=False)
