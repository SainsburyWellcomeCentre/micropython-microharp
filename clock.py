"""
Harp timestamp clock class.
"""

from micropython import const
import time
from machine import Timer
import micropython


class HarpClock:
    """
    A system tick counter that follows the Harp timestamp format.
    """

    # The value that system tick wraps around after 30-bits"""
    TICK_MAX = const(1_073_741)  # ms
    READ_OFFSET = const(10)
    UART_OFFSET = const(272)

    def __init__(self):
        self.custom_offset = 0
        self._offset_s = 0
        self._offset_us = 0
        self.overflow_count = 0
        self.timer = Timer(period=self.TICK_MAX, mode=Timer.PERIODIC, callback=self._count)
        self.buf = bytearray(6)

    def read(self):
        """
        Returns a tuple of Harp timestamp (Seconds, Microseconds/32)
        The full Timestamp(s) = [Seconds] + [Microseconds] * 32 * 1e-6
        """
        self._read_count(time.ticks_us())

        return self.buf

    def write(self, buf):
        """
        Overwriting the Harp timestamp in microsecond
        """
        self._offset_s = self._unpack(buf) - self.overflow_count + 1
        self._offset_us = -self.UART_OFFSET - time.ticks_us() + self.READ_OFFSET

    def _count(self, t):
        self.overflow_count += 1

    @micropython.viper
    def _unpack(self, buf: ptr8) -> int:
        sum = 0
        sum += (buf[0] << 24) + (buf[5] << 16) + (buf[4] << 8) + buf[3]
        return sum

    @micropython.viper
    def _read_count(self, tick: int):
        tick_s = int(self._offset_s + self.overflow_count)
        tick_us = int(self._offset_us - self.READ_OFFSET) + tick
        buf = ptr32(self.buf)
        buf[0] = (tick_us // 1_000_000) + tick_s
        buf[1] = ((tick_us % 1_000_000) >> 5) & 0xFFFF
