param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $PWD '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '请先创建 .venv 并安装项目的 GUI 与开发依赖。' }
$dist = Join-Path $PWD 'build\windows'
$work = Join-Path $PWD '.local\pyinstaller\windows'
$spec = Join-Path $PWD '.local\pyinstaller\spec'
New-Item -ItemType Directory -Force -Path $dist, $work, $spec | Out-Null
$env:PYINSTALLER_CONFIG_DIR = Join-Path $PWD '.local\pyinstaller\cache'
if (-not $SkipBuild) {
    & $python scripts/make_windows_icon.py
    if ($LASTEXITCODE -ne 0) { throw '生成图标失败' }
    & $python -m PyInstaller --workpath $work --distpath $dist --specpath $spec --noconfirm --clean --onedir --name 'tg-cache-lens' --exclude-module PySide6 scripts/entry.py
    if ($LASTEXITCODE -ne 0) { throw '构建命令行失败' }
    & $python -m PyInstaller --workpath $work --distpath $dist --specpath $spec --noconfirm --clean --windowed --onedir --name 'Cache Lens' --icon (Join-Path $PWD '.local\icon.ico') --collect-data tg_cache_lens scripts/gui_entry.py
    if ($LASTEXITCODE -ne 0) { throw '构建窗口失败' }
}
# Qt uses the Windows system ICU API. A PATH-provided ICU can have versioned
# exports and shadow the compatible Windows DLL, making QtCore fail to load.
$bundledIcu = Join-Path $dist 'Cache Lens\_internal\icuuc.dll'
if (Test-Path -LiteralPath $bundledIcu) { Remove-Item -LiteralPath $bundledIcu -Force }
foreach ($name in @('tg-cache-lens', 'Cache Lens')) {
    $target = Join-Path $dist $name
    if (-not (Test-Path -LiteralPath (Join-Path $target "$name.exe"))) { throw "找不到已构建的程序：$name" }
    Copy-Item -LiteralPath LICENSE, README.md -Destination $target -Force
    Copy-Item -LiteralPath docs, assets -Destination $target -Recurse -Force
    & $python scripts/collect_licenses.py (Join-Path $target 'LICENSES')
    if ($LASTEXITCODE -ne 0) { throw '收集许可证失败' }
}
& (Join-Path $PSScriptRoot 'check_windows_gui.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Windows GUI 启动检查失败' }
Write-Host "Windows 程序已生成：$dist"
