"""Telegram Desktop streaming cache part maps.

The cache does not itself establish the original media length.  These helpers
only decode a map whose slice and bounds are already known; they never infer a
complete file from the last cached byte.
"""
from dataclasses import dataclass
import struct

PART_SIZE = 128 * 1024
SLICE_SIZE = 64 * PART_SIZE
MAX_PARTS = 80
MAX_SLICES = 0x1FE


class AmbiguousSlice(ValueError):
    """An unaligned slice cannot be distinguished from a full final slice."""


@dataclass(frozen=True)
class Part:
    offset: int
    data: bytes


def parse_map(data: bytes, max_size: int):
    """Return (parts, consumed bytes) for one complex serialized map.

    A header can contain a second map, and Telegram may append padding; callers
    must decide how to interpret the remaining bytes.
    """
    if max_size <= 0 or len(data) < 4:
        raise ValueError('缓存分片索引不完整')
    count, = struct.unpack_from('<I', data)
    if not 1 <= count <= MAX_PARTS:
        raise ValueError('缓存分片数量错误')
    offset = 4
    parts = []
    seen = set()
    for _ in range(count):
        if offset + 8 > len(data):
            raise ValueError('缓存分片索引不完整')
        start, length = struct.unpack_from('<II', data, offset)
        offset += 8
        if not length or start + length > max_size or offset + length > len(data):
            raise ValueError('缓存分片范围错误')
        if start in seen:
            raise ValueError('缓存分片索引重复')
        seen.add(start)
        parts.append(Part(start, data[offset:offset + length]))
        offset += length
    parts.sort(key=lambda part: part.offset)
    if any(left.offset + len(left.data) > right.offset for left, right in zip(parts, parts[1:])):
        raise ValueError('缓存分片范围重叠')
    return parts, offset


def available_ranges(parts):
    """Return merged half-open ranges; never treat their end as original size."""
    result = []
    for part in sorted(parts, key=lambda item: item.offset):
        end = part.offset + len(part.data)
        if result and part.offset <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((part.offset, end))
    return result


def decode_header_prefix(data):
    """Recover a valid header map when the appended first-slice map is damaged."""
    parts, consumed = parse_map(data, 0xffffffff)
    if consumed == len(data):
        raise ValueError('缓存头没有附加分片')
    return parts


def matching_final_slice(data, number, evidence):
    """Recognize an unaligned raw slice using independently cached bytes.

    Without the original length an unaligned entry could instead be a complex
    map. Only use this fallback after parsing failed, and require a substantial,
    non-uniform matching overlap with already decoded ranges.
    """
    if not number or not data or len(data) > SLICE_SIZE or len(data) % PART_SIZE == 0:
        return None
    base = (number - 1) * SLICE_SIZE
    end = base + len(data)
    overlaps, sample = [], bytearray()
    for part in evidence:
        start = max(base, part.offset)
        stop = min(end, part.offset + len(part.data))
        if start >= stop:
            continue
        expected = part.data[start - part.offset:stop - part.offset]
        actual = data[start - base:stop - base]
        if expected != actual:
            return None
        overlaps.append((start, stop))
        sample.extend(actual[:64])
    merged = []
    for start, stop in sorted(overlaps):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((start, stop))
    if sum(stop - start for start, stop in merged) < 64 or len(set(sample)) < 8:
        return None
    return [Part(base, data)]


def group_documents(entries):
    """Group document cache keys by their actual base, including missing headers.

    Desktop shifts document IDs by 16 bits, reserving the low bits for the
    slice number. Grouping by the high word alone would merge unrelated videos.
    """
    groups = {}
    for (high, low), entry in entries.items():
        if high >> 16 != 1:
            continue
        slice_number = low & 0xffff
        if slice_number > MAX_SLICES:
            continue
        base = (high, low - slice_number)
        groups.setdefault(base, {})[slice_number] = entry
    return groups


def decode_slice(data, number):
    """Decode unambiguous cached ranges without assuming the original size.

    Desktop also serializes a final, unaligned slice as raw bytes when its
    size equals the remaining file size. Without that external size this is
    indistinguishable from malformed complex data, so do not guess.
    """
    if not 0 <= number <= MAX_SLICES or not data:
        raise ValueError('缓存分片编号或内容无效')
    base = max(0, number - 1) * SLICE_SIZE
    signature = ((len(data) >= 8 and data[4:8] == b'ftyp')
                 or data.startswith(b'\x1aE\xdf\xa3')
                 or (data.startswith(b'RIFF') and data[8:12] == b'AVI '))
    if len(data) % PART_SIZE == 0 or (number == 0 and signature):
        if number and len(data) > SLICE_SIZE:
            raise ValueError('缓存分片超出大小限制')
        return [Part(base, data)]
    if number and (len(data) < 4 or not 1 <= struct.unpack_from('<I', data)[0] <= MAX_PARTS):
        raise AmbiguousSlice('末尾分片格式不能确认，已忽略该分片')
    maximum = 0xffffffff if number == 0 else SLICE_SIZE
    parts, consumed = parse_map(data, maximum)
    result = [Part(base + part.offset, part.data) for part in parts]
    rest = data[consumed:]
    if number == 0 and len(rest) >= 4 and any(rest):
        # A good header may be followed by a second, folded-in first slice.
        if len(rest) % PART_SIZE == 0:
            result.append(Part(0, rest))
        else:
            included, consumed2 = parse_map(rest, SLICE_SIZE)
            if any(rest[consumed2:]):
                raise ValueError('缓存分片存在无法识别的尾部数据')
            result.extend(included)
    elif any(rest):
        raise ValueError('缓存分片存在无法识别的尾部数据')
    return result
