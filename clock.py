"""
Harp timestamp clock class.
"""

import sys
import micropython
from micropython import const

if 'ptr32' not in globals():
    # Fallback for static analysis and non-viper contexts.
    def ptr32(_addr):
        return (0,)

# Platform detection at import time — selects fastest available tick source.
# sys.implementation._machine contains e.g. 'RP2040 with 264kB RAM' or 'RP2350 with 520kB RAM'.
_machine = getattr(sys.implementation, '_machine', '')
_RP2350 = sys.platform == 'rp2' and 'RP2350' in _machine
_RP2040 = sys.platform == 'rp2' and not _RP2350
_USE_HW_TIMER = _RP2040 or _RP2350

if _RP2040:
    _TIMER_TIMERAWL = 0x40054028  # RP2040 TIMER base 0x40054000 + TIMERAWL offset 0x028
elif _RP2350:
    _TIMER_TIMERAWL = 0x400B0028  # RP2350 TIMER0 base 0x400B0000 + TIMERAWL offset 0x028
else:
    import time


class HarpClock:
    """
    Clock synchronized to the Harp Synchronization Clock protocol.

    Packet format: [0xAA, 0xAF, U32 LE] (6 bytes, 100 kbps).
    The U32 payload encodes the *previous* whole second (little-endian). The last byte
    is transmitted exactly 672 μs before the next whole second boundary, so at the
    moment of reception the sub-second part of the Harp clock is 999_328 μs
    (= 1_000_000 - 672).

    Anchor model (no overflow counter needed — safe as long as sync arrives at least
    once every ~17.9 min, the ticks_us rollover period):
        abs_us = anchor_s * 1_000_000 + anchor_sub_us + (ticks_us - anchor_ticks)
    Seconds and sub-seconds are kept separate to avoid 64-bit multiplication.
    """

    # Per spec: last byte is transmitted 672 μs before the next whole second.
    SYNC_OFFSET_US = const(672)
    # RP2 viper _read() execution latency compensation (measured ~80 μs).
    READ_COMP_US = const(100)
    # RP2 viper _write() invocation latency compensation (measured ~130 μs).
    WRITE_COMP_US = const(130)

    def __init__(self):
        self._anchor_ticks: int = 0    # ticks captured at last sync reception
        self._anchor_s: int = 0        # whole seconds at anchor
        self._anchor_sub_us: int = 0   # sub-second microseconds at anchor
        self.timestamp_s: int = 0
        self.timestamp_us: int = 0

    def read(self):
        """Return current Harp timestamp as (seconds, microseconds/32)."""
        return _read(self)

    def write(self, sec):
        """Update clock anchor from a uint32 second value."""
        if not isinstance(sec, int):
            raise TypeError("sec must be an integer uint32")
        _write(self, sec)


# ---------------------------------------------------------------------------
# Platform-specific tick source. Read/write math is shared across platforms.
# ---------------------------------------------------------------------------

if _USE_HW_TIMER:
    @micropython.viper
    def _ticks_us() -> int:
        return ptr32(_TIMER_TIMERAWL)[0]
else:
    def _ticks_us():
        return time.ticks_us()


def _read(self) -> tuple[int, int]:
    """Return (seconds, microseconds/32) Harp timestamp."""
    t = _ticks_us()
    sub_us = t - self._anchor_ticks - 10  # elapsed minus _read() latency
    bonus = sub_us // 1_000_000
    s = self._anchor_s + bonus
    us = (sub_us - bonus * 1_000_000) >> 5
    self.timestamp_s = s
    self.timestamp_us = us
    return (s, us)


def _write(self, sec: int):
    """Process a uint32 Harp sync second."""
    self._anchor_s = sec
    self._anchor_sub_us = 0   # = 1_000_000 - SYNC_OFFSET_US (672)
    self._anchor_ticks = _ticks_us()

