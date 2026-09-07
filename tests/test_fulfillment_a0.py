import pytest
import asyncio
from pathlib import Path
import subprocess

from xianyu_manager.database import Database
from xianyu_manager.fulfillment_rules import matches_registered_listing
from xianyu_manager.fulfillment_rules import fulfillment_fingerprint, delivery_issues
from xianyu_manager.scanner import ScannedProduct


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    database.sync_products(
        [
            ScannedProduct(
                dir_name="01-test",
                number=1,
                name="test",
                title="test",
                zip_name="test.zip",
                zip_hash="a" * 64,
                zip_size=42,
                image_count=5,
                quality_status="passed",
                quality_errors=[],
                scanned_at="old",
            )
        ]
    )
    database.ensure_default_accounts()
    database.update_product(
        "01-test", {"share_url": "https://pan.baidu.com/s/test", "share_code": "fake"}
    )
    return database


def test_ordinary_save_cannot_verify_new_share(db):
    product = db.update_product(
        "01-test", {"share_url": "https://pan.baidu.com/s/new", "share_verified": True}
    )
    assert not product["share_verified"]
    assert product["share_needs_review"]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.goofish.com/item?id=1234",
        "https://www.goofish.com/item?other=id=123",
        "https://evil.goofish.com/item?id=123",
        "https://goofish.com.evil/item?id=123",
        "https://www.goofish.com/item?id=123&id=1234",
    ],
)
def test_exact_listing_rejects_prefix_and_ambiguity(url):
    assert not matches_registered_listing(
        {
            "enabled_for_account": True,
            "listing_status": "published",
            "listing_url": url,
        },
        "123",
    )


def test_mapping_requires_account_id(db):
    with pytest.raises(ValueError, match="account_id"):
        db.get_product_by_listing_item_id("123")


@pytest.mark.parametrize(
    "url",
    [
        "http://www.goofish.com/item?id=123",
        "https://www.goofish.com/item?id=123&id=123",
        "https://www.goofish.com/item?id=",
        "https://user@www.goofish.com/item?id=123",
        "https://www.goofish.com:443/item?id=123",
        "https://www.goofish.com/other?id=123",
    ],
)
def test_listing_url_validation(url):
    from xianyu_manager.fulfillment_rules import parse_listing_id

    assert not parse_listing_id(url)


def test_valid_listing_preserves_string_id_with_extra_parameters():
    from xianyu_manager.fulfillment_rules import parse_listing_id

    assert (
        parse_listing_id("https://www.goofish.com/item?source=test&id=00123&other=1234")
        == "00123"
    )


def test_explicit_confirmation_api_and_stale_page(db, monkeypatch):
    from fastapi.testclient import TestClient
    from xianyu_manager import app as api

    monkeypatch.setattr(api, "database", db)
    client = TestClient(
        api.app, base_url="http://127.0.0.1:8765"
    )  # No lifespan or real services.
    endpoint = "/api/products/01-test/verify-share"
    fingerprint = fulfillment_fingerprint(db.get_product("01-test"))
    response = client.post(endpoint, json={"fingerprint": fingerprint})
    assert response.status_code == 200
    assert response.json()["online_checked"] is False
    db.update_product("01-test", {"share_code": "new"})
    assert client.post(endpoint, json={"fingerprint": fingerprint}).status_code == 409


def test_safe_database_confirmation_and_claim_blocked(db):
    safe = Database(db.path, safe_mode=True)
    with pytest.raises(ValueError, match="SAFE_MODE"):
        safe.confirm_product_share(
            "01-test", fulfillment_fingerprint(db.get_product("01-test"))
        )
    with pytest.raises(ValueError, match="SAFE_MODE"):
        safe.claim_verified_delivery("order", 1, "123")
    assert not safe.validate_delivery_claim("order", 1, "chat", "text")


def test_same_account_duplicate_mapping_is_not_arbitrarily_selected(db):
    account = setup_order(db)
    db.sync_products(
        [
            ScannedProduct(
                dir_name="02-test",
                number=2,
                name="test2",
                title="test2",
                zip_name="test.zip",
                zip_hash="a" * 64,
                zip_size=42,
                image_count=5,
                quality_status="passed",
                quality_errors=[],
                scanned_at="new",
            )
        ]
    )
    db.update_product(
        "02-test",
        {
            "enabled_for_account": True,
            "listing_url": "https://www.goofish.com/item?id=123",
            "listing_status": "published",
        },
        account,
    )
    assert db.get_product_by_listing_item_id("123", account) is None
    assert db.claim_verified_delivery("order", account, "123") is None


def confirm(db):
    db.confirm_product_share(
        "01-test", fulfillment_fingerprint(db.get_product("01-test"))
    )


@pytest.mark.parametrize(
    "change",
    [
        {"share_url": "https://pan.baidu.com/s/new"},
        {"share_code": "new"},
        {"share_code": ""},
        {"share_code": None},
    ],
)
def test_share_changes_revoke_even_with_old_true(db, change):
    confirm(db)
    old = db.get_product("01-test")["fulfillment_fingerprint"]
    current = db.update_product("01-test", {**change, "share_verified": True})
    assert not current["share_verified"] and current["share_needs_review"]
    with pytest.raises(ValueError, match="STALE"):
        db.confirm_product_share("01-test", old)


def test_partial_save_and_repeat_save_keep_confirmation(db):
    confirm(db)
    old = db.get_product("01-test")
    for fields in (
        {"suggested_price_cents": 200},
        {"share_code": "fake"},
        {"share_url": old["share_url"]},
        {"share_verified": None},
    ):
        current = db.update_product("01-test", fields)
        assert current["verified_fingerprint"] == old["verified_fingerprint"]
        assert current["share_verified"] and not current["share_needs_review"]
    revoked = db.update_product("01-test", {"share_verified": False})
    assert not revoked["share_verified"]


@pytest.mark.parametrize("new_hash", ["b" * 64, ""])
def test_scanner_invalidates_zip_change_and_stale_page(db, new_hash):
    confirm(db)
    old = db.get_product("01-test")

    def scan(hash_value):
        db.sync_products(
            [
                ScannedProduct(
                    dir_name="01-test",
                    number=1,
                    name="test",
                    title="test",
                    zip_name="test.zip" if hash_value else "",
                    zip_hash=hash_value,
                    zip_size=42 if hash_value else 0,
                    image_count=5,
                    quality_status="passed",
                    quality_errors=[],
                    scanned_at="new",
                )
            ]
        )

    scan("a" * 64)
    assert db.get_product("01-test")["share_verified"]
    scan(new_hash)
    assert not db.get_product("01-test")["share_verified"]
    with pytest.raises(ValueError, match="STALE"):
        db.confirm_product_share("01-test", old["fulfillment_fingerprint"])


def setup_order(db):
    account_id = db.get_active_account()["id"]
    db.update_product(
        "01-test",
        {
            "enabled_for_account": True,
            "listing_url": "https://www.goofish.com/item?id=123",
            "listing_status": "published",
        },
        account_id,
    )
    with db.connect() as c:
        c.execute(
            "UPDATE accounts SET binding_status='bound',delivery_enabled=1 WHERE id=?",
            (account_id,),
        )
    confirm(db)
    db.upsert_paid_order(
        order_id="order",
        account_id=account_id,
        product_dir_name="01-test",
        listing_item_id="123",
        buyer_id="buyer",
        chat_id="chat",
        event_fingerprint="event",
    )
    return account_id


def test_two_connections_claim_locks_verified_snapshot(db):
    account = setup_order(db)
    other = Database(db.path)
    prepared = db.claim_verified_delivery("order", account, "123")
    assert prepared is not None
    assert other.claim_verified_delivery("order", account, "123") is None
    with pytest.raises(ValueError, match="DELIVERY_IN_FLIGHT"):
        other.update_product("01-test", {"share_code": "changed"}, account)
    assert db.validate_delivery_claim("order", account, "chat", prepared["message"])
    assert not db.validate_delivery_claim(
        "order", account, "chat", prepared["message"] + "changed"
    )
    assert not db.validate_delivery_claim("order", 1, "chat", prepared["message"])


def test_change_before_claim_blocks_and_cross_account_event_rejected(db):
    account = setup_order(db)
    other = Database(db.path)
    other.update_product("01-test", {"share_code": "changed"}, account)
    assert db.claim_verified_delivery("order", account, "123") is None
    assert db.get_product_by_listing_item_id("123", 1) is None
    with pytest.raises(ValueError, match="CONFLICT"):
        db.upsert_paid_order(
            order_id="order",
            account_id=1,
            product_dir_name="01-test",
            listing_item_id="123",
            buyer_id="buyer",
            chat_id="chat",
            event_fingerprint="other",
        )


@pytest.mark.parametrize(
    "stage",
    ["sending", "manual_review", "confirm_pending", "message_sent", "delivered"],
)
def test_no_reclaim_uncertain_or_sent_order(db, stage):
    account = setup_order(db)
    with db.connect() as c:
        c.execute("UPDATE orders SET delivery_status=?", (stage,))
    assert db.claim_verified_delivery("order", account, "123") is None


def test_duplicate_mapping_and_manual_takeover_block(db):
    account = setup_order(db)
    with db.connect() as c:
        c.execute(
            "INSERT INTO chat_sessions(account_id,chat_id,manual_takeover_until) VALUES(?,'chat','2999-01-01')",
            (account,),
        )
    assert db.claim_verified_delivery("order", account, "123") is None
    with db.connect() as c:
        c.execute(
            "INSERT INTO account_products(account_id,product_dir_name,enabled,listing_url,listing_status) VALUES(1,'01-test',1,'https://www.goofish.com/item?id=123','published') ON CONFLICT DO UPDATE SET listing_url=excluded.listing_url,listing_status=excluded.listing_status"
        )
    assert db.get_product_by_listing_item_id("123", account)["account_id"] == account


def test_publishing_warning_allowed_blocking_quality_rejected(db):
    confirm(db)
    p = db.get_product("01-test")
    p["quality_errors"] = ["第一行标题超过 20 个字符"]
    assert delivery_issues(p) == []
    p["quality_errors"] = ["交付包.zip: 压缩包成员损坏"]
    assert "QUALITY_BLOCKED" in delivery_issues(p)


def test_confirm_pending_only_confirms_platform_despite_changed_share(db, monkeypatch):
    from xianyu_manager.runtime_policy import RuntimePolicy
    from xianyu_manager.delivery import DeliveryService
    from xianyu_manager.security import SecretStore

    account = setup_order(db)
    with db.connect() as c:
        c.execute(
            "UPDATE orders SET delivery_status='confirm_pending',message_sent_at='2026-01-01',delivery_message_hash='already-sent'"
        )
    db.update_product("01-test", {"share_code": "new"}, account)
    service = DeliveryService(
        Path("/unused"), None, db, SecretStore(db.path.parent / "secret")
    )
    service._account_id = account
    events = []

    async def preflight(*args):
        return {"allowed": True}

    async def platform(*args):
        events.append("platform")

    async def no_text(*args):
        pytest.fail("confirm_pending resent message")

    monkeypatch.setattr(service, "_outbound_preflight", preflight)
    monkeypatch.setattr(service, "_confirm_platform_delivery", platform)
    service.runtime_policy = RuntimePolicy(order_cutoff_at='2020-01-01T00:00:00Z')
    monkeypatch.setattr(service, "_send_text", no_text)
    asyncio.run(service.retry_platform_confirmation("order", account, {}))
    assert events == ["platform"]
    assert db.get_order("order")["delivery_message_hash"] == "already-sent"


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("path", ["normal", "recovery", "manual"])
def test_delivery_paths_use_shared_rules_and_uncertain_ack_is_not_retried(
    db, monkeypatch, blocked, path
):
    from xianyu_manager.delivery import DeliveryService
    from xianyu_manager.security import SecretStore

    account = setup_order(db)
    item = "1068148111818"
    order = "223344556677889900"
    db.update_product(
        "01-test", {"listing_url": f"https://www.goofish.com/item?id={item}"}, account
    )
    if blocked:
        db.update_product("01-test", {"share_code": "new"}, account)
    service = DeliveryService(
        db.path.parent / "profiles", None, db, SecretStore(db.path.parent / "secret")
    )
    service._account_id = account
    service._runtime_seller_id = "111111"
    from xianyu_manager.runtime_policy import RuntimePolicy
    service.runtime_policy = RuntimePolicy(order_cutoff_at='2020-01-01T00:00:00Z')
    service._status = "listening"
    service._runtime_cookie_map = {}
    service._runtime_websocket = object()
    calls = []

    async def create(*args):
        return "667788"

    async def fetch(*args, **kwargs):
        import time

        return {
            "orders": [
                {
                    "order_id": order,
                    "item_id": item,
                    "buyer_id": "998877",
                    "order_status": "待发货",
                    "paid_time": int(time.time()),
                }
            ]
        }

    async def send(*args, **kwargs):
        calls.append("send")
        raise asyncio.TimeoutError()

    async def preflight(*args):
        return {"allowed": True}

    monkeypatch.setattr(service, "_fetch_recent_sold_orders", fetch)
    monkeypatch.setattr(service, "_create_chat", create)
    monkeypatch.setattr(service, "_send_text", send)
    monkeypatch.setattr(service, "_outbound_preflight", preflight)
    event = {"orderId": order, "itemId": item, "buyerId": "998877", "sid": "667788"}

    async def execute():
        if path == "normal":
            await service._process_paid_event(
                service._runtime_websocket, account, "111111", {}, event
            )
        elif path == "recovery":
            await service._recover_recent_paid_orders(
                service._runtime_websocket, account, "111111", {}
            )
        else:
            try:
                await service.reconcile_order(order)
            except ValueError:
                assert blocked

    asyncio.run(execute())
    assert calls == ([] if blocked else ["send"])
    if not blocked:
        assert db.get_order(order)["delivery_status"] == "manual_review"
        asyncio.run(execute())
        assert calls == ["send"]


def test_real_frontend_partial_save_and_confirmation_requests():
    repo = Path(__file__).resolve().parents[1]
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('static/app.js','utf8');
const elements={}; const el=id=>elements[id]||(elements[id]={value:'',checked:false,close(){},showModal(){}});
const requests=[];
const context={el,state:{},Object,JSON,encodeURIComponent,
 inputToCents:v=>v===''?null:Math.round(Number(v)*100),
 loadProducts:async()=>{},openEdit:()=>{},showActionNotice:()=>{},
 fetch:async(url,options)=>{requests.push({url,...options});return {ok:true}}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function readEditFields()'),source.indexOf('async function copyListing(')),context);
(async()=>{
 context.state.editBaseline=context.readEditFields();
 el('suggestedPrice').value='2';
 await context.saveEdit({preventDefault(){}});
 assert.deepStrictEqual(JSON.parse(requests[0].body),{suggested_price_cents:200});
 requests.length=0;
 context.state.editBaseline=context.readEditFields();context.state.editFingerprint='old-fingerprint';
 el('shareCode').value='changed';
 await context.confirmShare(false);assert.equal(requests.length,0);
 await context.saveEdit({preventDefault(){}});
 assert.deepStrictEqual(JSON.parse(requests[0].body),{share_code:'changed'});
 requests.length=0;context.state.editBaseline=context.readEditFields();
 await context.confirmShare(false);
 assert.equal(requests[0].method,'POST');
 assert.equal(JSON.parse(requests[0].body).fingerprint,'old-fingerprint');
 requests.length=0;await context.confirmShare(true);
 assert.deepStrictEqual(JSON.parse(requests[0].body),{share_verified:false});
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(
        ["node", "-e", script], cwd=repo, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr


def test_listing_share_partial_save_preserves_code_without_verifying(db):
    account = db.get_active_account()["id"]
    item = "1068279950541"
    db.sync_live_listings(
        account,
        [
            {
                "item_id": item,
                "title": "synthetic",
                "url": f"https://www.goofish.com/item?id={item}",
                "matched_product_dir_name": None,
            }
        ],
    )
    db.configure_listing_delivery(
        account,
        item,
        share_url="https://pan.baidu.com/s/fake",
        share_code="code",
        share_verified=True,
    )
    current = db.configure_listing_delivery(
        account, item, share_url="https://pan.baidu.com/s/fake"
    )
    assert current["share_code"] == "code"
    assert not current["share_verified"] and not current["delivery_ready"]
    cleared = db.configure_listing_delivery(
        account, item, share_url="https://pan.baidu.com/s/fake", share_code=""
    )
    assert cleared["share_code"] == ""


def test_legacy_verified_flag_is_not_current_version_evidence(db):
    with db.connect() as connection:
        connection.execute("UPDATE products SET share_verified=1,share_needs_review=0")
    assert "VERIFICATION_VERSION_UNCONFIRMED" in delivery_issues(
        db.get_product("01-test")
    )


def test_claim_rejects_changed_buyer_and_unpaid_order(db):
    account = setup_order(db)
    prepared = db.claim_verified_delivery("order", account, "123")
    assert not db.validate_delivery_claim(
        "order", account, "chat", prepared["message"], buyer_id="other"
    )
    with pytest.raises(ValueError, match="CONFLICT"):
        db.upsert_paid_order(
            order_id="order",
            account_id=account,
            product_dir_name="01-test",
            listing_item_id="123",
            buyer_id="other",
            chat_id="chat",
            event_fingerprint="other",
        )
    with db.connect() as connection:
        connection.execute("UPDATE orders SET payment_status='unpaid'")
    assert not db.validate_delivery_claim("order", account, "chat", prepared["message"])


def test_scanner_on_second_connection_cannot_replace_in_flight_package(db):
    account = setup_order(db)
    prepared = db.claim_verified_delivery("order", account, "123")
    other = Database(db.path)
    with pytest.raises(ValueError, match="DELIVERY_IN_FLIGHT"):
        other.sync_products(
            [
                ScannedProduct(
                    dir_name="01-test",
                    number=1,
                    name="test",
                    title="test",
                    zip_name="new.zip",
                    zip_hash="b" * 64,
                    zip_size=50,
                    image_count=5,
                    quality_status="passed",
                    quality_errors=[],
                    scanned_at="new",
                )
            ]
        )
    assert db.validate_delivery_claim("order", account, "chat", prepared["message"])
    assert db.get_product("01-test")["zip_hash"] == "a" * 64


def test_offline_and_online_share_same_product_block_and_are_read_only(db):
    import hashlib
    from xianyu_manager.offline_catalog import read_catalog

    setup_order(db)
    db.update_product("01-test", {"share_code": "new"})
    before = hashlib.sha256(db.path.read_bytes()).hexdigest()
    record = read_catalog(db.path)["records"][0]
    issues = delivery_issues(db.get_product("01-test"))
    assert set(issues) <= set(record["issues"])
    assert record["delivery_text"] is None
    assert hashlib.sha256(db.path.read_bytes()).hexdigest() == before
