"""Диагностика атрибуции. Запуск на боевом ноуте из папки проекта:

    python diag_attr.py "ССЫЛКА_КОТОРУЮ_ТЫ_ВСТАВЛЯЛ_В_ЗАКУП"

Читает avito_ringer.db (только чтение), ничего не меняет.
"""
import sqlite3, sys, re
from urllib.parse import urlparse

DB = "avito_ringer.db"


def extract_item_id(url):
    if not url:
        return None
    path = urlparse(url.strip()).path
    m = re.search(r"_(\d+)/?$", path)
    if m:
        return m.group(1)
    nums = re.findall(r"\d{6,}", path)
    return nums[-1] if nums else None


c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

n = c.execute("SELECT COUNT(*) FROM sent_items").fetchone()[0]
print(f"\n=== sent_items: всего записей = {n} ===")
print("последние 12 присланных (listing_id | время | extract(link) | заголовок):")
for r in c.execute("SELECT listing_id, sent_at, link, title FROM sent_items ORDER BY id DESC LIMIT 12"):
    eid = extract_item_id(r["link"])
    flag = "" if eid == r["listing_id"] else "  ⚠️ID≠extract(link)"
    print(f"  {str(r['listing_id']):>12} | {r['sent_at']} | {str(eid):>12} | {(r['title'] or '')[:34]}{flag}")

url = sys.argv[1] if len(sys.argv) > 1 else None
if url:
    iid = extract_item_id(url)
    print(f"\n=== твоя ссылка ===\n  {url}\n  → item_id = {iid}")
    hit = c.execute("SELECT sent_at, buyer_id FROM sent_items WHERE listing_id = ?", (str(iid),)).fetchall()
    if hit:
        print(f"  ✅ НАЙДЕНО в присланном ({len(hit)} раз): {[dict(h) for h in hit]}")
        print("  → атрибуция ДОЛЖНА быть «через бота». Если была «сам» — баг в коде, пришли этот вывод.")
    else:
        print("  ❌ НЕ найдено в sent_items → «не через бота» (корректно для этого item_id).")
        print("  Сравни свой item_id со столбцом listing_id выше: если такого числа там нет —")
        print("  объявление либо прислано до деплоя hook, либо ссылка другого формата.")
else:
    print("\n(ссылку не передал — запусти: python diag_attr.py \"<ссылка>\")")

print("\n=== последние сделки (deals) ===")
for r in c.execute("SELECT id, item_id, attributed, buy_price, status FROM deals ORDER BY id DESC LIMIT 6"):
    print(f"  #{r['id']} item_id={r['item_id']} attributed={r['attributed']} закуп={r['buy_price']} {r['status']}")
print()
