#!/usr/bin/env bash
# Launch the live Wine Picasa oracle headless onto the weston Xwayland display.
# Run inside a detached tmux session so Picasa keeps a controlling terminal yet
# survives across agent tool calls:
#   ORACLE_DISPLAY=:3 tmux new-session -d -s oracle scripts/launch-oracle-picasa.sh
# See docs/research/wine-oracle.md "Driving headlessly".
set -u
cd "$(dirname "$0")/.." || exit 1
PREFIX="$PWD/cache/wine-oracle"
[ -d "$PREFIX" ] || { echo "missing Wine prefix $PREFIX (Wine would silently bootstrap a blank one)" >&2; exit 1; }
# The weston Xwayland display is discovered from the weston log, not fixed —
# require it explicitly rather than guessing (fall back to the host DISPLAY).
DISP="${ORACLE_DISPLAY:-${DISPLAY:?set ORACLE_DISPLAY to the weston Xwayland display (e.g. :3)}}"
[ -S "/tmp/.X11-unix/X${DISP#:}" ] || { echo "no X server socket on $DISP" >&2; exit 1; }

# Flatpak bind-mounts only the X socket matching the HOST DISPLAY at launch and
# forces the sandbox DISPLAY to match, so the host env must carry the target
# display — --env= alone is clobbered, and an inner export names a socket that
# was never mounted, leaving Wine on its null driver (see
# scripts/oracle-sentinel-experiment.py, fauxcasa-5kl runs 1-9).
exec env DISPLAY="$DISP" flatpak run --filesystem=home \
  --env=DISPLAY="$DISP" \
  --env=WINEPREFIX="$PREFIX" \
  --command=sh org.winehq.Wine -c \
  "unset WAYLAND_DISPLAY; \
   export WINEDLLOVERRIDES='mscoree,mshtml=;winewayland.drv='; \
   exec wine 'C:\\Program Files (x86)\\Google\\Picasa3\\Picasa3.exe'"
