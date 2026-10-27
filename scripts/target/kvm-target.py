#!/usr/bin/env python3
"""Test agent for the PC that simplekvm controls (Linux with a desktop).

Shows a full-screen test page in a browser and offers a small HTTP API, so
scripts/kvm-test.py (on the KVM board) can check the whole chain:

  - video: every display refresh draws a frame counter as a row of black and
    white blocks (top edge), so dropped or repeated frames can be counted in
    the received stream; a click draws a white square at the pointer, for
    latency tests
  - input: key and mouse events reaching the page are recorded (GET /events)
  - audio: the page plays test tones on request (GET /tone)
  - display modes: xrandr (X11 sessions) on request (GET /modes, /mode)

    python3 kvm-target.py                  # starts the server and a kiosk browser
    python3 kvm-target.py --no-browser     # open http://localhost:8800/ yourself

Run it from the desktop session (DISPLAY set). Python standard library only.
The API has no authentication: use it on a test network.
"""
import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><title>kvm-target</title>
<style>
  html, body { margin: 0; height: 100%; overflow: hidden; background: #202020; }
  canvas { display: block; width: 100vw; height: 100vh; cursor: crosshair; }
  #audio { position: fixed; left: 50%; top: 50%; width: 360px; height: 120px;
           margin: -60px 0 0 -180px; font: 24px sans-serif; color: #000;
           background: #ffd040; border: 0; border-radius: 8px; }
</style></head><body>
<canvas id="c"></canvas>
<button id="audio" type="button">クリックで音声を有効にする</button>
<script>
// Frame counter strip (see kvm-test.py): STRIP blocks along the top edge,
// each 1/STRIP of the width and BLOCK_H of the height: block 0 white,
// block 1 black (references), blocks 2..17 the counter's 16 bits, MSB first.
const STRIP = 40, BLOCK_H = 1 / 25, BITS = 16;
const cv = document.getElementById('c'), ctx = cv.getContext('2d');
let W = 0, H = 0, n = 0, audio = null;
const clicks = [], keys = [];
let outbox = [];

function resize() {
  W = cv.width = Math.round(window.innerWidth * devicePixelRatio);
  H = cv.height = Math.round(window.innerHeight * devicePixelRatio);
  post({ t: 'screen', w: screen.width, h: screen.height, iw: window.innerWidth,
         ih: window.innerHeight, dpr: devicePixelRatio });
}
window.addEventListener('resize', resize);

function post(ev) { ev.ts = performance.now(); outbox.push(ev); }
setInterval(() => {
  if (!outbox.length) return;
  const body = JSON.stringify(outbox); outbox = [];
  fetch('/ev', { method: 'POST', body, keepalive: true }).catch(() => {});
}, 50);

function draw(now) {
  n = (n + 1) & 0xffff;
  ctx.fillStyle = '#202020'; ctx.fillRect(0, 0, W, H);
  const bw = W / STRIP, bh = Math.round(H * BLOCK_H);
  for (let i = 0; i < 2 + BITS; i++) {
    const on = i === 0 ? 1 : i === 1 ? 0 : (n >> (BITS - 1 - (i - 2))) & 1;
    ctx.fillStyle = on ? '#fff' : '#000';
    ctx.fillRect(Math.round(i * bw), 0, Math.round(bw) + 1, bh);
  }
  // moving bar and the counter in figures, for people watching
  const x = (n * 8) % W;
  ctx.fillStyle = '#3070ff'; ctx.fillRect(x, H * 0.1, Math.max(8, W / 120), H * 0.6);
  ctx.fillStyle = '#ddd'; ctx.font = Math.round(H / 12) + 'px monospace';
  ctx.fillText(String(n).padStart(5, '0'), W * 0.05, H * 0.85);
  ctx.font = Math.round(H / 30) + 'px monospace';
  ctx.fillText(keys.join(' '), W * 0.05, H * 0.95);
  // clicks: white square for 400 ms
  for (let i = clicks.length - 1; i >= 0; i--) {
    const c = clicks[i];
    if (now - c.t > 400) { clicks.splice(i, 1); continue; }
    const s = Math.round(64 * devicePixelRatio);
    ctx.fillStyle = '#fff'; ctx.fillRect(c.x - s / 2, c.y - s / 2, s, s);
  }
  requestAnimationFrame(draw);
}

function mouse(t, e) {
  post({ t, x: e.screenX, y: e.screenY, cx: e.clientX, cy: e.clientY,
         b: e.button, buttons: e.buttons, mx: e.movementX, my: e.movementY });
}
let lastMove = 0;
window.addEventListener('mousedown', e => {
  clicks.push({ x: e.clientX * devicePixelRatio, y: e.clientY * devicePixelRatio,
                t: performance.now() });
  mouse('mousedown', e);
});
window.addEventListener('mouseup', e => mouse('mouseup', e));
window.addEventListener('mousemove', e => {
  const now = performance.now(); if (now - lastMove < 30) return; lastMove = now;
  mouse('mousemove', e);
});
window.addEventListener('wheel', e => {
  e.preventDefault();
  post({ t: 'wheel', dx: e.deltaX, dy: e.deltaY, mode: e.deltaMode });
}, { passive: false });
window.addEventListener('contextmenu', e => e.preventDefault());
for (const t of ['keydown', 'keyup']) {
  window.addEventListener(t, e => {
    if (e.code !== 'F11') e.preventDefault();
    post({ t, code: e.code, key: e.key, repeat: e.repeat, shift: e.shiftKey,
           ctrl: e.ctrlKey, alt: e.altKey, meta: e.metaKey });
    if (t === 'keydown') { keys.push(e.key); if (keys.length > 24) keys.shift(); }
  });
}

// audio: needs one click (browser autoplay policy); kvm-test.py can click
// the button through the KVM, which is a test of its own
const btn = document.getElementById('audio');
function audioOn() {
  btn.remove();
  post({ t: 'audio', rate: audio.sampleRate });
}
btn.addEventListener('click', e => {
  e.stopPropagation();
  if (!audio) audio = new AudioContext();
  audio.resume().then(audioOn);
});
// Chromium started by kvm-target.py may play without a click
try {
  const a = new AudioContext();
  a.resume().then(() => { if (a.state === 'running' && !audio) { audio = a; audioOn(); } });
} catch (e) {}
function tone(freq, ms, gain, pan) {
  if (!audio) return post({ t: 'tone-skipped' });
  const o = audio.createOscillator(), g = audio.createGain(), p = audio.createStereoPanner();
  o.frequency.value = freq; g.gain.value = gain; p.pan.value = pan || 0;
  o.connect(g).connect(p).connect(audio.destination);
  const t0 = audio.currentTime + 0.05;
  o.start(t0); o.stop(t0 + ms / 1000);
  post({ t: 'tone', freq, ms, gain, pan: pan || 0 });
}

const es = new EventSource('/cmd');
es.onmessage = m => {
  const c = JSON.parse(m.data);
  if (c.t === 'tone') tone(c.freq, c.ms, c.gain, c.pan);
  else if (c.t === 'reload') location.reload();
};
setInterval(() => post({ t: 'ping', n, audio: !!audio }), 1000);
resize();
requestAnimationFrame(draw);
</script></body></html>
"""


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = []              # (seq, server time, event)
        self.seq = 0
        self.cmd_queues = []          # one per open /cmd (EventSource)
        self.last = {}                # last event of each type

    def add(self, evs):
        now = time.time()
        with self.lock:
            for ev in evs:
                self.seq += 1
                ev["seq"], ev["time"] = self.seq, now
                self.events.append(ev)
                self.last[ev.get("t")] = ev
            del self.events[:-20000]

    def since(self, seq):
        with self.lock:
            return self.seq, [e for e in self.events if e["seq"] > seq]

    def command(self, cmd):
        with self.lock:
            qs = list(self.cmd_queues)
        for q in qs:
            q.put(cmd)
        return len(qs)


def xrandr_query():
    """Connected outputs and their modes: {output: [{w, h, rates, current}]}."""
    out = subprocess.run(["xrandr", "--query"], capture_output=True, text=True,
                         timeout=10).stdout
    outputs, cur = {}, None
    for line in out.splitlines():
        m = re.match(r"(\S+) connected", line)
        if m:
            cur = m.group(1)
            outputs[cur] = []
            continue
        if not line.startswith(" ") or cur is None:
            if not line.startswith(" "):
                cur = None
            continue
        m = re.match(r"\s+(\d+)x(\d+)\S*\s+(.*)", line)
        if m:
            rates = re.findall(r"([\d.]+)([ *+]*)", m.group(3))
            outputs[cur].append({
                "w": int(m.group(1)), "h": int(m.group(2)),
                "rates": [float(r) for r, _ in rates],
                "current": any("*" in f for _, f in rates)})
    return outputs


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def reply(self, obj, code=200, ctype="application/json"):
            body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/ev":
                return self.reply({"error": "not found"}, 404)
            n = int(self.headers.get("Content-Length", 0))
            try:
                evs = json.loads(self.rfile.read(n))
            except ValueError:
                return self.reply({"error": "bad json"}, 400)
            state.add(evs if isinstance(evs, list) else [evs])
            self.reply({"ok": True})

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
            if u.path == "/":
                return self.reply(PAGE.encode(), ctype="text/html; charset=utf-8")
            if u.path == "/events":
                seq, evs = state.since(int(q.get("since", 0)))
                return self.reply({"seq": seq, "events": evs})
            if u.path == "/status":
                with state.lock:
                    last = dict(state.last)
                ping = last.get("ping")
                return self.reply({
                    "page": bool(ping and time.time() - ping["time"] < 3),
                    "audio": bool(ping and ping.get("audio")),
                    "screen": last.get("screen"), "seq": state.seq,
                    "time": time.time()})
            if u.path == "/tone":
                cmd = {"t": "tone", "freq": float(q.get("freq", 1000)),
                       "ms": float(q.get("ms", 2000)), "gain": float(q.get("gain", 0.3)),
                       "pan": float(q.get("pan", 0))}
                return self.reply({"pages": state.command(cmd), "time": time.time()})
            if u.path == "/reload":
                return self.reply({"pages": state.command({"t": "reload"})})
            if u.path == "/modes":
                try:
                    return self.reply(xrandr_query())
                except (OSError, subprocess.SubprocessError) as e:
                    return self.reply({"error": str(e)}, 500)
            if u.path == "/mode":
                try:
                    outs = xrandr_query()
                    out = q.get("output") or next(iter(outs), None)
                    if out is None:
                        return self.reply({"error": "no connected output"}, 500)
                    cmd = ["xrandr", "--output", out, "--mode", "%sx%s" % (q["w"], q["h"])]
                    if "r" in q:
                        cmd += ["--rate", q["r"]]
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                    return self.reply({"ok": r.returncode == 0, "cmd": cmd,
                                       "stderr": r.stderr.strip(), "time": time.time()},
                                      200 if r.returncode == 0 else 500)
                except KeyError:
                    return self.reply({"error": "need w and h"}, 400)
                except (OSError, subprocess.SubprocessError) as e:
                    return self.reply({"error": str(e)}, 500)
            if u.path == "/cmd":                       # Server-Sent Events
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                cq = queue.Queue()
                with state.lock:
                    state.cmd_queues.append(cq)
                try:
                    while True:
                        try:
                            c = cq.get(timeout=15)
                            self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except OSError:
                    pass
                finally:
                    with state.lock:
                        state.cmd_queues.remove(cq)
                return
            self.reply({"error": "not found"}, 404)
    return H


def start_browser(url):
    """Chromium/Chrome in kiosk mode (full screen, no UI), else Firefox."""
    for exe in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if shutil.which(exe):
            prof = os.path.expanduser("~/.cache/kvm-target-browser")
            return subprocess.Popen([exe, "--kiosk", "--no-first-run",
                                     "--autoplay-policy=no-user-gesture-required",
                                     "--user-data-dir=" + prof, url])
    if shutil.which("firefox"):
        return subprocess.Popen(["firefox", "--kiosk", url])
    print("no chromium/chrome/firefox found; open %s full screen yourself" % url)
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=8800)
    p.add_argument("--no-browser", action="store_true", help="do not start a browser")
    a = p.parse_args()
    state = State()
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(state))
    srv.daemon_threads = True
    url = "http://localhost:%d/" % a.port
    print("kvm-target on port %d; test page %s" % (a.port, url))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    browser = None if a.no_browser else start_browser(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        if browser is not None:
            browser.terminate()
        srv.shutdown()


if __name__ == "__main__":
    main()
