from __future__ import annotations

__all__ = [
    "BinaryWriter",
]

import struct

from constrata.byte_order import ByteOrder
from .base import BinaryBase


class BinaryWriter(BinaryBase):
    """Manages `bytearray` binary data, with features like reserved offsets for later writing and big endian mode."""

    class Reserved(str):
        """Indicates a reserved name that should be used, rather than a string to pack."""

        def __repr__(self):
            return "AUTO_RESERVE" if not self else super().__repr__()

    AUTO_RESERVE = Reserved()  # reserve using `id(source)` and field name

    _array: bytearray
    reserved: dict[str, tuple[int, str]]

    def __init__(self, byte_order=ByteOrder.LittleEndian, long_varints: bool = True):
        super().__init__(byte_order, long_varints)
        self._array = bytearray()
        self.reserved = {}

    def pack(self, fmt: str, *values):
        self._array += struct.pack(self.parse_fmt(fmt), *values)

    def pack_at(self, offset: int, fmt: str, *values):
        packed = struct.pack(self.parse_fmt(fmt), *values)
        self._array[offset:offset + len(packed)] = packed

    def pack_z_string(self, value: str, encoding="utf-8"):
        """Pack null-terminated string `value` with `encoding` at current position.

        Two-char null terminator will be used for UTF-16 encodings.
        """
        terminator = b"\0\0" if encoding.replace("-", "").startswith("utf16") else b"\0"
        self._array += value.encode(encoding) + terminator

    def append(self, bytes_: bytearray | bytes):
        """Manually add existing binary data (e.g. a packed `BinaryStruct`) all at once."""
        self._array += bytes_

    def pad(self, size: int, char=b"\0"):
        if size > 0:
            self._array += char * size

    def pad_to_offset(self, offset: int, char=b"\0"):
        if self.position > offset:
            raise ValueError(f"Writer is already past offset {offset}: {self.position}")
        self.pad(offset - self.position, char=char)

    def pad_align(self, alignment: int, char=b"\0"):
        amount = alignment - self.position % alignment
        if amount != alignment:
            self.pad(amount, char=char)

    def reserve(self, name: str, fmt: str, obj: object = None):
        if obj is not None:
            name = f"{obj.__class__.__name__}__{id(obj)}({name})"
        if name in self.reserved:
            raise ValueError(f"Name {repr(name)} is already reserved in `BinaryWriter`.")
        fmt = self.parse_fmt(fmt)
        self.reserved[name] = (len(self._array), fmt)
        self._array += b"\0" * struct.calcsize(fmt)  # reserved space is nulls

    def mark_reserved_offset(self, name: str, fmt: str, offset: int, obj: object = None):
        """Does not pad array anywhere. User must write nulls to the reserved offset themselves."""
        if obj is not None:
            name = f"{obj.__class__.__name__}__{id(obj)}({name})"
        if name in self.reserved:
            raise ValueError(f"Name {repr(name)} is already reserved in `BinaryWriter`.")
        fmt = self.parse_fmt(fmt)
        self.reserved[name] = (offset, fmt)

    def fill(self, name: str, *values, obj: object = None):
        if not values:
            raise ValueError("No values given to fill.")
        if obj is not None:
            name = f"{obj.__class__.__name__}__{id(obj)}({name})"
            if name not in self.reserved:
                raise ValueError(f"Field {repr(name)} is not reserved by `{type(obj).__name__}` object.")
        elif name not in self.reserved:
            raise ValueError(f"Name {repr(name)} is not reserved in `BinaryWriter`.")
        offset, fmt = self.reserved[name]  # fmt endianness already specified
        try:
            packed = struct.pack(fmt, *values)
        except struct.error:
            raise ValueError(f"Error occurred when packing values to reserved offset with fmt {repr(fmt)}: {values}")
        self._array[offset:offset + len(packed)] = packed
        self.reserved.pop(name)  # pop after successful fill only

    def fill_with_position(self, name: str, obj: object = None) -> int:
        """Fill `name` (optionally also identified by `id(obj)` with current `writer.position`.

        Also returns current `writer.position` for convenience, as it is often needed right after.
        """
        self.fill(name, self.position, obj=obj)
        return self.position

    def block_copy(self, source_offset: int, dest_offset: int, size: int):
        """Copy one block of the buffer to another."""
        self._array[dest_offset:dest_offset + size] = self._array[source_offset:source_offset + size]

    def __bytes__(self) -> bytes:
        """Just checks that no reserved offsets remain, then converts stored `bytearray` to immutable `bytes`."""
        if self.reserved:
            reserved_values = "\n    ".join(self.reserved)
            raise ValueError(f"Reserved `BinaryWriter` offsets not filled:\n    {reserved_values}")
        return bytes(self.array)

    def __repr__(self):
        return f"BinaryWriter({self.array})"

    @property
    def position(self):
        """Return current 'position' of writer, which is just the number of bytes written so far."""
        return len(self._array)

    @property
    def position_hex(self) -> str:
        """Return current 'position' of writer as a hex string."""
        return hex(len(self._array))

    @property
    def array(self):
        """Return immutable copy of current array, for inspection/display only."""
        return bytes(self._array)
