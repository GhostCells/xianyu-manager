from __future__ import annotations

import os
import secrets
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl, model_validator

from .auto_reply import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    SUPPORTED_MODELS,
    siliconflow_chat_endpoint,
)
from .config import load_settings
from .database import Database
from .delivery import DeliveryService, listing_item_id
from .fulfillment_rules import delivery_issues
from .knowledge import load_knowledge_folder
from .scanner import scan_library
from .security import SecretStore
from .runtime_policy import RuntimePolicy
from .selection_bridge import SelectionBridgeError
from .session import BrowserSessionManager, normalize_listing_url


settings = load_settings()
runtime_policy = RuntimePolicy(settings.safe_mode)
database = Database(settings.database_path, safe_mode=settings.safe_mode)
secret_store = SecretStore(settings.auto_reply_secret_path)
session_manager = BrowserSessionManager(
    settings.browser_profiles_dir,
    settings.browser_executable,
    database,
    browser_headless=settings.browser_headless,
    runtime_policy=runtime_policy,
)
delivery_service = DeliveryService(
    settings.browser_profiles_dir,
    settings.browser_executable,
    database,
    secret_store,
    session_manager=session_manager,
    runtime_policy=runtime_policy,
)


def refresh_products() -> list[dict[str, object]]:
    scanned = scan_library(settings.product_library, settings.validator_path)
    database.sync_products(scanned, safe_mode=runtime_policy.safe_mode)
    if runtime_policy.safe_mode:
        return database.list_products(allow_no_account=True)
    database.ensure_default_accounts()
    database.ensure_legacy_product_policy()
    account = database.enforce_single_account_mode("七月账号")
    database.reconcile_configured_listings(int(account["id"]))
    return database.list_products()


@asynccontextmanager
async def lifespan(_: FastAPI):
    refresh_products()
    if not runtime_policy.safe_mode:
        await delivery_service.start_if_enabled()
    try:
        yield
    finally:
        if not runtime_policy.safe_mode:
            await delivery_service.shutdown()
            await session_manager.shutdown()


app = FastAPI(title="闲鱼本地管理系统", version="0.5.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")

# Explicitly reviewed local reads only. In particular /api/session is NOT a
# read-only route: its snapshot can restore bindings and inspect cookies.
SAFE_READ_PATHS = frozenset({"/", "/api/health", "/api/accounts", "/api/products"})


@app.middleware("http")
async def local_request_guard(request: Request, call_next):
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    host = (request.url.hostname or "").lower()
    if host not in loopback_hosts:
        return JSONResponse(status_code=403, content={"detail": "管理系统只允许本机访问"})

    if runtime_policy.safe_mode and not (
        request.method in {"GET", "HEAD"}
        and (request.url.path in SAFE_READ_PATHS or request.url.path.startswith("/static/"))
    ):
        return JSONResponse(status_code=403, content={
            "error_code": "SAFE_MODE_OPERATION_BLOCKED",
            "detail": runtime_policy.error_message,
        })

    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        origin = str(request.headers.get("origin") or "").strip()
        referer = str(request.headers.get("referer") or "").strip()
        source = origin or referer
        if source and (urlparse(source).hostname or "").lower() not in loopback_hosts:
            return JSONResponse(status_code=403, content={"detail": "已拒绝跨站操作"})
        if str(request.headers.get("sec-fetch-site") or "").lower() == "cross-site":
            return JSONResponse(status_code=403, content={"detail": "已拒绝跨站操作"})

    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response


class ProductUpdate(BaseModel):
    enabled_for_account: bool | None = None
    share_url: HttpUrl | None = None
    share_code: str | None = Field(default=None, max_length=16)
    share_verified: bool | None = None
    suggested_price_cents: int | None = Field(default=None, ge=0)
    confirmed_price_cents: int | None = Field(default=None, ge=0)
    listing_url: HttpUrl | None = None
    listing_status: str | None = Field(default=None, pattern="^(draft|ready|published|paused)$")


class KnowledgeFolderUpdate(BaseModel):
    path: str = Field(min_length=1, max_length=1200)


class DeliveryStart(BaseModel):
    auto_confirm_platform: bool = True
    auto_free_group: bool = False
    initial_delay_seconds: int = Field(default=0, ge=0, le=3600)


class DeliveryReconcile(BaseModel):
    order_id: str = Field(pattern=r"^\d{10,}$")


class AutoReplySettingsUpdate(BaseModel):
    enabled: bool = False
    base_url: str = Field(default=DEFAULT_BASE_URL, max_length=200)
    model: str = Field(default=DEFAULT_MODEL, max_length=80)
    api_key: str | None = Field(default=None, max_length=300)
    system_prompt: str = Field(default=DEFAULT_SYSTEM_PROMPT, max_length=3000)
    min_delay_seconds: int = Field(default=5, ge=2, le=120)
    max_delay_seconds: int = Field(default=12, ge=2, le=180)
    max_reply_chars: int = Field(default=180, ge=40, le=300)
    manual_takeover_hours: int = Field(default=12, ge=1, le=168)

    @model_validator(mode="after")
    def validate_range(self) -> "AutoReplySettingsUpdate":
        if self.max_delay_seconds < self.min_delay_seconds:
            raise ValueError("最大回复延迟不能小于最小回复延迟")
        if self.model not in SUPPORTED_MODELS:
            raise ValueError(
                "模型必须选择 deepseek-ai/DeepSeek-V4-Flash 或 "
                "deepseek-ai/DeepSeek-V4-Pro"
            )
        siliconflow_chat_endpoint(self.base_url)
        return self


class AutoReplyTest(BaseModel):
    api_key: str | None = Field(default=None, max_length=300)
    base_url: str = Field(default=DEFAULT_BASE_URL, max_length=200)
    model: str = Field(default=DEFAULT_MODEL, max_length=80)
    system_prompt: str = Field(default=DEFAULT_SYSTEM_PROMPT, max_length=3000)
    message: str = Field(default="你好，请问这个商品适合新手吗？", min_length=1, max_length=500)
    max_reply_chars: int = Field(default=180, ge=40, le=300)
    listing_item_id: str | None = Field(default=None, pattern=r"^\d{10,}$")


class AutomationSafetyUpdate(BaseModel):
    max_replies_per_hour: int = Field(default=20, ge=1, le=120)
    max_replies_per_day: int = Field(default=100, ge=1, le=1000)
    max_deliveries_per_hour: int = Field(default=15, ge=1, le=100)
    min_outbound_interval_seconds: int = Field(default=5, ge=2, le=120)
    risk_cooldown_minutes: int = Field(default=30, ge=5, le=180)
    notifications_enabled: bool = True

    @model_validator(mode="after")
    def validate_reply_limits(self) -> "AutomationSafetyUpdate":
        if self.max_replies_per_day < self.max_replies_per_hour:
            raise ValueError("每日自动回复上限不能小于每小时上限")
        return self


class ListingDeliveryUpdate(BaseModel):
    share_url: HttpUrl
    share_code: str | None = Field(default=None, max_length=16)
    share_verified: bool | None = None

    @model_validator(mode="after")
    def validate_baidu_share(self) -> "ListingDeliveryUpdate":
        parsed = urlparse(str(self.share_url))
        if parsed.scheme != "https" or parsed.hostname != "pan.baidu.com" or not parsed.path.startswith("/s/"):
            raise ValueError("交付链接必须是百度网盘 HTTPS 分享地址")
        return self


class ListingProductMapping(BaseModel):
    product_dir_name: str = Field(min_length=1, max_length=180)


class SelectionSearchRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=40)
    page: int = Field(default=1, ge=1, le=1)
    limit: int = Field(default=30, ge=1, le=30)


class SelectionDetailRequest(BaseModel):
    item_id: str | None = Field(default=None, max_length=40)
    url: HttpUrl | None = None
    observation_run_id: str | None = Field(default=None, max_length=64)
    observation_keyword: str | None = Field(default=None, max_length=40)
    observation_search_rank: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_target(self):
        if not str(self.item_id or "").strip() and self.url is None:
            raise ValueError("item_id 和 url 至少填写一个")
        context = (
            self.observation_run_id,
            self.observation_keyword,
            self.observation_search_rank,
        )
        if any(value is not None for value in context) and not all(
            value is not None for value in context
        ):
            raise ValueError("详情观测上下文必须同时提供 run_id、keyword 和 search_rank")
        return self


def _selection_bridge_token() -> str:
    try:
        return settings.selection_bridge_token_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@app.get("/")
def index() -> FileResponse:
    return FileResponse(settings.static_dir / "index.html")


@app.post("/api/internal/selection/search")
async def internal_selection_search(
    body: SelectionSearchRequest,
    request: Request,
    x_internal_token: str = Header(default="", alias="X-Internal-Token"),
) -> dict[str, object]:
    if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="选品浏览器桥只允许本机调用")
    expected_token = _selection_bridge_token()
    if not expected_token or not secrets.compare_digest(x_internal_token, expected_token):
        raise HTTPException(status_code=401, detail="选品浏览器桥密钥无效")
    account = database.get_active_account()
    try:
        return await session_manager.search_listings(
            int(account["id"]),
            body.keyword.strip(),
            page_number=body.page,
            limit=body.limit,
        )
    except SelectionBridgeError as exc:
        status_code = 409 if exc.code in {
            "SELECTION_BUSY", "LOGIN_REQUIRED", "VERIFICATION_REQUIRED"
        } else 504 if exc.code in {
            "NAVIGATION_TIMEOUT", "SEARCH_RESPONSE_TIMEOUT", "SEARCH_TIMEOUT"
        } else 502
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "stage": exc.stage, "message": exc.message},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/internal/selection/detail")
async def internal_selection_detail(
    body: SelectionDetailRequest,
    request: Request,
    x_internal_token: str = Header(default="", alias="X-Internal-Token"),
) -> dict[str, object]:
    if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="详情采集只允许本机调用")
    expected_token = _selection_bridge_token()
    if not expected_token or not secrets.compare_digest(x_internal_token, expected_token):
        raise HTTPException(status_code=401, detail="详情采集密钥无效")

    raw_item_id = str(body.item_id or "").strip()
    raw_url = str(body.url or "").strip()
    if raw_url:
        normalized = normalize_listing_url(raw_url)
        if normalized is None:
            raise HTTPException(status_code=422, detail="商品链接格式无效")
        url_item_id, item_url = normalized
        if raw_item_id and raw_item_id != url_item_id:
            raise HTTPException(status_code=422, detail="item_id 与商品链接不一致")
        item_id = url_item_id
    else:
        if not raw_item_id.isdigit():
            raise HTTPException(status_code=422, detail="item_id 格式无效")
        item_id = raw_item_id
        item_url = f"https://www.goofish.com/item?id={item_id}"

    if database.get_selection_item(item_id) is None:
        raise HTTPException(status_code=404, detail="候选商品不存在，请先执行搜索入库")
    account = database.get_active_account()
    try:
        result = await session_manager.collect_listing_detail(int(account["id"]), item_url)
        detail = result["detail"]
        snapshot = database.save_selection_item_snapshot(
            item_id,
            price_cents=detail.get("price_cents"),
            price_text=str(detail.get("price_text") or ""),
            want_count=detail.get("want_count"),
            browse_count=detail.get("browse_count"),
            collect_count=detail.get("collect_count"),
            observation_run_id=body.observation_run_id,
            observation_keyword=body.observation_keyword,
            observation_search_rank=body.observation_search_rank,
        )
        return {
            "ok": True,
            "item_id": item_id,
            "source": result["source"],
            "detail": detail,
            "snapshot": snapshot,
        }
    except SelectionBridgeError as exc:
        status_code = 409 if exc.code in {
            "SELECTION_BUSY", "LOGIN_REQUIRED", "VERIFICATION_REQUIRED"
        } else 504 if exc.code in {
            "DETAIL_NAVIGATION_TIMEOUT", "DETAIL_RESPONSE_TIMEOUT", "DETAIL_TIMEOUT"
        } else 502
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "stage": exc.stage, "message": exc.message},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/health")
def health() -> dict[str, object]:
    if runtime_policy.safe_mode:
        products = database.list_products(allow_no_account=True)
        with database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM accounts WHERE is_active=1 AND is_archived=0"
            ).fetchone()
        return {
            "status": "ok", "safe_mode": True, "automation_allowed": False,
            "product_library": str(settings.product_library),
            "database": str(settings.database_path),
            "active_account": database.get_account(int(row[0])) if row else None,
            "next_product_number": max((int(p["number"]) for p in products), default=0) + 1,
        }
    products = database.list_products()
    return {
        "status": "ok",
        "safe_mode": False,
        "automation_allowed": True,
        "product_library": str(settings.product_library),
        "database": str(settings.database_path),
        "active_account": database.get_active_account(),
        "next_product_number": max((int(item["number"]) for item in products), default=0) + 1,
    }


@app.get("/api/operation/selection-candidates")
def operation_selection_candidates(limit: int = 50) -> dict[str, object]:
    items = database.list_operation_candidates(limit=limit)
    return {
        "schema_version": "selection-candidates-v1",
        "count": len(items),
        "items": items,
    }


@app.get("/api/operation/selection-tracking/{item_id}")
def operation_selection_tracking(item_id: str) -> dict[str, object]:
    try:
        return database.get_selection_tracking_diagnostics(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/accounts")
def accounts() -> list[dict[str, object]]:
    return database.list_accounts()


@app.get("/api/session")
async def session_status() -> dict[str, object]:
    snapshot = await session_manager.snapshot()
    delivery = delivery_service.snapshot()
    snapshot["message_ready"] = delivery.get("status") == "listening"
    snapshot["delivery_status"] = delivery.get("status")
    return snapshot


@app.post("/api/session/start")
async def start_session_binding() -> dict[str, object]:
    account = database.get_active_account()
    try:
        await delivery_service.stop(persist=False)
        return await session_manager.start_login(int(account["id"]))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/session/confirm")
async def confirm_session_binding() -> dict[str, object]:
    account = database.get_active_account()
    try:
        session = await session_manager.confirm_login(int(account["id"]))
        session["delivery"] = await _resume_configured_automation(int(account["id"]))
        return session
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _resume_configured_automation(account_id: int) -> dict[str, object]:
    account = database.get_account(account_id) or {}
    auto_reply = database.get_auto_reply_settings(account_id)
    if account.get("delivery_enabled"):
        return await delivery_service.start(account_id, persist=False)
    if auto_reply.get("enabled"):
        return await delivery_service.start_auto_reply(account_id)
    return delivery_service.snapshot()


@app.post("/api/session/sync")
async def sync_session_binding() -> dict[str, object]:
    account = database.get_active_account()
    account_id = int(account["id"])
    try:
        session = await session_manager.sync_session(account_id)
        await delivery_service.stop(persist=False)
        session["delivery"] = await _resume_configured_automation(account_id)
        return session
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/session/cancel")
async def cancel_session_binding() -> dict[str, object]:
    account = database.get_active_account()
    return await session_manager.cancel_login(int(account["id"]))


@app.get("/api/delivery")
def delivery_status() -> dict[str, object]:
    account = database.get_active_account()
    snapshot = delivery_service.snapshot()
    reply_settings = database.get_auto_reply_settings(int(account["id"]))
    snapshot["enabled"] = bool(account.get("delivery_enabled"))
    snapshot["auto_reply_enabled"] = bool(reply_settings.get("enabled"))
    snapshot["auto_confirm_platform"] = bool(account.get("auto_confirm_delivery"))
    snapshot["auto_free_group"] = bool(account.get("auto_free_group"))
    snapshot["ready_product_count"] = sum(
        bool(
            item.get("enabled_for_account")
            and item.get("listing_status") == "published"
            and not delivery_issues(item)
            and listing_item_id(str(item.get("listing_url") or ""))
        )
        for item in database.list_products(int(account["id"]))
    )
    return snapshot


@app.post("/api/delivery/start")
async def start_delivery(payload: DeliveryStart) -> dict[str, object]:
    account = database.get_active_account()
    try:
        return await delivery_service.start(
            int(account["id"]),
            auto_confirm=payload.auto_confirm_platform,
            auto_free_group=payload.auto_free_group,
            initial_delay_seconds=payload.initial_delay_seconds,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/delivery/probe")
async def probe_delivery() -> dict[str, object]:
    account = database.get_active_account()
    try:
        return await delivery_service.probe(int(account["id"]))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/delivery/reconcile-preview")
async def preview_delivery_reconciliation() -> dict[str, object]:
    try:
        return await delivery_service.preview_recent_orders()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/delivery/reconcile")
async def reconcile_delivery(payload: DeliveryReconcile) -> dict[str, object]:
    try:
        return await delivery_service.reconcile_order(payload.order_id)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/delivery/stop")
async def stop_delivery() -> dict[str, object]:
    return await delivery_service.stop()


def auto_reply_payload(account_id: int) -> dict[str, object]:
    reply_settings = database.get_auto_reply_settings(account_id)
    if not str(reply_settings.get("system_prompt") or "").strip():
        reply_settings["system_prompt"] = DEFAULT_SYSTEM_PROMPT
    runtime = delivery_service.snapshot()
    published_products = [
        item
        for item in database.list_products(account_id, include_listing_only=True)
        if item.get("enabled_for_account") and item.get("listing_status") == "published"
    ]
    knowledge_ready = [
        item for item in published_products if int(item.get("knowledge_chars") or 0) > 0
    ]
    return {
        "settings": reply_settings,
        "has_api_key": secret_store.has_secret(),
        "connected": runtime.get("status") == "listening",
        "runtime_status": runtime.get("status"),
        "last_reply_at": runtime.get("last_auto_reply_at"),
        "last_error": runtime.get("last_auto_reply_error") or "",
        "knowledge": {
            "ready": len(knowledge_ready),
            "total": len(published_products),
            "missing": [str(item.get("name") or item.get("title") or "未命名商品") for item in published_products if int(item.get("knowledge_chars") or 0) <= 0][:8],
        },
        "records": database.list_auto_reply_records(account_id),
    }


@app.get("/api/auto-reply")
def auto_reply_status() -> dict[str, object]:
    account = database.get_active_account()
    return auto_reply_payload(int(account["id"]))


@app.put("/api/auto-reply/settings")
async def update_auto_reply_settings(
    payload: AutoReplySettingsUpdate,
) -> dict[str, object]:
    account = database.get_active_account()
    account_id = int(account["id"])
    if payload.api_key and payload.api_key.strip():
        try:
            secret_store.save(payload.api_key)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=f"API Key 加密保存失败：{exc}") from exc
    if payload.enabled and not secret_store.has_secret():
        raise HTTPException(status_code=422, detail="启用自动回复前请先填写硅基流动 API Key")

    values = payload.model_dump(exclude={"api_key"})
    database.update_auto_reply_settings(account_id, values)
    runtime_error = ""
    try:
        if not payload.enabled or account.get("binding_status") == "bound":
            await delivery_service.sync_auto_reply_runtime(account_id)
    except (ValueError, RuntimeError) as exc:
        runtime_error = str(exc)
    result = auto_reply_payload(account_id)
    if runtime_error:
        result["runtime_error"] = runtime_error
    return result


@app.post("/api/auto-reply/test")
async def test_auto_reply(payload: AutoReplyTest) -> dict[str, object]:
    if payload.model not in SUPPORTED_MODELS:
        raise HTTPException(status_code=422, detail="不支持的硅基流动模型")
    try:
        siliconflow_chat_endpoint(payload.base_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    api_key = str(payload.api_key or "").strip()
    if not api_key:
        try:
            api_key = secret_store.load()
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=500, detail=f"API Key 解密失败：{exc}") from exc
    if not api_key:
        raise HTTPException(status_code=422, detail="请先填写或保存硅基流动 API Key")

    account = database.get_active_account()
    account_id = int(account["id"])
    product = None
    if payload.listing_item_id:
        product = database.get_product_by_listing_item_id(payload.listing_item_id, account_id)
        if product is None:
            raise HTTPException(status_code=422, detail="测试商品未匹配到当前账号的本地商品资料")
    if product is None:
        product = next(
            (
                item
                for item in database.list_products(account_id)
                if item.get("enabled_for_account")
                and item.get("listing_status") == "published"
                and int(item.get("knowledge_chars") or 0) > 0
            ),
            {
                "name": "AI 数字商品",
                "title": "AI 工具与配套使用说明",
                "confirmed_price_cents": 19900,
                "listing_status": "published",
            },
        )
    try:
        decision = await delivery_service.reply_client.generate(
            api_key=api_key,
            base_url=payload.base_url,
            model=payload.model,
            system_prompt=payload.system_prompt,
            buyer_message=payload.message,
            product=product,
            context=[],
            max_reply_chars=payload.max_reply_chars,
        )
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "ok": True,
        "action": decision.action,
        "reply": decision.reply,
        "reason": decision.reason,
        "product": str(product.get("name") or product.get("title") or "当前商品"),
        "listing_item_id": payload.listing_item_id or "",
    }


@app.post("/api/auto-reply/chats/{chat_id}/resume")
def resume_chat_auto_reply(chat_id: str) -> dict[str, object]:
    normalized = chat_id.strip()
    if not normalized.isdigit() or len(normalized) < 5:
        raise HTTPException(status_code=422, detail="会话 ID 格式不正确")
    account = database.get_active_account()
    database.set_chat_manual(int(account["id"]), normalized, enabled=False)
    return auto_reply_payload(int(account["id"]))


@app.get("/api/automation-safety")
def automation_safety_status() -> dict[str, object]:
    account = database.get_active_account()
    result = database.get_automation_status(int(account["id"]))
    result["runtime_status"] = delivery_service.snapshot().get("status")
    return result


@app.put("/api/automation-safety")
def update_automation_safety(
    payload: AutomationSafetyUpdate,
) -> dict[str, object]:
    account = database.get_active_account()
    try:
        database.update_automation_safety(int(account["id"]), payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return automation_safety_status()


@app.post("/api/automation-safety/circuit/reset")
async def reset_automation_circuit() -> dict[str, object]:
    account = database.get_active_account()
    account_id = int(account["id"])
    database.clear_automation_circuit(account_id)
    if delivery_service.snapshot().get("status") == "cooldown":
        await delivery_service.stop(persist=False)
        await delivery_service.start_if_enabled()
    return automation_safety_status()


@app.get("/api/orders")
def orders() -> list[dict[str, object]]:
    return database.list_orders()


@app.get("/api/listings")
def listings() -> list[dict[str, object]]:
    return database.list_live_listings()


@app.put("/api/listings/{item_id}/delivery")
def configure_listing_delivery(
    item_id: str, payload: ListingDeliveryUpdate
) -> dict[str, object]:
    account = database.get_active_account()
    try:
        return database.configure_listing_delivery(
            int(account["id"]),
            item_id,
            share_url=str(payload.share_url),
            share_code=payload.share_code,
            share_verified=payload.share_verified,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/listings/{item_id}/product")
def map_listing_product(
    item_id: str, payload: ListingProductMapping
) -> dict[str, object]:
    account = database.get_active_account()
    safe_name = Path(payload.product_dir_name).name
    if safe_name != payload.product_dir_name:
        raise HTTPException(status_code=422, detail="本地商品名称格式不正确")
    try:
        return database.map_live_listing_to_product(
            int(account["id"]), item_id, safe_name
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/accounts/{account_id}/activate")
def activate_account(account_id: int) -> dict[str, object]:
    account = database.activate_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"account": account, "products": database.list_products(account_id)}


@app.get("/api/products")
def products() -> list[dict[str, object]]:
    return database.list_products(allow_no_account=runtime_policy.safe_mode)


@app.post("/api/scan")
async def scan() -> dict[str, object]:
    refresh_products()
    listing_sync: dict[str, object]
    try:
        remote = await delivery_service.refresh_live_listings()
        listing_sync = {
            "ok": True,
            "remote_count": int(remote.get("remote_count") or 0),
            "new_count": int(remote.get("new_count") or 0),
            "matched_count": int(remote.get("matched_count") or 0),
            "synced_at": str(remote.get("synced_at") or ""),
            "error": "",
        }
    except RuntimeError as exc:
        listing_sync = {
            "ok": False,
            "remote_count": 0,
            "new_count": 0,
            "matched_count": 0,
            "synced_at": "",
            "error": str(exc)[:300],
        }
    products = database.list_products()
    listings = database.list_live_listings()
    return {
        "count": len(products),
        "passed": sum(item["quality_status"] == "passed" for item in products),
        "products": products,
        "listing_count": len(listings),
        "listings": listings,
        "listing_sync": listing_sync,
    }


@app.get("/api/products/{dir_name}/copy", response_class=PlainTextResponse)
def product_copy(dir_name: str) -> str:
    safe_name = Path(unquote(dir_name)).name
    copy_path = settings.product_library / safe_name / "发布文案.txt"
    if not copy_path.is_file():
        raise HTTPException(status_code=404, detail="发布文案不存在")
    return copy_path.read_text(encoding="utf-8-sig")


def _load_product_knowledge(safe_name: str, folder_path: str) -> dict[str, object]:
    if database.get_product(safe_name) is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    try:
        result = load_knowledge_folder(folder_path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    updated = database.set_product_knowledge_folder(
        safe_name,
        source_path=result.source_path,
        knowledge_text=result.text,
        knowledge_hash=result.content_hash,
        knowledge_chars=result.chars,
        file_count=result.file_count,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    return {
        "product": updated,
        "file_count": result.file_count,
        "chars": result.chars,
        "skipped_files": result.skipped_files,
        "warnings": list(result.warnings),
    }


@app.put("/api/products/{dir_name}/knowledge-folder")
def update_product_knowledge_folder(
    dir_name: str,
    payload: KnowledgeFolderUpdate,
) -> dict[str, object]:
    return _load_product_knowledge(Path(unquote(dir_name)).name, payload.path)


@app.post("/api/products/{dir_name}/knowledge-folder/pick")
def pick_product_knowledge_folder(dir_name: str) -> dict[str, object]:
    safe_name = Path(unquote(dir_name)).name
    current = database.get_product(safe_name)
    if current is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    if os.name != "nt":
        raise HTTPException(
            status_code=501,
            detail="当前服务器不支持本地 GUI 目录选择；请通过已配置的服务器绝对路径加载资料",
        )
    env = os.environ.copy()
    env["XIANYU_INITIAL_FOLDER"] = str(
        current.get("knowledge_source_path") or (settings.product_library / safe_name)
    )
    script = """
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择用于自动回复的商品资料文件夹'
$dialog.ShowNewFolderButton = $false
if (Test-Path -LiteralPath $env:XIANYU_INITIAL_FOLDER) {
  $dialog.SelectedPath = $env:XIANYU_INITIAL_FOLDER
}
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
  Write-Output $dialog.SelectedPath
}
"""
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=408, detail="选择文件夹超时，请重新操作") from exc
    selected = str(completed.stdout or "").strip()
    if not selected:
        if completed.returncode:
            raise HTTPException(status_code=500, detail="无法打开文件夹选择窗口")
        return {"cancelled": True, "product": current}
    return _load_product_knowledge(safe_name, selected)


@app.delete("/api/products/{dir_name}/knowledge-folder")
def clear_product_knowledge_folder(dir_name: str) -> dict[str, object]:
    safe_name = Path(unquote(dir_name)).name
    if not database.clear_product_knowledge_folder(safe_name):
        raise HTTPException(status_code=404, detail="商品不存在")
    refresh_products()
    product = database.get_product(safe_name)
    if product is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    return {"product": product}


@app.get("/api/products/{dir_name}/image")
def product_image(dir_name: str) -> FileResponse:
    safe_name = Path(unquote(dir_name)).name
    image_dir = settings.product_library / safe_name / "图片"
    image_path = next(
        (path for path in sorted(image_dir.glob("*.png")) if path.is_file()),
        None,
    )
    if image_path is None:
        raise HTTPException(status_code=404, detail="商品图片不存在")
    return FileResponse(image_path)


@app.post("/api/products/{dir_name}/detect-listing")
async def detect_product_listing(dir_name: str) -> dict[str, object]:
    safe_name = Path(unquote(dir_name)).name
    current = database.get_product(safe_name)
    if current is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    if not current["enabled_for_account"]:
        raise HTTPException(status_code=409, detail="该商品尚未加入当前账号")
    if current["quality_status"] != "passed":
        raise HTTPException(status_code=409, detail="商品质检未通过，不能进行发布检测")
    if delivery_issues(current):
        raise HTTPException(status_code=409, detail="请先完成百度网盘链接验证")

    account = database.get_active_account()
    try:
        result = await session_manager.detect_listing(
            int(account["id"]),
            str(current["title"] or current["name"]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if result["matched"]:
        listing = result["listing"]
        updated = database.update_product(
            safe_name,
            {"listing_url": listing["url"], "listing_status": "published"},
        )
        result["product"] = updated
    else:
        result["product"] = current
    return result


class ShareConfirmation(BaseModel):
    fingerprint: str = Field(pattern="^[0-9a-f]{64}$")


@app.post("/api/products/{dir_name}/verify-share")
def verify_share(dir_name: str, payload: ShareConfirmation) -> dict[str, object]:
    try:
        database.confirm_product_share(Path(unquote(dir_name)).name, payload.fingerprint)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "verification_method": "user_confirmation", "online_checked": False}


@app.patch("/api/products/{dir_name}")
def update_product(dir_name: str, payload: ProductUpdate) -> dict[str, object]:
    fields = payload.model_dump(exclude_unset=True)
    for key in ("share_url", "listing_url"):
        if key in fields:
            fields[key] = str(fields[key]) if fields[key] is not None else ""
    current = database.get_product(Path(unquote(dir_name)).name)
    if current is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    try:
        updated = database.update_product(Path(unquote(dir_name)).name, fields)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    return updated
