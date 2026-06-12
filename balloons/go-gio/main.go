// Trial balloon: Go + gioui.org (immediate mode). See balloons/README.md
// for the fixed task, the fcache format, and the output contract. This
// mirrors the rust-egui reference implementation's phase machine, metrics
// definitions, prefetch and bounded-LRU design.
package main

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"image"
	"image/draw"
	"image/jpeg"
	"log"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"gioui.org/app"
	"gioui.org/f32"
	"gioui.org/op"
	"gioui.org/op/clip"
	"gioui.org/op/paint"
	"gioui.org/unit"
	"image/color"
)

const (
	cell            = 168.0
	tileSide        = 160.0
	winW            = 1280
	winH            = 800
	textureCap      = 1200 // bounded decoded-thumb cache: ~300 MB worst case at 256x256 RGBA
	prefetchScreens = 2.0
	dwellAfterFillS = 2.0
	decodeWorkers   = 4
)

type entry struct {
	offset uint64
	length uint32
}

func readCacheIndex(path string) []entry {
	f, err := os.Open(path)
	if err != nil {
		log.Fatalf("open fcache: %v", err)
	}
	defer f.Close()
	header := make([]byte, 16)
	if _, err := f.Read(header); err != nil {
		log.Fatalf("header: %v", err)
	}
	if string(header[0:4]) != "FCTC" {
		log.Fatal("bad magic")
	}
	if binary.LittleEndian.Uint32(header[4:8]) != 1 {
		log.Fatal("bad version")
	}
	count := int(binary.LittleEndian.Uint32(header[8:12]))
	index := make([]byte, count*16)
	if _, err := f.ReadAt(index, 16); err != nil {
		log.Fatalf("index: %v", err)
	}
	entries := make([]entry, count)
	for i := 0; i < count; i++ {
		r := index[i*16 : i*16+16]
		entries[i] = entry{
			offset: binary.LittleEndian.Uint64(r[0:8]),
			length: binary.LittleEndian.Uint32(r[8:12]),
			// u16 width/height at r[12:16] unused; dims come from decode
		}
	}
	return entries
}

type decoded struct {
	idx int
	img *image.RGBA
}

func spawnDecoders(path string, entries []entry, jobs <-chan int, results chan<- decoded) {
	for w := 0; w < decodeWorkers; w++ {
		f, err := os.Open(path)
		if err != nil {
			log.Fatalf("worker open: %v", err)
		}
		go func(f *os.File) {
			for idx := range jobs {
				e := entries[idx]
				buf := make([]byte, e.length)
				if _, err := f.ReadAt(buf, int64(e.offset)); err != nil {
					continue
				}
				img, err := jpeg.Decode(bytes.NewReader(buf))
				if err != nil {
					continue
				}
				b := img.Bounds()
				rgba := image.NewRGBA(image.Rect(0, 0, b.Dx(), b.Dy()))
				draw.Draw(rgba, rgba.Bounds(), img, b.Min, draw.Src)
				results <- decoded{idx: idx, img: rgba}
			}
		}(f)
	}
}

type phase int

const (
	phaseColdStart phase = iota
	phaseFlickA
	phaseTeleport
	phaseTeleportDwell
	phaseFlickB
	phaseDone
)

type tile struct {
	ready bool
	op    paint.ImageOp
	w, h  int
	used  uint64
}

type metrics struct {
	coldStartMs     float64
	frameMs         []float64
	blankTileFrames uint64
	fillMs          []float64
}

type appState struct {
	entries           []entry
	jobs              chan<- int
	results           <-chan decoded
	tiles             map[int]*tile
	start             time.Time
	phase             phase
	teleportStop      int
	phaseStart        time.Time
	teleportFillStart time.Time
	haveFillStart     bool
	lastFrame         time.Time
	haveLastFrame     bool
	frameNo           uint64
	offsetY           float32
	metrics           metrics
	seconds           float64
	speed             float32
}

func (a *appState) cols(width float32) int {
	c := int(width / cell)
	if c < 1 {
		c = 1
	}
	return c
}

func (a *appState) totalHeight(cols int) float32 {
	rows := (len(a.entries) + cols - 1) / cols
	return float32(rows) * cell
}

func (a *appState) visibleRange(cols int, height float32) (int, int) {
	firstRow := int(a.offsetY / cell)
	if firstRow < 0 {
		firstRow = 0
	}
	lastRow := int((a.offsetY+height)/cell) + 1 // ceil
	lo := firstRow * cols
	hi := (lastRow + 1) * cols
	if hi > len(a.entries) {
		hi = len(a.entries)
	}
	if lo > hi {
		lo = hi
	}
	return lo, hi
}

func (a *appState) pumpDecoded() {
	for {
		select {
		case d := <-a.results:
			b := d.img.Bounds()
			a.tiles[d.idx] = &tile{
				ready: true,
				op:    paint.NewImageOp(d.img),
				w:     b.Dx(),
				h:     b.Dy(),
				used:  a.frameNo,
			}
		default:
			return
		}
	}
}

func (a *appState) requestRange(lo, hi int) {
	for i := lo; i < hi; i++ {
		if _, ok := a.tiles[i]; !ok {
			a.tiles[i] = &tile{ready: false}
			a.jobs <- i
		}
	}
}

func (a *appState) evictLRU() {
	ready := 0
	for _, t := range a.tiles {
		if t.ready {
			ready++
		}
	}
	if ready <= textureCap {
		return
	}
	type aged struct {
		idx  int
		used uint64
	}
	var list []aged
	for i, t := range a.tiles {
		if t.ready {
			list = append(list, aged{i, t.used})
		}
	}
	sort.Slice(list, func(x, y int) bool { return list[x].used < list[y].used })
	for _, v := range list[:ready-textureCap] {
		delete(a.tiles, v.idx)
	}
}

func (a *appState) allVisibleReady(lo, hi int) bool {
	for i := lo; i < hi; i++ {
		t, ok := a.tiles[i]
		if !ok || !t.ready {
			return false
		}
	}
	return true
}

func (a *appState) phaseAnchor(maxOff float32) float32 {
	switch a.phase {
	case phaseFlickA:
		return 0
	case phaseFlickB:
		return 0.5 * maxOff
	default:
		return a.offsetY
	}
}

func (a *appState) advance(now time.Time) {
	switch a.phase {
	case phaseColdStart:
		a.phase = phaseFlickA
	case phaseFlickA:
		a.phase = phaseTeleport
		a.teleportStop = 0
	case phaseTeleport:
		a.phase = phaseTeleportDwell
	case phaseTeleportDwell:
		if a.teleportStop < 3 {
			a.teleportStop++
			a.phase = phaseTeleport
		} else {
			a.phase = phaseFlickB
		}
	case phaseFlickB:
		a.phase = phaseDone
	}
	a.phaseStart = now
	a.haveFillStart = false
	if os.Getenv("BALLOON_DEBUG") != "" {
		fmt.Fprintf(os.Stderr, "phase -> %d (stop %d) wall=%.2fs frameNo=%d recorded=%d\n",
			a.phase, a.teleportStop, now.Sub(a.start).Seconds(), a.frameNo,
			len(a.metrics.frameMs))
	}
}

func readRSSMb() (float64, float64) {
	status, err := os.ReadFile("/proc/self/status")
	if err != nil {
		return 0, 0
	}
	grab := func(key string) float64 {
		for _, l := range strings.Split(string(status), "\n") {
			if strings.HasPrefix(l, key) {
				fields := strings.Fields(l)
				if len(fields) >= 2 {
					if kb, err := strconv.ParseFloat(fields[1], 64); err == nil {
						return kb / 1024.0
					}
				}
			}
		}
		return 0
	}
	return grab("VmRSS:"), grab("VmHWM:")
}

func (a *appState) finish() {
	sorted := append([]float64(nil), a.metrics.frameMs...)
	sort.Float64s(sorted)
	pct := func(p float64) float64 {
		if len(sorted) == 0 {
			return 0
		}
		i := int(float64(len(sorted)) * p)
		if i > len(sorted)-1 {
			i = len(sorted) - 1
		}
		return sorted[i]
	}
	maxMs := 0.0
	if len(sorted) > 0 {
		maxMs = sorted[len(sorted)-1]
	}
	rss, hwm := readRSSMb()
	fills := make([]string, len(a.metrics.fillMs))
	for i, f := range a.metrics.fillMs {
		fills[i] = fmt.Sprintf("%.0f", f)
	}
	fmt.Printf("{\"candidate\": \"go-gio\", \"photos\": %d, "+
		"\"cold_start_ms\": %.0f, \"frames\": %d, \"p50_ms\": %.2f, "+
		"\"p99_ms\": %.2f, \"max_ms\": %.2f, \"blank_tile_frames\": %d, "+
		"\"fill_ms\": [%s], \"vm_rss_mb\": %.1f, \"vm_hwm_mb\": %.1f}\n",
		len(a.entries),
		a.metrics.coldStartMs,
		len(a.metrics.frameMs),
		pct(0.50),
		pct(0.99),
		maxMs,
		a.metrics.blankTileFrames,
		strings.Join(fills, ", "),
		rss,
		hwm,
	)
}

func (a *appState) frame(ops *op.Ops, w, h float32) bool {
	now := time.Now()
	var frameMs float64
	haveFrameMs := false
	if a.haveLastFrame {
		frameMs = float64(now.Sub(a.lastFrame)) / float64(time.Millisecond)
		haveFrameMs = true
	}
	a.lastFrame = now
	a.haveLastFrame = true
	a.frameNo++

	a.pumpDecoded()

	// The interval just measured belongs to the phase that was active while
	// the frame was produced — record it BEFORE the state machine advances,
	// so a stall landing exactly on a phase deadline is not dropped.
	if a.phase == phaseFlickA || a.phase == phaseFlickB {
		if haveFrameMs {
			a.metrics.frameMs = append(a.metrics.frameMs, frameMs)
		}
	}

	cols := a.cols(w)
	total := a.totalHeight(cols)
	maxOff := total - h
	if maxOff < 0 {
		maxOff = 0
	}

	// ----- phase state machine drives the offset -----
	elapsed := now.Sub(a.phaseStart).Seconds()
	switch a.phase {
	case phaseColdStart:
	case phaseFlickA, phaseFlickB:
		a.offsetY = a.phaseAnchor(maxOff) + float32(elapsed)*a.speed
		if a.offsetY > maxOff {
			a.offsetY = maxOff
		}
		if elapsed >= a.seconds {
			a.advance(now)
		}
	case phaseTeleport:
		a.offsetY = []float32{0.25, 0.5, 0.75, 0.99}[a.teleportStop] * maxOff
		// fill measured below once visible range known
	case phaseTeleportDwell:
		if elapsed >= dwellAfterFillS {
			a.advance(now)
		}
	case phaseDone:
	}

	lo, hi := a.visibleRange(cols, h)
	// prefetch beyond the viewport in scroll direction
	preRows := int((h*prefetchScreens)/cell) + 1
	pb := hi + preRows*cols
	if pb > len(a.entries) {
		pb = len(a.entries)
	}
	a.requestRange(lo, pb)
	a.evictLRU()

	// ----- draw background + visible tiles -----
	paint.FillShape(ops, color.NRGBA{R: 20, G: 20, B: 20, A: 255},
		clip.Rect(image.Rect(0, 0, int(w), int(h))).Op())
	anyBlank := false
	for i := lo; i < hi; i++ {
		row := i / cols
		col := i % cols
		x := float32(col)*cell + 4
		y := float32(row)*cell - a.offsetY + 4
		t := a.tiles[i]
		if t != nil && t.ready {
			t.used = a.frameNo
			scale := float32(tileSide) / float32(t.w)
			if s := float32(tileSide) / float32(t.h); s < scale {
				scale = s
			}
			dw := float32(t.w) * scale
			dh := float32(t.h) * scale
			cx := x + (tileSide-dw)/2
			cy := y + (tileSide-dh)/2
			tr := f32.Affine2D{}.
				Scale(f32.Point{}, f32.Pt(scale, scale)).
				Offset(f32.Pt(cx, cy))
			st := op.Affine(tr).Push(ops)
			cl := clip.Rect(image.Rect(0, 0, t.w, t.h)).Push(ops)
			t.op.Add(ops)
			paint.PaintOp{}.Add(ops)
			cl.Pop()
			st.Pop()
		} else {
			anyBlank = true
			paint.FillShape(ops, color.NRGBA{R: 60, G: 60, B: 60, A: 255},
				clip.Rect(image.Rect(int(x), int(y), int(x+tileSide), int(y+tileSide))).Op())
		}
	}

	// ----- metrics by phase -----
	switch a.phase {
	case phaseColdStart:
		if a.allVisibleReady(lo, hi) {
			a.metrics.coldStartMs = now.Sub(a.start).Seconds() * 1000.0
			fmt.Println("READY")
			a.advance(now)
		}
	case phaseFlickA, phaseFlickB:
		if anyBlank {
			a.metrics.blankTileFrames++
		}
	case phaseTeleport:
		if !a.haveFillStart {
			a.teleportFillStart = now
			a.haveFillStart = true
		} else if a.allVisibleReady(lo, hi) {
			ms := now.Sub(a.teleportFillStart).Seconds() * 1000.0
			a.metrics.fillMs = append(a.metrics.fillMs, ms)
			a.haveFillStart = false
			a.advance(now)
		}
	}

	if a.phase == phaseDone {
		a.finish()
		return true
	}
	return false
}

func run(w *app.Window, a *appState) error {
	var ops op.Ops
	for {
		switch e := w.Event().(type) {
		case app.DestroyEvent:
			return e.Err
		case app.FrameEvent:
			gtx := app.NewContext(&ops, e)
			width := float32(gtx.Constraints.Max.X)
			height := float32(gtx.Constraints.Max.Y)
			done := a.frame(gtx.Ops, width, height)
			e.Frame(gtx.Ops)
			if done {
				os.Exit(0)
			}
			w.Invalidate()
		}
	}
}

func main() {
	start := time.Now()
	var path string
	seconds := 45.0
	speed := float32(2000.0)
	args := os.Args[1:]
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--seconds":
			i++
			v, err := strconv.ParseFloat(args[i], 64)
			if err != nil {
				log.Fatalf("--seconds: %v", err)
			}
			seconds = v
		case "--speed":
			i++
			v, err := strconv.ParseFloat(args[i], 32)
			if err != nil {
				log.Fatalf("--speed: %v", err)
			}
			speed = float32(v)
		default:
			path = args[i]
		}
	}
	if path == "" {
		log.Fatal("usage: balloon-go-gio <fcache> [--seconds S] [--speed PX_S]")
	}
	entries := readCacheIndex(path)
	jobs := make(chan int, len(entries)+64)
	results := make(chan decoded, 256)
	spawnDecoders(path, entries, jobs, results)

	state := &appState{
		entries:    entries,
		jobs:       jobs,
		results:    results,
		tiles:      make(map[int]*tile),
		start:      start,
		phase:      phaseColdStart,
		phaseStart: start,
		seconds:    seconds,
		speed:      speed,
	}

	go func() {
		w := new(app.Window)
		w.Option(
			app.Title("balloon-go-gio"),
			app.Size(unit.Dp(winW), unit.Dp(winH)),
			app.Decorated(false),
		)
		if err := run(w, state); err != nil {
			log.Fatal(err)
		}
		os.Exit(0)
	}()
	app.Main()
}
