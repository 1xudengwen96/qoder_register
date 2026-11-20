@echo off
cd /d %~dp0
set "FOUND="
for /f "delims=" %%i in ('where python 2^>nul') do (
  echo %%i | findstr /i /c:"WindowsApps" >nul
  if errorlevel 1 (
    "%%i" -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,12) else 1)" >nul 2>nul
    if not errorlevel 1 set "FOUND=1"
  )
)
if defined FOUND (
  echo Python 3.12 OK. Nothing to do.
  pause
  exit /b 0
)
echo No real Python 3.12 found ^(store stubs ignored^), installing...
if not exist "python-3.12.10-amd64.exe" (
  echo ERROR: missing python-3.12.10-amd64.exe - copy it from source machine.
  pause
  exit /b 1
)
echo Installing Python 3.12.10 silently, about 1 minute...
python-3.12.10-amd64.exe /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
if %errorlevel%==0 (
  echo Done. Close ALL windows, open a NEW one, then run start.bat
) else (
  echo Install failed with code %errorlevel%. Run the exe manually.
)
pause
