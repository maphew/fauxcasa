// Trial balloon: web-wasm (canvas + plain JS in a Chromium shell).
// Mirrors balloons/rust-egui/src/main.rs: same phase machine, same metric
// definitions, prefetch + bounded LRU. fcache is read over localhost HTTP
// with Range requests; decode via createImageBitmap (off-main-thread in
// Chromium). The page POSTs /ready at cold-start, /measure-now right before
// finishing (runner samples the Chromium process tree PSS), and /result
// with the contract JSON (vm_rss_mb left 0 here; runner injects real value).
'use strict';

const CELL = 168;
const TILE = 160;
const CACHE_CAP = 1200;       // bounded decoded-bitmap cache; 1200 * 256*256*4 B ~ 300 MB worst case
const PREFETCH_SCREENS = 2;   // prefetch ~2 screens ahead of the viewport
const DWELL_MS = 2000;
const MAX_INFLIGHT = 32;      // per-thumb Range fetches batched ~32 at a time

const params = new URLSearchParams(location.search);
const SECONDS = parseFloat(params.get('seconds') || '45');
const SPEED = parseFloat(params.get('speed') || '2000');
const DEBUG = params.get('debug') === '1';
const transitions = []; // {to, ts (rAF clock), now (performance.now)} when DEBUG

const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d', { alpha: false });

// ---- fcache index ----
let N = 0;
let offsets = null; // Float64-safe Numbers (file sizes << 2^53)
let lengths = null;

async function loadIndex() {
  const hr = await fetch('/fcache', { headers: { Range: 'bytes=0-15' } });
  const hb = new Uint8Array(await hr.arrayBuffer());
  if (hb[0] !== 0x46 || hb[1] !== 0x43 || hb[2] !== 0x54 || hb[3] !== 0x43) {
    throw new Error('bad magic (want FCTC)');
  }
  const hdv = new DataView(hb.buffer);
  const version = hdv.getUint32(4, true);
  if (version !== 1) throw new Error('bad version ' + version);
  N = hdv.getUint32(8, true);
  const ir = await fetch('/fcache', {
    headers: { Range: `bytes=16-${16 + N * 16 - 1}` },
  });
  const idv = new DataView(await ir.arrayBuffer());
  offsets = new Float64Array(N);
  lengths = new Uint32Array(N);
  for (let i = 0; i < N; i++) {
    offsets[i] = Number(idv.getBigUint64(i * 16, true));
    lengths[i] = idv.getUint32(i * 16 + 8, true);
    // u16 width/height at +12/+14 unused; ImageBitmap knows its size.
  }
}

// ---- tile cache: idx -> {state:'queued'|'ready'|'failed', bmp?, used} ----
const tiles = new Map();
let readyCount = 0;
const pending = [];
let inflight = 0;
let frameNo = 0;

function requestRange(a, b) {
  for (let i = a; i < b; i++) {
    if (!tiles.has(i)) {
      tiles.set(i, { state: 'queued' });
      pending.push(i);
    }
  }
  pump();
}

function pump() {
  while (inflight < MAX_INFLIGHT && pending.length > 0) {
    const idx = pending.shift();
    const t = tiles.get(idx);
    if (!t || t.state !== 'queued') continue;
    inflight++;
    fetchDecode(idx)
      .catch(() => {
        const u = tiles.get(idx);
        if (u && u.state === 'queued') tiles.set(idx, { state: 'failed' });
      })
      .finally(() => {
        inflight--;
        pump();
      });
  }
}

async function fetchDecode(idx) {
  const off = offsets[idx];
  const len = lengths[idx];
  const r = await fetch('/fcache', {
    headers: { Range: `bytes=${off}-${off + len - 1}` },
  });
  const buf = await r.arrayBuffer();
  const bmp = await createImageBitmap(new Blob([buf], { type: 'image/jpeg' }));
  const t = tiles.get(idx);
  if (t && t.state === 'queued') {
    tiles.set(idx, { state: 'ready', bmp, used: frameNo });
    readyCount++;
  } else {
    bmp.close(); // evicted (or stale) while in flight
  }
}

function evictLru() {
  if (readyCount <= CACHE_CAP) return;
  const aged = [];
  for (const [i, t] of tiles) {
    if (t.state === 'ready') aged.push([i, t.used]);
  }
  aged.sort((x, y) => x[1] - y[1]);
  let toEvict = readyCount - CACHE_CAP;
  for (const [i] of aged) {
    if (toEvict <= 0) break;
    tiles.get(i).bmp.close();
    tiles.delete(i);
    readyCount--;
    toEvict--;
  }
}

function allVisibleReady(a, b) {
  for (let i = a; i < b; i++) {
    const t = tiles.get(i);
    if (!t || t.state !== 'ready') return false;
  }
  return true;
}

// ---- phase machine (mirrors rust-egui) ----
// cold -> flickA -> (teleport -> dwell) x4 -> flickB -> done
let phase = 'cold';
let tIdx = 0;
let phaseStart = 0;
let offsetY = 0;
let lastTs = null;
let fillStart = null;
let finished = false;

const frameMs = [];
const fillMs = [];
let blankFrames = 0;
let coldStartMs = 0;

function advance(ts) {
  if (phase === 'cold') phase = 'flickA';
  else if (phase === 'flickA') { phase = 'teleport'; tIdx = 0; }
  else if (phase === 'teleport') phase = 'dwell';
  else if (phase === 'dwell') {
    if (tIdx < 3) { tIdx++; phase = 'teleport'; }
    else phase = 'flickB';
  } else if (phase === 'flickB') phase = 'done';
  phaseStart = ts;
  fillStart = null;
  if (DEBUG) {
    transitions.push({
      to: phase,
      ts: Math.round(ts),
      now: Math.round(performance.now()),
    });
  }
}

function frame(ts) {
  frameNo++;
  const dt = lastTs === null ? null : ts - lastTs;
  lastTs = ts;

  // The interval just measured belongs to the phase that was active while
  // the frame was produced — record it BEFORE the state machine advances,
  // so a stall landing exactly on a phase deadline is not dropped.
  if ((phase === 'flickA' || phase === 'flickB') && dt !== null) {
    frameMs.push(dt);
  }

  const W = canvas.width;
  const H = canvas.height;
  const cols = Math.max(1, Math.floor(W / CELL));
  const rows = Math.ceil(N / cols);
  const total = rows * CELL;
  const maxOff = Math.max(0, total - H);
  const elapsed = ts - phaseStart;

  // phase drives the offset
  if (phase === 'flickA' || phase === 'flickB') {
    const anchor = phase === 'flickA' ? 0 : 0.5 * maxOff;
    offsetY = Math.min(anchor + (elapsed / 1000) * SPEED, maxOff);
    if (elapsed >= SECONDS * 1000) advance(ts);
  } else if (phase === 'teleport') {
    offsetY = [0.25, 0.5, 0.75, 0.99][tIdx] * maxOff;
  } else if (phase === 'dwell') {
    if (elapsed >= DWELL_MS) advance(ts);
  }

  // visible range + prefetch
  const firstRow = Math.max(0, Math.floor(offsetY / CELL));
  const lastRow = Math.ceil((offsetY + H) / CELL);
  const a = Math.min(firstRow * cols, N);
  const b = Math.min((lastRow + 1) * cols, N);
  const preRows = Math.ceil((H * PREFETCH_SCREENS) / CELL);
  const pb = Math.min(b + preRows * cols, N);
  requestRange(a, pb);
  evictLru();

  // draw visible tiles
  ctx.fillStyle = '#141414';
  ctx.fillRect(0, 0, W, H);
  let anyBlank = false;
  for (let i = a; i < b; i++) {
    const row = Math.floor(i / cols);
    const col = i % cols;
    const x = col * CELL + 4;
    const y = row * CELL - offsetY + 4;
    const t = tiles.get(i);
    if (t && t.state === 'ready') {
      t.used = frameNo;
      const s = Math.min(TILE / t.bmp.width, TILE / t.bmp.height);
      const dw = t.bmp.width * s;
      const dh = t.bmp.height * s;
      ctx.drawImage(t.bmp, x + (TILE - dw) / 2, y + (TILE - dh) / 2, dw, dh);
    } else {
      anyBlank = true;
      ctx.fillStyle = '#3c3c3c';
      ctx.fillRect(x, y, TILE, TILE);
    }
  }

  // metrics by phase
  if (phase === 'cold') {
    if (allVisibleReady(a, b)) {
      coldStartMs = performance.now();
      fetch('/ready', { method: 'POST' }).catch(() => {});
      advance(ts);
    }
  } else if (phase === 'flickA' || phase === 'flickB') {
    if (anyBlank) blankFrames++;
  } else if (phase === 'teleport') {
    if (fillStart === null) {
      fillStart = ts;
    } else if (allVisibleReady(a, b)) {
      fillMs.push(ts - fillStart);
      fillStart = null;
      advance(ts);
    }
  }

  if (phase === 'done' && !finished) {
    finished = true;
    finish();
    return;
  }
  requestAnimationFrame(frame);
}

async function finish() {
  const sorted = frameMs.slice().sort((x, y) => x - y);
  const pct = (p) =>
    sorted.length === 0
      ? 0
      : sorted[Math.min(Math.floor(sorted.length * p), sorted.length - 1)];
  const r2 = (x) => Math.round(x * 100) / 100;
  const result = {
    candidate: 'web-wasm',
    photos: N,
    cold_start_ms: Math.round(coldStartMs),
    frames: frameMs.length,
    p50_ms: r2(pct(0.5)),
    p99_ms: r2(pct(0.99)),
    max_ms: r2(sorted.length ? sorted[sorted.length - 1] : 0),
    blank_tile_frames: blankFrames,
    fill_ms: fillMs.map((f) => Math.round(f)),
    vm_rss_mb: 0.0, // pages can't see /proc; runner injects the real value
    vm_hwm_mb: 0.0,
  };
  if (DEBUG) result.debug_transitions = transitions;
  try {
    // Let the runner sample the Chromium process tree while bitmaps are live.
    await fetch('/measure-now', { method: 'POST' });
  } catch (e) { /* measurement is runner's problem; keep going */ }
  await fetch('/result', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(result),
  });
  document.title = 'DONE';
}

function fatal(msg) {
  fetch('/result', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ error: String(msg) }),
  }).catch(() => {});
}

window.addEventListener('error', (e) => fatal(e.message || e));
window.addEventListener('unhandledrejection', (e) => fatal(e.reason || e));

(async () => {
  try {
    await loadIndex();
    requestAnimationFrame(frame);
  } catch (e) {
    fatal(e && e.stack ? e.stack : e);
  }
})();
