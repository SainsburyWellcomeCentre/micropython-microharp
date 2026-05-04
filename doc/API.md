# API reference

Quick lookup for the public surface. For deeper explanations, follow the
links into the source — every module has a top-of-file docstring.

## Top-level imports

```python
from microharp import (
    HarpDevice,
    StdioTransport, CdcTransport, UartTransport, StreamTransport,
    Clock, EventSource,
    RegisterEntry, RegisterBank,
    PT_U8, PT_S8, PT_U16, PT_S16, PT_U32, PT_S32,
    PT_U64, PT_S64, PT_FLOAT, PT_HAS_TIMESTAMP,
    READ_ONLY, WRITE_ONLY, READ_WRITE, EVENT,
    MSG_READ, MSG_WRITE, MSG_EVENT, MSG_FLAG_ERROR,
)
```

## HarpDevice

Top-level orchestrator. Constructor sizes everything; helper methods add
registers, events, and tasks.

### Construction

```python
HarpDevice(
    transport,                  # StreamTransport-compatible
    *,
    sync_uart=None,             # machine.UART or None
    led_pin=None,               # machine.Pin or None
    who_am_i=0,
    fw_version=(1, 0),
    hw_version=(1, 0),
    device_name=b"harp-mpy",
    serial_number=0,
    slab_count=16,
    slab_size=64,
    rx_q_size=8,
    tx_q_size=16,
)
```

Passing `sync_uart=None` disables sync (free-running second tick).
Passing `led_pin=None` skips the LED task.

### Register helpers

| Helper                                                                     | Purpose                                           |
| -------------------------------------------------------------------------- | ------------------------------------------------- |
| `add_register(addr, type, *, n_elements, access, on_read, on_write, name)` | Create + add a `RegisterEntry`.                   |
| `add_u8 / add_u16 / add_u32 / add_s16 / add_float`                         | Sugar for the common payload types.               |
| `@on_read(addr, type)`                                                     | Decorator: declare/attach an async read handler.  |
| `@on_write(addr, type)`                                                    | Decorator: declare/attach an async write handler. |

Handler signatures:

```python
async def reader(reg) -> None:
    reg.storage[0] = ...                    # refresh before reply

async def writer(reg, payload_mv) -> int | None:
    # parse payload_mv, apply
    return None                             # or an error number
```

### Event helpers

| Helper                                                       | Purpose                                                                               |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| `add_event_source(addr, type, *, port, ring_size, pack)`     | Returns an `EventSource`; emit IRQ-safe with `.emit(word)`. Adds 1 always-on task.    |
| `add_periodic_event(addr, type, period_ms, *, pack, port)`   | Auto-emit a register's value every `period_ms` (drift-corrected).                     |
| `bind_pin_event(pin, *, address, payload_type, trigger, ...)`| GPIO IRQ → EVENT (ts at IRQ). `task=` / `sequence=` for press-driven task sequence.   |
| `bind_event_register(addr, type, *, n_elements, name)`       | Create a R/O EVENT register; returns `await emit(value)` closure (no always-on task). |
| `bind_state_register(addr, type, sequence, mapping, *, name)`| Create a R/W register whose written byte drives a `TaskSequence` via `mapping`.       |
| `await emit(addr, payload, payload_type=None)`               | Push an ad-hoc EVENT. `payload` may be bytes-like, an int/float, or a list/tuple.     |

### Task helpers

| Helper                                                     | Purpose                                                                                |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `@task`                                                    | Register an extra coroutine for `asyncio.gather` set.                                  |
| `add_task_sequence(states, *, guards, on_timeout)`         | N-state mutually-exclusive task sequence with per-state `wait_for` timeouts and gates. |
| `await run()`                                              | Launch every core task + your event sources + your tasks.                              |

### Clock access

| Helper            | Purpose                                                     |
| ----------------- | ----------------------------------------------------------- |
| `device.clock`    | The `Clock` instance (read `.now()`, subscribe to `.tick`). |
| `timestamp_now()` | `(seconds, ticks)` — current Harp time.                     |

## Payload types

| Constant           | Wire type                         | Element size |
| ------------------ | --------------------------------- | -----------: |
| `PT_U8`            | unsigned 8                        |            1 |
| `PT_S8`            | signed 8                          |            1 |
| `PT_U16`           | unsigned 16                       |            2 |
| `PT_S16`           | signed 16                         |            2 |
| `PT_U32`           | unsigned 32                       |            4 |
| `PT_S32`           | signed 32                         |            4 |
| `PT_U64`           | unsigned 64                       |            8 |
| `PT_S64`           | signed 64                         |            8 |
| `PT_FLOAT`         | IEEE 754                          |            4 |
| `PT_HAS_TIMESTAMP` | OR-mask: prepend 6-byte timestamp |            — |

## Register access bits

| Constant     | Meaning                                            |
| ------------ | -------------------------------------------------- |
| `READ_ONLY`  | Host may issue READ.                               |
| `WRITE_ONLY` | Host may issue WRITE.                              |
| `READ_WRITE` | Both (= `READ_ONLY \| WRITE_ONLY`).                |
| `EVENT`      | Register can also be the source of EVENT messages. |

## RegisterEntry

Direct construction, in case you want to bypass the `HarpDevice` helpers:

```python
RegisterEntry(address, payload_type, n_elements=1, access=READ_WRITE,
              on_read=None, on_write=None, name="")
```

Fields: `address`, `payload_type`, `n_elements`, `access`, `storage`
(bytearray), `on_read`, `on_write`, `name`. Add to a bank with
`bank.add(reg)`.

## Standard registers

Addresses 0..19 are populated automatically by the constructor (Harp
Device spec). Notable ones:

| Addr | Name                  | Notes                                      |
| ---: | --------------------- | ------------------------------------------ |
|    0 | `R_WHOAMI`            | Identifies device model.                   |
|    1 | `R_HW_VERSION_H/L`    | Hardware version.                          |
|    6 | `R_FW_VERSION_H/L`    | Firmware version.                          |
|    8 | `R_TIMESTAMP_SECOND`  | Read = now; write = set Harp clock.        |
|    9 | `R_TIMESTAMP_MICRO`   | 32 µs ticks within the current second.     |
|   10 | `R_OPERATION_CONTROL` | OP_MODE, DUMP, MUTE_RPL, HEARTBEAT_EN, ... |
|   11 | `R_RESET_DEV`         | Soft-reset trigger.                        |
|   12 | `R_DEVICE_NAME`       | 25-byte ASCII name (null-padded).          |
|   13 | `R_SERIAL_NUMBER`     | U16.                                       |
|   14 | `R_CLOCK_CONFIG`      | Sync source / output config.               |
|   15 | `R_TIMESTAMP_OFFSET`  | Bus offset compensation.                   |

Application registers should start at address 32.

## EventSource

Returned by `add_event_source` / `bind_pin_event`. IRQ-safe.

```python
src.emit(payload_word)          # safe in hard IRQ — captures ts now
```

Internally: ring buffer of `(timestamp, word)` tuples + a
`ThreadSafeFlag`. The `run()` task drains, packs the word with the
configured `pack(word, scratch) -> n_bytes` callable (default = U8), and
puts the slab on `tx_q`.

## Clock

```python
device.clock.now()              # (seconds, ticks)
device.clock.tick.subscribe()   # asyncio.Event for per-second tick
```

The clock's per-second pulse fans out to LED, heartbeat, and any
subscriber via `TickBroadcaster`. See [ARCHITECTURE.md](ARCHITECTURE.md)
for sync details.
