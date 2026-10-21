#include "bridge.h"

#include <stdio.h>
#include <string.h>

#include "pico/time.h"

#ifndef KVM_HID_VERSION
#define KVM_HID_VERSION "0.1"
#endif

hidq_t bridge_q[HID_ITF_COUNT];
volatile uint32_t bridge_activity_ms;

static void touch(void) { bridge_activity_ms = to_ms_since_boot(get_absolute_time()); }

static void on_keyboard(uint8_t mods, const uint8_t keys[6]) {
  kbd_report_t r = {.modifiers = mods};
  memcpy(r.keys, keys, 6);
  hidq_push(&bridge_q[HID_ITF_KEYBOARD], &r);
  touch();
}

static void on_abs(uint8_t buttons, uint16_t x, uint16_t y, int8_t wheel) {
  abs_report_t r = {.buttons = buttons, .x = x, .y = y, .wheel = wheel};
  hidq_push(&bridge_q[HID_ITF_MOUSE_ABS], &r);
  touch();
}

static void on_rel(uint8_t buttons, int8_t dx, int8_t dy, int8_t wheel) {
  rel_report_t r = {.buttons = buttons, .x = dx, .y = dy, .wheel = wheel};
  hidq_push_rel(&bridge_q[HID_ITF_MOUSE_REL], &r);
  touch();
}

static void on_release(void) {
  static const uint8_t none[6] = {0};
  on_keyboard(0, none);
  // keep the absolute position, drop the buttons
  hidq_t *q = &bridge_q[HID_ITF_MOUSE_ABS];
  abs_report_t r = {0};
  if (q->count) memcpy(&r, q->data[(q->head + q->count - 1) % HIDQ_LEN], sizeof r);
  r.buttons = 0;
  r.wheel = 0;
  hidq_push(q, &r);
  rel_report_t z = {0};
  hidq_push(&bridge_q[HID_ITF_MOUSE_REL], &z);
}

static void on_identify(void) {
  char buf[48];
  snprintf(buf, sizeof buf, "OK kvm-hid %s %s\n", bridge_variant(), KVM_HID_VERSION);
  bridge_reply(buf);
}

static const proto_ops_t ops = {
    .keyboard = on_keyboard,
    .mouse_abs = on_abs,
    .mouse_rel = on_rel,
    .release_all = on_release,
    .identify = on_identify,
};

void bridge_init(void) {
  hidq_init(&bridge_q[HID_ITF_KEYBOARD], sizeof(kbd_report_t));
  hidq_init(&bridge_q[HID_ITF_MOUSE_ABS], sizeof(abs_report_t));
  hidq_init(&bridge_q[HID_ITF_MOUSE_REL], sizeof(rel_report_t));
}

const proto_ops_t *bridge_ops(void) { return &ops; }
