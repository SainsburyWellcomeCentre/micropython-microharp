"""Minimal Harp device over the REPL CDC.

Simplest possible setup: StdioTransport speaks Harp on whatever the REPL
is using (sys.stdin.buffer / sys.stdout.buffer).  Good for first-time
bring-up; the REPL becomes unusable while the device is running because
Harp framing collides with REPL bytes on the same CDC.  For production,
see `example_secondary_cdc.py` (Harp on a separate CDC interface).

Wiring (Pico/RP2040):
    Sync UART : UART(1) RX = GP5, 100 000 baud, 8N1 (optional; sync_uart=None
                disables it and the LED falls back to the 4 Hz unsynced blink)
    Status LED: GP25 (Pico onboard)
    Input pin : GP14 with internal pull-up, IRQ on both edges -> EVENT @ 32
"""

import asyncio
from machine import Pin, UART

from microharp import HarpDevice, StdioTransport, PT_U8

LED_PIN = 25
INPUT_PIN = 14
ADDR_DIGITAL_IN = 32

device = HarpDevice(
    transport=StdioTransport(),
    sync_uart=UART(1, baudrate=100_000, bits=8, parity=None, stop=1, rx=Pin(5), timeout=0),
    led_pin=Pin(LED_PIN, Pin.OUT),
    who_am_i=1234,
    fw_version=(1, 0),
    hw_version=(1, 0),
    device_name=b"microharp-basic",
    serial_number=1,
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
