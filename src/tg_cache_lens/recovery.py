"""Recover independently decodable clips from verified MediaBox ranges."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import json
import math
import os
import shutil
import struct
import tempfile
import subprocess
import time
from itertools import islice

from . import core
from .sparse import prepare_sparse


@dataclass(frozen=True)
class Clip:
    path: Path
    start: float
    length: float
    digest: bytes | None = None
    audio: bool | None = None


class CachedMediaInvalid(RuntimeError):
    """A cached video needs fragment recovery before it can be exported."""


MAX_RECOVERY_RUN_SECONDS = 15.0


def clip_digest(path):
    digest = sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.digest()


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


def fragment_boxes(source, start, end):
    """Find complete moof/mdat pairs even when a cached range starts mid-box."""
    position = start
    carry = b''
    last = -1
    while position < end:
        source.seek(position)
        block = source.read(min(1024 * 1024, end - position))
        if not block:
            break
        data = carry + block
        offset = position - len(carry)
        cursor = 0
        while True:
            found = data.find(b'moof', cursor)
            if found < 0:
                break
            cursor = found + 4
            begin = offset + found - 4
            if begin < start or begin <= last:
                continue
            pair = list(islice(boxes(source, begin, end), 2))
            if len(pair) == 2 and pair[0][0] == b'moof' and pair[1][0] == b'mdat':
                last = begin
                yield pair[0][1], pair[1][2]
        carry = data[-7:]
        position += len(block)


def probe(path, fields, timeout):
    options = ['-show_data_hash', 'sha256'] if 'data_hash' in fields else []
    raw, _ = core.run([core.binary('ffprobe'), '-v', 'error', *options, '-show_entries', fields,
                       '-of', 'json', str(path)], timeout)
    return json.loads(raw)


def stream_packets(path, timeout, progress):
    """Read packet records as ffprobe writes them, without a Windows pipe."""
    command = [core.binary('ffprobe'), '-v', 'error', '-show_optional_fields', 'always', '-show_data_hash', 'sha256',
               '-show_entries', 'packet=stream_index,pos,size,pts_time,duration_time,flags,data_hash',
               '-of', 'compact=p=0:nk=1', str(path)]
    with tempfile.TemporaryDirectory(prefix='cache-lens-probe-') as folder:
        output = Path(folder) / 'packets'
        errors = Path(folder) / 'errors'
        with output.open('wb') as writer, errors.open('wb') as stderr:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=writer, stderr=stderr,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            deadline = time.monotonic() + timeout
            try:
                with output.open('rb') as reader:
                    pending = b''
                    while True:
                        progress(0, 0, '分析片段')
                        data = reader.read(65536)
                        if data:
                            pending += data
                            lines = pending.split(b'\n')
                            pending = lines.pop()
                            for line in lines:
                                values = line.decode('utf-8', errors='replace').rstrip('\r').split('|')
                                if not values[0].isdigit():
                                    continue
                                if len(values) < 7:
                                    raise ValueError('媒体数据包信息不完整')
                                names = ('stream_index', 'pts_time', 'duration_time', 'size', 'pos', 'flags', 'data_hash')
                                fields = {key: value for key, value in zip(names, values) if value and value != 'N/A'}
                                fields['stream_index'] = int(fields['stream_index'])
                                yield fields
                            continue
                        if process.poll() is not None:
                            if process.returncode:
                                raise RuntimeError('读取媒体数据包失败')
                            break
                        if time.monotonic() >= deadline:
                            raise RuntimeError('处理超时；可通过 --timeout 增加时限')
                        time.sleep(.05)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()


def checked_clip(source, dest, start, duration, timeout, include_audio=True):
    cmd = [core.binary('ffmpeg'), '-nostdin', '-v', 'error']
    if start is not None:
        cmd += ['-ss', str(start)]
    cmd += ['-i', str(source)]
    if duration is not None:
        cmd += ['-t', str(duration)]
    streams = ['-map', '0:V:0'] + (['-map', '0:a?'] if include_audio else ['-an'])
    core.run(cmd + streams + ['-c', 'copy', '-map_metadata', '-1',
                    '-avoid_negative_ts', 'make_zero', '-movflags', '+faststart', str(dest)], timeout)
    info = core.inspect(dest, deep=True, timeout=timeout)
    if not info or info.status != 'unknown' or not info.duration:
        raise ValueError('片段未通过完整解码')
    return info.duration, bool(info.audio)


def validate_work_location(row, directory):
    desktop_source = getattr(row, 'desktop_source', None)
    source_root = desktop_source.database.parent.parent if desktop_source else row.path.parent
    if Path(directory).resolve().is_relative_to(source_root.resolve()):
        raise RuntimeError('请选择缓存目录以外的位置')


def prepare_output(row, output):
    output = Path(output).expanduser()
    validate_work_location(row, output)
    try:
        output.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise RuntimeError(f'无法访问导出目录：{output}。请选择其他可写目录。') from exc
    return output


def can_export_cached(row):
    return (bool(getattr(row, 'desktop_source', None)) and row.status == 'unknown'
            and ranges_for(row.fingerprint) == [(0, row.size)])


def export_cached(row, output, timeout=600, fmt='mp4', progress=lambda *args: None):
    """Export the continuous cached bytes, explicitly without claiming completeness."""
    if not can_export_cached(row):
        raise RuntimeError('缓存范围不连续，请先分析片段')
    if fmt not in ('mp4', 'mkv', 'original'):
        raise ValueError('不支持的导出格式')
    output = prepare_output(row, output)
    source = row.desktop_source
    progress(0, 0, '导出 · 检查缓存源')
    source.revalidate()
    if core.stamp(row.path) != row.fingerprint:
        raise RuntimeError('源缓存已变化，请重新扫描')
    try:
        with tempfile.TemporaryDirectory(prefix='.cache-lens-', dir=output) as work:
            ext = row.kind if fmt == 'original' else fmt
            candidate = Path(work)/f'cached.{ext}'
            progress(0, 0, '导出 · 生成视频')
            if fmt == 'original':
                shutil.copyfile(row.path, candidate)
            else:
                if fmt == 'mp4' and (row.codec not in ('h264', 'hevc', 'mpeg4', 'av1')
                                     or row.audio not in ('', 'aac', 'mp3', 'alac')):
                    command = [core.binary('ffmpeg'), '-nostdin', '-v', 'error', '-xerror',
                               '-err_detect', 'explode', '-i', str(row.path), '-map', '0:V:0', '-map', '0:a?',
                               '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
                               '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', '-threads', '2', '-c:a', 'aac',
                               '-movflags', '+faststart', str(candidate)]
                else:
                    command = [core.binary('ffmpeg'), '-nostdin', '-v', 'error', '-i', str(row.path),
                               '-map', '0:V:0', '-map', '0:a?', '-c', 'copy', '-map_metadata', '-1']
                    command += (['-movflags', '+faststart'] if fmt == 'mp4' else []) + [str(candidate)]
                try:
                    core.run(command, timeout, lambda percent: progress(percent, 100, '导出 · 生成视频'), row.duration)
                except RuntimeError as exc:
                    raise CachedMediaInvalid('缓存视频无法直接导出') from exc
            progress(0, 0, '导出 · 验证视频')
            checked = core.inspect(candidate, deep=True, timeout=timeout)
            if not checked or checked.status != 'unknown' or not checked.duration:
                raise CachedMediaInvalid('已缓存内容未通过解码，无法直接导出；请尝试分析片段')
            if core.stamp(row.path) != row.fingerprint:
                raise RuntimeError('导出期间源缓存发生变化')
            source.revalidate()
            n = 1
            while True:
                target = output/(row.key + '-cached-unknown' + (f'-{n}' if n > 1 else '') + f'.{ext}')
                try:
                    core.publish_temp(candidate, target)
                    return target
                except FileExistsError:
                    n += 1
    except PermissionError as exc:
        raise RuntimeError(f'无法写入导出目录：{output}。请选择其他可写目录。') from exc


def recover(row, directory, timeout=600, progress=lambda *args: None, on_clip=None):
    progress(0, 0, '分析片段')
    validate_work_location(row, directory)
    desktop_source = getattr(row, 'desktop_source', None)
    before = core.stamp(row.path)
    if before != row.fingerprint or (row.status != 'partial' and not (desktop_source and row.status == 'unknown')):
        raise RuntimeError('缓存状态已变化，请重新扫描')
    ranges = ranges_for(before)
    clips = []
    result = []

    def record(clip):
        if on_clip is None:
            clips.append(clip)
            return
        progress(0, 0, '分析片段')
        fd, name = tempfile.mkstemp(dir=directory, suffix='.mp4')
        os.close(fd)
        try:
            clip.path.replace(name)
            saved = Clip(Path(name), clip.start, clip.length, clip_digest(name), clip.audio)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
        result.append(saved)
        on_clip(saved)
    with tempfile.TemporaryDirectory(dir=directory) as work:
        work = Path(work)
        with row.path.open('rb') as source:
            first = list(boxes(source, 0, ranges[0][1])) if ranges and ranges[0][0] == 0 and row.kind == 'mp4' else []
            init_end = next((b for k, a, b in first if k == b'moov'), 0)
            fragmented = any(k == b'moof' for k, a, b in first)
            if init_end and not fragmented:
                fragmented = any(next(fragment_boxes(source, a, b), None) for a, b in ranges[1:])
            if fragmented and init_end:
                for a, b in ranges:
                    progress(0, 0, '分析片段')
                    for begin, end in fragment_boxes(source, a, b):
                        progress(0, 0, '分析片段')
                        candidate = work/'fragment.mp4'
                        with candidate.open('wb') as out:
                            copy_range(source, out, 0, init_end)
                            copy_range(source, out, begin, end)
                        target = work/f'clip-{len(clips) + len(result)}.mp4'
                        try:
                            streams = probe(candidate, 'stream=codec_type,start_time', timeout)['streams']
                            start = float(next(s for s in streams if s['codec_type']=='video')['start_time'])
                            length, has_audio = checked_clip(candidate, target, None, None, timeout)
                            record(Clip(target, start, length, audio=has_audio))
                        except (RuntimeError, ValueError, KeyError, StopIteration):
                            target.unlink(missing_ok=True)
            else:
                # Preserve original offsets so the demuxer can use a front or trailing sample table.
                if desktop_source:
                    # Desktop snapshots already contain only verified ranges;
                    # their holes are sparse zeros. Avoid copying them again.
                    masked = row.path
                else:
                    masked = work/'cached-media'
                    with masked.open('wb') as out:
                        if ranges and (ranges[0][0] or len(ranges) > 1):
                            prepare_sparse(out)
                        out.truncate(before[2])
                        for a, b in ranges:
                            out.seek(a)
                            copy_range(source, out, a, b)
                try:
                    info = probe(masked, 'format=start_time:stream=index,codec_type', timeout)
                except RuntimeError:
                    info = {}
                types = {s['index']: s['codec_type'] for s in info.get('streams', [])}
                first_video = next((i for i, t in types.items() if t == 'video'), None)
                origin = float(info.get('format', {}).get('start_time') or 0)
                available_hashes = set()
                active = None
                from bisect import bisect_right
                starts = [a for a, _ in ranges]

                def recover_run(item):
                    number, (start, end) = item
                    progress(0, 0, '分析片段')
                    if end <= start or not math.isfinite(end-start):
                        return None
                    target = work/f'clip-{number}.mp4'
                    span = end - start
                    attempts = [(True, span), (False, span)]
                    if span > 0.6:
                        attempts.append((False, span - 0.5))
                    for with_audio, clip_span in attempts:
                        progress(0, 0, '分析片段')
                        try:
                            length, has_audio = checked_clip(masked, target, max(0, start-origin), clip_span, timeout, with_audio)
                            packets = probe(target, 'packet=data_hash', timeout).get('packets', [])
                            if not packets or any(bytes.fromhex(p.get('data_hash', '').removeprefix('SHA256:')) not in available_hashes for p in packets):
                                raise ValueError('片段引用了未缓存的数据包')
                            return Clip(target, start-origin, length, audio=has_audio)
                        except (ValueError, RuntimeError):
                            target.unlink(missing_ok=True)
                    return None

                with ThreadPoolExecutor(max_workers=3) as pool:
                    run_with_cancel = core.propagate_cancel(recover_run)
                    pending = set()
                    deferred = None
                    number = 0
                    def collect(block=False):
                        nonlocal pending
                        if not pending: return
                        ready, pending = wait(pending, timeout=.2 if block else 0, return_when=FIRST_COMPLETED)
                        for future in ready:
                            clip = future.result()
                            if clip: record(clip)
                    def queue(run):
                        nonlocal deferred, number
                        if run is None: return
                        # One completed run of lookahead lets interleaved audio
                        # and reordered video packets reach the source hash set.
                        if deferred is not None:
                            while len(pending) >= 6:
                                progress(0, 0, '分析片段')
                                collect(True)
                            pending.add(pool.submit(run_with_cancel, (number, deferred)))
                            number += 1
                        deferred = tuple(run)
                    try:
                        if first_video is not None:
                            for packet in stream_packets(masked, timeout, progress):
                                collect()
                                stream = packet['stream_index']
                                if stream != first_video and types.get(stream) != 'audio':
                                    continue
                                pos, size = int(packet.get('pos', -1)), int(packet.get('size', 0))
                                idx = bisect_right(starts, pos)-1
                                if idx < 0 or size <= 0 or pos+size > ranges[idx][1]:
                                    if active is not None:
                                        queue(active)
                                        active = None
                                    continue
                                if 'data_hash' in packet:
                                    available_hashes.add(bytes.fromhex(packet['data_hash'].removeprefix('SHA256:')))
                                if stream != first_video or 'pts_time' not in packet:
                                    continue
                                pts = float(packet['pts_time'])
                                if (active is not None and 'K' in packet.get('flags', '')
                                        and pts - active[0] >= MAX_RECOVERY_RUN_SECONDS):
                                    active[1] = max(active[0], min(active[1], pts))
                                    queue(active)
                                    active = None
                                if active is None and 'K' in packet.get('flags', ''):
                                    active = [pts, pts]
                                if active is not None:
                                    active[1] = max(active[1], pts+float(packet.get('duration_time') or 0))
                        queue(active)
                        if deferred is not None:
                            pending.add(pool.submit(run_with_cancel, (number, deferred)))
                        while pending:
                            progress(0, 0, '分析片段')
                            collect(True)
                    finally:
                        for future in pending: future.cancel()
        if core.stamp(row.path) != before:
            raise RuntimeError('缓存已变化，请重新扫描')
        if desktop_source:
            desktop_source.revalidate()
        for clip in clips:
            fd, name = tempfile.mkstemp(dir=directory, suffix='.mp4')
            os.close(fd)
            clip.path.replace(name)
            result.append(Clip(Path(name), clip.start, clip.length, clip_digest(name), clip.audio))
        return sorted(result, key=lambda clip: clip.start)


def export_merged_clips(row, clips, output, timeout, progress=lambda *args: None):
    """Merge selected verified clips in their original time order."""
    if not clips:
        return [], []
    output = prepare_output(row, output)
    clips = sorted(clips, key=lambda clip: clip.start)
    source = getattr(row, 'desktop_source', None)
    stage = '读取缓存源'
    try:
        progress(0, 0, '导出 · 检查缓存源')
        if source:
            source.revalidate()
        if core.stamp(row.path) != row.fingerprint:
            raise RuntimeError('源缓存已变化，请重新分析')
        stage = '创建导出临时文件'
        with tempfile.TemporaryDirectory(prefix='.cache-lens-', dir=output) as work:
            work = Path(work)
            for clip in clips:
                stage = '读取已分析片段'
                if clip.digest is None or clip_digest(clip.path) != clip.digest:
                    raise RuntimeError('片段内容已变化，请重新分析')
            manifest = work/'concat.txt'
            def concat_path(path):
                return str(Path(path).resolve().as_posix()).replace("'", "'\\''")
            manifest.write_text(''.join(f"file '{concat_path(clip.path)}'\n" for clip in clips), encoding='utf-8')
            merged = work/'merged.mp4'
            stage = '合并片段'
            progress(0, 0, '导出 · 合并片段')
            has_audio = [clip.audio if clip.audio is not None else any(
                s.get('codec_type') == 'audio' for s in probe(clip.path, 'stream=codec_type', timeout).get('streams', []))
                for clip in clips]
            def encode_clips():
                command = [core.binary('ffmpeg'), '-nostdin', '-v', 'error']
                for clip in clips:
                    command += ['-i', str(clip.path)]
                filters, inputs = [], []
                audio = any(has_audio)
                for index, clip in enumerate(clips):
                    filters.append(f'[{index}:v:0]setpts=PTS-STARTPTS[v{index}]')
                    inputs.append(f'[v{index}]')
                    if audio:
                        if has_audio[index]:
                            filters.append(f'[{index}:a:0]aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS[a{index}]')
                        else:
                            filters.append(f'anullsrc=r=44100:cl=stereo,atrim=duration={clip.length:.6f}[a{index}]')
                        inputs.append(f'[a{index}]')
                filters.append(''.join(inputs) + f'concat=n={len(clips)}:v=1:a={int(audio)}[v]' + ('[a]' if audio else ''))
                command += ['-filter_complex', ';'.join(filters), '-map', '[v]']
                if audio: command += ['-map', '[a]']
                command += ['-c:v', 'libx264', '-preset', 'veryfast', '-threads', str(min(8, os.cpu_count() or 2))]
                if audio: command += ['-c:a', 'aac']
                core.run(command + ['-movflags', '+faststart', str(merged)], timeout,
                         lambda percent: progress(percent, 100, '导出 · 合并片段'), sum(c.length for c in clips))

            checked = None
            if not (any(has_audio) and not all(has_audio)):
                command = [core.binary('ffmpeg'), '-nostdin', '-v', 'error', '-f', 'concat', '-safe', '0',
                           '-i', str(manifest), '-map', '0:v:0', '-map', '0:a?', '-c', 'copy',
                           '-movflags', '+faststart', str(merged)]
                try:
                    core.run(command, timeout, lambda percent: progress(percent, 100, '导出 · 合并片段'), sum(c.length for c in clips))
                    progress(0, 0, '导出 · 验证视频')
                    checked = core.inspect(merged, deep=True, timeout=timeout)
                except RuntimeError:
                    pass
            if not checked or checked.status != 'unknown' or not checked.duration:
                merged.unlink(missing_ok=True)
                encode_clips()
                progress(0, 0, '导出 · 验证视频')
                checked = core.inspect(merged, deep=True, timeout=timeout)
            if not checked or checked.status != 'unknown' or not checked.duration:
                raise RuntimeError('合并结果未通过解码检查')
            if core.stamp(row.path) != row.fingerprint:
                raise RuntimeError('导出期间源缓存发生变化')
            stage = '复验缓存源'
            if source:
                source.revalidate()
            base = f'{row.key}-partial-{clips[0].start:.3f}s-{clips[-1].start+clips[-1].length:.3f}s'
            n = 1
            while True:
                target = output/(base + (f'-{n}' if n > 1 else '') + '.mp4')
                try:
                    stage = '写入导出目录'
                    core.publish_temp(merged, target)
                    return [target], []
                except FileExistsError:
                    n += 1
    except PermissionError as exc:
        return [], [f'{stage}时访问被拒绝：{exc}']
    except (OSError, ValueError, RuntimeError) as exc:
        return [], [str(exc)]


def export_clips(row, clips, output, timeout):
    output = prepare_output(row, output)
    desktop_source = getattr(row, 'desktop_source', None)
    paths, errors = [], []
    for clip in clips:
        stage = '读取缓存源'
        try:
            if desktop_source:
                desktop_source.revalidate()
            if core.stamp(row.path) != row.fingerprint:
                raise RuntimeError('源缓存已变化，请重新分析')
            stage = '创建导出临时文件'
            fd, tmpname = tempfile.mkstemp(dir=output, suffix='.mp4')
            os.close(fd)
            try:
                stage = '读取已分析片段'
                shutil.copyfile(clip.path, tmpname)
                if clip.digest is None or clip_digest(tmpname) != clip.digest:
                    raise RuntimeError('片段内容已变化，请重新分析')
                checked = core.inspect(tmpname, deep=True, timeout=timeout)
                if not checked or checked.status != 'unknown' or not checked.duration:
                    raise RuntimeError('片段解码验证失败')
                if core.stamp(row.path) != row.fingerprint:
                    raise RuntimeError('导出期间源缓存发生变化')
                stage = '复验缓存源'
                if desktop_source:
                    desktop_source.revalidate()
                base = f'{row.key}-partial-{clip.start:.3f}s-{clip.start+clip.length:.3f}s'
                n = 1
                while True:
                    target = output/(base + (f'-{n}' if n > 1 else '')+'.mp4')
                    try:
                        stage = '写入导出目录'
                        core.publish_temp(tmpname, target)
                        break
                    except FileExistsError:
                        n += 1
                paths.append(target)
            finally:
                Path(tmpname).unlink(missing_ok=True)
        except PermissionError as exc:
            errors.append(f'{clip.start:.3f}s · {stage}时访问被拒绝：{exc}')
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(f'{clip.start:.3f}s · {exc}')
    return paths, errors
