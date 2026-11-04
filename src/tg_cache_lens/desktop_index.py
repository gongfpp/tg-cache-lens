"""Telegram Desktop cache binlog records (official x86-64 layout)."""
from dataclasses import dataclass
import struct


@dataclass(frozen=True)
class Entry:
    key: tuple[int, int]
    tag: int
    size: int
    place: bytes
    checksum: int
    store_time: int | None = None

    def relative_path(self):
        # Telegram formats the low nibble of each place byte first.
        digits = ''.join(f'{byte & 15:X}{byte >> 4:X}' for byte in self.place)
        return digits[:2] + '/' + digits[2:]


def parse_binlog(payload):
    if len(payload) < 16 or payload[0] != 0:
        raise ValueError('不支持的 Desktop 缓存索引格式')
    flags = int.from_bytes(payload[1:4], 'little')
    if flags & ~1:
        raise ValueError('不支持的 Desktop 缓存索引标志')
    store_size = 48 if flags & 1 else 32
    entries = {}
    offset = 16

    def store(record):
        size = int.from_bytes(record[2:5], 'little')
        if not size:
            raise ValueError('缓存索引条目大小错误')
        key = struct.unpack_from('<QQ', record, 16)
        if key == (0, 0):
            raise ValueError('缓存索引条目标识错误')
        timestamp = struct.unpack_from('<I', record, 40)[0] if store_size == 48 else 0
        entries[key] = Entry(key, record[1], size, record[5:12],
                             struct.unpack_from('<I', record, 12)[0], timestamp or None)

    while offset < len(payload):
        kind = payload[offset]
        if kind == 0 and not any(payload[offset:]):
            break
        if kind == 1:
            if offset + store_size > len(payload):
                raise ValueError('缓存索引记录不完整')
            store(payload[offset:offset + store_size])
            offset += store_size
            continue
        if kind not in (2, 3, 4) or offset + 16 > len(payload):
            raise ValueError('缓存索引记录无效或不完整')
        count = int.from_bytes(payload[offset + 1:offset + 4], 'little')
        item_size = store_size if kind == 2 else 16
        end = offset + 16 + count * item_size
        if not count or end > len(payload):
            raise ValueError('缓存索引批量记录不完整')
        for pos in range(offset + 16, end, item_size):
            if kind == 2:
                store(payload[pos:pos + item_size])
            elif kind == 3:
                entries.pop(struct.unpack_from('<QQ', payload, pos), None)
        offset = end
    return entries
