from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:
    from . import storage
except ImportError:  # pragma: no cover - CLI fallback
    import storage  # type: ignore

PUBLISH_SCHEMA_VERSION = 1
APP_VERSION = "2.9.19"

CORE_TABLES = {
    "projects",
    "datasets",
    "function_sources",
    "import_batches",
    "photos",
    "source_files",
    "source_versions",
    "source_sheets",
    "data_snapshots",
    "audit_log",
    "users",
    "user_project_access",
}

PROJECT_SCOPED_TABLES = [
    "function_sources",
    "import_batches",
    "photos",
    "source_versions",
    "source_sheets",
    "data_snapshots",
    "source_files",
    "datasets",
    "audit_log",
    "user_project_access",
]

PATH_COLUMNS = {
    "projects": [
        "logo_owner_path",
        "logo_consultant_planner_path",
        "logo_consultant_path",
        "logo_contractor_path",
    ],
    "photos": ["file_path"],
    "source_files": ["current_file_path"],
    "source_versions": ["file_path"],
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_name(value: str, fallback: str = "project") -> str:
    out = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(value or "").strip())
    out = out.strip("_-")
    return (out or fallback)[:80]


def _json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _json_load(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _clean_path_text(value: str | Path) -> str:
    text = os.path.expandvars(str(value or "").strip())
    # Copy/paste dari Windows Explorer sering membawa tanda kutip.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"\"", "'"}:
        text = text[1:-1].strip()
    return text


def normalize_publish_root(data_dir: Path, publish_root: str | Path | None = None) -> Path:
    data_dir = Path(data_dir)
    text = _clean_path_text(publish_root or "")
    if not text:
        return data_dir / "web_published"
    root = Path(text).expanduser()
    # Relative path selalu ditambatkan ke root aplikasi agar tidak tergantung current working directory.
    if not root.is_absolute():
        root = data_dir.parent / root
    return root


def ensure_publish_root(data_dir: Path, publish_root: str | Path | None = None) -> Path:
    root = normalize_publish_root(data_dir, publish_root)

    # Pesan khusus untuk drive Windows yang tidak tersedia (mis. G: belum terpasang).
    if os.name == "nt" and root.drive:
        drive_root = Path(root.drive + "\\")
        if not drive_root.exists():
            raise FileNotFoundError(
                3,
                f"Drive {root.drive} tidak tersedia. Pilih folder lokal atau drive Google Drive yang benar.",
                str(root),
            )

    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(
            getattr(exc, "winerror", None) or getattr(exc, "errno", 1),
            f"Folder Published tidak dapat dibuat/diakses: {root}. {exc}",
            str(root),
        ) from exc

    # Preflight tulis-hapus: publish harus gagal di depan, bukan setelah paket setengah terbentuk.
    probe = root / ".hdk_publish_write_test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise PermissionError(f"Folder Published tidak dapat ditulis: {root}. {exc}") from exc
    return root


def publish_root_health(data_dir: Path, publish_root: str | Path | None = None) -> dict[str, Any]:
    root = normalize_publish_root(data_dir, publish_root)
    try:
        ready = ensure_publish_root(data_dir, root)
        return {"ok": True, "root": str(ready), "message": "Folder Published siap digunakan."}
    except Exception as exc:
        return {"ok": False, "root": str(root), "message": str(exc)}


def load_publish_root(data_dir: Path) -> Path:
    env = _clean_path_text(os.getenv("HDK_PUBLISH_ROOT", "") or "")
    if env:
        return normalize_publish_root(data_dir, env)
    cfg = _json_load(Path(data_dir) / "publish_settings.json", {}) or {}
    saved = _clean_path_text(cfg.get("publish_root") or "")
    if saved:
        return normalize_publish_root(data_dir, saved)
    return normalize_publish_root(data_dir, None)


def save_publish_root(data_dir: Path, publish_root: str | Path) -> Path:
    root = ensure_publish_root(data_dir, publish_root)
    _json_dump(Path(data_dir) / "publish_settings.json", {"publish_root": str(root)})
    return root


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if not _table_exists(conn, table):
        return []
    return [str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def _query_scalar(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
    try:
        row = conn.execute(sql, tuple(params)).fetchone()
        return default if row is None or row[0] is None else row[0]
    except Exception:
        return default


def project_summary(db_path: Path, project_id: str) -> dict[str, Any]:
    with _connect(db_path) as conn:
        p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not p:
            raise KeyError("Project tidak ditemukan.")
        summary = {
            "project_id": project_id,
            "project_code": p["code"],
            "project_name": p["name"],
            "project_updated_at": p["updated_at"],
            "datasets": int(_query_scalar(conn, "SELECT COUNT(*) FROM datasets WHERE project_id=?", [project_id])),
            "dataset_rows": int(_query_scalar(conn, "SELECT COALESCE(SUM(row_count),0) FROM datasets WHERE project_id=?", [project_id])),
            "source_files": int(_query_scalar(conn, "SELECT COUNT(*) FROM source_files WHERE project_id=?", [project_id])),
            "source_versions": int(_query_scalar(conn, "SELECT COUNT(*) FROM source_versions WHERE project_id=?", [project_id])),
            "photos": int(_query_scalar(conn, "SELECT COUNT(*) FROM photos WHERE project_id=?", [project_id])),
            "snapshots": int(_query_scalar(conn, "SELECT COUNT(*) FROM data_snapshots WHERE project_id=?", [project_id])),
            "access_users": int(_query_scalar(conn, "SELECT COUNT(*) FROM user_project_access WHERE project_id=?", [project_id])),
            "active_internal_users": int(_query_scalar(conn, "SELECT COUNT(*) FROM users WHERE active=1 AND role='internal'")),
        }
        timestamps: list[str] = []
        for sql in [
            "SELECT MAX(updated_at) FROM projects WHERE id=?",
            "SELECT MAX(updated_at) FROM datasets WHERE project_id=?",
            "SELECT MAX(updated_at) FROM source_files WHERE project_id=?",
            "SELECT MAX(created_at) FROM source_versions WHERE project_id=?",
            "SELECT MAX(created_at) FROM photos WHERE project_id=?",
            "SELECT MAX(created_at) FROM data_snapshots WHERE project_id=?",
            "SELECT MAX(created_at) FROM audit_log WHERE project_id=?",
        ]:
            val = str(_query_scalar(conn, sql, [project_id], "") or "")
            if val:
                timestamps.append(val)
        summary["latest_local_change"] = max(timestamps) if timestamps else str(p["updated_at"] or "")
        return summary


def project_asset_health(db_path: Path, data_dir: Path, project_id: str) -> dict[str, Any]:
    """Check every project file reference before publish.

    Missing references are treated as an incomplete package so the UI can stop
    publish before Owner/Consultant receive a dashboard with broken visuals.
    """
    refs: list[tuple[str, str]] = []
    with _connect(Path(db_path)) as conn:
        if _table_exists(conn, "projects"):
            cols = _table_columns(conn, "projects")
            p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if p:
                for col in PATH_COLUMNS["projects"]:
                    if col in cols and str(p[col] or "").strip():
                        refs.append((f"Project/{col}", str(p[col])))
        for table, col in [("photos", "file_path"), ("source_files", "current_file_path"), ("source_versions", "file_path")]:
            if not _table_exists(conn, table) or col not in _table_columns(conn, table):
                continue
            rows = conn.execute(f'SELECT id,"{col}" FROM "{table}" WHERE project_id=?', (project_id,)).fetchall()
            for row in rows:
                raw = str(row[col] or "").strip()
                if raw:
                    refs.append((f"{table}/{row['id']}", raw))

    available: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    recovery_notes: list[str] = []
    for label, raw in refs:
        path, note = storage.find_existing_file(raw, Path(data_dir), project_id)
        if path is None:
            missing.append({"label": label, "path": raw, "filename": Path(raw.replace("\\", "/")).name})
        else:
            available.append({"label": label, "path": raw, "resolved": str(path)})
            if note:
                recovery_notes.append(note)
    return {
        "total": len(refs),
        "available": len(available),
        "missing_count": len(missing),
        "missing": missing,
        "recovery_notes": list(dict.fromkeys(recovery_notes)),
        "ok": len(missing) == 0,
    }


def project_fingerprint(db_path: Path, data_dir: Path, project_id: str) -> str:
    h = hashlib.sha256()
    with _connect(db_path) as conn:
        # Hash metadata rows that define a published project. Dynamic data changes are
        # represented by datasets.updated_at/row_count and snapshots/source versions.
        queries = [
            ("SELECT * FROM projects WHERE id=?", [project_id]),
            ("SELECT * FROM datasets WHERE project_id=? ORDER BY id", [project_id]),
            ("SELECT * FROM function_sources WHERE project_id=? ORDER BY id", [project_id]),
            ("SELECT * FROM source_files WHERE project_id=? ORDER BY id", [project_id]),
            ("SELECT * FROM source_versions WHERE project_id=? ORDER BY id", [project_id]),
            ("SELECT * FROM source_sheets WHERE project_id=? ORDER BY id", [project_id]),
            ("SELECT * FROM data_snapshots WHERE project_id=? ORDER BY id", [project_id]),
            ("SELECT * FROM photos WHERE project_id=? ORDER BY id", [project_id]),
            ("SELECT * FROM user_project_access WHERE project_id=? ORDER BY user_id", [project_id]),
            ("SELECT id,username,email,full_name,role,active,updated_at FROM users WHERE role<>'admin' ORDER BY id", []),
        ]
        referenced_paths: set[str] = set()
        for sql, params in queries:
            if "source_sheets" in sql and not _table_exists(conn, "source_sheets"):
                continue
            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                data = dict(row)
                h.update(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
                for key in [
                    "logo_owner_path", "logo_consultant_planner_path", "logo_consultant_path", "logo_contractor_path",
                    "file_path", "current_file_path",
                ]:
                    value = str(data.get(key) or "").strip()
                    if value:
                        referenced_paths.add(value)
        for raw in sorted(referenced_paths):
            p = Path(raw)
            if not p.is_absolute():
                candidate = Path(data_dir).parent / p if str(p).replace("\\", "/").startswith("data/") else Path(data_dir) / p
                p = candidate
            try:
                stat = p.stat()
                h.update(f"{raw}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8"))
            except Exception:
                h.update(f"{raw}|MISSING".encode("utf-8"))
    return h.hexdigest()


def _backup_sqlite(source_db: Path, target_db: Path) -> None:
    target_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_db, timeout=30) as src, sqlite3.connect(target_db, timeout=30) as dst:
        src.backup(dst)


def _selected_dynamic_tables(conn: sqlite3.Connection, project_id: str) -> set[str]:
    names: set[str] = set()
    if _table_exists(conn, "datasets"):
        for r in conn.execute("SELECT table_name FROM datasets WHERE project_id=?", (project_id,)).fetchall():
            if r[0]:
                names.add(str(r[0]))
    if _table_exists(conn, "data_snapshots"):
        for r in conn.execute("SELECT table_name FROM data_snapshots WHERE project_id=?", (project_id,)).fetchall():
            if r[0]:
                names.add(str(r[0]))
    return names


def _prune_db_to_project(bundle_db: Path, project_id: str) -> None:
    with _connect(bundle_db) as conn:
        dynamic_keep = _selected_dynamic_tables(conn, project_id)
        conn.execute("PRAGMA foreign_keys=OFF")

        for table in PROJECT_SCOPED_TABLES:
            if _table_exists(conn, table) and "project_id" in _table_columns(conn, table):
                conn.execute(f'DELETE FROM "{table}" WHERE project_id IS NULL OR project_id<>?', (project_id,))
        if _table_exists(conn, "projects"):
            conn.execute("DELETE FROM projects WHERE id<>?", (project_id,))

        # Web package never contains an Admin account. Keep all non-admin users so
        # user state (active/reset/name) stays consistent across project bundles.
        if _table_exists(conn, "users"):
            conn.execute("DELETE FROM users WHERE role='admin'")
        if _table_exists(conn, "user_project_access"):
            conn.execute("DELETE FROM user_project_access WHERE project_id<>?", (project_id,))

        all_tables = [str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in all_tables:
            if table in CORE_TABLES or table == "sqlite_sequence" or table in dynamic_keep:
                continue
            conn.execute(f'DROP TABLE IF EXISTS "{table.replace(chr(34), chr(34)*2)}"')
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("VACUUM")


def _resolve_source_path(raw: str, data_dir: Path) -> Path:
    p = Path(str(raw or "").strip())
    if p.is_absolute():
        return p
    norm = str(p).replace("\\", "/")
    if norm.startswith("data/"):
        return data_dir.parent / p
    return data_dir / p


def _candidate_source_paths(raw: str, data_dir: Path, project_id: str = "") -> list[Path]:
    """Return safe candidates for an asset path stored by older/local builds.

    Absolute paths in SQLite can become stale when the whole application folder is
    copied from v2.9.x to another folder.  We therefore also try to remap the
    suffix beginning at ``data/`` to the current data directory and, as a final
    project-scoped fallback, search the current project's isolated folder by
    filename.
    """
    text = str(raw or "").strip().strip('\"').strip("'")
    if not text:
        return []

    out: list[Path] = []

    def add(path: Path) -> None:
        key = os.path.normcase(os.path.normpath(str(path)))
        if all(os.path.normcase(os.path.normpath(str(x))) != key for x in out):
            out.append(path)

    try:
        add(_resolve_source_path(text, data_dir))
    except Exception:
        pass

    # Remap a stale absolute path from an older extracted application folder.
    # Example: C:/.../v2.9.15/data/projects/... -> <current>/data/projects/...
    norm = text.replace("\\", "/")
    parts = [part for part in norm.split("/") if part not in {"", "."}]
    data_indexes = [idx for idx, part in enumerate(parts) if part.lower() == "data"]
    if data_indexes:
        idx = data_indexes[-1]
        suffix = parts[idx + 1 :]
        if suffix:
            add(Path(data_dir).joinpath(*suffix))

    # Project-scoped fallback after isolated-storage migration. Avoid a global
    # filename search so same-named files from Project A/B cannot be mixed.
    filename = Path(norm).name
    projects_root = Path(data_dir) / "projects"
    if project_id and filename and projects_root.exists():
        try:
            for project_root in projects_root.iterdir():
                if not project_root.is_dir() or not project_root.name.endswith(f"__{project_id}"):
                    continue
                for match in project_root.rglob(filename):
                    add(match)
                break
        except OSError:
            pass
    return out


def _find_existing_source(raw: str, data_dir: Path, project_id: str = "") -> tuple[Path | None, str]:
    """Find a readable file across current and previous portable app builds."""
    return storage.find_existing_file(raw, data_dir, project_id)


def _copy_into_bundle(source: Path, bundle_root: Path, data_dir: Path) -> str:
    """Copy one referenced asset to a deliberately short bundle path.

    Published bundles may live under a long Windows path (Desktop/company/project/
    web_published/...). Preserving the full WIP tree can easily exceed the classic
    Windows path limit and surface as WinError 3 even when the source file exists.

    The database only needs a stable relative path, not the original directory
    hierarchy. Assets are therefore stored under ``data/f/<kind>/<hash>.<ext>``.
    The original filename remains in SQLite/manifest metadata elsewhere, while the
    physical bundle path stays short and portable.
    """
    source = source.resolve(strict=False)
    suffix = source.suffix.lower()

    # Keep a tiny category purely for readability; never preserve the long source
    # directory hierarchy inside a Published package.
    norm_parts = [part.lower() for part in source.parts]
    if "photos" in norm_parts:
        kind = "photo"
    elif "bim" in norm_parts:
        kind = "bim"
    elif "excel" in norm_parts or suffix in {".xlsx", ".xlsm", ".xls"}:
        kind = "excel"
    elif "assets" in norm_parts or "project_assets" in norm_parts:
        kind = "logo"
    else:
        kind = "file"

    try:
        st = source.stat()
        identity = f"{source}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        identity = str(source)
    token = hashlib.sha1(identity.encode("utf-8", errors="ignore")).hexdigest()[:16]
    rel = Path("data") / "f" / kind / f"{token}{suffix}"
    dest = bundle_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.exists() and source.is_file():
        if not dest.exists() or source.stat().st_size != dest.stat().st_size:
            shutil.copy2(source, dest)
    return rel.as_posix()


def _portable_paths_and_copy_assets(bundle_db: Path, bundle_root: Path, data_dir: Path) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    copied: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    warnings: list[str] = []

    # Bundle has already been pruned to one project, so this is safe and keeps
    # stale-path recovery strictly inside that project's isolated storage.
    project_id = ""
    try:
        with _connect(bundle_db) as probe:
            row = probe.execute("SELECT id FROM projects LIMIT 1").fetchone() if _table_exists(probe, "projects") else None
            project_id = str(row[0]) if row else ""
    except Exception:
        project_id = ""

    def portable(raw: str) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        try:
            src, recovery_note = _find_existing_source(text, data_dir, project_id)
        except Exception as exc:
            missing.append(text)
            warnings.append(f"Path tidak dapat diperiksa: {text} ({exc})")
            return ""
        if src is None:
            missing.append(text)
            return ""
        if recovery_note:
            warnings.append(recovery_note)
        try:
            rel = _copy_into_bundle(src, bundle_root, data_dir)
            dest = bundle_root / rel
            if not dest.exists() or not dest.is_file():
                missing.append(text)
                warnings.append(f"File tidak berhasil disalin: {src}")
                return ""
            copied[rel] = {"path": rel, "size": dest.stat().st_size, "sha256": sha256_file(dest)}
            return rel
        except (OSError, shutil.Error) as exc:
            # Satu file lama/hilang tidak boleh menggagalkan seluruh publish.
            # Web bundle akan mendapat path kosong dan manifest mencatat warning.
            missing.append(text)
            warnings.append(f"Gagal menyalin asset: {src} ({exc})")
            return ""

    with _connect(bundle_db) as conn:
        if _table_exists(conn, "projects"):
            cols = _table_columns(conn, "projects")
            for col in PATH_COLUMNS["projects"]:
                if col in cols:
                    rows = conn.execute(f'SELECT id,"{col}" FROM projects').fetchall()
                    for r in rows:
                        new = portable(r[col])
                        conn.execute(f'UPDATE projects SET "{col}"=? WHERE id=?', (new, r["id"]))
        for table in ["photos", "source_files", "source_versions"]:
            if not _table_exists(conn, table):
                continue
            cols = _table_columns(conn, table)
            for col in PATH_COLUMNS[table]:
                if col not in cols:
                    continue
                rows = conn.execute(f'SELECT id,"{col}" FROM "{table}"').fetchall()
                for r in rows:
                    new = portable(r[col])
                    conn.execute(f'UPDATE "{table}" SET "{col}"=? WHERE id=?', (new, r["id"]))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        conn.execute("PRAGMA journal_mode=DELETE")
    for sidecar in [Path(str(bundle_db) + "-wal"), Path(str(bundle_db) + "-shm")]:
        try:
            sidecar.unlink(missing_ok=True)
        except Exception:
            pass
    return list(copied.values()), sorted(set(missing)), list(dict.fromkeys(warnings))



def _copy_project_storage_snapshot(bundle_root: Path, data_dir: Path, project_id: str, project_code: str, project_name: str) -> list[str]:
    """Legacy compatibility hook.

    Older builds copied the *entire* isolated project directory into every Published
    package. On Windows that produced very deep destination paths and caused
    WinError 3/206 for perfectly valid files. The actual web dashboard only needs
    the pruned project database plus files referenced by that database; those files
    are copied by :func:`_portable_paths_and_copy_assets` to short paths.

    Keep this function as a no-op so existing publish flow/call sites remain stable.
    """
    return []


def _verify_bundle_references(bundle_db: Path, bundle_root: Path) -> list[str]:
    """Return non-empty DB file references that are not real files in bundle."""
    missing: list[str] = []
    with _connect(bundle_db) as conn:
        for table, cols in PATH_COLUMNS.items():
            if not _table_exists(conn, table):
                continue
            table_cols = _table_columns(conn, table)
            for col in cols:
                if col not in table_cols:
                    continue
                rows = conn.execute(f'SELECT id,"{col}" FROM "{table}"').fetchall()
                for row in rows:
                    raw = str(row[col] or "").strip()
                    if not raw:
                        continue
                    pp = Path(raw)
                    candidate = pp if pp.is_absolute() else Path(bundle_root) / pp
                    try:
                        ok = candidate.exists() and candidate.is_file()
                    except OSError:
                        ok = False
                    if not ok:
                        missing.append(f"{table}/{row['id']}/{col}: {raw}")
    return missing

def _project_dir(publish_root: Path, project_id: str, project_code: str) -> Path:
    root = publish_root / "projects"
    if root.exists():
        for child in root.iterdir():
            if child.is_dir() and child.name.endswith(f"__{project_id}"):
                return child
    return root / f"{_safe_name(project_code)[:32]}__{project_id}"


def current_manifest(publish_root: Path, project_id: str, project_code: str | None = None) -> dict[str, Any] | None:
    if project_code:
        pdir = _project_dir(publish_root, project_id, project_code)
        pointer = _json_load(pdir / "current.json", {}) or {}
        manifest_path = pdir / str(pointer.get("version") or "") / "manifest.json"
        return _json_load(manifest_path, None) if manifest_path.exists() else None
    root = publish_root / "projects"
    if not root.exists():
        return None
    for pdir in root.iterdir():
        if pdir.is_dir() and pdir.name.endswith(f"__{project_id}"):
            pointer = _json_load(pdir / "current.json", {}) or {}
            manifest_path = pdir / str(pointer.get("version") or "") / "manifest.json"
            if manifest_path.exists():
                return _json_load(manifest_path, None)
    return None


def list_project_versions(publish_root: Path, project_id: str, project_code: str) -> list[dict[str, Any]]:
    pdir = _project_dir(publish_root, project_id, project_code)
    if not pdir.exists():
        return []
    current = (_json_load(pdir / "current.json", {}) or {}).get("version")
    versions: list[dict[str, Any]] = []
    for child in sorted(pdir.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        manifest = _json_load(child / "manifest.json", None)
        if manifest:
            manifest = dict(manifest)
            manifest["is_current"] = child.name == current
            manifest["version_dir"] = str(child)
            manifest["zip_path"] = str(child / str(manifest.get("zip_name") or ""))
            versions.append(manifest)
    return versions


def _update_index(publish_root: Path) -> dict[str, Any]:
    projects: list[dict[str, Any]] = []
    root = publish_root / "projects"
    if root.exists():
        for pdir in sorted(root.iterdir()):
            if not pdir.is_dir():
                continue
            pointer = _json_load(pdir / "current.json", {}) or {}
            version = str(pointer.get("version") or "")
            manifest = _json_load(pdir / version / "manifest.json", None) if version else None
            if manifest:
                projects.append({
                    "project_id": manifest.get("project_id"),
                    "project_code": manifest.get("project_code"),
                    "project_name": manifest.get("project_name"),
                    "version": manifest.get("version"),
                    "generated_at": manifest.get("generated_at"),
                    "fingerprint": manifest.get("fingerprint"),
                    "bundle_dir": str((pdir / version).resolve()),
                })
    index = {"schema_version": PUBLISH_SCHEMA_VERSION, "updated_at": _now(), "projects": projects}
    _json_dump(publish_root / "published_index.json", index)
    return index


def publish_project(source_db: Path, data_dir: Path, project_id: str, publish_root: Path) -> dict[str, Any]:
    source_db = Path(source_db)
    data_dir = Path(data_dir)
    if not source_db.exists():
        raise FileNotFoundError(f"Database lokal tidak ditemukan: {source_db}")

    stage = "menyiapkan folder Published"
    version_dir: Path | None = None
    try:
        publish_root = ensure_publish_root(data_dir, publish_root)

        stage = "membaca ringkasan proyek"
        summary = project_summary(source_db, project_id)
        fingerprint = project_fingerprint(source_db, data_dir, project_id)

        stage = "membuat folder versi"
        version = datetime.now().strftime("%Y%m%d_%H%M%S")
        project_dir = _project_dir(publish_root, project_id, str(summary["project_code"]))
        project_dir.mkdir(parents=True, exist_ok=True)
        version_dir = project_dir / version
        if version_dir.exists():
            version += "_" + datetime.now().strftime("%f")[:4]
            version_dir = project_dir / version
        version_dir.mkdir(parents=True, exist_ok=False)

        stage = "membuat salinan database proyek"
        bundle_db = version_dir / "project_hub.db"
        _backup_sqlite(source_db, bundle_db)
        _prune_db_to_project(bundle_db, project_id)

        stage = "menyalin folder proyek WIP"
        _copy_project_storage_snapshot(
            version_dir, data_dir, project_id,
            str(summary.get("project_code") or ""),
            str(summary.get("project_name") or ""),
        )

        stage = "menyalin Excel, foto, BIM, dan logo"
        files, missing, asset_warnings = _portable_paths_and_copy_assets(bundle_db, version_dir, data_dir)

        stage = "memvalidasi kelengkapan paket"
        bundle_missing = _verify_bundle_references(bundle_db, version_dir)
        if bundle_missing:
            asset_warnings.extend([f"Referensi paket belum valid: {item}" for item in bundle_missing])

        generated_at = _now()
        manifest: dict[str, Any] = {
            "schema_version": PUBLISH_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "version": version,
            "generated_at": generated_at,
            "project_id": project_id,
            "project_code": summary["project_code"],
            "project_name": summary["project_name"],
            "fingerprint": fingerprint,
            "summary": summary,
            "database": "project_hub.db",
            "files": files,
            "missing_files": missing,
            "asset_warnings": asset_warnings,
        }
        _json_dump(version_dir / "manifest.json", manifest)
        (version_dir / "PACKAGE_README.txt").write_text(
            "HDK Project Data Hub - Published Project Package\n"
            "Paket ini dibuat dari workspace lokal yang sudah direview Admin.\n"
            "Jangan edit isi paket secara manual. Gunakan Publish & Sync dari aplikasi.\n",
            encoding="utf-8",
        )

        stage = "membuat ZIP Published"
        zip_code = _safe_name(str(summary["project_code"]))[:40]
        zip_name = f"{zip_code}_{version}_PUBLISHED.zip"
        zip_path = version_dir / zip_name
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in version_dir.rglob("*"):
                if path.is_file() and path != zip_path:
                    zf.write(path, path.relative_to(version_dir))
        manifest["zip_name"] = zip_name
        manifest["zip_size"] = zip_path.stat().st_size
        manifest["zip_sha256"] = sha256_file(zip_path)
        _json_dump(version_dir / "manifest.json", manifest)

        stage = "mengaktifkan versi Published"
        _json_dump(project_dir / "current.json", {
            "version": version,
            "generated_at": generated_at,
            "project_id": project_id,
            "project_code": summary["project_code"],
        })
        _update_index(publish_root)
        manifest["version_dir"] = str(version_dir)
        manifest["zip_path"] = str(zip_path)
        return manifest
    except Exception as exc:
        # Paket setengah jadi tidak boleh muncul sebagai histori Published.
        if version_dir is not None and version_dir.exists():
            try:
                shutil.rmtree(version_dir, ignore_errors=True)
            except Exception:
                pass
        raise RuntimeError(f"Publish gagal pada tahap '{stage}': {exc}") from exc

def set_current_version(publish_root: Path, project_id: str, project_code: str, version: str) -> dict[str, Any]:
    pdir = _project_dir(Path(publish_root), project_id, project_code)
    manifest = _json_load(pdir / version / "manifest.json", None)
    if not manifest:
        raise FileNotFoundError("Versi publish tidak ditemukan.")
    _json_dump(pdir / "current.json", {
        "version": version,
        "generated_at": manifest.get("generated_at"),
        "project_id": project_id,
        "project_code": project_code,
        "rollback_at": _now(),
    })
    _update_index(Path(publish_root))
    return manifest


def compare_with_current(source_db: Path, data_dir: Path, project_id: str, publish_root: Path) -> dict[str, Any]:
    summary = project_summary(source_db, project_id)
    fingerprint = project_fingerprint(source_db, data_dir, project_id)
    manifest = current_manifest(Path(publish_root), project_id, str(summary["project_code"]))
    old_summary = (manifest or {}).get("summary") or {}
    count_keys = ["datasets", "dataset_rows", "source_files", "source_versions", "photos", "snapshots", "access_users", "active_internal_users"]
    changes: list[dict[str, Any]] = []
    for key in count_keys:
        before = int(old_summary.get(key) or 0)
        after = int(summary.get(key) or 0)
        if before != after:
            changes.append({"item": key, "published": before, "local": after, "delta": after - before})
    if manifest and str(old_summary.get("project_updated_at") or "") != str(summary.get("project_updated_at") or ""):
        changes.append({"item": "master_project", "published": old_summary.get("project_updated_at"), "local": summary.get("project_updated_at"), "delta": "changed"})
    published_complete = bool(manifest and not (manifest.get("missing_files") or []))
    same_fingerprint = bool(manifest and manifest.get("fingerprint") == fingerprint)
    return {
        "summary": summary,
        "fingerprint": fingerprint,
        "current_manifest": manifest,
        "is_published": bool(manifest),
        # Paket lama yang dibuat dengan asset hilang tidak boleh dianggap up to date.
        # Setelah file WIP dipulihkan, Admin harus bisa rebuild paket Published
        # walaupun fingerprint metadata/database belum berubah.
        "published_complete": published_complete,
        "same_fingerprint": same_fingerprint,
        "is_up_to_date": bool(same_fingerprint and published_complete),
        "needs_rebuild": bool(manifest and same_fingerprint and not published_complete),
        "changes": changes,
    }


def _copy_tree_contents(source: Path, target: Path) -> None:
    if not source.exists():
        return
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(source)
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)


def _create_table_like(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> None:
    row = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not row or not row[0]:
        return
    dst.execute(f'DROP TABLE IF EXISTS "{table.replace(chr(34), chr(34)*2)}"')
    dst.execute(str(row[0]))


def _copy_table_all(src: sqlite3.Connection, dst: sqlite3.Connection, table: str, replace: bool = True) -> None:
    if not _table_exists(src, table):
        return
    src_cols = _table_columns(src, table)
    if not src_cols:
        return
    if not _table_exists(dst, table):
        row = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row and row[0]:
            dst.execute(str(row[0]))
    dst_cols = _table_columns(dst, table)
    cols = [c for c in src_cols if c in dst_cols]
    if not cols:
        return
    qcols = ",".join(f'"{c.replace(chr(34), chr(34)*2)}"' for c in cols)
    placeholders = ",".join("?" for _ in cols)
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    rows = src.execute(f'SELECT {qcols} FROM "{table.replace(chr(34), chr(34)*2)}"').fetchall()
    if rows:
        dst.executemany(f'{verb} INTO "{table.replace(chr(34), chr(34)*2)}" ({qcols}) VALUES ({placeholders})', [tuple(r[c] for c in cols) for r in rows])


def _merge_bundle_db(source_db: Path, target_db: Path, first: bool) -> None:
    if first:
        shutil.copy2(source_db, target_db)
        return
    with _connect(source_db) as src, _connect(target_db) as dst:
        dst.execute("PRAGMA foreign_keys=OFF")
        # Core tables. Users are global; project-specific records have stable IDs.
        for table in ["users", "projects", "datasets", "function_sources", "import_batches", "photos", "source_files", "source_versions", "source_sheets", "data_snapshots", "user_project_access"]:
            _copy_table_all(src, dst, table, replace=True)
        # Audit log has integer IDs. Same local source uses globally unique IDs, so replace is fine.
        _copy_table_all(src, dst, "audit_log", replace=True)

        source_tables = [str(r[0]) for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in source_tables:
            if table in CORE_TABLES or table == "sqlite_sequence":
                continue
            _create_table_like(src, dst, table)
            _copy_table_all(src, dst, table, replace=False)
        dst.commit()


def _rewrite_target_paths(target_db: Path, target_data_dir: Path) -> None:
    app_root = target_data_dir.parent

    def absolute(raw: str) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        p = Path(text)
        if p.is_absolute():
            return str(p)
        norm = str(p).replace("\\", "/")
        if norm.startswith("data/"):
            # Path di dalam paket Published selalu relatif terhadap root paket,
            # mis. data/projects/<project>/source/excel/file.xlsx. Setelah paket
            # direbuild ke web_preview, folder "data/" paket menjadi langsung
            # target_data_dir. Jangan tambahkan "data" dua kali.
            rel = Path(*p.parts[1:]) if len(p.parts) > 1 else Path()
            return str((target_data_dir / rel).resolve())
        return str((target_data_dir / p).resolve())

    with _connect(target_db) as conn:
        for table, cols in PATH_COLUMNS.items():
            if not _table_exists(conn, table):
                continue
            table_cols = _table_columns(conn, table)
            for col in cols:
                if col not in table_cols:
                    continue
                rows = conn.execute(f'SELECT id,"{col}" FROM "{table}"').fetchall()
                for r in rows:
                    conn.execute(f'UPDATE "{table}" SET "{col}"=? WHERE id=?', (absolute(r[col]), r["id"]))
        conn.commit()


def rebuild_live_from_publish_root(publish_root: Path, target_data_dir: Path) -> dict[str, Any]:
    publish_root = Path(publish_root).expanduser()
    target_data_dir = Path(target_data_dir).expanduser()
    try:
        if publish_root.resolve().is_relative_to(target_data_dir.resolve()):
            raise ValueError("Folder Published tidak boleh berada di dalam target data web. Gunakan folder/mount terpisah.")
    except FileNotFoundError:
        pass
    index = _json_load(publish_root / "published_index.json", {}) or {}
    projects = list(index.get("projects") or [])
    if not projects:
        raise RuntimeError("Belum ada project Published pada folder ini.")

    # Oldest first, newest last. User profile state from the newest package wins.
    projects.sort(key=lambda x: str(x.get("generated_at") or ""))
    target_data_dir.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="hdk_web_sync_", dir=str(target_data_dir.parent)))
    temp_data = temp_root / "data"
    temp_data.mkdir(parents=True, exist_ok=True)
    temp_db = temp_data / "project_hub.db"

    try:
        first = True
        synced: list[dict[str, Any]] = []
        for item in projects:
            bundle_dir = Path(str(item.get("bundle_dir") or ""))
            manifest = _json_load(bundle_dir / "manifest.json", None)
            source_db = bundle_dir / "project_hub.db"
            if not manifest or not source_db.exists():
                continue
            _merge_bundle_db(source_db, temp_db, first=first)
            first = False
            bundle_data = bundle_dir / "data"
            _copy_tree_contents(bundle_data, temp_data)
            synced.append({
                "project_id": manifest.get("project_id"),
                "project_code": manifest.get("project_code"),
                "project_name": manifest.get("project_name"),
                "version": manifest.get("version"),
                "generated_at": manifest.get("generated_at"),
            })

        if first or not temp_db.exists():
            raise RuntimeError("Tidak ada paket Published valid yang dapat disinkronkan.")
        _rewrite_target_paths(temp_db, target_data_dir)

        # Atomic-ish replacement: preserve target publish preview only, not WIP data.
        backup_dir = target_data_dir.parent / f"{target_data_dir.name}_previous"
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        if target_data_dir.exists():
            # Windows dapat menahan handle SQLite/file pada folder preview lama.
            # Pindahkan preview lama lebih dulu agar hasil sinkron baru tidak bercampur.
            shutil.rmtree(backup_dir, ignore_errors=True)
            try:
                target_data_dir.rename(backup_dir)
            except PermissionError as exc:
                raise RuntimeError(
                    "Folder Web Preview sedang dipakai Windows/aplikasi lain. "
                    "Tutup jendela Web Preview/Streamlit yang masih berjalan, lalu coba lagi."
                ) from exc

        # Jangan rename temp_data -> target_data_dir di Windows. Pada beberapa instalasi
        # Python/Windows Store, os.rename() pada direktori sementara dapat menghasilkan
        # WinError 5 meskipun source/destination berada di drive yang sama. Copytree
        # lebih stabil dan temp_root akan dibersihkan pada blok finally.
        try:
            shutil.copytree(temp_data, target_data_dir, dirs_exist_ok=False)
        except Exception as exc:
            # Jangan tinggalkan preview setengah jadi. Jika ada preview sebelumnya,
            # coba pulihkan kembali agar user tetap punya versi terakhir yang valid.
            shutil.rmtree(target_data_dir, ignore_errors=True)
            if backup_dir.exists() and not target_data_dir.exists():
                try:
                    backup_dir.rename(target_data_dir)
                except Exception:
                    pass
            raise RuntimeError(f"Gagal memasang data Web Preview baru: {exc}") from exc

        _json_dump(target_data_dir / "sync_status.json", {
            "synced_at": _now(),
            "publish_root": str(publish_root),
            "project_count": len(synced),
            "projects": synced,
        })
        shutil.rmtree(backup_dir, ignore_errors=True)
        return {"synced_at": _now(), "project_count": len(synced), "projects": synced, "target_db": str(target_data_dir / "project_hub.db")}
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def sync_if_changed(publish_root: Path, target_data_dir: Path) -> dict[str, Any] | None:
    index_path = Path(publish_root) / "published_index.json"
    if not index_path.exists():
        return None
    marker_path = Path(target_data_dir) / "sync_status.json"
    marker = _json_load(marker_path, {}) or {}
    index = _json_load(index_path, {}) or {}
    if marker.get("publish_index_updated_at") == index.get("updated_at") and (Path(target_data_dir) / "project_hub.db").exists():
        return None
    result = rebuild_live_from_publish_root(Path(publish_root), Path(target_data_dir))
    status = _json_load(Path(target_data_dir) / "sync_status.json", {}) or {}
    status["publish_index_updated_at"] = index.get("updated_at")
    _json_dump(Path(target_data_dir) / "sync_status.json", status)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync HDK Published packages into a read-only web data directory.")
    parser.add_argument("--publish-root", required=True)
    parser.add_argument("--target-data", required=True)
    args = parser.parse_args()
    result = rebuild_live_from_publish_root(Path(args.publish_root), Path(args.target_data))
    print(json.dumps(result, ensure_ascii=False, indent=2))
