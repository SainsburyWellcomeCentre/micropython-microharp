# microharp

[![GitHub release](https://img.shields.io/github/v/release/SainsburyWellcomeCentre/micropython-microharp?style=flat-square&cacheSeconds=3600)](https://github.com/SainsburyWellcomeCentre/micropython-microharp/releases)
[![GitHub issues](https://img.shields.io/github/issues/SainsburyWellcomeCentre/micropython-microharp?style=flat-square)](https://github.com/SainsburyWellcomeCentre/micropython-microharp/issues)

Async MicroPython implementation of the [Harp protocol](https://github.com/harp-tech/protocol):
8-bit binary message framing, sync clock discipline, register-based
device model, and timestamped events. Designed for low-jitter I/O on
RP2040/RP2350/ESP32 and any other MicroPython port with USB CDC or UART.

## Documentation

| Document                               | What it covers                                                |
| -------------------------------------- | ------------------------------------------------------------- |
| [TRANSPORTS.md](doc/TRANSPORTS.md)     | CDC / UART / custom transports + slab pool tuning.            |
| [TASKS.md](doc/TASKS.md)               | `@device.task` patterns, pin events, custom event sources.    |
| [API.md](doc/API.md)                   | Public API reference: `HarpDevice`, payload types, registers. |
| [ARCHITECTURE.md](doc/ARCHITECTURE.md) | Internals: clock sync, IRQ bridge, slab pool, performance.    |
| [TESTING.md](doc/TESTING.md)           | Running the CPython unit tests and adding new ones.           |

## Install

Install via [`mip`](https://docs.micropython.org/en/latest/reference/packages.html)
(MicroPython's package manager). The dependency on `usb-device-cdc` is
declared in `package.json` and pulled in automatically:

```bash
mpremote mip install github:SainsburyWellcomeCentre/micropython-microharp
```

For a local development install, copy the package directory to the
board and one of the examples as `main.py`.

Note: `mpremote cp` does not resolve `package.json` dependencies, so
install `usb-device-cdc` explicitly first:

```bash
mpremote mip install usb-device-cdc
mpremote cp -r microharp/ :
mpremote cp example/example_secondary_cdc.py :main.py
mpremote reset
```

For a fully offline install, vendor the dependency on-device first, then
use the copy commands above.

## Quick start

```python
import asyncio
from machine import Pin, UART
from usb.device.cdc import CDCInterface
import usb.device
from microharp import HarpDevice, CdcTransport, PT_U8

cdc = CDCInterface(baudrate=1_000_000, timeout=0, txbuf=2048, rxbuf=512)
usb.device.get().init(cdc, builtin_driver=True)

device = HarpDevice(
    transport     = CdcTransport(cdc),
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

Harp traffic runs on the secondary CDC (`/dev/ttyACM1`); the REPL stays
accessible on `/dev/ttyACM0`. Requires MicroPython ≥ 1.23 with the
`usb.device.cdc` module (pulled in automatically by `mip install`).

> **Why not `sys.stdin` / `sys.stdout`?**  
> The REPL CDC intercepts `0x03` (Ctrl-C → `KeyboardInterrupt`) and
> `0x04` (Ctrl-D → soft reset) at the C firmware level, before MicroPython’s
> Python layer ever sees the byte. `PT_U32 = 0x04`, so any message
> targeting a U32 register would instantly reset the device. DTR assertion
> when the host opens the port also triggers a firmware reset. These are
> impossible to intercept from Python. `CdcTransport` uses a separate
> CDC interface that has none of these restrictions.
> See [doc/TRANSPORTS.md](doc/TRANSPORTS.md) for details.

## Layout

```text
microharp/
  __init__.py     public API
  framing.py      encode/decode + viper checksum
  clock.py        Clock + sync_task (UART RX of sync packets)
  transport.py    Cdc / Uart / generic Stream transports + slab pool
  registers.py    RegisterEntry, RegisterBank, common registers 0..19
  dispatch.py     routes parsed frames to register handlers
  events.py       IRQ ring + EventSource (timestamp-at-IRQ)
  led.py          status LED task, clock-synchronized when locked
  heartbeat.py    1 Hz heartbeat EVENT when enabled
  device.py       HarpDevice — top-level orchestrator + HOFs
  queue.py        small asyncio queue
example/          seven runnable examples (see below)
test/             CPython unit tests + host-side benchmark (benchmark.py)
doc/              documentation (TRANSPORTS, TASKS, API, ARCHITECTURE, TESTING)
package.json      mip metadata + dependency declaration
```

## Examples

Seven `example_*.py` files in [`example/`](example/) cover the common cases:

| File                           | What it shows                                                                    |
| ------------------------------ | -------------------------------------------------------------------------------- |
| `example_secondary_cdc.py`     | Minimal setup — `CdcTransport`, digital input EVENT, REPL on `/dev/ttyACM0`.     |
| `example_uart.py`              | Hardware UART transport (FTDI / device-to-device).                               |
| `example_periodic_event.py`    | 10 Hz ADC → EVENT via a `@device.task` loop.                                     |
| `example_custom_task.py`       | Three `@device.task` patterns: plain loop, per-second tick, task with arguments. |
| `example_button_controller.py` | Button spawns/cancels a worker task — declarative `task=` API.                   |
| `example_3state_timeout.py`    | Three-state sequence with per-state timeouts and error EVENTs.                   |
| `example_motor_chain.py`       | Motor-control sequence with chained cancellation and sticky errors.              |

## Port matrix

| Port            |       Cdc       | Uart | Sync UART | Hard IRQ |
| --------------- | :-------------: | :--: | :-------: | :------: |
| RP2040 / RP2350 |  ✅ (MP ≥1.23)  |  ✅  |    ✅     |    ✅    |
| ESP32-S2 / S3   | ⚠ port-specific |  ✅  |    ✅     |    ⚠     |
| STM32 (Pyboard) |       n/a       |  ✅  |    ✅     |    ✅    |

ESP32 hard IRQs have stricter restrictions; `bind_pin_event` falls back
to soft IRQ semantics if `hard=True` raises. See
[doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) for the per-port story.

## License

**Sainsbury Wellcome Centre code, firmware, and software is released under the [BSD 3-Clause License](https://opensource.org/license/bsd-3-clause).**

## 📚 Credits

This library references the following resources:

- [harp-tech/protocol](https://github.com/harp-tech/protocol) — the official Harp protocol specification.

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## ❤ Contributors

 <a href = "https://github.com/SainsburyWellcomeCentre/micropython-microharp/graphs/contributors">
   <img src = "https://contrib.rocks/image?repo=SainsburyWellcomeCentre/micropython-microharp" alt="Contributors"/>
 </a>

## 📧 Contact

- **Author**: [Sainsbury Wellcome Centre FabLabs](https://www.sainsburywellcome.org/content/fablab)
- **Email**: [swc.fablabs@ucl.ac.uk](mailto:swc.fablabs@ucl.ac.uk)
- **Website**: [FabLabs](https://sainsburywellcomecentre.github.io/fablabs-documentation/#micropython-microharp)
