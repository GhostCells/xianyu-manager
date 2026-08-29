from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus


SEARCH_API_HINT = "mtop.taobao.idlemtopsearch"
LOGIN_URL_HINTS = ("passport.goofish.com", "login.taobao.com", "mini_login")
LOGIN_TEXT_HINTS = ("账号登录", "短信登录", "扫码登录", "登录后继续")
RISK_TEXT_HINTS = ("验证码", "滑块验证", "访问异常", "操作频繁", "安全验证", "请完成验证")
RISK_SELECTORS = (
    "div.baxia-dialog-mask",
    "div.J_MIDDLEWARE_FRAME_WIDGET",
    "iframe[src*='captcha']",
    "iframe[src*='punish']",
    "iframe[src*='baxia']",
)

class SelectionBridgeError(RuntimeError):
    def __init__(self, code: str, stage: str, message: str) -> None:
        self.code = code
        self.stage = stage
        self.message = message
        super().__init__(message)


class SelectionBusyError(SelectionBridgeError):
    def __init__(self) -> None:
        super().__init__("SELECTION_BUSY", "acquire_lock", "当前账号已有选品任务正在执行")


def build_search_url(keyword: str) -> str:
    return f"https://www.goofish.com/search?q={quote_plus(keyword)}"


def _result_list(payload: Any) -> list[Any] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    result = data.get("resultList")
    return result if isinstance(result, list) else None


def _nested(value: Any, *keys: str, default: Any = None) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current



def _price_text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(
            str(part.get("text", ""))
            for part in value
            if isinstance(part, dict)
        ).replace("当前价", "").strip()
    return str(value or "").strip()


def _price_number(value: str) -> float | None:
    normalized = value.replace("¥", "").replace("￥", "").replace(",", "").strip()
    multiplier = 10_000 if normalized.endswith("万") else 1
    if multiplier != 1:
        normalized = normalized[:-1]
    try:
        return float(normalized) * multiplier
    except (TypeError, ValueError):
        return None


def _publish_time(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw.isdigit():
        return ""
    timestamp = int(raw)
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    try:
        return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="minutes")
    except (OverflowError, OSError, ValueError):
        return ""


def normalize_search_items(payload: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for card in (_result_list(payload) or [])[:limit]:
        main = _nested(card, "data", "item", "main", "exContent", default={})
        click = _nested(card, "data", "item", "main", "clickParam", "args", default={})
        if not isinstance(main, dict):
            continue
        if not isinstance(click, dict):
            click = {}
        item_id = str(main.get("itemId") or "").strip()
        raw_url = str(main.get("targetUrl") or "").strip()
        if raw_url.startswith("fleamarket://"):
            raw_url = raw_url.replace("fleamarket://", "https://www.goofish.com/", 1)
        if not raw_url and item_id:
            raw_url = f"https://www.goofish.com/item?id={item_id}"
        price_text = _price_text(main.get("price"))
        tags: list[str] = []
        if click.get("tag") == "freeship":
            tags.append("包邮")
        tag_list = _nested(main, "fishTags", "r1", "tagList", default=[])
        if isinstance(tag_list, list):
            for tag in tag_list:
                content = _nested(tag, "data", "content", default="")
                if content:
                    tags.append(str(content))
        normalized.append(
            {
                "item_id": item_id,
                "title": str(main.get("title") or "").strip(),
                "price": _price_number(price_text),
                "price_text": price_text,
                "want_count": click.get("wantNum"),
                "want_count_text": str(click.get("wantNum") or ""),
                "publish_time": _publish_time(click.get("publishTime")),
                "url": raw_url,
                "image_url": str(main.get("picUrl") or "").strip(),
                "region": str(main.get("area") or "").strip(),
                "seller_nickname": str(main.get("userNickName") or "").strip(),
                "raw_tags": tags,
            }
        )
    return normalized


async def _visible_signal(page: Any) -> tuple[str, str] | None:
    url = str(page.url or "").lower()
    if any(hint in url for hint in LOGIN_URL_HINTS):
        return "LOGIN_REQUIRED", "页面进入闲鱼登录校验"
    try:
        text = (await page.locator("body").inner_text(timeout=1000))[:8000]
    except Exception:
        text = ""
    if any(marker in text for marker in LOGIN_TEXT_HINTS):
        return "LOGIN_REQUIRED", "页面要求重新登录"
    if any(marker in text for marker in RISK_TEXT_HINTS):
        return "VERIFICATION_REQUIRED", "页面要求人工完成平台验证"
    for selector in RISK_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible():
                box = await locator.bounding_box()
                if box and box.get("width", 0) > 1 and box.get("height", 0) > 1:
                    return "VERIFICATION_REQUIRED", "页面出现可见的闲鱼安全验证"
        except Exception:
            continue
    return None


async def capture_search_json(
    page: Any,
    *,
    keyword: str,
    timeout_seconds: float = 30,
) -> tuple[dict[str, Any], dict[str, Any]]:
    loop = asyncio.get_running_loop()
    matched: asyncio.Future[tuple[Any, dict[str, Any]]] = loop.create_future()
    inspection_tasks: set[asyncio.Task[Any]] = set()

    async def inspect_response(response: Any) -> None:
        request = response.request
        if getattr(request, "resource_type", "") not in {"xhr", "fetch"}:
            return
        if SEARCH_API_HINT not in str(response.url).lower():
            return
        try:
            payload = await response.json()
        except Exception:
            return
        if _result_list(payload) is not None and not matched.done():
            matched.set_result((response, payload))

    def on_response(response: Any) -> None:
        task = asyncio.create_task(inspect_response(response))
        inspection_tasks.add(task)
        task.add_done_callback(inspection_tasks.discard)

    page.on("response", on_response)
    try:
        try:
            await page.goto(
                build_search_url(keyword),
                wait_until="domcontentloaded",
                timeout=20_000,
            )
        except Exception as exc:
            signal = await _visible_signal(page)
            if signal:
                raise SelectionBridgeError(signal[0], "navigate", signal[1]) from exc
            raise SelectionBridgeError("NAVIGATION_TIMEOUT", "navigate", "打开闲鱼搜索页失败") from exc

        deadline = loop.time() + timeout_seconds
        while not matched.done():
            signal = await _visible_signal(page)
            if signal:
                raise SelectionBridgeError(signal[0], "wait_search_response", signal[1])
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SelectionBridgeError(
                    "SEARCH_RESPONSE_TIMEOUT",
                    "wait_search_response",
                    "等待闲鱼商品搜索响应超时",
                )
            try:
                await asyncio.wait_for(asyncio.shield(matched), timeout=min(0.5, remaining))
            except asyncio.TimeoutError:
                continue

        response, payload = matched.result()
        return payload, {
            "request_url": str(response.url),
            "http_status": int(response.status),
            "content_type": str((await response.all_headers()).get("content-type", "")),
            "ret": payload.get("ret") if isinstance(payload, dict) else None,
            "list_path": "data.resultList",
        }
    finally:
        page.remove_listener("response", on_response)
        if inspection_tasks:
            await asyncio.gather(*inspection_tasks, return_exceptions=True)
