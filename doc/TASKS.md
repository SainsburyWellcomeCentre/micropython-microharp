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

### Running a task only while a pin is active

`bind_pin_event` can take a `task=` coroutine factory. When the pin
transitions to `active_level` (default 0, matches `Pin.PULL_UP`) the
framework starts a new task from `task()`; on the opposite transition
it cancels that task. No always-on controller task is added — the
create/cancel are bounced out of hard IRQ via `micropython.schedule`,
so `asyncio.create_task` / `Task.cancel` run in normal context one
main-loop tick later.

```python
async def worker():
    n = 0
    try:
        while True:
            n += 1
            print("tick", n)
            await asyncio.sleep_ms(200)
    except asyncio.CancelledError:
        print("stopped at", n)
        raise

device.bind_pin_event(
    button, address=32, payload_type=PT_U8,
    trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING,
    task=worker,
)
```

The Harp EVENT is still emitted on every edge — the sequence runs
alongside it. For lower-level use (your own flag, custom dispatch) the
`on_irq(level)` hook is also available and runs in hard-IRQ context;
pre-bind a `ThreadSafeFlag.set` and write your own task. See
[`example/example_button_controller.py`](example/example_button_controller.py).

### Multi-state sequence with timeouts and error EVENTs

For more than two states, per-state timeouts, or driving the same
sequence from several triggers (a pin AND a register write), use
`add_task_sequence`:

```python
emit_error = device.bind_event_register(0x22, PT_U8, name="Error")

async def on_timeout(name):
    await emit_error(1 if name == "A" else 2)

sequence = device.add_task_sequence(
    states={
        "A": (taskA,  5),       # cancel after 5 s, EVENT to 0x22
        "B": (taskB, 10),
    },
    on_timeout=on_timeout,
)

# Trigger 1: pin level → state name.
device.bind_pin_event(
    button, address=0x20, payload_type=PT_U8,
    trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING,
    sequence=sequence, level_state={0: "A", 1: "B"},
)

# Trigger 2: register write → state name.
device.bind_state_register(
    address=0x21, payload_type=PT_U8,
    sequence=sequence, mapping={1: "A", 0: "B"},
)
```

Same-state calls dedupe — writing 0x21=1 while taskA is already running
is a no-op. On timeout the framework awaits `on_timeout(name)` (typical
use: emit an error EVENT) and resets the sequence to idle so the next
trigger can start fresh. `bind_event_register` returns an
`await emit(value)` closure that does not add an always-on drain task,
so total task count stays at 0 idle and 1 active. See
[`example/example_3state_timeout.py`](example/example_3state_timeout.py).

**Mapping no-op semantics.** Keys absent from `level_state` /
`mapping` are silently ignored — they don't trigger a transition. To
explicitly cancel any current task and go to idle on a particular
value, include it with state `None` (e.g. `mapping={1: "A", 0: None}`).

**Guards.** `add_task_sequence(..., guards={"A": pred})` rejects a
transition to `"A"` when `pred()` returns False. Useful for "only
allow this state from idle" or "only chain from a specific running
state" patterns.

**Pin as pure trigger.** Pass `address=None` to `bind_pin_event` to
skip Harp-event emission and the per-pin register entirely; the pin
becomes a trigger only and you wire behaviour through `on_irq=`,
`task=`, or `sequence=`. See
[`example/example_motor_chain.py`](example/example_motor_chain.py)
for a worked example combining `address=None`, `guards=`, and a
sticky-error status register.

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
