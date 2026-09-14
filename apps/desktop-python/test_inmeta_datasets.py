#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pytest", "exiv2"]
# ///
"""Dataset-gated tests: run inmeta.py / metareader.py over the public
metadata test corpora adopted in docs/research/test-image-datasets.md
("Adopted", fauxcasa-lj1) and fetched by scripts/fetch-test-datasets.py.

fauxcasa-co7: this file wires those corpora into the test suite. Every
test below self-skips (pytest.mark.skipif) when its corpus's
``.fetch-complete`` marker (written by the fetch script, not a
MANIFEST.json -- there isn't one) is absent, so this module is always
safe to import and run -- including from scripts/preflight.py, with no
corpus fetched at all -- and CI only ever fetches the tiny IPTC set
(tracer.yml). The dataset root is
``cache/test-datasets/`` (gitignored, machine-local), overridable via the
``FAUXCASA_TEST_DATASETS`` env var for testing the skip behavior itself.

Two corpora, two roles (mirrors the "correctness baseline" / "edge cases"
split in docs/research/test-image-datasets.md):
  iptc-reference   Every field is filled with a self-describing value
                   (e.g. a caption reading "...(ref2021.1)"), so a
                   reader's mistakes are obvious. Exact values below were
                   read from the actual files, not guessed; the SHA-256
                   integrity test pins the corpus version this suite was
                   written against.
  exif-samples     ianare/exif-samples: real-world tricky/odd JPG/TIFF/
                   HEIC, incl. a GPS folder, an orientation folder, and
                   deliberately malformed/invalid files. Exercises the
                   fail-soft contract (never raise) both modules document.

metadata-extractor (drewnoakes/metadata-extractor-images) is included as
an opt-in bounded fail-soft sweep; it is NOT fetched by default (~2.5 GB)
so it self-skips on an ordinary machine, this one included.

Run:  uv run apps/desktop-python/test_inmeta_datasets.py -q
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import inmeta
import metareader

DATASETS = Path(
    os.environ.get("FAUXCASA_TEST_DATASETS")
    or Path(__file__).resolve().parents[2] / "cache" / "test-datasets"
)

# Suffixes the fail-soft sweeps consider "an image these readers should
# attempt" -- matches the corpus layout described in the task/docs, not an
# exhaustive format list.
_SUFFIXES = {".jpg", ".jpeg", ".tif", ".tiff", ".heic", ".heif"}


def needs_dataset(name: str):
    """Skip marker keyed on the fetch script's completion marker (no
    MANIFEST.json -- see scripts/fetch-test-datasets.py's ``is_complete``).
    """
    return pytest.mark.skipif(
        not (DATASETS / name / ".fetch-complete").exists(),
        reason=(
            f"{name} dataset not fetched -- run "
            f"`uv run scripts/fetch-test-datasets.py {name}`"
        ),
    )


# ---------------------------------------------------------------------------
# iptc-reference: correctness baseline (docs/research/test-image-datasets.md)
# ---------------------------------------------------------------------------

IPTC_DIR = DATASETS / "iptc-reference"
IPTC_FILES = (
    sorted(IPTC_DIR.glob("IPTC-PhotometadataRef-Std*.jpg"))
    if IPTC_DIR.is_dir() else []
)
_IPTC_IDS = [p.name for p in IPTC_FILES]

_IPTC_VERSION_RE = re.compile(r"IPTC-PhotometadataRef-Std([\d.]+)\.jpg$")

# EXIF DateTimeOriginal/DateTime is absent from this one file in the
# corpus (measured, not assumed) -- metareader.read_file_meta correctly
# returns None for it rather than guessing.
_IPTC_NO_DATE_TAKEN = {"IPTC-PhotometadataRef-Std2017.1.jpg"}

# sha256 of the 11 reference JPEGs as fetched on 2026-09-13, pinning the
# exact corpus version this suite's value assertions were written against
# (https://www.iptc.org/std/photometadata/examples/). A re-fetch that
# changes IPTC's published files should fail this loudly, not drift
# silently past the field-value checks below.
IPTC_SHA256 = {
    "IPTC-PhotometadataRef-Std2010.jpg": "f87850a1f6107a80cab2fca07992470dfddedadccc6a57a60bf9e36fadb17377",
    "IPTC-PhotometadataRef-Std2014.jpg": "94e85c977f156eeb23a330652c479ab1d8e140feddbdb0d3903c341a362986e6",
    "IPTC-PhotometadataRef-Std2016.jpg": "ca15eaaec343de8d8f40a8f7d742696e131ad1cff02b673cbc954097ed956157",
    "IPTC-PhotometadataRef-Std2017.1.jpg": "57b8fe4e64e322b65ec4546ef870a2b1c56909b41ea3db00e644738c815aff2d",
    "IPTC-PhotometadataRef-Std2019.1.jpg": "52068150f99739e692de2211445af34b990aaf3b20027c7046c8af0e88fafe4e",
    "IPTC-PhotometadataRef-Std2021.1.jpg": "c578389d83d513de2afbd5834bf96590c6fa0bbf894c318b2f89d9a4946cfe99",
    "IPTC-PhotometadataRef-Std2022.1.jpg": "b14ab0ef98251465c23ef429d8c5c4c56aa2b51ba05d25a7455bf31b67ec4830",
    "IPTC-PhotometadataRef-Std2023.1.jpg": "6031119963955cfc142301a0b918a5559dfd367122bfd2ea2bfa0ad733eb57d9",
    "IPTC-PhotometadataRef-Std2023.2.jpg": "979a7a3eb10c66760cccfa71fd2bcb076d4b3a0230a58aa786e009893e24bd7c",
    "IPTC-PhotometadataRef-Std2024.1.jpg": "adb4d638d98b7a08272b8fd6937998b986a481d586c38aff0c3b7d5d266e5dbe",
    "IPTC-PhotometadataRef-Std2025.1.jpg": "c5db0ba76bb4a495485334491d57b3b55cf8a151a79f0c7cc7865321fa3c0027",
}


@needs_dataset("iptc-reference")
@pytest.mark.parametrize("path", IPTC_FILES, ids=_IPTC_IDS)
def test_iptc_reference_integrity(path: Path) -> None:
    """Pins the corpus version: a changed upstream file must fail here
    first, before its (possibly now-wrong) field-value assertions run."""
    assert hashlib.sha256(path.read_bytes()).hexdigest() == IPTC_SHA256[path.name]


@needs_dataset("iptc-reference")
@pytest.mark.parametrize("path", IPTC_FILES, ids=_IPTC_IDS)
def test_iptc_reference_caption(path: Path) -> None:
    im = inmeta.read_jpeg_metadata(path.read_bytes())
    assert isinstance(im.caption, str) and im.caption
    assert im.caption.startswith("The description aka caption")
    version = _IPTC_VERSION_RE.search(path.name).group(1)
    # Every version except the oldest (2010) suffixes the caption with its
    # own version token, e.g. "...(ref2021.1)" -- verified by reading the
    # actual values, not guessed. 2010's caption carries no such suffix.
    if version != "2010":
        assert f"(ref{version})" in im.caption


@needs_dataset("iptc-reference")
@pytest.mark.parametrize("path", IPTC_FILES, ids=_IPTC_IDS)
def test_iptc_reference_keywords(path: Path) -> None:
    im = inmeta.read_jpeg_metadata(path.read_bytes())
    assert isinstance(im.keywords, tuple) and im.keywords
    assert all(isinstance(k, str) for k in im.keywords)
    # Self-describing per IPTC's reference-image convention
    # ("Keyword1ref2021.1", ...) -- except IPTC-PhotometadataRef-Std2010.jpg,
    # whose 5th keyword is a reference-doc identifier
    # ("IPTC-PhotometadataRef01wmd_2mdc") rather than "KeywordN", so only
    # "at least one" is asserted rather than "every".
    assert any("Keyword" in kw for kw in im.keywords)


@needs_dataset("iptc-reference")
@pytest.mark.parametrize("path", IPTC_FILES, ids=_IPTC_IDS)
def test_iptc_reference_date_taken(path: Path) -> None:
    fm = metareader.read_file_meta(path.read_bytes())
    if path.name in _IPTC_NO_DATE_TAKEN:
        assert fm.date_taken is None
        return
    assert fm.date_taken is not None
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", fm.date_taken)


@needs_dataset("iptc-reference")
@pytest.mark.parametrize("path", IPTC_FILES, ids=_IPTC_IDS)
def test_iptc_reference_gps(path: Path) -> None:
    fm = metareader.read_file_meta(path.read_bytes())
    if fm.gps is None:
        return
    lat, lon = fm.gps
    assert isinstance(lat, float) and isinstance(lon, float)
    assert -90.0 <= lat <= 90.0
    assert -180.0 <= lon <= 180.0


# ---------------------------------------------------------------------------
# exif-samples: edge cases / fuzzing (ianare/exif-samples)
# ---------------------------------------------------------------------------

EXIF_DIR = DATASETS / "exif-samples"
EXIF_ALL_FILES = (
    sorted(p for p in EXIF_DIR.rglob("*")
           if p.is_file() and p.suffix.lower() in _SUFFIXES)
    if EXIF_DIR.is_dir() else []
)
_EXIF_ALL_IDS = [str(p.relative_to(EXIF_DIR)) for p in EXIF_ALL_FILES]


@needs_dataset("exif-samples")
@pytest.mark.parametrize("path", EXIF_ALL_FILES, ids=_EXIF_ALL_IDS)
def test_exif_samples_fail_soft(path: Path) -> None:
    """Every file in the corpus (jpg/tiff/heic, incl. corrupted/invalid
    subfolders): the readers must not raise and must return their
    documented types. Parametrized by relative path so a failure names
    the exact file."""
    data = path.read_bytes()
    im = inmeta.read_jpeg_metadata(data)
    fm = metareader.read_file_meta(data)
    orientation = metareader.read_orientation(data)

    assert isinstance(im.caption, (str, type(None)))
    assert isinstance(im.keywords, tuple)
    assert all(isinstance(k, str) for k in im.keywords)
    assert isinstance(fm.date_taken, (str, type(None)))
    assert fm.gps is None or (
        isinstance(fm.gps, tuple) and len(fm.gps) == 2
        and all(isinstance(v, float) for v in fm.gps)
    )
    assert isinstance(fm.rating, (int, type(None)))
    assert isinstance(orientation, int) and 1 <= orientation <= 8


GPS_DIR = EXIF_DIR / "jpg" / "gps"
GPS_FILES = sorted(GPS_DIR.glob("DSCN*.jpg")) if GPS_DIR.is_dir() else []
# Measured 2026-09-13: all 9 DSCN*.jpg under jpg/gps/ carry EXIF GPS.
_GPS_MIN_HITS = 9


@needs_dataset("exif-samples")
def test_exif_samples_gps_dscn_yield_gps() -> None:
    hits = sum(
        1 for p in GPS_FILES
        if metareader.read_file_meta(p.read_bytes()).gps is not None
    )
    assert hits >= _GPS_MIN_HITS


ORIENTATION_DIR = EXIF_DIR / "jpg" / "orientation"
ORIENTATION_FILES = (
    sorted(ORIENTATION_DIR.glob("*.jpg")) if ORIENTATION_DIR.is_dir() else []
)
_ORIENTATION_IDS = [p.name for p in ORIENTATION_FILES]
# Filenames encode the expected orientation as a trailing "_<1-8>":
# landscape_1.jpg..landscape_8.jpg, portrait_1.jpg..portrait_8.jpg.
_ORIENTATION_NAME_RE = re.compile(r"_(\d)\.jpg$")


@needs_dataset("exif-samples")
@pytest.mark.parametrize("path", ORIENTATION_FILES, ids=_ORIENTATION_IDS)
def test_exif_samples_orientation_matches_filename(path: Path) -> None:
    m = _ORIENTATION_NAME_RE.search(path.name)
    assert m, f"unexpected orientation-sample filename: {path.name}"
    expected = int(m.group(1))
    got = metareader.read_orientation(path.read_bytes())
    assert 1 <= got <= 8
    assert got == expected


INVALID_DIR = EXIF_DIR / "jpg" / "invalid"
INVALID_FILES = sorted(INVALID_DIR.glob("*.jpg")) if INVALID_DIR.is_dir() else []
_INVALID_IDS = [p.name for p in INVALID_FILES]


@needs_dataset("exif-samples")
@pytest.mark.parametrize("path", INVALID_FILES, ids=_INVALID_IDS)
def test_exif_samples_invalid_files_yield_empty(path: Path) -> None:
    data = path.read_bytes()
    im = inmeta.read_jpeg_metadata(data)
    fm = metareader.read_file_meta(data)
    orientation = metareader.read_orientation(data)
    assert im.caption is None
    assert im.keywords == ()
    assert fm.date_taken is None
    assert fm.gps is None
    assert fm.rating is None
    assert orientation == 1


@needs_dataset("exif-samples")
def test_exif_samples_corrupted_jpg_does_not_raise() -> None:
    """Surprising, measured behavior: despite the filename, corrupted.jpg's
    EXIF header is intact enough for metareader to recover a
    DateTimeOriginal -- fail-soft means "never raise", not "always empty".
    inmeta finds no caption/keywords in it either way. Assert shape (never
    raise, right types), not the specific non-empty date, so this test does
    not overfit to one upstream file's exact bytes beyond what the corpus's
    own content already implies."""
    path = EXIF_DIR / "jpg" / "corrupted.jpg"
    data = path.read_bytes()
    im = inmeta.read_jpeg_metadata(data)
    fm = metareader.read_file_meta(data)
    orientation = metareader.read_orientation(data)
    assert im.caption is None
    assert im.keywords == ()
    assert isinstance(fm.date_taken, (str, type(None)))
    assert isinstance(orientation, int) and 1 <= orientation <= 8


@needs_dataset("exif-samples")
def test_exif_samples_long_description_caption() -> None:
    """jpg/long_description.jpg: measured caption length is 419 chars;
    assert the shape (str, > 200 chars) rather than the exact text."""
    path = EXIF_DIR / "jpg" / "long_description.jpg"
    im = inmeta.read_jpeg_metadata(path.read_bytes())
    assert isinstance(im.caption, str)
    assert len(im.caption) > 200


# ---------------------------------------------------------------------------
# metadata-extractor: heavy fuzzing corpus, opt-in only (~2.5 GB, not
# fetched by scripts/fetch-test-datasets.py's default set) -- self-skips on
# an ordinary machine, this one included.
# ---------------------------------------------------------------------------

MDEX_DIR = DATASETS / "metadata-extractor"
MDEX_FILES = (
    sorted(p for p in MDEX_DIR.rglob("*")
           if p.is_file() and p.suffix.lower() in _SUFFIXES)[:500]
    if MDEX_DIR.is_dir() else []
)
_MDEX_IDS = [str(p.relative_to(MDEX_DIR)) for p in MDEX_FILES]


@needs_dataset("metadata-extractor")
@pytest.mark.parametrize("path", MDEX_FILES, ids=_MDEX_IDS)
def test_metadata_extractor_fail_soft(path: Path) -> None:
    """Bounded (first 500 matching files) fail-soft sweep: the reference
    fuzzing corpus is ~2.5 GB, so this only ever runs when a developer has
    explicitly opted in via `fetch-test-datasets.py metadata-extractor`."""
    data = path.read_bytes()
    im = inmeta.read_jpeg_metadata(data)
    fm = metareader.read_file_meta(data)
    orientation = metareader.read_orientation(data)
    assert isinstance(im.caption, (str, type(None)))
    assert isinstance(im.keywords, tuple)
    assert isinstance(fm.date_taken, (str, type(None)))
    assert isinstance(fm.rating, (int, type(None)))
    assert isinstance(orientation, int) and 1 <= orientation <= 8


if __name__ == "__main__":
    # Forward CLI args so `uv run test_inmeta_datasets.py -k X -x` selects
    # tests instead of silently running the whole suite (mirrors
    # test_tracer.py's __main__, fauxcasa-q6l.17).
    sys.exit(pytest.main([__file__] + (sys.argv[1:] or ["-v"])))
