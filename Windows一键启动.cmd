@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title SMSI Archive Backup Client

set "STATE_DIR=%LOCALAPPDATA%\SMSIArchiveBackupClient"
if not exist "%STATE_DIR%" mkdir "%STATE_DIR%" >nul 2>nul
set "LOG_FILE=%STATE_DIR%\windows-launcher.log"
>"%LOG_FILE%" echo [%date% %time%] Checking the Windows client environment

where py >nul 2>nul
if errorlevel 1 goto :try_python
py -3 -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 1)" >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :try_python
set "PYTHON_EXE=py"
set "PYTHON_ARGS=-3"
goto :python_ready

:try_python
where python >nul 2>nul
if errorlevel 1 goto :python_missing
python -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 1)" >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :python_missing
set "PYTHON_EXE=python"
set "PYTHON_ARGS="

:python_ready
"%PYTHON_EXE%" %PYTHON_ARGS% --version >>"%LOG_FILE%" 2>&1

where rclone >nul 2>nul
if not errorlevel 1 goto :rclone_ready
echo rclone is required for Ubuntu SFTP access. Attempting installation...
where winget >nul 2>nul
if errorlevel 1 goto :rclone_missing
winget install --id Rclone.Rclone -e --accept-package-agreements --accept-source-agreements >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :failed
where rclone >nul 2>nul
if errorlevel 1 goto :rclone_missing

:rclone_ready

if exist ".venv\Scripts\python.exe" goto :venv_ready
echo Creating the Python environment...
"%PYTHON_EXE%" %PYTHON_ARGS% -m venv .venv >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :failed

:venv_ready
".venv\Scripts\python.exe" -c "from pathlib import Path; import sys; marker=Path('.venv/requirements-windows.installed'); current=Path('requirements-windows.txt').read_bytes(); sys.exit(0 if marker.exists() and marker.read_bytes()==current else 1)" >>"%LOG_FILE%" 2>&1
if not errorlevel 1 goto :dependencies_ready
echo Installing or updating dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements-windows.txt >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :failed
copy /y requirements-windows.txt ".venv\requirements-windows.installed" >nul

:dependencies_ready
".venv\Scripts\python.exe" -c "import tkinter, pyarrow; import archive_backup.desktop" >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :failed
if /i "%~1"=="--check" goto :preflight
if /i "%SMSI_ARCHIVE_PREFLIGHT_ONLY%"=="1" goto :preflight

echo Starting the Windows desktop client...
>>"%LOG_FILE%" echo [%date% %time%] Starting the native desktop client
start "" /b ".venv\Scripts\pythonw.exe" -m archive_backup.desktop
if errorlevel 1 goto :failed
exit /b 0

:preflight
".venv\Scripts\python.exe" -m archive_backup.desktop --check >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :failed
>>"%LOG_FILE%" echo [%date% %time%] Environment check passed
exit /b 0

:python_missing
echo Python 3.10 or newer is required. Install Python with PATH or Python Launcher enabled.
>>"%LOG_FILE%" echo Python 3.10 or newer was not found
goto :failed

:rclone_missing
echo rclone is required for Ubuntu SFTP access. Install it from https://rclone.org/install/ and run this file again.
>>"%LOG_FILE%" echo rclone was not found
goto :failed

:failed
echo.
echo The client could not start. Error log:
echo %LOG_FILE%
echo.
type "%LOG_FILE%"
pause
exit /b 1
