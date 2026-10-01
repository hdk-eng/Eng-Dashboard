@echo off
setlocal
cd /d "%~dp0"
echo CEK PYTHON - HDK PROJECT DATA HUB
echo.
where py >nul 2>nul
if %errorlevel%==0 (
  py --version
  echo Python siap digunakan melalui perintah: py
  pause
  exit /b 0
)
where python >nul 2>nul
if %errorlevel%==0 (
  python --version
  echo Python siap digunakan melalui perintah: python
  pause
  exit /b 0
)
echo Python BELUM ditemukan.
echo Install Python 3.11/3.12 dan centang Add python.exe to PATH.
pause
