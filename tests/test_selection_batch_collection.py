from __future__ import annotations

import asyncio
import json

import httpx

from xianyu_manager.database import Database
from xianyu_manager.selection_batch_collection import collect_selection_details_batch


def test_batch_collection_is_serial_and_skips_successful_snapshots(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.start_selection_search_run("run-1", "skill")
    database.complete_selection_search_run(
        "run-1",
        [
            {
                "item_id": str(item_id),
                "title": f"商品{item_id}",
                "url": f"https://www.goofish.com/item?id={item_id}",
            }
            for item_id in (1001, 1002, 1003)
        ],
    )
    database.save_selection_item_snapshot(
        "1001",
        price_cents=990,
        price_text="9.90",
        want_count=10,
        browse_count=100,
        collect_count=5,
    )
    request_order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        item_id = str(request.content.decode("utf-8")).split('"item_id":"', 1)[1].split('"', 1)[0]
        request_order.append(item_id)
        snapshot = database.save_selection_item_snapshot(
            item_id,
            price_cents=1990,
            price_text="19.90",
            want_count=20,
            browse_count=200,
            collect_count=8,
        )
        return httpx.Response(200, json={"ok": True, "snapshot": snapshot})

    async def collect() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_selection_details_batch(
                "run-1",
                database,
                bridge_token="token",
                limit=10,
                interval_seconds=0,
                detail_url="http://detail.test/collect",
                client=client,
            )

    result = asyncio.run(collect())
    assert request_order == ["1002", "1003"]
    assert result["processed_count"] == 2
    assert result["success_count"] == 2
    with database.connect() as connection:
        snapshots = connection.execute(
            "SELECT item_id, detail_status FROM selection_item_snapshots ORDER BY item_id"
        ).fetchall()
    assert [(row["item_id"], row["detail_status"]) for row in snapshots] == [
        ("1001", "success"),
        ("1002", "success"),
        ("1003", "success"),
    ]


def test_batch_mixes_due_tracking_and_new_without_exceeding_limit(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.start_selection_search_run("old-run", "skill")
    database.complete_selection_search_run(
        "old-run",
        [{"item_id": "tracked", "title": "追踪商品", "url": "https://example.test/tracked"}],
    )
    database.save_selection_item_snapshot(
        "tracked", price_cents=100, price_text="1", want_count=1,
        browse_count=10, collect_count=1,
    )
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE selection_item_snapshots
            SET observed_at='2026-08-20 00:00:00',
                detail_observed_at='2026-08-20 00:00:00'
            WHERE run_id='old-run'
            """
        )

    database.start_selection_search_run("current-run", "skill")
    database.complete_selection_search_run(
        "current-run",
        [
            {"item_id": "tracked", "title": "追踪商品", "url": "https://example.test/tracked"},
            *[
                {"item_id": f"new-{index}", "title": f"新商品{index}", "url": f"https://example.test/new-{index}"}
                for index in range(1, 15)
            ],
        ],
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        item_id = json.loads(request.content)["item_id"]
        requested.append(item_id)
        snapshot = database.save_selection_item_snapshot(
            item_id, price_cents=100, price_text="1", want_count=2,
            browse_count=20, collect_count=2,
        )
        return httpx.Response(200, json={"ok": True, "snapshot": snapshot})

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_selection_details_batch(
                "current-run", database, bridge_token="token", limit=10,
                interval_seconds=0, detail_url="http://detail.test/collect", client=client,
            )

    result = asyncio.run(collect())
    assert len(requested) == 10
    assert "tracked" in requested
    assert result["selected_tracking_count"] == 1
    assert result["selected_new_count"] == 9
    assert result["processed_count"] <= result["requested_limit"] == 10


def test_cross_keyword_recent_success_is_globally_deduplicated_but_rank_is_saved(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.start_selection_search_run("skill-run", "skill")
    database.complete_selection_search_run(
        "skill-run",
        [{"item_id": "shared", "title": "共享商品", "url": "https://example.test/shared"}],
    )
    database.save_selection_item_snapshot(
        "shared", price_cents=100, price_text="1", want_count=1,
        browse_count=10, collect_count=1,
    )
    database.start_selection_search_run("ai-run", "AI工具")
    database.complete_selection_search_run(
        "ai-run",
        [
            {"item_id": "shared", "title": "共享商品", "url": "https://example.test/shared"},
            {"item_id": "new-ai", "title": "新商品", "url": "https://example.test/new-ai"},
        ],
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        item_id = json.loads(request.content)["item_id"]
        requested.append(item_id)
        snapshot = database.save_selection_item_snapshot(
            item_id, price_cents=100, price_text="1", want_count=1,
            browse_count=10, collect_count=1,
        )
        return httpx.Response(200, json={"ok": True, "snapshot": snapshot})

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_selection_details_batch(
                "ai-run", database, bridge_token="token", limit=10,
                interval_seconds=0, detail_url="http://detail.test/collect", client=client,
            )

    result = asyncio.run(collect())
    assert requested == ["new-ai"]
    assert result["skip_counts"]["already_observed_today"] == 1
    with database.connect() as connection:
        evidence = connection.execute(
            """
            SELECT keyword, run_id, search_rank
            FROM selection_search_item_observations
            WHERE item_id='shared' ORDER BY observation_id
            """
        ).fetchall()
    assert [(row["keyword"], row["run_id"], row["search_rank"]) for row in evidence] == [
        ("skill", "skill-run", 1),
        ("AI工具", "ai-run", 1),
    ]


def test_ordinary_failure_consumes_budget_and_continues(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.start_selection_search_run("run", "skill")
    database.complete_selection_search_run(
        "run",
        [
            {"item_id": str(index), "title": f"商品{index}", "url": f"https://example.test/{index}"}
            for index in range(1, 4)
        ],
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        item_id = json.loads(request.content)["item_id"]
        requested.append(item_id)
        if item_id == "1":
            return httpx.Response(500, json={"detail": {"code": "DETAIL_FAILED", "message": "失败"}})
        snapshot = database.save_selection_item_snapshot(
            item_id, price_cents=100, price_text="1", want_count=1,
            browse_count=10, collect_count=1,
        )
        return httpx.Response(200, json={"ok": True, "snapshot": snapshot})

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_selection_details_batch(
                "run", database, bridge_token="token", limit=3,
                interval_seconds=0, detail_url="http://detail.test/collect", client=client,
            )

    result = asyncio.run(collect())
    assert requested == ["1", "2", "3"]
    assert result["processed_count"] == 3
    assert result["failed_count"] == 1
    assert result["success_count"] == 2


def test_verification_stops_remaining_detail_candidates(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.start_selection_search_run("run", "skill")
    database.complete_selection_search_run(
        "run",
        [
            {"item_id": str(index), "title": f"商品{index}", "url": f"https://example.test/{index}"}
            for index in range(1, 4)
        ],
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(json.loads(request.content)["item_id"])
        return httpx.Response(
            409,
            json={"detail": {"code": "VERIFICATION_REQUIRED", "message": "需要人工验证"}},
        )

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_selection_details_batch(
                "run", database, bridge_token="token", limit=3,
                interval_seconds=0, detail_url="http://detail.test/collect", client=client,
            )

    result = asyncio.run(collect())
    assert requested == ["1"]
    assert result["processed_count"] == 1
    assert result["stopped_code"] == "VERIFICATION_REQUIRED"


def test_recent_tracked_item_not_in_current_search_respects_cooldown(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.start_selection_search_run("old-run", "skill")
    database.complete_selection_search_run(
        "old-run",
        [{"item_id": "tracked", "title": "旧商品", "url": "https://example.test/tracked"}],
    )
    database.save_selection_item_snapshot(
        "tracked", price_cents=100, price_text="1", want_count=1,
        browse_count=10, collect_count=1,
    )
    database.start_selection_search_run("current-run", "skill")
    database.complete_selection_search_run(
        "current-run",
        [{"item_id": "new", "title": "新商品", "url": "https://example.test/new"}],
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        item_id = json.loads(request.content)["item_id"]
        requested.append(item_id)
        snapshot = database.save_selection_item_snapshot(
            item_id, price_cents=100, price_text="1", want_count=1,
            browse_count=10, collect_count=1,
        )
        return httpx.Response(200, json={"ok": True, "snapshot": snapshot})

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_selection_details_batch(
                "current-run", database, bridge_token="token", limit=10,
                interval_seconds=0, detail_url="http://detail.test/collect", client=client,
            )

    asyncio.run(collect())
    assert requested == ["new"]


def test_active_tracking_item_not_in_current_search_gets_new_snapshot(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.start_selection_search_run("day-1", "skill")
    database.complete_selection_search_run(
        "day-1",
        [{"item_id": "tracked", "title": "追踪商品", "url": "https://example.test/tracked"}],
    )
    database.save_selection_item_snapshot(
        "tracked", price_cents=100, price_text="1", want_count=18,
        browse_count=100, collect_count=5,
    )
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE selection_item_snapshots
            SET observed_at='2026-08-20 00:00:00',
                detail_observed_at='2026-08-20 00:00:00'
            WHERE run_id='day-1'
            """
        )
    database.start_selection_search_run("day-2", "skill")
    database.complete_selection_search_run("day-2", [])
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requested.append(body["item_id"])
        snapshot = database.save_selection_item_snapshot(
            body["item_id"], price_cents=100, price_text="1", want_count=31,
            browse_count=150, collect_count=8,
            observation_run_id=body["observation_run_id"],
            observation_keyword=body["observation_keyword"],
            observation_search_rank=body["observation_search_rank"],
        )
        return httpx.Response(200, json={"ok": True, "snapshot": snapshot})

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_selection_details_batch(
                "day-2", database, bridge_token="token", limit=10,
                interval_seconds=0, detail_url="http://detail.test/collect", client=client,
            )

    result = asyncio.run(collect())
    assert requested == ["tracked"]
    assert result["selected_tracking_count"] == 1
    with database.connect() as connection:
        snapshots = connection.execute(
            "SELECT run_id, want_count FROM selection_item_snapshots WHERE item_id='tracked' ORDER BY snapshot_id"
        ).fetchall()
    assert [tuple(row) for row in snapshots] == [("day-1", 18), ("day-2", 31)]
