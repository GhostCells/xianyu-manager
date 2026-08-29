from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
PROFILE = ROOT / "data" / "browser-profiles" / "msedge" / "account-2"
POST_URL = "https://x.com/Pluvio9yte/status/2081580929492131947?s=20"
OUTPUT = ROOT / "research" / f"{date.today().isoformat()}-pluvio9yte-2081580929492131947"


async def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            executable_path=str(EDGE),
            headless=False,
            no_viewport=True,
            args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(POST_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(10_000)

        body = await page.locator("body").inner_text(timeout=20_000)
        articles = await page.locator("article").all_inner_texts()
        links = await page.locator("article a").evaluate_all(
            "els => els.map(a => ({text: (a.innerText || '').trim(), href: a.href || ''}))"
        )
        images = await page.locator("article img").evaluate_all(
            "els => els.map(i => ({alt: i.alt || '', src: i.src || ''}))"
        )
        videos = await page.locator("article video").evaluate_all(
            "els => els.map(v => ({src: v.currentSrc || v.src || '', poster: v.poster || ''}))"
        )
        result = {
            "date": date.today().isoformat(),
            "url": page.url,
            "title": await page.title(),
            "articles": [" ".join(item.split()) for item in articles if item.strip()],
            "links": links,
            "images": images,
            "videos": videos,
            "body_excerpt": " ".join(body.split())[:20_000],
        }
        await page.screenshot(path=str(OUTPUT / "x-post.png"), full_page=True)
        (OUTPUT / "x-post.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await context.close()

    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
