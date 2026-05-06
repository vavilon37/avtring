import asyncio
import random
import logging
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, BrowserContext

logger = logging.getLogger(__name__)

PROFILE_DIR = str(Path(__file__).parent / "chrome_profile")
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# Comprehensive stealth: removes all automation fingerprints
STEALTH_SCRIPT = """
// Remove webdriver flag
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// Fake plugins (real browser has plugins)
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const arr = [
            {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
            {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''},
            {name: 'Native Client', filename: 'internal-nacl-plugin', description: ''},
        ];
        arr.__proto__ = PluginArray.prototype;
        return arr;
    }
});

// Fake mimeTypes
Object.defineProperty(navigator, 'mimeTypes', {
    get: () => {
        const arr = [
            {type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format'},
        ];
        arr.__proto__ = MimeTypeArray.prototype;
        return arr;
    }
});

// Russian languages
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']});
Object.defineProperty(navigator, 'language', {get: () => 'ru-RU'});

// Hardware concurrency (real CPU cores)
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});

// Device memory
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

// Proper chrome object
window.chrome = {
    runtime: {
        id: undefined,
        connect: function(){},
        sendMessage: function(){},
        onMessage: {addListener: function(){}, removeListener: function(){}},
    },
    loadTimes: function(){
        return {
            requestTime: Date.now() / 1000 - Math.random() * 2,
            startLoadTime: Date.now() / 1000 - Math.random() * 1.5,
            commitLoadTime: Date.now() / 1000 - Math.random() * 1,
            finishDocumentLoadTime: Date.now() / 1000 - Math.random() * 0.5,
            finishLoadTime: Date.now() / 1000,
            firstPaintTime: Date.now() / 1000 - Math.random() * 0.3,
            firstPaintAfterLoadTime: 0,
            navigationType: 'Other',
            wasFetchedViaSpdy: false,
            wasNpnNegotiated: true,
            npnNegotiatedProtocol: 'h2',
            wasAlternateProtocolAvailable: false,
            connectionInfo: 'h2',
        };
    },
    csi: function(){ return {startE: Date.now(), onloadT: Date.now() + 300, pageT: 1200, tran: 15}; },
    app: {isInstalled: false, InstallState: {DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed'}, RunningState: {CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running'}},
};

// Permissions API
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : originalQuery(parameters)
);

// WebGL vendor/renderer spoofing
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.call(this, parameter);
};

// Canvas fingerprint noise
const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(type) {
    const ctx = this.getContext('2d');
    if (ctx) {
        const imageData = ctx.getImageData(0, 0, this.width, this.height);
        for (let i = 0; i < imageData.data.length; i += 100) {
            imageData.data[i] = imageData.data[i] ^ 1;
        }
        ctx.putImageData(imageData, 0, 0);
    }
    return originalToDataURL.apply(this, arguments);
};
"""


BLOCK_COOLDOWN = 600   # 10 min pause after captcha detected


class AvitoParser:
    def __init__(self, on_blocked=None):
        self._pw = None
        self._context: BrowserContext | None = None
        self._blocked_until: float = 0
        self._on_blocked = on_blocked  # async callback()

    async def start(self):
        self._pw = await async_playwright().start()
        await self._new_context()
        logger.info("Parser started (Playwright persistent)")

    async def _new_context(self):
        if self._context:
            await self._context.close()

        ua = random.choice(USER_AGENTS)
        chrome_path = CHROME_PATH if Path(CHROME_PATH).exists() else None

        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--disable-restore-session-state",
            "--restore-last-session=false",
            "--window-size=1366,768",
            "--disable-features=VizDisplayCompositor",
            f"--user-agent={ua}",
        ]

        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            executable_path=chrome_path,
            headless=True,
            args=args,
            user_agent=ua,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
            },
        )
        await self._context.add_init_script(STEALTH_SCRIPT)

        for page in self._context.pages[1:]:
            await page.close()

        logger.info("New browser context created")

    async def stop(self):
        if self._context:
            await self._context.close()
        if self._pw:
            await self._pw.stop()
        logger.info("Parser stopped")

    @staticmethod
    def _is_blocked(html: str) -> bool:
        if len(html) > 100_000:
            return False
        lower = html.lower()
        return (
            "доступ ограничен" in lower
            or 'class="firewall' in lower
            or ("captcha" in lower and len(html) < 50_000)
            or len(html) < 30_000
        )

    async def _get_html(self, url: str, referer: str = "", wait_selector: str = "") -> str:
        # If we're in cooldown after a block — skip
        import time
        if time.time() < self._blocked_until:
            wait_sec = int(self._blocked_until - time.time())
            logger.info(f"Cooldown after block, {wait_sec}s left — skipping {url}")
            return ""

        page = await self._context.new_page()
        try:
            if referer:
                await page.set_extra_http_headers({"Referer": referer})

            await asyncio.sleep(random.uniform(1.5, 3.5))
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # Detect IP block / captcha
            early_html = await page.content()
            if self._is_blocked(early_html):
                import time
                self._blocked_until = time.time() + BLOCK_COOLDOWN
                logger.warning(
                    f"Avito block/captcha detected (html_len={len(early_html)}) — "
                    f"pausing {BLOCK_COOLDOWN//60} min."
                )
                Path("last_failed_page.html").write_text(early_html, encoding="utf-8")
                if self._on_blocked:
                    try:
                        await self._on_blocked()
                    except Exception:
                        pass
                return ""

            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=20000)
                except Exception:
                    await page.evaluate("window.scrollBy(0, 300)")
                    await asyncio.sleep(2)
                    try:
                        # fallback selectors for detail page
                        fallbacks = [
                            '[data-marker="item-view/item-description"]',
                            '[data-marker="seller-info/name"]',
                            '[data-marker="item-view/item-price"]',
                        ]
                        found = False
                        for sel in fallbacks:
                            try:
                                await page.wait_for_selector(sel, timeout=5000)
                                found = True
                                break
                            except Exception:
                                continue
                        if not found:
                            html = await page.content()
                            logger.warning(f"No selector for {url}, html_len={len(html)}")
                            Path("last_failed_page.html").write_text(html, encoding="utf-8")
                            return ""
                    except Exception:
                        return ""
            else:
                await asyncio.sleep(random.uniform(1.5, 2.5))

            # Human-like scroll
            await page.evaluate(f"window.scrollBy(0, {random.randint(100, 400)})")
            await asyncio.sleep(random.uniform(0.5, 1.0))

            return await page.content()
        except Exception as e:
            logger.error(f"Page error {url}: {e}")
            return ""
        finally:
            await page.close()

    async def fetch_listings(self, url: str) -> list[dict]:
        await asyncio.sleep(random.uniform(1.0, 3.0))
        html = await self._get_html(url, wait_selector='[data-marker="item"]')
        if not html:
            return []
        return self._parse_list_html(html, url)

    async def fetch_listing_detail(self, listing: dict) -> dict:
        url = listing["link"]
        if not url:
            return listing
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        clean_url = urlunparse(parsed._replace(query="", fragment=""))
        await asyncio.sleep(random.uniform(0.5, 1.2))
        # Try fast selector first (title loads before description)
        html = await self._get_html(
            clean_url,
            referer="https://www.avito.ru/",
            wait_selector='[data-marker="item-view/title-info"]',
        )
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

        Path("last_detail_page.html").write_text(html, encoding="utf-8")

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
        # Try old marker first, then new Avito structure
        params_section = soup.find(attrs={"data-marker": "item-view/item-params"})
        if params_section:
            for li in params_section.find_all("li"):
                spans = li.find_all("span")
                if len(spans) >= 2:
                    key = spans[0].get_text(strip=True).rstrip(":")
                    val = spans[1].get_text(strip=True)
                    if key and val:
                        params[key] = val
        if not params:
            # New Avito layout: params are in dl/dt+dd pairs
            for dl in soup.find_all("dl"):
                dts = dl.find_all("dt")
                dds = dl.find_all("dd")
                for dt, dd in zip(dts, dds):
                    key = dt.get_text(strip=True).rstrip(":")
                    val = dd.get_text(strip=True)
                    if key and val:
                        params[key] = val
        if not params:
            # Another new layout: params-wrapper with key/value spans
            for section in soup.find_all(class_=lambda c: c and "params" in c.lower()):
                rows = section.find_all(class_=lambda c: c and "param" in c.lower())
                for row in rows:
                    spans = row.find_all("span")
                    if len(spans) >= 2:
                        key = spans[0].get_text(strip=True).rstrip(":")
                        val = spans[-1].get_text(strip=True)
                        if key and val and key != val:
                            params[key] = val
        result["params"] = params

        loc_el = soup.find(attrs={"data-marker": "item-view/item-address"})
        if loc_el:
            result["location"] = loc_el.get_text(strip=True)

        date_el = soup.find(attrs={"data-marker": "item-view/item-date"})
        if date_el:
            result["date"] = date_el.get_text(strip=True)

        return result
