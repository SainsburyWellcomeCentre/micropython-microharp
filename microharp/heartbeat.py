"""Heartbeat task — periodic 1 Hz status EVENT.

Per Device.md §R_OPERATION_CTRL the device must emit one of two events
every second, gated by OperationControl bits:

    * HEARTBEAT_EN (bit 2) set → emit R_TIMESTAMP_SECOND (U32).
    * ALIVE_EN (bit 7, deprecated) set, HEARTBEAT_EN clear → emit
        R_TIMESTAMP_SECOND (U32).
    * Both set → still emit R_TIMESTAMP_SECOND.
  * Both clear → silent.

Events MUST NOT be sent in Standby mode (Device.md §Operation Mode), so
this task additionally gates on OP_MODE == Active.

Wake source is `clock.tick`, the same per-second broadcaster the LED
uses, so heartbeat phase and LED toggle are co-aligned.  Allocation-free
in steady state: the payload buffer and bound methods are cached.
"""

import struct

from .framing import MSG_EVENT, encode_into, PT_U32
from .clock import Clock
try:
    from asyncio import Queue  # available on CPython and MicroPython ≥1.20 with full asyncio
except (ImportError, AttributeError):
    from .queue import Queue  # fallback for bare RP2 / stripped MicroPython builds
from .transport import SlabPool
from .registers import (
    R_TIMESTAMP_SECOND,
    R_OPERATION_CONTROL,
    OP_HEARTBEAT_EN,
    OP_ALIVE_EN,
    OP_OP_MODE_MASK,
    OP_OP_MODE_ACTIVE,
    RegisterBank,
)


async def heartbeat_task(clock: Clock, bank: RegisterBank, tx_queue: Queue, slab_pool: SlabPool, *, port: int = 255):
    """Emit a 1 Hz status EVENT when enabled and OP_MODE == Active."""
    tick_ev = clock.tick.subscribe()
    op_reg  = bank.get(R_OPERATION_CONTROL)

    # Pre-allocated payload buffers reused across emissions.
    pl_u32 = bytearray(4)

    while True:
        await tick_ev.wait()
        tick_ev.clear()

        if op_reg is None:
            continue
        op_val = op_reg.storage[0]

        # Spec: events MUST NOT be sent in Standby.
        if (op_val & OP_OP_MODE_MASK) != OP_OP_MODE_ACTIVE:
            continue

        # Read the boundary snapshot taken inside apply_sync (synced) or
        # second_ticker (unsynced).  Using clock.now() here would pick up
        # any asyncio wake jitter — USB drain back-pressure, GC, slow
        # dispatch handlers — and put it on the wire timestamp.  The
        # snapshot is captured at the actual second boundary, so the
        # outgoing TS has µs-level precision when synced and ~poll_ms
        # precision otherwise, regardless of how late this task wakes.
        secs  = clock._tick_secs[0]
        ticks = 0
        # secs, ticks = clock.now()

        if op_val & OP_HEARTBEAT_EN:
            struct.pack_into("<I", pl_u32, 0, secs & 0xFFFFFFFF)
            idx = await slab_pool.lease()
            buf = slab_pool.buf(idx)
            n = encode_into(buf, MSG_EVENT, R_TIMESTAMP_SECOND, port, PT_U32,
                            payload=pl_u32, payload_len=4,
                            ts_seconds=secs, ts_ticks=ticks)
            slab_pool.set_length(idx, n)
            await tx_queue.put(idx)

        elif op_val & OP_ALIVE_EN:
            # Deprecated path: emit R_TIMESTAMP_SECOND.
            struct.pack_into("<I", pl_u32, 0, secs & 0xFFFFFFFF)
            idx = await slab_pool.lease()
            buf = slab_pool.buf(idx)
            n = encode_into(buf, MSG_EVENT, R_TIMESTAMP_SECOND, port, PT_U32,
                            payload=pl_u32, payload_len=4,
                            ts_seconds=secs, ts_ticks=ticks)
            slab_pool.set_length(idx, n)
            await tx_queue.put(idx)
