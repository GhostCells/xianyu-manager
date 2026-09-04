from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlparse

from .scanner import ScannedProduct


SELECTION_CANDIDATE_STATUSES = {
    "unreviewed",
    "worth_testing",
    "not_worth_testing",
    "tested",
    "converted",
}

SELECTION_PIPELINE_STATUSES = {
    "new_discovery",
    "observing",
    "hot_candidate",
    "reviewed",
    "rejected",
    "production",
    "stopped",
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    dir_name TEXT PRIMARY KEY,
    number INTEGER NOT NULL,
    name TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    knowledge_text TEXT NOT NULL DEFAULT '',
    knowledge_hash TEXT NOT NULL DEFAULT '',
    knowledge_chars INTEGER NOT NULL DEFAULT 0,
    knowledge_source_path TEXT NOT NULL DEFAULT '',
    knowledge_file_count INTEGER NOT NULL DEFAULT 0,
    knowledge_updated_at TEXT,
    zip_name TEXT NOT NULL DEFAULT '',
    zip_hash TEXT NOT NULL DEFAULT '',
    zip_size INTEGER NOT NULL DEFAULT 0,
    image_count INTEGER NOT NULL DEFAULT 0,
    quality_status TEXT NOT NULL DEFAULT 'unknown',
    quality_errors_json TEXT NOT NULL DEFAULT '[]',
    scanned_at TEXT NOT NULL,
    share_url TEXT NOT NULL DEFAULT '',
    share_code TEXT NOT NULL DEFAULT '',
    share_verified INTEGER NOT NULL DEFAULT 0,
    share_needs_review INTEGER NOT NULL DEFAULT 0,
    catalog_status TEXT NOT NULL DEFAULT 'active',
    suggested_price_cents INTEGER,
    confirmed_price_cents INTEGER,
    listing_url TEXT NOT NULL DEFAULT '',
    listing_status TEXT NOT NULL DEFAULT 'draft',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 0,
    is_archived INTEGER NOT NULL DEFAULT 0,
    binding_status TEXT NOT NULL DEFAULT 'unbound',
    binding_confirmed_at TEXT,
    session_last_checked_at TEXT,
    session_last_error TEXT NOT NULL DEFAULT '',
    delivery_enabled INTEGER NOT NULL DEFAULT 0,
    auto_confirm_delivery INTEGER NOT NULL DEFAULT 1,
    auto_free_group INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_account
ON accounts(is_active) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS account_products (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    product_dir_name TEXT NOT NULL REFERENCES products(dir_name) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1,
    suggested_price_cents INTEGER,
    confirmed_price_cents INTEGER,
    listing_url TEXT NOT NULL DEFAULT '',
    listing_status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, product_dir_name)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    subject TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    xianyu_order_id TEXT NOT NULL UNIQUE,
    account_id INTEGER REFERENCES accounts(id),
    product_dir_name TEXT NOT NULL REFERENCES products(dir_name),
    listing_item_id TEXT NOT NULL DEFAULT '',
    buyer_id TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    event_fingerprint TEXT NOT NULL DEFAULT '',
    payment_status TEXT NOT NULL DEFAULT 'unknown',
    delivery_status TEXT NOT NULL DEFAULT 'pending',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivery_message_hash TEXT NOT NULL DEFAULT '',
    message_sent_at TEXT,
    platform_confirm_status TEXT NOT NULL DEFAULT 'pending',
    platform_confirmed_at TEXT,
    group_status TEXT NOT NULL DEFAULT 'not_applicable',
    group_attempts INTEGER NOT NULL DEFAULT 0,
    group_exempted_at TEXT,
    group_event_fingerprint TEXT NOT NULL DEFAULT '',
    detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS account_listings (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    title TEXT NOT NULL,
    listing_url TEXT NOT NULL,
    image_url TEXT NOT NULL DEFAULT '',
    price_cents INTEGER,
    source_text TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'account_snapshot',
    matched_product_dir_name TEXT REFERENCES products(dir_name),
    is_active INTEGER NOT NULL DEFAULT 1,
    synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, item_id)
);

CREATE TABLE IF NOT EXISTS auto_reply_settings (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0,
    base_url TEXT NOT NULL DEFAULT 'https://api.siliconflow.cn/v1',
    model TEXT NOT NULL DEFAULT 'deepseek-ai/DeepSeek-V4-Flash',
    system_prompt TEXT NOT NULL DEFAULT '',
    min_delay_seconds INTEGER NOT NULL DEFAULT 5,
    max_delay_seconds INTEGER NOT NULL DEFAULT 12,
    max_reply_chars INTEGER NOT NULL DEFAULT 180,
    manual_takeover_hours INTEGER NOT NULL DEFAULT 12,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    buyer_id TEXT NOT NULL DEFAULT '',
    listing_item_id TEXT NOT NULL DEFAULT '',
    manual_takeover_until TEXT,
    last_buyer_message_at TEXT,
    last_auto_reply_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, chat_id)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    buyer_id TEXT NOT NULL DEFAULT '',
    listing_item_id TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL,
    content TEXT NOT NULL,
    event_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'received',
    reply_source TEXT NOT NULL DEFAULT '',
    related_message_id INTEGER REFERENCES chat_messages(id),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, event_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_lookup
ON chat_messages(account_id, chat_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_chat_messages_status
ON chat_messages(account_id, status, id DESC);

CREATE TABLE IF NOT EXISTS automation_safety (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    max_replies_per_hour INTEGER NOT NULL DEFAULT 20,
    max_replies_per_day INTEGER NOT NULL DEFAULT 100,
    max_deliveries_per_hour INTEGER NOT NULL DEFAULT 15,
    min_outbound_interval_seconds INTEGER NOT NULL DEFAULT 5,
    risk_cooldown_minutes INTEGER NOT NULL DEFAULT 30,
    notifications_enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS automation_runtime (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    circuit_open_until TEXT,
    circuit_reason TEXT NOT NULL DEFAULT '',
    last_startup_at TEXT,
    last_startup_report_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS automation_outbound_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    reference TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved',
    error TEXT NOT NULL DEFAULT '',
    reserved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT,
    UNIQUE(account_id, kind, reference)
);

CREATE INDEX IF NOT EXISTS idx_automation_outbound_usage
ON automation_outbound_events(account_id, kind, status, reserved_at DESC);

CREATE TABLE IF NOT EXISTS selection_items (
    item_id TEXT PRIMARY KEY,
    title_raw TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    candidate_status TEXT NOT NULL DEFAULT 'unreviewed'
        CHECK (candidate_status IN ('unreviewed', 'worth_testing', 'not_worth_testing', 'tested', 'converted')),
    candidate_status_updated_at TEXT,
    last_review_id INTEGER,
    last_search_run_id TEXT,
    last_search_keyword TEXT,
    last_search_rank INTEGER CHECK (last_search_rank IS NULL OR last_search_rank >= 1),
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_selection_items_last_seen
ON selection_items(last_seen_at DESC);

CREATE TABLE IF NOT EXISTS selection_search_runs (
    run_id TEXT PRIMARY KEY,
    keyword TEXT NOT NULL,
    page INTEGER NOT NULL DEFAULT 1 CHECK (page >= 1),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'success', 'partial', 'failed', 'blocked')),
    result_count INTEGER NOT NULL DEFAULT 0 CHECK (result_count >= 0),
    source_api TEXT,
    http_status INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_selection_search_runs_keyword_started
ON selection_search_runs(keyword, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_selection_search_runs_status_started
ON selection_search_runs(status, started_at DESC);

CREATE TABLE IF NOT EXISTS selection_item_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES selection_search_runs(run_id),
    item_id TEXT NOT NULL REFERENCES selection_items(item_id),
    keyword TEXT NOT NULL,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    search_rank INTEGER NOT NULL CHECK (search_rank >= 1),
    title_raw TEXT NOT NULL,
    price_cents INTEGER CHECK (price_cents IS NULL OR price_cents >= 0),
    price_text TEXT,
    price_parse_status TEXT NOT NULL DEFAULT 'missing'
        CHECK (price_parse_status IN ('parsed', 'missing', 'unparseable')),
    want_count INTEGER CHECK (want_count IS NULL OR want_count >= 0),
    browse_count INTEGER CHECK (browse_count IS NULL OR browse_count >= 0),
    collect_count INTEGER CHECK (collect_count IS NULL OR collect_count >= 0),
    detail_status TEXT NOT NULL DEFAULT 'skipped'
        CHECK (detail_status IN ('success', 'skipped', 'failed', 'verification_required')),
    detail_error_code TEXT,
    detail_observed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(run_id, item_id, keyword)
);

CREATE INDEX IF NOT EXISTS idx_selection_snapshots_item_observed
ON selection_item_snapshots(item_id, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_selection_snapshots_run_rank
ON selection_item_snapshots(run_id, keyword, search_rank);

CREATE TABLE IF NOT EXISTS selection_search_item_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES selection_search_runs(run_id),
    source_platform TEXT NOT NULL DEFAULT 'xianyu',
    item_id TEXT NOT NULL REFERENCES selection_items(item_id),
    keyword TEXT NOT NULL,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    search_rank INTEGER NOT NULL CHECK (search_rank >= 1),
    title_raw TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(run_id, source_platform, item_id, keyword)
);

CREATE INDEX IF NOT EXISTS idx_selection_search_observations_run_rank
ON selection_search_item_observations(run_id, keyword, search_rank);

CREATE INDEX IF NOT EXISTS idx_selection_search_observations_item_time
ON selection_search_item_observations(source_platform, item_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS selection_item_tracking (
    source_platform TEXT NOT NULL DEFAULT 'xianyu',
    item_id TEXT NOT NULL REFERENCES selection_items(item_id),
    tracking_status TEXT NOT NULL DEFAULT 'active'
        CHECK (tracking_status IN ('active', 'paused', 'retired')),
    tracking_priority INTEGER NOT NULL DEFAULT 0,
    tracking_reason_codes_json TEXT NOT NULL DEFAULT '["successful_detail_baseline"]',
    tracking_source TEXT NOT NULL DEFAULT 'automatic',
    min_interval_hours REAL,
    manual_note TEXT,
    enabled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paused_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_platform, item_id)
);

CREATE INDEX IF NOT EXISTS idx_selection_tracking_status_priority
ON selection_item_tracking(tracking_status, tracking_priority DESC, updated_at);

CREATE TABLE IF NOT EXISTS selection_item_tracking_events (
    tracking_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_platform TEXT NOT NULL DEFAULT 'xianyu',
    item_id TEXT NOT NULL REFERENCES selection_items(item_id),
    previous_status TEXT,
    new_status TEXT NOT NULL
        CHECK (new_status IN ('active', 'paused', 'retired')),
    reason_code TEXT,
    note TEXT,
    source TEXT NOT NULL DEFAULT 'manual_cli',
    changed_by TEXT NOT NULL DEFAULT 'owner',
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_selection_tracking_events_item_time
ON selection_item_tracking_events(source_platform, item_id, changed_at DESC);

CREATE TRIGGER IF NOT EXISTS prevent_selection_tracking_events_update
BEFORE UPDATE ON selection_item_tracking_events
BEGIN
    SELECT RAISE(ABORT, 'selection tracking history is immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_selection_tracking_events_delete
BEFORE DELETE ON selection_item_tracking_events
BEGIN
    SELECT RAISE(ABORT, 'selection tracking history cannot be deleted');
END;

CREATE TABLE IF NOT EXISTS selection_item_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL REFERENCES selection_items(item_id),
    previous_status TEXT NOT NULL
        CHECK (previous_status IN ('unreviewed', 'worth_testing', 'not_worth_testing', 'tested', 'converted')),
    new_status TEXT NOT NULL
        CHECK (new_status IN ('unreviewed', 'worth_testing', 'not_worth_testing', 'tested', 'converted')),
    reason_code TEXT,
    note TEXT,
    source TEXT NOT NULL DEFAULT 'manual_cli',
    reviewed_by TEXT NOT NULL DEFAULT 'owner',
    reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    related_snapshot_id INTEGER REFERENCES selection_item_snapshots(snapshot_id),
    related_search_run_id TEXT REFERENCES selection_search_runs(run_id),
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_selection_reviews_item_time
ON selection_item_reviews(item_id, reviewed_at DESC, review_id DESC);

CREATE INDEX IF NOT EXISTS idx_selection_reviews_status_time
ON selection_item_reviews(new_status, reviewed_at DESC);

CREATE TRIGGER IF NOT EXISTS prevent_selection_item_reviews_update
BEFORE UPDATE ON selection_item_reviews
BEGIN
    SELECT RAISE(ABORT, 'selection review history is immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_selection_item_reviews_delete
BEFORE DELETE ON selection_item_reviews
BEGIN
    SELECT RAISE(ABORT, 'selection review history cannot be deleted');
END;

CREATE TABLE IF NOT EXISTS selection_scores (
    snapshot_id INTEGER NOT NULL REFERENCES selection_item_snapshots(snapshot_id),
    score_version TEXT NOT NULL,
    want_rate_score REAL,
    collect_rate_score REAL,
    browse_score REAL,
    relevance_score REAL NOT NULL,
    price_fit_score REAL NOT NULL,
    total_score REAL,
    missing_fields_json TEXT NOT NULL DEFAULT '[]',
    calculated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (snapshot_id, score_version),
    CHECK (want_rate_score IS NULL OR want_rate_score BETWEEN 0 AND 100),
    CHECK (collect_rate_score IS NULL OR collect_rate_score BETWEEN 0 AND 100),
    CHECK (browse_score IS NULL OR browse_score BETWEEN 0 AND 100),
    CHECK (relevance_score BETWEEN 0 AND 100),
    CHECK (price_fit_score BETWEEN 0 AND 100),
    CHECK (total_score IS NULL OR total_score BETWEEN 0 AND 100)
);

CREATE INDEX IF NOT EXISTS idx_selection_scores_version_total
ON selection_scores(score_version, total_score DESC);

CREATE TABLE IF NOT EXISTS selection_item_trends (
    snapshot_id INTEGER PRIMARY KEY REFERENCES selection_item_snapshots(snapshot_id),
    previous_snapshot_id INTEGER REFERENCES selection_item_snapshots(snapshot_id),
    interval_hours REAL,
    browse_delta INTEGER,
    want_delta INTEGER,
    collect_delta INTEGER,
    browse_per_hour REAL,
    want_per_hour REAL,
    collect_per_hour REAL,
    anomaly_codes_json TEXT NOT NULL DEFAULT '[]',
    calculated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS selection_candidate_assessments (
    item_id TEXT PRIMARY KEY REFERENCES selection_items(item_id),
    snapshot_id INTEGER NOT NULL REFERENCES selection_item_snapshots(snapshot_id),
    score_version TEXT NOT NULL,
    total_score REAL,
    trend_score REAL,
    current_score REAL,
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    explanation_json TEXT NOT NULL DEFAULT '{}',
    assessed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (total_score IS NULL OR total_score BETWEEN 0 AND 100)
);

CREATE INDEX IF NOT EXISTS idx_selection_candidate_assessments_score
ON selection_candidate_assessments(total_score DESC, assessed_at DESC);
"""


DEFAULT_AUTOMATION_SAFETY: dict[str, object] = {
    "max_replies_per_hour": 20,
    "max_replies_per_day": 100,
    "max_deliveries_per_hour": 15,
    "min_outbound_interval_seconds": 5,
    "risk_cooldown_minutes": 30,
    "notifications_enabled": True,
}


GLOBAL_PRODUCT_FIELDS = {"share_url", "share_code", "share_verified"}
ACCOUNT_PRODUCT_FIELDS = {
    "enabled_for_account",
    "suggested_price_cents",
    "confirmed_price_cents",
    "listing_url",
    "listing_status",
}


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._ensure_orders_account_column(connection)
            self._ensure_order_delivery_columns(connection)
            self._ensure_products_catalog_status_column(connection)
            self._ensure_product_knowledge_columns(connection)
            self._ensure_account_binding_columns(connection)
            self._ensure_account_delivery_columns(connection)
            self._ensure_account_listings_source_column(connection)
            self._ensure_selection_item_context_columns(connection)
            self._ensure_selection_review_columns(connection)
            self._ensure_selection_pipeline_columns(connection)
            self._backfill_selection_tracking(connection)
            self._migrate_auto_reply_to_siliconflow(connection)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def start_selection_search_run(
        self,
        run_id: str,
        keyword: str,
        *,
        page: int = 1,
    ) -> dict[str, object]:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO selection_search_runs (
                    run_id, keyword, page, status, result_count
                ) VALUES (?, ?, ?, 'running', 0)
                """,
                (run_id, keyword, page),
            )
            row = connection.execute(
                "SELECT * FROM selection_search_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else {}

    def complete_selection_search_run(
        self,
        run_id: str,
        items: list[dict[str, object]],
        *,
        source_api: str = "",
        http_status: int | None = None,
    ) -> dict[str, object]:
        valid_items: list[tuple[str, str, str, str, str, int]] = []
        for rank, item in enumerate(items, start=1):
            item_id = str(item.get("item_id") or "").strip()
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            if item_id and title and url:
                valid_items.append((item_id, title, url, run_id, "", rank))

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT status FROM selection_search_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise ValueError("选品搜索任务不存在")
            if run["status"] != "running":
                raise ValueError("选品搜索任务不处于运行状态")
            keyword_row = connection.execute(
                "SELECT keyword FROM selection_search_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            run_keyword = str(keyword_row["keyword"] if keyword_row else "")
            valid_items = [
                (item_id, title, url, saved_run_id, run_keyword, rank)
                for item_id, title, url, saved_run_id, _, rank in valid_items
            ]

            connection.executemany(
                """
                INSERT INTO selection_items (
                    item_id, title_raw, canonical_url,
                    last_search_run_id, last_search_keyword, last_search_rank
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    title_raw=excluded.title_raw,
                    canonical_url=excluded.canonical_url,
                    last_search_run_id=excluded.last_search_run_id,
                    last_search_keyword=excluded.last_search_keyword,
                    last_search_rank=excluded.last_search_rank,
                    last_seen_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                """,
                valid_items,
            )
            connection.executemany(
                """
                INSERT INTO selection_search_item_observations (
                    run_id, source_platform, item_id, keyword,
                    search_rank, title_raw, canonical_url
                ) VALUES (?, 'xianyu', ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, source_platform, item_id, keyword) DO UPDATE SET
                    observed_at=CURRENT_TIMESTAMP,
                    search_rank=excluded.search_rank,
                    title_raw=excluded.title_raw,
                    canonical_url=excluded.canonical_url
                """,
                [
                    (saved_run_id, item_id, run_keyword, rank, title, url)
                    for item_id, title, url, saved_run_id, _, rank in valid_items
                ],
            )
            connection.execute(
                """
                UPDATE selection_search_runs
                SET status='success',
                    result_count=?,
                    source_api=?,
                    http_status=?,
                    error_code=NULL,
                    finished_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE run_id=?
                """,
                (len(valid_items), source_api or None, http_status, run_id),
            )
            row = connection.execute(
                "SELECT * FROM selection_search_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else {}

    def get_selection_item(self, item_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM selection_items WHERE item_id=?",
                (item_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_selection_review_items(
        self,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        if status is not None and status not in SELECTION_CANDIDATE_STATUSES:
            raise ValueError("候选状态无效")
        bounded_limit = max(1, min(int(limit), 100))
        conditions = "WHERE i.candidate_status=?" if status is not None else ""
        parameters: tuple[object, ...] = (status, bounded_limit) if status is not None else (bounded_limit,)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT i.item_id, i.title_raw, i.canonical_url,
                       i.candidate_status, i.candidate_status_updated_at,
                       i.first_seen_at, i.last_seen_at,
                       s.snapshot_id, s.price_cents, s.price_text,
                       s.want_count, s.browse_count, s.collect_count,
                       s.detail_status, s.detail_observed_at
                FROM selection_items AS i
                LEFT JOIN selection_item_snapshots AS s
                  ON s.snapshot_id=(
                    SELECT recent.snapshot_id
                    FROM selection_item_snapshots AS recent
                    WHERE recent.item_id=i.item_id
                    ORDER BY COALESCE(recent.detail_observed_at, recent.observed_at) DESC,
                             recent.snapshot_id DESC
                    LIMIT 1
                  )
                {conditions}
                ORDER BY
                  CASE WHEN i.candidate_status='unreviewed' THEN 0 ELSE 1 END,
                  i.last_seen_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def set_selection_candidate_status(
        self,
        item_id: str,
        new_status: str,
        *,
        reason_code: str = "",
        note: str = "",
        source: str = "manual_cli",
        reviewed_by: str = "owner",
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        normalized_item_id = item_id.strip()
        if not normalized_item_id:
            raise ValueError("item_id不能为空")
        if new_status not in SELECTION_CANDIDATE_STATUSES:
            raise ValueError("候选状态无效")
        normalized_reason = reason_code.strip()[:80]
        normalized_note = note.strip()
        if len(normalized_note) > 500:
            raise ValueError("审核备注不能超过500字")
        if not source.strip() or not reviewed_by.strip():
            raise ValueError("审核来源和操作者不能为空")

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT * FROM selection_items WHERE item_id=?",
                (normalized_item_id,),
            ).fetchone()
            if item is None:
                raise ValueError("候选商品不存在")
            previous_status = str(item["candidate_status"] or "unreviewed")
            if previous_status == new_status:
                raise ValueError("商品已经处于该候选状态")
            if (new_status == "unreviewed" or previous_status == "converted") and not normalized_note:
                raise ValueError("恢复待审核或撤销已成交状态时必须填写备注")

            snapshot = connection.execute(
                """
                SELECT snapshot_id, run_id
                FROM selection_item_snapshots
                WHERE item_id=?
                ORDER BY COALESCE(detail_observed_at, observed_at) DESC, snapshot_id DESC
                LIMIT 1
                """,
                (normalized_item_id,),
            ).fetchone()
            cursor = connection.execute(
                """
                INSERT INTO selection_item_reviews (
                    item_id, previous_status, new_status, reason_code, note,
                    source, reviewed_by, related_snapshot_id,
                    related_search_run_id, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_item_id,
                    previous_status,
                    new_status,
                    normalized_reason or None,
                    normalized_note or None,
                    source.strip()[:40],
                    reviewed_by.strip()[:80],
                    snapshot["snapshot_id"] if snapshot else None,
                    snapshot["run_id"] if snapshot else None,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            review_id = int(cursor.lastrowid)
            connection.execute(
                """
                UPDATE selection_items
                SET candidate_status=?,
                    candidate_status_updated_at=CURRENT_TIMESTAMP,
                    last_review_id=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE item_id=?
                """,
                (new_status, review_id, normalized_item_id),
            )
            row = connection.execute(
                "SELECT * FROM selection_item_reviews WHERE review_id=?",
                (review_id,),
            ).fetchone()
        return dict(row) if row is not None else {}

    def list_selection_item_reviews(self, item_id: str) -> list[dict[str, object]]:
        normalized_item_id = item_id.strip()
        with self.connect() as connection:
            item = connection.execute(
                "SELECT item_id FROM selection_items WHERE item_id=?",
                (normalized_item_id,),
            ).fetchone()
            if item is None:
                raise ValueError("候选商品不存在")
            rows = connection.execute(
                """
                SELECT * FROM selection_item_reviews
                WHERE item_id=?
                ORDER BY reviewed_at ASC, review_id ASC
                """,
                (normalized_item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_selection_scoring_snapshots(self, run_id: str) -> list[dict[str, object]]:
        normalized_run_id = run_id.strip()
        if not normalized_run_id:
            raise ValueError("run_id不能为空")
        with self.connect() as connection:
            run = connection.execute(
                "SELECT run_id FROM selection_search_runs WHERE run_id=?",
                (normalized_run_id,),
            ).fetchone()
            if run is None:
                raise ValueError("选品搜索任务不存在")
            rows = connection.execute(
                """
                SELECT s.*, i.canonical_url, i.candidate_status
                FROM selection_item_snapshots AS s
                JOIN selection_items AS i ON i.item_id=s.item_id
                WHERE s.run_id=?
                ORDER BY s.keyword ASC, s.search_rank ASC, s.snapshot_id ASC
                """,
                (normalized_run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_selection_scores(
        self,
        score_version: str,
        scores: list[dict[str, object]],
    ) -> int:
        normalized_version = score_version.strip()
        if not normalized_version or len(normalized_version) > 40:
            raise ValueError("score_version不能为空且不能超过40字符")
        values: list[tuple[object, ...]] = []
        for score in scores:
            values.append((
                int(score["snapshot_id"]),
                normalized_version,
                score.get("want_rate_score"),
                score.get("collect_rate_score"),
                score.get("browse_score"),
                float(score["relevance_score"]),
                float(score["price_fit_score"]),
                score.get("total_score"),
                json.dumps(score.get("missing_fields", []), ensure_ascii=False),
            ))
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO selection_scores (
                    snapshot_id, score_version,
                    want_rate_score, collect_rate_score, browse_score,
                    relevance_score, price_fit_score, total_score,
                    missing_fields_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id, score_version) DO UPDATE SET
                    want_rate_score=excluded.want_rate_score,
                    collect_rate_score=excluded.collect_rate_score,
                    browse_score=excluded.browse_score,
                    relevance_score=excluded.relevance_score,
                    price_fit_score=excluded.price_fit_score,
                    total_score=excluded.total_score,
                    missing_fields_json=excluded.missing_fields_json,
                    calculated_at=CURRENT_TIMESTAMP
                """,
                values,
            )
        return len(values)

    def list_selection_scores(
        self,
        run_id: str,
        *,
        score_version: str,
    ) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT sc.*, s.run_id, s.item_id, s.keyword, s.search_rank,
                       s.title_raw, s.price_text, s.want_count,
                       s.browse_count, s.collect_count,
                       i.canonical_url, i.candidate_status
                FROM selection_scores AS sc
                JOIN selection_item_snapshots AS s ON s.snapshot_id=sc.snapshot_id
                JOIN selection_items AS i ON i.item_id=s.item_id
                WHERE s.run_id=? AND sc.score_version=?
                ORDER BY sc.total_score IS NULL ASC,
                         sc.total_score DESC,
                         s.search_rank ASC
                """,
                (run_id.strip(), score_version.strip()),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_selection_successful_snapshots(self, item_id: str) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM selection_item_snapshots
                WHERE item_id=? AND detail_status='success'
                ORDER BY COALESCE(detail_observed_at, observed_at), snapshot_id
                """,
                (item_id.strip(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_selection_tracking_diagnostics(self, item_id: str) -> dict[str, object]:
        item = self.get_selection_item(item_id)
        if item is None:
            raise ValueError("候选商品不存在")
        snapshots = self.list_selection_successful_snapshots(item_id)
        from .selection_hot_candidates import calculate_multi_snapshot_trend

        with self.connect() as connection:
            tracking = connection.execute(
                """
                SELECT * FROM selection_item_tracking
                WHERE source_platform='xianyu' AND item_id=?
                """,
                (item_id.strip(),),
            ).fetchone()
        return {
            "item": item,
            "tracking": dict(tracking) if tracking is not None else None,
            "trend": calculate_multi_snapshot_trend(snapshots),
            "snapshots": snapshots,
        }

    def update_selection_tracking_priority(
        self,
        item_id: str,
        priority: int,
        reason_codes: list[str],
    ) -> None:
        bounded_priority = max(-100, min(int(priority), 100))
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE selection_item_tracking
                SET tracking_priority=?,
                    tracking_reason_codes_json=?,
                    tracking_source='automatic_trend',
                    updated_at=CURRENT_TIMESTAMP
                WHERE source_platform='xianyu' AND item_id=?
                  AND tracking_source IN (
                    'automatic', 'automatic_trend', 'historical_backfill'
                  )
                """,
                (
                    bounded_priority,
                    json.dumps(reason_codes, ensure_ascii=False),
                    item_id.strip(),
                ),
            )

    def save_selection_trend(self, trend: dict[str, object]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO selection_item_trends (
                    snapshot_id, previous_snapshot_id, interval_hours,
                    browse_delta, want_delta, collect_delta,
                    browse_per_hour, want_per_hour, collect_per_hour,
                    anomaly_codes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO UPDATE SET
                    previous_snapshot_id=excluded.previous_snapshot_id,
                    interval_hours=excluded.interval_hours,
                    browse_delta=excluded.browse_delta,
                    want_delta=excluded.want_delta,
                    collect_delta=excluded.collect_delta,
                    browse_per_hour=excluded.browse_per_hour,
                    want_per_hour=excluded.want_per_hour,
                    collect_per_hour=excluded.collect_per_hour,
                    anomaly_codes_json=excluded.anomaly_codes_json,
                    calculated_at=CURRENT_TIMESTAMP
                """,
                (
                    trend["snapshot_id"], trend.get("previous_snapshot_id"),
                    trend.get("interval_hours"), trend.get("browse_delta"),
                    trend.get("want_delta"), trend.get("collect_delta"),
                    trend.get("browse_per_hour"), trend.get("want_per_hour"),
                    trend.get("collect_per_hour"),
                    json.dumps(trend.get("anomaly_codes", []), ensure_ascii=False),
                ),
            )

    def save_selection_candidate_assessment(self, assessment: dict[str, object]) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO selection_candidate_assessments (
                    item_id, snapshot_id, score_version, total_score,
                    trend_score, current_score, reason_codes_json, explanation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    snapshot_id=excluded.snapshot_id,
                    score_version=excluded.score_version,
                    total_score=excluded.total_score,
                    trend_score=excluded.trend_score,
                    current_score=excluded.current_score,
                    reason_codes_json=excluded.reason_codes_json,
                    explanation_json=excluded.explanation_json,
                    assessed_at=CURRENT_TIMESTAMP
                """,
                (
                    assessment["item_id"], assessment["snapshot_id"],
                    assessment["score_version"], assessment.get("total_score"),
                    assessment.get("trend_score"), assessment.get("current_score"),
                    json.dumps(assessment.get("reason_codes", []), ensure_ascii=False),
                    json.dumps(assessment.get("explanation", {}), ensure_ascii=False),
                ),
            )
            score = assessment.get("total_score")
            if score is not None:
                connection.execute(
                    """
                    UPDATE selection_items
                    SET pipeline_status=CASE
                          WHEN pipeline_status IN ('reviewed','rejected','production','stopped')
                            THEN pipeline_status
                          WHEN ? >= 60 THEN 'hot_candidate'
                          ELSE 'observing'
                        END,
                        pipeline_status_updated_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE item_id=?
                    """,
                    (float(score), assessment["item_id"]),
                )

    def list_operation_candidates(self, *, limit: int = 50) -> list[dict[str, object]]:
        bounded_limit = max(1, min(int(limit), 200))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT i.item_id, i.title_raw AS title, i.canonical_url AS url,
                       i.first_seen_at, i.last_seen_at, i.pipeline_status,
                       i.candidate_status AS review_status,
                       s.price_cents, s.browse_count, s.want_count, s.collect_count,
                       s.detail_observed_at AS last_observed_at,
                       t.interval_hours, t.browse_delta, t.want_delta, t.collect_delta,
                       t.browse_per_hour, t.want_per_hour, t.collect_per_hour,
                       t.anomaly_codes_json,
                       a.total_score, a.trend_score, a.current_score,
                       a.reason_codes_json, a.explanation_json,
                       (SELECT json_group_array(keyword) FROM (
                          SELECT DISTINCT keyword
                          FROM selection_search_item_observations o
                          WHERE o.item_id=i.item_id ORDER BY keyword
                       )) AS source_keywords_json
                FROM selection_items i
                JOIN selection_candidate_assessments a ON a.item_id=i.item_id
                JOIN selection_item_snapshots s ON s.snapshot_id=a.snapshot_id
                LEFT JOIN selection_item_trends t ON t.snapshot_id=s.snapshot_id
                WHERE i.pipeline_status='hot_candidate'
                ORDER BY a.total_score DESC, i.last_seen_at DESC
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        output = []
        for raw in rows:
            row = dict(raw)
            for key in ("anomaly_codes_json", "reason_codes_json", "source_keywords_json"):
                row[key.removesuffix("_json")] = json.loads(str(row.pop(key) or "[]"))
            row["explanation"] = json.loads(str(row.pop("explanation_json") or "{}"))
            output.append(row)
        return output

    def set_selection_pipeline_status(self, item_id: str, new_status: str) -> dict[str, object]:
        if new_status not in SELECTION_PIPELINE_STATUSES:
            raise ValueError("候选阶段无效")
        transitions = {
            "new_discovery": {"observing", "stopped"},
            "observing": {"hot_candidate", "rejected", "stopped"},
            "hot_candidate": {"reviewed", "rejected", "stopped"},
            "reviewed": {"hot_candidate", "rejected", "production", "stopped"},
            "rejected": {"observing", "stopped"},
            "production": {"stopped"},
            "stopped": {"observing"},
        }
        with self.connect() as connection:
            row = connection.execute(
                "SELECT pipeline_status FROM selection_items WHERE item_id=?", (item_id.strip(),)
            ).fetchone()
            if row is None:
                raise ValueError("候选商品不存在")
            previous = str(row["pipeline_status"])
            if new_status not in transitions[previous]:
                raise ValueError(f"候选阶段不能从 {previous} 转为 {new_status}")
            connection.execute(
                """
                UPDATE selection_items SET pipeline_status=?,
                    pipeline_status_updated_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP WHERE item_id=?
                """,
                (new_status, item_id.strip()),
            )
            updated = connection.execute(
                "SELECT * FROM selection_items WHERE item_id=?", (item_id.strip(),)
            ).fetchone()
        return dict(updated)

    def list_selection_detail_candidates(
        self,
        run_id: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        bounded_limit = max(1, min(int(limit), 20))
        with self.connect() as connection:
            run = connection.execute(
                "SELECT * FROM selection_search_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise ValueError("选品搜索任务不存在")
            if run["status"] != "success":
                raise ValueError("只能补采已成功完成的搜索任务")
            rows = connection.execute(
                """
                SELECT i.*, s.detail_status AS snapshot_detail_status
                FROM selection_items AS i
                LEFT JOIN selection_item_snapshots AS s
                  ON s.run_id=i.last_search_run_id
                 AND s.item_id=i.item_id
                 AND s.keyword=i.last_search_keyword
                WHERE i.last_search_run_id=?
                  AND (s.snapshot_id IS NULL OR s.detail_status != 'success')
                ORDER BY i.last_search_rank ASC
                LIMIT ?
                """,
                (run_id, bounded_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_selection_detail_candidate_contexts(
        self,
        run_id: str,
    ) -> list[dict[str, object]]:
        normalized_run_id = run_id.strip()
        if not normalized_run_id:
            raise ValueError("search_run不能为空")
        with self.connect() as connection:
            run = connection.execute(
                "SELECT status FROM selection_search_runs WHERE run_id=?",
                (normalized_run_id,),
            ).fetchone()
            if run is None:
                raise ValueError("选品搜索任务不存在")
            if run["status"] != "success":
                raise ValueError("只能补采已成功完成的搜索任务")
            rows = connection.execute(
                """
                WITH candidates AS (
                  SELECT o.observation_id, o.run_id, o.source_platform,
                         o.item_id, o.keyword, o.observed_at,
                         o.search_rank, o.title_raw, o.canonical_url,
                         1 AS appeared_in_current_search
                  FROM selection_search_item_observations AS o
                  WHERE o.run_id=?

                  UNION ALL

                  SELECT NULL, ?, t.source_platform,
                         i.item_id,
                         COALESCE(i.last_search_keyword, r.keyword),
                         i.last_seen_at,
                         COALESCE(i.last_search_rank, 999999),
                         i.title_raw, i.canonical_url,
                         0 AS appeared_in_current_search
                  FROM selection_item_tracking AS t
                  JOIN selection_items AS i ON i.item_id=t.item_id
                  JOIN selection_search_runs AS r ON r.run_id=?
                  WHERE t.tracking_status='active'
                    AND NOT EXISTS (
                      SELECT 1
                      FROM selection_search_item_observations AS current_o
                      WHERE current_o.run_id=?
                        AND current_o.source_platform=t.source_platform
                        AND current_o.item_id=t.item_id
                    )
                )
                SELECT c.observation_id, c.run_id, c.source_platform,
                       c.item_id, c.keyword, c.observed_at,
                       c.search_rank, c.title_raw, c.canonical_url,
                       c.appeared_in_current_search,
                       t.tracking_status, t.tracking_priority,
                       t.tracking_reason_codes_json, t.tracking_source,
                       t.min_interval_hours,
                       (
                         SELECT COALESCE(s.detail_observed_at, s.observed_at)
                         FROM selection_item_snapshots AS s
                         WHERE s.item_id=c.item_id
                           AND s.detail_status='success'
                         ORDER BY COALESCE(s.detail_observed_at, s.observed_at) DESC,
                                  s.snapshot_id DESC
                         LIMIT 1
                       ) AS last_successful_detail_at,
                       (
                         SELECT COUNT(*)
                         FROM selection_item_snapshots AS counted
                         WHERE counted.item_id=c.item_id
                           AND counted.detail_status='success'
                       ) AS snapshot_count,
                       (
                         SELECT trend.want_per_hour
                         FROM selection_item_trends AS trend
                         JOIN selection_item_snapshots AS trend_snapshot
                           ON trend_snapshot.snapshot_id=trend.snapshot_id
                         WHERE trend_snapshot.item_id=c.item_id
                         ORDER BY COALESCE(trend_snapshot.detail_observed_at,
                                           trend_snapshot.observed_at) DESC,
                                  trend_snapshot.snapshot_id DESC
                         LIMIT 1
                       ) AS latest_want_per_hour,
                       EXISTS(
                         SELECT 1
                         FROM selection_item_snapshots AS current_snapshot
                         WHERE current_snapshot.run_id=c.run_id
                           AND current_snapshot.item_id=c.item_id
                           AND current_snapshot.keyword=c.keyword
                           AND current_snapshot.detail_status='success'
                       ) AS current_run_success
                FROM candidates AS c
                LEFT JOIN selection_item_tracking AS t
                  ON t.source_platform=c.source_platform
                 AND t.item_id=c.item_id
                ORDER BY c.appeared_in_current_search DESC,
                         c.search_rank ASC, c.observation_id ASC
                """,
                (
                    normalized_run_id,
                    normalized_run_id,
                    normalized_run_id,
                    normalized_run_id,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_selection_tracking_status(
        self,
        item_id: str,
        new_status: str,
        *,
        source_platform: str = "xianyu",
        priority: int | None = None,
        reason_code: str = "manual_observe",
        note: str = "",
        source: str = "manual_cli",
        changed_by: str = "owner",
    ) -> dict[str, object]:
        normalized_item_id = item_id.strip()
        normalized_platform = source_platform.strip()
        if not normalized_item_id or not normalized_platform:
            raise ValueError("来源平台和item_id不能为空")
        if new_status not in {"active", "paused", "retired"}:
            raise ValueError("追踪状态无效")
        bounded_priority = max(-100, min(int(priority or 0), 100))
        with self.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM selection_items WHERE item_id=?",
                (normalized_item_id,),
            ).fetchone() is None:
                raise ValueError("候选商品不存在")
            previous = connection.execute(
                """
                SELECT tracking_status FROM selection_item_tracking
                WHERE source_platform=? AND item_id=?
                """,
                (normalized_platform, normalized_item_id),
            ).fetchone()
            previous_status = str(previous["tracking_status"]) if previous else None
            connection.execute(
                """
                INSERT INTO selection_item_tracking (
                    source_platform, item_id, tracking_status,
                    tracking_priority, tracking_reason_codes_json,
                    tracking_source, manual_note, paused_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?,
                          CASE WHEN ?='paused' THEN CURRENT_TIMESTAMP ELSE NULL END)
                ON CONFLICT(source_platform, item_id) DO UPDATE SET
                    tracking_status=excluded.tracking_status,
                    tracking_priority=excluded.tracking_priority,
                    tracking_reason_codes_json=excluded.tracking_reason_codes_json,
                    tracking_source=excluded.tracking_source,
                    manual_note=excluded.manual_note,
                    paused_at=CASE WHEN excluded.tracking_status='paused'
                                   THEN CURRENT_TIMESTAMP ELSE NULL END,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    normalized_platform, normalized_item_id, new_status,
                    bounded_priority, json.dumps([reason_code], ensure_ascii=False),
                    source, note or None, new_status,
                ),
            )
            connection.execute(
                """
                INSERT INTO selection_item_tracking_events (
                    source_platform, item_id, previous_status, new_status,
                    reason_code, note, source, changed_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_platform, normalized_item_id, previous_status,
                    new_status, reason_code or None, note or None,
                    source, changed_by,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM selection_item_tracking
                WHERE source_platform=? AND item_id=?
                """,
                (normalized_platform, normalized_item_id),
            ).fetchone()
        return dict(row) if row is not None else {}

    def save_selection_item_snapshot(
        self,
        item_id: str,
        *,
        price_cents: int | None,
        price_text: str,
        want_count: int | None,
        browse_count: int | None,
        collect_count: int | None,
        detail_status: str = "success",
        detail_error_code: str = "",
        observation_run_id: str | None = None,
        observation_keyword: str | None = None,
        observation_search_rank: int | None = None,
    ) -> dict[str, object]:
        item = self.get_selection_item(item_id)
        if item is None:
            raise ValueError("候选商品不存在，请先执行搜索入库")
        run_id = str(observation_run_id or item.get("last_search_run_id") or "").strip()
        keyword = str(observation_keyword or item.get("last_search_keyword") or "").strip()
        rank = observation_search_rank if observation_search_rank is not None else item.get("last_search_rank")
        if not run_id or not keyword or not isinstance(rank, int):
            raise ValueError("候选商品缺少搜索批次上下文，请重新搜索后再补采详情")
        parse_status = "parsed" if price_cents is not None else (
            "unparseable" if price_text else "missing"
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO selection_item_snapshots (
                    run_id, item_id, keyword, search_rank, title_raw,
                    price_cents, price_text, price_parse_status,
                    want_count, browse_count, collect_count,
                    detail_status, detail_error_code, detail_observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(run_id, item_id, keyword) DO UPDATE SET
                    observed_at=CURRENT_TIMESTAMP,
                    search_rank=excluded.search_rank,
                    title_raw=excluded.title_raw,
                    price_cents=excluded.price_cents,
                    price_text=excluded.price_text,
                    price_parse_status=excluded.price_parse_status,
                    want_count=excluded.want_count,
                    browse_count=excluded.browse_count,
                    collect_count=excluded.collect_count,
                    detail_status=excluded.detail_status,
                    detail_error_code=excluded.detail_error_code,
                    detail_observed_at=CURRENT_TIMESTAMP
                """,
                (
                    run_id,
                    item_id,
                    keyword,
                    rank,
                    str(item["title_raw"]),
                    price_cents,
                    price_text,
                    parse_status,
                    want_count,
                    browse_count,
                    collect_count,
                    detail_status,
                    detail_error_code[:120] or None,
                ),
            )
            if detail_status == "success":
                connection.execute(
                    """
                    INSERT OR IGNORE INTO selection_item_tracking (
                        source_platform, item_id, tracking_status,
                        tracking_priority, tracking_reason_codes_json,
                        tracking_source
                    ) VALUES (
                        'xianyu', ?, 'active', 0,
                        '["successful_detail_baseline"]', 'automatic'
                    )
                    """,
                    (item_id,),
                )
                connection.execute(
                    """
                    UPDATE selection_items
                    SET pipeline_status=CASE WHEN pipeline_status='new_discovery'
                                             THEN 'observing' ELSE pipeline_status END,
                        pipeline_status_updated_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE item_id=?
                    """,
                    (item_id,),
                )
            row = connection.execute(
                """
                SELECT * FROM selection_item_snapshots
                WHERE run_id=? AND item_id=? AND keyword=?
                """,
                (run_id, item_id, keyword),
            ).fetchone()
        saved = dict(row) if row is not None else {}
        if detail_status == "success":
            from .selection_hot_candidates import refresh_item_assessment

            refresh_item_assessment(self, item_id)
        return saved

    def fail_selection_search_run(
        self,
        run_id: str,
        error_code: str,
        *,
        blocked: bool = False,
        source_api: str = "",
        http_status: int | None = None,
    ) -> dict[str, object]:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE selection_search_runs
                SET status=?,
                    source_api=?,
                    http_status=?,
                    error_code=?,
                    finished_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE run_id=? AND status='running'
                """,
                (
                    "blocked" if blocked else "failed",
                    source_api or None,
                    http_status,
                    error_code[:120],
                    run_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM selection_search_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else {}

    @staticmethod
    def _ensure_orders_account_column(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(orders)").fetchall()}
        if "account_id" not in columns:
            connection.execute("ALTER TABLE orders ADD COLUMN account_id INTEGER REFERENCES accounts(id)")

    @staticmethod
    def _ensure_order_delivery_columns(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(orders)").fetchall()}
        additions = {
            "listing_item_id": "TEXT NOT NULL DEFAULT ''",
            "buyer_id": "TEXT NOT NULL DEFAULT ''",
            "chat_id": "TEXT NOT NULL DEFAULT ''",
            "event_fingerprint": "TEXT NOT NULL DEFAULT ''",
            "delivery_message_hash": "TEXT NOT NULL DEFAULT ''",
            "message_sent_at": "TEXT",
            "platform_confirm_status": "TEXT NOT NULL DEFAULT 'pending'",
            "platform_confirmed_at": "TEXT",
            "group_status": "TEXT NOT NULL DEFAULT 'not_applicable'",
            "group_attempts": "INTEGER NOT NULL DEFAULT 0",
            "group_exempted_at": "TEXT",
            "group_event_fingerprint": "TEXT NOT NULL DEFAULT ''",
            "detected_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE orders ADD COLUMN {name} {declaration}")

    @staticmethod
    def _ensure_account_listings_source_column(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(account_listings)").fetchall()
        }
        if "source_kind" not in columns:
            connection.execute(
                "ALTER TABLE account_listings "
                "ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'account_snapshot'"
            )

    @staticmethod
    def _ensure_selection_item_context_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(selection_items)").fetchall()
        }
        additions = {
            "last_search_run_id": "TEXT",
            "last_search_keyword": "TEXT",
            "last_search_rank": "INTEGER",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE selection_items ADD COLUMN {name} {declaration}"
                )

    @staticmethod
    def _ensure_selection_review_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(selection_items)").fetchall()
        }
        additions = {
            "candidate_status": "TEXT NOT NULL DEFAULT 'unreviewed'",
            "candidate_status_updated_at": "TEXT",
            "last_review_id": "INTEGER",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE selection_items ADD COLUMN {name} {declaration}"
                )

    @staticmethod
    def _ensure_selection_pipeline_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(selection_items)").fetchall()
        }
        additions = {
            "pipeline_status": "TEXT NOT NULL DEFAULT 'new_discovery'",
            "pipeline_status_updated_at": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE selection_items ADD COLUMN {name} {declaration}"
                )

    @staticmethod
    def _backfill_selection_tracking(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO selection_item_tracking (
                source_platform, item_id, tracking_status,
                tracking_priority, tracking_reason_codes_json, tracking_source
            )
            SELECT 'xianyu', s.item_id, 'active', 0,
                   '["successful_detail_baseline"]', 'historical_backfill'
            FROM selection_item_snapshots AS s
            WHERE s.detail_status='success'
            GROUP BY s.item_id
            """
        )

    @staticmethod
    def _ensure_products_catalog_status_column(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(products)").fetchall()}
        if "catalog_status" not in columns:
            connection.execute(
                "ALTER TABLE products ADD COLUMN catalog_status TEXT NOT NULL DEFAULT 'active'"
            )

    @staticmethod
    def _ensure_product_knowledge_columns(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(products)").fetchall()}
        additions = {
            "knowledge_text": "TEXT NOT NULL DEFAULT ''",
            "knowledge_hash": "TEXT NOT NULL DEFAULT ''",
            "knowledge_chars": "INTEGER NOT NULL DEFAULT 0",
            "knowledge_source_path": "TEXT NOT NULL DEFAULT ''",
            "knowledge_file_count": "INTEGER NOT NULL DEFAULT 0",
            "knowledge_updated_at": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE products ADD COLUMN {name} {declaration}")

    @staticmethod
    def _ensure_account_binding_columns(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(accounts)").fetchall()}
        additions = {
            "binding_status": "TEXT NOT NULL DEFAULT 'unbound'",
            "binding_confirmed_at": "TEXT",
            "session_last_checked_at": "TEXT",
            "session_last_error": "TEXT NOT NULL DEFAULT ''",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE accounts ADD COLUMN {name} {declaration}")

    @staticmethod
    def _ensure_account_delivery_columns(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(accounts)").fetchall()}
        additions = {
            "delivery_enabled": "INTEGER NOT NULL DEFAULT 0",
            "auto_confirm_delivery": "INTEGER NOT NULL DEFAULT 1",
            "auto_free_group": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE accounts ADD COLUMN {name} {declaration}")

    @staticmethod
    def _migrate_auto_reply_to_siliconflow(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE auto_reply_settings
            SET base_url='https://api.siliconflow.cn/v1',
                model=CASE
                    WHEN model='deepseek-v4-pro' THEN 'deepseek-ai/DeepSeek-V4-Pro'
                    ELSE 'deepseek-ai/DeepSeek-V4-Flash'
                END,
                updated_at=CURRENT_TIMESTAMP
            WHERE base_url LIKE 'https://api.deepseek.com%'
              AND model IN ('deepseek-v4-flash', 'deepseek-v4-pro')
            """
        )

    def sync_products(self, scanned: list[ScannedProduct]) -> None:
        with self.connect() as connection:
            for product in scanned:
                existing = connection.execute(
                    "SELECT zip_hash FROM products WHERE dir_name = ?", (product.dir_name,)
                ).fetchone()
                changed = bool(existing and existing["zip_hash"] and existing["zip_hash"] != product.zip_hash)
                connection.execute(
                    """
                    INSERT INTO products (
                        dir_name, number, name, title, knowledge_text, knowledge_hash,
                        knowledge_chars, zip_name, zip_hash, zip_size, image_count,
                        quality_status, quality_errors_json, scanned_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dir_name) DO UPDATE SET
                        number=excluded.number,
                        name=excluded.name,
                        title=excluded.title,
                        knowledge_text=CASE
                            WHEN products.knowledge_source_path <> '' THEN products.knowledge_text
                            ELSE excluded.knowledge_text
                        END,
                        knowledge_hash=CASE
                            WHEN products.knowledge_source_path <> '' THEN products.knowledge_hash
                            ELSE excluded.knowledge_hash
                        END,
                        knowledge_chars=CASE
                            WHEN products.knowledge_source_path <> '' THEN products.knowledge_chars
                            ELSE excluded.knowledge_chars
                        END,
                        zip_name=excluded.zip_name,
                        zip_hash=excluded.zip_hash,
                        zip_size=excluded.zip_size,
                        image_count=excluded.image_count,
                        quality_status=excluded.quality_status,
                        quality_errors_json=excluded.quality_errors_json,
                        scanned_at=excluded.scanned_at,
                        share_needs_review=CASE
                            WHEN products.zip_hash <> '' AND products.zip_hash <> excluded.zip_hash THEN 1
                            ELSE products.share_needs_review
                        END,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        product.dir_name,
                        product.number,
                        product.name,
                        product.title,
                        product.knowledge_text,
                        product.knowledge_hash,
                        product.knowledge_chars,
                        product.zip_name,
                        product.zip_hash,
                        product.zip_size,
                        product.image_count,
                        product.quality_status,
                        json.dumps(product.quality_errors, ensure_ascii=False),
                        product.scanned_at,
                    ),
                )
                if changed:
                    self._log(connection, "zip_changed", product.dir_name, {"zip_hash": product.zip_hash})

    def ensure_default_accounts(self) -> None:
        """首次迁移：现有商品归旧账号，新账号为空并成为当前账号。"""
        with self.connect() as connection:
            if connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
                return
            old_id = connection.execute(
                "INSERT INTO accounts (name, is_active) VALUES (?, 0)", ("旧账号（已有商品）",)
            ).lastrowid
            new_id = connection.execute(
                "INSERT INTO accounts (name, is_active) VALUES (?, 1)", ("新账号（当前）",)
            ).lastrowid
            connection.execute(
                """
                INSERT INTO account_products (
                    account_id, product_dir_name, enabled, suggested_price_cents,
                    confirmed_price_cents, listing_url, listing_status
                )
                SELECT ?, dir_name, 1, suggested_price_cents, confirmed_price_cents,
                       listing_url, listing_status
                FROM products
                """,
                (old_id,),
            )
            self._log(
                connection,
                "accounts_migrated",
                str(new_id),
                {"old_account_id": old_id, "new_account_id": new_id},
            )

    def ensure_legacy_product_policy(self) -> None:
        """把旧账号已有关联商品标记为历史品，防止新账号误发。"""
        with self.connect() as connection:
            old_account = connection.execute(
                "SELECT id FROM accounts WHERE name LIKE '旧账号%' ORDER BY id LIMIT 1"
            ).fetchone()
            if old_account is None:
                return
            connection.execute(
                """
                UPDATE products
                SET catalog_status = 'legacy', updated_at=CURRENT_TIMESTAMP
                WHERE dir_name IN (
                    SELECT product_dir_name FROM account_products
                    WHERE account_id = ? AND enabled = 1
                )
                """,
                (old_account["id"],),
            )

    def enforce_single_account_mode(self, display_name: str = "七月账号") -> dict[str, object]:
        """Keep the July account as the only visible/activatable account.

        Historical account rows and their orders remain in the database for audit,
        but they are archived and cannot become active again through the API.
        """
        with self.connect() as connection:
            target = connection.execute(
                """
                SELECT id FROM accounts
                WHERE is_archived = 0 AND name LIKE '新账号%'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if target is None:
                target = connection.execute(
                    """
                    SELECT id FROM accounts
                    WHERE is_archived = 0 AND is_active = 1
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
            if target is None:
                target = connection.execute(
                    "SELECT id FROM accounts ORDER BY id DESC LIMIT 1"
                ).fetchone()
            if target is None:
                raise RuntimeError("没有可用的闲鱼账号")

            account_id = int(target["id"])
            connection.execute(
                """
                UPDATE accounts
                SET is_active = 0, is_archived = 1, delivery_enabled = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id <> ?
                """,
                (account_id,),
            )
            connection.execute(
                """
                UPDATE accounts
                SET name = ?, is_active = 1, is_archived = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (display_name, account_id),
            )
            self._log(
                connection,
                "single_account_mode_enforced",
                str(account_id),
                {"display_name": display_name},
            )
        return self.get_active_account()

    def list_accounts(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.*, COUNT(CASE WHEN ap.enabled = 1 THEN 1 END) AS product_count
                FROM accounts a
                LEFT JOIN account_products ap ON ap.account_id = a.id
                WHERE a.is_archived = 0
                GROUP BY a.id
                ORDER BY a.id
                """
            ).fetchall()
        return [
            {
                **dict(row),
                "is_active": bool(row["is_active"]),
                "is_archived": bool(row["is_archived"]),
                "delivery_enabled": bool(row["delivery_enabled"]),
                "auto_confirm_delivery": bool(row["auto_confirm_delivery"]),
                "auto_free_group": bool(row["auto_free_group"]),
            }
            for row in rows
        ]

    def get_active_account(self) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE is_active = 1 AND is_archived = 0"
            ).fetchone()
        if row is None:
            raise RuntimeError("没有当前账号")
        item = dict(row)
        item["is_active"] = True
        item["is_archived"] = bool(item["is_archived"])
        item["delivery_enabled"] = bool(item["delivery_enabled"])
        item["auto_confirm_delivery"] = bool(item["auto_confirm_delivery"])
        item["auto_free_group"] = bool(item["auto_free_group"])
        return item

    def get_account(self, account_id: int) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE id = ? AND is_archived = 0", (account_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["is_active"] = bool(item["is_active"])
        item["is_archived"] = bool(item["is_archived"])
        item["delivery_enabled"] = bool(item["delivery_enabled"])
        item["auto_confirm_delivery"] = bool(item["auto_confirm_delivery"])
        item["auto_free_group"] = bool(item["auto_free_group"])
        return item

    def update_delivery_settings(
        self,
        account_id: int,
        *,
        enabled: bool,
        auto_confirm: bool,
        auto_free_group: bool | None = None,
    ) -> dict[str, object] | None:
        with self.connect() as connection:
            exists = connection.execute(
                "SELECT id FROM accounts WHERE id = ? AND is_archived = 0", (account_id,)
            ).fetchone()
            if exists is None:
                return None
            current = connection.execute(
                "SELECT auto_free_group FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            resolved_auto_free_group = (
                bool(current["auto_free_group"])
                if auto_free_group is None and current is not None
                else bool(auto_free_group)
            )
            connection.execute(
                """
                UPDATE accounts
                SET delivery_enabled=?, auto_confirm_delivery=?, auto_free_group=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (int(enabled), int(auto_confirm), int(resolved_auto_free_group), account_id),
            )
            self._log(
                connection,
                "delivery_settings_updated",
                str(account_id),
                {
                    "enabled": enabled,
                    "auto_confirm": auto_confirm,
                    "auto_free_group": resolved_auto_free_group,
                },
            )
        return self.get_account(account_id)

    def update_account_binding(
        self,
        account_id: int,
        status: str,
        *,
        error: str = "",
        confirmed: bool = False,
    ) -> dict[str, object] | None:
        allowed = {"unbound", "starting", "waiting_scan", "detected", "bound", "expired", "error"}
        if status not in allowed:
            raise ValueError(f"未知绑定状态：{status}")
        with self.connect() as connection:
            exists = connection.execute(
                "SELECT id FROM accounts WHERE id = ? AND is_archived = 0", (account_id,)
            ).fetchone()
            if exists is None:
                return None
            confirmed_sql = ", binding_confirmed_at=CURRENT_TIMESTAMP" if confirmed else ""
            connection.execute(
                f"""
                UPDATE accounts
                SET binding_status=?, session_last_checked_at=CURRENT_TIMESTAMP,
                    session_last_error=?, updated_at=CURRENT_TIMESTAMP{confirmed_sql}
                WHERE id=?
                """,
                (status, error, account_id),
            )
            self._log(
                connection,
                "account_binding_status",
                str(account_id),
                {"status": status, "confirmed": confirmed, "has_error": bool(error)},
            )
        return self.get_account(account_id)

    def activate_account(self, account_id: int) -> dict[str, object] | None:
        with self.connect() as connection:
            target = connection.execute(
                "SELECT * FROM accounts WHERE id = ? AND is_archived = 0", (account_id,)
            ).fetchone()
            if target is None:
                return None
            connection.execute("UPDATE accounts SET is_active = 0 WHERE is_active = 1")
            connection.execute(
                "UPDATE accounts SET is_active = 1, updated_at=CURRENT_TIMESTAMP WHERE id = ?",
                (account_id,),
            )
            self._log(connection, "account_activated", str(account_id), {})
        return self.get_active_account()

    def list_products(
        self,
        account_id: int | None = None,
        *,
        include_listing_only: bool = False,
    ) -> list[dict[str, object]]:
        if account_id is None:
            account_id = int(self.get_active_account()["id"])
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    p.dir_name, p.number, p.name, p.title, p.knowledge_text,
                    p.knowledge_hash, p.knowledge_chars, p.knowledge_source_path,
                    p.knowledge_file_count, p.knowledge_updated_at, p.zip_name, p.zip_hash,
                    p.zip_size, p.image_count, p.quality_status, p.quality_errors_json,
                    p.scanned_at, p.share_url, p.share_code, p.share_verified,
                    p.share_needs_review, p.catalog_status, p.updated_at,
                    ? AS account_id,
                    COALESCE(ap.enabled, 0) AS enabled_for_account,
                    ap.suggested_price_cents,
                    ap.confirmed_price_cents,
                    COALESCE(ap.listing_url, '') AS listing_url,
                    COALESCE(ap.listing_status, 'draft') AS listing_status
                FROM products p
                LEFT JOIN account_products ap
                  ON ap.product_dir_name = p.dir_name AND ap.account_id = ?
                WHERE (? = 1 OR p.catalog_status <> 'listing_only')
                ORDER BY p.number
                """,
                (account_id, account_id, int(include_listing_only)),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["quality_errors"] = json.loads(str(item.pop("quality_errors_json")))
            for key in ("share_verified", "share_needs_review", "enabled_for_account"):
                item[key] = bool(item[key])
            result.append(item)
        return result

    def get_product(self, dir_name: str, account_id: int | None = None) -> dict[str, object] | None:
        return next((item for item in self.list_products(account_id) if item["dir_name"] == dir_name), None)

    def set_product_knowledge_folder(
        self,
        dir_name: str,
        *,
        source_path: str,
        knowledge_text: str,
        knowledge_hash: str,
        knowledge_chars: int,
        file_count: int,
    ) -> dict[str, object] | None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE products
                SET knowledge_source_path=?, knowledge_text=?, knowledge_hash=?,
                    knowledge_chars=?, knowledge_file_count=?,
                    knowledge_updated_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE dir_name=?
                """,
                (
                    source_path,
                    knowledge_text,
                    knowledge_hash,
                    int(knowledge_chars),
                    int(file_count),
                    dir_name,
                ),
            )
            if cursor.rowcount == 0:
                return None
            self._log(
                connection,
                "product_knowledge_loaded",
                dir_name,
                {"source_path": source_path, "file_count": int(file_count), "chars": int(knowledge_chars)},
            )
        return self.get_product(dir_name)

    def clear_product_knowledge_folder(self, dir_name: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE products
                SET knowledge_source_path='', knowledge_text='', knowledge_hash='',
                    knowledge_chars=0, knowledge_file_count=0,
                    knowledge_updated_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE dir_name=?
                """,
                (dir_name,),
            )
            if cursor.rowcount:
                self._log(connection, "product_knowledge_cleared", dir_name, {})
            return bool(cursor.rowcount)

    def update_product(
        self, dir_name: str, fields: dict[str, object], account_id: int | None = None
    ) -> dict[str, object] | None:
        if account_id is None:
            account_id = int(self.get_active_account()["id"])
        global_updates = {key: value for key, value in fields.items() if key in GLOBAL_PRODUCT_FIELDS}
        account_updates = {key: value for key, value in fields.items() if key in ACCOUNT_PRODUCT_FIELDS}
        wants_enable = account_updates.get("enabled_for_account") is True
        if "enabled_for_account" in account_updates:
            account_updates["enabled"] = account_updates.pop("enabled_for_account")

        with self.connect() as connection:
            exists = connection.execute(
                "SELECT catalog_status FROM products WHERE dir_name = ?", (dir_name,)
            ).fetchone()
            if exists is None:
                return None

            current_binding = connection.execute(
                """
                SELECT enabled FROM account_products
                WHERE account_id = ? AND product_dir_name = ?
                """,
                (account_id, dir_name),
            ).fetchone()
            already_enabled = bool(current_binding and current_binding["enabled"])
            if wants_enable and exists["catalog_status"] == "legacy" and not already_enabled:
                raise ValueError("旧账号历史商品不能加入其他账号")

            if global_updates.get("share_verified"):
                global_updates["share_needs_review"] = 0
            if global_updates:
                assignments = ", ".join(f"{key} = ?" for key in global_updates)
                values = [int(value) if isinstance(value, bool) else value for value in global_updates.values()]
                connection.execute(
                    f"UPDATE products SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE dir_name = ?",
                    (*values, dir_name),
                )

            if account_updates:
                connection.execute(
                    "INSERT OR IGNORE INTO account_products (account_id, product_dir_name) VALUES (?, ?)",
                    (account_id, dir_name),
                )
                assignments = ", ".join(f"{key} = ?" for key in account_updates)
                values = [int(value) if isinstance(value, bool) else value for value in account_updates.values()]
                connection.execute(
                    f"""
                    UPDATE account_products
                    SET {assignments}, updated_at=CURRENT_TIMESTAMP
                    WHERE account_id = ? AND product_dir_name = ?
                    """,
                    (*values, account_id, dir_name),
                )
            self._log(
                connection,
                "product_updated",
                dir_name,
                {"account_id": account_id, "fields": sorted(global_updates | account_updates)},
            )
        return self.get_product(dir_name, account_id)

    def get_product_by_listing_item_id(
        self, item_id: str, account_id: int | None = None
    ) -> dict[str, object] | None:
        normalized = str(item_id or "").strip()
        if not normalized:
            return None
        matches = []
        for product in self.list_products(account_id, include_listing_only=True):
            listing_url = str(product.get("listing_url") or "")
            if (
                product["enabled_for_account"]
                and product["listing_status"] == "published"
                and f"id={normalized}" in listing_url
            ):
                matches.append(product)
        return matches[0] if len(matches) == 1 else None

    def configure_listing_delivery(
        self,
        account_id: int,
        item_id: str,
        *,
        share_url: str,
        share_code: str = "",
        share_verified: bool = True,
    ) -> dict[str, object]:
        normalized_item_id = str(item_id or "").strip()
        normalized_url = str(share_url or "").strip()
        normalized_code = str(share_code or "").strip()[:16]
        if not normalized_item_id.isdigit() or len(normalized_item_id) < 8:
            raise ValueError("闲鱼商品 ID 格式不正确")
        parsed = urlparse(normalized_url)
        if parsed.scheme != "https" or parsed.netloc.lower() != "pan.baidu.com" or not parsed.path.startswith("/s/"):
            raise ValueError("交付链接必须是百度网盘 HTTPS 分享地址")

        synthetic_dir = f"__listing__{normalized_item_id}"
        with self.connect() as connection:
            listing = connection.execute(
                """
                SELECT title, listing_url, price_cents
                FROM account_listings
                WHERE account_id=? AND item_id=? AND is_active=1
                """,
                (account_id, normalized_item_id),
            ).fetchone()
            if listing is None:
                raise ValueError("当前账号没有找到这件在售商品")
            title = str(listing["title"] or f"闲鱼商品 {normalized_item_id}").strip()
            connection.execute(
                """
                INSERT INTO products (
                    dir_name, number, name, title, zip_name, zip_hash, zip_size,
                    image_count, quality_status, quality_errors_json, scanned_at,
                    share_url, share_code, share_verified, share_needs_review,
                    catalog_status, updated_at
                ) VALUES (?, 0, ?, ?, '', '', 0, 0, 'passed', '[]',
                          CURRENT_TIMESTAMP, ?, ?, ?, 0, 'listing_only', CURRENT_TIMESTAMP)
                ON CONFLICT(dir_name) DO UPDATE SET
                    name=excluded.name,
                    title=excluded.title,
                    quality_status='passed',
                    quality_errors_json='[]',
                    share_url=excluded.share_url,
                    share_code=excluded.share_code,
                    share_verified=excluded.share_verified,
                    share_needs_review=0,
                    catalog_status='listing_only',
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    synthetic_dir,
                    title,
                    title,
                    normalized_url,
                    normalized_code,
                    int(share_verified),
                ),
            )
            connection.execute(
                """
                INSERT INTO account_products (
                    account_id, product_dir_name, enabled, confirmed_price_cents,
                    listing_url, listing_status, updated_at
                ) VALUES (?, ?, 1, ?, ?, 'published', CURRENT_TIMESTAMP)
                ON CONFLICT(account_id, product_dir_name) DO UPDATE SET
                    enabled=1,
                    confirmed_price_cents=COALESCE(
                        excluded.confirmed_price_cents,
                        account_products.confirmed_price_cents
                    ),
                    listing_url=excluded.listing_url,
                    listing_status='published',
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    account_id,
                    synthetic_dir,
                    listing["price_cents"],
                    str(listing["listing_url"] or f"https://www.goofish.com/item?id={normalized_item_id}"),
                ),
            )
            connection.execute(
                """
                UPDATE account_listings
                SET matched_product_dir_name=?, synced_at=CURRENT_TIMESTAMP
                WHERE account_id=? AND item_id=?
                """,
                (synthetic_dir, account_id, normalized_item_id),
            )
            self._log(
                connection,
                "listing_delivery_configured",
                normalized_item_id,
                {
                    "account_id": account_id,
                    "has_share_code": bool(normalized_code),
                    "share_verified": bool(share_verified),
                },
            )

        configured = next(
            (
                item
                for item in self.list_live_listings(account_id)
                if str(item.get("item_id")) == normalized_item_id
            ),
            None,
        )
        if configured is None:
            raise RuntimeError("交付配置已保存，但商品状态读取失败")
        return configured

    def sync_live_listings(
        self,
        account_id: int,
        listings: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Store the current account inventory and reconcile explicit product matches."""
        active_ids = {str(item.get("item_id") or "").strip() for item in listings}
        active_ids.discard("")
        with self.connect() as connection:
            connection.execute(
                "UPDATE account_listings SET is_active = 0 WHERE account_id = ?",
                (account_id,),
            )
            for item in listings:
                item_id = str(item.get("item_id") or "").strip()
                if not item_id:
                    continue
                matched = str(item.get("matched_product_dir_name") or "").strip() or None
                price = item.get("price_cents")
                connection.execute(
                    """
                    INSERT INTO account_listings (
                        account_id, item_id, title, listing_url, image_url,
                        price_cents, source_text, source_kind, matched_product_dir_name,
                        is_active, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'account_snapshot', ?, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(account_id, item_id) DO UPDATE SET
                        title=excluded.title,
                        listing_url=excluded.listing_url,
                        image_url=excluded.image_url,
                        price_cents=excluded.price_cents,
                        source_text=excluded.source_text,
                        source_kind='account_snapshot',
                        matched_product_dir_name=COALESCE(
                            excluded.matched_product_dir_name,
                            account_listings.matched_product_dir_name
                        ),
                        is_active=1,
                        synced_at=CURRENT_TIMESTAMP
                    """,
                    (
                        account_id,
                        item_id,
                        str(item.get("title") or "").strip(),
                        str(item.get("url") or "").strip(),
                        str(item.get("image") or "").strip(),
                        price if isinstance(price, int) else None,
                        str(item.get("text") or "").strip(),
                        matched,
                    ),
                )
                if matched:
                    connection.execute(
                        "UPDATE products SET catalog_status = 'active', updated_at=CURRENT_TIMESTAMP WHERE dir_name = ?",
                        (matched,),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO account_products (account_id, product_dir_name) VALUES (?, ?)",
                        (account_id, matched),
                    )
                    connection.execute(
                        """
                        UPDATE account_products
                        SET enabled = 1, listing_url = ?, listing_status = 'published',
                            confirmed_price_cents = COALESCE(?, confirmed_price_cents),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE account_id = ? AND product_dir_name = ?
                        """,
                        (str(item.get("url") or "").strip(), price, account_id, matched),
                    )

            # Missing from one scrape does not prove a listing was removed.
            # Lazy loading and review delays can both produce incomplete pages,
            # so only an explicit user action may pause a configured product.
            self._log(
                connection,
                "live_listings_synced",
                str(account_id),
                {"active_count": len(active_ids)},
            )
        return self.list_live_listings(account_id)

    def map_live_listing_to_product(
        self,
        account_id: int,
        item_id: str,
        product_dir_name: str,
    ) -> dict[str, object]:
        """Persist one explicit listing-to-product choice by immutable item ID."""
        normalized_item_id = str(item_id or "").strip()
        normalized_dir = str(product_dir_name or "").strip()
        if not normalized_item_id.isdigit() or len(normalized_item_id) < 8:
            raise ValueError("闲鱼商品 ID 格式不正确")
        if not normalized_dir:
            raise ValueError("请选择要关联的本地商品")

        with self.connect() as connection:
            listing = connection.execute(
                """
                SELECT item_id, listing_url, price_cents, matched_product_dir_name
                FROM account_listings
                WHERE account_id = ? AND item_id = ? AND is_active = 1
                """,
                (account_id, normalized_item_id),
            ).fetchone()
            if listing is None:
                raise ValueError("没有找到当前账号的这件在售商品，请先重新同步")

            existing_match = str(listing["matched_product_dir_name"] or "").strip()
            if existing_match and existing_match != normalized_dir:
                raise ValueError("这件闲鱼商品已经关联了其他本地商品")

            product = connection.execute(
                """
                SELECT dir_name, catalog_status
                FROM products
                WHERE dir_name = ?
                """,
                (normalized_dir,),
            ).fetchone()
            if product is None:
                raise ValueError("本地商品不存在，请先同步商品库")
            if str(product["catalog_status"]) in {"legacy", "listing_only"}:
                raise ValueError("历史归档或独立交付商品不能用于新的商品映射")

            # Mapping only establishes which local product belongs to this
            # immutable listing ID. Delivery readiness is evaluated separately,
            # so an incomplete product may be mapped first without ever being
            # auto-delivered before its files and share link pass validation.

            other_listing = connection.execute(
                """
                SELECT item_id
                FROM account_listings
                WHERE account_id = ? AND matched_product_dir_name = ?
                  AND item_id <> ? AND is_active = 1
                LIMIT 1
                """,
                (account_id, normalized_dir, normalized_item_id),
            ).fetchone()
            if other_listing is not None:
                raise ValueError(
                    f"该本地商品已关联闲鱼商品 {other_listing['item_id']}，请先核对后再操作"
                )

            configured = connection.execute(
                """
                SELECT listing_url, listing_status
                FROM account_products
                WHERE account_id = ? AND product_dir_name = ?
                """,
                (account_id, normalized_dir),
            ).fetchone()
            if configured is not None:
                configured_url = str(configured["listing_url"] or "").strip()
                configured_item_id = ""
                try:
                    configured_item_id = parse_qs(urlparse(configured_url).query).get(
                        "id", [""]
                    )[0].strip()
                except Exception:
                    configured_item_id = ""
                if (
                    configured_item_id
                    and configured_item_id != normalized_item_id
                    and str(configured["listing_status"] or "") == "published"
                ):
                    raise ValueError(
                        f"该本地商品已配置闲鱼商品 {configured_item_id}，请先核对后再操作"
                    )

            canonical_url = f"https://www.goofish.com/item?id={normalized_item_id}"
            connection.execute(
                """
                UPDATE account_listings
                SET matched_product_dir_name = ?, synced_at = CURRENT_TIMESTAMP
                WHERE account_id = ? AND item_id = ?
                """,
                (normalized_dir, account_id, normalized_item_id),
            )
            connection.execute(
                "INSERT OR IGNORE INTO account_products (account_id, product_dir_name) VALUES (?, ?)",
                (account_id, normalized_dir),
            )
            connection.execute(
                """
                UPDATE account_products
                SET enabled = 1, listing_url = ?, listing_status = 'published',
                    confirmed_price_cents = COALESCE(?, confirmed_price_cents),
                    updated_at = CURRENT_TIMESTAMP
                WHERE account_id = ? AND product_dir_name = ?
                """,
                (canonical_url, listing["price_cents"], account_id, normalized_dir),
            )
            connection.execute(
                """
                UPDATE products
                SET catalog_status = 'active', updated_at = CURRENT_TIMESTAMP
                WHERE dir_name = ?
                """,
                (normalized_dir,),
            )
            self._log(
                connection,
                "live_listing_mapped",
                normalized_item_id,
                {"account_id": account_id, "product_dir_name": normalized_dir},
            )

        mapped = next(
            (
                item
                for item in self.list_live_listings(account_id)
                if str(item.get("item_id") or "") == normalized_item_id
            ),
            None,
        )
        if mapped is None:
            raise RuntimeError("商品关联已保存，但读取关联结果失败")
        return mapped

    def reconcile_configured_listings(self, account_id: int) -> list[dict[str, object]]:
        """Safely merge explicit published-product records into the account view.

        A remote profile scrape can be incomplete because of lazy loading or
        platform review delays. This method only adds records that the owner has
        explicitly marked as published and never deactivates profile-snapshot
        rows. Configured-only rows are removed from the active view only after
        their product is explicitly paused or disabled.
        """
        with self.connect() as connection:
            configured = connection.execute(
                """
                SELECT
                    ap.product_dir_name, ap.listing_url, ap.confirmed_price_cents,
                    p.title, p.name
                FROM account_products ap
                JOIN products p ON p.dir_name = ap.product_dir_name
                WHERE ap.account_id = ? AND ap.enabled = 1
                  AND ap.listing_status = 'published'
                """,
                (account_id,),
            ).fetchall()

            active_dirs: set[str] = set()
            merged_count = 0
            for row in configured:
                listing_url = str(row["listing_url"] or "").strip()
                parsed = urlparse("")
                try:
                    parsed = urlparse(listing_url)
                    item_id = parse_qs(parsed.query).get("id", [""])[0].strip()
                except Exception:
                    item_id = ""
                if parsed.netloc not in {"www.goofish.com", "goofish.com"} or not item_id.isdigit():
                    continue
                product_dir = str(row["product_dir_name"])
                active_dirs.add(product_dir)
                title = str(row["title"] or row["name"] or "").strip()
                connection.execute(
                    """
                    INSERT INTO account_listings (
                        account_id, item_id, title, listing_url, image_url,
                        price_cents, source_text, source_kind,
                        matched_product_dir_name, is_active, synced_at
                    ) VALUES (?, ?, ?, ?, '', ?, '', 'configured', ?, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(account_id, item_id) DO UPDATE SET
                        title=CASE
                            WHEN account_listings.source_kind = 'configured'
                            THEN excluded.title ELSE account_listings.title END,
                        listing_url=excluded.listing_url,
                        price_cents=COALESCE(
                            account_listings.price_cents, excluded.price_cents
                        ),
                        matched_product_dir_name=excluded.matched_product_dir_name,
                        is_active=1
                    """,
                    (
                        account_id,
                        item_id,
                        title,
                        f"https://www.goofish.com/item?id={item_id}",
                        row["confirmed_price_cents"],
                        product_dir,
                    ),
                )
                merged_count += 1

            configured_rows = connection.execute(
                """
                SELECT item_id, matched_product_dir_name
                FROM account_listings
                WHERE account_id = ? AND source_kind = 'configured'
                """,
                (account_id,),
            ).fetchall()
            for row in configured_rows:
                product_dir = str(row["matched_product_dir_name"] or "")
                if product_dir not in active_dirs:
                    connection.execute(
                        """
                        UPDATE account_listings
                        SET is_active = 0
                        WHERE account_id = ? AND item_id = ?
                          AND source_kind = 'configured'
                        """,
                        (account_id, row["item_id"]),
                    )

            self._log(
                connection,
                "configured_listings_reconciled",
                str(account_id),
                {"configured_count": merged_count},
            )
        return self.list_live_listings(account_id)

    def list_live_listings(self, account_id: int | None = None) -> list[dict[str, object]]:
        if account_id is None:
            account_id = int(self.get_active_account()["id"])
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    l.account_id, l.item_id, l.title, l.listing_url, l.image_url,
                    l.price_cents, l.source_text, l.source_kind,
                    l.matched_product_dir_name,
                    l.is_active, l.synced_at,
                    p.name AS product_name, p.share_url, p.share_code,
                    p.share_verified, p.share_needs_review, p.quality_status,
                    p.knowledge_hash, p.knowledge_chars
                FROM account_listings l
                LEFT JOIN products p ON p.dir_name = l.matched_product_dir_name
                WHERE l.account_id = ? AND l.is_active = 1
                ORDER BY l.synced_at DESC, l.item_id DESC
                """,
                (account_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("is_active", "share_verified", "share_needs_review"):
                item[key] = bool(item.get(key))
            item["delivery_ready"] = bool(
                item.get("matched_product_dir_name")
                and item.get("share_url")
                and item.get("share_verified")
                and not item.get("share_needs_review")
            )
            result.append(item)
        return result

    def upsert_paid_order(
        self,
        *,
        order_id: str,
        account_id: int,
        product_dir_name: str,
        listing_item_id: str,
        buyer_id: str,
        chat_id: str,
        event_fingerprint: str,
    ) -> dict[str, object]:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO orders (
                    xianyu_order_id, account_id, product_dir_name, listing_item_id,
                    buyer_id, chat_id, event_fingerprint, payment_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'paid')
                ON CONFLICT(xianyu_order_id) DO UPDATE SET
                    payment_status='paid',
                    delivery_status=CASE
                        WHEN orders.delivery_status='waiting_group' THEN 'pending'
                        ELSE orders.delivery_status
                    END,
                    listing_item_id=CASE WHEN excluded.listing_item_id <> '' THEN excluded.listing_item_id ELSE orders.listing_item_id END,
                    buyer_id=CASE WHEN excluded.buyer_id <> '' THEN excluded.buyer_id ELSE orders.buyer_id END,
                    chat_id=CASE WHEN excluded.chat_id <> '' THEN excluded.chat_id ELSE orders.chat_id END,
                    event_fingerprint=CASE WHEN excluded.event_fingerprint <> '' THEN excluded.event_fingerprint ELSE orders.event_fingerprint END,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    order_id,
                    account_id,
                    product_dir_name,
                    listing_item_id,
                    buyer_id,
                    chat_id,
                    event_fingerprint,
                ),
            )
            self._log(
                connection,
                "paid_order_detected",
                order_id,
                {"account_id": account_id, "product_dir_name": product_dir_name},
            )
        order = self.get_order(order_id)
        if order is None:
            raise RuntimeError("订单写入失败")
        return order

    def upsert_group_waiting_order(
        self,
        *,
        order_id: str,
        account_id: int,
        product_dir_name: str,
        listing_item_id: str,
        buyer_id: str,
        chat_id: str,
        event_fingerprint: str,
    ) -> dict[str, object]:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO orders (
                    xianyu_order_id, account_id, product_dir_name, listing_item_id,
                    buyer_id, chat_id, event_fingerprint, payment_status,
                    delivery_status, group_status, group_event_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'group_waiting', 'waiting_group', 'pending', ?)
                ON CONFLICT(xianyu_order_id) DO UPDATE SET
                    listing_item_id=CASE WHEN excluded.listing_item_id <> '' THEN excluded.listing_item_id ELSE orders.listing_item_id END,
                    buyer_id=CASE WHEN excluded.buyer_id <> '' THEN excluded.buyer_id ELSE orders.buyer_id END,
                    chat_id=CASE WHEN excluded.chat_id <> '' THEN excluded.chat_id ELSE orders.chat_id END,
                    group_event_fingerprint=CASE
                        WHEN excluded.group_event_fingerprint <> '' THEN excluded.group_event_fingerprint
                        ELSE orders.group_event_fingerprint
                    END,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    order_id,
                    account_id,
                    product_dir_name,
                    listing_item_id,
                    buyer_id,
                    chat_id,
                    event_fingerprint,
                    event_fingerprint,
                ),
            )
            self._log(
                connection,
                "group_waiting_detected",
                order_id,
                {"account_id": account_id, "product_dir_name": product_dir_name},
            )
        order = self.get_order(order_id)
        if order is None:
            raise RuntimeError("拼单订单写入失败")
        return order

    def claim_group_exemption(self, order_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE orders
                SET group_status='exempting', group_attempts=group_attempts + 1,
                    last_error='', updated_at=CURRENT_TIMESTAMP
                WHERE xianyu_order_id=?
                  AND payment_status='group_waiting'
                  AND delivery_status='waiting_group'
                  AND group_status IN ('pending', 'failed')
                  AND group_exempted_at IS NULL
                  AND message_sent_at IS NULL
                  AND group_attempts < 3
                """,
                (order_id,),
            )
            claimed = cursor.rowcount == 1
            if claimed:
                self._log(connection, "group_exemption_claimed", order_id, {})
        return claimed

    def mark_group_exempted(self, order_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE orders
                SET group_status='exempted', group_exempted_at=CURRENT_TIMESTAMP,
                    last_error='', updated_at=CURRENT_TIMESTAMP
                WHERE xianyu_order_id=? AND message_sent_at IS NULL
                """,
                (order_id,),
            )
            self._log(connection, "group_exemption_completed", order_id, {})

    def mark_group_exemption_failed(self, order_id: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE orders
                SET group_status='failed', last_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE xianyu_order_id=? AND message_sent_at IS NULL
                """,
                (error[:500], order_id),
            )
            self._log(connection, "group_exemption_failed", order_id, {"has_error": True})

    def claim_order_delivery(self, order_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE orders
                SET delivery_status='sending', delivery_attempts=delivery_attempts + 1,
                    last_error='', updated_at=CURRENT_TIMESTAMP
                WHERE xianyu_order_id=? AND payment_status='paid'
                  AND delivery_status IN ('pending', 'failed')
                  AND message_sent_at IS NULL
                  AND delivery_attempts < 3
                """,
                (order_id,),
            )
            claimed = cursor.rowcount == 1
            if claimed:
                self._log(connection, "delivery_claimed", order_id, {})
        return claimed

    def mark_order_message_sent(self, order_id: str, message_hash: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE orders
                SET delivery_status='message_sent', delivery_message_hash=?,
                    message_sent_at=CURRENT_TIMESTAMP, last_error='', updated_at=CURRENT_TIMESTAMP
                WHERE xianyu_order_id=?
                """,
                (message_hash, order_id),
            )
            self._log(connection, "delivery_message_sent", order_id, {})

    def mark_order_delivered(self, order_id: str, *, platform_status: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE orders
                SET delivery_status='delivered', platform_confirm_status=?,
                    platform_confirmed_at=CASE WHEN ? = 'confirmed' THEN CURRENT_TIMESTAMP ELSE platform_confirmed_at END,
                    delivered_at=CURRENT_TIMESTAMP, last_error='', updated_at=CURRENT_TIMESTAMP
                WHERE xianyu_order_id=? AND message_sent_at IS NOT NULL
                """,
                (platform_status, platform_status, order_id),
            )
            self._log(connection, "delivery_completed", order_id, {"platform_status": platform_status})

    def mark_order_confirmation_pending(self, order_id: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE orders
                SET delivery_status='confirm_pending', platform_confirm_status='failed',
                    last_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE xianyu_order_id=? AND message_sent_at IS NOT NULL
                """,
                (error[:500], order_id),
            )
            self._log(connection, "delivery_confirmation_pending", order_id, {"has_error": True})

    def mark_order_failed(self, order_id: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE orders
                SET delivery_status='failed', last_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE xianyu_order_id=? AND message_sent_at IS NULL
                """,
                (error[:500], order_id),
            )
            self._log(connection, "delivery_failed", order_id, {"has_error": True})

    def mark_order_manual_review(self, order_id: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE orders
                SET delivery_status='manual_review', last_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE xianyu_order_id=?
                """,
                (error[:500], order_id),
            )
            self._log(connection, "delivery_manual_review", order_id, {"has_error": True})

    def get_order(self, order_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE xianyu_order_id = ?", (order_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_orders(self, account_id: int | None = None, *, limit: int = 50) -> list[dict[str, object]]:
        if account_id is None:
            account_id = int(self.get_active_account()["id"])
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT o.*, p.name AS product_name, p.title AS product_title
                FROM orders o
                JOIN products p ON p.dir_name = o.product_dir_name
                WHERE o.account_id = ?
                ORDER BY o.id DESC
                LIMIT ?
                """,
                (account_id, max(1, min(limit, 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_auto_reply_settings(self, account_id: int | None = None) -> dict[str, object]:
        if account_id is None:
            account_id = int(self.get_active_account()["id"])
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM auto_reply_settings WHERE account_id = ?", (account_id,)
            ).fetchone()
        if row is None:
            return {
                "account_id": account_id,
                "enabled": False,
                "base_url": "https://api.siliconflow.cn/v1",
                "model": "deepseek-ai/DeepSeek-V4-Flash",
                "system_prompt": "",
                "min_delay_seconds": 5,
                "max_delay_seconds": 12,
                "max_reply_chars": 180,
                "manual_takeover_hours": 12,
                "updated_at": None,
            }
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    def update_auto_reply_settings(
        self, account_id: int, values: dict[str, object]
    ) -> dict[str, object]:
        current = self.get_auto_reply_settings(account_id)
        resolved = {
            "enabled": bool(values.get("enabled", current["enabled"])),
            "base_url": str(values.get("base_url", current["base_url"])).strip().rstrip("/"),
            "model": str(values.get("model", current["model"])).strip(),
            "system_prompt": str(values.get("system_prompt", current["system_prompt"])).strip(),
            "min_delay_seconds": int(
                values.get("min_delay_seconds", current["min_delay_seconds"])
            ),
            "max_delay_seconds": int(
                values.get("max_delay_seconds", current["max_delay_seconds"])
            ),
            "max_reply_chars": int(values.get("max_reply_chars", current["max_reply_chars"])),
            "manual_takeover_hours": int(
                values.get("manual_takeover_hours", current["manual_takeover_hours"])
            ),
        }
        with self.connect() as connection:
            exists = connection.execute(
                "SELECT id FROM accounts WHERE id = ? AND is_archived = 0", (account_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("账号不存在")
            connection.execute(
                """
                INSERT INTO auto_reply_settings (
                    account_id, enabled, base_url, model, system_prompt,
                    min_delay_seconds, max_delay_seconds, max_reply_chars,
                    manual_takeover_hours, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id) DO UPDATE SET
                    enabled=excluded.enabled,
                    base_url=excluded.base_url,
                    model=excluded.model,
                    system_prompt=excluded.system_prompt,
                    min_delay_seconds=excluded.min_delay_seconds,
                    max_delay_seconds=excluded.max_delay_seconds,
                    max_reply_chars=excluded.max_reply_chars,
                    manual_takeover_hours=excluded.manual_takeover_hours,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    account_id,
                    int(resolved["enabled"]),
                    resolved["base_url"],
                    resolved["model"],
                    resolved["system_prompt"],
                    resolved["min_delay_seconds"],
                    resolved["max_delay_seconds"],
                    resolved["max_reply_chars"],
                    resolved["manual_takeover_hours"],
                ),
            )
            self._log(
                connection,
                "auto_reply_settings_updated",
                str(account_id),
                {
                    "enabled": resolved["enabled"],
                    "model": resolved["model"],
                    "has_system_prompt": bool(resolved["system_prompt"]),
                },
            )
        return self.get_auto_reply_settings(account_id)

    def record_chat_message(
        self,
        *,
        account_id: int,
        chat_id: str,
        buyer_id: str,
        listing_item_id: str,
        direction: str,
        content: str,
        event_fingerprint: str,
        status: str = "received",
        reply_source: str = "",
        related_message_id: int | None = None,
        reason: str = "",
    ) -> int | None:
        if direction not in {"inbound", "outbound"}:
            raise ValueError("未知消息方向")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO chat_messages (
                    account_id, chat_id, buyer_id, listing_item_id, direction,
                    content, event_fingerprint, status, reply_source,
                    related_message_id, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    chat_id,
                    buyer_id,
                    listing_item_id,
                    direction,
                    content[:2000],
                    event_fingerprint,
                    status,
                    reply_source,
                    related_message_id,
                    reason[:500],
                ),
            )
            if cursor.rowcount != 1:
                return None
            message_id = int(cursor.lastrowid)
            if direction == "inbound":
                connection.execute(
                    """
                    INSERT INTO chat_sessions (
                        account_id, chat_id, buyer_id, listing_item_id,
                        last_buyer_message_at, updated_at
                    ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(account_id, chat_id) DO UPDATE SET
                        buyer_id=excluded.buyer_id,
                        listing_item_id=CASE
                            WHEN excluded.listing_item_id <> '' THEN excluded.listing_item_id
                            ELSE chat_sessions.listing_item_id
                        END,
                        last_buyer_message_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (account_id, chat_id, buyer_id, listing_item_id),
                )
            return message_id

    def claim_auto_reply(self, message_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chat_messages
                SET status='processing'
                WHERE id=? AND direction='inbound' AND status='received'
                """,
                (message_id,),
            )
            return cursor.rowcount == 1

    def mark_chat_message(self, message_id: int, status: str, reason: str = "") -> None:
        allowed = {"received", "processing", "replied", "manual", "skipped", "failed"}
        if status not in allowed:
            raise ValueError("未知自动回复状态")
        with self.connect() as connection:
            connection.execute(
                "UPDATE chat_messages SET status=?, reason=? WHERE id=?",
                (status, reason[:500], message_id),
            )

    def record_auto_reply_sent(
        self,
        *,
        account_id: int,
        chat_id: str,
        buyer_id: str,
        listing_item_id: str,
        inbound_message_id: int,
        reply: str,
        model: str,
        decision_action: str = "reply",
    ) -> int | None:
        decision_reason = "自动澄清追问" if decision_action == "clarify" else ""
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO chat_messages (
                    account_id, chat_id, buyer_id, listing_item_id, direction,
                    content, event_fingerprint, status, reply_source,
                    related_message_id, reason
                ) VALUES (?, ?, ?, ?, 'outbound', ?, ?, 'replied', ?, ?, ?)
                """,
                (
                    account_id,
                    chat_id,
                    buyer_id,
                    listing_item_id,
                    reply[:2000],
                    f"auto:{inbound_message_id}",
                    model,
                    inbound_message_id,
                    decision_reason,
                ),
            )
            connection.execute(
                "UPDATE chat_messages SET status='replied', reason=? WHERE id=?",
                (decision_reason, inbound_message_id),
            )
            connection.execute(
                """
                INSERT INTO chat_sessions (
                    account_id, chat_id, buyer_id, listing_item_id,
                    last_auto_reply_at, updated_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id, chat_id) DO UPDATE SET
                    last_auto_reply_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (account_id, chat_id, buyer_id, listing_item_id),
            )
            return int(cursor.lastrowid) if cursor.rowcount == 1 else None

    def set_chat_manual(
        self,
        account_id: int,
        chat_id: str,
        *,
        enabled: bool,
        hours: int = 12,
    ) -> None:
        modifier = f"+{max(1, min(hours, 168))} hours"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO chat_sessions (
                    account_id, chat_id, manual_takeover_until, updated_at
                ) VALUES (?, ?, CASE WHEN ? THEN datetime('now', ?) ELSE NULL END, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id, chat_id) DO UPDATE SET
                    manual_takeover_until=CASE
                        WHEN ? THEN datetime('now', ?)
                        ELSE NULL
                    END,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (account_id, chat_id, int(enabled), modifier, int(enabled), modifier),
            )
            self._log(
                connection,
                "chat_manual_takeover" if enabled else "chat_auto_reply_resumed",
                chat_id,
                {"account_id": account_id, "hours": hours if enabled else 0},
            )

    def is_chat_manual(self, account_id: int, chat_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT manual_takeover_until
                FROM chat_sessions
                WHERE account_id=? AND chat_id=?
                  AND manual_takeover_until IS NOT NULL
                  AND manual_takeover_until > CURRENT_TIMESTAMP
                """,
                (account_id, chat_id),
            ).fetchone()
        return row is not None

    def list_recent_chat_messages(
        self, account_id: int, chat_id: str, *, limit: int = 8
    ) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM chat_messages
                    WHERE account_id=? AND chat_id=?
                    ORDER BY id DESC LIMIT ?
                ) ORDER BY id
                """,
                (account_id, chat_id, max(1, min(limit, 20))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_auto_reply_records(
        self, account_id: int | None = None, *, limit: int = 30
    ) -> list[dict[str, object]]:
        if account_id is None:
            account_id = int(self.get_active_account()["id"])
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*, l.title AS listing_title,
                       CASE
                         WHEN s.manual_takeover_until > CURRENT_TIMESTAMP THEN 1
                         ELSE 0
                       END AS manual_takeover
                FROM chat_messages m
                LEFT JOIN account_listings l
                  ON l.account_id=m.account_id AND l.item_id=m.listing_item_id
                LEFT JOIN chat_sessions s
                  ON s.account_id=m.account_id AND s.chat_id=m.chat_id
                WHERE m.account_id=?
                ORDER BY m.id DESC
                LIMIT ?
                """,
                (account_id, max(1, min(limit, 100))),
            ).fetchall()
        return [
            {**dict(row), "manual_takeover": bool(row["manual_takeover"])} for row in rows
        ]

    def get_automation_safety(self, account_id: int | None = None) -> dict[str, object]:
        if account_id is None:
            account_id = int(self.get_active_account()["id"])
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM automation_safety WHERE account_id=?", (account_id,)
            ).fetchone()
        if row is None:
            return {"account_id": account_id, **DEFAULT_AUTOMATION_SAFETY, "updated_at": None}
        result = dict(row)
        result["notifications_enabled"] = bool(result["notifications_enabled"])
        return result

    def update_automation_safety(
        self, account_id: int, values: dict[str, object]
    ) -> dict[str, object]:
        current = self.get_automation_safety(account_id)
        resolved = {
            "max_replies_per_hour": int(
                values.get("max_replies_per_hour", current["max_replies_per_hour"])
            ),
            "max_replies_per_day": int(
                values.get("max_replies_per_day", current["max_replies_per_day"])
            ),
            "max_deliveries_per_hour": int(
                values.get("max_deliveries_per_hour", current["max_deliveries_per_hour"])
            ),
            "min_outbound_interval_seconds": int(
                values.get(
                    "min_outbound_interval_seconds",
                    current["min_outbound_interval_seconds"],
                )
            ),
            "risk_cooldown_minutes": int(
                values.get("risk_cooldown_minutes", current["risk_cooldown_minutes"])
            ),
            "notifications_enabled": bool(
                values.get("notifications_enabled", current["notifications_enabled"])
            ),
        }
        ranges = {
            "max_replies_per_hour": (1, 120),
            "max_replies_per_day": (1, 1000),
            "max_deliveries_per_hour": (1, 100),
            "min_outbound_interval_seconds": (2, 120),
            "risk_cooldown_minutes": (5, 180),
        }
        for name, (minimum, maximum) in ranges.items():
            value = int(resolved[name])
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
        if int(resolved["max_replies_per_day"]) < int(resolved["max_replies_per_hour"]):
            raise ValueError("每日自动回复上限不能小于每小时上限")

        with self.connect() as connection:
            exists = connection.execute(
                "SELECT id FROM accounts WHERE id=? AND is_archived=0", (account_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("账号不存在")
            connection.execute(
                """
                INSERT INTO automation_safety (
                    account_id, max_replies_per_hour, max_replies_per_day,
                    max_deliveries_per_hour, min_outbound_interval_seconds,
                    risk_cooldown_minutes, notifications_enabled, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id) DO UPDATE SET
                    max_replies_per_hour=excluded.max_replies_per_hour,
                    max_replies_per_day=excluded.max_replies_per_day,
                    max_deliveries_per_hour=excluded.max_deliveries_per_hour,
                    min_outbound_interval_seconds=excluded.min_outbound_interval_seconds,
                    risk_cooldown_minutes=excluded.risk_cooldown_minutes,
                    notifications_enabled=excluded.notifications_enabled,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    account_id,
                    resolved["max_replies_per_hour"],
                    resolved["max_replies_per_day"],
                    resolved["max_deliveries_per_hour"],
                    resolved["min_outbound_interval_seconds"],
                    resolved["risk_cooldown_minutes"],
                    int(resolved["notifications_enabled"]),
                ),
            )
            self._log(connection, "automation_safety_updated", str(account_id), resolved)
        return self.get_automation_safety(account_id)

    def get_automation_status(self, account_id: int | None = None) -> dict[str, object]:
        if account_id is None:
            account_id = int(self.get_active_account()["id"])
        safety = self.get_automation_safety(account_id)
        with self.connect() as connection:
            runtime = connection.execute(
                """
                SELECT circuit_open_until, circuit_reason, last_startup_at,
                       last_startup_report_json,
                       CASE
                         WHEN circuit_open_until > CURRENT_TIMESTAMP THEN 1 ELSE 0
                       END AS circuit_is_open,
                       CASE
                         WHEN circuit_open_until > CURRENT_TIMESTAMP
                         THEN MAX(0, CAST(strftime('%s', circuit_open_until) AS INTEGER)
                                      - CAST(strftime('%s', 'now') AS INTEGER))
                         ELSE 0
                       END AS circuit_retry_after_seconds
                FROM automation_runtime WHERE account_id=?
                """,
                (account_id,),
            ).fetchone()
            usage = connection.execute(
                """
                SELECT
                  SUM(CASE WHEN kind='reply' AND
                    ((status='sent' AND sent_at >= datetime('now', '-1 hour')) OR
                     (status='reserved' AND reserved_at >= datetime('now', '-2 minutes')))
                    THEN 1 ELSE 0 END) AS replies_hour,
                  SUM(CASE WHEN kind='reply' AND
                    ((status='sent' AND sent_at >= datetime('now', '-24 hours')) OR
                     (status='reserved' AND reserved_at >= datetime('now', '-2 minutes')))
                    THEN 1 ELSE 0 END) AS replies_day,
                  SUM(CASE WHEN kind='delivery' AND
                    ((status='sent' AND sent_at >= datetime('now', '-1 hour')) OR
                     (status='reserved' AND reserved_at >= datetime('now', '-2 minutes')))
                    THEN 1 ELSE 0 END) AS deliveries_hour,
                  MAX(CASE WHEN status='sent' THEN sent_at
                           WHEN status='reserved' AND reserved_at >= datetime('now', '-2 minutes')
                           THEN reserved_at END) AS last_outbound_at
                FROM automation_outbound_events WHERE account_id=?
                """,
                (account_id,),
            ).fetchone()
            interval_row = connection.execute(
                """
                SELECT CASE WHEN MAX(at_time) IS NULL THEN 0 ELSE
                    MAX(0, ? - (CAST(strftime('%s', 'now') AS INTEGER)
                      - CAST(strftime('%s', MAX(at_time)) AS INTEGER))) END AS retry_after
                FROM (
                    SELECT sent_at AS at_time FROM automation_outbound_events
                    WHERE account_id=? AND status='sent'
                    UNION ALL
                    SELECT reserved_at AS at_time FROM automation_outbound_events
                    WHERE account_id=? AND status='reserved'
                      AND reserved_at >= datetime('now', '-2 minutes')
                )
                """,
                (
                    int(safety["min_outbound_interval_seconds"]),
                    account_id,
                    account_id,
                ),
            ).fetchone()

        runtime_data = dict(runtime) if runtime is not None else {}
        try:
            startup_report = json.loads(
                str(runtime_data.get("last_startup_report_json") or "{}")
            )
        except json.JSONDecodeError:
            startup_report = {}
        is_open = bool(runtime_data.get("circuit_is_open"))
        return {
            "settings": safety,
            "usage": {
                "replies_hour": int(usage["replies_hour"] or 0),
                "replies_day": int(usage["replies_day"] or 0),
                "deliveries_hour": int(usage["deliveries_hour"] or 0),
                "last_outbound_at": usage["last_outbound_at"],
                "interval_retry_after_seconds": int(interval_row["retry_after"] or 0),
            },
            "circuit": {
                "is_open": is_open,
                "open_until": runtime_data.get("circuit_open_until") if is_open else None,
                "reason": str(runtime_data.get("circuit_reason") or "") if is_open else "",
                "retry_after_seconds": int(
                    runtime_data.get("circuit_retry_after_seconds") or 0
                ),
            },
            "startup": {
                "at": runtime_data.get("last_startup_at"),
                "report": startup_report,
            },
        }

    def check_automation_outbound(self, account_id: int, kind: str) -> dict[str, object]:
        if kind not in {"reply", "delivery"}:
            raise ValueError("未知的自动发送类型")
        status = self.get_automation_status(account_id)
        safety = status["settings"]
        usage = status["usage"]
        circuit = status["circuit"]
        if circuit["is_open"]:
            return {
                "allowed": False,
                "code": "circuit",
                "reason": f"安全熔断中：{circuit['reason'] or '等待冷却'}",
                "retry_after_seconds": circuit["retry_after_seconds"],
            }
        if int(usage["interval_retry_after_seconds"]) > 0:
            return {
                "allowed": False,
                "code": "interval",
                "reason": "距离上一条自动发送过近",
                "retry_after_seconds": usage["interval_retry_after_seconds"],
            }
        if kind == "reply" and int(usage["replies_hour"]) >= int(
            safety["max_replies_per_hour"]
        ):
            return {"allowed": False, "code": "reply_hour", "reason": "已达到每小时自动回复上限", "retry_after_seconds": 60}
        if kind == "reply" and int(usage["replies_day"]) >= int(
            safety["max_replies_per_day"]
        ):
            return {"allowed": False, "code": "reply_day", "reason": "已达到每日自动回复上限", "retry_after_seconds": 3600}
        if kind == "delivery" and int(usage["deliveries_hour"]) >= int(
            safety["max_deliveries_per_hour"]
        ):
            return {"allowed": False, "code": "delivery_hour", "reason": "已达到每小时自动发货上限", "retry_after_seconds": 60}
        return {"allowed": True, "code": "ok", "reason": "", "retry_after_seconds": 0}

    def reserve_automation_outbound(
        self, account_id: int, kind: str, reference: str
    ) -> dict[str, object]:
        normalized_reference = str(reference).strip()[:200]
        if not normalized_reference:
            raise ValueError("自动发送缺少唯一引用")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            decision = self.check_automation_outbound(account_id, kind)
            if not decision["allowed"]:
                return decision
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO automation_outbound_events (
                    account_id, kind, reference, status
                ) VALUES (?, ?, ?, 'reserved')
                """,
                (account_id, kind, normalized_reference),
            )
            if cursor.rowcount != 1:
                return {
                    "allowed": False,
                    "code": "duplicate",
                    "reason": "该自动发送已处理，已阻止重复发送",
                    "retry_after_seconds": 0,
                }
            return {
                "allowed": True,
                "code": "ok",
                "reason": "",
                "retry_after_seconds": 0,
                "event_id": int(cursor.lastrowid),
            }

    def finish_automation_outbound(
        self, event_id: int, *, sent: bool, error: str = ""
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE automation_outbound_events
                SET status=?, error=?, sent_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END
                WHERE id=? AND status='reserved'
                """,
                ("sent" if sent else "failed", error[:300], int(sent), event_id),
            )

    def open_automation_circuit(
        self, account_id: int, reason: str, *, minutes: int | None = None
    ) -> dict[str, object]:
        safety = self.get_automation_safety(account_id)
        cooldown = int(minutes or safety["risk_cooldown_minutes"])
        modifier = f"+{max(5, min(cooldown, 180))} minutes"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO automation_runtime (
                    account_id, circuit_open_until, circuit_reason, updated_at
                ) VALUES (?, datetime('now', ?), ?, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id) DO UPDATE SET
                    circuit_open_until=datetime('now', ?),
                    circuit_reason=excluded.circuit_reason,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (account_id, modifier, reason[:500], modifier),
            )
            self._log(
                connection,
                "automation_circuit_opened",
                str(account_id),
                {"reason": reason[:300], "cooldown_minutes": cooldown},
            )
        return self.get_automation_status(account_id)

    def clear_automation_circuit(self, account_id: int) -> dict[str, object]:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO automation_runtime (account_id, updated_at)
                VALUES (?, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id) DO UPDATE SET
                    circuit_open_until=NULL, circuit_reason='', updated_at=CURRENT_TIMESTAMP
                """,
                (account_id,),
            )
            self._log(connection, "automation_circuit_cleared", str(account_id), {})
        return self.get_automation_status(account_id)

    def record_startup_report(
        self, account_id: int, report: dict[str, object]
    ) -> dict[str, object]:
        encoded = json.dumps(report, ensure_ascii=False)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO automation_runtime (
                    account_id, last_startup_at, last_startup_report_json, updated_at
                ) VALUES (?, CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id) DO UPDATE SET
                    last_startup_at=CURRENT_TIMESTAMP,
                    last_startup_report_json=excluded.last_startup_report_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (account_id, encoded),
            )
            self._log(connection, "automation_startup_report", str(account_id), report)
        return self.get_automation_status(account_id)

    def record_audit(self, event_type: str, subject: str, details: dict[str, object]) -> None:
        with self.connect() as connection:
            self._log(connection, event_type, subject, details)

    @staticmethod
    def _log(connection: sqlite3.Connection, event_type: str, subject: str, details: dict[str, object]) -> None:
        connection.execute(
            "INSERT INTO audit_log (event_type, subject, details_json) VALUES (?, ?, ?)",
            (event_type, subject, json.dumps(details, ensure_ascii=False)),
        )
