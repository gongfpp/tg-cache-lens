"""Small, line-oriented interface. No alternate screen or terminal sizing required."""
from collections import Counter
from pathlib import Path
import subprocess
import sys
import unicodedata
import tempfile
import os
import getpass
from . import core

SHORT = {'ready': '完整', 'covered': '待验证', 'partial': '数据不全', 'unknown': '未知', 'invalid': '无法验证'}


def duration(seconds):
    if seconds is None: return '—'
    seconds = int(seconds)
    return f'{seconds//3600}:{seconds//60%60:02}:{seconds%60:02}' if seconds >= 3600 else f'{seconds//60:02}:{seconds%60:02}'


def pad(text, width):
    cells = sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)
    return text + ' ' * max(0, width-cells)


def size(value):
    return f'{value/1024**3:.1f} GB' if value >= 1024**3 else f'{value/1024**2:.1f} MB'


def progress_line(n, total, text):
    if n < total:
        print('\r' + text + ' ' * 12, end='', file=sys.stderr, flush=True)
    elif total:
        print(file=sys.stderr, flush=True)


def batch(rows, output, fmt='mp4', timeout=600, progress=lambda *args: None, on_failure=lambda row: None, on_item=lambda *args: None):
    results, errors = [], []
    for n, row in enumerate(rows, 1):
        on_item(row, '正在导出')
        def stage(value, total, text):
            on_item(row, f'导出 {value}%' if total == 100 else '正在导出')
            progress(n-1, len(rows), f'{text} · {n}/{len(rows)}')
        progress(n-1, len(rows), f'正在导出 {n}/{len(rows)} · {row.key}')
        try:
            from . import recovery
            if row.status == 'pending':
                from .desktop_scan import prepare_lazy
                progress(n-1, len(rows), f'正在加载视频 {n}/{len(rows)}')
                prepare_lazy(row)
            if row.status == 'partial' and getattr(row, 'desktop_source', None):
                with tempfile.TemporaryDirectory(prefix='cache-lens-recovery-') as work:
                    clips = recovery.recover(row, work, timeout, stage)
                    paths, failures = recovery.export_merged_clips(row, clips, output, timeout, stage)
                    if failures: raise RuntimeError(failures[0])
                    if not paths: raise RuntimeError('未找到可导出的片段')
                    results.append(paths[0])
                on_item(row, '已导出片段')
                continue
            if recovery.can_export_cached(row):
                try:
                    results.append(recovery.export_cached(row, output, timeout, fmt,
                        stage))
                except recovery.CachedMediaInvalid:
                    if fmt != 'mp4':
                        raise
                    with tempfile.TemporaryDirectory(prefix='cache-lens-recovery-') as work:
                        clips = recovery.recover(row, work, timeout, lambda a, b, text: progress(n-1, len(rows), f'{text} · {n}/{len(rows)}'))
                        if not clips:
                            raise RuntimeError('未找到可导出的片段')
                        paths, failures = recovery.export_merged_clips(row, clips, output, timeout, stage)
                        if failures:
                            raise RuntimeError(failures[0])
                        if not paths:
                            raise RuntimeError('未找到可导出的片段')
                        results.append(paths[0])
                        on_item(row, '已导出片段')
                        continue
            else:
                results.append(core.export(row, output, fmt, timeout, lambda value: on_item(row, f'导出 {value}%')))
            on_item(row, '已导出')
        except (OSError, RuntimeError, ValueError) as e:
            errors.append(f'{row.path.name}: {e}')
            on_item(row, '导出失败')
            on_failure(row)
    progress(len(rows), len(rows), f'成功 {len(results)} 项 · 失败 {len(errors)} 项')
    return results, errors


def report_batch(result, output):
    paths, errors = result
    print(f'成功 {len(paths)} 项 · 失败 {len(errors)} 项\n导出目录：{output}')
    for error in errors: print(f'  失败：{error}', file=sys.stderr)
    return 1 if errors else 0


def recover_interactive(row, previews, output, timeout):
    from . import recovery
    clips = recovery.recover(row, previews, timeout)
    if not clips:
        print('未找到可恢复片段')
        return
    try:
        for number, clip in enumerate(clips, 1):
            print(f'{number}. {clip.start:.3f}s–{clip.start + clip.length:.3f}s')
        try:
            selection = input('输入片段编号，回车返回：').strip()
        except EOFError:
            return
        if not selection.isdigit() or not 1 <= int(selection) <= len(clips):
            return
        clip = clips[int(selection) - 1]
        try:
            action = input('[p] 预览  [e] 导出  [回车] 返回：').strip()
        except EOFError:
            return
        if action == 'p':
            # The verified copy is kept until the menu exits.
            core.open_path(clip.path)
            clips.remove(clip)
        elif action == 'e':
            report_batch(recovery.export_clips(row, [clip], output, timeout), output)
    finally:
        for clip in clips:
            clip.path.unlink(missing_ok=True)


def interactive(cache, output, timeout=600, fmt='mp4'):
    query, page = '', 0
    passcode = os.environ.get('TG_CACHE_LENS_PASSCODE', '')
    with tempfile.TemporaryDirectory(prefix='cache-lens-preview-') as previews:
        rows = []
        rescan = True
        while True:
            if rescan:
                print('正在扫描…', flush=True)
                try:
                    rows = core.scan(cache, timeout=timeout, passcode=passcode)
                except RuntimeError as exc:
                    if '本地密码错误' not in str(exc) or passcode:
                        raise
                    passcode = getpass.getpass('Telegram 本地密码：')
                    rows = core.scan(cache, timeout=timeout, passcode=passcode)
                for warning in getattr(rows, 'warnings', []):
                    print(f'读取失败：{warning}', file=sys.stderr)
                rescan = False
                counts = Counter(SHORT[r.status] for r in rows)
                print('\nTG Cache Lens\n找到 ' + str(len(rows)) + ' 项：' + ' · '.join(f'{s} {n}' for s, n in counts.items()))
            visible = [(i, r) for i, r in enumerate(rows, 1) if query.lower() in f'{r.source_path} {r.key} {r.codec}'.lower()]
            pages = max(1, (len(visible)+9)//10)
            page = max(0, min(page, pages-1))
            print('\n编号  状态            时长       已缓存')
            for i, r in visible[page*10:(page+1)*10]:
                cached = size(r.cached_size) if r.cached_size is not None else '未知'
                print(f'{i:>4}  {pad(SHORT[r.status], 10)}  {duration(r.duration):>8}  {cached:>9}')
            print(f'第 {page+1}/{pages} 页 · {len(visible)} 项' + (f' · 搜索：{query}' if query else ''))
            print('输入编号查看 · [n/b] 翻页 · [/关键词] 搜索 · [/] 清除')
            print('[a] 导出全部完整视频  [r] 重扫  [g] GUI  [q] 退出')
            try: choice = input('> ').strip()
            except EOFError: return 0
            if choice == 'q': return 0
            if choice == 'r': rescan, page = True, 0
            elif choice == 'n': page += 1
            elif choice == 'b': page -= 1
            elif choice.startswith('/'): query, page = choice[1:], 0
            elif choice == 'g':
                from .__main__ import launch_gui
                return launch_gui(cache, output, timeout)
            elif choice == 'a':
                candidates = [r for r in rows if r.status in ('ready','covered')]
                report_batch(batch(candidates, output, fmt, timeout, progress_line), output)
            elif choice.isdigit() and 1 <= int(choice) <= len(rows):
                row = rows[int(choice)-1]
                while True:
                    extent = f'已知范围 {size(row.size)}' if getattr(row, 'desktop_source', None) else size(row.size)
                    print(f'\n{row.key}\n{core.LABELS[row.status]} · {duration(row.duration)} · {extent}\n{row.detail}')
                    permitted = row.status in ('ready', 'covered')
                    recoverable = row.status == 'partial' or (row.status == 'unknown' and getattr(row, 'desktop_source', None))
                    prompt = '[p] 预览  [e] 导出  [回车] 返回\n> ' if permitted else '[c] 恢复片段  [回车] 返回\n> ' if recoverable else '[回车] 返回\n> '
                    try: action = input(prompt).strip()
                    except EOFError: return 0
                    if not action: break
                    try:
                        if action == 'p' and permitted:
                            print('验证并准备预览…', flush=True)
                            core.preview(row, previews, timeout)
                        elif action == 'e' and permitted:
                            print('验证并导出…', flush=True)
                            print(f'已导出：{core.export(row, output, fmt, timeout)}')
                        elif action == 'c' and recoverable:
                            recover_interactive(row, previews, output, timeout)
                        else: print('请选择显示的操作。')
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e: print(f'失败：{e}')
            else: print('请输入编号或显示的操作。')
