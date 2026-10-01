@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo   RESET PIN ADMIN LOKAL - HDK PROJECT DATA HUB
echo ============================================================
echo.
echo Perintah ini hanya menghapus HASH PIN Admin Lokal.
echo Data proyek, user, Excel, foto, dan database TIDAK dihapus.
echo Setelah reset, buka dashboard dan buat PIN Admin Lokal baru.
echo.
set /p CONFIRM=Ketik RESET untuk melanjutkan: 
if /I not "%CONFIRM%"=="RESET" (
  echo Dibatalkan.
  pause
  exit /b 0
)
if exist "data\.local_admin_pin" (
  del /f /q "data\.local_admin_pin"
  echo.
  echo [OK] PIN Admin Lokal sudah direset.
) else (
  echo.
  echo [INFO] Belum ada PIN Admin Lokal yang tersimpan.
)
echo Buka kembali START_DASHBOARD.bat untuk membuat PIN baru.
pause
