"""Status LED task — synchronized to the Harp clock when locked.

States:
  - Synced + active   : toggle on each second-rollover (2 Hz, 50 % duty).
  - Synced + standby  : 50 ms pulse at each second-rollover (visible "still
                        synced but not streaming").
  - Unsynced          : 4 second free-running blink (asyncio.sleep_ms-based).

Wake source while synced is `clock.tick.subscribe()` — a per-subscriber
asyncio.Event the clock fires on each second.  No ThreadSafeFlag needed:
the producer (sync_task / clock.apply_sync) is itself an asyncio task.

The OperationControl register's VISUAL_EN bit gates whether the LED runs at
all; if cleared, the task simply turns the pin off and waits for it to be
re-enabled.
"""

import asyncio


from machine import Pin


from .clock import Clock
from .registers import (
    RegisterBank,
    R_OPERATION_CONTROL,
    OP_OP_MODE_MASK,
    OP_OP_MODE_ACTIVE,
    OP_VISUAL_EN,
)


async def led_task(pin: Pin, clock: Clock, bank: RegisterBank):
    """Drive `pin` (a `machine.Pin`) according to clock + OperationControl.

    `pin` must support .value(0/1) and .on()/.off().
    """
    tick_ev = clock.tick.subscribe()
    op_reg = bank.get(R_OPERATION_CONTROL)

    def visual_en() -> bool:
        return op_reg is None or (op_reg.storage[0] & OP_VISUAL_EN)

    def is_active() -> bool:
        return op_reg is not None and (op_reg.storage[0] & OP_OP_MODE_MASK) == OP_OP_MODE_ACTIVE

    state = 0
    while True:
        if not visual_en():
            pin.value(0)
            # Re-check periodically; this is a slow path.
            await asyncio.sleep_ms(100)
            continue

        if clock.synced:
            # Wait for the next second-rollover.
            await tick_ev.wait()
            tick_ev.clear()
            if not visual_en():
                pin.value(0)
                continue

            if is_active():
                # 2 Hz square wave.
                pin.value(not (clock._seconds & 0x2))
                # await asyncio.sleep_ms(2000)
            else:
                pin.value(not (clock._seconds & 0x4))
                # await asyncio.sleep_ms(4000)
        else:
            # Free-running blink — explicitly NOT clock-aligned, so the
            # visual cue for "unsynced" is unmistakable.
            state ^= 1
            pin.value(state)
            await asyncio.sleep_ms(4000)
            # If sync gets acquired during the sleep, fall through to the
            # synced branch on next iteration.
