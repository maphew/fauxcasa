#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pillow",
#   "piexif",
# ]
# ///
"""Generate a synthetic photo library at cache/synthetic-library/ for
differential testing against real Picasa (the Wine oracle) and Fauxcasa.

Fully synthetic — safe to show to agents, screenshot, or publish. Deterministic:
same output every run. Each photo is visually distinct (color + label) and
carries EXIF DateTimeOriginal matching its folder's date.

``--scale N`` instead writes cache/synthetic-library-scale/ — N small
solid-color photos spread over N/25 folders — for indexing-at-scale runs
against the oracle (bead fauxcasa-ok6). The default library is untouched.

``--benchmark N`` writes cache/benchmark-library/ — the M0 deliverable
(spec §9): N photos (reference target 100_000) with *defined composition*
so §7 numbers are reproducible: seeded-deterministic EXIF-date and
folder-shape distributions over a 25-year family-archive arc, two volume
roots (the §7 reference library spans two volumes), byte-identical
duplicates (~2%), missing-EXIF strays (~5%), mixed formats
(JPEG/PNG/GIF/BMP/TIFF/WebP), and a documented file-size distribution
scaled by ``--size-scale`` (default 0.02 keeps 100k photos ≈ 22 GB; 1.0
reproduces the documented multi-MB camera-JPEG distribution ≈ 500 GB and
is required for full-fidelity §7 *index-rate* gates — grid/scroll/cold
gates read only the thumbnail cache and are size-scale-independent;
see scripts/make-thumbcache.py).

``--picasa-extras`` (fauxcasa-ed5.2) writes cache/synthetic-library-extras/
— a Picasa-metadata-rich corpus for the M1 ingest-loss gate (spec §9,
exercised by scripts/check-ingest-parity.py): .picasa.ini sidecars with
faces=/[Contacts2] (conflicting and orphaned contact ids), a synthetic
contacts.xml, .pal album files (agreeing / divergent / orphaned vs the ini),
geotag= keys, unknown-album-UID references (the placeholder-album case),
plus the already-ingested classes (stars, captions, keywords, hidden,
rotate, album defs/memberships, folder descriptions, Hidden Folders).
The corpus ships a manifest.json of ground-truth expected counts.
The default / --scale / --benchmark outputs are byte-for-byte unchanged
by this profile (existing benchmark caches bind to library digests).
"""

import argparse
import random
from pathlib import Path

import piexif
from PIL import Image, ImageDraw

CACHE = Path(__file__).resolve().parent.parent / "cache"
ROOT = CACHE / "synthetic-library"
SCALE_ROOT = CACHE / "synthetic-library-scale"
BENCH_ROOT = CACHE / "benchmark-library"
EXTRAS_ROOT = CACHE / "synthetic-library-extras"

FOLDERS = [
    ("2009-07-04 Beach Day", (2009, 7, 4), 8),
    ("2010-12-25 Winter Holiday", (2010, 12, 25), 8),
    ("2015-03-15 Garden Project", (2015, 3, 15), 8),
]

SIZES = [(1600, 1200), (1200, 1600), (2048, 1536), (800, 600)]


def make_photo(path: Path, label: str, idx: int, date: tuple[int, int, int]) -> None:
    w, h = SIZES[idx % len(SIZES)]
    hue = (idx * 137) % 360  # golden-angle spacing keeps colors distinct
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(0, h, 4):  # coarse vertical gradient, blocked for speed
        from colorsys import hsv_to_rgb
        r, g, b = hsv_to_rgb(hue / 360, 0.6, 0.35 + 0.5 * y / h)
        row = (int(r * 255), int(g * 255), int(b * 255))
        for yy in range(y, min(y + 4, h)):
            for x in range(w):
                px[x, yy] = row
    draw = ImageDraw.Draw(img)
    text = f"{label}\n#{idx:02d}  {w}x{h}"
    draw.multiline_text((w // 10, h // 3), text, fill="white", font_size=w // 16)

    y_, m_, d_ = date
    stamp = f"{y_}:{m_:02d}:{d_:02d} {9 + idx}:00:00"
    exif = piexif.dump({
        "0th": {piexif.ImageIFD.Make: "Fauxcasa", piexif.ImageIFD.Model: "SyntheticCam"},
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: stamp,
            piexif.ExifIFD.DateTimeDigitized: stamp,
        },
    })
    img.save(path, "JPEG", quality=85, exif=exif)


SCALE_SIZES = [(640, 480), (480, 640), (800, 600), (320, 240)]


def make_photo_fast(path: Path, label: str, idx: int, date: tuple[int, int, int]) -> None:
    """Solid-color variant for scale runs: same EXIF scheme, ~10ms each."""
    from colorsys import hsv_to_rgb

    w, h = SCALE_SIZES[idx % len(SCALE_SIZES)]
    hue = (idx * 137) % 360
    r, g, b = hsv_to_rgb(hue / 360, 0.6, 0.6)
    img = Image.new("RGB", (w, h), (int(r * 255), int(g * 255), int(b * 255)))
    draw = ImageDraw.Draw(img)
    draw.multiline_text(
        (w // 10, h // 3), f"{label}\n#{idx:02d}", fill="white", font_size=w // 12
    )
    y_, m_, d_ = date
    stamp = f"{y_}:{m_:02d}:{d_:02d} {idx % 24:02d}:{idx % 60:02d}:00"
    exif = piexif.dump({
        "0th": {piexif.ImageIFD.Make: "Fauxcasa", piexif.ImageIFD.Model: "SyntheticCam"},
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: stamp,
            piexif.ExifIFD.DateTimeDigitized: stamp,
        },
    })
    img.save(path, "JPEG", quality=70, exif=exif)


def make_scale_library(n_photos: int) -> None:
    per_folder = 25
    n_folders = max(1, (n_photos + per_folder - 1) // per_folder)
    left = n_photos
    for fi in range(n_folders):
        date = (2000 + fi % 20, 1 + fi % 12, 1 + fi % 28)
        folder = f"{date[0]}-{date[1]:02d}-{date[2]:02d} Scale Batch {fi:03d}"
        d = SCALE_ROOT / folder
        d.mkdir(parents=True, exist_ok=True)
        count = min(per_folder, left)
        left -= count
        for i in range(count):
            f = d / f"photo{i:03d}.jpg"
            if not f.exists():
                make_photo_fast(f, folder, fi * per_folder + i, date)
    print(f"{n_photos} photos in {n_folders} folders at {SCALE_ROOT}")


# --------------------------------------------------------------------------
# --benchmark: the M0 100k library with defined composition (spec §9)
# --------------------------------------------------------------------------

BENCH_SEED = 20260612  # fixed: same composition every run, every machine

# Event-folder size distribution (family-archive shape): weights chosen so
# the mean is ~55 photos/folder — 100k photos land in ~1800 folders.
FOLDER_SHAPES = [
    # (min_photos, max_photos, weight)  — small outings dominate by count
    (4, 15, 45),     # quick outings, single-subject bursts
    (16, 60, 35),    # day trips, birthdays
    (61, 250, 17),   # vacations, holiday seasons
    (251, 900, 3),   # weddings, the mega-events
]

# Year weights 2000..2025: film-scan trickle, digital ramp, phone-era flood.
YEAR_WEIGHTS = [1, 1, 2, 2, 3, 4, 4, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10,
                10, 11, 11, 12, 12, 13, 13, 14]

EVENT_WORDS = (
    "Beach Lake Garden Picnic Birthday Holiday Camping Hike Visit Market "
    "Festival Parade Museum Zoo Park River Snow Spring Summer Autumn Winter "
    "Reunion Wedding Graduation Concert Fair Harvest Cabin Road Trip"
).split()

UNDATED_FOLDERS = ["Scans", "Misc", "Phone Dump", "Old Album Box", "Inbox"]

# File-size distribution at --size-scale 1.0 (documented camera-JPEG shape):
# log-normal, median ~3.0 MB, clamped to [0.4 MB, 14 MB]. Sizes are hit by
# padding valid JPEG COM segments — files stay well-formed images.
SIZE_LOGNORM_MU, SIZE_LOGNORM_SIGMA = 14.9, 0.55  # ln-bytes
SIZE_CLAMP = (400_000, 14_000_000)

BENCH_FORMATS = [  # (PIL format, extension, weight)
    ("JPEG", ".jpg", 90),
    ("PNG", ".png", 4),
    ("GIF", ".gif", 2),
    ("BMP", ".bmp", 1),
    ("TIFF", ".tif", 2),
    ("WEBP", ".webp", 1),
]

BENCH_DIMS = [(1600, 1200), (1200, 1600), (2048, 1536), (1024, 768),
              (1920, 1080), (800, 600)]

DUPLICATE_RATE = 0.02   # byte-identical copies dropped into other folders
NO_EXIF_RATE = 0.05     # strays with no EXIF dates (mtime is their truth)


def _bench_photo_bytes(idx: int, label: str, fmt: str, dims: tuple[int, int],
                       stamp: str | None, target_size: int | None) -> bytes:
    """Render one deterministic photo to bytes (solid hue + label text)."""
    import io
    from colorsys import hsv_to_rgb

    w, h = dims
    hue = (idx * 137) % 360
    r, g, b = hsv_to_rgb(hue / 360, 0.6, 0.6)
    img = Image.new("RGB", (w, h), (int(r * 255), int(g * 255), int(b * 255)))
    draw = ImageDraw.Draw(img)
    draw.multiline_text(
        (w // 12, h // 3), f"{label}\n#{idx:06d}", fill="white", font_size=w // 14
    )
    buf = io.BytesIO()
    if fmt == "JPEG" and stamp is not None:
        exif = piexif.dump({
            "0th": {piexif.ImageIFD.Make: "Fauxcasa",
                    piexif.ImageIFD.Model: "SyntheticCam"},
            "Exif": {piexif.ExifIFD.DateTimeOriginal: stamp,
                     piexif.ExifIFD.DateTimeDigitized: stamp},
        })
        img.save(buf, fmt, quality=70, exif=exif)
    else:
        img.save(buf, fmt)
    data = buf.getvalue()
    if fmt == "JPEG" and target_size is not None and target_size > len(data):
        # pad with COM segments (0xFFFE, <=65533 payload each) before EOI;
        # the file stays a valid JPEG at the documented byte size
        pad_total = target_size - len(data)
        segs = []
        while pad_total > 0:
            payload = min(pad_total, 65000)
            segs.append(b"\xff\xfe" + (payload + 2).to_bytes(2, "big")
                        + b"\x00" * payload)
            pad_total -= payload + 4
        data = data[:-2] + b"".join(segs) + data[-2:]
    return data


def _bench_plan(n_photos: int, size_scale: float) -> list[dict]:
    """The deterministic composition: a flat list of photo work orders."""
    rng = random.Random(BENCH_SEED)
    plan: list[dict] = []
    folder_year_seq: dict[int, int] = {}
    idx = 0
    while idx < n_photos:
        year = rng.choices(range(2000, 2026), weights=YEAR_WEIGHTS)[0]
        lo, hi, _w = rng.choices(FOLDER_SHAPES,
                                 weights=[s[2] for s in FOLDER_SHAPES])[0]
        count = min(rng.randint(lo, hi), n_photos - idx)
        month, day = rng.randint(1, 12), rng.randint(1, 28)
        volume = "volA" if rng.random() < 0.7 else "volB"  # two-volume split
        seq = folder_year_seq.get(year, 0)
        folder_year_seq[year] = seq + 1
        if rng.random() < 0.04:  # undated shoebox folders, no event date
            name = f"{rng.choice(UNDATED_FOLDERS)} {year}-{seq:02d}"
            dated = False
        else:
            event = " ".join(rng.sample(EVENT_WORDS, rng.randint(1, 2)))
            name = f"{year}-{month:02d}-{day:02d} {event} {seq:02d}"
            dated = True
        # ~20% of folders nest under a year directory (mixed folder shapes)
        rel = (f"{volume}/{year}/{name}" if rng.random() < 0.2
               else f"{volume}/{name}")
        for i in range(count):
            fmt, ext, _w2 = rng.choices(BENCH_FORMATS,
                                        weights=[f[2] for f in BENCH_FORMATS])[0]
            dims = rng.choice(BENCH_DIMS)
            if dated and rng.random() > NO_EXIF_RATE and fmt == "JPEG":
                stamp = (f"{year}:{month:02d}:{day:02d} "
                         f"{rng.randint(8, 21):02d}:{rng.randint(0, 59):02d}:00")
            else:
                stamp = None
            target = None
            if fmt == "JPEG" and size_scale > 0:
                raw = rng.lognormvariate(SIZE_LOGNORM_MU, SIZE_LOGNORM_SIGMA)
                target = int(max(SIZE_CLAMP[0], min(SIZE_CLAMP[1], raw))
                             * size_scale)
            plan.append({"idx": idx, "rel": f"{rel}/img{i:04d}{ext}",
                         "label": name, "fmt": fmt, "dims": dims,
                         "stamp": stamp, "target": target})
            idx += 1
    # byte-identical duplicates: replace the tail ~2% of work orders with
    # copies of earlier photos, landed in *different* folders
    n_dupes = int(len(plan) * DUPLICATE_RATE)
    for k in range(n_dupes):
        victim = plan[-(k + 1)]
        src = plan[rng.randrange(0, len(plan) - n_dupes)]
        stem = (Path(victim["rel"]).parent
                / f"copy{k:04d}-of-{Path(src['rel']).name}")
        plan[-(k + 1)] = {**src, "rel": str(stem), "dupe_of": src["rel"]}
    return plan


def _bench_write_one(args: tuple) -> int:
    plan_item, root = args
    out = root / plan_item["rel"]
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        return 0
    data = _bench_photo_bytes(
        plan_item["idx"], plan_item["label"], plan_item["fmt"],
        tuple(plan_item["dims"]), plan_item["stamp"], plan_item["target"],
    )
    out.write_bytes(data)
    return len(data)


def make_benchmark_library(n_photos: int, size_scale: float, jobs: int) -> None:
    import json
    from concurrent.futures import ProcessPoolExecutor

    plan = _bench_plan(n_photos, size_scale)
    # dupes must be written after their sources; sources first, then dupes
    sources = [p for p in plan if "dupe_of" not in p]
    dupes = [p for p in plan if "dupe_of" in p]
    written = 0
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for n in pool.map(_bench_write_one,
                          ((p, BENCH_ROOT) for p in sources), chunksize=64):
            written += n
    for p in dupes:
        out = BENCH_ROOT / p["rel"]
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists():
            out.write_bytes((BENCH_ROOT / p["dupe_of"]).read_bytes())
    manifest = {
        "photos": len(plan), "seed": BENCH_SEED, "size_scale": size_scale,
        "duplicates": len(dupes),
        "folders": len({str(Path(p["rel"]).parent) for p in plan}),
    }
    (BENCH_ROOT / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"{len(plan)} photos ({len(dupes)} dupes) in {manifest['folders']} "
          f"folders at {BENCH_ROOT} (size_scale={size_scale})")


# --------------------------------------------------------------------------
# --picasa-extras: Picasa-metadata-rich corpus for the M1 ingest-loss gate
# (fauxcasa-ed5.2; consumed by scripts/check-ingest-parity.py)
# --------------------------------------------------------------------------
#
# Layout mirrors the oracle fixtures (fixtures/oracle/*/): the corpus root
# holds `library/` (photos + .picasa.ini sidecars), `contacts/contacts.xml`
# (Picasa keeps it outside the library, under %LocalAppData%), and
# `albums/<uid>.pal` (the Picasa2Albums directory), plus `manifest.json`
# with the generator's ground-truth expected counts per ingest class.
#
# All metadata grammar follows docs/research/picasa-ini-format.md: CRLF
# ini lines, `star=yes` presence-only, rect64 hex with leading zeros
# stripped, contact ids %llx-printed (one deliberately 15 chars to
# exercise zero-padding), [.album:<32hex>] with ISO-8601 date, and the
# .pal XML shape from docs/research/sources/forensicir-2007-picasa-analysis.md.

# Contact ids (16 hex). The mix exercises the §4 precedence merge:
CONTACT_ALICE = "aaaa000000000001"   # [Contacts2] + contacts.xml, same name
CONTACT_BOB = "bbbb000000000002"     # names CONFLICT (contacts.xml wins)
CONTACT_CAROL = "cccc000000000003"   # [Contacts2] only
CONTACT_ORPHAN = "dddd000000000004"  # faces= only: orphaned id, no name anywhere
CONTACT_EVE = "eeee000000000005"     # contacts.xml only
CONTACT_PAD = "0f0e000000000006"     # stored padded; faces= writes it stripped
CONTACT_UNKNOWN = "ffffffffffffffff"  # unconfirmed name suggestion

# Album uids (32 hex). The mix exercises ini/.pal agreement and drift:
ALBUM_AGREE = "a1b2c3d4e5f60718293a4b5c6d7e8f90"    # ini def + agreeing .pal
ALBUM_DIVERGE = "b2c3d4e5f60718293a4b5c6d7e8f90a1"  # ini def + DIVERGENT .pal
ALBUM_PAL_ONLY = "c3d4e5f60718293a4b5c6d7e8f90a1b2"  # .pal only (orphaned .pal)
ALBUM_UNKNOWN = "d4e5f60718293a4b5c6d7e8f90a1b2c3"  # albums= refs, no def anywhere

EXTRAS_DBID = "0164eaeacdd4046f5c1e44522fe44527"  # synthetic, forensicir shape

# Per-folder plan: (folder name, (y, m, d), photo count, ini lines or None).
# Ini lines are joined with CRLF; None means "no sidecar in this folder".
_EXTRAS_FOLDERS: list[tuple[str, tuple[int, int, int], int, list[str] | None]] = [
    (
        "2009-07-04 Beach Day",
        (2009, 7, 4),
        6,
        [
            "[Picasa]",
            "name=2009-07-04 Beach Day",
            "description=Family beach trip",
            "[encoding]",
            "utf8=1",
            "[Contacts2]",
            f"{CONTACT_ALICE}=Alice Example;;",
            f"{CONTACT_BOB}=Bob Ini;;",
            f"{CONTACT_CAROL}=Carol IniOnly;;",
            f"{CONTACT_PAD}=Zoe Padded;;",
            f"[.album:{ALBUM_AGREE}]",
            "name=Best Of",
            f"token=]album:{ALBUM_AGREE}",
            "date=2020-06-15T10:00:00-07:00",
            "[photo00.jpg]",
            "star=yes",
            f"faces=rect64(3f845bcb59418507),{CONTACT_ALICE}",
            f"albums={ALBUM_AGREE}",
            "[photo01.jpg]",
            "caption=Sunset over the bay",
            f"faces=rect64(4a8e8e6b),{CONTACT_BOB};"
            f"rect64(1111222233334444),{CONTACT_UNKNOWN}",
            f"albums={ALBUM_AGREE}",
            "[photo02.jpg]",
            "star=yes",
            f"faces=rect64(2222333344445555),{CONTACT_CAROL}",
            "[photo03.jpg]",
            "keywords=beach, family",
            f"faces=rect64(3333444455556666),{CONTACT_ORPHAN};"
            f"rect64(823a48e19c2f5d01),{CONTACT_EVE}",
            "[photo04.jpg]",
            "geotag=-33.856800,151.215300",
            # rect hex AND contact id both leading-zero-stripped (%llx form)
            f"faces=rect64(51c39e4b7a2d80),{CONTACT_PAD.lstrip('0')}",
            "[photo05.jpg]",
            "rotate=rotate(3)",
        ],
    ),
    (
        "2010-12-25 Winter Holiday",
        (2010, 12, 25),
        5,
        [
            # deliberately NO [Picasa] section (order/presence is arbitrary
            # in the wild) and one dynamic backup key (robustness).
            "[encoding]",
            "utf8=1",
            "[photo00.jpg]",
            "star=yes",
            "geotag=48.858844,2.294351",
            f"albums={ALBUM_AGREE}",
            "[photo01.jpg]",
            "keywords=holiday,snow , lights",
            f"albums={ALBUM_UNKNOWN}",
            "[photo02.jpg]",
            "caption=First snow",
            "rotate=rotate(1)",
            "[photo03.jpg]",
            "BKTag Family Set-backuphash=29250",
            "[photo04.jpg]",
            "hidden=yes",
        ],
    ),
    (
        "2015-03-15 Garden Project",
        (2015, 3, 15),
        5,
        [
            "[Picasa]",
            "name=2015-03-15 Garden Project",
            "description=Garden build log",
            "[encoding]",
            "utf8=1",
            f"[.album:{ALBUM_DIVERGE}]",
            "name=Garden Picks",
            f"token=]album:{ALBUM_DIVERGE}",
            "date=2021-04-02T08:30:00-07:00",
            "[photo00.jpg]",
            "star=yes",
            "caption=Seedlings",
            "filters=tilt=1,0.280632,0.000000;",
            "[photo01.jpg]",
            "star=yes",
            f"albums={ALBUM_DIVERGE}",
            "[photo02.jpg]",
            "keywords=garden",
            f"albums={ALBUM_DIVERGE}",
            "[photo03.jpg]",
            "geotag=64.145200,-21.942600",
            f"albums={ALBUM_UNKNOWN}",
            "[photo04.jpg]",
            "hidden=yes",
            "caption=",  # empty caption: not "captioned" on either side
        ],
    ),
    (
        "Private Scans",
        (2003, 5, 1),
        2,
        [
            "[Picasa]",
            "P2category=Hidden Folders",
            "[encoding]",
            "utf8=1",
        ],
    ),
    # ini-less folder: parity must hold where no sidecar exists at all
    ("Unsorted Box", (2018, 9, 9), 2, None),
]

_EXTRAS_CONTACTS_XML = (
    "<contacts>\n"
    f' <contact id="{CONTACT_ALICE}" name="Alice Example"'
    ' modified_time="2026-06-11T20:40:16-07:00" local_contact="1"/>\n'
    f' <contact id="{CONTACT_BOB}" name="Robert Xml"'
    ' modified_time="2026-06-11T20:41:02-07:00" local_contact="1"/>\n'
    f' <contact id="{CONTACT_EVE}" name="Eve XmlOnly"'
    ' modified_time="2026-06-11T20:42:37-07:00" local_contact="1"/>\n'
    "</contacts>\n"
)

# .pal member lists as library-relative POSIX paths; written into the file
# in Picasa's "[C]\..." volume-token form.
_EXTRAS_PALS: dict[str, tuple[str, list[str]]] = {
    # agrees with the ini: same members the albums= tokens produce
    ALBUM_AGREE: (
        "Best Of",
        [
            "2009-07-04 Beach Day/photo00.jpg",
            "2009-07-04 Beach Day/photo01.jpg",
            "2010-12-25 Winter Holiday/photo00.jpg",
        ],
    ),
    # DIVERGES from the ini (ini: Garden photo01+photo02): one member
    # missing, one extra from another folder
    ALBUM_DIVERGE: (
        "Garden Picks",
        [
            "2015-03-15 Garden Project/photo01.jpg",
            "2010-12-25 Winter Holiday/photo03.jpg",
        ],
    ),
    # orphaned .pal: no ini definition, no albums= references anywhere
    ALBUM_PAL_ONLY: (
        "Pal Orphan",
        [
            "2018-09-09 nonexistent/photo99.jpg",
            "2009-07-04 Beach Day/photo02.jpg",
        ],
    ),
}


def _pal_xml(uid: str, name: str, members: list[str]) -> str:
    """One .pal file, shaped per the forensicir 2007 writeup: properties,
    then a <files> block nested inside the trailing name property."""
    files = "\n".join(
        f" <filename>[C]\\{m.replace('/', chr(92))}</filename>" for m in members
    )
    return (
        "<picasa2album>\n"
        f"<dbid>{EXTRAS_DBID}</dbid>\n"
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


# Ground truth the generator promises (checked three ways by
# check-ingest-parity.py: manifest == survey rollup == tracer catalog for
# ingested classes). Keep in sync with _EXTRAS_FOLDERS by construction —
# the counts below are derived by hand from the plan above and asserted
# against the survey reader in the harness, so drift fails loudly.
_EXTRAS_EXPECTED = {
    "photos": 20,
    "folders": 5,
    "ini_files": 4,
    "starred": 5,
    "captioned": 3,          # the empty caption= line does not count
    "keyworded": 3,
    "hidden_photos": 2,      # per-photo hidden=yes
    "hidden_folders": 1,     # [Picasa] P2category=Hidden Folders
    "rotated": 2,
    "album_definitions": 2,  # [.album:] sections (AGREE, DIVERGE)
    "album_memberships": 7,  # albums= tokens incl. the unknown-uid refs
    "folder_descriptions": 2,
    "face_tags": 7,
    # named after the §4 merge ([Contacts2] ∪ contacts.xml): unnamed are the
    # ffffffffffffffff suggestion AND the CONTACT_ORPHAN (dddd…) whose id is
    # defined nowhere — the original value 6 miscounted its own orphan case
    "face_tags_named": 5,
    "contacts_ini": 4,       # [Contacts2] entries
    "contacts_xml": 3,
    "contacts_registry": 5,  # merged registry: 4 ini ∪ {CONTACT_EVE}
    "geotagged": 3,
    "pal_albums": 3,
    "placeholder_album_uids": 1,   # distinct unknown-UID albums (ALBUM_UNKNOWN)
    "placeholder_album_refs": 2,   # albums= tokens naming them
    "edit_keys_other": 1,    # filters=/crop besides the ingested rotate=
}


def make_extras_library(root: Path | None = None) -> Path:
    """Write the picasa-extras corpus; returns the corpus root.

    Deterministic and self-contained: photos via make_photo_fast (same
    EXIF scheme as --scale), metadata from the fixed plan above. Existing
    photo files are kept (same skip-if-exists behavior as other profiles);
    sidecars/manifest are always rewritten so plan edits propagate.
    """
    import json

    root = EXTRAS_ROOT if root is None else Path(root)
    library = root / "library"
    idx = 0
    for folder, date, count, ini_lines in _EXTRAS_FOLDERS:
        d = library / folder
        d.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            f = d / f"photo{i:02d}.jpg"
            if not f.exists():
                make_photo_fast(f, folder, idx, date)
            idx += 1
        if ini_lines is not None:
            # CRLF + trailing newline, UTF-8: what Picasa 3.9 writes
            (d / ".picasa.ini").write_bytes(
                ("\r\n".join(ini_lines) + "\r\n").encode("utf-8")
            )
    contacts = root / "contacts"
    contacts.mkdir(parents=True, exist_ok=True)
    (contacts / "contacts.xml").write_text(_EXTRAS_CONTACTS_XML, encoding="utf-8")
    albums = root / "albums"
    albums.mkdir(parents=True, exist_ok=True)
    for uid, (name, members) in _EXTRAS_PALS.items():
        (albums / f"{uid}.pal").write_text(
            _pal_xml(uid, name, members), encoding="utf-8"
        )
    manifest = {
        "profile": "picasa-extras",
        "expected": _EXTRAS_EXPECTED,
        "uids": {
            "ini_defined": [ALBUM_AGREE, ALBUM_DIVERGE],
            "pal_only": ALBUM_PAL_ONLY,
            "unknown": ALBUM_UNKNOWN,
            "pal_files": sorted(_EXTRAS_PALS),
        },
        "contacts_xml_ids": [CONTACT_ALICE, CONTACT_BOB, CONTACT_EVE],
        "contacts_ini_ids": [CONTACT_ALICE, CONTACT_BOB, CONTACT_CAROL,
                             CONTACT_PAD],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=1), encoding="utf-8"
    )
    print(
        f"{_EXTRAS_EXPECTED['photos']} photos in {_EXTRAS_EXPECTED['folders']} "
        f"folders, {_EXTRAS_EXPECTED['ini_files']} inis, "
        f"{_EXTRAS_EXPECTED['pal_albums']} .pal, contacts.xml at {root}"
    )
    return root


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--scale",
        type=int,
        default=None,
        metavar="N",
        help=f"write N fast photos to {SCALE_ROOT.name}/ instead",
    )
    ap.add_argument(
        "--benchmark",
        type=int,
        default=None,
        metavar="N",
        help=f"write the N-photo M0 benchmark library to {BENCH_ROOT.name}/",
    )
    ap.add_argument(
        "--size-scale",
        type=float,
        default=0.02,
        help="benchmark file-size scale (1.0 = documented camera-JPEG "
        "distribution, ~500 GB at 100k; default 0.02 ≈ 22 GB)",
    )
    ap.add_argument(
        "--jobs", type=int, default=None,
        help="parallel writers for --benchmark (default: CPU count)",
    )
    ap.add_argument(
        "--picasa-extras",
        nargs="?",
        const=str(EXTRAS_ROOT),
        default=None,
        metavar="DIR",
        help="write the Picasa-metadata-rich M1 ingest-gate corpus "
        f"(default DIR: {EXTRAS_ROOT.name}/)",
    )
    args = ap.parse_args()
    if args.picasa_extras is not None:
        make_extras_library(Path(args.picasa_extras))
        return
    if args.scale is not None:
        if args.scale < 1:
            ap.error("--scale must be >= 1")
        make_scale_library(args.scale)
        return
    if args.benchmark is not None:
        if args.benchmark < 1:
            ap.error("--benchmark must be >= 1")
        import os
        make_benchmark_library(
            args.benchmark, args.size_scale, args.jobs or os.cpu_count() or 4
        )
        return
    for folder, date, count in FOLDERS:
        d = ROOT / folder
        d.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            f = d / f"photo{i:02d}.jpg"
            if not f.exists():
                make_photo(f, folder, i, date)
        print(f"{folder}: {count} photos")
    print(f"library at {ROOT}")


if __name__ == "__main__":
    main()
