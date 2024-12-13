# constrata

An efficient Python library for parsing and building binary data structures based on the built-in `dataclasses` module,
with support for reserving/filling fields, pure field type support (without `= field(...)`), asserted values, and more.

Pure Python, no dependencies, MIT license. **Requires Python 3.11 or later.**

## Installation

```shell
pip install constrata
```

## Usage

### NOTE: If you have updated from v1.0, note that `dataclass` wrappers are now automatically applied to `BinaryStruct`
### subclasses. You must not add an additional `dataclass` decorator yourself.

Define a subclass of `constrata.BinaryStruct`, and specify the binary format, size, asserted values, and 
unpacking/packing functions of fields using `constrata` field functions with these basic arguments:
- `binary(fmt, asserted)`
- `binary_string(fmt_or_byte_size, asserted, encoding)`
- `binary_array(length, element_format, asserted)`
- `binary_pad(length, char = b'\0')`

Each of these field functions also have other advanced arguments, such as custom unpacking/packing callbacks
(`unpack_func` and `pack_func`), conditional field skipping based on the values of previous fields (`should_skip_func`),
and `rstrip_null` boolean for `binary_string()`. 

All of these functions also accept standard keyword arguments for `dataclasses.field()` and pass them through directly.
Additionally, if only a single value is `asserted` (which is always the case for `binary_pad()`), then `init=False` and
`default=asserted` will be passed through to `field()` by default. However, your IDE (e.g. PyCharm) may not be able to
detect these arguments; if this interferes with constructor usage, you can just specify these keywords explicitly.

Alternatively, you can use `dataclasses.field()` directly and pass in double-asterisk metadata generators `**Binary()`,
`**BinaryString()`, `**BinaryArray()`, and `**BinaryPad()`. This has the exact same effect as using the `constrata`
field functions above. (These capitalised functions return a dictionary with a single key, 'metadata', which a double
asterisk will unpack as a keyword into the `field()` call, which looks slightly nicer and more compact than having to
pass `metadata=Binary(...)` to all the fields.) However, since Python 3.11 introduced `typing.dataclass_transform`
(which the base `BinaryStruct` uses to reveal its automatic `dataclass` wrapper to IDEs), your IDE should be able to
see that the standard functions like `binary()` function as `field()` wrappers.

Rather than using `unpack_func` and `pack_func` in every field, you can also create an instance of `BinaryMetadata` or
`BinaryArrayMetadata` to support your custom classes (e.g. a `Vector3` class) and add them to the `METADATA_FACTORIES`
attribute of your `BinaryStruct` subclass (or an intermediate class that all of your `Vector3`-using structs can
inherit from). This allows direct use of your custom class as a type hint in a `BinaryStruct` with no field call needed
at all (unless you want to set a default, assert values, etc.). See the examples below.

`BinaryStruct` subclasses **have `dataclass(slots=True)` wrapping built in to their metaclass.** You must not add an
additional `dataclass` decorator yourself, with or without `slots`, as this will cause the binary field metadata to be
lost.

## Basic Example

```python
from constrata.binary_struct import BinaryStruct
from constrata.fields import *
from constrata.field_types import *


class MyStruct(BinaryStruct):
    my_int32: int
    my_uint64: uint64
    my_single: float32
    my_double: float64 = binary(asserted=(1.0, 2.0, 3.0))  # only three permitted values
    _padding: bytes = binary_pad(8)
    my_ascii_string: str = binary_string(12, encoding="ascii")
    my_eight_bools: list[bool] = binary_array(8, default_factory=lambda: [False] * 8)
    my_bitflag1: bool = binary(bit_count=1, default=False)
    my_bitflag2: bool = binary(bit_count=1, default=True)
    # Six unused bits in byte skipped here (and must all be 0).

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
from typing import NamedTuple
from constrata import BinaryStruct, binary_string, BinaryReader, BinaryWriter, RESERVED
from constrata.field_types import float32


class Vector(NamedTuple):
    name: str
    x: float
    y: float
    z: float

    
class VectorListHeader(BinaryStruct):
    magic: bytes = binary_string(4, asserted=b"VEC\0")
    vector_count: int
    names_offset: int  # offset to packed null-terminated vector name data
    data_offset: int  # offset to packed (x, y, z) vector float data
    file_size: int  # total file size in bytes


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
    # `magic` has `init=False` and `default=b"VEC\0"` set automatically.
    # If your IDE complains about this and wants to see the `magic` keyword, you can add those arguments explicitly.
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

By default, you can only omit binary metadata when the field type hint is a built-in type with a known
size. You can extend this support by adding custom metadata factories to `BinaryStruct.METADATA_FACTORIES`, most easily
done with a subclass.

The example below defines a `Vector` metadata factory for use with the example above, which allows `Vector` fields to be
explicitly used in the `VectorData` struct without needing to read or write the three `float` components manually. (In
this case, since the `name` field of each `Vector` must be changed after object initialization, we use a `dataclass` for
it instead of an immutable `NamedTuple`.)

```python
from dataclasses import dataclass
from constrata import BinaryStruct, binary
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

class VectorData(BinaryStruct):
    vector: Vector = binary("3f", unpack_func=unpack_vector, pack_func=pack_vector)
    name_offset: int
    next_vector_offset: int

# Since `Vector` may appear in many structs, we can define a custom metadata factory for it in a new `BinaryStruct`
# subclass, and use that subclass in place of `BinaryStruct` in our code:    
    
class EnhancedBinaryStruct(BinaryStruct):
    
    METADATA_FACTORIES = {
        "Vector": lambda: BinaryMetadata("3f", unpack_func=unpack_vector, pack_func=pack_vector),
    }

# Now we can use `Vector` fields in our `EnhancedBinaryStruct` subclasses across all of our code, including:

class VectorData_Enhanced(EnhancedBinaryStruct):
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
