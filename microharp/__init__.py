"""Harp protocol implementation for MicroPython (asyncio-based).

Install
-------
    mpremote mip install github:DCisHurt/micropython-microharp
        # Pulls usb-device-cdc as a transitive dependency.

Layout
------
    microharp/clock.py       Clock + sync_task (UART RX of sync packets)
    microharp/framing.py     Encode / decode of the 8-bit binary protocol
    microharp/transport.py   Cdc / Uart / Stream transports + slab pool
    microharp/registers.py   RegisterEntry, RegisterBank, common registers 0..19
    microharp/dispatch.py    Dispatcher routing parsed frames -> register handlers
    microharp/events.py      Event source helper (timestamp at IRQ, encode in task)
    microharp/heartbeat.py   1 Hz heartbeat EVENT task
    microharp/led.py         Status LED task, clock-synchronized when locked
    microharp/device.py      HarpDevice top-level orchestrator

Wiring (defaults assumed by example/example_secondary_cdc.py)
------------------------------------------------------------
    Sync UART : RX-only, 100 000 baud, 8N1.  RP2040 default = UART(1) RX=GP5.
    USB CDC   : Secondary CDC interface via usb.device.cdc.CDCInterface().
                REPL stays on /dev/ttyACM0; Harp runs on /dev/ttyACM1.
    LED       : GP25 onboard (Pico).
    Input pin : GP14 with internal pull-up, IRQ on both edges.

Port-specific notes
-------------------
    * RP2040 / RP2350 : machine.UART supports IRQ_RX (and IRQ_RXIDLE on
      newer firmware).  Hard IRQs are supported and `time.ticks_us()` is
      callable from a hard IRQ.
    * ESP32 / ESP32-S3 : UART IRQ semantics differ; `sync_task` falls back
      to a polling loop automatically if `uart.irq()` raises.  Hard IRQs
      have stricter restrictions on ESP32 (no allocation), so EventSource
      uses a soft IRQ via asyncio.ThreadSafeFlag -- already compatible.
    * Ports without USB CDC : use UartTransport (see
      example/example_uart.py).

Running on the device
---------------------
    After `mip install`, copy one of example/example_*.py to :main.py and
    reboot.  Or, for an offline install:
        mpremote cp -r microharp/ :
        mpremote cp example/example_secondary_cdc.py :main.py

Running the framing self-test on a desktop
------------------------------------------
    python tests/test_framing.py
        # Tests stub `micropython.const`, time.ticks_*, and ThreadSafeFlag
        # so the package imports cleanly under CPython.
"""

from .framing import (
    MSG_READ,
    MSG_WRITE,
    MSG_EVENT,
    MSG_FLAG_ERROR,
    PT_U8,
    PT_S8,
    PT_U16,
    PT_S16,
    PT_U32,
    PT_S32,
    PT_U64,
    PT_S64,
    PT_FLOAT,
    PT_HAS_TIMESTAMP,
)
from .clock import Clock
from .registers import RegisterEntry, RegisterBank, READ_ONLY, WRITE_ONLY, READ_WRITE, EVENT
from .transport import (
    CdcTransport,
    UartTransport,
    StreamTransport,
    SlabPool,
    usb_rx_task,
    usb_tx_task,
)
from .dispatch import Dispatcher
from .led import led_task
from .heartbeat import heartbeat_task
from .events import emit_event_from_irq, EventSource
from .device import HarpDevice
