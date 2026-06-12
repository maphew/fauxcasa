# Trial balloon: Go + gioui.org

See `balloons/README.md` for the contract. Mirrors `balloons/rust-egui`
(phase machine, metrics, prefetch ~2 screens, bounded LRU of 1200 decoded
thumbs ≈ 300 MB worst case).

## Build

Host lacks cgo headers; build in the benchbox toolbox (deps already
installed there: wayland-devel, libxkbcommon-devel, mesa-libEGL-devel,
mesa-libGLES-devel, libX11-devel, libXcursor-devel, libXfixes-devel,
vulkan-headers, golang):

```sh
toolbox run -c benchbox bash -c \
  'cd /var/home/matt/dev/fauxcasa/balloons/go-gio && go build -o balloon-go-gio .'
```

Cross-compiles for Windows with no cgo (`GOOS=windows go build`) — useful
for the CI "green on both platforms" requirement.

## Run

```sh
toolbox run -c benchbox bash -c \
  'WAYLAND_DISPLAY=wayland-bench /var/home/matt/dev/fauxcasa/balloons/go-gio/balloon-go-gio \
   <path.fcache> [--seconds S] [--speed PX_S]'
```

`BALLOON_DEBUG=1` logs phase transitions with wall time and per-phase frame
counts to stderr — useful for spotting compositor stalls (see note below).

## Known measurement hole (shared with rust-egui reference)

In the frame that crosses a flick-phase deadline, the phase machine
advances *before* the metrics block matches on phase, so that frame's
interval is silently dropped. If a long compositor/CPU stall lands exactly
on that frame, the stall vanishes from p99/max and the phase loses that
wall time from its frame count. Observed once on a loaded machine
(frames ≈ half expected with clean percentiles). Kept as-is for parity
with the reference; run benchmarks on an idle machine and check
`BALLOON_DEBUG` per-phase counts (~2700/phase at 60 Hz) to detect it.
