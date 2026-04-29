"""Harp device class."""

import uasyncio
from collections import deque
from micropython import const

from .type import HarpTypes
from .message import HarpMessage, HarpRxMessage, HarpTxMessage
from .register import ReadOnlyReg, ReadWriteReg, TimestampSecondReg, TimestampMicroReg, OperationalCtrlReg
from .event import PeriodicEvent, HarpEvent
from .clock import HarpClock
import sys
from machine import Pin, UART
from machine import Timer
from .lib.cdc import CDCInterface
from .lib import core as usb

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
    R_CLOCK_CONFIG = const(14)
    R_TIMESTAMP_OFFSET = const(15)
    R_UID = const(16)
    R_TAG = const(17)
    R_HEARTBEAT = const(18)
    R_VERSION = const(19)
    CLK_BUAD = const(100_000)
    CLK_LEN = const(6)

    ledIntervals = (2.0, 1.0, 0.05, 0.5)

    def __init__(self, led, clocksync: UART, rxqlen=16, txqlen=16, monitor=True):
        """Constructor.

        Connects the logical device to its physical interfaces and creates the register map.
        Sub-classes should extend (and update) the register dictionary with register classes
        which implement the required device specific functionality.
        """

        self.clock = HarpClock()
        self.led = led
        self.monitor = sys.stdout if monitor else None
  
        self.blink_flag = True

        self.cdc = CDCInterface(timeout=0, txbuf=512, rxbuf=512)
        usb.get().init(
            self.cdc,
            builtin_driver=True,
        )
        self.cdc.set_line_state_cb(self._line_state_cb)

        self.rxMessages = deque((), rxqlen)
        self.txMessages = deque((), txqlen)
        self.clockSync = clocksync

        self.registers = {
            HarpDevice.R_WHO_AM_I: ReadOnlyReg(HarpTypes.U16),
            HarpDevice.R_TIMESTAMP_SECOND: TimestampSecondReg(self.clock),
            HarpDevice.R_TIMESTAMP_MICRO: TimestampMicroReg(self.clock),
            HarpDevice.R_OPERATION_CTRL: OperationalCtrlReg(self._ctrl_hook),
            HarpDevice.R_HEARTBEAT: ReadOnlyReg(HarpTypes.U16),
            HarpDevice.R_VERSION: ReadOnlyReg(HarpTypes.U8, (0,)),

            # Optional, device specific registers.
            HarpDevice.R_RESET_DEV: ReadWriteReg(HarpTypes.U8, (0,)),
            HarpDevice.R_DEVICE_NAME: ReadWriteReg(HarpTypes.U8, tuple(b"Microharp Device" + bytes(25 - len(b"Microharp Device")))),  # 25-byte null-padded
            HarpDevice.R_CLOCK_CONFIG: ReadWriteReg(HarpTypes.U8, (0,)),
            HarpDevice.R_UID: ReadOnlyReg(HarpTypes.U8, (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
            HarpDevice.R_TAG: ReadOnlyReg(HarpTypes.U8, (0, 0, 0, 0, 0, 0, 0, 0)),

            # Deprecated registers, return 0 if called.
            HarpDevice.R_HW_VERSION_H: ReadOnlyReg(HarpTypes.U8, (1,)),
            HarpDevice.R_HW_VERSION_L: ReadOnlyReg(HarpTypes.U8, (0,)),
            HarpDevice.R_ASSEMBLY_VERSION: ReadOnlyReg(HarpTypes.U8, (1,)),
            HarpDevice.R_HARP_VERSION_H: ReadOnlyReg(HarpTypes.U8, (1,)),
            HarpDevice.R_HARP_VERSION_L: ReadOnlyReg(HarpTypes.U8, (0,)),
            HarpDevice.R_FW_VERSION_H: ReadOnlyReg(HarpTypes.U8, (1,)),
            HarpDevice.R_FW_VERSION_L: ReadOnlyReg(HarpTypes.U8, (0,)),
            HarpDevice.R_SERIAL_NUMBER: ReadOnlyReg(HarpTypes.U16, (0xFFFF,)),
            HarpDevice.R_TIMESTAMP_OFFSET: ReadOnlyReg(HarpTypes.U8, (0,)),
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

    DEVICE_NAME_LEN = const(25)

    def set_name(self, name: str):
        """Set the device name (max 24 chars, null-padded to 25 bytes)."""
        encoded = name.encode("ascii")[:HarpDevice.DEVICE_NAME_LEN - 1]
        padded = encoded + bytes(HarpDevice.DEVICE_NAME_LEN - len(encoded))
        self.registers[HarpDevice.R_DEVICE_NAME].value = tuple(padded)

    def _line_state_cb(self, line_state):
        """Private member function.

        CDC line state callback. Resets the device to standby when the host connects (DTR high),
        preventing spurious event frames before the host writes the operation control register.
        """
        if line_state & 0x01:  # DTR set - host opened the port
            op_ctrl = self.registers[HarpDevice.R_OPERATION_CTRL]
            op_ctrl.value = (op_ctrl.value[0] & ~0x03,)  # Clear OP_MODE to STANDBY
            for event in self.events:
                event.enabled = False

    def _ctrl_hook(self):
        """Private member function.

        Control register write hook, updates device state.
        """

        op_ctrl = self.registers[HarpDevice.R_OPERATION_CTRL]

        isActive = op_ctrl.OP_MODE != OperationalCtrlReg.STANDBY_MODE
        tmp = self.registers[HarpDevice.R_HEARTBEAT].value[0]
        tmp &= isActive << 1
        self.registers[HarpDevice.R_HEARTBEAT].value = (tmp,)

        self.led.value(op_ctrl.OPLEDEN)

        event_en = op_ctrl.OP_MODE != OperationalCtrlReg.STANDBY_MODE

        if op_ctrl.DUMP:
            for address, reg in self.registers.items():
                length = (
                    len(reg) * HarpTypes.size(reg.typ)
                    + HarpMessage.offset(reg.typ | HarpTypes.HAS_TIMESTAMP)
                    - 1
                )
                txMessage = HarpTxMessage(
                    HarpMessage.READ,
                    length,
                    address,
                    reg.typ | HarpTypes.HAS_TIMESTAMP,
                    self.clock.read(),
                )
                txMessage.payload = reg.read(reg.typ)
                txMessage.calc_set_checksum()
                self.txMessages.append(txMessage)
            op_ctrl.value = (op_ctrl.value[0] & ~0x08,)

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
            chunk = self.cdc.read(1)
            if chunk:
                buf.extend(chunk)
                nbytes -= 1
            await uasyncio.sleep(0)

    async def _clock_task(self):
        """Private member co-routine.

        Reads nbytes from stream into buf in the largest blocks available, whilst playing nicely.
        """
        buf = bytearray(6)
        while True:
            if self.clockSync.any() >= 6:
                self.clockSync.readinto(buf)
                self.clock.write(buf[3] | (buf[4] << 8) | (buf[5] << 16) | (buf[0] << 24))
                
                self.aliveEvent._callback(0)
                self.led.value(buf[3] & 0x2)

                if self.blink_flag:
                    self.blink_flag = False
                    self.aliveEvent.timer.deinit()

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
        while self.blink_flag:
            if self.registers[HarpDevice.R_OPERATION_CTRL].VISUALEN:
                if self.registers[HarpDevice.R_OPERATION_CTRL].OPLEDEN:
                    self.led.toggle()
                interval = HarpDevice.ledIntervals[self.registers[HarpDevice.R_OPERATION_CTRL].OP_MODE]
            else:
                self.led.on()
            await uasyncio.sleep(interval)

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

                    # Perform write operation (may enqueue DUMP messages via hook).
                    if rxMessage.messageType == HarpMessage.WRITE:
                        self.registers[rxMessage.address].write(rxMessage.payloadType, rxMessage.payload)

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

                # Format and post response to transmit queue (unless muted).
                if not self.registers[HarpDevice.R_OPERATION_CTRL].MUTE_RPL:
                    txMessage.calc_set_checksum()
                    self.txMessages.append(txMessage)

            # Process tx message queue.
            if len(self.txMessages) > 0:
                txMessage = self.txMessages.popleft()
                if self.monitor:
                    self.monitor.write("TX msg: " + txMessage.to_string() + "\n")
                self.cdc.write(txMessage.buffer)

            await uasyncio.sleep(0)
