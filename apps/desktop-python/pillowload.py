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
PIL.

HEIC orientation, two signals, applied at most ONCE (fauxcasa-zq9):

  1. Container transform (`irot`/`imir` item properties in `iprp`/
     `ipco`, bound to the primary item through `ipma`). libheif applies
     these itself during decode -- the pixels PIL receives are already
     display-upright and `im.size` is the post-transform size. Most
     encoders (Apple included) write BOTH this and an EXIF Orientation
     tag describing the same turn; for those files EXIF must be
     ignored or the image would be double-rotated.
  2. EXIF Orientation alone (no irot/imir bound to the primary item).
     libheif has nothing to rotate, so the coded plane comes out as
     stored -- sideways for a rotated capture. pi-heif's opener has by
     then already reset the live tag to 1, but it keeps the value it
     found under `im.info["original_orientation"]` (set_orientation's
     return value, stashed by as_plugin._init_from_heif_file; None or
     absent when the file carried no tag). pillow_qimage applies THAT
     value with the same transpose table exif_transpose uses.

How pillow_qimage tells the two apart: pi-heif's C surface exposes no
"transforms applied" flag and no untransformed (`ispe`) size, and a
size comparison could not distinguish an `irot` of 180 or an `imir`
anyway (neither swaps the axes). So heif_container_transform() sniffs
the container's own boxes -- ftyp -> meta -> pitm (primary item id),
iprp/ipco (the property list, 1-based), ipma (which properties bind to
which item) -- and reports True when an `irot` with a non-zero angle or
any `imir` is bound to the primary item. It is a pure header parse in
the tiff_is_16bit mould: never raises, and returns None (treated as
"unknown, trust libheif, do not apply EXIF") for anything it cannot
parse, so a malformed container can degrade to the pre-zq9 behavior
but never to a double rotation. Rule: EXIF is applied only when the
sniff says definitively False. The two paths (Qt's autoTransform vs.
this module's exif_transpose/libheif-native handling) are alternatives,
never composed.

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


def _iso_boxes(data: bytes, start: int, end: int):
    """Yield (type, payload_start, box_end) for each ISO BMFF box in
    data[start:end]. Handles 64-bit `largesize` and size==0 (to end).
    Stops silently (no raise) at a truncated or nonsensical header."""
    off = start
    while off + 8 <= end:
        size, typ = struct.unpack_from(">I4s", data, off)
        hdr = 8
        if size == 1:
            if off + 16 > end:
                return
            size = struct.unpack_from(">Q", data, off + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr or off + size > end:
            return
        yield typ, off + hdr, off + size
        off += size


def heif_container_transform(data: bytes) -> bool | None:
    """Does this HEIF/HEIC's PRIMARY item carry a container-level
    orientation transform (an `irot` with a non-zero angle, or any
    `imir`) that libheif applies itself while decoding?

    True  -> yes: pixels out of libheif are already upright; any EXIF
             Orientation tag describes the SAME turn and must be ignored.
    False -> definitively no: nothing bound to the primary item rotates
             or mirrors, so an EXIF Orientation tag is the only signal
             and pillow_qimage must apply it.
    None  -> could not tell (not a HEIF `ftyp`, no `meta`/`pitm`/`ipma`,
             truncated boxes). Callers treat None like True -- trusting
             libheif alone reproduces the pre-zq9 behavior, whereas
             guessing False risks a double rotation.

    Pure header parse (no decode, never raises), modelled on
    tiff_is_16bit: ftyp -> meta (FullBox: 4-byte version/flags before
    its children) -> pitm (primary item_ID, 16-bit for version 0, else
    32-bit) + iprp/ipco (the property list, indexed 1-based in ipma
    order) + ipma (per-item property index lists; `flags & 1` selects
    16-bit indices, of which the top bit is `essential`). Item-level
    `clap` (clean aperture) is deliberately not counted: it crops, it
    does not orient. Only the primary item is inspected -- that is the
    one pi-heif hands to Pillow (HeifFile.primary_index)."""
    try:
        if len(data) < 16 or data[4:8] != b"ftyp":
            return None
        meta = None
        for typ, pstart, pend in _iso_boxes(data, 0, len(data)):
            if typ == b"meta":
                meta = (pstart + 4, pend)  # skip FullBox version/flags
                break
        if meta is None:
            return None
        primary = None
        props: list[tuple[bytes, int]] = []   # (type, payload_start), 1-based
        ipma = None
        for typ, pstart, pend in _iso_boxes(data, *meta):
            if typ == b"pitm":
                version = data[pstart]
                if version == 0:
                    primary = struct.unpack_from(">H", data, pstart + 4)[0]
                else:
                    primary = struct.unpack_from(">I", data, pstart + 4)[0]
            elif typ == b"iprp":
                for t2, s2, e2 in _iso_boxes(data, pstart, pend):
                    if t2 == b"ipco":
                        props = [(t3, s3)
                                 for t3, s3, _e3 in _iso_boxes(data, s2, e2)]
                    elif t2 == b"ipma":
                        ipma = (s2, e2)
        if primary is None or ipma is None:
            return None
        s, e = ipma
        version = data[s]
        flags = data[s + 3]
        entry_count = struct.unpack_from(">I", data, s + 4)[0]
        p = s + 8
        bound: list[int] | None = None
        for _ in range(entry_count):
            if version < 1:
                item_id = struct.unpack_from(">H", data, p)[0]
                p += 2
            else:
                item_id = struct.unpack_from(">I", data, p)[0]
                p += 4
            count = data[p]
            p += 1
            idxs = []
            for _ in range(count):
                if flags & 1:
                    idxs.append(struct.unpack_from(">H", data, p)[0] & 0x7FFF)
                    p += 2
                else:
                    idxs.append(data[p] & 0x7F)
                    p += 1
            if item_id == primary:
                bound = idxs
                break
        if bound is None:
            return None
        for idx in bound:
            if idx < 1 or idx > len(props):
                return None                   # out-of-range: malformed
            ptype, pstart = props[idx - 1]
            if ptype == b"imir":
                return True
            if ptype == b"irot" and (data[pstart] & 0x3):
                return True
        return False
    except (struct.error, IndexError, OverflowError):
        return None


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
            # a no-op for HEIC. Uprightness normally comes from libheif
            # itself applying the HEIF container's own irot/imir
            # transform boxes while DECODING, before pixels ever reach
            # PIL -- see the HEIC block below for the EXIF-only case.
            im = ImageOps.exif_transpose(im)
            # HEIC/HEIF, EXIF-only rotation (fauxcasa-zq9). pi-heif
            # stashes the tag value it neutralised under
            # info["original_orientation"] (the key exists only when
            # pi-heif's plugin opened the image). Apply it manually iff
            # the container sniff says DEFINITIVELY that no irot/imir is
            # bound to the primary item -- True or None ("both present"
            # or "can't tell") leaves libheif's output alone, so a file
            # in the common Apple shape is never rotated twice. Module
            # docstring, "HEIC orientation, two signals".
            original = im.info.get("original_orientation")
            if original not in (None, 1) \
                    and heif_container_transform(data) is False:
                method = {
                    2: Image.Transpose.FLIP_LEFT_RIGHT,
                    3: Image.Transpose.ROTATE_180,
                    4: Image.Transpose.FLIP_TOP_BOTTOM,
                    5: Image.Transpose.TRANSPOSE,
                    6: Image.Transpose.ROTATE_270,
                    7: Image.Transpose.TRANSVERSE,
                    8: Image.Transpose.ROTATE_90,
                }.get(original)               # same table as exif_transpose
                if method is not None:
                    im = im.transpose(method)
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
