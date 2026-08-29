from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
PROFILE = ROOT / "data" / "browser-profiles" / "msedge" / "account-2"
SECTION_NAMES = (
    "【商品定位】",
    "【它能完成什么】",
    "【一次完整流程】",
    "【你会收到】",
    "【适合人群】",
    "【使用条件】",
    "【购买前说明】",
    "【搜索关键词】",
)


def one(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    return match.group(1) if match else ""


async def main() -> None:
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            executable_path=str(EDGE),
            headless=False,
            no_viewport=True,
            args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        results: list[dict[str, object]] = []
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
            try:
                await page.locator('a[href*="/item?"]').first.wait_for(state="attached", timeout=15_000)
            except Exception:
                pass
            raw = await page.locator('a[href*="/item?"]').evaluate_all(
                """anchors => anchors.slice(0, 100).map(anchor => ({
                  url: anchor.href || '',
                  text: (anchor.innerText || anchor.textContent || '').trim()
                }))"""
            )
            items: dict[str, dict[str, str]] = {}
            for item in raw:
                match = re.search(r"[?&]id=(\d+)", str(item.get("url", "")))
                if not match:
                    continue
                item_id = match.group(1)
                text = " ".join(str(item.get("text", "")).split())
                if item_id not in items or len(text) > len(items[item_id]["card_text"]):
                    items[item_id] = {
                        "item_id": item_id,
                        "url": f"https://www.goofish.com/item?id={item_id}",
                        "card_text": text,
                    }

            for item in items.values():
                await page.goto(item["url"], wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(800)
                body = "\n".join(line.strip() for line in (await page.locator("body").inner_text()).splitlines() if line.strip())
                results.append(
                    {
                        **item,
                        "page_url": page.url,
                        "view_count": one(r"(\d+)\s*浏览", body),
                        "want_count": one(r"(\d+)\s*人想要", body),
                        "published_text": one(r"((?:刚刚|\d+分钟前|\d+小时前|\d+天前|一周内发布|\d{4}-\d{2}-\d{2}))", body),
                        "section_count": sum(name in body for name in SECTION_NAMES),
                        "missing_sections": [name for name in SECTION_NAMES if name not in body],
                        "has_auto_delivery": "自动发货" in body,
                        "has_fixed_ending": "标价即售价" in body,
                        "captcha_or_login": any(term in body for term in ("拖动下方滑块", "短信登录", "手机扫码安全登录")),
                    }
                )
        finally:
            print(json.dumps({"item_count": len(results), "items": results}, ensure_ascii=False, indent=2))
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
