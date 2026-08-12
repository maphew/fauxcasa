#!/usr/bin/env bash
# Launch the live Wine Picasa oracle headless onto display :2 (weston Xwayland).
# Run inside a detached tmux session so Picasa keeps a controlling terminal yet
# survives across agent tool calls:  tmux new-session -d -s oracle scripts/launch-oracle-picasa.sh
# See docs/research/wine-oracle.md "Driving headlessly".
set -u
cd "$(dirname "$0")/.." || exit 1
PREFIX="$PWD/cache/wine-oracle"
DISP="${ORACLE_DISPLAY:-:2}"

exec flatpak run --filesystem=home \
  --env=WINEPREFIX="$PREFIX" \
  --command=sh org.winehq.Wine -c \
  "unset WAYLAND_DISPLAY; export DISPLAY=$DISP; \
   export WINEDLLOVERRIDES='mscoree,mshtml=;winewayland.drv='; \
   exec wine 'C:\\Program Files (x86)\\Google\\Picasa3\\Picasa3.exe'"
