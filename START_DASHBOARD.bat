@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title HDK Project Data Hub v2.10.1 Launcher

 echo ============================================================
 echo   HDK PROJECT DATA HUB v2.10.1
 echo   Excel multi-fungsi ^| Foto Lapangan ^| BIMx ^| SQLite
 echo ============================================================
 echo.

set "PY_CMD="
where py >nul 2>nul && set "PY_CMD=py"
if not defined PY_CMD (
    where python >nul 2>nul && set "PY_CMD=python"
)

if not defined PY_CMD (
    echo [ERROR] Python tidak ditemukan.
    echo Install Python 3.11, 3.12, atau 3.13 dari https://www.python.org/downloads/
    echo Saat install, CENTANG: Add python.exe to PATH
    echo.
    pause
    exit /b 1
)

%PY_CMD% --version
if errorlevel 1 goto :fail

rem VENV sengaja ditempatkan di path Windows yang pendek untuk mencegah WinError 206.
if defined LOCALAPPDATA (
    set "HDK_ENV_ROOT=%LOCALAPPDATA%\HDKDataHub"
) else (
    set "HDK_ENV_ROOT=%TEMP%\HDKDataHub"
)
set "VENV_DIR=%HDK_ENV_ROOT%\venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "DEPS_MARKER=%VENV_DIR%\.deps_ready_v2101"

if not exist "%VENV_PY%" (
    echo.
    echo [1/4] Membuat virtual environment di lokasi pendek...
    if not exist "%HDK_ENV_ROOT%" mkdir "%HDK_ENV_ROOT%"
    %PY_CMD% -m venv "%VENV_DIR%"
    if errorlevel 1 goto :fail
) else (
    echo [1/4] Virtual environment siap: %VENV_DIR%
)

if not exist "%DEPS_MARKER%" (
    echo [2/4] Instalasi library pertama kali...
    "%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 goto :fail
    echo ready> "%DEPS_MARKER%"
) else (
    echo [2/4] Library siap. Lewati instalasi ulang.
)

 echo [3/4] Mencari port localhost yang tersedia...
set "PORT="
for /f %%P in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=8501; while($p -le 8599 -and (Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue)){ $p++ }; if($p -gt 8599){ exit 2 }; Write-Output $p"') do set "PORT=%%P"

if not defined PORT (
    echo [ERROR] Tidak menemukan port kosong antara 8501-8599.
    echo Tutup aplikasi localhost lama lalu jalankan kembali.
    goto :fail
)

if not "%PORT%"=="8501" (
    echo [INFO] Port 8501 sedang digunakan.
    echo [INFO] Dashboard baru akan menggunakan port %PORT%.
)

 echo [4/4] Menjalankan dashboard...
 echo.
set "HDK_LOCAL_MODE=1"
 echo ============================================================
 echo   Dashboard: http://localhost:%PORT%
 echo   JANGAN tutup jendela ini selama aplikasi digunakan.
 echo   Untuk menghentikan dashboard tekan CTRL+C.
 echo ============================================================
 echo.

start "HDK Browser Launcher" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://localhost:%PORT%'"

"%VENV_PY%" -m streamlit run app.py --server.address 127.0.0.1 --server.port %PORT% --server.headless true --server.fileWatcherType none --server.runOnSave false --browser.gatherUsageStats false
if errorlevel 1 goto :fail
exit /b 0

:fail
 echo.
 echo ============================================================
 echo [ERROR] Dashboard gagal dijalankan.
 echo Salin pesan di atas atau kirim screenshot/log ke ChatGPT.
 echo ============================================================
pause
exit /b 1
