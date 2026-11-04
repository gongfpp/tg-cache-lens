from hashlib import sha256
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import pytest
import xxhash

from tg_cache_lens.desktop_store import databases, read_document, read_index, read_index_snapshot, read_value
from tg_cache_lens.desktop_materialize import materialize
from tg_cache_lens.core import read_map


def write_container(path, key, payload):
    salt = bytes(range(64))
    header = bytes(16)
    payload += bytes((-len(payload)) % 16)
    plaintext = header + sha256(key + salt + header).digest() + payload
    aes_key = sha256(key[:128] + salt[:32]).digest()
    iv = sha256(key[128:] + salt[32:]).digest()[:16]
    encryptor = Cipher(algorithms.AES(aes_key), modes.CTR(iv)).encryptor()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'TDEF' + salt + encryptor.update(plaintext) + encryptor.finalize())


def test_encrypted_synthetic_index_and_value(tmp_path):
    key = bytes(range(256))
    root = tmp_path/'tdata'
    db = root/'user_data'/'media_cache'
    version = db/'3'
    version.mkdir(parents=True)
    (db/'version').write_bytes(struct.pack('<i', 3))
    value = b'synthetic video payload'
    place = bytes.fromhex('123456789ABCDE')
    record = bytearray(32)
    record[0] = 1
    record[2:5] = len(value).to_bytes(3, 'little')
    record[5:12] = place
    struct.pack_into('<IQQ', record, 12, xxhash.xxh32_intdigest(value), 10, 11)
    write_container(version/'binlog', key, bytes(16) + record)
    write_container(version/'21'/'436587A9CBED', key, value)
    assert list(databases(root)) == [db]
    folder, entries = read_index(db, key)
    assert read_value(folder, entries[(10, 11)], key) == value
    snapshot = read_index_snapshot(db, key)
    (version/'binlog').write_bytes(b'changed')
    with pytest.raises(ValueError, match='正在变化'):
        snapshot.check()
    write_container(version/'21'/'436587A9CBED', key, b'X' * len(value))
    with pytest.raises(ValueError, match='校验失败'):
        read_value(folder, entries[(10, 11)], key)


def test_missing_version_is_reported(tmp_path):
    with pytest.raises(ValueError, match='找不到当前'):
        read_index(tmp_path, bytes(256))


def test_missing_version_marker_uses_zero(tmp_path):
    key = bytes(range(256))
    db = tmp_path/'media_cache'
    write_container(db/'0'/'binlog', key, bytes(16))
    directory, entries = read_index(db, key)
    assert directory == db/'0' and entries == {}


def test_reconstruct_synthetic_verified_ranges(tmp_path, monkeypatch):
    key = bytes(range(256))
    db = tmp_path/'tdata'/'user_data'/'media_cache'
    version = db/'0'
    version.mkdir(parents=True)
    (db/'version').write_bytes(struct.pack('<i', 0))
    header = struct.pack('<III', 1, 0, 8) + b'0000ftyp'
    later = struct.pack('<III', 1, 10, 3) + b'abc'
    records = []
    for number, value in enumerate((header, later)):
        place = bytes([number + 1]) + bytes(6)
        record = bytearray(32)
        record[0] = 1; record[2:5] = len(value).to_bytes(3, 'little')
        record[5:12] = place
        struct.pack_into('<IQQ', record, 12, xxhash.xxh32_intdigest(value), 0x10001, 0x10000 + number)
        records.append(record)
        write_container(version/f'{number + 1:01X}0'/'000000000000', key, value)
    write_container(version/'binlog', key, bytes(16) + b''.join(records))
    parts, ranges, ambiguous, unavailable, identity = read_document(db, (0x10001, 0x10000), key)
    assert len(identity) == 32
    assert not ambiguous and not unavailable
    assert ranges == [(0, 8), (10, 13)]
    assert parts[1].data == b'abc'
    from tempfile import TemporaryFile
    from tg_cache_lens.desktop_store import DiskSlice
    with TemporaryFile() as spool:
        stored, stored_ranges, stored_ambiguous, stored_unavailable, stored_identity = read_document(
            db, (0x10001, 0x10000), key, spool=spool)
        assert all(isinstance(part.data, DiskSlice) for part in stored)
        assert [part.data[:] for part in stored] == [part.data for part in parts]
        assert (stored_ranges, stored_ambiguous, stored_unavailable, stored_identity) == (ranges, ambiguous, unavailable, identity)
    monkeypatch.setattr('tg_cache_lens.desktop_materialize.SPOOL_THRESHOLD', 0)
    snapshot = tmp_path/'temporary'/'snapshot'
    source = materialize(db, (0x10001, 0x10000), key, snapshot)
    assert snapshot.read_bytes() == b'0000ftyp\0\0abc'
    assert read_map(snapshot.with_name(snapshot.name + '.meta').read_bytes(), 13) == (None, 11, True, 13)
    with monkeypatch.context() as patch:
        patch.setattr('tg_cache_lens.desktop_store.decode_slice', lambda *args: pytest.fail('revalidation should not parse or spool unchanged slices'))
        source.revalidate()
    write_container(version/'20'/'000000000000', key, b'changed')
    with pytest.raises(RuntimeError, match='已变化'):
        source.revalidate()
    with pytest.raises(ValueError, match='缓存目录之外'):
        materialize(db, (0x10001, 0x10000), key, db/'not-allowed')


def test_damaged_folded_slice_preserves_verified_header(tmp_path):
    key = bytes(range(256))
    db = tmp_path/'tdata'/'user_data'/'media_cache'
    version = db/'0'
    header = struct.pack('<III', 1, 0, 8) + b'0000ftyp'
    value = header + struct.pack('<III', 1, 8, 100) + b'truncated'
    record = bytearray(32)
    record[0] = 1
    record[2:5] = len(value).to_bytes(3, 'little')
    record[5:12] = bytes([1]) + bytes(6)
    struct.pack_into('<IQQ', record, 12, xxhash.xxh32_intdigest(value), 0x10001, 0x10000)
    write_container(version/'10'/'000000000000', key, value)
    write_container(version/'binlog', key, bytes(16) + record)
    parts, ranges, ambiguous, unavailable, _ = read_document(db, (0x10001, 0x10000), key)
    assert [part.data for part in parts] == [b'0000ftyp']
    assert ranges == [(0, 8)]
    assert ambiguous == () and unavailable == (0,)
    target = tmp_path/'temporary'/'snapshot'
    source = materialize(db, (0x10001, 0x10000), key, target)
    assert target.read_bytes() == b'0000ftyp'
    source.revalidate()


def test_conflicting_overlap_is_excluded_without_discarding_other_ranges(tmp_path):
    from tg_cache_lens.desktop_parts import Part
    key = bytes(range(256))
    db = tmp_path/'tdata'/'user_data'/'media_cache'
    version = db/'0'
    header = (struct.pack('<I', 2) + struct.pack('<II', 0, 8) + b'0000ftyp'
              + struct.pack('<II', 12, 8) + b'abcdefgh')
    later = struct.pack('<III', 1, 16, 8) + b'WXYZ1234'
    records = []
    for number, value in enumerate((header, later)):
        record = bytearray(32)
        record[0] = 1
        record[2:5] = len(value).to_bytes(3, 'little')
        record[5:12] = bytes([number + 1]) + bytes(6)
        struct.pack_into('<IQQ', record, 12, xxhash.xxh32_intdigest(value), 0x10001, 0x10000 + number)
        records.append(record)
        write_container(version/f'{number + 1}0'/'000000000000', key, value)
    write_container(version/'binlog', key, bytes(16) + b''.join(records))
    parts, ranges, ambiguous, unavailable, _ = read_document(db, (0x10001, 0x10000), key)
    assert parts == [Part(0, b'0000ftyp'), Part(12, b'abcd'), Part(20, b'1234')]
    assert ranges == [(0, 8), (12, 16), (20, 24)]
    assert ambiguous == () and unavailable == (0, 1)
    target = tmp_path/'temporary'/'snapshot'
    source = materialize(db, (0x10001, 0x10000), key, target)
    assert target.read_bytes() == b'0000ftyp' + bytes(4) + b'abcd' + bytes(4) + b'1234'
    source.revalidate()


def test_matching_header_recovers_unaligned_final_slice_without_assuming_total(tmp_path):
    from tg_cache_lens.desktop_parts import SLICE_SIZE
    key = bytes(range(256))
    db = tmp_path/'tdata'/'user_data'/'media_cache'
    version = db/'0'
    raw = bytes(range(1, 201))
    header = (struct.pack('<I', 2) + struct.pack('<II', 0, 8) + b'0000ftyp'
              + struct.pack('<II', SLICE_SIZE + 40, 96) + raw[40:136])
    records = []
    for number, place_byte, value in ((0, 1, header), (2, 2, raw)):
        record = bytearray(32)
        record[0] = 1
        record[2:5] = len(value).to_bytes(3, 'little')
        record[5:12] = bytes([place_byte]) + bytes(6)
        struct.pack_into('<IQQ', record, 12, xxhash.xxh32_intdigest(value), 0x10001, 0x10000 + number)
        records.append(record)
        write_container(version/f'{place_byte}0'/'000000000000', key, value)
    write_container(version/'binlog', key, bytes(16) + b''.join(records))
    parts, ranges, ambiguous, unavailable, _ = read_document(db, (0x10001, 0x10000), key)
    assert any(part.offset == SLICE_SIZE and part.data == raw for part in parts)
    assert ranges == [(0, 8), (SLICE_SIZE, SLICE_SIZE + len(raw))]
    assert ambiguous == () and unavailable == ()
    target = tmp_path/'temporary'/'snapshot'
    source = materialize(db, (0x10001, 0x10000), key, target)
    total, cached, gap, end = read_map(target.with_name(target.name + '.meta').read_bytes(), target.stat().st_size)
    assert total is None and cached == 8 + len(raw) and gap and end == SLICE_SIZE + len(raw)
    source.revalidate()


@pytest.mark.skipif(__import__('sys').platform != 'win32', reason='Windows byte-range lock')
def test_locked_slice_does_not_hide_verified_header(tmp_path):
    import subprocess
    import sys
    key = bytes(range(256))
    db = tmp_path/'tdata'/'user_data'/'media_cache'
    version = db/'0'
    values = [struct.pack('<III', 1, 0, 8) + b'0000ftyp',
              struct.pack('<III', 1, 16, 4) + b'tail']
    records = []
    for number, value in enumerate(values):
        record = bytearray(32)
        record[0] = 1
        record[2:5] = len(value).to_bytes(3, 'little')
        record[5:12] = bytes([number + 1]) + bytes(6)
        struct.pack_into('<IQQ', record, 12, xxhash.xxh32_intdigest(value), 0x10001, 0x10000 + number)
        records.append(record)
        write_container(version/f'{number + 1}0'/'000000000000', key, value)
    write_container(version/'binlog', key, bytes(16) + b''.join(records))
    locked = version/'20'/'000000000000'
    child = subprocess.Popen([sys.executable, '-c', '''
import msvcrt, sys
with open(sys.argv[1], 'r+b') as stream:
    stream.seek(4)
    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 64)
    print('locked', flush=True)
    sys.stdin.readline()
''', str(locked)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        parts, ranges, ambiguous, unavailable, _ = read_document(db, (0x10001, 0x10000), key)
        assert [part.data for part in parts] == [b'0000ftyp']
        assert ranges == [(0, 8)] and not ambiguous and unavailable == (1,)
        source = materialize(db, (0x10001, 0x10000), key, tmp_path/'temporary'/'snapshot')
        source.revalidate()
    finally:
        child.communicate('\n', timeout=10)
    with pytest.raises(RuntimeError, match='已变化'):
        source.revalidate()
