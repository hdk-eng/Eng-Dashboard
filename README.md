# HDK Project Data Hub v2.10

## v2.10 — Reporting & Portfolio

- Menambahkan **Konsolidasi Proyek** untuk Admin/Internal HDK: status, owner, lokasi, tanggal mulai, Effective Finish, jumlah fungsi, dan visual.
- Master Proyek memiliki **Status Proyek**: Aktif, On Hold, Selesai, atau Terminasi.
- **Effective Finish** memakai Revised Finish bila tersedia, jika tidak memakai Finish Contract.
- Menambahkan **Periode Laporan** dengan workflow Draft → Published dan revision history per periode.
- Menambahkan export **PDF laporan proyek** serta tombol membuat draft email laporan.
- Menambahkan **Profil Saya** agar user non-admin dapat memperbarui nama/email dan mengganti password sendiri dengan verifikasi password lama.
- Semua perubahan database memakai migrasi additive/backward-compatible; data proyek, user, Excel, foto/BIM, dan histori lama tidak dihapus.
- Dependency baru: `fpdf2` untuk PDF report.

> Catatan logo HDK: source repo saat ini tidak memiliki asset logo HDK resmi yang dapat diverifikasi. Karena logo perusahaan bersifat krusial, v2.10 tidak membuat/mengarang logo pengganti. Asset resmi dapat ditambahkan setelah file logo resmi tersedia.

## WIP → Publish → Web

## Perbaikan v2.9.20 — Portable Project Paths & Publish Integrity

- Path Excel, foto, BIM, dan logo di SQLite sekarang disimpan **relatif terhadap folder `data`**, bukan path absolut build/version.
- Saat pindah folder aplikasi/versi, referensi file tetap valid selama folder `data` ikut dipindahkan.
- Jika database lama masih menunjuk ke build sebelumnya, aplikasi mencoba memulihkan file dari folder versi lama yang berada di lokasi sibling yang sama, lalu menyalinnya ke folder proyek aktif.
- Publish sekarang memiliki **integrity gate**: paket Published tidak boleh dibuat jika masih ada Excel/foto/BIM/logo yang hilang.
- Menu Publish menyediakan tombol **Coba Pulihkan File dari Versi Sebelumnya**.
- Struktur per proyek tetap terisolasi berdasarkan Internal Project ID.

## Perbaikan sebelumnya — Publish Session State
- Memperbaiki error Streamlit `publish_root_path cannot be modified after the widget ... is instantiated`.
- Tombol **Pakai Folder Lokal** sekarang memakai pending state lalu rerun, sehingga path dapat diganti tanpa bentrok dengan widget.
- Tombol **Simpan Lokasi** tetap menyimpan path yang sedang tampil.

- Folder Published divalidasi dan dibuat otomatis sebelum publish.
- Ada tombol **Pakai Folder Lokal** jika path Google Drive/drive letter tidak tersedia.
- Error publish sekarang menyebut tahap yang gagal, bukan hanya `[WinError 3]`.
- Paket publish setengah jadi otomatis dibersihkan jika proses gagal.
- Nama folder paket diperpendek agar lebih aman terhadap batas panjang path Windows.
- Virtual environment dipindahkan ke `%LOCALAPPDATA%\HDKDataHub\venv` agar instalasi Python tidak terkena `WinError 206` walaupun folder aplikasi panjang.

## Struktur penyimpanan v2.9.20 — per proyek

File fisik WIP sekarang **tidak lagi dicampur** pada folder global berdasarkan jenis file. Setiap proyek memiliki satu folder sendiri berdasarkan Project Code + Internal Project ID yang immutable:

```text
data/
├── project_hub.db                    # database pusat: project, user, access, register
├── projects/
│   ├── 5HDK10__<project_id>/
│   │   ├── source/excel/             # seluruh workbook dan versi sumber proyek ini
│   │   ├── photos/                   # foto lapangan
│   │   ├── bim/                      # BIM screenshot / visual
│   │   ├── assets/                   # 4 logo/asset proyek
│   │   └── snapshots/                # ruang snapshot fisik bila diperlukan
│   └── 5HDK11__<project_id>/
│       └── ...
└── web_published/
    └── projects/                     # paket Published per proyek + histori versi
```

- Internal Project ID menjadi jangkar folder sehingga perubahan nama/kode proyek tidak mencampur data.
- Saat aplikasi lokal pertama kali dibuka, layout lama `data/uploads/excel`, `data/photos`, dan `data/project_assets` dimigrasikan otomatis ke folder proyek masing-masing dan path database ikut diperbarui.
- Folder lama hanya dibersihkan jika benar-benar kosong; file yang tidak dikenali tidak dihapus.
- Publish tetap dilakukan **per proyek** ke `web_published`, sehingga Project A tidak menimpa Project B.

---

Versi ini menambahkan workflow publikasi agar data proyek tidak perlu di-upload ke GitHub dan perubahan lokal tidak langsung terlihat oleh Owner/Konsultan.

Alur utama:

```text
LOCAL / WIP
  ↓ Admin review
Publish & Sync
  ↓ paket per proyek + histori versi
Published Folder / Google Drive for Desktop
  ↓ sync
WEB / READ ONLY
```

### Yang baru

- Menu **Publish & Sync** khusus Admin Lokal.
- Publish dilakukan **per proyek**, bukan seluruh database sekaligus.
- Setiap publish membuat versi baru; versi lama tidak ditimpa.
- Ada status **Belum Publish / Ada Perubahan / Up To Date**.
- Preview jumlah dataset, row, file sumber, foto/BIM, snapshot, dan user access sebelum publish.
- Paket Published berbentuk folder versi + ZIP self-contained.
- Akun Admin **tidak ikut** ke paket Published.
- Owner/Konsultan/Internal dan hak akses proyek ikut ke paket web.
- Ada **Rollback Current Published** tanpa mengubah WIP lokal.
- Folder Published dapat diarahkan ke folder **Google Drive for Desktop**.
- `START_WEB_PREVIEW.bat` membangun web read-only dari **Current Published** saja, sehingga hasil publish dapat diuji sebelum deploy.
- `SYNC_PUBLISHED_TO_WEB.py` / `.bat` membangun database web dari seluruh Current Published project packages.

### Cara uji lokal

1. Jalankan `START_DASHBOARD.bat`.
2. Masuk sebagai Admin Lokal dengan PIN.
3. Pilih proyek → **Publish & Sync**.
4. Untuk tahap awal, pakai folder default `data/web_published`, atau arahkan ke folder Google Drive for Desktop.
5. Centang konfirmasi review → klik **PUBLISH PROJECT INI**.
6. Tutup dashboard lokal bila perlu, lalu jalankan `START_WEB_PREVIEW.bat`.
7. Login memakai user Internal/Owner/Konsultan. Web Preview hanya menampilkan data yang sudah Published.

### Google Drive

Google Drive dipakai sebagai **media sinkronisasi paket Published**, bukan tempat menjalankan SQLite secara langsung. Pilih folder yang sudah disinkron oleh Google Drive for Desktop, misalnya:

```text
G:\My Drive\HDK Project Data Hub\Published
```

GitHub tetap hanya berisi source code aplikasi. Folder `data/`, paket Published, foto, Excel, dan database tidak perlu di-commit.

> Streamlit Community Cloud tidak dapat membaca folder Google Drive for Desktop di komputer lokal secara langsung. Untuk produksi, gunakan server dengan folder Drive/rclone yang mounted, atau konektor Google Drive API. Struktur paket v2.9.20 sudah disiapkan agar tahap konektor tersebut tidak mengubah workflow Admin.

---


## v2.9.13 — Nama User / Username Login

Untuk pengujian **lokal**, jalankan `START_DASHBOARD.bat`. Launcher ini mengaktifkan `HDK_LOCAL_MODE=1` dan Streamlit tetap diikat ke `127.0.0.1` (localhost).

- Jika database belum punya user, aplikasi otomatis masuk sebagai **Admin Lokal** agar Admin dapat membuat akun pertama dari menu **User & Akses**.
- Jika database lama sudah berisi user dan login Admin bermasalah, halaman Login lokal menampilkan tombol **Masuk sebagai Admin Lokal**.
- Admin Lokal dapat membuat/reset akun user, mengatur role, dan akses proyek.
- Tombol **Keluar / Uji Login User** mengembalikan halaman Login sehingga akun Owner/Konsultan/Internal dapat diuji.
- Tombol Admin Lokal **tidak tersedia** pada Streamlit Cloud/server normal karena `HDK_LOCAL_MODE` tidak diaktifkan di sana.
- Login user sekarang memakai **Nama User / Username**; email tetap disimpan sebagai identitas/kontak dan sementara masih dapat dipakai sebagai fallback kompatibilitas.
- Admin membuat `Nama User`, nama lengkap, email, role, password awal, dan akses proyek dari menu **User & Akses**.
- Deployment web memakai `HDK_ADMIN_USERNAME` + `HDK_ADMIN_EMAIL` + `HDK_ADMIN_PASSWORD` untuk recovery/system admin.


## Perubahan utama v2.9.10

- Master Proyek sekarang memiliki **4 identitas organisasi**: Owner, Konsultan Perencana, Konsultan Pengawas, dan Kontraktor.
- Masing-masing identitas memiliki nama perusahaan dan logo tersendiri.
- Header proyek diubah menjadi **compact 4-party brand strip** yang lebih kecil, ringan, dan seragam.
- Logo tetap auto-crop dan auto-fit tanpa mengubah rasio; logo lama tetap kompatibel.
- Database lama dimigrasikan otomatis dengan field `consultant_planner_name` dan `logo_consultant_planner_path`.
- Data proyek lama, Excel, foto, snapshot, user access, dan project ID tidak berubah.


## User & Project Access (v2.9.8)

Aplikasi sekarang memakai login dan hak akses per proyek:

- **Admin HDK**: semua proyek + upload/edit/delete/User Management.
- **Internal HDK**: semua proyek, read-only.
- **Owner**: hanya proyek yang ditugaskan, read-only.
- **Konsultan Pengawas**: hanya proyek yang ditugaskan, read-only.

Admin mengelola user dari menu **User & Akses**. Hak akses juga divalidasi pada `project_id`, sehingga user eksternal tidak dapat membuka proyek lain hanya dengan mengganti URL.

### Bootstrap Admin pertama

Pada server/Streamlit Secrets set:

```toml
HDK_ADMIN_USERNAME = "admin.hdk"
HDK_ADMIN_EMAIL = "admin@hdk.co.id"
HDK_ADMIN_PASSWORD = "password-yang-kuat"
```

System/Recovery Admin dijaga tetap tersedia. Setelah login, seluruh user operasional dikelola oleh Admin dari menu **User & Akses**.

# HDK Project Data Hub v2.9.10

Project Data Hub untuk **multi-proyek, multi-file, multi-sheet**, dengan Excel sebagai media bulk update, SQLite sebagai master data lokal/server, visual dashboard, foto lapangan, screenshot 3D BIM, BIMx, version log, snapshot, dan Public Viewer read-only.



## Perubahan utama v2.9.6
- Menambahkan **Proyek Aktif / Active Project Context Bar** yang selalu terlihat di aplikasi.
- Admin dapat mengganti proyek langsung dari tombol **Ganti Proyek** tanpa kehilangan konteks halaman.
- Halaman Update Data menampilkan tujuan update dengan kode + nama proyek secara eksplisit.
- Tombol simpan/timpa file menyebut kode proyek tujuan.
- Halaman Foto & BIM Update menampilkan proyek tujuan upload secara eksplisit.
- Halaman Master Proyek menampilkan **Edit Master Proyek: [kode · nama]** agar tidak salah edit proyek.
- Public Viewer tetap read-only dan menampilkan proyek yang sedang dibuka.

## Perubahan utama v2.9.5

- Seluruh modul **Engineering & BIM** memakai tampilan seragam berupa donut status: Model Progress, Clash/Issue, RFI, SI, Shop Drawing, APM, dan WMS.
- Di bawah setiap donut tersedia tombol status yang dapat diklik (Open, Close, Coordination, Approved, Revision, Submit, dll.) untuk membuka **window detail terfilter**.
- Window detail Shop Drawing mempertahankan hierarki **Fungsi → Subfungsi → Judul Shop Drawing**.
- Nilai tanggal kosong/invalid disanitasi sehingga `NaT`/`NaN` tidak tampil di UI.
- Parser Shop Drawing membaca header kelompok fungsi dan subfungsi MEP dari workbook Engineering aktual.
- Semua perubahan v2.9.4 dan sebelumnya tetap dipertahankan.

## Perubahan utama v2.9.4

- Master Proyek: Start Construction, Finish Contract, dan Revised Finish memakai date picker / kalender.
- Nilai kontrak diinput sebagai teks Rupiah (mis. 125.000.000.000) lalu disimpan sebagai angka.
- Status PPN dipisah: **Termasuk PPN** atau **Belum termasuk PPN**.
- Database lama dimigrasikan otomatis dengan kolom `contract_vat_status` tanpa menghapus data lama.
- Seluruh perbaikan visual dan Gantt dari build sebelumnya tetap dipertahankan.

## Perubahan utama v2.9.1

### Perbaikan Gantt v2.9.1

- Nama kegiatan dipindahkan ke kolom subplot khusus sehingga tidak lagi ter-clipping/hilang.
- Layout Gantt: **Kegiatan | Rencana/Realisasi | Kalender**.
- Nama kegiatan rata kiri dan dapat membungkus maksimal dua baris.
- Gantt memakai tema putih khusus sehingga hasil **Download plot as PNG** tidak lagi berlatar hitam.
- Export PNG melalui toolbar Plotly diset ke resolusi 2x.
- Header bulan tetap menempel pada deret tanggal harian dan diberi border yang jelas.


### 1. Pengelompokan memakai Fungsi Sheet, bukan Fungsi File
File Excel sekarang hanya berperan sebagai **source/container**. Pengelompokan web mengikuti fungsi setiap sheet.

Contoh Master Deliverables Engineering:
- MODEL PROGRESS → `Engineering & BIM · Model Progress`
- CLASH & ISSUE → `Engineering & BIM · Clash & Issue`
- RFI → `Engineering & BIM · RFI`
- SI → `Engineering & BIM · Site Instruction`
- Shopdrawing → `Engineering & BIM · Shop Drawing`
- APM → `Engineering & BIM · Material Approval`
- WMS → `Engineering & BIM · Work Method Statement`

`DASHBOARD` Excel dan `Sheet1`/reference **tidak dipilih secara default**.

### 2. Register Engineering memakai Incremental Register Sync
Untuk sheet Engineering yang dikenali, aplikasi tidak meminta key CRUD generik.

- ID baru → tambah record.
- ID lama → update kolom yang berubah.
- Record lama yang tidak ada pada upload baru → tidak dihapus otomatis.
- Preview menunjukkan jumlah **Baru / Berubah / Tetap** dan perubahan field sebelum Apply Update.
- Ada tombol **Apply Update Semua Register Engineering** untuk sinkronisasi workbook sekaligus.

Key otomatis:
- MODEL PROGRESS → ID model.
- RFI/SI/APM/WMS → nomor register.
- CLASH & ISSUE → nomor issue internal.
- Shop Drawing → nomor dokumen; bila template masih berisi placeholder, dibuat ID otomatis stabil dan ditampilkan warning data quality.

### 3. Dashboard dibersihkan
Elemen teknis berikut dihapus dari Executive Dashboard:
- jumlah file sumber;
- jumlah sheet;
- baris SQLite;
- tabel `Sumber informasi`;
- `Semua file & sheet yang tersedia`.

Informasi tersebut tetap tersedia di Admin / Data Management.

### 4. Data per tanggal (As-of Date)
Executive Dashboard sekarang memiliki pilihan **Data per tanggal**.

Jika dipilih 01 Oct 2026, setiap fungsi memakai versi terakhir yang tersedia **pada atau sebelum** tanggal tersebut. Dengan begitu Kurva S, Engineering, foto, dan fungsi lain tetap dapat ditampilkan walaupun waktu upload masing-masing berbeda.

### 5. Gantt 2 Mingguan diperbaiki
`2 Minggu Lalu`:
- layout dibagi jelas menjadi **Kegiatan | Baris | Kalender**;
- nama kegiatan hanya ditulis satu kali dan rata kiri pada panel khusus;
- `Rencana` dan `Realisasi` berada pada kolom tersendiri sehingga tidak menumpuk dengan nama kegiatan;
- tidak ada lagi `Realisasi (1)`, `Realisasi (2)`, dst.;
- bulan berada tepat di atas tanggal dan dibingkai sebagai grup kalender;
- tanggal harian berada langsung di bawah header bulan;
- weekend diberi shading ringan;
- pergeseran RA vs RI terbaca langsung.

`2 Minggu ke Depan`:
- nama kegiatan menggunakan panel kiri yang lega;
- kalender harian dan header bulan menggunakan struktur yang sama;
- visual Gantt rencana lebih bersih dan konsisten.

### 6. Header proyek dirapikan
- subtitle panjang `Executive Project Dashboard · ...` dihapus dari dashboard utama;
- logo Owner, Konsultan Pengawas, dan Kontraktor dibuat **lebih kecil dan compact**;
- normalisasi logo otomatis tetap aktif untuk logo landscape, square, maupun tall;
- tinggi strip identitas proyek dikurangi agar ruang dashboard lebih efektif.

### 7. Foto dan BIM 3D diseimbangkan
Executive Dashboard:
- kiri: **4 Foto Progress Lapangan** dalam grid 2×2;
- kanan: **2 Screenshot Update 3D BIM** lebih besar dan ditumpuk vertikal.

Batch lama tetap tersimpan dan dapat dilihat lewat histori.

### 8. BIMx
Link BIMx disimpan per proyek pada Master Proyek.

Executive Dashboard menyediakan:
- tombol **Buka Model BIMx ↗**;
- opsi mencoba menampilkan BIMx langsung dalam iframe;
- fallback tetap membuka model melalui link jika BIMx menolak embed.

### 9. Visual Sheet berdasarkan fungsi
Menu Visual Sheet sekarang memakai alur:

`Fungsi → Sheet/Register → Versi`

Nama file Excel hanya menjadi metadata sumber, bukan navigasi utama pengguna.

### 10. Quick Edit
CRUD generik diganti nama menjadi **Quick Edit Data**.

Bulk update tetap melalui Excel. Quick Edit dipakai untuk koreksi individual seperti:
- Status;
- Tanggal Close;
- Response Date;
- Remarks;
- field tertentu lainnya.

Perubahan tetap dicatat di audit log.

## Profil data otomatis

- Kurva S → period-based update berdasarkan Week dan Realisasi.
- Rencana 2 Mingguan → snapshot periodik; RA vs RI untuk 2 Minggu Lalu.
- Engineering registers → incremental sync.
- Tabel lain → generic import/upsert bila dibutuhkan.

## Master Proyek
Editable di Admin Mode:
- Kode proyek
- Nama proyek
- Owner / Client
- Konsultan Pengawas
- Kontraktor
- Lokasi
- No. kontrak
- Start / Finish / Revised Finish
- Nilai kontrak
- 3 logo resmi
- Link BIMx
- Deskripsi

Internal Project ID dikunci agar seluruh relasi data tidak terputus.

Logo otomatis di-crop, aspect ratio dipertahankan, lalu dinormalisasi ke canvas standar agar logo dengan bentuk berbeda terlihat seimbang.

## Multi-proyek
Semua upload terikat ke **Project ID aktif**. Data antar-proyek tidak bercampur.

## Public Viewer / Admin Mode
- Public Viewer: hanya Dashboard Proyek + Visual Sheet, read-only.
- Admin Mode: upload/update, foto, Quick Edit, Master Proyek, file management, backup.
- Password admin: environment variable `HDK_ADMIN_PASSWORD`.
- Link publik: `?view=public&project=<INTERNAL_PROJECT_ID>`.

## Penyimpanan
Semua data persisten ada di folder `data`:

- `data/project_hub.db` → SQLite, metadata, register, snapshot, audit log.
- `data/uploads/excel/...` → arsip Excel per proyek & versi.
- `data/photos/...` → foto progress dan screenshot BIM.
- `data/project_assets/...` → logo proyek.

Backup **FULL BACKUP ZIP** membawa seluruh folder data.

## Menjalankan di Windows
1. Extract ZIP ke folder biasa.
2. Jalankan `CEK_PYTHON.bat` bila perlu.
3. Jalankan `START_DASHBOARD.bat`.
4. Launcher otomatis mencari port kosong mulai 8501.

## Deployment Web
Lihat `DEPLOY_WEB.md`.

## Migrasi dari v2.7.x
Folder `data` dari versi lama dapat dipertahankan. Saat file sumber disimpan ulang di v2.9, fungsi sheet lama yang masih terlalu umum seperti `Engineering & BIM`, `Progress & Schedule`, atau `Other` akan dipetakan ulang otomatis ke fungsi sheet yang lebih spesifik bila profilnya dikenali.

### v2.9.13 — Locked Local Admin
- Form login umum hanya menerima akun `Internal`, `Owner`, dan `Konsultan`.
- Role `Admin` ditolak dari form login umum walaupun username/password diketahui.
- Admin lokal hanya dapat masuk dari launcher localhost (`START_DASHBOARD.bat`) menggunakan PIN khusus.
- Pada pemakaian pertama, sistem meminta pembuatan PIN Admin Lokal minimal 8 karakter.
- PIN tidak disimpan sebagai teks biasa; file `data/.local_admin_pin` hanya berisi salt + PBKDF2 hash.
- Jika PIN terlupa, jalankan `RESET_LOCAL_ADMIN_PIN.bat`, lalu buat PIN baru dari localhost.
- User Management tidak lagi menawarkan pembuatan akun Admin biasa.
