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
from constrata.metadata import BinaryMetadata, BinaryStringMetadata
from constrata.metacls import BinaryStructMeta
from constrata.streams import BinaryReader, BinaryWriter

_LOGGER = logging.getLogger("constrata")

OBJ_T = tp.TypeVar("OBJ_T")


class BinaryStruct(metaclass=BinaryStructMeta):
    """Dataclass that supports automatic reading/writing from packed binary data."""

    class _StructMetadata:
        """Internal class that stores combined Struct, offsets, and sizes for each byte order and varint size.

        To ensure correct varint-handling, we initially act as though `True`, `False`, and `None` are all valid
        possibilities for the `long_varints` argument to `from_bytes()` and `to_writer()`, then remove the `None` key
        if any varints are used in the struct (meaning that `long_varints` must be specified).
        """
        _fmts: dict[bool | None, str]  # in-progress per-`long_varints` fmt strings for `Struct` creation
        _structs: dict[tuple[ByteOrder, bool | None], struct.Struct | None]  # canonical `Struct` for packing/unpacking
        _field_offsets: dict[tuple[ByteOrder, bool | None], list[int | None]]  # `None` for bit fields
        _field_sizes: dict[tuple[ByteOrder, bool | None], list[int | None]]  # `None` for bit fields

        _has_varints: bool = False  # whether any varints were used in the struct

        def get_metadata(
            self, byte_order: ByteOrder, long_varints: bool | None
        ) -> tuple[struct.Struct, list[int | None], list[int | None]]:
            """Get the struct metadata for the given `byte_order` and `long_varints`."""
            try:
                return (
                    self._structs[byte_order, long_varints],
                    self._field_offsets[byte_order, long_varints],
                    self._field_sizes[byte_order, long_varints],
                )
            except KeyError:
                raise ValueError(
                    f"No struct metadata found for byte_order {byte_order} and long_varints {long_varints}. "
                    f"If the struct contains any `varint` or `varuint` fields, you must specify `long_varints`."
                )

        def __init__(self):
            self._fmts = {}
            self._structs = {}
            self._field_offsets = {}
            self._field_sizes = {}

            for long_varints in (None, False, True):
                self._fmts[long_varints] = ""
                for byte_order in ByteOrder:
                    self._structs[byte_order, long_varints] = None
                    self._field_offsets[byte_order, long_varints] = []
                    self._field_sizes[byte_order, long_varints] = []

        def skip_field(self):
            """Add `None` offset and size for a bit field."""
            for byte_order in ByteOrder:
                for long_varints in (None, False, True):
                    self._field_offsets[byte_order, long_varints].append(None)
                    self._field_sizes[byte_order, long_varints].append(None)

        def append_fmt_only(self, fmt: str):
            """Extend format for a finished bit run, without adding a field offset/size."""
            if not self._has_varints and ("v" in fmt or "V" in fmt):
                self._has_varints = True

            self._fmts[None] += fmt
            self._fmts[False] += fmt.replace("v", "i").replace("V", "I")
            self._fmts[True] += fmt.replace("v", "q").replace("V", "Q")

        def append_field_fmt(self, fmt: str):
            if not self._has_varints and ("v" in fmt or "V" in fmt):
                self._has_varints = True

            short_fmt = fmt.replace("v", "i").replace("V", "I")
            long_fmt = fmt.replace("v", "q").replace("V", "Q")

            for byte_order in ByteOrder:
                if not self._has_varints:
                    # If we've already confirmed a varint in this struct, this will fail (and full fmt will be unused).
                    previous_full_fmt = self._fmts[None]
                    self._field_offsets[byte_order, None].append(struct.calcsize(byte_order.value + previous_full_fmt))
                    self._field_sizes[byte_order, None].append(struct.calcsize(fmt))
                previous_short_fmt = self._fmts[False]
                previous_long_fmt = self._fmts[True]
                self._field_offsets[byte_order, False].append(struct.calcsize(byte_order.value + previous_short_fmt))
                self._field_offsets[byte_order, True].append(struct.calcsize(byte_order.value + previous_long_fmt))
                self._field_sizes[byte_order, False].append(struct.calcsize(short_fmt))
                self._field_sizes[byte_order, True].append(struct.calcsize(long_fmt))

            # Extend format after offsets and sizes have been calculated above.
            self.append_fmt_only(fmt)

        def finish(self):
            if self._has_varints:
                # Delete `None` key, as it is not valid for structs with varints.
                del self._fmts[None]
                for byte_order in ByteOrder:
                    del self._structs[byte_order, None]
                    del self._field_offsets[byte_order, None]
                    del self._field_sizes[byte_order, None]

            for byte_order in ByteOrder:
                for long_varints, fmt in self._fmts.items():
                    self._structs[byte_order, long_varints] = struct.Struct(byte_order.value + fmt)

    # Caches for class binary information, each constructed on first use and immutable thereafter.
    __STRUCT_INITIALIZED: tp.ClassVar[bool] = False
    _FIELDS: tp.ClassVar[tuple[dataclasses.Field, ...]]  # from `dataclass`; cannot be `None`
    _BINARY_FIELDS: tp.ClassVar[tuple[dataclasses.Field, ...] | None] = None  # filtered from `_FIELDS`
    _BFIELD_TYPES: tp.ClassVar[tuple[type, ...] | None] = None  # all types supported via custom packers/unpackers
    _BFIELD_METADATA: tp.ClassVar[tuple[BinaryMetadata, ...] | None] = None
    _BFIELD_INIT: tp.ClassVar[tuple[bool, ...] | None] = None

    # Different versions of structs, offsets, and sizes are created for all `byte_order` and `long_varints` combos.
    # If the format contains any varints, then `False` and `True` second keys will be present. Otherwise, only `None`.
    _STRUCT_METADATA: tp.ClassVar[_StructMetadata] = None

    # Maps bit field names to a bit shift and bit mask to apply to its struct output/input (one or more 's' bytes).
    _BIT_OFFSET_SHIFT_MASK: tp.ClassVar[dict[str, tuple[int, int]]] = {}

    IS_SIMPLE: tp.ClassVar[bool] = False

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
            self._initialize_metadata()

        # Set single-asserted fields to their default values, regardless of `init` setting.
        for field, field_metadata in zip(self._BINARY_FIELDS, self._BFIELD_METADATA, strict=True):
            if field_metadata.single_asserted is not None:
                setattr(self, field.name, field_metadata.single_asserted)

    @property
    def cls_name(self) -> str:
        """Convenience instance property that returns the class name."""
        return self.__class__.__name__

    @classmethod
    def _initialize_metadata(cls: type[BinaryStruct]) -> None:
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
        cls._STRUCT_METADATA = cls._StructMetadata()

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
                    # TODO: not reliable; need to use `field_type._STRUCTS` in rt.
                    #  Make a `BinarySubstructMetadata` subclass that can be used here.
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

        cls._BIT_OFFSET_SHIFT_MASK = {}
        run_bit_offset = -1  # -1 means no run is currently active
        run_bit_fmt = ""  # a change in the `metadata.fmt` of a bit field forces a new run
        run_bit_fmt_bit_size = 0  # maximum number of bits in run

        field_struct_index = 0  # index into `struct.unpack()/pack()` output/input

        for binary_field, metadata in zip(cls._BINARY_FIELDS, all_metadata):

            if metadata.bit_count != -1:
                # BIT FIELD
                if run_bit_offset == -1:
                    # New run of bit fields has started.
                    run_bit_offset = 0
                    run_bit_fmt = metadata.fmt
                    run_bit_fmt_bit_size = 8 * struct.calcsize(run_bit_fmt)
                elif run_bit_fmt != metadata.fmt or run_bit_offset == run_bit_fmt_bit_size:
                    # Run fmt has changed or previous run is maxed out. Finish current run and start another.
                    run_value_count = (run_bit_offset + run_bit_fmt_bit_size - 1) // run_bit_fmt_bit_size
                    field_struct_index += 1
                    cls._STRUCT_METADATA.append_fmt_only(f"{run_value_count}{run_bit_fmt}")

                    # Start new run (used immediately below).
                    run_bit_offset = 0
                    run_bit_fmt = metadata.fmt
                    run_bit_fmt_bit_size = 8 * struct.calcsize(run_bit_fmt)

                shift = run_bit_offset
                mask = (1 << metadata.bit_count) - 1
                cls._BIT_OFFSET_SHIFT_MASK[binary_field.name] = (shift, mask)
                run_bit_offset += metadata.bit_count

                if run_bit_offset > run_bit_fmt_bit_size:
                    raise BinaryFieldTypeError(
                        binary_field,
                        cls_name,
                        f"Bit field `{binary_field.name}` overflows its bit field run with fmt {run_bit_fmt}. "
                        f"Maximum bit size of run is {run_bit_fmt_bit_size} bits, but this field pushes the offset to "
                        f"{run_bit_offset}."
                    )

                metadata.set_struct_index(field_struct_index)
                # We don't increment bit field struct index until end of run is found.
                cls._STRUCT_METADATA.skip_field()  # no offset or size (cannot be reserved)

            else:
                # NOT A BIT FIELD
                if run_bit_offset >= 0:
                    # Just finished a run of bit fields.
                    bit_field_size = 8 * struct.calcsize(run_bit_fmt)  # in bits
                    run_value_count = (run_bit_offset + bit_field_size - 1) // bit_field_size
                    run_bit_offset = -1  # end run
                    field_struct_index += 1
                    cls._STRUCT_METADATA.append_fmt_only(f"{run_value_count}{run_bit_fmt}")

                # This non-bit field is a single or `metadata.length`-sized `struct` input/output.
                metadata.set_struct_index(field_struct_index)
                field_struct_index += metadata.length or 1
                cls._STRUCT_METADATA.append_field_fmt(metadata.fmt)

        # Check if class is simple (primitive fields only).
        for metadata in cls._BFIELD_METADATA:
            if metadata.bit_count != -1:
                cls.IS_SIMPLE = False
                break
            if metadata.length > 0:
                cls.IS_SIMPLE = False
                break
            if metadata.unpack_func is not None or metadata.pack_func is not None:
                cls.IS_SIMPLE = False
                break
            if isinstance(metadata, BinaryStringMetadata) and metadata.encoding:
                cls.IS_SIMPLE = False
                break

        cls._STRUCT_METADATA.finish()
        cls.__STRUCT_INITIALIZED = True

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
        # This may be the first time the class is used.
        if not cls.__STRUCT_INITIALIZED:
            cls._initialize_metadata()

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

        old_byte_order = None
        old_long_varints = None
        if isinstance(data, (bytes, bytearray, io.BufferedIOBase)):
            # Transient reader; we can set `byte_order` and `long_varints` directly.
            reader = BinaryReader(data, byte_order=byte_order, long_varints=long_varints)
        elif isinstance(data, BinaryReader):
            # Save old `byte_order` and `long_varints`.
            reader = data  # assumes it is at the correct offset already
            if byte_order is not None:
                old_byte_order, reader.byte_order = byte_order, reader.byte_order
            else:
                byte_order = reader.byte_order
            if long_varints is not None:
                old_long_varints, reader.long_varints = long_varints, reader.long_varints
            else:
                long_varints = reader.long_varints
        else:
            raise TypeError("`data` must be `bytes`, `bytearray`, or opened `io.BufferedIOBase`.")

        def restore_reader():
            if old_byte_order is not None:
                reader.byte_order = old_byte_order
            if old_long_varints is not None:
                reader.long_varints = old_long_varints

        cls_name = cls.__name__

        try:
            internal_struct, _, _ = cls._STRUCT_METADATA.get_metadata(byte_order, long_varints)
        except KeyError:
            _LOGGER.error(
                f"No struct exists for `{cls_name}` with byte order {byte_order} and long_varints {long_varints}. "
                f"If any 'v' or 'V' fields exist, `long_varints` must be specified."
            )
            raise
        finally:
            restore_reader()

        struct_output = reader.unpack_struct(internal_struct)
        all_field_values = {}  # for logging errors

        field_values = []
        for field, field_type, field_metadata in zip(
            cls._BINARY_FIELDS, cls._BFIELD_TYPES, cls._BFIELD_METADATA, strict=True
        ):
            index = field_metadata.struct_index

            if cls.IS_SIMPLE:
                # Only need to validate.
                value = struct_output[index]
                try:
                    field_metadata.validate(value)
                finally:
                    restore_reader()
                field_values.append(value)
                all_field_values[field.name] = value
                continue

            # Handle arrays, strings, bit fields, and custom unpackers.
            if field_metadata.length > 0:
                # Array of values. (No bit fields here.)
                value = list(struct_output[index:index + field_metadata.length])
            else:
                # Single value.
                value = struct_output[index]

                if field.name in cls._BIT_OFFSET_SHIFT_MASK:
                    shift, mask = cls._BIT_OFFSET_SHIFT_MASK[field.name]
                    value = (value >> shift) & mask

            # Additional processing and asserted check.
            try:
                value = field_metadata.process_from_unpack(value, byte_order)
            except Exception as ex:
                _LOGGER.error(
                    f"Error occurred while trying to unpack field `{cls_name}.{field.name}`: {ex}\n"
                    f"  Unpacked field values: {all_field_values}"
                )
                raise
            finally:
                restore_reader()

            try:
                field_metadata.validate(value)  # will raise error on fail
            except:
                raise
            finally:
                restore_reader()

            field_values.append(value)
            all_field_values[field.name] = value

        init_values = {}
        non_init_values = {}
        for field, value, is_init in zip(cls._BINARY_FIELDS, field_values, cls._BFIELD_INIT, strict=True):
            (init_values if is_init else non_init_values).__setitem__(field.name, value)

        # noinspection PyArgumentList
        instance = cls(**init_values)
        for field_name, value in non_init_values.items():
            setattr(instance, field_name, value)

        restore_reader()

        return instance

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
            cls._initialize_metadata()

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
        """Use fields to pack this instance into a `BinaryWriter`, which may be existing or created.

        If `byte_order` and `long_varints` are given along with an existing `writer`, they will temporarily override
        that writer's settings for this call only.

        Any non-auto-computed fields whose values are `None` will be left as reserved keys in the writer of format:
            '{reserve_prefix}.{field_name}'
        and must be filled with `writer.fill()` by the caller before the writer can be converted to bytes. If
        `reserve_prefix = None` (default), it will default to the name of this class. The main use of setting it
        manually is for nested structs and lists of structs, which will keep chaining their names together and include
        list/tuple indices where relevant (handled automatically).
        """
        # No need to check struct initialization here, as it is necessarily done in `self.__post_init__()`.

        if reserve_obj is None:
            reserve_obj = self

        old_byte_order = None
        old_long_varints = None

        if writer is not None:
            if byte_order is not None:
                old_byte_order, writer.byte_order = writer.byte_order, byte_order
            else:
                byte_order = writer.byte_order
            if long_varints is not None:
                old_long_varints, writer.long_varints = writer.long_varints, long_varints
            else:
                long_varints = writer.long_varints
        else:
            # Create new writer. `byte_order` has a class default, but `long_varints` must be specified if any fields
            # contain 'v' or 'V' variable int formats.
            byte_order = byte_order or self.DEFAULT_BYTE_ORDER
            writer = BinaryWriter(byte_order, long_varints)

        def restore_writer():
            if old_byte_order is not None:
                writer.byte_order = old_byte_order
            if old_long_varints is not None:
                writer.long_varints = old_long_varints

        cls_name = self.cls_name
        start_offset = writer.position

        # Map all field names to current (or single-asserted) values.
        field_values = self.get_binary_field_values(include_single_asserted=True)

        try:
            internal_struct, field_offsets, field_sizes = self._STRUCT_METADATA.get_metadata(byte_order, long_varints)
        except KeyError:
            _LOGGER.error(
                f"No struct exists for `{cls_name}` with byte order {byte_order} and long_varints {long_varints}. "
                f"If any 'v' or 'V' fields exist, `long_varints` must be specified."
            )
            raise
        finally:
            restore_writer()

        for (field_name, field_value), field_metadata, field_offset, field_size in zip(
            field_values.items(), self._BFIELD_METADATA, field_offsets, field_sizes, strict=True
        ):
            if field_value is not None:
                continue
            # Add reserve pad value and mark reserved offset (absolute offset in `writer`).
            writer.mark_reserved_offset(field_name, field_metadata.fmt, start_offset + field_offset, obj=reserve_obj)
            # TODO: use a different reserve pattern like 0xFE?
            field_values[field_name] = field_metadata.get_null(field_size)

        struct_input = []
        run_index = -1
        run_bits = 0
        for field, field_type, field_metadata, field_value in zip(
            self._BINARY_FIELDS, self._BFIELD_TYPES, self._BFIELD_METADATA, field_values.values()
        ):
            # We always validate first.
            try:
                field_metadata.validate(field_value)
            finally:
                restore_writer()

            if self.IS_SIMPLE:
                # All field values are struct-ready (no strings to encode, or arrays, or bit fields).
                struct_input.append(field_value)
                continue

            # First, process field value (encode string, custom pack).
            packing_value = field_metadata.process_to_pack(field_value, byte_order)

            if field_metadata.length > 0:
                # Extend struct input with array values.
                struct_input.extend(packing_value)
            elif field.name in self._BIT_OFFSET_SHIFT_MASK:
                # Accumulate bit fields into a single run.
                shift, mask = self._BIT_OFFSET_SHIFT_MASK[field.name]
                if packing_value & ~mask:
                    raise BinaryFieldValueError(
                        f"Field `{cls_name}.{field.name}` value {repr(field_value)} is out of range for "
                        f"bit field with mask {mask:b} (field bit count = {field_metadata.bit_count})."
                    )
                if run_index == -1:
                    # No run currently active, so nothing to finish.
                    # Start a new run.
                    run_index = field_metadata.struct_index
                    run_bits = 0
                elif field_metadata.struct_index != run_index:
                    # Finish current run and start new run (different field fmt).
                    struct_input.append(run_bits)
                    run_index = field_metadata.struct_index
                    run_bits = 0

                run_bits |= (packing_value & mask) << shift
            else:
                if run_index != -1:
                    # Finish previous run of bit fields.
                    struct_input.append(run_bits)
                    run_index = -1
                    run_bits = 0
                # Standard single value.
                struct_input.append(packing_value)

        # Single pack call.
        try:
            writer.pack_struct(internal_struct, *struct_input)
        except Exception as ex:
            _LOGGER.error(
                f"Could not pack struct fmt for `{cls_name}`: {internal_struct.format} (size {internal_struct.size}). "
                f"Error: {ex}"
            )
            raise
        finally:
            restore_writer()

        return writer  # done (may have reserved pad fields)

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
            for name, value in self.get_binary_field_values(include_single_asserted=False).items()
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
            lines.append(f"  {field.name:>20} = {repr(value)},")
        lines.append(")")
        return "\n".join(lines)

    @classmethod
    def get_fields(cls):
        return dataclasses.fields(cls)

    def get_binary_field_values(self, include_single_asserted=False) -> dict[str, tp.Any]:
        """Get all current binary field values. By default, omit single-asserted values."""
        field_values = {}
        for field, metadata in zip(self.get_binary_fields(), self._BFIELD_METADATA):
            if metadata.single_asserted is None:
                field_values[field.name] = getattr(self, field.name, None)
            elif include_single_asserted:
                field_values[field.name] = metadata.single_asserted
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
            cls._initialize_metadata()  # could be first time class is used

        if byte_order is None:
            byte_order = ByteOrder.LittleEndian  # no alignment
        return cls._STRUCT_METADATA.get_metadata(byte_order, long_varints)[0].size

    @staticmethod
    def join_bytes(struct_iterable: tp.Iterable[BinaryStruct]) -> bytes:
        return b"".join(bytes(s) for s in struct_iterable)
