# Transports

A _transport_ is the byte pipe between `HarpDevice` and the host. Pick one to
match your wiring; the rest of the API is identical.

| Class             | Backed by                                  | When to use                                                    |
| ----------------- | ------------------------------------------ | -------------------------------------------------------------- |
| `StdioTransport`  | `sys.stdin.buffer` / `sys.stdout.buffer`   | Fastest to prototype. Conflicts with the REPL on the same CDC. |
| `CdcTransport`    | `usb.device.cdc.CDCInterface()` (MP ≥1.23) | Production. REPL on `/dev/ttyACM0`, Harp on `/dev/ttyACM1`.    |
| `UartTransport`   | `machine.UART(...)`                        | No USB, or device-to-device link via FTDI.                     |
| `StreamTransport` | any object with `readinto` + `write`       | Custom transports (BLE, network bridge, ...).                  |

All four duck-type the same interface — `read_some(buf) -> int` and
`write(memoryview) -> None`. They use `asyncio.StreamReader` /
`StreamWriter` internally, so reads and writes wake on the port's native
poll/select mechanism rather than 1 ms polling. A `sleep_ms(0)` yield-loop
fallback is used only on objects that can't be wrapped.

## StdioTransport

```python
from microharp import HarpDevice, StdioTransport
device = HarpDevice(transport=StdioTransport(), ...)
```

Disable the REPL on the same interface or you'll see Harp framing
collide with terminal text. Use `CdcTransport` for production.

See [`example/example_basic_stdio.py`](example/example_basic_stdio.py).

## CdcTransport — secondary CDC interface

REPL stays on `/dev/ttyACM0`; Harp gets its own `/dev/ttyACM1`. Requires
MicroPython ≥ 1.23 with the `usb.device.cdc` module
(installed automatically as a `mip` dependency).

```python
from usb.device.cdc import CDCInterface
import usb.device
from microharp import HarpDevice, CdcTransport

cdc = CDCInterface(baudrate=1_000_000, timeout=0, txbuf=2048, rxbuf=512)
usb.device.get().init(cdc, builtin_driver=True)

device = HarpDevice(transport=CdcTransport(cdc), ...)
```

`HarpDevice` auto-installs a CDC line-state callback that drops
`OP_MODE` to STANDBY on DTR-high (the host has just opened the port), so
the host never sees stale events from a previous session.

See [`../example/example_secondary_cdc.py`](../example/example_secondary_cdc.py).

## UartTransport

No USB, or a device-to-device link via FTDI / USB-UART bridge. Pick a
baud rate that comfortably exceeds your event rate — 921 600+ recommended.

```python
from machine import Pin, UART
from microharp import HarpDevice, UartTransport

data_uart = UART(0, baudrate=921_600, tx=Pin(0), rx=Pin(1), timeout=0)
device = HarpDevice(transport=UartTransport(data_uart), ...)
```

Use `timeout=0` so reads are non-blocking — the StreamReader wraps it.

See [`../example/example_uart.py`](../example/example_uart.py).

## StreamTransport — custom backends

Anything with `readinto(buf) -> int` and `write(mv) -> None|int` plugs
in. Useful for BLE serial bridges, network tunnels, or test harnesses.

```python
from microharp import HarpDevice, StreamTransport
device = HarpDevice(transport=StreamTransport(my_stream), ...)
```

If your stream object exposes `fileno()`, the asyncio StreamReader
will register it for poll-based wake-up. Otherwise the transport falls
back to a non-blocking `readinto` + `sleep_ms(0)` yield loop.

## Slab pool

Outgoing frames are built into pre-allocated bytearrays leased from
`SlabPool`. The slab _index_ travels through the asyncio queues — the
bytearray itself is never copied. Steady-state TX is allocation-free.

Tune the pool only if you see pressure:

```python
device = HarpDevice(
    transport=...,
    slab_count=32,    # default 16
    slab_size=128,    # default 64; raise if you emit > ~50 B payloads
    rx_q_size=16,
    tx_q_size=32,
)
```

`tx_q.put` / `slab.lease` block (asynchronously) when full / exhausted.
For IRQ-time emission, use `lease_nowait()` which returns -1 instead of
awaiting — the [event source](TASKS.md) handles this for you.
