# Architecture & performance

## Module layout

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
  queue.py        asyncio.Queue fallback for bare RP2 firmware (see below)
```

### `queue.py` — platform fallback

MicroPython's RP2 port ships with a stripped asyncio that omits
`asyncio.Queue`. `queue.py` is a self-contained implementation (Peter
Hinch, MIT) kept as a local fallback.

Callers import it with a try/except guard:

```python
try:
    from asyncio import Queue   # CPython + MicroPython ≥ 1.20 full build
except ImportError:
    from .queue import Queue    # bare RP2 / stripped firmware
```

This means:

- On CPython (used by the test suite) and on MicroPython builds that
  bundle a complete asyncio, the stdlib class is used and `queue.py`
  is never loaded.
- On a bare RP2 firmware image without asyncio.Queue the local fallback
  is loaded transparently — no source changes needed.
- If a future firmware release adds `asyncio.Queue`, the fallback
  silently becomes dead code and can be removed.

## Data flow

```text
host bytes ─▶ transport ─▶ FrameDecoder ─▶ rx_q (slab idx) ─▶ Dispatcher
                                                                   │
                                          ┌────────────────────────┴────────────────────────┐
                                          ▼                                                 ▼
                                       READ handler                                     WRITE handler
                                          │                                                 │
                                          └──▶ encode_into ──▶ tx_q (slab idx) ◀─── EventSource (IRQ)
                                                                    │
                                                                    ▼
                                                           transport ─▶ host bytes
```

Slab indices, not bytes, travel through the queues. Steady-state TX
allocates nothing.

## Clock and sync

- The sync UART RX IRQ captures `_ticks_us()` (a viper-direct read of
  the RP2 hardware timer where available, otherwise `time.ticks_us`).
- `apply_sync` back-dates the epoch by the spec's 672 µs sync offset and
  by the 600 µs packet duration so seconds align with the master's
  timeline.
- A `TickBroadcaster` fans the per-second pulse out to LED, heartbeat,
  and any other subscribers — each gets its own `asyncio.Event`.
- Without a sync master, `second_ticker` keeps the clock advancing so
  HEARTBEAT_EN still works per spec.

## IRQ → asyncio bridge

- Hard IRQs capture `_ticks_us()` and write into pre-allocated arrays,
  then set an `asyncio.ThreadSafeFlag`.
- The `Clock` has its own `sync_flag`; each `EventSource` has its own.
- Drain tasks wake within one scheduler iteration of the IRQ — no 1 ms
  polling.

## Transport wake-up

- Transports use `asyncio.StreamReader` / `StreamWriter` internally, so
  reads and writes go through the port's native poll/select wake
  mechanism. Bytes wake the task as soon as they arrive; `drain()` waits
  only when the underlying buffer is full.
- Falls back to a `sleep_ms(0)` yield-loop only on objects that can't be
  wrapped (no `fileno`).

## Slab pool

- Pre-allocated bytearrays leased by index; the index travels through
  asyncio queues, never the bytes.
- `lease()` blocks (asynchronously) when exhausted; `lease_nowait()`
  returns -1 — used by IRQ-time emission so a hard IRQ can never block.
- Zero allocation in the steady-state TX path.

## Status LED

Driven by the same per-second tick the clock publishes, so every device
on the bench blinks in unison when sync is healthy. A 4 Hz free-running
blink is the explicit "unsynced" tell. Gated by `VISUAL_EN` and
`OPLED_EN` in OperationControl.

## Heartbeat

Publishes an `R_HEARTBEAT` (U16) EVENT at 1 Hz when
`OperationControl.HEARTBEAT_EN` (bit 2) is set and the device is in
Active mode. The U16 payload reports `IS_ACTIVE` (bit 0) and
`IS_SYNCHRONIZED` (bit 1). `ALIVE_EN` (bit 7, deprecated) selects
`R_TIMESTAMP_SECOND` instead — `HEARTBEAT_EN` takes precedence per spec.

## Operation mode gating

Per Device.md §Operation Mode, events MUST NOT be sent in Standby:

- `EventSource.run` drops entries when `OP_MODE != Active`.
- The heartbeat task does the same.
- The ring still drains, so a backlog can't grow.

## MUTE_RPL

When set, the dispatcher suppresses _every_ reply (READ, WRITE, errors,
the DUMP sequence). State-changing writes still take effect.

## DUMP

A WRITE to `OperationControl` with the DUMP bit set sends the WRITE
reply _first_, then one READ reply per readable register, then
auto-clears the DUMP bit (Device.md §Request-Reply).

## Connected / NotConnected

`HarpDevice` auto-installs a CDC line-state callback that drops
`OP_MODE` to STANDBY on DTR-high (host opens the port), so the host
never sees stale events from a previous session.

---

## Performance

The hot paths use the speed-Python toolkit:

- **`_checksum` is `@micropython.viper`** — runs as native machine code
  with C-style integer typing. ~5–10× faster than a Python loop on
  viper-capable ports (RP2 / ESP32 / STM32). Falls back to a plain
  Python loop on builds without viper, and on a CPython test harness.
- **`encode_into`, `parse_header`, `Clock._compute`,
  `_convert_to_harp_ts`, and the IRQ ring `push_irq` are
  `@micropython.native`** — bytecode-free Python overhead between bulk
  operations.
- **All long-lived async loops cache attribute lookups** and bound
  methods as locals before entering their `while True:` —
  `LOAD_FAST` instead of `LOAD_ATTR` per iteration.
- **Module-level integer constants are wrapped in `const(...)`** so
  they're inlined at compile time.
- **`time.ticks_us()` is captured _inside the IRQ_**, not at
  message-encoding time, so timestamp accuracy is bounded by IRQ entry
  latency (~single-digit µs on RP2040), not by asyncio scheduling.
- **TX coalescing**: when multiple slabs are queued, the TX task
  chains memoryviews into back-to-back writes. Most CDC stacks pack
  consecutive small writes into one USB packet.

## Port matrix

| Port            | Stdio |       Cdc       | Uart | Sync UART | Hard IRQ |
| --------------- | :---: | :-------------: | :--: | :-------: | :------: |
| RP2040 / RP2350 |  ✅   |  ✅ (MP ≥1.23)  |  ✅  |    ✅     |    ✅    |
| ESP32-S2 / S3   |  ✅   | ⚠ port-specific |  ✅  |    ✅     |    ⚠     |
| STM32 (Pyboard) |  ✅   |       n/a       |  ✅  |    ✅     |    ✅    |

ESP32 hard IRQs have stricter restrictions; `bind_pin_event` falls back
to soft IRQ semantics if `hard=True` raises. UART IRQ semantics also
differ on ESP32 — `sync_task` falls back to a polling loop automatically
if `uart.irq()` raises.
