import struct

from constrata import *


class DataStruct(BinaryStruct):
    int_field: int
    short_field: int16
    str_field: str = binary_string(8, encoding="ascii")
    array_field: list[uint8] = binary_array(4)
    # Keywords `init=False` and `default=17`/`default=b'\0' * 16` are automatic for these two fields as a single
    # value is asserted. However, your IDE may not recognize this unless you pass these arguments explicitly.
    _always_17: int64 = binary(asserted=17)
    _pad: bytes = binary_pad(16)
    var_int: varint


def example():

    packed_data = struct.pack("<ih8s4Bq16sQ", 1000, 25, b"test\0\0\0\0", 1, 2, 3, 4, 17, b'\0' * 16, 100000)

    data = DataStruct.from_bytes(packed_data, long_varints=True)
    print(data)

    repacked_data = data.to_bytes(long_varints=True)
    print(repacked_data)
    print(repacked_data == packed_data)

    new_data = DataStruct(int_field=33, short_field=97, str_field="hello", array_field=[5, 6, 7, 8], var_int=255)
    print(new_data)
    print(new_data.repr_multiline())  # ignores asserted fields


if __name__ == '__main__':
    example()
