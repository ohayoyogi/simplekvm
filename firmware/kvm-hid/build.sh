#!/bin/bash
# Build kvm_hid_uart.uf2 / kvm_hid_cdc.uf2 in Docker (run inside WSL).
# The build tree lives on the WSL ext4 side (~/kvm-hid-build), not on /mnt.
#
#   bash firmware/kvm-hid/build.sh            # -> firmware/kvm-hid/out/*.uf2
#   KVM_UART_BAUD=921600 bash firmware/kvm-hid/build.sh
#
# Flash: hold BOOT, plug USB-C into a PC, copy the .uf2 to the RP2350 drive.
set -euo pipefail

SRC=$(cd "$(dirname "$0")" && pwd)
WORK=${WORK:-$HOME/kvm-hid-build}
SDK_TAG=${SDK_TAG:-2.3.1}
PIOUSB_TAG=${PIOUSB_TAG:-0.7.3}
IMG=kvm-hid-pico
BAUD=${KVM_UART_BAUD:-115200}
SYS_KHZ=${KVM_SYS_KHZ:-240000}

mkdir -p "$WORK"
if ! docker image inspect $IMG >/dev/null 2>&1; then
	docker build -q -t $IMG - <<'DOCKERFILE'
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    cmake ninja-build gcc-arm-none-eabi libnewlib-arm-none-eabi libstdc++-arm-none-eabi-newlib \
    build-essential python3 git ca-certificates pkg-config libusb-1.0-0-dev \
 && rm -rf /var/lib/apt/lists/*
DOCKERFILE
fi

cd "$WORK"
[ -d pico-sdk ] || git clone -q --depth 1 --branch "$SDK_TAG" https://github.com/raspberrypi/pico-sdk
git -C pico-sdk submodule update -q --init --depth 1 lib/tinyusb
if [ -d Pico-PIO-USB ] && [ "$(git -C Pico-PIO-USB describe --tags 2>/dev/null)" != "$PIOUSB_TAG" ]; then
	rm -rf Pico-PIO-USB   # another version: fetch again
fi
[ -d Pico-PIO-USB ] || git clone -q --depth 1 --branch "$PIOUSB_TAG" https://github.com/sekigon-gonnoc/Pico-PIO-USB
# Our fixes to Pico-PIO-USB (patches/pico-pio-usb-<tag>-*.patch), applied once.
# PIOUSB_PATCHED=1 skips this, e.g. while the tree carries extra debug edits.
for p in "$SRC"/patches/pico-pio-usb-"$PIOUSB_TAG"-*.patch; do
	[ "${PIOUSB_PATCHED:-0}" = 1 ] && break
	[ -e "$p" ] || continue
	git -C Pico-PIO-USB apply --reverse --check "$p" 2>/dev/null || git -C Pico-PIO-USB apply "$p"
done
rm -rf src && cp -r "$SRC" src && rm -rf src/out

docker run --rm -u "$(id -u):$(id -g)" -v "$WORK:/w" -w /w -e HOME=/w \
	-e PICO_SDK_PATH=/w/pico-sdk -e PICO_PIO_USB_PATH=/w/Pico-PIO-USB $IMG bash -c "
set -e
cmake -S src -B build -G Ninja -DKVM_UART_BAUD=$BAUD -DKVM_SYS_KHZ=$SYS_KHZ >/dev/null
ninja -C build kvm_hid_uart kvm_hid_cdc
"
mkdir -p "$SRC/out"
cp "$WORK"/build/kvm_hid_uart.uf2 "$WORK"/build/kvm_hid_cdc.uf2 "$SRC/out/"
ls -la "$SRC/out"
