#include "proto.h"

#include <string.h>

void proto_init(proto_t *p, const proto_ops_t *ops) {
  memset(p, 0, sizeof(*p));
  p->ops = ops;
}

static int hexval(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// Parse up to n space-separated hex fields; returns the count parsed, or -1 on
// a malformed field. Each field may be 1..4 hex digits.
static int parse_hex_fields(const char *s, uint32_t *out, int n) {
  int count = 0;
  while (*s) {
    while (*s == ' ') s++;
    if (!*s) break;
    if (count == n) return -1;  // too many fields
    uint32_t v = 0;
    int digits = 0;
    while (*s && *s != ' ') {
      int h = hexval(*s++);
      if (h < 0 || ++digits > 4) return -1;
      v = (v << 4) | (uint32_t)h;
    }
    out[count++] = v;
  }
  return count;
}

bool proto_line(proto_t *p, const char *line) {
  const proto_ops_t *ops = p->ops;
  uint32_t f[7];
  if (!line[0]) return false;
  char cmd = line[0];
  const char *args = line + 1;
  if (*args && *args != ' ') return false;

  switch (cmd) {
    case 'K': {
      if (parse_hex_fields(args, f, 7) != 7) return false;
      uint8_t keys[6];
      for (int i = 0; i < 6; i++) {
        if (f[i + 1] > 0xff) return false;
        keys[i] = (uint8_t)f[i + 1];
      }
      if (f[0] > 0xff) return false;
      if (ops->keyboard) ops->keyboard((uint8_t)f[0], keys);
      return true;
    }
    case 'A': {
      if (parse_hex_fields(args, f, 4) != 4) return false;
      if (f[0] > 0x1f || f[1] > 0x7fff || f[2] > 0x7fff || f[3] > 0xff) return false;
      if (ops->mouse_abs)
        ops->mouse_abs((uint8_t)f[0], (uint16_t)f[1], (uint16_t)f[2], (int8_t)(uint8_t)f[3]);
      return true;
    }
    case 'R': {
      if (parse_hex_fields(args, f, 4) != 4) return false;
      if (f[0] > 0x1f || f[1] > 0xff || f[2] > 0xff || f[3] > 0xff) return false;
      if (ops->mouse_rel)
        ops->mouse_rel((uint8_t)f[0], (int8_t)(uint8_t)f[1], (int8_t)(uint8_t)f[2],
                       (int8_t)(uint8_t)f[3]);
      return true;
    }
    case 'Z':
      if (*args) return false;
      if (ops->release_all) ops->release_all();
      return true;
    case '?':
      if (*args) return false;
      if (ops->identify) ops->identify();
      return true;
  }
  return false;
}

void proto_feed(proto_t *p, char c) {
  if (c == '\r') return;
  if (c == '\n') {
    if (!p->overflow) {
      p->line[p->len] = '\0';
      proto_line(p, p->line);
    }
    p->len = 0;
    p->overflow = false;
    return;
  }
  if (p->len >= PROTO_LINE_MAX - 1) {
    p->overflow = true;  // drop the whole line rather than act on a fragment
    return;
  }
  p->line[p->len++] = c;
}
