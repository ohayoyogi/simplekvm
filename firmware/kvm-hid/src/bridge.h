// Glue shared by both variants: protocol callbacks -> report queues.
#pragma once

#include "hid_reports.h"
#include "proto.h"

extern hidq_t bridge_q[HID_ITF_COUNT];
extern volatile uint32_t bridge_activity_ms;  // last command time, for the LED

void bridge_init(void);
const proto_ops_t *bridge_ops(void);

// Provided by each variant
void bridge_reply(const char *s);   // send a line (with '\n') back to the host
const char *bridge_variant(void);
