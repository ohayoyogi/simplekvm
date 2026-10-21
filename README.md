# simplekvm

A minimal IP-KVM (keyboard, video, mouse over the LAN) that installs on an
ordinary Linux single-board computer instead of replacing its OS.

```
browser ──HTTPS / WebSocket── SBC running kvm.py ──┬─ HDMI capture (UVC stick, or CSI-2 bridge) ── HDMI ── target PC
  (WebCodecs H.264 decode,                         └─ RP2350 HID bridge (UART or USB CDC) ── USB ──── target PC
   keyboard / mouse capture)
```

- **One Python file, standard library only** (`kvm.py`). Video encoding is
  done by GStreamer with the SoC's hardware H.264 encoder.
- **H.264 over WebSocket, decoded in the browser with WebCodecs**: no
  WebRTC server, no MJPEG bandwidth.
- **Keyboard and mouse through a small RP2350 board** acting as a USB HID
  device (boot keyboard, absolute and relative mouse) for the target PC.

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
**rk3288-mainline-media** (to be published): Hantro H1 stateless H.264
encoder, rkisp1 / MIPI DPHY support for RK3288, TC358743 fixes, device tree
overlays and EDIDs.

## Requirements

- An SBC with a V4L2 stateless H.264 encoder exposed to GStreamer as
  `v4l2slh264enc` (on RK3288: the patches above), GStreamer 1.24 runtime
  (`gstreamer1.0-plugins-base`, `-good`, `-bad`), `openssl`, Python 3.
- A video source: a UVC HDMI capture stick, or on RK3288 a TC358743
  bridge / OV5647 camera on the CSI connector (`media-ctl` and `v4l2-ctl`
  from `v4l-utils`).
- A browser with WebCodecs: Chrome or Edge. WebCodecs needs a secure
  context, so kvm.py serves HTTPS with a self-signed certificate.
- For keyboard and mouse: a Waveshare RP2350-USB-A with the firmware in
  [`firmware/kvm-hid`](firmware/kvm-hid/README.md).

## Quick start

```sh
python3 kvm.py                       # picks a UVC stick, then CSI, else the test screen
python3 kvm.py --source csi --fps 60 # TC358743 / C790 on the RK3288 ISP
python3 kvm.py --hid /dev/ttyS1      # RP2350 UART firmware on the 40-pin header
```

Open `https://<board>:8443/` and accept the self-signed certificate once
(it is generated in `~/.config/dev_tinker/` on first start). Encoding
starts with the first viewer and stops 10 s after the last one leaves.

Useful options (`python3 kvm.py --help` for all):

| Option | Meaning |
|---|---|
| `--source auto\|capture\|csi\|screen` | video source |
| `--width/--height` | output size (CSI: scaled by the ISP) |
| `--fps`, `--qp`, `--bitrate`, `--keyint` | encoder settings |
| `--hid auto\|none\|TTY` | RP2350 bridge (`auto` finds the CDC firmware) |
| `--csi-edid FILE` | EDID for the TC358743 (e.g. one that offers 1080p50) |
| `--fb-echo /dev/fb0` | loopback latency test: draw browser clicks on the board's screen |
| `--idle-stop SEC` | stop capture/encoding when nobody watches (`-1` = never) |
| `--gst-dir DIR` | where the patched GStreamer plugin is (`~/h264enc/gst`) |

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

## Repository layout

| Path | Contents |
|---|---|
| `kvm.py` | the whole server: capture/encoder control, HTTPS + WebSocket, the page, HID |
| `firmware/kvm-hid/` | RP2350-USB-A HID firmware, UART and USB CDC variants (README in Japanese) |
| `scripts/ws-probe.py` | headless viewer for tests (fps, first-frame time, saves the H.264) |
| `scripts/tinker-hid-gadget.sh`, `scripts/tinker-otg-role-switch.dts` | experiment: the board itself as a USB HID gadget (untested) |
| `scripts/99-dev_tinker-usb-power.rules` | keeps UVC capture sticks from autosuspending |
| `scripts/serial.ps1`, `scripts/send-file.ps1` | Windows helpers for the board's serial console |

## Known limitations

- Tested on one board only; the RP2350 firmware has not been run on
  hardware yet.
- With the C790 on RK3288 the ISP's MI_CTRL register resets itself a few
  times a minute; the patched rkisp1 driver restores it and drops one
  frame each time.
- No `install.sh` / systemd unit yet; kvm.py is started by hand.
- No authentication: use it on a trusted LAN only.

## License

MIT, see [LICENSE](LICENSE). The kernel and GStreamer patches in
rk3288-mainline-media are under the licenses of the projects they patch.
