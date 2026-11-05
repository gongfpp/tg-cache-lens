import argparse
import json
import sys
import subprocess
import os
from pathlib import Path
from . import __version__, core


def launch_gui(cache, output, timeout):
    try:
        from .gui import launch
    except ImportError as e:
        if e.name and e.name.startswith('PySide6'):
            sibling = Path(sys.executable).resolve().parent.parent / 'Cache Lens.app'
            if getattr(sys, 'frozen', False) and sys.platform == 'darwin' and sibling.is_dir():
                command = ['open', '-a', str(sibling), '--args']
                if cache is not None: command += ['--cache', str(cache)]
                if output is not None: command += ['--output', str(output)]
                subprocess.run(command + ['--timeout', str(timeout)], check=True)
                return 0
            print('GUI 需要安装：pip install "tg-cache-lens[gui]"，或在源码目录运行 uv sync --extra gui', file=sys.stderr)
            return 1
        raise
    return launch(cache, output, timeout)


def main(argv=None):
    p = argparse.ArgumentParser(description='Telegram 视频缓存检测、预览与导出')
    p.add_argument('command', nargs='?', choices=['gui', 'export'], help='默认简洁交互；gui 打开独立窗口；export 非交互导出')
    p.add_argument('--version', action='version', version=__version__)
    p.add_argument('--cache', type=Path, help='MediaBox 目录、Desktop tdata 或 portable 目录')
    p.add_argument('--output', type=Path, help='导出目录，默认 ~/Downloads/Telegram Exports')
    p.add_argument('--timeout', type=int, default=600, help='每次解码/导出超时秒数，默认 600')
    p.add_argument('--format', choices=['mp4', 'mkv', 'original'], default='mp4')
    p.add_argument('--scan', action='store_true', help='输出 JSON 扫描报告')
    p.add_argument('--deep', action='store_true', help='JSON 扫描时全程解码')
    targets = p.add_mutually_exclusive_group()
    targets.add_argument('--all-complete', action='store_true', help='验证并导出所有完整视频')
    targets.add_argument('--file', type=Path, help='单个缓存文件路径')
    args = p.parse_args(argv)
    if args.timeout < 1: p.error('--timeout 必须大于 0')
    if args.scan and args.command: p.error('--scan 不能与子命令同时使用')
    if args.deep and not args.scan: p.error('--deep 需要 --scan')
    if (args.all_complete or args.file) and args.command != 'export': p.error('导出目标需要 export 子命令')
    if args.command == 'export' and not (args.all_complete or args.file): p.error('export 需要 --all-complete 或 --file')
    try:
        cache = args.cache if args.command == 'gui' else args.cache or (core.default_root() if args.file else core.auto_root())
        output = args.output if args.command == 'gui' else args.output or core.default_output()
        if args.scan:
            rows = core.scan(cache, args.deep, timeout=args.timeout, passcode=os.environ.get('TG_CACHE_LENS_PASSCODE', ''))
            for warning in getattr(rows, 'warnings', []):
                print(f'读取失败：{warning}', file=sys.stderr)
            reports = []
            for item in rows:
                source = getattr(item, 'desktop_source', None)
                path = str(source.database) + '/' + '-'.join(f'{part:016x}' for part in source.base_key) if source else str(item.path)
                report = dict(path=path, status=item.status, detail=item.detail, coverage=item.coverage,
                              size=None if source else item.size, copies=item.copies)
                if source:
                    report.update(cached_size=item.cached_size, known_end=item.size)
                reports.append(report)
            print(json.dumps(reports, ensure_ascii=False, indent=2))
            return 1 if getattr(rows, 'warnings', []) else 0
        if args.command == 'gui': return launch_gui(cache, output, args.timeout)
        from .cli import interactive, batch, report_batch, progress_line
        if args.command == 'export':
            scan_errors = False
            if args.file:
                row = core.inspect(args.file.expanduser())
                if row is None: raise RuntimeError('文件不是可识别的视频缓存')
                rows = [row]
            else:
                from .recovery import can_export_cached
                scanned = core.scan(cache, timeout=args.timeout, passcode=os.environ.get('TG_CACHE_LENS_PASSCODE', ''))
                for warning in getattr(scanned, 'warnings', []):
                    print(f'读取失败：{warning}', file=sys.stderr)
                scan_errors = bool(getattr(scanned, 'warnings', []))
                rows = [r for r in scanned if r.status in ('ready', 'covered') or can_export_cached(r)]
            result = report_batch(batch(rows, output, args.format, args.timeout,
                progress_line), output)
            return 1 if scan_errors else result
        if not sys.stdin.isatty(): p.error('非交互输入请使用 --scan、export 或 gui')
        return interactive(cache, output, args.timeout, args.format)
    except (OSError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\n已取消。', file=sys.stderr)
        return 130

if __name__ == '__main__': raise SystemExit(main())
