#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "pi-heif", "exiv2"]
# ///
"""Generate and verify the fauxcasa-zq9 HEIC orientation fixtures in
fixtures/heic-smoke/ -- HEICs whose rotation lives in metadata ONLY,
with NO irot/imir container transform:

  synthetic-exif6.heic  EXIF Orientation=6, no XMP        (default build)
  synthetic-xmp6.heic   XMP tiff:Orientation="6", no EXIF (--xmp build)

Why byte patches rather than an encoder: the app's own pi-heif is
decode-only, and the encode-capable sibling pillow-heif (GPLv2 wheels,
never a repo dependency) always writes an `irot` box when the metadata
it is handed carries a non-1 Orientation -- exactly the "both present"
shape these fixtures must NOT have. So each fixture is encoded (or
reused) with Orientation=1 and then the ONE value that holds the
orientation is rewritten in place, 1 -> 6, same length, so every iloc/
idat offset stays valid and nothing else about the file changes.

  exif6: starts from the already-committed synthetic.heic (Orientation=1,
         no irot -- see its .txt); patches the 2-byte SHORT in IFD0.
         Needs nothing beyond this script's deps; byte-deterministic.
  xmp6:  encodes the same 96x64 quadrant plane with pillow-heif, handing
         it an XMP packet with tiff:Orientation="1" and NO EXIF at all,
         then patches the one ASCII "1" -> "6". Needs pillow-heif, so it
         is opt-in:  uv run --with pillow-heif scripts/make-heic-exif-only-fixture.py --xmp
         HEVC output is not guaranteed byte-stable across x265 versions,
         so --check verifies this one structurally, not by rebuilding.

Verification (exit 1 on any miss), per fixture as applicable:
  - pillowload.heif_container_transform(bytes) is False (nothing bound to
    the primary item rotates), same as the source fixture
  - pi-heif standalone decode is 96x64 -- libheif had nothing to rotate
  - pi-heif in Pillow-plugin mode reports info["original_orientation"] == 6
    for BOTH (its misc._get_orientation falls through to XMP), with the
    live EXIF tag reset to 1 (exif6) / absent (xmp6)
  - metareader.read_orientation (exiv2, EXIF tag 274 only) reads 6 for
    exif6 and 1 for xmp6 -- the divergence pillowload.heic_manual_
    orientation exists to handle
  - pillowload.heic_manual_orientation(bytes) is 6 for exif6, 1 for xmp6

Usage:
    uv run scripts/make-heic-exif-only-fixture.py            # build exif6, verify all committed
    uv run scripts/make-heic-exif-only-fixture.py --check    # verify only (preflight gate)
    uv run --with pillow-heif scripts/make-heic-exif-only-fixture.py --xmp   # (re)build xmp6 too
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DIR = REPO / "fixtures" / "heic-smoke"
SRC = DIR / "synthetic.heic"
EXIF6 = DIR / "synthetic-exif6.heic"
XMP6 = DIR / "synthetic-xmp6.heic"
ORIENTATION = 6

W, H = 96, 64
COLORS = {(0, 0): (220, 40, 40), (1, 0): (40, 200, 60),
          (0, 1): (40, 80, 220), (1, 1): (230, 210, 30)}
XMP_TEMPLATE = (
    b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    b'<rdf:Description rdf:about="" xmlns:tiff="http://ns.adobe.com/tiff/1.0/"'
    b' tiff:Orientation="%d"/>'
    b'</rdf:RDF></x:xmpmeta><?xpacket end="w"?>')

sys.path.insert(0, str(REPO / "apps" / "desktop-python"))


def _find_orientation_value_offset(data: bytes) -> tuple[int, str]:
    """(absolute offset of the 2-byte EXIF Orientation value, struct
    byte-order prefix) inside the file's single embedded TIFF. Walks
    IFD0 only (the tag lives there)."""
    hits = [i for i in range(len(data) - 4)
            if data[i:i + 4] in (b"II*\x00", b"MM\x00*")]
    if len(hits) != 1:
        sys.exit(f"expected exactly one TIFF header, found {len(hits)}")
    tiff = hits[0]
    bo = "<" if data[tiff:tiff + 2] == b"II" else ">"
    ifd = tiff + struct.unpack_from(f"{bo}I", data, tiff + 4)[0]
    n = struct.unpack_from(f"{bo}H", data, ifd)[0]
    for k in range(n):
        entry = ifd + 2 + 12 * k
        tag, dtype, count = struct.unpack_from(f"{bo}HHI", data, entry)
        if tag == 0x0112:
            if dtype != 3 or count != 1:
                sys.exit(f"Orientation entry has dtype={dtype} count={count}")
            return entry + 8, bo  # SHORT stored inline, left-justified
    sys.exit("no Orientation (0x0112) entry in IFD0")


def build_exif6() -> bytes:
    data = bytearray(SRC.read_bytes())
    off, bo = _find_orientation_value_offset(bytes(data))
    struct.pack_into(f"{bo}H", data, off, ORIENTATION)
    return bytes(data)


def build_xmp6() -> bytes:
    """Encode with pillow-heif (dev-time only, never a repo dependency),
    XMP tiff:Orientation="1" and no EXIF, then patch the "1" to "6"."""
    try:
        import pillow_heif
    except ImportError:
        sys.exit("--xmp needs pillow-heif: "
                 "uv run --with pillow-heif scripts/make-heic-exif-only-fixture.py --xmp")
    from PIL import Image

    pillow_heif.register_heif_opener()
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        for x in range(W):
            px[x, y] = COLORS[(1 if x >= W // 2 else 0, 1 if y >= H // 2 else 0)]
    buf = io.BytesIO()
    img.save(buf, format="HEIF", xmp=XMP_TEMPLATE % 1, quality=90)
    data = buf.getvalue()
    needle = b'tiff:Orientation="1"'
    if data.count(needle) != 1:
        sys.exit(f"expected one XMP orientation attribute, found {data.count(needle)}")
    if b"irot" in data or b"imir" in data:
        sys.exit("encoder wrote a container transform for Orientation=1 -- unexpected")
    return data.replace(needle, b'tiff:Orientation="%d"' % ORIENTATION)


def verify(name: str, data: bytes, *, exif_expected: int,
           live_exif_expected: int | None) -> None:
    from pillowload import heic_manual_orientation, heif_container_transform
    from metareader import read_orientation
    from PIL import Image
    import pi_heif

    if heif_container_transform(data) is not False:
        sys.exit(f"{name}: container carries (or may carry) a transform -- wrong")
    if pi_heif.open_heif(data)[0].size != (W, H):
        sys.exit(f"{name}: raw decode size is not {(W, H)}")
    pi_heif.register_heif_opener()
    with Image.open(io.BytesIO(data)) as im:
        got = im.info.get("original_orientation")
        live = im.getexif().get(0x0112)
    if got != ORIENTATION:
        sys.exit(f"{name}: pi-heif original_orientation={got!r}, want {ORIENTATION}")
    if live != live_exif_expected:
        sys.exit(f"{name}: pi-heif live EXIF Orientation={live!r}, "
                 f"want {live_exif_expected!r}")
    exif = read_orientation(data)
    if exif != exif_expected:
        sys.exit(f"{name}: exiv2 read_orientation={exif}, want {exif_expected}")
    manual = heic_manual_orientation(data, got)
    if manual != exif_expected:
        sys.exit(f"{name}: heic_manual_orientation={manual}, want {exif_expected}")
    print(f"ok: fixtures/heic-smoke/{name} -- {len(data)} bytes, no irot/imir, "
          f"coded plane {W}x{H}, pi-heif original_orientation={got}, "
          f"exiv2 EXIF={exif}, applied={manual}")


def main(argv: list[str]) -> int:
    check = "--check" in argv
    exif6 = build_exif6()
    if check:
        if not EXIF6.exists() or EXIF6.read_bytes() != exif6:
            sys.exit(f"{EXIF6} missing or differs from a fresh build")
    else:
        EXIF6.write_bytes(exif6)
    if "--xmp" in argv:
        if check:
            sys.exit("--xmp and --check are exclusive (xmp6 is verified "
                     "structurally, not by rebuild)")
        XMP6.write_bytes(build_xmp6())
    from pillowload import heif_container_transform
    if heif_container_transform(SRC.read_bytes()) is not False:
        sys.exit("source synthetic.heic unexpectedly carries a transform")
    verify(EXIF6.name, EXIF6.read_bytes(), exif_expected=6, live_exif_expected=1)
    if XMP6.exists():
        verify(XMP6.name, XMP6.read_bytes(), exif_expected=1,
               live_exif_expected=None)
    elif check:
        sys.exit(f"{XMP6} missing")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
