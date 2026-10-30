import argparse
import json
import sys
import subprocess
from pathlib import Path
from . import __version__, core


def launch_gui(cache, output, timeout):
    try:
        from .gui import launch
    except ImportError as e:
        if e.name and e.name.startswith('PySide6'):
            sibling = Path(sys.executable).resolve().parent.parent / 'Cache Lens.app'
            if getattr(sys, 'frozen', False) and sys.platform == 'darwin' and sibling.is_dir():
                subprocess.run(['open', '-a', str(sibling), '--args', '--cache', str(cache), '--output', str(output), '--timeout', str(timeout)], check=True)
                return 0
            print('GUI 需要安装：pip install "tg-cache-lens[gui]"，或在源码目录运行 uv sync --extra gui', file=sys.stderr)
            return 1
        raise
    return launch(cache, output, timeout)


def main(argv=None):
    p = argparse.ArgumentParser(description='Telegram macOS 视频缓存检测、预览与导出')
    p.add_argument('command', nargs='?', choices=['gui', 'export'], help='默认简洁交互；gui 打开独立窗口；export 非交互导出')
    p.add_argument('--version', action='version', version=__version__)
    p.add_argument('--cache', type=Path, help='media 文件夹或 Telegram Group Container')
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
    cache, output = args.cache or core.default_root(), args.output or core.default_output()
    try:
        if args.scan:
            rows = core.scan(cache, args.deep, timeout=args.timeout)
            print(json.dumps([dict(path=str(i.path), status=i.status, detail=i.detail, coverage=i.coverage,
                                   size=i.size, copies=i.copies) for i in rows], ensure_ascii=False, indent=2))
            return 0
        if args.command == 'gui': return launch_gui(cache, output, args.timeout)
        from .cli import interactive, batch, report_batch, progress_line
        if args.command == 'export':
            if args.file:
                row = core.inspect(args.file.expanduser())
                if row is None: raise RuntimeError('文件不是可识别的视频缓存')
                rows = [row]
            else:
                rows = [r for r in core.scan(cache, timeout=args.timeout) if r.status in ('ready', 'covered')]
            return report_batch(batch(rows, output, args.format, args.timeout,
                progress_line), output)
        if not sys.stdin.isatty(): p.error('非交互输入请使用 --scan、export 或 gui')
        return interactive(cache, output, args.timeout, args.format)
    except (OSError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\n已取消。', file=sys.stderr)
        return 130

if __name__ == '__main__': raise SystemExit(main())
