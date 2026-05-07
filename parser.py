import asyncio
import random
import logging
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from bs4 import BeautifulSoup, NavigableString
from playwright.async_api import async_playwright, BrowserContext

logger = logging.getLogger(__name__)

PROFILE_DIR = str(Path(__file__).parent / "chrome_profile")
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]

# Windows-appropriate GPU profiles to match Windows UA
_WEBGL_PROFILES = [
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580 Series Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]


def _build_stealth_script(webgl_vendor: str, webgl_renderer: str, canvas_seed: int) -> str:
    return f"""
// Remove webdriver flag
Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});

// Fake plugins
Object.defineProperty(navigator, 'plugins', {{
    get: () => {{
        const arr = [
            {{name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'}},
            {{name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''}},
            {{name: 'Native Client', filename: 'internal-nacl-plugin', description: ''}},
        ];
        arr.__proto__ = PluginArray.prototype;
        return arr;
    }}
}});

Object.defineProperty(navigator, 'mimeTypes', {{
    get: () => {{
        const arr = [
            {{type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format'}},
        ];
        arr.__proto__ = MimeTypeArray.prototype;
        return arr;
    }}
}});

Object.defineProperty(navigator, 'languages', {{get: () => ['ru-RU', 'ru', 'en-US', 'en']}});
Object.defineProperty(navigator, 'language', {{get: () => 'ru-RU'}});
Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => {random.choice([4, 6, 8, 12])}}});
Object.defineProperty(navigator, 'deviceMemory', {{get: () => {random.choice([4, 8, 16])}}});

window.chrome = {{
    runtime: {{
        id: undefined,
        connect: function(){{}},
        sendMessage: function(){{}},
        onMessage: {{addListener: function(){{}}, removeListener: function(){{}}}},
    }},
    loadTimes: function(){{
        return {{
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
        }};
    }},
    csi: function(){{ return {{startE: Date.now(), onloadT: Date.now() + 300, pageT: 1200, tran: 15}}; }},
    app: {{isInstalled: false, InstallState: {{DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed'}}, RunningState: {{CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running'}}}},
}};

const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({{state: Notification.permission}})
        : originalQuery(parameters)
);

// WebGL with randomized GPU profile
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {{
    if (parameter === 37445) return '{webgl_vendor}';
    if (parameter === 37446) return '{webgl_renderer}';
    return getParameter.call(this, parameter);
}};

// Canvas fingerprint noise — unique per session via seed
const _seed = {canvas_seed};
const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(type) {{
    const ctx = this.getContext('2d');
    if (ctx) {{
        const imageData = ctx.getImageData(0, 0, this.width, this.height);
        for (let i = 0; i < imageData.data.length; i += 97 + (_seed % 13)) {{
            imageData.data[i] = imageData.data[i] ^ (_seed % 3 + 1);
        }}
        ctx.putImageData(imageData, 0, 0);
    }}
    return originalToDataURL.apply(this, arguments);
}};

// AudioContext fingerprint noise
if (window.AudioContext || window.webkitAudioContext) {{
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    const origGetChannelData = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function() {{
        const arr = origGetChannelData.apply(this, arguments);
        for (let i = 0; i < arr.length; i += 137) {{
            arr[i] += (_seed % 100) * 0.0000001;
        }}
        return arr;
    }};
}}
"""


BLOCK_COOLDOWNS = [120, 300, 600]  # 2 min → 5 min → 10 min backoff


class AvitoParser:
    def __init__(self, on_blocked=None):
        self._pw = None
        self._context: BrowserContext | None = None
        self._blocked_until: float = 0
        self._block_count: int = 0
        self._on_blocked = on_blocked
        self._request_count: int = 0
        self._rotate_at: int = random.randint(20, 30)
        self._context_lock = asyncio.Lock()

    async def start(self):
        self._pw = await async_playwright().start()
        await self._new_context()
        logger.info("Parser started (Playwright persistent)")

    async def _new_context(self):
        if self._context:
            await self._context.close()

        ua = random.choice(USER_AGENTS)
        webgl_vendor, webgl_renderer = random.choice(_WEBGL_PROFILES)
        canvas_seed = random.randint(1000, 9999)
        stealth = _build_stealth_script(webgl_vendor, webgl_renderer, canvas_seed)

        chrome_path = CHROME_PATH if Path(CHROME_PATH).exists() else None

        w, h = random.choice([(1366, 768), (1440, 900), (1920, 1080), (1280, 800)])

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
            f"--window-size={w},{h}",
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
            viewport={"width": w, "height": h},
            extra_http_headers={
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
            },
        )
        await self._context.add_init_script(stealth)

        for page in self._context.pages[1:]:
            await page.close()

        self._rotate_at = self._request_count + random.randint(20, 30)
        logger.info(f"New browser context: UA={ua[:40]}... WebGL={webgl_vendor}, next rotate at req {self._rotate_at}")

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

    @staticmethod
    async def _human_mouse(page, viewport_w: int, viewport_h: int):
        # Start from center area (where mouse would realistically be)
        cx = viewport_w // 2 + random.randint(-80, 80)
        cy = viewport_h // 2 + random.randint(-60, 60)
        await page.mouse.move(cx, cy, steps=random.randint(3, 7))
        await asyncio.sleep(random.uniform(0.1, 0.3))

        for _ in range(random.randint(2, 4)):
            tx = random.randint(120, viewport_w - 120)
            ty = random.randint(80, viewport_h - 150)
            # Curved path: move through a midpoint offset from the straight line
            mx = (cx + tx) // 2 + random.randint(-100, 100)
            my = (cy + ty) // 2 + random.randint(-60, 60)
            await page.mouse.move(mx, my, steps=random.randint(4, 10))
            await asyncio.sleep(random.uniform(0.03, 0.12))
            await page.mouse.move(tx, ty, steps=random.randint(4, 10))
            await asyncio.sleep(random.uniform(0.08, 0.25))
            cx, cy = tx, ty

    @staticmethod
    async def _human_scroll(page):
        # Use real wheel events — not detectable as scripted JS
        for _ in range(random.randint(2, 5)):
            delta = random.randint(80, 300)
            await page.mouse.wheel(0, delta)
            await asyncio.sleep(random.uniform(0.2, 0.6))

    async def _get_html(self, url: str, referer: str = "", wait_selector: str = "") -> str:
        if time.time() < self._blocked_until:
            wait_sec = int(self._blocked_until - time.time())
            logger.info(f"Cooldown after block, {wait_sec}s left — skipping {url}")
            return ""

        async with self._context_lock:
            self._request_count += 1
            if self._request_count >= self._rotate_at:
                logger.info(f"Rotating browser context after {self._request_count} requests")
                await self._new_context()
            context = self._context

        page = await context.new_page()
        try:
            if referer:
                await page.set_extra_http_headers({"Referer": referer})

            await asyncio.sleep(random.uniform(1.5, 4.0))
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            early_html = await page.content()
            if self._is_blocked(early_html):
                cooldown = BLOCK_COOLDOWNS[min(self._block_count, len(BLOCK_COOLDOWNS) - 1)]
                self._blocked_until = time.time() + cooldown
                self._block_count += 1
                logger.warning(
                    f"Avito block/captcha detected (html_len={len(early_html)}) — "
                    f"attempt {self._block_count}, pausing {cooldown//60} min."
                )
                Path("last_failed_page.html").write_text(early_html, encoding="utf-8")
                if self._on_blocked:
                    try:
                        await self._on_blocked()
                    except Exception:
                        pass
                return ""

            vp = page.viewport_size or {"width": 1366, "height": 768}

            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=20000)
                except Exception:
                    await asyncio.sleep(random.uniform(1.5, 2.5))
                    try:
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

            # Human behaviour after content is loaded
            await self._human_mouse(page, vp["width"], vp["height"])
            await self._human_scroll(page)
            await asyncio.sleep(random.uniform(0.4, 1.0))

            self._block_count = 0  # successful request — reset backoff
            return await page.content()
        except Exception as e:
            logger.error(f"Page error {url}: {e}")
            return ""
        finally:
            await page.close()

    async def fetch_listings(self, url: str) -> list[dict]:
        html = await self._get_html(url, wait_selector='[data-marker="item"]')
        if not html:
            return []
        return self._parse_list_html(html, url)

    async def fetch_listing_detail(self, listing: dict) -> dict:
        url = listing["link"]
        if not url:
            return listing
        parsed = urlparse(url)
        clean_url = urlunparse(parsed._replace(query="", fragment=""))
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

    @staticmethod
    def _extract_element_value(el) -> str:
        # 1. Plain text content
        text = el.get_text(strip=True)
        if text:
            return text
        # 2. Avito condition ratings: value in aria-label, title, or data-* on children
        for child in el.find_all(True):
            for attr in ("aria-label", "title", "data-value", "data-rating", "data-label", "content"):
                v = child.get(attr, "")
                if v and v.strip():
                    return v.strip()
        # 3. Try the element's own attributes
        for attr in ("aria-label", "title", "data-value", "content"):
            v = el.get(attr, "")
            if v and v.strip():
                return v.strip()
        return ""

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

        # Method 1: data-marker section
        # Avito structure: <span class="d6e8fd2e...">KEY[<span>: </span>][<img>]</span>VALUE_TEXT
        # Value is a NavigableString after the key span, NOT inside a span
        params_section = soup.find(attrs={"data-marker": "item-view/item-params"})
        if params_section:
            for li in params_section.find_all("li"):
                key_span = li.find("span", class_="d6e8fd2e3d52b32a")
                if not key_span:
                    continue
                key = "".join(str(c) for c in key_span.children if isinstance(c, NavigableString)).strip().rstrip(":")
                p_el = key_span.parent
                val_parts = [str(c).strip() for c in p_el.children if isinstance(c, NavigableString) and str(c).strip()]
                val = " ".join(val_parts).strip()
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
