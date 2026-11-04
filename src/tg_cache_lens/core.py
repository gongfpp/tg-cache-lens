"""Read-only cache inspection. No Telegram credentials or network access."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import json
import locale
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

LABELS = {"ready": "完整可导出", "covered": "数据齐全·待验证", "partial": "数据不全",
          "unknown": "完整性未知", "invalid": "无法验证"}

@dataclass
class Media:
    path: Path
    kind: str
    size: int
    status: str = "unknown"
    detail: str = ""
    total: int | None = None
    cached: int | None = None
    duration: float | None = None
    width: int = 0
    height: int = 0
    codec: str = ""
    audio: str = ""
    copies: int = 1
    fingerprint: tuple = field(default_factory=tuple)

    @property
    def cached_size(self):
        # File length can include sparse/preallocated bytes, so it is not a fallback.
        return min(self.size, self.cached) if self.cached is not None else None

    @property
    def suspected_animation(self):
        return (not self.audio and self.duration is not None and 0 < self.duration <= 30
                and 0 < self.width <= 512 and 0 < self.height <= 512)

    @property
    def key(self):
        source = getattr(self, 'desktop_source', None)
        if source:
            return '-'.join(f'{part:016x}' for part in source.base_key)
        return re.sub(r"(_partial|\.(mp4|mkv|webm|mov))$", "", self.path.name)

    @property
    def source_path(self):
        source = getattr(self, 'desktop_source', None)
        return source.database if source else self.path

    @property
    def source_modified_ns(self):
        source = getattr(self, 'desktop_source', None)
        return (source.cache_time * 1_000_000_000 if source.cache_time else None) if source else self.fingerprint[3]

    @property
    def coverage(self):
        return f"{min(100, 100*self.cached/self.total):.1f}%" if self.total and self.cached is not None else "—"

def default_root():
    if os.name == 'nt':
        return Path(os.environ.get('APPDATA', str(Path.home()))) / 'Telegram Desktop' / 'tdata'
    return Path.home() / "Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram"


def running_telegram_dirs():
    """Find candidate portable directories from running Telegram processes."""
    import psutil
    paths = set()
    for process in psutil.process_iter(['name']):
        try:
            if (process.info.get('name') or '').casefold() != 'telegram.exe':
                continue
            executable = process.exe()
            if executable:
                paths.add(Path(executable).parent / 'tdata')
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
    return paths


def auto_root():
    if os.name == 'nt' and sys.platform != 'darwin':
        from .desktop_scan import resolve_tdata
        candidates = {path.resolve() for path in running_telegram_dirs() if resolve_tdata(path)}
        if len(candidates) == 1:
            return candidates.pop()
        if len(candidates) > 1:
            raise RuntimeError('检测到多个正在运行的 Telegram 缓存目录；请手动选择')
        root = default_root()
        if resolve_tdata(root): return root
        raise RuntimeError('未找到标准安装目录；portable 版请手动选择程序目录或 tdata')
    if sys.platform != "darwin":
        raise RuntimeError("自动定位支持 macOS 原生 Telegram；其他来源请手动选择兼容的 MediaBox 目录")
    root = default_root()
    if not root.is_dir() or not media_dirs(root):
        raise RuntimeError("未找到 Telegram 缓存目录，请先运行 Telegram，或手动选择目录")
    return root

def media_dirs(root):
    return sorted(p for p in set(root.glob("*/account-*/postbox/media")) |
                  set(root.glob("account-*/postbox/media")) if p.is_dir())

def default_output():
    return Path.home() / "Downloads/Telegram Exports"

def binary(name):
    path = shutil.which(name)
    if not path:
        if os.name == 'nt':
            local = os.environ.get('LOCALAPPDATA')
            if local:
                candidate = Path(local) / 'Microsoft' / 'WinGet' / 'Links' / (name + '.exe')
                if candidate.is_file(): return str(candidate)
        else:
            for root in ("/opt/homebrew/bin", "/usr/local/bin"):
                if (Path(root)/name).is_file(): return str(Path(root)/name)
        hint = "请安装 FFmpeg 并将 ffmpeg 和 ffprobe 加入 PATH" if os.name == "nt" else "请先安装 FFmpeg（macOS 可运行 brew install ffmpeg）"
        raise RuntimeError(f"找不到 {name}。{hint}")
    return path

class WorkCancelled(BaseException):
    pass


_cancel_context = threading.local()


@contextmanager
def cancel_scope(event):
    previous = getattr(_cancel_context, 'event', None)
    _cancel_context.event = event
    try:
        yield
    finally:
        _cancel_context.event = previous


def current_cancel_event():
    return getattr(_cancel_context, 'event', None)


def propagate_cancel(function):
    event = current_cancel_event()
    def wrapped(*args):
        with cancel_scope(event):
            return function(*args)
    return wrapped


def run(command, timeout=30):
    event = current_cancel_event()
    if event is not None and event.is_set(): raise WorkCancelled()
    # File-backed output avoids waiting for inherited pipe handles from other
    # concurrently launched Windows media processes.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        deadline = time.monotonic() + timeout
        try:
            while True:
                if event is not None and event.is_set(): raise WorkCancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise subprocess.TimeoutExpired(command, timeout)
                try:
                    process.wait(timeout=min(.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException as exc:
            process.kill()
            process.wait()
            if isinstance(exc, subprocess.TimeoutExpired):
                raise RuntimeError('处理超时；可通过 --timeout 增加时限') from exc
            raise
        stdout.seek(0); stderr.seek(0)
        encoding = locale.getpreferredencoding(False)
        output = stdout.read().decode(encoding, errors='replace')
        errors = stderr.read().decode(encoding, errors='replace')
        if process.returncode:
            raise RuntimeError(errors.strip()[-700:] or '媒体工具处理失败')
        return output, errors

def signature_bytes(b):
    if b[4:8] == b"ftyp": return "mp4"
    if b[:4] == b"\x1aE\xdf\xa3": return "mkv"
    if b[:4] == b"RIFF" and b[8:12] == b"AVI ": return "avi"
    return None

def signature(path):
    try:
        with path.open("rb") as f: b = f.read(16)
        return signature_bytes(b)
    except OSError:
        pass
    return None

def read_map(data, file_size=None):
    # Independently implemented from the public MediaBoxFileMap format.
    if len(data) < 20 or struct.unpack_from("<I", data)[0] != 0x7bac1487:
        raise ValueError("不支持的缓存索引格式")
    crc, count = struct.unpack_from("<Ii", data, 4)
    if count < 0 or 20+16*count > len(data): raise ValueError("缓存索引长度错误")
    payload = data[12:20+16*count]
    if zlib.crc32(payload) != crc: raise ValueError("缓存索引 CRC 校验失败")
    total = struct.unpack_from("<q", payload)[0]
    end = cached = 0
    gap = False
    for start, length in sorted(struct.unpack_from("<qq", payload, 8+16*i) for i in range(count)):
        if start < 0 or length < 0: raise ValueError("缓存索引范围错误")
        gap |= start > end
        available_end = start+length if file_size is None else min(start+length, file_size)
        cached += max(0, available_end-max(start, end))
        end = max(end, start+length)
    gap |= total >= 0 and end < total
    return (total if total >= 0 else None), cached, gap, end

def stamp(path):
    s = path.stat()
    meta = Path(str(path)+".meta")
    data = meta.read_bytes() if meta.is_file() else None
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, data)

def inspect(path, kind=None, deep=False, timeout=600):
    path = Path(path)
    kind = kind or signature(path)
    if not kind: return None
    before = stamp(path)
    item = Media(path, kind, before[2], fingerprint=before)
    verified_map = False
    if before[4] is not None:
        try:
            item.total, item.cached, gap, end = read_map(before[4], item.size)
        except ValueError as e:
            item.status, item.detail = "invalid", str(e)
            return item
        if gap or end > item.size:
            item.status, item.detail = "partial", "缓存索引存在缺口，不能导出完整视频"
        else:
            verified_map = item.total is not None and item.total > 0
    try:
        raw, _ = run([binary("ffprobe"), "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,duration:stream_disposition=attached_pic", "-of", "json", str(path)], 15)
        info = json.loads(raw)
        streams = info.get("streams", [])
        videos = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
        if not videos: return None
        v = videos[0]
        item.width, item.height, item.codec = v.get("width", 0), v.get("height", 0), v.get("codec_name", "")
        item.audio = next((s.get("codec_name", "") for s in streams if s.get("codec_type") == "audio"), "")
        for value in (v.get('duration'), info.get('format', {}).get('duration')):
            try: duration = float(value)
            except (ValueError, TypeError): continue
            if math.isfinite(duration) and duration > 0:
                item.duration = duration
                break
        if item.status != "partial":
            if deep:
                _, errors = run([binary("ffmpeg"), "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode",
                    "-threads", "2", "-i", str(path), "-map", "0:v", "-map", "0:a?",
                    "-fps_mode:v", "passthrough", "-enc_time_base:v", "1:1000000", "-f", "null", "-"], timeout)
                if errors.strip(): raise RuntimeError(errors[-700:])
                item.status = "ready" if verified_map else "unknown"
                item.detail = "完整范围已确认，音视频全程解码通过" if verified_map else "现有内容解码通过，但缺少原始完整长度依据"
            else:
                item.status = "covered" if verified_map else "unknown"
                item.detail = "索引覆盖完整长度；导出前自动深度验证" if verified_map else "缺少完整长度依据；深度验证也不能证明原片完整"
    except (RuntimeError, ValueError) as e:
        if item.status != "partial": item.status = "invalid"
        item.detail = str(e)
    if stamp(path) != before:
        item.status, item.detail = "invalid", "缓存正在变化，请稍后重新扫描"
    return item

def scan(root, deep=False, progress=lambda *a: None, timeout=600, passcode=''):
    root = Path(root).expanduser()
    if not root.is_dir() and not root.is_file(): raise RuntimeError("缓存目录不存在；请用 --cache 指定缓存位置")
    binary("ffmpeg"); binary("ffprobe")
    from . import desktop_scan
    if desktop_scan.resolve_tdata(root):
        import tempfile
        owner = tempfile.TemporaryDirectory(prefix='cache-lens-desktop-')
        try:
            result = desktop_scan.scan(root, owner.name, passcode, deep, timeout, progress)
        except BaseException:
            owner.cleanup()
            raise
        for row in result:
            row._desktop_owner = owner
        return result
    if not root.is_dir(): raise RuntimeError("请选择支持的缓存目录或 Desktop 密钥文件")
    dirs = [root] if root.name == "media" else media_dirs(root)
    if not dirs:
        # Explicit flat fixture or custom cache directory.
        dirs = [root]
    paths = [Path(e.path) for d in dirs for e in os.scandir(d)
             if e.is_file() and not e.name.endswith(".meta")]
    def identify(p): return p, signature(p)
    progress(0, len(paths), "识别媒体格式")
    with ThreadPoolExecutor(max_workers=8) as pool:
        candidates = [(p, k) for p, k in pool.map(identify, paths) if k]
    rows = []
    # Keep this serial to avoid saturating the machine with decoders.
    for i, (p, kind) in enumerate(candidates):
        progress(i, len(candidates), p.name)
        try:
            item = inspect(p, kind, deep, timeout)
            if item: rows.append(item)
        except OSError:
            continue  # Telegram can evict files during a scan.
    grouped = {}
    rank = {"ready": 0, "covered": 1, "unknown": 2, "partial": 3, "invalid": 4}
    for row in rows:
        key = (str(row.path.parent), row.key)
        old = grouped.get(key)
        if old is None: grouped[key] = row
        elif rank[row.status] < rank[old.status]:
            row.copies += old.copies
            grouped[key] = row
        else: old.copies += 1
    result = sorted(grouped.values(), key=lambda r: (rank[r.status], -r.size, r.path.name))
    progress(len(candidates), len(candidates), f"完成 · {len(result)} 项，合并 {len(rows)-len(result)} 份重复缓存")
    return result


def publish_temp(temp, target):
    """Atomically publish without replacing an existing file."""
    if os.name == 'nt':
        # Windows rename refuses an existing destination and works on filesystems
        # such as exFAT that do not support hard links.
        os.rename(temp, target)
    else:
        os.link(temp, target)


def export(item, output, fmt="mp4", timeout=600):
    if fmt not in ("mp4", "mkv", "original"): raise ValueError("不支持的导出格式")
    # Always recheck: a prior report is not authority to export changed bytes.
    fresh = inspect(item.path, deep=True, timeout=timeout)
    if not fresh or fresh.status != "ready": raise RuntimeError("此文件未确认完整，已停止导出。请重新扫描。")
    output = Path(output).expanduser()
    if output.resolve().is_relative_to(fresh.path.parent.resolve()):
        raise RuntimeError("请选择缓存目录以外的导出位置")
    output.mkdir(parents=True, exist_ok=True)
    ext = fresh.kind if fmt == "original" else fmt
    base = re.sub(r"[^\w.-]", "_", fresh.key)
    target = output / f"{base}.{ext}"
    n = 2
    while target.exists():
        target = output / f"{base}-{n}.{ext}"
        n += 1
    import tempfile
    fd, tempname = tempfile.mkstemp(prefix=".export-", suffix=f".{ext}", dir=output)
    os.close(fd)
    temp = Path(tempname)
    try:
        if fmt == "original": shutil.copyfile(fresh.path, temp)
        else:
            command = [binary("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", str(fresh.path),
                       "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "-1"]
            codec = ["-c", "copy"]
            if fmt == "mp4" and (fresh.codec not in ("h264", "hevc", "mpeg4", "av1") or fresh.audio not in ("", "aac", "mp3", "alac")):
                codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                         "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-threads", "2", "-c:a", "aac"]
            run(command+codec+(["-movflags", "+faststart"] if fmt == "mp4" else [])+[str(temp)], timeout)
        # Check the produced file rather than trusting an ffmpeg exit status alone.
        checked = inspect(temp, deep=True, timeout=timeout)
        if not checked or checked.status == "invalid": raise RuntimeError("输出文件未通过解码检查")
        if stamp(fresh.path) != fresh.fingerprint: raise RuntimeError("导出期间缓存发生变化，请重新扫描")
        while True:
            try:
                publish_temp(temp, target)
                break
            except FileExistsError:
                target = output / f"{base}-{n}.{ext}"
                n += 1
        item.status, item.detail = fresh.status, fresh.detail
        item.fingerprint = fresh.fingerprint
        return target
    finally:
        temp.unlink(missing_ok=True)

def open_path(target):
    if sys.platform == "darwin": subprocess.run(["open", str(target)], check=True)
    elif os.name == "nt": os.startfile(str(target))
    else: subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def preview(item, output, timeout=600):
    target = export(item, output, "mp4", timeout)
    # Use the user's default player. Keep its input until the app exits.
    open_path(target)
    return target
