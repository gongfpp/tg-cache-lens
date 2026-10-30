"""Small, line-oriented interface. No alternate screen or terminal sizing required."""
from collections import Counter
from pathlib import Path
import subprocess
import sys
import unicodedata
import tempfile
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


def batch(rows, output, fmt='mp4', timeout=600, progress=lambda *args: None):
    results, errors = [], []
    for n, row in enumerate(rows, 1):
        progress(n-1, len(rows), f'验证并导出 {n}/{len(rows)}')
        try:
            results.append(core.export(row, output, fmt, timeout))
        except (OSError, RuntimeError, ValueError) as e:
            errors.append(f'{row.path.name}: {e}')
    progress(len(rows), len(rows), f'成功 {len(results)} 项 · 失败 {len(errors)} 项')
    return results, errors


def report_batch(result, output):
    paths, errors = result
    print(f'成功 {len(paths)} 项 · 失败 {len(errors)} 项\n导出目录：{output}')
    for error in errors: print(f'  失败：{error}', file=sys.stderr)
    return 1 if errors else 0


def interactive(cache, output, timeout=600, fmt='mp4'):
    query, page = '', 0
    with tempfile.TemporaryDirectory(prefix='cache-lens-preview-') as previews:
        rows = []
        rescan = True
        while True:
            if rescan:
                print('正在扫描…', flush=True)
                rows = core.scan(cache, timeout=timeout)
                rescan = False
                counts = Counter(SHORT[r.status] for r in rows)
                print('\nTG Cache Lens\n找到 ' + str(len(rows)) + ' 项：' + ' · '.join(f'{s} {n}' for s, n in counts.items()))
            visible = [(i, r) for i, r in enumerate(rows, 1) if query.lower() in r.path.name.lower()]
            pages = max(1, (len(visible)+9)//10)
            page = max(0, min(page, pages-1))
            print('\n编号  状态            时长       大小')
            for i, r in visible[page*10:(page+1)*10]:
                print(f'{i:>4}  {pad(SHORT[r.status], 10)}  {duration(r.duration):>8}  {size(r.size):>9}')
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
                    print(f'\n{row.path.name}\n{core.LABELS[row.status]} · {duration(row.duration)} · {size(row.size)}\n{row.detail}')
                    permitted = row.status in ('ready', 'covered')
                    try: action = input('[p] 预览  [e] 导出  [回车] 返回\n> ' if permitted else '[回车] 返回\n> ').strip()
                    except EOFError: return 0
                    if not action: break
                    try:
                        if action == 'p' and permitted:
                            print('验证并准备预览…', flush=True)
                            core.preview(row, previews, timeout)
                        elif action == 'e' and permitted:
                            print('验证并导出…', flush=True)
                            print(f'已导出：{core.export(row, output, fmt, timeout)}')
                        else: print('请选择显示的操作。')
                    except (OSError, RuntimeError, subprocess.SubprocessError) as e: print(f'失败：{e}')
            else: print('请输入编号或显示的操作。')
