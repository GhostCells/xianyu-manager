"""Read-only diagnostics of an existing SQLite snapshot. Standard library only."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .fulfillment_rules import compose_delivery_message, matches_registered_listing, delivery_issues, parse_listing_id


FORMAT_VERSION = "offline-fulfillment-v1"
NOTICE = (
    "私有运营快照。数据库核验标记不代表本次在线验证；离线期间链接可能变化。"
    "可提供文本不代表订单获准发送。复制不等于已发送，人工已发送不等于平台已确认。"
    "恢复自动化前必须核对人工补发及不确定订单，不能保证离线操作 exactly-once。"
)
FIELDS = {
    "accounts": "id",
    "products": "dir_name number name title zip_hash zip_name zip_size quality_errors_json share_url share_code share_verified share_needs_review quality_status",
    "account_products": "account_id product_dir_name enabled listing_url listing_status",
    "account_listings": "account_id item_id matched_product_dir_name is_active",
}


class CatalogError(ValueError):
    """Messages are fixed codes: never echo database values or secrets."""


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _listing_id(url: str) -> str:
    return parse_listing_id(url)


def read_catalog(database_path: Path, *, blocked_product_refs: tuple[str, ...] = ()) -> dict:
    path = database_path.expanduser().resolve(strict=True)
    if not path.is_file():
        raise CatalogError("DATABASE_NOT_FILE")
    # Require a consistent standalone backup. Do not create/modify WAL shared
    # memory as a side effect of opening an actively used production database.
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise CatalogError("STANDALONE_SQLITE_BACKUP_REQUIRED")
    before_hash = _hash(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise CatalogError("DATABASE_INTEGRITY_FAILED")
        rows = {}
        for table, required in FIELDS.items():
            columns = {r["name"] for r in connection.execute(f'PRAGMA table_info("{table}")')}
            if not set(required.split()) <= columns:
                raise CatalogError("UNSUPPORTED_SCHEMA")
            # Deliberately do not SELECT *: knowledge, chat, buyers and secrets
            # are never read or exported.
            select = ",".join('"' + name + '"' for name in required.split())
            if table == "products":
                for name in ("verified_fingerprint", "share_verified_at"):
                    select += ',' + ('"' + name + '"' if name in columns else 'NULL AS "' + name + '"')
            rows[table] = [dict(r) for r in connection.execute(f'SELECT {select} FROM "{table}" ORDER BY rowid')]
    finally:
        connection.close()
    if _hash(path) != before_hash:
        raise CatalogError("DATABASE_CHANGED_DURING_READ")

    products = {p["dir_name"]: p for p in rows["products"]}
    if set(blocked_product_refs) - products.keys():
        raise CatalogError("UNKNOWN_BLOCKED_PRODUCT_REF")
    bindings = rows["account_products"]
    listings = rows["account_listings"]
    accounts = {a["id"] for a in rows["accounts"]}
    mapping_problems = Counter()
    eligible_mappings = {}
    for binding in bindings:
        ref = binding["product_dir_name"]
        reasons = []
        if binding["account_id"] not in accounts:
            reasons.append("MAPPING_ACCOUNT_MISSING")
        item_id = _listing_id(binding["listing_url"])
        if ref not in products:
            reasons.append("MAPPING_PRODUCT_MISSING")
        if not binding["enabled"]:
            reasons.append("ACCOUNT_PRODUCT_DISABLED")
        if binding["listing_status"] != "published":
            reasons.append("NOT_PUBLISHED")
        if not item_id:
            reasons.append("LISTING_ID_UNCONFIRMED")
        else:
            # Share the online exact match predicate and reject ambiguity.
            matches = [b for b in bindings if b["account_id"] == binding["account_id"]
                       and matches_registered_listing({**b, "enabled_for_account": b["enabled"]}, item_id)]
            if len(matches) != 1:
                reasons.append("AMBIGUOUS_RUNTIME_MAPPING")
            snapshots = [l for l in listings if l["account_id"] == binding["account_id"] and l["item_id"] == item_id]
            if snapshots and any(not l["is_active"] or l["matched_product_dir_name"] != ref for l in snapshots):
                reasons.append("LISTING_MAPPING_CONFLICT")
        mapping_problems.update(reasons)
        eligible_mappings.setdefault(ref, []).append({
            "account_id": binding["account_id"], "listing_item_id": item_id or None,
            "issues": reasons,
        })
    for listing in listings:
        if listing["is_active"] and listing["matched_product_dir_name"] not in products:
            mapping_problems["ACTIVE_LISTING_PRODUCT_MISSING"] += 1
        elif listing["is_active"] and not any(
            b["account_id"] == listing["account_id"] and b["product_dir_name"] == listing["matched_product_dir_name"]
            for b in bindings
        ):
            mapping_problems["ACTIVE_LISTING_ACCOUNT_MAPPING_MISSING"] += 1

    records = []
    problems = Counter()
    for ref, product in products.items():
        reasons = delivery_issues(product, operator_blocked=ref in blocked_product_refs)
        mappings = eligible_mappings.get(ref, [])
        if not mappings:
            reasons.append("ACCOUNT_MAPPING_MISSING")
        elif not any(not m["issues"] for m in mappings):
            reasons.append("NO_ELIGIBLE_LISTING_MAPPING")
        ready = not reasons
        problems.update(reasons)
        records.append({
            "record_ref": "product:" + ref, "product_ref": ref,
            "stable_product_id": None,
            "display_name": product["title"] or product["name"],
            "delivery_version_id": None, "zip_sha256": product["zip_hash"] or None,
            "share_revision": product.get("verified_fingerprint") or None,
            "share_verified_at": product.get("share_verified_at"),
            "registered_share_verified": bool(product["share_verified"]),
            "share_needs_review": bool(product["share_needs_review"]),
            "online_status": "not_checked", "eligible_mappings": [m for m in mappings if not m["issues"]],
            "mapping_issues": [m for m in mappings if m["issues"]],
            "can_provide_delivery_text": ready, "issues": reasons,
            # Blocked records contain issues only, not an accidentally reusable
            # raw URL/code alongside the refusal.
            "delivery_text": compose_delivery_message(product) if ready else None,
        })
    return {
        "format_version": FORMAT_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_snapshot_sha256": before_hash, "notice": NOTICE,
        "summary": {"products": len(records), "text_available": sum(r["can_provide_delivery_text"] for r in records),
                    "text_blocked": sum(not r["can_provide_delivery_text"] for r in records),
                    "product_issue_counts": dict(sorted(problems.items())),
                    "mapping_issue_counts": dict(sorted(mapping_problems.items()))},
        "records": records,
    }


def _markdown(catalog: dict) -> str:
    lines = ["# 私有离线交付台账", "", NOTICE, "", "生成时间：" + catalog["generated_at"],
             "格式：" + FORMAT_VERSION, "", "缺失的稳定商品 ID、交付版本、分享修订及核验时间均未提供。", ""]
    for record in catalog["records"]:
        # Untrusted display text is literal code, never rendered HTML, images,
        # Markdown links or embedded remote resources.
        lines.extend(["## 交付记录", ""])
        for text in (record["record_ref"], record["display_name"]):
            lines.extend("    " + line for line in str(text).splitlines())
        lines.extend(["", "资格：" + ("可提供文本；仅适用于下列映射，不代表已经发送" if record["can_provide_delivery_text"] else "仅诊断，禁止据此发送"), ""])
        lines.append("问题：" + (", ".join(record["issues"]) or "无商品级阻断"))
        lines.append("允许映射：" + json.dumps(record["eligible_mappings"], ensure_ascii=False))
        lines.append("映射问题：" + json.dumps(record["mapping_issues"], ensure_ascii=False))
        lines.append("ZIP SHA-256：" + str(record["zip_sha256"] or "未提供"))
        lines.append("")
        if record["delivery_text"] is not None:
            lines.extend("    " + line for line in record["delivery_text"].splitlines())
            lines.append("")
    return "\n".join(lines)


def export_catalog(database_path: Path, output_directory: Path, *, blocked_product_refs: tuple[str, ...] = ()) -> dict:
    target = output_directory.expanduser().resolve()
    repository = Path(__file__).resolve().parents[2]
    if target == repository or repository in target.parents or any((p / ".git").exists() for p in (target, *target.parents)):
        raise CatalogError("OUTPUT_MUST_BE_OUTSIDE_GIT_REPOSITORIES")
    if target.exists() or output_directory.is_symlink():
        raise CatalogError("OUTPUT_ALREADY_EXISTS")
    if not target.parent.is_dir():
        raise CatalogError("OUTPUT_PARENT_MUST_EXIST")
    catalog = read_catalog(database_path, blocked_product_refs=blocked_product_refs)
    target.mkdir(mode=0o700)  # exclusive: never reuse an earlier snapshot
    for filename, content in (
        ("fulfillment-catalog.private.json", json.dumps(catalog, ensure_ascii=False, indent=2)),
        ("fulfillment-catalog.private.md", _markdown(catalog)),
    ):
        descriptor = os.open(target / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
    return catalog["summary"]


def main() -> int:
    parser = argparse.ArgumentParser(description="只读离线交付诊断；仅使用已存在的一致性 SQLite 备份")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, help="显式私有导出：必须是仓库外尚不存在的目录；省略仅输出脱敏摘要")
    parser.add_argument("--block-product-ref", action="append", default=[], help="人工已知不可用的现有 dir_name；可重复，只阻断本次文本导出，不写数据库")
    args = parser.parse_args()
    try:
        blocked = tuple(args.block_product_ref)
        result = (export_catalog(args.database, args.output_dir, blocked_product_refs=blocked)
                  if args.output_dir else read_catalog(args.database, blocked_product_refs=blocked)["summary"])
    except CatalogError as exc:
        print(json.dumps({"error_code": str(exc)}))
        return 2
    except (OSError, sqlite3.Error, ValueError, TypeError):
        print(json.dumps({"error_code": "OFFLINE_CATALOG_FAILED"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
