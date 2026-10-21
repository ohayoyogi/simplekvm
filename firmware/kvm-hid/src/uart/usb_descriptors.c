// UART variant: the target PC sees a composite HID device (boot keyboard,
// absolute pointer, boot mouse).
#include <string.h>

#include "hid_reports.h"
#include "pico/unique_id.h"
#include "tusb.h"

#define USB_VID 0xCafe       // TinyUSB example VID (prototype only)
#define USB_PID 0x4b48       // "KH"

static const tusb_desc_device_t desc_device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = 0x00,
    .bDeviceSubClass = 0x00,
    .bDeviceProtocol = 0x00,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = USB_VID,
    .idProduct = USB_PID,
    .bcdDevice = 0x0100,
    .iManufacturer = 1,
    .iProduct = 2,
    .iSerialNumber = 3,
    .bNumConfigurations = 1,
};

const uint8_t *tud_descriptor_device_cb(void) { return (const uint8_t *)&desc_device; }

#define CONFIG_LEN (TUD_CONFIG_DESC_LEN + 3 * TUD_HID_DESC_LEN)

// keyboard and boot mouse use the boot subclass so BIOS/UEFI accept them
static const uint8_t desc_config[] = {
    TUD_CONFIG_DESCRIPTOR(1, HID_ITF_COUNT, 0, CONFIG_LEN,
                          TUSB_DESC_CONFIG_ATT_REMOTE_WAKEUP, 100),
    TUD_HID_DESCRIPTOR(HID_ITF_KEYBOARD, 4, HID_ITF_PROTOCOL_KEYBOARD,
                       HID_DESC_KEYBOARD_LEN, 0x81, CFG_TUD_HID_EP_BUFSIZE, 1),
    TUD_HID_DESCRIPTOR(HID_ITF_MOUSE_ABS, 5, HID_ITF_PROTOCOL_NONE,
                       HID_DESC_MOUSE_ABS_LEN, 0x82, CFG_TUD_HID_EP_BUFSIZE, 1),
    TUD_HID_DESCRIPTOR(HID_ITF_MOUSE_REL, 6, HID_ITF_PROTOCOL_MOUSE,
                       HID_DESC_MOUSE_REL_LEN, 0x83, CFG_TUD_HID_EP_BUFSIZE, 1),
};

const uint8_t *tud_descriptor_configuration_cb(uint8_t index) {
  (void)index;
  return desc_config;
}

const uint8_t *tud_hid_descriptor_report_cb(uint8_t instance) {
  switch (instance) {
    case HID_ITF_KEYBOARD: return hid_desc_keyboard;
    case HID_ITF_MOUSE_ABS: return hid_desc_mouse_abs;
    default: return hid_desc_mouse_rel;
  }
}

static const char *const strings[] = {
    NULL, "dev_tinker", "dev_tinker KVM keyboard/mouse", NULL,
    "Keyboard", "Absolute pointer", "Mouse",
};

static uint16_t desc_str[33];

const uint16_t *tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
  (void)langid;
  char serial[2 * PICO_UNIQUE_BOARD_ID_SIZE_BYTES + 1];
  const char *s;
  size_t n;
  if (index == 0) {
    desc_str[1] = 0x0409;
    n = 1;
  } else {
    if (index >= sizeof(strings) / sizeof(strings[0])) return NULL;
    if (index == 3) {
      pico_get_unique_board_id_string(serial, sizeof serial);
      s = serial;
    } else {
      s = strings[index];
    }
    n = strlen(s);
    if (n > 32) n = 32;
    for (size_t i = 0; i < n; i++) desc_str[1 + i] = (uint8_t)s[i];
  }
  desc_str[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * n + 2));
  return desc_str;
}
