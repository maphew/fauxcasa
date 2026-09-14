#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pillow",
#   "piexif",
#   "av",
#   "exiv2",
# ]
# ///
"""Generate a realistic-looking demo photo library at cache/demo-library/
for documentation screenshots and manual testing.

Complements scripts/make-synthetic-library.py's ``--picasa-extras`` profile
(edge-case-dense, deliberately ugly on purpose) with the opposite goal:
something a human can actually look at and believe is someone's photo
library. Two layers of realism:

1. Real, freely licensed photos (picsum.photos, backed by Unsplash) of
   varied resolution/orientation, PLUS a handful of real camera-JPEG/TIFF/
   HEIC files borrowed verbatim from the ianare/exif-samples corpus (see
   ``scripts/fetch-test-datasets.py exif-samples``) for genuine camera
   quirks (real GPS, real EXIF orientation, HDR/mobile makernotes).
2. A full synthetic metadata layer matching what Picasa 3.9 actually
   writes: in-file EXIF (dates/camera/GPS) + XMP/IPTC (caption/keywords)
   on the picsum photos, and a full ``.picasa.ini`` sidecar / contacts.xml /
   ``.pal`` album layer on top (stars, captions, keywords, faces, albums,
   geotags, hidden, rotate) -- format copied byte-for-byte from
   ``make_extras_library`` in make-synthetic-library.py: CRLF ini lines,
   ``[Contacts2]`` ``<16-hex-id>=Name;;``, ``faces=rect64(<hex>),<id>;...``,
   ``[.album:<uid>]`` + ``token=]album:<uid>`` + ``date=``, membership
   ``albums=<uid>,<uid>``.

Output: cache/demo-library/ (gitignored -- nothing downloaded here is ever
committed):

  library/            the photo library (folders + .picasa.ini sidecars)
  contacts/contacts.xml
  albums/*.pal
  manifest.json        plan summary + expected ingest counts
  ATTRIBUTION.md        picsum author credits + exif-samples CC BY-SA note
  .downloads/           cached raw picsum downloads (never re-fetched)

Idempotent: re-running keeps existing downloads and derived photo files
(rewrite only if missing, or pass --force); sidecars/manifest/attribution
are always rewritten so PLAN edits below propagate immediately.

The PLAN table below is deliberately literal and easy to edit -- picsum ids
were chosen by guessing at plausible content from picsum's id range
(10..1084); run with --contact-sheet to render a labeled contact sheet and
eyeball what each id actually depicts, then swap ids in PLAN as needed.

Usage (uv only; on Windows: ``uv run scripts/make-demo-library.py``):
  make-demo-library.py                              # build/refresh the library
  make-demo-library.py --force                       # rebuild derived photos too
  make-demo-library.py --contact-sheet out/sheet.png  # + a labeled contact sheet
"""

from __future__ import annotations

import argparse
import datetime
import json
import struct
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import piexif
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
DEMO_ROOT = REPO / "cache" / "demo-library"
LIBRARY = DEMO_ROOT / "library"
DOWNLOADS = DEMO_ROOT / ".downloads"
CONTACTS_DIR = DEMO_ROOT / "contacts"
ALBUMS_DIR = DEMO_ROOT / "albums"
EXIF_SAMPLES_ROOT = REPO / "cache" / "test-datasets" / "exif-samples"

USER_AGENT = "fauxcasa-demo-library/1.0"

APP_DIR = REPO / "apps" / "desktop-python"


# --------------------------------------------------------------------------
# Contact / album ids (deterministic, computed -- not hand-counted hex).
# --------------------------------------------------------------------------

CONTACT_ALICE = f"{0xA11CE:016x}"       # Alice Hartley
CONTACT_BEN = f"{0xBE2000:016x}"        # Ben Okafor
CONTACT_CHLOE = f"{0xC410E:016x}"       # Chloe Nguyen
CONTACT_DEV = f"{0xDE7000:016x}"        # Dev Patel (ini-only: not in contacts.xml)
CONTACT_ORPHAN = f"{0xBADFACE:016x}"    # faces= only: named nowhere (unnamed face)

NAMED_CONTACTS = {
    CONTACT_ALICE: "Alice Hartley",
    CONTACT_BEN: "Ben Okafor",
    CONTACT_CHLOE: "Chloe Nguyen",
    CONTACT_DEV: "Dev Patel",
}
CONTACTS_XML_IDS = (CONTACT_ALICE, CONTACT_BEN, CONTACT_CHLOE)  # NOT dev
CONTACTS_XML_MTIME = "2019-11-01T08:00:00-07:00"

ALBUM_BEST_OF_2014 = f"{0xBE5702014:032x}"
ALBUM_FAMILY = f"{0xFA51170:032x}"
ALBUM_LAKE_PICKS = f"{0x1A4E7269C5:032x}"  # .pal-only, no ini definition anywhere

ALBUMS = {
    "best_of_2014": (ALBUM_BEST_OF_2014, "Best of 2014"),
    "family": (ALBUM_FAMILY, "Family"),
}

DBID = f"{0xD81D:032x}"

# Rect64 presets (left, top, right, bottom fractions of stored pixels).
FACE_SOLO = (0.32, 0.12, 0.68, 0.58)
FACE_LEFT = (0.08, 0.15, 0.42, 0.62)
FACE_RIGHT = (0.58, 0.15, 0.92, 0.62)


# --------------------------------------------------------------------------
# Helpers copied verbatim (per spec) from apps/desktop-python/test_tracer.py
# (_inject/_xmp_app1/_iptc_app13) and test_sandbox_e2e.py (_rect64_hex), and
# from make-synthetic-library.py (_pal_xml) -- scripts are standalone, so
# these are copied rather than imported.
# --------------------------------------------------------------------------


def _inject(jpeg: bytes, marker: int, payload: bytes) -> bytes:
    """Splice an APPn marker segment in right after SOI (FFD8)."""
    assert jpeg[:2] == b"\xff\xd8"
    seg = bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload
    return jpeg[:2] + seg + jpeg[2:]


def _xmp_app1(caption: str | None = None, keywords: tuple[str, ...] = ()) -> bytes:
    body = ""
    if caption is not None:
        body += ('<dc:description><rdf:Alt>'
                 f'<rdf:li xml:lang="x-default">{caption}</rdf:li>'
                 '</rdf:Alt></dc:description>')
    if keywords:
        lis = "".join(f"<rdf:li>{k}</rdf:li>" for k in keywords)
        body += f"<dc:subject><rdf:Bag>{lis}</rdf:Bag></dc:subject>"
    xml = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<rdf:Description rdf:about="">{body}</rdf:Description>'
        '</rdf:RDF></x:xmpmeta>'
    )
    return b"http://ns.adobe.com/xap/1.0/\x00" + xml.encode("utf-8")


def _iptc_app13(caption: str | None = None,
               keywords: tuple[str, ...] = ()) -> bytes:
    def ds(record: int, dataset: int, value: bytes) -> bytes:
        return bytes([0x1C, record, dataset]) + struct.pack(">H", len(value)) \
            + value

    iim = b""
    if caption is not None:
        iim += ds(2, 120, caption.encode("utf-8"))
    for k in keywords:
        iim += ds(2, 25, k.encode("utf-8"))
    block = b"8BIM" + struct.pack(">H", 0x0404) + b"\x00\x00" \
        + struct.pack(">I", len(iim)) + iim
    if len(iim) % 2:
        block += b"\x00"
    return b"Photoshop 3.0\x00" + block


def _rect64_hex(left: float, top: float, right: float, bottom: float) -> str:
    return "".join(format(round(v * 65536), "04x")
                   for v in (left, top, right, bottom))


def _pal_xml(uid: str, name: str, members: list[str]) -> str:
    """One .pal file, copied from make-synthetic-library.py's _pal_xml."""
    files = "\n".join(
        f" <filename>[C]\\{m.replace('/', chr(92))}</filename>" for m in members
    )
    return (
        "<picasa2album>\n"
        f"<dbid>{DBID}</dbid>\n"
        f"<albumid>{uid}</albumid>\n"
        f'<property name="uid" type="string" value="{uid}"/>\n'
        f'<property name="category" type="num" value="0"/>\n'
        f'<property name="date" type="real64" value="39272.630035"/>\n'
        f'<property name="token" type="string" value="]album:{uid}"/>\n'
        f'<property name="name" type="string" value="{name}">\n'
        "<files>\n"
        f"{files}\n"
        "</files>\n"
        "</property>\n"
        "</picasa2album>\n"
    )


# --------------------------------------------------------------------------
# PLAN: the data table. Edit ids/sizes/metadata here.
# --------------------------------------------------------------------------


@dataclass
class PhotoSpec:
    id: int
    size: tuple[int, int]
    bw: bool = False
    camera: tuple[str, str] | None = None
    day_offset: int | None = None
    hour: int | None = None
    gps: tuple[float, float] | None = None
    caption: str | None = None
    keywords: tuple[str, ...] = ()
    star: bool = False
    faces: list[tuple[tuple[float, float, float, float], str]] = field(
        default_factory=list)
    albums: list[str] = field(default_factory=list)  # keys into ALBUMS
    hidden: bool = False
    rotate: int = 0
    ini_geotag: tuple[float, float] | None = None
    orient_hack: str | None = None  # None | "cw" | "ccw"

    @property
    def file(self) -> str:
        return f"IMG_{self.id:04d}.jpg"


@dataclass
class Folder:
    name: str  # library-relative path; may nest, e.g. "Scans/1988 Family Album"
    date: tuple[int, int, int]
    description: str | None = None
    camera: tuple[str, str] | None = None
    photos: list[PhotoSpec] = field(default_factory=list)

    def __post_init__(self) -> None:
        for idx, spec in enumerate(self.photos):
            if spec.camera is None:
                spec.camera = self.camera
            if spec.day_offset is None:
                spec.day_offset = idx // 3
            if spec.hour is None:
                spec.hour = 8 + (idx % 10)


PLAN: list[Folder] = [
    Folder(
        name="2008-08-16 Lake Weekend",
        date=(2008, 8, 16),
        description="Cabin weekend at the lake",
        camera=("Canon", "EOS 40D"),
        photos=[
            PhotoSpec(id=10, size=(2400, 1600), star=True,
                      keywords=("lake", "family")),
            PhotoSpec(id=11, size=(1600, 1067),
                      caption="The valley below the cabin", keywords=("lake", "hike")),
            PhotoSpec(id=12, size=(1200, 1800),
                      caption="Low tide on the rocks", keywords=("beach", "family")),
            PhotoSpec(id=13, size=(2400, 1600),
                      gps=(51.4254, -116.1773), keywords=("lake", "beach")),
            PhotoSpec(id=14, size=(1600, 1067), star=True),
            PhotoSpec(id=15, size=(1200, 1800),
                      caption="The falls, after the rain", keywords=("waterfall", "hike")),
            PhotoSpec(id=16, size=(3600, 2400)),  # the big one
            PhotoSpec(id=19, size=(2400, 1600), hidden=True),
            PhotoSpec(id=65, size=(2016, 1512), keywords=("family", "sunset"),
                      caption="Evening in the long grass"),
        ],
    ),
    Folder(
        name="2012-05-20 Garden",
        date=(2012, 5, 20),
        camera=("Nikon", "D70"),
        photos=[
            PhotoSpec(id=25, size=(2000, 1333), keywords=("garden", "spring")),
            PhotoSpec(id=106, size=(1333, 2000), star=True, keywords=("flowers",)),
            PhotoSpec(id=152, size=(2000, 1333), rotate=1,
                      keywords=("garden", "flowers")),
            PhotoSpec(id=82, size=(1333, 2000), keywords=("spring", "flowers"),
                      caption="First blossom on the plum tree"),
            PhotoSpec(id=118, size=(2000, 1333),
                      ini_geotag=(49.2827, -123.1207), keywords=("flowers",)),
            PhotoSpec(id=75, size=(1333, 2000), keywords=("garden",),
                      caption="Grapes, finally"),
        ],
    ),
    Folder(
        name="2014-10-03 City Trip",
        date=(2014, 10, 3),
        camera=("Apple", "iPhone 5s"),
        photos=[
            PhotoSpec(id=318, size=(2448, 3264), gps=(48.8584, 2.2945),
                      caption="First evening in Paris", keywords=("city", "travel"),
                      star=True, albums=["best_of_2014"]),
            PhotoSpec(id=234, size=(2448, 3264), gps=(48.8566, 2.3522),
                      keywords=("architecture", "city")),
            PhotoSpec(id=57, size=(3264, 2448), keywords=("city",)),
            PhotoSpec(id=257, size=(1080, 1920), gps=(52.3676, 4.9041),
                      caption="Canal lights", keywords=("night", "city"),
                      star=True, albums=["best_of_2014"]),
            PhotoSpec(id=288, size=(3264, 2448), gps=(59.3293, 18.0686),
                      caption="Stockholm from the water",
                      keywords=("travel", "city")),
            PhotoSpec(id=49, size=(3264, 2448), gps=(36.4618, 25.3753),
                      caption="Santorini", keywords=("travel", "architecture"),
                      albums=["best_of_2014"]),
            PhotoSpec(id=1040, size=(2448, 3264), gps=(47.5576, 10.7498),
                      keywords=("architecture", "travel")),
            PhotoSpec(id=58, size=(2448, 3264), gps=(38.7139, -9.1394),
                      keywords=("travel",)),
            PhotoSpec(id=134, size=(3264, 2448), keywords=("architecture",)),
            PhotoSpec(id=129, size=(1080, 1920), keywords=("travel",),
                      caption="Waiting for the ferry"),
        ],
    ),
    Folder(
        name="2016-07-22 Mountain Hike",
        date=(2016, 7, 22),
        camera=("Sony", "ILCE-6000"),
        photos=[
            PhotoSpec(id=29, size=(2400, 1600), keywords=("hike", "trail")),
            PhotoSpec(id=1043, size=(4000, 2667),  # the big file
                      caption="The valley from the pass",
                      keywords=("mountains", "summit"), star=True),
            PhotoSpec(id=190, size=(427, 640),   # the small file
                      keywords=("trail",)),
            PhotoSpec(id=1036, size=(2400, 1600), gps=(51.1784, -115.5708),
                      star=True, keywords=("hike", "mountains")),
            PhotoSpec(id=177, size=(1512, 2016), keywords=("hike", "summit"),
                      caption="Made it", star=True),
            PhotoSpec(id=1037, size=(2400, 1600), gps=(52.8737, -118.0814),
                      keywords=("mountains", "sunrise")),
            PhotoSpec(id=1015, size=(1600, 2400), star=True,
                      keywords=("trail", "hike"), albums=["best_of_2014"]),
            PhotoSpec(id=1016, size=(2400, 1600)),
            PhotoSpec(id=1018, size=(2400, 1600), keywords=("summit", "trail")),
        ],
    ),
    Folder(
        name="2019-12-24 Holidays",
        date=(2019, 12, 24),
        camera=("Google", "Pixel 3"),
        photos=[
            PhotoSpec(id=64, size=(1512, 2016),
                      caption="Christmas Eve", keywords=("holiday", "family"),
                      star=True, faces=[((0.30, 0.08, 0.72, 0.42), CONTACT_ALICE)],
                      albums=["family"]),
            PhotoSpec(id=1027, size=(2016, 1512), keywords=("family", "portrait"),
                      faces=[((0.36, 0.12, 0.84, 0.72), CONTACT_CHLOE)],
                      albums=["family"], star=True),
            PhotoSpec(id=1013, size=(2016, 1512), keywords=("holiday", "portrait"),
                      caption="Off to the grandparents",
                      faces=[((0.44, 0.18, 0.76, 0.56), CONTACT_ALICE)],
                      albums=["family"]),
            PhotoSpec(id=1010, size=(2016, 1512), keywords=("family",),
                      faces=[((0.12, 0.10, 0.44, 0.48), CONTACT_BEN)],
                      albums=["family"]),
            PhotoSpec(id=1066, size=(2016, 1512), keywords=("family", "baby"),
                      caption="Christmas morning",
                      faces=[((0.34, 0.28, 0.66, 0.62), CONTACT_ORPHAN)],
                      albums=["family"]),
            PhotoSpec(id=395, size=(2016, 1512), keywords=("holiday",),
                      caption="Christmas Eve dinner",
                      faces=[((0.04, 0.22, 0.20, 0.46), CONTACT_BEN),
                             ((0.24, 0.18, 0.40, 0.42), CONTACT_DEV)],
                      albums=["family"]),
            PhotoSpec(id=342, size=(1512, 2016), keywords=("holiday", "travel"),
                      caption="Christmas market"),
        ],
    ),
    Folder(
        name="2021-03 Phone",
        date=(2021, 3, 1),
        camera=("Samsung", "SM-G991B"),
        photos=[
            PhotoSpec(id=1005, size=(1200, 1600), keywords=("selfie",),
                      faces=[((0.38, 0.14, 0.72, 0.46), CONTACT_DEV)]),
            PhotoSpec(id=1011, size=(1200, 1600), keywords=("boat", "lake"),
                      caption="Spring paddle"),
            PhotoSpec(id=1012, size=(1200, 1600), keywords=("dog", "family"),
                      faces=[((0.06, 0.08, 0.36, 0.40), CONTACT_BEN)]),
            PhotoSpec(id=1025, size=(1200, 1600), keywords=("dog",),
                      orient_hack="cw"),
            PhotoSpec(id=1062, size=(1200, 1600), keywords=("dog",),
                      orient_hack="ccw"),
        ],
    ),
    Folder(
        name="Scans/1988 Family Album",
        date=(1988, 6, 1),
        description="Scanned from the blue album",
        camera=None,
        photos=[
            PhotoSpec(id=200, size=(1400, 1000), bw=True, star=True,
                      keywords=("scan", "1988"), caption="Uncle Ray's farm"),
            PhotoSpec(id=263, size=(1400, 1000), bw=True,
                      keywords=("bw", "1988"), caption="The old car"),
            PhotoSpec(id=203, size=(1400, 1000), bw=True,
                      keywords=("scan", "bw")),
            PhotoSpec(id=209, size=(1400, 1000), bw=True, keywords=("1988",)),
        ],
    ),
]

# Lake Weekend photo ids picked for the .pal-only "Lake trip picks" album.
LAKE_PAL_MEMBER_IDS = (10, 13, 16)

# The Ken Burns clip: which folder + which downloaded photo it pans across.
VIDEO_FOLDER = "2019-12-24 Holidays"
VIDEO_SOURCE_ID = 342
VIDEO_FILE = "VID_0001.mp4"
VIDEO_SIZE = (640, 360)
VIDEO_SECONDS = 3
VIDEO_FPS = 12

# Camera Odds and Ends: real camera files copied verbatim from exif-samples.
COE_FOLDER_NAME = "Camera Odds and Ends"
COE_SOURCES: list[tuple[str, str]] = [
    ("jpg/gps/DSCN0010.jpg", "DSCN0010.jpg"),
    ("jpg/gps/DSCN0021.jpg", "DSCN0021.jpg"),
    ("jpg/gps/DSCN0042.jpg", "DSCN0042.jpg"),  # real GPS
    ("jpg/orientation/portrait_6.jpg", "portrait_6.jpg"),   # orientation 6
    ("jpg/orientation/landscape_3.jpg", "landscape_3.jpg"),  # orientation 3
    ("jpg/hdr/iphone_hdr_YES.jpg", "iphone_hdr_YES.jpg"),
    ("jpg/mobile/HMD_Nokia_8.3_5G.jpg", "HMD_Nokia_8.3_5G.jpg"),
    ("heic/IMG_5195.HEIC", "IMG_5195.HEIC"),
    ("tiff/Arbitro.tiff", "Arbitro.tiff"),
    ("jpg/Canon_40D.jpg", "Canon_40D.jpg"),
]
COE_STAR_FILE = "Canon_40D.jpg"
COE_REAL_GPS_FILE = "DSCN0042.jpg"


# --------------------------------------------------------------------------
# picsum downloading
# --------------------------------------------------------------------------


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def download_picsum(photo_id: int, w: int, h: int, bw: bool) -> tuple[Path, Path]:
    """Return (raw_jpg_path, info_json_path), fetching + caching as needed."""
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    suffix = "-bw" if bw else ""
    raw_path = DOWNLOADS / f"picsum-{photo_id}-{w}x{h}{suffix}.jpg"
    if not raw_path.exists():
        url = f"https://picsum.photos/id/{photo_id}/{w}/{h}"
        if bw:
            url += "?grayscale"
        try:
            data = _http_get(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise SystemExit(
                    f"FATAL: picsum id {photo_id} 404s at size {w}x{h} -- "
                    f"pick a different id in PLAN (scripts/make-demo-library.py)"
                ) from e
            raise
        raw_path.write_bytes(data)
    info_path = DOWNLOADS / f"picsum-{photo_id}-info.json"
    if not info_path.exists():
        try:
            info = _http_get(f"https://picsum.photos/id/{photo_id}/info")
            info_path.write_bytes(info)
        except urllib.error.HTTPError:
            info_path.write_bytes(b"{}")
    return raw_path, info_path


# --------------------------------------------------------------------------
# In-file metadata injection
# --------------------------------------------------------------------------


def _deg_to_dms_rational(deg: float) -> list[tuple[int, int]]:
    d = int(deg)
    m = int((deg - d) * 60)
    s = round((deg - d - m / 60) * 3600 * 100)
    return [(d, 1), (m, 1), (s, 100)]


def _gps_ifd(lat: float, lon: float) -> dict:
    return {
        piexif.GPSIFD.GPSVersionID: (2, 0, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef: "N" if lat >= 0 else "S",
        piexif.GPSIFD.GPSLatitude: _deg_to_dms_rational(abs(lat)),
        piexif.GPSIFD.GPSLongitudeRef: "E" if lon >= 0 else "W",
        piexif.GPSIFD.GPSLongitude: _deg_to_dms_rational(abs(lon)),
    }


def _exif_stamp(date: tuple[int, int, int], day_offset: int, hour: int,
                idx: int) -> str:
    d = datetime.date(*date) + datetime.timedelta(days=day_offset)
    minute = (idx * 13) % 60
    return f"{d.year}:{d.month:02d}:{d.day:02d} {hour:02d}:{minute:02d}:00"


def build_photo(folder: Folder, spec: PhotoSpec, idx: int, out_path: Path,
                force: bool) -> dict:
    """Download (if needed) + resize/re-encode + inject EXIF/XMP/IPTC.
    Returns a summary record for the report table / manifest."""
    stamp = _exif_stamp(folder.date, spec.day_offset or 0, spec.hour or 8, idx)
    record = {
        "folder": folder.name, "file": spec.file, "picsum_id": spec.id,
        "dims": spec.size, "date": stamp, "gps": spec.gps is not None,
        "caption": spec.caption is not None, "kw_count": len(spec.keywords),
        "faces": len(spec.faces), "star": spec.star, "kind": "photo",
    }
    if out_path.exists() and not force:
        return record

    raw_path, _info_path = download_picsum(spec.id, *spec.size, spec.bw)
    img = Image.open(raw_path).convert("RGB")
    if img.size != tuple(spec.size):
        img = img.resize(spec.size)

    orientation = 1
    if spec.orient_hack == "cw":
        img = img.transpose(Image.Transpose.ROTATE_270)  # pixels rotated 90 CW
        orientation = 8
    elif spec.orient_hack == "ccw":
        img = img.transpose(Image.Transpose.ROTATE_90)  # pixels rotated 90 CCW
        orientation = 6

    zeroth = {
        piexif.ImageIFD.ImageWidth: img.width,
        piexif.ImageIFD.ImageLength: img.height,
        piexif.ImageIFD.Orientation: orientation,
    }
    if spec.camera:
        zeroth[piexif.ImageIFD.Make] = spec.camera[0]
        zeroth[piexif.ImageIFD.Model] = spec.camera[1]
    exif_ifd = {
        piexif.ExifIFD.DateTimeOriginal: stamp,
        piexif.ExifIFD.DateTimeDigitized: stamp,
    }
    gps_ifd = _gps_ifd(*spec.gps) if spec.gps else {}
    exif_bytes = piexif.dump({"0th": zeroth, "Exif": exif_ifd, "GPS": gps_ifd})

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=88, exif=exif_bytes)

    if spec.caption or spec.keywords:
        data = out_path.read_bytes()
        data = _inject(data, 0xE1, _xmp_app1(caption=spec.caption,
                                              keywords=tuple(spec.keywords)))
        data = _inject(data, 0xED, _iptc_app13(caption=spec.caption,
                                                keywords=tuple(spec.keywords)))
        out_path.write_bytes(data)
    return record


# --------------------------------------------------------------------------
# Camera Odds and Ends (real exif-samples files, copied verbatim)
# --------------------------------------------------------------------------


def build_camera_odds_and_ends(force: bool) -> list[dict]:
    if not EXIF_SAMPLES_ROOT.exists():
        print(
            "NOTE: cache/test-datasets/exif-samples not found -- skipping "
            f"'{COE_FOLDER_NAME}'. Fetch it with:\n"
            "  uv run scripts/fetch-test-datasets.py exif-samples"
        )
        return []
    folder_dir = LIBRARY / COE_FOLDER_NAME
    folder_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    lines: list[str] = ["[encoding]", "utf8=1"]
    for rel, out_name in COE_SOURCES:
        src = EXIF_SAMPLES_ROOT / rel
        dst = folder_dir / out_name
        if not src.exists():
            print(f"WARNING: exif-samples file missing, skipping: {src}")
            continue
        if force or not dst.exists():
            dst.write_bytes(src.read_bytes())
        dims = None
        try:
            with Image.open(dst) as im:
                dims = im.size
        except Exception:
            dims = None
        records.append({
            "folder": COE_FOLDER_NAME, "file": out_name, "picsum_id": None,
            "dims": dims, "date": None,
            "gps": out_name == COE_REAL_GPS_FILE,
            "caption": False, "kw_count": 0, "faces": 0,
            "star": out_name == COE_STAR_FILE, "kind": "camera-sample",
        })
    for out_name in [n for _r, n in COE_SOURCES]:
        if (folder_dir / out_name).exists():
            lines.append(f"[{out_name}]")
            if out_name == COE_STAR_FILE:
                lines.append("star=yes")
    (folder_dir / ".picasa.ini").write_bytes(
        ("\r\n".join(lines) + "\r\n").encode("utf-8"))
    return records


# --------------------------------------------------------------------------
# .picasa.ini writer
# --------------------------------------------------------------------------


def _strip0(hexid: str) -> str:
    return hexid.lstrip("0") or "0"


def _iso(date: tuple[int, int, int], day_offset: int, hour: int) -> str:
    d = datetime.date(*date) + datetime.timedelta(days=day_offset)
    return f"{d.year}-{d.month:02d}-{d.day:02d}T{hour:02d}:00:00-07:00"


def write_folder_ini(folder: Folder, album_defs: list[tuple[str, str, str]],
                     video_spec: dict | None) -> None:
    folder_dir = LIBRARY / folder.name
    lines: list[str] = []
    if folder.description:
        leaf = Path(folder.name).name
        lines += ["[Picasa]", f"name={leaf}", f"description={folder.description}"]
    lines += ["[encoding]", "utf8=1"]

    contacts_used: list[str] = []
    for spec in folder.photos:
        for _rect, cid in spec.faces:
            if cid in NAMED_CONTACTS and cid not in contacts_used:
                contacts_used.append(cid)
    if contacts_used:
        lines.append("[Contacts2]")
        for cid in contacts_used:
            lines.append(f"{cid}={NAMED_CONTACTS[cid]};;")

    for uid, name, date_str in album_defs:
        lines += [f"[.album:{uid}]", f"name={name}", f"token=]album:{uid}",
                  f"date={date_str}"]

    for spec in folder.photos:
        lines.append(f"[{spec.file}]")
        if spec.star:
            lines.append("star=yes")
        if spec.caption:
            lines.append(f"caption={spec.caption}")
        if spec.keywords:
            lines.append(f"keywords={', '.join(spec.keywords)}")
        if spec.faces:
            parts = [f"rect64({_rect64_hex(*rect)}),{_strip0(cid)}"
                     for rect, cid in spec.faces]
            lines.append("faces=" + ";".join(parts))
        if spec.albums:
            uids = [ALBUMS[a][0] for a in spec.albums]
            lines.append("albums=" + ",".join(uids))
        if spec.ini_geotag:
            lines.append(f"geotag={spec.ini_geotag[0]},{spec.ini_geotag[1]}")
        if spec.rotate:
            lines.append(f"rotate=rotate({spec.rotate})")
        if spec.hidden:
            lines.append("hidden=yes")

    if video_spec is not None:
        lines.append(f"[{video_spec['file']}]")
        lines.append("star=yes")
        lines.append(f"width={video_spec['size'][0]}")
        lines.append(f"height={video_spec['size'][1]}")

    folder_dir.mkdir(parents=True, exist_ok=True)
    (folder_dir / ".picasa.ini").write_bytes(
        ("\r\n".join(lines) + "\r\n").encode("utf-8"))


# --------------------------------------------------------------------------
# contacts.xml + .pal albums
# --------------------------------------------------------------------------


def write_contacts_xml() -> None:
    CONTACTS_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["<contacts>"]
    for cid in CONTACTS_XML_IDS:
        lines.append(
            f' <contact id="{cid}" name="{NAMED_CONTACTS[cid]}" '
            f'modified_time="{CONTACTS_XML_MTIME}" local_contact="1"/>')
    lines.append("</contacts>")
    (CONTACTS_DIR / "contacts.xml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def write_pal_albums() -> None:
    ALBUMS_DIR.mkdir(parents=True, exist_ok=True)
    for key, (uid, name) in ALBUMS.items():
        members = [f"{folder.name}/{spec.file}"
                   for folder in PLAN for spec in folder.photos
                   if key in spec.albums]
        (ALBUMS_DIR / f"{uid}.pal").write_text(
            _pal_xml(uid, name, members), encoding="utf-8", newline="\n")
    # "Lake trip picks": .pal-only album, no ini definition/membership.
    lake_folder = PLAN[0].name
    lake_members = [f"{lake_folder}/IMG_{pid:04d}.jpg" for pid in LAKE_PAL_MEMBER_IDS]
    (ALBUMS_DIR / f"{ALBUM_LAKE_PICKS}.pal").write_text(
        _pal_xml(ALBUM_LAKE_PICKS, "Lake trip picks", lake_members),
        encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------
# Ken Burns video clip
# --------------------------------------------------------------------------


def build_video(force: bool) -> dict | None:
    out_path = LIBRARY / VIDEO_FOLDER / VIDEO_FILE
    if out_path.exists() and not force:
        return {"file": VIDEO_FILE, "size": VIDEO_SIZE}
    src_path = LIBRARY / VIDEO_FOLDER / f"IMG_{VIDEO_SOURCE_ID:04d}.jpg"
    if not src_path.exists():
        print(f"WARNING: video source photo missing ({src_path}); "
              "skipping VID_0001.mp4")
        return None
    try:
        import av
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: PyAV unavailable ({e}); skipping VID_0001.mp4")
        return None

    try:
        src = Image.open(src_path).convert("RGB")
        sw, sh = src.size
        tw, th = VIDEO_SIZE
        ratio = tw / th
        nframes = VIDEO_SECONDS * VIDEO_FPS
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with av.open(str(out_path), "w") as container:
            stream = container.add_stream("h264", rate=VIDEO_FPS)
            stream.width, stream.height = tw, th
            stream.pix_fmt = "yuv420p"
            for i in range(nframes):
                t = i / max(1, nframes - 1)
                scale = 1.0 - 0.3 * t          # zoom in over time
                cx = 0.5 + 0.08 * t
                cy = 0.5 - 0.05 * t
                box_w = min(sw, sw * scale)
                box_h = box_w / ratio
                if box_h > sh:
                    box_h = sh
                    box_w = box_h * ratio
                left = min(max(0, sw * cx - box_w / 2), sw - box_w)
                top = min(max(0, sh * cy - box_h / 2), sh - box_h)
                box = (int(left), int(top), int(left + box_w), int(top + box_h))
                frame_img = src.crop(box).resize((tw, th))
                frame = av.VideoFrame.from_image(frame_img)
                for pkt in stream.encode(frame):
                    container.mux(pkt)
            for pkt in stream.encode():
                container.mux(pkt)
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: PyAV encoding failed ({e}); skipping VID_0001.mp4")
        if out_path.exists():
            out_path.unlink()
        return None
    return {"file": VIDEO_FILE, "size": VIDEO_SIZE}


# --------------------------------------------------------------------------
# manifest.json + ATTRIBUTION.md
# --------------------------------------------------------------------------


def write_attribution(records: list[dict]) -> None:
    lines = ["# Attribution", "",
             "Photos from picsum.photos (Unsplash-licensed, free to use):", ""]
    for r in records:
        pid = r.get("picsum_id")
        if pid is None:
            continue
        info_path = DOWNLOADS / f"picsum-{pid}-info.json"
        author, url = "unknown", f"https://picsum.photos/id/{pid}/info"
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            author = info.get("author", author)
            url = info.get("url", url)
        except Exception:
            pass
        lines.append(f"- {r['folder']}/{r['file']} -- picsum id {pid}, "
                     f"photo by {author} -- {url}")
    lines += [
        "",
        "Camera-quirk fixtures in 'Camera Odds and Ends' are copied verbatim "
        "from ianare/exif-samples (CC BY-SA 4.0): "
        "https://github.com/ianare/exif-samples",
        "",
    ]
    DEMO_ROOT.mkdir(parents=True, exist_ok=True)
    (DEMO_ROOT / "ATTRIBUTION.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def write_manifest(records: list[dict], video: dict | None) -> dict:
    photos = [r for r in records if r["kind"] != "video"]
    stars = sum(1 for r in records if r["star"])
    captions = sum(1 for r in records if r["caption"])
    faces = sum(r["faces"] for r in records)
    named_people = len(NAMED_CONTACTS)
    gps = sum(1 for r in records if r["gps"])
    folders = sorted({r["folder"] for r in records})
    albums_count = len(ALBUMS) + 1  # + the .pal-only "Lake trip picks"

    manifest = {
        "generated_by": "scripts/make-demo-library.py",
        "folders": folders,
        "expected": {
            "photos": len(photos),
            "videos": 1 if video else 0,
            "media_total": len(photos) + (1 if video else 0),
            "folders": len(folders),
            "stars": stars + (1 if video else 0),
            "captions": captions,
            "faces": faces,
            "named_people": named_people,
            "albums": albums_count,
            "gps": gps,
        },
        "album_uids": {k: v[0] for k, v in ALBUMS.items()},
        "pal_only_album_uid": ALBUM_LAKE_PICKS,
        "contact_ids": NAMED_CONTACTS,
        "orphan_contact_id": CONTACT_ORPHAN,
    }
    DEMO_ROOT.mkdir(parents=True, exist_ok=True)
    (DEMO_ROOT / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest


# --------------------------------------------------------------------------
# contact sheet
# --------------------------------------------------------------------------


def write_contact_sheet(records: list[dict], out_path: Path) -> None:
    thumb = 240
    label_h = 36
    cell_w, cell_h = thumb, thumb + label_h
    cols = 6
    rows = (len(records) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), (30, 30, 30))
    draw = ImageDraw.Draw(sheet)
    for i, r in enumerate(records):
        path = LIBRARY / r["folder"] / r["file"]
        col, row = i % cols, i // cols
        x0, y0 = col * cell_w, row * cell_h
        try:
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((thumb, thumb))
                ox = x0 + (thumb - im.width) // 2
                oy = y0 + (thumb - im.height) // 2
                sheet.paste(im, (ox, oy))
        except Exception as e:  # noqa: BLE001
            draw.rectangle([x0, y0, x0 + thumb, y0 + thumb], outline="red")
            draw.text((x0 + 4, y0 + 4), f"ERR\n{e}", fill="red")
        idlabel = f" id{r['picsum_id']}" if r.get("picsum_id") is not None else ""
        label = f"{r['folder']}/{r['file']}{idlabel}"
        draw.text((x0 + 2, y0 + thumb + 2), label[:40], fill="white")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, "PNG")


# --------------------------------------------------------------------------
# verification pass (round-trips a sample through the app's own readers)
# --------------------------------------------------------------------------


def run_verification() -> bool:
    sys.path.insert(0, str(APP_DIR))
    import inmeta  # noqa: PLC0415
    import metareader  # noqa: PLC0415

    # Derived from PLAN so an edit to the table can never drift from the
    # check: every picsum photo's caption/keywords (inmeta) and date/GPS
    # (metareader) must read back exactly as planned through the app's
    # own readers.
    checks = []
    for folder in PLAN:
        y, m, _d = folder.date
        for spec in folder.photos:
            checks.append({
                "path": LIBRARY / folder.name / spec.file,
                "caption": spec.caption,
                "keywords": set(spec.keywords),
                "date_prefix": f"{y:04d}-{m:02d}",
                "gps": spec.gps,
            })

    ok = True
    for c in checks:
        path: Path = c["path"]
        if not path.exists():
            print(f"VERIFY FAIL: missing {path}")
            ok = False
            continue
        data = path.read_bytes()
        meta = inmeta.read_jpeg_metadata(data)
        if meta.caption != c["caption"]:
            print(f"VERIFY FAIL: {path.name} caption "
                  f"{meta.caption!r} != {c['caption']!r}")
            ok = False
        if set(meta.keywords) != c["keywords"]:
            print(f"VERIFY FAIL: {path.name} keywords "
                  f"{set(meta.keywords)!r} != {c['keywords']!r}")
            ok = False
        fm = metareader.read_file_meta(data)
        if not fm.date_taken or not fm.date_taken.startswith(c["date_prefix"]):
            print(f"VERIFY FAIL: {path.name} date_taken "
                  f"{fm.date_taken!r} does not start with {c['date_prefix']!r}")
            ok = False
        if c["gps"] is None:
            if fm.gps is not None:
                print(f"VERIFY FAIL: {path.name} expected no gps, got {fm.gps!r}")
                ok = False
        else:
            if fm.gps is None or abs(fm.gps[0] - c["gps"][0]) > 0.01 \
                    or abs(fm.gps[1] - c["gps"][1]) > 0.01:
                print(f"VERIFY FAIL: {path.name} gps {fm.gps!r} != {c['gps']!r}")
                ok = False
    if ok:
        print(f"VERIFY: {len(checks)}/{len(checks)} sample photos round-tripped OK")
    return ok


# --------------------------------------------------------------------------
# summary table
# --------------------------------------------------------------------------


def print_summary(records: list[dict]) -> None:
    print(f"\n{'folder':<28} {'file':<20} {'dims':<12} {'date':<12} "
          f"{'gps':<4} {'cap':<4} {'kw':<3} {'faces':<5} {'star':<4}")
    for r in records:
        dims = f"{r['dims'][0]}x{r['dims'][1]}" if r["dims"] else "?"
        date = (r["date"] or "")[:10]
        print(f"{r['folder'][:28]:<28} {r['file']:<20} {dims:<12} {date:<12} "
              f"{'Y' if r['gps'] else '':<4} {'Y' if r['caption'] else '':<4} "
              f"{r['kw_count']:<3} {r['faces']:<5} {'Y' if r['star'] else '':<4}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def generate_library(force: bool) -> tuple[list[dict], dict | None]:
    LIBRARY.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []

    for folder in PLAN:
        folder_dir = LIBRARY / folder.name
        for idx, spec in enumerate(folder.photos):
            out_path = folder_dir / spec.file
            records.append(build_photo(folder, spec, idx, out_path, force))

    video = build_video(force)
    if video:
        records.append({
            "folder": VIDEO_FOLDER, "file": VIDEO_FILE, "picsum_id": None,
            "dims": VIDEO_SIZE, "date": None, "gps": False, "caption": False,
            "kw_count": 0, "faces": 0, "star": True, "kind": "video",
        })

    records += build_camera_odds_and_ends(force)

    # ini sidecars: always rewritten.
    seen_album_defs: set[str] = set()
    for folder in PLAN:
        album_defs: list[tuple[str, str, str]] = []
        for spec in folder.photos:
            for key in spec.albums:
                if key not in seen_album_defs:
                    uid, name = ALBUMS[key]
                    album_defs.append(
                        (uid, name, _iso(folder.date, 30, 12)))
                    seen_album_defs.add(key)
        video_spec = ({"file": VIDEO_FILE, "size": VIDEO_SIZE}
                      if video and folder.name == VIDEO_FOLDER else None)
        write_folder_ini(folder, album_defs, video_spec)

    write_contacts_xml()
    write_pal_albums()
    write_attribution(records)
    return records, video


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true",
                    help="rebuild derived photo files even if present "
                    "(downloads and sidecars always refresh regardless)")
    ap.add_argument("--contact-sheet", type=Path, default=None, metavar="PATH",
                    help="write a labeled PNG contact sheet of every photo")
    ap.add_argument("--skip-verify", action="store_true",
                    help="skip the inmeta/metareader round-trip verification pass")
    args = ap.parse_args()

    records, video = generate_library(args.force)
    manifest = write_manifest(records, video)

    print_summary(records)
    print(f"\n{len(records)} media files "
          f"({manifest['expected']['photos']} photos"
          f"{' + 1 video' if video else ''}) at {LIBRARY}")
    print(f"manifest: {DEMO_ROOT / 'manifest.json'}")
    print(f"expected counts: {json.dumps(manifest['expected'])}")

    verified = True
    if not args.skip_verify:
        verified = run_verification()

    if args.contact_sheet:
        write_contact_sheet(records, args.contact_sheet)
        print(f"\ncontact sheet: {args.contact_sheet}")

    print(
        "\nOpen it with:\n"
        "  uv run apps/desktop-python/main.py cache/demo-library/library "
        "--contacts cache/demo-library/contacts/contacts.xml "
        "--pal-dir cache/demo-library/albums"
    )

    if not verified:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
