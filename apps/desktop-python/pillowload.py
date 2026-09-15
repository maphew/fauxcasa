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

HEIC/HEIF (fauxcasa-y5b) rides the SAME fallback: Qt ships no HEIF
plugin either, so callers route .heic/.heif by extension straight here,
exactly like .psd. Pillow itself cannot decode HEIC without a plugin —
this module registers one via **pi-heif** (PyPI `pi-heif`), not
pillow-heif: pi-heif wheels bundle only the libheif/libde265 decoder
(LGPLv3), while pillow-heif's wheels also bundle the x265 ENCODER and
are GPLv2 as a result (docs/research/heic-decode-decision.md). The
opener is registered ONCE per process, at IMPORT time (module scope,
below) rather than lazily from pillow_qimage() — see
_ensure_heif_opener's docstring for the worker-thread race that
motivates this — fail-soft: a build without the wheel logs one warning
and HEIC/HEIF then error-tile exactly like any other still Pillow
cannot open.

Orientation is applied exactly once per path, like rawload: Qt's path
uses setAutoTransform; this path bakes the tag via PIL's exif_transpose
for every format EXCEPT HEIC/HEIF (a no-op for untagged formats — PSD
carries no EXIF Orientation). pi-heif's opener resets EXIF Orientation
to 1 at open time (pi_heif/as_plugin.py's set_orientation(), called
automatically in Pillow-plugin mode), so exif_transpose is always a
no-op for HEIC specifically; libheif applies the container's own irot/
imir transform boxes while decoding instead, before pixels ever reach
PIL. KNOWN LIMITATION (not fixed here, tracked as fauxcasa-zq9, P4): an
HEIC whose rotation lives ONLY in an EXIF Orientation tag, with no
irot/imir in the container (not the common case — most encoders write
both, and Apple's HEIC shape uses irot/imir) decodes SIDEWAYS: pi-heif
neutralises the tag by design, and libheif has no container transform
to apply, so no orientation signal survives either path. The two paths
(Qt's autoTransform vs. this module's exif_transpose/libheif-native
handling) are alternatives, never composed.

Threat-model note (docs/decode-threat-model.md): in-process like all
tracer decode, but a single bytes-in/pixels-out function so the future
sandboxed decode service slots in behind the same seam. Fail-soft:
Pillow missing, unreadable bytes, truncated planes — all return a null
QImage (error tile), never an exception. HEIC/HEIF is a documented
in-process exception alongside PSD and 16-bit TIFF (libheif/libde265
have a real CVE history — tracked for sandbox migration under
fauxcasa-i92).
"""

from __future__ import annotations

import io
import logging
import struct

log = logging.getLogger("fauxcasa")

_heif_registered = False
_heif_import_failed = False


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
            # Includes BigTIFF (magic 43, 8-byte offsets): not sniffed, so
            # a 16-bit BigTIFF still takes the Qt path — known gap; no
            # BigTIFF has shown up in the corpus yet.
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
            # No sorted-tag early-out: the spec orders entries ascending,
            # but non-conforming writers exist, and a skipped BitsPerSample
            # means silent white-clip on Linux — scan every entry.
            entry_off += 12
    except (struct.error, OverflowError):
        return False
    return False


def _ensure_heif_opener() -> None:
    """Register pi-heif's Pillow opener, once per process (no-op on every
    call after the first, any outcome); on failure (missing wheel,
    native-lib load fault) warn ONCE and leave HEIC/HEIF to fail-soft to
    the error tile like any other format Pillow cannot open (N7 pattern,
    mirrors metareader._module).

    Called once at IMPORT time below, deliberately NOT from
    pillow_qimage() per call: Pillow's Image.register_open(id, ...)
    appends `id` to the format-probe order (Image._plugins/ID) BEFORE
    Image.OPEN[id] is populated, a window that is empty when this runs
    once at import (before the indexer's thread pool exists) but is a
    real race if the first call instead happens lazily from a decode
    worker thread while ANOTHER worker thread is mid-Image.open() on an
    unrelated photo -- that thread can observe `id` in the probe order
    and KeyError on the not-yet-populated OPEN entry, producing a
    permanent null tile for a file that has nothing to do with HEIC
    (fauxcasa-y5b review). Kept as a callable (not inlined at module
    scope) so an explicit caller can still assert it ran (e.g. a test
    importing this module fresh)."""
    global _heif_registered, _heif_import_failed
    if _heif_registered or _heif_import_failed:
        return
    try:
        from pi_heif import register_heif_opener

        register_heif_opener()
        _heif_registered = True
    except Exception as e:  # noqa: BLE001 — ImportError or native-lib faults
        _heif_import_failed = True
        log.warning("pi-heif unavailable (%s) — HEIC/HEIF files will "
                    "error-tile this run", e)


_ensure_heif_opener()  # at import time, single-threaded -- see docstring above


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
            # exif_transpose is the ONE orientation apply for every
            # OTHER format this fallback serves (PSD carries no EXIF
            # Orientation; 16-bit TIFF's tag, if any, is untouched here)
            # -- but NOT for HEIC/HEIF: pi-heif's opener calls its own
            # set_orientation() at open time (pi_heif/as_plugin.py),
            # which RESETS the EXIF Orientation tag to 1 unconditionally
            # whenever one is present, so this exif_transpose is always
            # a no-op for HEIC. Uprightness instead comes from libheif
            # itself applying the HEIF container's own irot/imir
            # transform boxes while DECODING, before pixels ever reach
            # PIL. Known gap (not fixed here, tracked as fauxcasa-zq9, P4):
            # an HEIC whose rotation lives ONLY in EXIF, with no irot/
            # imir in the container, decodes SIDEWAYS -- pi-heif
            # neutralises the tag by design and libheif has nothing to
            # rotate, so no orientation signal survives to correct it.
            im = ImageOps.exif_transpose(im)
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
