#!/usr/bin/env python3
"""Minimal LAN KVM prototype (Python standard library only).

Video comes from a UVC capture stick (MJPEG, decoded on the CPU) or from a
built-in test screen (an NV12 frame buffer rendered here). Either way it is
encoded to H.264 by the RK3288 VPU through GStreamer (v4l2slh264enc),
streamed to the browser over WebSocket and decoded there with WebCodecs.
There is no HID sink yet: mouse input from the browser is applied to the
test screen, or (capture + --fb-echo) drawn on this board's own HDMI
output, so the input -> frame -> encode -> display loop can be exercised
and its latency measured.

    python3 kvm.py [--source auto|capture|csi|screen] [--device /dev/v4l/by-id/...]
                   [--fb-echo /dev/fb0] [--port 8443]
                   [--width 1280 --height 720 --fps 30 --capture-fps 60]
                   [--qp 28 | --bitrate 4000000] [--keyint 30]
                   [--gst-dir ~/h264enc/gst] [--http]

--source auto uses a UVC capture stick when one is plugged in (MJPEG ->
jpegdec -> VPU), then a CSI-2 source on the RK3288 ISP (OV5647 camera or
TC358743 / C790 HDMI bridge: NV12 from the ISP -> VPU, no CPU decode; needs
the kernel and GStreamer patches of the rk3288-mainline-media repository),
otherwise the built-in test screen. --fb-echo draws
browser clicks on the board's own HDMI output for loopback latency tests.

Open https://<board>:8443/ in Chrome/Edge (accept the self-signed
certificate once). WebCodecs only runs in a secure context, so plain
--http works only through localhost, e.g. "ssh -L 8080:localhost:8080".

Later stages swap the two ends: the screen buffer for a UVC / C790 capture
source, and ScreenInput for a HID sink (RP2350 over UART or a USB gadget).
"""

import argparse
import base64
import collections
import hashlib
import http.server
import json
import os
import queue
import re
import signal
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import threading
import time

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
RTP_PORT = 5004


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Screen buffer (NV12)
# --------------------------------------------------------------------------

def rgb_to_yuv(r, g, b):
    """BT.601 limited range."""
    y = 16 + (65.738 * r + 129.057 * g + 25.064 * b) / 256
    u = 128 + (-37.945 * r - 74.494 * g + 112.439 * b) / 256
    v = 128 + (112.439 * r - 94.154 * g - 18.285 * b) / 256
    return int(round(y)), int(round(u)), int(round(v))


# 7-segment layout: a, b, c, d, e, f, g
SEGMENTS = {
    "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc",
    "5": "afgcd", "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abcdfg",
    "-": "g", " ": "",
}


class Screen:
    """A fake desktop: paint canvas, palette, clear button, clock, counter."""

    PALETTE = [(255, 255, 255), (255, 80, 80), (80, 220, 80),
               (80, 140, 255), (255, 210, 60), (20, 20, 20)]

    def __init__(self, width, height):
        self.w, self.h = width, height
        self.buf = bytearray(width * height * 3 // 2)
        self.y_size = width * height
        self.brush = 8
        self.color = 1
        self.last_pt = None
        self.clock_text = ""
        bar = 64
        self.bar_h = bar
        self.canvas = (0, bar, width, height - bar)
        self.swatches = [(16 + i * 56, 12, 40, 40) for i in range(len(self.PALETTE))]
        self.clear_btn = (16 + len(self.PALETTE) * 56 + 16, 12, 96, 40)
        self.reset()

    # -- primitives ---------------------------------------------------------
    def fill_rect(self, x, y, w, h, rgb):
        x0, y0 = max(0, x) & ~1, max(0, y) & ~1
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        x1, y1 = (x1 + 1) & ~1, (y1 + 1) & ~1
        x1, y1 = min(self.w, x1), min(self.h, y1)
        if x1 <= x0 or y1 <= y0:
            return
        yy, uu, vv = rgb_to_yuv(*rgb)
        row = bytes([yy]) * (x1 - x0)
        buf, W = self.buf, self.w
        for r in range(y0, y1):
            o = r * W + x0
            buf[o:o + (x1 - x0)] = row
        uv = bytes([uu, vv]) * ((x1 - x0) // 2)
        base = self.y_size
        for r in range(y0 // 2, y1 // 2):
            o = base + r * W + x0
            buf[o:o + (x1 - x0)] = uv

    def draw_7seg(self, x, y, text, size, rgb, bg):
        """Draw digits with 7-segment strokes; returns the drawn width."""
        t = max(2, size // 6)          # stroke thickness
        dw, dh = size // 2 + t, size
        pos = x
        for ch in text:
            if ch == ":":
                self.fill_rect(pos, y, t * 2, dh, bg)
                self.fill_rect(pos, y + dh // 3, t, t, rgb)
                self.fill_rect(pos, y + 2 * dh // 3, t, t, rgb)
                pos += t * 2
                continue
            self.fill_rect(pos, y, dw + t, dh, bg)
            segs = SEGMENTS.get(ch, "")
            half = dh // 2
            rects = {
                "a": (pos, y, dw, t), "g": (pos, y + half - t // 2, dw, t),
                "d": (pos, y + dh - t, dw, t),
                "f": (pos, y, t, half), "b": (pos + dw - t, y, t, half),
                "e": (pos, y + half, t, half), "c": (pos + dw - t, y + half, t, half),
            }
            for s in segs:
                self.fill_rect(*rects[s], rgb)
            pos += dw + t
        return pos - x

    # -- scene --------------------------------------------------------------
    def reset(self):
        self.fill_rect(0, 0, self.w, self.bar_h, (40, 44, 52))
        self.fill_rect(*self.canvas, (24, 70, 90))
        for i, (x, y, w, h) in enumerate(self.swatches):
            self.fill_rect(x - 3, y - 3, w + 6, h + 6,
                           (230, 230, 230) if i == self.color else (40, 44, 52))
            self.fill_rect(x, y, w, h, self.PALETTE[i])
        x, y, w, h = self.clear_btn
        self.fill_rect(x, y, w, h, (170, 40, 40))
        self.fill_rect(x + 12, y + h // 2 - 3, w - 24, 6, (255, 255, 255))  # "-" mark
        self.clock_text = ""

    def tick(self, frame_no):
        """Per-frame dynamic content: clock and frame counter (top right)."""
        now = time.strftime("%H:%M:%S")
        if now != self.clock_text:
            self.clock_text = now
            self.draw_7seg(self.w - 470, 14, now, 36, (120, 230, 255), (40, 44, 52))
        self.draw_7seg(self.w - 170, 18, "%06d" % (frame_no % 1000000), 28,
                       (255, 210, 60), (40, 44, 52))

    @staticmethod
    def _hit(r, x, y):
        return r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]

    def _paint(self, x, y):
        b = self.brush
        cx, cy, cw, ch = self.canvas
        if not (cx <= x < cx + cw and cy <= y < cy + ch):
            return
        self.fill_rect(max(cx, x - b // 2), max(cy, y - b // 2), b, b,
                       self.PALETTE[self.color])

    def _stroke(self, p0, p1):
        (x0, y0), (x1, y1) = p0, p1
        n = max(1, int(max(abs(x1 - x0), abs(y1 - y0)) / (self.brush / 2)))
        for i in range(1, n + 1):
            self._paint(x0 + (x1 - x0) * i // n, y0 + (y1 - y0) * i // n)

    def handle(self, ev):
        x, y = int(ev.get("x", 0)), int(ev.get("y", 0))
        t = ev.get("t")
        if t == "down":
            if ev.get("b", 0) == 2:                 # right click clears
                self.reset()
                return
            for i, r in enumerate(self.swatches):
                if self._hit(r, x, y):
                    self.color = i
                    self.reset_bar()
                    return
            if self._hit(self.clear_btn, x, y):
                self.fill_rect(*self.canvas, (24, 70, 90))
                return
            self._paint(x, y)
            self.last_pt = (x, y)
        elif t == "move" and self.last_pt is not None:
            self._stroke(self.last_pt, (x, y))
            self.last_pt = (x, y)
        elif t == "up":
            if self.last_pt is not None:
                self._stroke(self.last_pt, (x, y))
            self.last_pt = None

    def reset_bar(self):
        for i, (x, y, w, h) in enumerate(self.swatches):
            self.fill_rect(x - 3, y - 3, w + 6, h + 6,
                           (230, 230, 230) if i == self.color else (40, 44, 52))
            self.fill_rect(x, y, w, h, self.PALETTE[i])


class FbEcho:
    """Loopback test helper: draw browser clicks/drags onto this board's own
    framebuffer (its HDMI output). When that HDMI output is what the capture
    stick records, each click shows up in the stream, so the browser can
    measure true glass-to-glass latency. Stand-in until a real HID sink."""

    COLORS = [(255, 64, 64), (64, 255, 64), (64, 128, 255), (255, 255, 64), (255, 64, 255)]

    def __init__(self, path, src_w, src_h):
        import mmap
        name = os.path.basename(path)
        sysfs = "/sys/class/graphics/%s/" % name
        self.w, self.h = map(int, open(sysfs + "virtual_size").read().split(","))
        self.stride = int(open(sysfs + "stride").read())
        bpp = int(open(sysfs + "bits_per_pixel").read())
        if bpp != 32:
            raise RuntimeError("%s: only 32 bpp supported (got %d)" % (path, bpp))
        self.fd = os.open(path, os.O_RDWR)
        self.mem = mmap.mmap(self.fd, self.stride * self.h)
        self.sx, self.sy = self.w / src_w, self.h / src_h
        self.n = 0
        self.last = None
        log("fb echo on %s (%dx%d)" % (path, self.w, self.h))

    def rect(self, x, y, w, h, rgb):
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(self.w, int(x + w)), min(self.h, int(y + h))
        if x1 <= x0 or y1 <= y0:
            return
        px = bytes([rgb[2], rgb[1], rgb[0], 0xFF]) * (x1 - x0)   # XRGB8888 (BGRA in memory)
        for r in range(y0, y1):
            o = r * self.stride + x0 * 4
            self.mem[o:o + len(px)] = px

    def handle(self, ev):
        x, y = ev.get("x", 0) * self.sx, ev.get("y", 0) * self.sy
        t = ev.get("t")
        if t == "down":
            self.n += 1
            c = self.COLORS[self.n % len(self.COLORS)]
            self.rect(x - 24, y - 24, 48, 48, c)    # big enough to survive scaling + MJPEG
            self.last = (x, y, c)
        elif t == "move" and self.last:
            lx, ly, c = self.last
            steps = max(1, int(max(abs(x - lx), abs(y - ly)) / 6))
            for i in range(1, steps + 1):
                self.rect(lx + (x - lx) * i / steps - 6, ly + (y - ly) * i / steps - 6, 12, 12, c)
            self.last = (x, y, c)
        elif t == "up":
            self.last = None


# --------------------------------------------------------------------------
# HID output: line protocol to the RP2350 (UART or USB CDC, both are a tty)
# --------------------------------------------------------------------------
#
#   K mm k1 k2 k3 k4 k5 k6   keyboard report (hex): modifiers + 6 usage codes
#   A bb xxxx yyyy ww        absolute mouse: buttons, x/y 0..7fff, wheel (int8)
#   R bb xx yy ww            relative (boot) mouse: buttons, dx/dy/wheel (int8)
#   Z                        release everything
#   ?                        identify -> "OK kvm-hid <variant> <version>"
# From the RP2350:  "L xx" keyboard LEDs (bit0 Num, bit1 Caps, bit2 Scroll)

# KeyboardEvent.code (physical key) -> HID usage (Keyboard/Keypad page)
HID_KEYS = {}
for _i, _c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    HID_KEYS["Key" + _c] = 0x04 + _i
for _i, _c in enumerate("1234567890"):
    HID_KEYS["Digit" + _c] = 0x1E + _i
for _i in range(12):
    HID_KEYS["F%d" % (_i + 1)] = 0x3A + _i
for _i in range(12):
    HID_KEYS["F%d" % (_i + 13)] = 0x68 + _i
for _i, _c in enumerate("123456789"):
    HID_KEYS["Numpad" + _c] = 0x59 + _i
HID_KEYS.update({
    "Enter": 0x28, "Escape": 0x29, "Backspace": 0x2A, "Tab": 0x2B, "Space": 0x2C,
    "Minus": 0x2D, "Equal": 0x2E, "BracketLeft": 0x2F, "BracketRight": 0x30,
    "Backslash": 0x31, "IntlHash": 0x32, "Semicolon": 0x33, "Quote": 0x34,
    "Backquote": 0x35, "Comma": 0x36, "Period": 0x37, "Slash": 0x38,
    "CapsLock": 0x39, "PrintScreen": 0x46, "ScrollLock": 0x47, "Pause": 0x48,
    "Insert": 0x49, "Home": 0x4A, "PageUp": 0x4B, "Delete": 0x4C, "End": 0x4D,
    "PageDown": 0x4E, "ArrowRight": 0x4F, "ArrowLeft": 0x50, "ArrowDown": 0x51,
    "ArrowUp": 0x52, "NumLock": 0x53, "NumpadDivide": 0x54, "NumpadMultiply": 0x55,
    "NumpadSubtract": 0x56, "NumpadAdd": 0x57, "NumpadEnter": 0x58, "Numpad0": 0x62,
    "NumpadDecimal": 0x63, "IntlBackslash": 0x64, "ContextMenu": 0x65, "Power": 0x66,
    "NumpadEqual": 0x67, "NumpadComma": 0x85,
    # Japanese keyboards
    "IntlRo": 0x87, "KanaMode": 0x88, "IntlYen": 0x89, "Convert": 0x8A,
    "NonConvert": 0x8B, "Lang1": 0x90, "Lang2": 0x91,
})
HID_MODS = {"ControlLeft": 0x01, "ShiftLeft": 0x02, "AltLeft": 0x04, "MetaLeft": 0x08,
            "ControlRight": 0x10, "ShiftRight": 0x20, "AltRight": 0x40, "MetaRight": 0x80,
            "OSLeft": 0x08, "OSRight": 0x80}

# Typing text: printable ASCII -> (key code, shift) for the *target's* layout
_US_SHIFTED = {"!": "Digit1", "@": "Digit2", "#": "Digit3", "$": "Digit4", "%": "Digit5",
               "^": "Digit6", "&": "Digit7", "*": "Digit8", "(": "Digit9", ")": "Digit0",
               "_": "Minus", "+": "Equal", "{": "BracketLeft", "}": "BracketRight",
               "|": "Backslash", ":": "Semicolon", '"': "Quote", "~": "Backquote",
               "<": "Comma", ">": "Period", "?": "Slash"}
_US_PLAIN = {"-": "Minus", "=": "Equal", "[": "BracketLeft", "]": "BracketRight",
             "\\": "Backslash", ";": "Semicolon", "'": "Quote", "`": "Backquote",
             ",": "Comma", ".": "Period", "/": "Slash", " ": "Space", "\n": "Enter",
             "\t": "Tab"}
_JIS_SHIFTED = {"!": "Digit1", '"': "Digit2", "#": "Digit3", "$": "Digit4", "%": "Digit5",
                "&": "Digit6", "'": "Digit7", "(": "Digit8", ")": "Digit9", "=": "Minus",
                "~": "Equal", "|": "IntlYen", "`": "BracketLeft", "{": "BracketRight",
                "+": "Semicolon", "*": "Quote", "}": "Backslash", "<": "Comma",
                ">": "Period", "?": "Slash", "_": "IntlRo"}
_JIS_PLAIN = {"-": "Minus", "^": "Equal", "\\": "IntlRo", "@": "BracketLeft",
              "[": "BracketRight", ";": "Semicolon", ":": "Quote", "]": "Backslash",
              ",": "Comma", ".": "Period", "/": "Slash", " ": "Space", "\n": "Enter",
              "\t": "Tab"}


def char_to_key(ch, layout):
    if "a" <= ch <= "z":
        return "Key" + ch.upper(), False
    if "A" <= ch <= "Z":
        return "Key" + ch, True
    if "0" <= ch <= "9":
        return "Digit" + ch, False
    plain, shifted = (_JIS_PLAIN, _JIS_SHIFTED) if layout == "jis" else (_US_PLAIN, _US_SHIFTED)
    if ch in plain:
        return plain[ch], False
    if ch in shifted:
        return shifted[ch], True
    return None, False


class HidSerial:
    """Keeps keyboard/mouse state and sends reports to the RP2350 over a tty
    (/dev/ttyS* for the UART firmware, /dev/ttyACM* for the CDC firmware).
    Reopens the port if it goes away (CDC unplug)."""

    BAUDS = {9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600,
             1000000, 1500000, 2000000}

    def __init__(self, path, baud, on_status):
        self.path, self.baud, self.on_status = path, baud, on_status
        self.fd = None
        self.lock = threading.Lock()
        self.keys = []                 # pressed non-modifier usages, in order
        self.mods = 0
        self.buttons = 0
        self.abs = None                # latest (x, y) not yet sent
        self.last_abs_sent = 0.0
        self.version = None
        self.leds = 0
        self.wake = threading.Event()
        threading.Thread(target=self._writer, daemon=True).start()
        threading.Thread(target=self._reader, daemon=True).start()

    # -- port -------------------------------------------------------------------
    def _open(self):
        import termios
        import tty
        fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(fd)
        attr = termios.tcgetattr(fd)
        speed = getattr(termios, "B%d" % self.baud)
        attr[4] = attr[5] = speed
        attr[2] |= termios.CLOCAL | termios.CREAD
        attr[2] &= ~termios.HUPCL
        termios.tcsetattr(fd, termios.TCSANOW, attr)
        termios.tcflush(fd, termios.TCIOFLUSH)
        return fd

    def _ensure(self):
        if self.fd is not None:
            return True
        try:
            self.fd = self._open()
        except OSError:
            return False
        log("hid: opened %s @ %d" % (self.path, self.baud))
        self._send_raw(b"Z\n?\n")
        self.status_changed()
        return True

    def _drop(self, why):
        if self.fd is not None:
            log("hid: %s closed (%s)" % (self.path, why))
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd, self.version = None, None
        self.status_changed()

    def _send_raw(self, data):
        try:
            os.write(self.fd, data)
            return True
        except OSError as e:
            self._drop(e)
            return False

    def status(self):
        return {"path": self.path, "open": self.fd is not None,
                "version": self.version, "leds": self.leds}

    def status_changed(self):
        try:
            self.on_status(self.status())
        except Exception:
            pass

    # -- reports ----------------------------------------------------------------
    def _send(self, line):
        with self.lock:
            if self._ensure():
                self._send_raw(line.encode() + b"\n")

    def _key_report(self):
        k = (self.keys + [0] * 6)[:6]
        self._send("K %02x %s" % (self.mods, " ".join("%02x" % c for c in k)))

    def key(self, code, down):
        if code in HID_MODS:
            bit = HID_MODS[code]
            self.mods = (self.mods | bit) if down else (self.mods & ~bit)
        elif code in HID_KEYS:
            u = HID_KEYS[code]
            if down and u not in self.keys:
                self.keys.append(u)
            elif not down and u in self.keys:
                self.keys.remove(u)
        else:
            return
        self._key_report()

    def release_all(self):
        self.keys, self.mods, self.buttons = [], 0, 0
        self._send("Z")

    def combo(self, codes, hold=0.05):
        """Press keys in order, then release in reverse (Ctrl+Alt+Del etc.)."""
        for c in codes:
            self.key(c, True)
            time.sleep(0.01)
        time.sleep(hold)
        for c in reversed(codes):
            self.key(c, False)
            time.sleep(0.01)

    def type_text(self, text, layout="us", delay=0.012):
        for ch in text:
            code, shift = char_to_key(ch, layout)
            if code is None:
                continue
            if shift:
                self.key("ShiftLeft", True)
            self.key(code, True)
            time.sleep(delay)
            self.key(code, False)
            if shift:
                self.key("ShiftLeft", False)
            time.sleep(delay)

    def mouse_abs(self, x, y, buttons=None, wheel=0):
        """x, y in 0..1. Moves are coalesced (<= ~125 Hz); buttons and wheel
        go out at once with the latest position."""
        nx = max(0, min(0x7FFF, int(x * 0x7FFF)))
        ny = max(0, min(0x7FFF, int(y * 0x7FFF)))
        if buttons is None and not wheel:
            self.abs = (nx, ny)
            self.wake.set()
            return
        if buttons is not None:
            self.buttons = buttons & 0x1F
        self.abs = None
        self.last_abs_sent = time.monotonic()
        self._send("A %02x %04x %04x %02x" % (self.buttons, nx, ny, wheel & 0xFF))

    def mouse_rel(self, dx, dy, buttons=None, wheel=0):
        if buttons is not None:
            self.buttons = buttons & 0x1F
        while dx or dy or wheel is not None:
            sx = max(-127, min(127, dx))
            sy = max(-127, min(127, dy))
            self._send("R %02x %02x %02x %02x" % (self.buttons, sx & 0xFF, sy & 0xFF,
                                                  (wheel or 0) & 0xFF))
            dx, dy, wheel = dx - sx, dy - sy, None
            if not dx and not dy:
                break

    def _writer(self):
        while True:
            self.wake.wait(1.0)
            self.wake.clear()
            if self.fd is None:
                with self.lock:
                    self._ensure()
            pos = self.abs
            if pos is None:
                continue
            wait = 0.008 - (time.monotonic() - self.last_abs_sent)
            if wait > 0:
                time.sleep(wait)
            pos, self.abs = self.abs, None
            if pos is not None:
                self.last_abs_sent = time.monotonic()
                self._send("A %02x %04x %04x 00" % (self.buttons, pos[0], pos[1]))

    def _reader(self):
        buf = b""
        while True:
            fd = self.fd
            if fd is None:
                time.sleep(0.5)
                continue
            try:
                chunk = os.read(fd, 256)
            except OSError as e:
                with self.lock:
                    if self.fd == fd:
                        self._drop(e)
                continue
            if not chunk:
                time.sleep(0.05)
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip().decode("ascii", "replace")
                if line.startswith("OK "):
                    self.version = line[3:]
                    log("hid: device", self.version)
                    self.status_changed()
                elif line.startswith("L ") and len(line) >= 4:
                    try:
                        self.leds = int(line[2:4], 16)
                    except ValueError:
                        continue
                    self.status_changed()


# --------------------------------------------------------------------------
# CSI-2 input: RK3288 ISP (rkisp1) with an OV5647 camera or a TC358743
# HDMI bridge (C790). Needs the kernel patches and DT overlays from the
# rk3288-mainline-media repository (patches/camera/).
# --------------------------------------------------------------------------

class Csi:
    """Sets up the media graph sensor -> rkisp1_csi -> rkisp1_isp ->
    resizer -> main path with media-ctl / v4l2-ctl, so the ISP hands NV12
    straight to the encoder (no CPU decode as with the UVC stick)."""

    SENSORS = ("ov5647", "tc358743")

    def __init__(self, media, edid=""):
        self.media = media
        self.edid = edid
        self.edid_set = False
        graph = self._mc("-p")
        self.video = self._mc("-e", "rkisp1_mainpath").strip()
        self.sensor = ""
        for line in graph.splitlines():
            m = re.match(r"- entity \d+: (.+?) \(", line)
            if m and m.group(1).split()[0] in self.SENSORS:
                self.sensor = m.group(1)
        if not self.sensor:
            raise RuntimeError("no %s on %s" % ("/".join(self.SENSORS), media))
        self.kind = self.sensor.split()[0]
        self.subdev = self._mc("-e", self.sensor).strip()
        self.size = None                # (w, h) the encoder was set up for

    @staticmethod
    def find():
        """First media device with the rkisp1 main path, or ''."""
        for name in sorted(os.listdir("/dev")):
            if not re.fullmatch(r"media\d+", name):
                continue
            dev = "/dev/" + name
            try:
                out = subprocess.run(["media-ctl", "-d", dev, "-p"], capture_output=True,
                                     text=True, timeout=5).stdout
            except (OSError, subprocess.SubprocessError):
                return ""               # no media-ctl (v4l-utils)
            if "rkisp1_mainpath" in out:
                return dev
        return ""

    def _mc(self, *a):
        r = subprocess.run(["media-ctl", "-d", self.media] + list(a),
                           capture_output=True, text=True, timeout=5)
        if r.returncode:
            raise RuntimeError("media-ctl %s: %s" % (" ".join(a), r.stderr.strip()))
        return r.stdout

    def _ctl(self, *a):
        return subprocess.run(["v4l2-ctl", "-d", self.subdev] + list(a),
                              capture_output=True, text=True, timeout=5)

    def _set(self, entity, pad, fmt):
        self._mc("-V", '"%s":%d [%s]' % (entity, pad, fmt))

    def _get(self, entity, pad):
        m = re.search(r"fmt:\S+?/(\d+)x(\d+)", self._mc("--get-v4l2", '"%s":%d' % (entity, pad)))
        return (int(m.group(1)), int(m.group(2))) if m else None

    def hdmi_timings(self):
        """TC358743: load the EDID once, lock onto the incoming timings and
        return the active size, or None without a signal."""
        if not self.edid_set:
            if self.edid and os.path.exists(self.edid):
                r = self._ctl("--set-edid=pad=0,file=%s" % self.edid, "--fix-edid-checksums")
            else:
                r = self._ctl("--set-edid=pad=0,type=hdmi")
            self.edid_set = r.returncode == 0
            time.sleep(1)               # the source reacts to the hot-plug
        r = self._ctl("--query-dv-timings")
        w = re.search(r"Active width: (\d+)", r.stdout)
        h = re.search(r"Active height: (\d+)", r.stdout)
        if r.returncode or not w or not h:
            return None
        self._ctl("--set-dv-bt-timings", "query")
        return int(w.group(1)), int(h.group(1))

    def configure(self, width, height, out=None):
        """Program the pipeline; width/height 0 = the sensor's / source's size.
        out forces the output size (later runs: keep what the encoder and the
        page were set up for, the resizer scales). Returns the output size."""
        self.req = (width, height)
        if self.kind == "tc358743":
            code = "UYVY8_1X16"
            size = self.hdmi_timings()
            if size is None:
                log("csi: no HDMI signal yet, assuming", "%dx%d" % (width or 1280, height or 720))
                size = (width or 1280, height or 720)
        else:
            code = "SBGGR10_1X10"       # RAW10, the ISP demosaics
            size = (width or 1296, height or 972)   # 1296x972 = 2x2 binned, full view
        self._set(self.sensor, 0, "fmt:%s/%dx%d field:none" % (code, size[0], size[1]))
        sw, sh = self._get(self.sensor, 0) or size  # the sensor picks its nearest mode
        crop = "crop:(0,0)/%dx%d" % (sw, sh)
        self._set("rkisp1_csi", 0, "fmt:%s/%dx%d" % (code, sw, sh))
        self._set("rkisp1_isp", 0, "fmt:%s/%dx%d %s" % (code, sw, sh, crop))
        self._set("rkisp1_isp", 2, "fmt:YUYV8_2X8/%dx%d %s" % (sw, sh, crop))
        self._set("rkisp1_resizer_mainpath", 0, "fmt:YUYV8_2X8/%dx%d %s" % (sw, sh, crop))
        ow, oh = out or ((width or sw) & ~1, (height or sh) & ~1)
        # 4:2:0 out of the resizer, so the main path can write NV12
        self._set("rkisp1_resizer_mainpath", 1, "fmt:YUYV8_1_5X8/%dx%d" % (ow, oh))
        if self.kind == "ov5647":
            # no 3A in the ISP yet: use the sensor's own AE / AGC / AWB
            self._ctl("-c", "auto_exposure=0", "-c", "gain_automatic=1",
                      "-c", "white_balance_automatic=1")
        self.size = (ow, oh)
        log("csi: %s %dx%d -> %dx%d NV12 on %s" % (self.sensor, sw, sh, ow, oh, self.video))
        return ow, oh

    def prepare(self):
        """Before each GStreamer start: program the graph again, so whatever
        changed it in between (another tool, an HDMI mode change) cannot
        leave the main path refusing to stream (EPIPE). A new HDMI mode is
        scaled to the stream size."""
        self.configure(self.req[0], self.req[1], out=self.size)


# --------------------------------------------------------------------------
# Encoder: GStreamer v4l2slh264enc, frames in on stdin, RTP out on UDP
# --------------------------------------------------------------------------

class Encoder:
    def __init__(self, args, on_au, csi=None):
        self.args = args
        self.on_au = on_au
        self.csi = csi
        self.frame_size = args.width * args.height * 3 // 2
        self.proc = None
        self.sock = None
        self.stopping = False
        self.wanted = False          # someone is watching: keep GStreamer running
        self.lock = threading.Lock()
        self.au_count = 0
        self.written = 0             # screen mode: frames handed to GStreamer
        self.frame_base = 0          # self.written when the current process started
        self.last_au = time.monotonic()
        self.started = 0.0           # when the current process was started
        self.fails = 0               # consecutive runs that died within FAIL_SEC
        self.retry_at = None         # back-off: do not restart before this time

    FAIL_SEC = 5.0                   # a run shorter than this counts as a failure

    def gst_cmd(self):
        a = self.args
        enc = ["v4l2slh264enc", "keyframe-interval=%d" % a.keyint]
        if a.bitrate:
            enc += ["rate-control=cbr", "bitrate=%d" % a.bitrate, "quantizer=%d" % a.qp]
        else:
            enc += ["quantizer=%d" % a.qp]
        leaky = ["queue", "max-size-buffers=1", "max-size-bytes=0",
                 "max-size-time=0", "leaky=downstream", "!"]
        if a.source == "csi":
            # rkisp1 main path: NV12 straight from the ISP. drop-allocation
            # keeps v4l2src on its own buffers (importing the encoder's
            # macroblock-padded pool is rejected with EINVAL); the encoder
            # then copies the visible area (rk3288-mainline-media:
            # patches/gstreamer/0002).
            # io-mode=mmap: with the default (auto) v4l2src hands out stale
            # buffers, the same 4 frames over and over while the ISP runs.
            src = ["v4l2src", "device=%s" % self.csi.video, "io-mode=mmap",
                   "do-timestamp=true", "!",
                   "video/x-raw,format=NV12,width=%d,height=%d,framerate=%d/1"
                   % (a.width, a.height, a.fps), "!"] + leaky + [
                   "identity", "drop-allocation=true", "!"]
        elif a.source == "capture":
            # UVC MJPEG -> CPU jpegdec -> VPU. This capture stick sends a few
            # full-size (1080p) JPEGs before switching to the requested size,
            # and the encoder cannot follow a size change, so pin the size.
            # Leaky 1-frame queues keep only the newest frame (no backlog in
            # the driver queue) and put jpegdec and the encoder on separate
            # threads/cores. Stay in I420 (jpegdec's output, accepted by the
            # VPU) so videoconvert is a passthrough; ifast IDCT is ~25%
            # cheaper. 1080p: 18 fps (single thread, NV12) -> 49 fps.
            src =["v4l2src", "device=%s" % a.device, "do-timestamp=true", "!",
                   "image/jpeg,width=%d,height=%d,framerate=%d/1"
                   % (a.width, a.height, a.capture_fps), "!"] + leaky + [
                   "jpegdec", "idct-method=ifast", "!"] + leaky + [
                   "videoscale", "!", "videoconvert", "!",
                   "video/x-raw,format=I420,width=%d,height=%d" % (a.width, a.height), "!"]
        else:
            src = ["fdsrc", "fd=0", "blocksize=%d" % self.frame_size, "!",
                   "rawvideoparse", "format=nv12", "width=%d" % a.width,
                   "height=%d" % a.height, "framerate=%d/1" % a.fps, "!",
                   # The MR's v4l2slh264enc segfaults (g_object_ref on a
                   # non-object) when fed straight from fdsrc/rawvideoparse in
                   # push mode; a passthrough videoconvert in between avoids it.
                   "videoconvert", "!", "video/x-raw,format=NV12", "!"]
        return ["gst-launch-1.0", "-q"] + src + enc + [
                "!", "h264parse", "config-interval=-1", "!",
                "video/x-h264,stream-format=byte-stream,alignment=au", "!",
                "rtph264pay", "pt=96", "mtu=1400", "config-interval=-1",
                "aggregate-mode=zero-latency", "!",
                "udpsink", "host=127.0.0.1", "port=%d" % RTP_PORT,
                "sync=false", "async=false"]

    def start(self):
        env = dict(os.environ)
        gst = os.path.expanduser(self.args.gst_dir)
        if os.path.isdir(gst):
            env["GST_PLUGIN_PATH"] = os.path.join(gst, "lib", "gstreamer-1.0")
            env["LD_LIBRARY_PATH"] = os.path.join(gst, "lib")
        if self.sock is None:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
            self.sock.bind(("127.0.0.1", RTP_PORT))
            threading.Thread(target=self._rtp_loop, daemon=True).start()
        if self.csi is not None:
            try:
                self.csi.prepare()
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                log("csi:", e)
        cmd = self.gst_cmd()
        log("encoder:", " ".join(cmd))
        self.ts0 = None
        self.frame_base = self.written   # PTS restart at 0 with each process
        self.last_au = self.started = time.monotonic()
        stdin =subprocess.PIPE if self.args.source == "screen" else subprocess.DEVNULL
        self.proc = subprocess.Popen(cmd, stdin=stdin, env=env)

    def watchdog(self, timeout=5.0):
        """Capture mode: restart GStreamer when it dies or stops producing
        frames (the capture stick sometimes fails its first stream)."""
        while not self.stopping:
            time.sleep(1)
            with self.lock:
                if not self.wanted or self.stopping:
                    continue
                now = time.monotonic()
                dead = self.proc is None or self.proc.poll() is not None
                stalled = now - self.last_au > timeout
                if not (dead or stalled):
                    if now - self.started > self.FAIL_SEC:
                        self.fails = 0
                    continue
                if self.retry_at is None:
                    # back off when it keeps failing right away (e.g. the
                    # device refuses to stream): 2, 4, ... 30 s between tries
                    if now - self.started < self.FAIL_SEC + timeout:
                        self.fails += 1
                    delay = min(30, 2 ** self.fails) if self.fails > 1 else 0
                    self.retry_at = now + delay
                    log("encoder %s, %s" % ("exited" if dead else "stalled",
                        "retry in %d s" % delay if delay else "restarting"))
                    if not dead:
                        self.proc.kill()
                        self.proc.wait()
                if now >= self.retry_at:
                    self.retry_at = None
                    self.start()

    def resume(self):
        """Start GStreamer if it is not running (first viewer arrived)."""
        with self.lock:
            self.wanted = True
            if self.retry_at is not None and time.monotonic() < self.retry_at:
                return                   # backing off; the watchdog restarts it
            if not self.stopping and (self.proc is None or self.proc.poll() is not None):
                self.retry_at = None
                self.start()

    def pause(self):
        """Stop GStreamer while nobody watches: the capture path keeps a CPU
        core busy decoding MJPEG (and the board hot) for no one."""
        with self.lock:
            self.wanted = False
            p = self.proc
            if p is not None and p.poll() is None:
                log("encoder: no viewers, stopping")
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()

    def write(self, frame):
        if self.proc is None or self.proc.poll() is not None:
            with self.lock:
                if not self.stopping and self.wanted:
                    log("encoder exited, restarting")
                    self.start()
            return False
        try:
            self.proc.stdin.write(frame)
            self.proc.stdin.flush()
            self.written += 1
            return True
        except BrokenPipeError:
            return False

    def stop(self):
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # RTP (RFC 6184) depacketizer: single NAL, STAP-A, FU-A. The marker bit
    # ends an access unit, so each frame is forwarded as soon as it is done.
    def _rtp_loop(self):
        nals, fu, cur_ts, last_seq, broken = [], None, None, None, False
        while True:
            pkt = self.sock.recv(65536)
            if len(pkt) < 12:
                continue
            cc, ext, pad = pkt[0] & 0x0F, pkt[0] & 0x10, pkt[0] & 0x20
            marker = pkt[1] & 0x80
            seq, ts = struct.unpack("!HI", pkt[2:8])
            off = 12 + 4 * cc
            if ext:
                off += 4 + 4 * struct.unpack("!H", pkt[off + 2:off + 4])[0]
            end = len(pkt) - (pkt[-1] if pad else 0)
            payload = pkt[off:end]
            if last_seq is not None and seq != (last_seq + 1) & 0xFFFF:
                broken = True                     # lost packet: drop this AU
            last_seq = seq
            if cur_ts is not None and ts != cur_ts and nals:
                nals, fu = [], None               # marker lost; resync
            cur_ts = ts
            if not payload:
                continue
            t = payload[0] & 0x1F
            if 1 <= t <= 23:
                nals.append(payload)
            elif t == 24:                         # STAP-A
                i = 1
                while i + 2 <= len(payload):
                    n = struct.unpack("!H", payload[i:i + 2])[0]
                    nals.append(payload[i + 2:i + 2 + n])
                    i += 2 + n
            elif t == 28:                         # FU-A
                ind, hdr = payload[0], payload[1]
                if hdr & 0x80:
                    fu = bytearray([(ind & 0xE0) | (hdr & 0x1F)]) + payload[2:]
                elif fu is not None:
                    fu += payload[2:]
                if hdr & 0x40 and fu is not None:
                    nals.append(bytes(fu))
                    fu = None
            if marker:
                if not broken and nals:
                    self.last_au = time.monotonic()
                    if self.args.source != "screen":
                        frame_no = self.au_count     # live source: just count
                    else:
                        if self.ts0 is None:         # frame index from PTS,
                            self.ts0 = ts            # matches the render loop
                        frame_no = self.frame_base + round(
                            ((ts - self.ts0) & 0xFFFFFFFF) * self.args.fps / 90000)
                    self.au_count += 1
                    key = any((n[0] & 0x1F) == 5 for n in nals)
                    au = b"".join(b"\x00\x00\x00\x01" + n for n in nals)
                    self.on_au(frame_no, key, au)
                elif broken:
                    self.on_au(None, False, None)
                nals, fu, broken = [], None, False


# --------------------------------------------------------------------------
# WebSocket (RFC 6455) server side, just enough for this page
# --------------------------------------------------------------------------

class WSClient:
    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.q = queue.Queue(maxsize=90)
        self.need_key = True
        self.alive = True
        self.lock = threading.Lock()

    def send(self, data, opcode=2):
        n = len(data)
        if n < 126:
            hdr = struct.pack("!BB", 0x80 | opcode, n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x80 | opcode, 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x80 | opcode, 127, n)
        with self.lock:
            self.sock.sendall(hdr + data)

    def recv(self):
        """Returns (opcode, payload) of the next frame; raises on close."""
        def exact(n):
            b = b""
            while len(b) < n:
                c = self.sock.recv(n - len(b))
                if not c:
                    raise ConnectionError("closed")
                b += c
            return b
        b0, b1 = exact(2)
        opcode, masked, n = b0 & 0x0F, b1 & 0x80, b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", exact(8))[0]
        mask = exact(4) if masked else b"\0\0\0\0"
        data = bytearray(exact(n))
        for i in range(n):
            data[i] ^= mask[i & 3]
        return opcode, bytes(data)


# --------------------------------------------------------------------------
# KVM core: render loop, input queue, fan-out to clients
# --------------------------------------------------------------------------

class KVM:
    def __init__(self, args, csi=None):
        self.args = args
        self.csi = csi
        self.screen = Screen(args.width, args.height) if args.source == "screen" else None
        self.fb = None
        if args.source != "screen" and args.fb_echo:
            self.fb = FbEcho(args.fb_echo, args.width, args.height)
        self.clients = set()
        self.clients_lock = threading.Lock()
        self.hid = HidSerial(args.hid, args.hid_baud, self.on_hid_status) if args.hid else None
        self.inputs = queue.Queue()
        self.applied_seq = 0
        self.meta = {}                     # frame_no -> applied input seq
        self.clients = set()
        self.clients_lock = threading.Lock()
        self.encoder = Encoder(args, self.on_au, csi)
        self.idle_timer = None
        self.stats = collections.deque(maxlen=self.args.fps * 2)

    # input -----------------------------------------------------------------
    def on_input(self, client, ev):
        t = ev.get("t")
        hid = self.hid
        if hid is not None:
            if t == "key":
                hid.key(str(ev.get("code", "")), bool(ev.get("down")))
            elif t == "release":
                hid.release_all()
            elif t == "combo":
                codes = [str(c) for c in ev.get("codes", [])][:8]
                threading.Thread(target=hid.combo, args=(codes,), daemon=True).start()
            elif t == "type":
                threading.Thread(target=hid.type_text, daemon=True,
                                 args=(str(ev.get("text", ""))[:4096],
                                       "jis" if ev.get("layout") == "jis" else "us")).start()
            elif t in ("down", "up", "move", "wheel"):
                x = float(ev.get("x", 0)) / self.args.width
                y = float(ev.get("y", 0)) / self.args.height
                if t == "move":
                    hid.mouse_abs(x, y)
                else:
                    hid.mouse_abs(x, y, int(ev.get("buttons", 0)), int(ev.get("wheel", 0)))
            elif t == "rel":
                hid.mouse_rel(int(ev.get("dx", 0)), int(ev.get("dy", 0)),
                              int(ev.get("buttons", 0)), int(ev.get("wheel", 0)))
        if t in ("down", "up", "move"):
            self.inputs.put(ev)          # test screen / fb echo

    def on_hid_status(self, status):
        msg = json.dumps(dict(status, t="hid")).encode()
        with self.clients_lock:
            clients = list(self.clients)
        for c in clients:
            try:
                c.send(msg, opcode=1)
            except OSError:
                pass

    # render loop -------------------------------------------------------------
    def run(self):
        if self.args.idle_stop < 0:
            self.encoder.resume()        # always on
        else:
            log("encoder starts with the first viewer")
        if self.args.source != "screen":
            return self.run_capture()
        period = 1.0 / self.args.fps
        frame_no = 0
        next_t = time.monotonic()
        while True:
            if not self.encoder.wanted:      # nobody watching: idle
                time.sleep(0.2)
                next_t = time.monotonic()
                continue
            while True:
                try:
                    ev = self.inputs.get_nowait()
                except queue.Empty:
                    break
                self.screen.handle(ev)
                self.applied_seq = max(self.applied_seq, int(ev.get("seq", 0)))
            self.screen.tick(frame_no)
            self.meta[frame_no] = (self.applied_seq, time.monotonic())
            self.meta.pop(frame_no - 4 * self.args.fps, None)
            if self.encoder.write(self.screen.buf):
                frame_no += 1
            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()     # fell behind: don't burst

    def run_capture(self):
        """Video comes straight from the capture stick; input only goes to the
        (optional) framebuffer echo until a HID sink exists."""
        threading.Thread(target=self.encoder.watchdog, daemon=True).start()
        while True:
            ev = self.inputs.get()
            if self.fb:
                self.fb.handle(ev)

    # encoded output -----------------------------------------------------------
    def on_au(self, frame_no, key, au):
        with self.clients_lock:
            clients = list(self.clients)
        if au is None:                        # lost data: everyone resyncs
            for c in clients:
                c.need_key = True
            return
        self.stats.append((time.monotonic(), len(au)))
        if self.screen is not None:
            applied, t_render = self.meta.get(frame_no, (self.applied_seq, None))
        else:
            applied, t_render = 0, None     # capture: latency measured by pixel change
        # time from handing the raw frame to GStreamer until its encoded
        # access unit came back over RTP (encoder pipeline latency)
        enc_ms = 0 if t_render is None else int((time.monotonic() - t_render) * 1000)
        msg = struct.pack("!BBIIH", 1, 1 if key else 0, frame_no & 0xFFFFFFFF,
                          applied & 0xFFFFFFFF, min(enc_ms, 65535)) + au
        for c in clients:
            if c.need_key and not key:
                continue
            try:
                c.q.put_nowait(msg)
                c.need_key = False
            except queue.Full:                # slow client: skip to next IDR
                c.need_key = True
                with c.q.mutex:
                    c.q.queue.clear()

    def add_client(self, c):
        with self.clients_lock:
            self.clients.add(c)
            if self.idle_timer is not None:
                self.idle_timer.cancel()
                self.idle_timer = None
        log("client connected", c.addr, "clients=%d" % len(self.clients))
        try:
            self.encoder.resume()
        except OSError as e:          # e.g. RTP port taken by another kvm.py
            log("encoder start failed:", e)
            self.remove_client(c)
            raise
        c.send(json.dumps({"t": "hello", "source": self.args.source,
                           "sensor": self.csi.sensor if self.csi else "",
                           "fbEcho": bool(self.fb),
                           "hid": self.hid.status() if self.hid else None}).encode(), opcode=1)

    def remove_client(self, c):
        if self.hid is not None and len(self.clients) <= 1:
            self.hid.release_all()       # last viewer gone: no stuck keys
        c.alive = False
        with self.clients_lock:
            self.clients.discard(c)
            # last viewer gone: stop the encoder after a grace period (a page
            # reload reconnects within it and keeps the running pipeline)
            if not self.clients and self.args.idle_stop >= 0 and self.idle_timer is None:
                self.idle_timer = threading.Timer(self.args.idle_stop, self._idle)
                self.idle_timer.daemon = True
                self.idle_timer.start()
        log("client gone", c.addr, "clients=%d" % len(self.clients))

    def _idle(self):
        with self.clients_lock:
            self.idle_timer = None
            if self.clients:
                return
        self.encoder.pause()


# --------------------------------------------------------------------------
# HTTP: page + /ws
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dev_tinker KVM</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; background: #111; color: #ddd; font: 13px/1.4 system-ui, sans-serif; }
  #bar { display: flex; gap: 16px; padding: 6px 12px; background: #222; flex-wrap: wrap; }
  #bar b, #bar2 b { color: #8cf; font-weight: 600; }
  #bar2 { display: flex; gap: 12px; padding: 4px 12px; background: #1b1b1b; flex-wrap: wrap;
          align-items: center; }
  #bar2 button, #bar2 select { font: inherit; color: #ddd; background: #333;
          border: 1px solid #555; border-radius: 4px; padding: 0 8px; }
  #wrap:fullscreen { background: #000; align-items: center; justify-content: center; padding: 0; }
  #wrap:fullscreen canvas { max-width: 100vw; max-height: 100vh; width: auto; height: auto; outline: none; }
  canvas:focus { outline-width: 2px; }
  #wrap { display: flex; justify-content: center; padding: 8px; }
  canvas { background: #000; cursor: crosshair; outline: 1px solid #333; }
  /* fit: scale down to the window; 1:1: one video pixel per device pixel */
  #wrap.fit canvas { max-width: 100%; height: auto; image-rendering: auto; }
  #wrap.one { justify-content: flex-start; overflow: auto; }
  #wrap.one canvas { max-width: none; image-rendering: pixelated; }
  #bar button { font: inherit; color: #ddd; background: #333; border: 1px solid #555;
                border-radius: 4px; padding: 0 8px; cursor: pointer; }
  #err { color: #f88; }
</style></head>
<body>
<div id="bar">
  <span>状態 <b id="st">接続中…</b></span>
  <span>映像 <b id="src">-</b></span>
  <span>受信 <b id="fps">-</b> fps</span>
  <span>帯域 <b id="kbps">-</b> kbps</span>
  <span>クリック→表示 <b id="lat">-</b> ms（平均 <b id="lavg">-</b>）</span>
  <span>エンコード <b id="enc">-</b> ms</span>
  <span>フレーム <b id="fno">-</b></span>
  <span>__W__x__H__ <button id="scale" type="button">等倍</button></span>
  <span id="err"></span>
</div>
<div id="bar2">
  <span>HID <b id="hid">-</b></span>
  <span>LED <b id="leds">-</b></span>
  <span>押下 <b id="keys">-</b></span>
  <span><b id="inp">-</b></span>
  <button id="fs" type="button" title="全画面＋キーロック（Ctrl+W や Alt+Tab も相手に送れる）">全画面</button>
  <button id="rel" type="button" title="Pointer Lock で相対マウス（BIOS 等向け）">相対マウス</button>
  <select id="combo" title="ブラウザや OS が横取りするキーを送る"><option value="">特殊キー…</option></select>
  <button id="typebtn" type="button">文字列を入力</button>
  <select id="layout" title="相手の PC のキー配列（文字列入力用）">
    <option value="jis">JIS</option><option value="us">US</option></select>
  <button id="rall" type="button" title="押しっぱなしになったキーを離す">全部離す</button>
</div>
<div id="wrap" class="fit"><canvas id="c" width="__W__" height="__H__"></canvas></div>
<script>
const W = __W__, H = __H__, FPS = __FPS__;
const cv = document.getElementById('c'), ctx = cv.getContext('2d');

// Display scale: "fit" shrinks to the window, "1:1" maps each video pixel to
// one device pixel (CSS size = W / devicePixelRatio) for dot-by-dot viewing.
const wrap = document.getElementById('wrap'), scaleBtn = document.getElementById('scale');
let oneToOne = false;
try { oneToOne = localStorage.getItem('kvm.scale') === '1:1'; } catch (e) {}
function applyScale() {
  wrap.className = oneToOne ? 'one' : 'fit';
  cv.style.width = oneToOne ? (W / devicePixelRatio) + 'px' : '';
  cv.style.height = oneToOne ? (H / devicePixelRatio) + 'px' : '';
  scaleBtn.textContent = oneToOne ? 'フィット' : '等倍';
  try { localStorage.setItem('kvm.scale', oneToOne ? '1:1' : 'fit'); } catch (e) {}
}
scaleBtn.addEventListener('click', () => { oneToOne = !oneToOne; applyScale(); });
matchMedia('(resolution: ' + devicePixelRatio + 'dppx)').addEventListener('change', applyScale);
applyScale();
const $ = id => document.getElementById(id);
let ws, dec = null, configured = false, seq = 0;
const pending = new Map();          // input seq -> click time
const frameSeq = new Map();         // frame no -> applied input seq
let lats = [], rx = [], encMs = [];
let source = 'screen';
// Capture mode has no frame <-> input link, so watch the clicked pixel:
// the server's fb echo paints a square there on the board's HDMI output.
const probes = [];
function px(x, y) { return ctx.getImageData(x, y, 1, 1).data; }
function recordLat(l) {
  lats.push(l); if (lats.length > 50) lats.shift();
  $('lat').textContent = l.toFixed(0);
  $('lavg').textContent = (lats.reduce((a, b) => a + b, 0) / lats.length).toFixed(0);
}
function checkProbes() {
  const now = performance.now();
  for (let i = probes.length - 1; i >= 0; i--) {
    const p = probes[i], c = px(p.x, p.y);
    const d = Math.abs(c[0] - p.base[0]) + Math.abs(c[1] - p.base[1]) + Math.abs(c[2] - p.base[2]);
    if (d > 90) { recordLat(now - p.t0); probes.splice(i, 1); }
    else if (now - p.t0 > 3000) probes.splice(i, 1);
  }
}

function codecFromSps(au) {
  for (let i = 0; i + 4 < au.length; i++) {
    if (au[i] === 0 && au[i+1] === 0 && au[i+2] === 1 && (au[i+3] & 0x1f) === 7) {
      const h = x => x.toString(16).padStart(2, '0');
      return 'avc1.' + h(au[i+4]) + h(au[i+5]) + h(au[i+6]);
    }
  }
  return null;
}

function newDecoder() {
  if (dec) { try { dec.close(); } catch (e) {} }
  configured = false;
  dec = new VideoDecoder({
    output: f => {
      ctx.drawImage(f, 0, 0, W, H);
      const no = Math.round(f.timestamp * FPS / 1e6);
      f.close();
      $('fno').textContent = no;
      if (probes.length) checkProbes();
      const applied = frameSeq.get(no);
      frameSeq.delete(no);
      if (applied === undefined) return;
      const now = performance.now();
      for (const [s, t0] of pending) {
        if (s <= applied) { pending.delete(s); recordLat(now - t0); }
      }
    },
    error: e => { $('err').textContent = 'decoder: ' + e.message; configured = false; }
  });
}

function onVideo(buf) {
  const dv = new DataView(buf);
  const key = dv.getUint8(1) & 1, no = dv.getUint32(2), applied = dv.getUint32(6);
  encMs.push(dv.getUint16(10)); if (encMs.length > 30) encMs.shift();
  const au = new Uint8Array(buf, 12);
  rx.push([performance.now(), au.length]);
  if (!configured || dec.state !== 'configured') {
    if (!key) return;
    const codec = codecFromSps(au);
    if (!codec) return;
    newDecoder();
    dec.configure({ codec, codedWidth: W, codedHeight: H, optimizeForLatency: true });
    configured = true;
    $('st').textContent = '再生中 (' + codec + ')';
  }
  frameSeq.set(no, applied);
  dec.decode(new EncodedVideoChunk({ type: key ? 'key' : 'delta',
                                     timestamp: Math.round(no * 1e6 / FPS), data: au }));
}

function connect() {
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { $('st').textContent = '接続（キーフレーム待ち）'; configured = false; };
  ws.onmessage = e => {
    if (typeof e.data !== 'string') return onVideo(e.data);
    const m = JSON.parse(e.data);
    if (m.t === 'hid') return onHid(m);
    if (m.t === 'hello') {
      onHid(m.hid);
      source = m.source;
      const echo = m.fbEcho ? '（クリックを HDMI に描画）' : '';
      $('src').textContent = m.source === 'capture' ? 'キャプチャ' + echo
        : m.source === 'csi' ? 'CSI（' + m.sensor + '）' + echo : '画面バッファ';
    }
  };
  ws.onclose = () => { $('st').textContent = '切断（再接続します）'; setTimeout(connect, 1000); };
}

function pos(e) {
  const r = cv.getBoundingClientRect();
  return [Math.round((e.clientX - r.left) * W / r.width), Math.round((e.clientY - r.top) * H / r.height)];
}
function wsSend(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

// ---------------------------------------------------------------- mouse ----
// Absolute by default (x/y in video pixels; the server scales to 0..32767).
// "相対" uses Pointer Lock + movementX/Y and the boot (relative) mouse, for
// BIOS/UEFI setup screens and OSes that ignore absolute pointers.
let down = false, lastMove = 0, relDx = 0, relDy = 0, relTimer = null;
const isRel = () => document.pointerLockElement === cv;
function send(t, e, extra) {
  const [x, y] = pos(e); seq++;
  if (t === 'down') {
    if (source === 'screen') pending.set(seq, performance.now());
    else if (x >= 0 && y >= 0 && x < W && y < H)
      probes.push({ x, y, t0: performance.now(), base: px(x, y) });
  }
  wsSend(Object.assign({ t, x, y, b: e.button, buttons: e.buttons, seq }, extra || {}));
}
function flushRel(buttons, wheel) {
  if (relTimer) { clearTimeout(relTimer); relTimer = null; }
  wsSend({ t: 'rel', dx: Math.round(relDx), dy: Math.round(relDy), buttons, wheel: wheel || 0 });
  relDx = relDy = 0;
}
cv.addEventListener('mousedown', e => {
  cv.focus({ preventScroll: true });
  if (isRel()) flushRel(e.buttons); else { down = true; send('down', e); }
  e.preventDefault();
});
window.addEventListener('mouseup', e => {
  if (isRel()) flushRel(e.buttons);
  else if (down) { down = e.buttons !== 0; send('up', e); }
});
cv.addEventListener('mousemove', e => {
  if (isRel()) {                       // accumulate, send at most every 8 ms
    relDx += e.movementX; relDy += e.movementY;
    if (!relTimer) relTimer = setTimeout(() => flushRel(e.buttons), 8);
    return;
  }
  if (!down && !hidOpen) return;        // test screen only needs drags
  const now = performance.now(); if (now - lastMove < 8) return; lastMove = now;
  send('move', e);
});
let wheelAcc = 0;
cv.addEventListener('wheel', e => {
  e.preventDefault();
  wheelAcc += e.deltaMode === 0 ? e.deltaY / 100 : e.deltaY;   // px -> notches
  const n = Math.trunc(wheelAcc); if (!n) return;
  wheelAcc -= n;
  const w = Math.max(-127, Math.min(127, -n));                  // HID: + = away
  if (isRel()) flushRel(e.buttons, w); else send('wheel', e, { wheel: w });
}, { passive: false });
cv.addEventListener('contextmenu', e => e.preventDefault());

// ------------------------------------------------------------- keyboard ----
// Keys are sent by KeyboardEvent.code (physical position), so the target's
// own layout (JIS/US) applies, like a real keyboard. Captured while the video
// has focus. Browser/OS shortcuts (Ctrl+W/T/N, Alt+Tab, Win, ...) are only
// capturable with the Keyboard Lock API, which Chrome/Edge grant in
// fullscreen; Ctrl+Alt+Del never reaches a page -> use the special-key menu.
cv.tabIndex = 0;
const held = new Set();
function onKey(e, isDown) {
  if (document.activeElement !== cv || !e.code) return;
  e.preventDefault(); e.stopPropagation();
  if (isDown && e.repeat) return;        // the target generates its own repeat
  if (isDown) held.add(e.code); else held.delete(e.code);
  wsSend({ t: 'key', code: e.code, down: isDown });
  $('keys').textContent = held.size ? [...held].join('+') : '-';
}
document.addEventListener('keydown', e => onKey(e, true), true);
document.addEventListener('keyup', e => onKey(e, false), true);
function releaseAll() {
  // a key held while focus leaves never gets its keyup: release everything
  if (held.size) { held.clear(); $('keys').textContent = '-'; }
  wsSend({ t: 'release' });
}
cv.addEventListener('blur', releaseAll);
window.addEventListener('blur', releaseAll);
document.addEventListener('visibilitychange', () => { if (document.hidden) releaseAll(); });
cv.addEventListener('focus', () => updateInputState());
cv.addEventListener('blur', () => updateInputState());

// fullscreen + keyboard lock
const kbdLockSupported = !!(navigator.keyboard && navigator.keyboard.lock);
async function toggleFullscreen() {
  if (document.fullscreenElement) { await document.exitFullscreen(); return; }
  await wrap.requestFullscreen({ navigationUI: 'hide' });
  if (kbdLockSupported) { try { await navigator.keyboard.lock(); } catch (e) {} }
  cv.focus({ preventScroll: true });
}
document.addEventListener('fullscreenchange', () => {
  if (!document.fullscreenElement && kbdLockSupported) navigator.keyboard.unlock();
  updateInputState();
});
document.addEventListener('pointerlockchange', () => { releaseAll(); updateInputState(); });

const COMBOS = {
  'Ctrl+Alt+Del': ['ControlLeft', 'AltLeft', 'Delete'],
  'Ctrl+Shift+Esc': ['ControlLeft', 'ShiftLeft', 'Escape'],
  'Alt+Tab': ['AltLeft', 'Tab'], 'Alt+F4': ['AltLeft', 'F4'],
  'Win': ['MetaLeft'], 'Win+R': ['MetaLeft', 'KeyR'], 'Win+D': ['MetaLeft', 'KeyD'],
  'Win+L': ['MetaLeft', 'KeyL'], 'Ctrl+Esc': ['ControlLeft', 'Escape'],
  'Ctrl+W': ['ControlLeft', 'KeyW'], 'Ctrl+T': ['ControlLeft', 'KeyT'],
  'Ctrl+N': ['ControlLeft', 'KeyN'], 'Ctrl+Tab': ['ControlLeft', 'Tab'],
  'PrintScreen': ['PrintScreen'], 'F11': ['F11'], 'Esc': ['Escape'],
  '半角/全角': ['Backquote'], '変換': ['Convert'], '無変換': ['NonConvert'],
  'Ctrl+Alt+F1': ['ControlLeft', 'AltLeft', 'F1'], 'Ctrl+Alt+F2': ['ControlLeft', 'AltLeft', 'F2'],
  'Ctrl+Alt+F7': ['ControlLeft', 'AltLeft', 'F7'],
};
const comboSel = $('combo');
for (const name of Object.keys(COMBOS)) comboSel.add(new Option(name, name));
comboSel.addEventListener('change', () => {
  if (comboSel.value) wsSend({ t: 'combo', codes: COMBOS[comboSel.value] });
  comboSel.value = ''; cv.focus({ preventScroll: true });
});
$('fs').addEventListener('click', toggleFullscreen);
$('rel').addEventListener('click', () => {
  if (isRel()) document.exitPointerLock(); else cv.requestPointerLock();
});
$('rall').addEventListener('click', () => { releaseAll(); cv.focus({ preventScroll: true }); });
$('typebtn').addEventListener('click', () => {
  const text = prompt('相手の PC にタイプする文字列（ASCII のみ）:');
  if (text) wsSend({ t: 'type', text, layout: $('layout').value });
  cv.focus({ preventScroll: true });
});
try { $('layout').value = localStorage.getItem('kvm.layout') || 'jis'; } catch (e) {}
$('layout').addEventListener('change', () => {
  try { localStorage.setItem('kvm.layout', $('layout').value); } catch (e) {}
});

let hidOpen = false;
function onHid(h) {
  hidOpen = !!(h && h.open);
  if (!h) $('hid').textContent = 'なし（--hid 未指定）';
  else if (!h.open) $('hid').textContent = '未接続 ' + h.path;
  else $('hid').textContent = (h.version || '接続') ;
  const l = h ? h.leds || 0 : 0;
  $('leds').textContent = h && h.open
    ? ['Num', 'Caps', 'Scroll'].filter((n, i) => l & (1 << i)).join(' ') || '-' : '-';
  updateInputState();
}
function updateInputState() {
  const parts = [];
  parts.push(document.activeElement === cv ? 'キー入力中' : '画面をクリックで入力開始');
  if (isRel()) parts.push('相対マウス（Esc で解除）');
  if (document.fullscreenElement)
    parts.push(kbdLockSupported ? 'キーロック有効（Esc 長押しで全画面解除）' : 'キーロック非対応');
  $('inp').textContent = parts.join(' / ');
  cv.style.outlineColor = document.activeElement === cv ? '#4af' : '#333';
  $('rel').textContent = isRel() ? '絶対マウス' : '相対マウス';
}
updateInputState();

setInterval(() => {
  const now = performance.now();
  rx = rx.filter(([t]) => now - t < 2000);
  $('fps').textContent = (rx.length / 2).toFixed(1);
  $('kbps').textContent = (rx.reduce((a, [, n]) => a + n, 0) * 8 / 2 / 1000).toFixed(0);
  if (encMs.length) $('enc').textContent = (encMs.reduce((a, b) => a + b, 0) / encMs.length).toFixed(0);
}, 500);

if (!('VideoDecoder' in window)) {
  $('err').textContent = 'このブラウザは WebCodecs 非対応です（Chrome / Edge / Safari 16.4+ を使用）';
} else connect();
</script>
</body></html>
"""


def make_handler(kvm):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            pass

        def do_GET(self):
            if self.path == "/ws":
                return self.websocket()
            if self.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            body = (PAGE.replace("__W__", str(kvm.args.width))
                        .replace("__H__", str(kvm.args.height))
                        .replace("__FPS__", str(kvm.args.fps))).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def websocket(self):
            key = self.headers.get("Sec-WebSocket-Key")
            if not key or self.headers.get("Upgrade", "").lower() != "websocket":
                self.send_error(400)
                return
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            sock = self.connection
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            c = WSClient(sock, self.client_address[0])
            kvm.add_client(c)
            threading.Thread(target=self.sender, args=(c,), daemon=True).start()
            try:
                while c.alive:
                    op, data = c.recv()
                    if op == 8:                       # close
                        break
                    if op == 9:                       # ping
                        c.send(data, opcode=10)
                    elif op == 1:
                        try:
                            kvm.on_input(c, json.loads(data))
                        except ValueError:
                            pass
            except (ConnectionError, OSError):
                pass
            finally:
                kvm.remove_client(c)

        @staticmethod
        def sender(c):
            try:
                while c.alive:
                    try:
                        msg = c.q.get(timeout=1)
                    except queue.Empty:
                        continue
                    c.send(msg)
            except OSError:
                c.alive = False

    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def local_addresses():
    addrs = set()
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout
        addrs.update(out.split())
    except OSError:
        pass
    return sorted(a for a in addrs if ":" not in a)


def ensure_cert(cert, key):
    """WebCodecs needs a secure context (https or localhost), so serve TLS
    with a self-signed certificate made by openssl on first start."""
    if os.path.exists(cert) and os.path.exists(key):
        return
    os.makedirs(os.path.dirname(cert), exist_ok=True)
    host = socket.gethostname()
    san = ",".join(["DNS:" + host, "DNS:" + host + ".local", "DNS:localhost",
                    "IP:127.0.0.1"] + ["IP:" + a for a in local_addresses()])
    log("creating self-signed certificate", cert, "(" + san + ")")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-days", "3650", "-subj", "/CN=" + host,
                    "-addext", "subjectAltName=" + san,
                    "-keyout", key, "-out", cert],
                   check=True, capture_output=True)
    os.chmod(key, 0o600)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=8443)
    p.add_argument("--http", action="store_true",
                   help="plain http (only usable via localhost, e.g. an ssh tunnel)")
    p.add_argument("--cert", default="~/.config/dev_tinker/kvm-cert.pem")
    p.add_argument("--key", default="~/.config/dev_tinker/kvm-key.pem")
    p.add_argument("--source", choices=["auto", "capture", "csi", "screen"], default="auto",
                   help="capture = UVC MJPEG stick, csi = rkisp1 (OV5647 / TC358743), "
                        "screen = built-in test screen")
    p.add_argument("--device", default="auto", help="UVC device (default: first /dev/v4l/by-id/*index0)")
    p.add_argument("--csi-media", default="auto",
                   help="media device of the rkisp1 ISP (default: the one with rkisp1_mainpath)")
    p.add_argument("--csi-edid", default="",
                   help="TC358743 EDID file (default: ~/camera/edid-tc358743.txt, or "
                        "patches/camera/edid-tc358743.txt of an rk3288-mainline-media "
                        "checkout next to this repository)")
    p.add_argument("--capture-fps", type=int, default=60,
                   help="frame rate requested from the capture stick")
    p.add_argument("--fb-echo", default="", metavar="FB",
                   help="loopback test: draw clicks on this framebuffer (e.g. /dev/fb0)")
    p.add_argument("--hid", default="auto", metavar="TTY",
                   help="RP2350 HID bridge: /dev/ttyS* (UART firmware), /dev/ttyACM* "
                        "(CDC firmware), auto = the CDC firmware if plugged in, none = off")
    p.add_argument("--hid-baud", type=int, default=115200, help="UART baud rate")
    p.add_argument("--width", type=int, default=0,
                   help="video size (default: 1920x1080 for capture = dot-by-dot "
                        "for a 1080p source, 1280x720 for the test screen)")
    p.add_argument("--height", type=int, default=0)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--qp", type=int, default=28, help="constant QP, or initial QP with --bitrate")
    p.add_argument("--bitrate", type=int, default=0, help="CBR target in bit/s (0 = constant QP)")
    p.add_argument("--keyint", type=int, default=30,
                   help="frames between IDR (also bounds how long P-frame drift lingers)")
    p.add_argument("--idle-stop", type=float, default=10, metavar="SEC",
                   help="stop capture/encoding SEC seconds after the last viewer leaves "
                        "(-1 = keep running)")
    p.add_argument("--gst-dir",default="~/h264enc/gst",
                   help="GStreamer build with v4l2slh264enc (lib/gstreamer-1.0)")
    args = p.parse_args()
    if args.device == "auto":
        byid = "/dev/v4l/by-id"
        cands = sorted(os.listdir(byid)) if os.path.isdir(byid) else []
        cands = [os.path.join(byid, c) for c in cands if c.endswith("-video-index0")]
        args.device = cands[0] if cands else ""
    if args.csi_media == "auto":
        args.csi_media = Csi.find() if args.source in ("auto", "csi") else ""
    if args.source == "auto":
        args.source = "capture" if args.device else "csi" if args.csi_media else "screen"
    if args.source == "capture" and not args.device:
        p.error("no capture device found (use --device or --source screen)")
    csi = None
    if args.source == "csi":
        if not args.csi_media:
            p.error("no rkisp1 media device (camera overlay active? see "
                    "scripts/tinker-camera.sh in rk3288-mainline-media)")
        if not args.csi_edid:
            here = os.path.dirname(os.path.abspath(__file__))
            for f in (os.path.expanduser("~/camera/edid-tc358743.txt"),
                      os.path.join(here, "..", "rk3288-mainline-media", "patches", "camera",
                                   "edid-tc358743.txt")):
                if os.path.exists(f):
                    args.csi_edid = f
                    break
        try:
            csi = Csi(args.csi_media, args.csi_edid)
            if args.width % 2 or args.height % 2:
                p.error("width/height must be even")
            args.width, args.height = csi.configure(args.width, args.height)
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            p.error("csi: %s" % e)
    if args.hid == "auto":
        byid = "/dev/serial/by-id"
        cands = sorted(os.listdir(byid)) if os.path.isdir(byid) else []
        cands = [os.path.join(byid, c) for c in cands if "kvm-hid" in c]
        args.hid = cands[0] if cands else ""
    elif args.hid == "none":
        args.hid = ""
    if args.hid and args.hid_baud not in HidSerial.BAUDS:
        p.error("unsupported --hid-baud")
    if not args.width or not args.height:
        args.width, args.height = (1920, 1080) if args.source == "capture" else (1280, 720)
    if args.width % 2 or args.height % 2:
        p.error("width/height must be even")     # 4:2:0; the encoder crops to 16-px MBs
    log("source:", args.source,
        args.device if args.source == "capture" else csi.sensor if csi else "",
        "%dx%d" % (args.width, args.height), "hid:", args.hid or "none")

    kvm = KVM(args, csi)
    srv = Server(("0.0.0.0", args.port), make_handler(kvm))
    scheme = "http"
    if not args.http:
        cert, key = os.path.expanduser(args.cert), os.path.expanduser(args.key)
        ensure_cert(cert, key)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        scheme = "https"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for a in [socket.gethostname()] + local_addresses():
        log("%s://%s:%d/" % (scheme, a, args.port))
    def on_term(signum, frame):
        raise KeyboardInterrupt      # kill / systemd stop: take GStreamer down too
    signal.signal(signal.SIGTERM, on_term)
    try:
        kvm.run()
    except KeyboardInterrupt:
        pass
    finally:
        kvm.encoder.stop()
        srv.shutdown()


if __name__ == "__main__":
    main()
