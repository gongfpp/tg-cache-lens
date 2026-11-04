"""Read Telegram Desktop local-key and encrypted cache containers.

Only caller-supplied files are read; no paths are discovered here. Format
details follow Telegram Desktop's storage file utilities and lib_storage.
"""
from hashlib import md5, pbkdf2_hmac, scrypt, sha1, sha256, sha512
import hmac
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def _ige_decrypt(ciphertext, key, iv):
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    previous_cipher, previous_plain = iv[:16], iv[16:]
    out = bytearray()
    for i in range(0, len(ciphertext), 16):
        block = ciphertext[i:i + 16]
        plain = _xor(decryptor.update(_xor(block, previous_plain)), previous_cipher)
        out.extend(plain)
        previous_cipher, previous_plain = block, plain
    decryptor.finalize()
    return bytes(out)


def _decrypt_blob(blob, key):
    if len(blob) <= 16 or len(blob) % 16 or len(key) < 136:
        raise ValueError('本地密钥数据格式错误')
    msg = blob[:16]
    a = sha1(msg + key[8:40]).digest()
    b = sha1(key[40:56] + msg + key[56:72]).digest()
    c = sha1(key[72:104] + msg).digest()
    d = sha1(msg + key[104:136]).digest()
    aes_key = a[:8] + b[8:20] + c[4:16]
    iv = a[8:20] + b[:8] + c[16:20] + d[:8]
    plain = _ige_decrypt(blob[16:], aes_key, iv)
    if not hmac.compare_digest(sha1(plain).digest()[:16], msg):
        raise ValueError('本地密码错误或密钥数据损坏')
    length, = struct.unpack_from('<I', plain)
    if length < 4 or length > len(plain) or len(plain) - length >= 16:
        raise ValueError('本地密钥长度错误')
    return plain[4:length]


def _byte_array(data, offset):
    if offset + 4 > len(data):
        raise ValueError('本地密钥文件不完整')
    length, = struct.unpack_from('>I', data, offset)
    offset += 4
    if length == 0xffffffff:  # QDataStream null QByteArray
        return b'', offset
    if length > len(data) - offset:
        raise ValueError('本地密钥文件长度错误')
    return data[offset:offset + length], offset + length


def _new_wrap(trailer, password, validate_only=False):
    if len(trailer) < 16:
        raise ValueError('本地密钥封装不完整')
    magic, version, committed, count = struct.unpack_from('>IIII', trailer)
    if magic != 0x4b443200 or version > 2 or count > 2:
        raise ValueError('不支持的本地密钥封装格式')
    offset = 16
    wraps = []
    generations = set()
    for _ in range(count):
        if offset + 20 > len(trailer):
            raise ValueError('本地密钥封装不完整')
        kind, generation, memory, iterations, parallel = struct.unpack_from('>IIIII', trailer, offset)
        offset += 20
        salt, offset = _byte_array(trailer, offset)
        encrypted, offset = _byte_array(trailer, offset)
        if generation in generations or len(salt) < 8 or not iterations or not parallel:
            raise ValueError('本地密钥封装参数错误')
        generations.add(generation)
        if kind == 1:
            if not 8 * parallel <= memory <= 262144 or iterations > 64 or parallel > 16:
                raise ValueError('不支持的 Argon2id 参数')
        elif kind == 2:
            if memory < 2 or memory & (memory - 1):
                raise ValueError('不支持的 scrypt 参数')
        else:
            raise ValueError('不支持的本地密钥派生方式')
        wraps.append((generation, kind, memory, iterations, parallel, salt, encrypted))
    if offset != len(trailer):
        raise ValueError('本地密钥封装包含多余数据')
    if validate_only:
        return None
    live = [wrap for wrap in wraps if wrap[0] == committed]
    if len(live) != 1 or not password:
        raise ValueError('本地密码错误或密钥数据损坏')
    _, kind, memory, iterations, parallel, salt, encrypted = live[0]
    if kind == 1:
        from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
        passkey = Argon2id(salt=salt, length=256, iterations=iterations,
                           lanes=parallel, memory_cost=memory).derive(password)
    else:
        try:
            passkey = scrypt(password, salt=salt, n=memory, r=iterations,
                             p=parallel, maxmem=192 * 1024 * 1024, dklen=256)
        except ValueError as exc:
            raise ValueError('不支持的 scrypt 参数') from exc
    return encrypted, passkey


def _tdf_payload(path):
    raw = path.read_bytes()
    if len(raw) < 24 or raw[:4] != b'TDF$':
        raise ValueError('无法识别 Telegram Desktop 密钥文件')
    payload, checksum = raw[8:-16], raw[-16:]
    expected = md5(payload + struct.pack('<I', len(payload)) + raw[4:8] + b'TDF$').digest()
    if not hmac.compare_digest(checksum, expected):
        raise ValueError('本地密钥文件校验失败')
    return payload


def key_names(root):
    """List standard and custom Desktop data names with key files."""
    paths = list(root.iterdir())
    names = {path.name[4:-1] for path in paths
             if path.is_file() and path.name.startswith('key_')
             and len(path.name) > 5 and path.name[-1] in 's01'}
    legacy_names = {'data'} | {path.name[5:].split('#', 1)[0] for path in paths
                              if path.is_dir() and path.name.startswith('user_')}
    for name in legacy_names:
        directory = legacy_key_directory(root, name)
        if any((directory / ('map' + suffix)).is_file() for suffix in 's01'):
            names.add(name)
    return sorted(names)


def legacy_key_directory(root, data_name):
    digest = md5(data_name.replace('#', '').encode('utf-8')).digest()[:8]
    folder = ''.join(f'{byte & 15:X}{byte >> 4:X}' for byte in digest)
    return root / folder


def _stored_file(root, base):
    safe = root / (base + 's')
    if safe.is_file():
        return safe
    backups = [root / (base + suffix) for suffix in ('0', '1') if (root / (base + suffix)).is_file()]
    backups.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for path in backups:
        try:
            _tdf_payload(path)
            return path
        except (OSError, ValueError):
            continue
    return backups[0] if backups else None


def key_file(root, data_name='data'):
    """Choose the standard Desktop key file without changing backup files."""
    modern = _stored_file(root, 'key_' + data_name)
    if modern:
        return modern
    legacy = _stored_file(legacy_key_directory(root, data_name), 'map')
    if legacy:
        return legacy
    raise ValueError('未找到 Telegram Desktop 本地密钥文件')


def load_modern_key(path, passcode=''):
    payload = _tdf_payload(path)
    offset = 0
    salt, offset = _byte_array(payload, offset)
    encrypted, offset = _byte_array(payload, offset)
    _, offset = _byte_array(payload, offset)
    if len(salt) != 32:
        raise ValueError('本地密钥盐值长度错误')
    password = passcode.encode('utf-8')
    if path.name in ('maps', 'map0', 'map1'):
        if offset != len(payload):
            raise ValueError('旧版密钥文件包含多余数据')
        passkey = pbkdf2_hmac('sha1', password, salt, 4000 if password else 4, 256)
    elif offset < len(payload):
        trailer, offset = _byte_array(payload, offset)
        if offset != len(payload):
            raise ValueError('本地密钥文件包含多余数据')
        if password or not encrypted:
            encrypted, passkey = _new_wrap(trailer, password)
        else:
            _new_wrap(trailer, password, validate_only=True)
            seed = sha512(salt + salt).digest()
            passkey = pbkdf2_hmac('sha512', seed, salt, 1, 256)
    else:
        seed = sha512(salt + password + salt).digest()
        passkey = pbkdf2_hmac('sha512', seed, salt, 100000 if password else 1, 256)
    local_key = _decrypt_blob(encrypted, passkey)
    if len(local_key) not in (64, 256):
        raise ValueError('不支持的本地密钥长度')
    return local_key


def read_tdef(path, key):
    """Decrypt a complete TDEF payload; the caller trims entry padding."""
    if len(key) not in (64, 256):
        raise ValueError('不支持的本地密钥长度')
    with path.open('rb') as source:
        try:
            magic = source.read(4)
        except PermissionError:
            # A running Desktop client locks these four bytes.  The encrypted
            # header still authenticates the key and the remainder of the file.
            source.seek(4)
        else:
            if magic != b'TDEF':
                raise ValueError('无法识别加密缓存文件')
        salt = source.read(64)
        encrypted = source.read()
    if len(salt) != 64 or len(encrypted) < 48 or len(encrypted) % 16:
        raise ValueError('加密缓存文件不完整')
    half = len(key) // 2
    aes_key = sha256(key[:half] + salt[:32]).digest()
    iv = sha256(key[half:] + salt[32:]).digest()[:16]
    decryptor = Cipher(algorithms.AES(aes_key), modes.CTR(iv)).decryptor()
    plain = decryptor.update(encrypted) + decryptor.finalize()
    expected = sha256(key + salt + plain[:16]).digest()
    if not hmac.compare_digest(plain[16:48], expected):
        raise ValueError('加密缓存校验失败或本地密钥不匹配')
    if plain[0] != 0:
        raise ValueError('不支持的加密缓存版本')
    return plain[48:]
