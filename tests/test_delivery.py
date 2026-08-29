from __future__ import annotations

import base64
import asyncio
import json
import time

import msgpack

from xianyu_manager.delivery import (
    build_live_listing_snapshot,
    compose_delivery_message,
    DeliveryService,
    decode_sync_payload,
    extract_buyer_id,
    extract_chat_id,
    extract_group_card_title,
    extract_item_id,
    extract_order_id,
    inventory_cards_to_raw_items,
    is_paid_event,
    is_recent_event,
    is_recoverable_order,
    group_event_stage,
    parse_market_timestamp,
)


class FakeTokenResponse:
    async def json(self):
        return {"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "token-ok"}}


class FakeBrowserRequest:
    def __init__(self):
        self.headers = {}

    async def post(self, _url, **kwargs):
        self.headers = kwargs["headers"]
        return FakeTokenResponse()

    async def storage_state(self):
        return {"cookies": [{"name": "_m_h5_tk", "value": "fresh_123"}]}


def paid_event():
    return {
        "1": {
            "2": "667788@goofish",
            "5": str(int(time.time() * 1000)),
            "10": {
                "senderUserId": "998877",
                "reminderContent": "[我已付款，等待你发货]",
                "reminderUrl": (
                    "https://www.goofish.com/im?itemId=1068148111818"
                    "&orderId=223344556677889900"
                ),
            },
        },
        "3": {"redReminder": "等待卖家发货"},
    }


def test_paid_event_extracts_required_delivery_identity():
    event = paid_event()
    assert is_paid_event(event) is True
    assert extract_order_id(event) == "223344556677889900"
    assert extract_item_id(event) == "1068148111818"
    assert extract_buyer_id(event, "111111") == "998877"
    assert extract_chat_id(event) == "667788"
    assert is_recent_event(event) is True


def test_decode_sync_payload_accepts_json_and_messagepack():
    event = paid_event()
    json_payload = base64.b64encode(json.dumps(event).encode("utf-8")).decode("ascii")
    packed_payload = base64.b64encode(msgpack.packb(event, use_bin_type=True)).decode("ascii")
    assert decode_sync_payload(json_payload)["3"]["redReminder"] == "等待卖家发货"
    assert decode_sync_payload(packed_payload)["3"]["redReminder"] == "等待卖家发货"


def test_old_paid_event_is_ignored():
    event = paid_event()
    event["1"]["5"] = str(int((time.time() - 3600) * 1000))
    assert is_recent_event(event) is False


def test_recent_pending_order_is_safe_for_startup_recovery():
    now = 1_800_000_000
    order = {
        "order_status": "待发货",
        "paid_time": str((now - 60) * 1000),
    }
    assert is_recoverable_order(order, now=now) is True
    order["paid_time"] = str((now - 25 * 60 * 60) * 1000)
    assert is_recoverable_order(order, now=now) is False
    order["order_status"] = "交易成功"
    assert is_recoverable_order(order, now=now) is False


def test_market_timestamp_accepts_local_datetime():
    assert parse_market_timestamp("2026-07-30 11:10:00") is not None


def test_live_listing_snapshot_prefers_existing_ids_and_uniquely_matches_new_title():
    products = [
        {
            "dir_name": "12-AI漫剧制作工作流Skill",
            "name": "AI漫剧制作工作流Skill",
            "title": "AI漫剧完整制作工作流",
            "listing_url": "https://www.goofish.com/item?id=1069688468877",
        },
        {
            "dir_name": "24-AI漫剧分镜表生成器",
            "name": "AI漫剧分镜表生成器",
            "title": "AI漫剧分镜表生成器",
            "listing_url": "",
        },
        {
            "dir_name": "21-自媒体封面策划Skill",
            "name": "自媒体封面策划Skill",
            "title": "自媒体封面策划Skill",
            "listing_url": "",
        },
    ]
    snapshot = build_live_listing_snapshot(
        [
            {
                "url": "https://www.goofish.com/item?id=1069688468877&spm=test",
                "title": "AI漫剧完整制作工作流",
                "text": "AI漫剧完整制作工作流\n￥2.99",
                "image": "https://img.example/existing.png",
            },
            {
                "url": "https://www.goofish.com/item?id=1088888888888",
                "title": "AI漫剧分镜生成器软件",
                "text": "AI漫剧分镜生成器软件\n￥199\n3人想要",
                "image": "https://img.example/new.png",
            },
        ],
        products,
    )

    assert len(snapshot) == 2
    assert snapshot[0]["matched_product_dir_name"] == "12-AI漫剧制作工作流Skill"
    assert snapshot[1]["matched_product_dir_name"] == "24-AI漫剧分镜表生成器"
    assert snapshot[1]["price_cents"] == 19_900
    assert snapshot[1]["url"] == "https://www.goofish.com/item?id=1088888888888"


def test_live_listing_snapshot_leaves_ambiguous_titles_unmatched():
    products = [
        {"dir_name": "20-AI分镜Skill", "name": "AI分镜Skill", "title": "AI分镜Skill", "listing_url": ""},
        {"dir_name": "21-AI分镜工具", "name": "AI分镜工具", "title": "AI分镜工具", "listing_url": ""},
    ]
    snapshot = build_live_listing_snapshot(
        [{"url": "https://www.goofish.com/item?id=1088888888889", "title": "AI分镜", "text": "AI分镜\n￥9.90"}],
        products,
    )
    assert snapshot[0]["matched_product_dir_name"] is None


def test_inventory_cards_keep_only_currently_listed_items():
    items = inventory_cards_to_raw_items(
        [
            {
                "cardData": {
                    "id": 1088888888888,
                    "title": "AI漫剧分镜生成器",
                    "itemStatus": 0,
                    "priceInfo": {"price": "199"},
                    "picInfo": {"picUrl": "https://img.example/storyboard.png"},
                }
            },
            {
                "cardData": {
                    "id": 1077777777777,
                    "title": "已售商品",
                    "itemStatus": 1,
                }
            },
        ]
    )
    assert items == [
        {
            "url": "https://www.goofish.com/item?id=1088888888888",
            "title": "AI漫剧分镜生成器",
            "text": "AI漫剧分镜生成器\n¥199",
            "image": "https://img.example/storyboard.png",
        }
    ]


def test_live_inventory_uses_first_party_profile_api_shape():
    service = object.__new__(DeliveryService)
    service._runtime_user_agent = "Mozilla/5.0 test"
    captured = {}

    async def fake_post(url, *, params, data_json, headers, cookie_map):
        captured.update(
            url=url,
            params=params,
            data=json.loads(data_json),
            headers=headers,
        )
        return {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "cardList": [
                    {
                        "cardData": {
                            "id": "1088888888888",
                            "title": "AI漫剧分镜生成器",
                            "itemStatus": 0,
                            "priceInfo": {"price": "199"},
                        }
                    }
                ],
                "nextPage": False,
            },
        }

    service._post_mtop = fake_post
    result = asyncio.run(
        service._fetch_live_inventory(
            {"_m_h5_tk": "seed_123", "cookie2": "session"},
            "998877",
        )
    )
    assert len(result) == 1
    assert captured["params"]["api"] == "mtop.idle.web.xyh.item.list"
    assert captured["data"] == {
        "needGroupInfo": True,
        "pageNumber": 1,
        "userId": "998877",
        "pageSize": 20,
    }
    assert "cookie" in captured["headers"]


def test_im_token_uses_browser_request_context_and_refreshes_cookies():
    service = object.__new__(DeliveryService)
    service._runtime_user_agent = (
        "Mozilla/5.0 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"
    )
    request = FakeBrowserRequest()
    token, cookies = asyncio.run(
        service._fetch_im_token(
            {"_m_h5_tk": "seed_123", "cookie2": "session"},
            "device-1",
            request_context=request,
        )
    )
    assert token == "token-ok"
    assert cookies["_m_h5_tk"] == "fresh_123"
    assert "cookie" not in request.headers


def test_delivery_message_contains_only_verified_delivery_fields():
    message = compose_delivery_message(
        {
            "title": "测试商品",
            "name": "备用名称",
            "share_url": "https://pan.baidu.com/s/example",
            "share_code": "abcd",
        }
    )
    assert "测试商品" in message
    assert "https://pan.baidu.com/s/example" in message
    assert "提取码：abcd" in message
    assert message.startswith("拍下啦～这是你购买的「测试商品」：")
    assert "百度网盘：https://pan.baidu.com/s/example" in message
    assert "方便的话可以留下个评价，感谢支持～" in message
    assert message.endswith("有什么问题可以继续沟通～")
    assert "微信" not in message
    assert "永久有效" not in message


def group_card_event(title: str, *, system: bool = True):
    card = {
        "dxCard": {"item": {"main": {"exContent": {"title": title}}}}
    }
    return {
        "1": {
            "2": "667788@goofish",
            "7": 1 if system else 0,
            "10": {
                "senderUserId": "998877",
                "reminderUrl": (
                    "https://www.goofish.com/im?itemId=1068148111818"
                    "&orderId=223344556677889900"
                ),
            },
            "6": {"3": {"4": 6 if system else 1, "5": json.dumps(card, ensure_ascii=False)}},
        }
    }


def test_group_waiting_and_ready_cards_require_system_evidence():
    waiting = group_card_event("我已小刀，待刀成")
    ready = group_card_event("我已成功小刀，待发货")
    forged = group_card_event("我已小刀，待刀成", system=False)

    assert extract_group_card_title(waiting) == "我已小刀，待刀成"
    assert group_event_stage(waiting) == "waiting"
    assert group_event_stage(ready) == "ready"
    assert group_event_stage(forged) == ""


def test_group_card_extracts_order_id_from_nested_json_and_update_key():
    nested = group_card_event("我已小刀，待刀成")
    nested["1"]["10"].pop("reminderUrl")
    nested["1"]["6"]["3"]["5"] = json.dumps(
        {
            "dxCard": {
                "item": {
                    "main": {"exContent": {"title": "我已小刀，待刀成"}},
                    "params": {"bizOrderId": "223344556677889900"},
                }
            }
        },
        ensure_ascii=False,
    )
    update_key = {"payload": "buyer_confirm:223344556677889901:ready"}

    assert extract_order_id(nested) == "223344556677889900"
    assert extract_order_id(update_key) == "223344556677889901"

    sync_enriched = group_card_event("我已小刀，待刀成")
    sync_enriched["1"]["10"].pop("reminderUrl")
    sync_enriched["_sync_meta"] = {
        "updateKey": "trade_buyer_confirm:223344556677889902:waiting"
    }
    assert extract_order_id(sync_enriched) == "223344556677889902"
