#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "yt-dlp",
#   "imageio-ffmpeg",
# ]
# ///
"""Fetch the Picasa tutorial videos listed in begin.md (480p + English
subtitles) and extract analysis stills into cache/videos/<id>/.

Idempotent: skips videos and frames that are already cached.
Requires only uv (https://docs.astral.sh/uv/); on Windows run as
`uv run scripts/fetch-videos.py`.

Frame naming: tNNN.jpg ≈ (N-1)*15s into the video; sceneNNN.jpg are hard cuts.
"""

import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

VIDEOS = [
    "drf6OTywF6E",  # Picasa 3.5 Part 1: Organizing
    "PuNjY6Zq6Cw",  # Picasa 3.5 Part 2: Basic Fixes and More
    "4L7IV_3xKKM",  # Importing Photos in Picasa 3.5
    "sOOkONjzcss",  # Backup photos with Picasa
    "c1_Tbs5e6Ag",  # Running Picasa 3 for the First Time (no captions)
    "ArB400dG0sU",  # Simple Photo Slideshow with Picasa
    "De1U5mAqCTM",  # Google Picasa (overview)
    "YfqThYqDs-8",  # Managing Thousands of Pictures on External Drives
]

CACHE = Path(__file__).resolve().parent.parent / "cache" / "videos"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def download(video_id: str, dest: Path) -> None:
    subprocess.run(
        [
            sys.executable, "-m", "yt_dlp",
            "-f", "bv*[height<=480]+ba/b[height<=480]/b",
            "--write-auto-subs", "--write-subs",
            "--sub-langs", "en", "--sub-format", "vtt",
            "--remote-components", "ejs:github",
            "-o", str(dest / "video.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ],
        check=True,
    )


def extract_frames(video: Path, frames: Path) -> None:
    frames.mkdir(parents=True)
    common = [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(video)]
    subprocess.run(common + ["-vf", "fps=1/15", str(frames / "t%03d.jpg")], check=True)
    subprocess.run(
        common + ["-vf", "select='gt(scene,0.3)'", "-vsync", "vfr", str(frames / "scene%03d.jpg")],
        check=True,
    )


def main() -> None:
    for video_id in VIDEOS:
        dest = CACHE / video_id
        dest.mkdir(parents=True, exist_ok=True)

        video = next((p for p in (dest / "video.mp4", dest / "video.mkv") if p.exists()), None)
        if video is None:
            download(video_id, dest)
            video = next((p for p in (dest / "video.mp4", dest / "video.mkv") if p.exists()), None)
        if video is None:
            print(f"{video_id}: download produced no video file", file=sys.stderr)
            continue

        frames = dest / "frames"
        if not frames.is_dir():
            extract_frames(video, frames)

        print(f"{video_id}: {len(list(frames.iterdir()))} frames cached")


if __name__ == "__main__":
    main()
