"""Harp synchronization clock.

A Harp master broadcasts a 6-byte sync packet every second:

    0xAA 0xAF [seconds:U32 little-endian]

The first byte's arrival is taken as the second-boundary reference: the
encoded `seconds` value is the seconds counter as of that moment.

This module provides:

    Clock                : holds discipline state, exposes timestamp queries
    sync_task(uart, clk) : asyncio task that drains the sync UART and updates
                           the Clock; pairs with a hard-IRQ that captures the
                           start-of-byte timestamp.

Portability notes:
- `machine.UART.irq()` is supported on RP2/RP235x/ESP32 with slightly
  different flags. We capture the timestamp in a hard IRQ and let the task
  do all parsing — no asyncio internals are touched from the IRQ.
- If your port lacks UART IRQ on RX, fall back to a polling sync task
  (set `use_irq=False` when constructing). Latency is then bounded by the
  asyncio loop period (~1 ms) rather than IRQ latency (~tens of us).
"""

import micropython
from micropython import const
import asyncio
import sys
import time
import struct
# from typing import Any, Callable, Awaitable

if "ptr32" not in globals():
    # Fallback for static analysis and non-viper contexts.
    def ptr32(_addr):
        return (0,)


_native = getattr(micropython, "native", lambda fn: fn)

# ---------------------------------------------------------------------------
# Fast tick source.  On RP2040 / RP2350 we read the hardware TIMERAWL register
# directly via viper — this skips the MicroPython VM call overhead that
# `time.ticks_us()` carries.  Borrowed from SainsburyWellcomeCentre/microharp
# clock.py.  Falls back to `time.ticks_us()` everywhere else.
# ---------------------------------------------------------------------------

_machine_str = getattr(sys.implementation, "_machine", "")
_RP2350 = sys.platform == "rp2" and "RP2350" in _machine_str
_RP2040 = sys.platform == "rp2" and not _RP2350

# Viper's ptr32() requires a compile-time integer LITERAL — it cannot
# resolve a Python name like `_TIMER_TIMERAWL` at viper-compile time
# (you get "can't assign to expression").  So we install one of two
# platform-specific variants with the address spelled inline.
#
# 30-bit mask: keeps the return value inside MicroPython's small-int
# window (~30 bits on 32-bit ports).  Without the mask, the raw TIMERAWL
# register exceeds small-int range ~17.9 minutes after boot, at which
# point every read boxes a big-int — fatal in hard IRQ context
# (MemoryError).  The 30-bit period matches `time.ticks_us`, so
# `time.ticks_diff` / `time.ticks_add` semantics are unchanged.

if _RP2040:
    try:
        @micropython.viper
        def _ticks_us() -> int:
            return int(ptr32(0x40054028)[0]) & 0x3FFFFFFF
    except (AttributeError, NameError, SyntaxError):
        _ticks_us = time.ticks_us
elif _RP2350:
    try:
        @micropython.viper
        def _ticks_us() -> int:
            return int(ptr32(0x400B0028)[0]) & 0x3FFFFFFF
    except (AttributeError, NameError, SyntaxError):
        _ticks_us = time.ticks_us
else:
    _ticks_us = time.ticks_us

# 32 us per Harp tick → 31250 ticks per second.
US_PER_TICK = const(32)
TICKS_PER_SECOND = const(31250)

_SYNC_B0 = const(0xAA)
_SYNC_B1 = const(0xAF)

# Per the Harp Sync Clock spec, the *last* byte of the 6-byte packet is
# transmitted exactly 672 µs before the next whole-second boundary at
# 100 kbaud.  We approximate the start-of-first-byte reference point by
# subtracting the packet's own duration from that — at 100 kbaud, 6 bytes
# × 10 bits = 600 µs.  So the first byte starts ~ (672 + 600) µs before
# the next-second boundary, i.e. ~272 µs after the *previous* second.
# When polling, our t_first observation lands somewhere in that window.
# Constant offset shifts the epoch back so that secs[N] aligns with the
# physical second boundary the master broadcasted.
SYNC_OFFSET_US = const(672)


class TickBroadcaster:
    """Per-subscriber asyncio.Event broadcaster.

    Single producer fires `fire()` on every second rollover; each subscriber
    waits on its own Event so a slow consumer can't block another.
    """

    __slots__ = ("_subs",)

    def __init__(self):
        self._subs = []

    def subscribe(self):
        ev = asyncio.Event()
        self._subs.append(ev)
        return ev

    def fire(self):
        for ev in self._subs:
            ev.set()


class Clock:
    """Holds sync state and provides timestamp queries.

    All public methods are non-blocking and allocation-free in steady state.

    Threading:
        `apply_sync()` is called only from the asyncio sync_task.
        `now()` / `now_into()` may be called from any asyncio task.
        `_arm_irq_capture()` exposes a buffer the hard IRQ writes to.
    """

    __slots__ = (
        "_seconds",  # seconds value at _epoch_us
        "_epoch_us",  # ticks_us() at the start of `_seconds`
        "_synced",
        "_last_sync_us",
        "_unsync_timeout_us",
        "tick",  # TickBroadcaster fired on second rollover
        "synced_event",  # set/cleared as sync gained/lost
        # Hard-IRQ scratch + wake:
        "_irq_buf",  # array.array('I', [t_first_byte_us, packet_seq])
        "sync_flag",  # ThreadSafeFlag set by sync IRQ (wakes sync_task)
        # Per-tick timestamp snapshot (for heartbeat / LED / any periodic
        # consumer that needs the second-boundary timestamp without
        # picking up asyncio wake jitter).  See apply_sync / second_ticker.
        "_tick_secs",  # array.array('I', [secs])  — captured at tick-fire time
    )

    def __init__(self, unsync_timeout_ms: int = 1500):
        import array

        self._seconds = 0
        self._epoch_us = _ticks_us()
        self._synced = False
        self._last_sync_us = 0
        self._unsync_timeout_us = unsync_timeout_ms * 1000
        self.tick = TickBroadcaster()
        self.synced_event = asyncio.Event()
        # Slot 0: ticks_us at most recent first-byte arrival.
        # Slot 1: monotonic packet counter (lets sync_task detect new arrivals).
        self._irq_buf = array.array("I", [0, 0])
        # IRQ→task wake.  ThreadSafeFlag (not asyncio.Event) because the
        # producer is a hard IRQ — Event.set() would corrupt the asyncio
        # scheduler's wait list when called from that context.
        self.sync_flag = asyncio.ThreadSafeFlag()
        # Tick-time snapshot (see __slots__ comment).
        self._tick_secs = array.array("I", [0])

    # -- queries -------------------------------------------------------------

    @property
    def synced(self):
        return self._synced

    def now(self):
        """Return (seconds, ticks). Allocates a tuple — for hot paths use
        `now_into` to write into a pre-allocated bytearray instead."""
        return self._compute()

    def now_into(self, out: bytearray, offset: int = 0):
        """Write 6 bytes (U32 seconds LE, U16 ticks LE) into `out[offset:]`.
        `out` must have at least `offset + 6` bytes.  No allocation."""
        secs, ticks = self._compute()
        struct.pack_into("<IH", out, offset, secs & 0xFFFFFFFF, ticks & 0xFFFF)

    @_native
    def now_fast(self, arr: list):
        """Write current (secs, ticks) into arr[0] / arr[1].

        `arr` must be a pre-allocated 2-element list.  Uses list slots so
        that secs is stored as a *reference* to the existing self._seconds
        object — no new heap allocation even when seconds > small-int range.
        Eliminates the 2-tuple that clock.now() creates on every call.
        """
        delta_us = time.ticks_diff(_ticks_us(), self._epoch_us)
        if delta_us < 0:
            delta_us = 0
        if delta_us >= 1_000_000:
            extra = delta_us // 1_000_000
            rem = delta_us - extra * 1_000_000
            arr[0] = (self._seconds + extra) & 0xFFFFFFFF
            arr[1] = rem // US_PER_TICK
        else:
            arr[0] = self._seconds
            arr[1] = delta_us // US_PER_TICK

    @_native
    def _compute(self):
        # `time.ticks_diff` and the fast `_ticks_us()` are C-implemented;
        # the Python-level cost here is the arithmetic and tuple return.
        # Replacing divmod with two ops avoids one bytecode and one
        # tuple-unpack on the hot fast-path branch.
        delta_us = time.ticks_diff(_ticks_us(), self._epoch_us)
        if delta_us < 0:
            delta_us = 0
        if delta_us >= 1_000_000:
            extra = delta_us // 1_000_000
            rem = delta_us - extra * 1_000_000
            return (self._seconds + extra) & 0xFFFFFFFF, rem // US_PER_TICK
        return self._seconds, delta_us // US_PER_TICK

    # -- IRQ-side hook -------------------------------------------------------

    def irq_record_first_byte(self):
        """Hard-IRQ-safe.  Stamp first-byte arrival time and bump the seq.

        The handler does *only* this: no UART reads, no parsing, no
        ThreadSafeFlag.set() (the sync_task polls the seq counter).

        The seq mask is 0xFFFF (16 bits) — large enough that the consumer
        will never confuse old vs. new in practice, and small enough that
        the increment + mask stays inside MicroPython small-int range
        (no big-int allocation in IRQ context).
        """
        self._irq_buf[0] = _ticks_us()
        self._irq_buf[1] = (self._irq_buf[1] + 1) & 0xFFFF

    def irq_seq(self):
        return self._irq_buf[1]

    def irq_first_byte_us(self):
        return self._irq_buf[0]

    # -- Sync-task side ------------------------------------------------------

    def apply_sync(self, t_first_byte_us: int, encoded_seconds: int, *,
                   latency_us: int = 0):
        """Discipline the clock to a freshly-arrived sync packet.

        `t_first_byte_us` was captured by the IRQ at first-byte arrival
        (or by the polling fallback at task wake).  Pass an additional
        `latency_us` to shift the epoch backward by a known constant
        offset — useful when measurements show e.g. ~100 µs of consistent
        delay between physical byte arrival and IRQ entry.
        """
        self._seconds = encoded_seconds & 0xFFFFFFFF
        self._epoch_us = time.ticks_add(t_first_byte_us,
                                        -(SYNC_OFFSET_US + latency_us))
        self._last_sync_us = _ticks_us()
        if not self._synced:
            self._synced = True
            self.synced_event.set()
        # Snapshot the second value as of this boundary BEFORE firing the
        # tick — heartbeat / LED read this snapshot at their own pace and
        # therefore see the boundary timestamp regardless of asyncio wake
        # jitter (which can spike to hundreds of ms under USB-drain back-
        # pressure, dispatcher DUMPs, etc.).  Fire the broadcaster.  Wake
        # arrives at consumers within one scheduler iteration; the value
        # they read is the value captured at *this* line.
        self._tick_secs[0] = self._seconds
        self.tick.fire()

    def set_seconds(self, seconds: int, *, anchor_us = None, latency_us: int = 0):
        """Re-anchor the clock so that at `anchor_us` (default: now), the
        seconds counter equals `seconds`.  Used by writes to the
        TimestampSecond register.  `latency_us` lets you compensate for
        the known constant latency between request-processing and this
        call (e.g. ~100 µs of dispatch + handler overhead) — the anchor
        is shifted back by that amount so subsequent `now()` queries
        return values aligned with the caller's intended moment.
        """
        if anchor_us is None:
            anchor_us = _ticks_us()
        self._seconds = seconds & 0xFFFFFFFF
        self._epoch_us = time.ticks_add(anchor_us, -latency_us)

    def check_sync_loss(self):
        """Returns True if the synced flag transitioned to False."""
        if not self._synced:
            return False
        if time.ticks_diff(_ticks_us(), self._last_sync_us) > self._unsync_timeout_us:
            self._synced = False
            self.synced_event.clear()
            return True
        return False


# ---------------------------------------------------------------------------
# sync_task — IRQ-driven where the port supports it
# ---------------------------------------------------------------------------

# Sync packet duration at 100 kbaud (6 bytes × 10 bits / 100 000 bps = 600 µs).
# Used to backdate from end-of-burst (RXIDLE) to start-of-first-byte.
_SYNC_PACKET_US = const(600)


async def sync_task(uart, clock: Clock, *, baudrate: int = 100_000):
    """Drain a UART carrying Harp sync packets and update `clock`.

    Two paths:

    * **IRQ-driven** (RP2 and any port exposing `UART.IRQ_RXIDLE`).
      A hard IRQ fires once per packet at end-of-burst.  The handler
      back-dates `_ticks_us()` by the packet duration to recover the
      first-byte arrival time and stores it in `clock._irq_buf`.  Jitter
      is bounded by IRQ entry latency (single-digit µs on RP2040), well
      below the 32 µs Harp tick.

    * **Polling fallback.**  `t_first` is captured at the moment the
      task observes `uart.any()`.  Wake jitter ≈ asyncio loop period
      (~1 ms = ~31 ticks).  Manifests as a constant clock offset on the
      master timeline; host-side calibration absorbs it.

    `baudrate` is used only to compute the back-dated offset for the IRQ
    path, in case you run the sync line at a non-standard rate.
    """
    buf = bytearray(11)  # 6 bytes expected; over-sized is fine
    mv = memoryview(buf)

    # Bytes-per-second × 10 bits each ⇒ µs/byte = 10_000_000 / baudrate.
    # Total packet duration in µs:
    packet_us = (10 * 6 * 1_000_000) // baudrate

    # ---- Try to install IRQ_RXIDLE handler -------------------------------
    irq_installed = False
    if hasattr(uart, "IRQ_RXIDLE"):
        try:

            # Pre-bind every callable used in the IRQ body so the handler
            # body performs no attribute lookups — protects against
            # bound-method allocation on MicroPython builds without the
            # LOAD_METHOD optimization (fatal in hard IRQ).
            _buf      = clock._irq_buf
            _ticks    = _ticks_us
            _add      = time.ticks_add
            _flag_set = clock.sync_flag.set

            def _idle_irq(u, _b=_buf, _t=_ticks, _a=_add,
                          _pus=packet_us, _s=_flag_set):
                # End-of-burst → back-date to start of first byte.  Mask
                # is 0xFFFF (small-int safe, no IRQ allocation).
                _b[0] = _a(_t(), -_pus)
                _b[1] = (_b[1] + 1) & 0xFFFF
                _s()

            uart.irq(handler=_idle_irq, trigger=uart.IRQ_RXIDLE, hard=True)
            irq_installed = True
        except (TypeError, OSError):
            pass  # port refused hard=True or this trigger; fall through

    # Polling fallback uses StreamReader so the asyncio loop wakes the
    # task as soon as the UART has bytes — no fixed 1 ms latency.
    sreader = None
    sreader_readinto = None
    if not irq_installed:
        try:
            sreader = asyncio.StreamReader(uart)
            sreader_readinto = getattr(sreader, "readinto", None)
            if not callable(sreader_readinto):
                sreader = None
                sreader_readinto = None
        except (TypeError, AttributeError):
            sreader = None  # port doesn't support stream wrapping

    while True:
        # ---- Wait for a packet --------------------------------------------
        if irq_installed:
            # Auto-clearing wait — wakes within one scheduler iteration of
            # the IRQ.  Timestamp was already captured *inside* the IRQ.
            await clock.sync_flag.wait()
            t_first = clock.irq_first_byte_us()
        elif sreader_readinto is not None:
            # Stream-based wait: loop wakes as soon as bytes arrive.
            await sreader_readinto(mv[:1])
            t_first = _ticks_us()
        else:
            # Last-resort polling — kept for ports without stream support.
            while not uart.any():
                await asyncio.sleep_ms(1)
            t_first = _ticks_us()

        # ---- Read the rest of the packet ----------------------------------
        if irq_installed:
            # IRQ already fired at end-of-burst — full packet is in the FIFO.
            got = 0
            while got < 6:
                n = uart.readinto(mv[got:6])
                if n:
                    got += n
                else:
                    break  
            if got < 6:
                continue
        elif sreader_readinto is not None:
            await sreader_readinto(mv[1:6])
        else:
            deadline = time.ticks_add(_ticks_us(), 5000)
            got = 0
            while got < 6 and time.ticks_diff(deadline, _ticks_us()) > 0:
                n = uart.readinto(mv[got:6])
                if n:
                    got += n
                else:
                    await asyncio.sleep_ms(0)
            if got < 6:
                continue

        # if buf[0] != _SYNC_B0 or buf[1] != _SYNC_B1:
        #     # Bad header — flush and resync.
        #     while uart.any():
        #         uart.read(1)
        #     continue

        secs = buf[3] | (buf[4] << 8) | (buf[5] << 16) | (buf[0] << 24)

        clock.apply_sync(t_first, secs, latency_us=1160)


# ---------------------------------------------------------------------------
# second_ticker — fires `clock.tick` on every second-boundary crossing,
# whether the device is synced or running on its free-running local clock.
# This is what gives the heartbeat task and LED a 1 Hz wake source even
# when no sync master is attached (Device.md §R_OPERATION_CTRL says
# HEARTBEAT_EN MUST emit every second).
# ---------------------------------------------------------------------------

async def second_ticker(clock: Clock, *, poll_ms: int = 20):
    """Watch `clock.now()[0]` for changes; fire `clock.tick` on each new
    second when no sync master is attached.

    When `clock.synced` is True, the sync IRQ → `apply_sync` path owns
    tick firing (with µs-level boundary precision).  This task then just
    keeps `last` aligned so we don't fire spuriously when sync drops.

    When unsynced, this task sleeps until close to the next boundary,
    then polls at `poll_ms`, so heartbeat/LED phase still lags the
    local-clock boundary by at most that many ms while using far fewer
    wakeups than a fixed-rate loop.
    """
    last = clock.now()[0]
    # While synced, `apply_sync` drives second ticks.  We only need a
    # low-rate maintenance loop to keep `last` aligned and detect sync loss.
    synced_check_ms = 200
    while True:
        clock.check_sync_loss()

        if clock.synced:
            # apply_sync owns the tick path while synced.
            last = clock.now()[0]
            await asyncio.sleep_ms(synced_check_ms)
            continue

        s, ticks = clock.now()
        if s != last:
            last = s
            # Snapshot before firing so consumers see this boundary's
            # value regardless of when they wake up (see apply_sync
            # for the synced-path version of this comment).
            clock._tick_secs[0] = s
            clock.tick.fire()

        # Adaptive schedule: sleep until we enter a `poll_ms` guard window
        # before the next second, then poll at `poll_ms` cadence.
        ticks_to_next = (TICKS_PER_SECOND - 1) - ticks
        ms_to_next = (ticks_to_next * US_PER_TICK) // 1000
        if ms_to_next > poll_ms:
            sleep_ms = ms_to_next - poll_ms
        else:
            sleep_ms = poll_ms
        await asyncio.sleep_ms(sleep_ms)
