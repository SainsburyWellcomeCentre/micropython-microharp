from .device import HarpDevice
from .message import HarpMessage


_attrs = {
    "HarpClock": "clock",
    "HarpDevice": "device",
    "HarpEvent": "event",
    "PinEvent": "event",
    "PeriodicEvent": "event",
    "LooseEvent": "event",
    "HarpMessage": "message",
    "HarpRxMessage": "message",
    "HarpTxMessage": "message",
    "HarpRegister": "register",
    "ReadWriteReg": "register",
    "ReadOnlyReg": "register",
    "OperationalCtrlReg": "register",
    "TimestampSecondReg": "register",
    "TimestampMicroReg": "register",
    "PinRegister": "register",
    "HarpTypes": "type",
}


def __getattr__(attr):
    if attr in _attrs:
        module_name = _attrs[attr]
        module = __import__(f"microharp.{module_name}", None, None, [attr])
        return getattr(module, attr)
    raise AttributeError(f"module 'microharp' has no attribute '{attr}'")
