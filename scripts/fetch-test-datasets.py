#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Fetch the external test-image datasets adopted for Fauxcasa's test suite.

Decision recorded in docs/research/test-image-datasets.md and beads
fauxcasa-lj1: alongside our synthetic library generator
(scripts/make-synthetic-library.py), we adopt a small set of public,
read-only metadata test corpora. They are *local-fetch only* — downloaded
into cache/test-datasets/ (gitignored) and never committed, because real
photos and third-party corpora are not redistributable (see the privacy
rule in CLAUDE.md / bd memory privacy-real-picasa-data).

What each set is for (the synthetic generator still owns library structure,
albums, faces, .picasa.ini sidecars — nothing off-the-shelf models those):

  iptc-reference   Correctness baseline. Every metadata field is filled with
                   a self-describing value (Creator reads "Creator1 (ref...)"),
                   so a reader's mistakes are obvious. One JPEG per IPTC
                   standard version, 2010..2025.1.   ~small, no auth.
  exif-samples     Edge cases / fuzzing. ianare/exif-samples: the de-facto
                   community corpus of tricky/odd JPG/TIFF/HEIC incl. a GPS
                   folder. CC BY-SA 4.0; repo archived 2025-04.   ~tens of MB.
  metadata-extractor  Heavy fuzzing corpus. drewnoakes/metadata-extractor-images:
                   the malformed/real-world files metadata-extractor validates
                   against.   ~2.5 GB — OPT-IN ONLY (name it explicitly or --all).
  unsplash-lite    Volume & realism (thumbnail/scroll/scale at 25k photos).
                   Terms-gated: cannot be fetched non-interactively, so this
                   prints manual download instructions and the expected path.

Usage (uv only; on Windows: `uv run scripts/fetch-test-datasets.py`):
  fetch-test-datasets.py                 # fetch the default core set
  fetch-test-datasets.py --list          # show datasets and status
  fetch-test-datasets.py exif-samples    # fetch one named set
  fetch-test-datasets.py --all           # fetch everything auto-fetchable

Idempotent: an already-populated dataset dir is skipped.
"""

import argparse
import io
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "cache" / "test-datasets"

IPTC_BASE = "https://www.iptc.org/std/photometadata/examples/"
IPTC_VERSIONS = [
    "2010", "2014", "2016", "2017.1", "2019.1", "2021.1",
    "2022.1", "2023.1", "2023.2", "2024.1", "2025.1",
]


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "fauxcasa-fetch/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 (trusted hosts)
        return r.read()


def _extract_zip_stripped(data: bytes, dest: Path) -> int:
    """Extract a GitHub codeload zip, stripping its single top-level dir."""
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = zf.namelist()
        root = members[0].split("/", 1)[0] + "/" if members else ""
        for name in members:
            if name.endswith("/"):
                continue
            rel = name[len(root):] if name.startswith(root) else name
            if not rel:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count


def fetch_iptc(dest: Path) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    got = 0
    for v in IPTC_VERSIONS:
        fname = f"IPTC-PhotometadataRef-Std{v}.jpg"
        target = dest / fname
        if target.exists():
            got += 1
            continue
        target.write_bytes(_get(IPTC_BASE + fname))
        got += 1
    return f"{got} reference JPEGs"


def fetch_github_zip(repo: str, branch: str, dest: Path) -> str:
    url = f"https://codeload.github.com/{repo}/zip/refs/heads/{branch}"
    n = _extract_zip_stripped(_get(url), dest)
    return f"{n} files from {repo}@{branch}"


def fetch_unsplash_manual(dest: Path) -> str:
    raise ManualFetch(
        "Unsplash Lite is terms-gated and cannot be fetched non-interactively.\n"
        "  1. Accept the terms and download the Lite archive from:\n"
        "       https://unsplash.com/data  (the 'Lite' download)\n"
        "  2. Unzip the metadata TSVs into:\n"
        f"       {dest}\n"
        "  3. Real photos are NOT redistributable -- keep them local; the\n"
        "     repo only ever commits synthetic fixtures.\n"
        "  The Lite set is 25k photos of metadata (+ photo URLs) for volume\n"
        "  and messy-real-metadata tests; see docs/research/test-image-datasets.md."
    )


class ManualFetch(Exception):
    """Raised by a dataset that requires a manual, interactive download."""


# name -> (layer, default-fetch?, fetcher)
DATASETS = {
    "iptc-reference": ("correctness", True, fetch_iptc),
    "exif-samples": (
        "edge-cases", True,
        lambda d: fetch_github_zip("ianare/exif-samples", "master", d),
    ),
    "metadata-extractor": (
        "fuzzing (~2.5 GB)", False,
        lambda d: fetch_github_zip("drewnoakes/metadata-extractor-images", "main", d),
    ),
    "unsplash-lite": ("volume", False, fetch_unsplash_manual),
}


def is_populated(dest: Path) -> bool:
    return dest.is_dir() and any(dest.iterdir())


def do_list() -> None:
    print(f"Datasets (cache root: {CACHE})\n")
    for name, (layer, default, _) in DATASETS.items():
        dest = CACHE / name
        status = "present" if is_populated(dest) else "missing"
        tag = "default" if default else "opt-in"
        print(f"  {name:<20} [{tag:<7}] {layer:<22} {status}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("names", nargs="*", help="dataset names to fetch (default: core set)")
    ap.add_argument("--all", action="store_true", help="fetch every auto-fetchable set")
    ap.add_argument("--list", action="store_true", help="list datasets and exit")
    ap.add_argument("--force", action="store_true", help="re-fetch even if present")
    args = ap.parse_args()

    if args.list:
        do_list()
        return 0

    if args.names:
        unknown = [n for n in args.names if n not in DATASETS]
        if unknown:
            print(f"unknown dataset(s): {', '.join(unknown)}", file=sys.stderr)
            print(f"known: {', '.join(DATASETS)}", file=sys.stderr)
            return 2
        selected = args.names
    elif args.all:
        selected = list(DATASETS)
    else:
        selected = [n for n, (_, default, _) in DATASETS.items() if default]

    rc = 0
    for name in selected:
        _layer, _default, fetcher = DATASETS[name]
        dest = CACHE / name
        if is_populated(dest) and not args.force:
            print(f"{name}: already present ({dest}) -- skipping")
            continue
        try:
            result = fetcher(dest)
            print(f"{name}: {result} -> {dest}")
        except ManualFetch as e:
            print(f"{name}: manual step required\n{e}")
        except Exception as e:  # noqa: BLE001 — report and continue other sets
            print(f"{name}: FAILED ({e})", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
