from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable
CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
ENDED_STATUSES = {"success", "partial", "failed", "blocked"}
ATTEMPTED_DETAIL_STATUSES = {"success", "failed", "verification_required"}


def _bounds(value: date) -> tuple[str, str]:
    start = datetime.combine(value, time.min, tzinfo=CHINA_TIMEZONE)
    end = start + timedelta(days=1)
    return tuple(
        point.astimezone(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        for point in (start, end)
    )


def percentile(values: Iterable[float | int], q: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _distribution(values: list[float | int]) -> dict[str, Any]:
    return {
        "n": len(values),
        "min": min(values) if values else None,
        "p25": percentile(values, 0.25),
        "p50": percentile(values, 0.50),
        "p75": percentile(values, 0.75),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
        "mean": mean(values) if values else None,
    }


def _rate_distribution(rows: list[dict[str, Any]], numerator: str) -> dict[str, Any]:
    raw: list[float] = []
    smoothed: list[float] = []
    for row in rows:
        count, browse = row.get(numerator), row.get("browse_count")
        if count is None or browse is None or int(browse) <= 0:
            continue
        raw.append(int(count) / int(browse))
        smoothed.append((int(count) + 2) / (int(browse) + 100))
    return {"raw": _distribution(raw), "smoothed": _distribution(smoothed)}


def _latest_by_item(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        item_id = str(row["item_id"])
        observed = str(row.get("detail_observed_at") or row.get("observed_at") or "")
        old = latest.get(item_id)
        old_observed = str(old.get("detail_observed_at") or old.get("observed_at") or "") if old else ""
        if old is None or observed > old_observed:
            latest[item_id] = row
    return list(latest.values())


def _candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if all(row.get(field) is not None for field in ("want_count", "browse_count", "collect_count")):
            grouped[str(row["keyword"])].append(row)
    output: list[dict[str, Any]] = []
    for keyword_rows in grouped.values():
        want_p75 = percentile((int(row["want_count"]) for row in keyword_rows), 0.75)
        collect_p75 = percentile((int(row["collect_count"]) for row in keyword_rows), 0.75)
        browse_p50 = percentile((int(row["browse_count"]) for row in keyword_rows), 0.50)
        for row in keyword_rows:
            want, browse, collect = (int(row[key]) for key in ("want_count", "browse_count", "collect_count"))
            if browse >= browse_p50 and (want >= want_p75 or collect >= collect_p75):
                candidate = dict(row)
                candidate.update(
                    sample_size=len(keyword_rows),
                    want_rate=want / browse if browse else None,
                    collect_rate=collect / browse if browse else None,
                )
                output.append(candidate)
    return sorted(
        output,
        key=lambda row: (int(row["want_count"]), int(row["collect_count"]), int(row["browse_count"])),
        reverse=True,
    )[:20]


def collect_daily_observation(database_path: Path, report_date: date) -> dict[str, Any]:
    if not database_path.is_file():
        raise FileNotFoundError(f"数据库不存在：{database_path}")
    start, end = _bounds(report_date)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        runs = [dict(row) for row in connection.execute(
            "SELECT * FROM selection_search_runs WHERE started_at>=? AND started_at<? ORDER BY started_at", (start, end)
        ).fetchall()]
        snapshots = [dict(row) for row in connection.execute(
            """
            SELECT s.*, i.canonical_url FROM selection_item_snapshots s
            JOIN selection_items i ON i.item_id=s.item_id
            WHERE COALESCE(s.detail_observed_at,s.observed_at)>=?
              AND COALESCE(s.detail_observed_at,s.observed_at)<?
            ORDER BY COALESCE(s.detail_observed_at,s.observed_at)
            """, (start, end)
        ).fetchall()]
        first_seen = int(connection.execute(
            "SELECT COUNT(*) FROM selection_items WHERE first_seen_at>=? AND first_seen_at<?", (start, end)
        ).fetchone()[0])
    finally:
        connection.close()

    statuses = Counter(str(row["status"]) for row in runs)
    ended = [row for row in runs if row["status"] in ENDED_STATUSES]
    attempted = [row for row in snapshots if row["detail_status"] in ATTEMPTED_DETAIL_STATUSES]
    successful = [row for row in snapshots if row["detail_status"] == "success"]
    complete = [row for row in successful if all(row.get(key) is not None for key in ("want_count", "browse_count", "collect_count"))]
    latest = _latest_by_item(successful)

    keyword_rows: list[dict[str, Any]] = []
    names = sorted({str(row["keyword"]) for row in runs} | {str(row["keyword"]) for row in snapshots})
    for keyword in names:
        kruns = [row for row in runs if row["keyword"] == keyword]
        kdetails = [row for row in snapshots if row["keyword"] == keyword]
        kattempted = [row for row in kdetails if row["detail_status"] in ATTEMPTED_DETAIL_STATUSES]
        ksuccess = [row for row in kdetails if row["detail_status"] == "success"]
        kcomplete = [row for row in ksuccess if all(row.get(key) is not None for key in ("want_count", "browse_count", "collect_count"))]
        keyword_rows.append({
            "keyword": keyword,
            "runs": len(kruns),
            "search_success": sum(row["status"] == "success" for row in kruns),
            "results": sum(int(row["result_count"] or 0) for row in kruns if row["status"] == "success"),
            "detail_attempted": len(kattempted),
            "detail_success": len(ksuccess),
            "detail_rate": _ratio(len(ksuccess), len(kattempted)),
            "complete": len(kcomplete),
            "complete_rate": _ratio(len(kcomplete), len(ksuccess)),
            "want_p50": percentile((int(row["want_count"]) for row in kcomplete), 0.50),
            "browse_p50": percentile((int(row["browse_count"]) for row in kcomplete), 0.50),
            "collect_p50": percentile((int(row["collect_count"]) for row in kcomplete), 0.50),
            "sample_status": "可观察" if len(kcomplete) >= 3 else "样本不足",
        })

    collection = {
        "run_count": len(runs),
        "keyword_count": len({str(row["keyword"]) for row in runs}),
        "search_success": statuses["success"],
        "search_success_rate": _ratio(statuses["success"], len(ended)),
        "partial": statuses["partial"], "failed": statuses["failed"], "blocked": statuses["blocked"],
        "result_count": sum(int(row["result_count"] or 0) for row in runs if row["status"] == "success"),
        "first_seen": first_seen,
        "observable_items": len({str(row["item_id"]) for row in snapshots}),
        "detail_attempted": len(attempted), "detail_success": len(successful),
        "detail_success_rate": _ratio(len(successful), len(attempted)),
        "detail_failed": sum(row["detail_status"] == "failed" for row in snapshots),
        "verification": sum(row["detail_status"] == "verification_required" for row in snapshots),
        "skipped": sum(row["detail_status"] == "skipped" for row in snapshots),
        "errors": dict(Counter(str(row["error_code"]) for row in runs if row.get("error_code"))),
    }
    completeness = {
        "want": _ratio(sum(row.get("want_count") is not None for row in successful), len(successful)),
        "browse": _ratio(sum(row.get("browse_count") is not None for row in successful), len(successful)),
        "collect": _ratio(sum(row.get("collect_count") is not None for row in successful), len(successful)),
        "all": _ratio(len(complete), len(successful)),
        "price": _ratio(sum(row.get("price_cents") is not None and row["price_parse_status"] == "parsed" for row in snapshots), len(snapshots)),
    }
    return {
        "date": report_date.isoformat(), "utc_start": start, "utc_end": end,
        "collection": collection, "completeness": completeness,
        "distributions": {
            "want_count": _distribution([int(row["want_count"]) for row in latest if row.get("want_count") is not None]),
            "browse_count": _distribution([int(row["browse_count"]) for row in latest if row.get("browse_count") is not None]),
            "collect_count": _distribution([int(row["collect_count"]) for row in latest if row.get("collect_count") is not None]),
            "want_rate": _rate_distribution(latest, "want_count"),
            "collect_rate": _rate_distribution(latest, "collect_count"),
        },
        "keywords": keyword_rows,
        "candidates": _candidates(successful),
    }


def _pct(value: float | None) -> str:
    return "暂无样本" if value is None else f"{value * 100:.1f}%"


def _num(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}" if isinstance(value, float) and not value.is_integer() else str(int(value))


def _safe(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def render_daily_markdown(report: dict[str, Any]) -> str:
    c, complete, dist = report["collection"], report["completeness"], report["distributions"]
    lines = [
        f"# 闲鱼选品数据观察日报｜{report['date']}", "",
        "> 本报告只描述采集质量和互动分布，不是选品评分；想要数、浏览数和收藏数均不代表销量。", "",
        "## 1. 每日采集数量与成功率", "", "| 指标 | 结果 |", "|---|---:|",
        f"| 实际搜索关键词 | {c['keyword_count']} |", f"| 搜索运行数 | {c['run_count']} |",
        f"| 搜索成功数 | {c['search_success']} |", f"| 搜索成功率 | {_pct(c['search_success_rate'])} |",
        f"| 搜索返回商品条数 | {c['result_count']} |", f"| 当日首次发现商品 | {c['first_seen']} |",
        f"| 可观测详情商品 | {c['observable_items']} |", f"| 详情执行数 | {c['detail_attempted']} |",
        f"| 详情成功数 | {c['detail_success']} |", f"| 详情成功率 | {_pct(c['detail_success_rate'])} |", "",
        f"搜索异常：partial={c['partial']}，failed={c['failed']}，blocked={c['blocked']}。", "",
        "## 2. 字段完整率", "", "| 字段 | 完整率 |", "|---|---:|",
        f"| want_count | {_pct(complete['want'])} |", f"| browse_count | {_pct(complete['browse'])} |",
        f"| collect_count | {_pct(complete['collect'])} |", f"| 三项互动字段联合 | {_pct(complete['all'])} |",
        f"| 价格 | {_pct(complete['price'])} |", "", "## 3. 商品互动指标分布", "",
        "当日每个商品只采用最新一条成功详情快照。", "",
        "| 指标 | n | 最小 | P25 | P50 | P75 | P90 | P95 | 最大 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, label in (("want_count", "wantCnt"), ("browse_count", "browseCnt"), ("collect_count", "collectCnt")):
        row = dist[key]
        lines.append(f"| {label} | {row['n']} | {_num(row['min'])} | {_num(row['p25'])} | {_num(row['p50'])} | {_num(row['p75'])} | {_num(row['p90'])} | {_num(row['p95'])} | {_num(row['max'])} |")
    lines += ["", "### 互动率分布", "", "| 指标 | n | P25 | P50 | P75 | P90 | P95 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for key, label in (("want_rate", "平滑 wantRate"), ("collect_rate", "平滑 collectRate")):
        row = dist[key]["smoothed"]
        lines.append(f"| {label} | {row['n']} | {_pct(row['p25'])} | {_pct(row['p50'])} | {_pct(row['p75'])} | {_pct(row['p90'])} | {_pct(row['p95'])} |")
    lines += ["", "## 4. 关键词质量统计", "", "| 关键词 | 搜索成功/运行 | 返回商品 | 详情成功/执行 | 联合完整率 | want P50 | browse P50 | collect P50 | 状态 |", "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for row in report["keywords"]:
        lines.append(f"| {_safe(row['keyword'])} | {row['search_success']}/{row['runs']} | {row['results']} | {row['detail_success']}/{row['detail_attempted']} | {_pct(row['complete_rate'])} | {_num(row['want_p50'])} | {_num(row['browse_p50'])} | {_num(row['collect_p50'])} | {row['sample_status']} |")
    if not report["keywords"]:
        lines.append("| — | 0/0 | 0 | 0/0 | 暂无样本 | — | — | — | 无数据 |")
    lines += ["", "## 5. 高互动候选（仅供人工查看）", "", "> 同关键词内 wantCnt 或 collectCnt 达到 P75，且 browseCnt 不低于 P50；这不是评分结果。", "", "| 关键词 | 商品 | 价格 | want | browse | collect | wantRate | collectRate | 样本数 |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in report["candidates"]:
        title, url = _safe(row["title_raw"]), str(row.get("canonical_url") or "")
        linked = f"[{title}]({url})" if url else title
        price = f"¥{int(row['price_cents']) / 100:.2f}" if row.get("price_cents") is not None else "—"
        lines.append(f"| {_safe(row['keyword'])} | {linked} | {price} | {row['want_count']} | {row['browse_count']} | {row['collect_count']} | {_pct(row['want_rate'])} | {_pct(row['collect_rate'])} | {row['sample_size']} |")
    if not report["candidates"]:
        lines.append("| — | 当日没有满足条件的完整样本 | — | — | — | — | — | — | — |")
    lines += ["", "## 6. 异常与限制", ""]
    lines.append(f"详情失败 {c['detail_failed']} 条，验证中止 {c['verification']} 条，未执行详情 {c['skipped']} 条。")
    if c["errors"]:
        lines += ["", "搜索错误：" + "；".join(f"{_safe(code)}={count}" for code, count in sorted(c["errors"].items())) + "。"]
    lines += ["", "- 搜索批次当前只保存结果总数，没有保存全部搜索卡片的历史关联；去重商品数只能按详情快照口径观察。", "- 高互动候选只供人工查看，不代表销量、成交或未来表现。", "- 单关键词完整样本少于 3 条时标记为样本不足。", "", f"统计区间（UTC）：`{report['utc_start']}` 至 `{report['utc_end']}`。", ""]
    return "\n".join(lines)


def write_daily_markdown(database_path: Path, report_date: date, output_path: Path) -> dict[str, Any]:
    report = collect_daily_observation(database_path, report_date)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_daily_markdown(report), encoding="utf-8")
    return report
