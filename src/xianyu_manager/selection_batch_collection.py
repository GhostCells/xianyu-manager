from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .database import Database
from .selection_tracking import (
    DEFAULT_MIN_INTERVAL_HOURS,
    DEFAULT_TIMEZONE,
    plan_detail_candidates,
)


STOP_CODES = {
    "LOGIN_REQUIRED",
    "VERIFICATION_REQUIRED",
    "BROWSER_NOT_RUNNING",
    "SELECTION_BUSY",
}


def _error_detail(response: httpx.Response) -> tuple[str, str]:
    try:
        detail: Any = response.json().get("detail", {})
    except (ValueError, AttributeError):
        detail = {}
    if isinstance(detail, dict):
        return (
            str(detail.get("code") or f"HTTP_{response.status_code}"),
            str(detail.get("message") or "详情采集失败"),
        )
    return f"HTTP_{response.status_code}", str(detail or "详情采集失败")


async def collect_selection_details_batch(
    run_id: str,
    database: Database,
    *,
    bridge_token: str,
    limit: int = 10,
    interval_seconds: float = 5,
    tracking_min_interval_hours: float = DEFAULT_MIN_INTERVAL_HOURS,
    tracking_timezone: str = DEFAULT_TIMEZONE,
    detail_url: str = "http://127.0.0.1:8765/api/internal/selection/detail",
    client: httpx.AsyncClient | None = None,
) -> dict[str, object]:
    normalized_run_id = run_id.strip()
    if not normalized_run_id:
        raise ValueError("search_run不能为空")
    if not bridge_token.strip():
        raise ValueError("详情采集密钥为空")
    bounded_limit = max(1, min(int(limit), 20))
    bounded_interval = max(0.0, min(float(interval_seconds), 30.0))
    plan = plan_detail_candidates(
        database.list_selection_detail_candidate_contexts(normalized_run_id),
        limit=bounded_limit,
        min_interval_hours=tracking_min_interval_hours,
        timezone_name=tracking_timezone,
    )
    candidates = plan["selected"]
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=50)
    results: list[dict[str, object]] = []
    stopped_code = ""
    try:
        for index, candidate in enumerate(candidates):
            item_id = str(candidate["item_id"])
            try:
                response = await http_client.post(
                    detail_url,
                    headers={"X-Internal-Token": bridge_token.strip()},
                    json={"item_id": item_id},
                )
                if response.is_error:
                    code, message = _error_detail(response)
                    if code != "SELECTION_BUSY":
                        database.save_selection_item_snapshot(
                            item_id,
                            price_cents=None,
                            price_text="",
                            want_count=None,
                            browse_count=None,
                            collect_count=None,
                            detail_status=(
                                "verification_required"
                                if code in {"LOGIN_REQUIRED", "VERIFICATION_REQUIRED"}
                                else "failed"
                            ),
                            detail_error_code=code,
                        )
                    results.append(
                        {"item_id": item_id, "ok": False, "code": code, "message": message}
                    )
                    if code in STOP_CODES:
                        stopped_code = code
                        break
                else:
                    payload = response.json()
                    results.append(
                        {
                            "item_id": item_id,
                            "ok": True,
                            "snapshot_id": payload.get("snapshot", {}).get("snapshot_id"),
                        }
                    )
            except Exception as exc:
                code = type(exc).__name__.upper()
                database.save_selection_item_snapshot(
                    item_id,
                    price_cents=None,
                    price_text="",
                    want_count=None,
                    browse_count=None,
                    collect_count=None,
                    detail_status="failed",
                    detail_error_code=code,
                )
                results.append(
                    {"item_id": item_id, "ok": False, "code": code, "message": str(exc)}
                )
            if index + 1 < len(candidates) and bounded_interval:
                await asyncio.sleep(bounded_interval)
    finally:
        if owns_client:
            await http_client.aclose()

    return {
        "ok": not stopped_code,
        "run_id": normalized_run_id,
        "requested_limit": bounded_limit,
        "candidate_count": len(candidates),
        "new_eligible_count": plan["new_eligible_count"],
        "tracking_due_count": plan["tracking_due_count"],
        "selected_new_count": plan["selected_new_count"],
        "selected_tracking_count": plan["selected_tracking_count"],
        "skip_counts": plan["skip_counts"],
        "processed_count": len(results),
        "success_count": sum(bool(item["ok"]) for item in results),
        "failed_count": sum(not bool(item["ok"]) for item in results),
        "stopped_code": stopped_code or None,
        "results": results,
    }
