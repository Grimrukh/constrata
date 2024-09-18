from __future__ import annotations

import struct

from constrata.field_types.type_info import PRIMITIVE_FMT_SIZE_MINMAX
from .reader import BinaryReader
from .writer import BinaryWriter


class _BitFieldBase:

    __slots__ = ("_field", "_fmt", "_offset")

    _field: str
    _fmt: str
    _offset: int

    def __init__(self):
        self._field = ""
        self._fmt = ""
        self._offset = 0

    def clear(self):
        self._field = ""
        self._fmt = ""
        self._offset = 0

    @property
    def empty(self) -> bool:
        return not self._field


class BitFieldReader(_BitFieldBase):
    """Manages partial reading of one or more bytes, by keeping unused bits from previous reads and only consuming new
    bytes from `reader` when needed."""

    def read(self, reader: BinaryReader, bit_count: int, fmt: str = "B"):
        max_bit_count = 8 * struct.calcsize(fmt)
        if self._field == "" or fmt != self._fmt or self._offset + bit_count > max_bit_count:
            # Consume (and reverse) new bit field. Any previous bit field is discarded.
            integer = reader[fmt]
            self._field = format(integer, f"0{max_bit_count}b")[::-1]
            self._fmt = fmt
        binary_str = self._field[self._offset:self._offset + bit_count][::-1]
        self._offset += bit_count
        if self._offset % max_bit_count == 0:  # read new field next time
            self._field = ""
            self._offset = 0
        return int(binary_str, 2)

    def read_list_buffer(self, buffer: list[int], bit_count: int, fmt: str = "B"):
        """Same, but instead of using a reader, pop a new integer from end of `buffer` when needed.

        NOTE: Still uses `fmt`, but assumes that `buffer` was created appropriately with the same formats.
        """
        max_bit_count = 8 * struct.calcsize(fmt)
        if self._field == "" or fmt != self._fmt or self._offset + bit_count > max_bit_count:
            # Consume (and reverse) new bit field. Any previous bit field is discarded.
            integer = buffer.pop()
            max_size = PRIMITIVE_FMT_SIZE_MINMAX[fmt][1][1]
            if integer > max_size:
                raise ValueError(f"BitFieldReader popped value {integer} which is too large for fmt {fmt}.")
            self._field = format(integer, f"0{max_bit_count}b")[::-1]
            self._fmt = fmt
        binary_str = self._field[self._offset:self._offset + bit_count][::-1]
        self._offset += bit_count
        if self._offset % max_bit_count == 0:  # read new field next time
            self._field = ""
            self._offset = 0
        return int(binary_str, 2)


class BitFieldWriter(_BitFieldBase):
    """Manages partial writing of one or more bytes, by keeping incomplete bits from previous writes and flushing
    them when the full `fmt` is complete."""

    def write(self, writer: BinaryWriter, value: int, bit_count: int, fmt: str = "B"):
        """Appends `value` to bit field and returns packed data whenever a field is completed.

        `fmt` specifies the size of the field that bits are being written into (almost always `byte`). When the field is
        finished,

        Note that a field is completed if the given `fmt` is different to the type of the current bit field.
        """
        if value >= 2 ** bit_count:
            raise ValueError(
                f"Value {value} of new bit field value is too large for given bit count ({bit_count})."
            )

        new_fmt = False
        if fmt != self._fmt:
            if self._fmt:
                # Pad and append last bit field (of different type to new one) while starting new bit field.
                self.finish_field(writer)
            self._fmt = fmt
            new_fmt = True

        # Append value bits to partial field.
        self._field += format(value, f"0{bit_count}b")[::-1]

        max_bit_count = 8 * struct.calcsize(self._fmt)
        if len(self._field) >= max_bit_count:
            # Bits are ready to flush to `writer`.
            if new_fmt:
                # This shouldn't happen for new `fmt` because `bit_count < max_size`, but just in case.
                raise ValueError(f"New bit field was exceeded before previous bit field could be written.")

            # Complete and write finished field.
            completed_bit_field = self._field[:max_bit_count]
            integer = int(completed_bit_field[::-1], 2)  # reversed
            writer.pack(self._fmt, integer)

            # Leftover bits (if any) go into new field (though this should never happen in `Param`s due to pad fields).
            self._field = self._field[max_bit_count:]  # will be empty if `max_bit_count` exactly reached

    def write_to_buffer(self, buffer: list[int], value: int, bit_count: int, fmt: str = "B") -> str:
        """Appends `value` to bit field and appends packed data to `buffer` whenever a field is completed.

        `fmt` specifies the size of the field that bits are being written into (almost always `byte`).

        Note that a field is completed if the given `fmt` is different to the type of the current bit field.

        Returns `fmt` of appended values
        """
        if value >= 2 ** bit_count:
            raise ValueError(
                f"Value {value} of new bit field value is too large for given bit count ({bit_count})."
            )

        new_fmt = False
        added_fmt = ""
        if fmt != self._fmt:
            if self._fmt and not self.empty:
                # Pad and append last bit field (of different type to new one) while starting new bit field.
                self.finish_field_buffer(buffer)
                added_fmt += self._fmt
            self._fmt = fmt
            new_fmt = True

        # Append value bits to partial field.
        self._field += format(value, f"0{bit_count}b")[::-1]

        max_bit_count = 8 * struct.calcsize(self._fmt)
        if len(self._field) >= max_bit_count:
            # Bits are ready to flush to `writer`.
            if new_fmt:
                # This shouldn't happen for new `fmt` because `bit_count < max_size`, but just in case.
                raise ValueError(f"New bit field was exceeded before previous bit field could be written.")

            # Complete and write finished field.
            completed_bit_field = self._field[:max_bit_count]
            field_int = int(completed_bit_field[::-1], 2)  # reversed
            buffer.append(field_int)
            added_fmt += self._fmt

            # Leftover bits (if any) go into new field (though this should never happen in `Param`s due to pad fields).
            self._field = self._field[max_bit_count:]  # will be empty if `max_bit_count` exactly reached

        return added_fmt

    def finish_field(self, writer: BinaryWriter):
        """Pad existing bit field to its maximum size from `self._fmt`, clear it, and return packed data.

        Returns empty bytes if field is empty.
        """
        if not self._field:
            return
        size = struct.calcsize(self._fmt)
        padded_field = format(self._field, f"0<{size}")  # pad field out with zeroes
        writer.pack(self._fmt, int(padded_field[::-1], 2))  # note string reversal
        self.clear()

    def finish_field_buffer(self, buffer: list[int]) -> str:
        """Pad existing bit field to its maximum size from `self._fmt`, clear it, and return packed data.

        Returns added fmt.
        """
        size = struct.calcsize(self._fmt)
        padded_field = format(self._field, f"0<{size}")  # pad field out with zeroes
        buffer.append(int(padded_field[::-1], 2))  # note string reversal
        added_fmt = self._fmt
        self.clear()
        return added_fmt
