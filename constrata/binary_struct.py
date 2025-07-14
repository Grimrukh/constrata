from __future__ import annotations

__all__ = [
    "BinaryStruct",
]

import copy
import dataclasses
import io
import logging
import struct
import typing as tp
from types import GenericAlias

from constrata.byte_order import ByteOrder
from constrata.field_types.type_info import *
from constrata.exceptions import BinaryFieldTypeError, BinaryFieldValueError
from constrata.metadata import BinaryMetadata, BinaryStringMetadata, BinaryArrayMetadata
from constrata.metacls import BinaryStructMeta
from constrata.streams import BinaryReader, BinaryWriter, BitFieldReader, BitFieldWriter

_LOGGER = logging.getLogger("constrata")

OBJ_T = tp.TypeVar("OBJ_T")


class BinaryStruct(metaclass=BinaryStructMeta):
    """Dataclass that supports automatic reading/writing from packed binary data."""

    # Caches for class binary information, each constructed on first use and immutable thereafter.
    __STRUCT_INITIALIZED: tp.ClassVar[bool] = False
    _FIELDS: tp.ClassVar[tuple[dataclasses.Field, ...]]  # from `dataclass`; cannot be `None`
    _BINARY_FIELDS: tp.ClassVar[tuple[dataclasses.Field, ...] | None] = None  # filtered from `_FIELDS`
    _BFIELD_TYPES: tp.ClassVar[tuple[type, ...] | None] = None  # all types supported via custom packers/unpackers
    _BFIELD_METADATA: tp.ClassVar[tuple[BinaryMetadata, ...] | None] = None
    _BFIELD_INIT: tp.ClassVar[tuple[bool, ...] | None] = None
    _STRUCTS: tp.ClassVar[dict[tuple[ByteOrder, bool | None], struct.Struct]] = {}  # (byte_order, long_varints)

    _HAS_VARINTS: tp.ClassVar[bool] = False
    _HAS_DYNAMIC_FIELDS: tp.ClassVar[bool] = False
    _HAS_BIT_FIELDS: tp.ClassVar[bool] = False
    _HAS_ENCODING: tp.ClassVar[bool] = False
    _HAS_ARRAY: tp.ClassVar[bool] = False
    _HAS_CUSTOM_UNPACK: tp.ClassVar[bool] = False
    _HAS_CUSTOM_PACK: tp.ClassVar[bool] = False
    _USE_FAST_UNPACK: tp.ClassVar[bool] = False
    _USE_FAST_PACK: tp.ClassVar[bool] = False

    # Optional dictionary for subclass use that maps field type names to default metadata factories.
    # Example:
    #   `{'Vector3': lambda: BinaryArrayMetadata(3, '3f', unpack_func=Vector3)}`
    # This allows pure annotated fields like `position: Vector3` to be used without needing to specify field metadata.
    # Note that metadata `pack_func` may not be required if the custom type defines an `__iter__` method that converts
    # it to a list of primitive values supported by `struct.pack()` (e.g. such that
    # `pack(*v3) == pack(v3.x, v3.y, v3.z)`).
    METADATA_FACTORIES: tp.ClassVar[dict[str, tp.Callable[[], BinaryMetadata]]] = {}

    # Subclasses can set their own default byte order, which defaults to LittleEndian here.
    DEFAULT_BYTE_ORDER: tp.ClassVar[ByteOrder] = ByteOrder.LittleEndian

    # There is no class default for `long_varints`. Any structs that uses these must specify it explicitly with an
    # argument or via a passed-in `BinaryWriter`.

    # No instance fields in this base class.

    def __post_init__(self) -> None:
        if not self.__STRUCT_INITIALIZED:
            self._initialize_binary_metadata()

        # Set single-asserted fields to their default values, regardless of `init` setting.
        for field, field_metadata in zip(self._BINARY_FIELDS, self._BFIELD_METADATA, strict=True):
            if field_metadata.single_asserted is not None:
                setattr(self, field.name, field_metadata.single_asserted)

    @property
    def cls_name(self) -> str:
        """Convenience instance property that returns the class name."""
        return self.__class__.__name__

    @classmethod
    def _initialize_binary_metadata(cls: type[BinaryStruct]) -> None:
        """One-off class call that scans all fields and constructs their binary metadata."""
        if not hasattr(cls, "__dataclass_fields__"):
            raise TypeError(
                f"BinaryStruct subclass `{cls.__name__}` has not been processed as a dataclass. Was its metaclass "
                f"replaced?"
            )

        cls_name = cls.__name__
        binary_fields = cls.get_binary_fields()
        if not binary_fields:
            raise TypeError(f"`BinaryStruct` subclass `{cls_name}` has no binary fields.")

        all_metadata = []

        for binary_field, field_type in zip(binary_fields, cls.get_binary_field_types()):

            # Resolve field type name and validate `list` as the only generic alias.
            if isinstance(field_type, GenericAlias):
                if field_type.__origin__ is not list:
                    raise BinaryFieldTypeError(
                        binary_field, cls_name, "Binary fields types cannot be `tuple`. Use `list[type]`."
                    )
                field_type_name = "list"
            else:
                field_type_name = field_type.__name__

            metadata = binary_field.metadata.get("binary", None)  # type: BinaryMetadata | None

            if metadata is None:
                # NOTE: We can't add a new 'binary' key to `field.metadata` now. We store it in `_BFIELD_METADATA`.

                if field_type_name in cls.METADATA_FACTORIES:
                    try:
                        metadata = cls.METADATA_FACTORIES[field_type_name]()
                    except Exception as ex:
                        raise BinaryFieldTypeError(
                            binary_field,
                            cls_name,
                            f"Failed to construct default metadata for field type `{field_type_name}`: {ex}",
                        )
                elif issubclass(field_type, BinaryStruct):
                    # Sub-struct.
                    metadata = BinaryMetadata(
                        fmt=f"{field_type.get_size()}s",
                        unpack_func=field_type.from_bytes,
                        pack_func=lambda struct_value: struct_value.to_bytes(),
                    )
                else:
                    # Must be a primitive field type.
                    try:
                        fmt = PRIMITIVE_FIELD_FMTS[field_type]
                    except KeyError:
                        raise BinaryFieldTypeError(
                            binary_field,
                            cls_name,
                            f"Field with non-primitive type `{field_type.__name__}` must have `fmt` metadata.",
                        )

                    metadata = BinaryMetadata(fmt)

            metadata.finish_metadata(binary_field, field_type, cls_name)
            all_metadata.append(metadata)

        cls._BFIELD_METADATA = tuple(all_metadata)
        cls._BFIELD_INIT = tuple(field.init for field in cls._BINARY_FIELDS)

        # Construct full/native alignment 32-bit and 64-bit default structs.
        # One of these will be used unless any fields have `should_skip_func` set.
        v_fmt = cls._get_full_fmt()
        short_fmt = v_fmt.replace("v", "i").replace("V", "I")
        long_fmt = v_fmt.replace("v", "q").replace("V", "Q")

        # Don't just use parent class's dictionary!
        cls._STRUCTS = {}

        for byte_order in ByteOrder:
            if "v" in v_fmt or "V" in v_fmt:
                cls._HAS_VARINTS = True
                cls._STRUCTS[byte_order, False] = struct.Struct(byte_order.value + short_fmt)
                cls._STRUCTS[byte_order, True] = struct.Struct(byte_order.value + long_fmt)
            else:
                cls._STRUCTS[byte_order, None] = struct.Struct(byte_order.value + v_fmt)

        cls._HAS_DYNAMIC_FIELDS = any(metadata.should_skip_func is not None for metadata in cls._BFIELD_METADATA)
        cls._HAS_BIT_FIELDS = any(metadata.bit_count != -1 for metadata in cls._BFIELD_METADATA)
        cls._HAS_ENCODING = any(
            isinstance(metadata, BinaryStringMetadata) and metadata.encoding for metadata in cls._BFIELD_METADATA
        )
        cls._HAS_ARRAY = any(isinstance(metadata, BinaryArrayMetadata) for metadata in cls._BFIELD_METADATA)
        cls._HAS_CUSTOM_UNPACK = any(metadata.unpack_func for metadata in cls._BFIELD_METADATA)
        cls._HAS_CUSTOM_PACK = any(metadata.pack_func for metadata in cls._BFIELD_METADATA)
        is_simple = not (
            cls._HAS_DYNAMIC_FIELDS
            or cls._HAS_BIT_FIELDS
            or cls._HAS_ENCODING
            or cls._HAS_ARRAY
        )
        cls._USE_FAST_UNPACK = is_simple and not cls._HAS_CUSTOM_UNPACK
        cls._USE_FAST_PACK = is_simple and not cls._HAS_CUSTOM_PACK

        cls.__STRUCT_INITIALIZED = True  # enabled now to prevent recursive calls in `get_full_fmt()` below

    @classmethod
    def from_bytes(
        cls,
        data: bytes | bytearray | BinaryReader | tp.BinaryIO,
        byte_order: ByteOrder | str = None,
        long_varints: bool = None,
    ) -> tp.Self:
        """Create an instance of this class from binary `data`, by parsing its fields.

        Note that field defaults do not matter here, as ALL fields must be unpacked.
        """
        if not cls.__STRUCT_INITIALIZED:
            cls._initialize_binary_metadata()

        if byte_order is None:
            if isinstance(data, BinaryReader):
                byte_order = data.byte_order
            else:
                byte_order = cls.DEFAULT_BYTE_ORDER
        elif isinstance(byte_order, str):
            byte_order = ByteOrder(byte_order)
        elif not isinstance(byte_order, ByteOrder):
            raise ValueError(
                f"Invalid `byte_order`: {byte_order}. Must be a `ByteOrder`, value of such (e.g. '<'), or `None` "
                f"to use the class default."
            )

        if long_varints is None:
            if isinstance(data, BinaryReader):
                long_varints = data.long_varints
            # Otherwise, leave as `None` and allow errors to occur if varint fields are found.

        old_byte_order = None
        if isinstance(data, (bytes, bytearray, io.BufferedIOBase)):
            # Transient reader; we can set the byte order directly.
            reader = BinaryReader(data, byte_order=byte_order, long_varints=long_varints)
        elif isinstance(data, BinaryReader):
            # Save old byte order if it is different.
            if byte_order != data.byte_order:
                old_byte_order = data.byte_order
            reader = data  # assumes it is at the correct offset already
        else:
            raise TypeError("`data` must be `bytes`, `bytearray`, or opened `io.BufferedIOBase`.")

        cls_name = cls.__name__
        bit_reader = BitFieldReader() if cls._HAS_BIT_FIELDS else None

        if not cls._HAS_DYNAMIC_FIELDS:
            try:
                full_struct = cls._STRUCTS[byte_order, long_varints]
            except KeyError:
                _LOGGER.error(
                    f"No struct exists for `{cls_name}` with byte order {byte_order} and long_varints {long_varints}. "
                    f"If any 'v' or 'V' fields exist, `long_varints` must be specified."
                )
                raise

            if cls._USE_FAST_UNPACK:
                values = reader.unpack_struct(full_struct)
                init_values = {}
                non_init_values = {}
                for field, value, is_init in zip(cls._BINARY_FIELDS, values, cls._BFIELD_INIT, strict=True):
                    (init_values if is_init else non_init_values).__setitem__(field.name, value)

                # noinspection PyArgumentList
                instance = cls(**init_values)
                for field_name, value in non_init_values.items():
                    setattr(instance, field_name, value)

                # Restore old byte order if it was changed from a passed-in `BinaryReader`.
                if old_byte_order is not None:
                    reader.byte_order = old_byte_order

                return instance

            # Set up queue of struct outputs for individual parsing (bit fields, custom unpackers, etc.).
            try:
                struct_output = list(reversed(reader.unpack_struct(full_struct)))
            except Exception as ex:
                _LOGGER.error(
                    f"Could not unpack struct fmt for `{cls_name}`: {full_struct.format} (size {full_struct.size}). "
                    f"Error: {ex}"
                )
                raise

        else:
            # Fields must be unpacked one by one, as some may be skipped based on previous field values.
            struct_output = None

        init_values = {}
        non_init_values = {}
        all_field_values = {}

        for field, field_type, field_metadata in zip(
            cls._BINARY_FIELDS, cls._BFIELD_TYPES, cls._BFIELD_METADATA, strict=True
        ):

            if field_metadata.bit_count == -1 and bit_reader and not bit_reader.empty:
                # Last bit field was not finished. Discard bits.
                bit_reader.clear()

            if field_metadata.should_skip_func is not None:
                if field_metadata.should_skip_func(long_varints, all_field_values):
                    all_field_values[field.name] = None
                    if not field.init:
                        non_init_values[field.name] = None
                    else:
                        init_values[field.name] = None
                    continue

            if field_metadata.bit_count != -1:
                # Read bit field and cast to field type (e.g. `bool` for 1-bit fields).
                if not bit_reader:
                    bit_reader = BitFieldReader()  # first time use
                try:
                    if struct_output is None:
                        field_value = field_type(bit_reader.read(reader, field_metadata.bit_count, field_metadata.fmt))
                    else:
                        field_value = field_type(
                            bit_reader.read_list_buffer(
                                struct_output, field_metadata.bit_count, field_metadata.fmt
                            )
                        )
                except Exception as ex:
                    _LOGGER.error(f"Error occurred while trying to unpack bit field `{cls_name}.{field.name}`: {ex}")
                    raise
                if field_metadata.asserted and field_value not in field_metadata.asserted:
                    raise BinaryFieldValueError(
                        f"Bit field `{cls_name}.{field.name}` (bit count {field_metadata.bit_count}) value "
                        f"{repr(field_value)} is not an asserted value: {field_metadata.asserted}"
                    )
            else:
                # Read normal field.
                try:
                    if struct_output is None:
                        field_value = field_metadata.unpack(list(reader.unpack(field_metadata.fmt)), byte_order)
                    else:
                        field_value = field_metadata.unpack(struct_output, byte_order)
                except Exception as ex:
                    _LOGGER.error(
                        f"Error occurred while trying to unpack field `{cls_name}.{field.name}`: {ex}\n"
                        f"  Unpacked field values: {all_field_values}"
                    )
                    raise

            all_field_values[field.name] = field_value
            if not field.init:
                non_init_values[field.name] = field_value
            else:
                init_values[field.name] = field_value

        # noinspection PyArgumentList
        instance = cls(**init_values)
        for field_name, value in non_init_values.items():
            setattr(instance, field_name, value)

        # Restore old byte order if it was changed from a passed-in `BinaryReader`.
        if old_byte_order is not None:
            reader.byte_order = old_byte_order

        return instance

    @classmethod
    def _get_full_fmt(cls) -> str:
        """Constructs full `BinaryStruct` fmt string, which is complicated only by bit fields."""
        full_fmt = ""
        used_bits = 0
        bit_field_max = 0
        for field, metadata in zip(cls._BINARY_FIELDS, cls._BFIELD_METADATA):
            if metadata.bit_count == -1:
                full_fmt += metadata.fmt
                used_bits = 0
                bit_field_max = 0
                continue

            # Handle bit field:
            if not full_fmt or bit_field_max == 0 or metadata.fmt != full_fmt[-1]:
                # New bit field (or new bit field type).
                full_fmt += metadata.fmt
                used_bits = metadata.bit_count
                bit_field_max = BinaryReader.calcsize_parsed("<" + metadata.fmt) * 8
            elif used_bits + metadata.bit_count > bit_field_max:
                # Bit field type is correct but will be exhausted; new chunk needed.
                full_fmt += metadata.fmt
                used_bits = metadata.bit_count - (bit_field_max - used_bits)
            else:
                # Current bit field not exhausted.
                used_bits += metadata.bit_count
        return full_fmt

    @classmethod
    def from_object(
        cls,
        obj: OBJ_T,
        **field_values,
    ):
        """Create an instance by reading getting field values directly from the attributes of `obj`, with additional
        fields NOT on the object given in `**fields`. Will raise an error if the `init` signature does not match. Fields
        with `init=False` are ignored (all such fields should be asserted or auto-computed).

        Absent fields will be initialized with `None`, which will lead them to being reserved in `to_writer()`.

        Also has the advantage of bypassing type checker for the `int` size subtypes like `byte`, `short`, etc.
        """
        if not cls.__STRUCT_INITIALIZED:
            cls._initialize_binary_metadata()

        for field in dataclasses.fields(cls):  # not just binary fields
            if not field.init:
                if field.name in field_values:
                    raise ValueError(f"Cannot specify non-init binary field `{cls.__name__}.{field.name}`.")
                continue
            if field.name not in field_values:
                value = getattr(obj, field.name, None)
                field_values[field.name] = value

        # noinspection PyArgumentList
        binary_struct = cls(**field_values)
        return binary_struct

    @classmethod
    def from_dict(cls, data: dict[str, tp.Any]):
        """Default is just usage of dictionary as `kwargs`."""
        # noinspection PyArgumentList
        return cls(**data)

    @classmethod
    def object_to_writer(
        cls,
        obj: OBJ_T,
        writer: BinaryWriter | None = None,
        byte_order: ByteOrder = None,
        long_varints: bool = None,
        **field_values,
    ) -> BinaryWriter:
        """Convenience shortcut for creating a struct instance from `obj` and `field_values`, then immediately calling
        `to_writer(writer, reserve_obj=obj, byte_order=byte_order, long_varints=long_varints)` with that struct.

        Note that if `writer` is given, `byte_order` and `long_varints` must be `None`.
        """
        if writer is not None:
            if byte_order is not None:
                raise ValueError("Cannot specify `byte_order` when an existing `BinaryWriter` is given.")
            if long_varints is not None:
                raise ValueError("Cannot specify `long_varints` when an existing `BinaryWriter` is given.")
        binary_struct = cls.from_object(obj, **field_values)
        return binary_struct.to_writer(writer, reserve_obj=obj, byte_order=byte_order, long_varints=long_varints)

    def to_object(self, obj_type: type[OBJ_T], **init_kwargs) -> OBJ_T:
        """Initialize `obj_type` instance by automatically adding field names to `init_kwargs`.

        If `obj_type` is a dataclass, any of this struct's fields that match the name of one of `obj_type`'s fields
        will be used. Otherwise, only fields that do not start with an underscore will be used.
        """
        obj_fields = {f.name for f in dataclasses.fields(obj_type)} if dataclasses.is_dataclass(obj_type) else None
        for field in dataclasses.fields(self):  # not just binary fields
            if obj_fields is not None:
                if field.name not in obj_fields or field.name in init_kwargs:
                    continue  # skip
            elif field.name.startswith("_") or field.name in init_kwargs:
                continue
            value = getattr(self, field.name, field.name)
            if value is None:
                raise ValueError(f"Field `{self.cls_name}.{field.name}` is None. Cannot set to object.")
            init_kwargs[field.name] = value

        # noinspection PyArgumentList
        return obj_type(**init_kwargs)

    @classmethod
    def reader_to_object(cls, reader: BinaryReader, obj_type: type[OBJ_T], **init_kwargs) -> OBJ_T:
        """Convenience method for creating a struct instance with `from_bytes(reader)`, then immediately calling
        `to_object(obj_type, **init_kwargs)` with that struct.
        """
        struct_instance = cls.from_bytes(reader)
        obj = struct_instance.to_object(obj_type, **init_kwargs)
        return obj

    def to_bytes(self, byte_order: ByteOrder = None, long_varints: bool = None):
        """Convert struct to `bytes`, but with the ability to first update `byte_order` or `long_varints`.

        You can call simply `bytes(binary_struct)` if you do not need to change the byte order or varint size.
        """
        writer = self.to_writer(
            writer=None,
            reserve_obj=None,
            byte_order=byte_order,
            long_varints=long_varints,
        )
        if writer.reserved:
            raise ValueError(
                f"`{self.cls_name}` BinaryStruct cannot fill all fields on its own. Use `to_writer()`.\n"
                f"    Remaining: {writer.reserved}"
            )
        return bytes(writer)

    def __bytes__(self) -> bytes:
        """Calls `to_bytes()` without the ability to change byte order or varint size."""
        return self.to_bytes()

    def to_writer(
        self,
        writer: BinaryWriter = None,
        reserve_obj: OBJ_T = None,
        byte_order: ByteOrder = None,
        long_varints: bool = None,
    ) -> BinaryWriter:
        """Use fields to pack this instance into a `BinaryWriter`, which may be given or started automatically.

        Any non-auto-computed fields whose values are `None` will be left as reserved keys in the writer of format:
            '{reserve_prefix}.{field_name}'
        and must be filled with `writer.fill()` by the caller before the writer can be converted to bytes. If
        `reserve_prefix = None` (default), it will default to the name of this class. The main use of setting it
        manually is for nested structs and lists of structs, which will keep chaining their names together and include
        list/tuple indices where relevant (handled automatically).

        `byte_order` and `long_varints` cannot be given if an existing `writer` is given.
        """
        if not self.__STRUCT_INITIALIZED:
            self._initialize_binary_metadata()

        if reserve_obj is None:
            reserve_obj = self

        if writer is not None:
            if byte_order is not None:
                raise ValueError("Cannot specify `byte_order` when an existing `BinaryWriter` is given.")
            if long_varints is not None:
                raise ValueError("Cannot specify `long_varints` when an existing `BinaryWriter` is given.")
        else:
            # Create new writer. `byte_order` has a class default, but `long_varints` must be specified if any fields
            # contain 'v' or 'V' variable int formats.
            byte_order = byte_order or self.DEFAULT_BYTE_ORDER
            writer = BinaryWriter(byte_order, long_varints)

        cls_name = self.cls_name
        bit_writer = BitFieldWriter()

        # Get all field values.
        field_values = {field.name: getattr(self, field.name, None) for field in self._BINARY_FIELDS}

        # Unlike when unpacking, we can use `field_values` immediately to check skips and construct `full_fmt` as we go.
        full_fmt = ""  # for reserving
        struct_input = []  # type: list[float | int | bool | bytes]
        start_offset = writer.position

        def get_fmt_size() -> int:
            nonlocal full_fmt
            if not full_fmt:
                return 0
            return writer.calcsize(full_fmt)  # byte order and long varints parsed inside call

        for field, field_type, field_metadata, field_value in zip(
            self._BINARY_FIELDS, self._BFIELD_TYPES, self._BFIELD_METADATA, field_values.values()
        ):

            if field.metadata.get("NOT_BINARY", False):
                continue  # field excluded

            if field_metadata.should_skip_func is not None:
                if field_metadata.should_skip_func(long_varints, field_values):
                    # Write nothing for this field.
                    continue

            if not bit_writer.empty and field_metadata.bit_count == -1:
                # Pad out bit writer.
                full_fmt += bit_writer.finish_field_buffer(struct_input)

            if field_metadata.bit_count != -1:
                if field_metadata.asserted and field_value not in field_metadata.asserted:
                    raise ValueError(
                        f"Field `{cls_name}.{field.name}` value {repr(field_value)} is not an asserted value: "
                        f"{field_metadata.asserted}"
                    )
                full_fmt += bit_writer.write_to_buffer(
                    struct_input, field_value, field_metadata.bit_count, field_metadata.fmt
                )
                continue

            if field_value is None:
                if field_metadata.single_asserted is None:
                    # Reserved for custom external filling, as it requires data beyond this struct's scope (even just to
                    # choose one of multiple provided asserted values). Current byte order is used.
                    reserve_offset = start_offset + get_fmt_size()
                    writer.mark_reserved_offset(field.name, field_metadata.fmt, reserve_offset, obj=reserve_obj)
                    null_size = writer.calcsize(field_metadata.fmt)
                    struct_input.append(b"\0" * null_size)
                    full_fmt += f"{null_size}s"
                    continue
                else:
                    # Use lone asserted value.
                    field_value = field_metadata.single_asserted

            try:
                field_metadata.pack(struct_input, field_value)
                full_fmt += field_metadata.fmt
            except Exception as ex:
                _LOGGER.error(f"Error occurred while writing binary field `{field.name}`: {ex}")
                raise

        # Single pack call.
        try:
            writer.pack(full_fmt, *struct_input)
        except Exception as ex:
            _LOGGER.error(
                f"Error while packing `{cls_name}`: {ex}\n"
                f"    Fmt: {full_fmt}\n"
                f"    Struct input: {struct_input}"
            )
            raise

        return writer  # may have remaining unfilled fields (any non-auto-computed field with value `None`)

    def fill(self, writer: BinaryWriter, field_name: str, *values: tp.Any):
        """Fill reserved `field_name` in `writer` as reserved with the ID of this instance."""
        writer.fill(field_name, *values, obj=self)

    def fill_multiple(self, writer: BinaryWriter, **field_names_values: tp.Any):
        """Fill multiple reserved fields in `writer` as reserved with the ID of this instance.

        Can only be used with single-value reserved field formats.
        """
        for field_name, value in field_names_values.items():
            writer.fill(field_name, value, obj=self)

    def assert_field_values(self, **field_values):
        for field_name, field_value in field_values.items():
            try:
                value = getattr(self, field_name)
            except AttributeError:
                raise AssertionError(f"Field '{field_name}' does not exist on `{self.cls_name}`.")
            if value != field_value:
                raise AssertionError(f"Field value assertion error: {repr(value)} != asserted {repr(field_value)}")

    def to_dict(self, ignore_underscore_prefix=True) -> dict[str, tp.Any]:
        """Get all current (non-single-asserted) binary fields.

        Ignores fields with value `None` and (by default) underscore names.
        """
        return {
            name: value
            for name, value in self.get_binary_field_values().items()
            if value is not None and (not ignore_underscore_prefix or not name.startswith("_"))
        }

    def copy(self) -> tp.Self:
        return copy.copy(self)

    def deepcopy(self) -> tp.Self:
        return copy.deepcopy(self)

    def pop(self, field_name: str) -> tp.Any:
        """Simply sets `field_name` to None, marking it as 'consumed', without triggering type checkers.

        This has the same general usage pattern as `unpack_deferred_field()` but supports external field processing of
        arbitrary complexity. The main outcome is to ensure that `field_name` is externally reserved when packing.
        """
        value = getattr(self, field_name, None)
        if value is None:
            raise BinaryFieldValueError(f"Field `{self.cls_name}.{field_name}` has no set value to consume.")
        setattr(self, field_name, None)
        return value

    @staticmethod
    def pack_z_string(writer: BinaryWriter, value: str, encoding=""):
        """Convenience function for packing an encoded, null-terminated string."""
        z = b"\0\0" if encoding.startswith("utf-16") else b"\0"
        writer.append(value.encode(encoding) + z)

    def repr_multiline(self) -> str:
        """Only includes binary fields with non-default values."""
        lines = [
            f"{self.cls_name}(",
        ]
        for field in self._BINARY_FIELDS:
            if not field.repr:
                continue  # explicitly excluded
            value = getattr(self, field.name, None)
            if value is None:
                continue
            if field.default not in (None, dataclasses.MISSING) and value == field.default:
                continue
            lines.append(f"  {field.name} = {repr(value)},")
        lines.append(")")
        return "\n".join(lines)

    @classmethod
    def get_fields(cls):
        return dataclasses.fields(cls)

    def get_binary_field_values(self) -> dict[str, tp.Any]:
        """Get all current binary field values, unless it has a single asserted value."""
        field_values = {}
        for field, metadata in zip(self.get_binary_fields(), self._BFIELD_METADATA):
            if metadata.single_asserted is None:
                field_values[field.name] = getattr(self, field.name, None)
        return field_values

    @classmethod
    def get_binary_fields(cls) -> tuple[dataclasses.Field, ...]:
        if cls._BINARY_FIELDS is not None:
            return cls._BINARY_FIELDS
        cls._BINARY_FIELDS = tuple(
            field for field in cls.get_fields()
            if not field.metadata.get("NOT_BINARY", False)
        )
        return cls._BINARY_FIELDS

    @classmethod
    def get_binary_field_types(cls) -> tuple[type, ...]:
        if cls._BFIELD_TYPES is not None:
            return cls._BFIELD_TYPES
        all_type_hints = tp.get_type_hints(cls)
        cls._BFIELD_TYPES = tuple(all_type_hints[field.name] for field in cls.get_binary_fields())
        return cls._BFIELD_TYPES

    @classmethod
    def get_binary_field_names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in cls.get_binary_fields())

    @classmethod
    def get_binary_field_and_type(cls, field_name: str) -> tuple[dataclasses.Field, tp.Type]:
        for field, field_type in zip(cls._BINARY_FIELDS, cls._BFIELD_TYPES):
            if field.name == field_name:
                return field, field_type
        raise KeyError(f"Invalid field for `{cls.__name__}`: {field_name}")

    @classmethod
    def get_size(cls, byte_order: ByteOrder = None, long_varints: bool = None) -> int:
        """Get cached size of struct, based on native alignment and long varints.

        Assumes no fields are skipped.
        """
        if not cls.__STRUCT_INITIALIZED:
            cls._initialize_binary_metadata()

        if byte_order is None:
            byte_order = ByteOrder.LittleEndian  # no alignment
        return cls._STRUCTS[byte_order, long_varints].size

    @staticmethod
    def join_bytes(struct_iterable: tp.Iterable[BinaryStruct]) -> bytes:
        return b"".join(bytes(s) for s in struct_iterable)
