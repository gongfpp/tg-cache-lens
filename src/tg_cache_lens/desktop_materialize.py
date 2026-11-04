"""Create temporary, range-indexed media snapshots from Desktop cache parts."""
from dataclasses import dataclass
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
import struct
import tempfile
import zlib

from .desktop_store import read_document, read_plain, read_index_snapshot
from . import core
from .desktop_parts import Part
from .sparse import prepare_sparse

SPOOL_THRESHOLD = 64 * 1024 * 1024


def document_spool(snapshot, base_key, directory=None):
    group = snapshot.groups.get(base_key, {})
    return tempfile.TemporaryFile(dir=directory) if sum(entry.size for entry in group.values()) > SPOOL_THRESHOLD else nullcontext(None)


@dataclass(frozen=True)
class DocumentSource:
    database: Path
    base_key: tuple[int, int]
    local_key: bytes
    content_hash: bytes
    ambiguous: tuple[int, ...] = ()
    unavailable: tuple[int, ...] = ()
    cache_time: int | None = None

    def revalidate(self):
        snapshot = read_index_snapshot(self.database, self.local_key)
        with document_spool(snapshot, self.base_key) as spool:
            _, _, ambiguous, unavailable, identity = read_document(self.database, self.base_key, self.local_key, snapshot, spool)
        if identity != self.content_hash or ambiguous != self.ambiguous or unavailable != self.unavailable:
            raise RuntimeError('Desktop 缓存内容已变化，请重新扫描')


@dataclass(frozen=True)
class PlainSource:
    database: Path
    base_key: tuple[int, int]
    local_key: bytes
    content_hash: bytes
    entry: object

    @property
    def cache_time(self):
        return self.entry.store_time

    def revalidate(self):
        value, entry = read_plain(self.database, self.base_key, self.local_key)
        if sha256(value).digest() != self.content_hash or entry != self.entry:
            raise RuntimeError('Desktop 缓存内容已变化，请重新扫描')


def validate_destination(database, destination):
    database = Path(database).resolve()
    destination = Path(destination).resolve()
    if destination.is_relative_to(database.parent.parent.resolve()):
        raise ValueError('临时目录必须位于 Telegram 缓存目录之外')
    return destination


def write_snapshot(database, destination, parts, ranges):
    """Write a temporary snapshot; its index explicitly has unknown total size."""
    destination = validate_destination(database, destination)
    if not ranges:
        raise ValueError('没有可用的缓存分片')
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    metadata_created = False
    metadata_path = Path(str(destination) + '.meta')
    try:
        with destination.open('xb') as output:
            created = True
            if ranges[0][0] or len(ranges) > 1:
                prepare_sparse(output)
            for part in parts:
                event = core.current_cancel_event()
                if event is not None and event.is_set(): raise core.WorkCancelled()
                output.seek(part.offset)
                output.write(part.data[:] if not isinstance(part.data, bytes) else part.data)
        payload = struct.pack('<q', -1) + b''.join(
            struct.pack('<qq', start, end - start) for start, end in ranges)
        metadata = struct.pack('<IIi', 0x7bac1487, zlib.crc32(payload), len(ranges)) + payload
        with metadata_path.open('xb') as output:
            metadata_created = True
            output.write(metadata)
    except BaseException:
        if metadata_created:
            metadata_path.unlink(missing_ok=True)
        if created:
            destination.unlink(missing_ok=True)
        raise


def materialize(database, base_key, local_key, destination, snapshot=None):
    validate_destination(database, destination)
    snapshot = snapshot or read_index_snapshot(database, local_key)
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    with document_spool(snapshot, base_key, Path(destination).parent) as spool:
        parts, ranges, ambiguous, unavailable, identity = read_document(database, base_key, local_key, snapshot, spool)
        write_snapshot(database, destination, parts, ranges)
    times = [entry.store_time for entry in snapshot.groups[base_key].values() if entry.store_time]
    return DocumentSource(Path(database).resolve(), base_key, local_key, identity, ambiguous, unavailable, max(times) if times else None)


def materialize_plain(database, entry_key, local_key, destination, snapshot=None, value_and_entry=None):
    validate_destination(database, destination)
    value, entry = value_and_entry if value_and_entry is not None else read_plain(database, entry_key, local_key, snapshot)
    partial = value.startswith(b'partial:')
    data = value[8:] if partial else value
    if not data:
        raise ValueError('缓存条目不包含媒体数据')
    write_snapshot(database, destination, [Part(0, data)], [(0, len(data))])
    return PlainSource(Path(database).resolve(), entry_key, local_key, sha256(value).digest(), entry), partial
