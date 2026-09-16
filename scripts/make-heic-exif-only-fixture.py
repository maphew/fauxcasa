#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "pi-heif"]
# ///
"""Generate fixtures/heic-smoke/synthetic-exif6.heic: a HEIC whose
rotation lives ONLY in an EXIF Orientation tag (=6), with NO irot/imir
container transform (fauxcasa-zq9).

Why a byte patch rather than an encoder: the app's own pi-heif is
decode-only, and the encode-capable sibling pillow-heif (GPLv2 wheels,
never a repo dependency) always writes an `irot` box when the EXIF it is
handed carries an Orientation -- exactly the "both present" shape this
fixture must NOT have. So instead we start from the already-committed
fixtures/heic-smoke/synthetic.heic (Orientation=1, no irot -- see its
.txt) and rewrite the ONE SHORT that holds the EXIF Orientation value,
1 -> 6, in place. Same length, so every iloc/idat offset in the
container stays valid and nothing else about the file changes.

Verification (this script fails loudly, exit 1, if any check is off):
  - exactly one TIFF header in the file, IFD0 has an Orientation entry
  - pillowload.heif_container_transform(bytes) is False (no irot/imir
    bound to the primary item), same as the source fixture
  - pi-heif reports info["original_orientation"] == 6 and the live
    EXIF tag reset to 1 (the neutering pillowload.py documents)
  - the raw decode (pi-heif standalone) is still 96x64 -- libheif had
    nothing to rotate, so the coded plane comes out as stored

Deterministic and idempotent: rerunning produces identical bytes.

Usage:
    uv run scripts/make-heic-exif-only-fixture.py            # write + verify
    uv run scripts/make-heic-exif-only-fixture.py --check    # verify only
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "fixtures" / "heic-smoke" / "synthetic.heic"
DST = REPO / "fixtures" / "heic-smoke" / "synthetic-exif6.heic"
ORIENTATION = 6


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


def build() -> bytes:
    data = bytearray(SRC.read_bytes())
    off, bo = _find_orientation_value_offset(bytes(data))
    struct.pack_into(f"{bo}H", data, off, ORIENTATION)
    return bytes(data)


def verify(data: bytes) -> None:
    sys.path.insert(0, str(REPO / "apps" / "desktop-python"))
    from pillowload import heif_container_transform  # noqa: E402

    import pi_heif

    if heif_container_transform(SRC.read_bytes()) is not False:
        sys.exit("source fixture unexpectedly carries a container transform")
    if heif_container_transform(data) is not False:
        sys.exit("patched fixture carries a container transform -- wrong")

    # Standalone pi-heif: HeifFile does not call set_orientation, so the
    # EXIF bytes are as stored; decode size is the coded plane.
    hf = pi_heif.open_heif(data)
    img = hf[0]
    if img.size != (96, 64):
        sys.exit(f"raw decode size {img.size}, expected (96, 64)")
    off, bo = _find_orientation_value_offset(data)
    if struct.unpack_from(f"{bo}H", data, off)[0] != ORIENTATION:
        sys.exit("patched value did not stick")

    # Pillow-plugin mode: original_orientation stashed, live tag reset.
    from PIL import Image
    import io
    pi_heif.register_heif_opener()
    with Image.open(io.BytesIO(data)) as im:
        got = im.info.get("original_orientation")
        live = im.getexif().get(0x0112)
        if got != ORIENTATION:
            sys.exit(f"pi-heif original_orientation={got!r}, want {ORIENTATION}")
        if live != 1:
            sys.exit(f"pi-heif live EXIF Orientation={live!r}, expected reset to 1")
    print(f"ok: {DST.relative_to(REPO)} -- {len(data)} bytes, EXIF "
          f"Orientation={ORIENTATION}, no irot/imir, coded plane 96x64")


def main(argv: list[str]) -> int:
    data = build()
    if "--check" in argv:
        if not DST.exists() or DST.read_bytes() != data:
            sys.exit(f"{DST} missing or differs from a fresh build")
    else:
        DST.write_bytes(data)
    verify(DST.read_bytes())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
