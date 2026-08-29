from __future__ import annotations

import sqlite3

from xianyu_manager.database import Database
from xianyu_manager.selection_rule_scoring import SCORE_VERSION, _percentile_scores, score_selection_run


def _seed_run(database_path, run_id: str, count: int, *, missing_last: bool = False) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO selection_search_runs
            (run_id,keyword,page,status,result_count) VALUES (?, 'skill', 1, 'success', ?)""",
            (run_id, count),
        )
        for index in range(1, count + 1):
            item_id = f"{run_id}-item-{index}"
            status = "worth_testing" if index == 1 else "unreviewed"
            connection.execute(
                """INSERT INTO selection_items
                (item_id,title_raw,canonical_url,candidate_status,last_search_run_id,
                 last_search_keyword,last_search_rank)
                VALUES (?,?,?, ?, ?, 'skill', ?)""",
                (item_id, f"AI 自动化 Skill 工具 {index}", f"https://example.com/{item_id}", status, run_id, index),
            )
            missing = missing_last and index == count
            connection.execute(
                """INSERT INTO selection_item_snapshots
                (run_id,item_id,keyword,search_rank,title_raw,price_cents,price_text,
                 price_parse_status,want_count,browse_count,collect_count,detail_status)
                VALUES (?,?,'skill',?,?,1990,'19.90','parsed',?,?,?,?)""",
                (
                    run_id, item_id, index, f"AI 自动化 Skill 工具 {index}",
                    None if missing else index * index * 4,
                    None if missing else index * 100,
                    None if missing else index * index * 2,
                    "failed" if missing else "success",
                ),
            )


def test_rule_scoring_generates_ranked_scores_and_preserves_review_status(tmp_path) -> None:
    database_path = tmp_path / "manager.db"
    database = Database(database_path)
    _seed_run(database_path, "run-five", 5)
    report = score_selection_run(database, "run-five")
    assert report["formal_score_count"] == 5
    assert report["groups"][0]["confidence"] == "low"
    assert report["items"][0]["item_id"] == "run-five-item-5"
    assert report["items"][0]["total_score"] > report["items"][-1]["total_score"]
    with sqlite3.connect(database_path) as connection:
        statuses = dict(connection.execute("SELECT item_id, candidate_status FROM selection_items").fetchall())
    assert statuses["run-five-item-1"] == "worth_testing"


def test_under_five_complete_samples_store_components_but_no_total(tmp_path) -> None:
    database_path = tmp_path / "manager.db"
    database = Database(database_path)
    _seed_run(database_path, "run-four", 4)
    report = score_selection_run(database, "run-four")
    assert report["formal_score_count"] == 0
    assert report["null_score_count"] == 4
    assert all(item["total_score"] is None for item in report["items"])
    assert all("insufficient_cohort" in item["missing_fields"] for item in report["items"])
    assert all(item["want_rate_score"] is not None for item in report["items"])


def test_missing_detail_stays_null_while_five_complete_items_are_scored(tmp_path) -> None:
    database_path = tmp_path / "manager.db"
    database = Database(database_path)
    _seed_run(database_path, "run-six", 6, missing_last=True)
    first = score_selection_run(database, "run-six")
    second = score_selection_run(database, "run-six")
    assert first["formal_score_count"] == 5
    missing = next(item for item in first["items"] if item["item_id"] == "run-six-item-6")
    assert missing["total_score"] is None
    assert {"detail_status", "want_count", "browse_count", "collect_count"}.issubset(missing["missing_fields"])
    assert len(second["items"]) == 6
    with sqlite3.connect(database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM selection_scores WHERE score_version=?", (SCORE_VERSION,)).fetchone()[0]
    assert count == 6


def test_percentile_ties_use_average_rank_and_all_equal_is_neutral() -> None:
    assert _percentile_scores({1: 1.0, 2: 1.0}) == {1: 50.0, 2: 50.0}
    scores = _percentile_scores({1: 1.0, 2: 2.0, 3: 2.0, 4: 4.0})
    assert scores[1] == 0.0
    assert scores[2] == scores[3] == 50.0
    assert scores[4] == 100.0
