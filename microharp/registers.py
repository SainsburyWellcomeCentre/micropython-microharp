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

import asyncio
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
# Persisted by R_RESET_DEV.SAVE and restored at boot when NVM is configured
# (see microharp.nvm).  Purely a flag — does not affect read/write access.
NONVOLATILE = const(0x08)


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

    def __init__(
        self,
        address: int,
        payload_type: int,
        n_elements: int = 1,
        access: int = READ_WRITE,
        on_read=None,
        on_write=None,
        name: str = "",
    ):
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
OP_OP_MODE_MASK = const(0x03)  # bits 0-1
OP_OP_MODE_STANDBY = const(0x00)
OP_OP_MODE_ACTIVE = const(0x01)
OP_OP_MODE_RESERVED = const(0x02)
OP_OP_MODE_SPEED = const(0x03)  # deprecated
OP_HEARTBEAT_EN = const(0x04)  # bit 2: emit R_HEARTBEAT events @ 1 Hz
OP_DUMP = const(0x08)  # bit 3: enumerate registers (write-only)
OP_MUTE_REPLIES = const(0x10)  # bit 4: suppress ALL replies
OP_VISUAL_EN = const(0x20)  # bit 5: enable visual indicators
OP_OPLED_EN = const(0x40)  # bit 6: LED tracks op-mode
OP_ALIVE_EN = const(0x80)  # bit 7: emit R_TIMESTAMP_SECOND (deprecated)

# Default OperationControl per spec (§R_OPERATION_CTRL diagram):
#   ALIVE_EN | OPLED_EN | VISUAL_EN | HEARTBEAT_EN, OP_MODE=Standby = 0xE4.
OP_DEFAULT = const(OP_ALIVE_EN | OP_OPLED_EN | OP_VISUAL_EN | OP_HEARTBEAT_EN)

# ResetDevice bits — per Harp Device.md §R_RESET_DEV.
RST_DEF = const(0x01)  # bit 0: erase NVM, reboot with defaults
RST_EE = const(0x02)  # bit 1: reboot from NVM (error if no NVM)
RST_SAVE = const(0x04)  # bit 2: save NONVOLATILE registers, reboot (error if no NVM)
RST_NAME_TO_DEFAULT = const(0x08)  # bit 3: restore default DeviceName, reboot
RST_UPDATE_FIRMWARE = const(0x20)  # bit 5: enter bootloader
RST_BOOT_DEF = const(0x40)  # bit 6 (read-only): booted with default values
RST_BOOT_EE = const(0x80)  # bit 7 (read-only): booted from NVM
_RST_ACTIONS = const(RST_DEF | RST_EE | RST_SAVE | RST_NAME_TO_DEFAULT | RST_UPDATE_FIRMWARE)

# ClockConfig bits — per Harp Device.md §R_CLOCK_CONFIG.
CLK_REP = const(0x01)  # bit 0: repeat Clock Input on Clock Output
CLK_GEN = const(0x02)  # bit 1: generate sync clock on Clock Output
CLK_REP_ABLE = const(0x08)  # bit 3 (read-only): device can repeat
CLK_GEN_ABLE = const(0x10)  # bit 4 (read-only): device can generate
CLK_UNLOCK = const(0x40)  # bit 6: R_TIMESTAMP_SECOND writable
CLK_LOCK = const(0x80)  # bit 7: R_TIMESTAMP_SECOND write-protected

# Heartbeat bits — per Harp Device.md §R_HEARTBEAT.
HB_IS_SYNCHRONIZED = const(0x01)  # bit 0
HB_IS_ACTIVE = const(0x02)  # bit 1

DEVICE_NAME_LEN = const(25)
UID_LEN = const(16)
TAG_LEN = const(8)
VERSION_LEN = const(32)

# Non-None return from an on_write handler → dispatcher sends a WRITE error.
_ERR = 1


def _fixed(data, n: int, what: str) -> bytes:
    """Return `data` as exactly `n` bytes, zero-padded.  Raises if too long."""
    data = bytes(data or b"")
    if len(data) > n:
        raise ValueError("%s longer than %d bytes" % (what, n))
    return data + bytes(n - len(data))


def _default_reboot():
    try:
        import machine

        machine.reset()
    except Exception:
        pass  # CPython tests / ports without machine.reset


def _default_bootloader():
    import machine

    machine.bootloader()


async def _after_reply(action, tx_q):
    """Run `action()` once the pending Write reply has left the TX queue.

    The spec requires the Write reply to be sent *before* a reboot.  The
    dispatcher enqueues that reply after on_write returns, so we yield,
    wait for the TX queue to drain (bounded), then give the transport a
    moment to push the bytes out.
    """
    await asyncio.sleep(0)
    for _ in range(50):
        if tx_q is None or tx_q.empty():
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    action()


def install_common_registers(
    bank: RegisterBank,
    *,
    who_am_i: int = 0,
    fw_major: int = 1,
    fw_minor: int = 0,
    fw_patch: int = 0,
    hw_major: int = 1,
    hw_minor: int = 0,
    hw_patch: int = 0,
    harp_major: int = 1,
    harp_minor: int = 13,
    harp_patch: int = 0,
    core_id: bytes = b"",
    interface_hash: bytes = b"",
    device_name: bytes = b"harp-mpy",
    serial_number: int = 0,
    uid=None,
    tag: bytes = b"",
    clock=None,
    tx_q=None,
    slab_pool=None,
    nvm=None,
    reboot=None,
    bootloader=None
):
    """Populate `bank` with the standard 0..31 registers.

    `clock` is the microharp.Clock instance; the timestamp registers'
    callbacks use it.  `tx_q` lets reboot-type actions (R_RESET_DEV,
    R_DEVICE_NAME) wait for their Write reply to drain before rebooting.
    `slab_pool` is accepted for backward compatibility and unused.

    `nvm` is an optional `microharp.nvm.Nvm`.  Without it the device has
    no persistent storage: R_RESET_DEV.SAVE / RST_EE reply with an error,
    R_DEVICE_NAME writes keep the default name, and reads of R_RESET_DEV
    report BOOT_DEF.  Call `microharp.nvm.apply_boot_state(bank, nvm)`
    once all registers exist to restore stored values.

    `uid` defaults to `machine.unique_id()` (zero-padded to 16 bytes) when
    available.  `tag` is up to 8 opaque bytes (e.g. a short git hash).
    `core_id` is a 3-character MCU core code and `interface_hash` the
    20-byte SHA-1 of device.yml; both default to zeros.

    `reboot` / `bootloader` are zero-arg callables (defaults:
    `machine.reset` / `machine.bootloader`), injectable for testing.
    """
    import struct

    if reboot is None:
        reboot = _default_reboot
    if bootloader is None:
        try:
            import machine

            if hasattr(machine, "bootloader"):
                bootloader = _default_bootloader
        except ImportError:
            pass

    def _add(addr, pt, n=1, access=READ_WRITE, name="", on_write=None, on_read=None):
        return bank.add(RegisterEntry(addr, pt, n, access, on_read, on_write, name))

    def _schedule(action):
        asyncio.create_task(_after_reply(action, tx_q))

    # ---- 000 WHO_AM_I ------------------------------------------------------
    r = _add(R_WHO_AM_I, PT_U16, access=READ_ONLY, name="WhoAmI")
    struct.pack_into("<H", r.storage, 0, who_am_i)

    # ---- 001..007 legacy version registers (deprecated, kept readable) ----
    r = _add(R_HW_VERSION_H, PT_U8, access=READ_ONLY, name="HwVersionH")
    r.storage[0] = hw_major
    r = _add(R_HW_VERSION_L, PT_U8, access=READ_ONLY, name="HwVersionL")
    r.storage[0] = hw_minor

    _add(R_ASSEMBLY_VERSION, PT_U8, access=READ_ONLY, name="AssemblyVersion")

    r = _add(R_HARP_VERSION_H, PT_U8, access=READ_ONLY, name="CoreVersionH")
    r.storage[0] = harp_major
    r = _add(R_HARP_VERSION_L, PT_U8, access=READ_ONLY, name="CoreVersionL")
    r.storage[0] = harp_minor

    r = _add(R_FW_VERSION_H, PT_U8, access=READ_ONLY, name="FwVersionH")
    r.storage[0] = fw_major
    r = _add(R_FW_VERSION_L, PT_U8, access=READ_ONLY, name="FwVersionL")
    r.storage[0] = fw_minor

    # ---- 008 / 009 TIMESTAMP_SECOND / TIMESTAMP_MICRO -----------------------
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

    # The host re-anchors the device clock by writing TimestampSecond,
    # unless R_CLOCK_CONFIG.CLK_LOCK is set (-> error reply).  on_write
    # re-anchors as early in the handler chain as possible; residual
    # constant latency can be compensated via clock.set_seconds(latency_us=).
    async def _write_secs(reg, payload, _clk=clock, _bank=bank):
        cc = _bank.get(R_CLOCK_CONFIG)
        if cc is not None and (cc.storage[0] & CLK_LOCK):
            return _ERR
        new = struct.unpack_from("<I", payload, 0)[0]
        if _clk is not None:
            _clk.set_seconds(new)
        struct.pack_into("<I", reg.storage, 0, new & 0xFFFFFFFF)
        return None

    _add(
        R_TIMESTAMP_SECOND, PT_U32, access=READ_WRITE, name="TimestampSecond", on_read=_read_secs, on_write=_write_secs
    )
    _add(R_TIMESTAMP_MICRO, PT_U16, access=READ_ONLY, name="TimestampTicks", on_read=_read_ticks)

    # ---- 010 OPERATION_CTRL -------------------------------------------------
    # Spec default 0xE4 = ALIVE_EN | OPLED_EN | VISUAL_EN | HEARTBEAT_EN,
    # OP_MODE=Standby.  Only Standby and Active are supported; Reserved (2)
    # and the deprecated Speed mode (3) get an error reply and leave the
    # register unchanged.  DUMP ordering (Write reply, then the dump, then
    # clear) is handled by the dispatcher.  NotConnected -> Standby is
    # handled by HarpDevice's CDC line-state callback.
    async def _op_on_write(reg, payload):
        new = payload[0]
        mode = new & OP_OP_MODE_MASK
        if mode != OP_OP_MODE_STANDBY and mode != OP_OP_MODE_ACTIVE:
            return _ERR
        reg.storage[0] = new
        return None

    op = _add(R_OPERATION_CONTROL, PT_U8, access=READ_WRITE, name="OperationControl", on_write=_op_on_write)
    op.storage[0] = OP_DEFAULT

    # ---- 011 RESET_DEV ------------------------------------------------------
    # Storage holds only the read-only boot-source bits (BOOT_DEF / BOOT_EE);
    # action bits always read back as 0.  One action bit per write.  Every
    # action that reboots does so only after the Write reply has drained.
    async def _reset_on_write(reg, payload, _bank=bank, _nvm=nvm):
        v = payload[0]
        if v & ~_RST_ACTIONS & 0xFF:
            return _ERR  # read-only BOOT_* bits or reserved bit 4
        if v == 0:
            return None
        if v & (v - 1):
            return _ERR  # more than one action requested
        try:
            if v == RST_DEF:
                if _nvm is not None:
                    _nvm.erase()
                _schedule(reboot)
            elif v == RST_EE:
                if _nvm is None:
                    return _ERR
                _schedule(reboot)
            elif v == RST_SAVE:
                if _nvm is None:
                    return _ERR
                _nvm.write(_nvm.snapshot(_bank))
                _schedule(reboot)
            elif v == RST_NAME_TO_DEFAULT:
                if _nvm is not None:
                    entries = _nvm.read()
                    if entries is not None and R_DEVICE_NAME in entries:
                        del entries[R_DEVICE_NAME]
                        _nvm.write(entries)
                _schedule(reboot)
            else:  # RST_UPDATE_FIRMWARE
                if bootloader is None:
                    return _ERR
                _schedule(bootloader)
        except OSError:
            return _ERR
        return None

    rd = _add(R_RESET_DEV, PT_U8, access=READ_WRITE, name="ResetDevice", on_write=_reset_on_write)
    rd.storage[0] = RST_BOOT_DEF  # updated by nvm.apply_boot_state()

    # ---- 012 DEVICE_NAME ----------------------------------------------------
    # Zero-padded 25-byte string, NONVOLATILE.  With NVM a write persists
    # the new name and reboots; without NVM the write is acknowledged with
    # the unchanged default value (spec).
    async def _name_on_write(reg, payload, _nvm=nvm):
        if _nvm is None:
            return None
        raw = bytes(payload)
        end = raw.find(b"\x00")
        if end < 0:
            end = DEVICE_NAME_LEN - 1  # force a terminating NUL
        new = raw[:end] + bytes(DEVICE_NAME_LEN - end)
        try:
            entries = _nvm.read() or {}
            entries[R_DEVICE_NAME] = new
            _nvm.write(entries)
        except OSError:
            return _ERR
        reg.storage[:] = new
        _schedule(reboot)
        return None

    rn = bank.add(
        RegisterEntry(
            R_DEVICE_NAME,
            PT_U8,
            n_elements=DEVICE_NAME_LEN,
            access=READ_WRITE | NONVOLATILE,
            on_write=_name_on_write,
            name="DeviceName",
        )
    )
    rn.storage[:] = _fixed(device_name[: DEVICE_NAME_LEN - 1], DEVICE_NAME_LEN, "device_name")

    # ---- 013 SERIAL_NUMBER (deprecated) -------------------------------------
    rsn = _add(R_SERIAL_NUMBER, PT_U16, access=READ_ONLY, name="SerialNumber")
    struct.pack_into("<H", rsn.storage, 0, serial_number)

    # ---- 014 CLOCK_CONFIG ---------------------------------------------------
    # Boots unlocked.  CLK_LOCK / CLK_UNLOCK gate writes to
    # R_TIMESTAMP_SECOND.  This firmware has no Clock Output connector, so
    # GEN_ABLE / REP_ABLE read 0 and CLK_GEN / CLK_REP are ignored (read
    # back as 0).  Setting LOCK and UNLOCK together is an error.
    async def _clk_on_write(reg, payload):
        v = payload[0]
        if (v & CLK_LOCK) and (v & CLK_UNLOCK):
            return _ERR
        cur = reg.storage[0]
        if v & CLK_LOCK:
            cur = (cur & ~CLK_UNLOCK) | CLK_LOCK
        elif v & CLK_UNLOCK:
            cur = (cur & ~CLK_LOCK) | CLK_UNLOCK
        reg.storage[0] = cur & (CLK_LOCK | CLK_UNLOCK | CLK_GEN_ABLE | CLK_REP_ABLE)
        return None

    rc = _add(R_CLOCK_CONFIG, PT_U8, access=READ_WRITE, name="ClockConfig", on_write=_clk_on_write)
    rc.storage[0] = CLK_UNLOCK

    # ---- 015 Reserved (formerly TIMESTAMP_OFFSET) ---------------------------
    _add(R_TIMESTAMP_OFFSET, PT_U8, access=READ_ONLY, name="Reserved")

    # ---- 016 UID ------------------------------------------------------------
    if uid is None:
        try:
            import machine

            uid = machine.unique_id()[:UID_LEN]
        except Exception:
            uid = b""
    ru = bank.add(RegisterEntry(R_UID, PT_U8, n_elements=UID_LEN, access=READ_ONLY, name="UID"))
    ru.storage[:] = _fixed(uid, UID_LEN, "uid")

    # ---- 017 TAG ------------------------------------------------------------
    rt = bank.add(RegisterEntry(R_TAG, PT_U8, n_elements=TAG_LEN, access=READ_ONLY, name="Tag"))
    rt.storage[:] = _fixed(tag, TAG_LEN, "tag")

    # ---- 018 HEARTBEAT ------------------------------------------------------
    # Bit 0 IS_SYNCHRONIZED, bit 1 IS_ACTIVE.  Refreshed on every read; the
    # heartbeat task emits it at 1 Hz in Active mode when HEARTBEAT_EN.
    async def _read_hb(reg, _bank=bank, _clock=clock):
        struct.pack_into("<H", reg.storage, 0, heartbeat_value(_bank, _clock))

    _add(R_HEARTBEAT, PT_U16, access=READ_ONLY, name="Heartbeat", on_read=_read_hb)

    # ---- 019 VERSION --------------------------------------------------------
    #   [0..2]   PROTOCOL  (major, minor, patch)
    #   [3..5]   FIRMWARE  (major, minor, patch)
    #   [6..8]   HARDWARE  (major, minor, patch)
    #   [9..11]  CORE_ID   (3 chars)
    #   [12..31] INTERFACE_HASH (SHA-1 of device.yml; all-zero = unset)
    rv = bank.add(RegisterEntry(R_VERSION, PT_U8, n_elements=VERSION_LEN, access=READ_ONLY, name="Version"))
    rv.storage[0:9] = bytes(
        (harp_major, harp_minor, harp_patch, fw_major, fw_minor, fw_patch, hw_major, hw_minor, hw_patch)
    )
    rv.storage[9:12] = _fixed(core_id, 3, "core_id")
    rv.storage[12:32] = _fixed(interface_hash, 20, "interface_hash")

    return bank


def heartbeat_value(bank: RegisterBank, clock=None) -> int:
    """Current R_HEARTBEAT value (IS_SYNCHRONIZED | IS_ACTIVE bits)."""
    op = bank.get(R_OPERATION_CONTROL)
    v = 0
    if op is not None and (op.storage[0] & OP_OP_MODE_MASK) == OP_OP_MODE_ACTIVE:
        v |= HB_IS_ACTIVE
    if clock is not None and clock.synced:
        v |= HB_IS_SYNCHRONIZED
    return v
