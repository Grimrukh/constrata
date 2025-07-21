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
    bit_flag_1: byte = binary(bit_count=1)
    bit_flag_2: byte = binary(bit_count=1)
    # As the next bit field uses `uint16` rather than `byte`, six pad bits (-> byte) will be added here.
    other_bit_option: uint16 = binary(bit_count=3)
    # 13 pad bits (-> uint16) will be added here.
    var_int: varint


def example():

    packed_data = struct.pack(
        "<ih8s4Bq16s1B1Hq",
        1000, 25, b"test\0\0\0\0", 1, 2, 3, 4, 17, b'\0' * 16, 0 | (1 << 1), 5, 100000
    )

    data = DataStruct.from_bytes(packed_data, long_varints=True)
    print(data)

    repacked_data = data.to_bytes(long_varints=True)
    print(repacked_data)
    print("Packed == repacked?", repacked_data == packed_data)

    new_data = DataStruct(
        int_field=33,
        short_field=97,
        str_field="hello",
        array_field=[5, 6, 7, 8],
        bit_flag_1=1,
        bit_flag_2=0,
        other_bit_option=2,
        var_int=255,
    )  # your IDE may complain about missing '_always_17' and '_pad' arguments, but these are asserted fields
    print(new_data)
    print(new_data.repr_multiline())  # ignores asserted fields


if __name__ == '__main__':
    example()
