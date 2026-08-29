from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "data" / "browser-profiles" / "msedge" / "account-1"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
OUT = ROOT / "data" / "session-screenshots"


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            executable_path=str(EDGE),
            headless=False,
            no_viewport=True,
            args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://www.goofish.com/publish", wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(5_000)
        fields = await page.locator("input, textarea, select, [contenteditable='true']").evaluate_all(
            """nodes => nodes.map((node, index) => ({
              index,
              tag: node.tagName,
              type: node.getAttribute('type') || '',
              name: node.getAttribute('name') || '',
              id: node.id || '',
              placeholder: node.getAttribute('placeholder') || '',
              aria: node.getAttribute('aria-label') || '',
              role: node.getAttribute('role') || '',
              value: node.value || '',
              text: (node.innerText || node.textContent || '').trim(),
              accept: node.getAttribute('accept') || ''
            }))"""
        )
        actions = await page.locator("button, label, [role='button'], a").evaluate_all(
            """nodes => nodes.map((node, index) => ({
              index,
              tag: node.tagName,
              text: (node.innerText || node.textContent || '').trim(),
              aria: node.getAttribute('aria-label') || '',
              title: node.getAttribute('title') || '',
              href: node.href || ''
            })).filter(item => item.text || item.aria || item.title).slice(0, 200)"""
        )
        body_text = " ".join((await page.locator("body").inner_text()).split())[:12000]
        screenshot = OUT / "publish-form-inspection.png"
        await page.screenshot(path=str(screenshot), full_page=True)
        print(json.dumps({"url": page.url, "fields": fields, "actions": actions, "body_text": body_text, "screenshot": str(screenshot)}, ensure_ascii=False, indent=2))
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
