# Custom tasks and events

`HarpDevice.run()` builds the asyncio task set: RX, TX, dispatcher,
heartbeat, second-tick, optional sync UART, optional LED, and any event
sources or user tasks you've registered. This page covers how to add
your own work to that set.

## `@device.task` patterns

### 1. Plain background loop

The most common case. Decorate a no-arg async function; capture
peripherals by closure.

```python
@device.task
async def my_background():
    while True:
        # ... do something
        await asyncio.sleep_ms(500)
```

### 2. Emit a Harp EVENT from a task

Read a sensor, push it as a timestamped event:

```python
ADDR_TEMP = 33
device.add_u16(ADDR_TEMP, access=READ_ONLY | EVENT, name="Temperature")

@device.task
async def temperature_monitor():
    while True:
        raw = adc.read_u16()
        # Timestamp is captured inside emit().
        await device.emit(ADDR_TEMP, raw.to_bytes(2, "little"), PT_U16)
        await asyncio.sleep_ms(100)        # 10 Hz
```

See [`example/example_periodic_event.py`](example/example_periodic_event.py)
for a complete runnable version, or use the built-in
`device.add_periodic_event(addr, type, period_ms)` helper for the same
pattern with drift correction.

### 3. Wake on the clock's per-second tick

Useful for work that should land on a Harp-second boundary, in lockstep
with the LED and heartbeat:

```python
@device.task
async def per_second_work():
    tick = device.clock.tick.subscribe()
    while True:
        await tick.wait()
        tick.clear()
        # runs once per Harp second, edge-aligned across the bench
```

### 4. Task with arguments

Pass a coroutine object directly instead of a factory:

```python
async def watcher(threshold):
    while True:
        if some_value > threshold:
            await device.emit(ADDR_ALERT, b"\x01", PT_U8)
        await asyncio.sleep_ms(50)

device.task(watcher(threshold=42))
```

See [`example/example_custom_task.py`](example/example_custom_task.py).

## Pin events with timestamps at IRQ

`bind_pin_event` wires a `machine.Pin` IRQ to a Harp register —
read + event in one call:

```python
button = Pin(14, Pin.IN, Pin.PULL_UP)

device.bind_pin_event(
    button,
    address=32,
    payload_type=PT_U8,
    trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING,
    name="DigitalInput",
)
```

This:

1. Creates the register (READ_ONLY | EVENT) if it doesn't exist.
2. Mirrors the current pin level into storage so READs return state.
3. Installs a hard IRQ that captures the timestamp at IRQ time and
   queues an EVENT message.

The timestamp is captured _inside the IRQ handler_, not at asyncio wake,
so accuracy is bounded by IRQ entry latency (~single-digit µs on RP2040).

## Custom event sources for non-pin IRQs

For timers, encoders, or sensor-ready lines, build an `EventSource`
directly and emit from any callback:

```python
src = device.add_event_source(address=64, payload_type=PT_U16)

def on_timer(t):
    src.emit(some_value)        # captures timestamp now

machine.Timer(period=1, mode=machine.Timer.PERIODIC, callback=on_timer)
```

`emit()` is safe from hard-IRQ context — it uses `lease_nowait()` and
sets a `ThreadSafeFlag`. The encode work happens in the event source's
asyncio task.

## Rules of thumb

- **Don't `print()`** if your transport is `StdioTransport` — it scribbles
  into the Harp wire. Use `CdcTransport` for production, or accumulate
  diagnostics into a register the host can read.
- **`await asyncio.sleep_ms(0)`** to yield in CPU-heavy work — keeps the
  dispatcher and TX tasks responsive.
- **For sub-millisecond timing** (kHz sensor sampling, encoder edges),
  use a `machine.Timer` or pin IRQ that calls `event_source.emit(...)`
  directly. The asyncio wake jitter (~1 ms) is irrelevant because the
  timestamp was already captured at IRQ.
- **Tasks added before `device.run()`** are launched alongside the core
  tasks. You can also `asyncio.create_task(...)` inside another task at
  runtime to spawn dynamically.
- **Standby gating**: events are dropped when `OP_MODE != Active` per
  spec. The ring still drains — no backlog can grow.
