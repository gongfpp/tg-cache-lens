"""Translate explicitly selected Desktop cache data into media inspection rows."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from . import core
from .desktop_crypto import key_file, key_names, load_modern_key
from .desktop_materialize import materialize, materialize_plain
from .desktop_store import databases, read_index_snapshot, read_value


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


def scan(path, workdir, passcode='', deep=False, timeout=600, progress=lambda *a: None, data_name=None, cancelled=lambda: False, on_row=None):
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
                if database.name == 'cache':
                    snapshot.check()
                    data = read_value(snapshot.directory, entry, key)
                    sample = data[8:] if data.startswith(b'partial:') else data
                    snapshot.check()
                    if not core.signature_bytes(sample[:16]):
                        return None, None
                    source, partial = materialize_plain(database, base, key, target, snapshot, (data, entry))
                else:
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
