"""Harp device class."""

import uasyncio
from collections import deque
from micropython import const

from .type import HarpTypes
from .message import HarpMessage, HarpRxMessage, HarpTxMessage
from .register import (
    ReadOnlyReg,
    ReadWriteReg,
    TimestampSecondReg,
    TimestampMicroReg,
    OperationalCtrlReg
)
from .event import PeriodicEvent, HarpEvent
from .clock import HarpClock
import sys
import uselect
from machine import Pin, UART
from machine import Timer


class HarpDevice:
    """Harp device implementing the common registers and functionality.

    All Harp device classes should subclass this class, overload __init__ and call it from their
    implementation. Devices may overload _ctrl_hook(), but must call the base function. It is
    not recommended, nor should it be necessary, to overload other member functions of this class.
    """

    R_WHO_AM_I = const(0)
    R_HW_VERSION_H = const(1)
    R_HW_VERSION_L = const(2)
    R_ASSEMBLY_VERSION = const(3)
    R_HARP_VERSION_H = const(4)
    R_HARP_VERSION_L = const(5)
    R_FW_VERSION_H = const(6)
    R_FW_VERSION_L = const(7)
    R_TIMESTAMP_SECOND = const(8)
    R_TIMESTAMP_MICRO = const(9)
    R_OPERATION_CTRL = const(10)
    R_RESET_DEV = const(11)
    R_DEVICE_NAME = const(12)
    R_SERIAL_NUMBER = const(13)
    CLK_BUAD = const(100_000)
    CLK_LEN = const(6)

    ledIntervals = (2.0, 1.0, 0.05, 0.5)

    def __init__(self, led, clocksync: UART, rxqlen=16, txqlen=16, monitor=None):
        """Constructor.

        Connects the logical device to its physical interfaces and creates the register map.
        Sub-classes should extend (and update) the register dictionary with register classes
        which implement the required device specific functionality.
        """

        self.clock = HarpClock()
        self.led = led
        self.monitor = monitor
        self.blink_flag = True

        self.stream = uselect.poll()
        self.stream.register(sys.stdin, uselect.POLLIN)

        self.rxMessages = deque((), rxqlen)
        self.txMessages = deque((), txqlen)
        self.clockSync = clocksync

        self.registers = {
            HarpDevice.R_WHO_AM_I: ReadOnlyReg(HarpTypes.U16, (0,)),
            HarpDevice.R_HW_VERSION_H: ReadOnlyReg(HarpTypes.U8, (1,)),
            HarpDevice.R_HW_VERSION_L: ReadOnlyReg(HarpTypes.U8),
            HarpDevice.R_FW_VERSION_H: ReadOnlyReg(HarpTypes.U8),
            HarpDevice.R_FW_VERSION_L: ReadOnlyReg(HarpTypes.U8, (1,)),
            HarpDevice.R_TIMESTAMP_SECOND: TimestampSecondReg(self.clock),
            HarpDevice.R_TIMESTAMP_MICRO: TimestampMicroReg(self.clock),
            HarpDevice.R_OPERATION_CTRL: OperationalCtrlReg(self._ctrl_hook),
            HarpDevice.R_DEVICE_NAME: ReadWriteReg(HarpTypes.U8, tuple(b"Microharp Device")),
            HarpDevice.R_SERIAL_NUMBER: ReadWriteReg(HarpTypes.U16),
        }

        self.aliveEvent = PeriodicEvent(
            HarpDevice.R_TIMESTAMP_SECOND,
            self.registers[HarpDevice.R_TIMESTAMP_SECOND],
            self.clock,
            self.txMessages,
            1000,
        )
        self.events: list[HarpEvent] = [self.aliveEvent]

        self.tasks = [self._stream_task(), self._blink_task(), self._clock_task()]

    def _ctrl_hook(self):
        """Private member function.

        Control register write hook, updates device state.
        """

        op_ctrl = self.registers[HarpDevice.R_OPERATION_CTRL]

        self.led.value(op_ctrl.OPLEDEN)

        event_en = op_ctrl.OP_MODE != OperationalCtrlReg.STANDBY_MODE

        for event in self.events:
            if event == self.aliveEvent:
                event.enabled = op_ctrl.ALIVE_EN and event_en
            else:
                event.enabled = event_en

    async def _read_co(self, buf, nbytes=1):
        """Private member co-routine.

        Reads nbytes from stream into buf in the largest blocks available, whilst playing nicely.
        """
        while nbytes > 0:
            if self.stream.poll(0):
                buf.extend(sys.stdin.buffer.read(1))
                nbytes -= 1
            await uasyncio.sleep(0)

    async def _clock_task(self):
        """Private member co-routine.

        Reads nbytes from stream into buf in the largest blocks available, whilst playing nicely.
        """
        buf = bytearray(6)
        while True:
            if self.clockSync.any() >= 6:
                self.blink_flag = False
                self.aliveEvent.timer.deinit()

                self.clockSync.readinto(buf)
                self.clock.write(buf)

                self.aliveEvent._callback(0)
                self.aliveEvent.timer.init(mode=Timer.PERIODIC, period=1000, callback=self.aliveEvent._callback)
                self.blink_flag = True

            await uasyncio.sleep(0)

    async def _stream_task(self):
        """Private member co-operative task.

        Reads and validates complete messages from stream and posts them to the rxMessages queue.
        """
        # print('HarpDevice._stream_task()')
        while True:
            try:
                rxMessage = HarpRxMessage()
                await self._read_co(rxMessage.buffer, HarpMessage.LENGTH_BYTE)
                if not rxMessage.has_valid_message_type():
                    raise ValueError("Invalid messageType: " + rxMessage.to_string())
                await self._read_co(rxMessage.buffer, rxMessage.length)
                if not rxMessage.has_valid_checksum():
                    raise ValueError("Invalid checksum: " + rxMessage.to_string())
                self.rxMessages.append(rxMessage)
            except (ValueError, IndexError) as e:
                if self.monitor:
                    self.monitor.write(str(e) + "\n")

    async def _blink_task(self):
        """Private member co-operative task.

        Toggles the led to indicate the device operation mode.
        """
        # print('HarpDevice._blink_task()')
        while True:
            while self.blink_flag:
                if self.registers[HarpDevice.R_OPERATION_CTRL].VISUALEN:
                    if self.registers[HarpDevice.R_OPERATION_CTRL].OPLEDEN:
                        self.led.toggle()
                    interval = HarpDevice.ledIntervals[self.registers[HarpDevice.R_OPERATION_CTRL].OP_MODE]
                else:
                    self.led.on()
                await uasyncio.sleep(interval)
            await uasyncio.sleep(0)

    async def main(self):
        """Device main function, must be called using uasyncio.run().

        Creates and launches the device co-operative tasks and executes the main application loop.
        """
        # print('HarpDevice.main()')
        for task in self.tasks:
            uasyncio.create_task(task)

        while True:
            # Process rx message queue.
            if len(self.rxMessages) > 0:
                try:
                    # Fetch next message.
                    rxMessage = self.rxMessages.popleft()
                    if self.monitor:
                        self.monitor.write("RX msg: " + rxMessage.to_string() + "\n")
                    # Perform write operation.
                    if rxMessage.messageType == HarpMessage.WRITE:
                        self.registers[rxMessage.address].write(rxMessage.payloadType, rxMessage.payload)
                    # Prepare response.
                    length = (
                        len(self.registers[rxMessage.address]) * HarpTypes.size(rxMessage.payloadType)
                        + HarpMessage.offset(HarpTypes.HAS_TIMESTAMP)
                        - 1
                    )
                    txMessage = HarpTxMessage(
                        rxMessage.messageType,
                        length,
                        rxMessage.address,
                        rxMessage.payloadType,
                        self.clock.read(),
                    )

                    # Perform read operation.
                    txMessage.payload = self.registers[rxMessage.address].read(rxMessage.payloadType)

                except (TypeError, IndexError, KeyError):
                    # Prepare error response.
                    length = rxMessage.length + HarpMessage.resize(rxMessage.payloadType)
                    txMessage = HarpTxMessage(
                        rxMessage.messageType | HarpMessage.ERROR,
                        length,
                        rxMessage.address,
                        rxMessage.payloadType,
                        self.clock.read(),
                    )
                    if rxMessage.messageType == HarpMessage.WRITE:
                        txMessage.payload = rxMessage.payload

                # Format and post response to transmit queue.
                txMessage.calc_set_checksum()
                self.txMessages.append(txMessage)

            # Process tx message queue.
            if len(self.txMessages) > 0:
                txMessage = self.txMessages.popleft()
                if self.monitor:
                    self.monitor.write("TX msg: " + txMessage.to_string() + "\n")
                sys.stdout.buffer.write(txMessage.buffer)

            await uasyncio.sleep(0)
