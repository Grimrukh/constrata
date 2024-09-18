"""Functions for use inside `dataclasses.field()` or as wrappers around `dataclasses.field()` to provide metadata for
binary serialization/deserialization with `constrata.BinaryStruct` subclasses.

The safest way to use these functions is to pass the capitalized versions as double-asterisk-unpacked keyword arguments
to `dataclasses.field()` when defining any non-basic fields. For example:

```
    from dataclasses import dataclass, field
    from constrata import BinaryStruct

    @dataclass(slots=True)
    class MyStruct(BinaryStruct):
        basic_field: int
        _expected_zero: short = field(init=False, **Binary(asserted=0))  # or `field(..., **BinaryPad(2))`
        custom_field: CustomType = field(
            **Binary(fmt="3f", unpack_func=CustomType.read_from_bytes, pack_func=lambda v: v.write_to_bytes())
        )
```

These capitalized functions (`Binary`, `BinaryString`, `BinaryArray`, `BinaryPad`) return dictionaries containing the
field's metadata under the key `"metadata"`, which is then unpacked by the double-asterisk unpacking operator (`**`)
into the `metadata` argument of `dataclasses.field()`. Any other standard arguments for `dataclasses.field()`, such as
`default`, `init`, `repr`, etc., can then be passed as normal and changed as needed with any updates to the `dataclass`
built-in module. Certain IDEs may also only provide nice `dataclass` support (particularly for `cls.__init__`) if they
can see the `field` function being called directly (e.g. PyCharm as of 2024).

If you're not fussed about any of that, the lower-case versions of these functions can be used as wrappers around the
`dataclasses.field` function, with any additional `kwargs` passed through to `dataclasses.field()`. These functions also
modify the default value for `init` to `False` if a single `asserted` value is passed, as by design, you will never want
to initialize these fields to anything other than the asserted value. For example:

```
    from dataclasses import dataclass
    from constrata import BinaryStruct, binary

    @dataclass(slots=True)
    class MyStruct(BinaryStruct):
        basic_field: int
        _expected_zero: short = binary(asserted=0)  # or `binary_pad(2)`
        custom_field: CustomType = binary(
            "3f", unpack_func=CustomType.read_from_bytes, pack_func=lambda v: v.write_to_bytes()
        )
```
"""
from __future__ import annotations

__all__ = [
    "Binary",
    "binary",
    "BinaryString",
    "binary_string",
    "BinaryArray",
    "binary_array",
    "BinaryPad",
    "binary_pad",
]

import dataclasses
import typing as tp

from constrata.field_types.type_info import PRIMITIVE_FIELD_TYPING, PRIMITIVE_FIELD_FMTS
from constrata.metadata import FIELD_T, BinaryMetadata, BinaryStringMetadata, BinaryArrayMetadata


def Binary(
    fmt: str | type[PRIMITIVE_FIELD_TYPING] = None,
    asserted: tuple[FIELD_T, ...] | FIELD_T = None,
    unpack_func: tp.Callable[[PRIMITIVE_FIELD_TYPING], FIELD_T] = None,
    pack_func: tp.Callable[[FIELD_T], PRIMITIVE_FIELD_TYPING] = None,
    bit_count: int = -1,
    should_skip_func: tp.Callable[[bool, dict[str, tp.Any]], bool] = None
):
    if fmt is str or fmt is bytes:
        raise TypeError("Cannot use `Binary()` for `bytes` or `str` fields. Use `BinaryString()` instead.")
    elif fmt in PRIMITIVE_FIELD_FMTS:
        fmt = PRIMITIVE_FIELD_FMTS[fmt]
    elif not isinstance(fmt, str) and fmt is not None:
        raise TypeError(f"Binary `fmt` must be a string, primitive type, or `None` (to use type hint), not: {fmt}")

    if isinstance(asserted, list):
        asserted = tuple(asserted)
    elif asserted is not None and not isinstance(asserted, tuple):
        asserted = (asserted,)

    return {"metadata": {"binary": BinaryMetadata(
        fmt, asserted, unpack_func, pack_func, bit_count, should_skip_func
    )}}


def binary(
    fmt: str | type[PRIMITIVE_FIELD_TYPING] = None,
    asserted: tuple[FIELD_T, ...] | FIELD_T = None,
    unpack_func: tp.Callable[[PRIMITIVE_FIELD_TYPING], FIELD_T] = None,
    pack_func: tp.Callable[[FIELD_T], PRIMITIVE_FIELD_TYPING] = None,
    bit_count: int = -1,
    should_skip_func: tp.Callable[[bool, dict[str, tp.Any]], bool] = None,
    **field_kwargs,
) -> dataclasses.Field:
    metadata = Binary(fmt, asserted, unpack_func, pack_func, bit_count, should_skip_func)
    if metadata["metadata"]["binary"].single_asserted is not None:
        field_kwargs.setdefault("init", False)
    return dataclasses.field(**field_kwargs, metadata=metadata["metadata"])


def BinaryString(
    fmt_or_byte_size: str | int,  # required
    asserted: tuple[FIELD_T, ...] | FIELD_T = None,
    unpack_func: tp.Callable[[PRIMITIVE_FIELD_TYPING], FIELD_T] = None,
    pack_func: tp.Callable[[FIELD_T], PRIMITIVE_FIELD_TYPING] = None,
    encoding: str = None,
    rstrip_null: bool = True,
    should_skip_func: tp.Callable[[bool, dict[str, tp.Any]], bool] = None,
):
    if isinstance(fmt_or_byte_size, int):
        fmt = f"{fmt_or_byte_size}s"
    elif isinstance(fmt_or_byte_size, str):
        fmt = fmt_or_byte_size
        if not fmt.endswith("s"):
            raise ValueError(f"BinaryString `fmt_or_byte_size` must end with 's' if it is a fmt string, not: {fmt}")
    else:
        raise TypeError(
            f"BinaryString `fmt_or_byte_size` must be a 'Ns' fmt string or byte size `N`, not: {fmt_or_byte_size}"
        )

    if isinstance(asserted, list):
        asserted = tuple(asserted)
    elif asserted is not None and not isinstance(asserted, tuple):
        asserted = (asserted,)

    if encoding and encoding.lower().replace("-", "") == "utf16":
        encoding = "utf16"

    # If `rstrip_null=True`, asserted values should be stripped too for comparison.
    if rstrip_null and asserted:
        if encoding is not None:  # asserted value are strings
            asserted = tuple(s.rstrip("\0") for s in asserted)
        else:  # asserted value are bytes
            asserted = tuple(s.rstrip(b"\0") for s in asserted)

    return {"metadata": {"binary": BinaryStringMetadata(
        fmt, asserted, unpack_func, pack_func, bit_count=-1, should_skip_func=should_skip_func,
        encoding=encoding, rstrip_null=rstrip_null,
    )}}


def binary_string(
    fmt_or_byte_size: str | int,  # required
    asserted: tuple[FIELD_T, ...] | FIELD_T = None,
    unpack_func: tp.Callable[[PRIMITIVE_FIELD_TYPING], FIELD_T] = None,
    pack_func: tp.Callable[[FIELD_T], PRIMITIVE_FIELD_TYPING] = None,
    encoding: str = None,
    rstrip_null: bool = True,
    should_skip_func: tp.Callable[[bool, dict[str, tp.Any]], bool] = None,
    **field_kwargs,
) -> dataclasses.Field:
    metadata = BinaryString(fmt_or_byte_size, asserted, unpack_func, pack_func, encoding, rstrip_null, should_skip_func)
    if metadata["metadata"]["binary"].single_asserted is not None:
        field_kwargs.setdefault("init", False)
    return dataclasses.field(**field_kwargs, metadata=metadata["metadata"])


def BinaryArray(
    length: int,
    element_fmt: str | type[PRIMITIVE_FIELD_TYPING] = None,
    asserted: list[FIELD_T] | tuple[list[FIELD_T], ...] = None,
    unpack_func: tp.Callable[[PRIMITIVE_FIELD_TYPING], FIELD_T] = None,
    pack_func: tp.Callable[[FIELD_T], PRIMITIVE_FIELD_TYPING] = None,
    should_skip_func: tp.Callable[[bool, dict[str, tp.Any]], bool] = None,
):
    if element_fmt is str or element_fmt is bytes:
        raise TypeError("Cannot use `Binary()` for bytes/string fields. Use `BinaryString()` instead.")
    elif element_fmt in PRIMITIVE_FIELD_FMTS:
        element_fmt = PRIMITIVE_FIELD_FMTS[element_fmt]
        fmt = f"{length}{element_fmt}"
    elif isinstance(element_fmt, str):
        fmt = f"{length}{element_fmt}"
    elif element_fmt is None:
        fmt = element_fmt
    else:
        raise TypeError(
            f"BinaryArray `element_fmt` must be a string, primitive type, or `None` (to use type hint), "
            f"not: {element_fmt}"
        )

    if isinstance(asserted, list):
        if any(isinstance(element, (list, tuple)) for element in asserted):
            raise TypeError(
                "To give multiple asserted lists/tuples to `BinaryArray()`, you must contain them in a tuple."
            )
        asserted = (asserted,)

    return {"metadata": {"binary": BinaryArrayMetadata(
        length, fmt, asserted, unpack_func, pack_func, should_skip_func
    )}}


def binary_array(
    length: int,
    element_fmt: str | type[PRIMITIVE_FIELD_TYPING] = None,
    asserted: list[FIELD_T] | tuple[list[FIELD_T], ...] = None,
    unpack_func: tp.Callable[[PRIMITIVE_FIELD_TYPING], FIELD_T] = None,
    pack_func: tp.Callable[[FIELD_T], PRIMITIVE_FIELD_TYPING] = None,
    should_skip_func: tp.Callable[[bool, dict[str, tp.Any]], bool] = None,
    **field_kwargs,
) -> dataclasses.Field:
    metadata = BinaryArray(length, element_fmt, asserted, unpack_func, pack_func, should_skip_func)
    if metadata["metadata"]["binary"].single_asserted is not None:
        field_kwargs.setdefault("init", False)
    return dataclasses.field(**field_kwargs, metadata=metadata["metadata"])


def BinaryPad(
    length: int,
    char=b"\0",
    bit_count: int = -1,
    should_skip_func: tp.Callable[[bool, dict[str, tp.Any]], bool] = None,
) -> dict[str, tp.Any]:
    """Will assert `length` bytes of character `char`."""
    if not isinstance(char, bytes):
        raise TypeError("Padding `char` must be `bytes`.")
    pad = char * length
    return {
        "metadata": {
            "binary": BinaryStringMetadata(
                fmt=f"{length}s",
                asserted=(pad,),
                bit_count=bit_count,
                rstrip_null=False,
                should_skip_func=should_skip_func,
            ),
        },
        "repr": False,
    }


def binary_pad(
    length: int,
    char=b"\0",
    bit_count: int = -1,
    should_skip_func: tp.Callable[[bool, dict[str, tp.Any]], bool] = None,
    **field_kwargs,
) -> dataclasses.Field:
    metadata = BinaryPad(length, char, bit_count, should_skip_func)
    field_kwargs.setdefault("init", False)  # pad is single-asserted by definition
    return dataclasses.field(**field_kwargs, metadata=metadata["metadata"])
