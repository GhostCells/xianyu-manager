from __future__ import annotations

import asyncio

import httpx

from xianyu_manager.database import Database
from xianyu_manager.selection_collection import collect_selection_search


def test_collect_selection_search_saves_run_and_items(tmp_path):
    database = Database(tmp_path / "manager.db")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Internal-Token"] == "test-token"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "keyword": "skill",
                "page": 1,
                "source": {
                    "request_url": "https://h5api.m.goofish.com/search?sign=temporary",
                    "http_status": 200,
                },
                "total": 2,
                "items": [
                    {
                        "item_id": "1001",
                        "title": "Codex Skill",
                        "url": "https://www.goofish.com/item?id=1001",
                    },
                    {
                        "item_id": "1002",
                        "title": "AI Workflow",
                        "url": "https://www.goofish.com/item?id=1002",
                    },
                ],
            },
        )

    async def collect() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_selection_search(
                "skill",
                database,
                bridge_token="test-token",
                bridge_url="http://selection.test/search",
                client=client,
            )

    result = asyncio.run(collect())

    assert result["result_count"] == 2
    with database.connect() as connection:
        run = connection.execute(
            "SELECT * FROM selection_search_runs WHERE run_id=?",
            (result["run_id"],),
        ).fetchone()
        items = connection.execute(
            "SELECT * FROM selection_items ORDER BY item_id"
        ).fetchall()
        snapshots = connection.execute(
            "SELECT COUNT(*) AS count FROM selection_item_snapshots"
        ).fetchone()
        observations = connection.execute(
            """
            SELECT item_id, keyword, run_id, search_rank
            FROM selection_search_item_observations
            ORDER BY search_rank
            """
        ).fetchall()

    assert run is not None
    assert run["status"] == "success"
    assert run["result_count"] == 2
    assert run["source_api"] == "https://h5api.m.goofish.com/search"
    assert [row["item_id"] for row in items] == ["1001", "1002"]
    assert snapshots["count"] == 0
    assert [tuple(row) for row in observations] == [
        ("1001", "skill", result["run_id"], 1),
        ("1002", "skill", result["run_id"], 2),
    ]
