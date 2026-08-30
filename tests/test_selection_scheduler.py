from __future__ import annotations

import asyncio
import json
import logging

from xianyu_manager.selection_collection import SelectionCollectionError
from xianyu_manager.selection_scheduler import SchedulerConfig, run_collection_cycle


def test_collection_cycle_runs_search_then_details_serially() -> None:
    events: list[tuple[str, str]] = []

    async def fake_search(keyword, db, *, bridge_token):
        events.append(("search", keyword))
        return {"run_id": f"run-{keyword}", "item_count": 2}

    async def fake_batch(run_id, db, *, bridge_token, limit, interval_seconds, **kwargs):
        events.append(("details", run_id))
        return {"succeeded": limit, "failed": 0, "stopped_code": None}

    config = SchedulerConfig(
        keywords=("skill", "AI工具"),
        detail_limit_per_keyword=2,
        detail_interval_seconds=5,
        keyword_interval_seconds=0,
        daily_times=("09:30",),
        poll_seconds=30,
    )
    result = asyncio.run(
        run_collection_cycle(
            config,
            object(),
            "token",
            logging.getLogger("test-selection-scheduler"),
            search_function=fake_search,
            batch_function=fake_batch,
        )
    )

    assert events == [
        ("search", "skill"),
        ("details", "run-skill"),
        ("search", "AI工具"),
        ("details", "run-AI工具"),
    ]
    assert result["status"] == "ok"
    assert result["keyword_count"] == 2
    assert [item["status"] for item in result["results"]] == ["success", "success"]


def test_collection_cycle_logs_current_collection_count_keys() -> None:
    messages: list[str] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger("test-selection-scheduler-current-keys")
    logger.handlers = [ListHandler()]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    async def fake_search(keyword, db, *, bridge_token):
        return {"run_id": "run-skill", "result_count": 7}

    async def fake_batch(run_id, db, *, bridge_token, limit, interval_seconds, **kwargs):
        return {"success_count": 4, "failed_count": 1, "stopped_code": None}

    config = SchedulerConfig(
        keywords=("skill",),
        detail_limit_per_keyword=5,
        detail_interval_seconds=5,
        keyword_interval_seconds=15,
        daily_times=("09:30",),
        poll_seconds=30,
    )
    asyncio.run(
        run_collection_cycle(
            config,
            object(),
            "token",
            logger,
            search_function=fake_search,
            batch_function=fake_batch,
        )
    )

    events = [json.loads(message) for message in messages]
    search_finished = next(event for event in events if event["event"] == "search_finished")
    details_finished = next(event for event in events if event["event"] == "details_finished")
    assert search_finished["item_count"] == 7
    assert details_finished["succeeded"] == 4
    assert details_finished["failed"] == 1


def test_search_verification_preserves_success_and_skips_remaining_keywords() -> None:
    events: list[tuple[str, str]] = []

    async def fake_search(keyword, db, *, bridge_token):
        events.append(("search", keyword))
        if keyword == "Python自动化":
            raise SelectionCollectionError(
                "run-python",
                "VERIFICATION_REQUIRED",
                "页面要求人工完成平台验证",
            )
        return {"run_id": f"run-{keyword}", "result_count": 2}

    async def fake_batch(run_id, db, *, bridge_token, limit, interval_seconds, **kwargs):
        events.append(("details", run_id))
        return {"success_count": 2, "failed_count": 0, "stopped_code": None}

    config = SchedulerConfig(
        keywords=("skill", "AI工具", "Python自动化", "ComfyUI"),
        detail_limit_per_keyword=2,
        detail_interval_seconds=5,
        keyword_interval_seconds=0,
        daily_times=("09:30",),
        poll_seconds=30,
    )
    result = asyncio.run(
        run_collection_cycle(
            config,
            object(),
            "token",
            logging.getLogger("test-search-verification"),
            search_function=fake_search,
            batch_function=fake_batch,
        )
    )

    assert events == [
        ("search", "skill"),
        ("details", "run-skill"),
        ("search", "AI工具"),
        ("details", "run-AI工具"),
        ("search", "Python自动化"),
    ]
    assert result["status"] == "partial"
    assert result["requires_manual_action"] is True
    assert result["stopped_code"] == "VERIFICATION_REQUIRED"
    assert [item["status"] for item in result["results"]] == [
        "success",
        "success",
        "verification_required",
        "skipped",
    ]
    assert result["results"][-1]["skip_reason"] == "VERIFICATION_REQUIRED"


def test_detail_verification_marks_first_keyword_and_skips_remaining_keywords() -> None:
    events: list[tuple[str, str]] = []

    async def fake_search(keyword, db, *, bridge_token):
        events.append(("search", keyword))
        return {"run_id": f"run-{keyword}", "result_count": 2}

    async def fake_batch(run_id, db, *, bridge_token, limit, interval_seconds, **kwargs):
        events.append(("details", run_id))
        return {
            "success_count": 0,
            "failed_count": 1,
            "stopped_code": "VERIFICATION_REQUIRED",
        }

    config = SchedulerConfig(
        keywords=("Python自动化", "ComfyUI"),
        detail_limit_per_keyword=2,
        detail_interval_seconds=5,
        keyword_interval_seconds=0,
        daily_times=("09:30",),
        poll_seconds=30,
    )
    result = asyncio.run(
        run_collection_cycle(
            config,
            object(),
            "token",
            logging.getLogger("test-detail-verification"),
            search_function=fake_search,
            batch_function=fake_batch,
        )
    )

    assert events == [
        ("search", "Python自动化"),
        ("details", "run-Python自动化"),
    ]
    assert result["status"] == "verification_required"
    assert result["requires_manual_action"] is True
    assert [item["status"] for item in result["results"]] == [
        "verification_required",
        "skipped",
    ]


def test_ordinary_keyword_failure_does_not_stop_later_keywords() -> None:
    events = []

    async def fake_search(keyword, db, *, bridge_token):
        events.append(keyword)
        if keyword == "bad":
            raise SelectionCollectionError("run-bad", "VALUEERROR", "解析失败")
        return {"run_id": f"run-{keyword}", "result_count": 1}

    async def fake_batch(run_id, db, **kwargs):
        return {"success_count": 1, "failed_count": 0, "stopped_code": None}

    config = SchedulerConfig(
        keywords=("good-1", "bad", "good-2"), detail_limit_per_keyword=1,
        detail_interval_seconds=5, keyword_interval_seconds=0,
        daily_times=("09:30",), poll_seconds=30,
    )
    result = asyncio.run(run_collection_cycle(
        config, object(), "token", logging.getLogger("test-keyword-isolation"),
        search_function=fake_search, batch_function=fake_batch,
    ))
    assert events == ["good-1", "bad", "good-2"]
    assert result["status"] == "partial"
    assert [row["status"] for row in result["results"]] == ["success", "failed", "success"]
