"""Pure share registration predicate and delivery text; no IO or runtime imports."""

import hashlib
import json
import re
from urllib.parse import parse_qs, urlparse


def parse_listing_id(url: str) -> str:
    try:
        parsed = urlparse(url)
        ids = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
        if (
            parsed.scheme == "https"
            and parsed.netloc in {"goofish.com", "www.goofish.com"}
            and parsed.path in {"/item", "/item/"}
            and len(ids) == 1
            and re.fullmatch(r"[0-9]+", ids[0])
            and not parsed.fragment
        ):
            return ids[0]
    except (ValueError, TypeError):
        pass
    return ""


def fulfillment_fingerprint(product: dict[str, object]) -> str:
    values = [product.get(k) or "" for k in ("share_url", "share_code", "zip_hash")]
    if product.get('delivery_kind', 'zip') == 'cloud':
        values = ['cloud-v1', product.get('share_url') or '', product.get('share_code') or '', product.get('delivery_revision') or '']
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def package_safety_fingerprint(product: dict[str, object]) -> str:
    """Version-bound ZIP safety receipt; never a share/user verification."""
    return hashlib.sha256(json.dumps(
        ['zip-safety-v1', product.get('zip_name'), product.get('zip_hash'), product.get('zip_size')],
        ensure_ascii=False, separators=(',', ':'),
    ).encode()).hexdigest()


def delivery_issues(
    product: dict[str, object],
    *,
    require_verified: bool | None = None,
    operator_blocked: bool = False,
) -> list[str]:
    from .scanner import delivery_blocking_errors
    if require_verified is None:
        from .config import read_catalog_delivery
        require_verified = not read_catalog_delivery()

    issues = []
    url = str(product.get("share_url") or "")
    try:
        parsed = urlparse(url)
        valid_url = (
            parsed.scheme == "https"
            and parsed.netloc.lower() == "pan.baidu.com"
            and parsed.path.startswith("/s/")
            and len(parsed.path) > 3
            and not any(c.isspace() for c in url)
        )
    except ValueError:
        valid_url = False
    if not url:
        issues.append("SHARE_MISSING")
    elif not valid_url:
        issues.append("SHARE_SYNTAX_INVALID")
    if operator_blocked:
        issues.append("OPERATOR_REPORTED_UNUSABLE")
    kind = product.get('delivery_kind', 'zip')
    if kind not in ('zip', 'cloud'):
        issues.append('DELIVERY_KIND_INVALID')
    if kind == 'cloud':
        if not re.fullmatch(r'[0-9a-f]{64}', str(product.get('delivery_revision') or '')):
            issues.append('DELIVERY_CLOUD_VERSION_UNCONFIRMED')
        if product.get('zip_name') or product.get('zip_hash') or product.get('zip_size'):
            issues.append('DELIVERY_KIND_INVALID')
    elif (
        not product.get("zip_name")
        or not re.fullmatch(r"[0-9a-f]{64}", str(product.get("zip_hash") or ""))
        or not isinstance(product.get("zip_size"), int)
        or product["zip_size"] <= 0
    ):
        issues.append("DELIVERY_PACKAGE_UNCONFIRMED")
    errors = product.get("quality_errors")
    if errors is None:
        try:
            errors = json.loads(str(product.get("quality_errors_json", "null")))
        except ValueError:
            errors = None
    safety = product.get('delivery_safety_fingerprint')
    if kind == 'cloud':
        pass  # No fictitious ZIP or publishing quality approval for netdisk tutorials.
    elif safety:
        if safety != package_safety_fingerprint(product):
            issues.append('DELIVERY_PACKAGE_SAFETY_UNCONFIRMED')
    elif (
        product.get("quality_status") != "passed"
        or not isinstance(errors, list)
        or not all(isinstance(e, str) for e in errors)
        or delivery_blocking_errors(errors)
    ):
        issues.append("QUALITY_BLOCKED")
    if require_verified:
        if not product.get("share_verified"):
            issues.append("SHARE_UNVERIFIED")
        if product.get("share_needs_review"):
            issues.append("SHARE_NEEDS_REVIEW")
        if product.get("verified_fingerprint") != fulfillment_fingerprint(product):
            issues.append("VERIFICATION_VERSION_UNCONFIRMED")
    return issues


def matches_registered_listing(product: dict[str, object], item_id: str) -> bool:
    """Exact supported URL and string ID, without redirects or network IO."""
    return bool(
        product["enabled_for_account"]
        and product["listing_status"] == "published"
        and item_id
        and parse_listing_id(str(product.get("listing_url") or "")) == item_id
    )


def compose_delivery_message(product: dict[str, object]) -> str:
    lines = [
        f"拍下啦～这是你购买的「{product.get('title') or product.get('name')}」：",
        "",
        f"百度网盘：{product.get('share_url')}",
    ]
    share_code = str(product.get("share_code") or "").strip()
    if share_code:
        lines.append(f"提取码：{share_code}")
    lines.extend(
        [
            "",
            "如果觉得内容对你有帮助，方便的话可以留下个评价，感谢支持～",
            "有什么问题可以继续沟通～",
        ]
    )
    return "\n".join(lines)
