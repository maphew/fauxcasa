#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Hands-on spike for fauxcasa-cam.16: metadata-library candidates, raced.

DECISION under test: wrap a mature metadata library (spec section 5 P1,
"wrap, don't hand-roll") vs extend the hand-rolled apps/desktop-python/
inmeta.py to the full M1 surface (EXIF IFD dates, GPS rationals, XMP
Rating, mwg-rs face regions, ExtendedXMP, non-JPEG carriers).

What this script does, in plain language: it builds a small corpus of
test image files whose metadata content is known exactly (written with
ExifTool, an independent reference writer, onto synthetic pixels), then
asks each candidate library to read that metadata back, and times each
one over ~100 files. Every candidate runs in its own freshly-resolved
environment (`uv run --with <candidate>`) so the numbers include the
real install/import experience on this machine.

Corpus (built into a temp dir; nothing committed):
  jpeg_picasa.jpg   copy of a committed oracle fixture: real Picasa 3.9
                    wrote its XMP caption/keywords + IPTC mirror
                    (fixtures/oracle/013-face-tag-manual/after).
  jpeg_full.jpg     EXIF DateTimeOriginal + GPS rationals, XMP Rating,
                    dc:description/subject + IPTC mirror, and an
                    mwg-rs RegionInfo with two named face regions.
  jpeg_extxmp.jpg   same, plus a >64 KB XMP payload so the packet must
                    be split into ExtendedXMP APP1 segments (the
                    Google-style reassembly case).
  carrier.tiff/.png/.webp
                    the non-JPEG carriers M1 must read: XMP in TIFF tag
                    0x02BC, PNG iTXt, WebP XMP chunk (+ native/eXIf/
                    EXIF-chunk EXIF where the container has one).
  timing/*.jpg      100 copies of jpeg_full.jpg for per-file read cost.

Candidates:
  pyexiv2     PyPI `pyexiv2` (LeoHsiao1) — exiv2 C++ inside the process.
  exiv2       PyPI `exiv2` (python-exiv2, Jim Easterbrook) — the other,
              lower-level exiv2 binding.
  exiftool    PyPI `PyExifTool` driving the system ExifTool binary in
              -stay_open batch mode (plus one-process-per-file timing).
  pillow      PyPI `Pillow` (+defusedxml for getxmp()) — the "already a
              dependency" baseline.
  xmptoolkit  PyPI `python-xmp-toolkit` — expected to fail on Windows
              (needs the Exempi C library); the failure is the result.
  inmeta      the current hand-rolled reader (caption/keywords only) —
              the do-nothing baseline for timing and scope comparison.

Usage (from the repo root, or anywhere):
  uv run docs/research/spikes/metadata-lib-spike.py run
  uv run docs/research/spikes/metadata-lib-spike.py run --only pyexiv2,pillow
  # plumbing (normally invoked by `run` in per-candidate environments):
  uv run --no-project --with Pillow python .../metadata-lib-spike.py make-fixtures DIR
  uv run --no-project --with pyexiv2 python .../metadata-lib-spike.py read-pyexiv2 DIR

Requires: uv; the ExifTool binary on PATH (fixture writing + the
exiftool candidate). Results print as JSON per candidate plus a summary
table; raw results also land in <corpus>/results/*.json.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ORACLE_JPEG = (REPO_ROOT / "fixtures" / "oracle" / "013-face-tag-manual"
               / "after" / "library" / "2009-07-04 Beach Day" / "photo04.jpg")
CORPUS_FILES = ["jpeg_picasa.jpg", "jpeg_full.jpg", "jpeg_extxmp.jpg",
                "carrier.tiff", "carrier.png", "carrier.webp"]
TIMING_N = 100

# Ground truth written into jpeg_full.jpg (and the carriers, where the
# container supports the field). GPS is Whitehorse YT, deliberately W
# longitude so the sign/ref handling is exercised.
TRUTH = {
    "dto": "2009:07:04 13:00:00",
    "lat": 60.72125, "lon": -135.05685,
    "rating": 3,
    "caption": "Family at the beach",
    "keywords": ["beach", "family"],
    "faces": [("Alice Example", 0.40, 0.35, 0.20, 0.30),
              ("Bob Example", 0.70, 0.60, 0.15, 0.25)],
}

REGION_STRUCT = (
    "{AppliedToDimensions={W=1600,H=1200,Unit=pixel},"
    "RegionList=["
    "{Area={X=0.40,Y=0.35,W=0.20,H=0.30,Unit=normalized},"
    "Name=Alice Example,Type=Face},"
    "{Area={X=0.70,Y=0.60,W=0.15,H=0.25,Unit=normalized},"
    "Name=Bob Example,Type=Face}]}"
)


# --------------------------------------------------------------------------
# fixture building (runs under: uv run --with Pillow ... make-fixtures DIR)
# --------------------------------------------------------------------------

def make_fixtures(corpus: Path) -> None:
    from PIL import Image  # noqa: PLC0415 (candidate env import)

    exiftool = shutil.which("exiftool")
    if not exiftool:
        sys.exit("ExifTool not on PATH; fixture writing needs it.")
    corpus.mkdir(parents=True, exist_ok=True)

    # Synthetic pixels: a gradient, no real photo content.
    img = Image.new("RGB", (1600, 1200))
    px = img.load()
    for y in range(0, 1200, 4):
        for x in range(0, 1600, 4):
            c = (x * 255 // 1600, y * 255 // 1200, (x + y) % 256)
            for dy in range(4):
                for dx in range(4):
                    px[x + dx, y + dy] = c
    base_jpg = corpus / "base.jpg"
    img.save(base_jpg, quality=85)
    img.save(corpus / "carrier.tiff")
    img.save(corpus / "carrier.png")
    img.save(corpus / "carrier.webp", quality=85)

    shutil.copy2(ORACLE_JPEG, corpus / "jpeg_picasa.jpg")

    common = [
        f"-EXIF:DateTimeOriginal={TRUTH['dto']}",
        f"-GPSLatitude={TRUTH['lat']}", "-GPSLatitudeRef=N",
        f"-GPSLongitude={abs(TRUTH['lon'])}", "-GPSLongitudeRef=W",
        f"-XMP-xmp:Rating={TRUTH['rating']}",
        f"-XMP-dc:Description={TRUTH['caption']}",
        *[f"-XMP-dc:Subject={k}" for k in TRUTH["keywords"]],
        f"-XMP-mwg-rs:RegionInfo={REGION_STRUCT}",
    ]
    iptc = [f"-IPTC:Caption-Abstract={TRUTH['caption']}",
            *[f"-IPTC:Keywords={k}" for k in TRUTH["keywords"]]]

    shutil.copy2(base_jpg, corpus / "jpeg_full.jpg")
    run_exiftool(exiftool, [*common, *iptc], corpus / "jpeg_full.jpg")

    # ExtendedXMP: balloon the packet past the 64 KB APP1 limit with a
    # long xmp:Label; ExifTool then writes HasExtendedXMP + the extended
    # APP1 segments per Adobe/Google convention. Passed via an argfile
    # to dodge the Windows command-line length limit.
    shutil.copy2(base_jpg, corpus / "jpeg_extxmp.jpg")
    big = "X" * 70000
    argfile = corpus / "extxmp.args"
    argfile.write_text(
        "\n".join([*common, f"-XMP-xmp:Label={big}", "-overwrite_original",
                   str(corpus / "jpeg_extxmp.jpg")]),
        encoding="utf-8")
    subprocess.run([exiftool, "-@", str(argfile)], check=True,
                   capture_output=True)

    for name in ("carrier.tiff", "carrier.png", "carrier.webp"):
        run_exiftool(exiftool, common, corpus / name)

    tdir = corpus / "timing"
    tdir.mkdir(exist_ok=True)
    for i in range(TIMING_N):
        shutil.copy2(corpus / "jpeg_full.jpg", tdir / f"t{i:03}.jpg")
    print(f"fixtures built in {corpus}")


def run_exiftool(exe: str, args: list[str], target: Path) -> None:
    r = subprocess.run([exe, *args, "-overwrite_original", str(target)],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"exiftool write failed for {target.name}:\n{r.stderr}")


# --------------------------------------------------------------------------
# shared result shape + helpers
# --------------------------------------------------------------------------

def result_skeleton(candidate: str) -> dict:
    return {"candidate": candidate, "import_ms": None, "versions": {},
            "files": {}, "timing": {}, "notes": []}


def empty_fields() -> dict:
    # xlabel_len: length of the 70000-char xmp:Label planted in
    # jpeg_extxmp.jpg — nonzero only if the reader reassembled the
    # ExtendedXMP packet (the field is too big for the main packet).
    return {"dto": None, "gps": None, "rating": None, "faces": None,
            "caption": None, "keywords": None, "xlabel_len": None,
            "error": None}


def rational_dms(text: str, ref: str | None) -> float | None:
    """'60/1 43/1 1650/100' (+ ref N/S/E/W) -> signed decimal degrees."""
    try:
        parts = []
        for tok in text.split():
            num, _, den = tok.partition("/")
            parts.append(float(num) / float(den or 1))
        while len(parts) < 3:
            parts.append(0.0)
        deg = parts[0] + parts[1] / 60 + parts[2] / 3600
        if ref and ref.strip().upper()[:1] in ("S", "W"):
            deg = -deg
        return round(deg, 5)
    except (ValueError, ZeroDivisionError):
        return None


def face_str(name: str, x, y, w, h) -> str:
    try:
        return f"{name}@({float(x):g},{float(y):g},{float(w):g}x{float(h):g})"
    except (TypeError, ValueError):
        return f"{name}@(?)"


def cap_str(text: str | None) -> str | None:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= 60 else f"{text[:20]}...(len={len(text)})"


def timed_loop(files: list[Path], fn) -> dict:
    t0 = time.perf_counter()
    ok = sum(1 for f in files if fn(f))
    dt = time.perf_counter() - t0
    return {"files": len(files), "ok": ok, "total_s": round(dt, 3),
            "per_file_ms": round(dt / len(files) * 1000, 2)}


# --------------------------------------------------------------------------
# candidate: pyexiv2 (LeoHsiao1)
# --------------------------------------------------------------------------

def read_pyexiv2(corpus: Path) -> dict:
    res = result_skeleton("pyexiv2")
    t0 = time.perf_counter()
    import pyexiv2  # noqa: PLC0415
    res["import_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    res["versions"] = {"pyexiv2": pyexiv2.__version__,
                       "exiv2": pyexiv2.__exiv2_version__}

    def extract(path: Path) -> dict:
        out = empty_fields()
        img = pyexiv2.Image(str(path))
        try:
            exif, xmp = img.read_exif(), img.read_xmp()
        finally:
            img.close()
        out["dto"] = exif.get("Exif.Photo.DateTimeOriginal")
        lat = exif.get("Exif.GPSInfo.GPSLatitude")
        lon = exif.get("Exif.GPSInfo.GPSLongitude")
        if lat and lon:
            out["gps"] = (rational_dms(lat, exif.get("Exif.GPSInfo.GPSLatitudeRef")),
                          rational_dms(lon, exif.get("Exif.GPSInfo.GPSLongitudeRef")))
        out["rating"] = xmp.get("Xmp.xmp.Rating")
        label = xmp.get("Xmp.xmp.Label")
        out["xlabel_len"] = len(label) if label else None
        desc = xmp.get("Xmp.dc.description")
        if isinstance(desc, dict):  # {'lang="x-default"': 'text'}
            desc = next(iter(desc.values()), None)
        out["caption"] = cap_str(desc)
        subj = xmp.get("Xmp.dc.subject")
        out["keywords"] = subj if isinstance(subj, list) else None
        faces = []
        i = 1
        prefix = "Xmp.mwg-rs.Regions/mwg-rs:RegionList"
        while f"{prefix}[{i}]/mwg-rs:Name" in xmp:
            area = f"{prefix}[{i}]/mwg-rs:Area/stArea:"
            faces.append(face_str(xmp[f"{prefix}[{i}]/mwg-rs:Name"],
                                  xmp.get(area + "x"), xmp.get(area + "y"),
                                  xmp.get(area + "w"), xmp.get(area + "h")))
            i += 1
        out["faces"] = faces or None
        return out

    collect(res, corpus, extract)
    res["timing"]["in_process"] = timed_loop(
        timing_files(corpus), lambda f: extract(f)["dto"])
    return res


# --------------------------------------------------------------------------
# candidate: python-exiv2 (PyPI `exiv2`, Jim Easterbrook)
# --------------------------------------------------------------------------

def read_exiv2(corpus: Path) -> dict:
    res = result_skeleton("exiv2")
    t0 = time.perf_counter()
    import exiv2  # noqa: PLC0415
    res["import_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    res["versions"] = {"python-exiv2": exiv2.__version__,
                       "exiv2": exiv2.version()}

    def extract(path: Path) -> dict:
        out = empty_fields()
        img = exiv2.ImageFactory.open(str(path))
        img.readMetadata()
        exif = {d.key(): d.toString() for d in img.exifData()}
        xmp = {d.key(): d.toString() for d in img.xmpData()}
        out["dto"] = exif.get("Exif.Photo.DateTimeOriginal")
        lat = exif.get("Exif.GPSInfo.GPSLatitude")
        lon = exif.get("Exif.GPSInfo.GPSLongitude")
        if lat and lon:
            out["gps"] = (rational_dms(lat, exif.get("Exif.GPSInfo.GPSLatitudeRef")),
                          rational_dms(lon, exif.get("Exif.GPSInfo.GPSLongitudeRef")))
        out["rating"] = xmp.get("Xmp.xmp.Rating")
        label = xmp.get("Xmp.xmp.Label")
        out["xlabel_len"] = len(label) if label else None
        desc = xmp.get("Xmp.dc.description")
        if desc and desc.startswith('lang='):  # 'lang="x-default" text'
            desc = desc.split(" ", 1)[-1]
        out["caption"] = cap_str(desc)
        subj = xmp.get("Xmp.dc.subject")
        out["keywords"] = [s.strip() for s in subj.split(",")] if subj else None
        faces = []
        i = 1
        prefix = "Xmp.mwg-rs.Regions/mwg-rs:RegionList"
        while f"{prefix}[{i}]/mwg-rs:Name" in xmp:
            area = f"{prefix}[{i}]/mwg-rs:Area/stArea:"
            faces.append(face_str(xmp[f"{prefix}[{i}]/mwg-rs:Name"],
                                  xmp.get(area + "x"), xmp.get(area + "y"),
                                  xmp.get(area + "w"), xmp.get(area + "h")))
            i += 1
        out["faces"] = faces or None
        return out

    collect(res, corpus, extract)
    res["timing"]["in_process"] = timed_loop(
        timing_files(corpus), lambda f: extract(f)["dto"])
    return res


# --------------------------------------------------------------------------
# candidate: PyExifTool driving the ExifTool binary
# --------------------------------------------------------------------------

def read_exiftool(corpus: Path) -> dict:
    res = result_skeleton("exiftool")
    t0 = time.perf_counter()
    import exiftool  # noqa: PLC0415
    res["import_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    def to_fields(md: dict) -> dict:
        out = empty_fields()
        out["dto"] = md.get("ExifIFD:DateTimeOriginal") or md.get(
            "IFD0:DateTimeOriginal") or md.get("XMP-exif:DateTimeOriginal")
        lat, lon = md.get("Composite:GPSLatitude"), md.get("Composite:GPSLongitude")
        if lat is not None and lon is not None:
            out["gps"] = (round(float(lat), 5), round(float(lon), 5))
        out["rating"] = md.get("XMP-xmp:Rating")
        label = md.get("XMP-xmp:Label")
        out["xlabel_len"] = len(label) if label else None
        out["caption"] = cap_str(md.get("XMP-dc:Description")
                                 or md.get("IPTC:Caption-Abstract"))
        kw = md.get("XMP-dc:Subject") or md.get("IPTC:Keywords")
        out["keywords"] = kw if isinstance(kw, list) else (
            [kw] if kw is not None else None)
        ri = md.get("XMP-mwg-rs:RegionInfo")
        if isinstance(ri, dict):
            faces = []
            for r in ri.get("RegionList", []):
                a = r.get("Area", {})
                faces.append(face_str(r.get("Name"), a.get("X"), a.get("Y"),
                                      a.get("W"), a.get("H")))
            out["faces"] = faces or None
        return out

    args = ["-G1", "-n", "-struct"]
    with exiftool.ExifToolHelper(common_args=args) as et:
        res["versions"] = {"PyExifTool": exiftool.__version__,
                           "exiftool": et.version}
        for name in CORPUS_FILES:
            path = corpus / name
            try:
                res["files"][name] = to_fields(et.get_metadata(str(path))[0])
            except Exception as e:  # noqa: BLE001 (spike: record, don't die)
                res["files"][name] = {**empty_fields(), "error": repr(e)}

        tfiles = timing_files(corpus)
        # stay_open, one round trip per file (the realistic ingest shape)
        res["timing"]["stay_open_per_call"] = timed_loop(
            tfiles, lambda f: et.get_metadata(str(f))[0])
        # stay_open, all 100 files in one command
        t0 = time.perf_counter()
        n = len(et.get_metadata([str(f) for f in tfiles]))
        dt = time.perf_counter() - t0
        res["timing"]["stay_open_one_batch"] = {
            "files": n, "total_s": round(dt, 3),
            "per_file_ms": round(dt / n * 1000, 2)}

    # one process per file: the naive shape (10 files, it is slow)
    exe = shutil.which("exiftool")
    sub = timing_files(corpus)[:10]
    res["timing"]["process_per_file_10"] = timed_loop(
        sub, lambda f: subprocess.run([exe, "-j", "-n", str(f)],
                                      capture_output=True).returncode == 0)
    return res


# --------------------------------------------------------------------------
# candidate: Pillow (+defusedxml for getxmp)
# --------------------------------------------------------------------------

def _walk(node, key):  # depth-first search of getxmp()'s dict-of-dicts
    if isinstance(node, dict):
        if key in node:
            yield node[key]
        for v in node.values():
            yield from _walk(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)


def read_pillow(corpus: Path) -> dict:
    res = result_skeleton("pillow")
    t0 = time.perf_counter()
    from PIL import ExifTags, Image  # noqa: PLC0415
    import PIL  # noqa: PLC0415
    res["import_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    res["versions"] = {"Pillow": PIL.__version__}

    def extract(path: Path) -> dict:
        out = empty_fields()
        with Image.open(path) as im:
            exif = im.getexif()
            exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
            gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
            try:
                xmp = im.getxmp()
            except Exception:  # noqa: BLE001
                xmp = {}
        out["dto"] = exif_ifd.get(ExifTags.Base.DateTimeOriginal)
        if gps.get(2) and gps.get(4):
            def dms(v, ref):
                deg = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
                return round(-deg if ref in ("S", "W") else deg, 5)
            out["gps"] = (dms(gps[2], gps.get(1)), dms(gps[4], gps.get(3)))
        out["rating"] = next(_walk(xmp, "Rating"), None)
        label = next(_walk(xmp, "Label"), None)
        out["xlabel_len"] = len(label) if label else None
        desc = next(_walk(xmp, "description"), None)
        if desc is not None:
            texts = [t for t in _walk(desc, "text")]
            out["caption"] = cap_str(texts[0] if texts else str(desc))
        subj = next(_walk(xmp, "subject"), None)
        if subj is not None:
            items = next(_walk(subj, "li"), None)
            out["keywords"] = items if isinstance(items, list) else (
                [items] if items else None)
        regions = next(_walk(xmp, "Regions"), None)
        if regions is not None:
            faces = []
            for li in _walk(regions, "li"):
                for r in (li if isinstance(li, list) else [li]):
                    if isinstance(r, dict) and "Name" in r:
                        a = r.get("Area", {})
                        faces.append(face_str(r["Name"], a.get("x"), a.get("y"),
                                              a.get("w"), a.get("h")))
            out["faces"] = faces or None
        return out

    collect(res, corpus, extract)
    res["timing"]["in_process"] = timed_loop(
        timing_files(corpus), lambda f: extract(f)["dto"])
    return res


# --------------------------------------------------------------------------
# candidate: python-xmp-toolkit (expected to fail on Windows: needs Exempi)
# --------------------------------------------------------------------------

def read_xmptoolkit(corpus: Path) -> dict:
    res = result_skeleton("xmptoolkit")
    try:
        t0 = time.perf_counter()
        from libxmp import XMPFiles  # noqa: PLC0415
        res["import_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    except Exception as e:  # noqa: BLE001
        res["notes"].append(f"import failed: {e!r}")
        return res
    for name in CORPUS_FILES:
        try:
            xf = XMPFiles(file_path=str(corpus / name))
            xmp = xf.get_xmp()
            res["files"][name] = {**empty_fields(),
                                  "caption": cap_str(str(xmp)[:60])}
            xf.close_file()
        except Exception as e:  # noqa: BLE001
            res["files"][name] = {**empty_fields(), "error": repr(e)}
    return res


# --------------------------------------------------------------------------
# candidate: the current hand-rolled inmeta.py (baseline)
# --------------------------------------------------------------------------

def read_inmeta(corpus: Path) -> dict:
    res = result_skeleton("inmeta")
    sys.path.insert(0, str(REPO_ROOT / "apps" / "desktop-python"))
    t0 = time.perf_counter()
    import inmeta  # noqa: PLC0415
    res["import_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    res["versions"] = {"inmeta": "hand-rolled tracer module (JPEG-only)"}

    def extract(path: Path) -> dict:
        out = empty_fields()
        meta = inmeta.read_jpeg_metadata(path.read_bytes())
        out["caption"] = cap_str(meta.caption)
        out["keywords"] = list(meta.keywords) or None
        return out

    collect(res, corpus, extract)
    res["timing"]["in_process"] = timed_loop(
        timing_files(corpus),
        lambda f: inmeta.read_jpeg_metadata(f.read_bytes()) is not None)
    return res


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

CANDIDATES: dict[str, tuple[list[str], str]] = {
    # name -> (uv --with deps, read subcommand)
    "pyexiv2": (["pyexiv2"], "read-pyexiv2"),
    "exiv2": (["exiv2"], "read-exiv2"),
    "exiftool": (["PyExifTool"], "read-exiftool"),
    "pillow": (["Pillow", "defusedxml"], "read-pillow"),
    "xmptoolkit": (["python-xmp-toolkit"], "read-xmptoolkit"),
    "inmeta": ([], "read-inmeta"),
}

READERS = {"read-pyexiv2": read_pyexiv2, "read-exiv2": read_exiv2,
           "read-exiftool": read_exiftool, "read-pillow": read_pillow,
           "read-xmptoolkit": read_xmptoolkit, "read-inmeta": read_inmeta}


def timing_files(corpus: Path) -> list[Path]:
    return sorted((corpus / "timing").glob("t*.jpg"))


def collect(res: dict, corpus: Path, extract) -> None:
    for name in CORPUS_FILES:
        path = corpus / name
        try:
            res["files"][name] = extract(path)
        except Exception as e:  # noqa: BLE001 (spike: record, don't die)
            res["files"][name] = {**empty_fields(), "error": repr(e)}


def run_all(corpus: Path, only: set[str] | None) -> None:
    script = str(Path(__file__).resolve())
    if not (corpus / "jpeg_full.jpg").exists():
        print("== building fixtures (uv run --with Pillow) ==")
        rc = subprocess.run(
            ["uv", "run", "--no-project", "--with", "Pillow", "python",
             script, "make-fixtures", str(corpus)]).returncode
        if rc:
            sys.exit("fixture build failed")

    results_dir = corpus / "results"
    results_dir.mkdir(exist_ok=True)
    summary = {}
    for name, (deps, sub) in CANDIDATES.items():
        if only and name not in only:
            continue
        print(f"\n== {name} ==")
        cmd = ["uv", "run", "--no-project"]
        for d in deps:
            cmd += ["--with", d]
        cmd += ["python", script, sub, str(corpus)]
        t0 = time.perf_counter()
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8")
        wall = round(time.perf_counter() - t0, 1)
        if r.returncode:
            print(f"FAILED (env resolve or crash), wall {wall}s")
            print(r.stderr[-2000:])
            summary[name] = {"candidate": name, "error": r.stderr[-500:],
                             "uv_wall_s": wall}
            continue
        data = json.loads(r.stdout)
        data["uv_wall_s"] = wall
        summary[name] = data
        (results_dir / f"{name}.json").write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8")
        print(json.dumps(data, indent=2, default=str))

    (results_dir / "all.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"\nraw results in {results_dir}")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd = args[0]
    if cmd == "run":
        corpus = Path(tempfile.gettempdir()) / "fauxcasa-cam16-spike"
        only = None
        if "--only" in args:
            only = set(args[args.index("--only") + 1].split(","))
        if "--corpus" in args:
            corpus = Path(args[args.index("--corpus") + 1])
        run_all(corpus, only)
    elif cmd == "make-fixtures":
        make_fixtures(Path(args[1]))
    elif cmd in READERS:
        print(json.dumps(READERS[cmd](Path(args[1])), indent=2, default=str))
    else:
        sys.exit(f"unknown command {cmd!r}\n\n{__doc__}")


if __name__ == "__main__":
    main()
