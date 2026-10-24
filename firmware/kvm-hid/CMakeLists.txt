# dev_tinker KVM HID bridge for the Waveshare RP2350-USB-A.
#
#   kvm_hid_uart : host -> UART0 (GP0 TX / GP1 RX) ; USB-C = HID to the target
#   kvm_hid_cdc  : host -> USB-C (CDC ACM)         ; USB-A (PIO-USB) = HID to the target
#
# Build: see build.sh (Docker). Needs PICO_SDK_PATH and PICO_PIO_USB_PATH.
cmake_minimum_required(VERSION 3.13)

set(PICO_BOARD waveshare_rp2350_usb_a CACHE STRING "")
set(PICO_PLATFORM rp2350-arm-s CACHE STRING "")
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
set(KVM_SYS_KHZ 240000 CACHE STRING "CDC variant system clock (kHz): 120000 or 240000 for PIO-USB")
target_compile_definitions(kvm_hid_cdc PRIVATE KVM_HID_VERSION="${KVM_HID_VERSION}"
    KVM_SYS_KHZ=${KVM_SYS_KHZ})
pico_generate_pio_header(kvm_hid_cdc ${CMAKE_CURRENT_LIST_DIR}/src/ws2812.pio)
target_link_libraries(kvm_hid_cdc PRIVATE
    pico_stdlib pico_unique_id pico_multicore hardware_pio
    tinyusb_device tinyusb_board pico_pio_usb)
pico_enable_stdio_uart(kvm_hid_cdc 1)
pico_enable_stdio_usb(kvm_hid_cdc 0)
# PIO-USB answers IN tokens in software within ~1 us; run everything from RAM
# so a flash (XIP) stall on either core never delays that interrupt.
pico_set_binary_type(kvm_hid_cdc copy_to_ram)
pico_add_extra_outputs(kvm_hid_cdc)
