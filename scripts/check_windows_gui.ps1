$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$exe = Join-Path $root 'build\windows\Cache Lens\Cache Lens.exe'
$cache = Join-Path $root '.local\demo\media'
$output = Join-Path $root '.local\demo-export'
$check = @'
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
process = subprocess.Popen([sys.argv[1], '--cache', sys.argv[2], '--output', sys.argv[3]],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
try:
    deadline = time.monotonic() + 30
    found = []
    @callback_type
    def inspect(hwnd, unused):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == process.pid:
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, len(title))
            if title.value == 'Cache Lens':
                found.append(True)
        return True
    while time.monotonic() < deadline and process.poll() is None:
        user32.EnumWindows(inspect, 0)
        if found:
            print('Windows GUI 启动检查通过')
            break
        time.sleep(.25)
    else:
        raise SystemExit('Windows GUI 启动失败或未找到窗口')
finally:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=10)
'@
& $python -c $check $exe $cache $output
if ($LASTEXITCODE -ne 0) { throw 'Windows GUI 启动检查失败' }
