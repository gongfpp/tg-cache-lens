import struct

import pytest

from tg_cache_lens.desktop_parts import Part, SLICE_SIZE, available_ranges, decode_slice, group_documents, matching_final_slice, parse_map


def packed(*parts):
    return struct.pack('<I', len(parts)) + b''.join(
        struct.pack('<II', offset, len(data)) + data for offset, data in parts)


def test_map_with_following_data():
    data = packed((10, b'abc'), (0, b'012'))
    parts, consumed = parse_map(data + b'next map', 100)
    assert parts == [Part(0, b'012'), Part(10, b'abc')]
    assert consumed == len(data)
    assert available_ranges(parts) == [(0, 3), (10, 13)]


@pytest.mark.parametrize('data,limit', [
    (b'', 10), (struct.pack('<I', 0), 10),
    (struct.pack('<I', 81), 10), (struct.pack('<II', 1, 0), 10),
    (struct.pack('<III', 1, 9, 2) + b'ab', 10),
    (packed((0, b'ab'), (1, b'cd')), 10),
    (packed((0, b'a'), (0, b'b')), 10),
])
def test_invalid_map_rejected(data, limit):
    with pytest.raises(ValueError):
        parse_map(data, limit)


def test_ranges_are_cached_coverage_not_total_length():
    assert available_ranges([Part(3, b'ab'), Part(0, b'123')]) == [(0, 5)]


def test_document_keys_with_shared_high_word_are_not_merged():
    entries = {(0x10001, 0x10000): 'header-a', (0x10001, 0x10002): 'slice-a',
               (0x10001, 0x20001): 'slice-b', (0x10001, 0x20200): 'invalid',
               (0x20001, 0x10000): 'photo'}
    assert group_documents(entries) == {
        (0x10001, 0x10000): {0: 'header-a', 2: 'slice-a'},
        (0x10001, 0x20000): {1: 'slice-b'},
    }


def test_decode_header_with_folded_slice_and_padding():
    header = packed((0, b'abc'), (12, b'xyz'))
    included = packed((3, b'def'))
    assert decode_slice(header + included + b'\0', 0) == [
        Part(0, b'abc'), Part(12, b'xyz'), Part(3, b'def')]
    assert decode_slice(packed((3, b'def')) + b'\0', 2) == [Part(8388611, b'def')]


def test_unaligned_unrecognized_data_is_not_treated_as_media():
    with pytest.raises(ValueError):
        decode_slice(b'unrecognized data', 2)


def test_oversized_contiguous_slice_is_rejected():
    from tg_cache_lens.desktop_parts import SLICE_SIZE
    with pytest.raises(ValueError, match='大小限制'):
        decode_slice(bytes(SLICE_SIZE + 128 * 1024), 2)


def test_unaligned_final_slice_needs_matching_independent_overlap():
    raw = bytes(range(200))
    assert matching_final_slice(raw, 2, []) is None
    assert matching_final_slice(raw, 2, [Part(SLICE_SIZE + 20, raw[20:70])]) is None
    assert matching_final_slice(bytes(200), 2, [Part(SLICE_SIZE + 20, bytes(100))]) is None
    assert matching_final_slice(raw, 2, [Part(SLICE_SIZE + 20, b'x' * 100)]) is None
    assert matching_final_slice(raw, 2, [Part(SLICE_SIZE + 20, raw[20:120])]) == [Part(SLICE_SIZE, raw)]
