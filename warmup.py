"""
Run this once to warm up the browser profile.
It opens Avito in a real Chrome window — browse manually, solve captcha if needed.
Press Enter in terminal when done to close.
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

PROFILE_DIR = str(Path(__file__).parent / "chrome_profile_0")


async def main():
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--start-maximized",
            ],
            locale="ru-RU",
            timezone_id="Europe/Moscow",
        )

        page = await context.new_page()
        await page.goto("https://www.avito.ru/", wait_until="domcontentloaded")
        print("\n✓ Avito открыт в браузере.")
        print("  1. Поищи в поиске что-нибудь (например 'iPhone 13 256')")
        print("  2. Покликай по нескольким объявлениям")
        print("  3. Если появится капча — реши её вручную")
        print("  Время: 3-5 минут.")
        print("  Когда готово — нажми Enter здесь.\n")

        await asyncio.get_event_loop().run_in_executor(None, input)
        await context.close()
        print("Профиль сохранён. Теперь запускай python main.py")


asyncio.run(main())
