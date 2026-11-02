from pathlib import Path
import struct
import subprocess

from tg_cache_lens import core, recovery
from conftest import write_map


def broken_movie(tmp_path, fragmented=True):
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
    row = broken_movie(tmp_path)
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
    row = broken_movie(tmp_path,False)
    clips = recovery.recover(row,tmp_path)
    assert [(c.start,c.length) for c in clips] == [(0,2),(4,2)]
