// Host-side test for the line protocol and report queues (no Pico SDK).
//   cc -I../src -o test_proto test_proto.c ../src/proto.c ../src/hid_reports.c && ./test_proto
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "hid_reports.h"
#include "proto.h"

static char log_buf[4096];
static void logf_(const char *s) { strncat(log_buf, s, sizeof log_buf - strlen(log_buf) - 1); }

static void kb(uint8_t m, const uint8_t k[6]) {
  char s[64];
  snprintf(s, sizeof s, "K%02x:%02x%02x%02x%02x%02x%02x;", m, k[0], k[1], k[2], k[3], k[4], k[5]);
  logf_(s);
}
static void ab(uint8_t b, uint16_t x, uint16_t y, int8_t w) {
  char s[64];
  snprintf(s, sizeof s, "A%x,%u,%u,%d;", b, x, y, w);
  logf_(s);
}
static void rl(uint8_t b, int8_t x, int8_t y, int8_t w) {
  char s[64];
  snprintf(s, sizeof s, "R%x,%d,%d,%d;", b, x, y, w);
  logf_(s);
}
static void rel_all(void) { logf_("Z;"); }
static void ident(void) { logf_("?;"); }

static const proto_ops_t ops = {kb, ab, rl, rel_all, ident};

static void feed(proto_t *p, const char *s) {
  while (*s) proto_feed(p, *s++);
}

int main(void) {
  proto_t p;
  proto_init(&p, &ops);
  feed(&p, "K 01 04 00 00 00 00 00\n");
  feed(&p, "A 03 7fff 0000 ff\r\n");
  feed(&p, "R 00 81 7f 01\n");
  feed(&p, "Z\n?\n");
  // malformed / hostile input must be ignored without side effects
  feed(&p, "K 01 04\n");                 // too few fields
  feed(&p, "K 01 04 00 00 00 00 00 00\n"); // too many
  feed(&p, "A 20 0000 0000 00\n");         // buttons out of range
  feed(&p, "A 00 8000 0000 00\n");         // x out of range
  feed(&p, "Kx\n");
  feed(&p, "Z extra\n");
  feed(&p, "K 1g 00 00 00 00 00 00\n");    // bad hex
  char longline[200];
  memset(longline, 'K', sizeof longline - 2);
  longline[sizeof longline - 2] = '\n';
  longline[sizeof longline - 1] = 0;
  feed(&p, longline);                      // overflow: dropped as a whole
  feed(&p, "\n\n");
  feed(&p, "K 00 00 00 00 00 00 00\n");    // still in sync afterwards
  const char *want =
      "K01:040000000000;A3,32767,0,-1;R0,-127,127,1;Z;?;K00:000000000000;";
  printf("got : %s\nwant: %s\n", log_buf, want);
  assert(strcmp(log_buf, want) == 0);

  // queues: overwrite newest when full, merge relative motion
  hidq_t q;
  hidq_init(&q, sizeof(kbd_report_t));
  for (int i = 0; i < HIDQ_LEN + 5; i++) {
    kbd_report_t r = {.modifiers = (uint8_t)i};
    hidq_push(&q, &r);
  }
  assert(q.count == HIDQ_LEN);
  assert(((const kbd_report_t *)hidq_peek(&q))->modifiers == 0);
  for (int i = 0; i < HIDQ_LEN - 1; i++) hidq_pop(&q);
  assert(((const kbd_report_t *)hidq_peek(&q))->modifiers == HIDQ_LEN + 4);  // final state kept

  hidq_t m;
  hidq_init(&m, sizeof(rel_report_t));
  for (int i = 0; i < 10; i++) {
    rel_report_t r = {.buttons = 0, .x = 10, .y = -5};
    hidq_push_rel(&m, &r);
  }
  assert(m.count == 1);
  const rel_report_t *mr = (const rel_report_t *)hidq_peek(&m);
  assert(mr->x == 100 && mr->y == -50);
  rel_report_t big = {.buttons = 0, .x = 100};
  hidq_push_rel(&m, &big);                 // 200 does not fit: new entry
  assert(m.count == 2);
  rel_report_t click = {.buttons = 1};
  hidq_push_rel(&m, &click);               // button change: new entry
  assert(m.count == 3);
  printf("all tests passed\n");
  return 0;
}
