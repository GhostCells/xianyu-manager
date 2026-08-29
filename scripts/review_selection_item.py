from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from xianyu_manager.config import load_settings
from xianyu_manager.database import Database
from xianyu_manager.selection_review import normalize_status, status_label, validate_reason


def configure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="闲鱼候选商品人工审核")
    parser.add_argument("--database", type=Path, help="可选：指定数据库文件")
    commands = parser.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser("list", help="列出候选商品")
    list_parser.add_argument("--status", default="待审核", help="中文或英文状态；all 表示全部")
    list_parser.add_argument("--limit", type=int, default=20)

    set_parser = commands.add_parser("set", help="设置候选状态")
    set_parser.add_argument("item_id")
    set_parser.add_argument("status", help="待审核/值得测试/不值得测试/已测试/已成交")
    set_parser.add_argument("--reason", default="")
    set_parser.add_argument("--note", default="")
    set_parser.add_argument("--yes", action="store_true", help="跳过交互确认")

    history_parser = commands.add_parser("history", help="查看审核历史")
    history_parser.add_argument("item_id")
    return parser.parse_args()


def _short_title(value: object, limit: int = 60) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def command_list(database: Database, status: str, limit: int) -> int:
    normalized_status = None if status.strip().lower() == "all" else normalize_status(status)
    rows = database.list_selection_review_items(status=normalized_status, limit=limit)
    output = []
    for row in rows:
        output.append({
            "item_id": row["item_id"],
            "title": _short_title(row["title_raw"]),
            "url": row["canonical_url"],
            "status": status_label(str(row["candidate_status"])),
            "price": row.get("price_text"),
            "want_count": row.get("want_count"),
            "browse_count": row.get("browse_count"),
            "collect_count": row.get("collect_count"),
            "detail_observed_at": row.get("detail_observed_at"),
        })
    _print_json({"count": len(output), "items": output})
    return 0


def command_set(
    database: Database,
    item_id: str,
    status: str,
    reason: str,
    note: str,
    confirmed: bool,
) -> int:
    normalized_status = normalize_status(status)
    normalized_reason = validate_reason(normalized_status, reason)
    item = database.get_selection_item(item_id.strip())
    if item is None:
        raise ValueError("候选商品不存在")
    current = str(item.get("candidate_status") or "unreviewed")
    print(f"商品：{_short_title(item['title_raw'])}")
    print(f"状态：{status_label(current)} → {status_label(normalized_status)}")
    if not confirmed:
        answer = input("确认记录本次人工审核？输入 yes 继续：").strip().lower()
        if answer != "yes":
            print("已取消，数据库未修改。")
            return 1
    review = database.set_selection_candidate_status(
        item_id,
        normalized_status,
        reason_code=normalized_reason,
        note=note,
    )
    _print_json({
        "ok": True,
        "review_id": review["review_id"],
        "item_id": review["item_id"],
        "previous_status": status_label(str(review["previous_status"])),
        "new_status": status_label(str(review["new_status"])),
        "reason": review.get("reason_code"),
        "note": review.get("note"),
        "reviewed_at": review["reviewed_at"],
    })
    return 0


def command_history(database: Database, item_id: str) -> int:
    rows = database.list_selection_item_reviews(item_id)
    output = [{
        "review_id": row["review_id"],
        "from": status_label(str(row["previous_status"])),
        "to": status_label(str(row["new_status"])),
        "reason": row.get("reason_code"),
        "note": row.get("note"),
        "reviewed_at": row["reviewed_at"],
        "source": row["source"],
    } for row in rows]
    _print_json({"item_id": item_id, "count": len(output), "history": output})
    return 0


def main() -> int:
    configure_utf8_console()
    args = parse_args()
    settings = load_settings()
    database = Database(args.database or settings.database_path)
    if args.command == "list":
        return command_list(database, args.status, args.limit)
    if args.command == "set":
        return command_set(database, args.item_id, args.status, args.reason, args.note, args.yes)
    return command_history(database, args.item_id)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
