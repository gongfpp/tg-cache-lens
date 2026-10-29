"""Collect dependency notices for the binary distribution."""
from importlib.metadata import distribution
from pathlib import Path
import shutil
out=Path('dist/tg-cache-lens/LICENSES');out.mkdir(parents=True,exist_ok=True)
packages=['textual','rich','pygments','markdown-it-py','mdit-py-plugins','mdurl','platformdirs',
          'typing-extensions','linkify-it-py','uc-micro-py','pyinstaller']
for name in packages:
    try: dist=distribution(name)
    except Exception: continue
    for f in dist.files or []:
        if any(word in str(f).upper() for word in ('LICENSE','COPYING','NOTICE')) and '.dist-info/' in str(f):
            path=out/name/Path(str(f)).name;path.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(dist.locate_file(f),path)
import sysconfig
stdlib=Path(sysconfig.get_path('stdlib'))
for name in ('LICENSE.txt','LICENSE'):
    if (stdlib/name).is_file(): shutil.copyfile(stdlib/name,out/'PYTHON-LICENSE.txt')
