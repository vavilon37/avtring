"""
One-time migration: convert broken slug-based URLs back to text-search URLs.
Run once on the server: python migrate_urls.py
"""
import sqlite3
from urllib.parse import urlparse, parse_qs, urlencode

DB_PATH = "avito_ringer.db"

_SLUG_TO_QUERY = {
    "iphone_17_pro_max": "iPhone+17+Pro+Max",
    "iphone_17_pro":     "iPhone+17+Pro",
    "iphone_17_plus":    "iPhone+17+Plus",
    "iphone_17":         "iPhone+17",
    "iphone_16_pro_max": "iPhone+16+Pro+Max",
    "iphone_16_pro":     "iPhone+16+Pro",
    "iphone_16_plus":    "iPhone+16+Plus",
    "iphone_16_mini":    "iPhone+16+mini",
    "iphone_16":         "iPhone+16",
    "iphone_15_pro_max": "iPhone+15+Pro+Max",
    "iphone_15_pro":     "iPhone+15+Pro",
    "iphone_15_plus":    "iPhone+15+Plus",
    "iphone_15":         "iPhone+15",
    "iphone_14_pro_max": "iPhone+14+Pro+Max",
    "iphone_14_pro":     "iPhone+14+Pro",
    "iphone_14_plus":    "iPhone+14+Plus",
    "iphone_14":         "iPhone+14",
    "iphone_13_pro_max": "iPhone+13+Pro+Max",
    "iphone_13_pro":     "iPhone+13+Pro",
    "iphone_13_mini":    "iPhone+13+mini",
    "iphone_13":         "iPhone+13",
    "iphone_12_pro_max": "iPhone+12+Pro+Max",
    "iphone_12_pro":     "iPhone+12+Pro",
    "iphone_12_mini":    "iPhone+12+mini",
    "iphone_12":         "iPhone+12",
}

_STORAGE_SLUG_TO_GB = {
    "64_gb":  64,
    "128_gb": 128,
    "256_gb": 256,
    "512_gb": 512,
    "1_tb":   1000,
}


def _fix_url(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]

    # Broken format: /CITY/telefony/mobilnye_telefony/BRAND/MODEL/STORAGE
    if len(parts) < 3 or parts[1] != "telefony" or parts[2] != "mobilnye_telefony":
        return None  # already fine

    city = parts[0]
    model_slug   = parts[4] if len(parts) > 4 else ""
    storage_slug = parts[5] if len(parts) > 5 else ""

    qp = parse_qs(parsed.query)

    query   = _SLUG_TO_QUERY.get(model_slug, "")
    stor_gb = _STORAGE_SLUG_TO_GB.get(storage_slug, 0)
    pmin    = qp.get("pmin",        [""])[0]
    pmax    = qp.get("pmax",        [""])[0]
    cnd     = qp.get("cnd",         [""])[0]
    sel_raw = qp.get("seller_type", [""])[0]
    seller  = "private" if sel_raw == "private" else ("company" if sel_raw == "shop" else "")

    q_parts = []
    if query:
        q_parts.append(query.replace("+", " "))
    if stor_gb:
        q_parts.append("1 ТБ" if stor_gb == 1000 else str(stor_gb))

    params = {}
    if q_parts:
        params["q"] = " ".join(q_parts)
    if pmin:
        params["pmin"] = pmin
    if pmax:
        params["pmax"] = pmax
    if cnd:
        params["cnd"] = cnd
    if seller == "private":
        params["seller_type"] = "private"
    elif seller == "company":
        params["seller_type"] = "shop"
    params["sort"] = "date"
    params["s"]    = "104"

    base = f"https://www.avito.ru/{city}/telefony"
    return f"{base}?{urlencode(params, encoding='utf-8')}"


def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT id, url FROM watches")
    watches = cur.fetchall()

    fixed = 0
    for w in watches:
        new_url = _fix_url(w["url"])
        if new_url:
            print(f"Watch #{w['id']}:")
            print(f"  OLD: {w['url']}")
            print(f"  NEW: {new_url}")
            cur.execute("UPDATE watches SET url = ? WHERE id = ?", (new_url, w["id"]))
            fixed += 1
        else:
            print(f"Watch #{w['id']}: OK  {w['url'][:80]}")

    con.commit()
    con.close()
    print(f"\nДобавлено: 0  Исправлено: {fixed}  Всего: {len(watches)}")


if __name__ == "__main__":
    main()
