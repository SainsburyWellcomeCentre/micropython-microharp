"""Host-side Harp debug + benchmark client.

Talks to a Harp device over a serial port (USB CDC or UART bridge).
Self-contained — only requires `pyserial`.

Run on Windows:
    pip install pyserial
    python tests/benchmark.py --port COM8

Run on Linux/macOS:
    python3 tests/benchmark.py --port /dev/ttyACM0

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


def fmt_payload(addr, payload):
    info = EXPECTED.get(addr)
    if info is None:
        return repr(payload)
    pt, name, fmt = info
    if fmt is None:
        # Array payload — show as ascii if printable, else hex.
        if all(32 <= b < 127 or b == 0 for b in payload):
            s = payload.split(b"\x00", 1)[0].decode("ascii", "replace")
            return "%r (%d bytes)" % (s, len(payload))
        return payload.hex(" ")
    elem_size = struct.calcsize(fmt)
    n = len(payload) // elem_size
    if n == 1:
        return str(struct.unpack(fmt, payload)[0])
    return ", ".join(str(struct.unpack_from(fmt, payload, i * elem_size)[0]) for i in range(n))


def banner(s):
    print("\n" + "=" * 60)
    print("  " + s)
    print("=" * 60)


# ---------------------------------------------------------------------------
# Test sections
# ---------------------------------------------------------------------------


def section_reset_clock(ser):
    """Force the device's TimestampSecond back to 0 at the start of the
    session.  Otherwise repeated test runs leave the clock at increasingly
    large values that look like the device has been booted for hours."""
    banner("0. Reset device clock to 0")
    rep, rtt = request_reply(ser, MSG_WRITE, R_TIMESTAMP_SECOND, struct.pack("<I", 0), PT_U32)
    if rep is None:
        print("  TIMEOUT — device not responding")
        return False
    assert rtt is not None
    if rep["msg_type"] & MSG_FLAG_ERROR:
        print(f"  ERROR (msg_type=0x{rep['msg_type']:02X}) — TimestampSecond may be locked")
        return False
    rep, _ = request_reply(ser, MSG_READ, R_TIMESTAMP_SECOND)
    if rep:
        got = struct.unpack("<I", rep["payload"])[0]
        print(f"  TimestampSecond: 0 → {got}  (rtt={rtt*1000:.2f} ms)")
    return True


def section_read_common(ser):
    banner("1. READ each common register")
    print("  cols: address  name  ts=(secs,ticks)  payload  [pc_rtt]")
    for addr in sorted(EXPECTED):
        info = EXPECTED[addr]
        rep, rtt = request_reply(ser, MSG_READ, addr)
        if rep is None:
            print(f"  addr 0x{addr:02X} {info[1]:<18s} TIMEOUT")
            continue
        assert rtt is not None
        if rep["msg_type"] & MSG_FLAG_ERROR:
            print(f"  addr 0x{addr:02X} {info[1]:<18s} ERROR (msg_type=0x{rep['msg_type']:02X})")
            continue
        print(
            f"  addr 0x{addr:02X} {info[1]:<18s} ts=({rep['secs']},{rep['ticks']:5d})  "
            f"{fmt_payload(addr, rep['payload'])}    "
            f"[rtt={rtt*1000:6.2f} ms]"
        )


def section_dump(ser):
    banner("2. DUMP — write OperationControl with DUMP bit, expect N replies")
    # Read current OperationControl first.
    rep, _ = request_reply(ser, MSG_READ, R_OPERATION_CONTROL)
    if rep is None or rep["msg_type"] & MSG_FLAG_ERROR:
        print("  could not read OperationControl; skipping dump test")
        return
    cur = rep["payload"][0]
    new_val = cur | OP_DUMP
    # Send the WRITE manually because we expect MULTIPLE replies.
    ser.reset_input_buffer()
    t0 = time.perf_counter()
    ser.write(encode(MSG_WRITE, R_OPERATION_CONTROL, 255, PT_U8, bytes([new_val])))
    ser.flush()
    seen = []
    deadline = time.time() + 3.0
    # Skip bad/event frames while waiting for dump replies.
    while time.time() < deadline:
        f = read_one_frame(ser, deadline)
        if f is None:
            break
        if f["msg_type"] == MSG_EVENT:
            continue
        seen.append(f)
        # Heuristic: stop when we've gotten the WRITE reply and at least
        # 18 READ replies (the common-register count) and the queue is idle
        # for 100 ms.
        if len(seen) >= 1 + len(EXPECTED):
            time.sleep(0.05)
            if ser.in_waiting == 0:
                break
    elapsed = time.perf_counter() - t0
    write_replies = sum(1 for f in seen if f["msg_type"] == MSG_WRITE)
    read_replies = sum(1 for f in seen if f["msg_type"] == MSG_READ)
    print(f"  total replies: {len(seen)}  ({write_replies} WRITE, {read_replies} READ)  " f"[{elapsed*1000:.1f} ms]")
    addrs = sorted({f["address"] for f in seen if f["msg_type"] == MSG_READ})
    expected_addrs = sorted(EXPECTED.keys())
    missing = [a for a in expected_addrs if a not in addrs]
    extra = [a for a in addrs if a not in expected_addrs]
    if missing:
        print(f"  missing addresses: {[hex(a) for a in missing]}")
    if extra:
        print(f"  extra addresses (app registers?): {[hex(a) for a in extra]}")
    if not missing and not extra:
        print(f"  ✓ all {len(expected_addrs)} common registers came back")
    # Verify DUMP bit auto-cleared.
    rep, _ = request_reply(ser, MSG_READ, R_OPERATION_CONTROL)
    if rep and not (rep["payload"][0] & OP_DUMP):
        print("  ✓ DUMP bit auto-cleared after dump")
    else:
        print("  ✗ DUMP bit did NOT auto-clear")


def _stats_us(label, xs):
    """Percentile table for a list of microsecond values."""
    if not xs:
        print(f"  {label:<32s} no samples")
        return
    xs_s = sorted(xs)
    m = len(xs_s)
    mean = sum(xs_s) / m
    print(
        f"  {label:<32s} n={m:4d}  mean={mean:8.1f}  "
        f"p50={xs_s[m//2]:8.1f}  p95={xs_s[int(m*0.95)]:8.1f}  "
        f"p99={xs_s[int(m*0.99)]:8.1f}  max={xs_s[-1]:8.1f}  (µs)"
    )


def _stats_ms(label, xs):
    """Percentile table for a list of second values, displayed as ms."""
    if not xs:
        print(f"  {label:<32s} no samples")
        return
    xs_s = sorted(xs)
    m = len(xs_s)
    mean = sum(xs_s) / m
    print(
        f"  {label:<32s} n={m:4d}  mean={mean*1000:7.2f}  "
        f"p50={xs_s[m//2]*1000:7.2f}  p95={xs_s[int(m*0.95)]*1000:7.2f}  "
        f"p99={xs_s[int(m*0.99)]*1000:7.2f}  max={xs_s[-1]*1000:7.2f}  (ms)"
    )


def section_clock_read_benchmark(ser, n=200):
    """Benchmark READ round-trip latency using the device's own clock.

    Latency is computed as the difference between consecutive device
    timestamps (harp_us(reply[i+1]) - harp_us(reply[i])).  This measures
    exactly the time the device observed between finishing request i and
    finishing request i+1 — no PC clock involved, no USB-jitter inflation.
    """
    banner(f"3. Clock READ benchmark ({n} round trips)")
    print("  Latency = consecutive device-timestamp deltas (µs).")
    print("  PC round-trip shown separately for reference.")
    print()

    # Send n+1 requests so we get n inter-reply intervals.
    device_rtts = []  # harp_us[i+1] - harp_us[i]
    pc_rtts = []  # PC-side wall-clock RTT (reference only)
    prev_us = None

    for _ in range(n + 1):
        rep, rtt = request_reply(ser, MSG_READ, R_TIMESTAMP_SECOND)
        if rep is None or rep["msg_type"] & MSG_FLAG_ERROR:
            prev_us = None  # gap — don't diff across it
            continue
        cur_us = harp_us(rep)
        pc_rtts.append(rtt)
        if prev_us is not None:
            device_rtts.append(cur_us - prev_us)
        prev_us = cur_us

    print("  -- device-clock inter-reply interval --")
    _stats_us("R_TIMESTAMP_SECOND", device_rtts)
    print("  -- PC round-trip (reference only) --")
    _stats_ms("R_TIMESTAMP_SECOND", pc_rtts)


def section_clock_write_benchmark(ser, n=50):
    """Benchmark WRITE round-trip latency using the device's own clock.

    R_CLOCK_CONFIG is used (not R_TIMESTAMP_SECOND) because writing
    TimestampSecond re-anchors the device clock, which would corrupt
    inter-reply deltas with discontinuous jumps.  R_CLOCK_CONFIG is a
    plain R/W U8 with no on_write side-effects — the dispatcher copies
    the byte into storage and replies immediately, keeping the clock
    free-running so deltas remain meaningful.

    Latency = harp_us(reply[i+1]) - harp_us(reply[i]), same principle
    as the READ benchmark — no PC clock involved.
    """
    banner(f"4. WRITE round-trip benchmark ({n} round trips, R_CLOCK_CONFIG)")
    print("  Latency = consecutive device-timestamp deltas (µs).")
    print("  PC round-trip shown separately for reference.")
    print()

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

    print("  -- device-clock inter-reply interval --")
    _stats_us("R_CLOCK_CONFIG WRITE", device_rtts)
    print("  -- PC round-trip (reference only) --")
    _stats_ms("R_CLOCK_CONFIG WRITE", pc_rtts)


def section_active_listen(ser, seconds=5.0):
    banner(f"5. Active mode — listen for events / heartbeat ({seconds:.0f} s)")
    # Re-zero the clock so heartbeat timestamps in this section read out
    # as a small, easy-to-eyeball number.  Skipped silently if the device
    # rejects the write (e.g. CLK_LOCK is engaged).
    request_reply(ser, MSG_WRITE, R_TIMESTAMP_SECOND, struct.pack("<I", 0), PT_U32)
    # Set OP_MODE = ACTIVE, keep VISUAL/HEARTBEAT enabled.
    new_op = OP_ACTIVE | OP_HEARTBEAT_EN | OP_VISUAL_EN | OP_OPLED_EN | OP_ALIVE_EN
    rep, _ = request_reply(ser, MSG_WRITE, R_OPERATION_CONTROL, bytes([new_op]), PT_U8)
    if rep is None:
        print("  could not enter Active mode; skipping")
        return
    counts = {}
    deadline = time.time() + seconds
    while time.time() < deadline:
        f = read_one_frame(ser, deadline)
        if f is None or f["msg_type"] == MSG_EVENT:
            continue
        a: int = f["address"]
        counts[a] = counts.get(a, 0) + 1
        if counts[a] <= 10:  # show first 10 of each
            payload_bytes: bytes = f["payload"]
            print(
                f"  EVT addr=0x{a:02X} ({EXPECTED.get(a, (None, '?', None))[1]:>16s}) "
                f"ts=({f['secs']:>5d},{f['ticks']:>5d}) payload={payload_bytes.hex(' ')}"
            )
    print("  totals: " + ", ".join(f"0x{a:02X}={n}" for a, n in sorted(counts.items())))


def section_restore_standby(ser):
    banner("6. Restoring Standby + reset clock to 0 + clearing reply-mute")
    rep, _ = request_reply(ser, MSG_READ, R_OPERATION_CONTROL)
    if rep is None or rep["msg_type"] & MSG_FLAG_ERROR:
        return
    cur = rep["payload"][0]
    new = cur & ~(OP_OP_MODE_MASK | OP_MUTE_REPLIES)  # OP_MODE = STANDBY
    rep, _ = request_reply(ser, MSG_WRITE, R_OPERATION_CONTROL, bytes([new]), PT_U8)
    if rep:
        print(f"  OperationControl: 0x{cur:02X} → 0x{rep['payload'][0]:02X}")
    # Leave the clock at zero so the next session starts from a known
    # baseline (and so a stale large value isn't confused with sync drift).
    rep, _ = request_reply(ser, MSG_WRITE, R_TIMESTAMP_SECOND, struct.pack("<I", 0), PT_U32)
    if rep and not (rep["msg_type"] & MSG_FLAG_ERROR):
        print("  TimestampSecond reset to 0")


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

    print(f"opening {args.port} @ {args.baud} baud …")
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
