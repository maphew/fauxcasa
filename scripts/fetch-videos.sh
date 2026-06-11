#!/usr/bin/env bash
# Fetch the Picasa tutorial videos listed in begin.md (480p + English
# subtitles) and extract analysis stills into cache/videos/<id>/.
# Idempotent: skips videos and frames that are already cached.
# Requires: uv (for uvx), ffmpeg.
set -euo pipefail
cd "$(dirname "$0")/.."

CACHE=cache/videos

VIDEOS="
drf6OTywF6E
PuNjY6Zq6Cw
4L7IV_3xKKM
sOOkONjzcss
c1_Tbs5e6Ag
ArB400dG0sU
De1U5mAqCTM
YfqThYqDs-8
"

for id in $VIDEOS; do
  d="$CACHE/$id"
  mkdir -p "$d"

  if [ -z "$(ls "$d"/video.mp4 "$d"/video.mkv 2>/dev/null)" ]; then
    uvx yt-dlp -f "bv*[height<=480]+ba/b[height<=480]/b" \
      --write-auto-subs --write-subs --sub-langs en --sub-format vtt \
      --remote-components ejs:github \
      -o "$d/video.%(ext)s" "https://www.youtube.com/watch?v=$id"
  fi

  v=$(ls "$d"/video.mp4 "$d"/video.mkv 2>/dev/null | head -1)
  if [ -n "$v" ] && [ ! -d "$d/frames" ]; then
    mkdir -p "$d/frames"
    # tNNN.jpg ≈ (N-1)*15s into the video; sceneNNN.jpg are hard cuts
    ffmpeg -hide_banner -loglevel error -i "$v" -vf fps=1/15 "$d/frames/t%03d.jpg"
    ffmpeg -hide_banner -loglevel error -i "$v" -vf "select='gt(scene,0.3)'" -vsync vfr "$d/frames/scene%03d.jpg"
  fi

  echo "$id: $(ls "$d/frames" 2>/dev/null | wc -l) frames cached"
done
