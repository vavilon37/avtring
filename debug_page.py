"""Debug: open the watch URL and save HTML + screenshot"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

PROFILE_DIR = str(Path(__file__).parent / "chrome_profile")
URL = "https://www.avito.ru/rossiya/telefony?sort=date&s=104"

async def main():
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
            locale="ru-RU",
            timezone_id="Europe/Moscow",
        )
        page = await context.new_page()
        print(f"Opening {URL} ...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_selector('[data-marker="item"]', timeout=15000)
        await asyncio.sleep(3)

        html = await page.content()
        Path("debug.html").write_text(html, encoding="utf-8")
        await page.screenshot(path="debug.png", full_page=False)

        has_items = 'data-marker="item"' in html
        has_captcha = "captcha" in html.lower()
        print(f"has_items={has_items}, has_captcha={has_captcha}, html_len={len(html)}")
        print("Saved debug.html and debug.png")
        print("\nPress Enter to close...")
        await asyncio.get_event_loop().run_in_executor(None, input)
        await context.close()

asyncio.run(main())
