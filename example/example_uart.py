"""Harp over a hardware UART (FTDI bridge or device-to-device link).

Use this when the board has no USB CDC, or when you want a deterministic
high-baud-rate link to another MCU / FTDI USB-UART adapter without going
through the host's USB stack.

Wiring (Pico/RP2040 example):
    Harp data UART : UART(0) TX=GP0, RX=GP1, 921 600 baud, 8N1
    Sync UART      : UART(1) RX=GP5, 100 000 baud, 8N1
    Status LED     : GP25
    Input pin      : GP14 with pull-up, IRQ on both edges -> EVENT @ 32
"""

import asyncio
from machine import Pin, UART

from microharp import HarpDevice, UartTransport, PT_U8

DATA_UART_TX = 0
DATA_UART_RX = 1
DATA_UART_BAUD = 921_600

LED_PIN = 25
INPUT_PIN = 14
ADDR_DIGITAL_IN = 32

data_uart = UART(
    0,
    baudrate=DATA_UART_BAUD,
    tx=Pin(DATA_UART_TX),
    rx=Pin(DATA_UART_RX),
    timeout=0,
)

device = HarpDevice(
    transport=UartTransport(data_uart),
    sync_uart=UART(1, baudrate=100_000, bits=8, parity=None, stop=1, rx=Pin(5), timeout=0),
    led_pin=Pin(LED_PIN, Pin.OUT),
    who_am_i=1234,
    fw_version=(1, 0),
    hw_version=(1, 0),
    device_name=b"microharp-uart",
    serial_number=3,
)

button = Pin(INPUT_PIN, Pin.IN, Pin.PULL_UP)
device.bind_pin_event(
    button,
    address=ADDR_DIGITAL_IN,
    payload_type=PT_U8,
    trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING,
    name="DigitalInput",
)

asyncio.run(device.run())
