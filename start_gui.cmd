@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m minisql.gui %*
) else (
    py -3 -m minisql.gui %*
)
if errorlevel 1 pause
