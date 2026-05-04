"""Harp 8-bit binary protocol framing and codec.

Frame layout (per harp-tech/protocol/BinaryProtocol-8bit.md):

    [MessageType] [Length] [Address] [Port] [PayloadType]
    [Timestamp(6) optional] [Payload(N)] [Checksum]

`Length` = number of bytes from `Address` (inclusive) through `Checksum`
(inclusive).  Total wire size = 2 + Length.

`Checksum` = sum of every preceding byte modulo 256.

Allocation policy:
- `encode_into` writes into a caller-supplied `bytearray` and never allocates.
- `FrameDecoder` reuses a single internal buffer and yields `memoryview`s.

Performance notes:
- The per-byte checksum is implemented in viper (`_checksum_viper`) so it
  runs as native machine code on all viper-capable ports (RP2/ESP32/STM32).
  Falls back to a plain Python loop on ports where viper is unavailable.
- `encode_into` and `FrameDecoder.feed` are decorated `@_native`
  so the Python-level overhead between bulk operations is also compiled.
- Module-level integers are wrapped in `const()` so they're inlined by the
  compiler rather than looked up as module attributes at every reference.
"""

import micropython
from micropython import const
import struct

try:
    ptr8  # type: ignore[used-before-def]  # defined by viper at runtime
except NameError:
    # Fallback for static analysis and non-viper contexts.
    class ptr8:  # type: ignore[no-redef]
        def __init__(self, _addr):
            pass


# `native` and `viper` may be missing on stripped-down MicroPython builds
# or on a CPython test harness.  Fall back to no-op decorators so the
# module still imports.  The Python-level fallback for `_checksum` below
# handles the case where the viper *body* is unparseable.
_native = getattr(micropython, "native", lambda fn: fn)

# ---- MessageType byte values ------------------------------------------------
MSG_READ = const(1)
MSG_WRITE = const(2)
MSG_EVENT = const(3)
MSG_FLAG_ERROR = const(0x08)  # OR with the request type for an error reply
# Convenience:
MSG_READ_ERROR = const(MSG_READ | MSG_FLAG_ERROR)  # 9
MSG_WRITE_ERROR = const(MSG_WRITE | MSG_FLAG_ERROR)  # 10

# ---- PayloadType bits -------------------------------------------------------
PT_U8 = const(0x01)
PT_S8 = const(0x81)
PT_U16 = const(0x02)
PT_S16 = const(0x82)
PT_U32 = const(0x04)
PT_S32 = const(0x84)
PT_U64 = const(0x08)
PT_S64 = const(0x88)
PT_FLOAT = const(0x44)

PT_HAS_TIMESTAMP = const(0x10)


# Element width in bytes is encoded in the lower 4 bits of payload type.
def payload_elem_size(payload_type: int) -> int:
    low = payload_type & 0x0F
    if low == 1:
        return 1
    if low == 2:
        return 2
    if low == 4:
        return 4
    if low == 8:
        return 8
    raise ValueError("bad payload type")


# ---- Scalar / sequence packing ---------------------------------------------
#
# Helper for `device.emit(addr, value)` where `value` is a Python int / float
# / sequence rather than a pre-built bytes-like.  Returns a fresh bytearray
# sized to one element of `payload_type` (or N elements when `value` is a
# sequence).  Allocations here are unavoidable for the convenience API; if
# you need an alloc-free emit path, build the bytearray once and pass it.
_PT_FMT = {
    PT_U8:    "<B",
    PT_S8:    "<b",
    PT_U16:   "<H",
    PT_S16:   "<h",
    PT_U32:   "<I",
    PT_S32:   "<i",
    PT_U64:   "<Q",
    PT_S64:   "<q",
    PT_FLOAT: "<f",
}


def _pack_value(value, payload_type: int) -> bytes:
    fmt = _PT_FMT.get(payload_type & 0xCF)  # mask off PT_HAS_TIMESTAMP
    if fmt is None:
        raise ValueError("bad payload type for packing: 0x%02x" % payload_type)
    if isinstance(value, (list, tuple)):
        return struct.pack("<" + fmt[1] * len(value), *value)
    return struct.pack(fmt, value)


# ---- Checksum (viper if available) -----------------------------------------
#
# Viper compiles to native machine code with C-style integer typing.  No
# Python objects live inside the loop body, so the per-byte cost drops to
# a few cycles vs. the bytecode interpreter's ~200 ns per iteration.


def _checksum_python(buf: bytearray, n: int) -> int:
    cs = 0
    for i in range(n):
        cs += buf[i]
    return cs & 0xFF


try:

    @micropython.viper
    def _checksum(buf: ptr8, n: int) -> int:
        cs: int = 0
        i: int = 0
        while i < n:
            cs += int(buf[i])  # type: ignore[index]
            i += 1
        return cs & 0xFF

except (AttributeError, NameError, SyntaxError):
    # No viper on this build (or running under CPython for tests).
    _checksum = _checksum_python


# ---- Encoder ----------------------------------------------------------------


@_native
def encode_into(
    buf: bytearray, msg_type: int, address: int, port: int, payload_type: int, payload=None, payload_len: int = 0, ts_seconds: int = None, ts_ticks: int = None
) -> int:
    """Write a Harp frame into `buf` starting at offset 0.

    `payload` may be bytes/bytearray/memoryview, or None when payload_len == 0.
    Pass `ts_seconds` and `ts_ticks` to embed a timestamp; otherwise omit both.

    Returns the total byte count written (header + ts + payload + checksum).
    """
    has_ts = ts_seconds is not None
    pt = payload_type | (PT_HAS_TIMESTAMP if has_ts else 0)
    ts_bytes = 6 if has_ts else 0

    # Length field = bytes from Address through Checksum
    length = 1 + 1 + 1 + ts_bytes + payload_len + 1  # addr + port + pt + ts + payload + cs

    buf[0] = msg_type
    buf[1] = length
    buf[2] = address
    buf[3] = port
    buf[4] = pt

    idx = 5
    if has_ts:
        struct.pack_into("<IH", buf, idx, ts_seconds & 0xFFFFFFFF, (ts_ticks or 0) & 0xFFFF)
        idx += 6

    if payload_len and payload is not None:
        # memoryview slice avoids a copy when payload is already a memoryview
        buf[idx : idx + payload_len] = payload[:payload_len]
        idx += payload_len

    buf[idx] = _checksum(buf, idx)
    return idx + 1


# ---- Streaming decoder ------------------------------------------------------

# State constants (module-level so they can be `const`).
_S_TYPE = const(0)
_S_LEN = const(1)
_S_BODY = const(2)


class FrameDecoder:
    """Single-pass byte-fed parser.

    Usage:
        dec = FrameDecoder(max_payload=64)
        for frame_mv in dec.feed(rx_bytes):
            handle(frame_mv)        # memoryview into internal buffer
    """

    __slots__ = ("_buf", "_mv", "_state", "_idx", "_target", "_max_total")

    def __init__(self, max_payload: int = 64):
        # Worst-case frame size: 5 header + 6 ts + payload + 1 cs
        self._max_total = max_payload + 12
        self._buf = bytearray(self._max_total)
        self._mv = memoryview(self._buf)
        self._state = _S_TYPE
        self._idx = 0
        self._target = 0

    def feed(self, data: bytearray):
        """Generator: yields memoryview slices for each completed valid frame.

        `data` is a bytes/bytearray/memoryview chunk.  The yielded memoryview
        is only valid until the next call to `feed`.
        """
        i = 0
        n = len(data)
        buf = self._buf
        while i < n:
            st = self._state
            if st == _S_TYPE:
                b = data[i]
                # Accept any of the four base operations (and their error variants).
                low = b & 0x07
                if low == MSG_READ or low == MSG_WRITE or low == MSG_EVENT:
                    buf[0] = b
                    self._idx = 1
                    self._state = _S_LEN
                i += 1
            elif st == _S_LEN:
                length = data[i]
                # Sanity: total frame must fit
                if length < 4 or length + 2 > self._max_total:
                    self._state = _S_TYPE  # resync
                    i += 1
                    continue
                buf[1] = length
                self._target = 2 + length
                self._idx = 2
                self._state = _S_BODY
                i += 1
            else:  # _S_BODY  -- bulk copy
                avail = n - i
                need = self._target - self._idx
                take = avail if avail < need else need
                buf[self._idx : self._idx + take] = data[i : i + take]
                self._idx += take
                i += take
                if self._idx == self._target:
                    end = self._target
                    if _checksum(buf, end - 1) == buf[end - 1]:
                        yield self._mv[:end]
                    self._state = _S_TYPE
                    self._idx = 0


@_native
def parse_header(frame_mv: memoryview):
    """Parse the header of a complete frame memoryview.

    Returns:
        (msg_type, address, port, payload_type, has_ts,
         ts_seconds, ts_ticks, payload_offset, payload_len)
    """
    msg_type = frame_mv[0]
    length = frame_mv[1]
    address = frame_mv[2]
    port = frame_mv[3]
    pt = frame_mv[4]
    has_ts = bool(pt & PT_HAS_TIMESTAMP)
    if has_ts:
        secs, ticks = struct.unpack_from("<IH", frame_mv, 5)
        po = 11
    else:
        secs = 0
        ticks = 0
        po = 5
    pl = (2 + length) - po - 1  # subtract checksum byte
    return msg_type, address, port, pt, has_ts, secs, ticks, po, pl
