@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"

set "HDK_LOCAL_MODE=0"
set "HDK_WEB_PUBLISHED_ONLY=1"
set "LOCAL_DB_PATH=%~dp0data\web_preview\project_hub.db"

if defined LOCALAPPDATA (
    set "VENV_DIR=%LOCALAPPDATA%\HDKDataHub\venv"
) else (
    set "VENV_DIR=%TEMP%\HDKDataHub\venv"
)
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_PY%" (
  echo [INFO] Virtual environment belum ada. Jalankan START_DASHBOARD.bat sekali dulu.
  pause
  exit /b 1
)

 echo [INFO] Menyusun data Web Preview dari Current Published...
"%VENV_PY%" SYNC_PUBLISHED_TO_WEB.py --target-data "%~dp0data\web_preview"
if errorlevel 1 (
  echo.
  echo [ERROR] Sync gagal. Pastikan minimal satu project sudah di-Publish dari menu Publish ^& Sync.
  pause
  exit /b 1
)

set PORT=8502
:CHECKPORT
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
  set /a PORT+=1
  goto CHECKPORT
)

echo [INFO] Web Preview: http://127.0.0.1:%PORT%
start "" "http://127.0.0.1:%PORT%"
"%VENV_PY%" -m streamlit run app.py --server.address 127.0.0.1 --server.port %PORT%
endlocal
