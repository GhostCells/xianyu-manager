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
from xianyu_manager.selection_collection import collect_selection_search


async def main(keyword: str) -> None:
    settings = load_settings()
    token = settings.selection_bridge_token_path.read_text(encoding="utf-8").strip()
    result = await collect_selection_search(
        keyword,
        Database(settings.database_path),
        bridge_token=token,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="采集一页闲鱼搜索结果并保存商品主档")
    parser.add_argument("keyword", help="闲鱼搜索关键词，最长40个字符")
    args = parser.parse_args()
    asyncio.run(main(args.keyword))
