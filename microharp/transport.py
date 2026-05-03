"""Transports, slab pool, and RX/TX asyncio tasks.

Three transport options, all duck-typed (`read_some(buf)` and `write(mv)`):

    StdioTransport   — wraps sys.stdin.buffer / sys.stdout.buffer.
                       Easiest, but conflicts with the REPL on the same CDC.

    CdcTransport     — wraps a custom CDC interface object (e.g.
                       `usb.device.cdc.CDCInterface()` on RP2 / MP 1.23+).
                       Run REPL on the default CDC and Harp on the second.

    UartTransport    — wraps `machine.UART`.  Use when USB is unavailable
                       or when the host connects via FTDI/USB-UART bridge.

`StreamTransport` is the underlying generic implementation; CdcTransport
and UartTransport are thin aliases for naming clarity at call sites.

Slab pool
---------
Outgoing frames are built into fixed-size pre-allocated `bytearray`s
("slabs") leased from `SlabPool`.  The slab *index* is what travels
through the asyncio queues — the bytearray itself is never copied.
"""

from micropython import const
import asyncio
import sys

from .framing import FrameDecoder
from .queue import Queue

# Slab size: enough header (5) + timestamp (6) + payload (up to ~50) + cs (1).
DEFAULT_SLAB_SIZE = const(64)
DEFAULT_SLAB_COUNT = const(16)


# ---------------------------------------------------------------------------
# Slab pool
# ---------------------------------------------------------------------------


class SlabPool:
    """Lease/return fixed-size bytearrays without runtime allocation.

    Each slab carries a `length` field tracking how many bytes are valid.
    `lease()` blocks (asynchronously) when exhausted.
    """

    __slots__ = ("_slabs", "_lengths", "_free", "_avail")

    def __init__(self, count: int = DEFAULT_SLAB_COUNT, size: int = DEFAULT_SLAB_SIZE):
        self._slabs = [bytearray(size) for _ in range(count)]
        self._lengths = [0] * count
        self._free = list(range(count))
        # asyncio.Event used as a "non-empty" signal; we use a Semaphore-ish
        # pattern by setting on release and clearing when we drain to empty.
        self._avail = asyncio.Event()
        self._avail.set()

    async def lease(self):
        while not self._free:
            self._avail.clear()
            await self._avail.wait()
        return self._free.pop()

    def lease_nowait(self):
        """Returns slab index, or -1 if pool is exhausted.

        Safe to call from soft IRQ / scheduled callback context — does NOT
        await and does NOT touch asyncio internals beyond a list pop.
        """
        if not self._free:
            return -1
        return self._free.pop()

    def release(self, idx: int):
        self._free.append(idx)
        self._avail.set()

    def buf(self, idx: int):
        return self._slabs[idx]

    def view(self, idx: int, length = None):
        if length is None:
            length = self._lengths[idx]
        return memoryview(self._slabs[idx])[:length]

    def set_length(self, idx: int, length: int):
        self._lengths[idx] = length

    def length(self, idx: int):
        return self._lengths[idx]


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class StreamTransport:
    """Generic transport over any object with stream semantics
    (`readinto`, `write`, ideally `fileno`) — UART, custom CDC interface,
    BLE-as-stream, etc.

    Uses MicroPython's `asyncio.StreamReader` / `StreamWriter`, which
    register the underlying object with the event-loop's poll/select
    wait set.  A task awaiting `read_some` wakes within one scheduler
    iteration of bytes becoming available — no fixed 1 ms polling.

    Falls back to a non-blocking-readinto + sleep_ms(0) loop on objects
    that can't be wrapped (no `fileno`).
    """

    __slots__ = ("_s", "_reader", "_writer")

    def __init__(self, stream):
        self._s = stream
        try:
            self._reader = asyncio.StreamReader(stream)
            self._writer = asyncio.StreamWriter(stream, {})
        except (TypeError, AttributeError):
            self._reader = None
            self._writer = None

    async def read_some(self, buf: bytearray):
        if self._reader is not None:
            return await self._reader.readinto(buf)
        s = self._s
        while True:
            n = s.readinto(buf)
            if n:
                return n
            await asyncio.sleep_ms(0)

    async def write(self, mv: memoryview):
        if self._writer is not None:
            self._writer.write(mv)
            await self._writer.drain()
            return
        s = self._s
        n = 0
        total = len(mv)
        while n < total:
            written = s.write(mv[n:])
            if written is None:
                await asyncio.sleep_ms(0)
                continue
            n += written
            if n < total:
                await asyncio.sleep_ms(0)


class StdioTransport(StreamTransport):
    """Wraps `sys.stdin.buffer` / `sys.stdout.buffer` (default REPL CDC).

    Stdin and stdout are different objects, so we wrap each in its own
    StreamReader/StreamWriter — both go through the same poll-based wake
    machinery as StreamTransport.  Easiest to set up, but Harp framing
    collides with the REPL on the same interface; use `CdcTransport` for
    production.
    """

    __slots__ = ("_in", "_out", "_in_reader", "_out_writer")

    def __init__(self):
        # Bypass StreamTransport.__init__ — different stream objects in/out.
        self._s = None
        self._reader = None
        self._writer = None
        self._in = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
        self._out = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout
        try:
            self._in_reader = asyncio.StreamReader(self._in)
            self._out_writer = asyncio.StreamWriter(self._out, {})
        except (TypeError, AttributeError):
            self._in_reader = None
            self._out_writer = None

    async def read_some(self, buf):
        if self._in_reader is not None:
            return await self._in_reader.readinto(buf)
        while True:
            n = self._in.readinto(buf)
            if n:
                return n
            await asyncio.sleep_ms(0)

    async def write(self, mv):
        if self._out_writer is not None:
            self._out_writer.write(mv)
            await self._out_writer.drain()
            return
        n = 0
        total = len(mv)
        while n < total:
            written = self._out.write(mv[n:])
            if written is None:
                await asyncio.sleep_ms(0)
                continue
            n += written
            if n < total:
                await asyncio.sleep_ms(0)


# Naming aliases — same behaviour, different intent at the call site.
class UartTransport(StreamTransport):
    """Harp over a `machine.UART` — set baud high enough for your traffic
    (921600+ recommended).  Use `UART(..., timeout=0)` for non-blocking RX."""

    pass


class CdcTransport(StreamTransport):
    """Harp over a *custom* CDC interface, separate from the REPL CDC.

    Construct on RP2 / MicroPython ≥1.23 like this::

        import usb.device
        import usb.device.cdc
        cdc = usb.device.cdc.CDCInterface()
        cdc.init(timeout=0)
        usb.device.get().init(cdc, builtin_driver=True)
        transport = CdcTransport(cdc)

    Now Harp talks on /dev/ttyACM1 while the REPL keeps /dev/ttyACM0.
    """

    pass


# Back-compat alias for the previous public name.
UsbTransport = StdioTransport


# ---------------------------------------------------------------------------
# RX task
# ---------------------------------------------------------------------------


async def usb_rx_task(transport: StreamTransport, decoder: FrameDecoder, rx_queue: Queue, slab_pool: SlabPool, *, chunk: int = 64):
    """Read raw bytes from `transport`, feed `decoder`, enqueue completed
    frames as slab indices onto `rx_queue`.

    The decoder's internal buffer holds the last frame; we copy it into a
    leased slab so it survives until the dispatcher consumes it.
    """
    chunk_buf = bytearray(chunk)
    chunk_mv = memoryview(chunk_buf)
    # Bind hot attributes once.
    read_some = transport.read_some
    feed = decoder.feed
    lease = slab_pool.lease
    buf_of = slab_pool.buf
    set_length = slab_pool.set_length
    rx_put = rx_queue.put

    while True:
        n = await read_some(chunk_mv)
        if n <= 0:
            continue
        for frame_mv in feed(chunk_mv[:n]):
            length = len(frame_mv)
            idx = await lease()
            slab = buf_of(idx)
            slab[:length] = frame_mv
            set_length(idx, length)
            await rx_put(idx)


# ---------------------------------------------------------------------------
# TX task
# ---------------------------------------------------------------------------


async def usb_tx_task(transport: StreamTransport, tx_queue: Queue, slab_pool: SlabPool, *, hi_queue: Queue = None):
    """Drain `tx_queue` (and optional `hi_queue`) and write to the transport.

    Slabs are returned to the pool after each successful write.  When
    multiple slabs are queued, we coalesce them into a single write by
    chaining memoryviews (one syscall, one USB packet where size permits).
    """
    # Cache bound methods used per-iteration.
    write = transport.write
    tx_get = tx_queue.get
    tx_get_now = tx_queue.get_nowait
    tx_empty = tx_queue.empty
    view = slab_pool.view
    release = slab_pool.release
    hi_get_now = hi_queue.get_nowait if hi_queue is not None else None
    hi_empty = hi_queue.empty if hi_queue is not None else None

    pending_idxs = []
    pending_mvs = []
    p_idx_append = pending_idxs.append
    p_mv_append = pending_mvs.append

    while True:
        # Drain high-priority queue first if provided.
        if hi_get_now is not None and hi_empty is not None:
            while not hi_empty():
                idx = hi_get_now()
                p_idx_append(idx)
                p_mv_append(view(idx))

        if not pending_idxs:
            idx = await tx_get()
            p_idx_append(idx)
            p_mv_append(view(idx))

        # Opportunistically batch any further slabs already waiting.
        while not tx_empty() and len(pending_idxs) < 8:
            idx = tx_get_now()
            p_idx_append(idx)
            p_mv_append(view(idx))

        # Write each slab back-to-back; the underlying CDC stack packs
        # consecutive small writes into one USB packet on most ports.
        for mv in pending_mvs:
            await write(mv)

        for idx in pending_idxs:
            release(idx)

        pending_idxs.clear()
        pending_mvs.clear()
