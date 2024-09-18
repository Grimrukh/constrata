from distutils.dep_util import newer

# constrata

An efficient Python library for parsing and building binary data structures based on the built-in `dataclasses` module,
with support for reserving/filling fields, pure field type support (without `= field(...)`), asserted values, and more.

Pure Python, no dependencies, MIT license. **Requires Python 3.11 or later.**

## Installation

```shell
pip install constrata
```

## Usage

Use `dataclasses.dataclass` and `dataclasses.field` as you normally would to define a subclass of 
`constrata.BinaryStruct`, and specify the binary format, size, asserted values, and unpacking/packing functions of 
fields using `constrata` 'metadata' constructors. Many of these fields can be determined automatically by `constrata` 
based on the field type hint, and custom metadata factories can be added to expand the range of such automatic support. 

Usage of the genuine `dataclasses.field` with double-asterisk metadata arguments like `**Binary()` is recommended if you
want your IDE to continue detecting field types and default values for `__init__` (e.g. in PyCharm) -- but you can
equivalently use full `field` wrappers such as `binary()` for cleaner code and automatic `init=False` arguments for
asserted fields (but IDE dataclass support may break).

`BinaryStruct` subclasses **must use the `@dataclass` wrapper**. They do not require `slots=True`, but as these classes
are intended to represent binary data structures and should never have undefined fields anyway, there is NO reason not 
to take the performance gain here.

## Basic Example

```python
from dataclasses import dataclass, field
from constrata import BinaryStruct, Binary, BinaryString, BinaryArray, BinaryPad
from constrata.field_types import *

@dataclass(slots=True)
class MyStruct(BinaryStruct):
    my_int32: int
    my_uint64: uint64
    my_single: float32
    my_double: float64 = field(**Binary(asserted=(1.0, 2.0, 3.0)))  # only three permitted values
    _padding: bytes = field(init=False, **BinaryPad(8))
    my_ascii_string: str = field(**BinaryString(12, encoding="ascii"))
    my_eight_bools: list[bool] = field(default_factory=lambda: [False] * 8, **BinaryArray(8))
    my_bitflag1: bool = field(default=False, **Binary(bit_count=1))
    my_bitflag2: bool = field(default=True, **Binary(bit_count=1))
    # Six unused bits in byte skipped here (and must be 0).

# Read from a file.
bin_path = "my_struct.bin"
with open(bin_path, "rb") as f:
    my_struct = MyStruct.from_bytes(f)

# Modify fields.
my_struct.my_int32 = 123
my_struct.my_bitflag2 = True

# Write to a file.
my_struct_bytes = my_struct.to_bytes()
new_bin_path = "my_struct_new.bin"
with open(new_bin_path, "wb") as f:
    f.write(my_struct_bytes)

# Create a new instance from scratch as a standard dataclass.
new_struct = MyStruct(0, 0, 0.0, 1.0, my_ascii_string="helloworld")
```

An identical `MyStruct` can be defined using direct `dataclasses.field` wrappers:

```python
from dataclasses import dataclass
from constrata import BinaryStruct, binary, binary_string, binary_array, binary_pad
from constrata.field_types import *

@dataclass(slots=True)
class MyStruct(BinaryStruct):
    my_int32: int
    my_uint64: uint64
    my_single: float32
    my_double: float64 = binary(asserted=(1.0, 2.0, 3.0))  # only three permitted values
    _padding: bytes = binary_pad(8)  # `init=False` is automatic
    my_ascii_string: str = binary_string(12, encoding="ascii")
    my_eight_bools: list[bool] = binary_array(8, default_factory=lambda: [False] * 8)
    my_bitflag1: bool = binary(default=False, bit_count=1)
    my_bitflag2: bool = binary(default=True, bit_count=1)
    # Six unused bits in byte skipped here (and must be 0).
```

## Reserving and Filling Fields

The flexible `BinaryReader` and `BinaryWriter` classes can also serve as useful tools for managing binary IO streams.
They manage byte order, variable-sized ints, and (in the case of `BinaryWriter`) handle field *reserving and filling*.

When converting a `BinaryStruct` instance to a `BinaryWriter`, fields can also be left as `None` (or explicitly set to 
the unique value `RESERVED`) and filled in later using the `binary_writer.fill()` method. This is useful for fields that
store offsets or otherwise depend on future state. (The `binary_writer.fill_with_position()` method can be used in this
case, too.)

Field positions in the writer are reserved with reference to the `id()` of the `BinaryStruct` instance, so numerous
instances can all reserve the same field name in a single writer without conflict.

If any reserved fields are not filled before the final conversion of the writer to `bytes`, an error will be raised.

Example:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple
from constrata import BinaryStruct, BinaryString, BinaryReader, BinaryWriter, RESERVED
from constrata.field_types import float32

class Vector(NamedTuple):
    name: str
    x: float
    y: float
    z: float

@dataclass(slots=True)
class VectorListHeader(BinaryStruct):
    magic: bytes = field(init=False, **BinaryString(4, asserted=b"VEC\0"))
    vector_count: int
    names_offset: int  # offset to packed null-terminated vector name data
    data_offset: int  # offset to packed (x, y, z) vector float data
    file_size: int  # total file size in bytes

@dataclass(slots=True)
class VectorData(BinaryStruct):
    x: float32
    y: float32
    z: float32
    name_offset: int
    next_vector_offset: int
    
# Unpacking a `Vector` list from a binary file is straightforward.
bin_path = "my_vector_list.vec"
# We create our own `BinaryReader` instance to manage the file, since we will be using multiple `BinaryStruct` classes.
reader = BinaryReader(bin_path)
vector_list_header = VectorListHeader.from_bytes(reader)
# We use `reader.temp_offset()` to step in and out of file regions.
names = []
with reader.temp_offset(vector_list_header.names_offset):
    for _ in range(vector_list_header.vector_count):
        name = reader.unpack_string()  # null-terminated with default UTF-8 encoding
        names.append(name)
vectors = []
with reader.temp_offset(vector_list_header.data_offset):
    for i in range(vector_list_header.vector_count):
        data = VectorData.from_bytes(reader)
        # Attach indexed name from above.
        vector = Vector(names[i], data.x, data.y, data.z)
        vectors.append(vector)
# We don't need to do anything with the header `file_size` or the `name_offset` or `next_vector_offset` fields of each
# `VectorData` struct, since we know the data is tightly packed.    

# Add a new Vector.
vectors.append(Vector("new_vector", 1.0, 2.0, 3.0))

# To pack our `Vector` list, we can use the `BinaryWriter` class and `RESERVED` value.
writer = BinaryWriter()
new_header = VectorListHeader(
    vector_count=len(vectors),
    names_offset=RESERVED,
    data_offset=RESERVED,
    file_size=RESERVED,
)
# We call `to_writer()` rather than `to_bytes()`, which would raise an error due to the reserved fields.
new_header.to_writer(writer)
# Names will be packed first, which means we can fill `names_offset` immediately.
new_header.fill(writer, "names_offset", writer.position)
# We need to record the offset of each vector's name to write to that vector's `name_offset` field. Since that field
# comes AFTER the name data, reserving isn't a good approach here.
vector_name_offsets = []
for vector in vectors:
    vector_name_offsets.append(writer.position)  # record offset before writing
    writer.pack_z_string(vector.name)  # default UTF-8 encoding
# Let's say we know that our spec of the `.vec` file format requires alignment to 16 bytes here, as the name strings
# could end up being any total length.
writer.pad_align(0x10)
# Vector data will be packed next, so we can fill `data_offset` now.
new_header.fill(writer, "data_offset", writer.position)
# We will keep the `VectorData` struct instances we create, as they are each responsible for their own reserved
# `next_vector_offset` field in the writer.
vector_data_structs = []
for i, vector in enumerate(vectors):
    if i > 0:        
        # We need to fill the `next_vector_offset` field of the previous vector.
        vector_data_structs[i - 1].fill(writer, "next_vector_offset", writer.position)
    if i == len(vectors) - 1:
        # This is the last vector, so its `next_vector_offset` field should be 0.
        next_vector_offset = 0
    else:
        # Reserve this vector's `next_vector_offset` field.
        next_vector_offset = RESERVED
    name_offset = vector_name_offsets[i]
    # We index into the name offsets recorded above and reserve the next vector offset.
    vector_data = VectorData(vector.x, vector.y, vector.z, name_offset, next_vector_offset=next_vector_offset)
    vector_data.to_writer(writer)
    vector_data_structs.append(vector_data)

# We can now fill the `file_size` field of the header.
new_header.fill(writer, "file_size", writer.position)

# Finally, we can write the packed data to a new file.
new_bin_path = "my_vector_list_new.vec"
with open(new_bin_path, "wb") as f:
    f.write(bytes(writer))
```

## Custom Metadata Factories

By default, you can only omit `field(**Binary(...))` metadata when the field type hint is a built-in type with a known
size. You can extend this support by adding custom metadata factories to `BinaryStruct.METADATA_FACTORIES`, most easily
done with a subclass.

The example below defines a `Vector` metadata factory for use with the example above, which allows `Vector` fields to be
explicitly used in the `VectorData` struct without needing to read or write the three `float` components manually. (In
this case, since the `name` field of each `Vector` must be changed after object initialization, we use a `dataclass` for
it instead of an immutable `NamedTuple`.)

```python
from dataclasses import dataclass, field
from constrata import BinaryStruct, Binary
from constrata.metadata import BinaryMetadata

@dataclass(slots=True)
class Vector:
    name: str
    x: float
    y: float
    z: float
    

def unpack_vector(values: list[float]) -> Vector:
    """Name will be set later. The field metadata will have already read three floats (see below)."""
    return Vector("", *values)

def pack_vector(value: Vector) -> list[float]:
    """This function must convert the custom type to a list of values that can be packed with `struct.pack()`.
    
    Name is ignored and handled separately by our script.
    
    Note that if `Vector` defined `__iter__`, we could omit this, as the asterisk operator would unpack the values for
    us as we call `struct.pack(*value)`.
    """
    return [value.x, value.y, value.z]

# As binary metadata is powerful, we could support `Vector` as a field type by specifying its format and unpack/pack
# functions, but we would have to do this every time it appeared:

@dataclass(slots=True)
class VectorData(BinaryStruct):
    vector: Vector = field(**Binary("3f", unpack_func=unpack_vector, pack_func=pack_vector))
    name_offset: int
    next_vector_offset: int

# Since `Vector` may appear in many structs, we can define a custom metadata factory for it in a new `BinaryStruct`
# subclass, and use that subclass in place of `BinaryStruct` in our code:    
    
@dataclass(slots=True)
class EnhancedBinaryStruct(BinaryStruct):
    
    METADATA_FACTORIES = {
        "Vector": lambda: BinaryMetadata("3f", unpack_func=unpack_vector, pack_func=pack_vector),
    }

# Now we can use `Vector` fields in our `EnhancedBinaryStruct` subclasses across all of our code, including:

@dataclass(slots=True)
class VectorData(EnhancedBinaryStruct):
    vector: Vector  # replaces `x`, `y`, `z` separate fields; no `field()` call needed here!
    name_offset: int
    next_vector_offset: int

# We could add support for any common custom types to `EnhancedBinaryStruct.METADATA_FACTORIES` to make them available
# across all of our structs. This is especially useful for complex types that are used in many places.
```

You can also natively use other `BinaryStruct` subclasses as field types in your `BinaryStruct` subclasses, as long as
they do not create a circular reference. This can be useful for defining complex binary structures with nested fields.

## More to come...

More documentation and examples to come. For now, please refer to the source code and docstrings.

## License

```
MIT License

Copyright (c) 2017-2024 Scott Mooney (aka Grimrukh)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
