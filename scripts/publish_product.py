from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright


MANAGER_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = MANAGER_ROOT.parent
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
PUBLISH_URL = "https://www.goofish.com/publish"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用独立 Edge 会话填写闲鱼发布表单")
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--product-dir", required=True)
    parser.add_argument("--price", type=str, required=True)
    parser.add_argument("--category")
    parser.add_argument("--pricing-method")
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--inspect-categories", action="store_true")
    return parser.parse_args()


def load_product(product_dir: str) -> tuple[Path, str, list[Path]]:
    root = (PROJECT_ROOT / "商品库" / product_dir).resolve()
    library = (PROJECT_ROOT / "商品库").resolve()
    if library not in root.parents or not root.is_dir():
        raise ValueError("商品目录必须位于商品库内")
    copy_path = root / "发布文案.txt"
    copy_text = copy_path.read_text(encoding="utf-8-sig").strip()
    title = copy_text.splitlines()[0].strip() if copy_text else ""
    if not title or len(title) > 20:
        raise ValueError("发布标题为空或超过20个可见字符")
    if len(copy_text) > 1500:
        raise ValueError(f"发布文案超过网页1500字限制：{len(copy_text)}")
    image_dir = root / "图片"
    images = sorted(path for path in image_dir.iterdir() if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
    if len(images) != 5:
        raise ValueError(f"发布图片必须恰好5张，当前为{len(images)}张")
    return root, copy_text, images


async def main() -> None:
    args = parse_args()
    root, copy_text, images = load_product(args.product_dir)
    profile = MANAGER_ROOT / "data" / "browser-profiles" / "msedge" / f"account-{args.account_id}"
    screenshots = MANAGER_ROOT / "data" / "session-screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    screenshot = screenshots / ("publish-submitted.png" if args.submit else "publish-dry-run.png")

    result: dict[str, object] = {
        "submitted": False,
        "product_dir": root.name,
        "title": copy_text.splitlines()[0].strip(),
        "copy_length": len(copy_text),
        "image_count": len(images),
        "price": args.price,
        "shipping": "无需邮寄",
    }

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            executable_path=str(EDGE),
            headless=False,
            no_viewport=True,
            args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(3_000)
            if "/login" in page.url or "passport.goofish.com" in page.url:
                raise RuntimeError("账号登录已失效")

            await page.locator("input[type='file']").set_input_files([str(path) for path in images])
            await page.wait_for_timeout(8_000)

            editor = page.locator("[contenteditable='true']").first
            await editor.wait_for(state="visible", timeout=15_000)
            await editor.fill(copy_text)

            price_input = page.locator("input[placeholder='0.00']").nth(0)
            await price_input.fill(args.price)
            no_shipping = page.locator("input[type='radio'][value='3']")
            # 闲鱼使用 Ant Design 自定义单选框，直接 check 隐藏 input 不会
            # 触发 React 状态更新；点击用户实际看到的标签文字。
            await page.get_by_text("无需邮寄", exact=True).click()
            await page.wait_for_timeout(2_000)

            if args.category:
                await page.locator(".ant-select-selector").first.click()
                await page.get_by_text(args.category, exact=True).last.click()
                await page.wait_for_timeout(2_000)
            if args.pricing_method:
                await page.locator(".ant-select-selector").nth(1).click()
                await page.get_by_text(args.pricing_method, exact=True).last.click()
                await page.wait_for_timeout(1_000)

            file_count = await page.locator("input[type='file']").count()
            editor_text = (await editor.inner_text()).strip()
            copy_content_matches = re.sub(r"\s+", "", editor_text) == re.sub(r"\s+", "", copy_text)
            checked_shipping = await no_shipping.is_checked()
            price_value = await price_input.input_value()
            publish_button = page.get_by_role("button", name="发布", exact=True)
            disabled = await publish_button.is_disabled()
            body_text = await page.locator("body").inner_text()
            category_web_unsupported = "网页版暂不支持发布此分类" in body_text
            category = ""
            pricing_method = ""
            category_items = page.locator(".ant-select-selection-item")
            if await category_items.count():
                category = (await category_items.first.inner_text()).strip()
            if await category_items.count() > 1:
                pricing_method = (await category_items.nth(1).inner_text()).strip()
            category_options: list[str] = []
            pricing_options: list[str] = []
            if args.inspect_categories:
                await page.locator(".ant-select-selector").first.click()
                await page.wait_for_timeout(1_000)
                options = page.locator(".ant-select-item-option-content")
                category_options = [
                    (await options.nth(index).inner_text()).strip()
                    for index in range(await options.count())
                ]
                await page.keyboard.press("Escape")
                if await page.locator(".ant-select-selector").count() > 1:
                    await page.locator(".ant-select-selector").nth(1).click()
                    await page.wait_for_timeout(1_000)
                    pricing = page.locator(".ant-select-item-option-content")
                    pricing_options = [
                        (await pricing.nth(index).inner_text()).strip()
                        for index in range(await pricing.count())
                    ]
            result.update(
                {
                    "page_url": page.url,
                    "file_input_count": file_count,
                    "filled_copy_length": len(editor_text),
                    "copy_content_matches": copy_content_matches,
                    "price_value": price_value,
                    "shipping_checked": checked_shipping,
                    "publish_disabled": disabled,
                    "category": category,
                    "pricing_method": pricing_method,
                    "category_web_unsupported": category_web_unsupported,
                    "category_options": category_options,
                    "pricing_options": pricing_options,
                }
            )
            await page.screenshot(path=str(screenshot), full_page=True)
            result["screenshot"] = str(screenshot)

            if args.submit:
                if disabled or not copy_content_matches or price_value != args.price or not checked_shipping:
                    raise RuntimeError("提交前字段校验失败，已停止发布")
                if category_web_unsupported:
                    raise RuntimeError("当前分类明确不支持网页发布，已停止发布")
                if args.category and category != args.category:
                    raise RuntimeError("分类校验失败，已停止发布")
                if args.pricing_method and pricing_method != args.pricing_method:
                    raise RuntimeError("计价方式校验失败，已停止发布")
                await publish_button.click()
                await page.wait_for_timeout(5_000)
                body_text = " ".join((await page.locator("body").inner_text()).split())[:4000]
                await page.screenshot(path=str(screenshot), full_page=True)
                result.update({"submitted": True, "result_url": page.url, "result_text": body_text})
        finally:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
