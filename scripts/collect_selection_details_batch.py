from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from xianyu_manager.config import load_settings
from xianyu_manager.database import Database
from xianyu_manager.selection_batch_collection import collect_selection_details_batch


async def main(run_id: str, limit: int, interval: float) -> None:
    settings = load_settings()
    token = settings.selection_bridge_token_path.read_text(encoding="utf-8").strip()
    result = await collect_selection_details_batch(
        run_id,
        Database(settings.database_path),
        bridge_token=token,
        limit=limit,
        interval_seconds=interval,
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="串行补采指定搜索任务的候选商品详情")
    parser.add_argument("run_id", help="selection_search_runs.run_id")
    parser.add_argument("--limit", type=int, default=10, help="单次最多补采数量，最大20")
    parser.add_argument("--interval", type=float, default=5, help="相邻详情请求间隔秒数")
    args = parser.parse_args()
    asyncio.run(main(args.run_id, args.limit, args.interval))
