import asyncio
import random
import logging
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, BrowserContext

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']});
window.chrome = {runtime: {}};
"""


class AvitoParser:
    def __init__(self):
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def start(self):
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        await self._new_context()
        logger.info("Parser started (Playwright)")

    async def _new_context(self):
        if self._context:
            await self._context.close()
        self._context = await self._browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        await self._context.add_init_script(STEALTH_SCRIPT)

    async def stop(self):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        logger.info("Parser stopped")

    async def _get_html(self, url: str, referer: str = "") -> str:
        page = await self._context.new_page()
        try:
            if referer:
                await page.set_extra_http_headers({"Referer": referer})
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.5, 3.0))
            html = await page.content()

            if "captcha" in html.lower() and 'data-marker="item"' not in html:
                logger.warning(f"Captcha detected for {url}, rotating context...")
                await self._new_context()
                return ""

            return html
        except Exception as e:
            logger.error(f"Page error {url}: {e}")
            return ""
        finally:
            await page.close()

    async def fetch_listings(self, url: str) -> list[dict]:
        await asyncio.sleep(random.uniform(1.0, 3.0))
        html = await self._get_html(url)
        if not html:
            return []
        listings = self._parse_list_html(html, url)
        return listings

    async def fetch_listing_detail(self, listing: dict) -> dict:
        url = listing["link"]
        if not url:
            return listing
        await asyncio.sleep(random.uniform(0.8, 2.0))
        html = await self._get_html(url, referer="https://www.avito.ru/")
        if not html:
            return listing
        return self._parse_detail_html(html, listing)

    def _parse_list_html(self, html: str, base_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        listings = []
        items = soup.find_all("div", attrs={"data-marker": "item"})
        for item in items:
            try:
                listing = self._extract_list_item(item)
                if listing:
                    listings.append(listing)
            except Exception as e:
                logger.debug(f"Skip item: {e}")
        logger.info(f"Parsed {len(listings)} listings from {base_url}")
        return listings

    def _extract_list_item(self, item) -> dict | None:
        item_id = item.get("data-item-id") or item.get("id", "")
        if not item_id:
            return None

        title_el = item.find(attrs={"itemprop": "name"}) or item.find("h3")
        title = title_el.get_text(strip=True) if title_el else "Без названия"

        price_el = item.find(attrs={"data-marker": "item-price"})
        price = price_el.get_text(strip=True) if price_el else "Цена не указана"

        link_el = item.find("a", attrs={"data-marker": "item-title"}) or item.find("a", href=True)
        href = link_el["href"] if link_el else ""
        link = f"https://www.avito.ru{href}" if href.startswith("/") else href

        img_el = item.find("img")
        image_url = ""
        if img_el:
            image_url = img_el.get("src") or img_el.get("data-src") or ""

        geo_el = item.find(attrs={"data-marker": "item-address"})
        location = geo_el.get_text(strip=True) if geo_el else ""

        date_el = item.find(attrs={"data-marker": "item-date"})
        date = date_el.get_text(strip=True) if date_el else ""

        return {
            "id": str(item_id),
            "title": title,
            "price": price,
            "link": link,
            "images": [image_url] if image_url else [],
            "location": location,
            "date": date,
            "description": "",
            "seller_name": "",
            "seller_type": "",
            "params": {},
        }

    def _parse_detail_html(self, html: str, base: dict) -> dict:
        soup = BeautifulSoup(html, "lxml")
        result = base.copy()

        images = []
        gallery = soup.find("div", attrs={"data-marker": "image-frame/image-wrapper"})
        if not gallery:
            gallery = soup.find("div", class_=lambda c: c and "gallery" in c.lower())
        if gallery:
            for img in gallery.find_all("img"):
                src = img.get("src") or img.get("data-src") or ""
                if src and src not in images:
                    images.append(src)
        if not images and base.get("images"):
            images = base["images"]
        result["images"] = images[:10]

        desc_el = (
            soup.find(attrs={"data-marker": "item-view/item-description"})
            or soup.find("div", attrs={"itemprop": "description"})
        )
        if desc_el:
            result["description"] = desc_el.get_text(strip=True)[:800]

        seller_el = soup.find(attrs={"data-marker": "seller-info/name"})
        if seller_el:
            result["seller_name"] = seller_el.get_text(strip=True)

        seller_type_el = soup.find(attrs={"data-marker": "seller-info/label"})
        if seller_type_el:
            result["seller_type"] = seller_type_el.get_text(strip=True)

        params = {}
        params_section = soup.find(attrs={"data-marker": "item-view/item-params"})
        if params_section:
            for li in params_section.find_all("li"):
                spans = li.find_all("span")
                if len(spans) >= 2:
                    key = spans[0].get_text(strip=True).rstrip(":")
                    val = spans[1].get_text(strip=True)
                    if key and val:
                        params[key] = val
        result["params"] = params

        loc_el = soup.find(attrs={"data-marker": "item-view/item-address"})
        if loc_el:
            result["location"] = loc_el.get_text(strip=True)

        date_el = soup.find(attrs={"data-marker": "item-view/item-date"})
        if date_el:
            result["date"] = date_el.get_text(strip=True)

        return result
