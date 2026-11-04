from pathlib import Path
import os
import struct
import pytest
from tg_cache_lens import core


def test_cancel_terminates_active_media_subprocess():
    import sys
    import threading
    import time
    event = threading.Event()
    outcomes = []
    def run():
        try:
            with core.cancel_scope(event):
                core.run([sys.executable, '-c', 'import time; time.sleep(20)'], 30)
        except core.WorkCancelled:
            outcomes.append('cancelled')
    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(.3)
    event.set()
    thread.join(5)
    assert not thread.is_alive() and outcomes == ['cancelled']
from conftest import write_map

@pytest.mark.skipif(os.name != 'nt', reason='Windows WinGet discovery')
def test_windows_finds_winget_ffmpeg_when_path_is_stale(tmp_path, monkeypatch):
    links = tmp_path/'Microsoft'/'WinGet'/'Links'
    links.mkdir(parents=True)
    executable = links/'ffmpeg.exe'
    executable.write_bytes(b'synthetic')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr(core.shutil, 'which', lambda name: None)
    assert core.binary('ffmpeg') == str(executable)
    with pytest.raises(RuntimeError, match='ffprobe'):
        core.binary('ffprobe')

def test_inspect_and_dedupe(cache):
    original = cache/'telegram-cloud-document-1-1001_partial'
    os.link(original, cache/'telegram-cloud-document-1-1001.mp4')
    rows = core.scan(cache, deep=True)
    assert len(rows)==3
    assert {r.status for r in rows}=={'ready','partial','unknown'}
    good = next(r for r in rows if r.status=='ready')
    assert good.copies==2 and good.coverage=='100.0%'

def test_corrupt_map(cache):
    path=cache/'telegram-cloud-document-1-1001_partial'
    m=Path(str(path)+'.meta')
    b=bytearray(m.read_bytes()); b[15]^=1; m.write_bytes(b)
    assert core.inspect(path,deep=True).status=='invalid'

def test_unknown_length_is_not_complete(cache):
    path=cache/'telegram-cloud-document-1-1001_partial'
    write_map(path,-1,[(0,path.stat().st_size)])
    assert core.inspect(path,deep=True).status=='unknown'

def test_export_and_no_overwrite(cache,tmp_path):
    good=next(i for i in core.scan(cache,deep=True) if i.status=='ready')
    for fmt in ['mp4','mkv','original']:
        target=core.export(good,tmp_path/'exports',fmt)
        assert target.is_file()
        assert core.inspect(target,deep=True).status=='unknown'
    assert len(list((tmp_path/'exports').iterdir()))==3
    assert not list((tmp_path/'exports').glob('.export-*'))

def test_export_refuses_missing_and_changed(cache,tmp_path):
    rows=core.scan(cache,deep=True)
    for i in rows:
        if i.status!='ready':
            with pytest.raises(RuntimeError): core.export(i,tmp_path/'exports')
    good=next(i for i in rows if i.status=='ready')
    write_map(good.path,good.size+100,[(0,good.size)])
    with pytest.raises(RuntimeError): core.export(good,tmp_path/'exports')
    assert not (tmp_path/'exports').exists()


def test_export_refuses_cache_directory(cache):
    good = next(row for row in core.scan(cache) if row.status == 'covered')
    with pytest.raises(RuntimeError, match='缓存目录以外'):
        core.export(good, cache/'exports')
    assert not (cache/'exports').exists()


def test_publish_temp_refuses_existing_destination(tmp_path):
    source = tmp_path/'temporary'
    target = tmp_path/'existing'
    source.write_bytes(b'new')
    target.write_bytes(b'original')
    with pytest.raises(FileExistsError):
        core.publish_temp(source, target)
    assert target.read_bytes() == b'original'
    assert source.read_bytes() == b'new'


def test_export_handles_destination_created_during_publish(cache, tmp_path, monkeypatch):
    item = next(row for row in core.scan(cache) if row.status == 'covered')
    original = core.publish_temp
    calls = []
    def collision(temp, target):
        calls.append(target)
        if len(calls) == 1:
            target.write_bytes(b'existing')
            raise FileExistsError()
        original(temp, target)
    monkeypatch.setattr(core, 'publish_temp', collision)
    result = core.export(item, tmp_path/'out')
    assert calls[0].read_bytes() == b'existing'
    assert result == calls[1] and result.is_file()

def test_transcode_webm(cache,tmp_path):
    import subprocess
    p=cache/'telegram-cloud-document-1-1004_partial'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10',
        '-t','0.5','-c:v','libvpx-vp9','-threads','1','-f','webm',str(p)],check=True)
    write_map(p,p.stat().st_size,[(0,p.stat().st_size)])
    row=core.inspect(p,deep=True)
    out=core.export(row,tmp_path/'out','mp4')
    assert core.inspect(out,deep=True).codec=='h264'

def test_decode_detects_damage(cache):
    p=cache/'telegram-cloud-document-1-1001_partial'
    b=p.read_bytes(); p.write_bytes(b[:len(b)//2])
    write_map(p,p.stat().st_size,[(0,p.stat().st_size)])
    assert core.inspect(p,deep=True).status=='invalid'


def test_auto_location_and_image_exclusion(tmp_path, monkeypatch):
    monkeypatch.setattr(core.sys, 'platform', 'darwin')
    monkeypatch.setattr(core, 'default_root', lambda: tmp_path)
    with pytest.raises(RuntimeError, match='未找到'):
        core.auto_root()
    media = tmp_path/'appstore/account-example/postbox/media'
    media.mkdir(parents=True)
    assert core.auto_root() == tmp_path
    (media/'icon').write_bytes(b'\x89PNG\r\n\x1a\n'+bytes(20))
    (media/'animation').write_bytes(b'GIF89a'+bytes(20))
    monkeypatch.undo()
    assert core.scan(tmp_path) == []
    monkeypatch.setattr(core.sys, 'platform', 'win32')
    monkeypatch.setattr(core, 'default_root', lambda: tmp_path/'missing')
    monkeypatch.setattr(core, 'running_telegram_dirs', lambda: set())
    with pytest.raises(RuntimeError, match='macOS|portable'):
        core.auto_root()


@pytest.mark.skipif(os.name != 'nt', reason='Windows portable auto-location')
def test_windows_auto_root_prefers_running_portable_and_reports_ambiguity(tmp_path, monkeypatch):
    first = tmp_path/'portable-a'/'tdata'; first.mkdir(parents=True)
    second = tmp_path/'portable-b'/'tdata'; second.mkdir(parents=True)
    standard = tmp_path/'standard'; standard.mkdir()
    for root in (first, second, standard):
        (root/'key_datas').write_bytes(b'synthetic')
    monkeypatch.setattr(core, 'default_root', lambda: standard)
    monkeypatch.setattr(core, 'running_telegram_dirs', lambda: {first})
    assert core.auto_root() == first.resolve()
    monkeypatch.setattr(core, 'running_telegram_dirs', lambda: {first, second})
    with pytest.raises(RuntimeError, match='多个'):
        core.auto_root()
    monkeypatch.setattr(core, 'running_telegram_dirs', lambda: set())
    assert core.auto_root() == standard


@pytest.mark.skipif(os.name != 'nt', reason='Windows process discovery')
def test_running_telegram_paths_only_query_matching_process(monkeypatch, tmp_path):
    import psutil
    class FakeProcess:
        def __init__(self, name, path=None, denied=False):
            self.info = {'name': name}; self.path = path; self.denied = denied
        def exe(self):
            if self.denied: raise psutil.AccessDenied()
            assert self.info['name'].casefold() == 'telegram.exe'
            return self.path
    processes = [FakeProcess('other.exe'), FakeProcess('Telegram.exe', str(tmp_path/'portable'/'Telegram.exe')),
                 FakeProcess('telegram.exe', denied=True)]
    monkeypatch.setattr(psutil, 'process_iter', lambda fields: processes)
    assert core.running_telegram_dirs() == {tmp_path/'portable'/'tdata'}


def test_attached_cover_is_not_a_video(tmp_path, monkeypatch):
    import json
    path = tmp_path/'cover.mp4'
    path.write_bytes(b'0000ftyp'+bytes(20))
    monkeypatch.setattr(core, 'run', lambda *a, **kw: (json.dumps({'streams': [
        {'codec_type': 'video', 'codec_name': 'mjpeg', 'disposition': {'attached_pic': 1}},
        {'codec_type': 'audio', 'codec_name': 'aac'}]}), ''))
    assert core.inspect(path) is None


def test_unknown_cached_size_is_not_file_length(cache):
    rows = core.scan(cache)
    unknown = next(row for row in rows if row.status == 'unknown')
    assert unknown.size > 0 and unknown.cached_size is None
    partial = next(row for row in rows if row.status == 'partial')
    assert partial.cached_size == 1100 < partial.size


@pytest.mark.parametrize('relative', ['appstore/account-test/postbox/media', 'account-test/postbox/media'])
def test_auto_scan_account_layouts(cache, tmp_path, monkeypatch, relative):
    import shutil
    root = tmp_path/'container'
    shutil.copytree(cache, root/relative)
    monkeypatch.setattr(core.sys, 'platform', 'darwin')
    monkeypatch.setattr(core, 'default_root', lambda: root)
    located = core.auto_root()
    monkeypatch.undo()
    assert len(core.scan(located)) == 3


def test_cached_bytes_exclude_index_ranges_past_eof(cache):
    path = cache/'telegram-cloud-document-1-1001_partial'
    length = path.stat().st_size
    write_map(path, length + 10000, [(0, 1000), (length+5000, 1000)])
    row = core.inspect(path)
    assert row.status == 'partial' and row.cached_size == 1000


@pytest.mark.parametrize('stream_duration,format_duration,expected', [
    ('12.5', '20', 12.5), ('N/A', '20', 20), ('NaN', 'Infinity', None), ('0', '-1', None)])
def test_duration_comes_from_media_metadata(tmp_path, monkeypatch, stream_duration, format_duration, expected):
    import json
    path = tmp_path/'misleading-9999-seconds.mp4'
    path.write_bytes(b'0000ftyp'+bytes(20))
    monkeypatch.setattr(core, 'run', lambda *a, **kw: (json.dumps({
        'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'duration': stream_duration}],
        'format': {'duration': format_duration}}), ''))
    row = core.inspect(path)
    assert row.duration == expected and row.status == 'unknown'


@pytest.mark.parametrize('portable', [False, True])
def test_desktop_tdata_is_not_misreported_as_empty(tmp_path, portable):
    tdata = tmp_path/'tdata' if portable else tmp_path
    tdata.mkdir(exist_ok=True)
    (tdata/'key_datas').write_bytes(b'synthetic')
    with pytest.raises(RuntimeError, match='密钥文件'):
        core.scan(tmp_path)


def test_deep_inspection_accepts_valid_variable_frame_timestamps(tmp_path):
    import subprocess
    path = tmp_path/'variable-rate.mp4'
    command = [core.binary('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
               'testsrc2=size=160x90:rate=30', '-t', '8', '-vf',
               r'settb=1/1000000,setpts=if(eq(N\,200)\,6643300\,N*33333)',
               '-fps_mode', 'passthrough', '-enc_time_base', '1:1000000',
               '-c:v', 'libx264', '-bf', '0', '-video_track_timescale', '1000000', str(path)]
    subprocess.run(command, check=True, capture_output=True)
    result = core.inspect(path, deep=True)
    assert result is not None and result.status == 'unknown'
