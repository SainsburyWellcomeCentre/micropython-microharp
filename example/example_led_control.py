"""LED control via a Harp register — simple on_write + task loop.

Wiring (Pico/RP2040):
    Sync UART  : UART(1) RX = GP5
    Status LED : GP25  (managed automatically by HarpDevice)
    User LED   : GP14  (controlled via register 0x20)

Register:
    0x20 — LEDControl (R/W, PT_U8)
        0 = off
        1 = on  (steady)
        2 = blink (1 Hz, 500 ms on / 500 ms off)

Writing any other value is a no-op.  Reading back returns the last written value.
"""

import asyncio
from machine import Pin, UART
from usb.device.cdc import CDCInterface
import usb.device
from microharp import HarpDevice, CdcTransport, PT_U8, READ_WRITE

USER_LED_PIN   = 14
STATUS_LED_PIN = 25
ADDR_LED       = 0x20

cdc = CDCInterface(baudrate=1_000_000, timeout=0, txbuf=2048, rxbuf=512)
usb.device.get().init(cdc, builtin_driver=True)

device = HarpDevice(
    transport=CdcTransport(cdc),
    sync_uart=UART(1, baudrate=100_000, bits=8, parity=None, stop=1, rx=Pin(5), timeout=0),
    led_pin=Pin(STATUS_LED_PIN, Pin.OUT),
    who_am_i=1234,
    fw_version=(1, 0),
    hw_version=(1, 0),
    device_name=b"microharp-led",
    serial_number=5,
)

led = Pin(USER_LED_PIN, Pin.OUT)
led_mode = 0  # 0=off, 1=on, 2=blink

led_reg = device.add_u8(ADDR_LED, access=READ_WRITE, name="LEDControl")

@device.on_write(address=ADDR_LED, payload_type=PT_U8, name="LEDControl")
async def _write_led(reg, payload):
    global led_mode
    led_mode = payload[0]
    reg.storage[0] = payload[0]

@device.task
async def _led_loop():
    global led_mode
    while True:
        if led_mode == 1:
            led.value(1)
            await asyncio.sleep_ms(50)
        elif led_mode == 2:
            led.value(1)
            await asyncio.sleep_ms(500)
            led.value(0)
            await asyncio.sleep_ms(500)
        else:
            led.value(0)
            await asyncio.sleep_ms(50)

asyncio.run(device.run())
