"""Read-only cache inspection. No Telegram credentials or network access."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import re
import shutil
import struct
import subprocess
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
    def key(self):
        return re.sub(r"(_partial|\.(mp4|mkv|webm|mov))$", "", self.path.name)

    @property
    def coverage(self):
        return f"{min(100, 100*self.cached/self.total):.1f}%" if self.total and self.cached is not None else "—"

def default_root():
    return Path.home() / "Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram"

def default_output():
    return Path.home() / "Downloads/Telegram Exports"

def binary(name):
    path = shutil.which(name)
    if not path:
        for root in ("/opt/homebrew/bin", "/usr/local/bin"):
            if (Path(root)/name).is_file(): return str(Path(root)/name)
        raise RuntimeError(f"找不到 {name}。请先运行 brew install ffmpeg")
    return path

def run(command, timeout=30):
    try:
        p = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("处理超时；可通过 --timeout 增加时限") from e
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[-700:] or "媒体工具处理失败")
    return p.stdout, p.stderr

def signature(path):
    try:
        with path.open("rb") as f: b = f.read(16)
        if b[4:8] == b"ftyp": return "mp4"
        if b[:4] == b"\x1aE\xdf\xa3": return "mkv"
        if b[:4] == b"RIFF" and b[8:12] == b"AVI ": return "avi"
    except OSError:
        pass
    return None

def read_map(data):
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
        cached += max(0, start+length-max(start, end))
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
            item.total, item.cached, gap, end = read_map(before[4])
        except ValueError as e:
            item.status, item.detail = "invalid", str(e)
            return item
        if gap or end > item.size:
            item.status, item.detail = "partial", "缓存索引存在缺口，不能导出完整视频"
        else:
            verified_map = item.total is not None and item.total > 0
    try:
        raw, _ = run([binary("ffprobe"), "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height", "-of", "json", str(path)], 15)
        info = json.loads(raw)
        streams = info.get("streams", [])
        videos = [s for s in streams if s.get("codec_type") == "video"]
        if not videos: return None
        v = videos[0]
        item.width, item.height, item.codec = v.get("width", 0), v.get("height", 0), v.get("codec_name", "")
        item.audio = next((s.get("codec_name", "") for s in streams if s.get("codec_type") == "audio"), "")
        duration = info.get("format", {}).get("duration")
        item.duration = float(duration) if duration else None
        if item.status != "partial":
            if deep:
                _, errors = run([binary("ffmpeg"), "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode",
                    "-threads", "2", "-i", str(path), "-map", "0:v", "-map", "0:a?", "-f", "null", "-"], timeout)
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

def scan(root, deep=False, progress=lambda *a: None, timeout=600):
    root = Path(root).expanduser()
    if not root.is_dir(): raise RuntimeError("缓存目录不存在；请用 --cache 指定 media 文件夹")
    binary("ffmpeg"); binary("ffprobe")
    dirs = [root] if root.name == "media" else sorted(root.glob("*/account-*/postbox/media"))
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

def export(item, output, fmt="mp4", timeout=600):
    if fmt not in ("mp4", "mkv", "original"): raise ValueError("不支持的导出格式")
    # Always recheck: a prior report is not authority to export changed bytes.
    fresh = inspect(item.path, deep=True, timeout=timeout)
    if not fresh or fresh.status != "ready": raise RuntimeError("此文件未确认完整，已停止导出。请重新扫描。")
    output = Path(output).expanduser()
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
        # Hard link is atomic and refuses to overwrite an existing destination.
        os.link(temp, target)
        item.status, item.detail = fresh.status, fresh.detail
        item.fingerprint = fresh.fingerprint
        return target
    finally:
        temp.unlink(missing_ok=True)

def preview(item, output, timeout=600):
    target = export(item, output, "mp4", timeout)
    # Use the user's default player. Keep its input until the app exits.
    import sys
    if sys.platform == "darwin": subprocess.run(["open", str(target)], check=True)
    elif os.name == "nt": os.startfile(str(target))
    else: subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return target
