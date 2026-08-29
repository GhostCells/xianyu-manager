from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from xianyu_manager.database import Database
from xianyu_manager.session import (
    BrowserSessionManager,
    match_listing_candidates,
    normalize_listing_url,
)


class FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies
        self.closed = False

    async def cookies(self, _urls):
        if self.closed:
            raise RuntimeError("browser context closed")
        return self._cookies

    async def storage_state(self):
        return {"cookies": self._cookies}

    async def close(self):
        self.closed = True


class FakePage:
    def __init__(self, url: str, *, closed: bool = False):
        self.url = url
        self.closed = closed

    def is_closed(self):
        return self.closed

    async def goto(self, url: str, **_kwargs):
        self.url = url

    async def bring_to_front(self):
        return None


def test_detect_login_requires_identity_and_session_cookie(tmp_path):
    database = Database(tmp_path / "manager.db")
    manager = BrowserSessionManager(tmp_path / "profiles", None, database)
    manager._context = FakeContext([{"name": "unb"}, {"name": "cookie2"}])
    manager._page = FakePage("https://www.goofish.com/")

    detected, page_url = asyncio.run(manager._detect_login())
    assert detected is True
    assert page_url == "https://www.goofish.com/"


def test_login_page_is_not_treated_as_bound(tmp_path):
    database = Database(tmp_path / "manager.db")
    manager = BrowserSessionManager(tmp_path / "profiles", None, database)
    manager._context = FakeContext([{"name": "unb"}, {"name": "cookie2"}])
    manager._page = FakePage("https://passport.goofish.com/login")

    detected, _ = asyncio.run(manager._detect_login())
    assert detected is False


def test_confirm_login_keeps_visible_browser_open(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.ensure_default_accounts()
    account = database.get_active_account()
    manager = BrowserSessionManager(tmp_path / "profiles", None, database)
    context = FakeContext(
        [{"name": "unb", "value": "seller"}, {"name": "cookie2", "value": "session"}]
    )
    manager._context = context
    manager._page = FakePage("https://www.goofish.com/im")
    manager._account_id = int(account["id"])

    result = asyncio.run(manager.confirm_login(int(account["id"])))

    assert result["status"] == "bound"
    assert result["browser_open"] is True
    assert result["session_sync_available"] is True
    assert manager._context is context
    assert context.closed is False


def test_manual_session_sync_uses_current_visible_context(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.ensure_default_accounts()
    account = database.get_active_account()
    manager = BrowserSessionManager(tmp_path / "profiles", None, database)
    context = FakeContext(
        [{"name": "unb", "value": "seller"}, {"name": "cookie2", "value": "session"}]
    )
    manager._context = context
    manager._page = FakePage("https://www.goofish.com/im")
    manager._account_id = int(account["id"])

    result = asyncio.run(manager.sync_session(int(account["id"])))

    assert result["status"] == "bound"
    assert result["browser_mode"] == "visible_persistent"
    assert context.closed is False


def test_start_login_reopens_after_visible_browser_was_closed(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.ensure_default_accounts()
    account = database.get_active_account()
    account_id = int(account["id"])
    database.update_account_binding(account_id, "bound", confirmed=True)
    browser_executable = tmp_path / "chrome.exe"
    browser_executable.touch()
    manager = BrowserSessionManager(tmp_path / "profiles", browser_executable, database)

    stale_context = FakeContext([])
    stale_context.closed = True
    manager._context = stale_context
    manager._page = FakePage("https://www.goofish.com/im", closed=True)
    manager._account_id = account_id

    fresh_context = FakeContext(
        [{"name": "unb", "value": "seller"}, {"name": "cookie2", "value": "session"}]
    )
    fresh_page = FakePage("about:blank")

    async def fake_launch(reopened_account_id: int):
        manager._context = fresh_context
        manager._page = fresh_page
        manager._account_id = reopened_account_id

    manager._launch_visible_browser = fake_launch

    result = asyncio.run(manager.start_login(account_id))

    assert result["browser_open"] is True
    assert manager._context is fresh_context
    assert manager._page is fresh_page
    assert fresh_page.url == "https://www.goofish.com/im"


@pytest.mark.parametrize(
    ("browser_path", "browser_name"),
    [
        (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "msedge"),
        (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "chrome"),
        ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "google chrome"),
        ("/Applications/Chromium.app/Contents/MacOS/Chromium", "chromium"),
        ("/usr/bin/google-chrome", "google-chrome"),
        ("/usr/bin/chromium", "chromium"),
    ],
)
def test_profile_directory_is_scoped_by_browser(tmp_path, browser_path, browser_name):
    database = Database(tmp_path / "manager.db")
    manager = BrowserSessionManager(
        tmp_path / "profiles",
        Path(browser_path),
        database,
    )

    assert manager.profile_dir(2) == tmp_path / "profiles" / browser_name / "account-2"


def test_normalize_listing_url_keeps_only_item_id():
    assert normalize_listing_url(
        "https://www.goofish.com/item?id=123456&foo=bar"
    ) == ("123456", "https://www.goofish.com/item?id=123456")
    assert normalize_listing_url("https://example.com/item?id=123456") is None
    assert normalize_listing_url("https://www.goofish.com/item") is None


def test_match_listing_candidates_uses_normalized_title():
    result = match_listing_candidates(
        [
            {
                "url": "https://www.goofish.com/item?id=10001&spm=test",
                "text": "Seedance2 分镜生成 Skill  ¥29.90",
            }
        ],
        "Seedance2分镜生成Skill",
    )

    assert result["matched"] is True
    assert result["listing"]["item_id"] == "10001"
    assert result["listing"]["url"] == "https://www.goofish.com/item?id=10001"
