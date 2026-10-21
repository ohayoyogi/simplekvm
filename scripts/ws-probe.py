#!/usr/bin/env python3
"""Headless kvm.py viewer for tests: connect to /ws, count video frames.

    python3 ws-probe.py [SECONDS] [--port 8443] [--save out.h264]
"""
import argparse
import base64
import os
import socket
import ssl
import struct
import time

p = argparse.ArgumentParser()
p.add_argument("seconds", type=float, nargs="?", default=5)
p.add_argument("--host", default="127.0.0.1")
p.add_argument("--port", type=int, default=8443)
p.add_argument("--save", default="")
a = p.parse_args()

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
s = ctx.wrap_socket(socket.create_connection((a.host, a.port), timeout=10), server_hostname="x")
key = base64.b64encode(os.urandom(16)).decode()
s.sendall(("GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % key).encode())
f = s.makefile("rb")
while f.readline() not in (b"\r\n", b""):
    pass
out = open(a.save, "wb") if a.save else None
t0 = time.time()
first = None
frames = keys = nbytes = 0
while time.time() - t0 < a.seconds:
    try:
        h = f.read(2)
    except socket.timeout:
        break
    if len(h) < 2:
        break
    op, n = h[0] & 15, h[1] & 127
    if n == 126:
        n = struct.unpack("!H", f.read(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", f.read(8))[0]
    payload = f.read(n)
    if op == 1:
        print("text:", payload[:160].decode(errors="replace"))
    elif op == 2 and payload[:1] == b"\x01":
        if first is None:
            first = time.time() - t0
        frames += 1
        keys += payload[1]
        nbytes += n
        if out:
            out.write(payload[12:])
el = max(time.time() - t0 - (first or 0), 1e-3)
print("first video %s s, %d frames (%d key), %.1f fps, %.1f Mbit/s"
      % ("-" if first is None else "%.2f" % first, frames, keys, frames / el, nbytes * 8 / el / 1e6))
