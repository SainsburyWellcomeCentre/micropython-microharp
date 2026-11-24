# Host-side CPython script; workspace typeshed is MicroPython stubs (no time.perf_counter etc.).
# pyright: reportAttributeAccessIssue=false, reportCallIssue=false
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
    python -B test/test.py
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
print("Testing: Framing")
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
buf = bytearray(64)
_float_val = 3.14
_float_bytes = struct.pack("<f", _float_val)
n = encode_into(buf, MSG_WRITE, address=42, port=0, payload_type=PT_FLOAT, payload=_float_bytes, payload_len=4)
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
dec4 = FrameDecoder(max_payload=32)
b3 = bytearray(64)
n3 = encode_into(b3, MSG_WRITE, 7, 0, PT_U16, b"\x12\x34", 2)
got = []
for byte in b3[:n3]:
    got.extend(dec4.feed(bytearray([byte])))
check(len(got) == 1, "byte-by-byte still produces 1 frame")


# ---- Test 5: bad checksum is dropped ----------------------------------------
b4 = bytearray(64)
n4 = encode_into(b4, MSG_WRITE, 9, 0, PT_U8, b"\x55", 1)
b4[n4 - 1] ^= 0xFF  # corrupt the checksum
dec5 = FrameDecoder(max_payload=32)
frames = list(dec5.feed(b4[:n4]))
check(len(frames) == 0, "0 frames yielded for bad checksum")


# ---- Test 6: garbage prefix → resync ----------------------------------------
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
print("Testing: Clock")
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
print("Testing: Common register installation")
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
print("Testing: OperationControl")
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
print("Testing: OperationControl DUMP behaviour")
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
print("Testing: Dispatcher MUTE_REPLIES")
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
print("Testing: EventSource")
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
# Core registers — spec semantics (OPERATION_CTRL, RESET_DEV, DEVICE_NAME,
# CLOCK_CONFIG, UID, TAG, HEARTBEAT, VERSION) + NVM
# ===========================================================================
print("Testing: Core register semantics")
import tempfile
from microharp.registers import (
    R_DEVICE_NAME,
    R_CLOCK_CONFIG,
    R_UID,
    R_TAG,
    RST_DEF,
    RST_EE,
    RST_SAVE,
    RST_NAME_TO_DEFAULT,
    RST_BOOT_DEF,
    RST_BOOT_EE,
    CLK_LOCK,
    CLK_UNLOCK,
    CLK_GEN,
    HB_IS_ACTIVE,
    HB_IS_SYNCHRONIZED,
    NONVOLATILE,
)
from microharp.nvm import Nvm, apply_boot_state
from microharp.heartbeat import heartbeat_task


def make_core(nvm=None, **kw):
    """Fresh bank + dispatcher.  Returns (bank, disp, slabs, tx_q, clock, reboots)."""
    c = Clock()
    b = RegisterBank()
    s = SlabPool(count=64, size=64)
    tq = asyncio.Queue(64)
    reboots = []
    install_common_registers(b, clock=c, tx_q=tq, slab_pool=s, nvm=nvm, reboot=lambda: reboots.append(1), **kw)
    d = Dispatcher(b, c, asyncio.Queue(4), tq, s)  # type: ignore[arg-type]
    return b, d, s, tq, c, reboots


def drain(s, tq):
    """Pop every queued frame → list of (msg_type, address, payload bytes)."""
    out = []
    while tq.qsize():
        idx = tq.get_nowait()
        mv = memoryview(s.buf(idx))[: s.length(idx)]
        mt, addr, _, _, _, _, _, po, pl = parse_header(mv)
        out.append((mt, addr, bytes(mv[po : po + pl])))
        s.release(idx)
    return out


async def _request(d, s, tq, msg, addr, pt, payload):
    buf = bytearray(64)
    n = encode_into(buf, msg, addr, 255, pt, payload=payload, payload_len=len(payload) if payload else 0)
    idx = await s.lease()
    s.buf(idx)[:n] = buf[:n]
    await d._handle_one(s.buf(idx), n)
    s.release(idx)
    # Play the TX task: deferred reboots wait for the reply to drain.
    out = drain(s, tq)
    await asyncio.sleep(0.12)
    return out


def request(core, msg, addr, pt, payload=None):
    b, d, s, tq, _, _ = core
    return run(_request(d, s, tq, msg, addr, pt, payload))


W_ERR = MSG_WRITE | MSG_FLAG_ERROR
R_ERR = MSG_READ | MSG_FLAG_ERROR

# ---- OPERATION_CTRL ---------------------------------------------------------
core = make_core()
bk = core[0]
opr = bk.get(R_OPERATION_CONTROL)
assert opr is not None
for bad_mode in (2, 3):
    r = request(core, MSG_WRITE, R_OPERATION_CONTROL, PT_U8, bytes([OP_DEFAULT | bad_mode]))
    check(r and r[0][0] == W_ERR, "OP_MODE=%d → WRITE error" % bad_mode)
    check(opr.storage[0] == OP_DEFAULT, "OP_MODE=%d → register unchanged" % bad_mode)

r = request(core, MSG_WRITE, R_OPERATION_CONTROL, PT_U8, bytes([OP_DEFAULT | OP_DUMP]))
op_reads = [p for (mt, a, p) in r if mt == MSG_READ and a == R_OPERATION_CONTROL]
check(r[0][0] == MSG_WRITE, "DUMP → WRITE reply first")
check(len(op_reads) == 1 and not (op_reads[0][0] & OP_DUMP), "DUMP → dumped OperationControl has DUMP clear")
check(any(a == R_RESET_DEV for (_, a, _) in r), "DUMP includes R_RESET_DEV (now readable)")

opr.storage[0] |= OP_MUTE_REPLIES
r = request(core, MSG_WRITE, R_OPERATION_CONTROL, PT_U8, bytes([OP_DEFAULT | OP_DUMP | OP_MUTE_REPLIES]))
check(r == [], "DUMP while muted → nothing sent")
check(not (opr.storage[0] & OP_DUMP), "DUMP while muted → bit still cleared")
opr.storage[0] = OP_DEFAULT

# ---- CLOCK_CONFIG -------------------------------------------------------------
cc = bk.get(R_CLOCK_CONFIG)
assert cc is not None
check(cc.storage[0] == CLK_UNLOCK, "ClockConfig boots unlocked (0x40)")
r = request(core, MSG_WRITE, R_CLOCK_CONFIG, PT_U8, bytes([CLK_LOCK]))
check(r[0] == (MSG_WRITE, R_CLOCK_CONFIG, bytes([CLK_LOCK])), "LOCK → reply shows CLK_LOCK only")
r = request(core, MSG_WRITE, R_TIMESTAMP_SECOND, PT_U32, struct.pack("<I", 1234))
check(r[0][0] == W_ERR, "TimestampSecond write while locked → error")
check(core[4].now()[0] != 1234, "TimestampSecond write while locked → clock untouched")
r = request(core, MSG_WRITE, R_CLOCK_CONFIG, PT_U8, bytes([CLK_UNLOCK | CLK_GEN]))
check(r[0][2] == bytes([CLK_UNLOCK]), "UNLOCK|CLK_GEN → unlocked, CLK_GEN ignored (no clock output)")
r = request(core, MSG_WRITE, R_TIMESTAMP_SECOND, PT_U32, struct.pack("<I", 1234))
check(r[0][0] == MSG_WRITE and core[4].now()[0] == 1234, "TimestampSecond write while unlocked → applied")
r = request(core, MSG_WRITE, R_CLOCK_CONFIG, PT_U8, bytes([CLK_LOCK | CLK_UNLOCK]))
check(r[0][0] == W_ERR, "LOCK|UNLOCK together → error")

# ---- RESET_DEV without NVM ----------------------------------------------------
r = request(core, MSG_READ, R_RESET_DEV, PT_U8)
check(r[0] == (MSG_READ, R_RESET_DEV, bytes([RST_BOOT_DEF])), "ResetDev READ → BOOT_DEF (no NVM)")
for bad in (RST_BOOT_DEF, RST_BOOT_EE, 0x10, RST_DEF | RST_SAVE, RST_SAVE, RST_EE):
    r = request(core, MSG_WRITE, R_RESET_DEV, PT_U8, bytes([bad]))
    check(r[0][0] == W_ERR, "ResetDev write 0x%02X without NVM → error" % bad)
check(core[5] == [], "no reboot after rejected ResetDev writes")
r = request(core, MSG_WRITE, R_RESET_DEV, PT_U8, bytes([RST_DEF]))
check(r[0][0] == MSG_WRITE, "RST_DEF → WRITE reply")
check(core[5] == [1], "RST_DEF → reboot after reply")

# ---- DEVICE_NAME without NVM --------------------------------------------------
core = make_core(device_name=b"default")
dn_payload = b"renamed".ljust(25, b"\x00")
r = request(core, MSG_WRITE, R_DEVICE_NAME, PT_U8, dn_payload)
check(r[0][0] == MSG_WRITE and r[0][2].rstrip(b"\x00") == b"default", "DeviceName write w/o NVM → reply with default")
check(core[5] == [], "DeviceName write w/o NVM → no reboot")

# ---- UID / TAG / VERSION ------------------------------------------------------
core = make_core(
    uid=b"\x01\x02\x03",
    tag=b"abc1234",
    fw_major=2,
    fw_minor=3,
    fw_patch=4,
    hw_patch=7,
    harp_patch=1,
    core_id=b"MPY",
    interface_hash=bytes(range(20)),
)
bk = core[0]
check(bytes(bk.get(R_UID).storage) == b"\x01\x02\x03" + bytes(13), "UID zero-padded to 16 bytes")  # type: ignore[union-attr]
check(bytes(bk.get(R_TAG).storage) == b"abc1234\x00", "TAG zero-padded to 8 bytes")  # type: ignore[union-attr]
v = bytes(bk.get(R_VERSION).storage)  # type: ignore[union-attr]
check(v[0:9] == bytes((1, 13, 1, 2, 3, 4, 1, 0, 7)), "VERSION protocol/firmware/hardware semver")
check(v[9:12] == b"MPY", "VERSION CORE_ID")
check(v[12:32] == bytes(range(20)), "VERSION INTERFACE_HASH")
try:
    make_core(tag=b"123456789")
    check(False, "TAG longer than 8 bytes raises")
except ValueError:
    check(True, "TAG longer than 8 bytes raises")

# ---- HEARTBEAT ----------------------------------------------------------------
core = make_core()
bk, clkh = core[0], core[4]
r = request(core, MSG_READ, R_HEARTBEAT, PT_U16)
check(r[0][2] == b"\x00\x00", "Heartbeat in Standby, unsynced → 0")
bk.get(R_OPERATION_CONTROL).storage[0] = OP_DEFAULT | OP_OP_MODE_ACTIVE  # type: ignore[union-attr]
clkh._synced = True
r = request(core, MSG_READ, R_HEARTBEAT, PT_U16)
check(
    struct.unpack("<H", r[0][2])[0] == HB_IS_ACTIVE | HB_IS_SYNCHRONIZED,
    "Heartbeat Active+synced → IS_ACTIVE(bit1)|IS_SYNCHRONIZED(bit0)",
)
check(HB_IS_SYNCHRONIZED == 0x01 and HB_IS_ACTIVE == 0x02, "Heartbeat bit positions per spec")


async def one_heartbeat(c, b, tq, s):
    t = asyncio.ensure_future(heartbeat_task(c, b, tq, s))
    await asyncio.sleep(0)
    c.tick.fire()
    await asyncio.sleep(0.01)
    t.cancel()


clkh._synced = False
run(one_heartbeat(clkh, bk, core[3], core[2]))
r = drain(core[2], core[3])
check(r == [(MSG_EVENT, R_HEARTBEAT, struct.pack("<H", HB_IS_ACTIVE))], "HEARTBEAT_EN → R_HEARTBEAT EVENT")

# ---- NVM ----------------------------------------------------------------------
tmpdir = tempfile.mkdtemp()
nvm = Nvm(os.path.join(tmpdir, "nvm.bin"))
core = make_core(nvm=nvm, device_name=b"default")
bk = core[0]
bk.add(RegisterEntry(40, PT_U16, access=READ_WRITE | NONVOLATILE, name="Gain"))
bk.add(RegisterEntry(41, PT_U8, access=READ_WRITE, name="Volatile"))
check(apply_boot_state(bk, nvm) is False, "no NVM image → defaults")
check(bk.get(R_RESET_DEV).storage[0] == RST_BOOT_DEF, "no NVM image → BOOT_DEF")  # type: ignore[union-attr]

r = request(core, MSG_WRITE, R_DEVICE_NAME, PT_U8, b"bench-3".ljust(25, b"\x00"))
check(r[0][2].rstrip(b"\x00") == b"bench-3", "DeviceName write w/ NVM → reply with new name")
check(nvm.read() == {R_DEVICE_NAME: b"bench-3".ljust(25, b"\x00")}, "DeviceName write w/ NVM → persisted")
check(core[5] == [1], "DeviceName write w/ NVM → reboot after reply")

bk.get(40).storage[:] = b"\x34\x12"  # type: ignore[union-attr]
bk.get(41).storage[0] = 9  # type: ignore[union-attr]
r = request(core, MSG_WRITE, R_RESET_DEV, PT_U8, bytes([RST_SAVE]))
check(r[0][0] == MSG_WRITE and core[5] == [1, 1], "SAVE w/ NVM → reply + reboot")
check(set(nvm.read()) == {R_DEVICE_NAME, 40}, "SAVE stores only NONVOLATILE registers")  # type: ignore[arg-type]

# "Reboot": fresh bank restores the saved values.
core2 = make_core(nvm=nvm, device_name=b"default")
bk2 = core2[0]
bk2.add(RegisterEntry(40, PT_U16, access=READ_WRITE | NONVOLATILE, name="Gain"))
bk2.add(RegisterEntry(41, PT_U8, access=READ_WRITE, name="Volatile"))
check(apply_boot_state(bk2, nvm) is True, "NVM image → booted from NVM")
check(bk2.get(R_RESET_DEV).storage[0] == RST_BOOT_EE, "NVM image → BOOT_EE")  # type: ignore[union-attr]
check(bytes(bk2.get(R_DEVICE_NAME).storage).rstrip(b"\x00") == b"bench-3", "NVM restores DeviceName")  # type: ignore[union-attr]
check(bytes(bk2.get(40).storage) == b"\x34\x12", "NVM restores NONVOLATILE app register")  # type: ignore[union-attr]
check(bk2.get(41).storage[0] == 0, "volatile app register keeps default")  # type: ignore[union-attr]

r = request(core2, MSG_WRITE, R_RESET_DEV, PT_U8, bytes([RST_EE]))
check(r[0][0] == MSG_WRITE and core2[5] == [1], "RST_EE w/ NVM → reply + reboot")
r = request(core2, MSG_WRITE, R_RESET_DEV, PT_U8, bytes([RST_NAME_TO_DEFAULT]))
check(r[0][0] == MSG_WRITE and set(nvm.read()) == {40}, "NAME_TO_DEFAULT drops stored name only")  # type: ignore[arg-type]
r = request(core2, MSG_WRITE, R_RESET_DEV, PT_U8, bytes([RST_DEF]))
check(r[0][0] == MSG_WRITE and nvm.read() is None, "RST_DEF erases NVM")
check(core2[5] == [1, 1, 1], "every reset action rebooted once")


# ===========================================================================
print()
print("Summary: %d pass, %d fail" % (ok, fail))
sys.exit(0 if fail == 0 else 1)
