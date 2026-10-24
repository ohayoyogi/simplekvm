// HID report formats and descriptors shared by both firmware variants.
#pragma once

#include <stdbool.h>
#include <stdint.h>

// interface / endpoint order: keyboard, absolute mouse, relative mouse
enum { HID_ITF_KEYBOARD = 0, HID_ITF_MOUSE_ABS, HID_ITF_MOUSE_REL, HID_ITF_COUNT };

typedef struct __attribute__((packed)) {
  uint8_t modifiers, reserved, keys[6];
} kbd_report_t;                      // boot keyboard, 8 bytes

typedef struct __attribute__((packed)) {
  uint8_t buttons;
  uint16_t x, y;                     // 0..32767
  int8_t wheel;
} abs_report_t;                      // 6 bytes

typedef struct __attribute__((packed)) {
  uint8_t buttons;
  int8_t x, y, wheel, pan;
} rel_report_t;                      // boot mouse (first 3 bytes) + wheel/pan

// Report descriptor sizes are needed at compile time for the configuration
// descriptors; hid_reports.c static_asserts that they match.
#define HID_DESC_KEYBOARD_LEN 65
#define HID_DESC_MOUSE_ABS_LEN 64
#define HID_DESC_MOUSE_REL_LEN 67

extern const uint8_t hid_desc_keyboard[];
extern const uint16_t hid_desc_keyboard_len;
extern const uint8_t hid_desc_mouse_abs[];
extern const uint16_t hid_desc_mouse_abs_len;
extern const uint8_t hid_desc_mouse_rel[];
extern const uint16_t hid_desc_mouse_rel_len;

// CDC variant: one boot-mouse interface carries both pointers. In report
// protocol it sends [REPORT_ID_REL, rel_report_t] or [REPORT_ID_ABS,
// abs_report_t]; in boot protocol (BIOS) the 3-byte boot report.
#define REPORT_ID_REL 1
#define REPORT_ID_ABS 2
#define HID_DESC_MOUSE_COMBO_LEN 135
extern const uint8_t hid_desc_mouse_combo[];

// Small per-interface queues. Reports are state snapshots, so when a queue is
// full the newest entry is overwritten (the final state is never lost);
// relative motion is merged instead so no movement is dropped.
#define HIDQ_LEN 16

typedef struct {
  uint8_t data[HIDQ_LEN][8];
  uint8_t head, count, size;
} hidq_t;

void hidq_init(hidq_t *q, uint8_t report_size);
void hidq_push(hidq_t *q, const void *report);
void hidq_push_rel(hidq_t *q, const rel_report_t *r);
const uint8_t *hidq_peek(const hidq_t *q);
void hidq_pop(hidq_t *q);
