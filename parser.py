import asyncio
import json
import re
import random
import logging
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse, urlunparse, urlencode, parse_qs
from bs4 import BeautifulSoup, NavigableString
from playwright.async_api import async_playwright, BrowserContext

logger = logging.getLogger(__name__)

try:
    from playwright_stealth import stealth_async as _stealth_async
    _HAS_STEALTH = True
    logger.info("playwright-stealth loaded")
except ImportError:
    _HAS_STEALTH = False
    logger.info("playwright-stealth not installed — using built-in stealth only")

try:
    from curl_cffi.requests import AsyncSession as _CurlSession
    _HAS_CURL_CFFI = True
    print("[parser] curl-cffi OK")
except Exception as _cffi_err:
    _HAS_CURL_CFFI = False
    print(f"[parser] curl-cffi FAIL: {type(_cffi_err).__name__}: {_cffi_err}")

PROFILE_DIR = str(Path(__file__).parent / "chrome_profile")
PROFILE_DIRS = [
    str(Path(__file__).parent / f"chrome_profile_{i}") for i in range(6)
]
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


BLOCK_COOLDOWNS = [60, 180, 300]  # 1 min → 3 min → 5 min backoff

_CFFI_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "cache-control": "max-age=0",
    "upgrade-insecure-requests": "1",
    "sec-ch-ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-user": "?1",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}


class AvitoParser:
    def __init__(self, on_blocked=None):
        self._pw = None
        self._context: BrowserContext | None = None
        self._blocked_until: float = 0
        self._block_count: int = 0
        self._on_blocked = on_blocked
        self._request_count: int = 0
        self._rotate_at: int = random.randint(500, 1000)
        self._context_lock = asyncio.Lock()
        self._profile_idx: int = 0  # start with profile_0 (manually warmed by warmup.py)
        self._needs_warmup: bool = False
        self._curl_cookies: dict = {}  # cookies extracted from Playwright for curl_cffi
        self._api_url: str | None = None          # captured list API endpoint
        self._api_headers: dict = {}              # headers needed for API calls
        self._last_intercepted: list[dict] = []   # listings from last XHR interception

    async def start(self):
        self._pw = await async_playwright().start()
        await self._new_context()
        self._needs_warmup = True  # always warm up before first real request
        logger.info(f"Parser started. curl_cffi={'ON' if _HAS_CURL_CFFI else 'OFF'}")

    async def _new_context(self):
        if self._context:
            await self._context.close()
        self._curl_cookies = {}  # old profile cookies are invalid

        profile = PROFILE_DIRS[self._profile_idx % len(PROFILE_DIRS)]
        self._profile_idx += 1

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
            user_data_dir=profile,
            executable_path=chrome_path,
            headless=False,
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

        self._rotate_at = self._request_count + random.randint(500, 1000)
        logger.info(
            f"New browser context: profile={Path(profile).name}, "
            f"UA={ua[:40]}... WebGL={webgl_vendor}, next rotate at req {self._rotate_at}"
        )

    async def stop(self):
        if self._context:
            await self._context.close()
        if self._pw:
            await self._pw.stop()
        logger.info("Parser stopped")

    async def _extract_cookies(self) -> None:
        """Copy Avito cookies from Playwright context → used by curl_cffi fast path."""
        if not self._context:
            return
        try:
            raw = await self._context.cookies(urls=["https://www.avito.ru"])
            self._curl_cookies = {c["name"]: c["value"] for c in raw}
            logger.info(f"curl_cffi: saved {len(self._curl_cookies)} cookies")
        except Exception as e:
            logger.info(f"cookie extract failed: {e}")

    async def _cffi_get(self, url: str, referer: str = "") -> str:
        """Chrome-impersonating HTTP fetch via curl_cffi. No browser needed.
        Returns HTML string, or '' if unavailable/blocked."""
        if not _HAS_CURL_CFFI or not self._curl_cookies:
            return ""
        headers = dict(_CFFI_HEADERS)
        if referer:
            headers["referer"] = referer
            headers["sec-fetch-site"] = "same-origin"
        else:
            headers["sec-fetch-site"] = "none"
        try:
            async with _CurlSession(impersonate="chrome131") as session:
                r = await session.get(
                    url,
                    headers=headers,
                    cookies=self._curl_cookies,
                    timeout=20,
                    allow_redirects=True,
                )
            if r.status_code == 200:
                html = r.text
                if not self._is_blocked(html):
                    return html
                logger.info(f"cffi: blocked page ({len(html)} chars) for {url[:60]}")
            elif r.status_code == 429:
                cooldown = BLOCK_COOLDOWNS[min(self._block_count, len(BLOCK_COOLDOWNS) - 1)]
                self._blocked_until = time.time() + cooldown
                self._block_count += 1
                logger.warning(f"cffi: 429 rate-limited — {cooldown}s cooldown (block #{self._block_count})")
                if self._on_blocked:
                    try:
                        await self._on_blocked()
                    except Exception:
                        pass
            else:
                logger.info(f"cffi: HTTP {r.status_code} for {url[:60]}")
        except Exception as e:
            logger.info(f"cffi error {url[:50]}: {e}")
        return ""

    @staticmethod
    def _is_blocked(html: str) -> bool:
        if len(html) > 80_000:
            return False
        lower = html.lower()
        if (
            "доступ ограничен" in lower
            or 'class="firewall' in lower
            or "access denied" in lower
            or ("captcha" in lower and len(html) < 50_000)
            or ("robot" in lower and len(html) < 15_000)
        ):
            return True
        return len(html) < 8_000

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

    _WARMUP_PAGES = [
        "https://www.avito.ru/all/elektronika",
        "https://www.avito.ru/all/telefony",
        "https://www.avito.ru/all/bytovaya_elektronika",
        "https://www.avito.ru/all/igry_pristavki_i_programmy",
        "https://www.avito.ru/all/noutbuki",
    ]

    async def _warmup(self):
        """Browse avito naturally with the fresh profile before real requests."""
        context = self._context
        pages_to_visit = ["https://www.avito.ru/"] + [random.choice(self._WARMUP_PAGES)]
        logger.info(f"Warming up new profile ({len(pages_to_visit)} pages)...")
        for i, url in enumerate(pages_to_visit):
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(random.uniform(2.5, 5.0))
                vp = page.viewport_size or {"width": 1366, "height": 768}
                await self._human_scroll(page)
                await self._human_mouse(page, vp["width"], vp["height"])
                await asyncio.sleep(random.uniform(2.0, 4.5))
            except Exception as e:
                logger.debug(f"Warmup page error ({url}): {e}")
            finally:
                await page.close()
            if i < len(pages_to_visit) - 1:
                await asyncio.sleep(random.uniform(3.0, 7.0))
        logger.info("Warmup complete — starting real requests")
        await self._extract_cookies()

    @staticmethod
    async def _human_scroll(page):
        # Use real wheel events — not detectable as scripted JS
        for _ in range(random.randint(2, 5)):
            delta = random.randint(80, 300)
            await page.mouse.wheel(0, delta)
            await asyncio.sleep(random.uniform(0.2, 0.6))

    async def _get_html(self, url: str, referer: str = "", wait_selector: str = "",
                        playwright_only: bool = False) -> str:
        if time.time() < self._blocked_until:
            wait_sec = int(self._blocked_until - time.time())
            logger.info(f"Cooldown after block, {wait_sec}s left — skipping {url}")
            return ""

        if self._needs_warmup:
            self._needs_warmup = False
            try:
                await self._warmup()
            except Exception as e:
                logger.warning(f"Warmup failed: {e}")

        if not playwright_only:
            # ── Fast path: curl_cffi (no browser, Chrome TLS fingerprint) ──
            html = await self._cffi_get(url, referer)
            if html:
                logger.info(f"cffi OK ({len(html)} chars): {url[:70]}")
                self._block_count = 0
                return html

            # If cffi triggered a 429 block cooldown, don't try Playwright
            if time.time() < self._blocked_until:
                return ""

        # ── Playwright browser ─────────────────────────────────────────
        async with self._context_lock:
            self._request_count += 1
            if self._request_count >= self._rotate_at:
                logger.info(f"Rotating browser context after {self._request_count} requests")
                await self._new_context()
                self._needs_warmup = True  # warm up the fresh profile before real requests
            context = self._context

        page = await context.new_page()
        try:
            if _HAS_STEALTH:
                await _stealth_async(page)
            if referer:
                await page.set_extra_http_headers({"Referer": referer})

            # Intercept JSON API responses (list pages only)
            _captured: dict = {}
            if wait_selector and "item" in wait_selector:
                async def _on_response(resp):
                    try:
                        rurl = resp.url
                        if "avito.ru" not in rurl:
                            return
                        ct = resp.headers.get("content-type", "")
                        if "json" not in ct:
                            return
                        body = await resp.body()
                        data = json.loads(body)
                        if not isinstance(data, dict):
                            return
                        # Find items list in various Avito response shapes
                        items = (
                            data.get("items")
                            or (data.get("result") or {}).get("items")
                            or (data.get("catalog") or {}).get("items")
                            or (data.get("data") or {}).get("items")
                        )
                        if isinstance(items, list) and len(items) >= 3:
                            if len(items) > len(_captured.get("items", [])):
                                _captured["url"] = rurl
                                _captured["items"] = items
                    except Exception:
                        pass
                page.on("response", _on_response)

            await asyncio.sleep(random.uniform(0.3, 1.2))
            try:
                await asyncio.wait_for(
                    page.goto(url, wait_until="domcontentloaded", timeout=60000),
                    timeout=65.0,
                )
            except asyncio.TimeoutError:
                logger.warning(f"page.goto asyncio timeout for {url[:60]}")
                return ""

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
                # Switch to a fresh profile and schedule warmup for next request
                async with self._context_lock:
                    await self._new_context()
                self._needs_warmup = True
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
                await asyncio.sleep(random.uniform(0.3, 0.8))

            # Human behaviour after content is loaded
            await self._human_mouse(page, vp["width"], vp["height"])
            await self._human_scroll(page)
            await asyncio.sleep(random.uniform(0.2, 0.6))

            self._block_count = 0  # successful request — reset backoff
            html = await page.content()
            await self._extract_cookies()  # refresh curl_cffi cookies

            # Process intercepted API data
            if _captured:
                self._api_url = _captured["url"]
                parsed = self._parse_api_items(_captured["items"])
                self._last_intercepted = parsed
                logger.info(
                    f"XHR intercepted: {len(parsed)} listings from {self._api_url[:80]}"
                )
                if _captured["items"]:
                    logger.info(f"API item keys: {list(_captured['items'][0].keys())}")
            else:
                self._last_intercepted = []

            return html
        except Exception as e:
            logger.error(f"Page error {url}: {e}")
            return ""
        finally:
            await page.close()

    @staticmethod
    def _parse_api_items(items: list) -> list[dict]:
        """Convert Avito internal API item dicts to our listing format."""
        listings = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or item.get("itemId") or "")
            if not item_id:
                continue

            title = item.get("title") or item.get("name") or ""

            price_obj = item.get("price") or item.get("priceDetailed") or {}
            if isinstance(price_obj, dict):
                raw = price_obj.get("value") or price_obj.get("valueText") or "Цена не указана"
                price_text = f"{int(raw):,} ₽".replace(",", "\xa0") if str(raw).isdigit() else str(raw)
            elif isinstance(price_obj, (int, float)):
                price_text = f"{int(price_obj):,} ₽".replace(",", "\xa0")
            else:
                price_text = str(price_obj) if price_obj else "Цена не указана"

            url_path = item.get("url") or item.get("urlPath") or ""
            link = f"https://www.avito.ru{url_path}" if url_path.startswith("/") else url_path

            images = []
            for key in ("images", "photos", "image"):
                val = item.get(key)
                if isinstance(val, list):
                    for img in val[:8]:
                        src = img if isinstance(img, str) else (img.get("url") or img.get("src") or img.get("640x480") or "")
                        if src:
                            images.append(src)
                    break
                elif isinstance(val, dict):
                    src = val.get("url") or val.get("src") or ""
                    if src:
                        images.append(src)
                    break

            geo = item.get("geo") or item.get("location") or {}
            location = (geo.get("name") or geo.get("city") or "") if isinstance(geo, dict) else str(geo or "")

            date_raw = item.get("time") or item.get("sortTime") or item.get("closingAt") or ""
            if isinstance(date_raw, (int, float)) and date_raw > 1_000_000:
                from datetime import datetime, timezone as tz
                date_str = datetime.fromtimestamp(date_raw, tz=tz.utc).strftime(
                    "%a, %d %b %Y %H:%M:%S +0000"
                )
            else:
                date_str = str(date_raw) if date_raw else ""

            desc = item.get("description") or item.get("body") or ""

            seller = item.get("seller") or item.get("user") or {}
            if isinstance(seller, dict):
                seller_name = seller.get("name") or seller.get("displayName") or ""
                seller_type = seller.get("type") or seller.get("accountType") or ""
            else:
                seller_name = seller_type = ""

            params = {}
            for key in ("params", "attributes", "characteristics"):
                val = item.get(key)
                if isinstance(val, list):
                    for p in val:
                        if isinstance(p, dict):
                            k = p.get("title") or p.get("name") or ""
                            v = p.get("value") or p.get("values") or ""
                            if isinstance(v, list):
                                v = ", ".join(str(x) for x in v)
                            if k and v:
                                params[k] = str(v)
                    break
                elif isinstance(val, dict):
                    params = {k: str(v) for k, v in val.items() if v}
                    break

            listings.append({
                "id": item_id,
                "title": title,
                "price": price_text,
                "link": link,
                "images": images,
                "location": location,
                "date": date_str,
                "description": desc[:800] if desc else "",
                "seller_name": seller_name,
                "seller_type": seller_type,
                "params": params,
            })
        return listings

    async def _fetch_api(self, search_url: str) -> list[dict]:
        """Call the captured Avito XHR endpoint directly via cffi."""
        if not self._api_url or not _HAS_CURL_CFFI or not self._curl_cookies:
            return []
        if time.time() < self._blocked_until:
            return []
        try:
            async with _CurlSession(impersonate="chrome131") as session:
                r = await session.get(
                    self._api_url,
                    headers={
                        **_CFFI_HEADERS,
                        "accept": "application/json, text/plain, */*",
                        "sec-fetch-dest": "empty",
                        "sec-fetch-mode": "cors",
                        "sec-fetch-site": "same-origin",
                        "referer": search_url,
                    },
                    cookies=self._curl_cookies,
                    timeout=10,
                )
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                data = r.json()
                items = (
                    data.get("items")
                    or (data.get("result") or {}).get("items")
                    or (data.get("catalog") or {}).get("items")
                    or (data.get("data") or {}).get("items")
                )
                if isinstance(items, list) and items:
                    listings = self._parse_api_items(items)
                    logger.info(f"API cffi: {len(listings)} listings (no browser!)")
                    return listings
            elif r.status_code in (401, 403, 404):
                logger.info(f"API cffi: HTTP {r.status_code} — clearing cached endpoint")
                self._api_url = None
            else:
                logger.info(f"API cffi: HTTP {r.status_code}")
        except Exception as e:
            logger.info(f"API cffi error: {e}")
        return []

    @staticmethod
    def _to_rss_url(search_url: str) -> str:
        p = urlparse(search_url)
        rss_path = p.path.rstrip("/") + ".rss"
        kept = {k: v[0] for k, v in parse_qs(p.query).items()
                if k in ("q", "pmax", "pmin", "seller_type", "user", "companyId")}
        return urlunparse(p._replace(path=rss_path, query=urlencode(kept) if kept else ""))

    async def _fetch_rss(self, search_url: str) -> list[dict]:
        # Avito RSS requires a logged-in session — returns HTML for guests.
        # Disabled to avoid wasting time on guaranteed-empty requests.
        return []

    @staticmethod
    def _parse_rss(xml_text: str) -> list[dict]:
        # Strip ALL XML 1.0 invalid code points before parsing
        clean = re.sub(
            r'[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]',
            '', xml_text,
        )
        # BeautifulSoup + lxml-xml handles malformed RSS via lxml recovery mode
        try:
            soup = BeautifulSoup(clean, "lxml-xml")
        except Exception as e:
            logger.warning(f"RSS parse error: {e}")
            return []

        items = soup.find_all("item")
        if not items:
            logger.info(f"RSS: no <item> in soup; tags={[t.name for t in soup.find_all(True)][:20]}")
            return []

        listings = []
        for item in items:
            def _t(tag: str) -> str:
                el = item.find(tag)
                return el.get_text(strip=True) if el else ""

            link = _t("link")
            if not link:
                continue
            m = re.search(r"_(\d{6,})(?:\?|$|/)", link)
            if not m:
                continue
            item_id = m.group(1)

            price_el = item.find("price")
            price_text = price_el.get_text(strip=True) if price_el else ""
            if price_text.isdigit():
                price_text = f"{int(price_text):,} \u20bd".replace(",", "\xa0")
            elif not price_text:
                price_text = "\u0426\u0435\u043d\u0430 \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u0430"

            loc_el = item.find("location")
            location = loc_el.get_text(strip=True) if loc_el else ""

            img = ""
            enc = item.find("enclosure")
            if enc:
                img = enc.get("url", "")
            if not img:
                img_el = item.find("images")
                if img_el:
                    first_img = img_el.find("image")
                    if first_img:
                        img = first_img.get_text(strip=True)

            desc_raw = _t("description")
            desc = re.sub(r'<[^>]+>', ' ', desc_raw).strip()
            desc = re.sub(r'\s+', ' ', desc)[:800]

            listings.append({
                "id": item_id,
                "title": _t("title"),
                "price": price_text,
                "link": link,
                "images": [img] if img else [],
                "location": location,
                "date": _t("pubDate"),
                "description": desc,
                "seller_name": "",
                "seller_type": "",
                "params": {},
            })
        return listings

    @staticmethod
    def _extract_next_data(html: str) -> dict | None:
        """Extract Next.js __NEXT_DATA__ JSON embedded in the page."""
        m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
        return None

    @staticmethod
    def _listings_from_next_data(data: dict) -> list[dict]:
        """Try to extract listing items from Next.js page data."""
        if not isinstance(data, dict):
            return []
        candidates = [
            (data.get("props") or {}).get("pageProps") or {},
        ]
        # also try one level deeper
        pp = (data.get("props") or {}).get("pageProps") or {}
        for sub in ("initialState", "data", "catalog"):
            if isinstance(pp.get(sub), dict):
                candidates.append(pp[sub])
        for c in candidates:
            if not isinstance(c, dict):
                continue
            items = (
                (c.get("catalog") or {}).get("items")
                or c.get("items")
                or (c.get("result") or {}).get("items")
                or (c.get("data") or {}).get("items")
            )
            if isinstance(items, list) and len(items) >= 2:
                return AvitoParser._parse_api_items(items)
        return []

    @staticmethod
    def _detail_from_next_data(data: dict) -> dict:
        """Extract detail-page fields (description, seller, params) from __NEXT_DATA__."""
        result: dict = {}
        if not isinstance(data, dict):
            return result
        pp = (data.get("props") or {}).get("pageProps") or {}
        item = (
            pp.get("item")
            or (pp.get("initialState") or {}).get("item")
            or (pp.get("data") or {}).get("item")
        )
        if not isinstance(item, dict):
            return result
        desc = item.get("description") or item.get("body") or ""
        if desc:
            result["description"] = str(desc)[:800]
        seller = item.get("seller") or item.get("user") or {}
        if isinstance(seller, dict):
            if seller.get("name") or seller.get("displayName"):
                result["seller_name"] = seller.get("name") or seller.get("displayName") or ""
            if seller.get("type") or seller.get("accountType"):
                result["seller_type"] = seller.get("type") or seller.get("accountType") or ""
        params: dict = {}
        for key in ("params", "attributes", "characteristics"):
            val = item.get(key)
            if isinstance(val, list):
                for p in val:
                    if isinstance(p, dict):
                        k = p.get("title") or p.get("name") or ""
                        v = p.get("value") or p.get("values") or ""
                        if isinstance(v, list):
                            v = ", ".join(str(x) for x in v)
                        if k and v:
                            params[k] = str(v)
                break
            elif isinstance(val, dict):
                params = {str(k): str(v) for k, v in val.items() if v}
                break
        if params:
            result["params"] = params
        return result

    async def fetch_listings(self, url: str) -> list[dict]:
        # Fast path: cffi → captured API endpoint (no browser, ~200ms)
        if self._api_url:
            listings = await self._fetch_api(url)
            if listings:
                return listings

        # Playwright: loads page, intercepts XHR, discovers/refreshes API endpoint
        html = await self._get_html(url, wait_selector='[data-marker="item"]', playwright_only=True)

        # Prefer intercepted API data over HTML parsing (richer, has seller/params)
        if self._last_intercepted:
            logger.info(f"Using intercepted API data ({len(self._last_intercepted)} listings)")
            return self._last_intercepted

        if not html:
            return []

        # Try __NEXT_DATA__ embedded state before falling back to HTML scraping
        nd = self._extract_next_data(html)
        if nd:
            listings = self._listings_from_next_data(nd)
            if listings:
                logger.info(f"__NEXT_DATA__: {len(listings)} listings extracted")
                return listings

        return self._parse_list_html(html, url)

    async def fetch_listing_detail(self, listing: dict) -> dict:
        url = listing["link"]
        if not url:
            return listing

        # Skip detail fetch when XHR interception already gave us full data
        if listing.get("description") and listing.get("seller_name"):
            logger.debug(f"Detail skip: data already present for id={listing.get('id')!r}")
            return listing

        parsed = urlparse(url)
        clean_url = urlunparse(parsed._replace(query="", fragment=""))

        # cffi fast path — Avito detail pages are JS-rendered (React CSR).
        # cffi fetches SSR HTML which has no item content (only nav/footer).
        # Detect empty content and fall back to Playwright automatically.
        if _HAS_CURL_CFFI and self._curl_cookies and time.time() >= self._blocked_until:
            html = await self._cffi_get(clean_url, referer="https://www.avito.ru/")
            if html:
                result = self._parse_detail_html(html, listing)
                if result.get("description") or result.get("seller_name"):
                    return result
                logger.info(f"cffi detail: SSR-only page, no content — falling back to Playwright")

        # Playwright: waits for JS hydration, gets full item data
        html = await self._get_html(
            clean_url,
            referer="https://www.avito.ru/",
            wait_selector='[data-marker="item-view/title-info"]',
            playwright_only=True,
        )
        if not html:
            return {**listing, "_fetch_failed": True}
        return self._parse_detail_html(html, listing)

    def _parse_list_html(self, html: str, base_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        listings = []
        items = soup.find_all("div", attrs={"data-marker": "item"})
        if not items:
            # Diagnostic: log what data-markers ARE present in this HTML
            all_markers = list({t.get("data-marker") for t in soup.find_all(attrs={"data-marker": True})})
            logger.info(f"[list-debug] 0 items found. data-markers in page: {all_markers[:30]}")
            try:
                debug_path = Path("last_list_page.html")
                if not debug_path.exists():
                    debug_path.write_text(html[:80000], encoding="utf-8", errors="ignore")
                    logger.info("[list-debug] saved first 80KB to last_list_page.html")
            except Exception:
                pass
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

        # Fallback: JSON-LD structured data (always present in Avito SSR HTML)
        # Fills description + seller info when data-markers are JS-rendered only
        if not result.get("description") or not result.get("seller_name"):
            for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
                try:
                    data = json.loads(script.string or "")
                    if not isinstance(data, dict):
                        continue
                    if not result.get("description"):
                        desc = data.get("description") or ""
                        if desc:
                            result["description"] = desc[:800]
                    offers = data.get("offers") or {}
                    seller = offers.get("seller") or {}
                    if not result.get("seller_name") and seller.get("name"):
                        result["seller_name"] = seller["name"]
                    if not result.get("seller_type"):
                        stype = seller.get("@type", "")
                        if stype == "Person":
                            result["seller_type"] = "Частное лицо"
                        elif stype in ("Organization", "LocalBusiness"):
                            result["seller_type"] = "Компания"
                except Exception:
                    pass

        # Try __NEXT_DATA__ — may contain description/seller/params not in data-markers
        if not result.get("description") or not result.get("seller_name") or not result.get("params"):
            nd = self._extract_next_data(html)
            if nd:
                nd_detail = self._detail_from_next_data(nd)
                if nd_detail.get("description") and not result.get("description"):
                    result["description"] = nd_detail["description"]
                if nd_detail.get("seller_name") and not result.get("seller_name"):
                    result["seller_name"] = nd_detail["seller_name"]
                if nd_detail.get("seller_type") and not result.get("seller_type"):
                    result["seller_type"] = nd_detail["seller_type"]
                if nd_detail.get("params") and not result.get("params"):
                    result["params"] = nd_detail["params"]
                if nd_detail:
                    logger.debug(f"[detail] __NEXT_DATA__ enriched: {list(nd_detail.keys())}")

        if not result.get("description") and not result.get("seller_name"):
            markers = [t.get("data-marker") for t in soup.find_all(attrs={"data-marker": True})]
            logger.info(f"[detail] no data in {len(html)}-char page. markers={markers[:15]}")

        return result
