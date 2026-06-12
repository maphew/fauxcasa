// Trial balloon: Rust + eframe/egui. See balloons/README.md for the
// fixed task, the fcache format, and the output contract.

use std::collections::HashMap;
use std::fs::File;
use std::io::Read;
use std::path::PathBuf;
use std::sync::mpsc;
use std::sync::Arc;
use std::time::Instant;

const CELL: f32 = 168.0;
const TILE: f32 = 160.0;
const WIN: (f32, f32) = (1280.0, 800.0);
const TEXTURE_CAP: usize = 1200; // ~250 MB worst case at 256px RGBA
const PREFETCH_SCREENS: f32 = 2.0;
const DWELL_AFTER_FILL_S: f64 = 2.0;

struct Entry {
    offset: u64,
    len: u32,
}

struct Cache {
    file: PathBuf,
    entries: Vec<Entry>,
}

fn read_cache_index(path: &PathBuf) -> Cache {
    let mut f = File::open(path).expect("open fcache");
    let mut header = [0u8; 16];
    f.read_exact(&mut header).expect("header");
    assert_eq!(&header[0..4], b"FCTC", "bad magic");
    let version = u32::from_le_bytes(header[4..8].try_into().unwrap());
    assert_eq!(version, 1, "bad version");
    let count = u32::from_le_bytes(header[8..12].try_into().unwrap()) as usize;
    let mut index = vec![0u8; count * 16];
    f.read_exact(&mut index).expect("index");
    let entries = (0..count)
        .map(|i| {
            let r = &index[i * 16..i * 16 + 16];
            Entry {
                offset: u64::from_le_bytes(r[0..8].try_into().unwrap()),
                len: u32::from_le_bytes(r[8..12].try_into().unwrap()),
            }
        })
        .collect();
    Cache {
        file: path.clone(),
        entries,
    }
}

#[cfg(unix)]
fn read_at(f: &File, buf: &mut [u8], offset: u64) {
    use std::os::unix::fs::FileExt;
    f.read_exact_at(buf, offset).expect("read_at");
}

#[cfg(windows)]
fn read_at(f: &File, buf: &mut [u8], offset: u64) {
    use std::os::windows::fs::FileExt;
    let mut done = 0usize;
    while done < buf.len() {
        let n = f
            .seek_read(&mut buf[done..], offset + done as u64)
            .expect("seek_read");
        done += n;
    }
}

enum TileState {
    Queued,
    Ready(egui::TextureHandle, u64), // last-used frame number for LRU
}

struct DecodeJob {
    idx: usize,
}

struct Decoded {
    idx: usize,
    image: egui::ColorImage,
}

fn spawn_decoders(
    cache: Arc<Cache>,
    rx: mpsc::Receiver<DecodeJob>,
    tx: mpsc::Sender<Decoded>,
    workers: usize,
) -> mpsc::Sender<DecodeJob> {
    // fan a single job queue out to N workers, each with its own File
    let (job_tx, fan_rx) = mpsc::channel::<DecodeJob>();
    drop(rx); // caller passes a fresh channel; we build our own fan-out
    let fan_rx = Arc::new(std::sync::Mutex::new(fan_rx));
    for _ in 0..workers {
        let cache = cache.clone();
        let fan_rx = fan_rx.clone();
        let tx = tx.clone();
        let f = File::open(&cache.file).expect("worker open");
        std::thread::spawn(move || loop {
            let job = match fan_rx.lock().unwrap().recv() {
                Ok(j) => j,
                Err(_) => return,
            };
            let e = &cache.entries[job.idx];
            let mut buf = vec![0u8; e.len as usize];
            read_at(&f, &mut buf, e.offset);
            let img = match image::load_from_memory_with_format(
                &buf,
                image::ImageFormat::Jpeg,
            ) {
                Ok(i) => i.to_rgba8(),
                Err(_) => continue,
            };
            let (w, h) = (img.width() as usize, img.height() as usize);
            let color = egui::ColorImage::from_rgba_unmultiplied([w, h], &img);
            if tx.send(Decoded {
                idx: job.idx,
                image: color,
            })
            .is_err()
            {
                return;
            }
        });
    }
    job_tx
}

#[derive(Debug, Clone, Copy, PartialEq)]
enum Phase {
    ColdStart,
    FlickA,
    Teleport(usize),       // which stop (0..4)
    TeleportDwell(usize),  // after fill, dwell
    FlickB,
    Done,
}

struct Metrics {
    cold_start_ms: f64,
    frame_ms: Vec<f64>,
    blank_tile_frames: u64,
    fill_ms: Vec<f64>,
}

struct App {
    cache: Arc<Cache>,
    job_tx: mpsc::Sender<DecodeJob>,
    done_rx: mpsc::Receiver<Decoded>,
    tiles: HashMap<usize, TileState>,
    start: Instant,
    phase: Phase,
    phase_start: Instant,
    teleport_fill_start: Option<Instant>,
    last_frame: Option<Instant>,
    frame_no: u64,
    offset_y: f32,
    metrics: Metrics,
    seconds: f64,
    speed: f32,
    finished: bool,
}

impl App {
    fn cols(&self, width: f32) -> usize {
        (width / CELL).floor().max(1.0) as usize
    }

    fn total_height(&self, cols: usize) -> f32 {
        let rows = (self.cache.entries.len() + cols - 1) / cols;
        rows as f32 * CELL
    }

    fn visible_range(&self, cols: usize, height: f32) -> (usize, usize) {
        let first_row = (self.offset_y / CELL).floor().max(0.0) as usize;
        let last_row = ((self.offset_y + height) / CELL).ceil() as usize;
        let a = first_row * cols;
        let b = ((last_row + 1) * cols).min(self.cache.entries.len());
        (a.min(b), b)
    }

    fn pump_decoded(&mut self, ctx: &egui::Context) {
        while let Ok(d) = self.done_rx.try_recv() {
            let tex = ctx.load_texture(
                format!("t{}", d.idx),
                d.image,
                egui::TextureOptions::LINEAR,
            );
            self.tiles.insert(d.idx, TileState::Ready(tex, self.frame_no));
        }
    }

    fn request_range(&mut self, a: usize, b: usize) {
        for i in a..b {
            if !self.tiles.contains_key(&i) {
                self.tiles.insert(i, TileState::Queued);
                let _ = self.job_tx.send(DecodeJob { idx: i });
            }
        }
    }

    fn evict_lru(&mut self) {
        let ready: usize = self
            .tiles
            .values()
            .filter(|t| matches!(t, TileState::Ready(..)))
            .count();
        if ready <= TEXTURE_CAP {
            return;
        }
        let mut aged: Vec<(usize, u64)> = self
            .tiles
            .iter()
            .filter_map(|(i, t)| match t {
                TileState::Ready(_, used) => Some((*i, *used)),
                _ => None,
            })
            .collect();
        aged.sort_by_key(|(_, used)| *used);
        for (i, _) in aged.into_iter().take(ready - TEXTURE_CAP) {
            self.tiles.remove(&i);
        }
    }

    fn all_visible_ready(&self, a: usize, b: usize) -> bool {
        (a..b).all(|i| matches!(self.tiles.get(&i), Some(TileState::Ready(..))))
    }

    fn finish(&mut self) {
        let mut sorted = self.metrics.frame_ms.clone();
        sorted.sort_by(|x, y| x.partial_cmp(y).unwrap());
        let pct = |p: f64| -> f64 {
            if sorted.is_empty() {
                return 0.0;
            }
            sorted[(((sorted.len() as f64) * p).floor() as usize)
                .min(sorted.len() - 1)]
        };
        let (rss, hwm) = read_rss_mb();
        let fills = self
            .metrics
            .fill_ms
            .iter()
            .map(|f| format!("{:.0}", f))
            .collect::<Vec<_>>()
            .join(", ");
        println!(
            "{{\"candidate\": \"rust-egui\", \"photos\": {}, \
             \"cold_start_ms\": {:.0}, \"frames\": {}, \"p50_ms\": {:.2}, \
             \"p99_ms\": {:.2}, \"max_ms\": {:.2}, \"blank_tile_frames\": {}, \
             \"fill_ms\": [{}], \"vm_rss_mb\": {:.1}, \"vm_hwm_mb\": {:.1}}}",
            self.cache.entries.len(),
            self.metrics.cold_start_ms,
            self.metrics.frame_ms.len(),
            pct(0.50),
            pct(0.99),
            sorted.last().copied().unwrap_or(0.0),
            self.metrics.blank_tile_frames,
            fills,
            rss,
            hwm,
        );
    }
}

#[cfg(target_os = "linux")]
fn read_rss_mb() -> (f64, f64) {
    let status = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    let grab = |key: &str| -> f64 {
        status
            .lines()
            .find(|l| l.starts_with(key))
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|v| v.parse::<f64>().ok())
            .map(|kb| kb / 1024.0)
            .unwrap_or(0.0)
    };
    (grab("VmRSS:"), grab("VmHWM:"))
}

#[cfg(not(target_os = "linux"))]
fn read_rss_mb() -> (f64, f64) {
    (0.0, 0.0)
}

impl eframe::App for App {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        let now = Instant::now();
        let frame_ms = self
            .last_frame
            .map(|t| now.duration_since(t).as_secs_f64() * 1000.0);
        self.last_frame = Some(now);
        self.frame_no += 1;

        self.pump_decoded(ctx);

        egui::CentralPanel::default()
            .frame(egui::Frame::NONE.fill(egui::Color32::from_gray(20)))
            .show(ctx, |ui| {
                let rect = ui.max_rect();
                let (w, h) = (rect.width(), rect.height());
                let cols = self.cols(w);
                let total = self.total_height(cols);
                let max_off = (total - h).max(0.0);

                // ----- phase state machine drives the offset -----
                let elapsed = now.duration_since(self.phase_start).as_secs_f64();
                match self.phase {
                    Phase::ColdStart => {}
                    Phase::FlickA | Phase::FlickB => {
                        self.offset_y = (self.phase_anchor(max_off)
                            + (elapsed as f32) * self.speed)
                            .min(max_off);
                        if elapsed >= self.seconds {
                            self.advance(now, max_off);
                        }
                    }
                    Phase::Teleport(k) => {
                        self.offset_y = [0.25f32, 0.5, 0.75, 0.99][k] * max_off;
                        // fill measured below once visible range known
                    }
                    Phase::TeleportDwell(_) => {
                        if elapsed >= DWELL_AFTER_FILL_S {
                            self.advance(now, max_off);
                        }
                    }
                    Phase::Done => {}
                }

                let (a, b) = self.visible_range(cols, h);
                // prefetch beyond the viewport in scroll direction
                let pre_rows = ((h * PREFETCH_SCREENS) / CELL).ceil() as usize;
                let pb = (b + pre_rows * cols).min(self.cache.entries.len());
                self.request_range(a, pb);
                self.evict_lru();

                // ----- draw visible tiles -----
                let painter = ui.painter();
                let mut any_blank = false;
                for i in a..b {
                    let row = i / cols;
                    let col = i % cols;
                    let x = rect.left() + col as f32 * CELL + 4.0;
                    let y = rect.top() + row as f32 * CELL - self.offset_y + 4.0;
                    let cell =
                        egui::Rect::from_min_size(egui::pos2(x, y), egui::vec2(TILE, TILE));
                    match self.tiles.get_mut(&i) {
                        Some(TileState::Ready(tex, used)) => {
                            *used = self.frame_no;
                            let ts = tex.size_vec2();
                            let scale = (TILE / ts.x).min(TILE / ts.y);
                            let size = ts * scale;
                            let inner = egui::Rect::from_center_size(cell.center(), size);
                            painter.image(
                                tex.id(),
                                inner,
                                egui::Rect::from_min_max(
                                    egui::pos2(0.0, 0.0),
                                    egui::pos2(1.0, 1.0),
                                ),
                                egui::Color32::WHITE,
                            );
                        }
                        _ => {
                            any_blank = true;
                            painter.rect_filled(cell, 2.0, egui::Color32::from_gray(60));
                        }
                    }
                }

                // ----- metrics by phase -----
                match self.phase {
                    Phase::ColdStart => {
                        if self.all_visible_ready(a, b) {
                            self.metrics.cold_start_ms =
                                now.duration_since(self.start).as_secs_f64() * 1000.0;
                            println!("READY");
                            self.advance(now, max_off);
                        }
                    }
                    Phase::FlickA | Phase::FlickB => {
                        if let Some(ms) = frame_ms {
                            self.metrics.frame_ms.push(ms);
                        }
                        if any_blank {
                            self.metrics.blank_tile_frames += 1;
                        }
                    }
                    Phase::Teleport(_) => {
                        if self.teleport_fill_start.is_none() {
                            self.teleport_fill_start = Some(now);
                        } else if self.all_visible_ready(a, b) {
                            let ms = now
                                .duration_since(self.teleport_fill_start.unwrap())
                                .as_secs_f64()
                                * 1000.0;
                            self.metrics.fill_ms.push(ms);
                            self.teleport_fill_start = None;
                            self.advance(now, max_off);
                        }
                    }
                    _ => {}
                }

                if self.phase == Phase::Done && !self.finished {
                    self.finished = true;
                    self.finish();
                    ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                }
            });
        ctx.request_repaint();
    }
}

impl App {
    fn phase_anchor(&self, max_off: f32) -> f32 {
        match self.phase {
            Phase::FlickA => 0.0,
            Phase::FlickB => 0.5 * max_off,
            _ => self.offset_y,
        }
    }

    fn advance(&mut self, now: Instant, _max_off: f32) {
        self.phase = match self.phase {
            Phase::ColdStart => Phase::FlickA,
            Phase::FlickA => Phase::Teleport(0),
            Phase::Teleport(k) => Phase::TeleportDwell(k),
            Phase::TeleportDwell(k) if k < 3 => Phase::Teleport(k + 1),
            Phase::TeleportDwell(_) => Phase::FlickB,
            Phase::FlickB => Phase::Done,
            Phase::Done => Phase::Done,
        };
        self.phase_start = now;
        self.teleport_fill_start = None;
        if std::env::var_os("BALLOON_DEBUG").is_some() {
            eprintln!("phase -> {:?}", self.phase);
        }
    }
}

fn main() -> eframe::Result {
    let start = Instant::now();
    let args: Vec<String> = std::env::args().collect();
    let mut path = None;
    let mut seconds = 45.0f64;
    let mut speed = 2000.0f32;
    let mut it = args.iter().skip(1);
    while let Some(a) = it.next() {
        match a.as_str() {
            "--seconds" => seconds = it.next().expect("val").parse().unwrap(),
            "--speed" => speed = it.next().expect("val").parse().unwrap(),
            other => path = Some(PathBuf::from(other)),
        }
    }
    let path = path.expect("usage: balloon-rust-egui <fcache> [--seconds S] [--speed PX_S]");
    let cache = Arc::new(read_cache_index(&path));
    let (done_tx, done_rx) = mpsc::channel();
    let (dummy_tx, dummy_rx) = mpsc::channel();
    drop(dummy_tx);
    let job_tx = spawn_decoders(cache.clone(), dummy_rx, done_tx, 4);

    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([WIN.0, WIN.1])
            .with_resizable(false),
        vsync: true,
        ..Default::default()
    };
    eframe::run_native(
        "balloon-rust-egui",
        options,
        Box::new(move |cc| {
            if let Some(gl) = &cc.gl {
                use eframe::glow::HasContext;
                eprintln!("renderer: {}", unsafe {
                    gl.get_parameter_string(eframe::glow::RENDERER)
                });
            }
            Ok(Box::new(App {
                cache,
                job_tx,
                done_rx,
                tiles: HashMap::new(),
                start,
                phase: Phase::ColdStart,
                phase_start: start,
                teleport_fill_start: None,
                last_frame: None,
                frame_no: 0,
                offset_y: 0.0,
                metrics: Metrics {
                    cold_start_ms: 0.0,
                    frame_ms: Vec::new(),
                    blank_tile_frames: 0,
                    fill_ms: Vec::new(),
                },
                seconds,
                speed,
                finished: false,
            }))
        }),
    )
}
