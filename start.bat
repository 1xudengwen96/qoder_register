@echo off
cd /d %~dp0
set "PYTHONPATH=%~dp0;%~dp0.pylibs"
set PYTHONIOENCODING=utf-8
chcp 65001 >nul
set "PYEXE="
for /f "delims=" %%i in ('where python 2^>nul') do (
  echo %%i | findstr /i /c:"WindowsApps" >nul
  if errorlevel 1 (
    "%%i" -c "import sys" >nul 2>nul
    if not errorlevel 1 if not defined PYEXE set "PYEXE=%%i"
  )
)
if not defined PYEXE (
  echo ERROR: real Python not found - run setup.bat first.
  pause
  exit /b 1
)
"%PYEXE%" tools\portable_start.py
echo.
echo Console exited. If errors above, screenshot this window.
pause
