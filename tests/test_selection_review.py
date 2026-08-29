from __future__ import annotations

import sqlite3

import pytest

from xianyu_manager.database import Database
from xianyu_manager.selection_review import normalize_status, validate_reason


def _database_with_item(tmp_path) -> Database:
    database = Database(tmp_path / "manager.db")
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO selection_items (item_id, title_raw, canonical_url)
            VALUES ('123', '测试候选商品', 'https://www.goofish.com/item?id=123')
            """
        )
    return database


def test_review_schema_and_default_status_are_created(tmp_path) -> None:
    database = _database_with_item(tmp_path)
    item = database.get_selection_item("123")
    with database.connect() as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='selection_item_reviews'"
        ).fetchone()
    assert table is not None
    assert item is not None
    assert item["candidate_status"] == "unreviewed"
    assert item["last_review_id"] is None


def test_status_changes_keep_immutable_history(tmp_path) -> None:
    database = _database_with_item(tmp_path)
    first = database.set_selection_candidate_status(
        "123", "worth_testing", reason_code="clear_demand", note="需求明确"
    )
    second = database.set_selection_candidate_status(
        "123", "tested", reason_code="listing_published", note="已上架"
    )
    history = database.list_selection_item_reviews("123")
    item = database.get_selection_item("123")

    assert first["previous_status"] == "unreviewed"
    assert first["new_status"] == "worth_testing"
    assert second["previous_status"] == "worth_testing"
    assert second["new_status"] == "tested"
    assert [row["review_id"] for row in history] == [first["review_id"], second["review_id"]]
    assert item is not None
    assert item["candidate_status"] == "tested"
    assert item["last_review_id"] == second["review_id"]

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect() as connection:
            connection.execute(
                "UPDATE selection_item_reviews SET note='覆盖历史' WHERE review_id=?",
                (first["review_id"],),
            )
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        with database.connect() as connection:
            connection.execute(
                "DELETE FROM selection_item_reviews WHERE review_id=?",
                (first["review_id"],),
            )


def test_same_status_and_unexplained_rollback_are_rejected(tmp_path) -> None:
    database = _database_with_item(tmp_path)
    with pytest.raises(ValueError, match="已经处于"):
        database.set_selection_candidate_status("123", "unreviewed")
    database.set_selection_candidate_status("123", "converted", note="确认真实成交")
    with pytest.raises(ValueError, match="必须填写备注"):
        database.set_selection_candidate_status("123", "tested")
    assert len(database.list_selection_item_reviews("123")) == 1


def test_cli_status_alias_and_reason_validation() -> None:
    assert normalize_status("值得测试") == "worth_testing"
    assert normalize_status("已成交") == "converted"
    assert validate_reason("converted", "first_real_order") == "first_real_order"
    with pytest.raises(ValueError, match="原因与目标状态不匹配"):
        validate_reason("converted", "weak_demand")


def test_existing_database_receives_review_columns(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE selection_items (
                item_id TEXT PRIMARY KEY,
                title_raw TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    database = Database(path)
    with database.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(selection_items)")}
    assert {"candidate_status", "candidate_status_updated_at", "last_review_id"} <= columns
