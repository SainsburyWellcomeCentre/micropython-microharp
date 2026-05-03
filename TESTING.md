# Testing

CPython unit tests live in [`tests/`](tests/). They install small
`micropython`, `machine`, `time.ticks_*`, and `asyncio.ThreadSafeFlag`
shims so the package imports cleanly under CPython.

## Running the tests

From the repo root:

```bash
python -B test/test.py
```

The script exits non-zero on failure, so it is safe to run in CI.

## Coverage

### `test.py`

**Framing** (`microharp.framing`)

- `encode_into` / `FrameDecoder` / `parse_header` round-trips
- multi-frame buffers
- byte-by-byte feed
- bad-checksum drop
- garbage-prefix resync

**Clock** (`microharp.clock`)

- `Clock.set_seconds` (with `latency_us` compensation)
- the `R_TIMESTAMP_SECOND` write path

**OperationControl** (`microharp.registers`)

- default `OperationControl` value
- the `DUMP` enumeration handler
- `MUTE_REPLIES` suppressing ALL replies (WRITE, READ, errors) through the dispatcher

**EventSource** (`microharp.events`)

- events emitted in Active mode
- events silently dropped in Standby mode

## Adding a test

The shim layer is at the top of
[`test/test.py`](test/test.py). Copy the shim
block when adding tests for any module that touches `micropython.const`
or hardware-only APIs. Keep the shims minimal; don't pull in the full
`micropython-stubs` package.

## Benchmarking on a live device

[`test/benchmark.py`](test/benchmark.py) connects over USB CDC or UART and
measures round-trip latency using the device's own clock (not the PC clock),
so USB-driver scheduling jitter doesn't inflate the numbers.

```bash
pip install pyserial
python test/benchmark.py --port COM8          # Windows
python3 test/benchmark.py --port /dev/ttyACM0 # Linux/macOS
```
