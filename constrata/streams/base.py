from __future__ import annotations

import struct
from functools import lru_cache

from constrata.byte_order import ByteOrder


class BinaryBase:

    # Special format characters that become 'iI' or 'qQ' depending on `var_int_size`.
    VAR_INT = "v"
    VAR_UINT = "V"

    # Byte order to use when an overriding byte order character is not specified in a read/write format string.
    byte_order: ByteOrder
    # Determines size of 'v' (signed) and 'V' (unsigned) integer format characters: 4 (False) or 8 (True) bytes.
    # Defaults to `None` and will cause an error if either character is encountered.
    long_varints: bool | None

    def __init__(self, byte_order=ByteOrder.LittleEndian, long_varints: bool = None):
        self.byte_order = ByteOrder(byte_order)
        self.long_varints = long_varints

    def parse_fmt(self, fmt: str, byte_order: ByteOrder = None, long_varints: bool = None) -> str:
        """Insert default byte order character and replace 'v'/'V' var int characters."""
        if byte_order is None:
            byte_order = self.byte_order  # cannot be `None`
        if long_varints is None:
            long_varints = self.long_varints  # `None` permitted if not encountered in `fmt`

        if fmt[0] not in "@=><!":
            fmt = byte_order.value + fmt
        if self.VAR_INT in fmt or self.VAR_UINT in fmt:
            if long_varints is None:
                raise ValueError(
                    f"Cannot parse format string '{fmt}' with 'v' or 'V' characters without setting `long_varints` "
                    f"to `True` or `False`."
                )
            elif long_varints:
                fmt = fmt.replace(self.VAR_INT, "q").replace(self.VAR_UINT, "Q")
            else:
                fmt = fmt.replace(self.VAR_INT, "i").replace(self.VAR_UINT, "I")
        return fmt

    def calcsize(self, fmt: str, byte_order: ByteOrder = None, long_varints: bool = None) -> int:
        """Calculate fmt struct size after parsing it."""
        return self.calcsize_parsed(self.parse_fmt(fmt, byte_order, long_varints))

    @staticmethod
    @lru_cache(256)
    def calcsize_parsed(parsed_fmt: str) -> int:
        """Calculate fmt struct size known to already be parsed. Uses caching."""
        return struct.calcsize(parsed_fmt)

    def get_utf_16_encoding(self) -> str:
        """Get the appropriate UTF-16 string encoding for active byte order."""
        return self.byte_order.get_utf_16_encoding()
