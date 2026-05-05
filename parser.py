import asyncio
import random
import logging
from typing import Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Human-like delays (ms)
DELAY_MIN = 800
DELAY_MAX = 2500

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-accelerated-2d-canvas",
    "--no-first-run",
    "--no-zygote",
    "--disable-gpu",
    "--window-size=1920,1080",
    "--lang=ru-RU",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'permissions', {
    get: () => ({ query: (p) => Promise.resolve({ state: 'granted' }) })
});
"""


class AvitoParser:
    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=STEALTH_ARGS,
        )
        logger.info("Browser started")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser stopped")

    async def _new_context(self) -> BrowserContext:
        ua = random.choice(USER_AGENTS)
        context = await self._browser.new_context(
            user_agent=ua,
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            extra_http_headers={
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "DNT": "1",
            },
        )
        await context.add_init_script(STEALTH_JS)
        return context

    async def _human_delay(self):
        await asyncio.sleep(random.uniform(DELAY_MIN / 1000, DELAY_MAX / 1000))

    async def fetch_listings(self, url: str) -> list[dict]:
        context = await self._new_context()
        page: Page = await context.new_page()
        listings = []

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self._human_delay()

            # Scroll to load lazy images
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            await asyncio.sleep(0.5)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.5)

            content = await page.content()
            listings = self._parse_html(content, url)

        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
        finally:
            await context.close()

        return listings

    def _parse_html(self, html: str, base_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        listings = []

        # Avito uses data-marker attributes — more stable than class names
        items = soup.find_all("div", attrs={"data-marker": "item"})

        for item in items:
            try:
                listing = self._extract_item(item)
                if listing:
                    listings.append(listing)
            except Exception as e:
                logger.debug(f"Skip item parse error: {e}")

        logger.info(f"Parsed {len(listings)} listings from {base_url}")
        return listings

    def _extract_item(self, item) -> Optional[dict]:
        # ID
        item_id = item.get("data-item-id") or item.get("id", "")
        if not item_id:
            return None

        # Title
        title_el = item.find(attrs={"itemprop": "name"}) or item.find("h3")
        title = title_el.get_text(strip=True) if title_el else "Без названия"

        # Price
        price_el = item.find(attrs={"data-marker": "item-price"})
        price = price_el.get_text(strip=True) if price_el else "Цена не указана"

        # Link
        link_el = item.find("a", attrs={"data-marker": "item-title"}) or item.find("a", href=True)
        href = link_el["href"] if link_el else ""
        link = f"https://www.avito.ru{href}" if href.startswith("/") else href

        # Image
        img_el = item.find("img")
        image_url = ""
        if img_el:
            image_url = img_el.get("src") or img_el.get("data-src") or ""

        # Location + time
        geo_el = item.find(attrs={"data-marker": "item-address"})
        location = geo_el.get_text(strip=True) if geo_el else ""

        date_el = item.find(attrs={"data-marker": "item-date"})
        date = date_el.get_text(strip=True) if date_el else ""

        return {
            "id": str(item_id),
            "title": title,
            "price": price,
            "link": link,
            "image_url": image_url,
            "location": location,
            "date": date,
        }
