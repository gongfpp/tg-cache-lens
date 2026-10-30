"""Generate copyright-free test signals for screenshots of the real CLI/GUI."""
from pathlib import Path
import struct
import subprocess
import zlib
ROOT=Path(__file__).resolve().parents[1]
CACHE=ROOT/'.local/demo/media'
CACHE.mkdir(parents=True, exist_ok=True)
def metadata(path,total,ranges):
    data=struct.pack('<q',total)+b''.join(struct.pack('<qq',*r) for r in ranges)
    Path(str(path)+'.meta').write_bytes(struct.pack('<IIi',0x7bac1487,zlib.crc32(data),len(ranges))+data)
for name,source in [('demo-01-color-grid','testsrc2=size=960x540:rate=24'),('demo-02-blue','color=c=0x2786a0:size=640x360:rate=24')]:
    path=CACHE/(name+'_partial')
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',source,'-f','lavfi','-i','sine=frequency=440',
        '-t','20','-c:v','libx264','-threads','2','-c:a','aac','-movflags','+faststart','-f','mp4',str(path)],check=True)
    metadata(path,path.stat().st_size,[(0,path.stat().st_size)])
data=(CACHE/'demo-01-color-grid_partial').read_bytes()
path=CACHE/'demo-03-missing-ranges_partial';path.write_bytes(data);metadata(path,len(data),[(0,3000),(len(data)-100,100)])
path=CACHE/'demo-04-no-length';path.write_bytes(data)
path=CACHE/'demo-05-broken-index_partial';path.write_bytes(data);Path(str(path)+'.meta').write_bytes(b'bad index')
print('Demo media ready. Launch with --cache .local/demo/media')
