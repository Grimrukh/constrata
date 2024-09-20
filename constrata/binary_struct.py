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
from constrata.metadata import FIELD_T, BinaryMetadata, BinaryArrayMetadata
from constrata.streams import BinaryReader, BinaryWriter, BitFieldReader, BitFieldWriter

_LOGGER = logging.getLogger("constrata")


OBJ_T = tp.TypeVar("OBJ_T")


@dataclasses.dataclass(slots=True)
class BinaryStruct:
    """Dataclass that supports automatic reading/writing from packed binary data."""

    # Caches for class binary information, each constructed on first use.
    _STRUCT_INITIALIZED: tp.ClassVar[bool] = False
    _FIELDS: tp.ClassVar[tuple[dataclasses.Field, ...] | None] = None
    _FIELD_TYPES: tp.ClassVar[tuple[type[FIELD_T], ...] | None] = None
    _FIELD_METADATA: tp.ClassVar[tuple[BinaryMetadata, ...] | None] = None
    _FIELD_PACKERS: tp.ClassVar[tuple[tp.Callable, ...] | None] = None
    _FIELD_UNPACKERS: tp.ClassVar[tuple[tp.Callable, ...] | None] = None

    # Optional dictionary for subclass use that maps field type names to default metadata factories.
    # Example:
    #   `{'Vector3': lambda: BinaryArrayMetadata(3, '3f', unpack_func=Vector3)}`
    # This allows pure annotated fields like `position: Vector3` to be used without needing to specify field metadata.
    # Note that metadta `pack_func` may not be required if the custom type defines an `__iter__` method that converts it
    # to a list of primitive values supported by `struct.pack()` (e.g. such that `pack(*v3) == pack(v3.x, v3.y, v3.z)`).
    METADATA_FACTORIES: tp.ClassVar[dict[str, tp.Callable[[], BinaryMetadata]]] = {}

    # Set by `from_bytes()` class method, or can be manually set.
    # Will be auto-detected from values with `get_byte_order()` (defaulting to `LittleEndian`) if not defined, e.g.
    # by a manually-constructed instance.
    byte_order: None | ByteOrder = dataclasses.field(init=False, repr=False, default=None)
    long_varints: None | bool = dataclasses.field(init=False, repr=False, default=None)

    def __init_subclass__(cls, **kwargs) -> None:
        if "unpackers" in kwargs:
            cls._CUSTOM_UNPACKERS = copy.copy(kwargs.pop("unpackers"))
        if kwargs:
            raise TypeError(f"Invalid keyword arguments for `BinaryStruct` subclass: {kwargs}. ")
        super(BinaryStruct, cls).__init_subclass__()

    def __post_init__(self) -> None:
        if not self._STRUCT_INITIALIZED:
            self._initialize_struct_cls()

        # Set single-asserted fields to their default values, regardless of `init` setting.
        for field, field_metadata in zip(self._FIELDS, self._FIELD_METADATA, strict=True):
            if field_metadata.single_asserted is not None:
                setattr(self, field.name, field_metadata.single_asserted)

        if not hasattr(self, "byte_order"):
            self.byte_order = None
        if not hasattr(self, "long_varints"):
            self.long_varints = None

    @property
    def cls_name(self):
        return self.__class__.__name__

    @classmethod
    def _initialize_struct_cls(cls):
        if not dataclasses.is_dataclass(cls):
            raise TypeError(f"BinaryStruct subclass `{cls.__name__}` is missing a `dataclass` decorator.")

        cls_name = cls.__name__
        binary_dc_fields = cls.get_binary_fields()
        if not binary_dc_fields:
            raise TypeError(f"`BinaryStruct` subclass `{cls_name}` has no binary fields.")

        all_metadata = []
        all_packers = []
        all_unpackers = []

        for dc_field, field_type in zip(binary_dc_fields, cls.get_binary_field_types()):

            if isinstance(field_type, GenericAlias):
                if field_type.__origin__ is not list:
                    raise BinaryFieldTypeError(
                        dc_field, cls_name, "Binary fields types cannot be `tuple`. Use `list[type]`."
                    )
                field_type_name = "list"
            else:
                field_type_name = field_type.__name__

            metadata = dc_field.metadata.get("binary", None)  # type: BinaryMetadata | None
            if metadata is None:
                # NOTE: We can't add new keys to `field.metadata` now, but store it in `_FIELD_METADATA`.

                if field_type_name in cls.METADATA_FACTORIES:
                    try:
                        metadata = cls.METADATA_FACTORIES[field_type_name]()
                    except Exception as ex:
                        raise BinaryFieldTypeError(
                            dc_field,
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
                            dc_field,
                            cls_name,
                            f"Field with non-primitive type `{field_type.__name__}` must have `fmt` metadata.",
                        )

                    metadata = BinaryMetadata(fmt)
                    metadata.field_type = field_type

            metadata.field_name = dc_field.name
            metadata.field_type = field_type

            if metadata.fmt is None:
                if isinstance(metadata, BinaryArrayMetadata):
                    if (
                        not isinstance(field_type, GenericAlias)
                        or field_type.__origin__ != list
                        or len(field_type.__args__) != 1
                    ):
                        raise BinaryFieldTypeError(
                            dc_field, cls_name, f"Type hint for binary array field must be `list[type]`."
                        )
                    element_type = field_type.__args__[0]
                    if element_type in PRIMITIVE_FIELD_FMTS:
                        metadata.fmt = f"{metadata.length}{PRIMITIVE_FIELD_FMTS[element_type]}"
                    else:
                        raise BinaryFieldTypeError(
                            dc_field,
                            cls_name,
                            f"Array field with non-primitive element type `{element_type.__name__}` "
                            f"must have `fmt` metadata.",
                        )
                else:
                    if field_type in PRIMITIVE_FIELD_FMTS:
                        metadata.fmt = PRIMITIVE_FIELD_FMTS[field_type]
                    else:
                        raise BinaryFieldTypeError(
                            dc_field,
                            cls_name,
                            f"Field with non-primitive, non-BinaryStruct type `{field_type.__name__}` must have `fmt` "
                            f"metadata.",
                        )

            if field_type not in PRIMITIVE_FIELD_FMTS:
                # Use custom non-primitive type to unpack and pack (iter).
                if metadata.unpack_func is None:
                    metadata.unpack_func = field_type
                if metadata.pack_func is None:
                    # Just validate `__iter__` presence.
                    if not hasattr(field_type, "__iter__"):
                        raise BinaryFieldTypeError(
                            dc_field,
                            cls_name,
                            f"Non-primitive field type `{field_type.__name__}` must have `unpack_func` metadata or "
                            f"implement `__iter__` to enable default handling."
                        )

            all_metadata.append(metadata)
            all_unpackers.append(metadata.get_unpacker())
            all_packers.append(metadata.get_packer())

        cls._FIELD_METADATA = tuple(all_metadata)
        cls._FIELD_UNPACKERS = tuple(all_unpackers)
        cls._FIELD_PACKERS = tuple(all_packers)
        cls._STRUCT_INITIALIZED = True

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
        if not cls._STRUCT_INITIALIZED:
            cls._initialize_struct_cls()

        if byte_order is None:
            if isinstance(data, BinaryReader):
                byte_order = data.default_byte_order
            else:  # default
                byte_order = ByteOrder.LittleEndian
        elif isinstance(byte_order, str):
            byte_order = ByteOrder(byte_order)
        elif not isinstance(byte_order, ByteOrder):
            raise ValueError(f"Invalid `byte_order`: {byte_order}")

        if long_varints is None:
            if isinstance(data, BinaryReader):
                long_varints = data.long_varints
            # Otherwise, leave as `None` and allow errors to occur if varint fields are found.

        old_byte_order = None
        if isinstance(data, (bytes, bytearray, io.BufferedIOBase)):
            # Transient reader; we can set the byte order directly.
            reader = BinaryReader(data, default_byte_order=byte_order)
        elif isinstance(data, BinaryReader):
            # Save old byte order if it is different.
            if byte_order != data.default_byte_order:
                old_byte_order = data.default_byte_order
            reader = data  # assumes it is at the correct offset already
        else:
            raise TypeError("`data` must be `bytes`, `bytearray`, or opened `io.BufferedIOBase`.")

        cls_name = cls.__name__
        bit_reader = BitFieldReader()

        full_fmt = cls.get_full_fmt()
        careful_unpack_mode = any(metadata.should_skip_func is not None for metadata in cls._FIELD_METADATA)

        if not careful_unpack_mode:
            try:
                struct_output = list(reversed(reader.unpack(full_fmt)))
            except Exception as ex:
                _LOGGER.error(f"Could not unpack struct fmt for `{cls_name}`: {full_fmt}. Error: {ex}")
                raise
        else:
            struct_output = None

        init_values = {}
        non_init_values = {}
        all_field_values = {}

        for field, field_type, field_metadata, field_unpacker in zip(
            cls._FIELDS, cls._FIELD_TYPES, cls._FIELD_METADATA, cls._FIELD_UNPACKERS, strict=True
        ):

            if field_metadata.bit_count == -1 and not bit_reader.empty:
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
                try:
                    if struct_output is None:
                        field_value = field_type(bit_reader.read(reader, field_metadata.bit_count, field_metadata.fmt))
                    else:
                        field_value = field_type(bit_reader.read_list_buffer(
                            struct_output, field_metadata.bit_count, field_metadata.fmt)
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
                        field_value = field_unpacker(list(reader.unpack(field_metadata.fmt)))
                    else:
                        field_value = field_unpacker(struct_output)
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
        instance.byte_order = byte_order
        instance.long_varints = long_varints
        for field_name, value in non_init_values.items():
            setattr(instance, field_name, value)

        # Restore old byte order if it was changed from a passed-in `BinaryReader`.
        if old_byte_order is not None:
            reader.default_byte_order = old_byte_order

        return instance

    @classmethod
    def get_full_fmt(cls, long_varints: bool = None, field_values: dict[str, tp.Any] = None) -> str:
        """Constructs full `BinaryStruct` fmt string, which is complicated only by bit fields.

        If `long_varints` and/or `field_values` are given, `should_skip_func` will be checked for each field (e.g. when
        packing). This can't be done when unpacking, though, and this method is therefore not used.
        """
        if not cls._STRUCT_INITIALIZED:
            cls._initialize_struct_cls()

        full_fmt = ""
        used_bits = 0
        bit_field_max = 0
        for field, metadata in zip(cls._FIELDS, cls._FIELD_METADATA):
            if field_values is not None and metadata.should_skip_func is not None:
                if metadata.should_skip_func(long_varints, field_values):
                    continue  # skip field
            if metadata.bit_count == -1:
                full_fmt += metadata.fmt
                used_bits = 0
                bit_field_max = 0
            elif not full_fmt or bit_field_max == 0 or metadata.fmt != full_fmt[-1]:
                # New bit field (or new bit field type).
                full_fmt += metadata.fmt
                used_bits = metadata.bit_count
                bit_field_max = struct.calcsize(metadata.fmt) * 8
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
        byte_order: ByteOrder = None,
        long_varints: bool = None,
        **field_values,
    ):
        """Create an instance by reading getting field values directly from the attributes of `obj`, with additional
        fields NOT on the object given in `**fields`. Will raise an error if the `init` signature does not match. Fields
        with `init=False` are ignored (all such fields should be asserted or auto-computed).

        Absent fields will be initialized with `None`, which will lead them to being reserved in `to_writer()`.

        Also has the advantage of bypassing type checker for the `int` size subtypes like `byte`, `short`, etc.
        """
        if not cls._STRUCT_INITIALIZED:
            cls._initialize_struct_cls()

        for field in cls._FIELDS:
            if not field.init:
                if field.name in field_values:
                    raise ValueError(f"Cannot specify non-init binary field `{cls.__name__}.{field.name}`.")
                continue
            if field.name not in field_values:
                value = getattr(obj, field.name, None)
                field_values[field.name] = value

        # noinspection PyArgumentList
        binary_struct = cls(**field_values)
        binary_struct.byte_order = byte_order
        binary_struct.long_varints = long_varints
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
        `to_writer(writer, reserve_obj=obj)` with that struct.
        """
        if byte_order is None and writer is not None:
            byte_order = writer.default_byte_order
        if long_varints is None and writer is not None:
            long_varints = writer.long_varints
        binary_struct = cls.from_object(obj, byte_order=byte_order, long_varints=long_varints, **field_values)
        return binary_struct.to_writer(writer, reserve_obj=obj)

    def to_object(self, obj_type: type[OBJ_T], **init_kwargs) -> OBJ_T:
        """Initialize `obj_type` instance by automatically adding field names to `init_kwargs`.

        If `obj_type` is a dataclass, any of this struct's fields that match the name of one of `obj_type`'s fields
        will be used. Otherwise, only fields that do not start with an underscore will be used.
        """
        obj_fields = {f.name for f in dataclasses.fields(obj_type)} if dataclasses.is_dataclass(obj_type) else None
        for field in self._FIELDS:
            if obj_fields is not None:
                if field.name not in obj_fields or field.name in init_kwargs:
                    continue  # skip
            elif field.name.startswith("_") or field.name in init_kwargs:
                continue
            value = getattr(self, field.name, field.name)
            if value is None:
                raise ValueError(f"Field `{self.cls_name}.{field.name}` is None. Cannot set to object.")
            init_kwargs[field.name] = value

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
        if byte_order is not None:
            self.byte_order = byte_order
        if long_varints is not None:
            self.long_varints = long_varints
        writer = self.to_writer()
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
        self, writer: BinaryWriter = None, reserve_obj: OBJ_T = None, byte_order: ByteOrder = None
    ) -> BinaryWriter:
        """Use fields to pack this instance into a `BinaryWriter`, which may be given or started automatically.

        Any non-auto-computed fields whose values are `None` will be left as reserved keys in the writer of format:
            '{reserve_prefix}.{field_name}'
        and must be filled with `writer.fill()` by the caller before the writer can be converted to bytes. If
        `reserve_prefix = None` (default), it will default to the name of this class. The main use of setting it
        manually is for nested structs and lists of structs, which will keep chaining their names together and include
        list/tuple indices where relevant (handled automatically).
        """
        if not self._STRUCT_INITIALIZED:
            self._initialize_struct_cls()

        if reserve_obj is None:
            reserve_obj = self

        # Preference for byte order: argument, `self`, `writer`, or `get_default_byte_order()`.
        old_byte_order = None
        if byte_order is None:
            if self.byte_order is not None:
                byte_order = self.byte_order
            elif writer is not None:
                byte_order = writer.default_byte_order
            else:
                byte_order = self.get_default_byte_order()

            # Warn about byte order override (from struct or default).
            if writer is not None:
                if writer.default_byte_order != byte_order:
                    _LOGGER.warning(
                        f"Existing writer passed to `{self.cls_name}.to_writer()` has default byte order "
                        f"{writer.default_byte_order}, but this struct wants to use {byte_order}. Using this struct's "
                        f"byte order temporarily."
                    )
                    old_byte_order = writer.default_byte_order
                    writer.default_byte_order = byte_order

        if self.long_varints is None and writer is not None:
            long_varints = writer.long_varints
        else:
            # `long_varints` may be left as None (e.g. for formats that do not care about it) but an error will be
            # raised if any `varint` or `varuint` fields are encountered.
            long_varints = self.long_varints

        if writer is None:
            writer = BinaryWriter(byte_order)  # NOTE: `BinaryWriter.long_varints` not used here

        cls_name = self.cls_name
        bit_writer = BitFieldWriter()

        # Get all field values.
        field_values = {field.name: getattr(self, field.name, None) for field in self._FIELDS}

        # Unlike when unpacking, we can use `field_values` immediately to check skips and construct `full_fmt` as we go.
        full_fmt = ""  # for reserving
        struct_input = []  # type: list[float | int | bool | bytes]
        start_offset = writer.position

        def get_fmt_size() -> int:
            nonlocal full_fmt
            if not full_fmt:
                return 0
            if long_varints is None:
                return struct.calcsize(full_fmt)
            return writer.calcsize(full_fmt)

        for field, field_type, field_metadata, field_packer, field_value in zip(
            self._FIELDS, self._FIELD_TYPES, self._FIELD_METADATA, self._FIELD_PACKERS, field_values.values()
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
                    reserve_fmt = byte_order.value + field_metadata.fmt
                    writer.mark_reserved_offset(field.name, reserve_fmt, reserve_offset, obj=reserve_obj)
                    null_size = writer.calcsize(reserve_fmt)
                    struct_input.append(b"\0" * null_size)
                    full_fmt += f"{null_size}s"
                    continue
                else:
                    # Use lone asserted value.
                    field_value = field_metadata.single_asserted

            try:
                field_packer(struct_input, field_value)
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

        if old_byte_order is not None:
            writer.default_byte_order = byte_order

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

    def get_default_byte_order(self) -> ByteOrder:
        """Utility for subclasses to indicate their own default `byte_order`.

        Called on pack if `self.byte_order` is not already assigned. This base method also logs a warning.
        """
        _LOGGER.warning(f"Byte order defaulting to `LittleEndian` for `{self.cls_name}`.")
        return ByteOrder.LittleEndian

    def repr_multiline(self) -> str:
        """Only includes binary fields with non-default values."""
        lines = [
            f"{self.cls_name}(",
        ]
        for field in self._FIELDS:
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

    def get_binary_field_values(self) -> dict[str, tp.Any]:
        """Get all current binary field values, unless it has a single asserted value."""
        field_values = {}
        for field, metadata in zip(self.get_binary_fields(), self._FIELD_METADATA):
            if metadata.single_asserted is None:
                field_values[field.name] = getattr(self, field.name, None)
        return field_values

    @classmethod
    def get_binary_fields(cls) -> tuple[dataclasses.Field, ...]:
        if cls._FIELDS is not None:
            return cls._FIELDS
        cls._FIELDS = tuple(
            field for field in dataclasses.fields(cls)
            if field.name not in {"byte_order", "long_varints"}
            and not field.metadata.get("NOT_BINARY", False)
        )
        return cls._FIELDS

    @classmethod
    def get_binary_field_types(cls) -> tuple[type[FIELD_T], ...]:
        if cls._FIELD_TYPES is not None:
            return cls._FIELD_TYPES
        all_type_hints = tp.get_type_hints(cls)
        cls._FIELD_TYPES = tuple(all_type_hints[field.name] for field in cls.get_binary_fields())
        return cls._FIELD_TYPES

    @classmethod
    def get_binary_field_names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in cls.get_binary_fields())

    @classmethod
    def get_binary_field_and_type(cls, field_name: str) -> tuple[dataclasses.Field, tp.Type]:
        for field, field_type in zip(cls._FIELDS, cls._FIELD_TYPES):
            if field.name == field_name:
                return field, field_type
        raise KeyError(f"Invalid field for `{cls.__name__}`: {field_name}")

    @classmethod
    def get_size(cls, byte_order: ByteOrder = ByteOrder.LittleEndian, long_varints: bool = None) -> int:
        """Get full format of this struct, then calculate its size using the given `byte_order` and `long_varints`.

        `byte_order` will default to `LittleEndian`; only a change to `NativeAutoAligned` would potentially change the
        size of the struct, which is unlikely for game formats. `long_varints` may be omitted, but an error will be
        raised if any `varint` or `varuint` fields are used in the struct.
        """
        full_fmt = byte_order.value + cls.get_full_fmt()
        if "v" in full_fmt or "V" in full_fmt:
            if long_varints is None:
                raise ValueError(f"Struct `{cls.__name__}` has varint fields. `long_varints` must be set to get size.")
            if long_varints:
                full_fmt = full_fmt.replace("v", "q").replace("V", "Q")
            else:
                full_fmt = full_fmt.replace("v", "i").replace("V", "I")
        return struct.calcsize(full_fmt)

    @staticmethod
    def join_bytes(struct_iterable: tp.Iterable[BinaryStruct]) -> bytes:
        return b"".join(bytes(s) for s in struct_iterable)
