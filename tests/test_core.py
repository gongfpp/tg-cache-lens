from pathlib import Path
import os
import struct
import pytest
from tg_cache_lens import core
from conftest import write_map

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
    assert core.scan(tmp_path) == []
    monkeypatch.setattr(core.sys, 'platform', 'win32')
    with pytest.raises(RuntimeError, match='macOS'):
        core.auto_root()


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
    assert len(core.scan(core.auto_root())) == 3


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
