@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Virtual environment belum ada. Jalankan START_DASHBOARD.bat sekali dulu.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" SYNC_PUBLISHED_TO_WEB.py --target-data "%~dp0data\web_preview"
echo.
pause
endlocal
