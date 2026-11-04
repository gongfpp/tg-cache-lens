"""Synthetic Desktop containers, never a user's Telegram data."""
from hashlib import md5, pbkdf2_hmac, sha1, sha256, sha512
import struct
import subprocess
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import pytest

from tg_cache_lens.desktop_crypto import key_file, legacy_key_directory, load_modern_key, read_tdef


def encrypt_blob(passkey, local_key):
    plain = struct.pack('<I', len(local_key) + 4) + local_key
    plain += bytes((-len(plain)) % 16)
    msg = sha1(plain).digest()[:16]
    a = sha1(msg + passkey[8:40]).digest()
    b = sha1(passkey[40:56] + msg + passkey[56:72]).digest()
    c = sha1(passkey[72:104] + msg).digest()
    d = sha1(msg + passkey[104:136]).digest()
    key = a[:8] + b[8:20] + c[4:16]
    prev_cipher, prev_plain = a[8:20] + b[:4], b[4:8] + c[16:20] + d[:8]
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    cipher = bytearray()
    for pos in range(0, len(plain), 16):
        block = plain[pos:pos+16]
        intermediate = bytes(x ^ y for x, y in zip(block, prev_cipher))
        encrypted = bytes(x ^ y for x, y in zip(enc.update(intermediate), prev_plain))
        cipher.extend(encrypted)
        prev_cipher, prev_plain = encrypted, block
    return msg + cipher


def make_key_file(path, password, local_key):
    salt = bytes(range(32))
    encoded = password.encode()
    seed = sha512(salt + encoded + salt).digest()
    passkey = pbkdf2_hmac('sha512', seed, salt, 100000 if encoded else 1, 256)
    blob = encrypt_blob(passkey, local_key)
    arrays = (salt, blob, b'info')
    payload = b''.join(struct.pack('>I', len(value)) + value for value in arrays)
    version = struct.pack('<I', 1)
    path.write_bytes(b'TDF$' + version + payload + md5(payload + struct.pack('<I', len(payload)) + version + b'TDF$').digest())


def make_new_key_file(path, password, local_key, kind, *, open_wrap=False, generation=7, production=False):
    from hashlib import scrypt
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
    salt = bytes(range(32))
    if kind == 1:
        memory, iterations, parallel = (65536, 8, 1) if production else (32, 2, 1)
        passkey = Argon2id(salt=salt, length=256, iterations=iterations,
                           lanes=parallel, memory_cost=memory).derive(password.encode())
    else:
        memory, iterations, parallel = (131072, 8, 1) if production else (16, 1, 1)
        passkey = scrypt(password.encode(), salt=salt, n=memory, r=iterations,
                         p=parallel, maxmem=192*1024*1024, dklen=256)
    blob = encrypt_blob(passkey, local_key)
    pack_array = lambda data: struct.pack('>I', len(data)) + data
    trailer = (struct.pack('>IIII', 0x4b443200, 2, generation, 1)
               + struct.pack('>IIIII', kind, generation, memory, iterations, parallel)
               + pack_array(salt) + pack_array(blob))
    seed = sha512(salt + salt).digest()
    emptykey = pbkdf2_hmac('sha512', seed, salt, 1, 256)
    open_blob = encrypt_blob(emptykey, local_key) if open_wrap else b''
    payload = b''.join(pack_array(value) for value in (salt, open_blob, b'info', trailer))
    version = struct.pack('<I', 1)
    path.write_bytes(b'TDF$' + version + payload + md5(payload + struct.pack('<I', len(payload)) + version + b'TDF$').digest())


def make_legacy_map(path, password, local_key):
    salt = bytes(range(32))
    password = password.encode('utf-8')
    passkey = pbkdf2_hmac('sha1', password, salt, 4000 if password else 4, 256)
    blob = encrypt_blob(passkey, local_key)
    payload = b''.join(struct.pack('>I', len(value)) + value for value in (salt, blob, b'unread-map'))
    version = struct.pack('<I', 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'TDF$' + version + payload + md5(payload + struct.pack('<I', len(payload)) + version + b'TDF$').digest())


@pytest.mark.parametrize('password', ['', '测试pass'])
def test_modern_local_key(tmp_path, password):
    key = bytes(range(256))
    path = tmp_path/'key_datas'
    make_key_file(path, password, key)
    assert load_modern_key(path, password) == key
    with pytest.raises(ValueError):
        load_modern_key(path, 'wrong')
    damaged = bytearray(path.read_bytes()); damaged[-1] ^= 1; path.write_bytes(damaged)
    with pytest.raises(ValueError, match='校验'):
        load_modern_key(path, password)


@pytest.mark.parametrize('kind', [1, 2])
@pytest.mark.parametrize('open_wrap', [False, True])
def test_new_passcode_key_file(tmp_path, kind, open_wrap):
    path = tmp_path/'key_datas'
    key = bytes(range(256))
    make_new_key_file(path, '测试密码', key, kind, open_wrap=open_wrap)
    assert load_modern_key(path, '测试密码') == key
    if open_wrap:
        assert load_modern_key(path) == key
    else:
        with pytest.raises(ValueError, match='本地密码'):
            load_modern_key(path)
    with pytest.raises(ValueError, match='本地密码'):
        load_modern_key(path, '错误')


def test_new_key_rejects_uncommitted_wrap_and_excessive_cost(tmp_path):
    path = tmp_path/'key_datas'
    key = bytes(range(256))
    make_new_key_file(path, 'password', key, 1)
    raw = path.read_bytes()
    payload = bytearray(raw[8:-16])
    trailer_start = payload.index(b'KD2\0')
    def write_payload():
        version = raw[4:8]
        body = bytes(payload)
        path.write_bytes(b'TDF$' + version + body + md5(body + struct.pack('<I', len(body)) + version + b'TDF$').digest())
    struct.pack_into('>I', payload, trailer_start + 8, 8)  # committed generation
    write_payload()
    with pytest.raises(ValueError, match='本地密码'):
        load_modern_key(path, 'password')
    struct.pack_into('>I', payload, trailer_start + 8, 7)
    struct.pack_into('>I', payload, trailer_start + 24, 262145)  # Argon2 memory
    write_payload()
    with pytest.raises(ValueError, match='Argon2id'):
        load_modern_key(path, 'password')


def test_new_key_accepts_null_open_wrap(tmp_path):
    path = tmp_path/'key_datas'
    key = bytes(range(256))
    make_new_key_file(path, 'password', key, 1)
    raw = path.read_bytes()
    payload = bytearray(raw[8:-16])
    struct.pack_into('>I', payload, 36, 0xffffffff)
    body = bytes(payload); version = raw[4:8]
    path.write_bytes(b'TDF$' + version + body + md5(body + struct.pack('<I', len(body)) + version + b'TDF$').digest())
    assert load_modern_key(path, 'password') == key


@pytest.mark.parametrize('kind', [1, 2])
def test_new_key_default_kdf_costs(tmp_path, kind):
    path = tmp_path/'key_datas'
    key = bytes(range(256))
    make_new_key_file(path, 'synthetic', key, kind, production=True)
    assert load_modern_key(path, 'synthetic') == key


def test_legacy_backup_key_selection_is_read_only(tmp_path):
    import os
    key = bytes(range(256))
    first, second = tmp_path/'key_data0', tmp_path/'key_data1'
    make_key_file(first, '', key)
    second.write_bytes(b'corrupt')
    os.utime(first, ns=(1_000_000_000, 1_000_000_000))
    os.utime(second, ns=(2_000_000_000, 2_000_000_000))
    assert key_file(tmp_path) == first
    assert load_modern_key(key_file(tmp_path)) == key
    assert first.exists() and second.exists()
    safe = tmp_path/'key_datas'; safe.write_bytes(b'corrupt')
    assert key_file(tmp_path) == safe
    with pytest.raises(ValueError):
        load_modern_key(key_file(tmp_path))


@pytest.mark.parametrize('password', ['', '测试密码'])
def test_pre_multiauth_legacy_key_is_read_only(tmp_path, password):
    key = bytes(range(256))
    directory = legacy_key_directory(tmp_path, 'data')
    assert directory.name == 'D877F783D5D3EF8C'
    path = directory/'maps'
    make_legacy_map(path, password, key)
    original = path.read_bytes()
    assert key_file(tmp_path) == path
    assert load_modern_key(path, password) == key
    with pytest.raises(ValueError, match='本地密码'):
        load_modern_key(path, 'wrong')
    assert path.read_bytes() == original


def test_tdef_round_trip_and_wrong_key(tmp_path):
    key = bytes(range(256)); salt = bytes(range(64))
    header = bytes(range(16)); payload = b'synthetic payload' + bytes(15)
    plain = header + sha256(key + salt + header).digest() + payload
    aes_key = sha256(key[:128] + salt[:32]).digest()
    iv = sha256(key[128:] + salt[32:]).digest()[:16]
    encryptor = Cipher(algorithms.AES(aes_key), modes.CTR(iv)).encryptor()
    path = tmp_path/'value'; path.write_bytes(b'TDEF' + salt + encryptor.update(plain) + encryptor.finalize())
    assert read_tdef(path, key) == payload
    with pytest.raises(ValueError, match='校验'):
        read_tdef(path, bytes(256))
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ValueError, match='不完整'):
        read_tdef(path, key)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows byte-range lock')
def test_tdef_reads_while_first_four_bytes_are_locked(tmp_path):
    from test_desktop_store import write_container
    path = tmp_path/'locked'; key = bytes(range(256))
    write_container(path, key, b'synthetic-data')
    child = subprocess.Popen([sys.executable, '-c', '''
import msvcrt, sys
with open(sys.argv[1], 'r+b') as stream:
    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 4)
    print('locked', flush=True)
    sys.stdin.readline()
''', str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        assert read_tdef(path, key).startswith(b'synthetic-data')
        with pytest.raises(ValueError, match='校验'):
            read_tdef(path, bytes(256))
    finally:
        child.communicate('\n', timeout=10)
