@echo off
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 collect_frl.py --gui
) else (
  python collect_frl.py --gui
)
if errorlevel 1 (
  echo.
  echo Install Python 3.10+ with Tkinter and the Windows OpenSSH client.
  pause
)
