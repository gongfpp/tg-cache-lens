from pathlib import Path
import struct
import subprocess
import zlib
import pytest

def write_map(path, total, ranges):
    payload = struct.pack('<q', total)+b''.join(struct.pack('<qq', *r) for r in ranges)
    Path(str(path)+'.meta').write_bytes(struct.pack('<IIi', 0x7bac1487, zlib.crc32(payload), len(ranges))+payload)

@pytest.fixture
def cache(tmp_path):
    root = tmp_path/'media'
    root.mkdir()
    good = root/'telegram-cloud-document-1-1001_partial'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=12',
        '-f','lavfi','-i','sine=frequency=440','-t','1','-c:v','libx264','-threads','1',
        '-c:a','aac','-movflags','+faststart','-f','mp4',str(good)], check=True)
    data = good.read_bytes()
    write_map(good,len(data),[(0,len(data))])
    partial = root/'telegram-cloud-document-1-1002_partial'
    partial.write_bytes(data)
    write_map(partial,len(data),[(0,1000),(len(data)-100,100)])
    unknown = root/'telegram-cloud-document-1-1003'
    unknown.write_bytes(data)
    return root
