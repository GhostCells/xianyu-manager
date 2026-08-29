from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from xianyu_manager.config import load_settings
from xianyu_manager.database import Database
from xianyu_manager.selection_rule_scoring import SCORE_VERSION, score_selection_run


def configure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="同关键词、同批次候选商品规则排序")
    parser.add_argument("run_id", help="selection_search_runs.run_id")
    parser.add_argument("--database", type=Path, help="可选：指定数据库文件")
    parser.add_argument("--version", default=SCORE_VERSION)
    parser.add_argument("--limit", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    configure_utf8_console()
    args = parse_args()
    settings = load_settings()
    database = Database(args.database or settings.database_path)
    report = score_selection_run(database, args.run_id, score_version=args.version)
    report["items"] = report["items"][: max(1, args.limit)]
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)

