"""Harp base and common register classes."""

from micropython import const
from .type import HarpTypes


class HarpRegister:
    """Abstract base class, provides runtime type checking.

    All Harp register classes must sublcass this class, overload its methods and call its
    __init__, read, and write methods from their implementations.

    All (or the majority of) specific device functionality can and should be implemented within
    specific device register read and write methods.
    """

    def __init__(self, typ):
        self.typ = typ

    def __len__(self):
        return 0

    def read(self, typ):
        if typ != self.typ:
            raise TypeError("HarpRegister.read() type mismatch.")

    def write(self, typ, value):
        if typ != self.typ:
            raise TypeError("HarpRegister.write() type mismatch.")


class ReadWriteReg(HarpRegister):
    """Generic, memory mapped, read/write register."""

    def __init__(self, typ, reset=(0,)):
        super().__init__(typ)
        self.value = reset

    def __len__(self):
        return len(self.value)

    def read(self, typ):
        super().read(typ)
        return self.value

    def write(self, typ, value):
        super().write(typ, value)
        self.value = value


class ReadOnlyReg(ReadWriteReg):
    """Generic, memory mapped, read-only register.

    Delegates all operations to ReadWriteReg, with the exception of write.
    """

    def write(self, typ, value):
        raise TypeError("ReadOnlyReg.write().")


class OperationalCtrlReg(ReadWriteReg):
    """Harp operational control register, provides bit-field definitions and getters."""

    STANDBY_MODE = const(0)
    ACTIVE_MODE = const(1)
    SPEED_MODE = const(3)

    @property
    def OP_MODE(self):
        return self.value[0] & 0x03

    @property
    def DUMP(self):
        return bool(self.value[0] & 0x08)

    @property
    def MUTE_RPL(self):
        return bool(self.value[0] & 0x10)

    @property
    def VISUALEN(self):
        return bool(self.value[0] & 0x20)

    @property
    def OPLEDEN(self):
        return bool(self.value[0] & 0x40)

    @property
    def ALIVE_EN(self):
        return bool(self.value[0] & 0x80)

    def __init__(self, wr_hook):
        super().__init__(HarpTypes.U8, (0xE0,))
        self.wr_hook = wr_hook

    def write(self, typ, value):
        super().write(typ, value)
        self.wr_hook()


class TimestampSecondReg(HarpRegister):
    """Timestamp seconds register, delegates read and write operations to harpsync."""

    def __init__(self, sync):
        super().__init__(HarpTypes.U32)
        self.sync = sync

    def __len__(self):
        return 1

    def read(self, typ):
        super().read(typ)
        return (self.sync.read()[0],)

    def write(self, typ, value):
        super().write(typ, value)
        self.sync.write(value[0])


class TimestampMicroReg(HarpRegister):
    """Timestamp microseconds register, delegates read operations to harpsync."""

    def __init__(self, sync):
        super().__init__(HarpTypes.U16)
        self.sync = sync

    def __len__(self):
        return 1

    def read(self, typ):
        super().read(typ)
        return (self.sync.read()[1],)

    def write(self, typ, value):
        raise TypeError("TimestampMicroReg.write().")


class PinRegister(HarpRegister):
    """Pin register, maps a GPIO pin to a harp register."""

    def __init__(self, pin):
        super().__init__(HarpTypes.U8)
        self.pin = pin

    def __len__(self):
        return 1

    def read(self, typ):
        super().read(typ)
        return (self.pin.value(),)

    def write(self, typ, value):
        super().write(typ, value)
        self.pin.value(value[0])
