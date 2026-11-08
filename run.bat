@echo off
rem Launch the calculator without a console window.
rem Requires Python 3.8+ with Pillow (pip install pillow).
setlocal
cd /d "%~dp0"

set "PYW="
for %%P in (pythonw.exe) do if not "%%~$PATH:P"=="" set "PYW=%%~$PATH:P"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"

if not defined PYW (
    echo pythonw.exe not found. Please install Python 3.8+ and add it to PATH.
    echo Download: https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

start "" "%PYW%" "%~dp0app.py"
