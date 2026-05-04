"""Motor-control sequence with chained cancellation and sticky errors.

The pin GP14 (active-low) is a pure trigger — it has *no* Harp event
register; pressing it acts as if the host wrote 0x21 = 1.

Registers:
    0x21 — R/W EVENT.  Write 1 requests taskA.  Auto-resets to 0
                       whenever the device returns to idle / error.
                       Writing 0 from the host is ignored.
    0x22 — R/W EVENT.  Status:
                         0 = idle
                         1 = taskA running
                         2 = taskB running
                         3 = taskA error/timeout (sticky)
                         4 = taskB error/timeout (sticky)
                       Host writes 0 to clear sticky errors.
    0x23 — R/O EVENT.  Cancel pin (GP15, pull-down).  Rising edge
                       cancels taskA and starts taskB (only allowed
                       when status == 1).

Triggers and guards:
    pin GP14 falling          OR write 0x21 = 1   →  sequence("A")
    pin GP15 rising                                →  sequence("B")
    sequence("A") allowed only when status == 0   (idle)
    sequence("B") allowed only when status == 1   (taskA running)

Per-state timeouts (asyncio.wait_for):
    taskA timeout = 20 s  → on_timeout fires, status → 3, 0x21 → 0
    taskB timeout = 30 s  → on_timeout fires, status → 4, 0x21 → 0

FakeMotor demo timing:
    taskA targets pos 100 in 30 s  (will time out unless cancelled by 0x23)
    taskB targets pos   0 in  5 s  (succeeds normally)
"""

import asyncio
import time
import micropython
from machine import Pin, UART

from microharp import HarpDevice, CdcTransport, PT_U8, READ_WRITE, EVENT


# ---- Fake motor ------------------------------------------------------------

class FakeMotor:
    """Simulated position-seeking motor.

    Position interpolates linearly from `start_pos` to `target` over
    `duration_ms`.  `poll()` returns "moving", "done", or "error" — and
    "done" the moment current position equals target (no extra wait).
    """
    __slots__ = ("_start_ms", "_duration_ms", "_start_pos", "_target", "_fail")

    def __init__(self):
        self._start_ms = 0
        self._duration_ms = 0
        self._start_pos = 0
        self._target = 0
        self._fail = False

    @property
    def position(self):
        if self._duration_ms <= 0:
            return self._target
        elapsed = time.ticks_diff(time.ticks_ms(), self._start_ms)
        if elapsed >= self._duration_ms:
            return self._target
        return self._start_pos + (self._target - self._start_pos) * elapsed // self._duration_ms

    def move_to(self, target, duration_ms, *, fail=False):
        self._start_pos   = self.position
        self._start_ms    = time.ticks_ms()
        self._duration_ms = duration_ms
        self._target      = target
        self._fail        = fail

    def poll(self):
        elapsed = time.ticks_diff(time.ticks_ms(), self._start_ms)
        if self._fail and elapsed >= self._duration_ms:
            return "error"
        if self.position == self._target:
            return "done"
        return "moving"


# ---- Device + registers ----------------------------------------------------

ADDR_RUN       = 0x21
ADDR_STATUS      = 0x22
ADDR_PIN_CANCEL  = 0x23


Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=1)

from usb.device.cdc import CDCInterface
import usb.device
from microharp import CdcTransport
cdc = CDCInterface(baudrate=1_000_000, timeout=0, txbuf=2048, rxbuf=512)
usb.device.get().init(cdc, builtin_driver=True)
transport = CdcTransport(cdc)

device = HarpDevice(
    transport=transport,
    sync_uart=UART(1, baudrate=100_000, bits=8, parity=None, stop=1, rx=Pin(9), timeout=0),
    led_pin=Pin(19, Pin.OUT),
    who_am_i=1234,
    fw_version=(1, 0),
    hw_version=(1, 0),
    device_name=b"microharp-motor",
    serial_number=5,
)

run_reg  = device.add_register(ADDR_RUN,  PT_U8, access=READ_WRITE | EVENT, name="State")
status_reg = device.add_register(ADDR_STATUS, PT_U8, access=READ_WRITE | EVENT, name="Status")


# ---- Status / state-flag helper -------------------------------------------

async def set_status(code, _sr=status_reg, _ar=run_reg):
    """Update 0x22 (always EVENT) and 0x21 (auto-reset on idle/error)."""
    _sr.storage[0] = code
    await device.emit(ADDR_STATUS, code)
    if code in (0, 3, 4) and _ar.storage[0] != 0:
        _ar.storage[0] = 0
        await device.emit(ADDR_RUN, 0)


# ---- Tasks -----------------------------------------------------------------

motor = FakeMotor()


async def _drive(target, duration_ms, error_code):
    motor.move_to(target, duration_ms)
    while True:
        s = motor.poll()
        if s == "done":
            return
        if s == "error":
            await set_status(error_code)
            return
        await asyncio.sleep_ms(50)


async def taskA():
    await set_status(1)
    try:
        await _drive(target=100, duration_ms=30_000, error_code=3)
        if status_reg.storage[0] == 1:
            await set_status(0)
    except asyncio.CancelledError:
        # Cancelled by chain to taskB — taskB will set status=2.
        raise


async def taskB():
    await set_status(2)
    try:
        await _drive(target=0, duration_ms=5_000, error_code=4)
        if status_reg.storage[0] == 2:
            await set_status(0)
    except asyncio.CancelledError:
        raise


async def on_timeout(name):
    await set_status(3 if name == "A" else 4)


# ---- Sequence with strict guards ------------------------------------------

sequence = device.add_task_sequence(
    states={"A": (taskA, 20), "B": (taskB, 30)},
    on_timeout=on_timeout,
    guards={
        "A": lambda _r=status_reg: _r.storage[0] == 0,    # only from idle
        "B": lambda _r=status_reg: _r.storage[0] == 1,    # only from running A
    },
)


# ---- Triggers --------------------------------------------------------------

# Pin GP14 falling acts as if host wrote 0x21 = 1.  No Harp event register
# for the pin itself (address=None).  The IRQ updates 0x21 storage in hard
# IRQ context, then bounces an EVENT-emit + sequence-trigger through
# micropython.schedule so create_task runs in safe context.

async def _pin_request_async():
    await device.emit(ADDR_RUN, 1)
    sequence("A")                                          # gated by status==0


def _pin_request_sched(_arg):
    asyncio.create_task(_pin_request_async())


def _on_pin_irq(level, _r=run_reg, _s=micropython.schedule, _f=_pin_request_sched):
    # Falling-edge only IRQ, but check level for safety on noisy boards.
    if level == 0 and _r.storage[0] != 1:
        _r.storage[0] = 1
        _s(_f, 0)


device.bind_pin_event(
    Pin(29, Pin.IN, Pin.PULL_UP),
    address=None,                                          # no event register
    trigger=Pin.IRQ_FALLING,
    on_irq=_on_pin_irq,
)


# Host write to 0x21: write 1 requests taskA; write 0 ignored (per spec).

async def _on_state_write(reg, payload):
    if payload[0] == 1:
        reg.storage[0] = 1                                  # mirror; host knows
        sequence("A")                                       # gated


run_reg.on_write = _on_state_write


# Cancel pin GP15 (rising edge): start taskB.  Falling edge: no-op.
device.bind_pin_event(
    Pin(15, Pin.IN, Pin.PULL_DOWN),
    address=ADDR_PIN_CANCEL, payload_type=PT_U8,
    trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING,
    sequence=sequence, level_state={1: "B"},                # falling: no-op
    name="PinCancel",
)


asyncio.run(device.run())
