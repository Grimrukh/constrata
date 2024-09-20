from __future__ import annotations

__all__ = [
    "BinaryReader",
]

import io
import struct
import typing as tp
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from constrata.utilities import read_chars_from_buffer
from constrata.byte_order import ByteOrder
from .base import BinaryBase


class BinaryReader(BinaryBase):
    """Manages a buffered binary IO stream, with methods for unpacking data and moving to temporary offsets."""

    class ReaderError(Exception):
        """Exception raised when trying to unpack data."""
        pass

    buffer: tp.BinaryIO | io.BufferedIOBase | None
    path: Path | None  # optional path to file source

    def __init__(
        self,
        buffer: str | Path | bytes | bytearray | io.BufferedIOBase | BinaryReader,
        default_byte_order=ByteOrder.LittleEndian,
        long_varints=True,
    ):
        super().__init__(default_byte_order, long_varints)

        self.buffer = None
        self.path = None

        if isinstance(buffer, str):
            buffer = Path(buffer)
        if isinstance(buffer, Path):
            self.buffer = buffer.open("rb")
            self.path = buffer
        elif isinstance(buffer, (bytes, bytearray)):
            self.buffer = io.BytesIO(buffer)
        elif isinstance(buffer, io.BufferedIOBase):
            self.buffer = buffer
        elif isinstance(buffer, BinaryReader):
            self.buffer = buffer.buffer
        else:
            try:
                data = bytes(buffer)
            except TypeError:
                raise TypeError(
                    f"Invalid `buffer`: {buffer}. Should be a binary IO stream, `bytes`, file `Path | str`, or an "
                    f"object that defines `__bytes__`."
                )
            self.buffer = io.BytesIO(data)

    def unpack(self, fmt, offset=None, relative_offset=False, asserted=None) -> tuple:
        """Unpack appropriate number of bytes from `buffer` using `fmt` string from the given (or current) `offset`.

        Args:
            fmt (str): format string for `struct.unpack()`.
            offset (int): optional offset to seek to before reading. Old offset will be restored afterward.
            relative_offset (bool): indicates that `offset` is relative to current position.
            asserted: assert that the unpacked data is equal to this, if given..

        Returns:
            (tuple) Output of `struct.unpack()`.
        """
        fmt = self.parse_fmt(fmt)
        fmt_size = self.calcsize(fmt)

        initial_offset = self.buffer.tell() if offset is not None else None
        if offset is not None:
            self.buffer.seek(initial_offset + offset if relative_offset else offset)
        raw_data = self.buffer.read(fmt_size)
        if not raw_data and fmt_size > 0:
            raise ValueError(f"Could not unpack {fmt_size} bytes from reader for format '{fmt}'.")
        data = struct.unpack(fmt, raw_data)
        if asserted is not None and data != asserted:
            raise AssertionError(f"Unpacked data {repr(data)} does not equal asserted data {repr(asserted)}.")
        if initial_offset is not None:
            self.buffer.seek(initial_offset)
        return data

    def unpack_value(self, fmt, offset=None, relative_offset=False, asserted=None) -> bool | int | float | bytes:
        """Call `unpack()` and return the single value returned.

        If `asserted` is given, an `AssertionError` will be raised if the unpacked value is not equal to `asserted`.

        Also raises a `ValueError` if more than one value is unpacked.
        """
        data = self.unpack(fmt, offset, relative_offset)
        if len(data) > 1:
            raise ValueError(f"More than one value unpacked with `unpack_value()`: {data}")
        value = data[0]
        if asserted is not None and value != asserted:
            raise AssertionError(f"Unpacked value {repr(value)} does not equal asserted value {repr(asserted)}.")
        return data[0]

    def __getitem__(self, fmt: str | tuple[str, int]) -> bool | int | float | bytes:
        """Shortcut for `unpack_value(fmt)` at current offset or `(fmt, offset)` tuple."""
        if isinstance(fmt, str):
            return self.unpack_value(fmt)
        return self.unpack_value(fmt[0], offset=fmt[1])

    def peek(self, fmt_or_size: str | int, bytes_ahead: int = 0) -> bool | int | float | bytes | tuple:
        """Unpack `fmt_or_size` (or just read bytes) and return the unpacked values without changing the offset."""
        if isinstance(fmt_or_size, int):
            return self.read(fmt_or_size, offset=self.position + bytes_ahead)
        return self.unpack(fmt_or_size, offset=self.position + bytes_ahead)

    def peek_value(self, fmt, bytes_ahead: int = 0) -> bool | int | float:
        """Unpack `fmt` and return the unpacked value without changing the offset."""
        return self.unpack_value(fmt, offset=self.position + bytes_ahead)

    def unpack_bytes(
        self,
        length: int = None,
        offset: int = None,
        reset_old_offset=True,
        strip=True,
        asserted: bytes | None = None,
    ) -> bytes:
        """Read bytes (null-terminated if `length` is not given) from given `offset` (defaults to current).

        See `read_chars_from_buffer()` for more.
        """
        try:
            s = read_chars_from_buffer(self.buffer, length, offset, reset_old_offset, encoding=None, strip=strip)
        except struct.error as ex:
            raise self.ReaderError(f"Could not unpack bytes. Error: {ex}")
        if asserted is not None and s != asserted:
            raise AssertionError(f"Unpacked bytes {s} do not equal asserted bytes {asserted}.")
        return s

    def unpack_string(
        self,
        length: int = None,
        offset: int = None,
        reset_old_offset=True,
        encoding="utf-8",
        strip=True,
        asserted: bytes | str | None = None,
    ) -> str:
        """Read string (null-terminated if `length` is not given) from given `offset` (defaults to current).

        Encoding defaults to "utf-8". If a "utf-16" encoding is given, two bytes will be read at a time, and a double
        null terminator is required. See `read_chars_from_buffer()` for more.
        """
        try:
            s = read_chars_from_buffer(self.buffer, length, offset, reset_old_offset, encoding=encoding, strip=strip)
        except struct.error as ex:
            raise self.ReaderError(f"Could not unpack string. Error: {ex}")
        if asserted is not None and s != asserted:
            if encoding is not None:
                raise AssertionError(f"Unpacked string {repr(s)} does not equal asserted string {repr(asserted)}.")
            raise AssertionError(f"Unpacked bytes {s} do not equal asserted bytes {asserted}.")
        return s

    def read(self, size: int = None, offset: int = None) -> bytes:
        if offset is not None:
            with self.temp_offset(offset):
                return self.buffer.read(size)
        return self.buffer.read(size)

    def seek(self, offset: int, whence=None) -> int:
        """Returns final position.

        Reminder: `whence` is zero (or None) for absolute offset, 1 for relative to current, and 2 for relative to end.
        """
        if whence is not None:
            self.buffer.seek(offset, whence)
        else:
            self.buffer.seek(offset)
        return self.buffer.tell()

    def tell(self):
        """Also has alias property `position` for this."""
        return self.buffer.tell()

    def assert_pad(self, size: int, char=b"\0"):
        """Read and assert `size` instances of `char` (defaults to null/zero byte)."""
        padding = self.buffer.read(size)
        if padding.strip(char):
            raise ValueError(f"Reader `assert_pad({size})` found bytes other than {char}: {padding}")

    def align(self, alignment: int):
        """Align reader position to next multiple of `alignment`."""
        while self.buffer.tell() % alignment:
            self.buffer.read(1)

    def close(self):
        self.buffer.close()

    @contextmanager
    def temp_offset(self, offset: int = None):
        """Seek `buffer` to `offset` temporarily, then reset to original offset when done.

        If `offset=None` (default), resets to current offset.
        """
        initial_offset = self.buffer.tell()
        if offset is not None:
            self.buffer.seek(offset)
        yield
        self.buffer.seek(initial_offset)

    def __del__(self):
        if self.buffer:
            self.buffer.close()

    @property
    def position(self) -> int:
        return self.buffer.tell()

    @property
    def position_hex(self) -> str:
        return hex(self.buffer.tell())

    @staticmethod
    @lru_cache(maxsize=256)
    def calcsize(parsed_fmt: str):
        """LRU-decorated method for calculating struct size after parsing it."""
        return struct.calcsize(parsed_fmt)

    def print_labeled_position(self, label: str, as_hex=False):
        print(f"{label} position: {self.position_hex if as_hex else self.position}")
