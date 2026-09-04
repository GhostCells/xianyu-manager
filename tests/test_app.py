from __future__ import annotations

from fastapi.testclient import TestClient

from xianyu_manager.app import app, delivery_service
from xianyu_manager import app as app_module


def test_health_and_products():
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.headers["x-frame-options"] == "DENY"
        assert health.json()["active_account"]["name"] == "七月账号"
        assert health.json()["next_product_number"] >= 20

        accounts = client.get("/api/accounts")
        assert accounts.status_code == 200
        assert len(accounts.json()) == 1

        products = client.get("/api/products")
        assert products.status_code == 200
        assert len(products.json()) >= 19
        assert all("enabled_for_account" in product for product in products.json())
        assert all("knowledge_chars" in product for product in products.json())

        listings = client.get("/api/listings")
        assert listings.status_code == 200
        assert isinstance(listings.json(), list)

        session = client.get("/api/session")
        assert session.status_code == 200
        payload = session.json()
        assert payload["status"] in {
            "unbound", "starting", "waiting_scan", "detected", "bound", "expired", "error"
        }
        assert "cookies" not in payload
        assert "cookie" not in payload

        delivery = client.get("/api/delivery")
        assert delivery.status_code == 200
        delivery_payload = delivery.json()
        assert delivery_payload["status"] in {
            "disabled", "starting", "cooldown", "authenticating", "connecting", "listening", "reconnecting", "verification_required", "error"
        }
        assert isinstance(delivery_payload["auto_free_group"], bool)
        assert "cookies" not in delivery_payload

        auto_reply = client.get("/api/auto-reply")
        assert auto_reply.status_code == 200
        auto_reply_payload = auto_reply.json()
        assert auto_reply_payload["settings"]["model"] in {
            "deepseek-ai/DeepSeek-V4-Flash", "deepseek-ai/DeepSeek-V4-Pro"
        }
        assert "api_key" not in auto_reply_payload["settings"]
        assert isinstance(auto_reply_payload["has_api_key"], bool)
        assert set(auto_reply_payload["knowledge"]) == {"ready", "total", "missing"}

        safety = client.get("/api/automation-safety")
        assert safety.status_code == 200
        safety_payload = safety.json()
        assert safety_payload["settings"]["max_replies_per_hour"] >= 1
        assert safety_payload["circuit"]["is_open"] is False
        assert "startup" in safety_payload

        cross_site = client.post(
            "/api/delivery/stop", headers={"Origin": "https://example.com"}
        )
        assert cross_site.status_code == 403


def test_verified_share_requires_url():
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        products = client.get("/api/products").json()
        dir_name = products[0]["dir_name"]
        response = client.patch(
            f"/api/products/{dir_name}",
            json={"share_url": None, "share_verified": True},
        )
        assert response.status_code == 422


def test_scan_reports_remote_listing_refresh(monkeypatch):
    async def fake_refresh():
        return {
            "remote_count": 19,
            "new_count": 1,
            "matched_count": 17,
            "synced_at": "2026-08-03T12:00:00+08:00",
            "listings": [],
        }

    monkeypatch.setattr(delivery_service, "refresh_live_listings", fake_refresh)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/scan")
    assert response.status_code == 200
    assert response.json()["listing_sync"] == {
        "ok": True,
        "remote_count": 19,
        "new_count": 1,
        "matched_count": 17,
        "synced_at": "2026-08-03T12:00:00+08:00",
        "error": "",
    }


def test_scan_keeps_local_refresh_when_remote_listing_refresh_fails(monkeypatch):
    async def fake_refresh():
        raise RuntimeError("监听尚未连接")

    monkeypatch.setattr(delivery_service, "refresh_live_listings", fake_refresh)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/scan")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] >= 19
    assert payload["listing_sync"]["ok"] is False
    assert payload["listing_sync"]["error"] == "监听尚未连接"


def test_linux_folder_picker_returns_explicit_unsupported_response():
    product = app_module.database.list_products()[0]
    safe_name = product["dir_name"]
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(f"/api/products/{safe_name}/knowledge-folder/pick")

    assert response.status_code == 501
    assert "不支持本地 GUI 目录选择" in response.json()["detail"]
