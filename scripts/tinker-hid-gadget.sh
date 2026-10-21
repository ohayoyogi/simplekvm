#!/bin/bash
# Make the Tinker Board's micro-USB (OTG) port a USB HID device:
# keyboard (/dev/hidg0) + absolute mouse (/dev/hidg1, 0..32767).
# Run as root:  sudo ./tinker-hid-gadget.sh [up|down]
#
# Report formats (same as the RP2350 firmware, firmware/kvm-hid/src/hid_reports.h):
#   hidg0: 8 bytes  <mod> 0 <k1> <k2> <k3> <k4> <k5> <k6>
#   hidg1: 6 bytes  <buttons> <x lo> <x hi> <y lo> <y hi> <wheel(int8)>
set -euo pipefail

G=/sys/kernel/config/usb_gadget/kvm
UDC=ff580000.usb
ROLE=/sys/class/usb_role/ff580000.usb-role-switch/role

down() {
	[ -d "$G" ] || return 0
	echo "" > "$G/UDC" 2>/dev/null || true
	rm -f "$G"/configs/c.1/hid.* 2>/dev/null || true
	rmdir "$G"/configs/c.1/strings/0x409 "$G"/configs/c.1 \
		"$G"/functions/hid.* "$G"/strings/0x409 "$G" 2>/dev/null || true
}

up() {
	mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config
	# Force device mode (needs tinker-otg-role-switch overlay)
	[ -w "$ROLE" ] && echo device > "$ROLE"

	mkdir -p "$G"
	cd "$G"
	echo 0x1d6b > idVendor   # Linux Foundation
	echo 0x0104 > idProduct  # Multifunction Composite Gadget
	echo 0x0100 > bcdDevice
	echo 0x0200 > bcdUSB
	mkdir -p strings/0x409
	echo "dev_tinker" > strings/0x409/manufacturer
	echo "Tinker KVM HID" > strings/0x409/product
	echo "0001" > strings/0x409/serialnumber

	# Boot keyboard
	mkdir -p functions/hid.kbd
	echo 1 > functions/hid.kbd/protocol
	echo 1 > functions/hid.kbd/subclass
	echo 8 > functions/hid.kbd/report_length
	printf '%b' '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' \
		> functions/hid.kbd/report_desc

	# Absolute mouse: 5 buttons, X/Y 0..32767, wheel
	mkdir -p functions/hid.mouse
	echo 0 > functions/hid.mouse/protocol
	echo 0 > functions/hid.mouse/subclass
	echo 6 > functions/hid.mouse/report_length
	printf '%b' '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x05\x15\x00\x25\x01\x95\x05\x75\x01\x81\x02\x95\x01\x75\x03\x81\x03\x05\x01\x09\x30\x09\x31\x16\x00\x00\x26\xff\x7f\x75\x10\x95\x02\x81\x02\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x01\x81\x06\xc0\xc0' \
		> functions/hid.mouse/report_desc

	mkdir -p configs/c.1/strings/0x409
	echo "HID" > configs/c.1/strings/0x409/configuration
	echo 100 > configs/c.1/MaxPower
	ln -sf "$G/functions/hid.kbd" configs/c.1/
	ln -sf "$G/functions/hid.mouse" configs/c.1/

	echo "$UDC" > UDC
	echo "UDC state: $(cat /sys/class/udc/$UDC/state)"
	ls -l /dev/hidg*
}

case "${1:-up}" in
up) down; up ;;
down) down ;;
*) echo "usage: $0 [up|down]" >&2; exit 1 ;;
esac
