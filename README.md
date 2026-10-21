# simplekvm

A minimal IP-KVM (keyboard, video, mouse over the LAN) that installs on an
ordinary Linux single-board computer instead of replacing its OS.

```
browser ──HTTPS / WebSocket── SBC running kvm.py ──┬─ HDMI capture (UVC stick, or CSI-2 bridge) ── HDMI ── target PC
  (WebCodecs H.264 decode,                         └─ RP2350 HID bridge (UART or USB CDC) ── USB ──── target PC
   keyboard / mouse capture)                          or the board's own OTG port as a USB HID gadget
```

- **One Python file, standard library only** (`kvm.py`). Video encoding is
  done by GStreamer with the SoC's hardware H.264 encoder.
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

- An SBC with a V4L2 stateless H.264 encoder exposed to GStreamer as
  `v4l2slh264enc` (on RK3288: the patches above), GStreamer 1.24 runtime
  (`gstreamer1.0-plugins-base`, `-good`, `-bad`), `openssl`, Python 3.
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
| `--width/--height` | output size (CSI: scaled by the ISP) |
| `--fps`, `--qp`, `--bitrate`, `--keyint` | encoder settings |
| `--hid auto\|gadget\|none\|TTY` | RP2350 bridge or USB gadget (`auto`: the CDC firmware, else the gadget if set up) |
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

## Repository layout

| Path | Contents |
|---|---|
| `kvm.py` | the whole server: capture/encoder control, HTTPS + WebSocket, the page, HID |
| `firmware/kvm-hid/` | RP2350-USB-A HID firmware, UART and USB CDC variants (README in Japanese) |
| `scripts/ws-probe.py` | headless viewer for tests (fps, first-frame time, saves the H.264) |
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
- No `install.sh` / systemd unit yet; kvm.py is started by hand.
- No authentication: use it on a trusted LAN only.

## License

MIT, see [LICENSE](LICENSE). The kernel and GStreamer patches in
rk3288-mainline-media are under the licenses of the projects they patch.
