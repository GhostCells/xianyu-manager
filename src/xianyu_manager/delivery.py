from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import re
import secrets
import time
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import msgpack
import websockets

from .auto_reply import (
    DEFAULT_SYSTEM_PROMPT,
    SiliconFlowReplyClient,
    manual_review_reason,
)
from .database import Database
from .browser_launch import sandbox_options
from .fulfillment_rules import compose_delivery_message, delivery_issues, parse_listing_id
from .runtime_policy import PROCESS_POLICY, RuntimePolicy, business_operation
from .notifications import WindowsNotifier
from .security import SecretStore


GOOFISH_HOME = "https://www.goofish.com/"
TOKEN_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"
CONFIRM_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idle.logistic.consign.dummy/1.0/"
FREE_GROUP_URL = "https://h5api.m.goofish.com/h5/mtop.idle.groupon.activity.seller.freeshipping/1.0/"
ORDER_LIST_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idle.trade.merchant.sold.get/1.0/"
ITEM_LIST_URL = "https://h5api.m.goofish.com/h5/mtop.idle.web.xyh.item.list/1.0/"
WEBSOCKET_URL = "wss://wss-goofish.dingtalk.com/"
MTOP_APP_KEY = "34839810"
IM_APP_KEY = "444e9908a51d1cb236a27862abc769c9"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 "
    "Edg/150.0.0.0"
)


def browser_platform_header(platform_hint: str = "") -> str:
    value = platform_hint.strip().lower()
    if "win" in value:
        platform = "Windows"
    elif "mac" in value:
        platform = "macOS"
    elif "linux" in value or "x11" in value:
        platform = "Linux"
    elif sys.platform == "darwin":
        platform = "macOS"
    elif sys.platform == "win32":
        platform = "Windows"
    else:
        platform = "Linux"
    return f'"{platform}"'
PAID_MARKERS = (
    "[我已付款，等待你发货]",
    "我已付款，等待你发货",
    "[已付款，待发货]",
    "[买家已付款]",
    "等待卖家发货",
    "等待你发货",
    "付款完成待发货",
    "trade_paid_done_seller",
)
SESSION_COOKIE_NAMES = ("cookie2", "sgcookie", "_tb_token_")
RISK_CONTROL_COOLDOWN_SECONDS = 15 * 60
RECOVERY_WINDOW_SECONDS = 24 * 60 * 60
GROUP_WAITING_TITLES = {"我已小刀，待刀成", "我已小刀,待刀成"}
GROUP_READY_TITLES = {"我已成功小刀，待发货", "我已成功小刀,待发货"}


@dataclass
class PendingAutoReply:
    task: asyncio.Task[None]
    message_ids: list[int]
    messages: list[str]


def combine_buyer_messages(messages: list[str], *, max_chars: int = 2000) -> str:
    """Combine rapid buyer fragments into one coherent model input."""
    combined: list[str] = []
    for message in messages:
        normalized = re.sub(r"\s+", " ", str(message or "").strip())
        if not normalized or (combined and normalized == combined[-1]):
            continue
        combined.append(normalized)
    return " ".join(combined)[:max_chars]


class RiskControlError(RuntimeError):
    """The platform requested a quiet period before authentication is retried."""


class SessionVerificationRequired(RuntimeError):
    """The saved browser profile exists, but the messaging session is not usable."""


class OutboundSafetyError(RuntimeError):
    """A configured safety guard blocked an unattended outbound action."""


def generate_mid() -> str:
    return f"{secrets.randbelow(1000)}{int(time.time() * 1000)} 0"


def generate_message_uuid() -> str:
    return f"-{int(time.time() * 1000)}{secrets.randbelow(10)}"


def generate_mtop_sign(timestamp: str, token: str, data_json: str) -> str:
    payload = f"{token}&{timestamp}&{MTOP_APP_KEY}&{data_json}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def normalize_messagepack(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(normalize_messagepack(key)): normalize_messagepack(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_messagepack(item) for item in value]
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return base64.b64encode(value).decode("ascii")
    return value


def decode_sync_payload(raw_data: str) -> dict[str, Any] | None:
    try:
        decoded = base64.b64decode(raw_data)
    except Exception:
        return None
    try:
        parsed = json.loads(decoded.decode("utf-8"))
    except Exception:
        try:
            parsed = msgpack.unpackb(decoded, raw=False, strict_map_key=False)
        except Exception:
            return None
    normalized = normalize_messagepack(parsed)
    return normalized if isinstance(normalized, dict) else None


def walk_scalars(value: Any, path: str = "event") -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []

    def walk(item: Any, current_path: str, depth: int = 0) -> None:
        if depth > 16:
            return
        if isinstance(item, dict):
            for key, nested in item.items():
                walk(nested, f"{current_path}.{key}", depth + 1)
        elif isinstance(item, list):
            for index, nested in enumerate(item[:50]):
                walk(nested, f"{current_path}[{index}]", depth + 1)
        elif item is not None:
            text = str(item)
            result.append((current_path, text))
            stripped = text.strip()
            if len(stripped) <= 200_000 and stripped.startswith(("{", "[")):
                try:
                    parsed = json.loads(stripped)
                except (json.JSONDecodeError, TypeError):
                    return
                walk(parsed, f"{current_path}.json", depth + 1)

    walk(value, path)
    return result


def is_paid_event(event: dict[str, Any]) -> bool:
    return any(marker in text for _, text in walk_scalars(event) for marker in PAID_MARKERS)


def extract_group_card_title(event: dict[str, Any]) -> str:
    expected_titles = GROUP_WAITING_TITLES | GROUP_READY_TITLES

    def find_title(value: Any) -> str:
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() == "title" and str(nested).strip() in expected_titles:
                    return str(nested).strip()
                found = find_title(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value[:50]:
                found = find_title(nested)
                if found:
                    return found
        elif isinstance(value, str):
            text = value.strip()
            if text in expected_titles:
                return text
            if text.startswith(("{", "[")):
                try:
                    return find_title(json.loads(text))
                except (json.JSONDecodeError, TypeError):
                    return ""
        return ""

    return find_title(event)


def is_system_card_event(event: dict[str, Any]) -> bool:
    message = event.get("1")
    if not isinstance(message, dict):
        return False
    direction = str(message.get("7", "")).strip()
    content_type = ""
    nested_six = message.get("6")
    if isinstance(nested_six, dict):
        nested_three = nested_six.get("3")
        if isinstance(nested_three, dict):
            content_type = str(nested_three.get("4", "")).strip()
    biz_tag = ""
    metadata = message.get("10")
    if isinstance(metadata, dict):
        biz_tag = str(metadata.get("bizTag", ""))
    return direction == "1" or content_type == "6" or any(
        marker in biz_tag for marker in ("SECURITY", "taskName", "taskId")
    )


def group_event_stage(event: dict[str, Any]) -> str:
    if not is_system_card_event(event):
        return ""
    title = extract_group_card_title(event)
    if title in GROUP_WAITING_TITLES:
        return "waiting"
    if title in GROUP_READY_TITLES:
        return "ready"
    return ""


def extract_plain_chat_message(
    event: dict[str, Any], seller_id: str
) -> dict[str, str] | None:
    """Extract one ordinary buyer/seller text message from an IM sync event."""
    message = event.get("1")
    if not isinstance(message, dict) or is_system_card_event(event):
        return None
    metadata = message.get("10")
    if not isinstance(metadata, dict):
        return None
    content = str(metadata.get("reminderContent") or "").strip()
    sender_id = str(metadata.get("senderUserId") or "").strip()
    if not content or not sender_id:
        return None
    if is_paid_event(event) or content.startswith("[") and content.endswith("]"):
        return None
    nested = message.get("6")
    if isinstance(nested, dict):
        payload = nested.get("3")
        if isinstance(payload, dict):
            content_type = str(payload.get("4") or "").strip()
            if content_type and content_type != "1":
                return None

    chat_id = extract_chat_id(event)
    item_id = extract_item_id(event)
    if not chat_id or not item_id:
        return None
    return {
        "chat_id": chat_id,
        "item_id": item_id,
        "sender_id": sender_id,
        "direction": "outbound" if sender_id == seller_id else "inbound",
        "content": content[:2000],
    }


def _value_for_keys(event: dict[str, Any], keys: set[str]) -> str:
    for path, value in walk_scalars(event):
        if path.rsplit(".", 1)[-1].lower() in keys and value.strip():
            return value.strip()
    return ""


def extract_order_id(event: dict[str, Any]) -> str:
    direct = _value_for_keys(event, {"orderid", "bizorderid", "order_id", "tradeid", "trade_id"})
    if re.fullmatch(r"\d{10,}", direct):
        return direct
    patterns = (
        r"orderId(?:=|:|%3[Dd]|\\u003[dD])\s*[\"']?(\d{10,})",
        r"bizOrderId[\"']?\s*[:=]\s*[\"']?(\d{10,})",
        r"order[_-]?id[\"']?\s*[:=]\s*[\"']?(\d{10,})",
        r"order-detail\?(?:[^\s#]*?&)?orderId=(\d{10,})",
    )
    for path, text in walk_scalars(event):
        candidates = (text, unquote(text)) if "%" in text else (text,)
        for candidate in candidates:
            for pattern in patterns:
                match = re.search(pattern, candidate, flags=re.IGNORECASE)
                if match:
                    return match.group(1)
            candidate_lower = candidate.lower()
            if (
                "updatekey" in path.lower()
                or "updatekey" in candidate_lower
                or ("trade_" in candidate_lower and ":" in candidate)
                or ("buyer_confirm" in candidate_lower and ":" in candidate)
            ):
                long_ids = re.findall(r"\d{16,}", candidate)
                if long_ids:
                    return long_ids[0]
    return ""


def extract_item_id(event: dict[str, Any]) -> str:
    direct = _value_for_keys(event, {"itemid", "item_id", "auctionid", "auction_id"})
    if re.fullmatch(r"\d{8,}", direct):
        return direct
    for _, text in walk_scalars(event):
        for pattern in (r"[?&](?:itemId|item_id|id)=(\d{8,})", r"/item\?id=(\d{8,})"):
            match = re.search(pattern, text)
            if match:
                return match.group(1)
    return ""


def extract_buyer_id(event: dict[str, Any], seller_id: str) -> str:
    for key in ("buyerid", "buyer_id", "senderuserid", "sender_user_id", "userid", "user_id"):
        candidate = _value_for_keys(event, {key})
        if candidate and candidate != seller_id and re.fullmatch(r"\d{5,}", candidate):
            return candidate
    return ""


def extract_chat_id(event: dict[str, Any]) -> str:
    message_one = event.get("1")
    candidates: list[Any] = []
    if isinstance(message_one, str):
        candidates.extend([message_one, event.get("2")])
    elif isinstance(message_one, dict):
        candidates.extend([message_one.get("2"), message_one.get("sid")])
    candidates.extend([event.get("sid"), _value_for_keys(event, {"sid", "conversationid", "cid"})])
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if "@goofish" in normalized:
            normalized = normalized.split("@", 1)[0]
        if re.fullmatch(r"\d{5,}", normalized):
            return normalized
    return ""


def extract_event_timestamp_ms(event: dict[str, Any]) -> int | None:
    values: list[int] = []
    time_keys = {"createtime", "create_time", "timestamp", "msgtime", "msg_time", "time", "5"}
    for path, value in walk_scalars(event):
        if path.rsplit(".", 1)[-1].lower() not in time_keys or not value.isdigit():
            continue
        number = int(value)
        while number > 10_000_000_000_000:
            number //= 1000
        if 1_500_000_000_000 <= number <= 4_000_000_000_000:
            values.append(number)
        elif 1_500_000_000 <= number <= 4_000_000_000:
            values.append(number * 1000)
    return max(values) if values else None


def is_recent_event(event: dict[str, Any], *, max_age_seconds: int = 600) -> bool:
    timestamp_ms = extract_event_timestamp_ms(event)
    if timestamp_ms is None:
        return True
    age_ms = int(time.time() * 1000) - timestamp_ms
    return -60_000 <= age_ms <= max_age_seconds * 1000


def parse_market_timestamp(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        while number > 10_000_000_000:
            number //= 1000
        if 1_500_000_000 <= number <= 4_000_000_000:
            return float(number)
    normalized = text.replace("T", " ").replace("Z", "+00:00")
    for parser in (
        lambda: datetime.fromisoformat(normalized),
        lambda: datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S"),
        lambda: datetime.strptime(normalized, "%Y/%m/%d %H:%M:%S"),
    ):
        try:
            parsed = parser()
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
            return parsed.timestamp()
        except ValueError:
            continue
    return None


def is_recoverable_order(order: dict[str, object], *, now: float | None = None) -> bool:
    if order.get("order_status") not in {"待发货", "pending_ship"}:
        return False
    timestamp = parse_market_timestamp(order.get("paid_time") or order.get("create_time"))
    if timestamp is None:
        return False
    age = (time.time() if now is None else now) - timestamp
    return -300 <= age <= RECOVERY_WINDOW_SECONDS


def listing_item_id(listing_url: str) -> str:
    return parse_listing_id(listing_url)


def normalize_listing_match_text(value: object) -> str:
    return "".join(character.lower() for character in str(value or "") if character.isalnum())


def listing_price_cents(value: object) -> int | None:
    match = re.search(r"[¥￥]\s*(\d+(?:\.\d{1,2})?)", str(value or ""))
    if match is None:
        return None
    try:
        return round(float(match.group(1)) * 100)
    except ValueError:
        return None


def listing_title_from_text(value: object) -> str:
    raw_lines = [" ".join(line.split()) for line in str(value or "").splitlines()]
    for line in raw_lines:
        cleaned = re.sub(r"[¥￥]\s*\d+(?:\.\d{1,2})?", " ", line)
        cleaned = re.sub(r"\d+\s*人想要", " ", cleaned)
        cleaned = " ".join(cleaned.split()).strip(" ·|｜-")
        if len(normalize_listing_match_text(cleaned)) >= 4:
            return cleaned[:160]
    return " ".join(str(value or "").split())[:160]


def build_live_listing_snapshot(
    raw_items: list[dict[str, object]],
    products: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Normalize a profile-page scrape and apply only unique product matches."""
    candidates = {}
    for product in products:
        identity = listing_item_id(str(product.get("listing_url") or ""))
        if identity:
            candidates.setdefault(identity, []).append(str(product.get("dir_name") or ""))
    explicit_matches = {key: values[0] for key, values in candidates.items() if len(values) == 1}
    result: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for raw in raw_items:
        raw_url = str(raw.get("url") or "").strip()
        parsed = urlparse(raw_url)
        item_id = listing_item_id(raw_url)
        if (
            parsed.netloc not in {"www.goofish.com", "goofish.com"}
            or not item_id.isdigit()
            or len(item_id) < 8
            or item_id in seen_ids
        ):
            continue
        seen_ids.add(item_id)
        text = str(raw.get("text") or "").strip()[:1000]
        title = str(raw.get("title") or "").strip()[:160] or listing_title_from_text(text)
        matched = explicit_matches.get(item_id)
        result.append(
            {
                "item_id": item_id,
                "title": title or f"闲鱼商品 {item_id}",
                "url": f"https://www.goofish.com/item?id={item_id}",
                "image": str(raw.get("image") or "").strip()[:1000],
                "price_cents": listing_price_cents(text),
                "text": text,
                "matched_product_dir_name": matched,
            }
        )
    return result


def inventory_cards_to_raw_items(cards: object) -> list[dict[str, object]]:
    """Project the first-party profile API card shape into bounded listing facts."""
    result: list[dict[str, object]] = []
    for card in cards if isinstance(cards, list) else []:
        if not isinstance(card, dict):
            continue
        data = card.get("cardData", {})
        if not isinstance(data, dict) or str(data.get("itemStatus")) not in {"0", "0.0"}:
            continue
        item_id = str(data.get("id") or "").strip()
        title = " ".join(str(data.get("title") or "").split())[:160]
        if not item_id.isdigit() or len(item_id) < 8 or not title:
            continue
        price_info = data.get("priceInfo", {})
        pic_info = data.get("picInfo", {})
        price = str(price_info.get("price") or "").strip() if isinstance(price_info, dict) else ""
        image = str(pic_info.get("picUrl") or "").strip() if isinstance(pic_info, dict) else ""
        text = title if not price else f"{title}\n¥{price}"
        result.append(
            {
                "url": f"https://www.goofish.com/item?id={item_id}",
                "title": title,
                "text": text,
                "image": image,
            }
        )
    return result


class DeliveryService:
    def __init__(
        self,
        profiles_dir: Path,
        browser_executable: Path | None,
        database: Database,
        secret_store: SecretStore,
        reply_client: SiliconFlowReplyClient | None = None,
        notifier: WindowsNotifier | None = None,
        session_manager: Any | None = None,
        runtime_policy: RuntimePolicy = PROCESS_POLICY,
    ) -> None:
        self.runtime_policy = runtime_policy
        self.profiles_dir = profiles_dir
        self.browser_executable = browser_executable
        self._runtime_user_agent = USER_AGENT
        self._runtime_sec_ch_ua_platform = browser_platform_header()
        self.database = database
        self.secret_store = secret_store
        self.reply_client = reply_client or SiliconFlowReplyClient(runtime_policy=runtime_policy)
        self.notifier = notifier or WindowsNotifier()
        self.session_manager = session_manager
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._outbound_lock = asyncio.Lock()
        self._listing_sync_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._event_tasks: set[asyncio.Task[None]] = set()
        self._reply_tasks: dict[str, PendingAutoReply] = {}
        self._stop_event = asyncio.Event()
        self._pending_acks: dict[str, asyncio.Future[Any]] = {}
        self._runtime_cookie_map: dict[str, str] | None = None
        self._runtime_request_context: Any | None = None
        self._runtime_websocket: Any | None = None
        self._runtime_seller_id: str = ""
        self._session_handoff: dict[str, object] | None = None
        self._status = "disabled"
        self._account_id: int | None = None
        self._last_error = ""
        self._last_event_at: str | None = None
        self._last_chat_at: str | None = None
        self._last_delivery_at: str | None = None
        self._last_recovery_at: str | None = None
        self._recovered_order_count = 0
        self._last_auto_reply_at: str | None = None
        self._last_auto_reply_error = ""

    @business_operation
    def _notify(self, account_id: int, title: str, message: str) -> None:
        safety = self.database.get_automation_safety(account_id)
        if safety.get("notifications_enabled"):
            self.notifier.notify(title, message)

    @business_operation
    def _open_safety_circuit(self, account_id: int, reason: str) -> None:
        status = self.database.open_automation_circuit(account_id, reason)
        circuit = status["circuit"]
        self._status = "cooldown"
        self._last_error = reason[:300]
        retry_minutes = max(1, (int(circuit["retry_after_seconds"]) + 59) // 60)
        self._notify(
            account_id,
            "闲鱼自动化已安全暂停",
            f"{reason}；约 {retry_minutes} 分钟后自动尝试恢复。",
        )

    @business_operation
    async def _outbound_preflight(self, account_id: int, kind: str) -> dict[str, object]:
        decision = self.database.check_automation_outbound(account_id, kind)
        if not decision["allowed"] and decision.get("code") == "interval":
            await asyncio.sleep(max(0, int(decision["retry_after_seconds"])) + 0.25)
            decision = self.database.check_automation_outbound(account_id, kind)
        return decision

    def snapshot(self) -> dict[str, object]:
        running = self._task is not None and not self._task.done()
        return {
            "status": self._status,
            "running": running,
            "connected": self._status == "listening",
            "account_id": self._account_id,
            "last_error": self._last_error,
            "last_event_at": self._last_event_at,
            "last_chat_at": self._last_chat_at,
            "last_delivery_at": self._last_delivery_at,
            "last_recovery_at": self._last_recovery_at,
            "recovered_order_count": self._recovered_order_count,
            "last_auto_reply_at": self._last_auto_reply_at,
            "last_auto_reply_error": self._last_auto_reply_error,
            "message_auth_diagnostics": list(getattr(self, '_message_auth_diagnostics', [])),
            "orders": self.database.list_orders(self._account_id) if self._account_id else [],
        }

    @business_operation
    async def preview_recent_orders(self) -> dict[str, object]:
        """Read the seller order list without sending messages or confirming delivery."""
        cookie_map = self._runtime_cookie_map
        if self._status != "listening" or cookie_map is None:
            raise RuntimeError("自动发货监听尚未连接，暂时不能读取订单列表")
        return await self._fetch_recent_sold_orders(cookie_map)

    @business_operation
    async def refresh_live_listings(self) -> dict[str, object]:
        """Read current listings through the first-party profile API."""
        async with self._listing_sync_lock:
            cookie_map = self._runtime_cookie_map
            account_id = self._account_id
            seller_id = self._runtime_seller_id
            if (
                self._status != "listening"
                or cookie_map is None
                or account_id is None
                or not seller_id
            ):
                raise RuntimeError("闲鱼消息监听尚未连接，暂时不能读取最新在售商品")
            raw_items = await self._fetch_live_inventory(cookie_map, seller_id)
            products = self.database.list_products(account_id, include_listing_only=True)
            snapshot = build_live_listing_snapshot(raw_items, products)
            if not snapshot:
                raise RuntimeError("闲鱼接口没有返回在售商品，已保留原有商品数据")
            before_ids = {
                str(item.get("item_id") or "")
                for item in self.database.list_live_listings(account_id)
            }
            self.database.sync_live_listings(account_id, snapshot)
            listings = self.database.reconcile_configured_listings(account_id)
            remote_ids = {str(item.get("item_id") or "") for item in snapshot}
            return {
                "remote_count": len(snapshot),
                "new_count": len(remote_ids - before_ids),
                "matched_count": sum(
                    bool(item.get("matched_product_dir_name")) for item in snapshot
                ),
                "synced_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "listings": listings,
            }

    @business_operation
    async def reconcile_order(self, order_id: str) -> dict[str, object]:
        """Recover one explicitly selected paid order from the seller order list."""
        if self.runtime_policy.mvp_fulfillment:
            raise ValueError('MVP_HISTORICAL_RECONCILIATION_FORBIDDEN')
        self.runtime_policy.require_fulfillment()
        normalized_order_id = str(order_id or "").strip()
        cookie_map = self._runtime_cookie_map
        websocket = self._runtime_websocket
        seller_id = self._runtime_seller_id
        account_id = self._account_id
        if not re.fullmatch(r"\d{10,}", normalized_order_id):
            raise ValueError("订单号格式不正确")
        if (
            self._status != "listening"
            or cookie_map is None
            or websocket is None
            or not seller_id
            or account_id is None
        ):
            raise RuntimeError("自动发货监听尚未连接，暂时不能补偿发货")

        existing = self.database.get_order(normalized_order_id)
        if existing and existing.get("delivery_status") == "confirm_pending":
            await self.retry_platform_confirmation(normalized_order_id, account_id, cookie_map)
            return {"order": self.database.get_order(normalized_order_id), "delivery": self.snapshot()}
        preview = await self._fetch_recent_sold_orders(cookie_map)
        candidates = [
            item
            for item in preview.get("orders", [])
            if isinstance(item, dict) and item.get("order_id") == normalized_order_id
        ]
        if len(candidates) != 1:
            raise ValueError("卖家订单列表中没有唯一匹配的指定订单")
        candidate = candidates[0]
        if candidate.get("order_status") not in {"待发货", "pending_ship"}:
            raise ValueError(f"订单当前不是待发货状态：{candidate.get('order_status') or '未知'}")
        item_id = str(candidate.get("item_id") or "").strip()
        buyer_id = str(candidate.get("buyer_id") or "").strip()
        if not re.fullmatch(r"\d{8,}", item_id) or not re.fullmatch(r"\d{5,}", buyer_id):
            raise ValueError("订单列表缺少有效的商品 ID 或买家 ID")

        product = self.database.get_product_by_listing_item_id(item_id, account_id)
        if product is None:
            raise ValueError("订单商品没有唯一匹配到当前账号的已上架商品")
        if delivery_issues(product):
            raise ValueError("匹配商品的网盘链接尚未通过验证")

        chat_id = await self._create_chat(websocket, buyer_id, seller_id, item_id)
        event = {
            "orderId": normalized_order_id,
            "itemId": item_id,
            "buyerId": buyer_id,
            "sid": chat_id,
            "source": "seller_order_reconciliation",
        }
        await self._process_paid_event(
            websocket, account_id, seller_id, cookie_map, event
        )
        order = self.database.get_order(normalized_order_id)
        return {"order": order, "delivery": self.snapshot()}

    @business_operation
    async def retry_platform_confirmation(self, order_id, account_id, cookie_map):
        if self.runtime_policy.mvp_fulfillment:
            raise ValueError('MVP_HISTORICAL_CONFIRMATION_FORBIDDEN')
        self.runtime_policy.require_fulfillment()
        account = self.database.get_account(account_id)
        order = self.database.get_order(order_id)
        if (self._account_id != account_id or not account or not account.get("is_active")
                or not account.get("delivery_enabled") or account.get("binding_status") != "bound"
                or not order or order.get("account_id") != account_id
                or order.get("delivery_status") != "confirm_pending" or not order.get("message_sent_at")):
            raise ValueError("CONFIRMATION_NOT_ALLOWED")
        safety = await self._outbound_preflight(account_id, "delivery")
        if not safety["allowed"]:
            raise ValueError("CONFIRMATION_SAFETY_BLOCKED")
        await self._confirm_platform_delivery(order_id, cookie_map)
        self.database.mark_order_delivered(order_id, platform_status="confirmed")

    @business_operation
    async def start_if_enabled(self) -> dict[str, object]:
        account = (self.database.get_account(self.runtime_policy.account_id)
                   if self.runtime_policy.account_id is not None else self.database.get_active_account())
        auto_reply = self.database.get_auto_reply_settings(int(account["id"]))
        account_id = int(account["id"])
        automation_enabled = bool((self.runtime_policy.fulfillment_enabled and account.get("delivery_enabled")) or auto_reply.get("enabled"))
        if automation_enabled:
            if account.get("binding_status") != "bound":
                self._status = "verification_required"
                self._last_error = "已保留消息自动化设置，但闲鱼登录需要重新验证"
            else:
                try:
                    if self.runtime_policy.fulfillment_enabled and account.get("delivery_enabled"):
                        await self.start(int(account["id"]), persist=False)
                    else:
                        await self.start_auto_reply(int(account["id"]))
                except Exception as exc:
                    self._status = "error"
                    self._last_error = f"开机恢复消息监听失败：{exc}"[:300]
        snapshot = self.snapshot()
        report = {
            "status": str(snapshot.get("status") or "disabled"),
            "automation_enabled": automation_enabled,
            "delivery_enabled": bool(account.get("delivery_enabled")),
            "auto_reply_enabled": bool(auto_reply.get("enabled")),
            "binding_status": str(account.get("binding_status") or "unbound"),
            "message": str(snapshot.get("last_error") or "启动检查已完成"),
        }
        self.database.record_startup_report(account_id, report)
        if automation_enabled:
            if snapshot.get("status") == "listening":
                self._notify(account_id, "闲鱼管理系统已就绪", "登录会话有效，监听已自动恢复。")
            elif snapshot.get("status") == "verification_required":
                self._notify(account_id, "闲鱼登录需要验证", self._last_error)
            elif snapshot.get("status") == "error":
                self._notify(account_id, "闲鱼管理系统启动异常", self._last_error)
        return snapshot

    @business_operation
    async def start_auto_reply(
        self,
        account_id: int,
        *,
        session_handoff: dict[str, object] | None = None,
    ) -> dict[str, object]:
        async with self._lock:
            account = self.database.get_account(account_id)
            if not self.runtime_policy.reply_account_selected(account, account_id):
                raise ValueError("只能启动当前账号的自动回复")
            if account.get("binding_status") != "bound":
                raise ValueError("闲鱼账号尚未绑定或登录已失效")
            if self.browser_executable is None or not self.browser_executable.is_file():
                raise RuntimeError("没有找到 Chrome 或 Edge")
            reply_settings = self.database.get_auto_reply_settings(account_id)
            if not reply_settings.get("enabled"):
                raise ValueError("自动回复尚未启用")
            if not self.secret_store.has_secret():
                raise ValueError("尚未保存硅基流动 API Key")
            if self._task is not None and not self._task.done():
                return self.snapshot()
            self._stop_event = asyncio.Event()
            self._account_id = account_id
            if session_handoff is not None:
                self._session_handoff = session_handoff
            self._status = "starting"
            self._last_error = ""
            self._task = asyncio.create_task(self._run(account_id))
        return self.snapshot()

    @business_operation
    async def sync_auto_reply_runtime(self, account_id: int) -> dict[str, object]:
        reply_settings = self.database.get_auto_reply_settings(account_id)
        if reply_settings.get("enabled"):
            return await self.start_auto_reply(account_id)
        account = self.database.get_account(account_id)
        if self.runtime_policy.fulfillment_enabled and account and account.get("delivery_enabled"):
            return self.snapshot()
        return await self.stop(persist=False)

    @business_operation
    async def probe(self, account_id: int) -> dict[str, object]:
        async with self._lock:
            if self._task is not None and not self._task.done():
                return {**self.snapshot(), "probe_ok": self._status == "listening"}
            account = self.database.get_account(account_id)
            if account is None or not account["is_active"]:
                raise ValueError("只能检查当前账号的自动发货连接")
            if account.get("binding_status") != "bound":
                raise ValueError("闲鱼账号尚未绑定或登录已失效")
            self._status = "authenticating"
            self._account_id = account_id
            playwright: Any | None = None
            context: Any | None = None
            close_context = True
            try:
                (
                    playwright,
                    context,
                    seller_id,
                    device_id,
                    token,
                    cookie_map,
                    close_context,
                ) = await self._open_authenticated_profile(account_id)
                self._status = "connecting"
                headers = {
                    "Cookie": self._cookie_header(cookie_map),
                    "Origin": GOOFISH_HOME.rstrip("/"),
                    "User-Agent": USER_AGENT,
                    "Pragma": "no-cache",
                    "Cache-Control": "no-cache",
                }
                async with websockets.connect(
                    WEBSOCKET_URL,
                    extra_headers=headers,
                    open_timeout=30,
                    close_timeout=10,
                    ping_interval=None,
                    max_size=4 * 1024 * 1024,
                ) as websocket:
                    await self._register(websocket, token, device_id)
                    await asyncio.wait_for(websocket.recv(), timeout=10)
                self._status = "disabled"
                self._last_error = ""
                self.database.update_account_binding(account_id, "bound")
                return {**self.snapshot(), "probe_ok": True}
            except SessionVerificationRequired as exc:
                self._status = "verification_required"
                self._last_error = str(exc)[:300]
                self.database.update_account_binding(account_id, "expired", error=self._last_error)
                raise RuntimeError(self._last_error) from exc
            except Exception as exc:
                self._status = "error"
                self._last_error = f"连接自检失败：{exc}"[:300]
                raise RuntimeError(self._last_error) from exc
            finally:
                if context is not None and close_context:
                    await context.close()
                if playwright is not None:
                    await playwright.stop()
                self._account_id = None

    @business_operation
    async def start(
        self,
        account_id: int,
        *,
        auto_confirm: bool | None = None,
        auto_free_group: bool | None = None,
        initial_delay_seconds: int = 0,
        persist: bool = True,
        session_handoff: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.runtime_policy.require_fulfillment()
        async with self._lock:
            account = self.database.get_account(account_id)
            if not self.runtime_policy.reply_account_selected(account, account_id):
                raise ValueError("只能启动当前账号的自动发货")
            if account.get("binding_status") != "bound":
                raise ValueError("闲鱼账号尚未绑定或登录已失效")
            if self.browser_executable is None or not self.browser_executable.is_file():
                raise RuntimeError("没有找到 Chrome 或 Edge")
            ready_products = [
                product
                for product in self.database.list_products(account_id)
                if product["enabled_for_account"]
                and product["listing_status"] == "published"
                and not delivery_issues(product)
                and listing_item_id(str(product["listing_url"]))
                and (not self.runtime_policy.mvp_fulfillment or listing_item_id(str(product['listing_url'])) in self.runtime_policy.fulfillment_items)
            ]
            if not ready_products:
                raise ValueError("没有同时满足已上架、已验证网盘链接条件的商品")
            confirm_setting = (
                bool(account.get("auto_confirm_delivery")) if auto_confirm is None else auto_confirm
            )
            free_group_setting = (
                bool(account.get("auto_free_group"))
                if auto_free_group is None
                else auto_free_group
            )
            if persist:
                self.database.update_delivery_settings(
                    account_id,
                    enabled=True,
                    auto_confirm=confirm_setting,
                    auto_free_group=free_group_setting,
                )
            if self._task is not None and not self._task.done():
                return self.snapshot()
            self._stop_event = asyncio.Event()
            self._account_id = account_id
            if session_handoff is not None:
                self._session_handoff = session_handoff
            self._status = "starting"
            self._last_error = ""
            self._task = asyncio.create_task(self._run(account_id, initial_delay_seconds))
        return self.snapshot()

    async def stop(self, *, persist: bool = True) -> dict[str, object]:
        if persist:
            self.runtime_policy.require_business()
        async with self._lock:
            account_id = self._account_id
            if persist and account_id is not None:
                account = self.database.get_account(account_id)
                if account is not None:
                    self.database.update_delivery_settings(
                        account_id,
                        enabled=False,
                        auto_confirm=bool(account.get("auto_confirm_delivery")),
                        auto_free_group=bool(account.get("auto_free_group")),
                    )
                auto_reply = self.database.get_auto_reply_settings(account_id)
                if auto_reply.get("enabled") and self._task is not None and not self._task.done():
                    return self.snapshot()
            self._stop_event.set()
            task = self._task
            self._task = None
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for event_task in list(self._event_tasks):
                event_task.cancel()
            self._event_tasks.clear()
            for pending_reply in list(self._reply_tasks.values()):
                pending_reply.task.cancel()
            self._reply_tasks.clear()
            for future in self._pending_acks.values():
                if not future.done():
                    future.cancel()
            self._pending_acks.clear()
            self._status = "disabled"
            self._account_id = None
        return self.snapshot()

    async def shutdown(self) -> None:
        await self.stop(persist=False)

    @business_operation
    async def _run(self, account_id: int, initial_delay_seconds: int = 0) -> None:
        retry_delay = 3
        if initial_delay_seconds > 0:
            self._status = "cooldown"
            self._last_error = f"闲鱼风控冷却中，约 {initial_delay_seconds // 60} 分钟后自动重试"
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=initial_delay_seconds
                )
                return
            except asyncio.TimeoutError:
                pass
        while not self._stop_event.is_set():
            playwright: Any | None = None
            context: Any | None = None
            close_context = True
            control = self.database.get_automation_status(account_id)
            circuit = control["circuit"]
            if circuit["is_open"]:
                retry_seconds = max(1, int(circuit["retry_after_seconds"] or 1))
                self._status = "cooldown"
                self._last_error = str(circuit["reason"] or "安全熔断冷却中")[:300]
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=retry_seconds)
                    return
                except asyncio.TimeoutError:
                    continue
            try:
                self._status = "authenticating"
                (
                    playwright,
                    context,
                    seller_id,
                    device_id,
                    token,
                    cookie_map,
                    close_context,
                ) = await self._open_authenticated_profile(account_id)
                self._status = "connecting"
                headers = {
                    "Cookie": self._cookie_header(cookie_map),
                    "Origin": GOOFISH_HOME.rstrip("/"),
                    "User-Agent": USER_AGENT,
                    "Pragma": "no-cache",
                    "Cache-Control": "no-cache",
                }
                async with websockets.connect(
                    WEBSOCKET_URL,
                    extra_headers=headers,
                    open_timeout=30,
                    close_timeout=10,
                    ping_interval=None,
                    max_size=4 * 1024 * 1024,
                ) as websocket:
                    await self._register(websocket, token, device_id)
                    self._runtime_cookie_map = cookie_map
                    self._runtime_request_context = context.request
                    self._runtime_websocket = websocket
                    self._runtime_seller_id = seller_id
                    self._status = "listening"
                    self._reply_listen_started_ms = int(time.time() * 1000)
                    self._last_error = ""
                    self.database.update_account_binding(account_id, "bound")
                    account = self.database.get_account(account_id) or {}
                    auto_reply = self.database.get_auto_reply_settings(account_id)
                    self.database.record_startup_report(
                        account_id,
                        {
                            "status": "listening",
                            "automation_enabled": True,
                            "delivery_enabled": bool(account.get("delivery_enabled")),
                            "auto_reply_enabled": bool(auto_reply.get("enabled")),
                            "binding_status": "bound",
                            "message": "登录会话有效，消息监听已自动恢复",
                        },
                    )
                    self._notify(
                        account_id,
                        "闲鱼管理系统已就绪",
                        "登录会话有效，监听已自动恢复。",
                    )
                    retry_delay = 3
                    self._schedule_order_recovery(websocket, account_id, seller_id, cookie_map)
                    await self._listen(websocket, account_id, seller_id, cookie_map)
            except asyncio.CancelledError:
                raise
            except RiskControlError as exc:
                self._open_safety_circuit(account_id, str(exc)[:300])
                circuit = self.database.get_automation_status(account_id)["circuit"]
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=max(1, int(circuit["retry_after_seconds"] or 1)),
                    )
                    return
                except asyncio.TimeoutError:
                    continue
            except SessionVerificationRequired as exc:
                self._status = "verification_required"
                self._last_error = str(exc)[:300]
                self._session_handoff = None
                self.database.update_account_binding(account_id, "expired", error=self._last_error)
                self._notify(account_id, "闲鱼登录需要验证", self._last_error)
                return
            except ValueError as exc:
                self._status = "error"
                self._last_error = str(exc)[:300]
                self.database.update_account_binding(account_id, "expired", error=self._last_error)
                self._notify(account_id, "闲鱼自动化需要处理", self._last_error)
                return
            except Exception as exc:
                self._status = "reconnecting"
                self._last_error = f"监听连接异常：{exc}"[:300]
                if self._stop_event.is_set():
                    return
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=retry_delay)
                except asyncio.TimeoutError:
                    retry_delay = min(retry_delay * 2, 30)
            finally:
                self._runtime_cookie_map = None
                self._runtime_request_context = None
                self._runtime_websocket = None
                self._runtime_seller_id = ""
                if context is not None and close_context:
                    try:
                        await context.close()
                    except Exception:
                        pass
                if playwright is not None:
                    try:
                        await playwright.stop()
                    except Exception:
                        pass

    @business_operation
    async def _open_authenticated_profile(
        self, account_id: int
    ) -> tuple[Any | None, Any, str, str, str, dict[str, str], bool]:
        """Reuse the visible session window, falling back to the legacy headless profile."""
        from playwright.async_api import async_playwright

        if self.browser_executable is None or not self.browser_executable.is_file():
            raise RuntimeError("没有找到 Chrome 或 Edge")
        playwright = None
        context = None
        close_context = True
        try:
            if self.session_manager is not None:
                context = await self.session_manager.ensure_runtime_context(account_id)
                close_context = False
            else:
                playwright = await async_playwright().start()
                context = await playwright.chromium.launch_persistent_context(
                    **sandbox_options(),
                    user_data_dir=str(
                        self.profiles_dir
                        / self.browser_executable.stem.lower()
                        / f"account-{account_id}"
                    ),
                    executable_path=str(self.browser_executable),
                    headless=True,
                    no_viewport=True,
                    args=[
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                )
            if self._session_handoff:
                session_cookies = self._session_handoff.get("cookies", [])
                if isinstance(session_cookies, list) and session_cookies:
                    await context.add_cookies(session_cookies)
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                detected_user_agent = await page.evaluate("navigator.userAgent")
                if isinstance(detected_user_agent, str) and detected_user_agent.strip():
                    self._runtime_user_agent = detected_user_agent.strip()
            except Exception:
                self._runtime_user_agent = USER_AGENT
            try:
                platform_hint = await page.evaluate(
                    "navigator.userAgentData?.platform || navigator.platform || ''"
                )
                self._runtime_sec_ch_ua_platform = browser_platform_header(
                    str(platform_hint or "")
                )
            except Exception:
                self._runtime_sec_ch_ua_platform = browser_platform_header()
            cookies = await context.cookies(
                [GOOFISH_HOME, "https://passport.goofish.com/", TOKEN_URL]
            )
            cookie_map = {
                str(cookie.get("name", "")): str(cookie.get("value", ""))
                for cookie in cookies
                if cookie.get("name")
            }
            seller_id = cookie_map.get("unb", "").strip()
            if not seller_id or not any(cookie_map.get(name) for name in SESSION_COOKIE_NAMES):
                raise SessionVerificationRequired(
                    "闲鱼登录会话缺少必要标记，请重新登录并完成验证"
                )
            device_id = f"{uuid.uuid4()}-{seller_id}"
            token, cookie_map = await self._fetch_im_token(
                cookie_map, device_id, request_context=context.request
            )
            self._session_handoff = await context.storage_state()
            return (
                playwright,
                context,
                seller_id,
                device_id,
                token,
                cookie_map,
                close_context,
            )
        except Exception:
            if context is not None and close_context:
                try:
                    await context.close()
                except Exception:
                    pass
            if playwright is not None:
                await playwright.stop()
            raise

    @business_operation
    async def _read_profile_cookies(self, account_id: int) -> dict[str, str]:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        context = None
        try:
            context = await playwright.chromium.launch_persistent_context(
                **sandbox_options(),
                user_data_dir=str(
                    self.profiles_dir
                    / (self.browser_executable.stem.lower() if self.browser_executable else "chromium")
                    / f"account-{account_id}"
                ),
                executable_path=str(self.browser_executable),
                headless=False,
                no_viewport=True,
                args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                detected_user_agent = await page.evaluate("navigator.userAgent")
                if isinstance(detected_user_agent, str) and detected_user_agent.strip():
                    self._runtime_user_agent = detected_user_agent.strip()
            except Exception:
                self._runtime_user_agent = USER_AGENT
            # The account owner completes any login or challenge manually before
            # delivery starts. Do not navigate an automation-controlled page here:
            # opening the challenge again can invalidate an otherwise valid session.
            cookies = await context.cookies([GOOFISH_HOME, "https://passport.goofish.com/"])
            return {
                str(cookie.get("name", "")): str(cookie.get("value", ""))
                for cookie in cookies
                if cookie.get("name")
            }
        finally:
            if context is not None:
                await context.close()
            await playwright.stop()

    @staticmethod
    def _cookie_header(cookie_map: dict[str, str]) -> str:
        return "; ".join(f"{key}={value}" for key, value in cookie_map.items() if key)

    @business_operation
    async def _fetch_im_token(
        self,
        cookie_map: dict[str, str],
        device_id: str,
        *,
        request_context: Any | None = None,
    ) -> tuple[str, dict[str, str]]:
        data_json = json.dumps(
            {"appKey": IM_APP_KEY, "deviceId": device_id},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        last_ret = ""
        for _ in range(2):
            timestamp = str(int(time.time() * 1000))
            token_seed = cookie_map.get("_m_h5_tk", "").split("_", 1)[0]
            params = {
                "jsv": "2.7.2",
                "appKey": MTOP_APP_KEY,
                "t": timestamp,
                "sign": generate_mtop_sign(timestamp, token_seed, data_json),
                "v": "1.0",
                "type": "originaljson",
                "accountSite": "xianyu",
                "dataType": "json",
                "timeout": "20000",
                "api": "mtop.taobao.idlemessage.pc.login.token",
                "sessionOption": "AutoLoginOnly",
                "dangerouslySetWindvaneParams": "%5Bobject%20Object%5D",
                "smToken": "token",
                "queryToken": "sm",
                "sm": "sm",
                "spm_cnt": "a21ybx.im.0.0",
            }
            major_match = re.search(r"(?:Chrome|Edg)/(\d+)", self._runtime_user_agent)
            browser_major = major_match.group(1) if major_match else "150"
            browser_brand = (
                "Microsoft Edge" if "Edg/" in self._runtime_user_agent else "Google Chrome"
            )
            headers = {
                "accept": "application/json",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                "cache-control": "no-cache",
                "content-type": "application/x-www-form-urlencoded",
                "origin": GOOFISH_HOME.rstrip("/"),
                "pragma": "no-cache",
                "priority": "u=1, i",
                "referer": GOOFISH_HOME,
                "sec-ch-ua": (
                    f'"Not_A Brand";v="99", "Chromium";v="{browser_major}", '
                    f'"{browser_brand}";v="{browser_major}"'
                ),
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": getattr(
                    self, "_runtime_sec_ch_ua_platform", browser_platform_header()
                ),
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
                "user-agent": self._runtime_user_agent,
                "cookie": self._cookie_header(cookie_map),
            }
            if request_context is not None:
                browser_headers = dict(headers)
                browser_headers.pop("cookie", None)
                response = await request_context.post(
                    TOKEN_URL,
                    params=params,
                    form={"data": data_json},
                    headers=browser_headers,
                    timeout=30_000,
                )
                payload = await response.json()
                storage = await request_context.storage_state()
                for cookie in storage.get("cookies", []):
                    if cookie.get("name"):
                        cookie_map[str(cookie["name"])] = str(cookie.get("value", ""))
            else:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    response = await client.post(
                        TOKEN_URL,
                        params=params,
                        data={"data": data_json},
                        headers=headers,
                    )
                    for cookie in response.cookies.jar:
                        cookie_map[cookie.name] = cookie.value
                payload = response.json()
            ret = payload.get("ret", []) if isinstance(payload, dict) else []
            from .message_auth_diagnostics import auth_evidence
            evidence = auth_evidence(
                getattr(response, 'status', getattr(response, 'status_code', None)),
                ret, device_id, request_context)
            self._message_auth_diagnostics = (getattr(self, '_message_auth_diagnostics', []) + [evidence])[-6:]
            last_ret = "; ".join(str(item) for item in ret)[:240]
            access_token = str(payload.get("data", {}).get("accessToken", "")) if isinstance(payload, dict) else ""
            if access_token and any("SUCCESS" in str(item) for item in ret):
                return access_token, cookie_map
        if "RGV587_ERROR" in last_ret or "FAIL_SYS_USER_VALIDATE" in last_ret:
            raise RiskControlError("闲鱼消息接口处于风控冷却，15 分钟后自动重试")
        if any(
            marker in last_ret
            for marker in (
                "FAIL_SYS_SESSION_EXPIRED",
                "FAIL_SYS_TOKEN_EXPIRED",
                "FAIL_SYS_ILLEGAL_ACCESS",
            )
        ):
            raise SessionVerificationRequired(
                "网页登录信息已保存，但消息会话已失效；请重新登录并完成滑块验证"
            )
        suffix = f"（平台返回：{last_ret}）" if last_ret else ""
        raise SessionVerificationRequired(
            f"获取闲鱼消息令牌失败，请重新登录或在闲鱼网页完成验证{suffix}"
        )

    @business_operation
    async def _register(self, websocket: Any, token: str, device_id: str) -> None:
        registration = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": IM_APP_KEY,
                "token": token,
                "ua": f"{self._runtime_user_agent} DingTalk(2.1.5) OS(Windows/10) Browser(Edge) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": device_id,
                "mid": generate_mid(),
            },
        }
        await self._send_json(websocket, registration)
        await asyncio.sleep(1)
        await self._send_json(
            websocket,
            {
                "lwp": "/r/SyncStatus/ackDiff",
                "headers": {"mid": generate_mid()},
                "body": [
                    {
                        "pipeline": "sync",
                        "tooLong2Tag": "PNM,1",
                        "channel": "sync",
                        "topic": "sync",
                        "highPts": 0,
                        "pts": int(time.time() * 1000) * 1000,
                        "seq": 0,
                        "timestamp": int(time.time() * 1000),
                    }
                ],
            },
        )

    @business_operation
    async def _listen(
        self,
        websocket: Any,
        account_id: int,
        seller_id: str,
        cookie_map: dict[str, str],
    ) -> None:
        while not self._stop_event.is_set():
            try:
                raw_message = await asyncio.wait_for(websocket.recv(), timeout=15)
            except asyncio.TimeoutError:
                await self._send_json(websocket, {"lwp": "/!", "headers": {"mid": generate_mid()}})
                continue
            frame = json.loads(raw_message)
            headers = frame.get("headers", {}) if isinstance(frame, dict) else {}
            mid = str(headers.get("mid", ""))
            if mid and mid in self._pending_acks:
                pending = self._pending_acks.pop(mid, None)
                if pending is not None and not pending.done():
                    if frame.get("code") == 200:
                        pending.set_result(frame)
                    else:
                        error_text = json.dumps(frame, ensure_ascii=False, default=str)[:500]
                        risk_markers = (
                            "RGV587",
                            "FAIL_SYS_USER_VALIDATE",
                            "CSI_FORBID",
                            "被挤下线",
                            "风控",
                            "验证",
                        )
                        if any(marker in error_text for marker in risk_markers):
                            pending.set_exception(
                                RiskControlError("平台拒绝了自动发送，已进入安全冷却")
                            )
                        else:
                            pending.set_exception(
                                RuntimeError(
                                    f"平台未确认自动发送（code={frame.get('code', 'unknown')}）"
                                )
                            )
            elif mid:
                ack_headers = {"mid": mid, "sid": headers.get("sid", "")}
                for key in ("app-key", "ua", "dt"):
                    if key in headers:
                        ack_headers[key] = headers[key]
                await self._send_json(websocket, {"code": 200, "headers": ack_headers})

            package = frame.get("body", {}).get("syncPushPackage", {}) if isinstance(frame, dict) else {}
            for item in package.get("data", []) if isinstance(package, dict) else []:
                if not isinstance(item, dict) or not item.get("data"):
                    continue
                event = decode_sync_payload(str(item["data"]))
                if event is None or not is_recent_event(event):
                    continue
                # Order identifiers for card messages may live beside the encoded
                # payload in the sync item (for example inside updateKey). Keep
                # that metadata in-memory so all extractors can inspect it.
                event = dict(event)
                event["_sync_meta"] = {
                    str(key): value for key, value in item.items() if key != "data"
                }
                group_stage = group_event_stage(event)
                if (group_stage in {'waiting', 'ready'} or is_paid_event(event)) and not self.runtime_policy.fulfillment_enabled:
                    continue
                if group_stage == "waiting":
                    task = asyncio.create_task(
                        self._process_group_waiting_event(
                            account_id, seller_id, cookie_map, event
                        )
                    )
                elif group_stage == "ready" or is_paid_event(event):
                    task = asyncio.create_task(
                        self._process_paid_event(
                            websocket, account_id, seller_id, cookie_map, event
                        )
                    )
                else:
                    if self.runtime_policy.reply_only or self.runtime_policy.mvp_fulfillment:
                        event_ms = extract_event_timestamp_ms(event)
                        if event_ms is None or event_ms <= getattr(self, '_reply_listen_started_ms', int(time.time() * 1000)):
                            continue
                    chat_message = extract_plain_chat_message(event, seller_id)
                    if chat_message is None:
                        continue
                    task = asyncio.create_task(
                        self._process_chat_event(
                            websocket, account_id, seller_id, chat_message, event
                        )
                    )
                self._event_tasks.add(task)
                task.add_done_callback(self._event_tasks.discard)

    def _schedule_order_recovery(self, websocket, account_id, seller_id, cookie_map):
        if not self.runtime_policy.order_recovery_enabled:
            return
        account = self.database.get_account(account_id) or {}
        if not account.get('delivery_enabled'):
            return
        self.runtime_policy.require_fulfillment()
        task = asyncio.create_task(self._recover_recent_paid_orders(websocket, account_id, seller_id, cookie_map))
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)

    @business_operation
    async def _recover_recent_paid_orders(
        self,
        websocket: Any,
        account_id: int,
        seller_id: str,
        cookie_map: dict[str, str],
    ) -> None:
        """Safely recover exact, recent paid orders that arrived while offline."""
        if not self.runtime_policy.order_recovery_enabled:
            return
        self.runtime_policy.require_fulfillment()
        recovered_count = 0
        try:
            preview = await self._fetch_recent_sold_orders(cookie_map, page_size=20)
        except Exception as exc:
            self.database.record_audit(
                "startup_order_recovery_failed",
                str(account_id),
                {"error": str(exc)[:200]},
            )
            self._last_error = f"监听已连接，但近期订单补偿检查失败：{exc}"[:300]
            return

        for candidate in preview.get("orders", []):
            if not isinstance(candidate, dict) or not is_recoverable_order(candidate):
                continue
            order_id = str(candidate.get("order_id") or "").strip()
            item_id = str(candidate.get("item_id") or "").strip()
            buyer_id = str(candidate.get("buyer_id") or "").strip()
            if not (
                re.fullmatch(r"\d{10,}", order_id)
                and re.fullmatch(r"\d{8,}", item_id)
                and re.fullmatch(r"\d{5,}", buyer_id)
            ):
                continue
            existing = self.database.get_order(order_id)
            if existing is not None and existing.get("delivery_status") not in {
                "pending",
                "failed",
            }:
                continue
            product = self.database.get_product_by_listing_item_id(item_id, account_id)
            if (
                product is None
                or delivery_issues(product)
            ):
                self.database.record_audit(
                    "startup_order_recovery_skipped",
                    order_id,
                    {"reason": "product_not_delivery_ready", "item_id": item_id},
                )
                continue
            try:
                chat_id = await self._create_chat(websocket, buyer_id, seller_id, item_id)
                await self._process_paid_event(
                    websocket,
                    account_id,
                    seller_id,
                    cookie_map,
                    {
                        "orderId": order_id,
                        "itemId": item_id,
                        "buyerId": buyer_id,
                        "sid": chat_id,
                        "timestamp": candidate.get("paid_time"),
                        "source": "startup_recent_order_recovery",
                    },
                )
                recovered = self.database.get_order(order_id)
                if recovered is not None and recovered.get("delivery_status") in {
                    "message_sent",
                    "delivered",
                    "confirm_pending",
                }:
                    recovered_count += 1
            except Exception as exc:
                self.database.record_audit(
                    "startup_order_recovery_order_failed",
                    order_id,
                    {"error": str(exc)[:200], "item_id": item_id},
                )
        self._last_recovery_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._recovered_order_count = recovered_count

    @business_operation
    async def _process_chat_event(
        self,
        websocket: Any,
        account_id: int,
        seller_id: str,
        message: dict[str, str],
        event: dict[str, Any],
    ) -> None:
        settings = self.database.get_auto_reply_settings(account_id)
        if not settings.get("enabled"):
            return

        chat_id = message["chat_id"]
        item_id = message["item_id"]
        direction = message["direction"]
        content = message["content"]
        buyer_id = message["sender_id"] if direction == "inbound" else ""
        fingerprint = hashlib.sha256(
            json.dumps(event, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        self._last_chat_at = time.strftime("%Y-%m-%d %H:%M:%S")

        if direction == "outbound":
            message_id = self.database.record_chat_message(
                account_id=account_id,
                chat_id=chat_id,
                buyer_id="",
                listing_item_id=item_id,
                direction="outbound",
                content=content,
                event_fingerprint=fingerprint,
                status="manual",
                reply_source="manual",
                reason="检测到卖家人工回复",
            )
            if message_id is None:
                return
            self.database.set_chat_manual(
                account_id,
                chat_id,
                enabled=True,
                hours=int(settings.get("manual_takeover_hours") or 12),
            )
            pending = self._reply_tasks.pop(chat_id, None)
            if pending is not None:
                pending.task.cancel()
                self.database.mark_chat_message(
                    pending.message_ids[-1], "manual", "卖家已人工回复，会话进入人工接管"
                )
            return

        message_id = self.database.record_chat_message(
            account_id=account_id,
            chat_id=chat_id,
            buyer_id=buyer_id,
            listing_item_id=item_id,
            direction="inbound",
            content=content,
            event_fingerprint=fingerprint,
        )
        if message_id is None:
            return
        if self.database.is_chat_manual(account_id, chat_id):
            self.database.mark_chat_message(message_id, "manual", "会话正由卖家人工接管")
            return

        high_risk_reason = manual_review_reason(content)
        product = self.database.get_product_by_listing_item_id(item_id, account_id)
        if high_risk_reason:
            self.database.mark_chat_message(message_id, "manual", high_risk_reason)
            self.database.set_chat_manual(
                account_id,
                chat_id,
                enabled=True,
                hours=int(settings.get("manual_takeover_hours") or 12),
            )
            return
        if product is None:
            self.database.mark_chat_message(
                message_id,
                "manual",
                "消息没有唯一匹配到当前账号的已上架商品；仅跳过当前消息",
            )
            return

        pending = self._reply_tasks.pop(chat_id, None)
        merged_messages = [content]
        merged_message_ids = [message_id]
        if pending is not None:
            pending.task.cancel()
            merged_messages = [*pending.messages, content]
            merged_message_ids = [*pending.message_ids, message_id]
            self.database.mark_chat_message(
                pending.message_ids[-1], "skipped", "已合并到买家的后续消息"
            )
        minimum = int(settings.get("min_delay_seconds") or 5)
        maximum = int(settings.get("max_delay_seconds") or 12)
        delay = random.uniform(minimum, max(minimum, maximum))
        task = asyncio.create_task(
            self._delayed_auto_reply(
                websocket=websocket,
                account_id=account_id,
                seller_id=seller_id,
                buyer_id=buyer_id,
                chat_id=chat_id,
                item_id=item_id,
                message_id=message_id,
                buyer_message=combine_buyer_messages(merged_messages),
                merged_message_ids=merged_message_ids,
                delay_seconds=delay,
            )
        )
        self._reply_tasks[chat_id] = PendingAutoReply(
            task=task,
            message_ids=merged_message_ids,
            messages=merged_messages,
        )

        def cleanup(done: asyncio.Task[None], target_chat_id: str = chat_id) -> None:
            current = self._reply_tasks.get(target_chat_id)
            if current is not None and current.task is done:
                self._reply_tasks.pop(target_chat_id, None)

        task.add_done_callback(cleanup)

    @business_operation
    async def _delayed_auto_reply(
        self,
        *,
        websocket: Any,
        account_id: int,
        seller_id: str,
        buyer_id: str,
        chat_id: str,
        item_id: str,
        message_id: int,
        buyer_message: str,
        delay_seconds: float,
        merged_message_ids: list[int] | None = None,
    ) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            settings = self.database.get_auto_reply_settings(account_id)
            if not settings.get("enabled"):
                self.database.mark_chat_message(message_id, "skipped", "自动回复已关闭")
                return
            if self.database.is_chat_manual(account_id, chat_id):
                self.database.mark_chat_message(message_id, "manual", "会话正由卖家人工接管")
                return
            product = self.database.get_product_by_listing_item_id(item_id, account_id)
            if product is None:
                self.database.mark_chat_message(message_id, "manual", "商品映射已变化，转人工处理")
                return
            if self.runtime_policy.reply_only or self.runtime_policy.mvp_fulfillment:
                from .knowledge import sanitize_product_knowledge
                if self._account_id != account_id or not sanitize_product_knowledge(str(product.get('knowledge_text') or '')):
                    self.database.mark_chat_message(message_id, 'manual', 'REPLY_ACCOUNT_OR_KNOWLEDGE_NOT_READY')
                    return
            safety = await self._outbound_preflight(account_id, "reply")
            if not safety["allowed"]:
                reason = f"安全限流：{safety['reason']}"
                self.database.mark_chat_message(message_id, "skipped", reason)
                self._last_auto_reply_error = reason
                self._notify(account_id, "自动回复已限流", reason)
                return
            if not self.database.claim_auto_reply(message_id):
                return

            merged_ids = set(merged_message_ids or [message_id])
            recent = [
                item
                for item in self.database.list_recent_chat_messages(account_id, chat_id, limit=9)
                if int(item.get("id") or 0) not in merged_ids
            ]
            decision = await self.reply_client.generate(
                api_key=self.secret_store.load(),
                base_url=str(settings.get("base_url") or ""),
                model=str(settings.get("model") or ""),
                system_prompt=str(settings.get("system_prompt") or DEFAULT_SYSTEM_PROMPT),
                buyer_message=buyer_message,
                product=product,
                context=recent,
                max_reply_chars=int(settings.get("max_reply_chars") or 180),
            )
            if decision.action not in {"reply", "clarify"}:
                reason = f"{decision.reason or '模型建议人工处理'}；仅跳过当前消息"
                self.database.mark_chat_message(message_id, "manual", reason)
                return
            if self.database.is_chat_manual(account_id, chat_id):
                self.database.mark_chat_message(message_id, "manual", "生成期间卖家已人工接管")
                return
            if self._runtime_websocket is not websocket or self._status != "listening":
                self.database.mark_chat_message(message_id, "failed", "消息连接已变化，未发送")
                return

            current_product = self.database.get_product_by_listing_item_id(item_id, account_id)
            if (current_product is None or current_product['dir_name'] != product['dir_name']
                    or current_product.get('knowledge_text') != product.get('knowledge_text')):
                self.database.mark_chat_message(message_id, 'manual', 'REPLY_PRODUCT_CHANGED_DURING_GENERATION')
                return

            await self._guarded_send_text(
                websocket,
                account_id=account_id,
                kind="reply",
                reference=f"chat-message:{message_id}",
                chat_id=chat_id,
                buyer_id=buyer_id,
                seller_id=seller_id,
                text=decision.reply,
            )
            self.database.record_auto_reply_sent(
                account_id=account_id,
                chat_id=chat_id,
                buyer_id=buyer_id,
                listing_item_id=item_id,
                inbound_message_id=message_id,
                reply=decision.reply,
                model=str(settings.get("model") or "deepseek-ai/DeepSeek-V4-Flash"),
                decision_action=decision.action,
            )
            self._last_auto_reply_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self._last_auto_reply_error = ""
        except asyncio.CancelledError:
            raise
        except OutboundSafetyError as exc:
            reason = str(exc)[:300]
            self.database.mark_chat_message(message_id, "skipped", reason)
            self._last_auto_reply_error = reason
        except asyncio.TimeoutError:
            self.database.mark_chat_message(
                message_id, "manual", "回复已提交但未收到平台确认，为避免重复已转人工"
            )
            self.database.set_chat_manual(account_id, chat_id, enabled=True, hours=12)
            self._last_auto_reply_error = "自动回复发送确认超时，已转人工"
        except Exception as exc:
            error = str(exc)[:300]
            self.database.mark_chat_message(message_id, "failed", error)
            self._last_auto_reply_error = error
            self.database.record_audit(
                "auto_reply_failed",
                str(message_id),
                {"account_id": account_id, "chat_id": chat_id, "error": error},
            )

    @business_operation
    async def _process_group_waiting_event(
        self,
        account_id: int,
        seller_id: str,
        cookie_map: dict[str, str],
        event: dict[str, Any],
    ) -> None:
        if self.runtime_policy.mvp_fulfillment:
            return
        if not self.runtime_policy.fulfillment_enabled:
            return
        self.runtime_policy.require_fulfillment()
        self._last_event_at = time.strftime("%Y-%m-%d %H:%M:%S")
        account = self.database.get_account(account_id)
        if (
            not account
            or not account.get("delivery_enabled")
            or not account.get("auto_free_group")
        ):
            return
        fingerprint = hashlib.sha256(
            json.dumps(event, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        order_id = extract_order_id(event)
        item_id = extract_item_id(event)
        buyer_id = extract_buyer_id(event, seller_id)
        chat_id = extract_chat_id(event)
        if (not order_id or not item_id or not buyer_id) and chat_id:
            recovered = await self._resolve_order_by_chat_id(
                account_id, seller_id, cookie_map, chat_id
            )
            if recovered is not None:
                event = recovered
                fingerprint = hashlib.sha256(
                    json.dumps(event, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()
                order_id = extract_order_id(event)
                item_id = extract_item_id(event)
                buyer_id = extract_buyer_id(event, seller_id)
                chat_id = extract_chat_id(event)
        if not order_id or not item_id or not buyer_id:
            missing = [
                name
                for name, value in (
                    ("订单号", order_id),
                    ("商品ID", item_id),
                    ("买家ID", buyer_id),
                )
                if not value
            ]
            self._last_error = f"检测到待刀成系统卡片，但缺少{'、'.join(missing)}，未执行免拼"
            self.database.record_audit(
                "group_event_incomplete",
                fingerprint,
                {"missing": missing, "account_id": account_id},
            )
            return

        self.runtime_policy.require_delivery_item(item_id)
        product = self.database.get_product_by_listing_item_id(item_id, account_id)
        if product is None:
            self._last_error = "检测到待刀成订单，但没有唯一匹配的当前账号商品，未执行免拼"
            self.database.record_audit(
                "group_event_product_unmatched",
                order_id,
                {"account_id": account_id, "listing_item_id": item_id},
            )
            return
        if delivery_issues(product):
            self._last_error = "待刀成订单对应商品尚未满足自动交付条件，未执行免拼"
            return

        self.database.upsert_group_waiting_order(
            order_id=order_id,
            account_id=account_id,
            product_dir_name=str(product["dir_name"]),
            listing_item_id=item_id,
            buyer_id=buyer_id,
            chat_id=chat_id,
            event_fingerprint=fingerprint,
        )
        if not self.database.claim_group_exemption(order_id):
            return
        try:
            await self._free_group_order(order_id, item_id, buyer_id, cookie_map)
        except Exception as exc:
            self.database.mark_group_exemption_failed(order_id, f"自动免拼失败：{exc}")
            self._last_error = f"自动免拼失败：{exc}"[:300]
            return
        self.database.mark_group_exempted(order_id)
        self._last_error = ""

    @business_operation
    async def _process_paid_event(
        self,
        websocket: Any,
        account_id: int,
        seller_id: str,
        cookie_map: dict[str, str],
        event: dict[str, Any],
    ) -> None:
        if not self.runtime_policy.fulfillment_enabled:
            return
        self.runtime_policy.require_fulfillment()
        self._last_event_at = time.strftime("%Y-%m-%d %H:%M:%S")
        account = self.database.get_account(account_id)
        if (not account or not account.get("delivery_enabled") or not self.runtime_policy.reply_account_selected(account, account_id)
                or account.get("binding_status") != "bound" or self._account_id != account_id):
            return
        fingerprint = hashlib.sha256(
            json.dumps(event, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        order_id = extract_order_id(event)
        item_id = extract_item_id(event)
        buyer_id = extract_buyer_id(event, seller_id)
        chat_id = extract_chat_id(event)
        if (not order_id or not item_id or not buyer_id) and chat_id:
            recovered = await self._resolve_order_by_chat_id(
                account_id, seller_id, cookie_map, chat_id
            )
            if recovered is not None:
                event = recovered
                fingerprint = hashlib.sha256(
                    json.dumps(event, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()
                order_id = extract_order_id(event)
                item_id = extract_item_id(event)
                buyer_id = extract_buyer_id(event, seller_id)
                chat_id = extract_chat_id(event)
        if not order_id or not item_id or not buyer_id or not chat_id:
            missing = [
                name
                for name, value in (
                    ("订单号", order_id),
                    ("商品ID", item_id),
                    ("买家ID", buyer_id),
                    ("会话ID", chat_id),
                )
                if not value
            ]
            self._last_error = f"检测到付款信号，但缺少{'、'.join(missing)}，已停止自动发送"
            self.database.record_audit(
                "paid_event_incomplete", fingerprint, {"missing": missing, "account_id": account_id}
            )
            return

        self.runtime_policy.require_delivery_item(item_id)
        product = self.database.get_product_by_listing_item_id(item_id, account_id)
        if product is None:
            self._last_error = "检测到付款订单，但没有唯一匹配的已上架商品，已停止自动发送"
            self.database.record_audit(
                "paid_event_product_unmatched",
                order_id,
                {"account_id": account_id, "listing_item_id": item_id},
            )
            return
        if delivery_issues(product):
            self._last_error = "匹配商品的网盘链接尚未通过验证，已停止自动发送"
            return

        # All live, recovery and manual reconciliation routes re-read the exact
        # seller order. Event timestamps are not proof of payment time.
        try:
            payment_time = await self._verified_payment_time(account_id, order_id, item_id, buyer_id, cookie_map)
        except (ValueError, RuntimeError) as exc:
            self._last_error = f'ORDER_CUTOFF_BLOCKED: {type(exc).__name__}'
            from .order_cutoff import OrderCutoffBlocked
            reason = str(exc) if isinstance(exc, OrderCutoffBlocked) else 'PLATFORM_PAYMENT_LOOKUP_FAILED'
            self.database.record_audit('order_cutoff_blocked', order_id, {'account_id': account_id, 'reason': reason})
            return
        try:
            self.database.upsert_paid_order(
                order_id=order_id,
                account_id=account_id,
                product_dir_name=str(product["dir_name"]),
                listing_item_id=item_id,
                buyer_id=buyer_id,
                chat_id=chat_id,
                event_fingerprint=fingerprint,
            )
        except ValueError:
            self._last_error = "订单账号、映射或收件身份冲突，已停止发送，请人工核对"
            self.database.record_audit("paid_event_order_conflict", order_id, {"account_id": account_id})
            return
        safety = await self._outbound_preflight(account_id, "delivery")
        if not safety["allowed"]:
            reason = f"安全限流：{safety['reason']}"
            self.database.mark_order_manual_review(order_id, reason)
            self._last_error = reason
            self._notify(account_id, "自动发货已转人工", reason)
            return
        prepared = self.database.claim_verified_delivery(order_id, account_id, item_id)
        if prepared is None:
            return

        message = prepared["message"]
        message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
        try:
            await self._guarded_send_text(
                websocket,
                account_id=account_id,
                kind="delivery",
                reference=f"order:{order_id}",
                chat_id=chat_id,
                buyer_id=buyer_id,
                seller_id=seller_id,
                text=message,
                payment_time=payment_time,
            )
        except OutboundSafetyError as exc:
            self.database.mark_order_manual_review(order_id, str(exc)[:300])
            self._last_error = str(exc)[:300]
            return
        except asyncio.TimeoutError:
            self.database.mark_order_manual_review(
                order_id, "消息已提交到连接但未收到确认，为防止重复发送已转人工核对"
            )
            self._last_error = "发货消息确认超时，已停止重复发送并转人工核对"
            return
        except Exception as exc:
            self.database.mark_order_manual_review(order_id, "发送结果无法确定，禁止自动重发")
            self._last_error = f"发送发货消息失败：{exc}"[:300]
            return

        self.database.mark_order_message_sent(order_id, message_hash)
        account = self.database.get_account(account_id)
        if not account or not account.get("auto_confirm_delivery"):
            self.database.mark_order_delivered(order_id, platform_status="skipped")
            self._last_delivery_at = time.strftime("%Y-%m-%d %H:%M:%S")
            return

        try:
            await self._confirm_platform_delivery(order_id, cookie_map)
        except Exception as exc:
            self.database.mark_order_confirmation_pending(order_id, f"平台确认发货失败：{exc}")
            self._last_error = "网盘链接已发送，但平台确认发货失败；系统不会重复发送链接"
            return
        self.database.mark_order_delivered(order_id, platform_status="confirmed")
        self._last_delivery_at = time.strftime("%Y-%m-%d %H:%M:%S")

    @business_operation
    async def _guarded_send_text(
        self,
        websocket: Any,
        *,
        account_id: int,
        kind: str,
        reference: str,
        chat_id: str,
        buyer_id: str,
        seller_id: str,
        text: str,
        payment_time=None,
    ) -> None:
        async with self._outbound_lock:
            if kind == 'delivery':
                self.runtime_policy.require_fulfillment()
                order = self.database.get_order(reference[6:]) if reference.startswith('order:') else None
                self.runtime_policy.require_delivery_item((order or {}).get('listing_item_id'))
                from .order_cutoff import require_after_cutoff
                require_after_cutoff(self.runtime_policy.order_cutoff_at, payment_time)
            if kind == "delivery" and (self._account_id != account_id or not reference.startswith("order:") or not self.database.validate_delivery_claim(reference[6:], account_id, chat_id, text, buyer_id=buyer_id)):
                raise OutboundSafetyError("交付资格或账号/订单资料已变化，必须人工核对")
            decision = self.database.reserve_automation_outbound(
                account_id, kind, reference
            )
            if not decision["allowed"]:
                raise OutboundSafetyError(f"安全保护阻止发送：{decision['reason']}")
            event_id = int(decision["event_id"])
            try:
                await self._send_text(websocket, chat_id, buyer_id, seller_id, text)
            except RiskControlError as exc:
                self.database.finish_automation_outbound(
                    event_id, sent=False, error=str(exc)
                )
                self._open_safety_circuit(account_id, str(exc))
                try:
                    await websocket.close()
                except Exception:
                    pass
                raise
            except Exception as exc:
                self.database.finish_automation_outbound(
                    event_id, sent=False, error=str(exc)
                )
                raise
            self.database.finish_automation_outbound(event_id, sent=True)

    async def _verified_payment_time(self, account_id, order_id, item_id, buyer_id, cookie_map):
        from .order_cutoff import OrderCutoffBlocked, require_after_cutoff
        self.runtime_policy.require_fulfillment()
        self.runtime_policy.require_account(account_id)
        if self._account_id != account_id:
            raise OrderCutoffBlocked('ORDER_ACCOUNT_MISMATCH')
        preview = await self._fetch_recent_sold_orders(cookie_map)
        matches = [row for row in preview.get('orders', []) if isinstance(row, dict) and row.get('order_id') == order_id]
        if len(matches) != 1:
            raise OrderCutoffBlocked('PLATFORM_ORDER_NOT_UNIQUE_OR_NOT_FOUND')
        row = matches[0]
        if row.get('item_id') != item_id or row.get('buyer_id') != buyer_id or row.get('order_status') not in {'待发货', 'pending_ship'}:
            raise OrderCutoffBlocked('PLATFORM_ORDER_IDENTITY_OR_PAYMENT_STATE_MISMATCH')
        return require_after_cutoff(self.runtime_policy.order_cutoff_at, row.get('paid_time'))

    @business_operation
    async def _send_text(
        self,
        websocket: Any,
        chat_id: str,
        buyer_id: str,
        seller_id: str,
        text: str,
    ) -> None:
        content = {
            "contentType": 1,
            "text": {"text": text},
        }
        encoded = base64.b64encode(
            json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        mid = generate_mid()
        message = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {"mid": mid},
            "body": [
                {
                    "uuid": generate_message_uuid(),
                    "cid": f"{chat_id}@goofish",
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {"type": 1, "data": encoded},
                    },
                    "redPointPolicy": 0,
                    "extension": {"extJson": "{}"},
                    "ctx": {"appVersion": "1.0", "platform": "web"},
                    "mtags": {},
                    "msgReadStatusSetting": 1,
                },
                {"actualReceivers": [f"{buyer_id}@goofish", f"{seller_id}@goofish"]},
            ],
        }
        future = asyncio.get_running_loop().create_future()
        self._pending_acks[mid] = future
        try:
            await self._send_json(websocket, message)
            await asyncio.wait_for(future, timeout=10)
        finally:
            self._pending_acks.pop(mid, None)

    @business_operation
    async def _create_chat(
        self,
        websocket: Any,
        buyer_id: str,
        seller_id: str,
        item_id: str,
    ) -> str:
        mid = generate_mid()
        request = {
            "lwp": "/r/SingleChatConversation/create",
            "headers": {"mid": mid},
            "body": [
                {
                    "pairFirst": f"{buyer_id}@goofish",
                    "pairSecond": f"{seller_id}@goofish",
                    "bizType": "1",
                    "extension": {"itemId": item_id},
                    "ctx": {"appVersion": "1.0", "platform": "web"},
                }
            ],
        }
        future = asyncio.get_running_loop().create_future()
        self._pending_acks[mid] = future
        try:
            await self._send_json(websocket, request)
            response = await asyncio.wait_for(future, timeout=10)
        finally:
            self._pending_acks.pop(mid, None)

        def find_cid(value: Any) -> str:
            if isinstance(value, dict):
                direct = value.get("cid")
                if direct:
                    return str(direct)
                for nested in value.values():
                    found = find_cid(nested)
                    if found:
                        return found
            elif isinstance(value, list):
                for nested in value:
                    found = find_cid(nested)
                    if found:
                        return found
            return ""

        cid = find_cid(response)
        if "@goofish" in cid:
            cid = cid.split("@", 1)[0]
        if not re.fullmatch(r"\d{5,}", cid):
            raise RuntimeError("平台未返回有效的订单会话 ID")
        return cid

    @business_operation
    async def _post_mtop(
        self,
        url: str,
        *,
        params: dict[str, str],
        data_json: str,
        headers: dict[str, str],
        cookie_map: dict[str, str],
    ) -> dict[str, Any]:
        """Send MTOP calls through the same browser session used for IM auth."""
        request_context = self._runtime_request_context
        if request_context is not None:
            browser_headers = dict(headers)
            browser_headers.pop("cookie", None)
            response = await request_context.post(
                url,
                params=params,
                form={"data": data_json},
                headers=browser_headers,
                timeout=30_000,
            )
            payload = await response.json()
            storage = await request_context.storage_state()
            for cookie in storage.get("cookies", []):
                if cookie.get("name"):
                    cookie_map[str(cookie["name"])] = str(cookie.get("value", ""))
        else:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.post(
                    url,
                    params=params,
                    data={"data": data_json},
                    headers=headers,
                )
                for cookie in response.cookies.jar:
                    cookie_map[cookie.name] = cookie.value
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("平台返回了无法识别的数据")
        return payload

    @business_operation
    async def _confirm_platform_delivery(
        self, order_id: str, cookie_map: dict[str, str]
    ) -> None:
        self.runtime_policy.require_fulfillment()
        data_json = json.dumps(
            {
                "orderId": order_id,
                "tradeText": "",
                "picList": [],
                "newUnconsign": True,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        timestamp = str(int(time.time() * 1000))
        token_seed = cookie_map.get("_m_h5_tk", "").split("_", 1)[0]
        params = {
            "jsv": "2.7.2",
            "appKey": MTOP_APP_KEY,
            "t": timestamp,
            "sign": generate_mtop_sign(timestamp, token_seed, data_json),
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": "mtop.taobao.idle.logistic.consign.dummy",
            "sessionOption": "AutoLoginOnly",
        }
        headers = {
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded",
            "origin": GOOFISH_HOME.rstrip("/"),
            "referer": GOOFISH_HOME,
            "user-agent": USER_AGENT,
            "cookie": self._cookie_header(cookie_map),
        }
        payload = await self._post_mtop(
            CONFIRM_URL,
            params=params,
            data_json=data_json,
            headers=headers,
            cookie_map=cookie_map,
        )
        ret = payload.get("ret", []) if isinstance(payload, dict) else []
        if not any("SUCCESS" in str(item) for item in ret):
            error = str(ret[0]) if ret else "未知平台响应"
            raise RuntimeError(error[:200])

    @business_operation
    async def _free_group_order(
        self,
        order_id: str,
        item_id: str,
        buyer_id: str,
        cookie_map: dict[str, str],
    ) -> None:
        self.runtime_policy.require_fulfillment()
        data_json = json.dumps(
            {
                "bizOrderId": order_id,
                "itemId": int(item_id),
                "buyerId": int(buyer_id),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        timestamp = str(int(time.time() * 1000))
        token_seed = cookie_map.get("_m_h5_tk", "").split("_", 1)[0]
        params = {
            "jsv": "2.7.2",
            "appKey": MTOP_APP_KEY,
            "t": timestamp,
            "sign": generate_mtop_sign(timestamp, token_seed, data_json),
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": "mtop.idle.groupon.activity.seller.freeshipping",
            "sessionOption": "AutoLoginOnly",
        }
        headers = {
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded",
            "origin": GOOFISH_HOME.rstrip("/"),
            "referer": GOOFISH_HOME,
            "user-agent": self._runtime_user_agent,
            "cookie": self._cookie_header(cookie_map),
        }
        payload = await self._post_mtop(
            FREE_GROUP_URL,
            params=params,
            data_json=data_json,
            headers=headers,
            cookie_map=cookie_map,
        )
        ret = payload.get("ret", []) if isinstance(payload, dict) else []
        if not any("SUCCESS" in str(item) for item in ret):
            error = str(ret[0]) if ret else "未知平台响应"
            raise RuntimeError(error[:200])

    @business_operation
    async def _fetch_live_inventory(
        self,
        cookie_map: dict[str, str],
        seller_id: str,
        *,
        max_pages: int = 5,
    ) -> list[dict[str, object]]:
        """Fetch a bounded seller inventory using the API used by the PC profile page."""
        raw_items: list[dict[str, object]] = []
        next_page_model: object | None = None
        next_page_num: object | None = None
        for page_number in range(1, max(1, min(max_pages, 5)) + 1):
            token_seed = cookie_map.get("_m_h5_tk", "").split("_", 1)[0]
            if not token_seed:
                raise RuntimeError("登录 Cookie 缺少商品查询令牌")
            data: dict[str, object] = {
                "needGroupInfo": page_number == 1,
                "pageNumber": page_number,
                "userId": seller_id,
                "pageSize": 20,
            }
            if next_page_model is not None:
                data["nextPageModel"] = next_page_model
            if next_page_num is not None:
                data["nextPageNum"] = next_page_num
            data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            timestamp = str(int(time.time() * 1000))
            params = {
                "jsv": "2.7.2",
                "appKey": MTOP_APP_KEY,
                "t": timestamp,
                "sign": generate_mtop_sign(timestamp, token_seed, data_json),
                "v": "1.0",
                "type": "originaljson",
                "accountSite": "xianyu",
                "dataType": "json",
                "timeout": "20000",
                "api": "mtop.idle.web.xyh.item.list",
                "sessionOption": "AutoLoginOnly",
            }
            headers = {
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "origin": GOOFISH_HOME.rstrip("/"),
                "referer": f"https://www.goofish.com/personal?userId={seller_id}",
                "user-agent": self._runtime_user_agent,
                "cookie": self._cookie_header(cookie_map),
            }
            payload = await self._post_mtop(
                ITEM_LIST_URL,
                params=params,
                data_json=data_json,
                headers=headers,
                cookie_map=cookie_map,
            )
            ret = payload.get("ret", []) if isinstance(payload, dict) else []
            if not any("SUCCESS" in str(item) for item in ret):
                error = str(ret[0]) if ret else "未知平台响应"
                raise RuntimeError(f"读取闲鱼在售商品失败：{error[:200]}")
            response_data = payload.get("data", {})
            if not isinstance(response_data, dict):
                break
            cards = response_data.get("cardList", [])
            page_items = inventory_cards_to_raw_items(cards)
            raw_items.extend(page_items)
            if not response_data.get("nextPage") or not cards:
                break
            next_page_model = response_data.get("nextPageModel")
            next_page_num = response_data.get("nextPageNum")
        return raw_items

    @business_operation
    async def _fetch_recent_sold_orders(
        self, cookie_map: dict[str, str], *, page_size: int = 20
    ) -> dict[str, object]:
        """Fetch a read-only, bounded preview of the newest seller orders."""
        data_json = json.dumps(
            {
                "pageNumber": 1,
                "rowsPerPage": max(1, min(page_size, 20)),
                "orderIds": "",
                "queryCode": "ALL",
                "orderSearchParam": "{}",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        timestamp = str(int(time.time() * 1000))
        token_seed = cookie_map.get("_m_h5_tk", "").split("_", 1)[0]
        if not token_seed:
            raise RuntimeError("登录 Cookie 缺少订单查询令牌")
        params = {
            "jsv": "2.7.2",
            "appKey": MTOP_APP_KEY,
            "t": timestamp,
            "sign": generate_mtop_sign(timestamp, token_seed, data_json),
            "v": "1.0",
            "type": "json",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": "mtop.taobao.idle.trade.merchant.sold.get",
            "valueType": "string",
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21107h.42831410.0.0",
        }
        headers = {
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded",
            "idle_site_biz_code": "COMMONPRO",
            "origin": "https://seller.goofish.com",
            "referer": "https://seller.goofish.com/?site=COMMONPRO#/seller-trade/order-manage",
            "user-agent": self._runtime_user_agent,
            "cookie": self._cookie_header(cookie_map),
        }
        payload = await self._post_mtop(
            ORDER_LIST_URL,
            params=params,
            data_json=data_json,
            headers=headers,
            cookie_map=cookie_map,
        )
        ret = payload.get("ret", []) if isinstance(payload, dict) else []
        if not any("SUCCESS" in str(item) for item in ret):
            error = str(ret[0]) if ret else "未知平台响应"
            raise RuntimeError(f"读取卖家订单失败：{error[:200]}")

        module = payload.get("data", {}).get("module", {})
        raw_items = module.get("items", []) if isinstance(module, dict) else []
        orders: list[dict[str, object]] = []
        for raw in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(raw, dict):
                continue
            common = raw.get("commonData", {})
            buyer = raw.get("buyerInfoVO", {})
            if not isinstance(common, dict) or not isinstance(buyer, dict):
                continue
            order_id = str(common.get("orderId") or "").strip()
            if not order_id:
                continue
            diagnostics: dict[str, str] = {}
            for path, value in walk_scalars(raw):
                lowered = path.lower()
                if any(marker in lowered for marker in ("status", "group", "groupon", "bargain", "knife")):
                    diagnostics[path] = value[:200]
            orders.append(
                {
                    "order_id": order_id,
                    "item_id": str(common.get("itemId") or "").strip(),
                    "buyer_id": str(buyer.get("buyerId") or "").strip(),
                    "buyer_nick": str(buyer.get("userNick") or "").strip(),
                    "order_status": str(common.get("orderStatus") or "").strip(),
                    "create_time": str(common.get("createTime") or "").strip(),
                    "paid_time": str(common.get("paySuccessTime") or "").strip(),
                    "diagnostics": diagnostics,
                }
            )
        return {
            "orders": orders,
            "total_count": str(module.get("totalCount") or "") if isinstance(module, dict) else "",
        }

    @business_operation
    async def _resolve_order_by_chat_id(
        self,
        account_id: int,
        seller_id: str,
        cookie_map: dict[str, str],
        chat_id: str,
    ) -> dict[str, str] | None:
        """Resolve a simplified system card through the seller order list and cid."""
        websocket = self._runtime_websocket
        if websocket is None or not re.fullmatch(r"\d{5,}", chat_id):
            return None
        try:
            preview = await self._fetch_recent_sold_orders(cookie_map)
        except Exception as exc:
            self.database.record_audit(
                "order_reconciliation_failed",
                chat_id,
                {"account_id": account_id, "error": str(exc)[:200]},
            )
            return None

        matches: list[dict[str, str]] = []
        for candidate in preview.get("orders", []):
            if not isinstance(candidate, dict):
                continue
            if candidate.get("order_status") not in {"待发货", "pending_ship"}:
                continue
            order_id = str(candidate.get("order_id") or "").strip()
            item_id = str(candidate.get("item_id") or "").strip()
            buyer_id = str(candidate.get("buyer_id") or "").strip()
            if self.database.get_order(order_id) is not None:
                continue
            if self.database.get_product_by_listing_item_id(item_id, account_id) is None:
                continue
            try:
                candidate_chat_id = await self._create_chat(
                    websocket, buyer_id, seller_id, item_id
                )
            except Exception:
                continue
            if candidate_chat_id == chat_id:
                matches.append(
                    {
                        "orderId": order_id,
                        "itemId": item_id,
                        "buyerId": buyer_id,
                        "sid": chat_id,
                        "source": "automatic_order_reconciliation",
                    }
                )
        if len(matches) != 1:
            self.database.record_audit(
                "order_reconciliation_ambiguous",
                chat_id,
                {"account_id": account_id, "match_count": len(matches)},
            )
            return None
        return matches[0]

    @business_operation
    async def _send_json(self, websocket: Any, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await websocket.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
