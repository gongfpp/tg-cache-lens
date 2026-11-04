import struct
import pytest
from tg_cache_lens.desktop_index import parse_binlog


def store(high, low, size, place, checksum, timed=False):
    record = bytearray(48 if timed else 32)
    record[0:2] = b'\x01\x07'
    record[2:5] = size.to_bytes(3, 'little')
    record[5:12] = place
    struct.pack_into('<IQQ', record, 12, checksum, high, low)
    return record


@pytest.mark.parametrize('timed', [False, True])
def test_store_update_batch_remove_and_access(timed):
    header = bytes([0, int(timed)]) + bytes(14)
    first = store(3, 4, 123, bytes.fromhex('123456789ABCDE'), 0x12345678, timed)
    second = store(3, 5, 222, bytes.fromhex('01122334455667'), 10, timed)
    batch = b'\x02\x02\0\0' + bytes(12) + first + second
    access = b'\x04\x01\0\0' + bytes(12) + struct.pack('<QQ', 3, 4)
    removal = b'\x03\x01\0\0' + bytes(12) + struct.pack('<QQ', 3, 5)
    result = parse_binlog(header + batch + access + removal)
    assert list(result) == [(3, 4)]
    assert result[(3, 4)].size == 123
    assert result[(3, 4)].checksum == 0x12345678
    assert result[(3, 4)].relative_path() == '21/436587A9CBED'


def test_rejects_truncated_batch_and_invalid_header():
    with pytest.raises(ValueError):
        parse_binlog(bytes(16) + b'\x02\x01' + bytes(14))
    with pytest.raises(ValueError):
        parse_binlog(b'\x01' + bytes(15))


def test_windows_native_store_layout_vector():
    # Generated independently by a Windows C++ Store with the official
    # uint8[3], uint8[7], uint32 and uint64 fields and native alignment.
    record = bytes.fromhex('0107563412123456789abcde7856341203000000000000000400000000000000')
    entry = parse_binlog(bytes(16) + record)[(3, 4)]
    assert entry.size == 0x123456 and entry.tag == 7
    assert entry.checksum == 0x12345678 and entry.relative_path() == '21/436587A9CBED'


def test_timed_store_exposes_recorded_system_time():
    record = store(3, 4, 123, bytes(7), 7, True)
    struct.pack_into('<III', record, 32, 1, 0, 1700000000)
    assert parse_binlog(bytes([0, 1]) + bytes(14) + record)[(3, 4)].store_time == 1700000000
