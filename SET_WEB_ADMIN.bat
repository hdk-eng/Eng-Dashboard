@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo HDK DATA HUB - USER ACCESS / WEB ADMIN CONFIGURATION
echo ============================================================
echo.
echo Untuk publikasi internet, set email + password Admin pertama.
echo Password tidak boleh disimpan di source code / GitHub.
echo.
echo Contoh Command Prompt / PowerShell pada Windows Server:
echo   setx HDK_ADMIN_EMAIL "admin@hdk.co.id"
echo   setx HDK_ADMIN_PASSWORD "PASSWORD_KUAT_ANDA"
echo   setx HDK_PUBLIC_BASE_URL "https://dashboard.domainanda.com/"
echo.
echo Setelah setx, restart dashboard.
echo Admin pertama akan dibuat otomatis HANYA jika tabel user masih kosong.
echo User berikutnya dibuat dari menu: User ^& Akses.
echo Untuk Streamlit Community Cloud, masukkan variabel yang sama pada Secrets.
echo.
pause
