"""Save installed dependency notices and upstream Qt license texts for distribution."""
from importlib.metadata import distribution, PackageNotFoundError
from pathlib import Path
import shutil
import sys
import sysconfig
import urllib.request
import subprocess
import time
out=Path(sys.argv[1]); out.mkdir(parents=True,exist_ok=True)
for name in ['pyside6','pyside6-essentials','pyside6-addons','pyside6-webengine',
             'pyside6-pdf','shiboken6','pyinstaller','cryptography','xxhash','cffi','pycparser','psutil']:
    try: dist=distribution(name)
    except PackageNotFoundError: continue
    package=out/name; package.mkdir(exist_ok=True)
    (package/'METADATA.txt').write_text(dist.read_text('METADATA') or '')
    for f in dist.files or []:
        if any(word in str(f).upper() for word in ('LICENSE','COPYING','NOTICE')) and '.dist-info/' in str(f):
            shutil.copyfile(dist.locate_file(f),package/Path(str(f)).name)
stdlib=Path(sysconfig.get_path('stdlib'))
for name in ('LICENSE.txt','LICENSE'):
    if (stdlib/name).is_file(): shutil.copyfile(stdlib/name,out/'PYTHON-LICENSE.txt')
urls={
 'LGPL-3.0.txt':'https://raw.githubusercontent.com/qt/qtbase/v6.12.0/LICENSES/LGPL-3.0-only.txt',
 'GPL-3.0.txt':'https://raw.githubusercontent.com/qt/qtbase/v6.12.0/LICENSES/GPL-3.0-only.txt',
 'Qt-GPL-exception.txt':'https://raw.githubusercontent.com/qt/qtbase/v6.12.0/LICENSES/Qt-GPL-exception-1.0.txt',
 'Qt-for-Python.html':'https://doc.qt.io/qtforpython-6/licenses.html',
 'Qt-Core.html':'https://doc.qt.io/qt-6/qtcore-index.html',
 'Qt-Gui.html':'https://doc.qt.io/qt-6/qtgui-index.html',
 'Qt-Multimedia.html':'https://doc.qt.io/qt-6/qtmultimedia-attribution-ffmpeg.html',
}
cache=Path('.local/licenses'); cache.mkdir(parents=True,exist_ok=True)
def download(url, path):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read()
            path.write_bytes(data)
            return
        except OSError:
            if attempt < 2:
                time.sleep(0.5)
    # On some Windows setups Python's TLS connection fails while system curl works.
    result = subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                             '--retry', '3', '--max-time', '90', url], capture_output=True, check=True)
    path.write_bytes(result.stdout)
for name,url in urls.items():
    file=cache/name
    if not file.exists():
        download(url, file)
    shutil.copyfile(file,out/name)
# Keep copyright notices for bundled Qt third-party components available offline.
import re
from concurrent.futures import ThreadPoolExecutor
links=set()
for page in ('Qt-Core.html','Qt-Gui.html'):
    links.update(re.findall(r'href="([^"]*attribution[^"]*\.html)"', (cache/page).read_text()))
def attribution(link):
    name=Path(link).name
    path=cache/name
    if not path.exists():
        download('https://doc.qt.io/qt-6/'+name, path)
    shutil.copyfile(path,out/name)
with ThreadPoolExecutor(max_workers=6) as pool: list(pool.map(attribution,links))
(out/'SOURCES.txt').write_text('''TG Cache Lens uses unmodified dynamically linked Qt / PySide6 / Shiboken6 6.12.0.
Qt and PySide6 are Copyright The Qt Company Ltd. and contributors, used under LGPLv3.
Qt multimedia includes FFmpeg 9.0.1 (LGPLv2.1 or later) and its notices are included.
Complete corresponding upstream sources:
https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/
https://download.qt.io/official_releases/qt/6.12/6.12.0/submodules/
https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz
Qt FFmpeg build scripts: https://code.qt.io/cgit/qt/qtmultimedia.git/tree/src/3rdparty/ffmpeg?h=v6.12.0
Application source and rebuild scripts: https://github.com/gongfpp/tg-cache-lens
Libraries remain separate in the application bundle or distribution directory.
You may replace or rebuild them. On macOS, re-sign locally using
codesign --force --deep --sign - "Cache Lens.app".
No restrictions are imposed on modifying these libraries or reverse engineering for
debugging such modifications. License texts govern your rights.
The external ffmpeg/ffprobe command-line executables are not included in this bundle.
''')
