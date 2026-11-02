"""Recover independently decodable clips from verified MediaBox ranges."""
from dataclasses import dataclass
from pathlib import Path
import json
import math
import os
import shutil
import struct
import tempfile

from . import core


@dataclass(frozen=True)
class Clip:
    path: Path
    start: float
    length: float


def ranges_for(stamp):
    size, data = stamp[2], stamp[4]
    if data is None:
        raise ValueError('没有可验证的缓存索引')
    core.read_map(data, size)
    ranges = []
    for a, length in sorted(struct.iter_unpack('<qq', data[20:20+16*struct.unpack_from('<i', data, 8)[0]])):
        b = min(size, a + length)
        if b <= a:
            continue
        if ranges and a <= ranges[-1][1]:
            ranges[-1] = ranges[-1][0], max(ranges[-1][1], b)
        else:
            ranges.append((a, b))
    return ranges


def copy_range(source, dest, start, end):
    source.seek(start)
    remaining = end - start
    while remaining:
        block = source.read(min(remaining, 1024 * 1024))
        if not block:
            raise RuntimeError('缓存文件在读取期间缩短')
        dest.write(block)
        remaining -= len(block)


def boxes(source, start, end):
    while start + 8 <= end:
        source.seek(start)
        size, kind = struct.unpack('>I4s', source.read(8))
        header = 8
        if size == 1:
            if start + 16 > end:
                return
            size = struct.unpack('>Q', source.read(8))[0]
            header = 16
        if size < header or start + size > end:
            return
        yield kind, start, start + size
        start += size


def probe(path, fields, timeout):
    options = ['-show_data_hash', 'sha256'] if 'data_hash' in fields else []
    raw, _ = core.run([core.binary('ffprobe'), '-v', 'error', *options, '-show_entries', fields,
                       '-of', 'json', str(path)], timeout)
    return json.loads(raw)


def checked_clip(source, dest, start, duration, timeout):
    cmd = [core.binary('ffmpeg'), '-nostdin', '-v', 'error']
    if start is not None:
        cmd += ['-ss', str(start)]
    cmd += ['-i', str(source)]
    if duration is not None:
        cmd += ['-t', str(duration)]
    core.run(cmd + ['-map', '0:V:0', '-map', '0:a?', '-c', 'copy', '-map_metadata', '-1',
                    '-avoid_negative_ts', 'make_zero', '-movflags', '+faststart', str(dest)], timeout)
    info = core.inspect(dest, deep=True, timeout=timeout)
    if not info or info.status != 'unknown' or not info.duration:
        raise ValueError('片段未通过完整解码')
    return info.duration


def recover(row, directory, timeout=600):
    before = core.stamp(row.path)
    if before != row.fingerprint or row.status != 'partial':
        raise RuntimeError('缓存状态已变化，请重新扫描')
    ranges = ranges_for(before)
    clips = []
    with tempfile.TemporaryDirectory(dir=directory) as work:
        work = Path(work)
        with row.path.open('rb') as source:
            first = list(boxes(source, 0, ranges[0][1])) if ranges and ranges[0][0] == 0 and row.kind == 'mp4' else []
            init_end = next((b for k, a, b in first if k == b'moov'), 0)
            fragmented = any(k == b'moof' for k, a, b in first)
            if init_end and not fragmented:
                fragmented = any(next(boxes(source, a, b), (None,))[0] == b'moof' for a, b in ranges[1:])
            if fragmented and init_end:
                for a, b in ranges:
                    previous = None
                    for kind, begin, end in boxes(source, a, b):
                        if kind == b'mdat' and previous and previous[0] == b'moof':
                            candidate = work/'fragment.mp4'
                            with candidate.open('wb') as out:
                                copy_range(source, out, 0, init_end)
                                copy_range(source, out, previous[1], end)
                            target = work/f'clip-{len(clips)}.mp4'
                            try:
                                streams = probe(candidate, 'stream=codec_type,start_time', timeout)['streams']
                                start = float(next(s for s in streams if s['codec_type']=='video')['start_time'])
                                length = checked_clip(candidate, target, None, None, timeout)
                                clips.append(Clip(target, start, length))
                            except (RuntimeError, ValueError, KeyError, StopIteration):
                                target.unlink(missing_ok=True)
                        previous = kind, begin
            else:
                # Preserve original offsets so the demuxer can use a front or trailing sample table.
                masked = work/'cached-media'
                with masked.open('wb') as out:
                    out.truncate(before[2])
                    for a, b in ranges:
                        out.seek(a)
                        copy_range(source, out, a, b)
                try:
                    fields = 'format=start_time:stream=index,codec_type:packet=stream_index,pos,size,pts_time,duration_time,flags,data_hash'
                    info = probe(masked, fields, timeout)
                except RuntimeError:
                    info = {}
                types = {s['index']: s['codec_type'] for s in info.get('streams', [])}
                first_video = next((i for i, t in types.items() if t == 'video'), None)
                origin = float(info.get('format', {}).get('start_time') or 0)
                available_hashes = set()
                runs = []
                active = None
                from bisect import bisect_right
                starts = [a for a, _ in ranges]
                for packet in info.get('packets', []):
                    stream = packet['stream_index']
                    if stream != first_video and types.get(stream) != 'audio':
                        continue
                    pos, size = int(packet.get('pos', -1)), int(packet.get('size', 0))
                    idx = bisect_right(starts, pos)-1
                    if idx < 0 or size <= 0 or pos+size > ranges[idx][1]:
                        active = None
                        continue
                    if 'data_hash' in packet:
                        available_hashes.add(packet['data_hash'])
                    if stream != first_video or 'pts_time' not in packet:
                        continue
                    pts = float(packet['pts_time'])
                    if active is None and 'K' in packet.get('flags', ''):
                        active = [pts, pts]
                        runs.append(active)
                    if active is not None:
                        active[1] = max(active[1], pts+float(packet.get('duration_time') or 0))
                for start, end in runs:
                    if end <= start or not math.isfinite(end-start):
                        continue
                    target = work/f'clip-{len(clips)}.mp4'
                    try:
                        length = checked_clip(masked, target, max(0, start-origin), end-start, timeout)
                        packets = probe(target, 'packet=data_hash', timeout).get('packets', [])
                        if not packets or any(p.get('data_hash') not in available_hashes for p in packets):
                            raise ValueError('片段引用了未缓存的数据包')
                        clips.append(Clip(target, start-origin, length))
                    except (ValueError, RuntimeError):
                        target.unlink(missing_ok=True)
        if core.stamp(row.path) != before:
            raise RuntimeError('缓存已变化，请重新扫描')
        result = []
        for clip in clips:
            fd, name = tempfile.mkstemp(dir=directory, suffix='.mp4')
            os.close(fd)
            clip.path.replace(name)
            result.append(Clip(Path(name), clip.start, clip.length))
        return result


def export_clips(row, clips, output, timeout):
    output = Path(output)
    if output.resolve().is_relative_to(row.path.parent.resolve()):
        raise RuntimeError('请选择缓存目录以外的导出位置')
    output.mkdir(parents=True, exist_ok=True)
    paths, errors = [], []
    for clip in clips:
        try:
            if core.stamp(row.path) != row.fingerprint:
                raise RuntimeError('源缓存已变化，请重新分析')
            with tempfile.NamedTemporaryFile(dir=output, suffix='.mp4') as tmp:
                shutil.copyfile(clip.path, tmp.name)
                checked = core.inspect(tmp.name, deep=True, timeout=timeout)
                if not checked or checked.status != 'unknown' or not checked.duration:
                    raise RuntimeError('片段解码验证失败')
                if core.stamp(row.path) != row.fingerprint:
                    raise RuntimeError('导出期间源缓存发生变化')
                base = f'{row.key}-partial-{clip.start:.3f}s-{clip.start+clip.length:.3f}s'
                n = 1
                while True:
                    target = output/(base + (f'-{n}' if n > 1 else '')+'.mp4')
                    try:
                        os.link(tmp.name, target)
                        break
                    except FileExistsError:
                        n += 1
                paths.append(target)
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(f'{clip.start:.3f}s · {exc}')
    return paths, errors
