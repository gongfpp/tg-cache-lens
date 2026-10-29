"""Generate public sample media, run the real app, capture its rendered terminal."""
import asyncio
from pathlib import Path
import struct
import subprocess
import sys
import zlib
from textual.widgets import Input
from tg_cache_lens.app import CacheLens

ROOT=Path(__file__).resolve().parents[1]
CACHE=ROOT/'.local/demo/media'
CACHE.mkdir(parents=True,exist_ok=True)
def metadata(p,total,ranges):
    payload=struct.pack('<q',total)+b''.join(struct.pack('<qq',*r) for r in ranges)
    Path(str(p)+'.meta').write_bytes(struct.pack('<IIi',0x7bac1487,zlib.crc32(payload),len(ranges))+payload)
for name,source in [('demo-01-color-grid','testsrc2=size=640x360:rate=24'),('demo-02-blue','color=c=0x2786a0:size=320x180:rate=12')]:
    p=CACHE/(name+'_partial')
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',source,'-t','3','-c:v','libx264','-threads','1',
                    '-movflags','+faststart','-f','mp4',str(p)],check=True)
    metadata(p,p.stat().st_size,[(0,p.stat().st_size)])
data=(CACHE/'demo-01-color-grid_partial').read_bytes()
p=CACHE/'demo-03-missing-ranges_partial';p.write_bytes(data);metadata(p,len(data),[(0,3000),(len(data)-100,100)])
p=CACHE/'demo-04-no-length';p.write_bytes(data)
p=CACHE/'demo-05-broken-index_partial';p.write_bytes(data);Path(str(p)+'.meta').write_bytes(b'bad index')
async def capture():
    app=CacheLens(CACHE,Path('./exports'),autostart=False)
    async with app.run_test(size=(132,42)) as pilot:
        await pilot.click('#deep')
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.save_screenshot('tui.svg',str(ROOT/'.local'))
        await pilot.click('#export')
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.save_screenshot('export.svg',str(ROOT/'.local'))
    app=CacheLens(CACHE,Path('./exports'),autostart=False)
    async with app.run_test(size=(85,38)) as pilot:
        await pilot.click('#deep'); await app.workers.wait_for_complete(); await pilot.pause()
        app.save_screenshot('compact.svg',str(ROOT/'.local'))
asyncio.run(capture())
