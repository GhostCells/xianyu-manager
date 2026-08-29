from __future__ import annotations

import json
import math
from collections import defaultdict

from .database import Database

SCORE_VERSION = "rule-mvp-v1"
MIN_COMPLETE_COHORT = 5
SCENE_TERMS = ("自动化", "工作流", "生成", "剪辑", "分镜", "写作", "办公", "视频", "绘画", "代码", "开发", "运营", "电商", "财务", "测试")
AUDIENCE_TERMS = ("新手", "程序员", "开发者", "运营", "卖家", "自媒体", "设计师", "学生", "职场", "会计", "产品经理")
PRODUCT_TERMS = ("skill", "工具", "软件", "教程", "工作流", "模板", "插件", "脚本", "资料", "系统")
EXCLUSION_TERMS = ("实体", "二手手机", "招聘", "求职", "代学", "题库")


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _percentile_scores(values: dict[int, float]) -> dict[int, float]:
    """Stable 0..100 percentile scores using average ranks for ties."""
    if not values:
        return {}
    if len(values) == 1 or len(set(values.values())) == 1:
        return {key: 50.0 for key in values}
    ordered = sorted(values.items(), key=lambda pair: (pair[1], pair[0]))
    output: dict[int, float] = {}
    index = 0
    denominator = len(ordered) - 1
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        score = round(((index + end - 1) / 2) / denominator * 100, 4)
        for position in range(index, end):
            output[ordered[position][0]] = score
        index = end
    return output


def title_relevance_score(title: object, keyword: object) -> tuple[float, list[str]]:
    normalized_title = " ".join(str(title or "").casefold().split())
    normalized_keyword = " ".join(str(keyword or "").casefold().split())
    if not normalized_title:
        return 0.0, ["title_raw"]
    score = 0.0
    if normalized_keyword and normalized_keyword in normalized_title:
        score += 40
    if any(term in normalized_title for term in SCENE_TERMS):
        score += 25
    if any(term in normalized_title for term in AUDIENCE_TERMS):
        score += 20
    if any(term in normalized_title for term in PRODUCT_TERMS):
        score += 15
    if any(term in normalized_title for term in EXCLUSION_TERMS):
        score -= 30
    return _clamp(score), []


def price_fit_score(price_cents: object, parse_status: object) -> tuple[float, list[str]]:
    if str(parse_status or "") != "parsed" or price_cents is None:
        return 0.0, ["price_cents"]
    price = int(price_cents)
    if price < 100:
        return 20.0, []
    if price <= 989:
        return 60.0, []
    if price <= 9900:
        return 100.0, []
    if price <= 19900:
        return 80.0, []
    return 40.0, []


def _interaction_missing(row: dict[str, object]) -> list[str]:
    missing: list[str] = []
    if row.get("detail_status") != "success":
        missing.append("detail_status")
    for field in ("want_count", "browse_count", "collect_count"):
        value = row.get(field)
        if value is None:
            missing.append(field)
        elif int(value) < 0:
            missing.append(f"{field}_invalid")
    browse_count = row.get("browse_count")
    if browse_count is not None and int(browse_count) == 0:
        missing.append("browse_count_non_positive")
    return missing


def score_selection_run(database: Database, run_id: str, *, score_version: str = SCORE_VERSION) -> dict[str, object]:
    snapshots = database.list_selection_scoring_snapshots(run_id)
    if not snapshots:
        raise ValueError("该搜索批次没有可评分快照")
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for snapshot in snapshots:
        groups[str(snapshot.get("keyword") or "")].append(snapshot)

    score_rows: list[dict[str, object]] = []
    group_summaries: list[dict[str, object]] = []
    for keyword, rows in groups.items():
        eligible: dict[int, dict[str, object]] = {}
        missing_by_snapshot: dict[int, list[str]] = {}
        for row in rows:
            snapshot_id = int(row["snapshot_id"])
            missing = _interaction_missing(row)
            missing_by_snapshot[snapshot_id] = missing
            if not missing:
                eligible[snapshot_id] = row

        want_values = {key: (int(row["want_count"]) + 2) / (int(row["browse_count"]) + 100) for key, row in eligible.items()}
        collect_values = {key: (int(row["collect_count"]) + 2) / (int(row["browse_count"]) + 100) for key, row in eligible.items()}
        browse_values = {key: math.log1p(int(row["browse_count"])) for key, row in eligible.items()}
        want_scores = _percentile_scores(want_values)
        collect_scores = _percentile_scores(collect_values)
        browse_scores = _percentile_scores(browse_values)
        cohort_size = len(eligible)

        for row in rows:
            snapshot_id = int(row["snapshot_id"])
            relevance, relevance_missing = title_relevance_score(row.get("title_raw"), keyword)
            price_fit, price_missing = price_fit_score(row.get("price_cents"), row.get("price_parse_status"))
            missing = list(missing_by_snapshot[snapshot_id]) + relevance_missing + price_missing
            if cohort_size < MIN_COMPLETE_COHORT:
                missing.append("insufficient_cohort")
            want_score = want_scores.get(snapshot_id)
            collect_score = collect_scores.get(snapshot_id)
            browse_score = browse_scores.get(snapshot_id)
            total_score: float | None = None
            if cohort_size >= MIN_COMPLETE_COHORT and snapshot_id in eligible:
                total_score = round(want_score * 0.35 + collect_score * 0.25 + browse_score * 0.20 + relevance * 0.15 + price_fit * 0.05, 2)
            score_rows.append({
                "snapshot_id": snapshot_id,
                "want_rate_score": want_score,
                "collect_rate_score": collect_score,
                "browse_score": browse_score,
                "relevance_score": relevance,
                "price_fit_score": price_fit,
                "total_score": total_score,
                "missing_fields": list(dict.fromkeys(missing)),
            })
        group_summaries.append({
            "keyword": keyword,
            "snapshot_count": len(rows),
            "complete_sample_count": cohort_size,
            "formal_score_generated": cohort_size >= MIN_COMPLETE_COHORT,
            "confidence": "none" if cohort_size < 5 else "low" if cohort_size < 10 else "normal",
        })

    database.save_selection_scores(score_version, score_rows)
    results = database.list_selection_scores(run_id, score_version=score_version)
    for result in results:
        result["missing_fields"] = json.loads(str(result.pop("missing_fields_json")))
    return {
        "ok": True,
        "run_id": run_id,
        "score_version": score_version,
        "weights": {"smoothed_want_rate": 0.35, "smoothed_collect_rate": 0.25, "browse_heat": 0.20, "title_relevance": 0.15, "price_fit": 0.05},
        "groups": group_summaries,
        "formal_score_count": sum(row["total_score"] is not None for row in results),
        "null_score_count": sum(row["total_score"] is None for row in results),
        "items": results,
    }
