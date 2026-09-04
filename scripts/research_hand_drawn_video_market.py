from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import Page, async_playwright


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xianyu_manager.profile_lock import hold_profile_lock_for_process

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
PROFILE = ROOT / "data" / "browser-profiles" / "msedge" / "account-2"
OUTPUT = ROOT / "research" / f"{date.today().isoformat()}-hand-drawn-video-prompts"
X_URL = "https://x.com/Conflux_Intern/status/2082031793193238733"
QUERIES = [
    "口播转分镜",
    "Flow视频提示词",
    "Nano Banana视频",
]


async def collect_page_state(page: Page) -> dict[str, object]:
    title = await page.title()
    body = await page.locator("body").inner_text(timeout=15_000)
    meta = await page.locator('meta[name="description"]').get_attribute("content")
    articles = await page.locator("article").all_inner_texts()
    videos = await page.locator("video").evaluate_all(
        "els => els.map(v => ({src: v.currentSrc || v.src || '', poster: v.poster || ''}))"
    )
    return {
        "url": page.url,
        "title": title,
        "meta_description": meta or "",
        "articles": [" ".join(x.split()) for x in articles if x.strip()],
        "videos": videos,
        "body_excerpt": " ".join(body.split())[:12_000],
    }


async def research_x(page: Page) -> dict[str, object]:
    await page.goto(X_URL, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(8_000)
    result = await collect_page_state(page)
    await page.screenshot(path=str(OUTPUT / "x-post.png"), full_page=True)
    return result


async def research_goofish(page: Page, query: str, index: int) -> dict[str, object]:
    url = f"https://www.goofish.com/search?q={quote(query)}"
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(5_000)

    body = await page.locator("body").inner_text(timeout=15_000)
    blocked = any(term in body for term in ("滑动验证", "验证码", "访问过于频繁"))
    if not blocked:
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1_200)

    anchors = await page.locator('a[href*="/item?"]').evaluate_all(
        """anchors => anchors.slice(0, 120).map(anchor => ({
          url: anchor.href || '',
          text: (anchor.innerText || anchor.textContent || '').trim(),
          image: anchor.querySelector('img')?.src || ''
        }))"""
    )
    unique: dict[str, dict[str, str]] = {}
    for item in anchors:
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

    await page.screenshot(path=str(OUTPUT / f"goofish-{index:02d}-{quote(query, safe='')}.png"), full_page=True)
    return {
        "query": query,
        "url": page.url,
        "blocked": blocked,
        "body_excerpt": " ".join(body.split())[:4_000],
        "items": list(unique.values())[:40],
    }


async def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    hold_profile_lock_for_process(PROFILE)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            executable_path=str(EDGE),
            headless=False,
            no_viewport=True,
            args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        result: dict[str, object] = {"date": date.today().isoformat(), "x": {}, "goofish": []}
        try:
            result["x"] = {"source": X_URL, "note": "X post was captured in the first pass."}
            for index, query in enumerate(QUERIES, start=1):
                item = await research_goofish(page, query, index)
                result["goofish"].append(item)
                if item["blocked"]:
                    break
        finally:
            (OUTPUT / "research-2.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            await context.close()

    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
