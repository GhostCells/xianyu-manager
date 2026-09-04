from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import math
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import sqlite3


DEFAULT_MIN_INTERVAL_HOURS = 20.0
DEFAULT_TIMEZONE = "Asia/Shanghai"


def _utc_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _tracking_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        if name == DEFAULT_TIMEZONE:
            return timezone(timedelta(hours=8), name)
        raise ValueError(f"无效的追踪时区: {name}") from exc


def _budget_targets(
    limit: int,
    new_count: int,
    tracking_count: int,
    tracking_budget_ratio: float,
) -> tuple[int, int]:
    if new_count <= 0:
        return 0, min(limit, tracking_count)
    if tracking_count <= 0:
        return min(limit, new_count), 0
    tracking_target = math.floor(limit * tracking_budget_ratio + 0.5)
    if tracking_budget_ratio <= 0:
        tracking_target = 0
    elif tracking_budget_ratio >= 1:
        tracking_target = limit
    elif limit >= 2:
        tracking_target = max(1, min(tracking_target, limit - 1))
    else:
        tracking_target = 1 if tracking_budget_ratio >= 0.5 else 0
    new_target = limit - tracking_target
    selected_new = min(new_target, new_count)
    selected_tracking = min(tracking_target, tracking_count)
    unused = limit - selected_new - selected_tracking
    if unused:
        add_tracking = min(unused, tracking_count - selected_tracking)
        selected_tracking += add_tracking
        unused -= add_tracking
    if unused:
        selected_new += min(unused, new_count - selected_new)
    return selected_new, selected_tracking


def plan_detail_candidates(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    min_interval_hours: float = DEFAULT_MIN_INTERVAL_HOURS,
    timezone_name: str = DEFAULT_TIMEZONE,
    tracking_budget_ratio: float = 0.4,
    now: datetime | None = None,
) -> dict[str, Any]:
    bounded_limit = max(1, min(int(limit), 20))
    default_interval = float(min_interval_hours)
    if not 1 <= default_interval <= 168:
        raise ValueError("最短重复观测间隔必须在1到168小时之间")
    ratio = float(tracking_budget_ratio)
    if not 0 <= ratio <= 1:
        raise ValueError("追踪预算比例必须在0到1之间")
    local_timezone = _tracking_timezone(timezone_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    new_pool: list[dict[str, Any]] = []
    tracking_pool: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for source in rows:
        row = dict(source)
        row["candidate_kind"] = ""
        last_success = _utc_datetime(row.get("last_successful_detail_at"))
        if bool(row.get("current_run_success")):
            row["skip_reason"] = "current_run_success"
            skipped.append(row)
            continue
        tracking_status = str(row.get("tracking_status") or "")
        if tracking_status in {"paused", "retired"}:
            row["skip_reason"] = "tracking_inactive"
            skipped.append(row)
            continue
        if last_success is None:
            row["candidate_kind"] = "initial"
            new_pool.append(row)
            continue
        if tracking_status != "active":
            row["skip_reason"] = "tracking_inactive"
            skipped.append(row)
            continue

        row_interval = row.get("min_interval_hours")
        required_hours = float(row_interval) if row_interval is not None else default_interval
        required_hours = max(1.0, min(required_hours, 168.0))
        elapsed_hours = (current - last_success).total_seconds() / 3600
        row["elapsed_hours"] = elapsed_hours
        row["required_interval_hours"] = required_hours
        if last_success.astimezone(local_timezone).date() == current.astimezone(local_timezone).date():
            row["skip_reason"] = "already_observed_today"
            skipped.append(row)
            continue
        if elapsed_hours < required_hours:
            row["skip_reason"] = "tracking_not_due"
            skipped.append(row)
            continue
        row["candidate_kind"] = "tracking"
        row["overdue_hours"] = elapsed_hours - required_hours
        tracking_pool.append(row)

    new_pool.sort(key=lambda row: (int(row.get("search_rank") or 10**9), int(row.get("observation_id") or 0)))
    tracking_pool.sort(
        key=lambda row: (
            -int(row.get("tracking_priority") or 0),
            -float(row.get("latest_want_per_hour") or 0),
            -int(row.get("snapshot_count") or 0),
            -float(row.get("overdue_hours") or 0),
            int(row.get("search_rank") or 10**9),
            int(row.get("observation_id") or 0),
        )
    )

    new_target, tracking_target = _budget_targets(
        bounded_limit, len(new_pool), len(tracking_pool), ratio
    )
    selected_new = new_pool[:new_target]
    selected_tracking = tracking_pool[:tracking_target]

    selected: list[dict[str, Any]] = []
    while selected_new or selected_tracking:
        if selected_tracking:
            selected.append(selected_tracking.pop(0))
        if selected_new and len(selected) < bounded_limit:
            selected.append(selected_new.pop(0))
        if len(selected) >= bounded_limit:
            break

    skip_counts: dict[str, int] = {}
    for row in skipped:
        reason = str(row.get("skip_reason") or "unknown")
        skip_counts[reason] = skip_counts.get(reason, 0) + 1

    return {
        "limit": bounded_limit,
        "tracking_budget_ratio": ratio,
        "new_eligible_count": len(new_pool),
        "tracking_due_count": len(tracking_pool),
        "selected_new_count": sum(row["candidate_kind"] == "initial" for row in selected),
        "selected_tracking_count": sum(row["candidate_kind"] == "tracking" for row in selected),
        "skip_counts": skip_counts,
        "selected": selected,
        "skipped": skipped,
    }


def build_tracking_progress(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    counts = connection.execute(
        """
        WITH successful AS (
          SELECT item_id, COUNT(*) AS snapshot_count
          FROM selection_item_snapshots
          WHERE detail_status='success'
          GROUP BY item_id
        )
        SELECT
          (SELECT COUNT(*) FROM selection_item_tracking WHERE tracking_status='active')
            AS tracked_items,
          COALESCE(SUM(snapshot_count >= 2), 0) AS items_with_2_plus,
          COALESCE(SUM(snapshot_count >= 3), 0) AS items_with_3_plus
        FROM successful
        """
    ).fetchone()
    pair_counts = connection.execute(
        """
        WITH ordered AS (
          SELECT item_id,
                 COALESCE(detail_observed_at, observed_at) AS current_at,
                 LAG(COALESCE(detail_observed_at, observed_at)) OVER (
                   PARTITION BY item_id
                   ORDER BY COALESCE(detail_observed_at, observed_at), snapshot_id
                 ) AS previous_at
          FROM selection_item_snapshots
          WHERE detail_status='success'
        ), intervals AS (
          SELECT item_id,
                 (julianday(current_at)-julianday(previous_at))*24 AS hours
          FROM ordered
          WHERE previous_at IS NOT NULL
        )
        SELECT
          COUNT(DISTINCT CASE WHEN hours BETWEEN 20 AND 30 THEN item_id END)
            AS comparable_24h_items,
          COUNT(DISTINCT CASE WHEN hours BETWEEN 60 AND 84 THEN item_id END)
            AS comparable_72h_items
        FROM intervals
        """
    ).fetchone()
    return {
        "tracked_items": int(counts["tracked_items"] or 0),
        "items_with_2_plus_snapshots": int(counts["items_with_2_plus"] or 0),
        "items_with_3_plus_snapshots": int(counts["items_with_3_plus"] or 0),
        "comparable_24h_items": int(pair_counts["comparable_24h_items"] or 0),
        "comparable_72h_items": int(pair_counts["comparable_72h_items"] or 0),
        "comparison_windows_hours": {"24h": [20, 30], "72h": [60, 84]},
        "conclusion": "数据积累进度；不构成爆款或趋势热点结论",
    }
