from __future__ import annotations

from datetime import datetime, timezone

from xianyu_manager.database import Database
from xianyu_manager.selection_tracking import (
    build_tracking_progress,
    plan_detail_candidates,
)


NOW = datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc)


def candidate(item_id: str, rank: int, **overrides):
    row = {
        "observation_id": rank,
        "run_id": "run-current",
        "source_platform": "xianyu",
        "item_id": item_id,
        "keyword": "skill",
        "observed_at": "2026-08-28 02:00:00",
        "search_rank": rank,
        "tracking_status": None,
        "tracking_priority": 0,
        "min_interval_hours": None,
        "last_successful_detail_at": None,
        "current_run_success": 0,
    }
    row.update(overrides)
    return row


def test_only_new_items_use_existing_budget() -> None:
    plan = plan_detail_candidates(
        [candidate(str(index), index) for index in range(1, 16)],
        limit=10,
        now=NOW,
    )
    assert len(plan["selected"]) == 10
    assert plan["selected_new_count"] == 10
    assert plan["selected_tracking_count"] == 0
    assert all(row["candidate_kind"] == "initial" for row in plan["selected"])


def test_only_due_tracking_items_use_existing_budget() -> None:
    rows = [
        candidate(
            str(index),
            index,
            tracking_status="active",
            last_successful_detail_at="2026-08-27 02:00:00",
        )
        for index in range(1, 13)
    ]
    plan = plan_detail_candidates(rows, limit=10, now=NOW)
    assert len(plan["selected"]) == 10
    assert plan["selected_tracking_count"] == 10
    assert plan["selected_new_count"] == 0


def test_mixed_pool_is_adaptive_and_neither_pool_is_starved() -> None:
    rows = [candidate(f"new-{index}", index) for index in range(1, 21)]
    rows += [
        candidate(
            f"tracked-{index}",
            20 + index,
            tracking_status="active",
            tracking_priority=index,
            last_successful_detail_at="2026-08-27 02:00:00",
        )
        for index in range(1, 5)
    ]
    plan = plan_detail_candidates(rows, limit=10, now=NOW)
    assert len(plan["selected"]) == 10
    assert plan["selected_new_count"] > 0
    assert plan["selected_tracking_count"] > 0
    assert plan["selected_tracking_count"] != 5


def test_configured_budget_ratio_and_unused_budget_borrowing() -> None:
    rows = [candidate(f"new-{index}", index) for index in range(1, 9)]
    rows += [
        candidate(
            f"tracked-{index}", 20 + index,
            tracking_status="active",
            last_successful_detail_at="2026-08-27 02:00:00",
        )
        for index in range(1, 9)
    ]
    plan = plan_detail_candidates(
        rows, limit=10, tracking_budget_ratio=0.4, now=NOW
    )
    assert plan["selected_new_count"] == 6
    assert plan["selected_tracking_count"] == 4

    borrowed = plan_detail_candidates(
        [candidate("new", 1), *rows[8:]],
        limit=10, tracking_budget_ratio=0.4, now=NOW,
    )
    assert borrowed["selected_new_count"] == 1
    assert borrowed["selected_tracking_count"] == 8


def test_tracking_priority_then_velocity_and_snapshot_count_control_order() -> None:
    rows = [
        candidate(
            "lower", 1, tracking_status="active", tracking_priority=10,
            snapshot_count=5, latest_want_per_hour=4,
            last_successful_detail_at="2026-08-27 02:00:00",
        ),
        candidate(
            "higher", 2, tracking_status="active", tracking_priority=20,
            snapshot_count=2, latest_want_per_hour=1,
            last_successful_detail_at="2026-08-27 02:00:00",
        ),
    ]
    plan = plan_detail_candidates(
        rows, limit=1, tracking_budget_ratio=1, now=NOW
    )
    assert plan["selected"][0]["item_id"] == "higher"


def test_not_due_and_short_cross_midnight_items_are_skipped() -> None:
    rows = [
        candidate(
            "not-due",
            1,
            tracking_status="active",
            last_successful_detail_at="2026-08-27 10:00:00",
        ),
        candidate(
            "cross-midnight",
            2,
            tracking_status="active",
            last_successful_detail_at="2026-08-27 15:50:00",
        ),
    ]
    now = datetime(2026, 8, 27, 16, 10, tzinfo=timezone.utc)
    plan = plan_detail_candidates(rows, limit=10, now=now)
    assert plan["selected"] == []
    assert plan["skip_counts"] == {"tracking_not_due": 2}


def test_current_run_success_and_inactive_tracking_are_skipped() -> None:
    plan = plan_detail_candidates(
        [
            candidate("same-run", 1, current_run_success=1),
            candidate(
                "paused",
                2,
                tracking_status="paused",
                last_successful_detail_at="2026-08-20 00:00:00",
            ),
        ],
        limit=10,
        now=NOW,
    )
    assert plan["selected"] == []
    assert plan["skip_counts"] == {
        "current_run_success": 1,
        "tracking_inactive": 1,
    }


def test_tracking_progress_reports_series_without_trend_claim(tmp_path) -> None:
    database = Database(tmp_path / "manager.db")
    database.start_selection_search_run("run-1", "skill")
    database.complete_selection_search_run(
        "run-1",
        [{"item_id": "1001", "title": "商品", "url": "https://example.test/1001"}],
    )
    database.save_selection_item_snapshot(
        "1001", price_cents=100, price_text="1", want_count=1,
        browse_count=10, collect_count=1,
    )
    database.start_selection_search_run("run-2", "skill")
    database.complete_selection_search_run(
        "run-2",
        [{"item_id": "1001", "title": "商品", "url": "https://example.test/1001"}],
    )
    database.save_selection_item_snapshot(
        "1001", price_cents=100, price_text="1", want_count=2,
        browse_count=20, collect_count=2,
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE selection_item_snapshots SET detail_observed_at='2026-08-27 00:00:00', observed_at='2026-08-27 00:00:00' WHERE run_id='run-1'"
        )
        connection.execute(
            "UPDATE selection_item_snapshots SET detail_observed_at='2026-08-28 00:00:00', observed_at='2026-08-28 00:00:00' WHERE run_id='run-2'"
        )
        progress = build_tracking_progress(connection)
    assert progress["tracked_items"] == 1
    assert progress["items_with_2_plus_snapshots"] == 1
    assert progress["items_with_3_plus_snapshots"] == 0
    assert progress["comparable_24h_items"] == 1
    assert "不构成爆款" in progress["conclusion"]
