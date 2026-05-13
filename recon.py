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
            if "avito.ru" not in rurl:
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
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(6)  # let XHR responses settle

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
            print(f"  ✓  {var}: found at pos {m.start()} (raw extraction not implemented for JS vars)")

    # --- XHR summary ---
    print(f"\nXHR JSON responses ({len(xhr_log)} total):")
    for entry in sorted(xhr_log, key=lambda x: -x.get("bytes", 0))[:20]:
        if "error" in entry:
            continue
        has_items = bool(entry.get("items"))
        marker = "📦" if has_items else "  "
        print(f"  {marker} [{entry['status']}] {entry['url'][:90]}")
        print(f"       {entry['bytes']:,} bytes  keys={entry.get('top_keys', '?')[:6]}")
        for ip in entry.get("items", []):
            print(f"       items @ {ip['path']}: {ip['count']} items, first_keys={ip['first_keys'][:8]}")

    summary = {
        "url": url,
        "html_bytes": len(html),
        "state_vars": found_states,
        "xhr": xhr_log,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


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
        list_summary = await recon_page(page, SEARCH_URL, "list_page")

        # 2. Pick first listing and recon detail page
        html = await page.content()
        m = re.search(r'href="(/[a-z][^"]+_(\d{8,}))"', html)
        if m:
            detail_url = f"https://www.avito.ru{m.group(1)}"
            print(f"\n=== DETAIL PAGE ===")
            # Navigate back to main to mimic human behaviour
            await page.goto("https://www.avito.ru/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            await recon_page(page, detail_url, "detail_page")
        else:
            print("\n[!] No detail URL found in list HTML — run warmup.py first to get past captcha")

        await ctx.close()

    print(f"\nAll results saved to: {OUTPUT_DIR.absolute()}")
    print("Look in recon_results/list_page/summary.json and detail_page/summary.json")


asyncio.run(main())
