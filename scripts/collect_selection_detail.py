from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from xianyu_manager.config import load_settings


def main(target: str) -> None:
    settings = load_settings()
    token = settings.selection_bridge_token_path.read_text(encoding="utf-8").strip()
    body = {"url": target} if target.startswith(("http://", "https://")) else {"item_id": target}
    response = httpx.post(
        "http://127.0.0.1:8765/api/internal/selection/detail",
        headers={"X-Internal-Token": token},
        json=body,
        timeout=50,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="补采一个闲鱼候选商品详情并保存快照")
    parser.add_argument("target", help="候选商品 item_id 或闲鱼商品链接")
    args = parser.parse_args()
    main(args.target)
