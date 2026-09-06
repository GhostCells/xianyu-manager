from __future__ import annotations

import pytest

from xianyu_manager.database import Database
from xianyu_manager.scanner import ScannedProduct


def product(zip_hash: str = "abc", quality_status: str = "passed") -> ScannedProduct:
    return ScannedProduct(
        dir_name="01-测试商品",
        number=1,
        name="测试商品",
        title="测试标题",
        zip_name="测试商品-交付包.zip",
        zip_hash=zip_hash,
        zip_size=128,
        image_count=5,
        quality_status=quality_status,
        quality_errors=[] if quality_status == "passed" else ["交付资料待完善"],
        scanned_at="2026-07-26T00:00:00+00:00",
    )


def test_sync_and_update_product(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products([product()])
    database.ensure_default_accounts()
    updated = database.update_product(
        "01-测试商品",
        {
            "enabled_for_account": True,
            "share_url": "https://pan.baidu.com/s/example",
            "share_verified": True,
        },
    )
    assert updated is not None
    assert updated["share_verified"] is False
    assert updated["share_needs_review"] is True
    assert updated["knowledge_text"] == ""
    assert updated["knowledge_chars"] == 0


def test_selection_mvp_schema_is_created_with_nullable_uncollected_metrics(tmp_path):
    database = Database(tmp_path / "manager.db")

    expected_tables = {
        "selection_items",
        "selection_search_runs",
        "selection_item_snapshots",
        "selection_scores",
    }
    with database.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert expected_tables <= tables

        connection.execute(
            """
            INSERT INTO selection_items (item_id, title_raw, canonical_url)
            VALUES ('123', '测试商品', 'https://www.goofish.com/item?id=123')
            """
        )
        connection.execute(
            """
            INSERT INTO selection_search_runs (
                run_id, keyword, status, result_count
            ) VALUES ('run-1', 'skill', 'success', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO selection_item_snapshots (
                run_id, item_id, keyword, search_rank, title_raw,
                price_parse_status, detail_status
            ) VALUES (
                'run-1', '123', 'skill', 1, '测试商品',
                'missing', 'skipped'
            )
            """
        )
        snapshot = connection.execute(
            "SELECT * FROM selection_item_snapshots WHERE item_id='123'"
        ).fetchone()

    assert snapshot is not None
    assert snapshot["price_cents"] is None
    assert snapshot["want_count"] is None
    assert snapshot["browse_count"] is None
    assert snapshot["collect_count"] is None


def test_manual_knowledge_folder_survives_product_rescan(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products([product()])
    database.ensure_default_accounts()
    updated = database.set_product_knowledge_folder(
        "01-测试商品",
        source_path=r"D:\资料\测试商品",
        knowledge_text="这是手动载入的商品资料",
        knowledge_hash="manual-hash",
        knowledge_chars=11,
        file_count=2,
    )
    assert updated is not None
    database.sync_products([product("new")])

    rescanned = database.get_product("01-测试商品")
    assert rescanned is not None
    assert rescanned["knowledge_source_path"] == r"D:\资料\测试商品"
    assert rescanned["knowledge_text"] == "这是手动载入的商品资料"
    assert rescanned["knowledge_file_count"] == 2


def test_zip_change_requires_link_review(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products([product("old")])
    database.ensure_default_accounts()
    database.update_product("01-测试商品", {"share_verified": True})
    database.sync_products([product("new")])
    changed = database.get_product("01-测试商品")
    assert changed is not None
    assert changed["share_needs_review"] is True


def test_existing_products_are_archived_under_old_account(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products([product()])
    database.ensure_default_accounts()
    database.ensure_legacy_product_policy()

    accounts = database.list_accounts()
    old_account = next(account for account in accounts if account["name"].startswith("旧账号"))
    new_account = next(account for account in accounts if account["is_active"])

    assert old_account["product_count"] == 1
    assert new_account["product_count"] == 0
    new_product_view = database.list_products(int(new_account["id"]))[0]
    assert new_product_view["enabled_for_account"] is False
    assert new_product_view["catalog_status"] == "legacy"

    with pytest.raises(ValueError, match="旧账号历史商品"):
        database.update_product(
            "01-测试商品",
            {"enabled_for_account": True},
            int(new_account["id"]),
        )


def test_single_account_mode_archives_history_and_keeps_new_account(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products([product()])
    database.ensure_default_accounts()
    active = database.enforce_single_account_mode("七月账号")

    assert active["name"] == "七月账号"
    accounts = database.list_accounts()
    assert len(accounts) == 1
    assert accounts[0]["is_active"] is True
    assert database.activate_account(1) is None


def test_live_inventory_sync_keeps_explicit_matches_and_unmatched_items(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products([product()])
    database.ensure_default_accounts()
    account = database.get_active_account()
    database.update_product(
        "01-测试商品",
        {
            "share_url": "https://pan.baidu.com/s/example",
            "share_verified": True,
        },
        int(account["id"]),
    )
    rows = database.sync_live_listings(
        int(account["id"]),
        [
            {
                "item_id": "123456789",
                "title": "测试标题",
                "url": "https://www.goofish.com/item?id=123456789",
                "price_cents": 688,
                "matched_product_dir_name": "01-测试商品",
            },
            {
                "item_id": "987654321",
                "title": "未匹配商品",
                "url": "https://www.goofish.com/item?id=987654321",
                "price_cents": 299,
                "matched_product_dir_name": None,
            },
        ],
    )

    assert len(rows) == 2
    matched = next(item for item in rows if item["item_id"] == "123456789")
    assert matched["delivery_ready"] is False  # ordinary save is not confirmation
    assert matched["matched_product_dir_name"] == "01-测试商品"
    assert next(item for item in rows if item["item_id"] == "987654321")["delivery_ready"] is False


def test_unmatched_listing_can_be_manually_mapped_and_keeps_item_id(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products([product()])
    database.ensure_default_accounts()
    account = database.get_active_account()
    account_id = int(account["id"])
    database.sync_live_listings(
        account_id,
        [
            {
                "item_id": "987654321",
                "title": "改名后的商品标题",
                "url": "https://www.goofish.com/item?id=987654321",
                "price_cents": 1480,
                "matched_product_dir_name": None,
            }
        ],
    )

    mapped = database.map_live_listing_to_product(
        account_id, "987654321", "01-测试商品"
    )

    assert mapped["matched_product_dir_name"] == "01-测试商品"
    configured = database.get_product("01-测试商品", account_id)
    assert configured is not None
    assert configured["enabled_for_account"] is True
    assert configured["listing_status"] == "published"
    assert configured["listing_url"] == "https://www.goofish.com/item?id=987654321"
    assert configured["confirmed_price_cents"] == 1480
    assert database.get_product_by_listing_item_id("987654321", account_id) is not None


def test_incomplete_local_product_can_be_mapped_but_is_not_delivery_ready(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products([product(quality_status="failed")])
    database.ensure_default_accounts()
    account = database.get_active_account()
    account_id = int(account["id"])
    database.sync_live_listings(
        account_id,
        [
            {
                "item_id": "987654321",
                "title": "待手动关联的商品",
                "url": "https://www.goofish.com/item?id=987654321",
                "price_cents": 990,
                "matched_product_dir_name": None,
            }
        ],
    )

    mapped = database.map_live_listing_to_product(
        account_id, "987654321", "01-测试商品"
    )

    assert mapped["matched_product_dir_name"] == "01-测试商品"
    assert mapped["delivery_ready"] is False


def test_quality_failed_blocks_delivery_even_with_registered_link(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products([product(quality_status="failed")])
    database.ensure_default_accounts()
    account = database.get_active_account()
    account_id = int(account["id"])
    database.update_product(
        "01-测试商品",
        {
            "share_url": "https://pan.baidu.com/s/example",
            "share_code": "abcd",
            "share_verified": True,
        },
        account_id,
    )
    database.sync_live_listings(
        account_id,
        [
            {
                "item_id": "987654321",
                "title": "已确认网盘链接的商品",
                "url": "https://www.goofish.com/item?id=987654321",
                "price_cents": 990,
                "matched_product_dir_name": "01-测试商品",
            }
        ],
    )

    listing = database.list_live_listings(account_id)[0]
    assert listing["quality_status"] == "failed"
    assert listing["delivery_ready"] is False


def test_configured_published_listing_is_safely_merged_without_hiding_snapshot(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products([product()])
    database.ensure_default_accounts()
    account = database.get_active_account()
    account_id = int(account["id"])
    database.update_product(
        "01-测试商品",
        {
            "enabled_for_account": True,
            "listing_url": "https://www.goofish.com/item?id=123456789",
            "listing_status": "published",
        },
        account_id,
    )
    database.sync_live_listings(
        account_id,
        [
            {
                "item_id": "987654321",
                "title": "账号快照商品",
                "url": "https://www.goofish.com/item?id=987654321",
                "price_cents": 299,
                "matched_product_dir_name": None,
            }
        ],
    )

    rows = database.reconcile_configured_listings(account_id)

    assert {item["item_id"] for item in rows} == {"123456789", "987654321"}
    configured = next(item for item in rows if item["item_id"] == "123456789")
    assert configured["source_kind"] == "configured"
    assert configured["matched_product_dir_name"] == "01-测试商品"
    snapshot = next(item for item in rows if item["item_id"] == "987654321")
    assert snapshot["source_kind"] == "account_snapshot"


def test_account_binding_state_contains_no_cookie_data(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products([product()])
    database.ensure_default_accounts()
    account = database.get_active_account()

    updated = database.update_account_binding(int(account["id"]), "detected")
    assert updated is not None
    assert updated["binding_status"] == "detected"
    assert updated["session_last_checked_at"] is not None

    confirmed = database.update_account_binding(int(account["id"]), "bound", confirmed=True)
    assert confirmed is not None
    assert confirmed["binding_status"] == "bound"
    assert confirmed["binding_confirmed_at"] is not None
    assert all("cookie" not in key.lower() for key in confirmed)


def test_paid_order_delivery_is_idempotent(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products([product()])
    database.ensure_default_accounts()
    account = database.get_active_account()
    database.update_product(
        "01-测试商品",
        {
            "enabled_for_account": True,
            "share_url": "https://pan.baidu.com/s/example",
            "share_verified": True,
            "listing_url": "https://www.goofish.com/item?id=123456789",
            "listing_status": "published",
        },
    )

    database.upsert_paid_order(
        order_id="987654321012345678",
        account_id=int(account["id"]),
        product_dir_name="01-测试商品",
        listing_item_id="123456789",
        buyer_id="222222",
        chat_id="333333",
        event_fingerprint="fingerprint",
    )
    assert database.claim_order_delivery("987654321012345678") is True
    assert database.claim_order_delivery("987654321012345678") is False

    database.mark_order_message_sent("987654321012345678", "message-hash")
    database.mark_order_delivered("987654321012345678", platform_status="confirmed")
    assert database.claim_order_delivery("987654321012345678") is False
    order = database.get_order("987654321012345678")
    assert order is not None
    assert order["delivery_status"] == "delivered"
    assert order["delivery_attempts"] == 1


def test_delivery_settings_round_trip(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products([product()])
    database.ensure_default_accounts()
    account = database.get_active_account()

    updated = database.update_delivery_settings(
        int(account["id"]), enabled=True, auto_confirm=True, auto_free_group=True
    )
    assert updated is not None
    assert updated["delivery_enabled"] is True
    assert updated["auto_confirm_delivery"] is True
    assert updated["auto_free_group"] is True


def test_group_exemption_is_idempotent_and_waits_before_delivery(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products([product()])
    database.ensure_default_accounts()
    account = database.get_active_account()

    database.upsert_group_waiting_order(
        order_id="987654321012345678",
        account_id=int(account["id"]),
        product_dir_name="01-测试商品",
        listing_item_id="123456789",
        buyer_id="222222",
        chat_id="333333",
        event_fingerprint="group-fingerprint",
    )
    assert database.claim_group_exemption("987654321012345678") is True
    assert database.claim_group_exemption("987654321012345678") is False
    database.mark_group_exempted("987654321012345678")
    assert database.claim_order_delivery("987654321012345678") is False

    database.upsert_paid_order(
        order_id="987654321012345678",
        account_id=int(account["id"]),
        product_dir_name="01-测试商品",
        listing_item_id="123456789",
        buyer_id="222222",
        chat_id="333333",
        event_fingerprint="ready-fingerprint",
    )
    assert database.claim_order_delivery("987654321012345678") is True
    order = database.get_order("987654321012345678")
    assert order is not None
    assert order["group_status"] == "exempted"


def test_automation_safety_limits_duplicate_sends_and_persists_circuit(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.ensure_default_accounts()
    account_id = int(database.get_active_account()["id"])
    settings = database.update_automation_safety(
        account_id,
        {
            "max_replies_per_hour": 1,
            "max_replies_per_day": 2,
            "max_deliveries_per_hour": 3,
            "min_outbound_interval_seconds": 2,
            "risk_cooldown_minutes": 30,
            "notifications_enabled": False,
        },
    )
    assert settings["max_replies_per_hour"] == 1
    assert settings["notifications_enabled"] is False

    reservation = database.reserve_automation_outbound(
        account_id, "reply", "chat-message:1"
    )
    assert reservation["allowed"] is True
    database.finish_automation_outbound(int(reservation["event_id"]), sent=True)

    limited = database.reserve_automation_outbound(
        account_id, "reply", "chat-message:2"
    )
    assert limited["allowed"] is False
    assert "上限" in str(limited["reason"]) or "过近" in str(limited["reason"])

    duplicate = database.reserve_automation_outbound(
        account_id, "reply", "chat-message:1"
    )
    assert duplicate["allowed"] is False

    opened = database.open_automation_circuit(account_id, "平台要求验证")
    assert opened["circuit"]["is_open"] is True
    assert database.check_automation_outbound(account_id, "delivery")["allowed"] is False
    cleared = database.clear_automation_circuit(account_id)
    assert cleared["circuit"]["is_open"] is False

    report = database.record_startup_report(
        account_id, {"status": "listening", "message": "监听已恢复"}
    )
    assert report["startup"]["report"]["status"] == "listening"


def test_unmatched_live_listing_can_receive_an_isolated_delivery_profile(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.ensure_default_accounts()
    account_id = int(database.get_active_account()["id"])
    database.sync_live_listings(
        account_id,
        [
            {
                "item_id": "1068279950541",
                "title": "AI短视频本地剪辑Skill",
                "url": "https://www.goofish.com/item?id=1068279950541",
                "price_cents": 1990,
                "matched_product_dir_name": None,
            }
        ],
    )

    configured = database.configure_listing_delivery(
        account_id,
        "1068279950541",
        share_url="https://pan.baidu.com/s/example?pwd=9r1v",
        share_code="9r1v",
        share_verified=True,
    )

    assert configured["delivery_ready"] is False  # listing-only package remains unconfirmed
    assert configured["share_code"] == "9r1v"
    assert configured["matched_product_dir_name"] == "__listing__1068279950541"
    assert all(
        product["dir_name"] != "__listing__1068279950541"
        for product in database.list_products(account_id)
    )
    delivery_product = database.get_product_by_listing_item_id(
        "1068279950541", account_id
    )
    assert delivery_product is not None
    assert delivery_product["share_url"].endswith("pwd=9r1v")

    database.sync_live_listings(
        account_id,
        [
            {
                "item_id": "1068279950541",
                "title": "AI短视频本地剪辑Skill",
                "url": "https://www.goofish.com/item?id=1068279950541",
                "price_cents": 1990,
                "matched_product_dir_name": None,
            }
        ],
    )
    assert database.get_product_by_listing_item_id("1068279950541", account_id) is not None
