#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["PySide6-Essentials", "rawpy"]
# ///
"""Generate a tiny synthetic DNG fixture for CI smoke tests (fauxcasa-v46.5).

Produces fixtures/raw-smoke/synthetic.dng — a minimal-but-valid little-endian
DNG 1.4 with a 64x48 CFA strip and an embedded 64x48 blue JPEG preview. The
blue fill (QColor 40, 90, 160) matches the smoke library's JPEG palette so the
existing screenshot assertion's blue-dominant check covers the DNG tile too.

Provenance: hand-rolled TIFF structure (see _make_dng below; same logic as
test_tracer.py:_make_dng, reproduced here so the script is self-contained).
No real camera or Picasa data — entirely synthetic deterministic bytes.

Run once to regenerate:
    uv run scripts/make-synthetic-dng.py

Idempotent: overwrites fixtures/raw-smoke/synthetic.dng unconditionally.
Verify with:
    uv run --with rawpy python -c "import rawpy, io; r=rawpy.imread('fixtures/raw-smoke/synthetic.dng'); t=r.extract_thumb(); print(t.format, len(t.data)); r.close()"
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "fixtures" / "raw-smoke"
OUT_DNG = OUT_DIR / "synthetic.dng"
OUT_NOTE = OUT_DIR / "synthetic.dng.txt"


# ---------------------------------------------------------------------------
# Minimal DNG builder (reproduced from test_tracer.py:_make_dng)
# ---------------------------------------------------------------------------

def _dng_ifd(entries: list, ifd_off: int) -> bytes:
    entries = sorted(entries, key=lambda e: e[0])
    data_off = ifd_off + 2 + 12 * len(entries) + 4
    table = struct.pack("<H", len(entries))
    data = b""
    for tag, typ, count, payload in entries:
        if len(payload) <= 4:
            table += struct.pack("<HHI", tag, typ, count) \
                + payload.ljust(4, b"\0")
        else:
            if (data_off + len(data)) % 2:
                data += b"\0"
            table += struct.pack("<HHII", tag, typ, count,
                                 data_off + len(data))
            data += payload
    return table + struct.pack("<I", 0) + data


def _dng_ifd_size(entries: list) -> int:
    return 2 + 12 * len(entries) + 4 + sum(
        len(p) + (len(p) % 2) for _t, _y, _c, p in entries if len(p) > 4)


def _make_dng_bytes(w: int = 64, h: int = 48,
                    orientation: int = 1,
                    preview_jpeg: bytes | None = None,
                    preview_size: tuple[int, int] = (0, 0)) -> bytes:
    """Tiny synthetic DNG 1.4 (little-endian TIFF container, RGGB CFA,
    optional JPEG preview IFD). Matches test_tracer.py:_make_dng."""
    _SHORT, _LONG, _BYTE, _ASCII, _SRAT = 3, 4, 1, 2, 10
    strip = struct.pack(f"<{w * h}H",
                        *(((x * 89 + y * 71) % 4096)
                          for y in range(h) for x in range(w)))
    cam = b"Fauxcasa Synthetic\0"
    cm = b"".join(struct.pack("<ii", v, 10000) for v in
                  (10000, 0, 0, 0, 10000, 0, 0, 0, 10000))  # identity XYZ

    def E(tag, typ, fmt, *vals):
        return (tag, typ, len(vals) if len(vals) > 1 else 1,
                struct.pack(fmt, *vals))

    raw_entries = [
        E(254, _LONG, "<I", 0),
        E(256, _LONG, "<I", w), E(257, _LONG, "<I", h),
        E(258, _SHORT, "<H", 16),
        E(259, _SHORT, "<H", 1),
        E(262, _SHORT, "<H", 32803),
        E(277, _SHORT, "<H", 1),
        E(278, _LONG, "<I", h),
        E(279, _LONG, "<I", len(strip)),
        E(284, _SHORT, "<H", 1),
        E(33421, _SHORT, "<HH", 2, 2),
        (33422, _BYTE, 4, bytes([0, 1, 1, 2])),
        E(50714, _SHORT, "<H", 0),
        E(50717, _LONG, "<I", 4095),
    ]
    shared = [
        (50706, _BYTE, 4, bytes([1, 4, 0, 0])),
        (50708, _ASCII, len(cam), cam),
        (50721, _SRAT, 9, cm),
        E(50778, _SHORT, "<H", 21),
        E(274, _SHORT, "<H", orientation),
    ]

    if preview_jpeg is None:
        ifd0 = raw_entries + shared + [E(273, _LONG, "<I", 0)]
        strip_off = 8 + _dng_ifd_size(ifd0)
        ifd0[-1] = E(273, _LONG, "<I", strip_off)
        out = struct.pack("<2sHI", b"II", 42, 8) + _dng_ifd(ifd0, 8)
        assert len(out) == strip_off
        return out + strip
    else:
        pw, ph = preview_size
        ifd0 = [
            E(254, _LONG, "<I", 1),
            E(256, _LONG, "<I", pw), E(257, _LONG, "<I", ph),
            (258, _SHORT, 3, struct.pack("<HHH", 8, 8, 8)),
            E(259, _SHORT, "<H", 7),
            E(262, _SHORT, "<H", 6),
            E(277, _SHORT, "<H", 3),
            E(278, _LONG, "<I", ph),
            E(279, _LONG, "<I", len(preview_jpeg)),
            E(273, _LONG, "<I", 0),
            E(330, _LONG, "<I", 0),
        ] + shared
        raw_ifd = raw_entries + [E(273, _LONG, "<I", 0)]
        sub_off = 8 + _dng_ifd_size(ifd0)
        jpeg_off = sub_off + _dng_ifd_size(raw_ifd)
        strip_off = jpeg_off + len(preview_jpeg)
        ifd0 = [E(273, _LONG, "<I", jpeg_off) if e[0] == 273
                else E(330, _LONG, "<I", sub_off) if e[0] == 330
                else e for e in ifd0]
        raw_ifd[-1] = E(273, _LONG, "<I", strip_off)
        out = struct.pack("<2sHI", b"II", 42, 8) + _dng_ifd(ifd0, 8)
        assert len(out) == sub_off
        out += _dng_ifd(raw_ifd, sub_off)
        assert len(out) == jpeg_off
        return out + preview_jpeg + strip


def _blue_jpeg(w: int = 64, h: int = 48) -> bytes:
    """Encode a blue-fill JPEG matching the CI smoke library palette
    (QColor 40, 90, 160) so the DNG tile contributes to the existing
    blue-dominant screenshot assertion."""
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(40, 90, 160))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    assert img.save(buf, "JPEG", 85), "QImage.save failed"
    return bytes(buf.data())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Encoding 64x48 blue JPEG preview...")
    pj = _blue_jpeg(64, 48)
    print(f"  preview JPEG: {len(pj)} bytes")

    print("Building synthetic DNG...")
    dng = _make_dng_bytes(w=64, h=48, orientation=1,
                          preview_jpeg=pj, preview_size=(64, 48))
    OUT_DNG.write_bytes(dng)
    print(f"  {OUT_DNG}: {len(dng)} bytes")

    # Verify: rawpy can parse it and extract the preview.
    import io

    import rawpy
    with rawpy.imread(io.BytesIO(dng)) as raw:
        thumb = raw.extract_thumb()
    assert thumb.format == rawpy.ThumbFormat.JPEG, \
        f"Expected JPEG thumb, got {thumb.format}"
    print(f"  rawpy verification: ThumbFormat.JPEG, {len(thumb.data)} bytes OK")

    OUT_NOTE.write_text(
        "synthetic.dng — provenance\n"
        "==========================\n"
        "\n"
        "Generated by scripts/make-synthetic-dng.py (fauxcasa-v46.5).\n"
        "\n"
        "This is a SYNTHETIC file — no real camera, no personal data.\n"
        "\n"
        "Structure:\n"
        "  - Tiny valid DNG 1.4 (little-endian TIFF container)\n"
        "  - 64x48 RGGB CFA strip (16-bit, deterministic gradient)\n"
        "  - Embedded 64x48 JPEG preview (blue fill QColor(40,90,160))\n"
        "  - TIFF Orientation=1 (no rotation)\n"
        "  - Camera model: 'Fauxcasa Synthetic'\n"
        "\n"
        "Purpose: CI smoke test gate — bundle.yml copies this file into\n"
        "the ci-library so the frozen artifact's RAW decode path (rawpy)\n"
        "is exercised and gated, catching hidden-import gaps like #45.\n"
        "\n"
        "The blue fill matches the JPEG smoke library palette so the\n"
        "existing screenshot assertion's blue-dominant check covers the\n"
        "DNG tile without a separate color gate.\n"
        "\n"
        "To regenerate: uv run scripts/make-synthetic-dng.py\n",
        encoding="utf-8",
    )
    print(f"  {OUT_NOTE}: written")
    print("Done.")


if __name__ == "__main__":
    main()
    sys.exit(0)
