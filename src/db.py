from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
import hashlib
import secrets
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("LOCAL_DB_PATH", str(BASE_DIR / "data" / "project_hub.db")))


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # timeout/busy_timeout mencegah UI terasa hang ketika dua rerun Streamlit
    # menyentuh SQLite dalam waktu sangat berdekatan.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def sanitize_identifier(name: str, fallback: str = "col") -> str:
    text = str(name).strip().lower()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^0-9a-zA-Z_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"c_{text}"
    return text[:64]


def make_unique_columns(columns: list[Any]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for i, raw in enumerate(columns, start=1):
        base = str(raw).strip() if raw is not None and str(raw).strip() else f"Column_{i}"
        if base == "_row_id":
            base = "_row_id_data"
        if base not in seen:
            seen[base] = 1
            result.append(base)
        else:
            seen[base] += 1
            result.append(f"{base}_{seen[base]}")
    return result


def _infer_sql_type(series: pd.Series) -> str:
    non_null = series.dropna()
    if non_null.empty:
        return "TEXT"
    if pd.api.types.is_bool_dtype(non_null):
        return "INTEGER"
    if pd.api.types.is_integer_dtype(non_null):
        return "INTEGER"
    if pd.api.types.is_numeric_dtype(non_null):
        return "REAL"
    if pd.api.types.is_datetime64_any_dtype(non_null):
        return "TEXT"
    sample = non_null.head(250)
    numeric = pd.to_numeric(sample, errors="coerce")
    if numeric.notna().mean() >= 0.98:
        fractional = (numeric.dropna() % 1 != 0).any()
        return "REAL" if fractional else "INTEGER"
    return "TEXT"


def _python_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, (np.bool_, bool)):
        return int(bool(value))
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def init_db() -> None:
    with _connect() as conn:
        # Set WAL sekali saat inisialisasi, bukan pada setiap koneksi/rerun.
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                client TEXT,
                location TEXT,
                description TEXT,
                bimx_url TEXT,
                contract_no TEXT,
                contract_start TEXT,
                contract_finish TEXT,
                revised_finish TEXT,
                project_manager TEXT,
                site_manager TEXT,
                contract_value REAL,
                contract_vat_status TEXT,
                report_period TEXT,
                bim_revision TEXT,
                contractor_name TEXT,
                consultant_planner_name TEXT,
                consultant_name TEXT,
                logo_contractor_path TEXT,
                logo_owner_path TEXT,
                logo_consultant_planner_path TEXT,
                logo_consultant_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS datasets (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                function_name TEXT NOT NULL,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_ref TEXT,
                sheet_name TEXT,
                table_name TEXT NOT NULL UNIQUE,
                column_schema TEXT NOT NULL,
                row_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS function_sources (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                function_name TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                sheet_name TEXT,
                header_row INTEGER NOT NULL DEFAULT 1,
                key_columns TEXT NOT NULL,
                last_filename TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, function_name),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(dataset_id) REFERENCES datasets(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                function_name TEXT NOT NULL,
                dataset_id TEXT,
                filename TEXT NOT NULL,
                sheet_name TEXT,
                row_total INTEGER NOT NULL DEFAULT 0,
                inserted INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS photos (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                captured_date TEXT,
                zone TEXT,
                category TEXT,
                wbs_activity TEXT,
                description TEXT,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS source_files (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                function_name TEXT,
                description TEXT,
                last_size INTEGER NOT NULL DEFAULT 0,
                last_uploaded_at TEXT,
                current_file_path TEXT,
                current_version_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, filename),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS source_versions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                source_file_id TEXT NOT NULL,
                label TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(source_file_id) REFERENCES source_files(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS source_sheets (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                source_file_id TEXT NOT NULL,
                sheet_name TEXT NOT NULL,
                display_name TEXT,
                description TEXT,
                function_name TEXT,
                selected INTEGER NOT NULL DEFAULT 1,
                dataset_id TEXT,
                header_row INTEGER NOT NULL DEFAULT 1,
                key_columns TEXT NOT NULL DEFAULT '[]',
                update_mode TEXT NOT NULL DEFAULT 'auto',
                formula_mode TEXT NOT NULL DEFAULT 'values',
                include_excel_row INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_file_id, sheet_name),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(source_file_id) REFERENCES source_files(id) ON DELETE CASCADE,
                FOREIGN KEY(dataset_id) REFERENCES datasets(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS data_snapshots (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                source_file_id TEXT,
                source_sheet_id TEXT,
                source_version_id TEXT,
                dataset_id TEXT NOT NULL,
                label TEXT NOT NULL,
                file_name TEXT,
                table_name TEXT NOT NULL UNIQUE,
                column_schema TEXT NOT NULL,
                row_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(source_file_id) REFERENCES source_files(id) ON DELETE SET NULL,
                FOREIGN KEY(source_sheet_id) REFERENCES source_sheets(id) ON DELETE SET NULL,
                FOREIGN KEY(dataset_id) REFERENCES datasets(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT,
                dataset_id TEXT,
                action TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                username TEXT,
                full_name TEXT,
                role TEXT NOT NULL CHECK(role IN ('admin','internal','owner','consultant')),
                password_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_project_access (
                user_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                access_level TEXT NOT NULL DEFAULT 'view',
                created_at TEXT NOT NULL,
                PRIMARY KEY(user_id, project_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            """
        )

        # Migrasi v2.5: Master Proyek dapat diedit lebih lengkap tanpa mengubah
        # internal project UUID yang dipakai relasi data.
        project_cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
        project_additions = {
            "contract_no": "TEXT",
            "contract_start": "TEXT",
            "contract_finish": "TEXT",
            "revised_finish": "TEXT",
            "project_manager": "TEXT",
            "site_manager": "TEXT",
            "contract_value": "REAL",
            "contract_vat_status": "TEXT",
            "report_period": "TEXT",
            "bim_revision": "TEXT",
            "contractor_name": "TEXT",
            "consultant_planner_name": "TEXT",
            "consultant_name": "TEXT",
            "logo_contractor_path": "TEXT",
            "logo_owner_path": "TEXT",
            "logo_consultant_planner_path": "TEXT",
            "logo_consultant_path": "TEXT",
        }
        for col, typ in project_additions.items():
            if col not in project_cols:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {quote_ident(col)} {typ}")

        # Migrasi v2.9.12: login memakai Nama User / Username.
        # Email tetap disimpan sebagai identitas/kontak dan sebagai fallback kompatibilitas.
        user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "username" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN username TEXT")

        rows = conn.execute("SELECT id, email, full_name, username FROM users ORDER BY created_at, id").fetchall()
        used_usernames: set[str] = set()
        for row in rows:
            current_username = str(row["username"] or "").strip().lower()
            if current_username:
                base = re.sub(r"[^a-z0-9._-]+", "-", current_username).strip("-._") or "user"
            else:
                email_local = str(row["email"] or "").split("@", 1)[0].strip().lower()
                name_base = str(row["full_name"] or "").strip().lower().replace(" ", ".")
                base = email_local or name_base or "user"
                base = re.sub(r"[^a-z0-9._-]+", "-", base).strip("-._") or "user"
            candidate = base[:48]
            suffix = 2
            while candidate.lower() in used_usernames:
                tail = f"-{suffix}"
                candidate = f"{base[:48-len(tail)]}{tail}"
                suffix += 1
            used_usernames.add(candidate.lower())
            if current_username != candidate:
                conn.execute("UPDATE users SET username=? WHERE id=?", (candidate, row["id"]))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_nocase ON users(username COLLATE NOCASE)")

        # Migrasi v2.4: arsip file asli per versi untuk visual sheet/historical view.
        sf_cols = {r[1] for r in conn.execute("PRAGMA table_info(source_files)").fetchall()}
        if "current_file_path" not in sf_cols:
            conn.execute("ALTER TABLE source_files ADD COLUMN current_file_path TEXT")
        if "current_version_id" not in sf_cols:
            conn.execute("ALTER TABLE source_files ADD COLUMN current_version_id TEXT")
        snap_cols = {r[1] for r in conn.execute("PRAGMA table_info(data_snapshots)").fetchall()}
        if "source_version_id" not in snap_cols:
            conn.execute("ALTER TABLE data_snapshots ADD COLUMN source_version_id TEXT")

        # Migrasi v2.4.2: media update dibedakan per batch agar 4 foto progress
        # dan 2 screenshot BIM terbaru tidak bercampur dengan batch sebelumnya.
        photo_cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
        if "media_type" not in photo_cols:
            conn.execute("ALTER TABLE photos ADD COLUMN media_type TEXT")
        if "batch_id" not in photo_cols:
            conn.execute("ALTER TABLE photos ADD COLUMN batch_id TEXT")
        if "batch_label" not in photo_cols:
            conn.execute("ALTER TABLE photos ADD COLUMN batch_label TEXT")
        if "display_order" not in photo_cols:
            conn.execute("ALTER TABLE photos ADD COLUMN display_order INTEGER NOT NULL DEFAULT 0")

        # Migrasi ringan dari struktur v2.2 (1 fungsi = 1 sheet) ke registry
        # multi-file/multi-sheet. Aman dijalankan berulang karena UNIQUE constraints.
        legacy_rows = conn.execute(
            """
            SELECT fs.project_id, fs.function_name, fs.dataset_id, fs.sheet_name, fs.header_row,
                   fs.key_columns, fs.last_filename, fs.created_at, fs.updated_at
            FROM function_sources fs
            WHERE COALESCE(fs.last_filename, '') <> ''
            """
        ).fetchall()
        for r in legacy_rows:
            file_row = conn.execute(
                "SELECT id FROM source_files WHERE project_id=? AND filename=?",
                (r["project_id"], r["last_filename"]),
            ).fetchone()
            if file_row:
                sfid = file_row["id"]
            else:
                sfid = uuid.uuid4().hex[:12]
                conn.execute(
                    """INSERT INTO source_files(id, project_id, filename, function_name, description, last_size,
                       last_uploaded_at, created_at, updated_at) VALUES (?, ?, ?, ?, '', 0, ?, ?, ?)""",
                    (sfid, r["project_id"], r["last_filename"], r["function_name"], r["updated_at"], r["created_at"], r["updated_at"]),
                )
            existing_sheet = conn.execute(
                "SELECT id FROM source_sheets WHERE source_file_id=? AND sheet_name=?",
                (sfid, r["sheet_name"] or "Sheet1"),
            ).fetchone()
            if not existing_sheet:
                conn.execute(
                    """INSERT INTO source_sheets(id, project_id, source_file_id, sheet_name, display_name, description,
                       function_name, selected, dataset_id, header_row, key_columns, update_mode, formula_mode,
                       include_excel_row, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, '', ?, 1, ?, ?, ?, 'auto', 'values', 0, ?, ?)""",
                    (uuid.uuid4().hex[:12], r["project_id"], sfid, r["sheet_name"] or "Sheet1",
                     r["sheet_name"] or "Sheet1", r["function_name"], r["dataset_id"], int(r["header_row"] or 1),
                     r["key_columns"] or "[]", r["created_at"], r["updated_at"]),
                )


def _audit(conn: sqlite3.Connection, project_id: str | None, dataset_id: str | None, action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log(project_id, dataset_id, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (project_id, dataset_id, action, detail, datetime.now().isoformat(timespec="seconds")),
    )


def create_project(
    code: str, name: str, client: str = "", location: str = "", description: str = "", bimx_url: str = "",
    contract_no: str = "", contract_start: str = "", contract_finish: str = "", revised_finish: str = "",
    project_manager: str = "", site_manager: str = "", contract_value: float | None = None,
    contract_vat_status: str = "Belum termasuk PPN", report_period: str = "", bim_revision: str = "", contractor_name: str = "", consultant_planner_name: str = "", consultant_name: str = "",
    logo_contractor_path: str = "", logo_owner_path: str = "", logo_consultant_planner_path: str = "", logo_consultant_path: str = "",
) -> str:
    init_db()
    project_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO projects(
                id, code, name, client, location, description, bimx_url, contract_no, contract_start,
                contract_finish, revised_finish, project_manager, site_manager, contract_value, contract_vat_status,
                report_period, bim_revision, contractor_name, consultant_planner_name, consultant_name, logo_contractor_path,
                logo_owner_path, logo_consultant_planner_path, logo_consultant_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, code.strip(), name.strip(), client.strip(), location.strip(), description.strip(), bimx_url.strip(),
             contract_no.strip(), contract_start.strip(), contract_finish.strip(), revised_finish.strip(),
             project_manager.strip(), site_manager.strip(), contract_value, contract_vat_status.strip(), report_period.strip(), bim_revision.strip(),
             contractor_name.strip(), consultant_planner_name.strip(), consultant_name.strip(), logo_contractor_path.strip(), logo_owner_path.strip(),
             logo_consultant_planner_path.strip(), logo_consultant_path.strip(), now, now),
        )
        _audit(conn, project_id, None, "CREATE_PROJECT", f"{code} - {name}")
    return project_id


def update_project(project_id: str, **fields: Any) -> None:
    allowed = {
        "code", "name", "client", "location", "description", "bimx_url", "contract_no",
        "contract_start", "contract_finish", "revised_finish", "project_manager", "site_manager",
        "contract_value", "contract_vat_status", "report_period", "bim_revision", "contractor_name", "consultant_planner_name", "consultant_name",
        "logo_contractor_path", "logo_owner_path", "logo_consultant_planner_path", "logo_consultant_path",
    }
    updates: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "contract_value":
            updates[k] = None if v in (None, "") else float(v)
        else:
            updates[k] = (v or "").strip()
    if not updates:
        return
    with _connect() as conn:
        current_row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not current_row:
            raise KeyError(f"Project {project_id} tidak ditemukan")
        current = dict(current_row)
        changes = []
        for k, new_value in updates.items():
            old_value = current.get(k)
            if old_value != new_value:
                if k.startswith("logo_"):
                    old_show = "[logo ada]" if old_value else "[kosong]"
                    new_show = "[logo ada]" if new_value else "[kosong]"
                else:
                    old_show = "" if old_value is None else str(old_value)
                    new_show = "" if new_value is None else str(new_value)
                changes.append(f"{k}: {old_show} -> {new_show}")
        if not changes:
            return
        updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
        sets = ", ".join(f"{quote_ident(k)} = ?" for k in updates)
        conn.execute(f"UPDATE projects SET {sets} WHERE id = ?", list(updates.values()) + [project_id])
        _audit(conn, project_id, None, "UPDATE_PROJECT", " | ".join(changes))


def list_projects() -> pd.DataFrame:
    init_db()
    with _connect() as conn:
        return pd.read_sql_query(
            """
            SELECT p.*,
                   (SELECT COUNT(DISTINCT COALESCE(NULLIF(ss.function_name,''), sf.function_name))
                    FROM source_files sf LEFT JOIN source_sheets ss ON ss.source_file_id=sf.id AND ss.selected=1
                    WHERE sf.project_id=p.id) AS function_count,
                   (SELECT COUNT(*) FROM photos ph WHERE ph.project_id=p.id) AS photo_count
            FROM projects p ORDER BY p.updated_at DESC
            """,
            conn,
        )


def get_project(project_id: str) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        raise KeyError(f"Project {project_id} tidak ditemukan")
    return dict(row)


PASSWORD_ITERATIONS = 260_000


def _hash_password(password: str, salt_hex: str | None = None) -> str:
    if not password or len(password) < 8:
        raise ValueError("Password minimal 8 karakter.")
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt_hex, digest_hex = str(encoded).split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return secrets.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def user_count() -> int:
    init_db()
    with _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def _normalize_username(username: str) -> str:
    value = (username or "").strip().lower()
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^a-z0-9._-]+", "-", value).strip("-._")
    if len(value) < 3:
        raise ValueError("Nama User minimal 3 karakter.")
    if len(value) > 48:
        raise ValueError("Nama User maksimal 48 karakter.")
    return value


def ensure_system_admin(email: str, password: str, full_name: str = "Administrator HDK", username: str = "admin") -> str | None:
    """Pastikan recovery Admin selalu tersedia. Login utamanya memakai username."""
    init_db()
    email = (email or "").strip().lower()
    password = password or ""
    username = _normalize_username(username or "admin")
    if not email:
        email = f"{username}@hdk.local"
    if not password:
        return None
    if len(password) < 8:
        raise ValueError("HDK_ADMIN_PASSWORD minimal 8 karakter.")

    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE lower(username)=lower(?) OR lower(email)=lower(?) LIMIT 1",
            (username, email),
        ).fetchone()
        now = datetime.now().isoformat(timespec="seconds")

        if not row:
            user_id = uuid.uuid4().hex[:16]
            conn.execute(
                "INSERT INTO users(id,email,username,full_name,role,password_hash,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (user_id, email, username, full_name.strip(), "admin", _hash_password(password), 1, now, now),
            )
            conn.execute(
                "INSERT INTO audit_log(project_id,dataset_id,action,detail,created_at) VALUES(NULL,NULL,?,?,?)",
                ("CREATE_SYSTEM_ADMIN", f"{username} | {email}", now),
            )
            return user_id

        user = dict(row)
        user_id = user["id"]
        updates: dict[str, Any] = {}
        repaired: list[str] = []

        if str(user.get("username") or "").strip().lower() != username:
            conflict = conn.execute(
                "SELECT id FROM users WHERE lower(username)=lower(?) AND id<>?", (username, user_id)
            ).fetchone()
            if not conflict:
                updates["username"] = username
                repaired.append("username")
        if str(user.get("role") or "").lower() != "admin":
            updates["role"] = "admin"
            repaired.append("role")
        if int(user.get("active") or 0) != 1:
            updates["active"] = 1
            repaired.append("active")
        if not _verify_password(password, user.get("password_hash") or ""):
            updates["password_hash"] = _hash_password(password)
            repaired.append("password")
        if not str(user.get("full_name") or "").strip():
            updates["full_name"] = full_name.strip()
            repaired.append("name")
        if not str(user.get("email") or "").strip():
            updates["email"] = email
            repaired.append("email")

        if updates:
            updates["updated_at"] = now
            sets = ", ".join(f"{quote_ident(k)}=?" for k in updates)
            conn.execute(f"UPDATE users SET {sets} WHERE id=?", list(updates.values()) + [user_id])
            conn.execute(
                "INSERT INTO audit_log(project_id,dataset_id,action,detail,created_at) VALUES(NULL,NULL,?,?,?)",
                ("REPAIR_SYSTEM_ADMIN", f"{username}; synced={','.join(repaired)}", now),
            )
        return user_id


def ensure_bootstrap_admin(email: str, password: str, full_name: str = "Administrator HDK", username: str = "admin") -> str | None:
    """Backward-compatible alias for older callers."""
    return ensure_system_admin(email, password, full_name, username)


def authenticate_user(login_name: str, password: str) -> dict[str, Any] | None:
    """Login dengan Nama User. Email tetap diterima sebagai fallback selama masa migrasi."""
    init_db()
    login_name = (login_name or "").strip().lower()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM users
            WHERE active=1
              AND (lower(username)=lower(?) OR lower(email)=lower(?))
            LIMIT 1
            """,
            (login_name, login_name),
        ).fetchone()
    if not row:
        return None
    user = dict(row)
    if not _verify_password(password or "", user.get("password_hash") or ""):
        return None
    user.pop("password_hash", None)
    return user


def get_user(user_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out.pop("password_hash", None)
    return out


def list_users() -> pd.DataFrame:
    init_db()
    with _connect() as conn:
        return pd.read_sql_query(
            """
            SELECT u.id, u.username, u.email, u.full_name, u.role, u.active, u.created_at, u.updated_at,
                   (SELECT COUNT(*) FROM user_project_access a WHERE a.user_id=u.id) AS project_count
            FROM users u
            ORDER BY CASE u.role WHEN 'admin' THEN 0 WHEN 'internal' THEN 1 ELSE 2 END, lower(u.username)
            """,
            conn,
        )


def create_user(username: str, email: str, full_name: str, role: str, password: str, project_ids: list[str] | None = None, active: bool = True) -> str:
    init_db()
    username = _normalize_username(username)
    email = (email or "").strip().lower()
    role = (role or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("Email user tidak valid.")
    if role not in {"admin","internal","owner","consultant"}:
        raise ValueError("Role user tidak valid.")
    if len(password or "") < 8:
        raise ValueError("Password minimal 8 karakter.")
    now = datetime.now().isoformat(timespec="seconds")
    user_id = uuid.uuid4().hex[:16]
    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO users(id,email,username,full_name,role,password_hash,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (user_id, email, username, (full_name or "").strip(), role, _hash_password(password), int(bool(active)), now, now),
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc).lower()
            if "username" in message:
                raise ValueError("Nama User sudah digunakan.") from exc
            if "email" in message:
                raise ValueError("Email sudah digunakan.") from exc
            raise
        if role in {"owner","consultant"}:
            for pid in sorted(set(project_ids or [])):
                conn.execute(
                    "INSERT OR REPLACE INTO user_project_access(user_id,project_id,access_level,created_at) VALUES(?,?,?,?)",
                    (user_id, pid, "view", now),
                )
        conn.execute(
            "INSERT INTO audit_log(project_id,dataset_id,action,detail,created_at) VALUES(NULL,NULL,?,?,?)",
            ("CREATE_USER", f"{username} | {role}", now),
        )
    return user_id


def update_user(user_id: str, *, username: str | None = None, email: str | None = None, full_name: str | None = None, role: str | None = None, active: bool | None = None, password: str | None = None, project_ids: list[str] | None = None) -> None:
    init_db()
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise KeyError("User tidak ditemukan.")
        current = dict(row)
        new_role = (role or current["role"]).strip().lower()
        if new_role not in {"admin","internal","owner","consultant"}:
            raise ValueError("Role user tidak valid.")
        new_username = current.get("username") if username is None else _normalize_username(username)
        new_email = current.get("email") if email is None else (email or "").strip().lower()
        if not new_email or "@" not in new_email:
            raise ValueError("Email user tidak valid.")
        updates = {
            "username": new_username,
            "email": new_email,
            "full_name": current.get("full_name") if full_name is None else (full_name or "").strip(),
            "role": new_role,
            "active": int(current.get("active",1)) if active is None else int(bool(active)),
            "updated_at": now,
        }
        if password:
            if len(password) < 8:
                raise ValueError("Password minimal 8 karakter.")
            updates["password_hash"] = _hash_password(password)
        sets = ", ".join(f"{quote_ident(k)}=?" for k in updates)
        try:
            conn.execute(f"UPDATE users SET {sets} WHERE id=?", list(updates.values()) + [user_id])
        except sqlite3.IntegrityError as exc:
            message = str(exc).lower()
            if "username" in message:
                raise ValueError("Nama User sudah digunakan.") from exc
            if "email" in message:
                raise ValueError("Email sudah digunakan.") from exc
            raise
        conn.execute("DELETE FROM user_project_access WHERE user_id=?", (user_id,))
        if new_role in {"owner","consultant"}:
            for pid in sorted(set(project_ids or [])):
                conn.execute(
                    "INSERT INTO user_project_access(user_id,project_id,access_level,created_at) VALUES(?,?,?,?)",
                    (user_id, pid, "view", now),
                )
        conn.execute(
            "INSERT INTO audit_log(project_id,dataset_id,action,detail,created_at) VALUES(NULL,NULL,?,?,?)",
            ("UPDATE_USER", f"{new_username} | role={new_role} | active={updates['active']}", now),
        )


def user_project_ids(user_id: str) -> list[str]:
    init_db()
    with _connect() as conn:
        return [r[0] for r in conn.execute("SELECT project_id FROM user_project_access WHERE user_id=? ORDER BY project_id", (user_id,)).fetchall()]


def list_projects_for_user(user: dict[str, Any] | None) -> pd.DataFrame:
    init_db()
    if not user:
        return pd.DataFrame()
    role = str(user.get("role") or "").lower()
    if role in {"admin","internal"}:
        return list_projects()
    with _connect() as conn:
        return pd.read_sql_query(
            """
            SELECT p.*,
                   (SELECT COUNT(DISTINCT COALESCE(NULLIF(ss.function_name,''), sf.function_name))
                    FROM source_files sf LEFT JOIN source_sheets ss ON ss.source_file_id=sf.id AND ss.selected=1
                    WHERE sf.project_id=p.id) AS function_count,
                   (SELECT COUNT(*) FROM photos ph WHERE ph.project_id=p.id) AS photo_count
            FROM projects p
            JOIN user_project_access a ON a.project_id=p.id
            WHERE a.user_id=?
            ORDER BY p.updated_at DESC
            """,
            conn,
            params=(user.get("id"),),
        )


def user_can_access_project(user: dict[str, Any] | None, project_id: str | None) -> bool:
    if not user or not project_id:
        return False
    role = str(user.get("role") or "").lower()
    if role in {"admin","internal"}:
        return True
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM user_project_access WHERE user_id=? AND project_id=?", (user.get("id"), project_id)).fetchone()
    return bool(row)

def create_dataset(df: pd.DataFrame, project_id: str, function_name: str, name: str, source_type: str = "excel", source_ref: str = "", sheet_name: str = "") -> str:
    init_db()
    data = df.copy()
    data.columns = make_unique_columns(list(data.columns))
    dataset_id = uuid.uuid4().hex[:12]
    table_name = f"data_{dataset_id}"

    used_sql: dict[str, int] = {}
    schema: list[dict[str, str]] = []
    for idx, col in enumerate(data.columns, start=1):
        sql_name = sanitize_identifier(col, f"col_{idx}")
        if sql_name in used_sql:
            used_sql[sql_name] += 1
            sql_name = f"{sql_name}_{used_sql[sql_name]}"
        else:
            used_sql[sql_name] = 1
        schema.append({"display": str(col), "sql": sql_name, "type": _infer_sql_type(data[col])})

    defs = ["_row_id INTEGER PRIMARY KEY AUTOINCREMENT"]
    defs.extend(f"{quote_ident(x['sql'])} {x['type']}" for x in schema)
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(f"CREATE TABLE {quote_ident(table_name)} ({', '.join(defs)})")
        if not data.empty and schema:
            sql_cols = [x["sql"] for x in schema]
            placeholders = ",".join("?" for _ in sql_cols)
            insert_sql = f"INSERT INTO {quote_ident(table_name)} ({', '.join(quote_ident(c) for c in sql_cols)}) VALUES ({placeholders})"
            rows = [tuple(_python_value(v) for v in row) for row in data.itertuples(index=False, name=None)]
            conn.executemany(insert_sql, rows)
        conn.execute(
            """
            INSERT INTO datasets(id, project_id, function_name, name, source_type, source_ref, sheet_name,
                                 table_name, column_schema, row_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (dataset_id, project_id, function_name, name, source_type, source_ref, sheet_name, table_name,
             json.dumps(schema, ensure_ascii=False), int(len(data)), now, now),
        )
        _audit(conn, project_id, dataset_id, "CREATE_DATASET", f"{function_name}; {len(data)} baris")
    return dataset_id


def get_dataset_meta(dataset_id: str) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    if not row:
        raise KeyError(f"Dataset {dataset_id} tidak ditemukan")
    result = dict(row)
    result["column_schema"] = json.loads(result["column_schema"])
    return result


def list_project_datasets(project_id: str) -> pd.DataFrame:
    with _connect() as conn:
        return pd.read_sql_query(
            "SELECT id, function_name, name, sheet_name, row_count, updated_at FROM datasets WHERE project_id=? ORDER BY function_name",
            conn, params=(project_id,),
        )


def get_dataframe(dataset_id: str, limit: int | None = None, offset: int = 0) -> pd.DataFrame:
    """Read a dataset, optionally paginated.

    Large project tables must not be loaded in full on every Streamlit rerun.
    """
    meta = get_dataset_meta(dataset_id)
    sql = f"SELECT * FROM {quote_ident(meta['table_name'])} ORDER BY _row_id"
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([max(1, int(limit)), max(0, int(offset))])
    with _connect() as conn:
        df = pd.read_sql_query(sql, conn, params=params)
    rename = {x["sql"]: x["display"] for x in meta["column_schema"]}
    return df.rename(columns=rename)


def dataset_row_count(dataset_id: str) -> int:
    meta = get_dataset_meta(dataset_id)
    with _connect() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {quote_ident(meta['table_name'])}").fetchone()[0])


def _mapping(meta: dict[str, Any]) -> dict[str, str]:
    return {x["display"]: x["sql"] for x in meta["column_schema"]}


def ensure_dataset_columns(dataset_id: str, df: pd.DataFrame) -> dict[str, str]:
    meta = get_dataset_meta(dataset_id)
    mapping = _mapping(meta)
    schema = meta["column_schema"]
    incoming = df.copy()
    incoming.columns = make_unique_columns(list(incoming.columns))
    used_sql = {x["sql"] for x in schema}
    changed = False
    with _connect() as conn:
        for idx, col in enumerate(incoming.columns, start=1):
            if col in mapping:
                continue
            sql_name = sanitize_identifier(col, f"col_{idx}")
            base = sql_name
            n = 2
            while sql_name in used_sql:
                sql_name = f"{base}_{n}"
                n += 1
            used_sql.add(sql_name)
            col_type = _infer_sql_type(incoming[col])
            conn.execute(f"ALTER TABLE {quote_ident(meta['table_name'])} ADD COLUMN {quote_ident(sql_name)} {col_type}")
            schema.append({"display": col, "sql": sql_name, "type": col_type})
            mapping[col] = sql_name
            changed = True
        if changed:
            conn.execute("UPDATE datasets SET column_schema=?, updated_at=? WHERE id=?",
                         (json.dumps(schema, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), dataset_id))
            _audit(conn, meta["project_id"], dataset_id, "SCHEMA_EVOLVE", "Kolom baru: " + ", ".join([c for c in incoming.columns if c not in _mapping(meta)]))
    return mapping


def save_function_source(project_id: str, function_name: str, dataset_id: str, sheet_name: str, header_row: int, key_columns: list[str], last_filename: str = "") -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        existing = conn.execute("SELECT id FROM function_sources WHERE project_id=? AND function_name=?", (project_id, function_name)).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE function_sources SET dataset_id=?, sheet_name=?, header_row=?, key_columns=?, last_filename=?, updated_at=?
                WHERE project_id=? AND function_name=?
                """,
                (dataset_id, sheet_name, int(header_row), json.dumps(key_columns, ensure_ascii=False), last_filename, now, project_id, function_name),
            )
        else:
            conn.execute(
                """
                INSERT INTO function_sources(id, project_id, function_name, dataset_id, sheet_name, header_row, key_columns, last_filename, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (uuid.uuid4().hex[:12], project_id, function_name, dataset_id, sheet_name, int(header_row), json.dumps(key_columns, ensure_ascii=False), last_filename, now, now),
            )
        _audit(conn, project_id, dataset_id, "SAVE_FUNCTION_CONFIG", f"{function_name}; key={key_columns}")


def get_function_source(project_id: str, function_name: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM function_sources WHERE project_id=? AND function_name=?", (project_id, function_name)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["key_columns"] = json.loads(result["key_columns"])
    return result


def list_function_sources(project_id: str) -> pd.DataFrame:
    with _connect() as conn:
        df = pd.read_sql_query(
            """
            SELECT fs.function_name, fs.dataset_id, fs.sheet_name, fs.header_row, fs.key_columns, fs.last_filename,
                   d.row_count, d.updated_at
            FROM function_sources fs JOIN datasets d ON d.id=fs.dataset_id
            WHERE fs.project_id=? ORDER BY fs.function_name
            """,
            conn, params=(project_id,),
        )
    if not df.empty:
        df["key_columns"] = df["key_columns"].map(lambda x: ", ".join(json.loads(x)))
    return df


def _norm_key_value(v: Any) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def upsert_dataframe(
    dataset_id: str,
    df: pd.DataFrame,
    key_columns: list[str],
    *,
    allow_insert: bool = True,
    allow_new_columns: bool = True,
) -> tuple[dict[str, int], pd.DataFrame]:
    """Merge an incoming dataframe into a dataset.

    Parameters
    ----------
    allow_insert:
        If False, keys not already present are rejected instead of creating new
        rows. This is useful for a *column update* workbook whose purpose is to
        add/update fields on records that already exist.
    allow_new_columns:
        If False, columns that do not yet exist in the dataset are ignored.
        This is useful for a *row update* workbook where the table schema should
        remain stable.
    """
    if not key_columns:
        raise ValueError("Minimal satu key harus dipilih")
    incoming = df.copy()
    incoming.columns = make_unique_columns(list(incoming.columns))
    missing_keys = [c for c in key_columns if c not in incoming.columns]
    if missing_keys:
        raise ValueError("Key tidak ditemukan pada file: " + ", ".join(missing_keys))

    if allow_new_columns:
        ensure_dataset_columns(dataset_id, incoming)
    meta = get_dataset_meta(dataset_id)
    mapping = _mapping(meta)
    table = meta["table_name"]

    missing_dataset_keys = [c for c in key_columns if c not in mapping]
    if missing_dataset_keys:
        raise ValueError(
            "Key belum tersedia pada dataset lama: " + ", ".join(missing_dataset_keys) +
            ". Gunakan key yang sudah tersimpan atau buat ulang dataset dengan key tersebut."
        )

    ignored_columns = [c for c in incoming.columns if c not in mapping]

    # Hanya baca _row_id + kolom key dari SQLite. Dataset besar tidak perlu
    # dimuat penuh ke RAM hanya untuk proses pencocokan.
    key_sql = [mapping[c] for c in key_columns]
    with _connect() as conn:
        existing_rows = conn.execute(
            f"SELECT _row_id, {', '.join(quote_ident(c) for c in key_sql)} FROM {quote_ident(table)}"
        ).fetchall()
    existing_index: dict[tuple[str, ...], int] = {}
    for r in existing_rows:
        key = tuple(_norm_key_value(r[c]) for c in key_sql)
        if all(key):
            existing_index[key] = int(r["_row_id"])

    rejected_rows: list[dict[str, Any]] = []
    valid_indices: list[int] = []
    seen_incoming: dict[tuple[str, ...], int] = {}
    key_positions = [incoming.columns.get_loc(c) for c in key_columns]
    tuple_rows = list(incoming.itertuples(index=False, name=None))
    for idx, values in enumerate(tuple_rows):
        key = tuple(_norm_key_value(values[pos]) for pos in key_positions)
        if not all(key):
            rec = incoming.iloc[idx].to_dict()
            rec["_reject_reason"] = "Key kosong"
            rejected_rows.append(rec)
            continue
        if key in seen_incoming:
            rec = incoming.iloc[idx].to_dict()
            rec["_reject_reason"] = f"Key duplikat dalam file (baris sebelumnya {seen_incoming[key] + 2})"
            rejected_rows.append(rec)
            continue
        seen_incoming[key] = idx
        valid_indices.append(idx)

    incoming_cols = [c for c in incoming.columns if c in mapping]
    col_positions = [incoming.columns.get_loc(c) for c in incoming_cols]
    sql_cols = [mapping[c] for c in incoming_cols]
    insert_sql = (
        f"INSERT INTO {quote_ident(table)} ({', '.join(quote_ident(c) for c in sql_cols)}) "
        f"VALUES ({','.join('?' for _ in sql_cols)})"
    )
    assignments = ", ".join(f"{quote_ident(mapping[c])}=?" for c in incoming_cols)
    update_sql = f"UPDATE {quote_ident(table)} SET {assignments} WHERE _row_id=?"

    insert_rows: list[tuple[Any, ...]] = []
    update_rows: list[tuple[Any, ...]] = []
    for idx in valid_indices:
        vals_tuple = tuple_rows[idx]
        key = tuple(_norm_key_value(vals_tuple[pos]) for pos in key_positions)
        values = tuple(_python_value(vals_tuple[pos]) for pos in col_positions)
        row_id = existing_index.get(key)
        if row_id is None:
            if allow_insert:
                insert_rows.append(values)
            else:
                rec = incoming.iloc[idx].to_dict()
                rec["_reject_reason"] = "Key belum ada pada dataset; mode update kolom tidak menambah baris baru"
                rejected_rows.append(rec)
        else:
            update_rows.append(values + (row_id,))

    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        if insert_rows:
            conn.executemany(insert_sql, insert_rows)
        if update_rows:
            conn.executemany(update_sql, update_rows)
        count = conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0]
        conn.execute("UPDATE datasets SET row_count=?, updated_at=? WHERE id=?", (int(count), now, dataset_id))
        _audit(
            conn, meta["project_id"], dataset_id, "UPSERT",
            f"insert={len(insert_rows)}; update={len(update_rows)}; reject={len(rejected_rows)}; "
            f"new_columns={'yes' if allow_new_columns else 'no'}; ignored_columns={len(ignored_columns)}"
        )

    return {
        "inserted": len(insert_rows),
        "updated": len(update_rows),
        "rejected": len(rejected_rows),
        "total": len(incoming),
        "ignored_columns": len(ignored_columns),
    }, pd.DataFrame(rejected_rows)

def replace_dataset_contents(dataset_id: str, df: pd.DataFrame) -> dict[str, int]:
    """Replace the current dataset with one complete reporting snapshot.

    Used by rolling planning sheets such as the HDK 2-week lookahead. Historical
    versions are retained separately through source_versions / data_snapshots.
    """
    incoming = df.copy()
    incoming.columns = make_unique_columns(list(incoming.columns))
    ensure_dataset_columns(dataset_id, incoming)
    meta = get_dataset_meta(dataset_id)
    mapping = _mapping(meta)
    table = meta["table_name"]
    cols = [c for c in incoming.columns if c in mapping]
    sql_cols = [mapping[c] for c in cols]
    rows = [tuple(_python_value(v) for v in row) for row in incoming[cols].itertuples(index=False, name=None)] if cols else []
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        old_count = int(conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0])
        conn.execute(f"DELETE FROM {quote_ident(table)}")
        if rows:
            placeholders = ",".join("?" for _ in sql_cols)
            sql = f"INSERT INTO {quote_ident(table)} ({', '.join(quote_ident(c) for c in sql_cols)}) VALUES ({placeholders})"
            conn.executemany(sql, rows)
        conn.execute("UPDATE datasets SET row_count=?, updated_at=? WHERE id=?", (len(rows), now, dataset_id))
        _audit(conn, meta["project_id"], dataset_id, "REPLACE_SNAPSHOT", f"old={old_count}; new={len(rows)}")
    return {"total": len(incoming), "inserted": len(rows), "updated": 0, "rejected": 0, "replaced": old_count}


def apply_edits(dataset_id: str, original_view: pd.DataFrame, edited_view: pd.DataFrame) -> dict[str, int]:
    meta = get_dataset_meta(dataset_id)
    mapping = _mapping(meta)
    table = meta["table_name"]
    display_cols = list(mapping.keys())
    original_ids = set(pd.to_numeric(original_view.get("_row_id", pd.Series(dtype=float)), errors="coerce").dropna().astype(int))
    edited_ids = set(pd.to_numeric(edited_view.get("_row_id", pd.Series(dtype=float)), errors="coerce").dropna().astype(int))
    deleted_ids = sorted(original_ids - edited_ids)
    inserted = 0; updated = 0
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        for rid in deleted_ids:
            conn.execute(f"DELETE FROM {quote_ident(table)} WHERE _row_id=?", (int(rid),))
        for _, row in edited_view.iterrows():
            raw = row.get("_row_id")
            rid = None
            try:
                if not pd.isna(raw): rid = int(raw)
            except Exception:
                rid = None
            values = [_python_value(row.get(c)) for c in display_cols]
            if rid is None:
                sql_cols = [mapping[c] for c in display_cols]
                placeholders = ",".join("?" for _ in sql_cols)
                conn.execute(f"INSERT INTO {quote_ident(table)} ({', '.join(quote_ident(c) for c in sql_cols)}) VALUES ({placeholders})", values)
                inserted += 1
            else:
                assignments = ", ".join(f"{quote_ident(mapping[c])}=?" for c in display_cols)
                conn.execute(f"UPDATE {quote_ident(table)} SET {assignments} WHERE _row_id=?", values + [rid])
                updated += 1
        count = conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0]
        conn.execute("UPDATE datasets SET row_count=?, updated_at=? WHERE id=?", (int(count), now, dataset_id))
        _audit(conn, meta["project_id"], dataset_id, "CRUD", f"insert={inserted}; update={updated}; delete={len(deleted_ids)}")
    return {"inserted": inserted, "updated": updated, "deleted": len(deleted_ids)}


def add_import_batch(project_id: str, function_name: str, dataset_id: str | None, filename: str, sheet_name: str, stats: dict[str, int], status: str = "SUCCESS", error: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO import_batches(project_id, function_name, dataset_id, filename, sheet_name, row_total, inserted, updated, rejected, status, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, function_name, dataset_id, filename, sheet_name, int(stats.get("total", 0)), int(stats.get("inserted", 0)), int(stats.get("updated", 0)), int(stats.get("rejected", 0)), status, error, datetime.now().isoformat(timespec="seconds")),
        )
        _audit(conn, project_id, dataset_id, "IMPORT_BATCH", f"{filename}; {status}")


def get_import_history(project_id: str, limit: int = 100) -> pd.DataFrame:
    with _connect() as conn:
        return pd.read_sql_query(
            """
            SELECT id, function_name, filename, sheet_name, row_total, inserted, updated, rejected, status, error, created_at
            FROM import_batches WHERE project_id=? ORDER BY id DESC LIMIT ?
            """,
            conn, params=(project_id, int(limit)),
        )


def add_photo(
    project_id: str, file_name: str, file_path: str, captured_date: str = "",
    zone: str = "", category: str = "", wbs_activity: str = "", description: str = "",
    media_type: str = "", batch_id: str = "", batch_label: str = "", display_order: int = 0,
) -> str:
    photo_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO photos(
                id, project_id, captured_date, zone, category, wbs_activity, description,
                file_name, file_path, created_at, media_type, batch_id, batch_label, display_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (photo_id, project_id, captured_date, zone, category, wbs_activity, description,
             file_name, file_path, now, media_type, batch_id, batch_label, int(display_order)),
        )
        _audit(conn, project_id, None, "UPLOAD_PHOTO", f"{media_type or category}; {batch_label}; {file_name}")
    return photo_id


def list_photos(
    project_id: str, category: str = "", zone: str = "", media_type: str = "",
    batch_id: str = "", limit: int = 200, offset: int = 0,
) -> pd.DataFrame:
    sql = "SELECT * FROM photos WHERE project_id=?"
    params: list[Any] = [project_id]
    if category:
        sql += " AND category=?"; params.append(category)
    if zone:
        sql += " AND zone=?"; params.append(zone)
    if media_type:
        sql += " AND media_type=?"; params.append(media_type)
    if batch_id:
        sql += " AND batch_id=?"; params.append(batch_id)
    sql += " ORDER BY COALESCE(captured_date, created_at) DESC, created_at DESC, display_order ASC LIMIT ? OFFSET ?"
    params.extend([int(limit), max(0, int(offset))])
    with _connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def photo_count(project_id: str, category: str = "", zone: str = "", media_type: str = "") -> int:
    sql = "SELECT COUNT(*) FROM photos WHERE project_id=?"
    params: list[Any] = [project_id]
    if category:
        sql += " AND category=?"; params.append(category)
    if zone:
        sql += " AND zone=?"; params.append(zone)
    if media_type:
        sql += " AND media_type=?"; params.append(media_type)
    with _connect() as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def latest_photo_batch(project_id: str, media_type: str) -> pd.DataFrame:
    """Return only the latest uploaded batch for a media type, never mix old/new updates."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT batch_id FROM photos
            WHERE project_id=? AND media_type=? AND COALESCE(batch_id,'')<>''
            ORDER BY created_at DESC LIMIT 1
            """, (project_id, media_type),
        ).fetchone()
        if not row:
            return pd.DataFrame()
        return pd.read_sql_query(
            """
            SELECT * FROM photos WHERE project_id=? AND media_type=? AND batch_id=?
            ORDER BY display_order ASC, created_at ASC
            """, conn, params=(project_id, media_type, row["batch_id"]),
        )


def list_photo_batches(project_id: str, media_type: str = "") -> pd.DataFrame:
    sql = """
        SELECT batch_id, media_type, batch_label, captured_date, zone,
               COUNT(*) AS image_count, MIN(created_at) AS created_at
        FROM photos
        WHERE project_id=? AND COALESCE(batch_id,'')<>''
    """
    params: list[Any] = [project_id]
    if media_type:
        sql += " AND media_type=?"; params.append(media_type)
    sql += " GROUP BY batch_id, media_type, batch_label, captured_date, zone ORDER BY MIN(created_at) DESC"
    with _connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def delete_photo(photo_id: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT project_id, file_path, file_name FROM photos WHERE id=?", (photo_id,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM photos WHERE id=?", (photo_id,))
        _audit(conn, row["project_id"], None, "DELETE_PHOTO", row["file_name"])
        return row["file_path"]


def get_audit_log(project_id: str | None = None, limit: int = 300) -> pd.DataFrame:
    with _connect() as conn:
        if project_id:
            return pd.read_sql_query(
                "SELECT id, project_id, dataset_id, action, detail, created_at FROM audit_log WHERE project_id=? ORDER BY id DESC LIMIT ?",
                conn, params=(project_id, int(limit)),
            )
        return pd.read_sql_query(
            "SELECT id, project_id, dataset_id, action, detail, created_at FROM audit_log ORDER BY id DESC LIMIT ?",
            conn, params=(int(limit),),
        )


def database_size_bytes() -> int:
    return DB_PATH.stat().st_size if DB_PATH.exists() else 0


def vacuum_database() -> None:
    with _connect() as conn:
        conn.execute("VACUUM")


# ---------------------------------------------------------------------------
# Multi-file / multi-sheet source registry + data snapshots (v2.3)
# ---------------------------------------------------------------------------
def upsert_source_file(project_id: str, filename: str, function_name: str = "", description: str = "", size: int = 0) -> str:
    init_db()
    now = datetime.now().isoformat(timespec="seconds")
    filename = str(filename).strip()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM source_files WHERE project_id=? AND filename=?",
            (project_id, filename),
        ).fetchone()
        if row:
            source_file_id = row["id"]
            conn.execute(
                """UPDATE source_files SET function_name=?, description=?, last_size=?,
                   last_uploaded_at=?, updated_at=? WHERE id=?""",
                (function_name.strip(), description.strip(), int(size or 0), now, now, source_file_id),
            )
        else:
            source_file_id = uuid.uuid4().hex[:12]
            conn.execute(
                """INSERT INTO source_files(id, project_id, filename, function_name, description,
                   last_size, last_uploaded_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (source_file_id, project_id, filename, function_name.strip(), description.strip(), int(size or 0), now, now, now),
            )
        _audit(conn, project_id, None, "SAVE_SOURCE_FILE", f"{filename}; {function_name}")
    return source_file_id


def get_source_file(source_file_id: str) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM source_files WHERE id=?", (source_file_id,)).fetchone()
    if not row:
        raise KeyError(f"Source file {source_file_id} tidak ditemukan")
    return dict(row)


def get_source_file_by_name(project_id: str, filename: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM source_files WHERE project_id=? AND filename=?", (project_id, filename)
        ).fetchone()
    return dict(row) if row else None


def list_source_files(project_id: str) -> pd.DataFrame:
    with _connect() as conn:
        return pd.read_sql_query(
            """
            SELECT sf.*,
                   (SELECT COUNT(*) FROM source_sheets ss WHERE ss.source_file_id=sf.id AND ss.selected=1) AS selected_sheet_count,
                   (SELECT COUNT(*) FROM source_sheets ss WHERE ss.source_file_id=sf.id) AS configured_sheet_count
            FROM source_files sf
            WHERE sf.project_id=? ORDER BY sf.updated_at DESC, sf.filename
            """,
            conn, params=(project_id,),
        )


def sync_source_sheet_selection(project_id: str, source_file_id: str, sheet_names: list[str], selected_names: list[str], default_function: str = "") -> None:
    """Ensure workbook sheets exist in registry and persist which ones appear in dashboard."""
    now = datetime.now().isoformat(timespec="seconds")
    selected_set = set(selected_names)
    with _connect() as conn:
        # Reset pilihan lama agar sheet yang tidak lagi ada/tidak lagi dicentang
        # tidak tetap muncul di dashboard. Konfigurasinya tetap disimpan.
        conn.execute("UPDATE source_sheets SET selected=0, updated_at=? WHERE source_file_id=?", (now, source_file_id))
        for name in sheet_names:
            row = conn.execute(
                "SELECT id FROM source_sheets WHERE source_file_id=? AND sheet_name=?",
                (source_file_id, name),
            ).fetchone()
            selected = 1 if name in selected_set else 0
            if row:
                conn.execute(
                    "UPDATE source_sheets SET selected=?, updated_at=? WHERE id=?",
                    (selected, now, row["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO source_sheets(id, project_id, source_file_id, sheet_name, display_name,
                       description, function_name, selected, dataset_id, header_row, key_columns,
                       update_mode, formula_mode, include_excel_row, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, '', ?, ?, NULL, 1, '[]', 'auto', 'values', 0, ?, ?)""",
                    (uuid.uuid4().hex[:12], project_id, source_file_id, name, name, default_function, selected, now, now),
                )
        _audit(conn, project_id, None, "SELECT_SOURCE_SHEETS", f"file={source_file_id}; selected={sorted(selected_set)}")


def get_source_sheet(source_file_id: str, sheet_name: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM source_sheets WHERE source_file_id=? AND sheet_name=?",
            (source_file_id, sheet_name),
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["key_columns"] = json.loads(out.get("key_columns") or "[]")
    except Exception:
        out["key_columns"] = []
    return out


def save_source_sheet_config(
    project_id: str,
    source_file_id: str,
    sheet_name: str,
    *,
    dataset_id: str | None,
    function_name: str,
    display_name: str = "",
    description: str = "",
    selected: bool = True,
    header_row: int = 1,
    key_columns: list[str] | None = None,
    update_mode: str = "auto",
    formula_mode: str = "values",
    include_excel_row: bool = False,
) -> str:
    now = datetime.now().isoformat(timespec="seconds")
    key_json = json.dumps(key_columns or [], ensure_ascii=False)
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM source_sheets WHERE source_file_id=? AND sheet_name=?",
            (source_file_id, sheet_name),
        ).fetchone()
        if row:
            sid = row["id"]
            conn.execute(
                """UPDATE source_sheets SET display_name=?, description=?, function_name=?, selected=?, dataset_id=?,
                   header_row=?, key_columns=?, update_mode=?, formula_mode=?, include_excel_row=?, updated_at=?
                   WHERE id=?""",
                (display_name.strip() or sheet_name, description.strip(), function_name.strip(), int(bool(selected)), dataset_id,
                 int(header_row), key_json, update_mode, formula_mode, int(bool(include_excel_row)), now, sid),
            )
        else:
            sid = uuid.uuid4().hex[:12]
            conn.execute(
                """INSERT INTO source_sheets(id, project_id, source_file_id, sheet_name, display_name, description,
                   function_name, selected, dataset_id, header_row, key_columns, update_mode, formula_mode,
                   include_excel_row, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, project_id, source_file_id, sheet_name, display_name.strip() or sheet_name, description.strip(),
                 function_name.strip(), int(bool(selected)), dataset_id, int(header_row), key_json, update_mode,
                 formula_mode, int(bool(include_excel_row)), now, now),
            )
        _audit(conn, project_id, dataset_id, "SAVE_SOURCE_SHEET", f"{sheet_name}; dataset={dataset_id}")
    return sid


def list_source_sheets(source_file_id: str, selected_only: bool = False) -> pd.DataFrame:
    sql = """
        SELECT ss.*, d.row_count, d.updated_at AS dataset_updated_at
        FROM source_sheets ss
        LEFT JOIN datasets d ON d.id=ss.dataset_id
        WHERE ss.source_file_id=?
    """
    params: list[Any] = [source_file_id]
    if selected_only:
        sql += " AND ss.selected=1"
    sql += " ORDER BY ss.sheet_name"
    with _connect() as conn:
        df = pd.read_sql_query(sql, conn, params=params)
    if not df.empty:
        df["key_columns"] = df["key_columns"].map(lambda x: ", ".join(json.loads(x or "[]")))
    return df


def list_dashboard_sheets(project_id: str) -> pd.DataFrame:
    with _connect() as conn:
        return pd.read_sql_query(
            """
            SELECT sf.id AS source_file_id, sf.filename, sf.function_name AS file_function, sf.description AS file_description,
                   ss.id AS source_sheet_id, ss.sheet_name, ss.display_name, ss.description AS sheet_description,
                   ss.function_name, ss.dataset_id, ss.header_row, ss.update_mode,
                   d.row_count, d.updated_at
            FROM source_files sf
            JOIN source_sheets ss ON ss.source_file_id=sf.id AND ss.selected=1
            LEFT JOIN datasets d ON d.id=ss.dataset_id
            WHERE sf.project_id=?
            ORDER BY sf.filename, ss.sheet_name
            """,
            conn, params=(project_id,),
        )



def add_source_file_version(project_id: str, source_file_id: str, label: str, file_name: str, file_path: str, file_size: int) -> str:
    """Register an immutable archived workbook version and point source_files to it."""
    version_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat(timespec="seconds")
    clean_label = (label or "").strip() or f"Upload {now.replace('T',' ')[:16]}"
    with _connect() as conn:
        conn.execute(
            """INSERT INTO source_versions(id, project_id, source_file_id, label, file_name, file_path, file_size, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (version_id, project_id, source_file_id, clean_label, file_name, file_path, int(file_size or 0), now),
        )
        conn.execute(
            """UPDATE source_files SET current_file_path=?, current_version_id=?, last_uploaded_at=?, last_size=?, updated_at=?
               WHERE id=?""",
            (file_path, version_id, now, int(file_size or 0), now, source_file_id),
        )
        _audit(conn, project_id, None, "ARCHIVE_SOURCE_FILE", f"{file_name}; version={clean_label}")
    return version_id


def list_source_file_versions(source_file_id: str) -> pd.DataFrame:
    with _connect() as conn:
        return pd.read_sql_query(
            "SELECT * FROM source_versions WHERE source_file_id=? ORDER BY created_at DESC",
            conn, params=(source_file_id,),
        )


def get_source_file_version(version_id: str) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM source_versions WHERE id=?", (version_id,)).fetchone()
    if not row:
        raise KeyError(f"Source version {version_id} tidak ditemukan")
    return dict(row)

def overwrite_current_source_version(source_file_id: str, file_name: str, file_path: str, file_size: int, label: str = "") -> str:
    """Overwrite the *active* archived workbook without creating a new source version.

    The source-file identity, sheet mappings and dataset links are preserved. This
    operation is intended for a corrected/replacement workbook. Historical older
    versions remain intact; only the currently-active version record is replaced.
    """
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        src = conn.execute("SELECT * FROM source_files WHERE id=?", (source_file_id,)).fetchone()
        if not src:
            raise KeyError(f"Source file {source_file_id} tidak ditemukan")
        current_version_id = src["current_version_id"]
        clean_label = (label or "").strip()
        if current_version_id:
            if clean_label:
                conn.execute(
                    """UPDATE source_versions SET label=?, file_name=?, file_path=?, file_size=?, created_at=? WHERE id=?""",
                    (clean_label, file_name, file_path, int(file_size or 0), now, current_version_id),
                )
            else:
                conn.execute(
                    """UPDATE source_versions SET file_name=?, file_path=?, file_size=?, created_at=? WHERE id=?""",
                    (file_name, file_path, int(file_size or 0), now, current_version_id),
                )
            version_id = str(current_version_id)
        else:
            version_id = uuid.uuid4().hex[:12]
            clean_label = clean_label or f"Timpa {now.replace('T',' ')[:16]}"
            conn.execute(
                """INSERT INTO source_versions(id, project_id, source_file_id, label, file_name, file_path, file_size, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (version_id, src["project_id"], source_file_id, clean_label, file_name, file_path, int(file_size or 0), now),
            )
        conn.execute(
            """UPDATE source_files SET current_file_path=?, current_version_id=?, last_uploaded_at=?, last_size=?, updated_at=?
               WHERE id=?""",
            (file_path, version_id, now, int(file_size or 0), now, source_file_id),
        )
        _audit(conn, src["project_id"], None, "OVERWRITE_SOURCE_FILE", f"{src['filename']} <- {file_name}; active_version={version_id}")
    return version_id


def delete_source_file_completely(source_file_id: str) -> dict[str, Any]:
    """Permanently purge a source workbook and everything owned by it.

    Removes source versions, selected-sheet mappings, snapshots and their SQLite
    tables, source-owned datasets and their SQLite tables, matching import history,
    and archived workbook files. Project master data, photos and unrelated sources
    are not touched.
    """
    physical_paths: set[str] = set()
    dataset_tables: list[tuple[str, str]] = []
    snapshot_tables: list[str] = []
    with _connect() as conn:
        src = conn.execute("SELECT * FROM source_files WHERE id=?", (source_file_id,)).fetchone()
        if not src:
            raise KeyError(f"Source file {source_file_id} tidak ditemukan")
        project_id = str(src["project_id"])
        filename = str(src["filename"])
        if src["current_file_path"]:
            physical_paths.add(str(src["current_file_path"]))
        for r in conn.execute("SELECT file_path FROM source_versions WHERE source_file_id=?", (source_file_id,)).fetchall():
            if r["file_path"]:
                physical_paths.add(str(r["file_path"]))

        dataset_ids = [str(r["dataset_id"]) for r in conn.execute(
            "SELECT DISTINCT dataset_id FROM source_sheets WHERE source_file_id=? AND dataset_id IS NOT NULL",
            (source_file_id,),
        ).fetchall()]

        # Drop every snapshot table belonging to this source or one of its datasets.
        params: list[Any] = [source_file_id]
        snap_sql = "SELECT id, table_name FROM data_snapshots WHERE source_file_id=?"
        if dataset_ids:
            snap_sql += " OR dataset_id IN (" + ",".join("?" for _ in dataset_ids) + ")"
            params.extend(dataset_ids)
        snapshots = conn.execute(snap_sql, params).fetchall()
        for r in snapshots:
            snapshot_tables.append(str(r["table_name"]))
            conn.execute(f"DROP TABLE IF EXISTS {quote_ident(str(r['table_name']))}")
        if snapshots:
            ids = [str(r["id"]) for r in snapshots]
            conn.execute("DELETE FROM data_snapshots WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids)

        # Delete datasets only when no other source file references them.
        deleted_dataset_ids: list[str] = []
        for did in dataset_ids:
            other_refs = int(conn.execute(
                "SELECT COUNT(*) FROM source_sheets WHERE dataset_id=? AND source_file_id<>?",
                (did, source_file_id),
            ).fetchone()[0])
            if other_refs:
                continue
            meta = conn.execute("SELECT table_name FROM datasets WHERE id=?", (did,)).fetchone()
            if meta:
                table_name = str(meta["table_name"])
                dataset_tables.append((did, table_name))
                conn.execute(f"DROP TABLE IF EXISTS {quote_ident(table_name)}")
                conn.execute("DELETE FROM datasets WHERE id=?", (did,))
                deleted_dataset_ids.append(did)

        # Remove source-scoped import history. Dataset-linked rows are also removed.
        if deleted_dataset_ids:
            conn.execute(
                "DELETE FROM import_batches WHERE project_id=? AND (filename=? OR dataset_id IN (" + ",".join("?" for _ in deleted_dataset_ids) + "))",
                [project_id, filename, *deleted_dataset_ids],
            )
        else:
            conn.execute("DELETE FROM import_batches WHERE project_id=? AND filename=?", (project_id, filename))

        # Cascades source_sheets and source_versions.
        conn.execute("DELETE FROM source_files WHERE id=?", (source_file_id,))
        _audit(conn, project_id, None, "DELETE_SOURCE_FILE_ALL", f"{filename}; datasets={len(dataset_tables)}; snapshots={len(snapshot_tables)}")

    removed_files = 0
    for raw in physical_paths:
        try:
            fp = Path(raw)
            if fp.exists() and fp.is_file():
                fp.unlink()
                removed_files += 1
        except OSError:
            pass
    # Clean now-empty archive directories up to the source-file folder only.
    try:
        if physical_paths:
            parent = Path(next(iter(physical_paths))).parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
    except OSError:
        pass
    return {
        "source_file_id": source_file_id,
        "filename": filename,
        "removed_files": removed_files,
        "removed_datasets": len(dataset_tables),
        "removed_snapshots": len(snapshot_tables),
    }


def create_snapshot(dataset_id: str, source_file_id: str | None = None, source_sheet_id: str | None = None,
                    label: str = "", file_name: str = "", source_version_id: str | None = None) -> str:
    meta = get_dataset_meta(dataset_id)
    snap_id = uuid.uuid4().hex[:12]
    table_name = f"snap_{snap_id}"
    now = datetime.now().isoformat(timespec="seconds")
    label = label.strip() or f"Snapshot {now.replace('T', ' ')[:16]}"
    with _connect() as conn:
        conn.execute(
            f"CREATE TABLE {quote_ident(table_name)} AS SELECT * FROM {quote_ident(meta['table_name'])}"
        )
        count = int(conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table_name)}").fetchone()[0])
        conn.execute(
            """INSERT INTO data_snapshots(id, project_id, source_file_id, source_sheet_id, source_version_id, dataset_id, label,
               file_name, table_name, column_schema, row_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (snap_id, meta["project_id"], source_file_id, source_sheet_id, source_version_id, dataset_id, label, file_name,
             table_name, json.dumps(meta["column_schema"], ensure_ascii=False), count, now),
        )
        _audit(conn, meta["project_id"], dataset_id, "CREATE_SNAPSHOT", f"{label}; rows={count}")
    return snap_id


def list_snapshots(source_sheet_id: str | None = None, dataset_id: str | None = None, project_id: str | None = None) -> pd.DataFrame:
    where = []
    params: list[Any] = []
    if source_sheet_id:
        where.append("source_sheet_id=?"); params.append(source_sheet_id)
    if dataset_id:
        where.append("dataset_id=?"); params.append(dataset_id)
    if project_id:
        where.append("project_id=?"); params.append(project_id)
    sql = "SELECT * FROM data_snapshots"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    with _connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)


def get_snapshot_meta(snapshot_id: str) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM data_snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if not row:
        raise KeyError(f"Snapshot {snapshot_id} tidak ditemukan")
    out = dict(row)
    out["column_schema"] = json.loads(out["column_schema"])
    return out


def get_snapshot_dataframe(snapshot_id: str, limit: int | None = None, offset: int = 0) -> pd.DataFrame:
    meta = get_snapshot_meta(snapshot_id)
    sql = f"SELECT * FROM {quote_ident(meta['table_name'])} ORDER BY _row_id"
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"; params.extend([max(1, int(limit)), max(0, int(offset))])
    with _connect() as conn:
        df = pd.read_sql_query(sql, conn, params=params)
    rename = {x["sql"]: x["display"] for x in meta["column_schema"]}
    return df.rename(columns=rename)


def delete_snapshot(snapshot_id: str) -> None:
    meta = get_snapshot_meta(snapshot_id)
    with _connect() as conn:
        conn.execute(f"DROP TABLE IF EXISTS {quote_ident(meta['table_name'])}")
        conn.execute("DELETE FROM data_snapshots WHERE id=?", (snapshot_id,))
        _audit(conn, meta["project_id"], meta["dataset_id"], "DELETE_SNAPSHOT", meta["label"])

# ------------------------------ As-of / historical dashboard helpers ------------------------------
def list_project_update_dates(project_id: str) -> list[str]:
    """Return descending YYYY-MM-DD dates where project data/media changed.

    The dashboard uses these as selectable reporting cut-offs.  A selected date
    means: use the latest available source/media *on or before* that date.
    """
    dates: set[str] = set()
    with _connect() as conn:
        for row in conn.execute(
            "SELECT created_at FROM source_versions WHERE project_id=?", (project_id,)
        ).fetchall():
            value = str(row[0] or "")[:10]
            if value:
                dates.add(value)
        for row in conn.execute(
            "SELECT COALESCE(NULLIF(captured_date,''), created_at) FROM photos WHERE project_id=?",
            (project_id,),
        ).fetchall():
            value = str(row[0] or "")[:10]
            if value:
                dates.add(value)
    return sorted(dates, reverse=True)


def get_source_file_version_as_of(source_file_id: str, as_of_date: str | None = None) -> dict[str, Any] | None:
    """Get latest archived source version on/before YYYY-MM-DD; current if None."""
    with _connect() as conn:
        if not as_of_date:
            src = conn.execute("SELECT current_version_id FROM source_files WHERE id=?", (source_file_id,)).fetchone()
            if not src or not src[0]:
                return None
            row = conn.execute("SELECT * FROM source_versions WHERE id=?", (src[0],)).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM source_versions
                WHERE source_file_id=? AND substr(created_at,1,10) <= ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (source_file_id, as_of_date),
            ).fetchone()
    return dict(row) if row else None


def latest_photo_batch_as_of(project_id: str, media_type: str, as_of_date: str | None = None) -> pd.DataFrame:
    """Latest batch on/before reporting date, preferring captured_date over upload date."""
    with _connect() as conn:
        where = "project_id=? AND media_type=? AND COALESCE(batch_id,'')<>''"
        params: list[Any] = [project_id, media_type]
        if as_of_date:
            where += " AND substr(COALESCE(NULLIF(captured_date,''), created_at),1,10) <= ?"
            params.append(as_of_date)
        row = conn.execute(
            f"""
            SELECT batch_id FROM photos
            WHERE {where}
            ORDER BY COALESCE(NULLIF(captured_date,''), created_at) DESC, created_at DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        if not row:
            return pd.DataFrame()
        return pd.read_sql_query(
            """
            SELECT * FROM photos WHERE project_id=? AND media_type=? AND batch_id=?
            ORDER BY display_order ASC, created_at ASC
            """,
            conn, params=(project_id, media_type, row["batch_id"]),
        )
