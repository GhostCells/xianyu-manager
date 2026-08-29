from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx

from .database import Database


BRIDGE_BLOCKING_CODES = {
    "SELECTION_BUSY",
    "LOGIN_REQUIRED",
    "VERIFICATION_REQUIRED",
    "BROWSER_NOT_RUNNING",
}


class SelectionCollectionError(RuntimeError):
    def __init__(self, run_id: str, code: str, message: str) -> None:
        self.run_id = run_id
        self.code = code
        self.message = message
        super().__init__(message)


def _safe_source_api(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _bridge_error(response: httpx.Response) -> tuple[str, str]:
    try:
        detail = response.json().get("detail", {})
    except (ValueError, AttributeError):
        detail = {}
    if isinstance(detail, dict):
        return (
            str(detail.get("code") or f"HTTP_{response.status_code}"),
            str(detail.get("message") or "选品浏览器桥调用失败"),
        )
    return f"HTTP_{response.status_code}", str(detail or "选品浏览器桥调用失败")


async def collect_selection_search(
    keyword: str,
    database: Database,
    *,
    bridge_token: str,
    bridge_url: str = "http://127.0.0.1:8765/api/internal/selection/search",
    client: httpx.AsyncClient | None = None,
) -> dict[str, object]:
    normalized_keyword = keyword.strip()
    if not normalized_keyword:
        raise ValueError("搜索关键词不能为空")
    if len(normalized_keyword) > 40:
        raise ValueError("搜索关键词不能超过40个字符")
    if not bridge_token.strip():
        raise ValueError("选品浏览器桥密钥为空")

    run_id = uuid4().hex
    database.start_selection_search_run(run_id, normalized_keyword, page=1)
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=50)
    source_api = ""
    http_status: int | None = None
    try:
        response = await http_client.post(
            bridge_url,
            headers={"X-Internal-Token": bridge_token.strip()},
            json={"keyword": normalized_keyword, "page": 1, "limit": 30},
        )
        http_status = response.status_code
        if response.is_error:
            code, message = _bridge_error(response)
            database.fail_selection_search_run(
                run_id,
                code,
                blocked=code in BRIDGE_BLOCKING_CODES,
                http_status=http_status,
            )
            raise SelectionCollectionError(run_id, code, message)

        payload: Any = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise ValueError("选品浏览器桥返回格式无效")
        source = payload.get("source")
        if isinstance(source, dict):
            source_api = _safe_source_api(source.get("request_url"))
            source_status = source.get("http_status")
            if isinstance(source_status, int):
                http_status = source_status
        items = payload.get("items")
        if not isinstance(items, list):
            raise ValueError("选品浏览器桥未返回商品列表")

        run = database.complete_selection_search_run(
            run_id,
            [item for item in items if isinstance(item, dict)],
            source_api=source_api,
            http_status=http_status,
        )
        return {
            "ok": True,
            "run_id": run_id,
            "keyword": normalized_keyword,
            "page": 1,
            "result_count": int(run.get("result_count") or 0),
            "source_api": source_api,
            "http_status": http_status,
        }
    except SelectionCollectionError:
        raise
    except Exception as exc:
        database.fail_selection_search_run(
            run_id,
            type(exc).__name__.upper(),
            source_api=source_api,
            http_status=http_status,
        )
        raise SelectionCollectionError(run_id, type(exc).__name__.upper(), str(exc)) from exc
    finally:
        if owns_client:
            await http_client.aclose()
