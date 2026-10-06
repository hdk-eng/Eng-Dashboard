from __future__ import annotations

import sqlite3
from pathlib import Path

from src import db, publish


def _fresh_db(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "project_hub.db"
    db.DB_PATH = path
    db.init_db()
    return path


def test_reporting_history_and_password_reset(tmp_path):
    db_path = _fresh_db(tmp_path)
    project_id = db.create_project("TEST01", "Project Test")

    user_id = db.create_user(
        "owner.test",
        "owner.test@example.com",
        "Owner Test",
        "owner",
        "PasswordAwal1",
        [project_id],
        True,
    )

    temporary = db.admin_reset_password(user_id, actor="local-admin")
    assert len(temporary) >= 8

    auth = db.authenticate_user("owner.test", temporary)
    assert auth is not None
    assert int(auth.get("must_change_password") or 0) == 1

    db.change_own_password(user_id, temporary, "PasswordBaru2")
    auth2 = db.authenticate_user("owner.test", "PasswordBaru2")
    assert auth2 is not None
    assert int(auth2.get("must_change_password") or 0) == 0

    rev0 = db.create_reporting_period(
        project_id,
        "September 2026",
        "2026-09-30",
        note="Rev 0",
        created_by="local-admin",
    )
    db.publish_reporting_period(rev0, "local-admin")

    rev1 = db.create_reporting_period(
        project_id,
        "September 2026",
        "2026-09-30",
        note="Rev 1",
        created_by="local-admin",
    )
    db.publish_reporting_period(rev1, "local-admin")

    history = db.list_reporting_periods(project_id)
    same_period = history[history["period_date"] == "2026-09-30"]
    assert sorted(same_period["revision_no"].astype(int).tolist()) == [0, 1]
    assert set(same_period["status"].astype(str)) == {"published"}

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT revision_no,status,published_by FROM reporting_periods "
            "WHERE project_id=? ORDER BY revision_no",
            (project_id,),
        ).fetchall()
    assert rows == [(0, "published", "local-admin"), (1, "published", "local-admin")]


def test_publish_keeps_reporting_periods(tmp_path):
    db_path = _fresh_db(tmp_path)
    data_dir = db_path.parent
    project_id = db.create_project("TEST02", "Project Publish Test")

    report_id = db.create_reporting_period(
        project_id,
        "September 2026",
        "2026-09-30",
        created_by="local-admin",
    )
    db.publish_reporting_period(report_id, "local-admin")

    publish_root = tmp_path / "published"
    manifest = publish.publish_project(db_path, data_dir, project_id, publish_root)
    bundle_db = Path(manifest["version_dir"]) / "project_hub.db"

    with sqlite3.connect(bundle_db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM reporting_periods WHERE project_id=?",
            (project_id,),
        ).fetchone()[0]
        status = conn.execute(
            "SELECT status FROM reporting_periods WHERE id=?",
            (report_id,),
        ).fetchone()[0]

    assert count == 1
    assert status == "published"
