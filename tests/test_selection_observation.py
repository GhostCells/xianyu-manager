from __future__ import annotations

import sqlite3
from datetime import date

from xianyu_manager.database import Database
from xianyu_manager.selection_observation import collect_daily_observation, render_daily_markdown


def test_daily_observation_reports_collection_distribution_and_candidates(tmp_path) -> None:
    database_path = tmp_path / "manager.db"
    Database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO selection_search_runs
            (run_id,keyword,page,started_at,finished_at,status,result_count,http_status)
            VALUES ('run-1','skill',1,'2026-08-22 01:00:00','2026-08-22 01:00:05','success',3,200)"""
        )
        for index, values in enumerate(((10, 100, 5), (30, 200, 20), (80, 300, 40)), start=1):
            item_id = f"item-{index}"
            connection.execute(
                """INSERT INTO selection_items
                (item_id,title_raw,canonical_url,last_search_run_id,last_search_keyword,last_search_rank,first_seen_at,last_seen_at)
                VALUES (?,?,?,'run-1','skill',?,'2026-08-22 01:00:00','2026-08-22 01:00:00')""",
                (item_id, f"商品 {index}", f"https://example.com/{item_id}", index),
            )
            connection.execute(
                """INSERT INTO selection_item_snapshots
                (run_id,item_id,keyword,observed_at,search_rank,title_raw,price_cents,price_text,
                 price_parse_status,want_count,browse_count,collect_count,detail_status,detail_observed_at)
                VALUES ('run-1',?,'skill','2026-08-22 01:01:00',?,?,990,'9.90','parsed',?,?,?,'success','2026-08-22 01:01:00')""",
                (item_id, index, f"商品 {index}", *values),
            )

    report = collect_daily_observation(database_path, date(2026, 8, 22))
    markdown = render_daily_markdown(report)
    assert report["collection"]["search_success"] == 1
    assert report["collection"]["result_count"] == 3
    assert report["collection"]["detail_success"] == 3
    assert report["completeness"]["all"] == 1.0
    assert report["distributions"]["want_count"]["p50"] == 30
    assert report["keywords"][0]["sample_status"] == "可观察"
    assert report["candidates"][0]["item_id"] == "item-3"
    assert "不是选品评分" in markdown
