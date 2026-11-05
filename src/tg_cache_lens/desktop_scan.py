"""Translate explicitly selected Desktop cache data into media inspection rows."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from copy import copy
from hashlib import sha256
import os

from . import core
from .desktop_crypto import key_file, key_names, load_modern_key
from .desktop_materialize import materialize, materialize_plain, write_snapshot
from .desktop_store import databases, read_index_snapshot, read_value, read_value_prefix, _stamp
from .desktop_parts import decode_slice, available_ranges

LAZY_THRESHOLD = 32 * 1024 * 1024


class LazySource:
    """An indexed Desktop video whose full snapshot is loaded on demand."""
    def __init__(self, database, base_key, key, snapshot, sample, target, timeout):
        self.database, self.base_key, self.local_key = database, base_key, key
        self.snapshot, self.sample, self.target, self.timeout = snapshot, sample, target, timeout
        times = [entry.store_time for entry in snapshot.groups[base_key].values() if entry.store_time]
        self.cache_time = max(times) if times else None

    def prepare(self):
        snapshot = read_index_snapshot(self.database, self.local_key)
        source = materialize(self.database, self.base_key, self.local_key, self.target, snapshot)
        try:
            row = core.inspect(self.target, timeout=self.timeout)
            if not row: raise RuntimeError('缓存已变化或不包含可识别的视频')
            if source.ambiguous:
                row.detail += f'；{len(source.ambiguous)} 个分片格式不确定，未参与恢复'
            if source.unavailable:
                row.detail += f'；{len(source.unavailable)} 个分片缺失或损坏，未参与恢复'
            row.desktop_source = source
            return row
        except BaseException:
            self.target.unlink(missing_ok=True)
            Path(str(self.target) + '.meta').unlink(missing_ok=True)
            raise


def prepare_lazy(row):
    source = getattr(row, 'desktop_source', None)
    if isinstance(source, LazySource):
        ready = source.prepare()
        row.__dict__.update(ready.__dict__)
    return row


def sample_document(database, base, snapshot, key, target, timeout):
    group = snapshot.groups[base]
    first = 0 if 0 in group else 1 if 1 in group else None
    if first is None: return None
    snapshot.check()
    value = read_value(snapshot.directory, group[first], key)
    try: parts = decode_slice(value, first)
    except ValueError: return None
    if not any(part.offset == 0 and core.signature_bytes(part.data[:16]) for part in parts):
        return None
    # The final slice can contain an MP4 sample table; failure to parse it
    # must not prevent later full recovery.
    last = max(group)
    if last != first:
        try:
            value = read_value(snapshot.directory, group[last], key)
            tail = decode_slice(value, last)
            if not any(a.offset < b.offset + len(b.data) and b.offset < a.offset + len(a.data)
                       for a in parts for b in tail):
                parts.extend(tail)
        except (OSError, ValueError):
            pass
    snapshot.check()
    ranges = available_ranges(parts)
    sample = Path(str(target) + '.sample')
    write_snapshot(database, sample, parts, ranges)
    inspected = core.inspect(sample, timeout=timeout)
    if not inspected:
        sample.unlink(missing_ok=True)
        Path(str(sample) + '.meta').unlink(missing_ok=True)
        return None
    row = inspected
    row.path = target
    row.status = 'pending'
    row.cached = None
    row.total = None
    row.fingerprint = ()
    row.desktop_source = LazySource(database, base, key, snapshot, sample, target, timeout)
    return row


class ScanResults(list):
    def __init__(self):
        super().__init__()
        self.warnings = []


def resolve_tdata(path):
    root = Path(path).expanduser()
    if root.is_file() and root.name.startswith('key_') and root.name[-1:] in 's01':
        return root.parent
    for candidate in (root, root / 'tdata'):
        if candidate.is_dir() and key_names(candidate):
            return candidate
    return None


def select_data_name(root, selected=None):
    selected = Path(selected) if selected else None
    if selected and selected.is_file() and selected.parent.resolve() == root.resolve():
        name = selected.name
        if name.startswith('key_') and name[-1:] in 's01':
            return name[4:-1]
    names = key_names(root)
    if 'data' in names:
        return 'data'
    if len(names) == 1:
        return names[0]
    raise RuntimeError('检测到多组本地密钥；请指定其中一个 key_ 文件')


def scan(path, workdir, passcode='', deep=False, timeout=600, progress=lambda *a: None, data_name=None, cancelled=lambda: False, on_row=None, scan_cache=None, lazy=False):
    root = resolve_tdata(path)
    if root is None:
        raise RuntimeError('请选择包含本地密钥的 tdata 目录或 portable 程序目录')
    core.binary('ffmpeg')
    core.binary('ffprobe')
    try:
        data_name = data_name or select_data_name(root, path)
        key = load_modern_key(key_file(root, data_name), passcode)
        sources = list(databases(root, data_name))
        if not sources:
            raise RuntimeError('没有找到 Desktop 缓存数据库')
        groups = []
        rows = ScanResults()
        for database in sources:
            if cancelled():
                progress(0, 0, '扫描已停止')
            try:
                snapshot = read_index_snapshot(database, key)
            except (OSError, ValueError) as exc:
                rows.warnings.append(f'{database.parent.name}/{database.name}: {exc}')
                continue
            if database.name == 'media_cache':
                groups.extend((database, base, snapshot, None) for base in snapshot.groups)
            else:
                groups.extend((database, entry_key, snapshot, entry) for entry_key, entry in snapshot.entries.items())
        # Media-cache groups are overwhelmingly more likely to be videos than
        # small cache entries such as images and stickers. Start with the
        # smaller video groups so a large ordinary cache cannot delay the list.
        groups.sort(key=lambda group: (group[0].name != 'media_cache',
                    sum(entry.size for entry in group[2].groups[group[1]].values())
                    if group[3] is None else group[3].size))
        def inspect_group(job):
            if cancelled(): return None, None
            number, (database, base, snapshot, entry) = job
            target = Path(workdir) / f'{number:08d}-{base[0]:016x}-{base[1]:016x}'
            created = False
            try:
                cache_key = (str(database.resolve()), base, sha256(key).digest(), deep)
                entries = tuple(sorted(snapshot.groups[base].values(), key=lambda item: item.key)) if entry is None else (entry,)
                def identity():
                    snapshot.check()
                    states = []
                    for item in entries:
                        try: state = _stamp(snapshot.directory / item.relative_path())
                        except FileNotFoundError: state = None
                        except OSError: return None
                        states.append((item, state))
                    snapshot.check()
                    return snapshot.directory, tuple(states)
                identity_before = identity() if scan_cache is not None else None
                cached = scan_cache.get(cache_key) if scan_cache is not None else None
                if identity_before is not None and cached is not None and cached[0] == identity_before:
                    old = cached[1]
                    if old is None:
                        return None, None
                    try:
                        if core.stamp(old.path) != old.fingerprint:
                            raise OSError('snapshot changed')
                        os.link(old.path, target)
                        try: os.link(str(old.path) + '.meta', str(target) + '.meta')
                        except OSError:
                            target.unlink(missing_ok=True)
                            raise
                        row = copy(old)
                        row.path = target
                        row.fingerprint = core.stamp(target)
                        if identity() != identity_before:
                            target.unlink(missing_ok=True)
                            Path(str(target) + '.meta').unlink(missing_ok=True)
                            raise ValueError('Desktop 缓存条目正在变化，请重试')
                        scan_cache[cache_key] = (identity_before, row)
                        return row, None
                    except OSError:
                        pass
                if database.name == 'cache':
                    snapshot.check()
                    if entry.size >= 1024 * 1024:
                        prefix = read_value_prefix(snapshot.directory, entry, key)
                        sample = prefix[8:] if prefix.startswith(b'partial:') else prefix
                        if not core.signature_bytes(sample[:16]):
                            snapshot.check()
                            if identity_before is not None and identity() == identity_before:
                                scan_cache[cache_key] = (identity_before, None)
                            return None, None
                    data = read_value(snapshot.directory, entry, key)
                    sample = data[8:] if data.startswith(b'partial:') else data
                    snapshot.check()
                    if not core.signature_bytes(sample[:16]):
                        if identity_before is not None and identity() == identity_before:
                            scan_cache[cache_key] = (identity_before, None)
                        return None, None
                    source, partial = materialize_plain(database, base, key, target, snapshot, (data, entry))
                else:
                    if lazy and sum(item.size for item in entries) > LAZY_THRESHOLD:
                        try:
                            sampled = sample_document(database, base, snapshot, key, target, timeout)
                        except (OSError, ValueError):
                            sampled = None
                        if sampled is not None:
                            return sampled, None
                    source = materialize(database, base, key, target, snapshot)
                    partial = False
                created = True
                row = core.inspect(target, deep=deep, timeout=timeout)
                if row:
                    if getattr(source, 'ambiguous', ()):
                        row.detail += f'；{len(source.ambiguous)} 个分片格式不确定，未参与恢复'
                    if getattr(source, 'unavailable', ()):
                        row.detail += f'；{len(source.unavailable)} 个分片缺失或损坏，未参与恢复'
                    if partial:
                        row.status = 'partial'
                        row.detail = 'Desktop 标记为部分下载；只能恢复已验证片段'
                    row.desktop_source = source
                    if identity_before is not None and identity() == identity_before:
                        scan_cache[cache_key] = (identity_before, row)
                    return row, None
                else:
                    warning = None
                    if getattr(source, 'unavailable', ()) or getattr(source, 'ambiguous', ()):
                        warning = f'{database.name}/{base[0]:016x}-{base[1]:016x}: 缓存不完整，无法识别媒体'
                    target.unlink(missing_ok=True)
                    Path(str(target) + '.meta').unlink(missing_ok=True)
                    return None, warning
            except (OSError, ValueError, RuntimeError) as exc:
                if created:
                    target.unlink(missing_ok=True)
                    Path(str(target) + '.meta').unlink(missing_ok=True)
                return None, f'{database.name}/{base[0]:016x}-{base[1]:016x}: {exc}'
        progress(0, len(groups), '读取缓存索引')
        ordered = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            submit = core.propagate_cancel(inspect_group)
            pending = {}
            jobs = iter(enumerate(groups))
            def fill():
                while len(pending) < 8:
                    try: job = next(jobs)
                    except StopIteration: break
                    pending[pool.submit(submit, job)] = job[0]
            fill()
            completed = 0
            try:
                while pending:
                    if cancelled(): raise core.WorkCancelled()
                    ready, _ = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
                    for future in ready:
                        index = pending.pop(future)
                        row, warning = future.result()
                        if row:
                            ordered[index] = row
                            if on_row: on_row(row)
                        if warning: rows.warnings.append(warning)
                        completed += 1
                        progress(completed, len(groups), '读取缓存索引')
                    fill()
            finally:
                for future in pending: future.cancel()
        rows.extend(ordered[index] for index in sorted(ordered))
        progress(len(groups), len(groups), f'完成 · {len(rows)} 项')
        return rows
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
