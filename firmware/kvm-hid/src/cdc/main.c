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
#include "pico/multicore.h"
#include "pico/stdlib.h"
#include "pio_usb.h"
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

#define PIO_CONFIG_LEN (TUD_CONFIG_DESC_LEN + 3 * TUD_HID_DESC_LEN)
#define PIO_EP_SIZE 16

static const uint8_t pio_desc_config[] = {
    TUD_CONFIG_DESCRIPTOR(1, HID_ITF_COUNT, 0, PIO_CONFIG_LEN, 0, 100),
    TUD_HID_DESCRIPTOR(HID_ITF_KEYBOARD, 0, HID_ITF_PROTOCOL_KEYBOARD, HID_DESC_KEYBOARD_LEN,
                       0x81, PIO_EP_SIZE, 1),
    TUD_HID_DESCRIPTOR(HID_ITF_MOUSE_ABS, 0, HID_ITF_PROTOCOL_NONE, HID_DESC_MOUSE_ABS_LEN,
                       0x82, PIO_EP_SIZE, 1),
    TUD_HID_DESCRIPTOR(HID_ITF_MOUSE_REL, 0, HID_ITF_PROTOCOL_MOUSE, HID_DESC_MOUSE_REL_LEN,
                       0x83, PIO_EP_SIZE, 1),
};

static const uint8_t *pio_report_desc[HID_ITF_COUNT];
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
  pio_report_desc[HID_ITF_KEYBOARD] = hid_desc_keyboard;
  pio_report_desc[HID_ITF_MOUSE_ABS] = hid_desc_mouse_abs;
  pio_report_desc[HID_ITF_MOUSE_REL] = hid_desc_mouse_rel;
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
static uint8_t ep_buf[HID_ITF_COUNT][8];

static void pump_hid(void) {
  usb_device_t *dev = pio_dev;
  if (!dev) return;
  for (uint8_t itf = 0; itf < HID_ITF_COUNT; itf++) {
    const uint8_t *r = hidq_peek(&bridge_q[itf]);
    if (!r) continue;
    endpoint_t *ep = pio_usb_get_endpoint(dev, (uint8_t)(itf + 1));
    if (!ep || ep->has_transfer) continue;
    memcpy(ep_buf[itf], r, bridge_q[itf].size);
    if (pio_usb_set_out_data(ep, ep_buf[itf], bridge_q[itf].size) == 0) hidq_pop(&bridge_q[itf]);
  }
}

// ---- CDC link to the KVM host (native USB-C) ------------------------------

const char *bridge_variant(void) { return "cdc"; }

void bridge_reply(const char *s) {
  if (!tud_cdc_connected()) return;
  tud_cdc_write_str(s);
  tud_cdc_write_flush();
}

int main(void) {
  // PIO-USB needs a system clock that is a multiple of 12 MHz
  set_sys_clock_khz(120000, true);
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
