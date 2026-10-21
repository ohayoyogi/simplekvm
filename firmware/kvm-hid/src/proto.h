// Line protocol from the KVM host (Tinker Board) to this HID bridge.
//
//   K mm k1 k2 k3 k4 k5 k6   keyboard report (hex): modifiers + 6 usage codes
//   A bb xxxx yyyy ww        absolute mouse: buttons, x/y 0..7fff, wheel (int8)
//   R bb xx yy ww            relative (boot) mouse: buttons, dx/dy/wheel (int8)
//   Z                        release everything
//   ?                        identify -> "OK kvm-hid <variant> <version>"
//
// Back to the host: "L xx" when the target sets the keyboard LEDs
// (bit0 Num, bit1 Caps, bit2 Scroll). Lines end with '\n'; '\r' is ignored.
// Unknown or malformed lines are dropped silently.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define PROTO_LINE_MAX 64

typedef struct {
  void (*keyboard)(uint8_t modifiers, const uint8_t keys[6]);
  void (*mouse_abs)(uint8_t buttons, uint16_t x, uint16_t y, int8_t wheel);
  void (*mouse_rel)(uint8_t buttons, int8_t dx, int8_t dy, int8_t wheel);
  void (*release_all)(void);
  void (*identify)(void);
} proto_ops_t;

typedef struct {
  const proto_ops_t *ops;
  char line[PROTO_LINE_MAX];
  uint8_t len;
  bool overflow;
} proto_t;

void proto_init(proto_t *p, const proto_ops_t *ops);
void proto_feed(proto_t *p, char c);
// Parse one complete line (without the newline). Returns true if it was valid.
bool proto_line(proto_t *p, const char *line);
