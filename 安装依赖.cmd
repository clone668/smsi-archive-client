@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo 未找到 Python Launcher，请先安装 Python 3.11 或更高版本。
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\pip.exe" install -r requirements.txt
if errorlevel 1 goto :failed
echo.
echo 安装完成。
pause
exit /b 0
:failed
echo.
echo 安装失败，请查看上方错误。
pause
exit /b 1
