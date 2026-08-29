from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from xianyu_manager.config import load_settings
from xianyu_manager.database import Database
from xianyu_manager.selection_scheduler import (
    SchedulerInstanceLock,
    configure_scheduler_logging,
    load_scheduler_config,
    load_scheduler_state,
    run_collection_cycle,
    save_scheduler_state,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="闲鱼选品采集调度器")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="立即手动执行一轮后退出")
    mode.add_argument("--daemon", action="store_true", help="按配置的每日时刻持续调度")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "selection-schedule.json")
    parser.add_argument("--keyword", action="append", help="手动模式临时覆盖关键词，可重复提供")
    parser.add_argument("--detail-limit", type=int, help="手动模式临时覆盖每个关键词详情数量")
    return parser.parse_args()


async def run_once(config, db, token, logger):
    return await run_collection_cycle(config, db, token, logger)


async def run_daemon(config, db, token, logger, state_path: Path) -> None:
    if not config.daily_times:
        raise ValueError("定时模式必须配置 daily_times")
    while True:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        current_time = now.strftime("%H:%M")
        state = load_scheduler_state(state_path)
        if current_time in config.daily_times and state.get(current_time) != today:
            await run_collection_cycle(config, db, token, logger)
            state[current_time] = today
            save_scheduler_state(state_path, state)
        await asyncio.sleep(config.poll_seconds)


def main() -> int:
    args = parse_args()
    config = load_scheduler_config(args.config)
    if args.keyword:
        config = replace(config, keywords=tuple(dict.fromkeys(value.strip() for value in args.keyword if value.strip())))
    if args.detail_limit is not None:
        if not 1 <= args.detail_limit <= 20:
            raise ValueError("--detail-limit 必须在 1-20 之间")
        config = replace(config, detail_limit_per_keyword=args.detail_limit)

    settings = load_settings()
    token = settings.selection_bridge_token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("未配置 SELECTION_BRIDGE_TOKEN")

    db = Database(settings.database_path)
    logger = configure_scheduler_logging(PROJECT_ROOT / "data" / "selection-scheduler.log")
    lock_path = PROJECT_ROOT / "data" / "selection-scheduler.lock"
    state_path = PROJECT_ROOT / "data" / "selection-scheduler-state.json"
    with SchedulerInstanceLock(lock_path):
        if args.once:
            result = asyncio.run(run_once(config, db, token, logger))
            print(json.dumps(result, ensure_ascii=True, indent=2, default=str))
        else:
            asyncio.run(run_daemon(config, db, token, logger, state_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
