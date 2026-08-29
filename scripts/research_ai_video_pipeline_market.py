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
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
PROFILE = ROOT / "data" / "browser-profiles" / "msedge" / "account-2"
OUTPUT = ROOT / "research" / f"{date.today().isoformat()}-ai-video-pipeline"
QUERIES = [
    "AI视频流水线",
    "HyperFrames",
    "Codex视频制作",
    "AI视频制作Skill",
    "IndexTTS2声音克隆",
]


async def collect_search(page: Page, query: str, index: int) -> dict[str, object]:
    url = f"https://www.goofish.com/search?q={quote(query)}"
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(6_000)

    body = await page.locator("body").inner_text(timeout=20_000)
    blocked_terms = ("滑动验证", "验证码", "访问过于频繁", "安全验证")
    blocked = any(term in body for term in blocked_terms)
    if not blocked:
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1_200)

    anchors = await page.locator('a[href*="/item?"]').evaluate_all(
        """anchors => anchors.slice(0, 160).map(anchor => ({
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

    safe_name = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", query)
    await page.screenshot(path=str(OUTPUT / f"goofish-{index:02d}-{safe_name}.png"), full_page=True)
    return {
        "query": query,
        "requested_url": url,
        "final_url": page.url,
        "blocked": blocked,
        "body_excerpt": " ".join(body.split())[:6_000],
        "items": list(unique.values())[:60],
    }


async def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {"date": date.today().isoformat(), "queries": []}
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            executable_path=str(EDGE),
            headless=False,
            no_viewport=True,
            args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            for index, query in enumerate(QUERIES, start=1):
                entry = await collect_search(page, query, index)
                result["queries"].append(entry)
                if entry["blocked"]:
                    break
        finally:
            (OUTPUT / "goofish-market.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            await context.close()

    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
