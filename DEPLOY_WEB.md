
> **Penting:** Jangan set `HDK_LOCAL_MODE=1` pada Streamlit Cloud, Docker publik, atau server. Variabel itu hanya dipasang otomatis oleh `START_DASHBOARD.bat` untuk recovery/pengujian di `127.0.0.1`.

# Deploy HDK Project Data Hub v2.9.10

## Secrets / environment wajib

Set sebelum aplikasi dipakai di internet:

```text
HDK_ADMIN_USERNAME=admin.hdk
HDK_ADMIN_EMAIL=admin@hdk.co.id
HDK_ADMIN_PASSWORD=password-admin-yang-kuat
HDK_PUBLIC_BASE_URL=https://dashboard.domainanda.com/
```

Untuk Streamlit Community Cloud, masukkan sebagai **Secrets**. Untuk Windows Server gunakan environment variable. Untuk Docker gunakan `.env`/environment.

`HDK_ADMIN_USERNAME` + `HDK_ADMIN_EMAIL` + `HDK_ADMIN_PASSWORD` adalah **System/Recovery Admin**. Akun ini selalu dijaga aktif sebagai Admin: jika belum ada akan dibuat, dan jika email yang sama sudah ada tetapi password/role/status berbeda akan disinkronkan otomatis saat aplikasi start. Ini mencegah Admin terkunci setelah database sudah memiliki user.

Setelah login sebagai Admin, buat dan kelola seluruh akun lain dari menu **User & Akses**. Tidak ada pendaftaran publik.

## Role

- Admin HDK: semua proyek, full edit/update.
- Internal HDK: semua proyek, read-only.
- Owner: hanya proyek assigned, read-only.
- Konsultan Pengawas: hanya proyek assigned, read-only.

Hak akses proyek dicek di backend. Mengubah query `?project=...` tidak memberikan akses ke proyek lain.

## Persistent storage

Folder `data` harus persistent karena berisi SQLite, Excel, foto, logo, snapshot, dan user access.

Contoh Docker:

```bash
docker run -d --name hdk-dashboard --restart unless-stopped \
  -p 8501:8501 \
  -v /opt/hdk-dashboard/data:/app/data \
  -e HDK_ADMIN_USERNAME=admin.hdk \
  -e HDK_ADMIN_EMAIL=admin@hdk.co.id \
  -e HDK_ADMIN_PASSWORD='PASSWORD_KUAT' \
  hdk-project-hub
```

Untuk produksi dengan banyak update bersamaan, pertimbangkan migrasi database dari SQLite ke PostgreSQL.
