import asyncio
from dataclasses import replace
import logging

import pytest
from fastapi.testclient import TestClient

from xianyu_manager import app as module
from xianyu_manager.config import load_settings
from xianyu_manager.database import Database
from xianyu_manager.delivery import DeliveryService
from xianyu_manager.runtime_policy import RuntimePolicy, SafeModeOperationBlocked
from xianyu_manager.scanner import scan_library
from xianyu_manager.security import SecretStore
from xianyu_manager.session import BrowserSessionManager


PROTECTED = (
    "accounts", "account_products", "account_listings", "orders", "chat_sessions",
    "chat_messages", "automation_outbound_events", "auto_reply_settings", "audit_log",
)


def snapshot(db):
    with db.connect() as connection:
        return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                for table in PROTECTED}


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    policy = RuntimePolicy(True)
    settings = replace(module.settings, safe_mode=True, data_dir=tmp_path,
                       database_path=tmp_path / "manager.db",
                       browser_profiles_dir=tmp_path / "profiles")
    db = Database(settings.database_path, safe_mode=True)
    db.sync_products(scan_library(settings.product_library, settings.validator_path), safe_mode=True)
    db.ensure_default_accounts()
    with db.connect() as connection:
        connection.execute("UPDATE accounts SET is_active=0")
        connection.execute("INSERT INTO orders(xianyu_order_id,account_id,product_dir_name) VALUES('test-order',1,'01-synthetic')")
        connection.execute("INSERT INTO chat_sessions(account_id,chat_id) VALUES(1,'test-chat')")
        connection.execute("INSERT INTO chat_messages(account_id,chat_id,direction,content,event_fingerprint) VALUES(1,'test-chat','in','synthetic','test-event')")
        connection.execute("INSERT INTO automation_outbound_events(account_id,kind,reference) VALUES(1,'reply','test-ref')")
        connection.execute("INSERT INTO account_listings(account_id,item_id,title,listing_url) VALUES(1,'123','synthetic','https://example.invalid')")
        connection.execute("INSERT INTO auto_reply_settings(account_id,enabled) VALUES(1,0)")
        connection.execute("UPDATE products SET knowledge_source_path='C:\\test',knowledge_text='retained knowledge',knowledge_chars=18 WHERE dir_name='01-synthetic'")
        connection.execute("UPDATE products SET zip_hash='old-hash' WHERE dir_name='02-synthetic'")
        connection.execute("INSERT INTO products(dir_name,number,name,title,zip_name,zip_hash,zip_size,image_count,quality_status,quality_errors_json,scanned_at) VALUES('99-history',99,'history','history','','',0,0,'failed','[]','old')")
    session = BrowserSessionManager(settings.browser_profiles_dir, None, db, runtime_policy=policy)
    delivery = DeliveryService(settings.browser_profiles_dir, None, db,
                               SecretStore(tmp_path / "secret"), session_manager=session,
                               runtime_policy=policy)
    for name, value in {"runtime_policy": policy, "settings": settings, "database": db,
                        "session_manager": session, "delivery_service": delivery}.items():
        monkeypatch.setattr(module, name, value)

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected account recovery or business IO")

    for method in ("ensure_default_accounts", "ensure_legacy_product_policy", "enforce_single_account_mode", "reconcile_configured_listings"):
        monkeypatch.setattr(db, method, forbidden)
    import websockets
    import httpx
    from xianyu_manager.auto_reply import SiliconFlowReplyClient
    monkeypatch.setattr(websockets, "connect", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(SiliconFlowReplyClient, "generate", forbidden)
    return db, session, delivery


@pytest.mark.parametrize("misconfigured", [False, True])
def test_real_lifespan_preserves_accounts_and_business_data(rehearsal, misconfigured):
    db, session, delivery = rehearsal
    if misconfigured:
        with db.connect() as connection:
            connection.execute("UPDATE accounts SET is_active=1,binding_status='bound',delivery_enabled=1,auto_confirm_delivery=1,auto_free_group=1 WHERE id=1")
            connection.execute("UPDATE auto_reply_settings SET enabled=1")
    before = snapshot(db)
    with TestClient(module.app, base_url="http://127.0.0.1") as client:
        for _ in range(3):
            response = client.get("/api/health")
            assert response.status_code == 200
            payload = response.json()
            assert payload["safe_mode"] is True
            assert payload["automation_allowed"] is False
            assert bool(payload["active_account"]) is misconfigured
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        products = client.get("/api/products").json()
        assert len(products) == 20  # historical product retained
        assert next(p for p in products if p["dir_name"] == "01-synthetic")["knowledge_text"] == "retained knowledge"
        assert next(p for p in products if p["dir_name"] == "02-synthetic")["share_needs_review"]
        assert session._context is None
        assert delivery._task is None
        assert not session.profiles_dir.exists()
    assert snapshot(db) == before


def test_all_non_allowlisted_routes_block_before_validation(rehearsal):
    db, _, _ = rehearsal
    with TestClient(module.app, base_url="http://127.0.0.1") as client:
        before = snapshot(db)
        for route in module.app.routes:
            path = getattr(route, "path", "")
            for method in getattr(route, "methods", ()):
                if path in module.SAFE_READ_PATHS and method in {"GET", "HEAD"}:
                    continue
                response = client.request(method, path)
                assert response.status_code == 403, (method, path)
                if method != "HEAD":
                    assert response.json()["error_code"] == "SAFE_MODE_OPERATION_BLOCKED"
        assert client.post("/api/health").status_code == 403
        assert client.get("/api/session").status_code == 403
        assert snapshot(db) == before


@pytest.mark.parametrize("owner,name", [
    ("session", "start_login"), ("session", "ensure_runtime_context"),
    ("session", "snapshot"), ("session", "sync_session"),
    ("session", "search_listings"), ("session", "collect_listing_detail"),
    ("delivery", "start_if_enabled"), ("delivery", "start"),
    ("delivery", "start_auto_reply"), ("delivery", "probe"),
    ("delivery", "_read_profile_cookies"), ("delivery", "_fetch_im_token"),
    ("delivery", "_post_mtop"), ("delivery", "_send_json"),
    ("delivery", "_process_chat_event"), ("delivery", "_recover_recent_paid_orders"),
])
def test_direct_business_entries_reject_before_arguments_or_io(rehearsal, owner, name):
    db, session, delivery = rehearsal
    before = snapshot(db)
    target = session if owner == "session" else delivery
    with pytest.raises(SafeModeOperationBlocked):
        asyncio.run(getattr(target, name)())
    assert snapshot(db) == before


@pytest.mark.parametrize("value,expected", [(None, False), ("false", False), ("true", True)])
def test_safe_mode_configuration(tmp_path, monkeypatch, value, expected):
    monkeypatch.setenv("XIANYU_MANAGER_DATA_DIR", str(tmp_path))
    if value is None:
        monkeypatch.delenv("XIANYU_MANAGER_SAFE_MODE", raising=False)
    else:
        monkeypatch.setenv("XIANYU_MANAGER_SAFE_MODE", value)
    settings = load_settings()
    assert settings.safe_mode is expected
    monkeypatch.setenv("XIANYU_MANAGER_SAFE_MODE", "true" if not expected else "false")
    assert settings.safe_mode is expected


def test_invalid_safe_mode_rejects_before_data_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("XIANYU_MANAGER_SAFE_MODE", "typo")
    monkeypatch.setenv("XIANYU_MANAGER_DATA_DIR", str(tmp_path / "absent"))
    with pytest.raises(ValueError, match="XIANYU_MANAGER_SAFE_MODE"):
        load_settings()
    assert not (tmp_path / "absent").exists()


def test_scheduler_rejects_before_any_collection(monkeypatch):
    from xianyu_manager import selection_scheduler as scheduler
    monkeypatch.setattr(scheduler, "PROCESS_POLICY", RuntimePolicy(True))
    async def forbidden(*args, **kwargs):
        pytest.fail("scheduler invoked collection")
    with pytest.raises(SafeModeOperationBlocked):
        asyncio.run(scheduler.run_collection_cycle(None, None, "", logging.getLogger(__name__),
                    search_function=forbidden, batch_function=forbidden))


def test_llm_direct_entry_is_guarded():
    from xianyu_manager.auto_reply import SiliconFlowReplyClient
    client = SiliconFlowReplyClient(runtime_policy=RuntimePolicy(True))
    with pytest.raises(SafeModeOperationBlocked):
        asyncio.run(client.generate())


def test_safe_database_initialization_preserves_reply_configuration(rehearsal):
    db, _, _ = rehearsal
    with db.connect() as connection:
        connection.execute("UPDATE auto_reply_settings SET base_url='https://api.deepseek.com',model='deepseek-v4-pro',enabled=1")
    before = snapshot(db)
    Database(db.path, safe_mode=True)
    assert snapshot(db) == before
