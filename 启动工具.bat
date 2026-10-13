@echo off
setlocal
cd /d "%~dp0"

where pythonw.exe >nul 2>nul
if %errorlevel% equ 0 (
    start "" pythonw.exe "%~dp0app.py"
    exit /b 0
)

where pyw.exe >nul 2>nul
if %errorlevel% equ 0 (
    start "" pyw.exe -3 "%~dp0app.py"
    exit /b 0
)

echo 未找到 Python。请先安装 Python 3，并勾选“Add Python to PATH”。
pause
