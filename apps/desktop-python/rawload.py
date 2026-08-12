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
usable preview. Orientation is applied exactly ONCE per path: normally
an embedded preview carries its own EXIF Orientation tag, honored by
the caller's normal JPEG decode (QImageReader.setAutoTransform / PIL
exif_transpose) — while postprocess() bakes the raw's flip into the
returned pixels (LibRaw user_flip default), so demosaiced output gets
no further transform. The two sources are never composed.

PREVIEW-ORIENTATION EDGE (fauxcasa-v46.5): some vendors store the flip
ONLY in the container (LibRaw sizes.flip) and omit EXIF Orientation from
the embedded JPEG preview. Callers decode the preview with
setAutoTransform, which reads the preview's OWN tag — a missing tag
passes the image through unrotated. raw_preview_jpeg detects this via a
cheap APP1 scan (_jpeg_has_exif_orientation) and, when the preview lacks
its own tag AND sizes.flip is nonzero, injects a minimal EXIF APP1
carrying the equivalent Orientation so callers' auto-transform applies
the correct rotation. See _LIBRAW_FLIP_TO_EXIF_ORIENTATION for the
mapping (empirically verified: TIFF Orientation 1/3/6/8 → flip 0/3/6/5).

BITMAP thumbs (fauxcasa-v46.5, Kodak/Foveon — ThumbFormat.BITMAP):
raw_preview_jpeg encodes the numpy pixel array to JPEG bytes so the
caller's ordinary scaled-JPEG path handles it. Same flip injection
applies. If the bitmap array has an unexpected shape or dtype, the
function returns None and the demosaic fallback runs as before.

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
import struct

# LibRaw sizes.flip → EXIF Orientation tag 274.
# Empirically verified: TIFF/EXIF Orientation in the container produces
# flip=0 (no rotation), flip=3 (180°), flip=6 (90° CW → EXIF 6), or
# flip=5 (90° CCW → EXIF 8). Flip=0 never needs injection; unknown flip
# values are not in this map (no injection, callers see un-corrected preview).
_LIBRAW_FLIP_TO_EXIF_ORIENTATION: dict[int, int] = {3: 3, 5: 8, 6: 6}


def _jpeg_has_exif_orientation(jpeg: bytes) -> bool:
    """True when the JPEG bytes carry an EXIF Orientation tag (APP1/TIFF 274).
    Minimal APP1 walk: no external dependencies, fail-soft (any parse error
    → False). Used to determine whether _inject_exif_orientation is needed."""
    if len(jpeg) < 4 or jpeg[0] != 0xFF or jpeg[1] != 0xD8:
        return False
    pos = 2
    n = len(jpeg)
    while pos + 3 < n:
        if jpeg[pos] != 0xFF:
            break
        marker = jpeg[pos + 1]
        pos += 2
        while marker == 0xFF and pos < n:    # skip fill bytes
            marker = jpeg[pos]
            pos += 1
        if marker in (0xDA, 0xD9):           # SOS / EOI: stop
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # standalone, no length
            continue
        if pos + 2 > n:
            break
        seg_len = int.from_bytes(jpeg[pos:pos + 2], "big")
        if seg_len < 2:
            break
        end = pos + seg_len
        if end > n:
            break
        payload = jpeg[pos + 2:end]
        pos = end
        if marker != 0xE1 or not payload.startswith(b"Exif\x00\x00"):
            continue
        # Walk TIFF IFD0 inside the EXIF APP1 for tag 274 (Orientation).
        t = payload[6:]                      # TIFF data after "Exif\0\0"
        if len(t) < 8:
            continue
        if t[:2] == b"II":
            bo = "<"
        elif t[:2] == b"MM":
            bo = ">"
        else:
            continue
        if struct.unpack_from(f"{bo}H", t, 2)[0] != 42:
            continue
        ifd0 = struct.unpack_from(f"{bo}I", t, 4)[0]
        if ifd0 + 2 > len(t):
            continue
        n_ent = struct.unpack_from(f"{bo}H", t, ifd0)[0]
        for i in range(n_ent):
            off = ifd0 + 2 + i * 12
            if off + 2 > len(t):
                break
            tag = struct.unpack_from(f"{bo}H", t, off)[0]
            if tag == 274:
                return True
            if tag > 274:                    # IFD entries are tag-sorted
                break
    return False


def _inject_exif_orientation(jpeg: bytes, orientation: int) -> bytes:
    """Return `jpeg` with a minimal EXIF APP1 carrying only the Orientation
    tag (274) spliced in right after the SOI marker. Call only when the JPEG
    has no existing EXIF Orientation (_jpeg_has_exif_orientation is False):
    two EXIF APP1 segments are technically legal but some readers honour only
    the first, so the injected tag must be the sole Orientation source."""
    # Minimal TIFF-in-EXIF: LE marker, magic 42, IFD0 at offset 8,
    # 1 entry (tag 274, SHORT, count 1, value=orientation), next-IFD=0.
    tiff = b"II" + struct.pack("<H", 42) + struct.pack("<I", 8)
    ifd = (struct.pack("<H", 1)                  # 1 IFD entry
           + struct.pack("<HHI", 0x0112, 3, 1)   # Orientation, SHORT, count 1
           + struct.pack("<HH", orientation, 0)  # value, 2-byte pad
           + struct.pack("<I", 0))               # next-IFD offset
    payload = b"Exif\x00\x00" + tiff + ifd
    seg = b"\xFF\xE1" + struct.pack(">H", len(payload) + 2) + payload
    return jpeg[:2] + seg + jpeg[2:]             # splice after SOI


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
    """The RAW file's embedded preview as JPEG bytes, or None when the file
    has no usable preview, cannot be parsed, or rawpy is unavailable.

    JPEG previews: returned as-is when they carry their own EXIF Orientation
    tag (callers decode with setAutoTransform ONCE). When the preview lacks
    its own tag but LibRaw sizes.flip is nonzero (vendor stores the flip only
    in the container), a minimal EXIF APP1 carrying the equivalent Orientation
    is injected so callers' auto-transform still applies the correct rotation.

    BITMAP previews (Kodak/Foveon): encoded to JPEG here (RGB888 uint8 numpy
    array → QImage.save JPEG) so the caller's ordinary scaled-JPEG path
    handles them. Same flip injection applies. An unexpected array shape or
    dtype returns None (demosaic fallback runs as before).
    """
    try:
        import rawpy

        with rawpy.imread(io.BytesIO(data)) as raw:
            thumb = raw.extract_thumb()
            flip = raw.sizes.flip              # read while raw is still open
        if thumb.format == rawpy.ThumbFormat.JPEG:
            jpeg = bytes(thumb.data)
            # Inject orientation when preview has no own tag and container
            # flip is nonzero — the vendor-omits-preview-tag edge case.
            if flip and not _jpeg_has_exif_orientation(jpeg):
                exif_orient = _LIBRAW_FLIP_TO_EXIF_ORIENTATION.get(flip, 0)
                if exif_orient:
                    jpeg = _inject_exif_orientation(jpeg, exif_orient)
            return jpeg
        if thumb.format == rawpy.ThumbFormat.BITMAP:
            # Encode the pixel array to JPEG so the caller's ordinary
            # scaled-JPEG decode path handles it.
            rgb = thumb.data
            if (hasattr(rgb, "ndim") and rgb.ndim == 3
                    and rgb.shape[2] == 3 and rgb.dtype.name == "uint8"):
                from PySide6.QtCore import QBuffer, QIODevice
                from PySide6.QtGui import QImage

                h, w = rgb.shape[:2]
                rgb_bytes = bytes(rgb.tobytes())   # keep alive across QImage
                qimg = QImage(rgb_bytes, w, h, 3 * w,
                              QImage.Format.Format_RGB888)
                buf = QBuffer()
                buf.open(QIODevice.OpenModeFlag.WriteOnly)
                if qimg.save(buf, "JPEG", 85):
                    jpeg = bytes(buf.data())
                    if flip:
                        exif_orient = _LIBRAW_FLIP_TO_EXIF_ORIENTATION.get(
                            flip, 0)
                        if exif_orient:
                            jpeg = _inject_exif_orientation(jpeg, exif_orient)
                    return jpeg
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
    and usually full-size — with its own EXIF orientation applied (once),
    else a full demosaic (flip already baked by LibRaw). Returns a null
    QImage when neither path can decode the bytes.

    The preview is decoded via QImage.fromData (an internal C++ buffer)
    rather than QBuffer + QImageReader, and its orientation is applied
    manually with inmeta.apply_orientation instead of
    QImageReader.setAutoTransform: a Python-created QIODevice handed to
    QImageReader can deadlock the GIL against Qt's image-plugin factory
    mutex (fauxcasa-5dk) — see inmeta.apply_orientation's docstring for
    the full mechanism. The preview is guaranteed JPEG (rawpy only returns
    ThumbFormat.JPEG here), so the format is passed explicitly."""
    from PySide6.QtGui import QImage

    from inmeta import apply_orientation
    from metareader import read_orientation

    jpeg = raw_preview_jpeg(data)
    if jpeg is not None:
        img = QImage.fromData(jpeg, "JPEG")
        if not img.isNull():
            # The preview's OWN tag — but that tag may not be the camera's:
            # raw_preview_jpeg can inject its own orientation tag reflecting
            # a flip it applied while extracting the embedded preview, so
            # this is "whatever orientation the preview JPEG bytes carry
            # right now", not necessarily what the camera originally wrote.
            orientation = read_orientation(jpeg)
            return apply_orientation(img, orientation)
    return raw_demosaic_qimage(data, half_size=False)
