from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .selection_bridge import SelectionBridgeError, _visible_signal


DETAIL_API_HINT = "mtop.taobao.idle.pc.detail"


def _nonnegative_count(value: Any) -> int | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    multiplier = 10_000 if text.endswith("万") else 1
    if multiplier != 1:
        text = text[:-1]
    try:
        result = int(Decimal(text) * multiplier)
    except (InvalidOperation, ValueError):
        return None
    return result if result >= 0 else None


def _price_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("text", "value", "price"):
            if key in value:
                return _price_text(value[key])
        return ""
    if isinstance(value, list):
        return "".join(_price_text(part) for part in value).strip()
    return str(value or "").replace("¥", "").replace("￥", "").strip()


def _price_cents(value: str) -> int | None:
    normalized = value.replace(",", "").strip()
    if not normalized:
        return None
    multiplier = Decimal("10000") if normalized.endswith("万") else Decimal("1")
    if multiplier != 1:
        normalized = normalized[:-1]
    try:
        amount = Decimal(normalized) * multiplier
    except InvalidOperation:
        return None
    if amount < 0:
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def normalize_detail(payload: dict[str, Any]) -> dict[str, object]:
    data = payload.get("data")
    item = data.get("itemDO") if isinstance(data, dict) else None
    if not isinstance(item, dict):
        raise ValueError("详情响应缺少 data.itemDO")
    raw_price: Any = None
    for key in ("soldPrice", "price", "currentPrice", "originalPrice"):
        if item.get(key) not in (None, ""):
            raw_price = item[key]
            break
    price_text = _price_text(raw_price)
    return {
        "want_count": _nonnegative_count(item.get("wantCnt")),
        "browse_count": _nonnegative_count(item.get("browseCnt")),
        "collect_count": _nonnegative_count(item.get("collectCnt")),
        "price_text": price_text,
        "price_cents": _price_cents(price_text),
    }


async def capture_detail_json(
    page: Any,
    *,
    item_url: str,
    timeout_seconds: float = 30,
) -> tuple[dict[str, Any], dict[str, object]]:
    loop = asyncio.get_running_loop()
    matched: asyncio.Future[tuple[Any, dict[str, Any]]] = loop.create_future()
    inspection_tasks: set[asyncio.Task[Any]] = set()

    async def inspect_response(response: Any) -> None:
        request = response.request
        if getattr(request, "resource_type", "") not in {"xhr", "fetch"}:
            return
        if DETAIL_API_HINT not in str(response.url).lower():
            return
        try:
            payload = await response.json()
        except Exception:
            return
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and isinstance(data.get("itemDO"), dict):
            if not matched.done():
                matched.set_result((response, payload))

    def on_response(response: Any) -> None:
        task = asyncio.create_task(inspect_response(response))
        inspection_tasks.add(task)
        task.add_done_callback(inspection_tasks.discard)

    page.on("response", on_response)
    try:
        try:
            await page.goto(item_url, wait_until="domcontentloaded", timeout=20_000)
        except Exception as exc:
            signal = await _visible_signal(page)
            if signal:
                raise SelectionBridgeError(signal[0], "detail_navigate", signal[1]) from exc
            raise SelectionBridgeError(
                "DETAIL_NAVIGATION_TIMEOUT", "detail_navigate", "打开闲鱼商品详情页失败"
            ) from exc

        deadline = loop.time() + timeout_seconds
        while not matched.done():
            signal = await _visible_signal(page)
            if signal:
                raise SelectionBridgeError(signal[0], "wait_detail_response", signal[1])
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SelectionBridgeError(
                    "DETAIL_RESPONSE_TIMEOUT",
                    "wait_detail_response",
                    "等待闲鱼商品详情响应超时",
                )
            try:
                await asyncio.wait_for(asyncio.shield(matched), timeout=min(0.5, remaining))
            except asyncio.TimeoutError:
                continue

        response, payload = matched.result()
        return payload, {
            "request_url": str(response.url).split("?", 1)[0],
            "http_status": int(response.status),
            "list_path": "data.itemDO",
        }
    finally:
        page.remove_listener("response", on_response)
        if inspection_tasks:
            await asyncio.gather(*inspection_tasks, return_exceptions=True)
