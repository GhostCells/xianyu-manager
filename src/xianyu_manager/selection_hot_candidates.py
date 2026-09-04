from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import Database
from .selection_rule_scoring import price_fit_score, title_relevance_score


SCORE_VERSION = "hot-candidate-mvp-v1"
HOT_CANDIDATE_THRESHOLD = 60.0


def _timestamp(value: object) -> datetime | None:
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


def _metric_delta(
    previous: dict[str, object], current: dict[str, object], field: str, hours: float
) -> tuple[int | None, float | None, str | None]:
    before = previous.get(field)
    after = current.get(field)
    if before is None or after is None:
        return None, None, f"{field}_missing"
    delta = int(after) - int(before)
    if delta < 0:
        return delta, None, f"{field}_decreased"
    return delta, delta / hours, None


def calculate_trend(
    current: dict[str, object], previous: dict[str, object] | None
) -> dict[str, object]:
    result: dict[str, object] = {
        "snapshot_id": int(current["snapshot_id"]),
        "previous_snapshot_id": None,
        "interval_hours": None,
        "browse_delta": None,
        "want_delta": None,
        "collect_delta": None,
        "browse_per_hour": None,
        "want_per_hour": None,
        "collect_per_hour": None,
        "anomaly_codes": [],
    }
    if previous is None:
        result["anomaly_codes"] = ["first_observation"]
        return result
    result["previous_snapshot_id"] = int(previous["snapshot_id"])
    current_at = _timestamp(current.get("detail_observed_at") or current.get("observed_at"))
    previous_at = _timestamp(previous.get("detail_observed_at") or previous.get("observed_at"))
    if current_at is None or previous_at is None:
        result["anomaly_codes"] = ["observation_time_missing"]
        return result
    hours = (current_at - previous_at).total_seconds() / 3600
    result["interval_hours"] = hours
    if hours <= 0:
        result["anomaly_codes"] = ["non_positive_interval"]
        return result
    anomalies: list[str] = []
    for prefix, field in (
        ("browse", "browse_count"),
        ("want", "want_count"),
        ("collect", "collect_count"),
    ):
        delta, rate, anomaly = _metric_delta(previous, current, field, hours)
        result[f"{prefix}_delta"] = delta
        result[f"{prefix}_per_hour"] = rate
        if anomaly:
            anomalies.append(anomaly)
    result["anomaly_codes"] = anomalies
    return result


def _want_velocity(
    previous: dict[str, object], current: dict[str, object]
) -> float | None:
    before = previous.get("want_count")
    after = current.get("want_count")
    before_at = _timestamp(previous.get("detail_observed_at") or previous.get("observed_at"))
    after_at = _timestamp(current.get("detail_observed_at") or current.get("observed_at"))
    if before is None or after is None or before_at is None or after_at is None:
        return None
    hours = (after_at - before_at).total_seconds() / 3600
    if hours <= 0:
        return None
    return (int(after) - int(before)) / hours


def calculate_multi_snapshot_trend(
    snapshots: list[dict[str, object]],
) -> dict[str, object]:
    ordered = sorted(
        snapshots,
        key=lambda row: (
            _timestamp(row.get("detail_observed_at") or row.get("observed_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
            int(row.get("snapshot_id") or 0),
        ),
    )
    result: dict[str, object] = {
        "snapshot_count": len(ordered),
        "recent_3_want_delta": None,
        "recent_3_avg_want_velocity": None,
        "recent_3_continuous_growth": None,
        "want_delta_7d": None,
        "avg_want_velocity_7d": None,
        "latest_velocity": None,
        "previous_velocity": None,
        "velocity_acceleration": None,
        "insufficient_data": [],
    }
    insufficient: list[str] = []
    if len(ordered) >= 2:
        result["latest_velocity"] = _want_velocity(ordered[-2], ordered[-1])
    else:
        insufficient.append("latest_velocity")

    if len(ordered) >= 3:
        recent = ordered[-3:]
        previous_velocity = _want_velocity(recent[0], recent[1])
        latest_velocity = _want_velocity(recent[1], recent[2])
        result["previous_velocity"] = previous_velocity
        if latest_velocity is not None:
            result["latest_velocity"] = latest_velocity
        if previous_velocity is not None and latest_velocity is not None:
            result["velocity_acceleration"] = latest_velocity - previous_velocity
        else:
            insufficient.append("velocity_acceleration")
        values = [row.get("want_count") for row in recent]
        first_at = _timestamp(recent[0].get("detail_observed_at") or recent[0].get("observed_at"))
        last_at = _timestamp(recent[-1].get("detail_observed_at") or recent[-1].get("observed_at"))
        if all(value is not None for value in values) and first_at and last_at and last_at > first_at:
            delta = int(values[-1]) - int(values[0])
            result["recent_3_want_delta"] = delta
            result["recent_3_avg_want_velocity"] = delta / ((last_at - first_at).total_seconds() / 3600)
            result["recent_3_continuous_growth"] = all(
                int(current) > int(previous)
                for previous, current in zip(values, values[1:])
            )
        else:
            insufficient.extend(
                ["recent_3_want_delta", "recent_3_avg_want_velocity", "recent_3_continuous_growth"]
            )
    else:
        insufficient.extend(
            ["recent_3_want_delta", "recent_3_avg_want_velocity", "recent_3_continuous_growth",
             "previous_velocity", "velocity_acceleration"]
        )

    latest_at = (
        _timestamp(ordered[-1].get("detail_observed_at") or ordered[-1].get("observed_at"))
        if ordered else None
    )
    if latest_at is not None:
        window_start = latest_at - timedelta(days=7)
        window = [
            row for row in ordered
            if (at := _timestamp(row.get("detail_observed_at") or row.get("observed_at")))
            and window_start <= at <= latest_at
            and row.get("want_count") is not None
        ]
        if len(window) >= 2:
            first_at = _timestamp(window[0].get("detail_observed_at") or window[0].get("observed_at"))
            last_at = _timestamp(window[-1].get("detail_observed_at") or window[-1].get("observed_at"))
            if first_at and last_at and last_at > first_at:
                delta = int(window[-1]["want_count"]) - int(window[0]["want_count"])
                result["want_delta_7d"] = delta
                result["avg_want_velocity_7d"] = delta / ((last_at - first_at).total_seconds() / 3600)
            else:
                insufficient.extend(["want_delta_7d", "avg_want_velocity_7d"])
        else:
            insufficient.extend(["want_delta_7d", "avg_want_velocity_7d"])
    else:
        insufficient.extend(["want_delta_7d", "avg_want_velocity_7d"])
    result["insufficient_data"] = list(dict.fromkeys(insufficient))
    return result


def tracking_priority_from_trend(trend: dict[str, object]) -> tuple[int, list[str]]:
    snapshot_count = int(trend.get("snapshot_count") or 0)
    priority = min(20, snapshot_count * 5)
    reasons = ["snapshot_history"] if snapshot_count else []
    latest_velocity = trend.get("latest_velocity")
    if latest_velocity is not None and float(latest_velocity) > 0:
        priority += min(60, round(float(latest_velocity) * 20))
        reasons.append("want_velocity_positive")
    if trend.get("recent_3_continuous_growth") is True:
        priority += 20
        reasons.append("recent_3_continuous_growth")
    return min(100, priority), reasons


def _saturated(value: float | None, target: float) -> float | None:
    if value is None:
        return None
    return min(100.0, max(0.0, value) / target * 100)


def assess_item(
    item: dict[str, object], snapshot: dict[str, object], trend: dict[str, object]
) -> dict[str, object]:
    rates = {
        "browse": _saturated(trend.get("browse_per_hour"), 20.0),
        "want": _saturated(trend.get("want_per_hour"), 1.0),
        "collect": _saturated(trend.get("collect_per_hour"), 0.5),
    }
    available_trend = [(name, value) for name, value in rates.items() if value is not None]
    trend_score = None
    if available_trend:
        weights = {"browse": 25.0, "want": 20.0, "collect": 15.0}
        used = sum(weights[name] for name, _ in available_trend)
        trend_score = sum(value * weights[name] for name, value in available_trend) / used

    current_parts: list[tuple[float, float]] = []
    for field, cap, weight in (
        ("browse_count", 5000.0, 10.0),
        ("want_count", 200.0, 10.0),
        ("collect_count", 100.0, 5.0),
    ):
        value = snapshot.get(field)
        if value is not None:
            current_parts.append((min(100.0, math.log1p(int(value)) / math.log1p(cap) * 100), weight))
    relevance, _ = title_relevance_score(item.get("title_raw"), snapshot.get("keyword"))
    price_fit, price_missing = price_fit_score(snapshot.get("price_cents"), snapshot.get("price_parse_status"))
    current_parts.append((relevance, 10.0))
    if not price_missing:
        current_parts.append((price_fit, 5.0))
    current_score = sum(score * weight for score, weight in current_parts) / sum(
        weight for _, weight in current_parts
    )

    total_score = None
    reason_codes: list[str] = []
    if trend_score is not None:
        total_score = round(trend_score * 0.60 + current_score * 0.40, 2)
        if (trend.get("want_per_hour") or 0) > 0:
            reason_codes.append("want_growing")
        if (trend.get("browse_per_hour") or 0) > 0:
            reason_codes.append("browse_growing")
        if (trend.get("collect_per_hour") or 0) > 0:
            reason_codes.append("collect_growing")
        if total_score >= HOT_CANDIDATE_THRESHOLD:
            reason_codes.append("score_threshold_met")
    else:
        reason_codes.append("needs_comparable_observation")
    return {
        "item_id": item["item_id"],
        "snapshot_id": snapshot["snapshot_id"],
        "score_version": SCORE_VERSION,
        "total_score": total_score,
        "trend_score": None if trend_score is None else round(trend_score, 2),
        "current_score": round(current_score, 2),
        "reason_codes": reason_codes,
        "explanation": {
            "threshold": HOT_CANDIDATE_THRESHOLD,
            "weights": {"trend": 0.60, "current_signals": 0.40},
            "trend": trend,
            "current": {
                "browse_count": snapshot.get("browse_count"),
                "want_count": snapshot.get("want_count"),
                "collect_count": snapshot.get("collect_count"),
                "price_cents": snapshot.get("price_cents"),
                "published_at": None,
                "item_age_hours": None,
            },
            "unknown_fields": ["published_at", "item_age_hours"],
        },
    }


def refresh_item_assessment(database: Database, item_id: str) -> dict[str, Any]:
    item = database.get_selection_item(item_id)
    if item is None:
        raise ValueError("候选商品不存在")
    snapshots = database.list_selection_successful_snapshots(item_id)
    if not snapshots:
        raise ValueError("候选商品没有成功详情快照")
    current = snapshots[-1]
    previous = snapshots[-2] if len(snapshots) >= 2 else None
    trend = calculate_trend(current, previous)
    multi_snapshot_trend = calculate_multi_snapshot_trend(snapshots)
    database.save_selection_trend(trend)
    assessment = assess_item(item, current, trend)
    assessment["explanation"]["multi_snapshot_trend"] = multi_snapshot_trend
    database.save_selection_candidate_assessment(assessment)
    priority, priority_reasons = tracking_priority_from_trend(multi_snapshot_trend)
    database.update_selection_tracking_priority(item_id, priority, priority_reasons)
    return assessment
