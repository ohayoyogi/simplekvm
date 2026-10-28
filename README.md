# simplekvm

A minimal IP-KVM (keyboard, video, mouse over the LAN) that installs on an
ordinary Linux single-board computer instead of replacing its OS.

```
browser ──HTTPS / WebSocket── SBC running kvm.py ──┬─ HDMI capture (UVC stick, or CSI-2 bridge) ── HDMI ── target PC
  (WebCodecs H.264 decode,                         └─ RP2350 HID bridge (UART or USB CDC) ── USB ──── target PC
   keyboard / mouse capture)                          or the board's own OTG port as a USB HID gadget
```

- **One Python file, standard library only** (`kvm.py`). Video encoding is
  done by GStreamer: the SoC's hardware H.264 encoder on the RK3288, VA-API
  or NVENC on a PC, or x264 in software. With a UVC capture stick and the
  RP2350 it also runs on an ordinary x86 Linux machine.
- **H.264 over WebSocket, decoded in the browser with WebCodecs**: no
  WebRTC server, no MJPEG bandwidth.
- **Keyboard and mouse through a small RP2350 board** acting as a USB HID
  device (boot keyboard, absolute and relative mouse) for the target PC,
  or through the board itself as a USB HID gadget (same reports).

Status: **working prototype**, developed and tested on one ASUS Tinker
Board (RK3288) running Armbian. Expect rough edges.

## What works (Tinker Board, RK3288)

| Video source | Path | Frame rate | Browser click → screen |
|---|---|---|---|
| UVC HDMI capture stick (MJPEG) | CPU JPEG decode → H1 H.264 | 720p / 1080p up to ~49 fps | 174 ms (720p), 211 ms (1080p) |
| Geekworm C790 (TC358743, HDMI → CSI-2) | ISP → NV12 → H1 H.264, no CPU decode | 720p60, 1080p50 | **113 ms** (720p60) |
| Built-in test screen | NV12 rendered by kvm.py | 720p30 | 49–152 ms |

Latency is measured end to end: a click in the browser is drawn on the
board's own HDMI output (`--fb-echo`), captured, encoded, sent and decoded
until the pixel changes in the browser.

The H.264 encoder and the CSI-2 capture need kernel and GStreamer patches
that mainline does not have yet. They live in a separate repository,
**[rk3288-mainline-media](https://github.com/ohayoyogi/rk3288-mainline-media)**: Hantro H1 stateless H.264
encoder, rkisp1 / MIPI DPHY support for RK3288, TC358743 fixes, device tree
overlays and EDIDs.

## Requirements

- A Linux machine with Python 3, `openssl` and GStreamer 1.24
  (`gstreamer1.0-tools`, `-plugins-base`, `-good`, `-bad`), and an H.264
  encoder element (`--encoder`, default: the first one found):
  - `v4l2slh264enc`: a V4L2 stateless encoder, e.g. the RK3288 VPU with the
    patches above;
  - `vah264enc` / `vaapih264enc`: Intel or AMD graphics (VA-API);
  - `nvh264enc`: NVIDIA (NVENC);
  - `x264enc` (`gstreamer1.0-plugins-ugly`): software, any x86 PC.

  On x86 so far only x264 has been tried (Ubuntu 24.04 container, test
  screen and the capture path's formats); the VA-API and NVENC settings are
  untested.
- A video source: a UVC HDMI capture stick, or on RK3288 a TC358743
  bridge / OV5647 camera on the CSI connector (`media-ctl` and `v4l2-ctl`
  from `v4l-utils`).
- A browser with WebCodecs: Chrome or Edge. WebCodecs needs a secure
  context, so kvm.py serves HTTPS with a self-signed certificate.
- For keyboard and mouse: a Waveshare RP2350-USB-A with the firmware in
  [`firmware/kvm-hid`](firmware/kvm-hid/README.md), or a USB OTG port on
  the board (see "USB HID gadget" below).

## Quick start

```sh
python3 kvm.py                       # picks a UVC stick, then CSI, else the test screen
python3 kvm.py --source csi --fps 60 # TC358743 / C790 on the RK3288 ISP
python3 kvm.py --hid /dev/ttyS1      # RP2350 UART firmware on the 40-pin header
python3 kvm.py --hid gadget          # the board itself as a USB HID gadget
```

Open `https://<board>:8443/` and accept the self-signed certificate once
(it is generated in `~/.config/dev_tinker/` on first start). Encoding
starts with the first viewer and stops 10 s after the last one leaves.

Useful options (`python3 kvm.py --help` for all):

| Option | Meaning |
|---|---|
| `--source auto\|capture\|csi\|screen` | video source |
| `--bind ADDR`, `--port N`, `--http` | where to listen (default `0.0.0.0:8443`, HTTPS); see "Authentication" |
| `--width/--height` | output size (CSI: scaled by the ISP). Without them a TC358743 stream follows the HDMI mode: a new mode restarts the stream at its size (a few seconds without picture) and the page resizes |
| `--encoder auto\|v4l2sl\|va\|vaapi\|nv\|x264` | H.264 encoder (`auto`: the first GStreamer has, in this order) |
| `--fps`, `--qp`, `--bitrate`, `--keyint` | encoder settings |
| `--hid auto\|gadget\|none\|TTY` | RP2350 bridge or USB gadget (`auto`: the CDC firmware, else the gadget if set up) |
| `--csi-edid FILE` | EDID for the TC358743 (e.g. one that offers 1080p50); not rewritten if already loaded, e.g. at boot by `tinker-camera.sh boot-edid on` |
| `--fb-echo /dev/fb0` | loopback latency test: draw browser clicks on the board's screen |
| `--idle-stop SEC` | stop capture/encoding when nobody watches (`-1` = never) |
| `--gst-dir DIR` | where the patched GStreamer plugin is (`~/h264enc/gst`) |

### Start at boot (systemd)

```sh
sudo scripts/install-service.sh install          # runs kvm.py as the sudo user, from this checkout
sudoedit /etc/default/simplekvm                  # KVM_ARGS="--source csi --fps 60"
sudo systemctl restart simplekvm
journalctl -u simplekvm -f                       # log
sudo scripts/install-service.sh uninstall
```

The unit (`scripts/simplekvm.service`) restarts kvm.py when it exits with an
error, e.g. when the camera modules are not up yet at boot.

## Authentication (put it in front of kvm.py)

kvm.py has no login of its own: whoever reaches the port controls the
target PC. Keep it on a trusted network, and add authentication outside
kvm.py with one of the setups below. Never expose the port to the internet
directly.

In each setup kvm.py listens on localhost only (`--bind 127.0.0.1`), so the
port cannot be reached around the front end. The browser still needs a
secure context for WebCodecs: HTTPS from the proxy, or `http://localhost`
through a tunnel, so kvm.py itself can run with `--http`.

### Caddy (Basic auth, HTTPS)

```sh
python3 kvm.py --bind 127.0.0.1 --port 8080 --http --source csi --fps 60
caddy hash-password                  # prints the hash for the password you type
```

`/etc/caddy/Caddyfile`:

```
https://kvm.lan {
    tls internal                     # Caddy's own CA; or a real certificate
    basic_auth {
        admin <hash from caddy hash-password>
    }
    reverse_proxy 127.0.0.1:8080     # WebSocket (/ws) is proxied as is
}
```

`tls internal` needs the browser to trust Caddy's root certificate
(`caddy trust`, or import it on each client); otherwise accept the
warning once, as with kvm.py's own certificate.

### nginx (Basic auth, HTTPS)

```sh
python3 kvm.py --bind 127.0.0.1 --port 8080 --http --source csi --fps 60
sudo htpasswd -c /etc/nginx/kvm.htpasswd admin   # apache2-utils
```

```nginx
server {
    listen 443 ssl;
    server_name kvm.lan;
    ssl_certificate     /etc/nginx/kvm-cert.pem;
    ssl_certificate_key /etc/nginx/kvm-key.pem;

    auth_basic           "simplekvm";
    auth_basic_user_file /etc/nginx/kvm.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;     # WebSocket (/ws)
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 1h;                      # keep an idle session open
        proxy_buffering off;
    }
}
```

The certificate kvm.py made (`~/.config/dev_tinker/kvm-*.pem`) can be
reused here. For only a few known clients, client certificates
(`ssl_verify_client on;`, or `tls { client_auth ... }` in Caddy) are a
stronger alternative to a password.

### SSH tunnel (one user, nothing to install)

```sh
python3 kvm.py --bind 127.0.0.1 --port 8080 --http --source csi --fps 60   # on the board
ssh -N -L 8080:127.0.0.1:8080 user@board                                  # on the client
```

Then open `http://localhost:8080/`. SSH keys are the authentication.

### Tailscale / WireGuard (from outside the LAN)

Join the board to a VPN and reach kvm.py only through it, without opening
a port on the router. With Tailscale either bind to the board's tailnet
address (`--bind 100.x.y.z`), or keep `--bind 127.0.0.1 --http` and let
Tailscale serve it with a certificate and its access controls:

```sh
sudo tailscale serve --bg --https=443 http://127.0.0.1:8080
```

Open `https://<board>.<tailnet>.ts.net/`. Only devices allowed by the
tailnet's ACLs can connect.

### With the systemd unit

Put the options in `/etc/default/simplekvm`, e.g.
`KVM_ARGS="--bind 127.0.0.1 --port 8080 --http --source csi --fps 60"`.
When binding to a VPN address, kvm.py may start before that address
exists; the unit then restarts it until it does.

## Keyboard and mouse in the browser

- Keys are sent by physical position (`KeyboardEvent.code` → HID usage),
  including the JIS keys; a JIS/US switch only affects "type text".
- In fullscreen, the Keyboard Lock API (Chrome/Edge) passes Win, Alt+Tab,
  Esc and similar shortcuts to the target; a menu sends combinations such
  as Ctrl+Alt+Del.
- Absolute mouse by default; a relative mode (Pointer Lock) for BIOS/UEFI
  screens and OSes that ignore absolute pointers.
- All keys are released when the page loses focus or the last viewer
  leaves.

The page's labels are in Japanese for now.

## USB HID gadget (Tinker Board)

Instead of the RP2350, the board's micro-USB OTG port can be the keyboard
and mouse itself: `sudo scripts/tinker-hid-gadget.sh` creates a boot
keyboard (`/dev/hidg0`, LEDs readable), an absolute mouse (`/dev/hidg1`)
and a relative boot mouse (`/dev/hidg2`) with configfs, then
`kvm.py --hid gadget` writes to them. The stock Armbian kernel config
already has everything (`USB_DWC2_DUAL_ROLE`, `USB_CONFIGFS_F_HID`); no
device-tree overlay was needed.

- On the Tinker Board the micro-USB data lines go through a BQ24392
  charger-detect switch that only connects while VBUS is present. **Do not
  use a VBUS-cutting cable ("USB power blocker")**: use a normal cable to
  the target PC and power the board from the 5V pins of the 40-pin header
  (a solid 5 V / 3 A supply; a weak one ends in a reset loop). The board's
  power switch (NX5P3090) blocks current flowing back into the PC.
- Tested in loopback (micro-USB into the board's own USB-A port): keys,
  absolute and relative mouse and the keyboard LEDs all work. Not yet tried
  on a separate PC (BIOS, target reboot, suspend).
- The keyboard disappears from the target while the board reboots; the
  RP2350 stays powered by the target and does not.

## Testing with a target PC

`scripts/target/kvm-target.py` runs on the PC the KVM controls (Linux with
a desktop, Python 3, Chromium/Chrome or Firefox). It shows a full-screen
test page and a small HTTP API on port 8800: a frame counter drawn as black
and white blocks on every display refresh, a white square at each click,
a log of the key and mouse events that arrive, test tones, and display
modes through `xrandr` (X11 sessions).

`scripts/kvm-test.py` runs on the KVM board next to kvm.py, talks to kvm.py
like a browser and checks the results against the target:

```sh
# on the target, from the desktop session
python3 kvm-target.py
# on the board
python3 scripts/kvm-test.py --target http://TARGET:8800 all      # status, input, video, latency, mode
python3 scripts/kvm-test.py --target http://TARGET:8800 input --layout jis
python3 scripts/kvm-test.py --target http://TARGET:8800 audio --alsa-device hw:CARD=X
```

| Test | Checks |
|---|---|
| `status` | test page open, HID connected, video arriving |
| `input` | click positions (absolute mouse), key codes, Shift/Ctrl, typed text in the target's layout, wheel, relative mouse |
| `video` | every frame decodes, and the frame counter neither repeats (stale frames) nor jumps (dropped frames) |
| `latency` | click sent to kvm.py until the frame showing it arrives (the browser's decode not included) |
| `mode` | for each display mode: the stream follows it, and video keeps flowing |
| `audio` | a tone played on the target, recorded on the board (`arecord`): frequency, level, left/right |

The video is decoded on the board with GStreamer (`--decoder`, default the
hardware `v4l2slh264dec`). The test API has no authentication; run it on a
test network.

## Repository layout

| Path | Contents |
|---|---|
| `kvm.py` | the whole server: capture/encoder control, HTTPS + WebSocket, the page, HID |
| `firmware/kvm-hid/` | RP2350-USB-A HID firmware, UART and USB CDC variants (README in Japanese) |
| `scripts/ws-probe.py` | headless viewer for tests (fps, first-frame time, saves the H.264) |
| `scripts/simplekvm.service`, `scripts/install-service.sh` | start kvm.py at boot with systemd (see above) |
| `scripts/kvm-test.py`, `scripts/target/kvm-target.py` | end-to-end tests against a target PC (see "Testing with a target PC") |
| `scripts/tinker-hid-gadget.sh` | the board itself as a USB HID gadget (see above) |
| `scripts/tinker-otg-role-switch.dts` | overlay to force the OTG port's role by software (not needed so far, untested) |
| `scripts/99-dev_tinker-usb-power.rules` | keeps UVC capture sticks from autosuspending |
| `scripts/serial.ps1`, `scripts/send-file.ps1` | Windows helpers for the board's serial console |

## Known limitations

- Tested on one board only; the RP2350 firmware has not been run on
  hardware yet.
- With the C790 on RK3288 the ISP's MI_CTRL register resets itself a few
  times a minute; the patched rkisp1 driver restores it and drops one
  frame each time.
- 1080p50 from the C790 is captured at 50 fps, but encoding it currently
  falls far behind (under 12 fps in a test); 720p60 and 1080p30 run at
  full rate.
- No authentication of its own: use it on a trusted LAN only, or behind
  one of the front ends in "Authentication".

## License

MIT, see [LICENSE](LICENSE). The kernel and GStreamer patches in
rk3288-mainline-media are under the licenses of the projects they patch.
