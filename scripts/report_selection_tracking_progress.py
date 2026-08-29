from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from xianyu_manager.selection_tracking import build_tracking_progress


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读查看趋势数据冷启动积累进度")
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_ROOT / "data" / "manager.db",
        help="SQLite数据库路径；以只读模式打开",
    )
    return parser.parse_args()


def configure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    configure_utf8_console()
    args = parse_args()
    database_path = args.database.resolve()
    uri = f"file:{database_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        result = build_tracking_progress(connection)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
