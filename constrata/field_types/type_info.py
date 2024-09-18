from __future__ import annotations

__all__ = [
    "PRIMITIVE_FIELD_TYPING",
    "PRIMITIVE_FIELD_TYPES",
    "ASSERTED_TYPING",
    "PRIMITIVE_FIELD_FMTS",
    "PRIMITIVE_FMT_SIZE_MINMAX",
    "OFFSET_FIELD_TYPES",
]

import typing as tp

from .primitive_types import *


PRIMITIVE_FIELD_TYPING = tp.Union[
    bool, byte, sbyte, ushort, short, uint, int, ulong, long, float, double, bytes, varint, varuint
]

PRIMITIVE_FIELD_TYPES = (
    bool, byte, sbyte, ushort, short, uint, int, ulong, long, float, double, bytes, varint, varuint
)

ASSERTED_TYPING = tp.Union[
    None, bool, int, float, bytes, str,
    list[bool], list[int], list[float], list[bytes], list[str],
]

PRIMITIVE_FIELD_FMTS = {
    bool: "?",
    byte: "B",
    sbyte: "b",
    ushort: "H",
    short: "h",
    uint: "I",
    int: "i",
    ulong: "Q",
    long: "q",
    float: "f",
    double: "d",
    varint: "v",
    varuint: "V",
}

PRIMITIVE_FMT_SIZE_MINMAX = {
    "?": (1, None),
    "B": (1, (0, 2 ** 8 - 1)),
    "b": (1, (-(2 ** 7), (2 ** 7) - 1)),
    "H": (2, (0, (2 ** 16) - 1)),
    "h": (2, (-(2 ** 15), (2 ** 15) - 1)),
    "I": (4, (0, (2 ** 32) - 1)),
    "i": (4, (-(2 ** 31), (2 ** 31) - 1)),
    "Q": (8, (0, (2 ** 64) - 1)),
    "q": (8, (-(2 ** 63), (2 ** 63) - 1)),
    "f": (4, None),
    "d": (8, None),
    "v": (None, None),  # `size` and `min_max` must be computed
    "V": (None, None),  # `size` and `min_max` must be computed
}  # type: dict[str, tuple[int, tuple[int, int] | None]]


# Valid field types for `auto_offset`.
OFFSET_FIELD_TYPES = (
    byte, sbyte, ushort, short, uint, int, ulong, long, varint, varuint
)
