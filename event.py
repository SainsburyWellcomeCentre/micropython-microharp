"""Harp protocol event class."""

from machine import Timer

from .type import HarpTypes
from .message import HarpMessage, HarpTxMessage


class HarpEvent:
    """Base class, creates the event message and provides message queue binding.

    Event classes should sublcass this class and call __init__.
    """

    def __init__(self, address, register, sync, queue):
        length = (
            len(register) * HarpTypes.size(register.typ)
            + HarpMessage.offset(HarpTypes.HAS_TIMESTAMP)
            - 1
        )
        self.message = HarpTxMessage(
            HarpMessage.EVENT, length, address, register.typ, (0, 0)
        )
        self.register = register
        self.sync = sync
        self.queue = queue
        self.enabled = False

    def _callback(self, ins=-1):
        if self.enabled:
            self.message.timestamp = self.sync.read()
            self.message.payload = self.register.read(self.register.typ)
            self.message.calc_set_checksum()
            self.queue.append(self.message)


class PinEvent(HarpEvent):
    """Pin event.

    Triggers a read of register at address, generating an event message, on a pin event.
    """

    def __init__(self, address, register, sync, queue, trigger):
        super().__init__(address, register, sync, queue)
        register.pin.irq(handler=self._callback, trigger=trigger)


class PeriodicEvent(HarpEvent):
    """Periodic event.

    Triggers a read of register at address, generating an event message, every period milliseconds.

    The register read operation associated with a PeriodicEvent occurs in interrupt context, in
    order to minimise trigger latency, lengthy computation should therefore be avoided.
    """

    def __init__(self, address, register, sync, queue, period):
        super().__init__(address, register, sync, queue)
        self.timer = Timer(mode=Timer.PERIODIC, period=period, callback=self._callback)


class LooseEvent(HarpEvent):
    """Loose event.

    Triggers a read of register at address, generating a status message, when callback is called.

    The register read operation associated with a LooseEvent occurs in foreground context,
    allowing for lengthy computation or I/O operations at the expense of trigger latency.
    """

    def __init__(self, address, typ, queue):
        self.message = HarpTxMessage(
            HarpMessage.EVENT,
            HarpMessage.offset(~HarpTypes.HAS_TIMESTAMP) - 1,
            address,
            typ,
        )
        self.message.calc_set_checksum()
        self.queue = queue
        self.enabled = False

    def callback(self, ins=-1):
        if self.enabled:
            self.queue.append(self.message)
