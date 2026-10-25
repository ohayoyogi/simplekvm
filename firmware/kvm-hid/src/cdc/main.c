// CDC variant: the KVM host talks to the native USB-C port (CDC ACM, so no
// UART wiring), and the USB-A port (PIO-USB, GP12 = D+, GP13 = D-) acts as a
// full-speed HID device for the target PC.
//
// WARNING: the board's USB-A receptacle is a host port that supplies 5 V. To
// connect it to a PC you need an A-to-A cable with VBUS (pin 1) cut, or the
// two 5 V supplies get tied together.
//
// Limitation: Pico-PIO-USB's device stack discards SET_REPORT data, so the
// target's keyboard LEDs (Caps/Num) are not reported back in this variant.
#include <stdio.h>
#include <string.h>

#include "bridge.h"
#include "class/hid/hid.h"   // HID constants (TinyUSB's HID class is not built here)
#include "hardware/clocks.h"
#include "led.h"
#include "pico/bootrom.h"
#include "pico/multicore.h"
#include "pico/stdlib.h"
#include "hardware/sync.h"
#include "pio_usb.h"
#include "pio_usb_ll.h"
#include "tusb.h"

// ---- PIO-USB HID device (USB-A) -------------------------------------------

static const tusb_desc_device_t pio_desc_device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0110,
    .bDeviceClass = 0x00,
    .bDeviceSubClass = 0x00,
    .bDeviceProtocol = 0x00,
    .bMaxPacketSize0 = 64,
    .idVendor = 0xCafe,
    .idProduct = 0x4b48,
    .bcdDevice = 0x0100,
    .iManufacturer = 1,
    .iProduct = 2,
    .iSerialNumber = 3,
    .bNumConfigurations = 1,
};

// Keyboard, absolute mouse and relative (boot) mouse as separate interfaces:
// in one interface Linux maps the second pointer's buttons and wheel to
// nothing (duplicate usages). Polled every 8 / 4 / 4 ms (1 ms buys nothing
// next to the video latency, and every IN token is work for PIO-USB).
enum { PIO_ITF_KEYBOARD = 0, PIO_ITF_MOUSE_ABS, PIO_ITF_MOUSE_REL, PIO_ITF_COUNT };
#define PIO_EP(itf) ((itf) + 1)
#define PIO_CONFIG_LEN (TUD_CONFIG_DESC_LEN + PIO_ITF_COUNT * TUD_HID_DESC_LEN)
#define PIO_EP_SIZE 16

static const uint8_t pio_desc_config[] = {
    TUD_CONFIG_DESCRIPTOR(1, PIO_ITF_COUNT, 0, PIO_CONFIG_LEN, 0, 100),
    TUD_HID_DESCRIPTOR(PIO_ITF_KEYBOARD, 0, HID_ITF_PROTOCOL_KEYBOARD, HID_DESC_KEYBOARD_LEN,
                       0x80 | PIO_EP(PIO_ITF_KEYBOARD), PIO_EP_SIZE, 8),
    TUD_HID_DESCRIPTOR(PIO_ITF_MOUSE_ABS, 0, HID_ITF_PROTOCOL_NONE, HID_DESC_MOUSE_ABS_LEN,
                       0x80 | PIO_EP(PIO_ITF_MOUSE_ABS), PIO_EP_SIZE, 4),
    TUD_HID_DESCRIPTOR(PIO_ITF_MOUSE_REL, 0, HID_ITF_PROTOCOL_MOUSE, HID_DESC_MOUSE_REL_LEN,
                       0x80 | PIO_EP(PIO_ITF_MOUSE_REL), PIO_EP_SIZE, 4),
};
_Static_assert((int)PIO_ITF_KEYBOARD == (int)HID_ITF_KEYBOARD &&
                   (int)PIO_ITF_MOUSE_ABS == (int)HID_ITF_MOUSE_ABS &&
                   (int)PIO_ITF_MOUSE_REL == (int)HID_ITF_MOUSE_REL,
               "interfaces in bridge_q order");

static const uint8_t *pio_report_desc[PIO_ITF_COUNT];
static const char *const pio_strings[] = {
    (const char[]){0x09, 0x04}, "dev_tinker", "dev_tinker KVM keyboard/mouse", "0001",
};
static string_descriptor_t pio_str_desc[4];
static usb_descriptor_buffers_t pio_desc;
static usb_device_t *volatile pio_dev;

static void init_strings(void) {
  for (int idx = 0; idx < 4; idx++) {
    uint16_t *w = (uint16_t *)&pio_str_desc[idx];
    uint8_t len;
    if (idx == 0) {
      w[1] = 0x0409;
      len = 1;
    } else {
      len = (uint8_t)strnlen(pio_strings[idx], 31);
      for (int i = 0; i < len; i++) w[i + 1] = (uint8_t)pio_strings[idx][i];
    }
    w[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * len + 2));
  }
}

static void core1_main(void) {
  sleep_ms(10);
  static pio_usb_configuration_t config = PIO_USB_DEFAULT_CONFIG;
  config.pin_dp = PICO_DEFAULT_PIO_USB_DP_PIN;   // GP12, D- = GP13
  // TX stays on pio0; the device receiver from our Pico-PIO-USB patch
  // (usb_dev_rx) fills all 32 instructions of a PIO block, so it gets pio1.
  config.pio_rx_num = 1;
  config.sm_rx = 0;
  config.sm_eop = 1;                             // claimed, unused in device mode
  pio_report_desc[PIO_ITF_KEYBOARD] = hid_desc_keyboard;
  pio_report_desc[PIO_ITF_MOUSE_ABS] = hid_desc_mouse_abs;
  pio_report_desc[PIO_ITF_MOUSE_REL] = hid_desc_mouse_rel;
  init_strings();
  pio_desc.device = (const uint8_t *)&pio_desc_device;
  pio_desc.config = pio_desc_config;
  pio_desc.hid_report = pio_report_desc;
  pio_desc.string = pio_str_desc;
  pio_dev = pio_usb_device_init(&config, &pio_desc);
  while (true) pio_usb_device_task();
}

// Reports must stay valid until the PIO transfer completes, so each endpoint
// has its own buffer that is only rewritten when the endpoint is idle.
static uint8_t ep_buf[PIO_ITF_COUNT][8];

// Keepalive: while idle, send the current state again every KEEPALIVE_MS.
// Linux's usbhid leaves its retry state only on a report with data; an idle
// keyboard only NAKs, so two rare bus errors 1.0-1.5 s apart (with nothing
// in between) made it reset the device (hid_io_error). A repeated state
// report changes nothing on the target and clears that state; it also puts
// the state back right after a reset.
#define KEEPALIVE_MS 250
static uint8_t idle_rep[PIO_ITF_COUNT][8];   // last report, motion/wheel zeroed
static uint8_t idle_len[PIO_ITF_COUNT];
static uint8_t idle_proto[PIO_ITF_COUNT];    // HID protocol it was built for
static uint32_t last_sent_ms[PIO_ITF_COUNT];

static uint32_t now_ms(void) { return to_ms_since_boot(get_absolute_time()); }

static bool send_report(endpoint_t *ep, uint8_t itf, const uint8_t *data, uint8_t len) {
  memcpy(ep_buf[itf], data, len);
  if (pio_usb_set_out_data(ep, ep_buf[itf], len) != 0) return false;
  uint8_t *idle = idle_rep[itf];
  memcpy(idle, data, len);
  if (itf == PIO_ITF_MOUSE_REL) {
    memset(idle + 1, 0, (size_t)len - 1);        // buttons only: no motion, wheel, pan
  } else if (itf == PIO_ITF_MOUSE_ABS) {
    idle[len - 1] = 0;                           // keep the position, no wheel
  }
  idle_len[itf] = len;
  idle_proto[itf] = pio_usb_device_hid_protocol[itf];
  last_sent_ms[itf] = now_ms();
  return true;
}

static void keepalive(endpoint_t *ep, uint8_t itf) {
  if (!idle_len[itf] || now_ms() - last_sent_ms[itf] < KEEPALIVE_MS) return;
  if (idle_proto[itf] != pio_usb_device_hid_protocol[itf]) {
    idle_len[itf] = 0;   // the host switched boot/report protocol: format is stale
    return;
  }
  send_report(ep, itf, idle_rep[itf], idle_len[itf]);
}

static endpoint_t *idle_endpoint(usb_device_t *dev, uint8_t epnum) {
  endpoint_t *ep = pio_usb_get_endpoint(dev, epnum);
  if (!ep || ep->has_transfer) return NULL;
  // With patches/pico-pio-usb-*-device-fixes.patch the endpoint is idle but
  // still owned by core1 while its last packet's continue is pending.
  __dmb();
  if (PIO_USB_ROOT_PORT(0)->ep_continue & (1u << epnum)) return NULL;
  return ep;
}

static void pump_hid(void) {
  usb_device_t *dev = pio_dev;
  if (!dev) return;
  endpoint_t *ep;

  for (uint8_t itf = 0; itf < PIO_ITF_COUNT; itf++) {
    if (!(ep = idle_endpoint(dev, PIO_EP(itf)))) continue;
    hidq_t *q = &bridge_q[itf];
    const uint8_t *r = hidq_peek(q);
    if (!r) {
      keepalive(ep, itf);
      continue;
    }
    // A BIOS selects the boot protocol: the relative mouse then sends only
    // the 3-byte boot report (buttons, x, y).
    uint8_t len = (itf == PIO_ITF_MOUSE_REL && pio_usb_device_hid_protocol[itf] == 0) ? 3 : q->size;
    if (send_report(ep, itf, r, len)) hidq_pop(q);
  }
}

// ---- CDC link to the KVM host (native USB-C) ------------------------------

const char *bridge_variant(void) { return "cdc"; }

void bridge_reply(const char *s) {
  if (!tud_cdc_connected()) return;
  tud_cdc_write_str(s);
  tud_cdc_write_flush();
}

// Opening the port at 1200 baud reboots into the USB bootloader (like the
// Pico SDK's stdio_usb), so the host can reflash without the BOOT button:
//   stty -F /dev/ttyACM0 1200
void tud_cdc_line_coding_cb(uint8_t itf, cdc_line_coding_t const *coding) {
  (void)itf;
  if (coding->bit_rate == 1200) reset_usb_boot(0, 0);
}

int main(void) {
  // PIO-USB needs 120 or 240 MHz (TX at clk_sys / 48 MHz, our receiver at
  // clk_sys / 96 MHz). 120 MHz is within the RP2350's rating; the stock
  // receiver needed 240 MHz to re-arm in time, usb_dev_rx does not.
  set_sys_clock_khz(KVM_SYS_KHZ, true);
  stdio_uart_init();          // UART0 (GP0/GP1) carries debug printf only

  bridge_init();
  static proto_t proto;
  proto_init(&proto, bridge_ops());

  multicore_reset_core1();
  multicore_launch_core1(core1_main);

  led_init(pio2, PICO_DEFAULT_WS2812_PIN);   // pio0/pio1 belong to PIO-USB
  tusb_init();
  printf("kvm-hid cdc started\n");

  while (true) {
    tud_task();
    while (tud_cdc_available()) {
      uint8_t buf[64];
      uint32_t n = tud_cdc_read(buf, sizeof buf);
      for (uint32_t i = 0; i < n; i++) proto_feed(&proto, (char)buf[i]);
    }
    pump_hid();
    led_task(tud_cdc_connected(), pio_dev != NULL, false, bridge_activity_ms);
  }
}
