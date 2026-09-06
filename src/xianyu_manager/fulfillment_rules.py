"""Pure share registration predicate and delivery text; no IO or runtime imports."""


def registered_share_ready(product: dict[str, object]) -> bool:
    return bool(product.get("share_url") and product.get("share_verified")
                and not product.get("share_needs_review"))


def matches_registered_listing(product: dict[str, object], item_id: str) -> bool:
    """Existing runtime match semantics, intentionally preserved (see design)."""
    return bool(product["enabled_for_account"] and product["listing_status"] == "published"
                and f"id={item_id}" in str(product.get("listing_url") or ""))


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
