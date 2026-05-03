# microharp

Async MicroPython implementation of the [Harp protocol](https://github.com/harp-tech/protocol):
8-bit binary message framing, sync clock discipline, register-based
device model, and timestamped events. Designed for low-jitter I/O on
RP2040/RP2350/ESP32 and any other MicroPython port with USB CDC or UART.

## Documentation

| Document                           | What it covers                                                |
| ---------------------------------- | ------------------------------------------------------------- |
| [TRANSPORTS.md](TRANSPORTS.md)     | Stdio / CDC / UART / custom transports + slab pool tuning.    |
| [TASKS.md](TASKS.md)               | `@device.task` patterns, pin events, custom event sources.    |
| [API.md](API.md)                   | Public API reference: `HarpDevice`, payload types, registers. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Internals: clock sync, IRQ bridge, slab pool, performance.    |
| [TESTING.md](TESTING.md)           | Running the CPython unit tests and adding new ones.           |

## Install

Install via [`mip`](https://docs.micropython.org/en/latest/reference/packages.html)
(MicroPython's package manager). The dependency on `usb-device-cdc` is
declared in `package.json` and pulled in automatically:

```bash
mpremote mip install github:DCisHurt/micropython-microharp
```

For a local development install, copy the package directory to the
board and one of the examples as `main.py`.

Note: `mpremote cp` does not resolve `package.json` dependencies, so
install `usb-device-cdc` explicitly first:

```bash
mpremote mip install usb-device-cdc
mpremote cp -r microharp/ :
mpremote cp example/example_basic_stdio.py :main.py
mpremote reset
```

For a fully offline install, vendor the dependency on-device first, then
use the copy commands above.

## Quick start

```python
import asyncio
from machine import Pin, UART
from microharp import HarpDevice, StdioTransport, PT_U8

device = HarpDevice(
    transport     = StdioTransport(),
    sync_uart     = UART(1, baudrate=100_000, rx=Pin(5), timeout=0),
    led_pin       = Pin(25, Pin.OUT),
    who_am_i      = 1234,
    device_name   = b"my-harp",
)

button = Pin(14, Pin.IN, Pin.PULL_UP)

@device.on_read(address=32, payload_type=PT_U8, name="DigitalInput")
async def read_di(reg):
    reg.storage[0] = button.value()

device.bind_pin_event(button, address=32, payload_type=PT_U8,
                      trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING)

asyncio.run(device.run())
```

For more involved patterns, see [TASKS.md](TASKS.md) and the runnable
examples in [`example/`](example/).

## Layout

```text
microharp/
  __init__.py     public API
  framing.py      encode/decode + viper checksum
  clock.py        Clock + sync_task (UART RX of sync packets)
  transport.py    Stdio / Cdc / Uart / generic Stream transports + slab pool
  registers.py    RegisterEntry, RegisterBank, common registers 0..19
  dispatch.py     routes parsed frames to register handlers
  events.py       IRQ ring + EventSource (timestamp-at-IRQ)
  led.py          status LED task, clock-synchronized when locked
  heartbeat.py    1 Hz heartbeat EVENT when enabled
  device.py       HarpDevice — top-level orchestrator + HOFs
  queue.py        small asyncio queue
example/          five runnable examples (see below)
tests/            CPython unit tests + host-side benchmark (benchmark.py)
tools/            host-side helpers
package.json      mip metadata + dependency declaration
```

## Examples

Five `example_*.py` files in [`example/`](example/) cover the common cases:

| File                        | What it shows                                                                    |
| --------------------------- | -------------------------------------------------------------------------------- |
| `example_basic_stdio.py`    | Simplest setup — `StdioTransport` + a digital input EVENT.                       |
| `example_secondary_cdc.py`  | Production: REPL on `/dev/ttyACM0`, Harp on `/dev/ttyACM1`.                      |
| `example_uart.py`           | Hardware UART transport (FTDI / device-to-device).                               |
| `example_periodic_event.py` | 10 Hz ADC → EVENT via a `@device.task` loop.                                     |
| `example_custom_task.py`    | Three `@device.task` patterns: plain loop, per-second tick, task with arguments. |

## Port matrix

| Port            | Stdio |       Cdc       | Uart | Sync UART | Hard IRQ |
| --------------- | :---: | :-------------: | :--: | :-------: | :------: |
| RP2040 / RP2350 |  ✅   |  ✅ (MP ≥1.23)  |  ✅  |    ✅     |    ✅    |
| ESP32-S2 / S3   |  ✅   | ⚠ port-specific |  ✅  |    ✅     |    ⚠     |
| STM32 (Pyboard) |  ✅   |       n/a       |  ✅  |    ✅     |    ✅    |

ESP32 hard IRQs have stricter restrictions; `bind_pin_event` falls back
to soft IRQ semantics if `hard=True` raises. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the per-port story.
