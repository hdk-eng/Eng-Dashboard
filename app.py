from __future__ import annotations

import io
import os
import base64
import mimetypes
import hashlib
import hmac
import re
import html
import shutil
import uuid
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import plotly.express as px
import streamlit as st
from PIL import Image, ImageOps

from src import db
from src.importers import (
    build_dataframe,
    column_diagnostics,
    excel_sheet_names,
    guess_header_row,
    read_raw_sheet,
)
from src.ui import BENTO_CSS, dataframe_to_xlsx_bytes, fmt_bytes, fmt_int, global_search, hero, metric_card, numeric_columns
from src.visuals import (
    classify_sheet, extract_workbook_visuals, executive_summary, workbook_quality,
    default_function_for_profile, build_register_sync_frame, compare_register_frames,
    ENGINEERING_REGISTER_KINDS,
)
from src.renderers import render_payload, render_executive, render_engineering_dashboard, render_lookahead
from src.assets import normalize_logo_bytes, normalized_logo_data_uri
from src import publish, storage, reporting

APP_DIR = Path(__file__).resolve().parent
# Normal lokal: DB berada di <app>/data. Web Preview: LOCAL_DB_PATH menunjuk ke
# <app>/data/web_preview/project_hub.db, sehingga semua asset harus dibaca dari
# folder yang sama dengan database aktif, bukan selalu dari <app>/data.
DATA_DIR = Path(os.getenv("HDK_DATA_DIR", str(db.DB_PATH.parent))).resolve()
PROJECTS_DIR = DATA_DIR / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
LOCAL_ADMIN_PIN_FILE = DATA_DIR / ".local_admin_pin"

SHEET_FUNCTIONS = [
    "Project Control · Kurva S",
    "Project Control · Rencana 2 Mingguan",
    "Project Control · Progress & Schedule",
    "Cost Control",
    "Procurement",
    "Engineering & BIM · Model Progress",
    "Engineering & BIM · Clash & Issue",
    "Engineering & BIM · RFI",
    "Engineering & BIM · Site Instruction",
    "Engineering & BIM · Shop Drawing",
    "Engineering & BIM · Material Approval",
    "Engineering & BIM · Work Method Statement",
    "QAQC",
    "SHE",
    "Risk & Issue",
    "Cashflow",
    "Commercial / Contract",
    "Manpower",
    "Equipment",
    "Other",
]

st.set_page_config(
    page_title="HDK Project Data Hub",
    page_icon="🏗️",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.markdown(BENTO_CSS, unsafe_allow_html=True)

# Optional server-side auto-sync. This is intended for a self-hosted web server
# where the Published folder is mounted/synced (for example via Google Drive/rclone).
# Streamlit Community Cloud cannot mount a local Google Drive Desktop folder directly.
if str(os.getenv("HDK_WEB_PUBLISHED_ONLY", "0") or "0").strip().lower() in {"1", "true", "yes", "on"} \
   and str(os.getenv("HDK_AUTO_SYNC_PUBLISHED", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}:
    _pub_root_env = str(os.getenv("HDK_PUBLISH_ROOT", "") or "").strip()
    _target_db_env = str(os.getenv("LOCAL_DB_PATH", "") or "").strip()
    if _pub_root_env and _target_db_env:
        try:
            publish.sync_if_changed(Path(_pub_root_env), Path(_target_db_env).parent)
        except Exception:
            # Keep app bootable so an operator can inspect logs/configuration.
            pass

db.init_db()

ROLE_LABELS = {
    "admin": "Admin HDK",
    "internal": "Internal HDK",
    "owner": "Owner",
    "consultant": "Konsultan (Perencana/Pengawas)",
}


def _setting(name: str, default: str = "") -> str:
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return str(os.getenv(name, default) or default)


def _local_mode_enabled() -> bool:
    """True only when the local Windows launcher explicitly enables local test mode.

    The launcher binds Streamlit to 127.0.0.1, so this recovery path is not exposed
    to other computers. Cloud/server deployments do not set HDK_LOCAL_MODE.
    """
    return _setting("HDK_LOCAL_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}


LOCAL_MODE = _local_mode_enabled()
PUBLISHED_ONLY = _setting("HDK_WEB_PUBLISHED_ONLY", "0").strip().lower() in {"1", "true", "yes", "on"}
if PUBLISHED_ONLY:
    LOCAL_MODE = False


@st.cache_resource(show_spinner=False)
def _migrate_project_storage_once(db_path_text: str, data_dir_text: str):
    return storage.migrate_legacy_layout(Path(db_path_text), Path(data_dir_text))


# WIP/local storage is migrated once into isolated per-project folders. Published-only
# web data already arrives as a portable package and is left untouched.
_STORAGE_MIGRATION = None
if not PUBLISHED_ONLY:
    try:
        _STORAGE_MIGRATION = _migrate_project_storage_once(str(db.DB_PATH), str(DATA_DIR))
    except Exception:
        _STORAGE_MIGRATION = None


def _hash_local_admin_pin(pin: str) -> str:
    pin = str(pin or "")
    if len(pin) < 8:
        raise ValueError("PIN Admin Lokal minimal 8 karakter.")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, 240_000)
    return f"v1:{salt.hex()}:{digest.hex()}"


def _local_admin_pin_is_configured() -> bool:
    try:
        return LOCAL_ADMIN_PIN_FILE.exists() and bool(LOCAL_ADMIN_PIN_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return False


def _set_local_admin_pin(pin: str) -> None:
    LOCAL_ADMIN_PIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = _hash_local_admin_pin(pin)
    LOCAL_ADMIN_PIN_FILE.write_text(payload, encoding="utf-8")


def _verify_local_admin_pin(pin: str) -> bool:
    try:
        payload = LOCAL_ADMIN_PIN_FILE.read_text(encoding="utf-8").strip()
        version, salt_hex, digest_hex = payload.split(":", 2)
        if version != "v1":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", str(pin or "").encode("utf-8"), salt, 240_000)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


# System/recovery Admin from server/Streamlit Secrets. Safe to run on every app start.
_bootstrap_password = _setting("HDK_ADMIN_PASSWORD")
_bootstrap_email = _setting("HDK_ADMIN_EMAIL", "admin@hdk.local")
_bootstrap_username = _setting("HDK_ADMIN_USERNAME", "admin")
if _bootstrap_password and not PUBLISHED_ONLY:
    try:
        db.ensure_system_admin(_bootstrap_email, _bootstrap_password, username=_bootstrap_username)
    except Exception:
        pass


def require_login() -> dict[str, Any]:
    """App authentication with a separate, local-only Admin gate.

    Normal username/password login is intentionally limited to non-admin users.
    Admin access in local test mode is available only from localhost and requires
    a dedicated local PIN stored as a salted PBKDF2 hash in the data directory.
    """
    local_admin = {
        "id": "__local_admin__",
        "email": "local-admin@hdk",
        "username": "local-admin",
        "full_name": "Administrator Lokal HDK",
        "role": "admin",
        "active": 1,
    }

    if LOCAL_MODE and st.session_state.get("local_admin_mode"):
        return local_admin

    user_id = st.session_state.get("auth_user_id")
    if user_id:
        user = db.get_user(user_id)
        if user and int(user.get("active", 0)) == 1 and str(user.get("role") or "").lower() != "admin":
            return user
        st.session_state.pop("auth_user_id", None)

    st.markdown(hero("HDK Project Data Hub", "Masuk untuk melihat proyek sesuai hak akses Anda."), unsafe_allow_html=True)
    c1, c2, c3 = st.columns([1, 1.35, 1])
    with c2:
        with st.container(border=True):
            st.markdown("### Login")
            with st.form("rbac_login_form"):
                login_name = st.text_input("Nama User", placeholder="contoh: gagah.hdk")
                password = st.text_input("Password", type="password")
                submit = st.form_submit_button("Masuk", type="primary", use_container_width=True)
            if submit:
                user = db.authenticate_user(login_name, password)
                if user and str(user.get("role") or "").lower() == "admin":
                    st.error("Akun Admin tidak dapat digunakan dari form login umum. Gunakan akses Admin Lokal pada komputer server.")
                elif user:
                    st.session_state["auth_user_id"] = user["id"]
                    st.session_state.pop("local_admin_mode", None)
                    st.rerun()
                else:
                    st.error("Nama User/password tidak sesuai atau user sudah dinonaktifkan.")

            if LOCAL_MODE:
                st.markdown("---")
                st.caption("Admin dikunci khusus komputer ini (localhost). Akun Admin tidak bisa digunakan melalui login umum.")
                if not _local_admin_pin_is_configured():
                    st.markdown("#### Buat PIN Admin Lokal")
                    st.caption("Pengaturan satu kali. PIN disimpan dalam bentuk hash, bukan teks biasa. Minimal 8 karakter.")
                    with st.form("setup_local_admin_pin"):
                        pin1 = st.text_input("PIN Admin Lokal baru", type="password")
                        pin2 = st.text_input("Ulangi PIN", type="password")
                        setup_pin = st.form_submit_button("Simpan PIN Admin Lokal", use_container_width=True)
                    if setup_pin:
                        if pin1 != pin2:
                            st.error("PIN dan konfirmasi PIN tidak sama.")
                        else:
                            try:
                                _set_local_admin_pin(pin1)
                                st.success("PIN Admin Lokal berhasil dibuat. Silakan masuk menggunakan PIN tersebut.")
                                st.rerun()
                            except Exception as exc:
                                st.error(str(exc))
                else:
                    with st.form("local_admin_pin_login"):
                        local_pin = st.text_input("PIN Admin Lokal", type="password")
                        admin_submit = st.form_submit_button("Masuk sebagai Admin Lokal", use_container_width=True)
                    if admin_submit:
                        if _verify_local_admin_pin(local_pin):
                            st.session_state.pop("auth_user_id", None)
                            st.session_state["local_admin_mode"] = True
                            st.rerun()
                        else:
                            st.error("PIN Admin Lokal tidak sesuai.")

            st.caption("Akun user dibuat oleh Admin HDK · tidak ada pendaftaran publik. Owner dan Konsultan hanya melihat proyek yang diberikan kepada mereka.")
    st.stop()


def logout_button(user: dict[str, Any]) -> None:
    st.sidebar.caption(f"{ROLE_LABELS.get(user.get('role'), user.get('role'))} · {user.get('full_name') or user.get('username')}")
    if user.get("id") == "__local_admin__":
        if LOCAL_MODE and st.sidebar.button("Keluar / Uji Login User", use_container_width=True, key="local_admin_logout"):
            st.session_state.pop("local_admin_mode", None)
            st.session_state.pop("auth_user_id", None)
            st.session_state.pop("project_id", None)
            st.rerun()
        return
    if st.sidebar.button("Keluar", use_container_width=True, key="rbac_logout"):
        st.session_state.pop("auth_user_id", None)
        st.session_state.pop("project_id", None)
        st.rerun()


def _date_value(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce")
        return None if pd.isna(ts) else ts.date()
    except Exception:
        return None


def _date_iso(value: date | None) -> str:
    return value.isoformat() if value else ""


def _parse_rupiah(value: str) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    digits = re.sub(r"[^0-9]", "", raw)
    if not digits:
        raise ValueError("Nilai kontrak harus berupa angka Rupiah.")
    return float(int(digits))


def _rupiah_input_value(value: Any) -> str:
    try:
        if value in (None, "") or float(value) == 0:
            return ""
        return f"{float(value):,.0f}".replace(",", ".")
    except Exception:
        return ""


@st.cache_data(show_spinner=False, max_entries=48)
def cached_sheet_names(data: bytes, filename: str) -> list[str]:
    return excel_sheet_names(data, filename)


@st.cache_data(show_spinner=False, max_entries=96)
def cached_raw_preview(data: bytes, filename: str, sheet_name: str, data_only: bool, max_rows: int = 500) -> pd.DataFrame:
    return read_raw_sheet(data, filename, sheet_name, data_only=data_only, max_rows=max_rows)


@st.cache_data(show_spinner=False, max_entries=24)
def cached_full_dataframe(
    data: bytes, filename: str, sheet_name: str, header_row: int, fill_down: bool,
    data_only: bool, include_excel_row: bool = False,
) -> pd.DataFrame:
    raw = read_raw_sheet(data, filename, sheet_name, data_only=data_only, max_rows=None)
    return build_dataframe(
        raw, header_row=int(header_row), fill_down=fill_down,
        include_excel_row=include_excel_row,
    )


@st.cache_data(show_spinner=False, max_entries=256)
def cached_thumbnail(path_text: str, mtime_ns: int, max_size: int = 900) -> bytes:
    """Return a lightweight JPEG thumbnail so the gallery never decodes hundreds of full-size photos."""
    path = Path(path_text)
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_size, max_size))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=82, optimize=True)
        return out.getvalue()




@st.cache_data(show_spinner=False, max_entries=256)
def cached_fixed_thumbnail(path_text: str, mtime_ns: int, width: int = 960, height: int = 540) -> bytes:
    """Create a consistent 16:9 thumbnail without distorting the source image."""
    path = Path(path_text)
    with Image.open(path) as img:
        img = img.convert("RGB")
        fitted = ImageOps.contain(img, (width, height), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (width, height), (248, 250, 252))
        x = (width - fitted.width) // 2
        y = (height - fitted.height) // 2
        canvas.paste(fitted, (x, y))
        out = io.BytesIO()
        canvas.save(out, format="JPEG", quality=86, optimize=True)
        return out.getvalue()


@st.cache_data(show_spinner=False, max_entries=72)
def cached_workbook_visuals(path_text: str, mtime_ns: int, selected_sheets: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    return extract_workbook_visuals(path_text, selected_sheets)


@st.cache_data(show_spinner=False, max_entries=72)
def cached_workbook_quality(path_text: str, mtime_ns: int, selected_sheets: tuple[str, ...]) -> dict[str, Any]:
    return workbook_quality(path_text, list(selected_sheets))


def _folder_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


@st.cache_data(show_spinner=False, ttl=30)
def data_folder_backup_bytes(data_dir_text: str, mtime_hint: int) -> bytes:
    """Create a portable backup of SQLite + Excel archives + photos + logos."""
    data_dir = Path(data_dir_text)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if data_dir.exists():
            for item in sorted(data_dir.rglob("*")):
                if item.is_file():
                    try:
                        arcname = Path("data") / item.relative_to(data_dir)
                        zf.write(item, arcname.as_posix())
                    except OSError:
                        pass
    return out.getvalue()


def data_dir_mtime_hint() -> int:
    latest = 0
    if DATA_DIR.exists():
        for item in DATA_DIR.rglob("*"):
            try:
                if item.is_file():
                    latest = max(latest, item.stat().st_mtime_ns)
            except OSError:
                pass
    return latest


def _project_storage_paths(project_id: str) -> dict[str, Path]:
    project = db.get_project(project_id) or {}
    return storage.project_paths(
        DATA_DIR,
        project_id,
        str(project.get("code") or ""),
        str(project.get("name") or ""),
    )


def safe_source_archive_path(project_id: str, source_file_id: str, filename: str) -> Path:
    stem = Path(filename).stem
    suffix = Path(filename).suffix.lower() or ".xlsx"
    clean = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)[:80]
    folder = _project_storage_paths(project_id)["excel"] / source_file_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{clean}{suffix}"


def archive_uploaded_source(project_id: str, source_file_id: str, uploaded, version_label: str) -> str:
    target = safe_source_archive_path(project_id, source_file_id, uploaded.name)
    target.write_bytes(uploaded.getvalue())
    return db.add_source_file_version(
        project_id, source_file_id, version_label, uploaded.name, storage.to_stored_path(target, DATA_DIR), uploaded.size
    )


def overwrite_uploaded_source(project_id: str, source_file_id: str, uploaded, version_label: str = "") -> str:
    """Replace the active workbook bytes while preserving source/sheet identity."""
    src = db.get_source_file(source_file_id)
    old_path = storage.resolve_stored_path(src.get("current_file_path") or "", DATA_DIR)
    # Use a fresh safe path so extension/name changes never leave misleading files.
    target = safe_source_archive_path(project_id, source_file_id, uploaded.name)
    target.write_bytes(uploaded.getvalue())
    vid = db.overwrite_current_source_version(
        source_file_id, uploaded.name, storage.to_stored_path(target, DATA_DIR), uploaded.size, version_label
    )
    try:
        if old_path.exists() and old_path.is_file() and old_path.resolve() != target.resolve():
            # Only the previous active-version file is replaced. Older version files have different paths.
            old_path.unlink()
    except OSError:
        pass
    return vid




def save_project_logo(project_id: str, uploaded, role: str) -> str:
    """Validate, normalize, and persist one official project logo locally."""
    if uploaded is None:
        return ""
    suffix = Path(uploaded.name).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError("Logo harus PNG, JPG/JPEG, atau WEBP.")
    raw = uploaded.getvalue()
    try:
        normalized = normalize_logo_bytes(raw)
    except Exception as exc:
        raise ValueError(f"File logo {role} tidak valid sebagai gambar: {exc}") from exc
    folder = _project_storage_paths(project_id)["assets"]
    folder.mkdir(parents=True, exist_ok=True)
    clean_role = "".join(ch if ch.isalnum() else "_" for ch in role.lower())[:40]
    target = folder / f"{clean_role}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
    target.write_bytes(normalized)
    return storage.to_stored_path(target, DATA_DIR)



@st.cache_data(show_spinner=False, max_entries=64)
def _logo_data_uri(path_text: str, mtime_ns: int) -> str:
    """Normalize even legacy logos at render time so every company appears balanced."""
    return normalized_logo_data_uri(path_text)


def render_project_logos(project: dict[str, Any]) -> None:
    """Compact, presentation-ready four-party identity strip."""
    roles = [
        ("OWNER", project.get("client") or "Owner / Client", project.get("logo_owner_path")),
        ("KONSULTAN PERENCANA", project.get("consultant_planner_name") or "Konsultan Perencana", project.get("logo_consultant_planner_path")),
        ("KONSULTAN PENGAWAS", project.get("consultant_name") or "Konsultan Pengawas", project.get("logo_consultant_path")),
        ("KONTRAKTOR", project.get("contractor_name") or "Kontraktor", project.get("logo_contractor_path")),
    ]
    cards=[]
    for label, entity, path_text in roles:
        logo_html='<div class="project-logo-placeholder">LOGO</div>'
        path = storage.resolve_stored_path(path_text or "", DATA_DIR) if path_text else Path("")
        if path_text and path.exists():
            try:
                uri=_logo_data_uri(str(path), path.stat().st_mtime_ns)
                logo_html=f'<img class="project-logo-img" src="{uri}" alt="{html.escape(label)}">'
            except Exception:
                logo_html='<div class="project-logo-placeholder">LOGO</div>'
        cards.append(
            f'<div class="project-party">'
            f'<div class="project-party-role">{html.escape(label)}</div>'
            f'<div class="project-party-logo">{logo_html}</div>'
            f'<div class="project-party-name">{html.escape(str(entity))}</div>'
            f'</div>'
        )
    st.markdown('<div class="project-brand-strip">'+''.join(cards)+'</div>', unsafe_allow_html=True)


def collect_project_visuals(project_id: str, as_of_date: str | None = None) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], dict[str, dict[str, Any]]]]]:
    """Load workbook visuals for a reporting cut-off.

    ``as_of_date`` means: for every source file, use the latest archived version
    whose upload date is on/before the selected date.  When omitted, use current.
    """
    files_df = db.list_source_files(project_id)
    all_payloads: list[dict[str, Any]] = []
    by_file: list[tuple[dict[str, Any], dict[str, dict[str, Any]]]] = []
    if files_df.empty:
        return all_payloads, by_file
    for _, row in files_df.iterrows():
        version = db.get_source_file_version_as_of(str(row["id"]), as_of_date)
        path_text = version.get("file_path") if version else row.get("current_file_path")
        if not isinstance(path_text, str) or not path_text.strip():
            continue
        path = storage.resolve_stored_path(path_text, DATA_DIR)
        if not path.exists() or path.suffix.lower() not in {".xlsx", ".xlsm"}:
            continue
        selected = db.list_source_sheets(row["id"], selected_only=True)
        names = tuple(selected["sheet_name"].tolist()) if not selected.empty else tuple()
        if not names:
            continue
        try:
            payload_map = cached_workbook_visuals(str(path), path.stat().st_mtime_ns, names)
            meta = row.to_dict()
            meta["version_label"] = version.get("label") if version else "Current"
            meta["version_date"] = str(version.get("created_at") or "")[:10] if version else ""
            by_file.append((meta, payload_map))
            for sh_name, payload in payload_map.items():
                p = dict(payload)
                p["source_file_id"] = row["id"]
                p["source_filename"] = row["filename"]
                p["sheet_name"] = sh_name
                p["version_label"] = meta["version_label"]
                p["version_date"] = meta["version_date"]
                all_payloads.append(p)
        except Exception:
            continue
    return all_payloads, by_file


def flash(kind: str, text: str) -> None:
    st.session_state["flash"] = (kind, text)


def render_flash() -> None:
    item = st.session_state.pop("flash", None)
    if not item:
        return
    kind, text = item
    getattr(st, kind, st.info)(text)


def get_project_options(user: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, str]]:
    projects = db.list_projects_for_user(user)
    if projects.empty:
        return projects, {}
    labels = {row["id"]: f"{row['code']} · {row['name']}" for _, row in projects.iterrows()}
    return projects, labels


def sidebar_project_selector(user: dict[str, Any]) -> str | None:
    projects, labels = get_project_options(user)
    st.sidebar.markdown("### Project Data Hub")
    st.sidebar.caption("v2.9.19 · Portable Project Paths")
    if projects.empty:
        if user.get("role") == "admin":
            st.sidebar.info("Belum ada proyek. Buat proyek dari menu Master Proyek.")
        else:
            st.sidebar.warning("Belum ada proyek yang diberikan kepada akun Anda.")
        return None
    ids = projects["id"].tolist()
    query_project = str(st.query_params.get("project", "") or "")
    if query_project and query_project not in ids:
        st.sidebar.warning("Proyek pada link tidak termasuk hak akses akun ini.")
    current = query_project if query_project in ids else st.session_state.get("project_id")
    index = ids.index(current) if current in ids else 0
    if len(ids) == 1:
        selected = ids[0]
        st.sidebar.caption("Proyek aktif")
        st.sidebar.markdown(f"**{labels[selected]}**")
    else:
        selected = st.sidebar.selectbox("Proyek aktif", ids, index=index, format_func=lambda x: labels[x])
    st.session_state["project_id"] = selected
    st.query_params["project"] = selected
    return selected


def app_access_mode(user: dict[str, Any]) -> str:
    """Only Admin HDK can modify data. All other roles are read-only."""
    force_public = str(st.query_params.get("view", "") or "").lower() == "public"
    if PUBLISHED_ONLY:
        return "viewer"
    if str(user.get("role") or "").lower() == "admin" and not force_public:
        return "admin"
    return "viewer"


def render_project_header(project_id: str, subtitle: str = "") -> dict[str, Any]:
    p = db.get_project(project_id)
    st.markdown(hero(f"{p['code']} · {p['name']}", subtitle), unsafe_allow_html=True)
    render_project_logos(p)
    meta=[]
    if p.get("location"): meta.append(f"Lokasi: {p['location']}")
    if p.get("contract_start") or p.get("contract_finish"): meta.append(f"Kontrak: {p.get('contract_start') or '—'} → {p.get('revised_finish') or p.get('contract_finish') or '—'}")
    if meta: st.caption(" · ".join(meta))
    return p


def render_active_project_context(project_id: str | None, access_mode: str, user: dict[str, Any]) -> dict[str, Any] | None:
    """Persistent project context bar so admin always knows where changes will be saved."""
    if not project_id:
        return None
    p = db.get_project(project_id)
    if not p:
        return None
    code = html.escape(str(p.get("code") or "—"))
    name = html.escape(str(p.get("name") or "—"))
    location = html.escape(str(p.get("location") or ""))
    role_label = ROLE_LABELS.get(str(user.get("role") or ""), str(user.get("role") or "").upper())
    mode_text = f"{role_label} · EDIT & UPDATE" if access_mode == "admin" else f"{role_label} · READ ONLY"
    mode_bg = "#fff7ed" if access_mode == "admin" else "#eff6ff"
    mode_fg = "#9a3412" if access_mode == "admin" else "#1d4ed8"
    loc_html = f'<span style="opacity:.65;margin-left:10px">{location}</span>' if location else ''
    st.markdown(
        f'<div style="border:1px solid rgba(128,128,128,.24);border-radius:16px;padding:10px 14px;margin:0 0 12px 0;display:flex;align-items:center;justify-content:space-between;gap:12px;background:linear-gradient(135deg,rgba(255,255,255,.055),rgba(255,255,255,.012))">'
        f'<div><div style="font-size:.66rem;font-weight:800;letter-spacing:.13em;opacity:.58">PROYEK AKTIF</div>'
        f'<div style="font-size:1.02rem;font-weight:800;margin-top:1px">{code} · {name}{loc_html}</div></div>'
        f'<div style="font-size:.67rem;font-weight:800;padding:5px 9px;border-radius:999px;background:{mode_bg};color:{mode_fg};white-space:nowrap">{mode_text}</div></div>',
        unsafe_allow_html=True,
    )
    projects, labels = get_project_options(user)
    if not projects.empty and len(projects) > 1:
        ids = projects["id"].tolist()
        with st.popover("Ganti Proyek", use_container_width=False):
            idx = ids.index(project_id) if project_id in ids else 0
            new_id = st.selectbox("Pilih proyek aktif", ids, index=idx, format_func=lambda x: labels[x], key=f"context_project_{project_id}")
            if st.button("Gunakan proyek ini", type="primary", use_container_width=True, key=f"switch_context_{project_id}"):
                st.session_state["project_id"] = new_id
                st.query_params["project"] = new_id
                st.rerun()
    return p


def safe_photo_path(project_id: str, filename: str, media_type: str = "progress") -> Path:
    stem = Path(filename).stem
    suffix = Path(filename).suffix.lower() or ".jpg"
    clean = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)[:70]
    paths = _project_storage_paths(project_id)
    mt = str(media_type or "").lower()
    project_folder = paths["bim"] if (mt.startswith("bim") or "bim" in mt) else paths["photos"]
    project_folder.mkdir(parents=True, exist_ok=True)
    return project_folder / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{clean}{suffix}"


def save_media_batch(
    project_id: str, files, media_type: str, category: str, captured_date: date,
    zone: str, wbs_activity: str, description: str, batch_label: str, max_files: int,
) -> tuple[int, list[str]]:
    """Save one visual update batch. New/old batches never get mixed on the dashboard."""
    files = list(files or [])
    if len(files) > max_files:
        return 0, [f"Maksimal {max_files} gambar untuk satu batch {category}."]
    batch_id = uuid.uuid4().hex[:14]
    saved = 0
    errors: list[str] = []
    for order, file in enumerate(files, start=1):
        try:
            Image.open(io.BytesIO(file.getvalue())).verify()
            target = safe_photo_path(project_id, file.name, media_type)
            target.write_bytes(file.getvalue())
            db.add_photo(
                project_id,
                file_name=file.name,
                file_path=storage.to_stored_path(target, DATA_DIR),
                captured_date=captured_date.isoformat(),
                zone=zone,
                category=category,
                wbs_activity=wbs_activity,
                description=description,
                media_type=media_type,
                batch_id=batch_id,
                batch_label=batch_label.strip() or f"{category} · {captured_date.isoformat()}",
                display_order=order,
            )
            saved += 1
        except Exception as exc:
            errors.append(f"{file.name}: {exc}")
    return saved, errors


def render_media_batch(
    project_id: str, media_type: str, slots: int, title: str,
    as_of_date: str | None = None, layout: str = "grid",
) -> None:
    batch = db.latest_photo_batch_as_of(project_id, media_type, as_of_date)
    st.markdown(f"#### {title}")
    if batch.empty:
        st.info("Belum ada update pada tanggal yang dipilih.")
        return
    first = batch.iloc[0]
    st.caption(
        f"{first.get('batch_label') or '-'} · {first.get('captured_date') or '-'}"
        + (f" · {first.get('zone')}" if str(first.get('zone') or '').strip() else "")
    )
    if layout == "stack":
        cols = None
    else:
        cols = st.columns(2)
    for i in range(slots):
        target = st.container() if cols is None else cols[i % 2]
        with target:
            if i < len(batch):
                row = batch.iloc[i]
                raw_media_path = str(row.get("file_path") or "").strip()
                pp = storage.resolve_stored_path(raw_media_path, DATA_DIR) if raw_media_path not in {"", ".", "./", ".\\"} else None
                if pp is not None and pp.is_file():
                    try:
                        if layout == "grid":
                            st.image(cached_fixed_thumbnail(str(pp), pp.stat().st_mtime_ns), use_container_width=True)
                        else:
                            st.image(cached_thumbnail(str(pp), pp.stat().st_mtime_ns, 1500), use_container_width=True)
                    except Exception:
                        try:
                            st.image(str(pp), use_container_width=True)
                        except Exception:
                            st.caption("Gambar tidak dapat dibuka.")
                else:
                    st.markdown('<div class="bento-card"><div class="label">Gambar tidak tersedia</div><div class="sub">File belum tersedia atau referensinya sudah tidak valid.</div></div>', unsafe_allow_html=True)
                cap = str(row.get("description") or "").strip()
                if cap:
                    st.caption(cap[:120])
            else:
                st.markdown('<div class="bento-card"><div class="label">Slot kosong</div><div class="sub">Belum ada gambar pada batch ini.</div></div>', unsafe_allow_html=True)



def render_bim_comparison_grid(project_id: str, as_of_date: str | None = None) -> None:
    """Render 4 BIM screenshots as a tidy 2x2 comparison grid."""
    st.markdown("#### 4 Screenshot Update 3D BIM")
    groups = [
        ("bim_3d_prev", "2 Minggu Lalu"),
        ("bim_3d_current", "2 Minggu Sekarang"),
    ]
    for media_type, label in groups:
        batch = db.latest_photo_batch_as_of(project_id, media_type, as_of_date)
        st.markdown(f"**{label}**")
        cols = st.columns(2, gap="small")
        for i in range(2):
            with cols[i]:
                if i < len(batch):
                    row = batch.iloc[i]
                    raw_media_path = str(row.get("file_path") or "").strip()
                    pp = storage.resolve_stored_path(raw_media_path, DATA_DIR) if raw_media_path not in {"", ".", "./", ".\\"} else None
                    if pp is not None and pp.is_file():
                        try:
                            st.image(cached_fixed_thumbnail(str(pp), pp.stat().st_mtime_ns), use_container_width=True)
                        except Exception:
                            try:
                                st.image(str(pp), use_container_width=True)
                            except Exception:
                                st.caption("Screenshot BIM tidak dapat dibuka.")
                    else:
                        st.markdown(
                            '<div class="bento-card" style="min-height:150px;display:flex;align-items:center;justify-content:center">'
                            '<div><div class="label">Gambar tidak tersedia</div><div class="sub">File BIM belum tersedia atau referensinya tidak valid.</div></div></div>',
                            unsafe_allow_html=True,
                        )
                    cap = str(row.get("description") or "").strip()
                    if cap:
                        st.caption(cap[:100])
                else:
                    st.markdown(
                        '<div class="bento-card" style="min-height:150px;display:flex;align-items:center;justify-content:center">'
                        '<div><div class="label">Slot kosong</div><div class="sub">Belum ada screenshot BIM.</div></div></div>',
                        unsafe_allow_html=True,
                    )
        if not batch.empty:
            first = batch.iloc[0]
            meta = f"{first.get('captured_date') or '-'}"
            if str(first.get("zone") or "").strip():
                meta += f" · {first.get('zone')}"
            st.caption(meta)


def prepare_excel(
    uploaded, sheet_name: str, header_row: int, fill_down: bool,
    formula_mode: str = "values", include_excel_row: bool = False,
) -> pd.DataFrame:
    return cached_full_dataframe(
        uploaded.getvalue(), uploaded.name, sheet_name, int(header_row), fill_down,
        data_only=(formula_mode == "values"), include_excel_row=include_excel_row,
    ).copy()


def dataset_selector(project_id: str, key: str, label: str = "Dataset") -> str | None:
    datasets = db.list_project_datasets(project_id)
    if datasets.empty:
        st.info("Belum ada dataset. Upload Excel pada menu Update Data terlebih dahulu.")
        return None
    by_id = datasets.set_index("id").to_dict("index")
    ids = datasets["id"].tolist()
    return st.selectbox(
        label,
        ids,
        key=key,
        format_func=lambda did: f"{by_id[did]['function_name']} · {by_id[did]['name']} ({fmt_int(by_id[did]['row_count'])} row)",
    )


# ------------------------------ Login / Sidebar ------------------------------
current_user = require_login()
access_mode = app_access_mode(current_user)
project_id = sidebar_project_selector(current_user)
logout_button(current_user)
if access_mode == "viewer":
    if str(current_user.get("role") or "").lower() == "internal":
        nav_options = ["Konsolidasi Proyek", "Dashboard Proyek", "Visual Sheet", "Profil Saya"]
    else:
        nav_options = ["Dashboard Proyek", "Visual Sheet", "Profil Saya"]
else:
    nav_options = [
        "Konsolidasi Proyek",
        "Dashboard Proyek",
        "Visual Sheet",
        "Periode Laporan",
        "Update Data",
        "Foto & BIM Update",
        "Quick Edit Data",
        "Data Explorer",
        "Master Proyek",
        "User & Akses",
        "Publish & Sync",
        "Riwayat & Database",
        "Profil Saya",
    ]
page = st.sidebar.radio("Navigasi", nav_options)
st.sidebar.markdown("---")
if access_mode == "admin":
    st.sidebar.caption(f"Database lokal: {fmt_bytes(db.database_size_bytes())}")
else:
    st.sidebar.caption("Read-only · data tidak dapat diubah")

render_flash()
active_project = render_active_project_context(project_id, access_mode, current_user)


# ------------------------------ Profile / Portfolio / Reporting ------------------------------
if page == "Profil Saya":
    st.markdown(hero("Profil Saya", "Perbarui identitas akun dan password Anda tanpa mengubah role atau akses proyek."), unsafe_allow_html=True)
    if current_user.get("id") == "__local_admin__":
        st.info("Admin Lokal memakai PIN khusus localhost. Profil user tidak berlaku untuk akun recovery lokal ini.")
        st.caption("Untuk mengganti PIN Admin Lokal, gunakan RESET_LOCAL_ADMIN_PIN.bat lalu buat PIN baru saat login.")
    else:
        fresh_user = db.get_user(str(current_user.get("id"))) or current_user
        p1, p2 = st.columns([1, 1])
        with p1:
            with st.form("self_profile_form"):
                st.text_input("Nama User", value=fresh_user.get("username") or "", disabled=True)
                own_name = st.text_input("Nama lengkap", value=fresh_user.get("full_name") or "")
                own_email = st.text_input("Email", value=fresh_user.get("email") or "")
                save_profile = st.form_submit_button("Simpan Profil", type="primary", use_container_width=True)
            if save_profile:
                try:
                    db.update_own_profile(str(fresh_user["id"]), full_name=own_name, email=own_email)
                    flash("success", "Profil berhasil diperbarui.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Profil tidak dapat disimpan: {exc}")
        with p2:
            with st.form("self_password_form"):
                current_pwd = st.text_input("Password saat ini", type="password")
                new_pwd = st.text_input("Password baru", type="password", help="Minimal 8 karakter.")
                new_pwd2 = st.text_input("Ulangi password baru", type="password")
                save_pwd = st.form_submit_button("Ganti Password", use_container_width=True)
            if save_pwd:
                if new_pwd != new_pwd2:
                    st.error("Konfirmasi password baru tidak sama.")
                else:
                    try:
                        db.change_own_password(str(fresh_user["id"]), current_pwd, new_pwd)
                        flash("success", "Password berhasil diganti.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Password tidak dapat diganti: {exc}")

elif page == "Konsolidasi Proyek":
    role = str(current_user.get("role") or "").lower()
    if role not in {"admin", "internal"}:
        st.error("Konsolidasi Proyek hanya tersedia untuk Admin dan Internal HDK.")
        st.stop()
    st.markdown(hero("Konsolidasi Proyek", "Ringkasan seluruh proyek yang dapat Anda akses dalam satu tampilan manajemen."), unsafe_allow_html=True)
    portfolio_projects = db.list_projects_for_user(current_user)
    portfolio = reporting.portfolio_frame(portfolio_projects)
    if portfolio.empty:
        st.info("Belum ada proyek.")
    else:
        today_ts = pd.Timestamp(date.today())
        finish_ts = pd.to_datetime(portfolio["Effective Finish"], errors="coerce")
        active_count = int(portfolio["Status"].astype(str).str.lower().eq("aktif").sum())
        due_90 = int(((finish_ts >= today_ts) & (finish_ts <= today_ts + pd.Timedelta(days=90))).sum())
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total Proyek", len(portfolio))
        k2.metric("Aktif", active_count)
        k3.metric("Finish ≤ 90 hari", due_90)
        k4.metric("Total Foto / Visual", int(pd.to_numeric(portfolio["Foto"], errors="coerce").fillna(0).sum()))
        status_filter = st.multiselect(
            "Filter status",
            sorted(portfolio["Status"].dropna().astype(str).unique().tolist()),
            default=[],
            key="portfolio_status_filter",
        )
        view = portfolio[portfolio["Status"].isin(status_filter)].copy() if status_filter else portfolio
        st.dataframe(view, use_container_width=True, hide_index=True, height=520)
        st.caption("Effective Finish = Revised Finish bila tersedia; jika tidak, memakai Finish Contract.")

elif page == "Periode Laporan":
    if access_mode != "admin":
        st.error("Periode Laporan hanya dapat dikelola oleh Admin HDK.")
        st.stop()
    if not project_id:
        st.info("Pilih proyek terlebih dahulu.")
        st.stop()
    project = db.get_project(project_id)
    st.markdown(hero("Periode Laporan", "Buat Draft, terbitkan Published, dan pertahankan histori revisi laporan proyek."), unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown("### Buat Draft / Revisi")
        with st.form("create_reporting_period"):
            r1, r2, r3 = st.columns(3)
            with r1:
                report_label = st.text_input("Nama periode *", value=date.today().strftime("%B %Y"))
            with r2:
                report_start = st.date_input("Awal periode", value=None, format="DD/MM/YYYY")
            with r3:
                report_end = st.date_input("Akhir periode", value=date.today(), format="DD/MM/YYYY")
            data_as_of = st.date_input("Data per tanggal *", value=date.today(), format="DD/MM/YYYY")
            report_note = st.text_area("Catatan revisi", placeholder="Contoh: Update progress minggu ke-38 dan status engineering.")
            create_report = st.form_submit_button("Simpan sebagai Draft", type="primary", use_container_width=True)
        if create_report:
            try:
                db.create_reporting_period(
                    project_id, report_label, data_as_of.isoformat(),
                    period_start=_date_iso(report_start), period_end=_date_iso(report_end),
                    note=report_note, created_by=str(current_user.get("username") or current_user.get("id") or ""),
                )
                flash("success", "Draft periode laporan tersimpan. Jika nama periode sama, sistem otomatis membuat revisi berikutnya.")
                st.rerun()
            except Exception as exc:
                st.error(f"Draft tidak dapat disimpan: {exc}")

    reports = db.list_reporting_periods(project_id)
    if reports.empty:
        st.info("Belum ada periode laporan.")
    else:
        st.markdown("### Histori Revisi")
        show = reports[["id","period_label","revision_no","data_as_of","status","note","created_at","published_at"]].copy()
        show["status"] = show["status"].astype(str).str.upper()
        show.columns = ["ID","Periode","Rev","Data per tanggal","Status","Catatan","Dibuat","Dipublikasikan"]
        st.dataframe(show, use_container_width=True, hide_index=True)
        report_ids = reports["id"].tolist()
        report_map = reports.set_index("id").to_dict("index")
        selected_report_id = st.selectbox(
            "Pilih revisi",
            report_ids,
            format_func=lambda rid: f"{report_map[rid]['period_label']} · Rev {report_map[rid]['revision_no']} · {str(report_map[rid]['status']).upper()}",
            key="reporting_revision_select",
        )
        selected_report = report_map[selected_report_id]
        a1, a2, a3 = st.columns(3)
        with a1:
            if st.button(
                "Publish Revisi Ini",
                type="primary",
                use_container_width=True,
                disabled=str(selected_report.get("status")) == "published",
                key="publish_reporting_revision",
            ):
                try:
                    db.publish_reporting_period(selected_report_id, str(current_user.get("username") or "local-admin"))
                    flash("success", "Revisi laporan resmi berhasil dipublikasikan.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Revisi tidak dapat dipublikasikan: {exc}")
        datasets_now = db.list_project_datasets(project_id)
        photo_count_now = db.photo_count(project_id)
        pdf_bytes = reporting.build_project_report_pdf(
            project, selected_report, dataset_count=len(datasets_now), photo_count=photo_count_now,
        )
        safe_code = re.sub(r"[^A-Za-z0-9_-]+", "_", str(project.get("code") or "project"))
        safe_period = re.sub(r"[^A-Za-z0-9_-]+", "_", str(selected_report.get("period_label") or "report"))
        with a2:
            st.download_button(
                "Download PDF",
                data=pdf_bytes,
                file_name=f"{safe_code}_{safe_period}_Rev{selected_report.get('revision_no',0)}.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        with a3:
            subject = f"HDK Project Report - {project.get('code','')} - {selected_report.get('period_label','')}"
            body = (
                f"Project: {project.get('code','')} - {project.get('name','')}\n"
                f"Periode: {selected_report.get('period_label','')} Rev {selected_report.get('revision_no',0)}\n"
                f"Data per tanggal: {selected_report.get('data_as_of','')}\n\n"
                "PDF dapat diunduh dari HDK Project Data Hub lalu dilampirkan pada email ini."
            )
            mailto = f"mailto:?subject={quote(subject)}&body={quote(body)}"
            st.link_button("Buat Email Laporan", mailto, use_container_width=True)
        st.caption("Published menandai revisi resmi untuk periode tersebut. Revisi lama tetap tersimpan sebagai histori dan tidak dihapus.")


# ------------------------------ User & Project Access ------------------------------
elif page == "User & Akses":
    if access_mode != "admin":
        st.error("User & Akses hanya tersedia untuk Admin HDK.")
        st.stop()
    st.markdown(hero("User & Project Access", "Atur siapa yang dapat melihat proyek dan siapa yang dapat melakukan update."), unsafe_allow_html=True)
    st.info("Admin Lokal memiliki akses penuh ke semua proyek. Akun Admin tidak dibuat/dipakai melalui login umum. Internal HDK dapat melihat semua proyek. Owner dan Konsultan hanya dapat melihat proyek yang ditugaskan.")

    all_projects = db.list_projects()
    project_ids_all = all_projects["id"].tolist() if not all_projects.empty else []
    project_labels_all = {r["id"]: f"{r['code']} · {r['name']}" for _, r in all_projects.iterrows()} if not all_projects.empty else {}
    role_options = ["internal", "owner", "consultant"]

    c_new, c_list = st.columns([1, 1.35])
    with c_new:
        with st.container(border=True):
            st.markdown("### Tambah User")
            with st.form("create_rbac_user", clear_on_submit=True):
                u_username = st.text_input("Nama User / Username *", placeholder="contoh: budi.owner", help="Dipakai untuk login. Huruf kecil/angka serta . _ - diperbolehkan.")
                u_name = st.text_input("Nama lengkap")
                u_email = st.text_input("Email *", help="Untuk identitas/kontak. Login utama menggunakan Nama User.")
                u_role = st.selectbox("Role *", role_options, format_func=lambda x: ROLE_LABELS[x])
                u_password = st.text_input("Password awal *", type="password", help="Minimal 8 karakter. User dapat meminta Admin mereset password bila diperlukan.")
                if u_role in {"owner", "consultant"}:
                    u_projects = st.multiselect("Akses proyek", project_ids_all, format_func=lambda x: project_labels_all.get(x, x))
                else:
                    u_projects = []
                    st.caption("Role ini otomatis mendapat akses ke semua proyek.")
                u_active = st.checkbox("Aktif", value=True)
                create_user_btn = st.form_submit_button("Buat User", type="primary", use_container_width=True)
            if create_user_btn:
                try:
                    if u_role in {"owner", "consultant"} and not u_projects:
                        st.warning("User dibuat tanpa akses proyek. Anda dapat menambahkan proyek dari panel Edit User.")
                    db.create_user(u_username, u_email, u_name, u_role, u_password, u_projects, u_active)
                    flash("success", f"User {u_username} berhasil dibuat.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Gagal membuat user: {exc}")

    with c_list:
        st.markdown("### Daftar User")
        users_df = db.list_users()
        if users_df.empty:
            st.info("Belum ada user.")
        else:
            show_users = users_df.copy()
            show_users["role"] = show_users["role"].map(ROLE_LABELS).fillna(show_users["role"])
            show_users["active"] = show_users["active"].map({1: "Aktif", 0: "Nonaktif"})
            show_users = show_users.rename(columns={"username":"Nama User","email":"Email","full_name":"Nama Lengkap","role":"Role","active":"Status","project_count":"Jumlah Proyek"})
            st.dataframe(show_users[[c for c in ["Nama User","Nama Lengkap","Email","Role","Status","Jumlah Proyek"] if c in show_users.columns]], use_container_width=True, hide_index=True)

    users_df = db.list_users()
    if not users_df.empty:
        st.markdown("---")
        st.markdown("### Edit User & Akses Proyek")
        user_ids = users_df["id"].tolist()
        user_map = users_df.set_index("id").to_dict("index")
        selected_uid = st.selectbox(
            "Pilih user",
            user_ids,
            format_func=lambda uid: f"{user_map[uid].get('username')} · {user_map[uid].get('full_name') or 'Tanpa nama'} · {ROLE_LABELS.get(user_map[uid].get('role'), user_map[uid].get('role'))}",
            key="rbac_selected_user",
        )
        selected_user = user_map[selected_uid]
        existing_projects = db.user_project_ids(selected_uid)
        e1, e2 = st.columns([1, 1.25])
        with e1:
            e_username = st.text_input("Nama User / Username", value=selected_user.get("username") or "", key=f"rbac_username_{selected_uid}")
            e_name = st.text_input("Nama lengkap", value=selected_user.get("full_name") or "", key=f"rbac_name_{selected_uid}")
            e_email = st.text_input("Email", value=selected_user.get("email") or "", key=f"rbac_email_{selected_uid}")
            current_role = selected_user.get("role") or "owner"
            if current_role == "admin":
                st.text_input("Role", value="Admin HDK · terkunci", disabled=True, key=f"rbac_role_locked_{selected_uid}")
                e_role = "admin"
            else:
                e_role = st.selectbox("Role", role_options, index=role_options.index(current_role) if current_role in role_options else 1,
                                      format_func=lambda x: ROLE_LABELS[x], key=f"rbac_role_{selected_uid}")
            e_active = st.checkbox("User aktif", value=bool(selected_user.get("active")), key=f"rbac_active_{selected_uid}", disabled=(current_role == "admin"))
            reset_password = st.text_input("Password baru (opsional)", type="password", key=f"rbac_pwd_{selected_uid}", help="Kosongkan jika tidak ingin mengganti password.")
        with e2:
            if e_role in {"owner", "consultant"}:
                e_projects = st.multiselect("Proyek yang dapat dilihat", project_ids_all, default=[x for x in existing_projects if x in project_ids_all],
                                            format_func=lambda x: project_labels_all.get(x, x), key=f"rbac_projects_{selected_uid}")
                st.caption("User hanya dapat melihat proyek yang dipilih di atas. Mengubah URL ke proyek lain tetap akan ditolak.")
            else:
                e_projects = []
                st.success("Role ini otomatis dapat melihat semua proyek.")
        if current_role == "admin":
            st.warning("Akun Admin database lama dikunci dan tidak digunakan untuk login umum. Admin lokal menggunakan PIN khusus localhost.")
        save_disabled = current_role == "admin"
        if st.button("Simpan User & Akses", type="primary", use_container_width=True, disabled=save_disabled, key=f"save_rbac_{selected_uid}"):
            try:
                db.update_user(selected_uid, username=e_username, email=e_email, full_name=e_name, role=e_role, active=e_active,
                               password=reset_password or None, project_ids=e_projects)
                flash("success", "User dan akses proyek berhasil diperbarui.")
                st.rerun()
            except Exception as exc:
                st.error(f"Gagal memperbarui user: {exc}")


# ------------------------------ Publish & Sync ------------------------------
elif page == "Publish & Sync":
    if access_mode != "admin":
        st.error("Publish & Sync hanya tersedia untuk Admin HDK pada workspace lokal.")
        st.stop()
    if not project_id:
        st.info("Pilih atau buat proyek terlebih dahulu.")
        st.stop()

    st.markdown(hero(
        "Publikasikan ke Web",
        "Data kerja tetap di lokal. Saat sudah siap, publikasikan versi resmi proyek ke web dengan satu langkah."
    ), unsafe_allow_html=True)

    project = db.get_project(project_id)
    source_db = Path(db.DB_PATH)
    publish_root = publish.load_publish_root(DATA_DIR)

    # Apply deferred widget value before the widget is instantiated.
    pending_publish_root = st.session_state.pop("publish_root_pending", None)
    if pending_publish_root is not None:
        st.session_state["publish_root_path"] = str(pending_publish_root)
    elif "publish_root_path" not in st.session_state:
        st.session_state["publish_root_path"] = str(publish_root)

    # Folder configuration is intentionally kept out of the daily workflow.
    with st.expander("Pengaturan lokasi Published (opsional)", expanded=False):
        st.caption("Biarkan default untuk pemakaian lokal. Jika memakai Google Drive for Desktop, arahkan ke folder Drive di komputer ini.")
        configured_root = st.text_input(
            "Lokasi folder Published",
            help="Contoh Windows: G:\\My Drive\\HDK Project Data Hub\\Published",
            key="publish_root_path",
        )
        root_health = publish.publish_root_health(DATA_DIR, configured_root)
        if root_health.get("ok"):
            st.success("Lokasi Published siap digunakan.")
        else:
            st.error("Lokasi Published belum siap. Gunakan folder lokal atau pilih folder yang tersedia.")
        c_save, c_default = st.columns(2)
        with c_save:
            if st.button("Simpan Lokasi", use_container_width=True, key="save_publish_root"):
                try:
                    publish_root = publish.save_publish_root(DATA_DIR, configured_root)
                    flash("success", "Lokasi Published disimpan.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Lokasi tidak dapat digunakan: {exc}")
        with c_default:
            if st.button("Gunakan Folder Lokal", use_container_width=True, key="use_default_publish_root"):
                try:
                    default_root = DATA_DIR / "web_published"
                    saved_root = publish.save_publish_root(DATA_DIR, default_root)
                    st.session_state["publish_root_pending"] = str(saved_root)
                    flash("success", "Folder Published dikembalikan ke lokasi lokal.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Folder lokal tidak dapat digunakan: {exc}")
    configured_root = st.session_state.get("publish_root_path", str(publish_root))
    root_health = publish.publish_root_health(DATA_DIR, configured_root)
    publish_root = publish.normalize_publish_root(DATA_DIR, configured_root)
    publish_root_ok = bool(root_health.get("ok"))

    # Best-effort recovery once per project/session. This is intentionally silent;
    # Admin should not have to understand path migrations between app versions.
    asset_health = publish.project_asset_health(source_db, DATA_DIR, project_id)
    repair_key = f"auto_asset_repair_done_{project_id}"
    if (not asset_health.get("ok")) and not st.session_state.get(repair_key):
        st.session_state[repair_key] = True
        try:
            storage.migrate_legacy_layout(Path(db.DB_PATH), DATA_DIR)
            _migrate_project_storage_once.clear()
            asset_health = publish.project_asset_health(source_db, DATA_DIR, project_id)
        except Exception:
            pass

    try:
        status = publish.compare_with_current(source_db, DATA_DIR, project_id, publish_root)
    except Exception as exc:
        st.error(f"Status publikasi tidak dapat dibaca: {exc}")
        st.stop()

    local_summary = status["summary"]
    current_manifest = status.get("current_manifest") or {}
    current_summary = current_manifest.get("summary") or {}
    published_at = current_manifest.get("generated_at")

    # Friendly state for day-to-day use. Technical package state stays hidden below.
    local_complete = bool(asset_health.get("ok"))
    if not status["is_published"]:
        friendly_status = "BELUM DIPUBLIKASIKAN"
        friendly_help = "Proyek ini belum memiliki versi web."
    elif status["is_up_to_date"] and local_complete:
        friendly_status = "SUDAH TERPUBLIKASI"
        friendly_help = "Versi web sudah sama dengan data lokal terbaru."
    else:
        friendly_status = "PERLU DIPERBARUI"
        friendly_help = "Ada perubahan lokal atau paket web lama perlu diperbarui."

    st.markdown("### 1. Status Proyek")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Status Web", friendly_status)
    with c2:
        st.metric("Dataset", int(local_summary.get("datasets") or 0))
    with c3:
        st.metric("Foto / Visual", int(local_summary.get("photos") or 0))
    with c4:
        st.metric("Publish Terakhir", str(published_at or "Belum pernah").replace("T", " ")[:16])
    st.caption(f"{project.get('code')} · {project.get('name')} · {friendly_help}")

    if local_complete:
        st.success("Data proyek siap dipublikasikan.")
    else:
        missing_assets = asset_health.get("missing") or []
        counts = {"excel": 0, "visual": 0, "logo": 0, "other": 0}
        for item in missing_assets:
            label = str(item.get("label") or "")
            if label.startswith("photos/"):
                counts["visual"] += 1
            elif label.startswith("source_files/") or label.startswith("source_versions/"):
                counts["excel"] += 1
            elif label.startswith("Project/"):
                counts["logo"] += 1
            else:
                counts["other"] += 1
        parts = []
        if counts["excel"]:
            parts.append(f"{counts['excel']} file Excel")
        if counts["visual"]:
            parts.append(f"{counts['visual']} foto/BIM")
        if counts["logo"]:
            parts.append(f"{counts['logo']} logo")
        if counts["other"]:
            parts.append(f"{counts['other']} file lain")
        detail = ", ".join(parts) if parts else f"{asset_health.get('missing_count', 0)} file"
        st.warning(
            f"Ada {detail} yang belum ditemukan. Tidak perlu mengurus path atau histori teknis. "
            "Lengkapi file tersebut pada menu Update Data / Foto & BIM Update / Master Proyek, lalu kembali ke halaman ini."
        )
        if st.button("Coba Pulihkan Otomatis", use_container_width=False, key="repair_missing_project_assets_simple"):
            try:
                repair = storage.migrate_legacy_layout(Path(db.DB_PATH), DATA_DIR)
                _migrate_project_storage_once.clear()
                st.session_state.pop(repair_key, None)
                recovered = int(repair.get("copied_from_old_build", 0)) + int(repair.get("moved", 0))
                flash("success", f"Pemulihan selesai. {recovered} file berhasil dipulihkan/dipindahkan.")
                st.rerun()
            except Exception as exc:
                st.error(f"Pemulihan otomatis belum berhasil: {exc}")

    st.markdown("### 2. Publikasikan")
    with st.container(border=True):
        review_ok = st.checkbox(
            "Dashboard proyek sudah saya cek dan boleh ditampilkan sebagai versi resmi di web.",
            value=False,
            key="publish_review_confirm_simple",
        )
        no_update_needed = bool(status["is_up_to_date"] and local_complete)
        publish_disabled = (not review_ok) or no_update_needed or (not publish_root_ok) or (not local_complete)
        if st.button(
            "PUBLIKASIKAN KE WEB",
            type="primary",
            use_container_width=True,
            disabled=publish_disabled,
            key="publish_project_now_simple",
        ):
            try:
                result = publish.publish_project(source_db, DATA_DIR, project_id, publish_root)
                flash("success", f"Proyek berhasil dipublikasikan ke web · {result['project_code']}")
                st.rerun()
            except Exception as exc:
                st.error(f"Publikasi gagal: {exc}")
        if no_update_needed:
            st.caption("Tidak ada perubahan baru. Versi web sudah terbaru.")
        elif not local_complete:
            st.caption("Tombol akan aktif setelah file proyek lengkap.")
        elif not publish_root_ok:
            st.caption("Lokasi Published belum siap. Buka Pengaturan lokasi Published di atas.")
        else:
            st.caption("Setiap publish membuat versi baru; data kerja lokal tidak berubah.")

    # Current web status is kept simple. Old incomplete packages are history, not a daily-user problem.
    current_manifest = publish.current_manifest(publish_root, project_id, str(project.get("code") or "")) or current_manifest
    if current_manifest:
        versions = publish.list_project_versions(publish_root, project_id, str(project.get("code") or ""))
        current_ver = current_manifest.get("version")
        current_entry = next((v for v in versions if v.get("version") == current_ver), None)
        st.markdown("### 3. Versi Web")
        with st.container(border=True):
            if current_entry:
                current_missing = current_entry.get("missing_files") or []
                if current_missing and local_complete:
                    st.info("Versi web lama belum lengkap. Klik PUBLIKASIKAN KE WEB di atas untuk menggantinya dengan paket baru yang lengkap.")
                elif current_missing:
                    st.warning("Versi web lama belum lengkap dan beberapa file lokal masih perlu dilengkapi.")
                else:
                    st.success("Versi web aktif dalam kondisi lengkap.")
                st.caption(f"Publish terakhir: {str(current_entry.get('generated_at') or '—').replace('T', ' ')[:19]}")

        with st.expander("Riwayat & pengaturan teknis", expanded=False):
            st.caption("Bagian ini hanya untuk backup/rollback. Tidak perlu digunakan untuk update normal.")
            if current_entry:
                zpath = Path(str(current_entry.get("zip_path") or ""))
                d1, d2 = st.columns(2)
                with d1:
                    st.markdown(f"**Versi aktif:** `{current_ver}`")
                with d2:
                    if zpath.exists():
                        st.download_button(
                            "Download Backup Published",
                            data=zpath.read_bytes(),
                            file_name=zpath.name,
                            mime="application/zip",
                            use_container_width=True,
                            key=f"download_published_{current_ver}",
                        )
            if len(versions) > 1:
                version_ids = [v.get("version") for v in versions if v.get("version")]
                rb = st.selectbox(
                    "Rollback ke versi",
                    version_ids,
                    index=version_ids.index(current_ver) if current_ver in version_ids else 0,
                    format_func=lambda vid: next((f"{v.get('generated_at', vid)}" for v in versions if v.get('version') == vid), str(vid)),
                    key="rollback_publish_version_simple",
                )
                if rb != current_ver:
                    confirm_rb = st.checkbox("Saya yakin ingin mengaktifkan versi lama ini.", key="confirm_publish_rollback_simple")
                    if st.button("Aktifkan Versi Lama", disabled=not confirm_rb, use_container_width=True, key="activate_publish_version_simple"):
                        try:
                            publish.set_current_version(publish_root, project_id, str(project.get("code") or ""), str(rb))
                            flash("success", "Versi web berhasil dikembalikan.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Rollback gagal: {exc}")
            st.caption(f"Folder Published: {publish_root}")

# ------------------------------ Master Project ------------------------------
elif page == "Master Proyek":
    if access_mode != "admin":
        st.error("Master Proyek hanya tersedia pada Admin Mode.")
        st.stop()
    st.markdown(hero("Master Proyek", "Identitas proyek, kontrak, 4 logo resmi, BIMx, dan pengaturan publikasi. Internal Project ID tetap dikunci."), unsafe_allow_html=True)
    projects = db.list_projects()
    left, right = st.columns([1, 1.25])
    with left:
        st.markdown("#### Buat proyek")
        with st.form("create_project", clear_on_submit=True):
            code = st.text_input("Kode proyek *", placeholder="RSIA-MDN")
            name = st.text_input("Nama proyek *")
            owner_name = st.text_input("Owner / Client")
            planner_name = st.text_input("Konsultan Perencana")
            consultant_name = st.text_input("Konsultan Pengawas")
            contractor_name = st.text_input("Kontraktor", value="PT Harkat Digdaya Konstruksi")
            location = st.text_input("Lokasi")
            c1,c2=st.columns(2)
            with c1:
                contract_no = st.text_input("No. kontrak")
                contract_start = st.date_input("Start construction", value=None, format="DD/MM/YYYY")
            with c2:
                contract_finish = st.date_input("Finish contract", value=None, format="DD/MM/YYYY")
                revised_finish = st.date_input("Revised finish", value=None, format="DD/MM/YYYY", help="Kosongkan jika belum ada revisi tanggal selesai.")
            cv1, cv2 = st.columns([1.55, 1])
            with cv1:
                contract_value_text = st.text_input("Nilai kontrak", placeholder="Contoh: 125.000.000.000", help="Ketik nilai kontrak dalam Rupiah. Pemisah titik/koma boleh digunakan.")
            with cv2:
                contract_vat_status = st.selectbox("PPN", ["Belum termasuk PPN", "Termasuk PPN"], index=0)
            st.markdown("##### Logo resmi proyek")
            st.caption("Logo otomatis di-crop, dipertahankan rasionya, lalu dinormalisasi ke canvas standar agar ukuran visual terlihat seragam.")
            l1,l2,l3,l4=st.columns(4)
            with l1: logo_owner = st.file_uploader("Logo Owner", type=["png","jpg","jpeg","webp"], key="create_logo_owner")
            with l2: logo_planner = st.file_uploader("Logo Konsultan Perencana", type=["png","jpg","jpeg","webp"], key="create_logo_planner")
            with l3: logo_consultant = st.file_uploader("Logo Konsultan Pengawas", type=["png","jpg","jpeg","webp"], key="create_logo_consultant")
            with l4: logo_contractor = st.file_uploader("Logo Kontraktor", type=["png","jpg","jpeg","webp"], key="create_logo_contractor")
            bimx = st.text_input("Link BIMx", placeholder="https://...")
            desc = st.text_area("Deskripsi")
            submit = st.form_submit_button("Buat proyek", use_container_width=True)
        if submit:
            if not code.strip() or not name.strip():
                st.error("Kode dan nama proyek wajib diisi.")
            else:
                try:
                    contract_value = _parse_rupiah(contract_value_text)
                    new_id = db.create_project(
                        code, name, owner_name, location, desc, bimx,
                        contract_no=contract_no, contract_start=_date_iso(contract_start),
                        contract_finish=_date_iso(contract_finish), revised_finish=_date_iso(revised_finish),
                        contract_value=contract_value, contract_vat_status=contract_vat_status,
                        contractor_name=contractor_name, consultant_planner_name=planner_name, consultant_name=consultant_name,
                    )
                    logo_updates={}
                    if logo_owner is not None: logo_updates["logo_owner_path"] = save_project_logo(new_id, logo_owner, "owner")
                    if logo_planner is not None: logo_updates["logo_consultant_planner_path"] = save_project_logo(new_id, logo_planner, "konsultan_perencana")
                    if logo_consultant is not None: logo_updates["logo_consultant_path"] = save_project_logo(new_id, logo_consultant, "konsultan_pengawas")
                    if logo_contractor is not None: logo_updates["logo_contractor_path"] = save_project_logo(new_id, logo_contractor, "kontraktor")
                    if logo_updates: db.update_project(new_id, **logo_updates)
                    st.session_state["project_id"] = new_id
                    flash("success", "Proyek berhasil dibuat. Logo yang diunggah sudah dinormalisasi otomatis.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Gagal membuat proyek: {exc}")
    with right:
        st.markdown("#### Daftar proyek")
        if projects.empty:
            st.info("Belum ada proyek.")
        else:
            cols=[c for c in ["code","name","client","consultant_planner_name","consultant_name","contractor_name","location","function_count","photo_count","updated_at"] if c in projects.columns]
            show = projects[cols].copy()
            rename={"code":"Kode","name":"Nama","client":"Owner","consultant_planner_name":"Konsultan Perencana","consultant_name":"Konsultan Pengawas","contractor_name":"Kontraktor","location":"Lokasi","function_count":"Fungsi","photo_count":"Foto","updated_at":"Update"}
            st.dataframe(show.rename(columns=rename), use_container_width=True, hide_index=True)

    if project_id:
        st.markdown("---")
        p = db.get_project(project_id)
        st.markdown(f"#### Edit Master Proyek: {p.get('code') or '—'} · {p.get('name') or '—'}")
        st.info(f"Semua perubahan pada form di bawah hanya berlaku untuk proyek aktif **{p.get('code') or '—'} · {p.get('name') or '—'}**.")
        st.caption(f"Internal Project ID: `{project_id}` · ID ini dikunci agar relasi Excel, foto, snapshot, logo, dan BIMx tidak terputus.")
        with st.form("edit_project"):
            t1,t2,t3,t4 = st.tabs(["Identitas", "Kontrak", "Logo Proyek", "BIMx & Publikasi"])
            with t1:
                c1, c2 = st.columns(2)
                with c1:
                    e_code = st.text_input("Kode", value=p.get("code") or "")
                    e_name = st.text_input("Nama", value=p.get("name") or "")
                    e_owner = st.text_input("Owner / Client", value=p.get("client") or "")
                    e_planner = st.text_input("Konsultan Perencana", value=p.get("consultant_planner_name") or "")
                with c2:
                    e_location = st.text_input("Lokasi", value=p.get("location") or "")
                    e_consultant=st.text_input("Konsultan Pengawas", value=p.get("consultant_name") or "")
                    e_contractor=st.text_input("Kontraktor", value=p.get("contractor_name") or "")
                e_desc = st.text_area("Deskripsi", value=p.get("description") or "")
                current_project_status = p.get("project_status") or "Aktif"
                e_project_status = st.selectbox(
                    "Status Proyek",
                    reporting.STATUS_OPTIONS,
                    index=reporting.STATUS_OPTIONS.index(current_project_status) if current_project_status in reporting.STATUS_OPTIONS else 0,
                )
            with t2:
                c1,c2=st.columns(2)
                with c1:
                    e_contract_no = st.text_input("No. kontrak", value=p.get("contract_no") or "")
                    e_start=st.date_input("Start construction", value=_date_value(p.get("contract_start")), format="DD/MM/YYYY")
                with c2:
                    e_finish=st.date_input("Finish contract", value=_date_value(p.get("contract_finish")), format="DD/MM/YYYY")
                    e_revised=st.date_input("Revised finish", value=_date_value(p.get("revised_finish")), format="DD/MM/YYYY", help="Kosongkan jika belum ada revisi tanggal selesai.")
                cv1, cv2 = st.columns([1.55, 1])
                with cv1:
                    e_value_text = st.text_input("Nilai kontrak", value=_rupiah_input_value(p.get("contract_value")), placeholder="Contoh: 125.000.000.000")
                with cv2:
                    vat_options = ["Belum termasuk PPN", "Termasuk PPN"]
                    current_vat = p.get("contract_vat_status") or "Belum termasuk PPN"
                    e_vat = st.selectbox("PPN", vat_options, index=vat_options.index(current_vat) if current_vat in vat_options else 0)
            with t3:
                st.caption("Preview memakai normalisasi otomatis yang sama dengan Executive Dashboard. Upload baru akan menjadi logo aktif.")
                l1,l2,l3,l4=st.columns(4)
                with l1:
                    st.markdown("**Owner**")
                    owner_logo_path = storage.resolve_stored_path(p.get("logo_owner_path") or "", DATA_DIR)
                    if p.get("logo_owner_path") and owner_logo_path.exists(): st.image(_logo_data_uri(str(owner_logo_path), owner_logo_path.stat().st_mtime_ns), use_container_width=True)
                    logo_owner_new=st.file_uploader("Ganti Logo Owner", type=["png","jpg","jpeg","webp"], key=f"edit_owner_{project_id}")
                    clear_owner=st.checkbox("Hapus logo Owner", key=f"clear_owner_{project_id}")
                with l2:
                    st.markdown("**Konsultan Perencana**")
                    planner_logo_path = storage.resolve_stored_path(p.get("logo_consultant_planner_path") or "", DATA_DIR)
                    if p.get("logo_consultant_planner_path") and planner_logo_path.exists(): st.image(_logo_data_uri(str(planner_logo_path), planner_logo_path.stat().st_mtime_ns), use_container_width=True)
                    logo_planner_new=st.file_uploader("Ganti Logo Konsultan Perencana", type=["png","jpg","jpeg","webp"], key=f"edit_planner_{project_id}")
                    clear_planner=st.checkbox("Hapus logo Konsultan Perencana", key=f"clear_planner_{project_id}")
                with l3:
                    st.markdown("**Konsultan Pengawas**")
                    consultant_logo_path = storage.resolve_stored_path(p.get("logo_consultant_path") or "", DATA_DIR)
                    if p.get("logo_consultant_path") and consultant_logo_path.exists(): st.image(_logo_data_uri(str(consultant_logo_path), consultant_logo_path.stat().st_mtime_ns), use_container_width=True)
                    logo_consultant_new=st.file_uploader("Ganti Logo Konsultan Pengawas", type=["png","jpg","jpeg","webp"], key=f"edit_consultant_{project_id}")
                    clear_consultant=st.checkbox("Hapus logo Konsultan Pengawas", key=f"clear_consultant_{project_id}")
                with l4:
                    st.markdown("**Kontraktor**")
                    contractor_logo_path = storage.resolve_stored_path(p.get("logo_contractor_path") or "", DATA_DIR)
                    if p.get("logo_contractor_path") and contractor_logo_path.exists(): st.image(_logo_data_uri(str(contractor_logo_path), contractor_logo_path.stat().st_mtime_ns), use_container_width=True)
                    logo_contractor_new=st.file_uploader("Ganti Logo Kontraktor", type=["png","jpg","jpeg","webp"], key=f"edit_contractor_{project_id}")
                    clear_contractor=st.checkbox("Hapus logo Kontraktor", key=f"clear_contractor_{project_id}")
            with t4:
                e_bimx = st.text_input("Link BIMx", value=p.get("bimx_url") or "")
                st.text_input("Internal Project ID", value=project_id, disabled=True)
                public_base = os.getenv("HDK_PUBLIC_BASE_URL", "").strip().rstrip("/")
                public_link = f"{public_base}?view=public&project={project_id}" if public_base else f"?view=public&project={project_id}"
                st.caption("Public Viewer hanya menampilkan Dashboard Proyek dan Visual Sheet. Menu upload/edit/CRUD tidak tersedia tanpa Admin Login.")
                st.code(public_link, language=None)
            if st.form_submit_button("Simpan perubahan", use_container_width=True):
                logo_updates={}
                try:
                    if clear_owner: logo_updates["logo_owner_path"] = ""
                    elif logo_owner_new is not None: logo_updates["logo_owner_path"] = save_project_logo(project_id, logo_owner_new, "owner")
                    if clear_planner: logo_updates["logo_consultant_planner_path"] = ""
                    elif logo_planner_new is not None: logo_updates["logo_consultant_planner_path"] = save_project_logo(project_id, logo_planner_new, "konsultan_perencana")
                    if clear_consultant: logo_updates["logo_consultant_path"] = ""
                    elif logo_consultant_new is not None: logo_updates["logo_consultant_path"] = save_project_logo(project_id, logo_consultant_new, "konsultan_pengawas")
                    if clear_contractor: logo_updates["logo_contractor_path"] = ""
                    elif logo_contractor_new is not None: logo_updates["logo_contractor_path"] = save_project_logo(project_id, logo_contractor_new, "kontraktor")
                    e_value = _parse_rupiah(e_value_text)
                    db.update_project(
                        project_id, code=e_code, name=e_name, client=e_owner, location=e_location,
                        bimx_url=e_bimx, description=e_desc, contract_no=e_contract_no,
                        contract_start=_date_iso(e_start), contract_finish=_date_iso(e_finish), revised_finish=_date_iso(e_revised),
                        contract_value=e_value, contract_vat_status=e_vat, contractor_name=e_contractor, consultant_planner_name=e_planner, consultant_name=e_consultant,
                        project_status=e_project_status, **logo_updates,
                    )
                    flash("success", "Master proyek dan logo diperbarui tanpa mengubah Internal Project ID.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Gagal menyimpan Master Proyek: {exc}")


        with st.expander("Penyimpanan & backup proyek", expanded=False):
            paths = _project_storage_paths(project_id)
            st.caption("File fisik setiap proyek dipisahkan ke folder proyek masing-masing. Database SQLite tetap satu sebagai indeks pusat untuk master proyek, user, akses, dan register.")
            sc1, sc2, sc3, sc4, sc5 = st.columns(5)
            sc1.metric("Database Pusat", fmt_bytes(db.database_size_bytes()))
            sc2.metric("Arsip Excel", fmt_bytes(_folder_size(paths["excel"])))
            sc3.metric("Foto", fmt_bytes(_folder_size(paths["photos"])))
            sc4.metric("BIM Visual", fmt_bytes(_folder_size(paths["bim"])))
            sc5.metric("Logo / Asset", fmt_bytes(_folder_size(paths["assets"])))
            st.markdown("**Folder proyek aktif**")
            st.code(str(paths["root"]), language=None)
            st.caption("Struktur WIP: source/excel · photos · bim · assets · snapshots. Paket web dipisahkan di folder web_published. File proyek lain tidak disimpan di folder ini.")
            if _STORAGE_MIGRATION and _STORAGE_MIGRATION.get("moved"):
                st.success(f"Migrasi struktur lama selesai: {_STORAGE_MIGRATION.get('moved', 0)} file dipindahkan ke folder proyek masing-masing.")
            st.caption("Saat dipindah ke web/server, folder `data` harus berada pada persistent disk/volume. Jangan memakai storage ephemeral untuk produksi.")



# ------------------------------ Dashboard ------------------------------
elif page == "Dashboard Proyek":
    if not project_id:
        st.info("Buat proyek terlebih dahulu di menu Master Proyek.")
        st.stop()
    p = render_project_header(project_id, "")

    update_dates = db.list_project_update_dates(project_id)
    asof_options = ["latest"] + update_dates
    d1, d2 = st.columns([1.25, 2.75])
    with d1:
        selected_asof = st.selectbox(
            "Data per tanggal",
            asof_options,
            key=f"dashboard_asof_{project_id}",
            format_func=lambda x: "Terbaru" if x == "latest" else pd.to_datetime(x).strftime("%d %b %Y"),
            help="Setiap fungsi memakai versi terakhir yang tersedia pada atau sebelum tanggal yang dipilih.",
        )
    as_of_date = None if selected_asof == "latest" else selected_asof
    with d2:
        label = "Terbaru" if as_of_date is None else pd.to_datetime(as_of_date).strftime('%d %b %Y')
        st.markdown(f"<div style='padding-top:2.05rem;color:#9ca3af;font-size:.9rem'>Kondisi data: <b>{label}</b></div>", unsafe_allow_html=True)

    with st.spinner("Menyiapkan dashboard proyek..."):
        payloads, payloads_by_file = collect_project_visuals(project_id, as_of_date)
    summary = executive_summary(payloads)

    if payloads:
        render_executive(summary, key=f"exec_{project_id}_{selected_asof}")
    else:
        st.info("Belum ada data visual pada tanggal yang dipilih. Upload file sumber dari Admin Mode.")

    # Engineering resume is rebuilt from register sheets. Excel DASHBOARD itself is not required.
    engineering_kinds = {"model_progress", "clash_issue", "rfi", "si", "shopdrawing", "apm", "wms"}
    eng_modules = {x.get("kind"): x for x in payloads if x.get("kind") in engineering_kinds}
    if eng_modules:
        st.markdown("### Engineering & BIM")
        render_engineering_dashboard(
            {"kind": "engineering_dashboard", "title": "Engineering & BIM", "modules": eng_modules},
            key=f"exec_eng_{project_id}_{selected_asof}",
        )

    lookaheads = [x for x in payloads if x.get("kind") == "lookahead"]
    if lookaheads:
        with st.expander("Rencana Kerja 2 Mingguan", expanded=True):
            actual_la = next((x for x in lookaheads if x.get("mode") == "actual"), None)
            forward_la = next((x for x in lookaheads if x.get("mode") == "forward"), None)
            if actual_la and forward_la:
                t1, t2 = st.tabs(["2 Minggu Lalu · Rencana vs Realisasi", "2 Minggu ke Depan · Lookahead"])
                with t1:
                    render_lookahead(actual_la, key=f"exec_la_actual_{project_id}_{selected_asof}")
                with t2:
                    render_lookahead(forward_la, key=f"exec_la_forward_{project_id}_{selected_asof}")
            elif actual_la:
                render_lookahead(actual_la, key=f"exec_la_actual_{project_id}_{selected_asof}")
            elif forward_la:
                render_lookahead(forward_la, key=f"exec_la_forward_{project_id}_{selected_asof}")

    issues = summary.get("issues", pd.DataFrame())
    if isinstance(issues, pd.DataFrame) and not issues.empty:
        st.markdown("### Action Required · Issue / Clash Open")
        keep = [c for c in ["No", "Deskripsi", "Level", "Lokasi", "Tindak Lanjut", "Status", "BIM Update", "SD Update"] if c in issues.columns]
        st.dataframe(issues[keep].head(12), use_container_width=True, hide_index=True, height=380)

    st.markdown("### Update visual terbaru")
    vp, vb = st.columns([1.0, 1.15], gap="large")
    with vp:
        render_media_batch(project_id, "progress_field", 4, "4 Foto Progress Lapangan", as_of_date=as_of_date, layout="grid")
    with vb:
        render_bim_comparison_grid(project_id, as_of_date=as_of_date)

    st.markdown("### BIMx Model")
    if p.get("bimx_url"):
        bx1, bx2 = st.columns([1, 2.2])
        with bx1:
            st.link_button("Buka Model BIMx ↗", p["bimx_url"], use_container_width=True)
            st.caption("Model dibuka dari link BIMx milik proyek. Public Viewer tetap read-only.")
        with bx2:
            embed_bimx = st.checkbox("Tampilkan BIMx di halaman", value=False, key=f"embed_bimx_{project_id}")
        if embed_bimx:
            st.components.v1.iframe(p["bimx_url"], height=680, scrolling=True)
            st.caption("Jika BIMx menolak embed/iframe, gunakan tombol **Buka Model BIMx** di atas.")
    else:
        st.info("Link BIMx belum diisi pada Master Proyek.")


# ------------------------------ Visual Sheet ------------------------------
elif page == "Visual Sheet":
    if not project_id:
        st.info("Buat proyek terlebih dahulu di menu Master Proyek.")
        st.stop()
    render_project_header(project_id, "Pilih fungsi → sheet → versi. Nama file hanya menjadi sumber data di belakang layar.")
    sheets_all = db.list_dashboard_sheets(project_id)
    if sheets_all.empty:
        st.info("Belum ada sheet aktif. Upload file dari Admin Mode lalu pilih sheet yang digunakan.")
    else:
        sheets_all = sheets_all.copy()
        sheets_all["function_name"] = sheets_all["function_name"].fillna("").replace("", "Other")
        functions = sorted(sheets_all["function_name"].dropna().unique().tolist())
        selected_function = st.selectbox("Fungsi", functions, key="visual_function")
        subset = sheets_all[sheets_all["function_name"].eq(selected_function)].copy()
        sheet_ids = subset["source_sheet_id"].tolist()
        smap = subset.set_index("source_sheet_id").to_dict("index")
        source_sheet_id = st.selectbox(
            "Sheet / Register",
            sheet_ids,
            key=f"visual_sheet_byfunc_{selected_function}",
            format_func=lambda sid: smap[sid].get("display_name") or smap[sid].get("sheet_name") or sid,
        )
        cfg = smap[source_sheet_id]
        fid = cfg["source_file_id"]
        fmeta = db.get_source_file(fid)
        versions = db.list_source_file_versions(fid)
        current_vid = fmeta.get("current_version_id")
        older = [v for v in versions["id"].tolist() if v != current_vid] if not versions.empty else []
        options = ["current"] + older
        vmap = versions.set_index("id").to_dict("index") if not versions.empty else {}
        version = st.selectbox(
            "Versi / log",
            options,
            key=f"visual_version_{source_sheet_id}",
            format_func=lambda v: (
                f"Current · {vmap.get(current_vid,{}).get('label','versi aktif')}" if v == "current"
                else f"{vmap[v]['label']} · {str(vmap[v]['created_at'])[:16]}"
            ),
        )
        if version == "current":
            path_text = fmeta.get("current_file_path")
            version_label = vmap.get(current_vid, {}).get("label", "Current")
        else:
            path_text = vmap[version]["file_path"]
            version_label = vmap[version]["label"]
        path = storage.resolve_stored_path(path_text, DATA_DIR) if path_text else None
        sheet_name = cfg["sheet_name"]
        st.caption(f"{selected_function} · {cfg.get('display_name') or sheet_name} · versi {version_label}")
        desc = cfg.get("sheet_description")
        if desc and not pd.isna(desc):
            st.info(str(desc))

        if path and path.exists() and path.suffix.lower() in {".xlsx", ".xlsm"}:
            with st.spinner("Menyusun visual sheet..."):
                payload_map = cached_workbook_visuals(str(path), path.stat().st_mtime_ns, (sheet_name,))
            payload = payload_map.get(sheet_name)
            if payload:
                render_payload(payload, key=f"visual_{source_sheet_id}_{version}")
            else:
                st.warning("Sheet tidak ditemukan pada versi file yang dipilih.")
            quality = cached_workbook_quality(str(path), path.stat().st_mtime_ns, (sheet_name,))
            if quality.get("error_count", 0):
                with st.expander(f"Data Quality Warning · {quality['error_count']} cell error", expanded=False):
                    st.warning("Workbook sumber mengandung error Excel. Data Hub tidak mengoreksi file sumber secara otomatis.")
                    st.dataframe(pd.DataFrame(quality.get("errors", [])), use_container_width=True, hide_index=True)
        else:
            st.info("Visual khusus workbook tersedia untuk XLSX/XLSM.")

        dataset_id = cfg.get("dataset_id")
        if dataset_id is not None and not (isinstance(dataset_id, float) and pd.isna(dataset_id)):
            with st.expander("Data register / snapshot", expanded=False):
                snaps = db.list_snapshots(source_sheet_id=source_sheet_id)
                snap_options = ["current"] + snaps["id"].tolist()
                snapmap = snaps.set_index("id").to_dict("index") if not snaps.empty else {}
                snap = st.selectbox(
                    "Versi data tabel",
                    snap_options,
                    key=f"visual_dbver_{source_sheet_id}",
                    format_func=lambda x: "Current Data" if x == "current" else snapmap[x]["label"],
                )
                df = db.get_dataframe(dataset_id, limit=500) if snap == "current" else db.get_snapshot_dataframe(snap, limit=500)
                st.dataframe(df, use_container_width=True, hide_index=True, height=430)
                st.caption("Preview maksimal 500 baris. Data penuh tetap tersimpan di SQLite.")


# ------------------------------ Update Data ------------------------------
elif page == "Update Data":
    if not project_id:
        st.info("Buat proyek terlebih dahulu di menu Master Proyek.")
        st.stop()
    render_project_header(project_id, "Kelola banyak file Excel, pilih banyak sheet, simpan versi data, lalu update per sheet.")
    _upd_project = db.get_project(project_id)
    st.success(f"Tujuan update aktif: **{_upd_project.get('code') or '—'} · {_upd_project.get('name') or '—'}**. Semua file yang diunggah pada halaman ini akan terikat ke proyek tersebut.")

    st.info(
        "**Struktur data:** Proyek → File sumber → Sheet terpilih → Fungsi Sheet → Dataset. "
        "File hanya menjadi wadah/sumber. Pengelompokan dashboard mengikuti fungsi masing-masing sheet. "
        "Format yang sudah dikenali (Kurva S, 2 Mingguan, dan Register Engineering) memakai strategi update otomatis tanpa meminta key generik."
    )

    files = st.file_uploader(
        "Upload satu atau beberapa Excel / CSV",
        type=["xlsx", "xlsm", "xls", "csv"],
        accept_multiple_files=True,
        help="Upload semua file fungsi. File aktif dikonfigurasi satu per satu agar aplikasi tetap responsif.",
    )

    if files:
        queue = pd.DataFrame({"File": [f.name for f in files], "Ukuran": [fmt_bytes(f.size) for f in files]})
        st.caption(f"{len(files)} file berada dalam antrean upload.")
        st.dataframe(queue, use_container_width=True, hide_index=True, height=min(38 + 35 * len(queue), 220))

        selected_idx = st.selectbox(
            "File aktif",
            list(range(len(files))),
            format_func=lambda i: f"{i+1}. {files[i].name} · {fmt_bytes(files[i].size)}",
            key=f"active_excel_{project_id}",
        )
        uploaded = files[selected_idx]
        file_bytes = uploaded.getvalue()
        state_key = f"filecfg_{project_id}_{uploaded.name}_{uploaded.size}"
        existing_file = db.get_source_file_by_name(project_id, uploaded.name)

        try:
            sheets = cached_sheet_names(file_bytes, uploaded.name)
        except Exception as exc:
            st.error(f"Tidak dapat membaca file: {exc}")
            sheets = []

        if sheets:
            with st.container(border=True):
                st.markdown(f"### 1 · File Sumber · `{uploaded.name}`")
                file_description = st.text_input(
                    "Deskripsi file",
                    value=(existing_file.get("description") or "") if existing_file else "",
                    placeholder="Contoh: Master Deliverables Engineering / Project Control mingguan",
                    key=f"filedesc_{state_key}",
                )
                st.caption("Fungsi tidak lagi ditentukan pada level file. Pengelompokan mengikuti **fungsi masing-masing sheet**.")

                previous_selected: list[str] = []
                if existing_file:
                    prev_sheets = db.list_source_sheets(existing_file["id"], selected_only=True)
                    previous_selected = [x for x in prev_sheets["sheet_name"].tolist() if x in sheets]
                if existing_file:
                    default_selected = [s for s in previous_selected if classify_sheet(uploaded.name, s) not in {"engineering_dashboard", "reference"}]
                else:
                    # Dashboard Excel dan sheet referensi pada Master Deliverables tidak perlu dijadikan data dashboard.
                    default_selected = [s for s in sheets if classify_sheet(uploaded.name, s) not in {"engineering_dashboard", "reference"}]
                selected_sheets = st.multiselect(
                    "Sheet yang digunakan",
                    sheets,
                    default=default_selected,
                    key=f"selectsheets_{state_key}",
                    help="Pilih hanya sheet yang diperlukan. DASHBOARD Excel dan Sheet1/reference pada workbook Engineering otomatis tidak dipilih pada upload pertama.",
                )
                default_version_label = f"{Path(uploaded.name).stem} · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                save_action = "Simpan sebagai versi/log baru"
                if existing_file:
                    save_action = st.radio(
                        "Cara menyimpan file",
                        ["Timpa file aktif", "Simpan sebagai versi/log baru"],
                        horizontal=True,
                        key=f"saveaction_{state_key}",
                        help="Timpa mempertahankan identitas file, mapping sheet, dan histori versi lama. Versi/log baru menambah arsip baru.",
                    )
                version_label = st.text_input(
                    "Nama versi / log file",
                    value=default_version_label,
                    key=f"fileversion_{state_key}",
                    help="Dipakai sebagai label file aktif / histori. Pada mode Timpa, versi aktif diganti tanpa menambah record versi baru.",
                )
                _target_code = str(_upd_project.get("code") or "Proyek")
                button_label = (f"Timpa file aktif · {_target_code}" if existing_file and save_action == "Timpa file aktif" else f"Simpan ke {_target_code}")
                if st.button(button_label, type="primary", key=f"savefile_{state_key}"):
                    sfid = db.upsert_source_file(project_id, uploaded.name, "", file_description, uploaded.size)
                    db.sync_source_sheet_selection(project_id, sfid, sheets, selected_sheets, "")
                    # Auto-map recognized sheet functions while preserving any existing configuration/dataset.
                    for sh in selected_sheets:
                        cfg0 = db.get_source_sheet(sfid, sh) or {}
                        kind0 = classify_sheet(uploaded.name, sh)
                        current_func0 = str(cfg0.get("function_name") or "").strip()
                        detected_func0 = default_function_for_profile(kind0)
                        should_remap = (not current_func0) or (current_func0 in {"Engineering & BIM", "Progress & Schedule", "Other"} and detected_func0 not in {"Other", "Reference / Master", "Ignored / Dashboard Excel"})
                        if should_remap:
                            mode0 = "period" if kind0 == "s_curve" else ("snapshot" if kind0 == "lookahead" else ("register_sync" if kind0 in ENGINEERING_REGISTER_KINDS else "auto"))
                            keys0 = ["Week"] if kind0 == "s_curve" else ([] if kind0 == "lookahead" else (['_Register_Key'] if kind0 in ENGINEERING_REGISTER_KINDS else []))
                            db.save_source_sheet_config(
                                project_id, sfid, sh, dataset_id=cfg0.get("dataset_id"),
                                function_name=detected_func0,
                                display_name=cfg0.get("display_name") or sh,
                                description=cfg0.get("description") or "", selected=True,
                                header_row=int(cfg0.get("header_row") or 1), key_columns=keys0,
                                update_mode=mode0, formula_mode=cfg0.get("formula_mode") or "values",
                                include_excel_row=bool(cfg0.get("include_excel_row") or 0),
                            )
                    if existing_file and save_action == "Timpa file aktif":
                        vid = overwrite_uploaded_source(project_id, sfid, uploaded, version_label)
                        action_text = f"File aktif {uploaded.name} berhasil ditimpa"
                    else:
                        vid = archive_uploaded_source(project_id, sfid, uploaded, version_label)
                        action_text = f"File {uploaded.name} diarsipkan sebagai versi '{version_label}'"
                    try:
                        src = db.get_source_file(sfid)
                        archived_path = storage.resolve_stored_path(src.get("current_file_path") or "", DATA_DIR)
                        quality = workbook_quality(archived_path, selected_sheets) if archived_path.exists() and archived_path.suffix.lower() in {".xlsx", ".xlsm"} else {"error_count":0}
                        warn = f" · warning formula/error: {quality.get('error_count',0)}" if quality.get("error_count",0) else ""
                    except Exception:
                        warn = ""
                    flash("success", f"{action_text}. {len(selected_sheets)} sheet dipilih{warn}.")
                    st.rerun()

            existing_file = db.get_source_file_by_name(project_id, uploaded.name)
            if existing_file:
                current_path = storage.resolve_stored_path(existing_file.get("current_file_path") or "", DATA_DIR)
                if current_path.exists() and current_path.suffix.lower() in {".xlsx", ".xlsm"}:
                    selected_now = db.list_source_sheets(existing_file["id"], selected_only=True)
                    selected_names_now = tuple(selected_now["sheet_name"].tolist()) if not selected_now.empty else tuple()
                    if selected_names_now:
                        q = cached_workbook_quality(str(current_path), current_path.stat().st_mtime_ns, selected_names_now)
                        if q.get("error_count",0):
                            with st.expander(f"Data Quality Check · {q['error_count']} error Excel ditemukan", expanded=True):
                                st.warning("File sumber memiliki error seperti #REF!, #DIV/0!, #VALUE!, #NAME? atau #N/A. Data Hub tetap dapat menampilkan data lain, tetapi sumber sebaiknya diperbaiki.")
                                st.dataframe(pd.DataFrame(q.get("errors",[])).head(100), use_container_width=True, hide_index=True)
                        else:
                            st.success("Data Quality Check: tidak ditemukan error Excel eksplisit pada sheet terpilih.")
                # One-click incremental synchronization for recognized Engineering register sheets.
                selected_cfgs = db.list_source_sheets(existing_file["id"], selected_only=True)
                eng_names = [
                    str(x) for x in selected_cfgs["sheet_name"].tolist()
                    if classify_sheet(uploaded.name, str(x)) in ENGINEERING_REGISTER_KINDS
                ] if not selected_cfgs.empty else []
                if eng_names and current_path.exists() and current_path.suffix.lower() in {".xlsx", ".xlsm"}:
                    with st.expander("Update Register Engineering · semua sheet terpilih", expanded=True):
                        st.caption("Workflow: ID baru → tambah record; ID lama → update kolom yang berubah; record lama yang tidak muncul → tetap disimpan.")
                        try:
                            eng_payloads = cached_workbook_visuals(str(current_path), current_path.stat().st_mtime_ns, tuple(eng_names))
                            sync_rows = []
                            prepared_sync = {}
                            for sh in eng_names:
                                pl = eng_payloads.get(sh, {})
                                frame, keycol, warns = build_register_sync_frame(pl)
                                cfgx = db.get_source_sheet(existing_file["id"], sh) or {}
                                oldx = pd.DataFrame()
                                if cfgx.get("dataset_id"):
                                    try:
                                        oldx = db.get_dataframe(cfgx["dataset_id"], limit=None).drop(columns=["_row_id"], errors="ignore")
                                    except Exception:
                                        oldx = pd.DataFrame()
                                comp = compare_register_frames(oldx, frame, keycol)
                                prepared_sync[sh] = (pl, frame, keycol, warns, cfgx, comp)
                                sync_rows.append({
                                    "Sheet": sh,
                                    "Fungsi": cfgx.get("function_name") or default_function_for_profile(classify_sheet(uploaded.name, sh)),
                                    "Record": len(frame),
                                    "Baru": comp["inserted"],
                                    "Berubah": comp["changed"],
                                    "Tetap": comp["unchanged"],
                                    "Warning": " | ".join(warns),
                                })
                            st.dataframe(pd.DataFrame(sync_rows), use_container_width=True, hide_index=True)
                            active_src = db.get_source_file(existing_file["id"])
                            active_vid = active_src.get("current_version_id")
                            versions_bulk = db.list_source_file_versions(existing_file["id"])
                            vlabel_bulk = "Current Upload"
                            if active_vid and not versions_bulk.empty and active_vid in versions_bulk["id"].tolist():
                                vlabel_bulk = str(versions_bulk.set_index("id").loc[active_vid, "label"])
                            if st.button("Apply Update Semua Register Engineering", type="primary", key=f"bulkeng_{state_key}"):
                                total_new = total_changed = 0
                                for sh, (pl, frame, keycol, warns, cfgx, comp) in prepared_sync.items():
                                    kindx = classify_sheet(uploaded.name, sh)
                                    funcx = cfgx.get("function_name") or default_function_for_profile(kindx)
                                    did = cfgx.get("dataset_id")
                                    if did:
                                        stats, _ = db.upsert_dataframe(did, frame, [keycol], allow_insert=True, allow_new_columns=True)
                                    else:
                                        did = db.create_dataset(frame, project_id, funcx, name=f"{uploaded.name} · {sh}", source_type="engineering_register", source_ref=uploaded.name, sheet_name=sh)
                                        stats = {"total": len(frame), "inserted": len(frame), "updated": 0, "rejected": 0}
                                    ssid = db.save_source_sheet_config(
                                        project_id, existing_file["id"], sh, dataset_id=did,
                                        function_name=funcx, display_name=cfgx.get("display_name") or sh,
                                        description=cfgx.get("description") or "", selected=True,
                                        header_row=1, key_columns=[keycol], update_mode="register_sync",
                                        formula_mode="values", include_excel_row=False,
                                    )
                                    db.add_import_batch(project_id, funcx, did, uploaded.name, sh, stats)
                                    db.create_snapshot(did, existing_file["id"], ssid, f"{cfgx.get('display_name') or sh} · {vlabel_bulk}", uploaded.name, source_version_id=active_vid)
                                    total_new += int(stats.get("inserted", 0))
                                    total_changed += int(comp.get("changed", 0))
                                flash("success", f"Register Engineering tersinkron: {total_new} record baru · {total_changed} record berubah.")
                                st.rerun()
                        except Exception as exc:
                            st.error(f"Preview/sinkronisasi register Engineering gagal: {exc}")

                registered = db.list_source_sheets(existing_file["id"], selected_only=True)
                registered_names = registered["sheet_name"].tolist() if not registered.empty else []
                if not registered_names:
                    st.warning("Belum ada sheet yang dipilih. Simpan pilihan sheet pada bagian Identitas File.")
                else:
                    with st.container(border=True):
                        st.markdown("### 2 · Konfigurasi & Update Sheet")
                        active_sheet = st.selectbox(
                            "Sheet aktif untuk dikonfigurasi / di-update",
                            registered_names,
                            key=f"activesheet_{state_key}",
                        )
                        sheet_cfg = db.get_source_sheet(existing_file["id"], active_sheet)
                        sheet_state = f"sheetcfg_{state_key}_{active_sheet}"
                        profile_kind = classify_sheet(uploaded.name, active_sheet)

                        sc1, sc2 = st.columns([1, 1.5])
                        with sc1:
                            detected_function = default_function_for_profile(profile_kind)
                            saved_function = (sheet_cfg or {}).get("function_name") or ""
                            sheet_function_default = saved_function if saved_function in SHEET_FUNCTIONS else (detected_function if detected_function in SHEET_FUNCTIONS else "Other")
                            sheet_func_idx = SHEET_FUNCTIONS.index(sheet_function_default) if sheet_function_default in SHEET_FUNCTIONS else len(SHEET_FUNCTIONS)-1
                            sheet_function = st.selectbox(
                                "Fungsi utama sheet",
                                SHEET_FUNCTIONS,
                                index=sheet_func_idx,
                                key=f"sheetfunc_{sheet_state}",
                                help="Pengelompokan dashboard mengikuti fungsi sheet, bukan nama workbook.",
                            )
                            display_name = st.text_input(
                                "Nama tampilan sheet",
                                value=(sheet_cfg or {}).get("display_name") or active_sheet,
                                key=f"display_{sheet_state}",
                            )
                        with sc2:
                            sheet_description = st.text_area(
                                "Deskripsi sheet",
                                value=(sheet_cfg or {}).get("description") or "",
                                placeholder="Jelaskan isi sheet dan tujuan datanya.",
                                key=f"sheetdesc_{sheet_state}",
                                height=105,
                            )

                        profile_labels = {
                            "s_curve": "Kurva S · update berdasarkan minggu/periode",
                            "lookahead": "2-Week Lookahead · snapshot periode",
                            "model_progress": "Model Progress / MIDP",
                            "clash_issue": "Clash & Issue Register",
                            "rfi": "RFI Register",
                            "si": "Site Instruction Register",
                            "shopdrawing": "Shop Drawing Register",
                            "apm": "Approval Material Register",
                            "wms": "Work Method Statement Register",
                            "reference": "Master / Reference",
                            "generic": "Tabel generik",
                        }
                        st.info(f"**Profil sheet terdeteksi:** {profile_labels.get(profile_kind, profile_kind)}")

                        # Profil khusus tidak meminta user memahami key database.
                        # File aktif harus diarsipkan terlebih dahulu karena arsip tersebut juga menjadi histori visual.
                        if profile_kind in ({"s_curve", "lookahead"} | ENGINEERING_REGISTER_KINDS):
                            current_path = storage.resolve_stored_path(existing_file.get("current_file_path") or "", DATA_DIR)
                            if not current_path.exists():
                                st.warning("Klik **Simpan file, sheet & versi** terlebih dahulu. Setelah itu Data Hub membaca profil sheet dari file yang sudah diarsipkan.")
                                st.stop()
                            try:
                                uploaded_hash = hashlib.sha256(file_bytes).hexdigest()
                                archived_hash = hashlib.sha256(current_path.read_bytes()).hexdigest()
                            except Exception:
                                uploaded_hash = archived_hash = ""
                            if uploaded_hash and archived_hash and uploaded_hash != archived_hash:
                                st.warning("File yang sedang dipilih berbeda dari versi aktif yang tersimpan. Klik **Simpan file, sheet & versi** terlebih dahulu agar update tidak memakai versi lama.")
                                st.stop()
                            try:
                                payload = cached_workbook_visuals(str(current_path), current_path.stat().st_mtime_ns, (active_sheet,)).get(active_sheet, {})
                            except Exception as exc:
                                st.error(f"Profil sheet tidak dapat dibaca: {exc}")
                                st.stop()
                            if payload.get("error"):
                                st.error(f"Profil sheet belum valid: {payload['error']}")
                                st.stop()

                            current_source = db.get_source_file(existing_file["id"])
                            source_version_id = current_source.get("current_version_id")
                            versions = db.list_source_file_versions(existing_file["id"])
                            active_version_label = "Current Upload"
                            if source_version_id and not versions.empty and source_version_id in versions["id"].tolist():
                                active_version_label = str(versions.set_index("id").loc[source_version_id, "label"])

                            if profile_kind == "s_curve":
                                st.markdown("#### Update khusus Kurva S")
                                st.caption("Tidak perlu memilih key. Data Hub memakai **Week (W1, W2, ...)** secara internal dan fokus membaca baris Rencana/Realisasi dari format Kurva S.")
                                k1,k2,k3,k4=st.columns(4)
                                with k1: st.metric("Minggu terakhir", payload.get("current_week") or "—")
                                with k2: st.metric("Realisasi", f"{float(payload.get('actual')):.2f}%" if payload.get("actual") is not None else "—")
                                with k3: st.metric("Rencana", f"{float(payload.get('plan')):.2f}%" if payload.get("plan") is not None else "—")
                                with k4: st.metric("Deviasi", f"{float(payload.get('deviation')):.2f}%" if payload.get("deviation") is not None else "—")
                                curve_df = payload.get("curve", pd.DataFrame()).copy()
                                if curve_df.empty:
                                    st.warning("Data W1.. dan baris realisasi belum terbaca.")
                                    st.stop()
                                actual_view = curve_df[[c for c in ["Week","Period","Weekly Actual","Actual","Plan","Deviation"] if c in curve_df.columns]].copy()
                                filled = actual_view[pd.to_numeric(actual_view.get("Weekly Actual"), errors="coerce").notna() | pd.to_numeric(actual_view.get("Actual"), errors="coerce").notna()]
                                st.dataframe(filled.tail(16), use_container_width=True, hide_index=True, height=360)

                                # Show corrections to historical weeks before update.
                                dataset_id = (sheet_cfg or {}).get("dataset_id")
                                changes = pd.DataFrame()
                                if dataset_id:
                                    try:
                                        old = db.get_dataframe(dataset_id, limit=None).drop(columns=["_row_id"], errors="ignore")
                                        if "Week" in old.columns:
                                            cols=[c for c in ["Weekly Actual","Actual"] if c in curve_df.columns and c in old.columns]
                                            merged=old[["Week"]+cols].merge(curve_df[["Week"]+cols],on="Week",how="outer",suffixes=(" Lama"," Baru"))
                                            masks=[]
                                            for c in cols:
                                                a=pd.to_numeric(merged[f"{c} Lama"],errors="coerce")
                                                b=pd.to_numeric(merged[f"{c} Baru"],errors="coerce")
                                                masks.append(~(a.fillna(-999999).round(8).eq(b.fillna(-999999).round(8))))
                                            if masks:
                                                m=masks[0]
                                                for extra in masks[1:]: m=m|extra
                                                changes=merged[m].copy()
                                    except Exception:
                                        changes=pd.DataFrame()
                                if not changes.empty:
                                    with st.expander(f"Perubahan realisasi terdeteksi · {len(changes)} minggu", expanded=True):
                                        st.dataframe(changes, use_container_width=True, hide_index=True)
                                        st.caption("Koreksi pada minggu lama tetap tercatat karena file asli setiap upload disimpan sebagai versi/log.")

                                save_snapshot = st.checkbox("Simpan snapshot data Kurva S setelah update", value=True, key=f"profilesnap_{sheet_state}")
                                snap_label = st.text_input("Nama snapshot", value=f"Kurva S · {active_version_label}", key=f"profilelabel_{sheet_state}", disabled=not save_snapshot)
                                if st.button("Validasi & update Kurva S", type="primary", key=f"profilesave_{sheet_state}"):
                                    try:
                                        if dataset_id:
                                            stats,rejected=db.upsert_dataframe(dataset_id,curve_df,["Week"],allow_insert=True,allow_new_columns=True)
                                        else:
                                            dataset_id=db.create_dataset(curve_df,project_id,sheet_function,name=f"{uploaded.name} · {active_sheet}",source_type="excel_profile",source_ref=uploaded.name,sheet_name=active_sheet)
                                            stats={"total":len(curve_df),"inserted":len(curve_df),"updated":0,"rejected":0}
                                            rejected=pd.DataFrame()
                                        source_sheet_id=db.save_source_sheet_config(
                                            project_id, existing_file["id"], active_sheet, dataset_id=dataset_id,
                                            function_name=sheet_function, display_name=display_name, description=sheet_description,
                                            selected=True, header_row=1, key_columns=["Week"], update_mode="period",
                                            formula_mode="values", include_excel_row=False,
                                        )
                                        db.add_import_batch(project_id,sheet_function,dataset_id,uploaded.name,active_sheet,stats)
                                        if save_snapshot:
                                            db.create_snapshot(dataset_id,existing_file["id"],source_sheet_id,snap_label,uploaded.name,source_version_id=source_version_id)
                                        flash("success", f"Kurva S diperbarui sampai {payload.get('current_week','—')}. {len(changes)} koreksi/perubahan periode lama terdeteksi.")
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(f"Update Kurva S gagal: {exc}")
                                st.stop()

                            # 2-Week Lookahead: one upload = one reporting snapshot.
                            if profile_kind == "lookahead":
                                mode = payload.get("mode", "forward")
                                st.markdown("#### " + ("Realisasi 2 Minggu Lalu" if mode=="actual" else "Rencana 2 Minggu ke Depan"))
                                st.caption(
                                    "Tidak perlu memilih key. Sheet **2 Minggu Lalu** dibaca sebagai RA vs RI; sheet **2 Minggu ke depan** dibaca sebagai rencana lookahead. "
                                    "Setiap upload disimpan sebagai snapshot periode sehingga histori Gantt tetap dapat dibuka kembali."
                                )
                                render_lookahead(payload, key=f"profile_la_{sheet_state}")
                                tasks = payload.get("tasks", pd.DataFrame()).copy()
                                if tasks.empty:
                                    st.warning("Tidak ada aktivitas yang terbaca dari format 2 mingguan.")
                                    st.stop()
                                save_snapshot = st.checkbox("Simpan snapshot tabel 2 mingguan", value=True, key=f"profilesnap_{sheet_state}")
                                snap_label = st.text_input("Nama snapshot", value=f"{payload.get('title','2 Mingguan')} · {active_version_label}", key=f"profilelabel_{sheet_state}", disabled=not save_snapshot)
                                if st.button("Validasi & simpan update 2 Mingguan", type="primary", key=f"profilesave_{sheet_state}"):
                                    try:
                                        dataset_id=(sheet_cfg or {}).get("dataset_id")
                                        if dataset_id:
                                            stats=db.replace_dataset_contents(dataset_id,tasks)
                                        else:
                                            dataset_id=db.create_dataset(tasks,project_id,sheet_function,name=f"{uploaded.name} · {active_sheet}",source_type="excel_snapshot",source_ref=uploaded.name,sheet_name=active_sheet)
                                            stats={"total":len(tasks),"inserted":len(tasks),"updated":0,"rejected":0}
                                        source_sheet_id=db.save_source_sheet_config(
                                            project_id, existing_file["id"], active_sheet, dataset_id=dataset_id,
                                            function_name=sheet_function, display_name=display_name, description=sheet_description,
                                            selected=True, header_row=1, key_columns=[], update_mode="snapshot",
                                            formula_mode="values", include_excel_row=False,
                                        )
                                        db.add_import_batch(project_id,sheet_function,dataset_id,uploaded.name,active_sheet,stats)
                                        if save_snapshot:
                                            db.create_snapshot(dataset_id,existing_file["id"],source_sheet_id,snap_label,uploaded.name,source_version_id=source_version_id)
                                        flash("success", f"{payload.get('title','2 Mingguan')} tersimpan sebagai snapshot periode. {len(tasks)} aktivitas terbaca.")
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(f"Update 2 Mingguan gagal: {exc}")
                                st.stop()

                            # Engineering registers: incremental sync, not generic CRUD setup.
                            if profile_kind in ENGINEERING_REGISTER_KINDS:
                                st.markdown("#### Sinkronisasi Register Engineering")
                                st.caption(
                                    "ID baru akan ditambahkan. ID yang sudah ada akan diperbarui hanya pada kolom yang berubah. "
                                    "Record lama yang tidak muncul pada upload baru **tidak dihapus otomatis**."
                                )
                                try:
                                    register_df, register_key, register_warnings = build_register_sync_frame(payload)
                                except Exception as exc:
                                    st.error(f"Register tidak dapat dinormalisasi: {exc}")
                                    st.stop()
                                if register_df.empty:
                                    st.warning("Tidak ada record register yang terbaca.")
                                    st.stop()
                                for warning in register_warnings:
                                    st.warning(warning)

                                dataset_id = (sheet_cfg or {}).get("dataset_id")
                                old_df = pd.DataFrame()
                                if dataset_id:
                                    try:
                                        old_df = db.get_dataframe(dataset_id, limit=None).drop(columns=["_row_id"], errors="ignore")
                                    except Exception:
                                        old_df = pd.DataFrame()
                                comparison = compare_register_frames(old_df, register_df, register_key)
                                c1, c2, c3, c4 = st.columns(4)
                                c1.metric("Record upload", len(register_df))
                                c2.metric("Baru", comparison["inserted"])
                                c3.metric("Berubah", comparison["changed"])
                                c4.metric("Tidak berubah", comparison["unchanged"])
                                changes_df = comparison.get("changes", pd.DataFrame())
                                if isinstance(changes_df, pd.DataFrame) and not changes_df.empty:
                                    with st.expander(f"Perubahan kolom · {len(changes_df)} field", expanded=True):
                                        st.dataframe(changes_df.head(250), use_container_width=True, hide_index=True, height=360)
                                with st.expander("Preview register hasil upload", expanded=False):
                                    st.dataframe(register_df.drop(columns=[register_key], errors="ignore").head(200), use_container_width=True, hide_index=True, height=420)

                                save_snapshot = st.checkbox("Simpan snapshot register setelah update", value=True, key=f"profilesnap_{sheet_state}")
                                snap_label = st.text_input(
                                    "Nama snapshot", value=f"{display_name} · {active_version_label}",
                                    key=f"profilelabel_{sheet_state}", disabled=not save_snapshot,
                                )
                                if st.button(f"Apply Update Register · {display_name}", type="primary", key=f"registersync_{sheet_state}"):
                                    try:
                                        if dataset_id:
                                            stats, rejected = db.upsert_dataframe(
                                                dataset_id, register_df, [register_key],
                                                allow_insert=True, allow_new_columns=True,
                                            )
                                        else:
                                            dataset_id = db.create_dataset(
                                                register_df, project_id, sheet_function,
                                                name=f"{uploaded.name} · {active_sheet}",
                                                source_type="engineering_register", source_ref=uploaded.name, sheet_name=active_sheet,
                                            )
                                            stats = {"total": len(register_df), "inserted": len(register_df), "updated": 0, "rejected": 0}
                                            rejected = pd.DataFrame()
                                        source_sheet_id = db.save_source_sheet_config(
                                            project_id, existing_file["id"], active_sheet, dataset_id=dataset_id,
                                            function_name=sheet_function, display_name=display_name, description=sheet_description,
                                            selected=True, header_row=1, key_columns=[register_key], update_mode="register_sync",
                                            formula_mode="values", include_excel_row=False,
                                        )
                                        db.add_import_batch(project_id, sheet_function, dataset_id, uploaded.name, active_sheet, stats)
                                        if save_snapshot:
                                            db.create_snapshot(
                                                dataset_id, existing_file["id"], source_sheet_id, snap_label, uploaded.name,
                                                source_version_id=source_version_id,
                                            )
                                        flash(
                                            "success",
                                            f"Register diperbarui: {stats.get('inserted',0)} baru · {comparison['changed']} record berubah · {comparison['unchanged']} tetap.",
                                        )
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(f"Sinkronisasi register gagal: {exc}")
                                st.stop()

                        formula_values = ["Nilai hasil formula", "Teks formula"]
                        formula_default = 0 if (sheet_cfg or {}).get("formula_mode", "values") == "values" else 1
                        formula_mode = st.selectbox("Baca formula", formula_values, index=formula_default, key=f"formula_{sheet_state}")
                        data_only = formula_mode == "Nilai hasil formula"

                        try:
                            raw_preview = cached_raw_preview(file_bytes, uploaded.name, active_sheet, data_only=data_only, max_rows=500)
                            guessed = guess_header_row(raw_preview)
                            default_header = int((sheet_cfg or {}).get("header_row") or guessed)
                            header_row = st.number_input("Baris header", 1, 10000, max(1, min(default_header, 10000)), key=f"header_{sheet_state}")
                            fill_down = st.checkbox("Fill-down sel kosong/merge", value=False, key=f"fill_{sheet_state}")
                        except Exception as exc:
                            st.error(f"Gagal membaca sheet: {exc}")
                            raw_preview = pd.DataFrame(); header_row = 1; fill_down = False

                        if not raw_preview.empty:
                            key_method_default = 1 if int((sheet_cfg or {}).get("include_excel_row") or 0) else 0
                            key_method = st.radio(
                                "Key pencocokan record",
                                ["Gunakan kolom ID", "Gunakan nomor baris Excel"],
                                index=key_method_default,
                                horizontal=True,
                                key=f"keymethod_{sheet_state}",
                            )
                            include_excel_row = key_method == "Gunakan nomor baris Excel"

                            preview_raw = raw_preview
                            if int(header_row) >= len(raw_preview):
                                preview_raw = cached_raw_preview(file_bytes, uploaded.name, active_sheet, data_only=data_only, max_rows=int(header_row)+200)
                            quick_preview = build_dataframe(preview_raw, int(header_row), fill_down=fill_down, include_excel_row=include_excel_row)

                            if quick_preview.empty:
                                st.warning("Tidak ada data setelah header yang dipilih.")
                            else:
                                full_preview_enabled = st.toggle("Muat preview lengkap semua baris", value=False, key=f"fullpreview_{sheet_state}")
                                if full_preview_enabled:
                                    with st.spinner("Membaca seluruh sheet untuk preview..."):
                                        preview = cached_full_dataframe(file_bytes, uploaded.name, active_sheet, int(header_row), fill_down, data_only=data_only, include_excel_row=include_excel_row)
                                else:
                                    preview = quick_preview

                                pp1, pp2 = st.columns(2)
                                with pp1:
                                    preview_size = st.selectbox("Baris per halaman preview", [25, 50, 100, 250, 500], index=2, key=f"previewsize_{sheet_state}")
                                total_pages = max(1, (len(preview)+int(preview_size)-1)//int(preview_size))
                                with pp2:
                                    preview_page = st.number_input("Halaman preview", 1, total_pages, 1, key=f"previewpage_{sheet_state}")
                                pstart=(int(preview_page)-1)*int(preview_size); pend=min(pstart+int(preview_size), len(preview))
                                st.dataframe(preview.iloc[pstart:pend], use_container_width=True, hide_index=True)
                                st.caption(f"Menampilkan {pstart+1:,}–{pend:,} dari {len(preview):,} baris preview. Update final membaca seluruh sheet.")

                                with st.popover("Cek struktur kolom"):
                                    st.dataframe(column_diagnostics(preview), use_container_width=True, hide_index=True)

                                mode_labels = {
                                    "auto": "Baris + Kolom (otomatis)",
                                    "row": "Tambah / update baris",
                                    "column": "Update / tambah kolom",
                                }
                                current_mode = (sheet_cfg or {}).get("update_mode") or "auto"
                                modes = list(mode_labels.values())
                                mode_index = list(mode_labels.keys()).index(current_mode) if current_mode in mode_labels else 0
                                update_mode_label = st.radio("Mode update", modes, index=mode_index, horizontal=True, key=f"mode_{sheet_state}")
                                update_mode = next(k for k,v in mode_labels.items() if v == update_mode_label)

                                if include_excel_row:
                                    key_cols = ["__Excel_Row"]
                                    st.info("Key aktif: nomor baris Excel. Gunakan hanya jika posisi baris konsisten antar-update.")
                                else:
                                    saved_keys = (sheet_cfg or {}).get("key_columns") or []
                                    default_keys = [k for k in saved_keys if k in preview.columns]
                                    key_cols = st.multiselect(
                                        "Kolom ID / key untuk pencocokan *",
                                        [c for c in preview.columns if c != "__Excel_Row"],
                                        default=default_keys,
                                        key=f"keys_{sheet_state}",
                                    )

                                if update_mode == "auto":
                                    allow_insert=True; allow_new_columns=True
                                    st.success("Key sama → update · key baru → tambah baris · kolom baru → tambah kolom.")
                                elif update_mode == "row":
                                    allow_insert=True; allow_new_columns=False
                                    st.info("Key sama → update · key baru → tambah baris · struktur kolom lama dipertahankan.")
                                else:
                                    allow_insert=False; allow_new_columns=True
                                    st.info("Record lama dapat menerima kolom baru; key baru tidak menambah baris.")

                                st.markdown("#### Simpan versi / log data")
                                save_snapshot = st.checkbox(
                                    "Simpan snapshot setelah update",
                                    value=True,
                                    key=f"snapshot_{sheet_state}",
                                    help="Snapshot menyimpan kondisi data hasil update agar dapat dipilih kembali dari Dashboard.",
                                )
                                default_label = f"{Path(uploaded.name).stem} · {active_sheet} · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                                snapshot_label = st.text_input("Nama versi / log", value=default_label, key=f"snapshotlabel_{sheet_state}", disabled=not save_snapshot)

                                if st.button(f"Validasi & update · {active_sheet}", key=f"import_{sheet_state}", type="primary"):
                                    if not key_cols:
                                        st.error("Pilih minimal satu key pencocokan sebelum update.")
                                    else:
                                        try:
                                            with st.spinner("Membaca seluruh sheet dan memperbarui database..."):
                                                full_df = prepare_excel(uploaded, active_sheet, int(header_row), fill_down, "values" if data_only else "formula", include_excel_row)
                                                missing=[k for k in key_cols if k not in full_df.columns]
                                                if missing: raise ValueError("Key tidak ada pada data: " + ", ".join(missing))

                                                dataset_id = (sheet_cfg or {}).get("dataset_id")
                                                if dataset_id:
                                                    stats, rejected = db.upsert_dataframe(dataset_id, full_df, key_cols, allow_insert=allow_insert, allow_new_columns=allow_new_columns)
                                                else:
                                                    key_frame=full_df[key_cols].copy()
                                                    norm=key_frame.astype("string").fillna("").apply(lambda c: c.str.strip())
                                                    blank_mask=norm.eq("").any(axis=1); dup_mask=norm.duplicated(keep="first"); reject_mask=blank_mask|dup_mask
                                                    valid_df=full_df.loc[~reject_mask].reset_index(drop=True)
                                                    rejected=full_df.loc[reject_mask].copy()
                                                    if not rejected.empty:
                                                        rejected["_reject_reason"]=["Key kosong" if blank_mask.loc[i] else "Key duplikat dalam file" for i in rejected.index]
                                                    dataset_id=db.create_dataset(valid_df, project_id, sheet_function, name=f"{uploaded.name} · {active_sheet}", source_type="excel", source_ref=uploaded.name, sheet_name=active_sheet)
                                                    stats={"total":len(full_df),"inserted":len(valid_df),"updated":0,"rejected":int(reject_mask.sum()),"ignored_columns":0}

                                                source_sheet_id=db.save_source_sheet_config(
                                                    project_id, existing_file["id"], active_sheet,
                                                    dataset_id=dataset_id, function_name=sheet_function, display_name=display_name,
                                                    description=sheet_description, selected=True, header_row=int(header_row),
                                                    key_columns=key_cols, update_mode=update_mode,
                                                    formula_mode="values" if data_only else "formula", include_excel_row=include_excel_row,
                                                )
                                                db.add_import_batch(project_id, sheet_function, dataset_id, uploaded.name, active_sheet, stats)
                                                snap_id=None
                                                if save_snapshot:
                                                    current_source = db.get_source_file(existing_file["id"])
                                                    snap_id=db.create_snapshot(dataset_id, existing_file["id"], source_sheet_id, snapshot_label, uploaded.name, source_version_id=current_source.get("current_version_id"))
                                            msg=f"Update selesai: {stats['inserted']} baris baru · {stats['updated']} record diperbarui · {stats['rejected']} ditolak."
                                            if save_snapshot: msg += " Snapshot versi tersimpan dan dapat dipilih di Dashboard."
                                            st.success(msg)
                                            if not rejected.empty:
                                                st.download_button(
                                                    "Download record ditolak", dataframe_to_xlsx_bytes(rejected, "Rejected"),
                                                    file_name=f"rejected_{Path(uploaded.name).stem}_{active_sheet}.xlsx",
                                                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                                    key=f"rej_{sheet_state}",
                                                )
                                        except Exception as exc:
                                            try:
                                                db.add_import_batch(project_id, sheet_function, (sheet_cfg or {}).get("dataset_id"), uploaded.name, active_sheet, {"total":0}, status="FAILED", error=str(exc))
                                            except Exception:
                                                pass
                                            st.error(f"Update gagal: {exc}")

    st.markdown("---")
    st.markdown("### File & sheet yang sudah terdaftar")
    registered_files = db.list_source_files(project_id)
    if registered_files.empty:
        st.caption("Belum ada file sumber tersimpan.")
    else:
        fview = registered_files[["filename","description","selected_sheet_count","configured_sheet_count","last_uploaded_at"]].copy()
        fview.columns=["File","Deskripsi","Sheet Aktif","Sheet Terdaftar","Upload Terakhir"]
        st.dataframe(fview, use_container_width=True, hide_index=True)
        file_for_detail = st.selectbox(
            "Lihat sheet dari file",
            registered_files["id"].tolist(),
            key="registered_file_detail",
            format_func=lambda fid: registered_files.set_index("id").loc[fid,"filename"],
        )
        sview=db.list_source_sheets(file_for_detail, selected_only=False)
        if not sview.empty:
            sshow=sview[["sheet_name","display_name","description","function_name","selected","row_count","dataset_updated_at"]].copy()
            sshow.columns=["Sheet","Nama Tampilan","Deskripsi","Fungsi","Tampil Dashboard","Row","Update Data"]
            st.dataframe(sshow, use_container_width=True, hide_index=True)



# ------------------------------ Photos / BIM visual updates ------------------------------
elif page == "Foto & BIM Update":
    if not project_id:
        st.info("Buat proyek terlebih dahulu di menu Master Proyek.")
        st.stop()
    render_project_header(
        project_id,
        "Update visual proyek per batch: maksimal 4 foto progress lapangan, 2 screenshot BIM 2 minggu lalu, dan 2 screenshot BIM 2 minggu sekarang. Batch lama tetap tersimpan sebagai histori.",
    )

    _media_project = db.get_project(project_id)
    st.success(f"Tujuan upload visual: **{_media_project.get('code') or '—'} · {_media_project.get('name') or '—'}**")
    st.markdown("### Update visual proyek")
    u1, u2 = st.columns([1.35, 1])

    with u1:
        with st.container(border=True):
            st.markdown("#### 1 · Progress Lapangan")
            st.caption("Upload bersamaan maksimal 4 foto. Dashboard hanya menampilkan batch progress terbaru, tanpa mencampur batch lama.")
            progress_files = st.file_uploader(
                "Foto progress (maks. 4)", type=["jpg", "jpeg", "png"], accept_multiple_files=True, key="progress_batch_upload"
            )
            pc1, pc2 = st.columns(2)
            with pc1:
                progress_date = st.date_input("Tanggal progress", value=date.today(), key="progress_date")
            with pc2:
                progress_zone = st.text_input("Zona / lokasi", placeholder="Lt. 4 · Zona B", key="progress_zone")
            progress_label = st.text_input(
                "Label update", value=f"Progress Lapangan · {date.today().isoformat()}", key="progress_label"
            )
            progress_wbs = st.text_input("WBS / Activity ID (opsional)", key="progress_wbs")
            progress_desc = st.text_area("Keterangan progress", placeholder="Keterangan untuk batch foto ini.", key="progress_desc")
            if progress_files and len(progress_files) > 4:
                st.error(f"Anda memilih {len(progress_files)} file. Maksimal 4 foto progress per batch.")
            if st.button(
                "Simpan 4 Foto Progress", type="primary", use_container_width=True,
                disabled=(not progress_files or len(progress_files) > 4), key="save_progress_batch"
            ):
                saved, errors = save_media_batch(
                    project_id, progress_files, "progress_field", "Progress Lapangan", progress_date,
                    progress_zone, progress_wbs, progress_desc, progress_label, 4,
                )
                if saved:
                    flash("success", f"Batch progress tersimpan: {saved} foto.")
                if errors:
                    st.error("\n".join(errors))
                if saved:
                    st.rerun()

    with u2:
        with st.container(border=True):
            st.markdown("#### 2 · Update 3D BIM")
            st.caption("Upload 4 screenshot BIM per siklus: 2 gambar untuk 2 minggu lalu dan 2 gambar untuk 2 minggu sekarang.")
            st.markdown("##### A. BIM · 2 Minggu Lalu")
            bim_prev_files = st.file_uploader(
                "Screenshot BIM 2 minggu lalu (maks. 2)", type=["jpg", "jpeg", "png"], accept_multiple_files=True, key="bim_prev_batch_upload"
            )
            bpa, bpb = st.columns(2)
            with bpa:
                bim_prev_date = st.date_input("Tanggal update BIM · 2 minggu lalu", value=date.today(), key="bim_prev_date")
            with bpb:
                bim_prev_zone = st.text_input("Area / discipline", placeholder="Structure / Architecture / MEP", key="bim_prev_zone")
            bim_prev_label = st.text_input(
                "Label batch · 2 minggu lalu", value=f"BIM · 2 Minggu Lalu · {date.today().isoformat()}", key="bim_prev_label"
            )
            bim_prev_info = st.text_input("Model / WBS / revision (opsional)", key="bim_prev_info")
            bim_prev_desc = st.text_area("Keterangan BIM · 2 minggu lalu", placeholder="Catatan kondisi/model pada periode 2 minggu lalu.", key="bim_prev_desc")
            if bim_prev_files and len(bim_prev_files) > 2:
                st.error(f"Anda memilih {len(bim_prev_files)} file. Maksimal 2 screenshot BIM untuk 2 minggu lalu.")
            if st.button(
                "Simpan 2 Screenshot BIM · 2 Minggu Lalu", type="primary", use_container_width=True,
                disabled=(not bim_prev_files or len(bim_prev_files) > 2), key="save_bim_prev_batch"
            ):
                saved, errors = save_media_batch(
                    project_id, bim_prev_files, "bim_3d_prev", "Update 3D BIM · 2 Minggu Lalu", bim_prev_date,
                    bim_prev_zone, bim_prev_info, bim_prev_desc, bim_prev_label, 2,
                )
                if saved:
                    flash("success", f"Batch BIM 2 minggu lalu tersimpan: {saved} screenshot.")
                if errors:
                    st.error("\n".join(errors))
                if saved:
                    st.rerun()
            st.markdown("---")
            st.markdown("##### B. BIM · 2 Minggu Sekarang")
            bim_curr_files = st.file_uploader(
                "Screenshot BIM 2 minggu sekarang (maks. 2)", type=["jpg", "jpeg", "png"], accept_multiple_files=True, key="bim_curr_batch_upload"
            )
            bca, bcb = st.columns(2)
            with bca:
                bim_curr_date = st.date_input("Tanggal update BIM · 2 minggu sekarang", value=date.today(), key="bim_curr_date")
            with bcb:
                bim_curr_zone = st.text_input("Area / discipline", placeholder="Structure / Architecture / MEP", key="bim_curr_zone")
            bim_curr_label = st.text_input(
                "Label batch · 2 minggu sekarang", value=f"BIM · 2 Minggu Sekarang · {date.today().isoformat()}", key="bim_curr_label"
            )
            bim_curr_info = st.text_input("Model / WBS / revision (opsional)", key="bim_curr_info")
            bim_curr_desc = st.text_area("Keterangan BIM · 2 minggu sekarang", placeholder="Catatan kondisi/model pada periode saat ini.", key="bim_curr_desc")
            if bim_curr_files and len(bim_curr_files) > 2:
                st.error(f"Anda memilih {len(bim_curr_files)} file. Maksimal 2 screenshot BIM untuk 2 minggu sekarang.")
            if st.button(
                "Simpan 2 Screenshot BIM · 2 Minggu Sekarang", type="primary", use_container_width=True,
                disabled=(not bim_curr_files or len(bim_curr_files) > 2), key="save_bim_curr_batch"
            ):
                saved, errors = save_media_batch(
                    project_id, bim_curr_files, "bim_3d_current", "Update 3D BIM · 2 Minggu Sekarang", bim_curr_date,
                    bim_curr_zone, bim_curr_info, bim_curr_desc, bim_curr_label, 2,
                )
                if saved:
                    flash("success", f"Batch BIM 2 minggu sekarang tersimpan: {saved} screenshot.")
                if errors:
                    st.error("\n".join(errors))
                if saved:
                    st.rerun()

    st.markdown("### Preview batch aktif")
    pv1, pv2 = st.columns([2, 1.15])
    with pv1:
        render_media_batch(project_id, "progress_field", 4, "Progress Lapangan Terbaru")
    with pv2:
        render_bim_comparison_grid(project_id)

    with st.expander("Upload dokumentasi lain (QAQC / SHE / Material / Issue / Inspection)", expanded=False):
        other_upload = st.file_uploader(
            "JPG / JPEG / PNG", type=["jpg", "jpeg", "png"], accept_multiple_files=True, key="other_photo_upload"
        )
        oc1, oc2, oc3 = st.columns(3)
        with oc1: other_date = st.date_input("Tanggal", value=date.today(), key="other_date")
        with oc2: other_zone = st.text_input("Zona / lokasi", key="other_zone")
        with oc3: other_cat = st.selectbox("Kategori", ["Quality", "SHE", "Material", "Issue", "Inspection", "Other"], key="other_cat")
        other_wbs = st.text_input("WBS / Activity ID", key="other_wbs")
        other_desc = st.text_area("Keterangan", key="other_desc")
        if st.button("Simpan dokumentasi lain", disabled=not other_upload, key="save_other_photos"):
            saved, errors = save_media_batch(
                project_id, other_upload, "other", other_cat, other_date, other_zone,
                other_wbs, other_desc, f"{other_cat} · {other_date.isoformat()}", max(1, len(other_upload or [])),
            )
            if saved: flash("success", f"{saved} dokumentasi tersimpan.")
            if errors: st.error("\n".join(errors))
            if saved: st.rerun()

    st.markdown("### Histori batch visual")
    batches = db.list_photo_batches(project_id)
    if batches.empty:
        st.info("Belum ada batch visual.")
    else:
        bshow = batches.copy()
        labels = {"progress_field":"Progress Lapangan", "bim_3d_prev":"BIM · 2 Minggu Lalu", "bim_3d_current":"BIM · 2 Minggu Sekarang", "other":"Dokumentasi Lain"}
        bshow["media_type"] = bshow["media_type"].map(labels).fillna(bshow["media_type"])
        bshow.columns = ["Batch ID", "Jenis", "Label", "Tanggal", "Zona/Area", "Jumlah Gambar", "Waktu Upload"]
        st.dataframe(bshow, use_container_width=True, hide_index=True)

    st.markdown("### Gallery")
    all_photos = db.list_photos(project_id, limit=500)
    if all_photos.empty:
        st.info("Belum ada foto / screenshot.")
    else:
        f0, f1, f2 = st.columns(3)
        media_values = ["", "progress_field", "bim_3d_prev", "bim_3d_current", "other"]
        media_labels = {"":"Semua", "progress_field":"Progress Lapangan", "bim_3d_prev":"BIM · 2 Minggu Lalu", "bim_3d_current":"BIM · 2 Minggu Sekarang", "other":"Dokumentasi Lain"}
        categories = [""] + sorted([x for x in all_photos["category"].dropna().unique().tolist() if str(x).strip()])
        zones = [""] + sorted([x for x in all_photos["zone"].dropna().unique().tolist() if str(x).strip()])
        with f0: media_filter = st.selectbox("Jenis media", media_values, format_func=lambda x: media_labels[x])
        with f1: cat_filter = st.selectbox("Filter kategori", categories, format_func=lambda x: x or "Semua")
        with f2: zone_filter = st.selectbox("Filter zona", zones, format_func=lambda x: x or "Semua")
        per_page = 24
        total_gallery = db.photo_count(project_id, category=cat_filter, zone=zone_filter, media_type=media_filter)
        page_count = max(1, (total_gallery + per_page - 1) // per_page)
        page_no = st.number_input("Halaman gallery", min_value=1, max_value=page_count, value=1, step=1, key="media_gallery_page")
        gallery = db.list_photos(
            project_id, category=cat_filter, zone=zone_filter, media_type=media_filter,
            limit=per_page, offset=(int(page_no)-1)*per_page
        )
        st.caption(f"Menampilkan {len(gallery)} dari {total_gallery} media · halaman {int(page_no)}/{page_count}")
        cols = st.columns(4)
        for pos, (_, row) in enumerate(gallery.iterrows()):
            with cols[pos % 4]:
                pth = storage.resolve_stored_path(row["file_path"], DATA_DIR)
                if pth.exists():
                    try:
                        thumb = cached_thumbnail(str(pth), pth.stat().st_mtime_ns, 800)
                        st.image(thumb, use_container_width=True)
                    except Exception:
                        st.image(str(pth), use_container_width=True)
                else:
                    st.warning("File tidak ditemukan")
                kind = media_labels.get(str(row.get("media_type") or ""), row.get("category") or "Dokumentasi")
                st.caption(f"{kind} · {row['captured_date'] or '-'} · {row['zone'] or '-'}")
                if row.get("batch_label"):
                    st.caption(str(row["batch_label"]))
                if row["wbs_activity"]:
                    st.caption(f"WBS/Model: {row['wbs_activity']}")
                if row["description"]:
                    st.write(str(row["description"])[:220])
                if st.button("Hapus", key=f"del_photo_{row['id']}"):
                    file_path = db.delete_photo(row["id"])
                    if file_path:
                        try: storage.resolve_stored_path(file_path, DATA_DIR).unlink(missing_ok=True)
                        except Exception: pass
                    flash("success", "Media dihapus.")
                    st.rerun()


# ------------------------------ CRUD ------------------------------
elif page == "Quick Edit Data":
    if not project_id:
        st.info("Buat proyek terlebih dahulu di menu Master Proyek.")
        st.stop()
    render_project_header(project_id, "Quick Edit bersifat opsional. Update rutin tetap melalui Excel; gunakan halaman ini untuk koreksi individual seperti Status, Tanggal Close, Response Date, atau Remarks.")
    dsid = dataset_selector(project_id, "crud_dataset")
    if dsid:
        meta = db.get_dataset_meta(dsid)
        total_rows = int(meta.get("row_count", 0))
        p1, p2 = st.columns([1, 1])
        with p1:
            page_size = st.selectbox("Baris per halaman", [100, 250, 500, 1000], index=1, key=f"crud_size_{dsid}")
        page_count = max(1, (total_rows + int(page_size) - 1) // int(page_size))
        with p2:
            page_no = st.number_input("Halaman", min_value=1, max_value=page_count, value=1, step=1, key=f"crud_page_{dsid}")
        df = db.get_dataframe(dsid, limit=int(page_size), offset=(int(page_no)-1)*int(page_size))
        st.caption(f"Fungsi: {meta['function_name']} · {fmt_int(total_rows)} row · halaman {int(page_no)}/{page_count}")
        term = st.text_input("Search pada halaman ini", placeholder="Cari kode, deskripsi, PIC, status, dll.")
        filtered = global_search(df, term)
        edited = st.data_editor(
            filtered, use_container_width=True, hide_index=True, num_rows="dynamic",
            disabled=["_row_id"], key=f"editor_{dsid}_{page_no}_{term}",
        )
        st.info("Quick Edit dipaginasi agar stabil. Perubahan langsung masuk database dan tercatat di audit log; Excel tetap menjadi media bulk update utama.")
        c1, c2 = st.columns([1, 1])
        with c1:
            if st.button("Simpan Quick Edit", type="primary"):
                result = db.apply_edits(dsid, filtered, edited)
                flash("success", f"Tersimpan: {result['inserted']} baru · {result['updated']} diubah · {result['deleted']} dihapus.")
                st.rerun()
        with c2:
            export_df = edited.drop(columns=["_row_id"], errors="ignore")
            st.download_button(
                "Export halaman ke Excel", dataframe_to_xlsx_bytes(export_df, "Data"),
                file_name=f"{meta['function_name'].replace(' ', '_')}_page_{int(page_no)}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True,
            )


# ------------------------------ Explorer ------------------------------
elif page == "Data Explorer":
    if not project_id:
        st.info("Buat proyek terlebih dahulu di menu Master Proyek.")
        st.stop()
    render_project_header(project_id, "Filter dan analisis dataset tanpa mengubah data sumber.")
    dsid = dataset_selector(project_id, "explore_dataset")
    if dsid:
        meta_exp = db.get_dataset_meta(dsid)
        df = db.get_dataframe(dsid, limit=10000)
        if int(meta_exp.get("row_count", 0)) > len(df):
            st.info(f"Explorer memuat 10.000 baris pertama dari {fmt_int(meta_exp['row_count'])} baris untuk menjaga performa. Quick Edit tersedia per halaman.")
        columns = [c for c in df.columns if c != "_row_id"]
        f1, f2, f3 = st.columns([1.4, 1, 1])
        with f1:
            term = st.text_input("Search", key="explore_search")
        with f2:
            filter_col = st.selectbox("Kolom filter", [""] + columns)
        work = global_search(df, term)
        with f3:
            filter_value = ""
            if filter_col:
                uniques = work[filter_col].dropna().astype(str).unique().tolist()
                if len(uniques) <= 100:
                    filter_value = st.selectbox("Nilai", [""] + sorted(uniques), format_func=lambda x: x or "Semua")
                else:
                    filter_value = st.text_input("Contains", key="contains_filter")
        if filter_col and filter_value:
            work = work[work[filter_col].astype(str).str.contains(str(filter_value), case=False, na=False, regex=False)]
        st.dataframe(work, use_container_width=True, hide_index=True, height=520)
        st.download_button(
            "Download hasil filter",
            dataframe_to_xlsx_bytes(work.drop(columns=["_row_id"], errors="ignore"), "Filtered"),
            file_name="filtered_data.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


# ------------------------------ History & DB ------------------------------
elif page == "Riwayat & Database":
    if not project_id:
        st.info("Buat proyek terlebih dahulu di menu Master Proyek.")
        st.stop()
    render_project_header(project_id, "Jejak update Excel, perubahan database, dan backup lokal.")
    t1, t2, t3, t4, t5 = st.tabs(["Riwayat Import", "Versi File", "Snapshot Data", "Audit Log", "Backup Database"])
    with t1:
        hist = db.get_import_history(project_id, limit=300)
        if hist.empty:
            st.info("Belum ada import.")
        else:
            st.dataframe(hist, use_container_width=True, hide_index=True)
    with t2:
        files_hist = db.list_source_files(project_id)
        if files_hist.empty:
            st.info("Belum ada file sumber.")
        else:
            for _, fr in files_hist.iterrows():
                versions = db.list_source_file_versions(fr["id"])
                with st.expander(f"{fr['filename']} · {len(versions)} versi", expanded=False):
                    if fr.get("description") and not pd.isna(fr.get("description")):
                        st.caption(str(fr.get("description")))
                    if versions.empty:
                        st.info("Belum ada versi file yang diarsipkan.")
                    else:
                        view=versions[["id","label","file_name","file_size","created_at"]].copy()
                        view["file_size"]=view["file_size"].map(fmt_bytes)
                        view.columns=["ID","Versi / Log","File","Ukuran","Waktu"]
                        st.dataframe(view,use_container_width=True,hide_index=True)

                    st.markdown("#### Kelola file sumber")
                    st.caption("Dua tindakan utama: **Timpa file aktif** atau **Hapus keseluruhan**. Keduanya hanya tersedia di Admin Mode.")
                    manage_overwrite, manage_delete = st.tabs(["Timpa file aktif", "Hapus keseluruhan"])
                    with manage_overwrite:
                        replacement = st.file_uploader(
                            "Pilih file pengganti",
                            type=["xlsx","xlsm","xls","csv"],
                            key=f"replace_source_{fr['id']}",
                            help="Mapping sheet dan identitas sumber tetap dipertahankan. File aktif diganti tanpa membuat source file baru.",
                        )
                        replace_label = st.text_input(
                            "Label file aktif",
                            value=f"Timpa · {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                            key=f"replace_label_{fr['id']}",
                        )
                        if replacement is not None:
                            try:
                                incoming_sheets = cached_sheet_names(replacement.getvalue(), replacement.name)
                                mapped = db.list_source_sheets(fr["id"], selected_only=True)
                                expected = mapped["sheet_name"].tolist() if not mapped.empty else []
                                missing = [x for x in expected if x not in incoming_sheets]
                                st.caption(f"Sheet file pengganti: {len(incoming_sheets)} · sheet terpilih/mapping lama: {len(expected)}")
                                if missing:
                                    st.error("Tidak dapat ditimpa karena sheet terpilih berikut hilang: " + ", ".join(missing))
                                elif st.button("Timpa file aktif sekarang", type="primary", key=f"do_replace_{fr['id']}"):
                                    overwrite_uploaded_source(project_id, fr["id"], replacement, replace_label)
                                    flash("success", f"File aktif {fr['filename']} berhasil ditimpa. Mapping sheet dipertahankan.")
                                    st.rerun()
                            except Exception as exc:
                                st.error(f"File pengganti tidak dapat dibaca: {exc}")
                        st.info("Timpa mengganti file aktif dan mempertahankan mapping/histori lama. Untuk dataset register SQLite, jalankan Update Data bila ingin memproses ulang isi tabel dari file pengganti.")

                    with manage_delete:
                        st.warning("Hapus keseluruhan bersifat permanen untuk sumber ini: arsip Excel, seluruh versi, mapping sheet, snapshot, dan dataset yang dimiliki file tersebut akan dihapus.")
                        confirm_delete = st.text_input(
                            f"Ketik nama file untuk konfirmasi: {fr['filename']}",
                            key=f"confirm_delete_source_{fr['id']}",
                        )
                        delete_ok = confirm_delete.strip() == str(fr["filename"]).strip()
                        if st.button(
                            "Hapus keseluruhan file sumber",
                            type="secondary",
                            disabled=not delete_ok,
                            key=f"delete_all_source_{fr['id']}",
                        ):
                            result = db.delete_source_file_completely(fr["id"])
                            flash(
                                "success",
                                f"{result['filename']} dihapus keseluruhan · {result['removed_files']} arsip fisik · "
                                f"{result['removed_datasets']} dataset · {result['removed_snapshots']} snapshot.",
                            )
                            st.rerun()
            st.caption("Versi file dapat dipilih kembali dari menu Visual Sheet. Pengelolaan sumber tersedia melalui opsi Timpa atau Hapus keseluruhan.")
    with t3:
        snaps = db.list_snapshots(project_id=project_id)
        if snaps.empty:
            st.info("Belum ada snapshot data tersimpan.")
        else:
            dash_sheets = db.list_dashboard_sheets(project_id)
            sheet_names = {}
            if not dash_sheets.empty:
                for _, r in dash_sheets.iterrows():
                    sheet_names[r['source_sheet_id']] = f"{r['filename']} · {r.get('display_name') or r['sheet_name']}"
            show = snaps[["id","source_sheet_id","label","file_name","row_count","created_at"]].copy()
            show["Sumber"] = show["source_sheet_id"].map(lambda x: sheet_names.get(x, x or "-"))
            show = show[["id","Sumber","label","file_name","row_count","created_at"]]
            show.columns = ["ID","Sumber","Versi / Log","File Upload","Row","Waktu"]
            st.dataframe(show, use_container_width=True, hide_index=True)
            st.caption("Snapshot tabel dapat dipilih kembali dari Visual Sheet → Data tabel / snapshot SQLite.")
    with t4:
        audit = db.get_audit_log(project_id, limit=500)
        st.dataframe(audit, use_container_width=True, hide_index=True)
    with t5:
        db_size = db.database_size_bytes()
        data_size = _folder_size(DATA_DIR)
        c1, c2 = st.columns(2)
        c1.markdown(metric_card("Ukuran database", fmt_bytes(db_size), str(db.DB_PATH)), unsafe_allow_html=True)
        c2.markdown(metric_card("Total folder data", fmt_bytes(data_size), str(DATA_DIR)), unsafe_allow_html=True)
        if db.DB_PATH.exists():
            st.download_button(
                "Download backup SQLite saja",
                data=db.DB_PATH.read_bytes(),
                file_name=f"project_hub_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db",
                mime="application/octet-stream",
            )
        full_backup = data_folder_backup_bytes(str(DATA_DIR), data_dir_mtime_hint())
        st.download_button(
            "Download FULL BACKUP (database + Excel + foto + logo)",
            data=full_backup,
            file_name=f"HDK_Project_Data_Full_Backup_{datetime.now().strftime('%Y%m%d_%H%M')}.zip",
            mime="application/zip",
            help="Gunakan backup ini bila memindahkan aplikasi/server agar file dan database tetap lengkap.",
        )
        st.info("Backup SQLite saja tidak membawa file JPG/PNG/logo/arsip Excel. Untuk migrasi atau disaster recovery gunakan FULL BACKUP atau copy seluruh folder `data`.")
