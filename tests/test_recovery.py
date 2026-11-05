from pathlib import Path
import struct
import subprocess
import pytest

from tg_cache_lens import core, recovery
from conftest import write_map


def broken_movie(tmp_path, fragmented=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    file = tmp_path/'missing.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=12',
                    '-t','6','-c:v','libx264','-g','24','-keyint_min','24','-sc_threshold','0','-bf','0',
                    '-movflags','+frag_keyframe+empty_moov+default_base_moof' if fragmented else '+faststart',
                    str(file)],check=True)
    data = bytearray(file.read_bytes())
    if fragmented:
        with file.open('rb') as source:
            frags = [a for k,a,b in recovery.boxes(source,0,len(data)) if k == b'moof']
        a,b = frags[1:3]
    else:
        info = recovery.probe(file,'packet=pos,flags',30)
        keys = [int(p['pos']) for p in info['packets'] if 'K' in p['flags']]
        a,b = keys[1:3]
    data[a:b] = bytes(b-a)
    file.write_bytes(data)
    write_map(file,len(data),[(0,a),(b,len(data)-b)])
    return core.inspect(file)


def test_recovery_preserves_gap_and_rechecks_source(tmp_path):
    row = broken_movie(tmp_path/'cache')
    tmp = tmp_path/'tmp'; tmp.mkdir()
    clips = recovery.recover(row,tmp)
    assert [(c.start, c.length) for c in clips] == [(0,2),(4,2)]
    out = tmp_path.parent/(tmp_path.name+'-exports')
    paths, errors = recovery.export_clips(row,clips,out,30)
    assert not errors and len(paths)==2
    assert all(core.inspect(path,deep=True).duration == 2 for path in paths)
    again, errors = recovery.export_clips(row,clips[:1],out,30)
    assert not errors and again[0] != paths[0]
    Path(str(row.path)+'.meta').unlink()
    paths, errors = recovery.export_clips(row,clips,out,30)
    assert not paths and len(errors)==2


def test_regular_faststart_mp4_recovers_after_gap(tmp_path):
    row = broken_movie(tmp_path/'cache',False)
    clips = recovery.recover(row,tmp_path)
    assert [(c.start,c.length) for c in clips] == [(0,2),(4,2)]


def test_merge_selected_clips_in_time_order_without_overwrite(tmp_path):
    row = broken_movie(tmp_path/'cache')
    work = tmp_path/'work'; work.mkdir()
    clips = recovery.recover(row, work)
    output = tmp_path/'output'
    paths, errors = recovery.export_merged_clips(row, list(reversed(clips)), output, 30)
    assert not errors and len(paths) == 1
    assert core.inspect(paths[0], deep=True).duration == 4
    again, errors = recovery.export_merged_clips(row, clips, output, 30)
    assert not errors and again[0] != paths[0]


def test_merge_uses_original_clips_with_unicode_and_apostrophe_paths(tmp_path, monkeypatch):
    row = broken_movie(tmp_path/'cache')
    work = tmp_path/"中文's clips"; work.mkdir()
    clips = recovery.recover(row, work)
    assert all(clip.audio is False for clip in clips)
    monkeypatch.setattr(recovery.shutil, 'copyfile', lambda *args: pytest.fail('片段不应被重复复制'))
    monkeypatch.setattr(recovery, 'probe', lambda *args: pytest.fail('已知音频信息不应重复探测'))
    paths, errors = recovery.export_merged_clips(row, clips, tmp_path/'output', 30)
    assert not errors and core.inspect(paths[0], deep=True).duration == 4


def test_merge_rejects_modified_clip(tmp_path):
    row = broken_movie(tmp_path/'cache')
    work = tmp_path/'work'; work.mkdir()
    clips = recovery.recover(row, work)
    with clips[0].path.open('ab') as dest:
        dest.write(b'changed')
    paths, errors = recovery.export_merged_clips(row, clips, tmp_path/'output', 30)
    assert not paths and '片段内容已变化' in errors[0]


def test_merge_mixed_audio_and_silent_clips_keeps_audio(tmp_path):
    row = broken_movie(tmp_path/'cache')
    work = tmp_path/'work'; work.mkdir()
    clips = recovery.recover(row, work)
    with_audio = work/'audio.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(clips[0].path),
                    '-f', 'lavfi', '-i', 'sine=frequency=440', '-shortest',
                    '-c:v', 'copy', '-c:a', 'aac', str(with_audio)], check=True)
    first = recovery.Clip(with_audio, clips[0].start, clips[0].length, recovery.clip_digest(with_audio), True)
    second = recovery.Clip(clips[1].path, clips[1].start, clips[1].length, clips[1].digest, False)
    paths, errors = recovery.export_merged_clips(row, [first, second], tmp_path/'output', 60)
    assert not errors and len(paths) == 1
    result = core.inspect(paths[0], deep=True)
    assert result.status == 'unknown' and result.audio and result.duration >= 3.9


def test_fragmented_range_can_start_before_next_moof(tmp_path):
    row = broken_movie(tmp_path/'cache')
    ranges = recovery.ranges_for(row.fingerprint)
    end = ranges[1][1]
    start = ranges[1][0] - 9
    write_map(row.path, row.size, [(0, ranges[0][1]), (start, end - start)])
    row = core.inspect(row.path)
    clips = recovery.recover(row, tmp_path)
    assert [(c.start, c.length) for c in clips] == [(0, 2), (4, 2)]


def test_regular_mp4_with_audio_and_b_frames_recovers_cached_segments(tmp_path):
    cache = tmp_path/'cache'; cache.mkdir()
    path = cache/'video.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=12',
                    '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '6', '-c:v', 'libx264',
                    '-g', '24', '-keyint_min', '24', '-sc_threshold', '0', '-bf', '3',
                    '-c:a', 'aac', '-movflags', '+faststart', str(path)], check=True)
    info = recovery.probe(path, 'packet=stream_index,pos,flags', 30)
    keys = [int(p['pos']) for p in info['packets'] if p['stream_index'] == 0 and 'K' in p['flags']]
    start, end = keys[1:3]
    data = bytearray(path.read_bytes()); data[start:end] = bytes(end-start); path.write_bytes(data)
    write_map(path, len(data), [(0, start), (end, len(data)-end)])
    row = core.inspect(path)
    clips = recovery.recover(row, tmp_path)
    assert clips
    assert all(core.inspect(clip.path, deep=True).status == 'unknown' for clip in clips)


@pytest.mark.parametrize('b_frames', [0, 3])
def test_long_cached_runs_are_split_on_keyframes_and_remain_exportable(tmp_path, monkeypatch, b_frames):
    cache = tmp_path/'cache'; cache.mkdir()
    path = cache/'synthetic.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=96x54:rate=12',
                    '-f', 'lavfi', '-i', 'sine=frequency=440',
                    '-t', '8', '-c:v', 'libx264', '-threads', '1', '-g', '12', '-keyint_min', '12',
                    '-sc_threshold', '0', '-bf', str(b_frames), '-c:a', 'aac', '-movflags', '+faststart', str(path)], check=True)
    packets = recovery.probe(path, 'packet=stream_index,pos,flags', 30)['packets']
    keys = [int(packet['pos']) for packet in packets if packet['stream_index'] == 0 and 'K' in packet.get('flags', '')]
    start, end = keys[4:6]
    data = bytearray(path.read_bytes()); data[start:end] = bytes(end-start); path.write_bytes(data)
    write_map(path, len(data), [(0, start), (end, len(data)-end)])
    monkeypatch.setattr(recovery, 'MAX_RECOVERY_RUN_SECONDS', 1.5)
    row = core.inspect(path)
    clips = recovery.recover(row, tmp_path)
    assert len(clips) >= 3
    assert all(0 < clip.length <= 2.5 for clip in clips)
    paths, errors = recovery.export_merged_clips(row, clips, tmp_path/'output', 60)
    assert not errors and len(paths) == 1
    assert core.inspect(paths[0], deep=True).duration > 4


def test_changed_but_decodable_temporary_clip_is_not_exported(tmp_path):
    import shutil
    row = broken_movie(tmp_path/'cache')
    work = tmp_path/'tmp'; work.mkdir()
    clips = recovery.recover(row, work)
    assert len(clips) == 2
    shutil.copyfile(clips[1].path, clips[0].path)
    assert core.inspect(clips[0].path, deep=True).duration == 2
    output = tmp_path.parent/(tmp_path.name+'-exports')
    paths, errors = recovery.export_clips(row, clips[:1], output, 30)
    assert not paths and len(errors) == 1 and '片段内容已变化' in errors[0]
    assert not list(output.glob('*.mp4'))


def test_recovery_rejects_temporary_files_inside_cache(tmp_path):
    row = broken_movie(tmp_path)
    work = tmp_path/'inside-cache'
    work.mkdir()
    with pytest.raises(RuntimeError, match='缓存目录以外'):
        recovery.recover(row, work)
    assert not list(work.iterdir())


def test_output_permission_error_explains_how_to_continue(tmp_path, monkeypatch):
    row = broken_movie(tmp_path/'cache')
    output = tmp_path/'denied'
    original = Path.mkdir
    def denied(path, *args, **kwargs):
        if path == output:
            raise PermissionError('synthetic Windows access denied')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'mkdir', denied)
    with pytest.raises(RuntimeError, match='请选择其他可写目录'):
        recovery.export_clips(row, [], output, 30)


def test_source_permission_error_is_not_reported_as_output_failure(tmp_path, monkeypatch):
    row = broken_movie(tmp_path/'cache')
    work = tmp_path/'work'; work.mkdir()
    clips = recovery.recover(row, work)
    class DeniedSource:
        database = tmp_path/'tdata'/'user_data'/'media_cache'
        def revalidate(self):
            raise PermissionError('synthetic source locked')
    row.desktop_source = DeniedSource()
    paths, errors = recovery.export_clips(row, clips[:1], tmp_path/'output', 30)
    assert not paths and '读取缓存源' in errors[0] and '导出目录' not in errors[0]


def test_merge_reencodes_when_stream_copy_has_invalid_timestamps(tmp_path, monkeypatch):
    from dataclasses import replace
    row = broken_movie(tmp_path/'cache')
    work = tmp_path/'work'; work.mkdir()
    clips = recovery.recover(row, work)
    inspect = core.inspect
    checks = []
    def fail_first_merge(path, *args, **kwargs):
        result = inspect(path, *args, **kwargs)
        if Path(path).name == 'merged.mp4':
            checks.append(result)
            if len(checks) == 1:
                return replace(result, status='invalid', detail='non-monotonic DTS')
        return result
    monkeypatch.setattr(core, 'inspect', fail_first_merge)
    paths, errors = recovery.export_merged_clips(row, clips, tmp_path/'output', 60)
    assert not errors and len(paths) == 1 and len(checks) == 2
    assert inspect(paths[0], deep=True).status == 'unknown'


def test_regular_video_reports_verified_clips_before_packet_scan_finishes(tmp_path, monkeypatch):
    import time
    cache = tmp_path/'cache'; cache.mkdir()
    movie = cache/'synthetic.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=96x64:rate=12',
                    '-t', '12', '-c:v', 'libx264', '-g', '12', '-keyint_min', '12',
                    '-sc_threshold', '0', '-bf', '0', '-movflags', '+faststart', str(movie)], check=True)
    length = movie.stat().st_size
    write_map(movie, length, [(0, length - 1)])
    row = core.inspect(movie)
    assert row.status == 'partial'
    original = recovery.stream_packets
    finished = False
    def slow_packets(*args):
        nonlocal finished
        for packet in original(*args):
            time.sleep(.03)
            yield packet
        finished = True
    monkeypatch.setattr(recovery, 'stream_packets', slow_packets)
    monkeypatch.setattr(recovery, 'MAX_RECOVERY_RUN_SECONDS', 2.0)
    early = []
    work = tmp_path/'work'; work.mkdir()
    clips = recovery.recover(row, work, on_clip=lambda clip: early.append(not finished))
    assert clips and any(early) and finished
