import argparse
import json
from pathlib import Path
from . import __version__, core

def main():
    p = argparse.ArgumentParser(description="Telegram macOS 视频缓存检测、预览与导出")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--cache", type=Path, help="media 文件夹或 Telegram Group Container")
    p.add_argument("--output", type=Path, help="导出目录，默认 ~/Downloads/Telegram Exports")
    p.add_argument("--timeout", type=int, default=600, help="每个解码/导出操作超时秒数，默认 600")
    p.add_argument("--scan", action="store_true", help="不打开 TUI，输出 JSON 扫描报告")
    p.add_argument("--deep", action="store_true", help="CLI 扫描时全程解码")
    args = p.parse_args()
    if args.scan:
        try:
            rows = core.scan(args.cache or core.default_root(), args.deep, timeout=args.timeout)
            print(json.dumps([dict(path=str(i.path), status=i.status, detail=i.detail, coverage=i.coverage,
                                   size=i.size, copies=i.copies) for i in rows], ensure_ascii=False, indent=2))
        except (OSError, RuntimeError) as e: p.exit(1, f"{e}\n")
    else:
        from .app import CacheLens
        CacheLens(args.cache, args.output, args.timeout).run()

if __name__ == "__main__": main()
