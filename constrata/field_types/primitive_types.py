from __future__ import annotations

__all__ = [
    "byte",
    "uint8",
    "sbyte",
    "int8",
    # "bool",
    "ushort",
    "uint16",
    "short",
    "int16",
    "uint",
    "uint32",
    "int32",
    # "int",
    "ulong",
    "uint64",
    "long",
    "int64",
    "single",
    "float32",
    # "float",
    "double",
    "float64",
    "varint",
    "varuint",
    # "bytes",
    "RESERVED",
]

import typing as tp

# BASIC FIELD TYPES
uint8 = byte = type("byte", (int,), {})
int8 = sbyte = type("sbyte", (int,), {})
# bool = bool  # byte that must be 0 (False) or 1 (True)
uint16 = ushort = type("ushort", (int,), {})
int16 = short = type("short", (int,), {})
uint32 = uint = type("uint", (int,), {})
int32 = int
uint64 = ulong = type("ulong", (int,), {})
int64 = long = type("long", (int,), {})  # actually Python `int`
float32 = single = float
float64 = double = type("double", (float,), {})  # actually Python `float`
varint = type("varint", (int,), {})  # either `int` or `long`
varuint = type("varuint", (int,), {})  # either `uint` or `ulong`
# bytes = bytes

# Alias that can be used to clearly indicate when a field is being reserved.
RESERVED = None  # type: tp.Any
