from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .database import Database
from .runtime_policy import PROCESS_POLICY
from .selection_batch_collection import collect_selection_details_batch
from .selection_collection import SelectionCollectionError, collect_selection_search


MAX_KEYWORDS = 10
MAX_DETAILS_PER_KEYWORD = 20
MIN_DETAIL_INTERVAL_SECONDS = 5
MIN_KEYWORD_INTERVAL_SECONDS = 15


@dataclass(frozen=True)
class SchedulerConfig:
    keywords: tuple[str, ...]
    detail_limit_per_keyword: int
    detail_interval_seconds: float
    keyword_interval_seconds: float
    daily_times: tuple[str, ...]
    poll_seconds: int
    tracking_min_interval_hours: float = 20.0
    tracking_timezone: str = "Asia/Shanghai"
    tracking_budget_ratio: float = 0.4


def load_scheduler_config(path: Path) -> SchedulerConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    keywords = tuple(dict.fromkeys(str(value).strip() for value in raw.get("keywords", []) if str(value).strip()))
    if not keywords:
        raise ValueError("keywords 不能为空")
    if len(keywords) > MAX_KEYWORDS:
        raise ValueError(f"keywords 最多允许 {MAX_KEYWORDS} 个")
    if any(len(keyword) > 40 for keyword in keywords):
        raise ValueError("单个关键词不能超过 40 个字符")

    detail_limit = int(raw.get("detail_limit_per_keyword", 10))
    if not 1 <= detail_limit <= MAX_DETAILS_PER_KEYWORD:
        raise ValueError(f"detail_limit_per_keyword 必须在 1-{MAX_DETAILS_PER_KEYWORD} 之间")

    detail_interval = float(raw.get("detail_interval_seconds", 8))
    if detail_interval < MIN_DETAIL_INTERVAL_SECONDS:
        raise ValueError(f"detail_interval_seconds 不能低于 {MIN_DETAIL_INTERVAL_SECONDS}")

    keyword_interval = float(raw.get("keyword_interval_seconds", 30))
    if keyword_interval < MIN_KEYWORD_INTERVAL_SECONDS:
        raise ValueError(f"keyword_interval_seconds 不能低于 {MIN_KEYWORD_INTERVAL_SECONDS}")

    daily_times = tuple(dict.fromkeys(str(value).strip() for value in raw.get("daily_times", []) if str(value).strip()))
    for value in daily_times:
        datetime.strptime(value, "%H:%M")

    poll_seconds = int(raw.get("poll_seconds", 30))
    if not 10 <= poll_seconds <= 300:
        raise ValueError("poll_seconds 必须在 10-300 之间")

    tracking_min_interval_hours = float(raw.get("tracking_min_interval_hours", 20))
    if not 1 <= tracking_min_interval_hours <= 168:
        raise ValueError("tracking_min_interval_hours 必须在 1-168 之间")
    tracking_timezone = str(raw.get("tracking_timezone", "Asia/Shanghai")).strip()
    try:
        ZoneInfo(tracking_timezone)
    except ZoneInfoNotFoundError as exc:
        if tracking_timezone != "Asia/Shanghai":
            raise ValueError("tracking_timezone 无效") from exc
    tracking_budget_ratio = float(raw.get("tracking_budget_ratio", 0.4))
    if not 0 <= tracking_budget_ratio <= 1:
        raise ValueError("tracking_budget_ratio 必须在0到1之间")

    return SchedulerConfig(
        keywords=keywords,
        detail_limit_per_keyword=detail_limit,
        detail_interval_seconds=detail_interval,
        keyword_interval_seconds=keyword_interval,
        daily_times=daily_times,
        poll_seconds=poll_seconds,
        tracking_min_interval_hours=tracking_min_interval_hours,
        tracking_timezone=tracking_timezone,
        tracking_budget_ratio=tracking_budget_ratio,
    )


def configure_scheduler_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("selection_scheduler")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def _log_event(logger: logging.Logger, event: str, **details: Any) -> None:
    logger.info(json.dumps({"event": event, **details}, ensure_ascii=False, default=str))


def _result_count(result: dict[str, Any], current_key: str, legacy_key: str) -> int:
    """Read collection counts while keeping older scheduler test doubles compatible."""
    value = result.get(current_key)
    if value is None:
        value = result.get(legacy_key)
    return int(value or 0)


def _mark_verification_required(
    summary: dict[str, Any],
    *,
    logger: logging.Logger,
    cycle_id: str,
    keywords: tuple[str, ...],
    current_index: int,
    code: str,
) -> None:
    """Finish the cycle without making more requests on the affected browser context."""
    summary["status"] = "partial" if current_index > 0 else "verification_required"
    summary["requires_manual_action"] = True
    summary["stopped_code"] = code
    for pending_keyword in keywords[current_index + 1:]:
        summary["results"].append(
            {
                "keyword": pending_keyword,
                "status": "skipped",
                "skip_reason": code,
            }
        )
        _log_event(
            logger,
            "keyword_skipped",
            cycle_id=cycle_id,
            keyword=pending_keyword,
            reason=code,
        )


async def run_collection_cycle(
    config: SchedulerConfig,
    db: Database,
    bridge_token: str,
    logger: logging.Logger,
    *,
    search_function: Callable[..., Awaitable[dict[str, Any]]] = collect_selection_search,
    batch_function: Callable[..., Awaitable[dict[str, Any]]] = collect_selection_details_batch,
) -> dict[str, Any]:
    PROCESS_POLICY.require_business()
    cycle_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    summary: dict[str, Any] = {
        "cycle_id": cycle_id,
        "status": "ok",
        "keyword_count": len(config.keywords),
        "results": [],
    }
    _log_event(logger, "cycle_started", cycle_id=cycle_id, keywords=list(config.keywords))

    for index, keyword in enumerate(config.keywords):
        keyword_result: dict[str, Any] = {"keyword": keyword, "status": "running"}
        try:
            _log_event(logger, "search_started", cycle_id=cycle_id, keyword=keyword)
            # bridge_token is keyword-only in the collection layer.
            search_result = await search_function(keyword, db, bridge_token=bridge_token)
            run_id = search_result["run_id"]
            keyword_result["search"] = search_result
            _log_event(
                logger,
                "search_finished",
                cycle_id=cycle_id,
                keyword=keyword,
                run_id=run_id,
                item_count=_result_count(search_result, "result_count", "item_count"),
            )

            batch_result = await batch_function(
                run_id,
                db,
                bridge_token=bridge_token,
                limit=config.detail_limit_per_keyword,
                interval_seconds=config.detail_interval_seconds,
                tracking_min_interval_hours=config.tracking_min_interval_hours,
                tracking_timezone=config.tracking_timezone,
                tracking_budget_ratio=config.tracking_budget_ratio,
            )
            keyword_result["details"] = batch_result
            _log_event(
                logger,
                "details_finished",
                cycle_id=cycle_id,
                keyword=keyword,
                run_id=run_id,
                succeeded=_result_count(batch_result, "success_count", "succeeded"),
                failed=_result_count(batch_result, "failed_count", "failed"),
                selected_new=int(batch_result.get("selected_new_count") or 0),
                selected_tracking=int(batch_result.get("selected_tracking_count") or 0),
                skipped=batch_result.get("skip_counts") or {},
                unused_budget=max(
                    0,
                    config.detail_limit_per_keyword
                    - int(batch_result.get("processed_count") or 0),
                ),
                stopped_code=batch_result.get("stopped_code"),
            )
            if batch_result.get("stopped_code"):
                stopped_code = str(batch_result["stopped_code"])
                keyword_result["status"] = (
                    "verification_required"
                    if stopped_code == "VERIFICATION_REQUIRED"
                    else "stopped"
                )
                summary["results"].append(keyword_result)
                if stopped_code == "VERIFICATION_REQUIRED":
                    _mark_verification_required(
                        summary,
                        logger=logger,
                        cycle_id=cycle_id,
                        keywords=config.keywords,
                        current_index=index,
                        code=stopped_code,
                    )
                else:
                    summary["status"] = "stopped"
                break
            keyword_result["status"] = "success"
        except SelectionCollectionError as exc:
            keyword_result["error"] = {"code": exc.code, "message": str(exc)}
            blocking_codes = {
                "VERIFICATION_REQUIRED", "LOGIN_REQUIRED",
                "BROWSER_NOT_RUNNING", "SELECTION_BUSY",
            }
            keyword_result["status"] = "verification_required" if exc.code == "VERIFICATION_REQUIRED" else (
                "stopped" if exc.code in blocking_codes else "failed"
            )
            _log_event(logger, "collection_stopped", cycle_id=cycle_id, keyword=keyword, code=exc.code, message=str(exc))
            summary["results"].append(keyword_result)
            if exc.code == "VERIFICATION_REQUIRED":
                _mark_verification_required(
                    summary,
                    logger=logger,
                    cycle_id=cycle_id,
                    keywords=config.keywords,
                    current_index=index,
                    code=exc.code,
                )
            elif exc.code in blocking_codes:
                summary["status"] = "stopped"
            else:
                summary["status"] = "partial"
                if index < len(config.keywords) - 1:
                    await asyncio.sleep(config.keyword_interval_seconds)
                continue
            break
        except Exception as exc:
            keyword_result["error"] = {"code": type(exc).__name__, "message": str(exc)}
            keyword_result["status"] = "failed"
            summary["status"] = "partial"
            _log_event(logger, "keyword_failed", cycle_id=cycle_id, keyword=keyword, code=type(exc).__name__, message=str(exc))

        summary["results"].append(keyword_result)
        if index < len(config.keywords) - 1:
            await asyncio.sleep(config.keyword_interval_seconds)

    _log_event(logger, "cycle_finished", cycle_id=cycle_id, status=summary["status"])
    return summary


class SchedulerInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: Any = None

    def __enter__(self) -> "SchedulerInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        self._file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self._file.close()
            self._file = None
            raise RuntimeError("已有选品采集调度器正在运行") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._file is None:
            return
        self._file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def load_scheduler_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_scheduler_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)
