from __future__ import annotations

import io
import typing as tp

if tp.TYPE_CHECKING:
    from constrata.streams.reader import BinaryReader


def long_varints_from_reader_peek(
    reader: BinaryReader, size: int, bytes_ahead: int, true_read: bytes, false_read: bytes
) -> bool:
    peeked = reader.peek(size, bytes_ahead)
    if peeked == true_read:
        return True
    elif peeked == false_read:
        return False
    else:
        raise ValueError(f"Could not determine `long_varints` from bytes: {peeked}")


def read_chars_from_bytes(data, offset=0, length=None, encoding=None) -> bytes | str:
    """Read characters from a bytes object (an encoded string). Use 'read_chars_from_buffer' if you are using a buffer.

    If 'length=None' (default), characters will be read until null termination from the given offset. Otherwise,
    'length' bytes will be read and all null padding bytes will be stripped from the right side.

    Use 'encoding' to automatically decode the bytes into a string before returning (e.g. 'shift_jis_2004'). Note that
    if 'utf-16-*' is specified as the encoding with no length, a double-null termination of b'\0\0' is required to
    terminate the string (as single nulls can appear in the two-byte characters).
    """
    bytes_per_char = 2 if encoding is not None and encoding.replace("-", "").startswith("utf16") else 1
    if length is not None:
        stripped_array = data[offset:offset + length].rstrip()  # remove trailing spaces
        while stripped_array.endswith(b"\0\0" if bytes_per_char == 2 else b"\0"):
            stripped_array = stripped_array[:-bytes_per_char]  # remove (pairs of) nulls
        if encoding is not None:
            return stripped_array.decode(encoding)
        return stripped_array
    else:
        # Find null termination.
        null_termination = data[offset:].find(b"\0" * bytes_per_char)
        if null_termination == -1:
            raise ValueError("No null termination found for characters.")
        array = data[offset:offset + null_termination]
        if encoding is not None:
            return array.decode(encoding)
        return array


def read_chars_from_buffer(
    buffer: io.BufferedIOBase,
    offset: int = None,
    length: int = None,
    reset_old_offset=True,
    encoding: str = None,
    strip=True,
) -> str | bytes:
    """Read characters from a buffer (type IOBase). Use 'read_chars_from_bytes' if your data is already in bytes format.

    Args:
        buffer (io.BufferedIOBase): byte-format data stream to read from.
        offset: offset to `seek()` in buffer before starting to read characters. Defaults to current offset (None).
        reset_old_offset: if True, and 'offset' is not None, the buffer offset will be restored to its original position
            (at function call time) before returning. (Default: True)
        length: number of characters to read (i.e. the length of the returned string). If None (default), characters
            will be read until a null termination is encountered. Otherwise, if a length is specified, any spaces at
            the end of the string will be stripped, then any nulls at the end will be stripped.
        encoding: attempt to decode characters in this encoding before returning. If 'utf-16-*' is specified, this
            function will infer that characters are two bytes long (and null terminations will be two bytes). Otherwise,
            it assumes they are one byte long. You can decode the characters yourself if you want to use another
            multiple-bytes-per-character encoding.
        strip: remove trailing spaces and nulls (default: True).
    """
    if length == 0:
        if not reset_old_offset and not isinstance(buffer, bytes):
            buffer.seek(offset)
        return "" if encoding is not None else b""

    if isinstance(buffer, bytes):
        buffer = io.BytesIO(buffer)
    chars = []
    old_offset = None
    bytes_per_char = 2 if encoding is not None and encoding.replace("-", "").startswith("utf16le") else 1
    terminator = b"\0" * bytes_per_char

    if offset is not None:
        old_offset = buffer.tell()
        buffer.seek(offset)

    while 1:
        c = buffer.read(bytes_per_char)
        if not c and length is None:
            raise ValueError("Ran out of bytes to read before null termination was found.")
        if length is None and c == terminator:
            # Null termination.
            array = b"".join(chars)
            if reset_old_offset and old_offset is not None:
                buffer.seek(old_offset)
            if encoding is not None:
                return array.decode(encoding)
            return array
        else:
            chars.append(c)
            if len(chars) == length:
                if reset_old_offset and old_offset is not None:
                    buffer.seek(old_offset)
                stripped_array = b"".join(chars)  # used to strip spaces as well, but not anymore
                if strip:
                    stripped_array = stripped_array.rstrip()  # strip spaces
                    while stripped_array.endswith(terminator):
                        stripped_array = stripped_array[:-bytes_per_char]  # remove terminators
                if encoding is not None:
                    return stripped_array.decode(encoding)
                return stripped_array


def read_null_terminated_bytes(reader: BinaryReader, encoding: str = None) -> bytes | str:

    # TODO: Currently just checking for UTF-16 encodings to enforce double null characters.
    bytes_per_char = 2 if encoding is not None and encoding.replace("-", "").startswith("utf16") else 1
    terminator = b"\0" * bytes_per_char

    chars = []
    while 1:
        c = reader.read(bytes_per_char)
        if not c:
            raise ValueError("Ran out of bytes to read before null termination was found.")
        if c == terminator:
            # Null termination.
            array = b"".join(chars)
            if encoding is not None:
                return array.decode(encoding)
            return array
        else:
            chars.append(c)
