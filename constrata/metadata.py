from __future__ import annotations

__all__ = [
    "FIELD_T",
    "BinaryMetadata",
    "BinaryStringMetadata",
    "BinaryArrayMetadata",
]

import dataclasses
import typing as tp

from constrata.field_types.type_info import PRIMITIVE_FIELD_TYPING
from constrata.exceptions import BinaryFieldValueError

FIELD_T = tp.TypeVar("FIELD_T")  # can be any type thanks to `unpack_func` option


@dataclasses.dataclass(slots=True)
class BinaryMetadata(tp.Generic[FIELD_T]):
    """Base class for optional metadata for `BinaryStruct` dataclass fields."""

    fmt: str
    asserted: tuple[FIELD_T, ...] = dataclasses.field(default=())
    unpack_func: tp.Callable[[PRIMITIVE_FIELD_TYPING], FIELD_T] = None
    pack_func: tp.Callable[[FIELD_T], PRIMITIVE_FIELD_TYPING] = None
    bit_count: int = -1  # NOTE: Field is packed/unpacked manually if this is not -1.
    should_skip_func: tp.Callable[[bool, dict[str, tp.Any]], bool] = None

    # Constructed in `__post_init__` for efficiency.
    single_asserted: FIELD_T | None = dataclasses.field(init=False, default=None)

    # Assigned by `BinaryStruct` to allow better error logging below. (NOT used otherwise.)
    field_name: str = dataclasses.field(default=None, init=False)
    field_type: type[FIELD_T] = dataclasses.field(default=None, init=False)

    def __post_init__(self):
        if self.asserted and len(self.asserted) == 1:
            self.single_asserted = self.asserted[0]
        else:
            self.single_asserted = None

    def get_unpacker(self) -> tp.Callable[[list[tp.Any]], FIELD_T]:
        """Configures and returns a function that produces field values from `struct.unpack()` output.

        This way, the (unchanging) field metadata options only have to be checked ONCE, here at construction.

        NOTE: Yes, I'm using the dreaded `exec` to build this function dynamically, like a preprocessor macro.
        I can't find any better way to do this, and it is NOT unsafe as the `exec()` input is fully determined here.
        """
        func = "def unpack(struct_output: list[tp.Any], metadata=self) -> FIELD_T:\n"
        func += "    value = struct_output.pop()\n"
        if self.unpack_func:
            func += "    value = metadata.unpack_func(value)\n"
        if self.asserted:
            error_msg = f"Field '{self.field_name}' read value {{value}} is not an asserted value: {self.asserted}"
            func += "    if value not in metadata.asserted:\n"
            func += f"        raise BinaryFieldValueError(\"{error_msg}\".format(value=value))\n"
        func += "    return value"
        exec(func)
        return locals()["unpack"]

    def get_packer(self) -> tp.Callable[[list[tp.Any], FIELD_T], None]:
        """Pack a single value into input for `struct.pack(full_fmt)`."""

        func = "def pack(struct_input: list[tp.Any], value: FIELD_T, metadata=self):\n"
        if self.asserted:
            error_msg = f"Field '{self.field_name}' value {{value}} is not an asserted value: {self.asserted}"
            func += "    if value not in metadata.asserted:\n"
            func += f"        raise BinaryFieldValueError(\"{error_msg}\".format(value=value))\n"
        if self.pack_func:
            func += "    value = metadata.pack_func(value)\n"
        func += "    struct_input.append(value)"
        exec(func)
        return locals()["pack"]


@dataclasses.dataclass(slots=True)
class BinaryStringMetadata(BinaryMetadata):
    """Dataclass field metadata for a fixed-length encoded `bytes` (if `encoding is None`) or decoded `str` value."""

    encoding: str | None = None
    rstrip_null: bool = True

    def get_unpacker(self) -> tp.Callable[[list[tp.Any]], FIELD_T]:
        func = "def unpack(struct_output: list[tp.Any], metadata=self) -> FIELD_T:\n"
        func += "    value = struct_output.pop()\n"
        if self.encoding:
            if self.encoding == "utf16":
                # `byte_order` local will be defined when unpacker is called.
                func += "    value = value.decode(byte_order.get_utf_16_encoding())\n"
            else:
                func += "    value = value.decode(metadata.encoding)\n"
            if self.rstrip_null:
                func += "    value = value.rstrip(\"\\0\")\n"
        else:
            # Presumably safe to rstrip (no UTF-16 bytes to damage).
            if self.rstrip_null:
                func += "    value = value.rstrip(b\"\\0\")\n"
        if self.unpack_func:  # called on decoded `str` if applicable
            func += "    value = metadata.unpack_func(value)\n"
        if self.asserted:
            error_msg = f"Field '{self.field_name}' read value {{value}} is not an asserted value: {self.asserted}"
            func += "    if value not in metadata.asserted:\n"
            func += f"        raise BinaryFieldValueError(\"{error_msg}\".format(value=value))\n"
        func += "    return value\n"
        exec(func)
        return locals()["unpack"]

    def get_packer(self) -> tp.Callable[[list[tp.Any], FIELD_T], None]:
        """Pack a single value into input for `struct.pack(full_fmt)`."""
        func = "def pack(struct_input: list[tp.Any], value: FIELD_T, metadata=self):\n"
        if self.rstrip_null:  # asserted values are stripped, so value should be too
            if self.encoding is None:  # bytes
                func += "    value = value.rstrip(b\"\\0\")\n"
            else:  # str
                func += "    value = value.rstrip(\"\\0\")\n"
        if self.asserted:
            error_msg = f"Field '{self.field_name}' value {{value}} is not an asserted value: {self.asserted}"
            func += "    if value not in metadata.asserted:\n"
            func += f"        raise BinaryFieldValueError(\"{error_msg}\".format(value=value))\n"
        if self.pack_func:
            func += "    value = metadata.pack_func(value)\n"
        if self.encoding:
            if self.encoding == "utf16":
                # `byte_order` local will be defined when unpacker is called.
                func += "    value = value.encode(byte_order.get_utf_16_encoding())\n"
            else:
                func += "    value = value.encode(metadata.encoding)\n"
        # NOTE: `writer.pack()` call will automatically pad these `bytes` using `metadata.fmt`.
        func += "    struct_input.append(value)\n"
        exec(func)
        return locals()["pack"]


@dataclasses.dataclass(slots=True, init=False)
class BinaryArrayMetadata(BinaryMetadata):
    """Dataclass field metadata for a fixed-length array of values."""

    length: int = 1

    def __init__(
        self,
        length: int,
        fmt: str | None = None,
        asserted: tuple[FIELD_T, ...] = (),
        unpack_func=None,
        pack_func=None,
        should_skip_func=None,
    ):
        """Custom argument order to make `length` required."""
        self.length = length
        self.fmt = fmt
        self.asserted = asserted
        self.unpack_func = unpack_func
        self.pack_func = pack_func
        self.should_skip_func = should_skip_func
        self.bit_count = -1
        super(BinaryArrayMetadata, self).__post_init__()

    def get_unpacker(self) -> tp.Callable[[list[tp.Any]], FIELD_T]:
        func = "def unpack(struct_output: list[tp.Any], metadata=self) -> FIELD_T:\n"
        func += f"    value = [struct_output.pop() for _ in range({self.length})]\n"  # pops in suitable reverse order
        if self.unpack_func:
            func += "    value = metadata.unpack_func(value)\n"
        if self.asserted:
            error_msg = f"Field '{self.field_name}' read value {{value}} is not an asserted value: {self.asserted}"
            func += f"    if value not in metadata.asserted:\n"
            func += f"        raise BinaryFieldValueError(\"{error_msg}\".format(value=value))\n"
        func += "    return value\n"
        exec(func)
        return locals()["unpack"]

    def get_packer(self) -> tp.Callable[[list[tp.Any], FIELD_T], None]:
        """Pack multiple values into input for `struct.pack(full_fmt)`."""
        func = "def pack(struct_input: list[tp.Any], value: FIELD_T, metadata=self):\n"
        if self.asserted:
            error_msg = f"Field '{self.field_name}' value {{value}} is not an asserted value: {self.asserted}"
            func += f"    if value not in metadata.asserted:\n"
            func += f"        raise BinaryFieldValueError(\"{error_msg}\".format(value=value))\n"
        if self.pack_func:
            func += "    value = metadata.pack_func(value)\n"
        func += "    struct_input.extend(value)\n"
        exec(func)
        return locals()["pack"]
