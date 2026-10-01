from __future__ import annotations

import filecmp
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any


def _safe_name(value: str, fallback: str = "PROJECT") -> str:
    text = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(value or "").strip())
    text = text.strip("_-")
    return (text or fallback)[:80]


def projects_root(data_dir: Path) -> Path:
    root = Path(data_dir) / "projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def project_dir(data_dir: Path, project_id: str, project_code: str = "", project_name: str = "") -> Path:
    """Return stable per-project folder keyed by immutable project_id."""
    root = projects_root(Path(data_dir))
    suffix = f"__{project_id}"
    for child in root.iterdir():
        if child.is_dir() and child.name.endswith(suffix):
            ensure_project_tree(child)
            return child
    prefix = _safe_name(project_code or project_name or "PROJECT")
    folder = root / f"{prefix}{suffix}"
    ensure_project_tree(folder)
    return folder


def ensure_project_tree(folder: Path) -> dict[str, Path]:
    folder = Path(folder)
    paths = {
        "root": folder,
        "source": folder / "source",
        "excel": folder / "source" / "excel",
        "photos": folder / "photos",
        "bim": folder / "bim",
        "assets": folder / "assets",
        "snapshots": folder / "snapshots",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return paths


def project_paths(data_dir: Path, project_id: str, project_code: str = "", project_name: str = "") -> dict[str, Path]:
    return ensure_project_tree(project_dir(data_dir, project_id, project_code, project_name))


def source_excel_dir(data_dir: Path, project_id: str, project_code: str = "", project_name: str = "") -> Path:
    return project_paths(data_dir, project_id, project_code, project_name)["excel"]


def photo_dir(data_dir: Path, project_id: str, project_code: str = "", project_name: str = "") -> Path:
    return project_paths(data_dir, project_id, project_code, project_name)["photos"]


def bim_dir(data_dir: Path, project_id: str, project_code: str = "", project_name: str = "") -> Path:
    return project_paths(data_dir, project_id, project_code, project_name)["bim"]


def asset_dir(data_dir: Path, project_id: str, project_code: str = "", project_name: str = "") -> Path:
    return project_paths(data_dir, project_id, project_code, project_name)["assets"]


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except Exception:
        return False


def to_stored_path(path: str | Path, data_dir: Path) -> str:
    """Return a portable DB path.

    Anything inside ``data_dir`` is stored relative to that directory, e.g.
    ``projects/5HDK10__abc/photos/x.jpg``.  This keeps SQLite valid when the
    whole application folder moves from one version/location to another.
    """
    if path is None or str(path).strip() == "":
        return ""
    p = Path(path)
    try:
        rel = p.resolve(strict=False).relative_to(Path(data_dir).resolve(strict=False))
        return rel.as_posix()
    except Exception:
        return str(p)


def resolve_stored_path(raw: str | Path, data_dir: Path) -> Path:
    """Resolve portable/legacy DB path to a filesystem path."""
    text = str(raw or "").strip().strip('"').strip("'")
    if not text or text in {".", "./", ".\\"}:
        # Never return Path("") here: pathlib interprets it as the current
        # directory ("."), which may exist and later be mistaken for a file.
        return Path(data_dir) / "__missing_asset__"
    p = Path(text)
    if p.is_absolute():
        return p
    norm = text.replace("\\", "/")
    if norm.startswith("data/"):
        return Path(data_dir).parent / Path(norm)
    return Path(data_dir) / Path(norm)


# Backwards-compatible private alias used by older code/tests.
def _resolve_path(raw: str, data_dir: Path) -> Path:
    return resolve_stored_path(raw, data_dir)


def _move_file(source: Path, destination: Path) -> Path:
    source = Path(source)
    destination = Path(destination)
    if not source.exists() or not source.is_file():
        return source
    if source.resolve() == destination.resolve():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        try:
            if filecmp.cmp(source, destination, shallow=False):
                source.unlink(missing_ok=True)
                return destination
        except OSError:
            pass
        stem, suffix = destination.stem, destination.suffix
        idx = 2
        while destination.exists():
            destination = destination.with_name(f"{stem}_{idx}{suffix}")
            idx += 1
    shutil.move(str(source), str(destination))
    return destination


def _copy_file(source: Path, destination: Path) -> Path:
    source = Path(source)
    destination = Path(destination)
    if not source.exists() or not source.is_file():
        return source
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        try:
            if filecmp.cmp(source, destination, shallow=False):
                return destination
        except OSError:
            pass
        stem, suffix = destination.stem, destination.suffix
        idx = 2
        while destination.exists():
            destination = destination.with_name(f"{stem}_{idx}{suffix}")
            idx += 1
    shutil.copy2(source, destination)
    return destination


def _data_suffix_from_raw(raw: str) -> list[str]:
    norm = str(raw or "").replace("\\", "/")
    parts = [part for part in norm.split("/") if part not in {"", "."}]
    idxs = [i for i, part in enumerate(parts) if part.lower() == "data"]
    if not idxs:
        return []
    return parts[idxs[-1] + 1 :]


def sibling_data_roots(data_dir: Path) -> list[Path]:
    """Find data roots of sibling extracted app versions.

    Only immediate siblings of the current app directory are inspected, keeping
    recovery bounded and avoiding broad disk scans.
    """
    data_dir = Path(data_dir)
    app_dir = data_dir.parent
    parent = app_dir.parent
    roots: list[Path] = []
    try:
        for child in parent.iterdir():
            if not child.is_dir() or child.resolve(strict=False) == app_dir.resolve(strict=False):
                continue
            candidate = child / "data"
            if candidate.is_dir():
                roots.append(candidate)
    except OSError:
        return []
    # Prefer HDK Data Hub-looking folders and newest names first.
    roots.sort(key=lambda p: ("hdk" not in p.parent.name.lower(), "data_hub" not in p.parent.name.lower(), p.parent.name), reverse=False)
    return roots[:40]


def find_existing_file(raw: str | Path, data_dir: Path, project_id: str = "") -> tuple[Path | None, str]:
    """Resolve current/legacy/stale path without mixing projects.

    Returns ``(path, recovery_note)``.  If the SQLite path points to a previous
    extracted build, sibling app folders are checked using the exact ``data/...``
    suffix first, then the same project's isolated directory by filename.
    """
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        return None, ""

    candidates: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        key = os.path.normcase(os.path.normpath(str(p)))
        if key not in seen:
            seen.add(key)
            candidates.append(p)

    add(resolve_stored_path(text, data_dir))

    suffix = _data_suffix_from_raw(text)
    if suffix:
        add(Path(data_dir).joinpath(*suffix))

    filename = Path(text.replace("\\", "/")).name
    project_root: Path | None = None
    if project_id:
        root = Path(data_dir) / "projects"
        if root.exists():
            try:
                for child in root.iterdir():
                    if child.is_dir() and child.name.endswith(f"__{project_id}"):
                        project_root = child
                        break
            except OSError:
                pass
    if project_root and filename:
        try:
            for match in project_root.rglob(filename):
                add(match)
        except OSError:
            pass

    # Previous extracted versions beside the current app.
    for old_data in sibling_data_roots(data_dir):
        if suffix:
            add(old_data.joinpath(*suffix))
        if project_id and filename:
            old_projects = old_data / "projects"
            if old_projects.exists():
                try:
                    for old_project in old_projects.iterdir():
                        if not old_project.is_dir() or not old_project.name.endswith(f"__{project_id}"):
                            continue
                        exact_groups = [old_project / "photos", old_project / "bim", old_project / "assets", old_project / "source" / "excel"]
                        for group in exact_groups:
                            if group.exists():
                                for match in group.rglob(filename):
                                    add(match)
                        break
                except OSError:
                    pass

    original = candidates[0] if candidates else None
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                note = ""
                if original is not None and os.path.normcase(os.path.normpath(str(candidate))) != os.path.normcase(os.path.normpath(str(original))):
                    note = f"Path lama dipulihkan: {text} -> {candidate}"
                return candidate, note
        except OSError:
            continue
    return None, ""


def _materialize_into_current(source: Path, destination: Path, data_dir: Path) -> tuple[Path, str]:
    """Move legacy files inside current data; copy files from old app versions."""
    source = Path(source)
    if not source.exists() or not source.is_file():
        return source, "missing"
    if source.resolve(strict=False) == destination.resolve(strict=False):
        return destination, "same"
    if _is_under(source, data_dir):
        return _move_file(source, destination), "moved"
    return _copy_file(source, destination), "copied"


def _prune_empty(path: Path) -> None:
    path = Path(path)
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_dir():
            try:
                child.rmdir()
            except OSError:
                pass
    try:
        path.rmdir()
    except OSError:
        pass


def migrate_legacy_layout(db_path: Path, data_dir: Path) -> dict[str, Any]:
    """Isolate project files, recover stale paths, and rewrite SQLite paths portable.

    The migration is idempotent.  Files from an older sibling app version are
    copied into the current project's folder (never removed from the older build).
    Paths inside the current ``data`` folder are stored relative to ``data``.
    """
    db_path = Path(db_path)
    data_dir = Path(data_dir)
    result = {
        "moved": 0,
        "copied_from_old_build": 0,
        "recovered": 0,
        "missing": [],
        "updated_paths": 0,
        "projects": 0,
        "errors": [],
    }
    if not db_path.exists():
        return result

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        projects = conn.execute("SELECT id, code, name FROM projects ORDER BY code").fetchall()
        result["projects"] = len(projects)
        for project in projects:
            pid = str(project["id"])
            code = str(project["code"] or "")
            name = str(project["name"] or "")
            paths = project_paths(data_dir, pid, code, name)
            project_root = paths["root"]

            path_map: dict[str, str] = {}
            try:
                rows = conn.execute("SELECT id, source_file_id, file_path FROM source_versions WHERE project_id=?", (pid,)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for row in rows:
                raw = str(row["file_path"] or "").strip()
                if not raw:
                    continue
                try:
                    src, note = find_existing_file(raw, data_dir, pid)
                    if src is None:
                        result["missing"].append(raw)
                        continue
                    if note:
                        result["recovered"] += 1
                    if _is_under(src, project_root):
                        final = src
                        action = "same"
                    else:
                        dest = paths["excel"] / _safe_name(str(row["source_file_id"] or "source"), "source") / src.name
                        final, action = _materialize_into_current(src, dest, data_dir)
                    if action == "moved": result["moved"] += 1
                    if action == "copied": result["copied_from_old_build"] += 1
                    stored = to_stored_path(final, data_dir)
                    if stored != raw:
                        conn.execute("UPDATE source_versions SET file_path=? WHERE id=?", (stored, row["id"]))
                        result["updated_paths"] += 1
                    path_map[raw] = stored
                except Exception as exc:
                    result["errors"].append(f"source_versions:{row['id']}: {exc}")

            try:
                rows = conn.execute("SELECT id, current_file_path FROM source_files WHERE project_id=?", (pid,)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for row in rows:
                raw = str(row["current_file_path"] or "").strip()
                if not raw:
                    continue
                try:
                    if raw in path_map:
                        stored = path_map[raw]
                    else:
                        src, note = find_existing_file(raw, data_dir, pid)
                        if src is None:
                            result["missing"].append(raw)
                            continue
                        if note: result["recovered"] += 1
                        if _is_under(src, project_root):
                            final = src
                            action = "same"
                        else:
                            dest = paths["excel"] / _safe_name(str(row["id"] or "source"), "source") / src.name
                            final, action = _materialize_into_current(src, dest, data_dir)
                        if action == "moved": result["moved"] += 1
                        if action == "copied": result["copied_from_old_build"] += 1
                        stored = to_stored_path(final, data_dir)
                    if stored != raw:
                        conn.execute("UPDATE source_files SET current_file_path=? WHERE id=?", (stored, row["id"]))
                        result["updated_paths"] += 1
                except Exception as exc:
                    result["errors"].append(f"source_files:{row['id']}: {exc}")

            try:
                rows = conn.execute("SELECT id, file_path, media_type, category FROM photos WHERE project_id=?", (pid,)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for row in rows:
                raw = str(row["file_path"] or "").strip()
                if not raw:
                    continue
                try:
                    src, note = find_existing_file(raw, data_dir, pid)
                    if src is None:
                        result["missing"].append(raw)
                        continue
                    if note: result["recovered"] += 1
                    mt = str(row["media_type"] or "").lower()
                    cat = str(row["category"] or "").lower()
                    target_base = paths["bim"] if (mt.startswith("bim") or "bim" in mt or "bim" in cat) else paths["photos"]
                    if _is_under(src, target_base):
                        final = src
                        action = "same"
                    else:
                        final, action = _materialize_into_current(src, target_base / src.name, data_dir)
                    if action == "moved": result["moved"] += 1
                    if action == "copied": result["copied_from_old_build"] += 1
                    stored = to_stored_path(final, data_dir)
                    if stored != raw:
                        conn.execute("UPDATE photos SET file_path=? WHERE id=?", (stored, row["id"]))
                        result["updated_paths"] += 1
                except Exception as exc:
                    result["errors"].append(f"photos:{row['id']}: {exc}")

            try:
                p = conn.execute(
                    "SELECT logo_owner_path, logo_consultant_planner_path, logo_consultant_path, logo_contractor_path FROM projects WHERE id=?",
                    (pid,),
                ).fetchone()
            except sqlite3.OperationalError:
                p = None
            if p:
                for col in ["logo_owner_path", "logo_consultant_planner_path", "logo_consultant_path", "logo_contractor_path"]:
                    raw = str(p[col] or "").strip()
                    if not raw:
                        continue
                    try:
                        src, note = find_existing_file(raw, data_dir, pid)
                        if src is None:
                            result["missing"].append(raw)
                            continue
                        if note: result["recovered"] += 1
                        if _is_under(src, paths["assets"]):
                            final = src
                            action = "same"
                        else:
                            final, action = _materialize_into_current(src, paths["assets"] / src.name, data_dir)
                        if action == "moved": result["moved"] += 1
                        if action == "copied": result["copied_from_old_build"] += 1
                        stored = to_stored_path(final, data_dir)
                        if stored != raw:
                            conn.execute(f'UPDATE projects SET "{col}"=? WHERE id=?', (stored, pid))
                            result["updated_paths"] += 1
                    except Exception as exc:
                        result["errors"].append(f"projects:{pid}:{col}: {exc}")

        conn.commit()
    finally:
        conn.close()

    result["missing"] = sorted(set(result["missing"]))
    for legacy in [data_dir / "uploads" / "excel", data_dir / "photos", data_dir / "project_assets"]:
        _prune_empty(legacy)
    return result
