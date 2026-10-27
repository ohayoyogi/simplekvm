#!/usr/bin/env python3
"""End-to-end tests of simplekvm against a real target PC.

The target runs scripts/target/kvm-target.py (full-screen test page + HTTP
API); this script runs on the KVM board next to kvm.py, talks to kvm.py
like a browser (WebSocket: video in, keyboard/mouse out) and checks what
arrived on the target, or what the target shows in the video.

    python3 kvm-test.py --target http://TARGET:8800 status
    python3 kvm-test.py --target ... input [--layout us|jis]
    python3 kvm-test.py --target ... video [--seconds 10]
    python3 kvm-test.py --target ... latency [--count 10]
    python3 kvm-test.py --target ... mode [--modes 1920x1080@60,1280x720@60]
    python3 kvm-test.py --target ... audio --alsa-device hw:CARD=X [--freq 1000]
    python3 kvm-test.py --target ... all

Video is decoded with GStreamer on the board (v4l2slh264dec, the hardware
decoder; --decoder avdec_h264 for software). The audio test records on the
board with arecord (the TC358743 I2S input), until kvm.py streams audio.
"""
import argparse
import base64
import json
import os
import random
import re
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# frame counter strip drawn by kvm-target.py (keep in sync with its page)
STRIP, BLOCK_H, BITS = 40, 1 / 25, 16


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------------
# kvm.py WebSocket client
# --------------------------------------------------------------------------

class Kvm:
    def __init__(self, host, port, insecure_http=False):
        sock = socket.create_connection((host, port), timeout=10)
        if not insecure_http:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(("GET /ws HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                      "Sec-WebSocket-Version: 13\r\n\r\n" % (host, key)).encode())
        self.sock = sock
        self.f = sock.makefile("rb")
        status = self.f.readline()
        if b" 101 " not in status:
            raise RuntimeError("kvm.py /ws: %r" % status)
        while self.f.readline() not in (b"\r\n", b""):
            pass
        sock.settimeout(None)
        self.lock = threading.Lock()
        self.msgs = []              # (time, dict) text messages
        self.aus = []               # (time, key, annex-b bytes) while recording
        self.recording = False
        self.frames = 0
        self.size = None            # stream size from hello / size
        self.hello = None
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()
        t0 = time.monotonic()
        while self.hello is None and time.monotonic() - t0 < 10:
            time.sleep(0.05)
        if self.hello is None:
            raise RuntimeError("no hello from kvm.py")

    def _reader(self):
        try:
            while True:
                h = self.f.read(2)
                if len(h) < 2:
                    break
                op, n = h[0] & 15, h[1] & 127
                if n == 126:
                    n = struct.unpack("!H", self.f.read(2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", self.f.read(8))[0]
                payload = self.f.read(n)
                now = time.monotonic()
                if op == 1:
                    m = json.loads(payload)
                    with self.lock:
                        self.msgs.append((now, m))
                        if m.get("t") == "hello":
                            self.hello = m
                            self.size = (m.get("w"), m.get("h"))
                        elif m.get("t") == "size":
                            self.size = (m["w"], m["h"])
                elif op == 2 and payload[:1] == b"\x01":
                    key = payload[1] & 1
                    with self.lock:
                        self.frames += 1
                        # start at a key frame: the decoder drops anything
                        # before one, and each AU must map to one frame
                        if self.recording and (self.aus or key):
                            self.aus.append((now, key, payload[12:]))
                elif op == 8:
                    break
        except (OSError, ValueError):
            pass
        self.alive = False

    def send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else
                               bytes([0x80 | 126]) + struct.pack("!H", n))
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self.lock:
            self.sock.sendall(hdr + mask + body)

    def record(self, on):
        with self.lock:
            if on:
                self.aus = []
            self.recording = on
            return list(self.aus)

    def wait_frames(self, n=1, timeout=10):
        t0 = time.monotonic()
        start = self.frames
        while self.frames - start < n and time.monotonic() - t0 < timeout:
            time.sleep(0.02)
        return self.frames - start >= n

    # input helpers: x, y in 0..1 of the stream
    def move(self, fx, fy, buttons=0):
        w, h = self.size
        self.send({"t": "move", "x": fx * w, "y": fy * h, "buttons": buttons})

    def click(self, fx, fy, button=1):
        w, h = self.size
        self.move(fx, fy)
        time.sleep(0.05)
        self.send({"t": "down", "x": fx * w, "y": fy * h, "b": 0, "buttons": button})
        time.sleep(0.08)
        self.send({"t": "up", "x": fx * w, "y": fy * h, "b": 0, "buttons": 0})

    def key(self, code, hold=0.04):
        self.send({"t": "key", "code": code, "down": True})
        time.sleep(hold)
        self.send({"t": "key", "code": code, "down": False})
        time.sleep(0.03)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# target (kvm-target.py) client
# --------------------------------------------------------------------------

class Target:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def get(self, path, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        try:
            with urllib.request.urlopen(self.url + path + q, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read() or b"{}")

    def events(self, since):
        return self.get("/events", since=since)["events"]


# --------------------------------------------------------------------------
# decoding and the frame counter
# --------------------------------------------------------------------------

def decode(aus, size, gst_dir, decoder):
    """Annex-B access units -> list of GRAY8 frames (bytes, w*h each)."""
    w, h = size
    env = dict(os.environ)
    gst = os.path.expanduser(gst_dir)
    if os.path.isdir(gst):
        env["GST_PLUGIN_PATH"] = os.path.join(gst, "lib", "gstreamer-1.0")
        env["LD_LIBRARY_PATH"] = os.path.join(gst, "lib")
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in.h264"), os.path.join(d, "out.gray")
        with open(src, "wb") as f:
            for _, _, au in aus:
                f.write(au)
        cmd = ["gst-launch-1.0", "-q", "filesrc", "location=" + src, "!", "h264parse", "!",
               decoder, "!", "videoconvert", "!", "videoscale", "!",
               "video/x-raw,format=GRAY8,width=%d,height=%d" % (w, h), "!",
               "filesink", "location=" + dst]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
        if not os.path.exists(dst):
            raise RuntimeError("decode failed: %s" % r.stderr.strip()[-300:])
        data = open(dst, "rb").read()
    n = w * h
    return [data[i:i + n] for i in range(0, len(data) - n + 1, n)]


def luma(frame, size, fx, fy, r=2):
    """Mean luminance around (fx, fy), fractions of the frame."""
    w, h = size
    x, y = int(fx * w), int(fy * h)
    vals = [frame[yy * w + xx] for yy in range(max(0, y - r), min(h, y + r + 1))
            for xx in range(max(0, x - r), min(w, x + r + 1))]
    return sum(vals) / len(vals)


def read_counter(frame, size):
    """The 16-bit frame counter of the test page, or None if not visible."""
    fy = BLOCK_H / 2
    white = luma(frame, size, 0.5 / STRIP, fy)
    black = luma(frame, size, 1.5 / STRIP, fy)
    if white - black < 80:
        return None
    mid = (white + black) / 2
    v = 0
    for i in range(BITS):
        v = (v << 1) | (luma(frame, size, (2.5 + i) / STRIP, fy) > mid)
    return v


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append((name, ok, detail))
        log("%-4s %s%s" % ("PASS" if ok else "FAIL", name, ("  " + detail) if detail else ""))
        return ok

    def summary(self):
        bad = [n for n, ok, _ in self.items if not ok]
        log("%d checks, %d failed%s" % (len(self.items), len(bad),
                                        (": " + ", ".join(bad)) if bad else ""))
        return not bad


def test_status(kvm, tgt, res, a):
    st = tgt.get("/status")
    log("kvm.py:", json.dumps({k: kvm.hello.get(k) for k in ("source", "w", "h", "sensor", "hid")}))
    log("target:", json.dumps(st))
    res.check("target page open", st.get("page"), "(start kvm-target.py on the target)")
    res.check("kvm.py HID connected", bool(kvm.hello.get("hid") and kvm.hello["hid"].get("open")),
              json.dumps(kvm.hello.get("hid")))
    res.check("video arrives", kvm.wait_frames(10), "%dx%d" % tuple(kvm.size))


def test_input(kvm, tgt, res, a):
    st = tgt.get("/status")
    scr = st.get("screen") or {}
    sw, sh = scr.get("w"), scr.get("h")
    if not sw:
        return res.check("input: target screen size known", False)
    seq0 = tgt.get("/events", since=1 << 60)["seq"]

    # absolute mouse: clicks away from the centre (the audio button) and the strip
    pts = [(0.25, 0.5), (0.75, 0.5), (0.5, 0.25), (0.5, 0.8), (0.1, 0.9), (0.9, 0.15)]
    for fx, fy in pts:
        kvm.click(fx, fy)
        time.sleep(0.25)
    time.sleep(0.5)
    downs = [e for e in tgt.events(seq0) if e["t"] == "mousedown"]
    res.check("mouse: clicks arrive", len(downs) == len(pts), "%d of %d" % (len(downs), len(pts)))
    errs = []
    for (fx, fy), e in zip(pts, downs):
        errs.append(max(abs(e["x"] - fx * sw), abs(e["y"] - fy * sh)))
    tol = max(4, 0.01 * sw)
    res.check("mouse: absolute position", errs and max(errs) <= tol,
              "max error %.1f px (tolerance %.0f)" % (max(errs) if errs else -1, tol))

    # keys, by physical position (KeyboardEvent.code), whatever the layout
    seq = tgt.get("/events", since=1 << 60)["seq"]
    codes = ["KeyA", "KeyZ", "Digit1", "Digit0", "Minus", "Space", "Enter", "Backspace",
             "ArrowLeft", "ArrowDown", "F2", "Escape", "Tab", "Comma", "Period", "Slash"]
    for c in codes:
        kvm.key(c)
    kvm.send({"t": "key", "code": "ShiftLeft", "down": True})
    time.sleep(0.05)
    kvm.key("KeyB")
    kvm.send({"t": "key", "code": "ShiftLeft", "down": False})
    kvm.send({"t": "key", "code": "ControlLeft", "down": True})
    time.sleep(0.05)
    kvm.key("KeyC")
    kvm.send({"t": "key", "code": "ControlLeft", "down": False})
    time.sleep(0.8)
    evs = tgt.events(seq)
    got = [e["code"] for e in evs if e["t"] == "keydown" and not e.get("repeat")]
    want = codes + ["ShiftLeft", "KeyB", "ControlLeft", "KeyC"]
    res.check("keyboard: key codes", got == want,
              "" if got == want else "got %s" % got)
    shiftb = [e for e in evs if e["t"] == "keydown" and e["code"] == "KeyB"]
    res.check("keyboard: Shift held", bool(shiftb) and shiftb[0].get("shift"))
    ctrlc = [e for e in evs if e["t"] == "keydown" and e["code"] == "KeyC"]
    res.check("keyboard: Ctrl held", bool(ctrlc) and ctrlc[0].get("ctrl"))
    ups = sum(1 for e in evs if e["t"] == "keyup")
    res.check("keyboard: every key released", ups == len(want), "%d keyup" % ups)

    # text input: kvm.py maps characters for the target's layout (--layout)
    seq = tgt.get("/events", since=1 << 60)["seq"]
    text = "Hello, KVM! 123 @[]:;=~"
    kvm.send({"t": "type", "text": text, "layout": a.layout})
    time.sleep(len(text) * 0.06 + 1.0)
    typed = "".join(e["key"] for e in tgt.events(seq)
                    if e["t"] == "keydown" and len(e.get("key", "")) == 1)
    res.check("keyboard: typed text (%s layout)" % a.layout, typed == text,
              "" if typed == text else "got %r" % typed)

    # wheel: +1 = away from the user = scroll up (negative deltaY)
    seq = tgt.get("/events", since=1 << 60)["seq"]
    w, h = kvm.size
    for d in (1, 1, 1, -1, -1, -1):
        kvm.send({"t": "wheel", "x": 0.3 * w, "y": 0.6 * h, "buttons": 0, "wheel": d})
        time.sleep(0.15)
    time.sleep(0.5)
    dys = [e["dy"] for e in tgt.events(seq) if e["t"] == "wheel"]
    up, down = sum(1 for d in dys if d < 0), sum(1 for d in dys if d > 0)
    res.check("mouse: wheel up and down", up >= 1 and down >= 1, "%d up, %d down" % (up, down))

    # relative (boot) mouse
    kvm.move(0.5, 0.6)
    time.sleep(0.3)
    seq = tgt.get("/events", since=1 << 60)["seq"]
    for _ in range(10):
        kvm.send({"t": "rel", "dx": 20, "dy": 0, "buttons": 0, "wheel": 0})
        time.sleep(0.03)
    time.sleep(0.5)
    moves = [e for e in tgt.events(seq) if e["t"] == "mousemove"]
    dx = (moves[-1]["x"] - moves[0]["x"]) if len(moves) > 1 else 0
    res.check("mouse: relative movement", dx > 20, "moved %+d px right" % dx)


def test_video(kvm, tgt, res, a):
    kvm.wait_frames(5)
    kvm.record(True)
    time.sleep(a.seconds)
    aus = kvm.record(False)
    if len(aus) < 2:
        return res.check("video: frames received", False, "%d" % len(aus))
    el = aus[-1][0] - aus[0][0]                # from the first key frame on
    size = tuple(kvm.size)
    log("video: %d frames in %.1f s (%.1f fps) at %dx%d, decoding" % (len(aus), el, len(aus) / el, *size))
    frames = decode(aus, size, a.gst_dir, a.decoder)
    res.check("video: all frames decode", len(frames) == len(aus), "%d of %d" % (len(frames), len(aus)))
    cnt = [read_counter(f, size) for f in frames]
    seen = [c for c in cnt if c is not None]
    res.check("video: test page visible", len(seen) >= 0.9 * len(cnt), "%d of %d frames" % (len(seen), len(cnt)))
    if len(seen) < 2:
        return
    deltas = [(b - a_) & 0xffff for a_, b in zip(seen, seen[1:])]
    hist = {}
    for d in deltas:
        hist[d] = hist.get(d, 0) + 1
    src_hz = ((seen[-1] - seen[0]) & 0xffff) / el
    log("video: counter steps %s, source refresh about %.1f Hz" %
        (", ".join("%d:%d" % kv for kv in sorted(hist.items())), src_hz))
    repeats = hist.get(0, 0)
    res.check("video: no repeated frames", repeats == 0, "%d repeated" % repeats)
    expected = max(1, round(src_hz / max(1, len(aus) / el)))
    skips = sum(n for d, n in hist.items() if d > expected)
    res.check("video: no dropped frames", skips <= len(deltas) * 0.01,
              "%d steps larger than %d" % (skips, expected))


def test_latency(kvm, tgt, res, a):
    kvm.wait_frames(5)
    kvm.record(True)
    t0 = time.monotonic()
    while not kvm.aus and time.monotonic() - t0 < 5:     # wait for a key frame
        time.sleep(0.02)
    time.sleep(0.2)
    trials = []
    for _ in range(a.count):
        fx, fy = random.uniform(0.15, 0.85), random.uniform(0.25, 0.75)
        if abs(fx - 0.5) < 0.2 and abs(fy - 0.5) < 0.12:
            fy += 0.2                       # stay off the audio button
        kvm.move(fx, fy)
        time.sleep(0.1)
        t = time.monotonic()
        w, h = kvm.size
        kvm.send({"t": "down", "x": fx * w, "y": fy * h, "b": 0, "buttons": 1})
        time.sleep(0.05)
        kvm.send({"t": "up", "x": fx * w, "y": fy * h, "b": 0, "buttons": 0})
        trials.append((t, fx, fy))
        time.sleep(0.7)                     # the square lasts 400 ms
    aus = kvm.record(False)
    size = tuple(kvm.size)
    frames = decode(aus, size, a.gst_dir, a.decoder)
    if len(frames) != len(aus):
        return res.check("latency: frames decode", False, "%d of %d" % (len(frames), len(aus)))
    lats = []
    for t, fx, fy in trials:
        for (ta, _, _), fr in zip(aus, frames):
            if ta >= t and luma(fr, size, fx, fy, 3) > 200:
                lats.append((ta - t) * 1000)
                break
    res.check("latency: every click seen", len(lats) == len(trials), "%d of %d" % (len(lats), len(trials)))
    if lats:
        lats.sort()
        log("latency (click sent -> frame received, without the browser's decode): "
            "min %.0f, median %.0f, max %.0f ms" % (lats[0], lats[len(lats) // 2], lats[-1]))


def fit(w, h, mw=1920, mh=1080):
    if w > mw or h > mh:
        s = min(mw / w, mh / h)
        w, h = int(w * s), int(h * s)
    return w & ~1, h & ~1


def test_mode(kvm, tgt, res, a):
    outs = tgt.get("/modes")
    if "error" in outs or not outs:
        return res.check("mode: xrandr on the target", False, outs.get("error", "no output"))
    out, modes = next(iter(outs.items()))
    cur = next((m for m in modes if m["current"]), None)
    if a.modes:
        want = []
        for s in a.modes.split(","):
            m = re.fullmatch(r"(\d+)x(\d+)(?:@([\d.]+))?", s.strip())
            want.append((int(m.group(1)), int(m.group(2)), m.group(3)))
    else:                                   # a few distinct sizes the target offers
        sizes = []
        for m in modes:
            if (m["w"], m["h"]) not in [(w, h) for w, h, _ in sizes]:
                sizes.append((m["w"], m["h"], None))
        want = sizes[:4]
    if cur:
        want.append((cur["w"], cur["h"], None))    # back to where we started
    log("mode: output %s, testing %s" % (out, ", ".join("%dx%d%s" % (w, h, "@" + r if r else "")
                                                         for w, h, r in want)))
    for w, h, r in want:
        mark = len(kvm.msgs)
        params = {"output": out, "w": w, "h": h}
        if r:
            params["r"] = r
        rep = tgt.get("/mode", **params)
        if not rep.get("ok"):
            res.check("mode %dx%d: set on target" % (w, h), False, rep.get("stderr") or rep.get("error", ""))
            continue
        exp = fit(w, h)
        t0 = time.monotonic()
        while tuple(kvm.size) != exp and time.monotonic() - t0 < 15:
            time.sleep(0.1)
        took = time.monotonic() - t0
        res.check("mode %dx%d: stream follows" % (w, h), tuple(kvm.size) == exp,
                  "%dx%d after %.1f s" % (*kvm.size, took))
        time.sleep(2)
        f0 = kvm.frames
        time.sleep(3)
        fps = (kvm.frames - f0) / 3
        res.check("mode %dx%d: video flows" % (w, h), fps > 5, "%.1f fps" % fps)


def capture_rate():
    """Audio sampling rate the TC358743 reports (v4l2 control), or None."""
    for n in sorted(os.listdir("/sys/class/video4linux")):
        try:
            name = open("/sys/class/video4linux/%s/name" % n).read()
        except OSError:
            continue
        if name.startswith("tc358743"):
            r = subprocess.run(["v4l2-ctl", "-d", "/dev/" + n, "--get-ctrl=audio_sampling_rate"],
                               capture_output=True, text=True)
            m = re.search(r"(\d+)", r.stdout)
            return int(m.group(1)) if m else None
    return None


def analyse(pcm, rate, channels=2):
    """Per channel: (dominant frequency by zero crossings, level in dBFS)."""
    import array
    import math
    s = array.array("h", pcm)
    out = []
    for c in range(channels):
        x = s[c::channels]
        if not x:
            out.append((0.0, -120.0))
            continue
        mean = sum(x) / len(x)
        rms = math.sqrt(sum((v - mean) ** 2 for v in x) / len(x))
        db = 20 * math.log10(max(rms, 1e-3) / 32768)
        # rising crossings with hysteresis (+-30% of the RMS), so noise near
        # zero does not count twice
        thr = rms * 0.3
        crossings, low, first, last = 0, False, None, None
        for i, v in enumerate(x):
            v -= mean
            if v < -thr:
                low = True
            elif v > thr and low:
                low = False
                crossings += 1
                if first is None:
                    first = i
                last = i
        f = (crossings - 1) * rate / (last - first) if crossings > 1 and last > first else 0.0
        out.append((f, db))
    return out


def test_audio(kvm, tgt, res, a):
    st = tgt.get("/status")
    if not st.get("audio"):
        log("audio: enabling audio on the test page (clicking its button through the KVM)")
        kvm.click(0.5, 0.5)
        time.sleep(1.0)
        st = tgt.get("/status")
    if not res.check("audio: test page can play", st.get("audio")):
        return
    rate = a.rate or capture_rate() or 48000
    log("audio: recording %s at %d Hz" % (a.alsa_device, rate))
    for pan, label in ((0, "both"), (-1, "left"), (1, "right")):
        sec = 3
        rec = subprocess.Popen(["arecord", "-q", "-D", a.alsa_device, "-f", "S16_LE", "-c", "2",
                                "-r", str(rate), "-d", str(sec), "-t", "raw"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.3)
        tgt.get("/tone", freq=a.freq, ms=(sec - 0.8) * 1000, gain=0.3, pan=pan)
        pcm, err = rec.communicate(timeout=sec + 10)
        if rec.returncode:
            return res.check("audio: arecord", False, err.decode(errors="replace").strip()[-200:])
        # skip the first 0.5 s (tone start, buffer fill)
        skip = int(rate * 0.5) * 4
        (fl, dl), (fr, dr) = analyse(pcm[skip:], rate)
        log("audio %s: left %.1f Hz %.1f dBFS, right %.1f Hz %.1f dBFS" % (label, fl, dl, fr, dr))
        tol = a.freq * 0.01
        if label == "both":
            res.check("audio: tone in both channels",
                      abs(fl - a.freq) < tol and abs(fr - a.freq) < tol and dl > -40 and dr > -40)
        elif label == "left":
            res.check("audio: left only on the left", abs(fl - a.freq) < tol and dl - dr > 20,
                      "%.1f dB apart" % (dl - dr))
        else:
            res.check("audio: right only on the right", abs(fr - a.freq) < tol and dr - dl > 20,
                      "%.1f dB apart" % (dr - dl))
        time.sleep(0.5)


TESTS = {"status": test_status, "input": test_input, "video": test_video,
         "latency": test_latency, "mode": test_mode, "audio": test_audio}


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="\n".join(__doc__.splitlines()[2:]))
    p.add_argument("test", choices=list(TESTS) + ["all"])
    p.add_argument("--target", required=True, help="kvm-target.py URL, e.g. http://192.0.2.10:8800")
    p.add_argument("--kvm", default="127.0.0.1:8443", help="kvm.py host:port")
    p.add_argument("--http", action="store_true", help="kvm.py runs with --http")
    p.add_argument("--layout", choices=["us", "jis"], default="us", help="target keyboard layout")
    p.add_argument("--seconds", type=float, default=10, help="video test length")
    p.add_argument("--count", type=int, default=10, help="latency test clicks")
    p.add_argument("--modes", default="", help="mode test: WxH[@rate],... (default: what xrandr offers)")
    p.add_argument("--alsa-device", default="", help="audio test: arecord device of the TC358743 I2S input")
    p.add_argument("--rate", type=int, default=0, help="audio test: sampling rate (default: from the TC358743)")
    p.add_argument("--freq", type=float, default=1000, help="audio test: tone frequency")
    p.add_argument("--gst-dir", default="~/h264enc/gst")
    p.add_argument("--decoder", default="v4l2slh264dec", help="GStreamer H.264 decoder element")
    a = p.parse_args()
    if a.test == "audio" and not a.alsa_device:
        p.error("audio needs --alsa-device (see arecord -l)")
    host, _, port = a.kvm.rpartition(":")
    kvm = Kvm(host, int(port), a.http)
    tgt = Target(a.target)
    res = Result()
    names = ["status", "input", "video", "latency", "mode"] if a.test == "all" else [a.test]
    try:
        for n in names:
            log("== %s" % n)
            TESTS[n](kvm, tgt, res, a)
    finally:
        kvm.close()
    sys.exit(0 if res.summary() else 1)


if __name__ == "__main__":
    main()
