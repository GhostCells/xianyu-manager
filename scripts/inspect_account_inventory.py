from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xianyu_manager.config import load_settings
from xianyu_manager.database import Database


EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
PROFILE = ROOT / "data" / "browser-profiles" / "msedge" / "account-2"
OUTPUT = ROOT / "data" / "session-screenshots" / "account-2-inventory.png"
OUTPUT_JSON = ROOT / "data" / "account-2-inventory.json"

TITLE_TO_PRODUCT_NUMBER = {
    "AI手绘视频口播分镜Skill": 22,
    "AI产品宣传片实战工作流": 19,
    "AI信息图解说视频Skill": 17,
    "SEO内容规划工作流Skill": 16,
    "AI Skill新手入门教程": 11,
    "教师备课工作流Skill": 9,
    "AI漫剧完整制作工作流": 12,
    "短视频四类脚本Skill": 10,
    "AI生成PPT完整工作流": 7,
    "跨境/国内电商 AI 工作流": 5,
    "AI测试用例工作流模板": 8,
    "自媒体AI选题口播Skill": 1,
    "产品经理全流程Skill": 3,
    "复杂问题结构化拆解模板提示词": 4,
    "小说创作AI技能包": 6,
}


def listing_title(text: str) -> str:
    head = re.split(r"\s+¥", text, maxsplit=1)[0].strip()
    return re.sub(r"\s+(?:24小时内发布|一周内发布|热销第\d+名|买家评价.*)$", "", head).strip()


def listing_price_cents(text: str) -> int | None:
    match = re.search(r"¥\s*(\d+)\s*(?:\.\s*(\d{1,2}))?", text)
    if not match:
        return None
    fraction = (match.group(2) or "").ljust(2, "0")[:2]
    return int(match.group(1)) * 100 + int(fraction or 0)


async def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            executable_path=str(EDGE),
            headless=False,
            no_viewport=True,
            args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        result: dict[str, object] = {"profile": str(PROFILE), "items": []}
        try:
            cookies = await context.cookies(["https://www.goofish.com/", "https://passport.goofish.com/"])
            user_id = next((str(c.get("value", "")).strip() for c in cookies if c.get("name") == "unb"), "")
            if not user_id:
                raise RuntimeError("没有检测到已登录账号")

            await page.goto(
                f"https://www.goofish.com/personal?userId={user_id}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            if "passport.goofish.com" in page.url:
                raise RuntimeError("登录会话已失效")

            try:
                await page.locator('a[href*="/item?"]').first.wait_for(state="attached", timeout=15_000)
            except Exception:
                pass

            previous = -1
            stable = 0
            for _ in range(8):
                count = await page.locator('a[href*="/item?"]').count()
                stable = stable + 1 if count == previous else 0
                if stable >= 2:
                    break
                previous = count
                await page.evaluate("window.scrollTo(0, document.body ? document.body.scrollHeight : 0)")
                await page.wait_for_timeout(1_000)

            raw = await page.locator('a[href*="/item?"]').evaluate_all(
                """anchors => anchors.slice(0, 100).map(anchor => ({
                  url: anchor.href || '',
                  text: (anchor.innerText || anchor.textContent || '').trim(),
                  image: anchor.querySelector('img')?.src || ''
                }))"""
            )
            unique: dict[str, dict[str, str]] = {}
            for item in raw:
                match = re.search(r"[?&]id=(\d+)", str(item.get("url", "")))
                if not match:
                    continue
                item_id = match.group(1)
                text = " ".join(str(item.get("text", "")).split())
                if item_id not in unique or len(text) > len(unique[item_id]["text"]):
                    unique[item_id] = {
                        "item_id": item_id,
                        "url": f"https://www.goofish.com/item?id={item_id}",
                        "text": text,
                        "image": str(item.get("image", "")),
                    }

            await page.evaluate("window.scrollTo(0, 0)")
            await page.screenshot(path=str(OUTPUT), full_page=True)
            settings = load_settings()
            database = Database(settings.database_path)
            account = database.get_active_account()
            products_by_number = {int(item["number"]): item for item in database.list_products(int(account["id"]))}
            enriched = []
            for item in unique.values():
                title = listing_title(item["text"])
                number = TITLE_TO_PRODUCT_NUMBER.get(title)
                matched = products_by_number.get(number) if number is not None else None
                enriched.append(
                    {
                        **item,
                        "title": title,
                        "price_cents": listing_price_cents(item["text"]),
                        "matched_product_dir_name": matched["dir_name"] if matched else None,
                    }
                )
            listings = database.sync_live_listings(int(account["id"]), enriched)
            result.update(
                {
                    "page_url": page.url,
                    "user_id": user_id,
                    "item_count": len(unique),
                    "items": enriched,
                    "matched_count": sum(bool(item["matched_product_dir_name"]) for item in enriched),
                    "unmatched_count": sum(not item["matched_product_dir_name"] for item in enriched),
                    "database_listing_count": len(listings),
                    "screenshot": str(OUTPUT),
                    "json": str(OUTPUT_JSON),
                }
            )
        finally:
            OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
