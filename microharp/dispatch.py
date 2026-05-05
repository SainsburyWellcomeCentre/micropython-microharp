"""Dispatcher: routes incoming Harp messages to register handlers.

Pulls (slab_idx) from `rx_queue`, parses the header, runs the appropriate
register handler, builds the reply into a fresh slab, and pushes it onto
`tx_queue`.

Reply rules (Device.md §Request-Reply):
  * READ  request  → READ  reply with current register payload (+ timestamp).
  * WRITE request  → WRITE reply echoing the written payload (or ERROR).
  * Unknown / mismatched-length / wrong-direction → READ_ERROR / WRITE_ERROR.
  * If `OperationControl.MUTE_RPL` is set, the device sends NO reply
    messages of any kind (READ, WRITE, EVENT-from-handler, errors).
  * If a WRITE to OperationControl sets the DUMP bit, after sending the
    WRITE reply the device emits one READ reply per register, then
    auto-clears DUMP.

Timestamping:
    READ reply  : timestamp captured before `on_read` runs — the spec
                  requires the time at which the request was processed
                  (i.e. received and dispatched).
    WRITE reply : timestamp re-captured *after* `on_write` completes —
                  the reply must carry the time at which the write
                  operation finished, not when the request arrived.
    ERROR reply : timestamp from request-arrival time (pre-handler).
"""

from .framing import (
    MSG_READ,
    MSG_WRITE,
    MSG_FLAG_ERROR,
    encode_into,
    parse_header,
    parse_header_into,
)
from .registers import (
    READ_ONLY,
    WRITE_ONLY,
    READ_WRITE,
    R_OPERATION_CONTROL,
    OP_MUTE_REPLIES,
    OP_DUMP,
    RegisterEntry,
    RegisterBank,
)
from .clock import Clock
from .queue import Queue
from .transport import SlabPool


class Dispatcher:
    """Owns the register bank, slab pool, and TX queue references."""

    __slots__ = ("bank", "clock", "rx_q", "tx_q", "slabs", "default_port", "errors", "_h", "_ts")

    def __init__(self, bank: RegisterBank, clock: Clock, rx_queue: Queue, tx_queue: Queue, slab_pool: SlabPool, default_port: int = 255):
        self.bank = bank
        self.clock = clock
        self.rx_q = rx_queue
        self.tx_q = tx_queue
        self.slabs = slab_pool
        self.default_port = default_port
        # Error counter: incremented on any handler exception.  Exposed for
        # apps that want to publish it in a register.  We deliberately do
        # NOT print, because stdout may be the Harp wire.
        self.errors = 0
        # Pre-allocated scratch for the dispatch hot path — avoids creating
        # a new 9-tuple (parse_header_into) and 2-tuples (now_fast) on every
        # message, which otherwise accumulate until a stop-the-world GC fires.
        self._h = [0, 0, 0, 0, 0, 0, 0]  # parse_header_into output
        self._ts = [0, 0]                  # now_fast output: [secs, ticks]

    # ---- helpers ---------------------------------------------------------

    def _replies_muted(self):
        op = self.bank.get(R_OPERATION_CONTROL)
        return op is not None and (op.storage[0] & OP_MUTE_REPLIES)

    async def _send_error(self, msg_type: int, address: int, port: int, secs: int, ticks: int):
        slab_idx = await self.slabs.lease()
        slab = self.slabs.buf(slab_idx)
        # Error reply: same operation, ERROR flag set, no payload (or zero).
        # `secs`/`ticks` are the request-processing timestamp captured by
        # the caller; do NOT recompute here — `slabs.lease()` may have
        # blocked, and the spec demands the request-processing time.
        n = encode_into(
            slab,
            msg_type | MSG_FLAG_ERROR,
            address,
            port,
            0x01,  # PT_U8 dummy
            payload=None,
            payload_len=0,
            ts_seconds=secs,
            ts_ticks=ticks,
        )
        self.slabs.set_length(slab_idx, n)
        await self.tx_q.put(slab_idx)

    async def _send_reg_reply(self, msg_type: int, reg: RegisterEntry, port: int, secs: int, ticks: int):
        slab_idx = await self.slabs.lease()
        slab = self.slabs.buf(slab_idx)
        # See _send_error: timestamp comes from the caller, not from now().
        n = encode_into(
            slab,
            msg_type,
            reg.address,
            port,
            reg.payload_type,
            payload=reg.storage,
            payload_len=len(reg.storage),
            ts_seconds=secs,
            ts_ticks=ticks,
        )
        self.slabs.set_length(slab_idx, n)
        await self.tx_q.put(slab_idx)

    # ---- main loop -------------------------------------------------------

    async def run(self):
        import gc as _gc
        import time as _time
        # Cache bound methods so the inner loop is LOAD_FAST, not LOAD_ATTR.
        rx_get = self.rx_q.get
        rx_empty = self.rx_q.empty
        slabs = self.slabs
        slabs_buf = slabs.buf
        slabs_length = slabs.length
        slabs_release = slabs.release
        handle_one = self._handle_one

        # Proactive GC: collect while rx_q is idle, but at most once per
        # _GC_MS.  This drains the heap BEFORE it fills so the automatic GC
        # never fires mid-message (a full-heap GC on RP2040 takes ~10-15 ms;
        # a near-empty one here takes <1 ms and doesn't appear as an outlier).
        _GC_MS = 1000
        _last_gc = _time.ticks_ms()

        while True:
            slab_idx = await rx_get()
            try:
                await handle_one(slabs_buf(slab_idx), slabs_length(slab_idx))
            except Exception:
                # Don't kill the dispatcher; do not print() (stdout may be
                # the Harp wire).  Bump a counter the app can expose.
                self.errors = (self.errors + 1) & 0xFFFFFFFF
            finally:
                slabs_release(slab_idx)
            if rx_empty():
                now = _time.ticks_ms()
                if _time.ticks_diff(now, _last_gc) >= _GC_MS:
                    _gc.collect()
                    _last_gc = now

    async def _handle_one(self, slab: bytearray, length: int):
        mv = memoryview(slab)[:length]
        h = self._h
        ts = self._ts
        parse_header_into(mv, h)
        # h[0]=msg_type, h[1]=address, h[2]=port, h[3]=pt,
        # h[4]=has_ts,   h[5]=po,      h[6]=pl
        op = h[0] & 0x07

        reg = self.bank.get(h[1])

        if op == MSG_READ:
            # Validate — then run the handler immediately, before any reply
            # preparation (muted check, timestamp, slab lease, encode).
            if reg is None or not (reg.access & READ_ONLY):
                self.clock.now_fast(ts)
                if not self._replies_muted():
                    await self._send_error(op, h[1], h[2], ts[0], ts[1])
                return
            if reg.on_read is not None:
                await reg.on_read(reg)
            # Timestamp after handler — reflects when the value was sampled.
            self.clock.now_fast(ts)
            if not self._replies_muted():
                await self._send_reg_reply(MSG_READ, reg, h[2], ts[0], ts[1])

        elif op == MSG_WRITE:
            payload_mv = mv[h[5] : h[5] + h[6]]
            # Validate — no handler runs on a bad request.
            if reg is None or not (reg.access & WRITE_ONLY):
                self.clock.now_fast(ts)
                if not self._replies_muted():
                    await self._send_error(op, h[1], h[2], ts[0], ts[1])
                return
            if len(payload_mv) != len(reg.storage):
                self.clock.now_fast(ts)
                if not self._replies_muted():
                    await self._send_error(op, h[1], h[2], ts[0], ts[1])
                return
            # Run the handler immediately — before any reply preparation.
            err = None
            if reg.on_write is not None:
                err = await reg.on_write(reg, payload_mv)
            else:
                reg.storage[:] = payload_mv
            # Timestamp after write completes — reply carries write-done time.
            self.clock.now_fast(ts)
            muted = self._replies_muted()
            if err is not None:
                if not muted:
                    await self._send_error(op, h[1], h[2], ts[0], ts[1])
                return
            # Spec ordering: WRITE reply first, then DUMP messages (if any).
            if not muted:
                await self._send_reg_reply(MSG_WRITE, reg, h[2], ts[0], ts[1])
            if (h[1] == R_OPERATION_CONTROL
                    and (reg.storage[0] & OP_DUMP)
                    and not muted):
                await self._dump_all_registers(h[2])
                reg.storage[0] = reg.storage[0] & ~OP_DUMP

        else:
            # MSG_EVENT received from host?  Not defined; ignore.
            return

    async def _dump_all_registers(self, port: int):
        """Emit one READ reply per register in the bank.  Each carries
        its own timestamp (the moment that register was sampled), per
        Harp convention for DUMP replies."""
        ts = self._ts
        for r in self.bank:
            if not (r.access & READ_ONLY):
                # Write-only register — skip in DUMP.
                continue
            if r.on_read is not None:
                await r.on_read(r)
            self.clock.now_fast(ts)
            await self._send_reg_reply(MSG_READ, r, port, ts[0], ts[1])


async def dispatch_task(dispatcher: Dispatcher):
    await dispatcher.run()
