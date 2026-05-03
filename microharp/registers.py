"""Register bank: typed addressable storage with handlers.

Each register has:
    address       : 0..255
    payload_type  : PT_U8, PT_U16, ... (from framing)
    n_elements    : 1 for scalar, >1 for arrays (e.g. 8-channel digital state)
    access        : bitmask of READ_ONLY / WRITE_ONLY / READ_WRITE / EVENT
    storage       : bytearray sized to (n_elements * elem_size)
    on_write      : optional async callback(reg, payload_mv) -> Optional[errno]
    on_read       : optional async callback(reg) -> None    (refresh storage)

The default behaviour is "memory-backed": reads serve `storage`, writes
overwrite `storage`.  Callbacks let an app react to writes (e.g. set GPIO,
start a peripheral) or recompute on read (e.g. ADC sample on demand).

Common registers (addresses 0..31 per the Harp Device spec) are populated
by `install_common_registers()`.
"""

from micropython import const
from .framing import (
    PT_U8,
    PT_U16,
    PT_U32,
    payload_elem_size,
)

# Access bits
READ_ONLY = const(0x01)
WRITE_ONLY = const(0x02)
READ_WRITE = const(0x03)
EVENT = const(0x04)


class RegisterEntry:
    """One register's metadata and storage."""

    __slots__ = (
        "address",
        "payload_type",
        "n_elements",
        "access",
        "storage",
        "on_read",
        "on_write",
        "name",
    )

    def __init__(self, address: int, payload_type: int, n_elements: int = 1, access: int = READ_WRITE, on_read=None, on_write=None, name: str = ""):
        self.address = address
        self.payload_type = payload_type
        self.n_elements = n_elements
        self.access = access
        self.on_read = on_read
        self.on_write = on_write
        self.name = name
        self.storage = bytearray(n_elements * payload_elem_size(payload_type))

    @property
    def payload_len(self):
        return len(self.storage)


class RegisterBank:
    """Flat dict-backed table.  Lookup is O(1) on small int keys."""

    __slots__ = ("_table",)

    def __init__(self):
        self._table = {}

    def add(self, reg: RegisterEntry):
        if reg.address in self._table:
            raise ValueError("register address %d already in use" % reg.address)
        self._table[reg.address] = reg
        return reg

    def get(self, address: int):
        return self._table.get(address)

    def __contains__(self, address: int):
        return address in self._table

    def __iter__(self):
        return iter(self._table.values())


# ---------------------------------------------------------------------------
# Common registers
# ---------------------------------------------------------------------------
#
# Per harp-tech/protocol/Device.md.  Addresses below 32 are reserved for
# these.  We populate them with sensible defaults; the app is expected to
# overwrite WHO_AM_I / SERIAL_NUMBER / DEVICE_NAME at startup.

R_WHO_AM_I = const(0x00)  # U16
R_HW_VERSION_H = const(0x01)  # U8
R_HW_VERSION_L = const(0x02)  # U8
R_ASSEMBLY_VERSION = const(0x03)  # U8
R_HARP_VERSION_H = const(0x04)  # U8
R_HARP_VERSION_L = const(0x05)  # U8
R_FW_VERSION_H = const(0x06)  # U8
R_FW_VERSION_L = const(0x07)  # U8
R_TIMESTAMP_SECOND = const(0x08)  # U32  -- updated on read
R_TIMESTAMP_MICRO = const(0x09)  # U16  -- updated on read (ticks, 32 us)
R_OPERATION_CONTROL = const(0x0A)  # U8
R_RESET_DEV = const(0x0B)  # U8   -- write-only command
R_DEVICE_NAME = const(0x0C)  # U8 array (string)
R_SERIAL_NUMBER = const(0x0D)  # U16
R_CLOCK_CONFIG = const(0x0E)  # U8
R_TIMESTAMP_OFFSET = const(0x0F)  # U8
R_UID = const(0x10)  # U8 array (16 bytes)
R_TAG = const(0x11)  # U8 array (8 bytes)
R_HEARTBEAT = const(0x12)  # U16
R_VERSION = const(0x13)  # U8

# OperationControl bits — per Harp Device.md §R_OPERATION_CTRL.
OP_OP_MODE_MASK     = const(0x03)   # bits 0-1
OP_OP_MODE_STANDBY  = const(0x00)
OP_OP_MODE_ACTIVE   = const(0x01)
OP_OP_MODE_RESERVED = const(0x02)
OP_OP_MODE_SPEED    = const(0x03)   # deprecated
OP_HEARTBEAT_EN     = const(0x04)   # bit 2: emit R_HEARTBEAT events @ 1 Hz
OP_DUMP             = const(0x08)   # bit 3: enumerate registers (write-only)
OP_MUTE_REPLIES     = const(0x10)   # bit 4: suppress ALL replies
OP_VISUAL_EN        = const(0x20)   # bit 5: enable visual indicators
OP_OPLED_EN         = const(0x40)   # bit 6: LED tracks op-mode
OP_ALIVE_EN         = const(0x80)   # bit 7: emit R_TIMESTAMP_SECOND (deprecated)

# Default OperationControl per spec (§R_OPERATION_CTRL diagram):
#   ALIVE_EN | OPLED_EN | VISUAL_EN | HEARTBEAT_EN, OP_MODE=Standby = 0xE4.
OP_DEFAULT = const(OP_ALIVE_EN | OP_OPLED_EN | OP_VISUAL_EN | OP_HEARTBEAT_EN)


def install_common_registers(
    bank: RegisterBank,
    *,
    who_am_i: int = 0,
    fw_major: int = 1,
    fw_minor: int = 0,
    hw_major: int = 1,
    hw_minor: int = 0,
    device_name: bytes = b"harp-mpy",
    serial_number: int = 0,
    clock=None,
    tx_q=None,
    slab_pool=None
):
    """Populate `bank` with the standard 0..31 registers.

    `clock` is the microharp.Clock instance; the timestamp register's `on_read`
    callback uses it to refresh storage just-in-time.

    `tx_q` and `slab_pool` are required for the OperationControl `DUMP`
    handler — it walks every register and pushes a READ reply for each
    onto the TX queue, so it needs the same slab pool the dispatcher uses.
    """
    import struct

    def _add(addr, pt, n=1, access=READ_WRITE, name="", on_write=None, on_read=None):
        return bank.add(RegisterEntry(addr, pt, n, access, on_read, on_write, name))

    # WHO_AM_I  ── identifies the device class.
    r = _add(R_WHO_AM_I, PT_U16, access=READ_ONLY, name="WhoAmI")
    struct.pack_into("<H", r.storage, 0, who_am_i)

    r = _add(R_HW_VERSION_H, PT_U8, access=READ_ONLY, name="HwVersionH")
    r.storage[0] = hw_major
    r = _add(R_HW_VERSION_L, PT_U8, access=READ_ONLY, name="HwVersionL")
    r.storage[0] = hw_minor

    _add(R_ASSEMBLY_VERSION, PT_U8, access=READ_ONLY, name="AssemblyVersion")
    _add(R_HARP_VERSION_H, PT_U8, access=READ_ONLY, name="CoreVersionH")
    _add(R_HARP_VERSION_L, PT_U8, access=READ_ONLY, name="CoreVersionL")

    r = _add(R_FW_VERSION_H, PT_U8, access=READ_ONLY, name="FwVersionH")
    r.storage[0] = fw_major
    r = _add(R_FW_VERSION_L, PT_U8, access=READ_ONLY, name="FwVersionL")
    r.storage[0] = fw_minor

    # TIMESTAMP_SECOND is read-on-demand from the Clock.
    async def _read_secs(reg, _clk=clock):
        if _clk is None:
            return
        secs, _ = _clk.now()
        struct.pack_into("<I", reg.storage, 0, secs & 0xFFFFFFFF)

    async def _read_ticks(reg, _clk=clock):
        if _clk is None:
            return
        _, ticks = _clk.now()
        struct.pack_into("<H", reg.storage, 0, ticks & 0xFFFF)

    # TimestampSecond is read-write per spec — the host re-anchors the
    # device clock by writing this register.  on_write captures
    # `_ticks_us()` as early in the handler chain as possible (here in
    # the on_write callback) so the residual constant latency between
    # dispatch's clock.now() and our anchor update stays small.  If
    # you've measured that residual, pass it via
    # `clock.set_seconds(..., latency_us=N)` from a wrapping handler.
    async def _write_secs(reg, payload, _clk=clock):
        if _clk is None:
            return None
        new = struct.unpack_from("<I", payload, 0)[0]
        _clk.set_seconds(new)
        struct.pack_into("<I", reg.storage, 0, new & 0xFFFFFFFF)
        return None

    _add(R_TIMESTAMP_SECOND, PT_U32, access=READ_WRITE,
         name="TimestampSecond", on_read=_read_secs, on_write=_write_secs)
    _add(R_TIMESTAMP_MICRO, PT_U16, access=READ_ONLY, name="TimestampTicks", on_read=_read_ticks)

    # OperationControl: spec default 0xE4 = ALIVE_EN | OPLED_EN |
    # VISUAL_EN | HEARTBEAT_EN, OP_MODE=Standby.  Per Device.md §"Operation
    # Mode": NotConnected → must enter Standby.  HarpDevice's CDC
    # line-state callback handles that transition explicitly.
    op = _add(R_OPERATION_CONTROL, PT_U8, access=READ_WRITE, name="OperationControl")
    op.storage[0] = OP_DEFAULT

    # The DUMP bit, when set in a Write request, instructs the Device to
    # send a sequence of Read replies — one per register — *after* the
    # Write reply (Device.md §Request-Reply).  We can't enqueue the dump
    # before the Write reply from inside on_write (Dispatcher posts the
    # WRITE reply *after* on_write returns).  Instead, on_write just
    # records that a dump is pending; the dispatcher handles the order.
    if tx_q is not None and slab_pool is not None:

        async def _op_on_write(reg, payload):
            new = payload[0]
            # Apply the new value but keep the DUMP bit visible to the
            # dispatcher so it can do the right ordering.  The bit is
            # auto-cleared right after the dump completes.
            reg.storage[0] = new
            return None

        op.on_write = _op_on_write

    # ResetDevice: write-only command.  Any non-zero write triggers a soft
    # reset (machine.reset()).  Caller may override.
    async def _reset_on_write(reg, payload):
        if payload[0]:
            try:
                import machine

                machine.reset()
            except Exception:
                pass  # CPython tests / non-machine port
        return None

    rd = _add(R_RESET_DEV, PT_U8, access=WRITE_ONLY, name="ResetDevice")
    rd.on_write = _reset_on_write

    # DeviceName: U8 array.  Pad/truncate to fit a fixed slot.
    # name_len = min(len(device_name), 25)
    name_len = 25
    rn = bank.add(RegisterEntry(R_DEVICE_NAME, PT_U8, n_elements=name_len, access=READ_WRITE, name="DeviceName"))
    rn.storage[:name_len] = device_name + bytes(name_len - len(device_name))

    rsn = _add(R_SERIAL_NUMBER, PT_U16, access=READ_ONLY, name="SerialNumber")
    struct.pack_into("<H", rsn.storage, 0, serial_number)

    _add(R_CLOCK_CONFIG, PT_U8, access=READ_WRITE, name="ClockConfig")

    # Newer common registers (Harp Device 1.13+).
    _add(R_TIMESTAMP_OFFSET, PT_U8, access=READ_ONLY, name="TimestampOffset")
    bank.add(RegisterEntry(R_UID, PT_U8, n_elements=16, access=READ_ONLY, name="UID"))
    bank.add(RegisterEntry(R_TAG, PT_U8, n_elements=8,  access=READ_ONLY, name="Tag"))

    # R_HEARTBEAT: U16, read-only.  Bits 0=IS_ACTIVE, 1=IS_SYNCHRONIZED.
    # Refresh just-in-time on every read so the host always sees current
    # state; the heartbeat task also calls _read_hb before emitting.
    async def _read_hb(reg, _bank=bank, _clock=clock):
        op = _bank.get(R_OPERATION_CONTROL)
        is_active = bool(op and (op.storage[0] & OP_OP_MODE_MASK) == OP_OP_MODE_ACTIVE)
        is_synced = bool(_clock and _clock.synced)
        v = (1 if is_active else 0) | (2 if is_synced else 0)
        struct.pack_into("<H", reg.storage, 0, v)

    _add(R_HEARTBEAT, PT_U16, access=READ_ONLY, name="Heartbeat",
         on_read=_read_hb)

    # R_VERSION: 32-byte U8 array per spec §R_VERSION.  Layout:
    #   [0..2]  PROTOCOL  (semver: major, minor, patch)
    #   [3..5]  FIRMWARE  (semver)
    #   [6..8]  HARDWARE  (semver)
    #   [9..11] CORE_ID   (3 chars)
    #   [12..31] INTERFACE_HASH (SHA-1 of device.yml; little-endian; 0 = unset)
    rv = bank.add(RegisterEntry(R_VERSION, PT_U8, n_elements=32,
                                access=READ_ONLY, name="Version"))
    rv.storage[3] = fw_major
    rv.storage[4] = fw_minor
    rv.storage[6] = hw_major
    rv.storage[7] = hw_minor
    # PROTOCOL, CORE_ID, INTERFACE_HASH default to zero per spec.

    return bank
