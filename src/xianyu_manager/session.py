from __future__ import annotations

import asyncio
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .database import Database
from .browser_launch import sandbox_options
from .fulfillment_rules import parse_listing_id
from .runtime_policy import PROCESS_POLICY, RuntimePolicy, RuntimeOperationBlocked, business_operation, login_operation
from .profile_lock import ProfileOwnerLock
from .selection_bridge import (
    SelectionBridgeError,
    SelectionBusyError,
    capture_search_json,
    normalize_search_items,
)
from .selection_detail import capture_detail_json, normalize_detail


GOOFISH_HOME = "https://www.goofish.com/"
GOOFISH_LOGIN_TARGET = "https://www.goofish.com/im"
IDENTITY_COOKIE_NAMES = {"unb", "lgc", "tracknick"}
SESSION_COOKIE_NAMES = {"cookie2", "sgcookie", "_tb_token_"}
MESSAGE_SESSION_COOKIE_NAMES = {"cookie2", "_tb_token_"}


def normalize_listing_url(raw_url: str) -> tuple[str, str] | None:
    item_id = parse_listing_id(raw_url)
    if not item_id:
        return None
    normalized = urlunparse(("https", "www.goofish.com", "/item", "", urlencode({"id": item_id}), ""))
    return item_id, normalized


def match_listing_candidates(raw_items: list[dict[str, str]], expected_title: str) -> dict[str, object]:
    expected = "".join(expected_title.split()).lower()
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_items:
        normalized = normalize_listing_url(str(raw.get("url", "")))
        if normalized is None:
            continue
        item_id, url = normalized
        if item_id in seen:
            continue
        seen.add(item_id)
        text = " ".join(str(raw.get("text", "")).split())[:500]
        compact = "".join(text.split()).lower()
        candidates.append({"item_id": item_id, "url": url, "text": text})
        if expected and expected in compact:
            return {"matched": True, "listing": candidates[-1], "candidates": candidates[:10]}
    return {"matched": False, "listing": None, "candidates": candidates[:10]}


class BrowserSessionManager:
    """Manage one user-confirmed Xianyu browser session at a time.

    Chromium owns the persistent profile and uses the Windows user context for
    its cookie database. Cookie values never enter SQLite, logs, or API output.
    """

    def __init__(
        self,
        profiles_dir: Path,
        browser_executable: Path | None,
        database: Database,
        *,
        browser_headless: bool = False,
        runtime_policy: RuntimePolicy = PROCESS_POLICY,
    ) -> None:
        self.runtime_policy = runtime_policy
        self.profiles_dir = profiles_dir
        if not runtime_policy.safe_mode:
            self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.browser_executable = browser_executable
        self.database = database
        self.browser_headless = browser_headless
        self._lock = asyncio.Lock()
        self._selection_lock = asyncio.Lock()
        self._detail_lock = asyncio.Lock()
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._account_id: int | None = None
        self._previous_status = "unbound"
        self._handoff_account_id: int | None = None
        self._handoff_storage_state: dict[str, object] | None = None
        self._profile_lock: ProfileOwnerLock | None = None

    @business_operation
    async def search_listings(
        self,
        account_id: int,
        keyword: str,
        *,
        page_number: int = 1,
        limit: int = 30,
    ) -> dict[str, object]:
        if page_number != 1:
            raise ValueError("第一阶段只支持搜索第一页")
        acquired = False
        selection_page: Any | None = None
        try:
            try:
                await asyncio.wait_for(self._selection_lock.acquire(), timeout=2)
                acquired = True
            except asyncio.TimeoutError as exc:
                raise SelectionBusyError() from exc

            async with asyncio.timeout(45):
                # Selection is a guest of the already-running manager session.
                # It must never launch or replace the account browser.
                async with self._lock:
                    if self._context is None or self._account_id != account_id:
                        raise SelectionBridgeError(
                            "BROWSER_NOT_RUNNING",
                            "acquire_context",
                            "闲鱼专用浏览器尚未由管理系统启动",
                        )
                    if not await self._context_is_alive():
                        raise SelectionBridgeError(
                            "BROWSER_NOT_RUNNING",
                            "acquire_context",
                            "闲鱼专用浏览器连接已经关闭",
                        )
                    context = self._context
                selection_page = await context.new_page()
                payload, source = await capture_search_json(
                    selection_page,
                    keyword=keyword,
                    timeout_seconds=30,
                )
                items = normalize_search_items(payload, limit)
                return {
                    "ok": True,
                    "keyword": keyword,
                    "page": page_number,
                    "source": source,
                    "total": len(items),
                    "items": items,
                }
        except TimeoutError as exc:
            raise SelectionBridgeError(
                "SEARCH_TIMEOUT", "selection_search", "选品搜索总耗时超过 45 秒"
            ) from exc
        finally:
            if selection_page is not None:
                try:
                    if not selection_page.is_closed():
                        await selection_page.close()
                except Exception:
                    pass
            if acquired:
                self._selection_lock.release()

    @business_operation
    async def collect_listing_detail(
        self,
        account_id: int,
        item_url: str,
    ) -> dict[str, object]:
        acquired = False
        detail_page: Any | None = None
        try:
            try:
                await asyncio.wait_for(self._detail_lock.acquire(), timeout=2)
                acquired = True
            except asyncio.TimeoutError as exc:
                raise SelectionBusyError() from exc

            async with asyncio.timeout(45):
                async with self._lock:
                    if self._context is None or self._account_id != account_id:
                        raise SelectionBridgeError(
                            "BROWSER_NOT_RUNNING",
                            "acquire_detail_context",
                            "闲鱼专用浏览器尚未由管理系统启动",
                        )
                    if not await self._context_is_alive():
                        raise SelectionBridgeError(
                            "BROWSER_NOT_RUNNING",
                            "acquire_detail_context",
                            "闲鱼专用浏览器连接已经关闭",
                        )
                    context = self._context
                detail_page = await context.new_page()
                payload, source = await capture_detail_json(
                    detail_page,
                    item_url=item_url,
                    timeout_seconds=30,
                )
                return {
                    "ok": True,
                    "source": source,
                    "detail": normalize_detail(payload),
                }
        except TimeoutError as exc:
            raise SelectionBridgeError(
                "DETAIL_TIMEOUT", "detail_collection", "详情采集总耗时超过45秒"
            ) from exc
        finally:
            if detail_page is not None:
                try:
                    if not detail_page.is_closed():
                        await detail_page.close()
                except Exception:
                    pass
            if acquired:
                self._detail_lock.release()

    @login_operation
    def profile_dir(self, account_id: int) -> Path:
        browser_name = (
            PureWindowsPath(str(self.browser_executable)).stem.lower()
            if self.browser_executable
            else "chromium"
        )
        return self.profiles_dir / browser_name / f"account-{account_id}"

    @login_operation
    async def _launch_visible_browser(self, account_id: int) -> None:
        from playwright.async_api import async_playwright

        profile_dir = self.profile_dir(account_id)
        profile_lock = ProfileOwnerLock(profile_dir)
        profile_lock.acquire()
        self._profile_lock = profile_lock
        try:
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                **sandbox_options(),
                user_data_dir=str(profile_dir),
                executable_path=str(self.browser_executable),
                headless=self.browser_headless,
                no_viewport=True,
                args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
            )
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            self._account_id = account_id
        except Exception:
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception:
                    pass
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
            self._context = None
            self._playwright = None
            profile_lock.release()
            self._profile_lock = None
            raise

    @login_operation
    async def _context_is_alive(self) -> bool:
        """Return whether the remembered browser context still has a live browser.

        Closing the last visible Chrome window does not immediately clear the
        Python references.  A lightweight cookie read crosses the Playwright
        connection and reliably fails once that browser/context has closed.
        """
        if self._context is None:
            return False
        try:
            await self._context.cookies([GOOFISH_HOME])
            return True
        except Exception:
            return False

    @login_operation
    async def _ensure_live_page(self) -> None:
        """Point ``self._page`` at an open page, creating one when necessary."""
        if self._context is None:
            raise RuntimeError("专用浏览器尚未启动")
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    return
            except Exception:
                pass
        try:
            for page in self._context.pages:
                if not page.is_closed():
                    self._page = page
                    return
            self._page = await self._context.new_page()
        except Exception as exc:
            raise RuntimeError("专用浏览器窗口已经关闭") from exc

    @business_operation
    async def ensure_runtime_context(self, account_id: int) -> Any:
        """Return the dedicated visible Chrome context used by automation.

        The browser profile remains owned by this manager. Callers may read the
        current session in memory, but must not close the context or export its
        cookies.
        """
        async with self._lock:
            account = self.database.get_account(account_id)
            if account is None or not account["is_active"]:
                raise ValueError("只能连接当前账号")
            if self.browser_executable is None or not self.browser_executable.is_file():
                raise RuntimeError("没有找到 Chrome 或 Edge")
            if self._context is not None:
                if await self._context_is_alive():
                    if self._account_id != account_id:
                        raise RuntimeError("另一个账号正在使用专用浏览器")
                    await self._ensure_live_page()
                    return self._context
                # The user may have closed the visible Chrome window manually.
                # Discard the stale Playwright objects before relaunching the
                # same persistent profile.
                await self._close_browser()

            self._previous_status = str(account.get("binding_status") or "unbound")
            try:
                await self._launch_visible_browser(account_id)
                try:
                    await self._page.goto(
                        GOOFISH_LOGIN_TARGET,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                except Exception:
                    pass
                return self._context
            except Exception as exc:
                await self._close_browser()
                raise RuntimeError(f"启动专用浏览器失败：{exc}") from exc

    @login_operation
    async def snapshot(self, account_id: int | None = None, *, inspect_live: bool = True) -> dict[str, object]:
        account = self.database.get_account(account_id) if account_id is not None else self.database.get_active_account()
        if account is None:
            raise ValueError("账号不存在")
        current_id = int(account["id"])
        detected = False
        page_url = ""
        browser_open = self._context is not None and self._account_id == current_id
        if browser_open and self._page is not None:
            try:
                browser_open = not self._page.is_closed()
            except Exception:
                browser_open = False
        if (
            not browser_open
            and account.get("binding_status") == "detected"
            and account.get("binding_confirmed_at")
        ):
            restored = self.database.update_account_binding(current_id, "bound")
            if restored is not None:
                account = restored
        if browser_open and inspect_live:
            detected, page_url = await self._detect_login()
            if detected and account["binding_status"] not in {"detected", "bound"}:
                updated = self.database.update_account_binding(current_id, "detected")
                if updated is not None:
                    account = updated
        return {
            "account_id": current_id,
            "account_name": account["name"],
            "status": account.get("binding_status", "unbound"),
            "binding_confirmed_at": account.get("binding_confirmed_at"),
            "last_checked_at": account.get("session_last_checked_at"),
            "last_error": account.get("session_last_error", ""),
            "browser_open": browser_open,
            "login_detected": detected or account.get("binding_status") == "detected",
            "page_url": page_url if browser_open else "",
            "browser_available": bool(self.browser_executable and self.browser_executable.is_file()),
            "browser_name": self.browser_executable.name if self.browser_executable else "",
            "browser_mode": "visible_persistent",
            "session_sync_available": browser_open
            and (detected or account.get("binding_status") in {"detected", "bound"}),
            "profile_security": "会话由独立浏览器配置目录保存；数据库不保存 Cookie 值",
        }

    @login_operation
    async def start_login(self, account_id: int) -> dict[str, object]:
        async with self._lock:
            account = self.database.get_account(account_id)
            if account is None:
                raise ValueError("账号不存在")
            if not account["is_active"] and self.runtime_policy.account_id != account_id:
                raise ValueError("只能绑定当前账号")
            if self.browser_executable is None or not self.browser_executable.is_file():
                self.database.update_account_binding(account_id, "error", error="没有找到 Chrome 或 Edge")
                raise RuntimeError("没有找到 Chrome 或 Edge")
            if self._context is not None:
                if await self._context_is_alive():
                    if self._account_id == account_id:
                        await self._ensure_live_page()
                        try:
                            await self._page.bring_to_front()
                        except Exception:
                            pass
                        return await self.snapshot(account_id)
                    raise RuntimeError("另一个账号正在进行扫码绑定")
                # The last Chrome window was closed outside the manager.  The
                # old context object is unusable even though it is non-null.
                await self._close_browser()

            self._account_id = account_id
            self._handoff_account_id = None
            self._handoff_storage_state = None
            self._previous_status = str(account.get("binding_status") or "unbound")
            self.database.update_account_binding(account_id, "starting")
            try:
                await self._launch_visible_browser(account_id)
                try:
                    await self._page.goto(
                        GOOFISH_HOME if self.runtime_policy.prepare_mode else GOOFISH_LOGIN_TARGET,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                except Exception:
                    # A slow page can still be usable for manual QR login.
                    pass
                self.database.update_account_binding(account_id, "waiting_scan")
                self._monitor_task = asyncio.create_task(self._monitor(account_id))
            except Exception as exc:
                await self._close_browser()
                self.database.update_account_binding(account_id, "error", error=str(exc)[:300])
                raise RuntimeError(f"启动扫码浏览器失败：{exc}") from exc
        return await self.snapshot(account_id)

    @login_operation
    async def confirm_login(self, account_id: int) -> dict[str, object]:
        async with self._lock:
            if self._account_id != account_id or self._context is None:
                raise ValueError("当前没有等待确认的扫码窗口")
            detected, _ = await self._detect_login()
            if not detected:
                raise ValueError("尚未检测到登录，请先在浏览器中完成扫码")
            self._handoff_storage_state = await self._context.storage_state()
            self._handoff_account_id = account_id
            self.database.update_account_binding(account_id, "bound", confirmed=True)
            self._previous_status = "bound"
        return await self.snapshot(account_id, inspect_live=False)

    @login_operation
    async def sync_session(self, account_id: int) -> dict[str, object]:
        """Refresh the in-memory handoff from the currently visible browser."""
        async with self._lock:
            if self._account_id != account_id or self._context is None:
                raise ValueError("请先打开闲鱼专用 Chrome")
            detected, _ = await self._detect_login()
            if not detected:
                self.database.update_account_binding(
                    account_id,
                    "expired",
                    error="专用 Chrome 中尚未检测到完整登录",
                )
                raise ValueError("专用 Chrome 尚未完成登录或验证")
            self._handoff_storage_state = await self._context.storage_state()
            self._handoff_account_id = account_id
            self.database.update_account_binding(account_id, "bound", confirmed=True)
            self._previous_status = "bound"
        return await self.snapshot(account_id, inspect_live=False)

    @business_operation
    def take_session_handoff(self, account_id: int) -> dict[str, object] | None:
        """Move the just-confirmed browser state into the delivery runtime.

        Session-cookie values exist only in process memory. They are never
        returned by the HTTP API or written to SQLite/log files.
        """
        if self._handoff_account_id != account_id:
            return None
        state = self._handoff_storage_state
        self._handoff_account_id = None
        self._handoff_storage_state = None
        return state

    async def read_preparation_inventory(self, account_id: int, frontend_count=None):
        from .preparation_inventory import collect_once
        self.runtime_policy.require_login(account_id)
        async with self._lock:
            if (self._account_id != account_id or self._context is None
                    or self._handoff_account_id != account_id):
                raise RuntimeOperationBlocked('MANUAL_LOGIN_CONFIRMATION_REQUIRED')
            return await collect_once(self, account_id, frontend_count)

    @login_operation
    async def cancel_login(self, account_id: int) -> dict[str, object]:
        async with self._lock:
            if self._account_id == account_id:
                restore = self._previous_status if self._previous_status in {"bound", "expired"} else "unbound"
                await self._close_browser()
                self.database.update_account_binding(account_id, restore)
            self._handoff_account_id = None
            self._handoff_storage_state = None
        return await self.snapshot(account_id, inspect_live=False)

    @business_operation
    async def detect_listing(self, account_id: int, expected_title: str) -> dict[str, object]:
        """Read the active account's public profile and match a manually published item."""
        async with self._lock:
            account = self.database.get_account(account_id)
            if account is None or not account["is_active"]:
                raise ValueError("只能检测当前账号")
            if account.get("binding_status") != "bound":
                raise ValueError("真实闲鱼账号尚未绑定或登录已失效")
            if self.browser_executable is None or not self.browser_executable.is_file():
                raise RuntimeError("没有找到 Chrome 或 Edge")

            playwright: Any | None = None
            context: Any | None = None
            page: Any | None = None
            profile_lock: ProfileOwnerLock | None = None
            shared_context = False
            try:
                if self._context is not None:
                    if self._account_id != account_id:
                        raise RuntimeError("另一个账号正在使用专用浏览器")
                    context = self._context
                    shared_context = True
                else:
                    from playwright.async_api import async_playwright

                    profile_lock = ProfileOwnerLock(self.profile_dir(account_id))
                    profile_lock.acquire()
                    playwright = await async_playwright().start()
                    context = await playwright.chromium.launch_persistent_context(
                        **sandbox_options(),
                        user_data_dir=str(self.profile_dir(account_id)),
                        executable_path=str(self.browser_executable),
                        headless=self.browser_headless,
                        no_viewport=True,
                        args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
                    )
                cookies = await context.cookies([GOOFISH_HOME, "https://passport.goofish.com/"])
                cookie_map = {str(item.get("name", "")): str(item.get("value", "")) for item in cookies}
                has_identity = bool(set(cookie_map) & IDENTITY_COOKIE_NAMES)
                has_session = bool(set(cookie_map) & SESSION_COOKIE_NAMES)
                user_id = cookie_map.get("unb", "").strip()
                if not (has_identity and has_session and user_id):
                    self.database.update_account_binding(account_id, "expired", error="登录会话缺少必要标记")
                    raise ValueError("闲鱼登录已失效，请重新扫码")

                page = await context.new_page() if shared_context else (
                    context.pages[0] if context.pages else await context.new_page()
                )
                profile_url = f"https://www.goofish.com/personal?userId={user_id}"
                await page.goto(profile_url, wait_until="domcontentloaded", timeout=60_000)
                if "passport.goofish.com" in page.url:
                    self.database.update_account_binding(account_id, "expired", error="访问个人主页时被要求重新登录")
                    raise ValueError("闲鱼登录已失效，请重新扫码")
                try:
                    await page.locator('a[href*="/item?"]').first.wait_for(state="attached", timeout=12_000)
                except Exception:
                    await page.wait_for_timeout(2_000)
                raw_items = await page.locator('a[href*="/item?"]').evaluate_all(
                    """anchors => anchors.slice(0, 50).map(anchor => ({
                      url: anchor.href || '',
                      text: (anchor.innerText || anchor.textContent || '').trim()
                    }))"""
                )
                result = match_listing_candidates(raw_items, expected_title)
                if not raw_items:
                    diagnostic_dir = self.profiles_dir.parent / "session-screenshots"
                    diagnostic_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        await page.screenshot(
                            path=str(diagnostic_dir / "listing-detection-latest.png"),
                            full_page=True,
                        )
                    except Exception:
                        pass
                self.database.update_account_binding(account_id, "bound")
                return result
            except ValueError:
                raise
            except Exception as exc:
                raise RuntimeError(f"检测已发布商品失败：{exc}") from exc
            finally:
                if shared_context and page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass
                if context is not None and not shared_context:
                    try:
                        await context.close()
                    except Exception:
                        pass
                if playwright is not None:
                    try:
                        await playwright.stop()
                    except Exception:
                        pass
                if profile_lock is not None:
                    profile_lock.release()

    async def shutdown(self) -> None:
        if self.runtime_policy.safe_mode:
            return
        async with self._lock:
            account_id = self._account_id
            restore = self._previous_status if self._previous_status in {"bound", "expired"} else "unbound"
            await self._close_browser()
            self._handoff_account_id = None
            self._handoff_storage_state = None
            if account_id is not None:
                self.database.update_account_binding(account_id, restore)

    @login_operation
    async def _detect_login(self) -> tuple[bool, str]:
        if self._context is None:
            return False, ""
        try:
            cookies = await self._context.cookies([GOOFISH_HOME, "https://passport.goofish.com/"])
            names = {str(item.get("name", "")) for item in cookies}
            page_url = str(self._page.url) if self._page is not None else ""
            has_identity = bool(names & IDENTITY_COOKIE_NAMES)
            # The public homepage can appear logged in with only ``sgcookie``,
            # while the PC message-token endpoint still rejects that session.
            # Wait for a stronger session marker before offering confirmation.
            has_session = bool(names & MESSAGE_SESSION_COOKIE_NAMES)
            on_login_page = "passport.goofish.com" in page_url
            return has_identity and has_session and not on_login_page, page_url
        except Exception:
            return False, ""

    @login_operation
    async def _monitor(self, account_id: int) -> None:
        try:
            while self._context is not None and self._account_id == account_id:
                detected, _ = await self._detect_login()
                if detected:
                    self.database.update_account_binding(account_id, "detected")
                    return
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.database.update_account_binding(account_id, "error", error=str(exc)[:300])

    async def _close_browser(self) -> None:
        monitor = self._monitor_task
        self._monitor_task = None
        if monitor is not None and monitor is not asyncio.current_task():
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._context = None
        self._page = None
        self._playwright = None
        self._account_id = None
        if self._profile_lock is not None:
            self._profile_lock.release()
            self._profile_lock = None
