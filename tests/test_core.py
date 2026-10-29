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
