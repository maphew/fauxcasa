#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow"]
# ///
"""Capture the documentation screenshot gallery from the demo library.

Runs the app natively (a window flashes briefly per shot; that is expected)
against ``cache/demo-library`` (build it first with
``uv run scripts/make-demo-library.py``) and saves one JPEG per major view
under ``docs/releases/gallery/``. Every shot goes through main.py's
scripted-run flags (``--view``, ``--search``, ``--select``, ``--info``,
``--open``, ``--faces``, ``--play``, ``--window-size``, ``--screenshot``),
so the gallery is reproducible on any machine with the demo library.

Why native rather than QT_QPA_PLATFORM=offscreen: offscreen renders every
UI string as an empty box on Windows (no fontconfig fonts) and ignores the
window size; see bd memory ``windows-screenshot-fonts``.

Usage (on Windows: ``uv run scripts/make-gallery.py``):
  make-gallery.py                 # all shots
  make-gallery.py --only viewer   # one shot by name
  make-gallery.py --list          # names and what each shows
  make-gallery.py --out DIR       # somewhere other than docs/releases/gallery

The first run builds the app's thumbnail cache for the demo library under
``cache/demo-library/.app-cache`` (gitignored); later runs start warm, which
is also what keeps ``--window-size`` exact (a cold scan can re-layout the
window once while the walk lands).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
MAIN = REPO / "apps" / "desktop-python" / "main.py"
DEMO = REPO / "cache" / "demo-library"
LIBRARY = DEMO / "library"
CONTACTS = DEMO / "contacts" / "contacts.xml"
PAL_DIR = DEMO / "albums"
APP_CACHE = DEMO / ".app-cache"
OUT_DEFAULT = REPO / "docs" / "releases" / "gallery"

WINDOW = "1600x1000"
MAX_WIDTH = 1920  # full-screen captures are downscaled to this
JPEG_QUALITY = 86

# name -> (what it shows, extra main.py args)
SHOTS: dict[str, tuple[str, list[str]]] = {
    "gallery": (
        "the library grouped by folder, sidebar with folders, albums, people",
        ["--zoom", "144"],
    ),
    "viewer": (
        "the photo viewer on a single photo",
        ["--search", "santorini", "--open", "0"],
    ),
    "faces": (
        "the viewer with Picasa's face boxes shown (F)",
        ["--view", "person:Alice Hartley", "--open", "0", "--faces"],
    ),
    "album": (
        "an album from Picasa, spanning two folders",
        ["--view", "album:Best of 2014"],
    ),
    "people": (
        "a person from Picasa's face tags, with the Info panel open",
        ["--view", "person:Ben Okafor", "--select", "0", "--info"],
    ),
    "search": (
        "search as you type, matching folder names, keywords and captions",
        ["--search", "lake"],
    ),
    "info": (
        "the Info panel: date, camera, place, caption, keywords, albums",
        ["--view", "album:Best of 2014", "--select", "0", "--info"],
    ),
    "slideshow": (
        "the slideshow (full screen) over the Starred view",
        ["--view", "starred", "--play"],
    ),
    "zoomed-out": (
        "the grid at its smallest thumbnail size",
        ["--zoom", "72"],
    ),
}


def _base_args(cache_root: Path) -> list[str]:
    return [
        "uv", "run", str(MAIN), str(LIBRARY),
        "--contacts", str(CONTACTS), "--pal-dir", str(PAL_DIR),
        "--cache-root", str(cache_root),
        "--timeout", "180",
    ]


def _native_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)  # native window: real fonts + size
    return env


def _run(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(args, env=_native_env(), cwd=str(REPO),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    return proc.returncode, proc.stdout + proc.stderr


def warm_cache(cache_root: Path) -> None:
    print("warming the app cache for the demo library ...", flush=True)
    rc, out = _run(_base_args(cache_root) + ["--finish-build", "--quit-after-ready"])
    ready = [ln for ln in out.splitlines() if '"event": "ready"' in ln]
    if rc != 0 or not ready:
        print(out[-4000:])
        raise SystemExit(f"warm-up run failed (exit {rc})")
    print("  " + ready[-1])


def capture(name: str, extra: list[str], cache_root: Path, out_dir: Path) -> Path:
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / f"{name}.png"
        rc, out = _run(_base_args(cache_root) + [
            "--window-size", WINDOW, "--screenshot", str(png), *extra])
        events = [ln for ln in out.splitlines() if '"event": "view"' in ln]
        for ev in events:
            print("  " + ev)
        if rc != 0 or not png.exists():
            print(out[-4000:])
            raise SystemExit(f"{name}: screenshot run failed (exit {rc})")
        for ev in events:
            if not json.loads(ev).get("ok", True):
                raise SystemExit(f"{name}: a scripted view step failed: {ev}")
        out_dir.mkdir(parents=True, exist_ok=True)
        jpg = out_dir / f"{name}.jpg"
        img = Image.open(png).convert("RGB")
        if img.width > MAX_WIDTH:
            # A full-screen surface (the slideshow) is captured at the
            # monitor's native size; a 4K doc image is bulk, not information.
            img = img.resize((MAX_WIDTH, round(img.height * MAX_WIDTH / img.width)),
                             Image.Resampling.LANCZOS)
        img.save(jpg, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return jpg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", action="append", metavar="NAME",
                    help="capture only this shot (repeatable)")
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT,
                    help=f"output directory (default: {OUT_DEFAULT.relative_to(REPO)})")
    ap.add_argument("--cache-root", type=Path, default=APP_CACHE,
                    help="the app's cache root for the demo library")
    ap.add_argument("--list", action="store_true", help="list shots and exit")
    args = ap.parse_args()

    if args.list:
        for name, (what, extra) in SHOTS.items():
            print(f"  {name:<12} {what}\n  {'':<12} {' '.join(extra)}")
        return 0
    if not LIBRARY.is_dir() or not CONTACTS.exists():
        print("demo library missing; build it first:\n"
              "  uv run scripts/make-demo-library.py", file=sys.stderr)
        return 2
    names = args.only or list(SHOTS)
    unknown = [n for n in names if n not in SHOTS]
    if unknown:
        print(f"unknown shot(s): {', '.join(unknown)}; known: {', '.join(SHOTS)}",
              file=sys.stderr)
        return 2

    warm_cache(args.cache_root)
    for name in names:
        what, extra = SHOTS[name]
        print(f"{name}: {what}", flush=True)
        jpg = capture(name, extra, args.cache_root, args.out)
        print(f"  -> {jpg.relative_to(REPO) if jpg.is_relative_to(REPO) else jpg}"
              f" ({jpg.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
