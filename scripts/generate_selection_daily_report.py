from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from xianyu_manager.config import load_settings
from xianyu_manager.selection_observation import CHINA_TIMEZONE, write_daily_markdown


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成闲鱼选品数据观察日报")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=datetime.now(CHINA_TIMEZONE).date(),
        help="北京时间日期 YYYY-MM-DD",
    )
    parser.add_argument("--database", type=Path, help="可选：指定数据库文件")
    parser.add_argument("--output", type=Path, help="可选：指定 Markdown 输出路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings()
    output = args.output or PROJECT_ROOT / "docs" / f"selection-daily-{args.date.isoformat()}.md"
    report = write_daily_markdown(args.database or settings.database_path, args.date, output)
    print(json.dumps({
        "ok": True,
        "date": report["date"],
        "output": str(output.resolve()),
        "search_runs": report["collection"]["run_count"],
        "search_results": report["collection"]["result_count"],
        "detail_success": report["collection"]["detail_success"],
        "candidate_count": len(report["candidates"]),
    }, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
