"""Periodic ADC sampling -> Harp EVENT, driven by a small @device.task.

For fixed-rate emission of a register value, the typical pattern is a
short `@device.task` that reads the sensor, encodes it, and pushes an
EVENT via `device.emit(...)`.  Timestamp accuracy is bounded only by the
asyncio wake jitter (~1 ms typical, well below Harp's 32 us tick).

For sub-millisecond sampling (kHz rates), use a `machine.Timer` to call
the EventSource's `.emit(word)` directly instead -- the timestamp is then
captured at IRQ time, not at the asyncio wake.

Wiring:
    ADC pin    : GP26 (ADC0 on RP2040)
    Sync UART  : UART(1) RX=GP5
    Status LED : GP25
"""

import asyncio
from machine import Pin, UART, ADC

from microharp import HarpDevice, StdioTransport, PT_U16, READ_ONLY, EVENT

ADC_PIN = 26
LED_PIN = 25
ADDR_ADC = 33
SAMPLE_PERIOD_MS = 100  # 10 Hz

adc = ADC(Pin(ADC_PIN))

device = HarpDevice(
    transport=StdioTransport(),
    sync_uart=UART(1, baudrate=100_000, bits=8, parity=None, stop=1, rx=Pin(5), timeout=0),
    led_pin=Pin(LED_PIN, Pin.OUT),
    who_am_i=1234,
    device_name=b"microharp-adc",
    serial_number=4,
)

# Declare the register (READ_ONLY | EVENT so hosts can both poll on demand
# and subscribe to the periodic stream).
adc_reg = device.add_u16(ADDR_ADC, access=READ_ONLY | EVENT, name="ADC")


# Refresh storage from the ADC on every host READ.
@device.on_read(address=ADDR_ADC, payload_type=PT_U16, name="ADC")
async def _read_adc(reg):
    raw = adc.read_u16()
    reg.storage[0] = raw & 0xFF
    reg.storage[1] = (raw >> 8) & 0xFF


# Periodic emission task.
@device.task
async def _adc_stream():
    payload = bytearray(2)
    while True:
        raw = adc.read_u16()
        payload[0] = raw & 0xFF
        payload[1] = (raw >> 8) & 0xFF
        # Update register storage too so on_read is consistent if the
        # host READs between emissions.
        adc_reg.storage[0] = payload[0]
        adc_reg.storage[1] = payload[1]
        await device.emit(ADDR_ADC, payload, PT_U16)
        await asyncio.sleep_ms(SAMPLE_PERIOD_MS)


asyncio.run(device.run())
