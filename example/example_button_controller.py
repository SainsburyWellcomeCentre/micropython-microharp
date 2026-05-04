"""Button creates and cancels a worker task — declarative `task=` API.

The button emits Harp EVENTs at addr 32 (so the host sees every edge with
IRQ-accurate timestamps), and `task=worker` tells the framework to spawn
`worker()` on press and cancel it on release.

Under the hood: the IRQ uses `micropython.schedule` to bounce the
`asyncio.create_task` / `Task.cancel` out of hard IRQ context (those
allocate and so cannot run in IRQ).  No always-on controller task is
added — the worker only exists while the button is held.

Wiring (Pico/RP2040):
    Sync UART : UART(1) RX = GP5
    Status LED: GP25
    Input pin : GP14 with internal pull-up (active-low button)
    Harp CDC  : /dev/ttyACM1  (REPL stays on /dev/ttyACM0)
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
    device_name=b"microharp-btnctl",
    serial_number=2,
)


async def worker():
    n = 0
    try:
        while True:
            n += 1
            print("worker tick", n)
            await asyncio.sleep_ms(200)
    except asyncio.CancelledError:
        print("worker cancelled at tick", n)
        raise


button = Pin(INPUT_PIN, Pin.IN, Pin.PULL_UP)

device.bind_pin_event(
    button,
    address=ADDR_DIGITAL_IN,
    payload_type=PT_U8,
    trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING,
    name="DigitalInput",
    task=worker,
    active_level=0,
)


asyncio.run(device.run())
