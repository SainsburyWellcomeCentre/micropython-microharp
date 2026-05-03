"""Event source helper: timestamp at the IRQ, encode in a task.

Pattern:
    1. A hard IRQ (GPIO edge, timer, peripheral) fires.  In <10 us it:
         - reads time.ticks_us()
         - reads/computes the payload (e.g. pin level, ADC sample)
         - writes (ticks_us, payload) into a single SPSC ring slot
         - bumps a head index and sets a uasyncio.ThreadSafeFlag
       No allocations, no asyncio internals touched.
    2. The drain task (`EventSource.run`) wakes on the flag, pulls every
       new entry from the ring, leases a slab, encodes the Harp EVENT
       message with the IRQ's timestamp, and pushes onto `tx_queue`.

Why ThreadSafeFlag here: the setter is a hard IRQ, so we cross the
asyncio-internals boundary.  Inside the task and below the boundary we
use plain asyncio primitives.

Why a ring rather than one slot: bursts.  A button bouncing or a fast
encoder can fire several edges per ms; a single slot drops events.
"""

import micropython
import asyncio
import time
import array

from .framing import MSG_EVENT, encode_into
from .clock import Clock, _ticks_us  # fast hardware-timer read on RP2
from .registers import RegisterBank
try:
    from asyncio import Queue  # available on CPython and MicroPython ≥1.20 with full asyncio
except (ImportError, AttributeError):
    from .queue import Queue  # fallback for bare RP2 / stripped MicroPython builds
from .transport import SlabPool

_native = getattr(micropython, "native", lambda fn: fn)


# ---------------------------------------------------------------------------
# SPSC ring of (ticks_us:U32, payload_word:U32) pairs
# ---------------------------------------------------------------------------


class _IrqRing:
    """Lock-free SPSC: one IRQ writer, one task reader.  Power-of-two size."""

    __slots__ = ("_size", "_mask", "_ts", "_pl", "_head", "_tail")

    def __init__(self, size: int = 16):
        # Round up to a power of two.
        n = 1
        while n < size:
            n <<= 1
        self._size = n
        self._mask = n - 1
        self._ts = array.array("I", [0] * n)  # ticks_us
        self._pl = array.array("I", [0] * n)  # payload word
        # Use bytearray of length 4 reinterpreted as a single int32 cell so
        # IRQ writes are bytecode-atomic on all ports.
        self._head = array.array("I", [0])
        self._tail = array.array("I", [0])

    # Head/tail counters wrap inside this 16-bit window.  Using a smaller
    # mask than the array's uint32 capacity keeps every load/store inside
    # MicroPython's small-int range (~30 bits on 32-bit ports), so the
    # increment/mask in IRQ context never boxes a big-int and never
    # triggers MemoryError.  16 bits >> any plausible ring size, so
    # ordering is preserved.
    _COUNTER_MASK = 0xFFFF

    @_native
    def push_irq(self, ts_us: int, payload_word: int):
        """Hard-IRQ-safe.  Drops the entry if the ring is full."""
        head = self._head[0]
        tail = self._tail[0]
        mask = self._mask
        nxt_pos = (head + 1) & 0xFFFF       # wrap counter (small-int safe)
        if (nxt_pos & mask) == (tail & mask):
            return False  # full; drop
        i = head & mask
        self._ts[i] = ts_us
        self._pl[i] = payload_word
        self._head[0] = nxt_pos
        return True

    def pop(self):
        """Non-blocking; returns (ts_us, payload_word) or None."""
        head = self._head[0]
        tail = self._tail[0]
        if head == tail:
            return None
        i = tail & self._mask
        ts = self._ts[i]
        pl = self._pl[i]
        self._tail[0] = (tail + 1) & 0xFFFF
        return ts, pl

    def empty(self):
        return self._head[0] == self._tail[0]


# ---------------------------------------------------------------------------
# Convenience: stamp & enqueue from any context
# ---------------------------------------------------------------------------


def emit_event_from_irq(ring: _IrqRing, flag, payload_word: int):
    """Hard-IRQ-safe single-call helper.

    Captures the fast tick value and pushes (ts, payload_word) onto `ring`,
    then sets `flag` (a `uasyncio.ThreadSafeFlag`) to wake the drain task.
    Payload is masked to 24 bits so the result fits in a small int — large
    enough for a U8/U16/U24 packed value, while keeping IRQ allocation-free.
    """
    ring.push_irq(_ticks_us(), payload_word & 0xFFFFFF)
    flag.set()


# ---------------------------------------------------------------------------
# EventSource: ring + drain task scaffold
# ---------------------------------------------------------------------------


class EventSource:
    """Owns one IRQ ring and emits Harp EVENT messages onto `tx_queue`.

    Subclass or supply `pack_payload(payload_word, out_buf, off) -> n_bytes`
    to control how the IRQ's 32-bit `payload_word` becomes the on-wire
    payload.  Default: 1-byte U8 (low 8 bits).
    """

    __slots__ = ("address", "port", "payload_type", "ring", "flag",
                 "clock", "tx_q", "slabs", "_pack", "_op_reg",
                 # Pre-bound callables for hard-IRQ paths (see emit()):
                 "_ring_push", "_flag_set")

    def __init__(self, address: int, payload_type: int, clock: Clock, tx_queue: Queue, slab_pool: SlabPool,
                 *, port: int = 255, ring_size: int = 16, pack=None, bank: RegisterBank):
        self.address = address
        self.port = port
        self.payload_type = payload_type
        self.ring = _IrqRing(ring_size)
        self.flag = asyncio.ThreadSafeFlag()
        self.clock = clock
        self.tx_q = tx_queue
        self.slabs = slab_pool
        self._pack = pack or self._default_pack
        # Pre-bind methods used in IRQ context.  Some MicroPython builds
        # do not have the LOAD_METHOD optimization for nested attribute
        # access (`self.ring.push_irq(...)`) and create a transient
        # bound-method object on every call — fatal in hard IRQ.  Caching
        # the bound methods once here means the IRQ path is just direct
        # function calls.
        self._ring_push = self.ring.push_irq
        self._flag_set  = self.flag.set
        # Optional reference to OperationControl for Active-mode gating —
        # Device.md §Operation Mode forbids events in Standby.  Set later
        # via `attach_bank()` if the bank wasn't available at init.
        self._op_reg = None
        if bank is not None:
            self.attach_bank(bank)

    def attach_bank(self, bank: RegisterBank):
        from .registers import R_OPERATION_CONTROL
        self._op_reg = bank.get(R_OPERATION_CONTROL)

    # IRQ-side hook -------------------------------------------------------

    def emit(self, payload_word: int):
        """Hard-IRQ-safe.  Stamp time, push, signal.

        `payload_word` is masked to 24 bits to stay inside MicroPython's
        small-int range — wide enough for any U8/U16 register payload
        plus a few flag bits, while keeping the IRQ allocation-free.
        Use a custom `pack` callback if you need wider event payloads.

        Calls only pre-bound methods (`_ring_push`, `_flag_set`) so the
        IRQ path performs no attribute lookups beyond `self.<slot>`.
        """
        self._ring_push(_ticks_us(), payload_word & 0xFFFFFF)
        self._flag_set()

    # Default packing: 1 byte (U8).  Writes into `scratch` starting at 0,
    # returns the number of bytes written.
    @staticmethod
    def _default_pack(payload_word: int, scratch: bytearray) -> int:
        scratch[0] = payload_word & 0xFF
        return 1

    # Drain task ----------------------------------------------------------

    async def run(self):
        # Cache every attribute and bound method we use in the loop so
        # the hot path is local-variable LOAD_FAST rather than LOAD_ATTR.
        clock = self.clock
        ring_pop = self.ring.pop
        flag_wait = self.flag.wait
        slabs = self.slabs
        slabs_lease = slabs.lease
        slabs_buf = slabs.buf
        slabs_set_len = slabs.set_length
        tx_put = self.tx_q.put
        addr = self.address
        port = self.port
        pt = self.payload_type
        pack = self._pack
        scratch = bytearray(16)  # per-task, no contention

        # Local imports for the OP_MODE constants — avoid module-level
        # name shadowing.
        from .registers import OP_OP_MODE_MASK, OP_OP_MODE_ACTIVE

        while True:
            await flag_wait()
            # Drain everything queued.  ThreadSafeFlag is auto-clearing.
            while True:
                entry = ring_pop()
                if entry is None:
                    break
                t_irq_us, payload_word = entry

                # Spec gating: events MUST NOT be sent in Standby mode.
                # We still drain the ring (so it doesn't backlog) but
                # silently drop entries until the device is Active again.
                op_reg = self._op_reg
                if op_reg is not None and (op_reg.storage[0] & OP_OP_MODE_MASK) != OP_OP_MODE_ACTIVE:
                    continue

                # Translate the IRQ-captured ticks_us into Harp (seconds,
                # ticks) using the clock's epoch — the event's wall-clock
                # time is fixed at IRQ arrival, not at encoding time.
                secs, ticks = _convert_to_harp_ts(clock, t_irq_us)

                n_payload = pack(payload_word, scratch)

                idx = await slabs_lease()
                buf = slabs_buf(idx)
                n = encode_into(
                    buf,
                    MSG_EVENT,
                    addr,
                    port,
                    pt,
                    payload=scratch,
                    payload_len=n_payload,
                    ts_seconds=secs,
                    ts_ticks=ticks,
                )
                slabs_set_len(idx, n)
                await tx_put(idx)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@_native
def _convert_to_harp_ts(clock: Clock, t_irq_us: int):
    """Translate an IRQ-captured ticks value into (seconds, ticks_32us).

    Uses the same epoch the clock uses internally, so the returned
    timestamp matches what `clock.now()` would have returned at exactly
    `t_irq_us`.

    `t_irq_us` should come from the same source the clock uses
    (`microharp.clock._ticks_us`).  On RP2 this reads the hardware TIMERAWL
    register directly via viper; elsewhere it's `time.ticks_us`.  The IRQ
    helpers below take care of using the right source.
    """
    # Reach into clock fields directly — saves a call back into _compute.
    epoch = clock._epoch_us
    secs0 = clock._seconds
    delta = time.ticks_diff(t_irq_us, epoch)
    if delta < 0:
        delta = 0
    if delta >= 1_000_000:
        extra = delta // 1_000_000
        rem = delta - extra * 1_000_000
        return (secs0 + extra) & 0xFFFFFFFF, rem >> 5  # // 32
    return secs0, delta >> 5
