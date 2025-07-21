from __future__ import annotations

__all__ = [
    "FIELD_T",
    "BinaryMetadata",
    "BinaryStringMetadata",
    "BinaryArrayMetadata",
]

import dataclasses
import typing as tp
from types import GenericAlias

from constrata.exceptions import BinaryFieldValueError, BinaryFieldTypeError
from constrata.field_types.type_info import *
from constrata.byte_order import ByteOrder

FIELD_T = tp.TypeVar("FIELD_T")  # can be any type thanks to `unpack_func` option
BYTE_ORDER_CHARS = tuple("@=<>!")


@dataclasses.dataclass(slots=True, frozen=True)
class BinaryMetadata(tp.Generic[FIELD_T]):
    """Base class for optional metadata for `BinaryStruct` dataclass fields.

    Dataclass is frozen to ensure cached packers/unpackers remain valid. Modifications to metadata are done only in
    `BinaryStruct._initialize_binary_metadata()`.

    NOTE: Fields do not store an `offset`, as this will depend on `byte_order` and `long_varints`.
    """

    fmt: str
    asserted: tuple[FIELD_T, ...] = dataclasses.field(default=())
    unpack_func: tp.Callable[[PRIMITIVE_FIELD_TYPING], FIELD_T] = None
    pack_func: tp.Callable[[FIELD_T], PRIMITIVE_FIELD_TYPING] = None
    bit_count: int = -1  # NOTE: field is packed/unpacked manually if this is not -1

    # Detected and assigned manually.
    struct_index: int = dataclasses.field(default=-1, init=False)  # index in `struct.unpack()` output
    field_name: str = dataclasses.field(default="", init=False)
    field_type: type[FIELD_T] = dataclasses.field(default=None, init=False)
    single_asserted: FIELD_T | None = dataclasses.field(default=None, init=False)

    # Not used by this non-array base class. (Faster than `isinstance(self, BinaryArrayMetadata)`.)
    length: int = dataclasses.field(default=0, init=False)  # only used by `BinaryArrayMetadata`

    def __post_init__(self):
        if self.fmt is not None and self.fmt.startswith(BYTE_ORDER_CHARS):
            raise ValueError(
                f"Individual binary field format string cannot start with byte order character: '{self.fmt}'. "
                f"Byte order is set when packing/unpacking the entire struct."
            )

        single_asserted = self.asserted[0] if self.asserted and len(self.asserted) == 1 else None
        object.__setattr__(self, "single_asserted", single_asserted)

    def finish_metadata(
        self,
        binary_field: dataclasses.Field,
        field_type: tp.Any,
        struct_cls_name: str,
    ):
        """Use binary field, its type, offset, and the struct class name (for exceptions) to fill out metadata."""
        object.__setattr__(self, "field_name", binary_field.name)
        object.__setattr__(self, "field_type", field_type)

        if self.fmt is None:
            self.set_default_fmt(binary_field, field_type, struct_cls_name)

        if field_type not in PRIMITIVE_FIELD_FMTS:
            # Use custom non-primitive type to unpack and pack (iter).
            if self.unpack_func is None:
                object.__setattr__(self, "unpack_func", field_type)  # use type constructor
            if self.pack_func is None:
                # Just validate `__iter__` presence.
                if not hasattr(field_type, "__iter__"):
                    raise BinaryFieldTypeError(
                        binary_field,
                        struct_cls_name,
                        f"Non-primitive field type `{field_type.__name__}` must have `unpack_func` metadata or "
                        f"implement `__iter__` to enable default handling."
                    )

    def set_struct_index(self, struct_index: int):
        """Set the index of this field in the struct's unpacked output."""
        if getattr(self, "struct_index", -1) != -1:
            raise ValueError(
                f"Cannot set `struct_index` for field '{self.field_name}' in struct '{self.field_type.__name__}' "
                f"because it has already been set to {self.struct_index}."
            )
        object.__setattr__(self, "struct_index", struct_index)
    
    def set_default_fmt(self, binary_field, field_type: tp.Any, struct_cls_name: str):

        if field_type in PRIMITIVE_FIELD_FMTS:
            object.__setattr__(self, "fmt", PRIMITIVE_FIELD_FMTS[field_type])
        else:
            raise BinaryFieldTypeError(
                binary_field,
                struct_cls_name,
                f"Field with non-primitive, non-BinaryStruct type `{field_type.__name__}` must have `fmt` "
                f"metadata.",
            )

    def validate(self, value: FIELD_T):
        if self.asserted and value not in self.asserted:
            raise BinaryFieldValueError(
                f"Field '{self.field_name}' unpacked value {value} is not an asserted value: {self.asserted}"
            )

    def process_from_unpack(self, unpacked_value: tp.Any, byte_order: ByteOrder) -> FIELD_T:
        return self.unpack_func(unpacked_value) if self.unpack_func else unpacked_value

    def process_to_pack(self, value: FIELD_T, byte_order: ByteOrder) -> int | float | bool | bytes:
        if self.pack_func:
            value = self.pack_func(value)
        return value

    def get_null(self, size: int) -> FIELD_T:
        """Get 'null' value for this field type, used for reserving."""
        if self.fmt == "?":
            return False
        elif self.fmt.endswith("s"):
            return b"\0" * size
        return 0


@dataclasses.dataclass(slots=True, frozen=True)
class BinaryStringMetadata(BinaryMetadata):
    """Dataclass field metadata for a fixed-length encoded `bytes` (if `encoding is None`) or decoded `str` value.

    If encoding is 'utf16', the actual encoding for pack/unpack will be detected from added `byte_order` argument.
    """

    encoding: str | None = None
    rstrip_null: bool = True

    def process_from_unpack(self, unpacked_value: bytes, byte_order: ByteOrder) -> FIELD_T:
        """Automatically decode and/or rstrip nulls from packed string/bytes as appropriate."""
        value = unpacked_value
        if self.encoding:
            if self.encoding == "utf16":
                value = value.decode(byte_order.get_utf_16_encoding())
            else:
                value = value.decode(self.encoding)
            if self.rstrip_null:
                value = value.rstrip("\0")
        else:
            # Presumably safe to rstrip (no UTF-16 bytes to damage).
            if self.rstrip_null:
                value = value.rstrip(b"\0")
        if self.unpack_func:  # called on decoded `str` if applicable
            value = self.unpack_func(value)
        return value

    def process_to_pack(self, value: FIELD_T, byte_order: ByteOrder) -> int | float | bool | bytes:
        if self.rstrip_null:  # asserted values are stripped, so value should be too
            if self.encoding is None:  # bytes
                value = value.rstrip(b"\0")
            else:  # str
                value = value.rstrip("\0")
        if self.pack_func:
            value = self.pack_func(value)
        if self.encoding:
            if self.encoding == "utf16":
                value = value.encode(byte_order.get_utf_16_encoding())
            else:
                value = value.encode(self.encoding)
        # NOTE: `writer.pack()` call will automatically pad these `bytes` using `metadata.fmt`.
        return value


@dataclasses.dataclass(slots=True, frozen=True, init=False)
class BinaryArrayMetadata(BinaryMetadata):
    """Dataclass field metadata for a fixed-length array of values."""

    def __init__(
        self,
        length: int,
        fmt: str | None = None,
        asserted: tuple[FIELD_T, ...] = (),
        unpack_func=None,
        pack_func=None,
    ):
        """Custom argument order to make `length` required."""
        object.__setattr__(self, "length", length)
        object.__setattr__(self, "fmt", fmt)
        object.__setattr__(self, "asserted", asserted)
        object.__setattr__(self, "unpack_func", unpack_func)
        object.__setattr__(self, "pack_func", pack_func)
        object.__setattr__(self, "bit_count", -1)  # not compatible
        super(BinaryArrayMetadata, self).__post_init__()
        
    def set_default_fmt(self, binary_field, field_type: tp.Any, struct_cls_name: str):
        if (
            not isinstance(field_type, GenericAlias)
            or field_type.__origin__ != list
            or len(field_type.__args__) != 1
        ):
            raise BinaryFieldTypeError(
                binary_field, struct_cls_name, "Type hint for binary array field must be `list[type]`."
            )
        element_type = field_type.__args__[0]
        if element_type in PRIMITIVE_FIELD_FMTS:
            object.__setattr__(
                self, "fmt", f"{self.length}{PRIMITIVE_FIELD_FMTS[element_type]}"
            )
        else:
            raise BinaryFieldTypeError(
                binary_field,
                struct_cls_name,
                f"Array field with non-primitive element type `{element_type.__name__}` "
                f"must have `fmt` metadata.",
            )

    # Same processing methods are used. They just implicitly expect a list of values (to pass to `unpack_func`,
    # `pack_func`, and check against `asserted`) instead of a single value.
