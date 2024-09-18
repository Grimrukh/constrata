from __future__ import annotations

import struct

from constrata.byte_order import ByteOrder


class BinaryBase:

    # Special format characters that become 'iI' or 'qQ' depending on `var_int_size`.
    VAR_INT = "v"
    VAR_UINT = "V"
    default_byte_order: ByteOrder
    long_varints: bool  # 4 (False) or 8 (True); determines size of 'v' and 'V' format characters

    def __init__(self, byte_order=ByteOrder.LittleEndian, long_varints=True):
        self.default_byte_order = ByteOrder(byte_order)
        self.long_varints = long_varints

    def parse_fmt(self, fmt: str) -> str:
        """Insert default byte order and replace 'vV' var int characters."""
        if fmt[0] not in "@=><!":
            fmt = self.default_byte_order.value + fmt
        if self.long_varints:
            fmt = fmt.replace("v", "q").replace("V", "Q")
        else:
            fmt = fmt.replace("v", "i").replace("V", "I")
        return fmt

    def calcsize(self, fmt: str) -> int:
        """Calculate fmt struct size after parsing it."""
        return struct.calcsize(self.parse_fmt(fmt))

    def get_utf_16_encoding(self) -> str:
        return self.default_byte_order.get_utf_16_encoding()
