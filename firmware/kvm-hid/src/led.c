#include "led.h"

#include "hardware/clocks.h"
#include "pico/time.h"
#include "ws2812.pio.h"

static PIO led_pio;
static int led_sm = -1;
static uint32_t last_rgb = 0xffffffff;

void led_init(PIO pio, unsigned pin) {
  led_pio = pio;
  led_sm = pio_claim_unused_sm(pio, false);
  if (led_sm < 0) return;
  uint offset = pio_add_program(pio, &ws2812_program);
  pio_gpio_init(pio, pin);
  pio_sm_set_consecutive_pindirs(pio, (uint)led_sm, pin, 1, true);
  pio_sm_config c = ws2812_program_get_default_config(offset);
  sm_config_set_sideset_pins(&c, pin);
  sm_config_set_out_shift(&c, false, true, 24);  // MSB first, autopull 24 bits
  sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_TX);
  int cycles_per_bit = ws2812_T1 + ws2812_T2 + ws2812_T3;
  sm_config_set_clkdiv(&c, (float)clock_get_hz(clk_sys) / (800000.0f * (float)cycles_per_bit));
  pio_sm_init(pio, (uint)led_sm, offset, &c);
  pio_sm_set_enabled(pio, (uint)led_sm, true);
}

static void put_rgb(uint8_t r, uint8_t g, uint8_t b) {
#if KVM_LED_RGB
  // the RP2350-USB-A's LED takes red first, not the usual WS2812 green
  uint32_t word = ((uint32_t)r << 16) | ((uint32_t)g << 8) | b;
#else
  uint32_t word = ((uint32_t)g << 16) | ((uint32_t)r << 8) | b;
#endif
  if (led_sm < 0 || word == last_rgb) return;
  last_rgb = word;
  pio_sm_put_blocking(led_pio, (uint)led_sm, word << 8u);
}

void led_task(bool link_up, bool target_ok, bool suspended, uint32_t last_activity_ms) {
  uint32_t now = to_ms_since_boot(get_absolute_time());
  if (!link_up || !target_ok) {
    put_rgb(24, 0, 0);
  } else if (suspended) {
    put_rgb(8, 6, 0);
  } else if (now - last_activity_ms < 40) {
    put_rgb(0, 0, 32);
  } else {
    put_rgb(0, 16, 0);
  }
}
