@echo off
cd /d "%~dp0"
set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links"
.venv\Scripts\python.exe -m tg_cache_lens gui
if errorlevel 1 pause
