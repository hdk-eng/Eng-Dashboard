# Deploy Web — HDK Project Data Hub v2.9.20

## Prinsip

- **Local Admin = WIP / update / upload / review.**
- **Web = Published / read-only.**
- GitHub hanya menyimpan kode aplikasi.
- Excel, foto, BIM screenshot, SQLite Published, dan histori publish disimpan di storage terpisah.

## Pengujian lokal sebelum deploy

1. Publish minimal satu proyek dari menu **Publish & Sync**.
2. Jalankan `START_WEB_PREVIEW.bat`.
3. Script membangun `data/web_preview/project_hub.db` hanya dari paket **Current Published**.
4. Login dengan user Internal/Owner/Konsultan untuk memeriksa hak akses dan tampilan web.

## Server produksi dengan folder Published yang mounted

Folder Published harus berada di lokasi terpisah dari folder data web. Contoh:

```text
/mnt/hdk-published        <- Google Drive/rclone/shared storage
/opt/hdk-web-data         <- database + asset runtime web
```

Environment:

```text
HDK_WEB_PUBLISHED_ONLY=1
HDK_AUTO_SYNC_PUBLISHED=1
HDK_PUBLISH_ROOT=/mnt/hdk-published
LOCAL_DB_PATH=/opt/hdk-web-data/project_hub.db
```

Saat aplikasi web start, jika `published_index.json` berubah, aplikasi dapat membangun ulang data web dari Current Published packages. Akun Admin tidak tersedia pada Published Web.

> Jangan meletakkan `HDK_PUBLISH_ROOT` di dalam folder target data web karena proses sync mengganti target data secara utuh.

## Google Drive

Pada komputer Admin, menu **Publish & Sync** dapat diarahkan ke folder Google Drive for Desktop. Pada server, folder yang sama dapat diakses dengan Google Drive/rclone mount.

Streamlit Community Cloud tidak memiliki akses langsung ke folder Google Drive Desktop di PC Admin. Jika tetap memakai Community Cloud, diperlukan konektor Google Drive API/object storage pada tahap deployment berikutnya.

## Security

- Tidak ada pendaftaran publik.
- Admin Lokal memakai PIN dan hanya tersedia saat `HDK_LOCAL_MODE=1` pada localhost.
- Paket Published menghapus akun role `admin`.
- Internal HDK melihat semua proyek Published.
- Owner/Konsultan hanya melihat project assignment mereka.
- Hak akses dicek di backend, bukan hanya dropdown proyek.
