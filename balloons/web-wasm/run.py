#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Trial balloon runner: web-wasm (Chromium shell + canvas JS).

Serves the balloon dir and the fcache file over localhost HTTP (with Range
support), launches flatpak Chromium pointed at index.html, and collects the
result JSON the page POSTs back. Prints READY when the page reports cold
start, then exactly one JSON line, and exits 0.

CLI contract: run.py FCACHE_PATH [--seconds S] [--speed PX_S]

Launch strategy: first try a real window on the headless weston compositor
(WAYLAND_DISPLAY=wayland-bench); if Chromium never loads the page (flatpak
usually forwards only the default wayland socket), fall back to
--headless=new and note mode "headless" in the JSON (rAF cadence then comes
from Chromium's internal BeginFrame scheduler, not a compositor).

External measurement: the runner samples summed Pss of the Chromium process
tree (found via the flatpak cgroup scope of the launched pid, else process
descendants) — once when the page POSTs /measure-now (-> vm_rss_mb) and at
1 Hz throughout (peak -> vm_hwm_mb). spawn_to_ready_ms is wall time from
Popen to the page's /ready POST.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BALLOON_DIR = os.path.dirname(os.path.abspath(__file__))
CHROMIUM_LOG = "/tmp/balloon-web-wasm-chromium.log"
PAGE_LOAD_GRACE_S = 25  # time for chromium to start and GET /

STATE = {
    "fcache": None,
    "spawn_t": None,
    "spawn_to_ready_ms": None,
    "rss_at_measure_mb": None,
    "peak_rss_mb": 0.0,
    "result": None,
    "mode": None,
    "chromium_pid": None,
}
PAGE_CONTACTED = threading.Event()
READY = threading.Event()
DONE = threading.Event()
LOCK = threading.Lock()


def log(msg):
    print(f"[run.py] {msg}", file=sys.stderr, flush=True)


# ---------- process-tree PSS measurement ----------

def _cgroup_scope(pid):
    """Return the flatpak app scope path of pid, or None."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            for line in f:
                path = line.strip().split(":", 2)[-1]
                if "org.chromium" in path:
                    return path
    except OSError:
        pass
    return None


def _descendants(root):
    children = {}
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            with open(f"/proc/{p}/stat", "rb") as f:
                stat = f.read()
            rpar = stat.rfind(b")")
            ppid = int(stat[rpar + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(p))
    out = []
    stack = [root]
    while stack:
        pid = stack.pop()
        for c in children.get(pid, []):
            out.append(c)
            stack.append(c)
    return out


def chromium_pids():
    launch_pid = STATE["chromium_pid"]
    if launch_pid is None:
        return []
    scope = _cgroup_scope(launch_pid)
    pids = []
    if scope:
        for p in os.listdir("/proc"):
            if not p.isdigit():
                continue
            try:
                with open(f"/proc/{p}/cgroup") as f:
                    if scope in f.read():
                        pids.append(int(p))
            except OSError:
                continue
    if not pids:
        pids = [launch_pid] + _descendants(launch_pid)
    return pids


def pss_mb(pids):
    total_kb = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/smaps_rollup") as f:
                for line in f:
                    if line.startswith("Pss:"):
                        total_kb += int(line.split()[1])
                        break
        except OSError:
            continue
    return total_kb / 1024.0


def sampler():
    while not DONE.is_set():
        mb = pss_mb(chromium_pids())
        with LOCK:
            if mb > STATE["peak_rss_mb"]:
                STATE["peak_rss_mb"] = mb
        DONE.wait(1.0)


# ---------- HTTP server ----------

class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Chromium aborts idle keep-alive sockets; don't spam tracebacks.
        if isinstance(sys.exception(),
                      (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep stdout/stderr quiet
        pass

    def _serve_file(self, path, ctype):
        try:
            size = os.path.getsize(path)
        except OSError:
            self.send_error(404)
            return
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            s, _, e = spec.partition("-")
            if s:
                start = int(s)
                end = int(e) if e else size - 1
            else:  # suffix form bytes=-N
                start = max(0, size - int(e))
                end = size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            PAGE_CONTACTED.set()
            self._serve_file(os.path.join(BALLOON_DIR, "index.html"),
                             "text/html; charset=utf-8")
        elif path == "/app.js":
            self._serve_file(os.path.join(BALLOON_DIR, "app.js"),
                             "text/javascript; charset=utf-8")
        elif path == "/fcache":
            self._serve_file(STATE["fcache"], "application/octet-stream")
        else:
            self.send_error(404)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _ok(self, body=b"ok"):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self._read_body()
        path = self.path.split("?", 1)[0]
        if path == "/ready":
            with LOCK:
                if STATE["spawn_to_ready_ms"] is None:
                    STATE["spawn_to_ready_ms"] = round(
                        (time.monotonic() - STATE["spawn_t"]) * 1000.0)
                    print("READY", flush=True)
            self._ok()
        elif path == "/measure-now":
            mb = pss_mb(chromium_pids())
            with LOCK:
                STATE["rss_at_measure_mb"] = mb
                if mb > STATE["peak_rss_mb"]:
                    STATE["peak_rss_mb"] = mb
            self._ok()
        elif path == "/result":
            try:
                result = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                result = {"error": f"unparseable /result body: {e}"}
            self._ok()
            with LOCK:
                STATE["result"] = result
            DONE.set()
        else:
            self.send_error(404)


# ---------- Chromium launch ----------

def launch_chromium(mode, url):
    if mode == "wayland":
        cmd = [
            "flatpak", "run",
            "--env=WAYLAND_DISPLAY=wayland-bench",
            "--socket=wayland",
            "--filesystem=xdg-run/wayland-bench",
            "org.chromium.Chromium",
            "--ozone-platform=wayland",
            f"--app={url}",
            "--window-size=1280,800",
            "--user-data-dir=/tmp/balloon-chrome-profile",
            "--no-first-run",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
        ]
    else:  # headless
        cmd = [
            "flatpak", "run",
            "org.chromium.Chromium",
            "--headless=new",
            "--window-size=1280,800",
            "--user-data-dir=/tmp/balloon-chrome-profile-headless",
            "--no-first-run",
            url,
        ]
    env = dict(os.environ)
    env["WAYLAND_DISPLAY"] = "wayland-bench"
    logf = open(CHROMIUM_LOG, "ab")
    logf.write(f"\n===== {mode}: {' '.join(cmd)}\n".encode())
    proc = subprocess.Popen(cmd, stdout=logf, stderr=logf,
                            stdin=subprocess.DEVNULL, env=env)
    return proc


def kill_chromium(proc):
    pids = chromium_pids()
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    except OSError:
        pass
    for pid in pids:  # sweep sandbox leftovers
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fcache")
    ap.add_argument("--seconds", type=float, default=45.0)
    ap.add_argument("--speed", type=float, default=2000.0)
    ap.add_argument("--debug", action="store_true",
                    help="include page-side phase-transition debug in JSON")
    ap.add_argument("--mode", choices=["auto", "wayland", "headless"],
                    default="auto", help="force a Chromium launch mode")
    args = ap.parse_args()

    fcache = os.path.abspath(args.fcache)
    if not os.path.isfile(fcache):
        log(f"fcache not found: {fcache}")
        return 1
    STATE["fcache"] = fcache

    server = QuietServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/?seconds={args.seconds}&speed={args.speed}"
    if args.debug:
        url += "&debug=1"
    log(f"serving on {url}")

    modes = ("wayland", "headless") if args.mode == "auto" else (args.mode,)
    proc = None
    for mode in modes:
        PAGE_CONTACTED.clear()
        STATE["spawn_t"] = time.monotonic()
        proc = launch_chromium(mode, url)
        STATE["chromium_pid"] = proc.pid
        STATE["mode"] = mode
        deadline = time.monotonic() + PAGE_LOAD_GRACE_S
        while time.monotonic() < deadline:
            if PAGE_CONTACTED.wait(0.25):
                break
            if proc.poll() is not None:
                break
        if PAGE_CONTACTED.is_set():
            log(f"page loaded in mode={mode}")
            break
        log(f"mode={mode} failed to load page "
            f"(chromium rc={proc.poll()}); see {CHROMIUM_LOG}")
        kill_chromium(proc)
        proc = None
    if proc is None:
        log("could not get Chromium to load the page in any mode")
        return 1

    threading.Thread(target=sampler, daemon=True).start()

    # cold start + flick A + 4x(fill + dwell) + flick B + generous slack
    budget = 60 + 2 * args.seconds + 4 * (15 + 2) + 30
    if not DONE.wait(budget):
        log(f"no result after {budget:.0f}s; giving up")
        kill_chromium(proc)
        return 1

    with LOCK:
        result = STATE["result"]
        if isinstance(result, dict) and "error" not in result:
            result["vm_rss_mb"] = round(STATE["rss_at_measure_mb"] or 0.0, 1)
            result["vm_hwm_mb"] = round(STATE["peak_rss_mb"], 1)
            result["spawn_to_ready_ms"] = STATE["spawn_to_ready_ms"]
            result["mode"] = STATE["mode"]
    kill_chromium(proc)
    server.shutdown()

    if not isinstance(result, dict) or "error" in result:
        log(f"page reported failure: {result}")
        return 1
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
