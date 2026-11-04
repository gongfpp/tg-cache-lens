"""Read the active Telegram Desktop cache index and its verified values."""
from pathlib import Path
import re
import struct
from dataclasses import dataclass
from hashlib import sha256

import xxhash

from .desktop_crypto import read_tdef
from . import core
from .desktop_index import parse_binlog
from .desktop_parts import (AmbiguousSlice, Part, available_ranges, decode_header_prefix,
                            decode_slice, group_documents, matching_final_slice)


class CacheChanged(ValueError):
    """A cache file changed while being read."""


class DiskSlice:
    """A part held in a temporary spool rather than in the Python heap."""
    def __init__(self, stream, start, length):
        self.stream, self.start, self.length = stream, start, length

    def __len__(self): return self.length

    def __getitem__(self, key):
        if not isinstance(key, slice): raise TypeError('slice required')
        start, stop, step = key.indices(self.length)
        if step != 1: raise ValueError('unsupported slice step')
        self.stream.seek(self.start + start)
        return self.stream.read(max(0, stop - start))

    def view(self, start, end):
        return DiskSlice(self.stream, self.start + start, end - start)


def databases(tdata, data_name='data'):
    """Find account databases under an explicitly selected tdata directory."""
    root = Path(tdata)
    prefix = 'user_' + data_name.replace('#', '')
    for account in sorted(root.iterdir()):
        if not account.is_dir() or not re.fullmatch(re.escape(prefix) + r'(?:#[0-9]+)?', account.name):
            continue
        for name in ('cache', 'media_cache'):
            database = account / name
            if database.is_dir():
                yield database


def active_version(database):
    database = Path(database)
    try:
        raw = (database / 'version').read_bytes()
    except FileNotFoundError:
        # Telegram's ReadVersionValue falls back to version zero.
        raw = struct.pack('<i', 0)
    if len(raw) < 4:
        raise ValueError('Desktop 缓存版本文件损坏')
    version, = struct.unpack_from('<i', raw)
    if version < 0:
        raise ValueError('Desktop 缓存版本无效')
    directory = database / str(version)
    for name in ('binlog', 'binlog-ready'):
        if (directory / name).is_file():
            return directory, directory / name
    raise ValueError('找不到当前 Desktop 缓存索引')


def _stamp(path):
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


@dataclass
class IndexSnapshot:
    database: Path
    directory: Path
    log: Path
    stamp: tuple
    entries: dict
    groups: dict

    def check(self):
        if _stamp(self.log) != self.stamp or active_version(self.database) != (self.directory, self.log):
            raise CacheChanged('Desktop 缓存索引正在变化，请重试')


def read_index_snapshot(database, key):
    database = Path(database)
    directory, log = active_version(database)
    before = _stamp(log)
    payload = read_tdef(log, key)
    if _stamp(log) != before or active_version(database) != (directory, log):
        raise CacheChanged('Desktop 缓存索引正在变化，请重试')
    entries = parse_binlog(payload)
    groups = group_documents(entries) if database.name == 'media_cache' else {}
    snapshot = IndexSnapshot(database, directory, log, before, entries, groups)
    snapshot.check()
    return snapshot


def read_index(database, key):
    snapshot = read_index_snapshot(database, key)
    return snapshot.directory, snapshot.entries


def read_value(directory, entry, key):
    path = Path(directory) / entry.relative_path()
    before = _stamp(path)
    payload = read_tdef(path, key)
    if _stamp(path) != before:
        raise CacheChanged('Desktop 缓存条目正在变化，请重试')
    if len(payload) < entry.size:
        raise ValueError('Desktop 缓存条目不完整')
    value = payload[:entry.size]
    if xxhash.xxh32_intdigest(value) != entry.checksum:
        raise ValueError('Desktop 缓存条目校验失败')
    return value


def read_plain(database, entry_key, key, snapshot=None):
    """Read a current small-cache value and confirm its index is still current."""
    snapshot = snapshot or read_index_snapshot(database, key)
    snapshot.check()
    directory, entries = snapshot.directory, snapshot.entries
    entry = entries.get(entry_key)
    if entry is None:
        raise ValueError('缓存条目已被移除，请重新扫描')
    value = read_value(directory, entry, key)
    snapshot.check()
    return value, entry


def read_document(database, base_key, key, snapshot=None, spool=None):
    """Read a consistent indexed document's verified parts and ranges.

    The returned ranges describe only cached bytes, not original file length.
    """
    snapshot = snapshot or read_index_snapshot(database, key)
    snapshot.check()
    directory = snapshot.directory
    group = snapshot.groups.get(base_key)
    if not group:
        raise ValueError('视频缓存已被移除，请重新扫描')
    def optional_stamp(path):
        try:
            return _stamp(path)
        except FileNotFoundError:
            return None
    paths = [directory / entry.relative_path() for entry in group.values()]
    stamps = [optional_stamp(path) for path in paths]
    parts, ambiguous, unavailable = [], [], []
    def add_parts(number, decoded):
        for part in decoded:
            if spool is not None:
                spool.seek(0, 2)
                start = spool.tell()
                spool.write(part.data)
                part = Part(part.offset, DiskSlice(spool, start, len(part.data)))
            parts.append((number, part))
    identity = sha256()
    for number, entry in sorted(group.items()):
        event = core.current_cancel_event()
        if event is not None and event.is_set(): raise core.WorkCancelled()
        identity.update(struct.pack('<IQQIBI', number, *entry.key, entry.size, entry.tag, entry.checksum))
        identity.update(entry.place)
        path = directory / entry.relative_path()
        state = optional_stamp(path)
        if state is None:
            identity.update(b'missing')
            unavailable.append(number)
            continue
        try:
            value = read_value(directory, entry, key)
        except (FileNotFoundError, CacheChanged) as exc:
            raise CacheChanged('Desktop 缓存条目正在变化，请重试') from exc
        except (OSError, ValueError):
            # Locked, unreadable or corrupt slices must not hide other ranges.
            identity.update(b'invalid')
            identity.update(repr(state).encode('ascii'))
            unavailable.append(number)
            continue
        identity.update(b'valid')
        identity.update(sha256(value).digest())
        try:
            add_parts(number, decode_slice(value, number))
        except AmbiguousSlice:
            matched = matching_final_slice(value, number, (part for _, part in parts))
            if matched:
                add_parts(number, matched)
            else:
                ambiguous.append(number)
        except ValueError:
            matched = matching_final_slice(value, number, (part for _, part in parts))
            if matched:
                add_parts(number, matched)
                continue
            if number == 0:
                try:
                    add_parts(number, decode_header_prefix(value))
                except ValueError:
                    pass
            unavailable.append(number)
    # Exclude conflicting overlap from every source, while retaining ranges
    # outside it. The cache alone cannot establish which copy is correct.
    parts.sort(key=lambda item: item[1].offset)
    conflicts, affected = [], set()
    for i, (number, part) in enumerate(parts):
        event = core.current_cancel_event()
        if event is not None and event.is_set(): raise core.WorkCancelled()
        for other_number, other in parts[i + 1:]:
            start = other.offset
            end = min(part.offset + len(part.data), other.offset + len(other.data))
            if start >= part.offset + len(part.data):
                break
            if part.data[start - part.offset:end - part.offset] != other.data[:end - start]:
                conflicts.append((start, end))
                affected.update((number, other_number))
    if conflicts:
        clean = []
        for _, part in parts:
            ranges = [(part.offset, part.offset + len(part.data))]
            for start, end in conflicts:
                ranges = [(a, b) for left, right in ranges
                          for a, b in ((left, min(right, start)), (max(left, end), right))
                          if a < b]
            clean.extend(Part(start, part.data.view(start - part.offset, end - part.offset)
                              if isinstance(part.data, DiskSlice) else part.data[start - part.offset:end - part.offset])
                         for start, end in ranges)
        parts = clean
        unavailable = sorted(set(unavailable) | affected)
    else:
        parts = [part for _, part in parts]
    snapshot.check()
    if [optional_stamp(path) for path in paths] != stamps:
        raise CacheChanged('视频缓存索引已变化，请重新扫描')
    return parts, available_ranges(parts), tuple(ambiguous), tuple(unavailable), identity.digest()
