from __future__ import annotations

import json
import os
import time
import asyncio
from pathlib import Path

import pytest

from xianyu_manager.auto_reply import (
    ReplyDecision,
    SiliconFlowReplyClient,
    humanize_reply_text,
    siliconflow_chat_endpoint,
    manual_review_reason,
    normalize_decision,
)
from xianyu_manager.database import Database
from xianyu_manager.delivery import (
    DeliveryService,
    combine_buyer_messages,
    extract_plain_chat_message,
)
from xianyu_manager.security import SecretStore
from xianyu_manager.scanner import ScannedProduct


def plain_chat_event(*, sender_id: str = "998877", content: str = "这个适合新手吗"):
    return {
        "1": {
            "2": "667788@goofish",
            "5": str(int(time.time() * 1000)),
            "10": {
                "senderUserId": sender_id,
                "reminderContent": content,
                "reminderUrl": "https://www.goofish.com/im?itemId=1068148111818",
            },
            "6": {"3": {"4": 1}},
        }
    }


def test_extract_plain_chat_message_distinguishes_buyer_and_seller():
    buyer = extract_plain_chat_message(plain_chat_event(), "111111")
    assert buyer == {
        "chat_id": "667788",
        "item_id": "1068148111818",
        "sender_id": "998877",
        "direction": "inbound",
        "content": "这个适合新手吗",
    }

    seller = extract_plain_chat_message(
        plain_chat_event(sender_id="111111", content="我来人工处理"), "111111"
    )
    assert seller is not None
    assert seller["direction"] == "outbound"


def test_extract_plain_chat_message_ignores_system_style_messages():
    assert extract_plain_chat_message(
        plain_chat_event(content="[我已付款，等待你发货]"), "111111"
    ) is None


def test_deepseek_decision_is_bounded_and_sensitive_reply_is_blocked():
    reply = normalize_decision(
        json.dumps({"action": "reply", "reply": "可以，新手按说明操作就行。", "reason": "普通售前"}),
        80,
    )
    assert reply.action == "reply"
    assert "新手" in reply.reply

    blocked = normalize_decision(
        json.dumps({"action": "reply", "reply": "加我微信后给你链接", "reason": ""}),
        80,
    )
    assert blocked.action == "manual"
    assert manual_review_reason("我要退款并投诉")

    clarify = normalize_decision(
        json.dumps(
            {
                "action": "clarify",
                "reply": "你想了解安装配置，还是生成效果？",
                "reason": "问题范围较宽",
            },
            ensure_ascii=False,
        ),
        80,
    )
    assert clarify.action == "clarify"


def test_humanize_reply_removes_ai_scaffolding_and_preserves_facts():
    reply = humanize_reply_text(
        "您好，简单来说，MiniMax H3 需要 12GB 显存。如果还有其他问题，可以随时问我。"
    )
    assert reply == "MiniMax H3 需要 12GB 显存。"
    assert humanize_reply_text("请问您是指5070显卡的显存是12GB吗？") == (
        "你是指5070显卡的显存是12GB吗？"
    )
    assert humanize_reply_text("可以，适合新手。") == "可以，适合新手。"
    assert humanize_reply_text("不是远程安装，是本地部署。") == "不是远程安装，是本地部署。"
    assert humanize_reply_text(
        "你是指导演台这个功能吗？商品资料里没有提到这个名称，能具体说一下你指的是什么功能吗？"
    ) == "你说的导演台具体是指什么功能？"


def test_normalize_decision_humanizes_before_length_check():
    decision = normalize_decision(
        json.dumps(
            {
                "action": "reply",
                "reply": "您好，简单来说，支持本地运行。如果还有问题，可以随时问我。",
                "reason": "资料明确",
            },
            ensure_ascii=False,
        ),
        10,
    )
    assert decision == ReplyDecision(action="reply", reply="支持本地运行。", reason="资料明确")


def test_rapid_buyer_fragments_are_combined_as_one_question():
    assert combine_buyer_messages(["这是 ComfyUI", "嘛"]) == "这是 ComfyUI 嘛"
    assert combine_buyer_messages(["有什么功能", "有什么功能", "和效果"]) == (
        "有什么功能 和效果"
    )


def test_siliconflow_endpoint_only_accepts_official_https_host():
    assert siliconflow_chat_endpoint("https://api.siliconflow.cn/v1") == (
        "https://api.siliconflow.cn/v1/chat/completions"
    )
    with pytest.raises(ValueError):
        siliconflow_chat_endpoint("http://127.0.0.1:9000/v1")
    with pytest.raises(ValueError):
        siliconflow_chat_endpoint("https://api.deepseek.com")


def test_siliconflow_client_uses_v4_flash_non_thinking_json(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {"x-siliconcloud-trace-id": "trace-test"}

        @staticmethod
        def json():
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "reply",
                                    "reply": "可以，适合新手。",
                                    "reason": "",
                                    "evidence": "适合新手的 AI 工具",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            captured.update({"url": url, "headers": headers, "body": json})
            return FakeResponse()

    monkeypatch.setattr(
        "xianyu_manager.auto_reply.httpx.AsyncClient", FakeAsyncClient
    )
    decision = asyncio.run(
        SiliconFlowReplyClient().generate(
            api_key="sk-test",
            base_url="https://api.siliconflow.cn/v1",
            model="deepseek-ai/DeepSeek-V4-Flash",
            system_prompt="安全回复",
            buyer_message="适合新手吗",
            product={
                "name": "测试商品",
                "title": "适合新手的 AI 工具",
                "confirmed_price_cents": 19900,
                "listing_status": "published",
                "knowledge_text": "适合新手的 AI 工具\n包含基础操作说明。\n提取码：9r1v\nhttps://pan.baidu.com/s/example",
            },
            context=[],
            max_reply_chars=180,
        )
    )

    assert decision.action == "reply"
    assert captured["url"] == "https://api.siliconflow.cn/v1/chat/completions"
    assert captured["body"]["model"] == "deepseek-ai/DeepSeek-V4-Flash"
    assert captured["body"]["enable_thinking"] is False
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    instruction = captured["body"]["messages"][0]["content"]
    assert "不得仅因为资料写了不承诺固定效果或速度就转 manual" in instruction
    assert "即使买家只说‘不知道效果’或‘效果怎么样’" in instruction
    assert "不要使用‘您好’‘亲’‘这边’" in instruction
    assert "对于‘这是干嘛的’‘有什么用’‘有哪些功能’" in instruction
    user_payload = json.loads(captured["body"]["messages"][1]["content"])
    assert "包含基础操作说明" in user_payload["商品资料"]["商品详细资料"]
    assert "pan.baidu.com" not in user_payload["商品资料"]["商品详细资料"]
    assert "9r1v" not in user_payload["商品资料"]["商品详细资料"]


def test_siliconflow_client_compacts_one_grounded_long_reply(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        headers = {}

        def __init__(self, content):
            self._content = content

        def json(self):
            return {"choices": [{"message": {"content": self._content}}]}

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, *, headers, json):
            calls.append(json)
            if len(calls) == 1:
                return FakeResponse(
                    '{"action":"reply","reply":"这是基于 ComfyUI 的 MiniMax H3 本地工作台，主要用于环境检查、模型安装、工作流启动和视频生成。","reason":"资料明确"}'
                )
            return FakeResponse(
                '{"action":"reply","reply":"是基于 ComfyUI 的 H3 本地工作台，可检查环境并启动视频工作流。","reason":"已压缩"}'
            )

    monkeypatch.setattr(
        "xianyu_manager.auto_reply.httpx.AsyncClient", FakeAsyncClient
    )
    decision = asyncio.run(
        SiliconFlowReplyClient().generate(
            api_key="sk-test",
            base_url="https://api.siliconflow.cn/v1",
            model="deepseek-ai/DeepSeek-V4-Flash",
            system_prompt="安全回复",
            buyer_message="这是 ComfyUI 嘛",
            product={
                "name": "MiniMax H3",
                "title": "MiniMax H3 本地工作台",
                "knowledge_text": "基于 ComfyUI，提供环境检查和工作流启动。",
            },
            context=[],
            max_reply_chars=40,
        )
    )

    assert len(calls) == 2
    assert decision.action == "reply"
    assert len(decision.reply) <= 40
    assert "只负责压缩" in calls[1]["messages"][0]["content"]


def test_product_reply_does_not_require_a_verbatim_evidence_quote():
    decision = normalize_decision(
        json.dumps(
            {
                "action": "reply",
                "reply": "支持导入 TXT 和 Markdown，导出 Excel 和 CSV。",
                "reason": "",
            },
            ensure_ascii=False,
        ),
        180,
    )
    assert decision.action == "reply"


def test_existing_deepseek_settings_migrate_to_siliconflow(tmp_path):
    path = tmp_path / "manager.db"
    database = Database(path)
    database.ensure_default_accounts()
    account_id = int(database.get_active_account()["id"])
    database.update_auto_reply_settings(
        account_id,
        {
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-pro",
        },
    )

    migrated = Database(path).get_auto_reply_settings(account_id)
    assert migrated["base_url"] == "https://api.siliconflow.cn/v1"
    assert migrated["model"] == "deepseek-ai/DeepSeek-V4-Pro"


def test_auto_reply_database_is_idempotent_and_supports_manual_takeover(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.ensure_default_accounts()
    account_id = int(database.get_active_account()["id"])
    saved = database.update_auto_reply_settings(
        account_id,
        {
            "enabled": True,
            "model": "deepseek-ai/DeepSeek-V4-Flash",
            "min_delay_seconds": 3,
            "max_delay_seconds": 6,
        },
    )
    assert saved["enabled"] is True
    assert saved["model"] == "deepseek-ai/DeepSeek-V4-Flash"

    message_id = database.record_chat_message(
        account_id=account_id,
        chat_id="667788",
        buyer_id="998877",
        listing_item_id="1068148111818",
        direction="inbound",
        content="你好",
        event_fingerprint="fingerprint-1",
    )
    assert message_id is not None
    assert database.record_chat_message(
        account_id=account_id,
        chat_id="667788",
        buyer_id="998877",
        listing_item_id="1068148111818",
        direction="inbound",
        content="你好",
        event_fingerprint="fingerprint-1",
    ) is None
    assert database.claim_auto_reply(message_id) is True
    assert database.claim_auto_reply(message_id) is False

    database.set_chat_manual(account_id, "667788", enabled=True, hours=12)
    assert database.is_chat_manual(account_id, "667788") is True
    database.set_chat_manual(account_id, "667788", enabled=False)
    assert database.is_chat_manual(account_id, "667788") is False


def test_delivery_service_sends_one_safe_model_reply(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products(
        [
            ScannedProduct(
                dir_name="01-测试商品",
                number=1,
                name="测试商品",
                title="适合新手的 AI 工具",
                zip_name="测试商品.zip",
                zip_hash="abc",
                zip_size=128,
                image_count=5,
                quality_status="passed",
                quality_errors=[],
                scanned_at="2026-08-02T00:00:00+00:00",
            )
        ]
    )
    database.ensure_default_accounts()
    account_id = int(database.get_active_account()["id"])
    database.update_product(
        "01-测试商品",
        {
            "enabled_for_account": True,
            "listing_url": "https://www.goofish.com/item?id=1068148111818",
            "listing_status": "published",
            "confirmed_price_cents": 19900,
        },
        account_id,
    )
    database.update_auto_reply_settings(account_id, {"enabled": True})
    message_id = database.record_chat_message(
        account_id=account_id,
        chat_id="667788",
        buyer_id="998877",
        listing_item_id="1068148111818",
        direction="inbound",
        content="适合新手吗",
        event_fingerprint="event-1",
    )
    assert message_id is not None

    class FakeSecretStore:
        def load(self):
            return "sk-test"

        def has_secret(self):
            return True

    class FakeReplyClient:
        async def generate(self, **_kwargs):
            return ReplyDecision(action="reply", reply="可以，按配套说明操作即可。")

    service = DeliveryService(
        tmp_path / "profiles",
        Path(__file__),
        database,
        FakeSecretStore(),
        FakeReplyClient(),
    )
    websocket = object()
    service._runtime_websocket = websocket
    service._status = "listening"
    sent = []

    async def fake_send(_websocket, chat_id, buyer_id, seller_id, text):
        sent.append((chat_id, buyer_id, seller_id, text))

    service._send_text = fake_send
    asyncio.run(
        service._delayed_auto_reply(
            websocket=websocket,
            account_id=account_id,
            seller_id="111111",
            buyer_id="998877",
            chat_id="667788",
            item_id="1068148111818",
            message_id=message_id,
            buyer_message="适合新手吗",
            delay_seconds=0,
        )
    )

    assert sent == [("667788", "998877", "111111", "可以，按配套说明操作即可。")]
    records = database.list_auto_reply_records(account_id)
    assert records[0]["direction"] == "outbound"
    assert records[0]["reply_source"] == "deepseek-ai/DeepSeek-V4-Flash"


def test_delivery_service_sends_clarifying_question_and_labels_it(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products(
        [
            ScannedProduct(
                dir_name="01-测试商品",
                number=1,
                name="测试商品",
                title="适合新手的 AI 工具",
                zip_name="测试商品.zip",
                zip_hash="abc",
                zip_size=128,
                image_count=5,
                quality_status="passed",
                quality_errors=[],
                scanned_at="2026-08-02T00:00:00+00:00",
            )
        ]
    )
    database.ensure_default_accounts()
    account_id = int(database.get_active_account()["id"])
    database.update_product(
        "01-测试商品",
        {
            "enabled_for_account": True,
            "listing_url": "https://www.goofish.com/item?id=1068148111818",
            "listing_status": "published",
        },
        account_id,
    )
    database.update_auto_reply_settings(account_id, {"enabled": True})
    message_id = database.record_chat_message(
        account_id=account_id,
        chat_id="667788",
        buyer_id="998877",
        listing_item_id="1068148111818",
        direction="inbound",
        content="效果呢",
        event_fingerprint="event-clarify",
    )
    assert message_id is not None

    class FakeSecretStore:
        def load(self):
            return "sk-test"

    class FakeReplyClient:
        async def generate(self, **_kwargs):
            return ReplyDecision(
                action="clarify", reply="你想了解生成画质，还是运行速度？"
            )

    service = DeliveryService(
        tmp_path / "profiles", Path(__file__), database, FakeSecretStore(), FakeReplyClient()
    )
    websocket = object()
    service._runtime_websocket = websocket
    service._status = "listening"
    sent = []

    async def fake_send(_websocket, chat_id, buyer_id, seller_id, text):
        sent.append(text)

    service._send_text = fake_send
    asyncio.run(
        service._delayed_auto_reply(
            websocket=websocket,
            account_id=account_id,
            seller_id="111111",
            buyer_id="998877",
            chat_id="667788",
            item_id="1068148111818",
            message_id=message_id,
            buyer_message="效果呢",
            delay_seconds=0,
        )
    )

    assert sent == ["你想了解生成画质，还是运行速度？"]
    records = database.list_auto_reply_records(account_id)
    assert records[0]["reason"] == "自动澄清追问"


def test_process_chat_event_merges_rapid_buyer_messages(tmp_path, monkeypatch):
    database = Database(tmp_path / "manager.db")
    database.sync_products(
        [
            ScannedProduct(
                dir_name="01-测试商品",
                number=1,
                name="测试商品",
                title="ComfyUI H3 本地工作台",
                zip_name="测试商品.zip",
                zip_hash="abc",
                zip_size=128,
                image_count=5,
                quality_status="passed",
                quality_errors=[],
                scanned_at="2026-08-02T00:00:00+00:00",
            )
        ]
    )
    database.ensure_default_accounts()
    account_id = int(database.get_active_account()["id"])
    database.update_product(
        "01-测试商品",
        {
            "enabled_for_account": True,
            "listing_url": "https://www.goofish.com/item?id=1068148111818",
            "listing_status": "published",
        },
        account_id,
    )
    database.update_auto_reply_settings(account_id, {"enabled": True})
    captured = []

    class FakeSecretStore:
        def load(self):
            return "sk-test"

    class FakeReplyClient:
        async def generate(self, **kwargs):
            captured.append(kwargs["buyer_message"])
            return ReplyDecision(action="reply", reply="是基于 ComfyUI 的本地工作台。")

    service = DeliveryService(
        tmp_path / "profiles", Path(__file__), database, FakeSecretStore(), FakeReplyClient()
    )
    websocket = object()
    service._runtime_websocket = websocket
    service._status = "listening"
    sent = []

    async def fake_send(_websocket, _chat_id, _buyer_id, _seller_id, text):
        sent.append(text)

    service._send_text = fake_send
    monkeypatch.setattr("xianyu_manager.delivery.random.uniform", lambda *_args: 0.05)

    async def run_fragments():
        common = {
            "websocket": websocket,
            "account_id": account_id,
            "seller_id": "111111",
        }
        await service._process_chat_event(
            **common,
            message={
                "chat_id": "667788",
                "item_id": "1068148111818",
                "sender_id": "998877",
                "direction": "inbound",
                "content": "这是 ComfyUI",
            },
            event={"event": "fragment-1"},
        )
        await service._process_chat_event(
            **common,
            message={
                "chat_id": "667788",
                "item_id": "1068148111818",
                "sender_id": "998877",
                "direction": "inbound",
                "content": "嘛",
            },
            event={"event": "fragment-2"},
        )
        await service._reply_tasks["667788"].task

    asyncio.run(run_fragments())

    assert captured == ["这是 ComfyUI 嘛"]
    assert sent == ["是基于 ComfyUI 的本地工作台。"]
    inbound = [
        row
        for row in database.list_auto_reply_records(account_id)
        if row["direction"] == "inbound"
    ]
    assert {row["content"]: row["status"] for row in inbound} == {
        "这是 ComfyUI": "skipped",
        "嘛": "replied",
    }


def test_model_manual_decision_only_skips_current_message(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.sync_products(
        [
            ScannedProduct(
                dir_name="01-测试商品",
                number=1,
                name="测试商品",
                title="适合新手的 AI 工具",
                zip_name="测试商品.zip",
                zip_hash="abc",
                zip_size=128,
                image_count=5,
                quality_status="passed",
                quality_errors=[],
                scanned_at="2026-08-02T00:00:00+00:00",
            )
        ]
    )
    database.ensure_default_accounts()
    account_id = int(database.get_active_account()["id"])
    database.update_product(
        "01-测试商品",
        {
            "enabled_for_account": True,
            "listing_url": "https://www.goofish.com/item?id=1068148111818",
            "listing_status": "published",
            "confirmed_price_cents": 19900,
        },
        account_id,
    )
    database.update_auto_reply_settings(account_id, {"enabled": True})

    first_message_id = database.record_chat_message(
        account_id=account_id,
        chat_id="667788",
        buyer_id="998877",
        listing_item_id="1068148111818",
        direction="inbound",
        content="什么意思",
        event_fingerprint="event-manual-current-only",
    )
    second_message_id = database.record_chat_message(
        account_id=account_id,
        chat_id="667788",
        buyer_id="998877",
        listing_item_id="1068148111818",
        direction="inbound",
        content="什么是离线草稿",
        event_fingerprint="event-follow-up",
    )
    assert first_message_id is not None
    assert second_message_id is not None

    class FakeSecretStore:
        def load(self):
            return "sk-test"

    class FakeReplyClient:
        def __init__(self):
            self.calls = 0

        async def generate(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return ReplyDecision(action="manual", reason="问题指代不明")
            return ReplyDecision(action="reply", reply="离线草稿无需调用 API。")

    service = DeliveryService(
        tmp_path / "profiles",
        Path(__file__),
        database,
        FakeSecretStore(),
        FakeReplyClient(),
    )
    websocket = object()
    service._runtime_websocket = websocket
    service._status = "listening"
    sent = []

    async def fake_send(_websocket, chat_id, buyer_id, seller_id, text):
        sent.append((chat_id, buyer_id, seller_id, text))

    service._send_text = fake_send

    async def run_messages():
        common = {
            "websocket": websocket,
            "account_id": account_id,
            "seller_id": "111111",
            "buyer_id": "998877",
            "chat_id": "667788",
            "item_id": "1068148111818",
            "delay_seconds": 0,
        }
        await service._delayed_auto_reply(
            **common,
            message_id=first_message_id,
            buyer_message="什么意思",
        )
        assert database.is_chat_manual(account_id, "667788") is False
        await service._delayed_auto_reply(
            **common,
            message_id=second_message_id,
            buyer_message="什么是离线草稿",
        )

    asyncio.run(run_messages())

    assert sent == [("667788", "998877", "111111", "离线草稿无需调用 API。")] 
    records = database.list_auto_reply_records(account_id)
    inbound = {row["content"]: row for row in records if row["direction"] == "inbound"}
    assert inbound["什么意思"]["status"] == "manual"
    assert inbound["什么是离线草稿"]["status"] == "replied"


def test_high_risk_or_seller_message_takes_over_chat(tmp_path):
    database = Database(tmp_path / "manager.db")
    database.ensure_default_accounts()
    account_id = int(database.get_active_account()["id"])
    database.update_auto_reply_settings(account_id, {"enabled": True})

    class FakeSecretStore:
        def load(self):
            return "sk-test"

    service = DeliveryService(
        tmp_path / "profiles",
        Path(__file__),
        database,
        FakeSecretStore(),
    )

    async def run_events():
        await service._process_chat_event(
            object(),
            account_id,
            "111111",
            {
                "chat_id": "risk-chat",
                "item_id": "1068148111818",
                "sender_id": "998877",
                "direction": "inbound",
                "content": "我要退款并投诉",
            },
            {"event": "risk"},
        )
        assert database.is_chat_manual(account_id, "risk-chat") is True

        await service._process_chat_event(
            object(),
            account_id,
            "111111",
            {
                "chat_id": "seller-chat",
                "item_id": "1068148111818",
                "sender_id": "111111",
                "direction": "outbound",
                "content": "我来人工处理",
            },
            {"event": "seller"},
        )
        assert database.is_chat_manual(account_id, "seller-chat") is True

        await service._process_chat_event(
            object(),
            account_id,
            "111111",
            {
                "chat_id": "unmapped-chat",
                "item_id": "1068148111818",
                "sender_id": "998877",
                "direction": "inbound",
                "content": "这个怎么使用",
            },
            {"event": "unmapped"},
        )
        assert database.is_chat_manual(account_id, "unmapped-chat") is False

    asyncio.run(run_events())


@pytest.mark.skipif(os.name != "nt", reason="DPAPI 仅在 Windows 可用")
def test_secret_store_uses_windows_dpapi(tmp_path):
    store = SecretStore(tmp_path / "siliconflow-key.dpapi")
    store.save("sk-test-secret")
    assert store.load() == "sk-test-secret"
    assert b"sk-test-secret" not in store.path.read_bytes()


def test_secret_store_reads_environment_without_writing(tmp_path, monkeypatch, caplog):
    store = SecretStore(tmp_path / "siliconflow-key.dpapi")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "environment-secret")

    assert store.has_secret() is True
    assert store.load() == "environment-secret"
    assert not store.path.exists()
    assert "environment-secret" not in caplog.text


def test_secret_store_environment_has_priority_over_dpapi_file(tmp_path, monkeypatch):
    store = SecretStore(tmp_path / "siliconflow-key.dpapi")
    store.path.write_bytes(b"encrypted-placeholder")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "environment-secret")

    assert store.load() == "environment-secret"


def test_secret_store_missing_key_is_explicitly_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    store = SecretStore(tmp_path / "siliconflow-key.dpapi")

    assert store.has_secret() is False
    assert store.load() == ""
