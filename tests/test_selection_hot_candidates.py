from __future__ import annotations

import sqlite3

from xianyu_manager.database import Database
from xianyu_manager.selection_hot_candidates import calculate_trend, refresh_item_assessment


def _snapshot(snapshot_id: int, at: str, **metrics):
    return {
        "snapshot_id": snapshot_id,
        "detail_observed_at": at,
        "observed_at": at,
        "browse_count": metrics.get("browse_count"),
        "want_count": metrics.get("want_count"),
        "collect_count": metrics.get("collect_count"),
    }


def test_trend_handles_first_missing_zero_growth_and_decrease() -> None:
    first = calculate_trend(_snapshot(1, "2026-08-29 00:00:00"), None)
    assert first["interval_hours"] is None
    assert first["anomaly_codes"] == ["first_observation"]

    previous = _snapshot(1, "2026-08-29 00:00:00", browse_count=100, want_count=None, collect_count=5)
    current = _snapshot(2, "2026-08-30 00:00:00", browse_count=100, want_count=9, collect_count=3)
    trend = calculate_trend(current, previous)
    assert trend["interval_hours"] == 24
    assert trend["browse_delta"] == 0
    assert trend["browse_per_hour"] == 0
    assert trend["want_delta"] is None
    assert trend["collect_delta"] == -2
    assert trend["collect_per_hour"] is None
    assert set(trend["anomaly_codes"]) == {"want_count_missing", "collect_count_decreased"}


def test_multi_observation_assessment_promotes_once_and_exports_one_cross_keyword_candidate(tmp_path) -> None:
    database = Database(tmp_path / "manager.db")
    database.start_selection_search_run("run-1", "skill")
    database.complete_selection_search_run(
        "run-1", [{"item_id": "1001", "title": "AI 自动化 Skill 工具", "url": "https://example.test/1001"}]
    )
    database.save_selection_item_snapshot(
        "1001", price_cents=1990, price_text="19.90",
        browse_count=100, want_count=5, collect_count=2,
    )
    assert database.get_selection_item("1001")["pipeline_status"] == "observing"

    database.start_selection_search_run("run-2", "AI工具")
    database.complete_selection_search_run(
        "run-2", [{"item_id": "1001", "title": "AI 自动化 Skill 工具", "url": "https://example.test/1001"}]
    )
    database.save_selection_item_snapshot(
        "1001", price_cents=1990, price_text="19.90",
        browse_count=1100, want_count=65, collect_count=32,
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE selection_item_snapshots SET observed_at='2026-08-29 00:00:00', detail_observed_at='2026-08-29 00:00:00' WHERE run_id='run-1'"
        )
        connection.execute(
            "UPDATE selection_item_snapshots SET observed_at='2026-08-30 00:00:00', detail_observed_at='2026-08-30 00:00:00' WHERE run_id='run-2'"
        )
    assessment = refresh_item_assessment(database, "1001")
    assert assessment["total_score"] >= 60
    assert "score_threshold_met" in assessment["reason_codes"]
    rows = database.list_operation_candidates()
    assert len(rows) == 1
    assert rows[0]["item_id"] == "1001"
    assert rows[0]["source_keywords"] == ["AI工具", "skill"]
    assert rows[0]["browse_delta"] == 1000
    assert rows[0]["review_status"] == "unreviewed"
    assert rows[0]["explanation"]["unknown_fields"] == ["published_at", "item_age_hours"]


def test_pipeline_transitions_and_repeatable_migration(tmp_path) -> None:
    path = tmp_path / "manager.db"
    database = Database(path)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO selection_items (item_id,title_raw,canonical_url) VALUES ('1','商品','https://example.test/1')"
        )
    assert database.set_selection_pipeline_status("1", "observing")["pipeline_status"] == "observing"
    assert database.set_selection_pipeline_status("1", "hot_candidate")["pipeline_status"] == "hot_candidate"
    assert database.set_selection_pipeline_status("1", "reviewed")["pipeline_status"] == "reviewed"
    assert database.set_selection_pipeline_status("1", "production")["pipeline_status"] == "production"
    Database(path)
    Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM selection_items WHERE item_id='1'").fetchone()[0] == 1
        assert connection.execute("SELECT pipeline_status FROM selection_items WHERE item_id='1'").fetchone()[0] == "production"


def test_operation_api_wrapper_is_machine_readable(monkeypatch) -> None:
    from xianyu_manager import app as app_module

    class FakeDatabase:
        def list_operation_candidates(self, *, limit):
            assert limit == 7
            return [{"item_id": "1001", "total_score": 88.0}]

    monkeypatch.setattr(app_module, "database", FakeDatabase())
    payload = app_module.operation_selection_candidates(limit=7)
    assert payload == {
        "schema_version": "selection-candidates-v1",
        "count": 1,
        "items": [{"item_id": "1001", "total_score": 88.0}],
    }
