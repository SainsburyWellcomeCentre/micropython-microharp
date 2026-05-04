"""HarpDevice — top-level orchestrator with high-level helpers.

Replaces the boilerplate of wiring up Clock, RegisterBank, SlabPool,
queues, transport, dispatcher, sync/LED/heartbeat tasks, and event
sources.  Application code becomes:

    from microharp import HarpDevice, CdcTransport, PT_U8, READ_ONLY, EVENT
    from machine import Pin, UART
    from usb.device.cdc import CDCInterface
    import usb.device

    cdc = CDCInterface(baudrate=1_000_000, timeout=0, txbuf=2048, rxbuf=512)
    usb.device.get().init(cdc, builtin_driver=True)

    device = HarpDevice(
        transport   = CdcTransport(cdc),
        sync_uart   = UART(1, baudrate=100_000, rx=Pin(5)),
        led_pin     = Pin(25, Pin.OUT),
        who_am_i    = 1234,
        device_name = b"my-harp",
    )

    @device.on_read(address=32, payload_type=PT_U8, name="DigitalInput")
    async def read_di(reg):
        reg.storage[0] = button.value()

    device.bind_pin_event(button, address=32, payload_type=PT_U8,
                          trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING)

    @device.task
    async def my_extra_loop():
        while True:
            await asyncio.sleep(1)
            ...

    asyncio.run(device.run())
"""

import asyncio
import time
# from typing import Any, Callable, Coroutine

try:
    from asyncio import Queue  # available on CPython and MicroPython ≥1.20 with full asyncio
except (ImportError, AttributeError):
    from .queue import Queue  # fallback for bare RP2 / stripped MicroPython builds

from .clock import Clock, sync_task, second_ticker
from .registers import (
    RegisterEntry,
    RegisterBank,
    READ_ONLY,
    WRITE_ONLY,
    READ_WRITE,
    EVENT,
    install_common_registers,
)
from .framing import FrameDecoder
from .transport import (
    StreamTransport,
    SlabPool,
    usb_rx_task,
    usb_tx_task,
    DEFAULT_SLAB_COUNT,
    DEFAULT_SLAB_SIZE,
)
from .dispatch import Dispatcher, dispatch_task
from .events import EventSource
from .led import led_task
from .heartbeat import heartbeat_task

from machine import Pin, UART

DEVICE_NAME_LEN = 25  # per spec; includes null terminator

# Sentinel used by bind_pin_event(level_state=...) and bind_state_register(mapping=...)
# to distinguish "this key is absent → no transition" from "this key maps to None →
# transition to the idle state".  Module-private; users never see it.
_NO_TRANSITION = object()

class HarpDevice:
    """Assembles all components and runs them.

    Parameters
    ----------
    transport :
        Any object exposing `read_some(buf) -> int` and `write(mv) -> None`
        (CdcTransport, UartTransport, or your own).
    sync_uart : machine.UART | None
        UART receiving 100 kbaud Harp sync packets.  None disables sync.
    led_pin : machine.Pin | None
        Status LED.  None disables the LED task.
    who_am_i, fw_version, hw_version, device_name, serial_number :
        Identity broadcast in the standard registers.
    slab_count, slab_size, rx_q_size, tx_q_size :
        Resource sizing for the slab pool and queues.
    """

    def __init__(
        self,
        transport: StreamTransport,
        *,
        sync_uart: UART,
        led_pin: Pin,
        who_am_i: int = 0,
        fw_version: tuple = (1, 0),
        hw_version: tuple = (1, 0),
        device_name: bytes = b"harp-mpy",
        serial_number: int = 0,
        slab_count: int = DEFAULT_SLAB_COUNT,
        slab_size: int = DEFAULT_SLAB_SIZE,
        rx_q_size: int = 8,
        tx_q_size: int = 16
    ):

        self.clock = Clock()
        self.bank = RegisterBank()
        self.slabs = SlabPool(slab_count, slab_size)
        self.rx_q = Queue(rx_q_size)
        self.tx_q = Queue(tx_q_size)
        self.transport = transport
        self.sync_uart = sync_uart
        self.led_pin = led_pin
        self.decoder = FrameDecoder(max_payload=slab_size - 12)

        install_common_registers(
            self.bank,
            who_am_i=who_am_i,
            fw_major=fw_version[0],
            fw_minor=fw_version[1],
            hw_major=hw_version[0],
            hw_minor=hw_version[1],
            device_name=device_name,
            serial_number=serial_number,
            clock=self.clock,
            tx_q=self.tx_q,
            slab_pool=self.slabs,
        )
        self.dispatcher = Dispatcher(self.bank, self.clock, self.rx_q, self.tx_q, self.slabs)

        self._user_tasks = []  # list of coroutines or factories
        self._event_sources = []  # list of EventSource instances

        # If the transport is a CdcTransport that exposes set_line_state_cb,
        # wire it up so the device drops to STANDBY on DTR-high.  This
        # prevents the host from receiving stale events from a previous
        # session — pattern adopted from microharp v2.
        try:
            cdc_iface = getattr(transport, "_s", None)
            if cdc_iface is not None and hasattr(cdc_iface, "set_line_state_cb"):
                cdc_iface.set_line_state_cb(self._line_state_cb)
        except Exception:
            pass

    def _line_state_cb(self, line_state: int):
        """Called by the CDC stack on DTR/RTS state change.

        DTR-high (bit 0) means the host has just opened the port.  Clear
        OP_MODE to STANDBY and disable heartbeat — the host will write
        OperationControl explicitly when it wants events.
        """
        if line_state & 0x01:
            from .registers import R_OPERATION_CONTROL, OP_OP_MODE_MASK

            op = self.bank.get(R_OPERATION_CONTROL)
            if op is not None:
                op.storage[0] &= ~OP_OP_MODE_MASK  # → STANDBY

    # ------------------------------------------------------------------
    # Register HOFs
    # ------------------------------------------------------------------

    def add_register(
        self, address: int, payload_type: int, *, n_elements: int = 1, access: int = READ_WRITE, on_read=None, on_write=None, name: str = ""
    ):
        """Create and register a RegisterEntry; return it."""
        reg = RegisterEntry(address, payload_type, n_elements, access, on_read=on_read, on_write=on_write, name=name)
        self.bank.add(reg)
        return reg

    def set_name(self, name: str):
        """Set the device name (max 24 chars, null-padded to 25 bytes)."""
        encoded = name.encode("ascii")[:DEVICE_NAME_LEN - 1]
        padded = encoded + bytes(DEVICE_NAME_LEN - len(encoded))
        return tuple(padded)

    # Typed shortcuts (just sugar over add_register with PT_* preset).
    def add_u8(self, address: int, **kw):
        from .framing import PT_U8

        return self.add_register(address, PT_U8, **kw)

    def add_u16(self, address: int, **kw):
        from .framing import PT_U16

        return self.add_register(address, PT_U16, **kw)

    def add_u32(self, address: int, **kw):
        from .framing import PT_U32

        return self.add_register(address, PT_U32, **kw)

    def add_s16(self, address: int, **kw):
        from .framing import PT_S16

        return self.add_register(address, PT_S16, **kw)

    def add_float(self, address: int, **kw):
        from .framing import PT_FLOAT

        return self.add_register(address, PT_FLOAT, **kw)

    def on_read(self, address: int, payload_type: int, *, n_elements: int = 1, name: str = ""):
        """Decorator: declare an async read handler.

        Creates the register if it doesn't exist (READ_ONLY by default);
        otherwise just attaches the handler::

            @device.on_read(address=32, payload_type=PT_U8)
            async def read_di(reg):
                reg.storage[0] = pin.value()
        """

        def deco(fn):
            reg = self.bank.get(address)
            if reg is None:
                if payload_type is None:
                    raise ValueError("payload_type required for new register")
                reg = self.add_register(address, payload_type, n_elements=n_elements, access=READ_ONLY, name=name)
            else:
                reg.access |= READ_ONLY
            reg.on_read = fn
            return fn

        return deco

    def on_write(self, address: int, payload_type: int, *, n_elements: int = 1, name: str = ""):
        """Decorator: declare an async write handler.

        The handler signature is `async def(reg, payload_mv) -> err_or_None`.
        Returning a non-None error number sends a WRITE_ERROR reply.
        """

        def deco(fn):
            reg = self.bank.get(address)
            if reg is None:
                if payload_type is None:
                    raise ValueError("payload_type required for new register")
                reg = self.add_register(address, payload_type, n_elements=n_elements, access=WRITE_ONLY, name=name)
            else:
                reg.access |= WRITE_ONLY
            reg.on_write = fn
            return fn

        return deco

    # ------------------------------------------------------------------
    # Event source HOFs
    # ------------------------------------------------------------------

    def add_event_source(self, address: int, payload_type: int, *, port: int = 255, ring_size: int = 16, pack=None):
        """Create + return an EventSource for an application register.

        `pack(payload_word, scratch) -> n_bytes` controls how the IRQ's
        32-bit word becomes the on-wire payload.  Default packs as U8.
        """
        # If no register exists, create one as READ_ONLY|EVENT.
        if self.bank.get(address) is None:
            self.add_register(address, payload_type, access=READ_ONLY | EVENT, name="event_%d" % address)

        # Pass `bank` so the source can self-gate on Active mode (Device.md
        # §Operation Mode: events MUST NOT be sent in Standby).
        src = EventSource(
            address, payload_type, self.clock, self.tx_q, self.slabs,
            port=port, ring_size=ring_size, pack=pack, bank=self.bank,
        )
        self._event_sources.append(src)
        return src

    def add_periodic_event(self, address: int, payload_type: int, period_ms: int, *, pack=None, port: int = 255):
        """Emit an EVENT for `address` every `period_ms` ms.

        Implemented as a drift-corrected asyncio task — no `machine.Timer`
        is consumed, and the body runs in normal task context (so you
        could swap in any logic you like by setting a custom `pack`).
        Wake jitter is bounded by the asyncio loop period (~1 ms typical,
        well below Harp's 32 µs tick where it matters).  Timestamp
        accuracy is preserved because `EventSource.emit()` captures
        `_ticks_us()` *inside the call*, not at the scheduler wake.

        For sub-millisecond precision (kHz sampling, encoder polling,
        etc.) construct your own `machine.Timer` and call the returned
        EventSource's `.emit(payload_word)` from its callback.

        Returns the EventSource.
        """
        src = self.add_event_source(address, payload_type, port=port, pack=pack)

        async def _periodic(_src=src, _bank=self.bank, _addr=address, _period_ms=period_ms):
            # Drift-corrected: aim for an absolute deadline that advances
            # by exactly `_period_ms` each iteration, regardless of how
            # long the work took.
            deadline = time.ticks_add(time.ticks_ms(), _period_ms)
            while True:
                reg = _bank.get(_addr)
                if reg is not None:
                    _src.emit(reg.storage[0])
                wait = time.ticks_diff(deadline, time.ticks_ms())
                if wait > 0:
                    await asyncio.sleep_ms(wait)
                else:
                    # Fell behind by more than a period — resync the
                    # deadline so we don't spin emitting back-to-back.
                    deadline = time.ticks_ms()
                deadline = time.ticks_add(deadline, _period_ms)

        self._user_tasks.append(_periodic)
        return src

    def bind_pin_event(
        self, pin: Pin, *, address=None, payload_type=None, trigger: int, hard: bool = True, port: int = 255, name: str = "", on_read=None, on_irq=None, task=None, active_level: int = 0, sequence=None, level_state=None
    ):
        """Wire a `machine.Pin` to a Harp register: read + event in one call.

        This single call does everything an input-pin register needs:

          1. Creates the register at `address` (READ_ONLY | EVENT) if it
             doesn't already exist, with the given `name`.
          2. Mirrors the current pin level into the register's storage so
             READ requests return a sensible value immediately.
          3. Installs an `on_read` handler that refreshes storage from
             `pin.value()` on every host READ — unless you pass a custom
             `on_read` (or had one declared on the register already, e.g.
             via `@device.on_read`), in which case yours wins.
          4. Installs a hard IRQ on `trigger` that captures the pin level
             at the IRQ moment and queues a Harp EVENT with that exact
             timestamp.  Falls back to soft IRQ on ports without `hard=`.

        Example::

            device.bind_pin_event(button,
                                  address=32, payload_type=PT_U8,
                                  trigger=Pin.IRQ_RISING|Pin.IRQ_FALLING,
                                  name="DigitalInput")

        Optional `on_irq(level)` is invoked from the same hard IRQ after
        the Harp event is emitted, with the captured pin level (0/1) the
        EVENT will carry.  Runs in **hard IRQ context** — it MUST be
        allocation-free and MUST NOT touch asyncio internals.

        Optional `task` is a coroutine factory (callable returning a
        coroutine).  When the pin transitions to `active_level`, the
        framework starts a new task from `task()`; on the opposite
        transition it cancels that task.  Create/cancel happen via
        `micropython.schedule` — the IRQ stays allocation-free and no
        always-on controller task is added.  `active_level` defaults to
        0 (active-low, matches `Pin.PULL_UP`).  Example::

            async def worker():
                while True:
                    print("running")
                    await asyncio.sleep_ms(200)

            device.bind_pin_event(button, ..., task=worker)

        Optional `sequence` (a `TaskSequence`) drives a multi-state
        machine from the same IRQ.  Requires `level_state`, a dict
        mapping pin level (0/1) to state name::

            sequence = device.add_task_sequence(states={"A": taskA, "B": taskB})
            device.bind_pin_event(button, ...,
                                  sequence=sequence,
                                  level_state={0: "A", 1: "B"})

        `task=` and `sequence=` are mutually exclusive.

        Pass `address=None` to skip the register and event-emission setup —
        the pin then acts as a pure trigger (use `on_irq=`, `task=`, or
        `sequence=` to react to edges) and no Harp EVENT is sent on each
        edge.

        Returns the underlying EventSource (or None when address=None).
        """
        from .framing import PT_U8

        if payload_type is None:
            payload_type = PT_U8

        # When address is None, skip register creation and event emission
        # — the pin becomes a pure IRQ trigger.  Otherwise, create the
        # source (and the register if needed).
        if address is not None:
            existing = self.bank.get(address)
            src = self.add_event_source(address, payload_type, port=port)
            reg = self.bank.get(address)
            if existing is None and name and reg is not None:
                reg.name = name

            # Mirror current level into storage so reads return current state.
            try:
                if reg is not None:
                    reg.storage[0] = pin.value()
            except Exception:
                pass

            # Install on_read.  Caller's explicit on_read wins; otherwise, if
            # nothing was set yet, install a refresh-from-pin reader.
            if on_read is not None and reg is not None:
                reg.on_read = on_read
            elif reg is not None and reg.on_read is None:

                async def _refresh(r, _p=pin):
                    r.storage[0] = _p.value()

                reg.on_read = _refresh

            _emit = src.emit         # bound method, created once here
        else:
            src = None
            _emit = None             # no event emission on this pin

        # Hard IRQ — every call goes through pre-bound default args so
        # the body performs no attribute lookups (and therefore no
        # bound-method allocation, which is fatal in hard IRQ context on
        # MicroPython builds without LOAD_METHOD optimization).
        _pin_value  = pin.value      # bound method, created once here

        # If `task=` was given, build a `_task_hook(level)` that schedules
        # the create/cancel via micropython.schedule.  asyncio.create_task
        # and Task.cancel allocate / touch the run queue and so cannot run
        # in hard IRQ context — micropython.schedule defers them to the
        # next safe main-loop point (no extra always-on task needed).
        # `sequence=` + `level_state=` is the multi-state alternative; it
        # routes each IRQ to sequence(level_state.get(level)).  The
        # sequence's __call__ is itself a single pre-bound schedule call.
        if task is not None and sequence is not None:
            raise ValueError("pass at most one of task=, sequence=")
        _task_hook = None
        if sequence is not None:
            if level_state is None:
                raise ValueError("sequence= requires level_state= mapping")
            _lc = sequence
            _ls = level_state

            def _task_hook(level, _l=_lc, _ls=_ls, _nt=_NO_TRANSITION):
                s = _ls.get(level, _nt)
                if s is not _nt:
                    _l(s)
        elif task is not None:
            import micropython
            _factory = task
            _active = active_level
            _state = [None]              # 1-cell mutable holder for the live Task
            _create_task = asyncio.create_task

            def _sequence_cb(level, _f=_factory, _a=_active, _st=_state, _ct=_create_task):
                cur = _st[0]
                if level == _a:
                    if cur is None or cur.done():
                        _st[0] = _ct(_f())
                else:
                    if cur is not None and not cur.done():
                        cur.cancel()
                    _st[0] = None

            _schedule = micropython.schedule

            def _task_hook(level, _s=_schedule, _lc=_sequence_cb):
                _s(_lc, level)

        # Compose the final IRQ handler with up to 2 hooks (on_irq + _task_hook).
        # Hand-roll each arity so the no-hook and 1-hook paths stay byte-identical
        # to before — extra branches in the IRQ body would defeat the purpose of
        # pre-binding the calls.  When `_emit is None` (address=None case), the
        # Harp-event emission is skipped entirely.
        if _emit is not None:
            if on_irq is None and _task_hook is None:
                def _irq_handler(p, _e=_emit, _v=_pin_value):
                    _e(_v())
            elif on_irq is not None and _task_hook is None:
                def _irq_handler(p, _e=_emit, _v=_pin_value, _h=on_irq):
                    v = _v()
                    _e(v)
                    _h(v)
            elif on_irq is None and _task_hook is not None:
                def _irq_handler(p, _e=_emit, _v=_pin_value, _h=_task_hook):
                    v = _v()
                    _e(v)
                    _h(v)
            else:
                def _irq_handler(p, _e=_emit, _v=_pin_value, _h1=on_irq, _h2=_task_hook):
                    v = _v()
                    _e(v)
                    _h1(v)
                    _h2(v)
        else:
            # No event emission (address=None).  At least one of on_irq /
            # _task_hook must be present for the pin to do anything.
            if on_irq is None and _task_hook is None:
                raise ValueError("address=None requires on_irq, task, or sequence")
            if on_irq is not None and _task_hook is None:
                def _irq_handler(p, _v=_pin_value, _h=on_irq):
                    _h(_v())
            elif on_irq is None and _task_hook is not None:
                def _irq_handler(p, _v=_pin_value, _h=_task_hook):
                    _h(_v())
            else:
                def _irq_handler(p, _v=_pin_value, _h1=on_irq, _h2=_task_hook):
                    v = _v()
                    _h1(v)
                    _h2(v)

        kw = {"handler": _irq_handler, "trigger": trigger}
        try:
            pin.irq(hard=hard, **kw)
        except TypeError:
            pin.irq(**kw)
        return src

    # ------------------------------------------------------------------
    # Manual emission helpers
    # ------------------------------------------------------------------

    async def emit(self, address: int, payload, payload_type: int = None):
        """Push an ad-hoc EVENT message for `address`.

        `payload` may be:
          - a bytes-like (`bytes` / `bytearray` / `memoryview`) — used as-is
          - an int / float — packed per `payload_type` (or the register's
            declared type if `payload_type` is None)
          - a list / tuple of scalars — packed as N elements of that type

        Use when you don't have an IRQ source — e.g. emitting from a
        polling task or in response to a subsystem callback.  Timestamp
        is captured at the moment of this call (not at IRQ).
        """
        from .framing import MSG_EVENT, encode_into

        reg = self.bank.get(address)
        if reg is None:
            raise ValueError("no register at %d" % address)
        pt = payload_type if payload_type is not None else reg.payload_type

        if isinstance(payload, (bytes, bytearray, memoryview)):
            buf_payload = payload
        else:
            from .framing import _pack_value
            buf_payload = _pack_value(payload, pt)

        secs, ticks = self.clock.now()
        idx = await self.slabs.lease()
        buf = self.slabs.buf(idx)
        n = encode_into(
            buf, MSG_EVENT, address, 255, pt, payload=buf_payload, payload_len=len(buf_payload), ts_seconds=secs, ts_ticks=ticks
        )
        self.slabs.set_length(idx, n)
        await self.tx_q.put(idx)

    def bind_event_register(self, address: int, payload_type: int, *, n_elements: int = 1, name: str = ""):
        """Create a `READ_ONLY | EVENT` register and return an
        `async emit(value)` closure.

        No always-on drain task — every call enqueues one EVENT frame
        via the standard ad-hoc emit path.  The returned closure bakes
        in the address and payload type so the caller passes only the
        value::

            emit_error = device.bind_event_register(0x22, PT_U8, name="Error")
            ...
            await emit_error(1)        # send EVENT 0x22 with payload byte 1
        """
        if self.bank.get(address) is None:
            self.add_register(
                address, payload_type, n_elements=n_elements,
                access=READ_ONLY | EVENT, name=name,
            )

        async def _emit(value, _self=self, _addr=address, _pt=payload_type):
            await _self.emit(_addr, value, _pt)

        return _emit

    def bind_state_register(self, address: int, payload_type: int, sequence, mapping, *, name: str = ""):
        """Create a `READ_WRITE` register whose written byte drives a
        `TaskSequence`.

        `mapping[written_value]` -> state name passed to `sequence(...)`.
        Written values not present in `mapping` are silently ignored (no
        transition, no cancel).  To map a value to "transition to idle",
        include it explicitly with value `None` (e.g. `{1: "A", 0: None}`).
        Storage mirrors the most-recently-written value so READs return
        current state.
        """
        reg = self.add_register(address, payload_type, access=READ_WRITE, name=name)

        async def _on_write(r, payload, _l=sequence, _m=mapping, _nt=_NO_TRANSITION):
            v = payload[0]
            r.storage[0] = v
            s = _m.get(v, _nt)
            if s is not _nt:
                _l(s)

        reg.on_write = _on_write
        return reg

    def add_task_sequence(self, states, *, guards=None, on_timeout=None):
        """Construct a `TaskSequence` driven by this device's loop.

        `states` is a dict mapping state names to one of:
          - None                       : passive state, no task
          - factory                    : run factory() until next transition
          - (factory, timeout_seconds) : factory wrapped in `asyncio.wait_for`

        `guards` is an optional `{state_name: predicate}` dict; transitions
        to a guarded state are silently dropped when `predicate()` returns
        False.

        On natural task completion or timeout, `_cur_state` resets to None.
        On timeout, `await on_timeout(name)` fires first (typically used to
        emit an error EVENT), then the reset.

        See `microharp.events.TaskSequence` for full semantics.
        """
        from .events import TaskSequence
        return TaskSequence(states, guards=guards, on_timeout=on_timeout)

    def timestamp_now(self):
        """`(seconds, ticks)` pair — current Harp time.  Allocates a tuple."""
        return self.clock.now()

    # ------------------------------------------------------------------
    # Task HOF
    # ------------------------------------------------------------------

    def task(
        self,
        coro_or_factory,
    ):
        """Decorator / function: enqueue an extra coroutine.

        Accepts either a no-arg async function (called once at run()) or a
        coroutine object directly::

            @device.task
            async def blink_extra():
                while True:
                    await asyncio.sleep(5)

            device.task(my_coroutine_object)
        """
        self._user_tasks.append(coro_or_factory)
        return coro_or_factory

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def run(self):
        """Launch all core tasks + event sources + user tasks."""
        tasks = [
            usb_rx_task(self.transport, self.decoder, self.rx_q, self.slabs),
            usb_tx_task(self.transport, self.tx_q, self.slabs),
            dispatch_task(self.dispatcher),
            heartbeat_task(self.clock, self.bank, self.tx_q, self.slabs),
            # Drives the per-second tick whether or not a sync master is
            # present — required for HEARTBEAT_EN per spec.
            second_ticker(self.clock),
        ]
        if self.sync_uart is not None:
            tasks.append(sync_task(self.sync_uart, self.clock))
        if self.led_pin is not None:
            tasks.append(led_task(self.led_pin, self.clock, self.bank))
        for src in self._event_sources:
            tasks.append(src.run())
        for ut in self._user_tasks:
            tasks.append(ut() if callable(ut) else ut)
        await asyncio.gather(*tasks)
