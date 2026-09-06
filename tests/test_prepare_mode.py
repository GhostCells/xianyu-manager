import asyncio
from dataclasses import replace
import json
import time

import pytest
from fastapi.testclient import TestClient

from xianyu_manager import app as api
from xianyu_manager.config import load_settings
from xianyu_manager.database import Database
from xianyu_manager.delivery import DeliveryService
from xianyu_manager.manual_review import fingerprint, record_review
from xianyu_manager.runtime_policy import (
    RuntimePolicy,
    RuntimeOperationBlocked,
    SafeModeOperationBlocked,
)
from xianyu_manager.security import SecretStore
from xianyu_manager.session import BrowserSessionManager
from test_safe_mode import snapshot


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    db = Database(tmp_path / "manager.db", prepare_mode=True)
    db.ensure_default_accounts()
    with db.connect() as c:
        c.execute("UPDATE accounts SET is_active=0")
        c.execute(
            "UPDATE accounts SET is_active=1,binding_status='bound',delivery_enabled=1,auto_free_group=1,auto_confirm_delivery=1 WHERE id=1"
        )
        c.execute("INSERT INTO auto_reply_settings(account_id,enabled) VALUES(1,1)")
        c.execute(
            "INSERT INTO products(dir_name,number,name,title,zip_name,zip_hash,zip_size,image_count,quality_status,quality_errors_json,scanned_at) VALUES('order-product',1,'fake','fake','','',0,0,'failed','[]','old')"
        )
        c.execute(
            "INSERT INTO orders(xianyu_order_id,account_id,product_dir_name,delivery_status,payment_status,delivery_attempts,fulfillment_fingerprint) VALUES('synthetic-order',1,'order-product','sending','paid',2,'original-claim')"
        )
    policy = RuntimePolicy(prepare_mode=True)
    settings = replace(
        api.settings,
        prepare_mode=True,
        database_path=db.path,
        data_dir=tmp_path,
        browser_profiles_dir=tmp_path / "profiles",
    )
    session = BrowserSessionManager(
        settings.browser_profiles_dir, None, db, runtime_policy=policy
    )
    delivery = DeliveryService(
        settings.browser_profiles_dir,
        None,
        db,
        SecretStore(tmp_path / "secret"),
        session_manager=session,
        runtime_policy=policy,
    )
    for name, value in dict(
        settings=settings,
        database=db,
        runtime_policy=policy,
        session_manager=session,
        delivery_service=delivery,
    ).items():
        monkeypatch.setattr(api, name, value)

    def forbidden(*a, **kw):
        pytest.fail("implicit startup/business recovery")

    for method in (
        "enforce_single_account_mode",
        "ensure_default_accounts",
        "ensure_legacy_product_policy",
        "reconcile_configured_listings",
    ):
        monkeypatch.setattr(db, method, forbidden)
    monkeypatch.setattr(delivery, "start_if_enabled", forbidden)
    return db, session, delivery


def test_prepare_full_lifespan_and_restart_never_recovers(prepared):
    db, session, delivery = prepared
    before = snapshot(db)
    for _ in range(2):
        with TestClient(api.app, base_url="http://127.0.0.1") as client:
            h = client.get("/api/health").json()
            assert (
                h["mode"] == "prepare"
                and not h["automation_allowed"]
                and not h["login_allowed"]
            )
            assert h["runtime_account_id"] is None
            assert client.post("/api/session/start").status_code == 403
            assert client.post("/api/delivery/start").status_code == 403
            assert client.post("/api/accounts/1/activate").status_code == 403
            assert session._context is None and delivery._task is None
        assert snapshot(db) == before


def ready_policy(tmp_path, **overrides):
    p = tmp_path / "egress.json"
    p.write_text(
        json.dumps(
            dict(
                checked_at=time.time(),
                exit_node_ip="100.66.224.40",
                client_running=True,
                routes_ready=True,
                enforcement_verified=True,
                review_required=False,
                observed_public_ip="192.0.2.1",
                reviewed_public_ip="192.0.2.1",
                path_kind="relay",
            )
        )
    )
    return RuntimePolicy(
        prepare_mode=True,
        account_id=1,
        login_authorized=True,
        egress_status_path=p,
        **overrides
    )


def test_mock_login_confirm_sync_never_resume(prepared, tmp_path, monkeypatch):
    db, session, delivery = prepared
    policy = ready_policy(tmp_path)
    db.runtime_account_id = 1
    monkeypatch.setattr(api, "runtime_policy", policy)
    session.runtime_policy = policy
    delivery.runtime_policy = policy
    calls = []

    async def mock_login(account_id):
        policy.require_login(account_id)
        calls.append(account_id)
        return {"status": "bound", "account_id": account_id}

    for name in ("start_login", "confirm_login", "sync_session"):
        monkeypatch.setattr(session, name, mock_login)

    async def forbidden(*a, **k):
        pytest.fail("business resumed after login")

    monkeypatch.setattr(delivery, "start", forbidden)
    monkeypatch.setattr(delivery, "start_auto_reply", forbidden)
    with TestClient(api.app, base_url="http://127.0.0.1") as client:
        for action in ("start", "confirm", "sync"):
            assert client.post("/api/session/" + action).status_code == 200
        assert not client.get("/api/health").json()["automation_allowed"]
    assert calls == [1, 1, 1]


def test_real_login_methods_with_fake_browser_on_inactive_explicit_account(
    prepared, tmp_path, monkeypatch
):
    db, session, delivery = prepared
    policy = ready_policy(tmp_path)
    db.runtime_account_id = 1
    session.runtime_policy = policy
    monkeypatch.setattr(api, "runtime_policy", policy)
    with db.connect() as c:
        c.execute("UPDATE accounts SET is_active=0")
    exe = tmp_path / "fake-chrome"
    exe.touch()
    session.browser_executable = exe

    class Page:
        async def goto(self, *a, **k):
            return None

        def is_closed(self):
            return False

    class Context:
        async def storage_state(self):
            return {"cookies": []}

        async def close(self):
            return None

    async def launch(account_id):
        assert account_id == 1
        session._context = Context()
        session._page = Page()

    async def detect():
        return True, ""

    monkeypatch.setattr(session, "_launch_visible_browser", launch)
    monkeypatch.setattr(session, "_detect_login", detect)
    with TestClient(api.app, base_url="http://127.0.0.1") as client:
        assert (
            client.post("/api/session/start", json={"account_id": 2}).status_code == 403
        )
        for action in ["start", "confirm", "sync"]:
            response = client.post("/api/session/" + action)
            assert response.status_code == 200, response.text
        assert delivery._task is None
        assert not client.get("/api/health").json()["automation_allowed"]
    assert not db.get_account(1)["is_active"]
    assert session._context is None


def test_prepare_whitelist_and_review_api(prepared, monkeypatch):
    db, session, delivery = prepared
    policy = RuntimePolicy(prepare_mode=True, account_id=1)
    monkeypatch.setattr(api, "runtime_policy", policy)
    db.runtime_account_id = 1
    with TestClient(api.app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/session").status_code == 403
        assert client.post("/api/preparation/order-review", json={}).status_code == 403
        order = client.get("/api/preparation/orders").json()[0]
        payload = dict(
            account_id=1,
            order_id=order["xianyu_order_id"],
            expected_fingerprint=order["fingerprint"],
            action="unknown",
            operator="synthetic",
            reason="test",
            evidence_ref="case:test",
        )
        headers = {"X-Preparation-Action": "confirm-local"}
        assert (
            client.post(
                "/api/preparation/order-review",
                json={**payload, "account_id": 2},
                headers=headers,
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/preparation/order-review", json=payload, headers=headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/preparation/order-review", json=payload, headers=headers
            ).status_code
            == 409
        )


@pytest.mark.parametrize(
    "entry",
    [
        "start",
        "_run",
        "_post_mtop",
        "_send_text",
        "_recover_recent_paid_orders",
        "retry_platform_confirmation",
    ],
)
def test_prepare_direct_business_rejected(prepared, entry):
    _, _, delivery = prepared
    with pytest.raises(RuntimeOperationBlocked):
        asyncio.run(getattr(delivery, entry)())


@pytest.mark.parametrize(
    "field,value",
    [
        ("checked_at", 0),
        ("exit_node_ip", "100.1.1.1"),
        ("enforcement_verified", False),
        ("review_required", True),
        ("client_running", False),
        ("routes_ready", False),
        ("observed_public_ip", "192.0.2.2"),
    ],
)
def test_egress_fail_closed_and_latched(tmp_path, field, value):
    policy = ready_policy(tmp_path)
    original = json.loads(policy.egress_status_path.read_text())
    assert policy.egress_status()["ready"]  # relay is not a wrong exit
    policy.egress_status_path.write_text(json.dumps({**original, field: value}))
    with pytest.raises(RuntimeOperationBlocked):
        policy.require_login(1)
    policy.egress_status_path.write_text(json.dumps(original))
    with pytest.raises(RuntimeOperationBlocked):
        policy.require_login(1)


def test_account_and_safe_priority(prepared, tmp_path):
    db, session, _ = prepared
    policy = ready_policy(tmp_path)
    with pytest.raises(RuntimeOperationBlocked):
        policy.require_login(2)
    assert RuntimePolicy(True, prepare_mode=True).mode == "safe"
    with pytest.raises(SafeModeOperationBlocked):
        RuntimePolicy(True, prepare_mode=True).require_login(1)
    with pytest.raises(ValueError, match="RUNTIME_ACCOUNT"):
        Database(db.path, prepare_mode=True, runtime_account_id=999)
    session.runtime_policy = policy
    with pytest.raises(RuntimeOperationBlocked):
        asyncio.run(session.start_login(2))


@pytest.mark.parametrize(
    "key,value",
    [
        ("XIANYU_MANAGER_PREPARE_MODE", "bad"),
        ("XIANYU_MANAGER_ACCOUNT_ID", "0"),
        ("XIANYU_MANAGER_ACCOUNT_ID", "1.5"),
        ("XIANYU_MANAGER_LOGIN_AUTHORIZED", "bad"),
    ],
)
def test_invalid_prepare_config_rejected_before_directory(
    tmp_path, monkeypatch, key, value
):
    monkeypatch.setenv(key, value)
    monkeypatch.setenv("XIANYU_MANAGER_DATA_DIR", str(tmp_path / "absent"))
    with pytest.raises(ValueError):
        load_settings()
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize(
    "action,unlocked",
    [
        ("copied", False),
        ("unknown", False),
        ("confirmed_sent", True),
        ("confirmed_not_sent", True),
        ("platform_only", False),
    ],
)
def test_manual_review_state_and_concurrency(prepared, action, unlocked):
    db, _, _ = prepared
    policy = RuntimePolicy(prepare_mode=True, account_id=1)
    original = db.get_order("synthetic-order")
    args = dict(
        account_id=1,
        order_id="synthetic-order",
        expected_fingerprint=fingerprint(original),
        action=action,
        platform_state="reported_confirmed",
        operator="local-test",
        reason="synthetic check",
        evidence_ref="case:test",
    )
    result = record_review(db, policy, **args)
    assert result["delivery_material_unlock"] == unlocked
    assert (
        not result["automatic_retry_authorized"]
        and not result["platform_verified_online"]
    )
    current = db.get_order("synthetic-order")
    for key in original:
        if not key.startswith("manual_"):
            assert current[key] == original[key]
    with pytest.raises(ValueError, match="STALE"):
        record_review(Database(db.path, prepare_mode=True), policy, **args)
    with db.connect() as c:
        if unlocked:
            db._require_no_pending_delivery(c, "order-product")
        else:
            with pytest.raises(ValueError, match="IN_FLIGHT"):
                db._require_no_pending_delivery(c, "order-product")
    with pytest.raises(ValueError):
        db.claim_verified_delivery("synthetic-order", 1, "123")
    with pytest.raises(RuntimeOperationBlocked):
        record_review(db, RuntimePolicy(True, prepare_mode=True, account_id=1), **args)
    # Neither the legacy claim helper nor the A0 send validator may retry a review.
    db.prepare_mode = False
    assert not db.claim_order_delivery("synthetic-order")
    assert not db.validate_delivery_claim("synthetic-order", 1, "", "")


def test_normal_managed_business_egress_gate_before_io(prepared):
    _, _, delivery = prepared
    delivery.runtime_policy = RuntimePolicy(account_id=1)
    for entry in ("_post_mtop", "_send_text", "_run", "_recover_recent_paid_orders"):
        with pytest.raises(RuntimeOperationBlocked, match="EGRESS_BLOCKED"):
            asyncio.run(getattr(delivery, entry)())


def test_manual_sent_history_cannot_be_reclassified_not_sent(prepared):
    db, _, _ = prepared
    policy = RuntimePolicy(prepare_mode=True, account_id=1)
    for action in ("confirmed_sent", "unknown", "confirmed_not_sent"):
        args = dict(
            account_id=1,
            order_id="synthetic-order",
            expected_fingerprint=fingerprint(db.get_order("synthetic-order")),
            action=action,
            platform_state="not_checked",
            operator="synthetic",
            reason="synthetic review",
            evidence_ref="test:history",
        )
        if action == "confirmed_not_sent":
            with pytest.raises(ValueError, match="EXISTING_SEND_EVIDENCE"):
                record_review(db, policy, **args)
        else:
            record_review(db, policy, **args)


@pytest.mark.parametrize(
    "code,stopping,expected",
    [(0, True, 0), (-15, True, 0), (-2, True, 0), (-15, False, -15), (1, True, 1)],
)
def test_preparation_launcher_stop_status(code, stopping, expected):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts/run_preparation_service.py"
    spec = importlib.util.spec_from_file_location("preparation_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.exit_status(code, stopping) == expected
