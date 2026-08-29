from __future__ import annotations

from typing import Final


STATUS_LABELS: Final[dict[str, str]] = {
    "unreviewed": "待审核",
    "worth_testing": "值得测试",
    "not_worth_testing": "不值得测试",
    "tested": "已测试",
    "converted": "已成交",
}
STATUS_ALIASES: Final[dict[str, str]] = {
    **{key: key for key in STATUS_LABELS},
    **{label: key for key, label in STATUS_LABELS.items()},
}
REASONS_BY_STATUS: Final[dict[str, set[str]]] = {
    "unreviewed": {"correction"},
    "worth_testing": {
        "clear_demand", "good_interaction", "good_price_band",
        "deliverable", "low_support_cost", "portfolio_fit", "correction",
    },
    "not_worth_testing": {
        "weak_demand", "irrelevant", "copyright_risk", "platform_risk",
        "delivery_difficulty", "high_support_cost", "price_too_low",
        "technical_gap", "duplicate_product", "correction",
    },
    "tested": {"listing_published", "manual_trial", "correction"},
    "converted": {"first_real_order", "repeat_order", "correction"},
}


def normalize_status(value: str) -> str:
    normalized = STATUS_ALIASES.get(value.strip())
    if normalized is None:
        allowed = "、".join(STATUS_LABELS.values())
        raise ValueError(f"状态无效，可选：{allowed}")
    return normalized


def validate_reason(status: str, reason: str) -> str:
    normalized = reason.strip()
    if normalized and normalized not in REASONS_BY_STATUS[status]:
        allowed = "、".join(sorted(REASONS_BY_STATUS[status]))
        raise ValueError(f"原因与目标状态不匹配，可选：{allowed}")
    return normalized


def status_label(value: str) -> str:
    return STATUS_LABELS.get(value, value)
