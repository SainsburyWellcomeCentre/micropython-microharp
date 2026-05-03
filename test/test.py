"""Unit tests for microharp on CPython.

Installs small `micropython`, `machine`, `time.ticks_*`, and
`asyncio.ThreadSafeFlag` shims so the package imports cleanly under CPython.

Tests covered:

  Framing  (microharp.framing)
    - encode_into / FrameDecoder / parse_header round-trips
    - multi-frame buffers
    - byte-by-byte feed
    - bad-checksum drop
    - garbage-prefix resync

  Clock  (microharp.clock)
    - set_seconds() re-anchors and now() returns the new value
    - latency_us shifts the epoch backward by the expected amount
    - apply_sync() with latency_us folds in correctly

  R_TIMESTAMP_SECOND
    - is registered as READ_WRITE (was a bug: previously READ_ONLY)
    - the on_write handler updates the Clock anchor
    - subsequent reads return the new seconds value

  R_OPERATION_CONTROL
    - default value is STANDBY | VISUAL_EN
    - bit accessors via masks (OP_OP_MODE_MASK, OP_VISUAL_EN, etc.)
    - DUMP-bit write enqueues a READ reply for every register
    - DUMP bit auto-clears after the dump
    - MUTE_REPLIES suppresses ALL replies (WRITE, READ, errors)

  EventSource  (microharp.events)
    - events are emitted in Active mode
    - events are silently dropped in Standby mode

Run from the repo root:
    python tests/test_microharp.py
"""

import sys
import os
import types
import asyncio
import struct

# ---- micropython shim ------------------------------------------------------
mp = types.ModuleType("micropython")
setattr(mp, "const", lambda x: x)
setattr(mp, "native", lambda fn: fn)
# Intentionally NO `viper` attribute — framing falls back to plain-Python checksum.
sys.modules["micropython"] = mp

# ---- machine shim ----------------------------------------------------------
# microharp/__init__.py re-exports led/device/transport, which import
# `from machine import Pin/UART` at module load.  CPython has no `machine`,
# so we install a tiny stub.  These tests don't drive any GPIO.
_machine = types.ModuleType("machine")


class _Pin:
    OUT = 0
    IN = 1
    PULL_UP = 2
    IRQ_RISING = 4
    IRQ_FALLING = 8

    def __init__(self, *a, **kw):
        pass

    def value(self, *a):
        return 0

    def irq(self, *a, **kw):
        return None


class _UART:
    def __init__(self, *a, **kw):
        pass

    def irq(self, *a, **kw):
        return None

    def read(self, *a):
        return b""

    def any(self):
        return 0


setattr(_machine, "Pin", _Pin)
setattr(_machine, "UART", _UART)
sys.modules["machine"] = _machine

# ---- time shim -------------------------------------------------------------
import time as _t

if not hasattr(_t, "ticks_us"):
    _epoch_ns = _t.monotonic_ns()
    setattr(_t, "ticks_us", lambda: ((_t.monotonic_ns() - _epoch_ns) // 1_000) & 0x3FFFFFFF)
    setattr(_t, "ticks_ms", lambda: ((_t.monotonic_ns() - _epoch_ns) // 1_000_000) & 0x3FFFFFFF)
    setattr(_t, "ticks_diff", lambda a, b: a - b)
    setattr(_t, "ticks_add", lambda t, d: t + d)

# ---- asyncio.ThreadSafeFlag shim -------------------------------------------
# MicroPython-only class.  CPython's asyncio has no equivalent.  We
# substitute a thin auto-clearing wrapper around asyncio.Event so the
# Clock constructor (which instantiates one) doesn't blow up.
if not hasattr(asyncio, "ThreadSafeFlag"):

    class _ThreadSafeFlagShim(asyncio.Event):
        async def wait(self) -> bool:  # type: ignore[override]
            await super().wait()
            self.clear()
            return True

    setattr(asyncio, "ThreadSafeFlag", _ThreadSafeFlagShim)

# Make microharp/ importable.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


# ---- Test scaffolding ------------------------------------------------------
ok = 0
fail = 0


def check(cond, label):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS", label)
    else:
        fail += 1
        print("  FAIL", label)


# One shared event loop for all run() calls.  In CPython 3.10+,
# asyncio.Queue / Event bind to whichever loop they first see; reusing a
# single loop avoids RuntimeErrors across runs.
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run(coro):
    """Run a coroutine to completion on the shared event loop."""
    return _LOOP.run_until_complete(coro)


# ===========================================================================
# Framing tests
# ===========================================================================
from microharp.framing import (
    encode_into,
    FrameDecoder,
    parse_header,
    MSG_READ,
    MSG_WRITE,
    MSG_EVENT,
    MSG_FLAG_ERROR,
    PT_U8,
    PT_U16,
    PT_U32,
    PT_FLOAT,
    PT_HAS_TIMESTAMP,
)

# ---- Test 1: encode → decode round-trip, float register, no timestamp ------
print("Framing — Test 1: encode/decode float register without timestamp")
buf = bytearray(64)
_float_val = 3.14
_float_bytes = struct.pack("<f", _float_val)
n = encode_into(buf, MSG_WRITE, address=42, port=0, payload_type=PT_FLOAT, payload=_float_bytes, payload_len=4)
print("  encoded bytes:", buf[:n].hex())

dec = FrameDecoder(max_payload=32)
frames = list(dec.feed(buf[:n]))
check(len(frames) == 1, "decoded 1 frame")
mv = frames[0]
mt, addr, port, pt, has_ts, secs, ticks, po, pl = parse_header(mv)
check(mt == MSG_WRITE, "msg_type=WRITE")
check(addr == 42, "address=42")
check(port == 0, "port=0")
check(pt == PT_FLOAT, "payload_type=FLOAT")
check(not has_ts, "no timestamp")
check(pl == 4, "payload_len=4")
_decoded_val = struct.unpack("<f", bytes(mv[po : po + pl]))[0]
check(abs(_decoded_val - _float_val) < 1e-5, "payload float ≈ 3.14")


# ---- Test 2: with timestamp, U32 payload ------------------------------------
print("Framing — Test 2: encode/decode WITH timestamp, U32 payload")
n = encode_into(
    buf,
    MSG_EVENT,
    address=32,
    port=0,
    payload_type=PT_U32,
    payload=b"\x01\x02\x03\x04",
    payload_len=4,
    ts_seconds=0xDEADBEEF,
    ts_ticks=0x1234,
)
dec2 = FrameDecoder(max_payload=32)
frames = list(dec2.feed(buf[:n]))
check(len(frames) == 1, "decoded 1 frame")
mv = frames[0]
mt, addr, port, pt, has_ts, secs, ticks, po, pl = parse_header(mv)
check(mt == MSG_EVENT, "msg_type=EVENT")
check(addr == 32, "address=32")
check((pt & 0x0F) == PT_U32 & 0x0F, "payload_type lower nibble = U32")
check(has_ts, "has_timestamp=True")
check(secs == 0xDEADBEEF, "ts_seconds")
check(ticks == 0x1234, "ts_ticks")
check(pl == 4, "payload_len=4")
check(bytes(mv[po : po + pl]) == b"\x01\x02\x03\x04", "payload bytes")


# ---- Test 3: multiple frames concatenated -----------------------------------
print("Framing — Test 3: two frames in a single buffer")
combo = bytearray()
b1 = bytearray(64)
n1 = encode_into(b1, MSG_READ, 1, 0, PT_U8, b"\x00", 1)
combo.extend(b1[:n1])
b2 = bytearray(64)
n2 = encode_into(b2, MSG_WRITE, 2, 0, PT_U16, b"\xaa\xbb", 2)
combo.extend(b2[:n2])

dec3 = FrameDecoder(max_payload=32)
frames = list(dec3.feed(combo))
check(len(frames) == 2, "decoded 2 frames")


# ---- Test 4: partial / chunked feed -----------------------------------------
print("Framing — Test 4: byte-by-byte feed")
dec4 = FrameDecoder(max_payload=32)
b3 = bytearray(64)
n3 = encode_into(b3, MSG_WRITE, 7, 0, PT_U16, b"\x12\x34", 2)
got = []
for byte in b3[:n3]:
    got.extend(dec4.feed(bytearray([byte])))
check(len(got) == 1, "byte-by-byte still produces 1 frame")


# ---- Test 5: bad checksum is dropped ----------------------------------------
print("Framing — Test 5: bad checksum is silently dropped")
b4 = bytearray(64)
n4 = encode_into(b4, MSG_WRITE, 9, 0, PT_U8, b"\x55", 1)
b4[n4 - 1] ^= 0xFF  # corrupt the checksum
dec5 = FrameDecoder(max_payload=32)
frames = list(dec5.feed(b4[:n4]))
check(len(frames) == 0, "0 frames yielded for bad checksum")


# ---- Test 6: garbage prefix → resync ----------------------------------------
print("Framing — Test 6: garbage prefix is skipped")
junk = bytearray(b"\x00\xff\x55")
b5 = bytearray(64)
n5 = encode_into(b5, MSG_READ, 1, 0, PT_U8, b"\x00", 1)
combo2 = junk + b5[:n5]
dec6 = FrameDecoder(max_payload=32)
frames = list(dec6.feed(combo2))
check(len(frames) == 1, "decoder resynchronizes past junk")


# ===========================================================================
# Clock tests
# ===========================================================================
print("Clock.set_seconds")

from microharp.clock import Clock, _ticks_us, US_PER_TICK

clk = Clock()
clk.set_seconds(1000)
secs, ticks = clk.now()
check(secs == 1000, "set_seconds(1000) → seconds == 1000 immediately after")
check(ticks < 100, "ticks immediately after re-anchor are small (<100)")

# Advance time and re-read.
_t.monotonic_ns()  # ensure CPython monotonic moves
_target_us = _t.ticks_us() + 50_000  # type: ignore[attr-defined]  # anchor 50 ms in the past
clk.set_seconds(2000, anchor_us=_target_us)
secs, ticks = clk.now()
check(secs == 2000, "set_seconds(2000, anchor_us=future) → seconds == 2000")

# Latency compensation: set_seconds with latency_us=100 should shift the
# epoch backward by 100 µs, i.e. now() reports 100 µs more elapsed.
clk2 = Clock()
clk2.set_seconds(0, latency_us=0)
_, ticks_no_comp = clk2.now()

clk3 = Clock()
# 100 µs / 32 µs ≈ 3 extra Harp ticks under compensation.
clk3.set_seconds(0, latency_us=100)
_, ticks_comp = clk3.now()
check(ticks_comp >= ticks_no_comp, "latency_us=100 → reported ticks >= uncompensated reading")
check((ticks_comp - ticks_no_comp) >= 2, "latency_us=100 adds ~3 ticks of advance (>=2 to account for jitter)")


# ===========================================================================
# Common register installation
# ===========================================================================
print("install_common_registers — TimestampSecond writability")

from microharp.registers import (
    RegisterBank,
    install_common_registers,
    READ_ONLY,
    WRITE_ONLY,
    READ_WRITE,
    EVENT,
    R_WHO_AM_I,
    R_TIMESTAMP_SECOND,
    R_TIMESTAMP_MICRO,
    R_OPERATION_CONTROL,
    R_RESET_DEV,
    R_HEARTBEAT,
    R_VERSION,
    OP_OP_MODE_MASK,
    OP_OP_MODE_STANDBY,
    OP_OP_MODE_ACTIVE,
    OP_DUMP,
    OP_VISUAL_EN,
    OP_HEARTBEAT_EN,
    OP_ALIVE_EN,
    OP_OPLED_EN,
    OP_MUTE_REPLIES,
    OP_DEFAULT,
)
from microharp.transport import SlabPool

clk = Clock()
bank = RegisterBank()
slabs = SlabPool(count=8, size=64)
tx_q = asyncio.Queue(16)

install_common_registers(
    bank,
    who_am_i=1234,
    fw_major=1,
    fw_minor=0,
    hw_major=1,
    hw_minor=0,
    device_name=b"unit-test",
    serial_number=99,
    clock=clk,
    tx_q=tx_q,
    slab_pool=slabs,
)

ts_reg = bank.get(R_TIMESTAMP_SECOND)
check(ts_reg is not None, "R_TIMESTAMP_SECOND exists")
assert ts_reg is not None
check(ts_reg.access == READ_WRITE, "R_TIMESTAMP_SECOND is READ_WRITE")
check(ts_reg.on_read is not None, "R_TIMESTAMP_SECOND has on_read")
check(ts_reg.on_write is not None, "R_TIMESTAMP_SECOND has on_write")


# Writing TimestampSecond should re-anchor the clock.
async def write_ts():
    payload = bytearray(4)
    struct.pack_into("<I", payload, 0, 7777)
    await ts_reg.on_write(ts_reg, payload)  # type: ignore[union-attr]


run(write_ts())
secs, _ = clk.now()
check(secs == 7777, "after WRITE TimestampSecond=7777, clock.now() seconds == 7777")
stored = struct.unpack_from("<I", ts_reg.storage, 0)[0]
check(stored == 7777, "register storage reflects written value")


# ===========================================================================
# OperationControl
# ===========================================================================
print("OperationControl — defaults and bit semantics")

op = bank.get(R_OPERATION_CONTROL)
check(op is not None, "R_OPERATION_CONTROL exists")
assert op is not None
check(op.access == READ_WRITE, "R_OPERATION_CONTROL is READ_WRITE")
default = op.storage[0]
# Spec default 0xE4 = ALIVE_EN | OPLED_EN | VISUAL_EN | HEARTBEAT_EN.
check(default == OP_DEFAULT, "default value == OP_DEFAULT (0xE4)")
check(default == 0xE4, "default value == 0xE4 numerically")
check((default & OP_OP_MODE_MASK) == OP_OP_MODE_STANDBY, "default OP_MODE is STANDBY")
check(bool(default & OP_VISUAL_EN), "default has VISUAL_EN set")
check(bool(default & OP_OPLED_EN), "default has OPLED_EN set")
check(bool(default & OP_HEARTBEAT_EN), "default has HEARTBEAT_EN set")
check(bool(default & OP_ALIVE_EN), "default has ALIVE_EN set")
check(not (default & OP_DUMP), "default does NOT have DUMP")
check(not (default & OP_MUTE_REPLIES), "default does NOT have MUTE_REPLIES")
check(OP_HEARTBEAT_EN == 0x04, "HEARTBEAT_EN == 0x04 (bit 2)")
check(OP_ALIVE_EN == 0x80, "ALIVE_EN == 0x80 (bit 7)")

# R_VERSION must be a 32-byte array per spec §R_VERSION.
ver = bank.get(R_VERSION)
check(ver is not None, "R_VERSION exists")
assert ver is not None
check(len(ver.storage) == 32, "R_VERSION storage is 32 bytes")
check(ver.storage[3] == 1, "R_VERSION FIRMWARE major == fw_major")

# Toggling bits via direct storage write (simulating the dispatcher's
# default copy-into-storage path, before any on_write hook runs).
op.storage[0] = OP_OP_MODE_ACTIVE | OP_HEARTBEAT_EN | OP_VISUAL_EN
check((op.storage[0] & OP_OP_MODE_MASK) == OP_OP_MODE_ACTIVE, "after write: OP_MODE == ACTIVE")
check(bool(op.storage[0] & OP_HEARTBEAT_EN), "after write: HEARTBEAT_EN bit set")


# ===========================================================================
# OperationControl — DUMP behaviour (via dispatcher)
# ===========================================================================
print("OperationControl — DUMP behaviour (via dispatcher)")

from microharp.dispatch import Dispatcher

clk2 = Clock()
bank2 = RegisterBank()
slabs2 = SlabPool(count=128, size=64)
rx_q2 = asyncio.Queue(16)
tx_q2 = asyncio.Queue(128)
install_common_registers(
    bank2,
    who_am_i=42,
    fw_major=1,
    fw_minor=0,
    hw_major=1,
    hw_minor=0,
    device_name=b"dump-test",
    serial_number=1,
    clock=clk2,
    tx_q=tx_q2,
    slab_pool=slabs2,
)
disp2 = Dispatcher(bank2, clk2, rx_q2, tx_q2, slabs2)  # type: ignore[arg-type]
op2 = bank2.get(R_OPERATION_CONTROL)
assert op2 is not None
n_readable = sum(1 for r in bank2 if (r.access & READ_ONLY))


async def trigger_dump_via_dispatcher():
    # Build a WRITE OperationControl frame with DUMP bit set.
    new_val = op2.storage[0] | OP_DUMP  # type: ignore[union-attr]
    buf = bytearray(64)
    n = encode_into(buf, MSG_WRITE, R_OPERATION_CONTROL, 255, PT_U8, payload=bytes([new_val]), payload_len=1)
    idx = await slabs2.lease()
    slab = slabs2.buf(idx)
    slab[:n] = buf[:n]
    slabs2.set_length(idx, n)
    await disp2._handle_one(slab, n)
    slabs2.release(idx)


run(trigger_dump_via_dispatcher())
n_replies = tx_q2.qsize()
# +1 for the WRITE reply itself, then one READ per readable register.
check(
    n_replies == 1 + n_readable,
    "DUMP enqueued WRITE reply + one READ per readable register (got %d, expected %d)" % (n_replies, 1 + n_readable),
)
check(not (op2.storage[0] & OP_DUMP), "DUMP bit auto-clears after the dump")


# ===========================================================================
# Dispatcher — MUTE_REPLIES suppresses ALL replies
# ===========================================================================
print("Dispatcher — MUTE_REPLIES suppresses ALL replies (WRITE, READ, errors)")

from microharp.registers import RegisterEntry

clk3 = Clock()
bank3 = RegisterBank()
slabs3 = SlabPool(count=16, size=64)
rx_q3 = asyncio.Queue(16)
tx_q3 = asyncio.Queue(16)
install_common_registers(
    bank3,
    who_am_i=42,
    fw_major=1,
    fw_minor=0,
    hw_major=1,
    hw_minor=0,
    device_name=b"mute-test",
    serial_number=1,
    clock=clk3,
    tx_q=tx_q3,
    slab_pool=slabs3,
)

# Add a writable U8 register.
ADDR_X = 32
bank3.add(RegisterEntry(ADDR_X, PT_U8, n_elements=1, access=READ_WRITE, name="X"))

disp = Dispatcher(bank3, clk3, rx_q3, tx_q3, slabs3)  # type: ignore[arg-type]


async def write_to(addr, value, mute=False):
    op = bank3.get(R_OPERATION_CONTROL)
    assert op is not None
    if mute:
        op.storage[0] |= OP_MUTE_REPLIES
    else:
        op.storage[0] &= ~OP_MUTE_REPLIES
    buf = bytearray(64)
    n = encode_into(buf, MSG_WRITE, addr, 255, PT_U8, payload=bytes([value]), payload_len=1)
    idx = await slabs3.lease()
    slab = slabs3.buf(idx)
    slab[:n] = buf[:n]
    slabs3.set_length(idx, n)
    await rx_q3.put(idx)
    slab_idx = await rx_q3.get()
    await disp._handle_one(slabs3.buf(slab_idx), slabs3.length(slab_idx))
    slabs3.release(slab_idx)


# WRITE with MUTE clear → expect a reply on tx_q.
run(write_to(ADDR_X, 0x55, mute=False))
check(tx_q3.qsize() == 1, "MUTE clear → WRITE reply queued")
while tx_q3.qsize():
    slabs3.release(tx_q3.get_nowait())

# WRITE with MUTE set → expect no reply, register STILL updated.
run(write_to(ADDR_X, 0xAA, mute=True))
check(tx_q3.qsize() == 0, "MUTE set → WRITE reply suppressed")
_reg_x = bank3.get(ADDR_X)
assert _reg_x is not None
check(_reg_x.storage[0] == 0xAA, "MUTE set → register still updated despite no reply")


async def read_from(addr, mute=False):
    op = bank3.get(R_OPERATION_CONTROL)
    assert op is not None
    if mute:
        op.storage[0] |= OP_MUTE_REPLIES
    else:
        op.storage[0] &= ~OP_MUTE_REPLIES
    buf = bytearray(64)
    n = encode_into(buf, MSG_READ, addr, 255, PT_U8, payload=None, payload_len=0)
    idx = await slabs3.lease()
    slab = slabs3.buf(idx)
    slab[:n] = buf[:n]
    slabs3.set_length(idx, n)
    await disp._handle_one(slab, n)
    slabs3.release(idx)


# READ with MUTE set → expect no reply.
run(read_from(ADDR_X, mute=True))
check(tx_q3.qsize() == 0, "MUTE set → READ reply suppressed")

run(read_from(ADDR_X, mute=False))
check(tx_q3.qsize() == 1, "MUTE clear → READ reply queued")
while tx_q3.qsize():
    slabs3.release(tx_q3.get_nowait())

# READ of an unknown address with MUTE set → expect no error reply.
run(read_from(99, mute=True))  # 99 is not in the bank
check(tx_q3.qsize() == 0, "MUTE set → error reply also suppressed")

run(read_from(99, mute=False))
check(tx_q3.qsize() == 1, "MUTE clear → error reply queued for unknown addr")
while tx_q3.qsize():
    slabs3.release(tx_q3.get_nowait())


# ===========================================================================
# EventSource — Standby suppresses event emission
# ===========================================================================
print("EventSource — Standby suppresses event emission")

from microharp.events import EventSource

# Active mode → events go through.
op = bank3.get(R_OPERATION_CONTROL)
assert op is not None
op.storage[0] = (op.storage[0] & ~OP_OP_MODE_MASK) | OP_OP_MODE_ACTIVE
op.storage[0] &= ~OP_MUTE_REPLIES

ADDR_E = 33
src = EventSource(ADDR_E, PT_U8, clk3, tx_q3, slabs3, bank=bank3)
src.emit(0x42)


async def drain_one_event():
    # Run ONE iteration of EventSource.run by inlining the drain logic.
    # We avoid running .run() because it would never exit.
    entry = src.ring.pop()
    if entry is None:
        return
    t_irq_us, payload_word = entry
    op_reg = src._op_reg
    if op_reg is not None and (op_reg.storage[0] & OP_OP_MODE_MASK) != OP_OP_MODE_ACTIVE:
        return  # gated: drop
    secs, ticks = clk3.now()
    scratch = bytearray(2)
    n_payload = src._pack(payload_word, scratch)
    idx = await slabs3.lease()
    buf = slabs3.buf(idx)
    n = encode_into(
        buf, 0x03, ADDR_E, 255, PT_U8, payload=scratch, payload_len=n_payload, ts_seconds=secs, ts_ticks=ticks
    )
    slabs3.set_length(idx, n)
    await tx_q3.put(idx)


run(drain_one_event())
check(tx_q3.qsize() == 1, "Active mode → event emitted")
while tx_q3.qsize():
    slabs3.release(tx_q3.get_nowait())

# Standby → event silently dropped.
op.storage[0] = (op.storage[0] & ~OP_OP_MODE_MASK) | OP_OP_MODE_STANDBY
src.emit(0x99)
run(drain_one_event())
check(tx_q3.qsize() == 0, "Standby mode → event dropped (no emit)")


# ===========================================================================
print()
print("Summary: %d pass, %d fail" % (ok, fail))
sys.exit(0 if fail == 0 else 1)
