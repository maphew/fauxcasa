"""Pillow decode fallback for stills Qt cannot decode (fauxcasa-v46.4).

The pinned PySide6 build ships no PSD image plugin, so a .psd file in
the walk (§5 stills matrix) would always error-tile through the Qt-only
decode paths. Pillow's PsdImagePlugin reads the FLATTENED COMPOSITE —
the Picasa-equivalent behavior: only PSDs saved with Photoshop's
"maximize compatibility" carry one, and a PSD without it legitimately
error-tiles (there is nothing composed to show).

The seam is generic, not PSD-gated: callers try QImageReader first and
hand the SAME bytes here only when Qt yielded a null image, so any
still Qt's plugins reject (exotic JPEG variants, 16-bit TIFF flavors)
gets one Pillow attempt before the error tile. Extension-routed RAW and
video files never reach this fallback — rawload/videoload route them
BEFORE Qt can sniff the bytes (rawload module doc), and their branches
never fall through here, so a TIFF-based RAW container can't be
"rescued" into a garbled preview by PIL either.

Orientation is applied exactly once per path, like rawload: Qt's path
uses setAutoTransform; this path bakes the tag via PIL's exif_transpose
(a no-op for untagged formats — PSD carries no EXIF Orientation). The
two paths are alternatives, never composed.

Threat-model note (docs/decode-threat-model.md): in-process like all
tracer decode, but a single bytes-in/pixels-out function so the future
sandboxed decode service slots in behind the same seam. Fail-soft:
Pillow missing, unreadable bytes, truncated planes — all return a null
QImage (error tile), never an exception.
"""

from __future__ import annotations

import io
import struct


def tiff_is_16bit(data: bytes) -> bool:
    """Return True when `data` is a TIFF whose BitsPerSample tag (258)
    contains any value >= 16 — the signal to pre-route to pillow_qimage
    before Qt's tiff plugin, which silently clips 16-bit grayscale to
    white on Linux (fauxcasa-v46.7).

    Pure header parse: walks the byte-order mark (II/MM), verifies the
    magic 42, seeks the first IFD, and reads tag 258 inline or at its
    offset. Returns False for non-TIFF bytes, truncated input, or a
    missing BitsPerSample tag; never raises."""
    if len(data) < 8:
        return False
    bom = data[:2]
    if bom == b"II":
        bo = "<"
    elif bom == b"MM":
        bo = ">"
    else:
        return False
    try:
        magic = struct.unpack_from(f"{bo}H", data, 2)[0]
        if magic != 42:
            return False
        ifd_off = struct.unpack_from(f"{bo}I", data, 4)[0]
        if ifd_off + 2 > len(data):
            return False
        n_entries = struct.unpack_from(f"{bo}H", data, ifd_off)[0]
        entry_off = ifd_off + 2
        for _ in range(n_entries):
            if entry_off + 12 > len(data):
                return False
            tag = struct.unpack_from(f"{bo}H", data, entry_off)[0]
            if tag == 258:  # BitsPerSample
                dtype = struct.unpack_from(f"{bo}H", data, entry_off + 2)[0]
                count = struct.unpack_from(f"{bo}I", data, entry_off + 4)[0]
                if dtype == 3:  # SHORT — 2 bytes per value
                    if count * 2 <= 4:  # fits inline in the 4-byte field
                        vals = struct.unpack_from(
                            f"{bo}{count}H", data, entry_off + 8)
                    else:
                        offset = struct.unpack_from(
                            f"{bo}I", data, entry_off + 8)[0]
                        if offset + count * 2 > len(data):
                            return False
                        vals = struct.unpack_from(
                            f"{bo}{count}H", data, offset)
                    return any(v >= 16 for v in vals)
                elif dtype == 4:  # LONG — 4 bytes per value
                    if count == 1:  # fits inline
                        vals = (struct.unpack_from(
                            f"{bo}I", data, entry_off + 8)[0],)
                    else:
                        offset = struct.unpack_from(
                            f"{bo}I", data, entry_off + 8)[0]
                        if offset + count * 4 > len(data):
                            return False
                        vals = struct.unpack_from(
                            f"{bo}{count}I", data, offset)
                    return any(v >= 16 for v in vals)
                return False  # unrecognised dtype for BitsPerSample
            if tag > 258:
                # IFD entries are sorted ascending by tag (TIFF spec);
                # nothing past 258 can be BitsPerSample.
                break
            entry_off += 12
    except (struct.error, OverflowError):
        return False
    return False


def pillow_qimage(data: bytes, max_edge: int | None = None):
    """Decode `data` with Pillow to a QImage (null on ANY failure —
    including Pillow itself being unavailable; the import is lazy so a
    build without the wheel degrades to error tiles, never a crash).
    `max_edge` bounds the long edge (the indexer passes its top level so
    a 100-megapixel PSD never materializes full-size for a 256 px thumb);
    None decodes at native size (the viewer path)."""
    from PySide6.QtGui import QImage

    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)  # the ONE orientation apply
            # Normalise high-bit-depth integer modes before any conversion:
            # convert('RGB') clips values > 255 for 'I' and 'I;*' modes
            # rather than scaling them, so a 16-bit grayscale sample of
            # 40000 would arrive as white instead of mid-gray. convert('I')
            # handles endian variants (e.g. 'I;16B'); point(x/256,'L') maps
            # the 16-bit range [0,65535] → 8-bit [0,255] (fauxcasa-v46.7).
            if im.mode in ("I", "I;16", "I;16B", "I;32", "I;32B"):
                im = im.convert("I").point(lambda x: x / 256, "L")
            if max_edge is not None:
                im.thumbnail((max_edge, max_edge))
            has_alpha = "A" in im.getbands() or im.mode == "P" \
                and "transparency" in im.info
            im = im.convert("RGBA" if has_alpha else "RGB")
            w, h = im.size
            if has_alpha:
                img = QImage(im.tobytes(), w, h, 4 * w,
                             QImage.Format.Format_RGBA8888)
            else:
                img = QImage(im.tobytes(), w, h, 3 * w,
                             QImage.Format.Format_RGB888)
            return img.copy()  # detach from the transient Python buffer
    except Exception:  # noqa: BLE001 — fail-soft per file (error tile)
        return QImage()
