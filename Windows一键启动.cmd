@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo 未找到 Python。请先安装 Python 3.11 或更高版本，并勾选 Python Launcher。
  pause
  exit /b 1
)

where rclone >nul 2>nul
if errorlevel 1 (
  where winget >nul 2>nul
  if errorlevel 1 (
    echo 未找到 rclone 和 winget，请先安装 rclone。
    pause
    exit /b 1
  )
  echo 正在安装 rclone...
  winget install --id Rclone.Rclone -e --accept-package-agreements --accept-source-agreements
  if errorlevel 1 goto :failed
)

if not exist ".venv\Scripts\python.exe" (
  echo 正在创建 Python 虚拟环境...
  py -3 -m venv .venv
  if errorlevel 1 goto :failed
)

".venv\Scripts\python.exe" -c "from pathlib import Path; import sys; marker=Path('.venv/requirements.installed'); current=Path('requirements.txt').read_bytes(); sys.exit(0 if marker.exists() and marker.read_bytes()==current else 1)"
if errorlevel 1 (
  echo 正在安装或更新依赖...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  if errorlevel 1 goto :failed
  ".venv\Scripts\pip.exe" install -r requirements.txt
  if errorlevel 1 goto :failed
  copy /y requirements.txt ".venv\requirements.installed" >nul
)

netstat -ano | findstr /r /c:":8788 .*LISTENING" >nul
if not errorlevel 1 (
  start "" http://127.0.0.1:8788/
  exit /b 0
)

start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8788/"
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo 启动失败，请查看上方错误。
pause
exit /b 1
