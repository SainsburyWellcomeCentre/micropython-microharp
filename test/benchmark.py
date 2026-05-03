"""Host-side Harp debug + benchmark client.

Talks to a Harp device over a serial port (USB CDC or UART bridge).
Self-contained — only requires `pyserial`.

Run on Windows:
    pip install pyserial
    python test/benchmark.py --port COM8

Run on Linux/macOS:
    python3 test/benchmark.py --port /dev/ttyACM0

What it does, in order:
    1.  Open the port and clear any pending bytes.
    2.  READ each common register; pretty-print the payload.
    3.  Write OperationControl with the DUMP bit; receive the dump and
        verify each register address came back exactly once.
    4.  Read R_TIMESTAMP_SECOND 200 times back-to-back and report latency
        as the difference between consecutive device timestamps (not the PC
        clock) so USB-driver scheduling jitter doesn't inflate the numbers.
    5.  Write R_CLOCK_CONFIG 50 times and measure write-path latency the
        same way — device clock before/after, no PC clock involved.
    6.  Set OperationControl to Active and listen for 5 seconds, printing
        any heartbeat and event messages that arrive.

Stop early with Ctrl-C; the script restores the device to Standby on exit
so subsequent debug sessions start clean.
"""

import argparse
import struct
import sys
import time
from contextlib import contextmanager
from typing import Any, Optional

try:
    import serial  # type: ignore[import-untyped]
except ImportError:
    sys.exit("This script needs pyserial: pip install pyserial")


# ---------------------------------------------------------------------------
# Harp protocol constants (matching microharp/framing.py)
# ---------------------------------------------------------------------------
MSG_READ = 1
MSG_WRITE = 2
MSG_EVENT = 3
MSG_FLAG_ERROR = 0x08
MSG_READ_ERROR = MSG_READ | MSG_FLAG_ERROR  # 9
MSG_WRITE_ERROR = MSG_WRITE | MSG_FLAG_ERROR  # 10

PT_U8 = 0x01
PT_S8 = 0x81
PT_U16 = 0x02
PT_S16 = 0x82
PT_U32 = 0x04
PT_S32 = 0x84
PT_FLOAT = 0x44
PT_HAS_TIMESTAMP = 0x10

# Common register addresses (matching harp/registers.py)
R_WHO_AM_I = 0x00
R_HW_VERSION_H = 0x01
R_HW_VERSION_L = 0x02
R_ASSEMBLY_VERSION = 0x03
R_HARP_VERSION_H = 0x04
R_HARP_VERSION_L = 0x05
R_FW_VERSION_H = 0x06
R_FW_VERSION_L = 0x07
R_TIMESTAMP_SECOND = 0x08
R_TIMESTAMP_MICRO = 0x09
R_OPERATION_CONTROL = 0x0A
R_RESET_DEV = 0x0B
R_DEVICE_NAME = 0x0C
R_SERIAL_NUMBER = 0x0D
R_CLOCK_CONFIG = 0x0E
R_TIMESTAMP_OFFSET = 0x0F
R_UID = 0x10
R_TAG = 0x11
R_HEARTBEAT = 0x12
R_VERSION = 0x13

# OperationControl bits
OP_OP_MODE_MASK = 0x03
OP_STANDBY = 0x00
OP_ACTIVE = 0x01
OP_HEARTBEAT_EN = 0x04
OP_DUMP = 0x08
OP_MUTE_REPLIES = 0x10
OP_VISUAL_EN = 0x20
OP_OPLED_EN = 0x40
OP_ALIVE_EN = 0x80

# Per-address payload-type expectations (matches install_common_registers).
EXPECTED = {
    R_WHO_AM_I: (PT_U16, "WhoAmI", "<H"),
    R_HW_VERSION_H: (PT_U8, "HwVersionH", "<B"),
    R_HW_VERSION_L: (PT_U8, "HwVersionL", "<B"),
    R_ASSEMBLY_VERSION: (PT_U8, "AssemblyVersion", "<B"),
    R_HARP_VERSION_H: (PT_U8, "CoreVersionH", "<B"),
    R_HARP_VERSION_L: (PT_U8, "CoreVersionL", "<B"),
    R_FW_VERSION_H: (PT_U8, "FwVersionH", "<B"),
    R_FW_VERSION_L: (PT_U8, "FwVersionL", "<B"),
    R_TIMESTAMP_SECOND: (PT_U32, "TimestampSecond", "<I"),
    R_TIMESTAMP_MICRO: (PT_U16, "TimestampTicks", "<H"),
    R_OPERATION_CONTROL: (PT_U8, "OperationControl", "<B"),
    R_RESET_DEV: (PT_U8, "ResetDevice", "<B"),
    R_DEVICE_NAME: (PT_U8, "DeviceName", None),  # array
    R_SERIAL_NUMBER: (PT_U16, "SerialNumber", "<H"),
    R_CLOCK_CONFIG: (PT_U8, "ClockConfig", "<B"),
    R_TIMESTAMP_OFFSET: (PT_U8, "TimestampOffset", "<B"),
    R_UID: (PT_U8, "UID", None),  # array
    R_TAG: (PT_U8, "Tag", None),  # array
    R_HEARTBEAT: (PT_U16, "Heartbeat", "<H"),
    R_VERSION: (PT_U8, "Version", None),  # array
}


# ---------------------------------------------------------------------------
# Frame encode / decode
# ---------------------------------------------------------------------------


def encode(msg_type, address, port, payload_type, payload=b""):
    """Build a Harp frame.  Read requests have empty payload."""
    has_ts = False  # host requests don't carry timestamps
    pt = payload_type | (PT_HAS_TIMESTAMP if has_ts else 0)
    body = bytes([address, port, pt]) + payload
    length = len(body) + 1  # +1 for checksum byte
    frame = bytes([msg_type, length]) + body
    cs = sum(frame) & 0xFF
    return frame + bytes([cs])


def decode_frame(buf: bytes) -> Optional[dict[str, Any]]:
    """Validate checksum and split a complete frame.  Returns dict.

    Frame layout: [type][len][addr][port][pt][ts(6) optional][payload][cs]
    Returns None on length or checksum errors.
    """
    if len(buf) < 6:
        return None
    cs = sum(buf[:-1]) & 0xFF
    if cs != buf[-1]:
        return None  # bad checksum — caller skips and resyncs
    pt = buf[4]
    has_ts = bool(pt & PT_HAS_TIMESTAMP)
    if has_ts:
        secs, ticks = struct.unpack_from("<IH", buf, 5)
        po = 11
    else:
        secs = ticks = 0
        po = 5
    payload = bytes(buf[po:-1])
    return {
        "msg_type": buf[0],
        "length": buf[1],
        "address": buf[2],
        "port": buf[3],
        "pt": pt,
        "has_ts": has_ts,
        "secs": secs,
        "ticks": ticks,
        "payload": payload,
    }


def read_one_frame(ser, deadline):
    """Read until one complete framed message arrives or `deadline` is hit.
    Returns the decoded dict, or None on timeout / framing error."""
    buf = bytearray()
    while time.time() < deadline:
        b = ser.read(2 - len(buf)) if len(buf) < 2 else b""
        if b:
            buf.extend(b)
            continue
        if len(buf) < 2:
            time.sleep(0.001)
            continue
        target = buf[1] + 2
        # Read the rest.
        while len(buf) < target and time.time() < deadline:
            chunk = ser.read(target - len(buf))
            if chunk:
                buf.extend(chunk)
        if len(buf) >= target:
            return decode_frame(bytes(buf[:target]))
    return None


def harp_us(reply):
    """Device timestamp expressed in microseconds (secs * 1e6 + ticks * 32).

    Ticks are 32 µs units.  The value wraps at ticks=65535 (~2.1 s), which
    is far longer than any expected round-trip, so inter-reply deltas are
    safe to compute without special wrap handling.
    """
    return reply["secs"] * 1_000_000 + reply["ticks"] * 32


def request_reply(
    ser: Any,
    msg_type: int,
    addr: int,
    payload: bytes = b"",
    payload_type: int = PT_U8,
    timeout: float = 1.0,
) -> tuple[Optional[dict[str, Any]], Optional[float]]:
    """Send one request, wait for the matching reply for `addr`.

    Returns (reply_dict, pc_rtt_seconds).
        pc_rtt_seconds = wall-clock round-trip time measured on the PC.

    For latency benchmarks, prefer computing deltas between consecutive
    device timestamps (harp_us(reply[i+1]) - harp_us(reply[i])) instead
    of using this value — the device clock is the authoritative reference
    and is unaffected by PC-side USB scheduling jitter.

    Returns (None, None) on timeout.
    """
    ser.reset_input_buffer()
    t0 = time.perf_counter()
    ser.write(encode(msg_type, addr, 255, payload_type, payload))
    ser.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        f = read_one_frame(ser, deadline)
        if f is None:
            return None, None
        if f["address"] == addr:
            return f, time.perf_counter() - t0
    return None, None


# ---------------------------------------------------------------------------
# Pretty-printers
# ---------------------------------------------------------------------------


_OP_MODE_NAMES = {0x00: "STANDBY", 0x01: "ACTIVE", 0x02: "RESERVED", 0x03: "RESERVED"}
_OP_FLAGS = [
    (0x04, "HEARTBEAT_EN"),
    (0x08, "DUMP"),
    (0x10, "MUTE_REPLIES"),
    (0x20, "VISUAL_EN"),
    (0x40, "OPLED_EN"),
    (0x80, "ALIVE_EN"),
]


def _fmt_op_control(val):
    mode = _OP_MODE_NAMES.get(val & 0x03, "?")
    parts = ["STANDBY=%s" % (mode == "STANDBY"), "ACTIVE=%s" % (mode == "ACTIVE")]
    parts += ["%s=%s" % (name, bool(val & mask)) for mask, name in _OP_FLAGS]
    return ", ".join(parts)


def _fmt_version_array(payload):
    """R_VERSION (U8[32]): PROTOCOL/FIRMWARE/HARDWARE each 3 bytes (maj,min,patch),
    then CORE_ID 3 ASCII chars, then INTERFACE_HASH 20 bytes SHA-1 LE."""
    if len(payload) < 12:
        return payload.hex(" ")
    proto  = "%d.%d.%d" % (payload[0], payload[1], payload[2])
    fw     = "%d.%d.%d" % (payload[3], payload[4], payload[5])
    hw     = "%d.%d.%d" % (payload[6], payload[7], payload[8])
    try:
        core_id = bytes(payload[9:12]).decode("ascii").rstrip("\x00")
    except Exception:
        core_id = payload[9:12].hex()
    parts = ["PROTOCOL=%s" % proto, "FIRMWARE=%s" % fw,
             "HARDWARE=%s" % hw, "CORE_ID=%s" % core_id]
    if len(payload) >= 32:
        ih = bytes(payload[12:32])
        if any(ih):
            parts.append("INTERFACE_HASH=%s" % ih.hex())
    return "  ".join(parts)


def _fmt_heartbeat(val):
    """R_HEARTBEAT (U16): IS_ACTIVE [bit 0], IS_SYNCHRONIZED [bit 1]."""
    return "IS_ACTIVE=%s  IS_SYNCHRONIZED=%s" % (
        bool(val & 0x01), bool(val & 0x02)
    )


def fmt_payload(addr, payload):
    info = EXPECTED.get(addr)
    if info is None:
        return repr(payload)
    pt, name, fmt = info

    # Register-specific decoders.
    if addr == R_OPERATION_CONTROL and payload:
        return _fmt_op_control(payload[0])
    if addr == R_VERSION:
        return _fmt_version_array(payload)
    if addr == R_TIMESTAMP_SECOND and fmt:
        val = struct.unpack(fmt, payload)[0]
        return "%d s" % val
    if addr == R_TIMESTAMP_MICRO and fmt:
        val = struct.unpack(fmt, payload)[0]
        return "%d µs" % (val * 32)
    if addr == R_HEARTBEAT and fmt:
        val = struct.unpack(fmt, payload)[0]
        return _fmt_heartbeat(val)
    if addr in (R_HW_VERSION_H, R_HW_VERSION_L, R_FW_VERSION_H, R_FW_VERSION_L,
                R_HARP_VERSION_H, R_HARP_VERSION_L, R_ASSEMBLY_VERSION) and fmt:
        return str(struct.unpack(fmt, payload)[0])

    if fmt is None:
        # Array payload — show as ASCII if printable, else hex.
        if all(32 <= b < 127 or b == 0 for b in payload):
            s = payload.split(b"\x00", 1)[0].decode("ascii", "replace")
            return "%r" % s
        return payload.hex(" ")

    elem_size = struct.calcsize(fmt)
    n = len(payload) // elem_size
    if n == 1:
        return str(struct.unpack(fmt, payload)[0])
    return ", ".join(str(struct.unpack_from(fmt, payload, i * elem_size)[0]) for i in range(n))


def banner(s):
    print("\n" + s)


# ---------------------------------------------------------------------------
# Test sections
# ---------------------------------------------------------------------------


def section_reset_clock(ser):
    rep, rtt = request_reply(ser, MSG_WRITE, R_TIMESTAMP_SECOND, struct.pack("<I", 0), PT_U32)
    if rep is None or rep["msg_type"] & MSG_FLAG_ERROR:
        return False
    return True


# Deprecated registers — still read for dump compliance but not shown in table.
DEPRECATED = {
    R_HW_VERSION_H, R_HW_VERSION_L, R_ASSEMBLY_VERSION,
    R_HARP_VERSION_H, R_HARP_VERSION_L,
    R_FW_VERSION_H, R_FW_VERSION_L,
    R_SERIAL_NUMBER, R_TIMESTAMP_OFFSET,
}


def _expand_heartbeat(val):
    return [("  IS_ACTIVE", str(bool(val & 0x01))), ("  IS_SYNCHRONIZED", str(bool(val & 0x02)))]


def _expand_version(payload):
    if len(payload) < 12:
        return [("  (raw)", payload.hex(" "))]
    rows = [
        ("  PROTOCOL", "%d.%d.%d" % (payload[0], payload[1], payload[2])),
        ("  FIRMWARE",  "%d.%d.%d" % (payload[3], payload[4], payload[5])),
        ("  HARDWARE",  "%d.%d.%d" % (payload[6], payload[7], payload[8])),
    ]
    try:
        core_id = bytes(payload[9:12]).decode("ascii").rstrip("\x00")
    except Exception:
        core_id = payload[9:12].hex()
    rows.append(("  CORE_ID", core_id))
    if len(payload) >= 32:
        ih = bytes(payload[12:32])
        if any(ih):
            rows.append(("  INTERFACE_HASH", ih.hex()))
    return rows


def _expand_op_control(val):
    mode = _OP_MODE_NAMES.get(val & 0x03, "?")
    rows = [("  STANDBY", str(mode == "STANDBY")), ("  ACTIVE", str(mode == "ACTIVE"))]
    rows += [("  " + name, str(bool(val & mask))) for mask, name in _OP_FLAGS]
    return rows


def section_read_common(ser):
    # (name, value, sub_rows) — sub_rows replaces the value cell when non-empty
    entries: list[tuple[str, str, list[tuple[str, str]]]] = []
    rtts = []

    for addr in sorted(EXPECTED):
        if addr in DEPRECATED:
            continue
        info = EXPECTED[addr]
        rep, rtt = request_reply(ser, MSG_READ, addr)
        if rep is None or rep["msg_type"] & MSG_FLAG_ERROR:
            continue
        assert rtt is not None
        rtts.append(rtt)
        payload = rep["payload"]
        if addr == R_OPERATION_CONTROL and payload:
            entries.append(("OperationControl", "", _expand_op_control(payload[0])))
        elif addr == R_HEARTBEAT and payload:
            val = struct.unpack("<H", payload)[0]
            entries.append(("Heartbeat", "", _expand_heartbeat(val)))
        elif addr == R_VERSION:
            entries.append(("Version", "", _expand_version(payload)))
        else:
            entries.append((info[1], fmt_payload(addr, payload), []))

    # Put DeviceName first.
    _ORDER = {"DeviceName": 0, "WhoAmI": 1}
    entries.sort(key=lambda e: (_ORDER.get(e[0], 2), e[0]))

    # Column width = widest top-level name OR widest sub-item name.
    all_names = [n for n, _, _ in entries] + [
        sub_n for _, _, subs in entries for sub_n, _ in subs
    ]
    col = max(len(n) for n in all_names) + 2

    banner("Registers")
    print(f"  {'Register':<{col}}  Value")
    print(f"  {'-'*col}  {'-'*40}")
    for name, val, subs in entries:
        if subs:
            print(f"  {name:<{col}}")
            for sub_n, sub_v in subs:
                print(f"  {sub_n:<{col}}  {sub_v}")
        else:
            print(f"  {name:<{col}}  {val}")
    print(f"\n  avg RTT: {_avg_ms(rtts):.2f} ms")


def section_dump(ser):
    rep, _ = request_reply(ser, MSG_READ, R_OPERATION_CONTROL)
    if rep is None or rep["msg_type"] & MSG_FLAG_ERROR:
        return
    cur = rep["payload"][0]
    new_val = cur | OP_DUMP
    ser.reset_input_buffer()
    t0 = time.perf_counter()
    ser.write(encode(MSG_WRITE, R_OPERATION_CONTROL, 255, PT_U8, bytes([new_val])))
    ser.flush()
    seen = []
    deadline = time.time() + 3.0
    while time.time() < deadline:
        f = read_one_frame(ser, deadline)
        if f is None:
            break
        if f["msg_type"] == MSG_EVENT:
            continue
        seen.append(f)
        if len(seen) >= 1 + len(EXPECTED):
            time.sleep(0.05)
            if ser.in_waiting == 0:
                break
    elapsed = time.perf_counter() - t0
    print(f"\nDump: {len(seen)} replies  [{(elapsed/len(seen)*1000):.1f} ms]")


def _avg_us(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def _avg_ms(xs):
    return sum(xs) / len(xs) * 1000 if xs else float("nan")


def section_clock_read_benchmark(ser, n=200):
    """"""
    device_rtts = []
    pc_rtts = []
    prev_us = None

    for _ in range(n + 1):
        rep, rtt = request_reply(ser, MSG_READ, R_TIMESTAMP_SECOND)
        if rep is None or rep["msg_type"] & MSG_FLAG_ERROR:
            prev_us = None
            continue
        cur_us = harp_us(rep)
        pc_rtts.append(rtt)
        if prev_us is not None:
            device_rtts.append(cur_us - prev_us)
        prev_us = cur_us

    banner(f"Read latency ({n} round trips)")
    print(f"  device avg: {_avg_us(device_rtts):.1f} µs")
    print(f"  PC avg:     {_avg_ms(pc_rtts)*1000:.1f} µs")


def section_clock_write_benchmark(ser, n=50):
    """"""
    payload = bytes([0])
    device_rtts = []
    pc_rtts = []
    prev_us = None

    for _ in range(n + 1):
        rep, rtt = request_reply(ser, MSG_WRITE, R_CLOCK_CONFIG, payload, PT_U8)
        if rep is None or rep["msg_type"] & MSG_FLAG_ERROR:
            prev_us = None
            continue
        cur_us = harp_us(rep)
        pc_rtts.append(rtt)
        if prev_us is not None:
            device_rtts.append(cur_us - prev_us)
        prev_us = cur_us

    banner(f"Write latency ({n} round trips)")
    print(f"  device avg: {_avg_us(device_rtts):.1f} µs")
    print(f"  PC avg:     {_avg_ms(pc_rtts)*1000:.1f} µs")


def section_active_listen(ser, seconds=5.0):
    banner(f"Active listen ({seconds:.0f} s)")
    new_op = OP_ACTIVE | OP_HEARTBEAT_EN | OP_VISUAL_EN | OP_OPLED_EN | OP_ALIVE_EN
    rep, _ = request_reply(ser, MSG_WRITE, R_OPERATION_CONTROL, bytes([new_op]), PT_U8)
    if rep is None:
        return
    counts = {}
    deadline = time.time() + seconds

    def _event_name(addr):
        if addr == R_TIMESTAMP_SECOND:
            return "Heartbeat"
        info = EXPECTED.get(addr)
        return info[1] if info is not None else f"Reg 0x{addr:02X}"

    def _event_payload_text(addr, payload):
        info = EXPECTED.get(addr)
        if info is not None:
            return fmt_payload(addr, payload)
        return payload.hex(" ")

    while time.time() < deadline:
        f = read_one_frame(ser, deadline)
        if f is None or f["msg_type"] != MSG_EVENT:
            continue
        a: int = f["address"]
        counts[a] = counts.get(a, 0) + 1
        if counts[a] <= 10:
            payload_bytes: bytes = f["payload"]
            name = _event_name(a)
            payload_text = _event_payload_text(a, payload_bytes)
            if payload_text:
                print(f"  {name:<18s} ts=({f['secs']},{f['ticks']:5d}) {payload_text}")
            else:
                print(f"  {name:<18s} ts=({f['secs']},{f['ticks']:5d})")

    print("  totals: " + ", ".join(f"{_event_name(a)}={n}" for a, n in sorted(counts.items())))


def section_restore_standby(ser):
    rep, _ = request_reply(ser, MSG_READ, R_OPERATION_CONTROL)
    if rep is None or rep["msg_type"] & MSG_FLAG_ERROR:
        return
    cur = rep["payload"][0]
    new = cur & ~(OP_OP_MODE_MASK | OP_MUTE_REPLIES)
    request_reply(ser, MSG_WRITE, R_OPERATION_CONTROL, bytes([new]), PT_U8)
    request_reply(ser, MSG_WRITE, R_TIMESTAMP_SECOND, struct.pack("<I", 0), PT_U32)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@contextmanager
def open_port(port, baud):
    ser = serial.Serial(port, baudrate=baud, timeout=0.05, write_timeout=1.0, rtscts=False, dsrdtr=False)
    # DTR high so the device's CDC line-state callback knows we're connected.
    ser.dtr = True
    time.sleep(0.1)
    try:
        yield ser
    finally:
        ser.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", default="COM8" if sys.platform == "win32" else "/dev/ttyACM0")
    p.add_argument("--baud", type=int, default=1_000_000, help="Ignored over USB CDC; required for UART transport")
    p.add_argument("--read-bench", type=int, default=200)
    p.add_argument("--write-bench", type=int, default=50)
    p.add_argument("--listen", type=float, default=5.0, help="Active-mode listen duration, seconds")
    p.add_argument("--skip-active", action="store_true", help="Don't enter Active mode (for read-only testing)")
    args = p.parse_args()

    with open_port(args.port, args.baud) as ser:
        try:
            # section_reset_clock(ser)
            section_read_common(ser)
            section_dump(ser)
            section_clock_read_benchmark(ser, args.read_bench)
            section_clock_write_benchmark(ser, args.write_bench)
            if not args.skip_active:
                section_active_listen(ser, args.listen)
        except KeyboardInterrupt:
            print("\ninterrupted — restoring Standby")
        finally:
            section_restore_standby(ser)


if __name__ == "__main__":
    main()
