"""Production-grade setup: Harp on a secondary CDC, REPL preserved.

Uses the upstream `usb-device-cdc` package (auto-installed via mip as a
microharp dependency) to expose a *second* CDC interface dedicated to
Harp traffic.  The host then sees:

    /dev/ttyACM0   <- REPL  (Thonny / mpremote / serial monitor)
    /dev/ttyACM1   <- Harp  (the application talks here)

Requires MicroPython >= 1.23 with the `usb.device.cdc` module.

The DTR-high callback (auto-installed by HarpDevice when a CdcTransport
is detected) drops OP_MODE to STANDBY whenever the host opens the port,
so the host never sees stale events from a previous session.
"""

import asyncio
from machine import Pin, UART

from usb.device.cdc import CDCInterface
import usb.device

from microharp import HarpDevice, CdcTransport, PT_U8

LED_PIN = 25
INPUT_PIN = 14
ADDR_DIGITAL_IN = 32

cdc = CDCInterface(baudrate=1_000_000, timeout=0, txbuf=2048, rxbuf=512)
usb.device.get().init(cdc, builtin_driver=True)

device = HarpDevice(
    transport=CdcTransport(cdc),
    sync_uart=UART(1, baudrate=100_000, bits=8, parity=None, stop=1, rx=Pin(5), timeout=0),
    led_pin=Pin(LED_PIN, Pin.OUT),
    who_am_i=1234,
    fw_version=(1, 0),
    hw_version=(1, 0),
    device_name=b"microharp-cdc",
    serial_number=2,
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
