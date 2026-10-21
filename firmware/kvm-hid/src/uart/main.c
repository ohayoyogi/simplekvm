// UART variant: commands arrive on UART0 (GP0 = TX, GP1 = RX) from the KVM
// host; the native USB-C port is a HID keyboard + mice for the target PC.
// Wire only TX/RX/GND to the host (3.3 V logic); power comes from the target.
#include <stdio.h>

#include "bridge.h"
#include "hardware/irq.h"
#include "hardware/uart.h"
#include "led.h"
#include "pico/stdlib.h"
#include "tusb.h"

#ifndef KVM_UART_BAUD
#define KVM_UART_BAUD 115200
#endif

#define KVM_UART uart0
#define KVM_UART_IRQ UART0_IRQ

// RX ring buffer filled from the UART interrupt (the hardware FIFO is only
// 32 bytes; tud_task() can occasionally take longer than that at 1 Mbaud)
static volatile uint8_t rx_buf[1024];
static volatile uint16_t rx_head, rx_tail;

static void on_uart_rx(void) {
  while (uart_is_readable(KVM_UART)) {
    uint8_t c = (uint8_t)uart_getc(KVM_UART);
    uint16_t next = (uint16_t)((rx_head + 1) % sizeof rx_buf);
    if (next != rx_tail) {
      rx_buf[rx_head] = c;
      rx_head = next;
    }
  }
}

const char *bridge_variant(void) { return "uart"; }

void bridge_reply(const char *s) { uart_puts(KVM_UART, s); }

static void pump_hid(void) {
  if (!tud_mounted()) return;
  for (uint8_t itf = 0; itf < HID_ITF_COUNT; itf++) {
    const uint8_t *r = hidq_peek(&bridge_q[itf]);
    if (!r) continue;
    if (tud_suspended()) {
      tud_remote_wakeup();   // only honoured if the host enabled it
      return;
    }
    if (!tud_hid_n_ready(itf)) continue;
    if (tud_hid_n_report(itf, 0, r, bridge_q[itf].size)) hidq_pop(&bridge_q[itf]);
  }
}

int main(void) {
  // not stdio_init_all(): UART0 carries the protocol, not printf
  uart_init(KVM_UART, KVM_UART_BAUD);
  gpio_set_function(PICO_DEFAULT_UART_TX_PIN, UART_FUNCSEL_NUM(KVM_UART, PICO_DEFAULT_UART_TX_PIN));
  gpio_set_function(PICO_DEFAULT_UART_RX_PIN, UART_FUNCSEL_NUM(KVM_UART, PICO_DEFAULT_UART_RX_PIN));
  gpio_pull_up(PICO_DEFAULT_UART_RX_PIN);   // idle high when the host is unplugged
  uart_set_fifo_enabled(KVM_UART, true);
  irq_set_exclusive_handler(KVM_UART_IRQ, on_uart_rx);
  irq_set_enabled(KVM_UART_IRQ, true);
  uart_set_irq_enables(KVM_UART, true, false);

  bridge_init();
  static proto_t proto;
  proto_init(&proto, bridge_ops());

  led_init(pio0, PICO_DEFAULT_WS2812_PIN);
  tusb_init();

  while (true) {
    tud_task();
    while (rx_tail != rx_head) {
      proto_feed(&proto, (char)rx_buf[rx_tail]);
      rx_tail = (uint16_t)((rx_tail + 1) % sizeof rx_buf);
    }
    pump_hid();
    // the UART link has no "connected" signal; treat it as always up
    led_task(true, tud_mounted(), tud_suspended(), bridge_activity_ms);
  }
}

// Keyboard LEDs from the target -> host
void tud_hid_set_report_cb(uint8_t instance, uint8_t report_id, hid_report_type_t type,
                           uint8_t const *buffer, uint16_t size) {
  (void)report_id;
  if (instance == HID_ITF_KEYBOARD && type == HID_REPORT_TYPE_OUTPUT && size >= 1) {
    char s[8];
    snprintf(s, sizeof s, "L %02x\n", buffer[0]);
    bridge_reply(s);
  }
}

uint16_t tud_hid_get_report_cb(uint8_t instance, uint8_t report_id, hid_report_type_t type,
                               uint8_t *buffer, uint16_t reqlen) {
  (void)instance; (void)report_id; (void)type; (void)buffer; (void)reqlen;
  return 0;
}
