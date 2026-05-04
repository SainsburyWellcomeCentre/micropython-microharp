"""Three-state sequence (A, B, neither) driven by a button or a register,
with per-state timeouts that emit an error EVENT on register 0x22.

Wiring (Pico/RP2040):
    Sync UART  : UART(1) RX = GP5
    Status LED : GP25
    Button     : GP14 with internal pull-up (active-low)
    Harp CDC   : /dev/ttyACM1  (REPL stays on /dev/ttyACM0)

Registers:
    0x20 — button event   (R/O EVENT, payload = pin level)
    0x21 — state R/W byte (write 1 → run taskA, write 0 → run taskB)
    0x22 — error EVENT    (1 = taskA timeout, 2 = taskB timeout)

Triggers (any source moves the sequence):
    falling edge OR write 0x21=1  →  cancel current, start taskA
    rising  edge OR write 0x21=0  →  cancel current, start taskB

Per-state timeouts (asyncio.wait_for, seconds):
    taskA timeout =  5 s  → cancel, EVENT 0x22 = 1, transition to neither
    taskB timeout = 10 s  → cancel, EVENT 0x22 = 2, transition to neither

Same-state retriggers dedupe (writing 0x21=1 while taskA is already
running is a no-op).  At any moment exactly one task runs (taskA or
taskB), or neither.  No always-on framework task is added.
"""

import asyncio
from machine import Pin, UART

from usb.device.cdc import CDCInterface
import usb.device

from microharp import HarpDevice, CdcTransport, PT_U8

ADDR_BUTTON = 0x20
ADDR_STATE  = 0x21
ADDR_ERROR  = 0x22

cdc = CDCInterface(baudrate=1_000_000, timeout=0, txbuf=2048, rxbuf=512)
usb.device.get().init(cdc, builtin_driver=True)

device = HarpDevice(
    transport=CdcTransport(cdc),
    sync_uart=UART(1, baudrate=100_000, bits=8, parity=None, stop=1, rx=Pin(5), timeout=0),
    led_pin=Pin(25, Pin.OUT),
    who_am_i=1234,
    fw_version=(1, 0),
    hw_version=(1, 0),
    device_name=b"microharp-3state",
    serial_number=4,
)


async def taskA():
    n = 0
    try:
        while True:
            n += 1
            print("A", n)
            await asyncio.sleep_ms(200)
    except asyncio.CancelledError:
        print("A cancelled at", n)
        raise


async def taskB():
    n = 0
    try:
        while True:
            n += 1
            print("B", n)
            await asyncio.sleep_ms(200)
    except asyncio.CancelledError:
        print("B cancelled at", n)
        raise


# Error register: one helper call sets up the R/O EVENT register and
# returns an `await emit_error(value)` closure.  No drain task added.
emit_error = device.bind_event_register(ADDR_ERROR, PT_U8, name="Error")


async def on_timeout(name):
    await emit_error(1 if name == "A" else 2)
    print("timeout", name)


sequence = device.add_task_sequence(
    states={
        "A": (taskA,  5),
        "B": (taskB, 10),
    },
    on_timeout=on_timeout,
)

button = Pin(14, Pin.IN, Pin.PULL_UP)
device.bind_pin_event(
    button, address=ADDR_BUTTON, payload_type=PT_U8,
    trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING,
    sequence=sequence, level_state={0: "A", 1: "B"},
    name="Button",
)

device.bind_state_register(
    address=ADDR_STATE, payload_type=PT_U8,
    sequence=sequence, mapping={1: "A", 0: "B"},
    name="State",
)

asyncio.run(device.run())
