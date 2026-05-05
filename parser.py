import asyncio
import random
import logging
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def _make_headers(referer: str = "") -> dict:
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if referer else "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }
    if referer:
        h["Referer"] = referer
    return h


class AvitoParser:
    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    async def start(self):
        self._client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            http2=True,
        )
        logger.info("Parser started")

    async def stop(self):
        if self._client:
            await self._client.aclose()
        logger.info("Parser stopped")

    async def fetch_listings(self, url: str) -> list[dict]:
        await asyncio.sleep(random.uniform(1.5, 4.0))
        try:
            resp = await self._client.get(url, headers=_make_headers())
            if resp.status_code != 200:
                logger.warning(f"HTTP {resp.status_code} for {url}")
                return []
            listings = self._parse_list_html(resp.text, url)
            return listings
        except Exception as e:
            logger.error(f"Fetch error for {url}: {e}")
            return []

    async def fetch_listing_detail(self, listing: dict) -> dict:
        """Fetch full details for a single listing page."""
        url = listing["link"]
        if not url:
            return listing
        await asyncio.sleep(random.uniform(0.8, 2.5))
        try:
            resp = await self._client.get(
                url, headers=_make_headers(referer="https://www.avito.ru/")
            )
            if resp.status_code != 200:
                return listing
            return self._parse_detail_html(resp.text, listing)
        except Exception as e:
            logger.debug(f"Detail fetch error {url}: {e}")
            return listing

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
            # detail fields filled later
            "description": "",
            "seller_name": "",
            "seller_type": "",
            "params": {},
        }

    def _parse_detail_html(self, html: str, base: dict) -> dict:
        soup = BeautifulSoup(html, "lxml")
        result = base.copy()

        # All images
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
        result["images"] = images[:10]  # max 10 photos

        # Description
        desc_el = (
            soup.find(attrs={"data-marker": "item-view/item-description"})
            or soup.find("div", attrs={"itemprop": "description"})
        )
        if desc_el:
            result["description"] = desc_el.get_text(strip=True)[:800]

        # Seller name
        seller_el = soup.find(attrs={"data-marker": "seller-info/name"})
        if seller_el:
            result["seller_name"] = seller_el.get_text(strip=True)

        # Seller type (частное лицо / компания)
        seller_type_el = soup.find(attrs={"data-marker": "seller-info/label"})
        if seller_type_el:
            result["seller_type"] = seller_type_el.get_text(strip=True)

        # Params (характеристики: год, пробег, состояние и т.д.)
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

        # Location more precise
        loc_el = soup.find(attrs={"data-marker": "item-view/item-address"})
        if loc_el:
            result["location"] = loc_el.get_text(strip=True)

        # Date more precise
        date_el = soup.find(attrs={"data-marker": "item-view/item-date"})
        if date_el:
            result["date"] = date_el.get_text(strip=True)

        return result
