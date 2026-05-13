"""
recon.py — diagnostic script: shows what data Avito embeds in its pages.
Run once after warmup.py to discover API endpoints and embedded state structures.
Results are saved to recon_results/ directory.

Usage:
    python recon.py
"""
import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

PROFILE_DIR = str(Path(__file__).parent / "chrome_profile_0")
OUTPUT_DIR = Path(__file__).parent / "recon_results"

# Change this to any search URL you're monitoring
SEARCH_URL = (
    "https://www.avito.ru/moskva/telefony/mobile_phones-ASgBAgICAUSSA8YQ"
    "?q=iphone+13+256"
)

_STATE_PATTERNS = {
    "__NEXT_DATA__":      r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    "__PRELOADED_STATE__": r'window\.__PRELOADED_STATE__\s*=\s*(\{)',
    "__initialData__":    r'window\.__initialData__\s*=\s*(\{)',
    "__APOLLO_STATE__":   r'window\.__APOLLO_STATE__\s*=\s*(\{)',
}


def _find_items_recursive(obj, path="", results=None):
    if results is None:
        results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if k == "items" and isinstance(v, list) and len(v) >= 2:
                first_keys = list(v[0].keys())[:15] if v and isinstance(v[0], dict) else "?"
                results.append({"path": p, "count": len(v), "first_keys": first_keys})
            _find_items_recursive(v, p, results)
    elif isinstance(obj, list) and obj:
        _find_items_recursive(obj[0], f"{path}[0]", results)
    return results


async def recon_page(page, url: str, label: str) -> dict:
    out_dir = OUTPUT_DIR / label
    out_dir.mkdir(parents=True, exist_ok=True)

    xhr_log: list[dict] = []

    async def on_response(resp):
        try:
            rurl = resp.url
            # Capture ALL avito.ru JSON (not just items)
            if "avito.ru" not in rurl and "avito.st" not in rurl:
                return
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            body = await resp.body()
            data = json.loads(body)
            entry: dict = {
                "url": rurl,
                "status": resp.status,
                "bytes": len(body),
            }
            if isinstance(data, dict):
                entry["top_keys"] = list(data.keys())[:20]
                items_paths = _find_items_recursive(data)
                if items_paths:
                    entry["items"] = items_paths
                    safe = re.sub(r"[^\w]", "_", rurl.split("?")[0].rsplit("/", 1)[-1])[:40]
                    (out_dir / f"xhr_{safe}.json").write_text(
                        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
            elif isinstance(data, list):
                entry["top_keys"] = f"list[{len(data)}]"
            xhr_log.append(entry)
        except Exception as e:
            xhr_log.append({"url": resp.url[:100], "error": str(e)})

    page.on("response", on_response)

    print(f"\n{'=' * 60}")
    print(f"Loading: {url}")
    await page.goto(url, wait_until="networkidle", timeout=90000)
    await asyncio.sleep(3)

    html = await page.content()
    (out_dir / "page.html").write_text(html, encoding="utf-8")
    print(f"HTML: {len(html):,} chars  →  saved to {label}/page.html")

    # --- Embedded state variables ---
    found_states: dict = {}
    print("\nEmbedded state variables:")
    for var, pattern in _STATE_PATTERNS.items():
        m = re.search(pattern, html, re.DOTALL)
        if not m:
            found_states[var] = {"found": False}
            print(f"  ✗  {var}")
            continue

        if var == "__NEXT_DATA__":
            raw = m.group(1)
            try:
                data = json.loads(raw)
                items_paths = _find_items_recursive(data)
                found_states[var] = {
                    "found": True,
                    "bytes": len(raw),
                    "top_keys": list(data.keys()),
                    "items_paths": items_paths,
                }
                (out_dir / f"{var}.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(f"  ✓  {var}: {len(raw):,} chars, keys={list(data.keys())[:8]}")
                for ip in items_paths:
                    print(f"       items @ {ip['path']}: {ip['count']} items, first_keys={ip['first_keys'][:8]}")
            except Exception as e:
                found_states[var] = {"found": True, "parse_error": str(e)}
                print(f"  ✓  {var}: found but JSON parse error: {e}")
        else:
            found_states[var] = {"found": True, "pos": m.start()}
            print(f"  ✓  {var}: found at pos {m.start()}")

    # --- Find any large JSON-like script blocks ---
    print("\nLarge <script> blocks (>10KB):")
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
    for i, sc in enumerate(scripts):
        if len(sc) > 10000:
            # Try to find variable assignments
            vars_found = re.findall(r'(?:window\.|var |const |let )(\w+)\s*=\s*\{', sc[:500])
            print(f"  script[{i}]: {len(sc):,} chars  vars={vars_found[:5]}")
            # Save first 5KB of large scripts
            (out_dir / f"script_{i}.js").write_text(sc[:5000], encoding="utf-8", errors="ignore")

    # --- Analyze item HTML structure ---
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    items = soup.find_all("div", attrs={"data-marker": "item"})
    print(f"\nListing items found: {len(items)}")
    if items:
        first = items[0]
        # Show all data-markers inside first item
        inner_markers = [t.get("data-marker") for t in first.find_all(attrs={"data-marker": True})]
        print(f"  Markers inside first item: {inner_markers}")
        # Look for seller info
        seller_candidates = []
        for tag in first.find_all(True):
            for attr in ("data-marker", "itemprop", "class"):
                val = tag.get(attr, "")
                if val and any(kw in str(val).lower() for kw in ("seller", "user", "account", "owner", "company")):
                    seller_candidates.append(f"{tag.name}[{attr}={val!r}]: {tag.get_text(strip=True)[:60]}")
        print(f"  Seller-related tags: {seller_candidates or ['none found']}")
        # Save full first item HTML
        (out_dir / "first_item.html").write_text(str(first), encoding="utf-8")
        print(f"  Full HTML of first item saved to {label}/first_item.html")

    # --- Find listing URLs ---
    all_links = re.findall(r'href="(/[^"]+)"', html)
    listing_links = [l for l in all_links if re.search(r'_\d{8,}', l)]
    if listing_links:
        print(f"\nListing URLs found: {len(listing_links)}")
        print(f"  First: {listing_links[0]}")
    else:
        print("\nNo listing URLs in href= — trying data-url:")
        data_urls = re.findall(r'data-url="([^"]+)"', html)
        listing_data_urls = [u for u in data_urls if re.search(r'_\d{8,}', u)]
        print(f"  data-url listings: {len(listing_data_urls)}")
        if listing_data_urls:
            print(f"  First: {listing_data_urls[0]}")
            listing_links = listing_data_urls

    # --- XHR summary ---
    print(f"\nXHR JSON responses ({len(xhr_log)} total):")
    for entry in sorted(xhr_log, key=lambda x: -x.get("bytes", 0))[:20]:
        if "error" in entry:
            continue
        has_items = bool(entry.get("items"))
        marker = ">>>" if has_items else "   "
        print(f"  {marker} [{entry['status']}] {entry['url'][:90]}")
        print(f"       {entry['bytes']:,} bytes  keys={entry.get('top_keys', '?')[:6]}")
        for ip in entry.get("items", []):
            print(f"       items @ {ip['path']}: {ip['count']} items, first_keys={ip['first_keys'][:8]}")

    summary = {
        "url": url,
        "html_bytes": len(html),
        "items_in_dom": len(items),
        "state_vars": found_states,
        "xhr": xhr_log,
        "listing_links_sample": listing_links[:3],
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary, listing_links


async def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
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
        page = await ctx.new_page()

        # 1. List page
        print("\n=== LIST PAGE ===")
        list_summary, listing_links = await recon_page(page, SEARCH_URL, "list_page")

        # 2. Pick first listing and recon detail page
        if listing_links:
            link = listing_links[0]
            detail_url = f"https://www.avito.ru{link}" if link.startswith("/") else link
            print(f"\n=== DETAIL PAGE ===")
            await page.goto("https://www.avito.ru/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            await recon_page(page, detail_url, "detail_page")
        else:
            print("\n[!] No listing URLs found — page may be blocked or captcha. Run warmup.py first.")

        await ctx.close()

    print(f"\nAll results saved to: {OUTPUT_DIR.absolute()}")
    print("Look in recon_results/list_page/summary.json and detail_page/summary.json")


asyncio.run(main())
