from __future__ import annotations

import argparse
from pathlib import Path

from src import publish

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build read-only web data from Current Published project packages.")
    parser.add_argument("--publish-root", default="", help="Folder Published. Empty = use saved app setting.")
    parser.add_argument("--target-data", default=str(DATA_DIR / "web_preview"), help="Target data folder for web/read-only app.")
    args = parser.parse_args()

    root = Path(args.publish_root).expanduser() if args.publish_root.strip() else publish.load_publish_root(DATA_DIR)
    target = Path(args.target_data).expanduser()
    print(f"Publish root : {root}")
    print(f"Target data  : {target}")
    result = publish.rebuild_live_from_publish_root(root, target)
    print(f"SYNC OK · {result['project_count']} project")
    for p in result["projects"]:
        print(f" - {p.get('project_code')} · {p.get('project_name')} · {p.get('version')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
