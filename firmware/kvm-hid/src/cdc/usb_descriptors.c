// CDC variant, native USB-C: a CDC ACM port for the KVM host. The product
// string contains "kvm-hid" so kvm.py --hid auto finds it under
// /dev/serial/by-id/.
#include <string.h>

#include "pico/unique_id.h"
#include "tusb.h"

#define USB_VID 0xCafe       // TinyUSB example VID (prototype only)
#define USB_PID 0x4b43       // "KC"

static const tusb_desc_device_t desc_device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = TUSB_CLASS_MISC,
    .bDeviceSubClass = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol = MISC_PROTOCOL_IAD,
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

enum { ITF_CDC = 0, ITF_CDC_DATA, ITF_TOTAL };
#define CONFIG_LEN (TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN)

static const uint8_t desc_config[] = {
    TUD_CONFIG_DESCRIPTOR(1, ITF_TOTAL, 0, CONFIG_LEN, 0, 100),
    TUD_CDC_DESCRIPTOR(ITF_CDC, 4, 0x81, 8, 0x02, 0x82, 64),
};

const uint8_t *tud_descriptor_configuration_cb(uint8_t index) {
  (void)index;
  return desc_config;
}

static const char *const strings[] = {NULL, "dev_tinker", "dev_tinker kvm-hid", NULL,
                                      "kvm-hid control"};
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
