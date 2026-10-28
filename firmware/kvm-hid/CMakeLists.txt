# dev_tinker KVM HID bridge for the Waveshare RP2350-USB-A or RP2040-Zero.
#
#   kvm_hid_uart : host -> UART0 (GP0 TX / GP1 RX) ; USB-C = HID to the target
#   kvm_hid_cdc  : host -> USB-C (CDC ACM)         ; PIO-USB (D+ / D- on GPIOs) = HID to the target
#
# Build: see build.sh (Docker). Needs PICO_SDK_PATH and PICO_PIO_USB_PATH.
cmake_minimum_required(VERSION 3.13)

# KVM_BOARD picks the chip, the SDK board and the PIO-USB pins (D- = D+ + 1):
#   rp2350_usb_a : Waveshare RP2350-USB-A, its USB-A receptacle (GP12/GP13)
#   rp2040_zero  : Waveshare RP2040-Zero, a USB cable wired to GP14/GP15
set(KVM_BOARD rp2350_usb_a CACHE STRING "rp2350_usb_a or rp2040_zero")
if(KVM_BOARD STREQUAL "rp2350_usb_a")
    set(PICO_BOARD waveshare_rp2350_usb_a)
    set(PICO_PLATFORM rp2350-arm-s)
    set(KVM_DP_PIN_DEFAULT 12)
elseif(KVM_BOARD STREQUAL "rp2040_zero")
    set(PICO_BOARD waveshare_rp2040_zero)
    set(PICO_PLATFORM rp2040)
    set(KVM_DP_PIN_DEFAULT 14)
else()
    message(FATAL_ERROR "KVM_BOARD: rp2350_usb_a or rp2040_zero")
endif()
set(KVM_PIO_USB_DP_PIN ${KVM_DP_PIN_DEFAULT} CACHE STRING "CDC variant: PIO-USB D+ GPIO (D- is the next one)")
# Colour order of the board's WS2812-type LED: 1 = RGB (seen on the
# RP2350-USB-A; reported for the RP2040-Zero too), 0 = the usual GRB
set(KVM_LED_RGB 1 CACHE STRING "LED takes R, G, B (1) or G, R, B (0)")
add_compile_definitions(KVM_LED_RGB=${KVM_LED_RGB})
include($ENV{PICO_SDK_PATH}/external/pico_sdk_import.cmake)

project(kvm_hid C CXX ASM)
set(CMAKE_C_STANDARD 11)
pico_sdk_init()

set(KVM_HID_VERSION "0.1" CACHE STRING "")
set(KVM_UART_BAUD 115200 CACHE STRING "UART variant baud rate")

set(COMMON_SRC
    ${CMAKE_CURRENT_LIST_DIR}/src/proto.c
    ${CMAKE_CURRENT_LIST_DIR}/src/hid_reports.c
    ${CMAKE_CURRENT_LIST_DIR}/src/bridge.c
    ${CMAKE_CURRENT_LIST_DIR}/src/led.c)

# ---- UART variant -----------------------------------------------------------
add_executable(kvm_hid_uart ${COMMON_SRC}
    src/uart/main.c src/uart/usb_descriptors.c)
target_include_directories(kvm_hid_uart PRIVATE src src/uart)
target_compile_definitions(kvm_hid_uart PRIVATE
    KVM_HID_VERSION="${KVM_HID_VERSION}" KVM_UART_BAUD=${KVM_UART_BAUD})
pico_generate_pio_header(kvm_hid_uart ${CMAKE_CURRENT_LIST_DIR}/src/ws2812.pio)
target_link_libraries(kvm_hid_uart PRIVATE
    pico_stdlib pico_unique_id hardware_pio hardware_uart hardware_irq
    tinyusb_device tinyusb_board)
target_compile_options(kvm_hid_uart PRIVATE -Wall -Wextra)
pico_add_extra_outputs(kvm_hid_uart)

# ---- CDC variant ------------------------------------------------------------
add_subdirectory($ENV{PICO_PIO_USB_PATH} pico_pio_usb)

add_executable(kvm_hid_cdc ${COMMON_SRC}
    src/cdc/main.c src/cdc/usb_descriptors.c)
target_include_directories(kvm_hid_cdc PRIVATE src src/cdc)
set(KVM_SYS_KHZ 120000 CACHE STRING "CDC variant system clock (kHz): 120000 or 240000 for PIO-USB")
target_compile_definitions(kvm_hid_cdc PRIVATE KVM_HID_VERSION="${KVM_HID_VERSION}"
    KVM_SYS_KHZ=${KVM_SYS_KHZ} KVM_PIO_USB_DP_PIN=${KVM_PIO_USB_DP_PIN})
pico_generate_pio_header(kvm_hid_cdc ${CMAKE_CURRENT_LIST_DIR}/src/ws2812.pio)
target_link_libraries(kvm_hid_cdc PRIVATE
    pico_stdlib pico_unique_id pico_multicore hardware_pio hardware_vreg
    tinyusb_device tinyusb_board pico_pio_usb)
pico_enable_stdio_uart(kvm_hid_cdc 1)
pico_enable_stdio_usb(kvm_hid_cdc 0)
# PIO-USB answers IN tokens in software within ~1 us; run everything from RAM
# so a flash (XIP) stall on either core never delays that interrupt.
pico_set_binary_type(kvm_hid_cdc copy_to_ram)
pico_add_extra_outputs(kvm_hid_cdc)
