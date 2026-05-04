"""Custom @device.task patterns.

Three of the four task patterns documented in the README:

  1. Plain background loop -- decorate a no-arg async function.
  2. Per-second tick -- subscribe to the clock's TickBroadcaster so work
     lands on a Harp-second boundary, in lockstep with LED + heartbeat.
  3. Coroutine with arguments -- pass an already-built coroutine object.

Wiring:
    Sync UART  : UART(1) RX=GP5
    Status LED : GP25
    Threshold-trigger pin : GP15 (analog or digital sensor proxy here is
                            just a counter incrementing on every tick)
"""

import asyncio
from machine import Pin, UART

from usb.device.cdc import CDCInterface
import usb.device

from microharp import HarpDevice, CdcTransport, PT_U16, READ_ONLY, EVENT

LED_PIN = 25
ADDR_TICK_COUNT = 34   # plain background loop emits here
ADDR_PER_SECOND = 35   # per-second tick emits here
ADDR_ALERT = 36        # threshold watcher emits here

cdc = CDCInterface(baudrate=1_000_000, timeout=0, txbuf=2048, rxbuf=512)
usb.device.get().init(cdc, builtin_driver=True)

device = HarpDevice(
    transport=CdcTransport(cdc),
    sync_uart=UART(1, baudrate=100_000, bits=8, parity=None, stop=1, rx=Pin(5), timeout=0),
    led_pin=Pin(LED_PIN, Pin.OUT),
    who_am_i=1234,
    device_name=b"microharp-tasks",
    serial_number=5,
)

device.add_u16(ADDR_TICK_COUNT, access=READ_ONLY | EVENT, name="TickCount")
device.add_u16(ADDR_PER_SECOND, access=READ_ONLY | EVENT, name="PerSecond")
device.add_u16(ADDR_ALERT, access=READ_ONLY | EVENT, name="Alert")


# 1. Plain background loop --------------------------------------------------
@device.task
async def counter_loop():
    counter = 0
    payload = bytearray(2)
    while True:
        counter = (counter + 1) & 0xFFFF
        payload[0] = counter & 0xFF
        payload[1] = (counter >> 8) & 0xFF
        await device.emit(ADDR_TICK_COUNT, payload, PT_U16)
        await asyncio.sleep_ms(500)


# 2. Wake on per-second clock tick ------------------------------------------
@device.task
async def per_second_work():
    tick = device.clock.tick.subscribe()
    seconds = 0
    payload = bytearray(2)
    while True:
        await tick.wait()
        tick.clear()
        seconds = (seconds + 1) & 0xFFFF
        payload[0] = seconds & 0xFF
        payload[1] = (seconds >> 8) & 0xFF
        await device.emit(ADDR_PER_SECOND, payload, PT_U16)


# 3. Task with arguments ----------------------------------------------------
async def threshold_watcher(addr, threshold):
    """Emit an EVENT to `addr` whenever `counter` crosses `threshold`."""
    counter = 0
    payload = bytearray(2)
    while True:
        counter += 1
        if counter >= threshold:
            payload[0] = counter & 0xFF
            payload[1] = (counter >> 8) & 0xFF
            await device.emit(addr, payload, PT_U16)
            counter = 0
        await asyncio.sleep_ms(50)


device.task(threshold_watcher(ADDR_ALERT, threshold=42))

asyncio.run(device.run())
