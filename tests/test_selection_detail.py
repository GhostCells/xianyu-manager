from __future__ import annotations

from xianyu_manager.database import Database
from xianyu_manager.selection_detail import normalize_detail


def test_normalize_detail_and_save_snapshot(tmp_path):
    detail = normalize_detail(
        {
            "data": {
                "itemDO": {
                    "wantCnt": "12",
                    "browseCnt": 345,
                    "collectCnt": "6",
                    "soldPrice": "19.90",
                }
            }
        }
    )
    assert detail == {
        "want_count": 12,
        "browse_count": 345,
        "collect_count": 6,
        "price_text": "19.90",
        "price_cents": 1990,
    }

    database = Database(tmp_path / "manager.db")
    database.start_selection_search_run("run-1", "skill")
    database.complete_selection_search_run(
        "run-1",
        [
            {
                "item_id": "1001",
                "title": "测试 Skill",
                "url": "https://www.goofish.com/item?id=1001",
            }
        ],
        http_status=200,
    )
    snapshot = database.save_selection_item_snapshot("1001", **detail)

    assert snapshot["run_id"] == "run-1"
    assert snapshot["keyword"] == "skill"
    assert snapshot["search_rank"] == 1
    assert snapshot["want_count"] == 12
    assert snapshot["browse_count"] == 345
    assert snapshot["collect_count"] == 6
    assert snapshot["price_cents"] == 1990
