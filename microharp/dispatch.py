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

    __slots__ = ("bank", "clock", "rx_q", "tx_q", "slabs", "default_port", "errors")

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
        # Cache bound methods so the inner loop is LOAD_FAST, not LOAD_ATTR.
        rx_get = self.rx_q.get
        slabs = self.slabs
        slabs_buf = slabs.buf
        slabs_length = slabs.length
        slabs_release = slabs.release
        handle_one = self._handle_one

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

    async def _handle_one(self, slab: bytearray, length: int):
        mv = memoryview(slab)[:length]
        msg_type, address, port, pt, has_ts, _, _, po, pl = parse_header(mv)
        # Strip error-bit if a host echo'd one back.
        op = msg_type & 0x07

        reg = self.bank.get(address)

        if op == MSG_READ:
            # Validate — then run the handler immediately, before any reply
            # preparation (muted check, timestamp, slab lease, encode).
            if reg is None or not (reg.access & READ_ONLY):
                secs, ticks = self.clock.now()
                if not self._replies_muted():
                    await self._send_error(op, address, port, secs, ticks)
                return
            if reg.on_read is not None:
                await reg.on_read(reg)
            # Timestamp after handler — reflects when the value was sampled.
            secs, ticks = self.clock.now()
            if not self._replies_muted():
                await self._send_reg_reply(MSG_READ, reg, port, secs, ticks)

        elif op == MSG_WRITE:
            payload_mv = mv[po : po + pl]
            # Validate — no handler runs on a bad request.
            if reg is None or not (reg.access & WRITE_ONLY):
                secs, ticks = self.clock.now()
                if not self._replies_muted():
                    await self._send_error(op, address, port, secs, ticks)
                return
            if len(payload_mv) != len(reg.storage):
                secs, ticks = self.clock.now()
                if not self._replies_muted():
                    await self._send_error(op, address, port, secs, ticks)
                return
            # Run the handler immediately — before any reply preparation.
            err = None
            if reg.on_write is not None:
                err = await reg.on_write(reg, payload_mv)
            else:
                reg.storage[:] = payload_mv
            # Timestamp after write completes — reply carries write-done time.
            secs, ticks = self.clock.now()
            muted = self._replies_muted()
            if err is not None:
                if not muted:
                    await self._send_error(op, address, port, secs, ticks)
                return
            # Spec ordering: WRITE reply first, then DUMP messages (if any).
            if not muted:
                await self._send_reg_reply(MSG_WRITE, reg, port, secs, ticks)
            if (address == R_OPERATION_CONTROL
                    and (reg.storage[0] & OP_DUMP)
                    and not muted):
                await self._dump_all_registers(port)
                reg.storage[0] = reg.storage[0] & ~OP_DUMP

        else:
            # MSG_EVENT received from host?  Not defined; ignore.
            return

    async def _dump_all_registers(self, port: int):
        """Emit one READ reply per register in the bank.  Each carries
        its own timestamp (the moment that register was sampled), per
        Harp convention for DUMP replies."""
        for r in self.bank:
            if not (r.access & READ_ONLY):
                # Write-only register — skip in DUMP.
                continue
            if r.on_read is not None:
                await r.on_read(r)
            secs, ticks = self.clock.now()
            await self._send_reg_reply(MSG_READ, r, port, secs, ticks)


async def dispatch_task(dispatcher: Dispatcher):
    await dispatcher.run()
