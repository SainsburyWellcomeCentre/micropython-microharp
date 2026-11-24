"""Non-volatile register storage (Device.md §R_RESET_DEV / §R_DEVICE_NAME).

A tiny file-backed store on the MicroPython filesystem.  Only registers
flagged with the `NONVOLATILE` access bit are persisted (the core
`R_DEVICE_NAME` plus any application register the app marks).

File layout (binary, little-endian):

    b"HNV1"                      magic + format version
    repeat:
        addr : U8
        len  : U8
        data : len bytes          raw register storage

Writes go to `<path>.tmp` first and are then renamed over `<path>`, so a
power cut mid-write leaves the previous image intact.
"""

import os

from .registers import NONVOLATILE, R_RESET_DEV, RST_BOOT_DEF, RST_BOOT_EE

_MAGIC = b"HNV1"


class Nvm:
    """File-backed persistent storage for NONVOLATILE registers."""

    __slots__ = ("path",)

    def __init__(self, path: str = "/harp_nvm.bin"):
        self.path = path

    def read(self):
        """Return `{addr: bytes}` from the stored image, or None if absent/corrupt."""
        try:
            with open(self.path, "rb") as f:
                raw = f.read()
        except OSError:
            return None
        if raw[:4] != _MAGIC:
            return None
        entries = {}
        i = 4
        n = len(raw)
        while i + 2 <= n:
            addr = raw[i]
            ln = raw[i + 1]
            i += 2
            if i + ln > n:
                return None  # truncated
            entries[addr] = bytes(raw[i : i + ln])
            i += ln
        return entries

    def write(self, entries: dict):
        """Replace the stored image with `entries` (`{addr: bytes}`).  Raises OSError."""
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(_MAGIC)
            for addr in entries:
                data = entries[addr]
                f.write(bytes((addr, len(data))))
                f.write(data)
        try:
            os.remove(self.path)
        except OSError:
            pass
        os.rename(tmp, self.path)

    def erase(self):
        """Delete the stored image; subsequent boots use defaults."""
        try:
            os.remove(self.path)
        except OSError:
            pass

    @staticmethod
    def snapshot(bank) -> dict:
        """Current storage of every NONVOLATILE register in `bank`."""
        return {r.address: bytes(r.storage) for r in bank if r.access & NONVOLATILE}

    def load_into(self, bank) -> bool:
        """Copy stored values into matching NONVOLATILE registers.

        Entries whose address is unknown, not NONVOLATILE, or whose length
        no longer matches the register are skipped.  Returns True if an
        image was found (i.e. the device "booted from persistent storage").
        """
        entries = self.read()
        if entries is None:
            return False
        for addr in entries:
            reg = bank.get(addr)
            data = entries[addr]
            if reg is not None and (reg.access & NONVOLATILE) and len(data) == len(reg.storage):
                reg.storage[:] = data
        return True


def apply_boot_state(bank, nvm):
    """Restore NONVOLATILE registers and set R_RESET_DEV's BOOT_EE / BOOT_DEF.

    Call once, after every application register has been added and before
    the dispatcher starts serving requests.  Without NVM the device always
    reports BOOT_DEF.
    """
    booted_ee = nvm is not None and nvm.load_into(bank)
    rd = bank.get(R_RESET_DEV)
    if rd is not None:
        rd.storage[0] = RST_BOOT_EE if booted_ee else RST_BOOT_DEF
    return booted_ee
