// On-board WS2812 status LED.
//   red    = no host link (UART: target USB not mounted / CDC: Tinker port closed)
//   green  = ready, blue flash = command received, dim yellow = target suspended
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "hardware/pio.h"

// false (and no LED) if the PIO block has no free state machine or no room
// for the program
bool led_init(PIO pio, unsigned pin);
void led_task(bool link_up, bool target_ok, bool suspended, uint32_t last_activity_ms);
