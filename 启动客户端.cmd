@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 尚未安装依赖，请先运行“安装依赖.cmd”。
  pause
  exit /b 1
)
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8788/"
".venv\Scripts\python.exe" app.py
if errorlevel 1 pause
