"""Independent regression check based on the publicly documented PR #250."""

from filesize import naturalsize


def test_one_byte_string_has_no_decimal_suffix(record_property):
    actual = naturalsize("1")
    expected = "1 Byte"
    record_property("输入", '字符串 "1"（1 字节）')
    record_property("实际输出", actual)
    record_property("预期输出", expected)
    assert actual == expected, "one byte must be formatted as 1 Byte"
