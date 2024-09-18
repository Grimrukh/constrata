from __future__ import annotations

import typing as tp
from enum import StrEnum

if tp.TYPE_CHECKING:
    from .streams.reader import BinaryReader


class ByteOrder(StrEnum):
    NativeAutoAligned = "@"
    NativeNotAutoAligned = "="  # standard size, no alignment
    LittleEndian = "<"  # default
    BigEndian = ">"
    Network = "!"  # big-endian

    def get_utf_16_encoding(self) -> str:
        """Get UTF-16 encoding string based on byte order."""
        if self in {ByteOrder.BigEndian, ByteOrder.Network}:
            return "utf-16-be"
        return "utf-16-le"

    @classmethod
    def big_endian_bool(cls, is_big_endian: bool):
        """Utility shortcut for switching between big/little endian based on a bool (common in game formats)."""
        return cls.BigEndian if is_big_endian else cls.LittleEndian

    @classmethod
    def from_reader_peek(
        cls, reader: BinaryReader, size: int, bytes_ahead: int, big_endian_read: bytes, little_endian_read: bytes
    ) -> ByteOrder:
        peeked = reader.peek(size, bytes_ahead)
        if peeked == big_endian_read:
            return ByteOrder.BigEndian
        elif peeked == little_endian_read:
            return ByteOrder.LittleEndian
        else:
            raise ValueError(f"Could not determine byte order from bytes: {peeked}")
