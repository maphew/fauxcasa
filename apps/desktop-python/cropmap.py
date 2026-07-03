"""Stored-frame fraction-rect math for Picasa crop/face regions
(fauxcasa-cam.15), shared by every consumer that maps rect64 fractions
into displayed pixels: the thumbnail bake (thumbcache._index_one), the
viewer's original decode (viewer.load_original_oriented), and the face
overlay (viewer.map_face_fraction delegates to map_fraction_rect;
viewer._face_rects rebases face rects through the crop).

rect64 fractions (crop=, faces=) are relative to the STORED pixels —
rotate= does NOT transform them and EXIF orientation handling is the
consumer's job (picasa-ini-format.md). The display pipeline composes,
in this order:

    crop (fauxcasa-cam.15)  ->  EXIF orientation  ->  rotate=

The order is load-bearing and evidence-backed:

- crop coordinates are fractions of the stored-on-disk dimensions (the
  rect64 grammar in picasa-ini-format.md; oracle fixture 004 writes
  ``crop=rect64(dc3369dc570a51e)`` against the on-disk photo, and
  fixture 005's bake rewrites the file to exactly that sub-rect of the
  ORIGINAL 800x600 pixels — 574x259 out), so the crop selects stored
  pixels FIRST;
- the format doc pins that rotate= "does NOT transform faces/crop
  coords", so the crop must be interpreted before the user turns;
- EXIF orientation is likewise a display transform OF stored pixels,
  applied by every decode path (autoTransform / the LibRaw bake), so it
  comes after the stored-frame crop and before rotate= — the same
  composition the face overlay already used (viewer.py, PR #52).

Every decode path in this app hands back EXIF-UPRIGHT pixels, so rather
than cropping stored pixels and re-orienting, consumers crop the upright
image with the orientation-MAPPED rect (crop_qimage_upright) — cropping
stored pixels then orienting is mathematically identical to orienting
then cropping with the mapped rect, and it costs one decode. Pure math
throughout (Qt is imported only inside crop_qimage_upright), so the
coordinate tests need no widgets.
"""

from __future__ import annotations

FractionRect = tuple[float, float, float, float]  # left, top, right, bottom

# Stored-frame fractional point (x right, y down, both 0..1) -> upright-frame
# point, one entry per EXIF Orientation value. Each is the display transform
# autoTransform applies to the pixels: 2 mirror-H, 3 rotate 180, 4 mirror-V,
# 5 transpose (main diagonal), 6 rotate 90 CW, 7 transverse (anti-diagonal),
# 8 rotate 90 CCW. Derivation check for 6: rotating an image 90 CW sends
# stored top-left (0,0) to upright top-right (1,0) = (1-y, x). Single-sourced
# here (moved from viewer.py) so the crop bake and the face overlay can never
# drift apart; verified against Qt's own pixel transforms by the 8x4 matrix
# test in test_tracer.py.
ORIENT_MAP = {
    1: lambda x, y: (x, y),
    2: lambda x, y: (1.0 - x, y),
    3: lambda x, y: (1.0 - x, 1.0 - y),
    4: lambda x, y: (x, 1.0 - y),
    5: lambda x, y: (y, x),
    6: lambda x, y: (1.0 - y, x),
    7: lambda x, y: (1.0 - y, 1.0 - x),
    8: lambda x, y: (y, 1.0 - x),
}


def map_fraction_rect(rect: FractionRect, orientation: int = 1,
                      rotate: int = 0) -> FractionRect:
    """A stored-pixel fractional rect (left, top, right, bottom) -> the
    SAME rect in the DISPLAYED frame: EXIF orientation first (all 8 cases
    incl. mirrors), then rotate= quarter-turns clockwise, i.e. exactly the
    transform the pixels get. Pure and fail-soft: an out-of-range
    orientation reads as 1 (matching metareader.read_orientation), rotate
    is taken mod 4, and the result is corner-normalized so mirrored cases
    stay (l, t, r, b)."""
    left, top, right, bottom = rect
    f = ORIENT_MAP.get(orientation, ORIENT_MAP[1])
    (x0, y0), (x1, y1) = f(left, top), f(right, bottom)
    for _ in range(rotate % 4):
        # one clockwise quarter-turn of the frame: (x, y) -> (1 - y, x)
        (x0, y0), (x1, y1) = (1.0 - y0, x0), (1.0 - y1, x1)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def rebase_fraction_rect(rect: FractionRect,
                         crop: FractionRect) -> FractionRect | None:
    """A stored-frame fraction rect re-expressed as fractions of the crop
    sub-rect (still the stored frame) — what a faces= rect means once the
    displayed image IS the crop: faces on a cropped photo still reference
    STORED pixels, so they rebase through the crop FIRST, then take the
    same EXIF x rotate mapping as the pixels (map_fraction_rect). The
    result is clamped to the crop (a face straddling the crop edge shows
    its visible part); None when the rect misses the crop entirely (those
    pixels are not on screen) or the crop is degenerate."""
    cl, ct, cr, cb = crop
    cw, ch = cr - cl, cb - ct
    if cw <= 0.0 or ch <= 0.0:
        return None
    left = min(1.0, max(0.0, (rect[0] - cl) / cw))
    top = min(1.0, max(0.0, (rect[1] - ct) / ch))
    right = min(1.0, max(0.0, (rect[2] - cl) / cw))
    bottom = min(1.0, max(0.0, (rect[3] - ct) / ch))
    if right - left <= 0.0 or bottom - top <= 0.0:
        return None
    return (left, top, right, bottom)


def crop_pixel_box(crop: FractionRect, w: int,
                   h: int) -> tuple[int, int, int, int] | None:
    """The integer pixel box (x, y, width, height) for fraction rect
    `crop` on a w x h image: rounded, clamped inside the image, never
    empty (each axis floors at 1 px). None for a degenerate/invalid rect
    or image — callers then skip the crop (fail-soft, like every other
    ini-value consumer)."""
    if w <= 0 or h <= 0:
        return None
    left, top, right, bottom = crop
    if not (right > left and bottom > top):
        return None
    x0 = min(w - 1, max(0, round(left * w)))
    y0 = min(h - 1, max(0, round(top * h)))
    x1 = min(w, max(x0 + 1, round(right * w)))
    y1 = min(h, max(y0 + 1, round(bottom * h)))
    return (x0, y0, x1 - x0, y1 - y0)


def crop_qimage_upright(img, crop: FractionRect, orientation: int = 1):
    """Crop an EXIF-UPRIGHT QImage to the STORED-frame fraction rect
    `crop`. The rect is mapped through the photo's EXIF orientation into
    the upright frame first (module doc: crop-then-orient == orient-then-
    crop-with-the-mapped-rect), then the pixel box is copied out.
    Fail-soft: a null image or a degenerate rect returns the image
    unchanged — a bad recipe must never blank a photo."""
    if img is None or img.isNull():
        return img
    mapped = map_fraction_rect(crop, orientation, 0)
    box = crop_pixel_box(mapped, img.width(), img.height())
    if box is None:
        return img
    return img.copy(box[0], box[1], box[2], box[3])
