"""
analyze_html.py — ищет встроенные данные объявлений в сохранённом HTML Avito.
Запускай после recon.py: python analyze_html.py
"""
import json
import re
from pathlib import Path
from bs4 import BeautifulSoup

HTML_PATH = Path("recon_results/list_page/page.html")

if not HTML_PATH.exists():
    print(f"[!] Файл не найден: {HTML_PATH}")
    print("    Сначала запусти: python recon.py")
    raise SystemExit(1)

print(f"Читаю {HTML_PATH} ({HTML_PATH.stat().st_size:,} байт)...")
html = HTML_PATH.read_text(encoding="utf-8", errors="ignore")
soup = BeautifulSoup(html, "lxml")

# ─── 1. Все <script> теги — оцениваем «данные vs код» ─────────────────────────
print(f"\n{'='*60}")
print("1. АНАЛИЗ <SCRIPT> БЛОКОВ")
scripts = soup.find_all("script")
print(f"   Всего script-тегов: {len(scripts)}")

best_data_script = None
best_data_score = 0

for i, sc in enumerate(scripts):
    content = sc.string or ""
    if len(content) < 5000:
        continue

    lo = content.lower()

    # Признаки ДАННЫХ
    iphone_n  = lo.count("iphone")
    price_n   = content.count('"price"') + content.count('"priceDetailed"')
    title_n   = content.count('"title"')
    items_n   = content.count('"items"') + content.count('"catalog"')
    seller_n  = content.count('"seller"') + content.count('"sellerType"')
    id_n      = content.count('"id":')
    rub_n     = content.count("₽") + content.count("\\u20bd")
    data_score = iphone_n*15 + price_n*8 + title_n*6 + items_n*10 + seller_n*6 + id_n*2 + rub_n*3

    # Признаки КОДА
    func_n  = content.count("function")
    arrow_n = content.count("=>")
    proto_n = content.count(".prototype.")
    code_score = func_n + arrow_n + proto_n * 3

    sc_type = sc.get("type", "")
    sc_id   = sc.get("id", "")
    tag = f"[{i}] type={sc_type!r} id={sc_id!r}" if sc_type or sc_id else f"[{i}]"

    verdict = "КОД" if code_score > data_score else "ДАННЫЕ❓"
    if data_score > 20:
        verdict = "★ ДАННЫЕ ★"

    print(f"\n  script{tag}: {len(content):,} chars")
    print(f"    data_score={data_score}  (iphone={iphone_n}, price={price_n}, title={title_n}, items={items_n}, seller={seller_n})")
    print(f"    code_score={code_score}  (function={func_n}, =>={arrow_n})")
    print(f"    → {verdict}")

    if data_score > best_data_score:
        best_data_score = data_score
        best_data_script = (i, content)

# ─── 2. Глубокий анализ лучшего script-блока ──────────────────────────────────
if best_data_script and best_data_score > 10:
    idx, content = best_data_script
    print(f"\n{'='*60}")
    print(f"2. ГЛУБОКИЙ АНАЛИЗ script[{idx}] (score={best_data_score})")

    # 2a. Ищем "items" массив
    m = re.search(r'"items"\s*:\s*\[', content)
    if m:
        print(f"\n  'items': найден в позиции {m.start()}")
        chunk = content[m.start():]
        # Найти конец массива
        depth = 0
        in_str = False
        esc = False
        found_end = None
        for j, c in enumerate(chunk):
            if esc:           esc = False; continue
            if c == '\\' and in_str: esc = True; continue
            if c == '"':      in_str = not in_str; continue
            if in_str:        continue
            if c == '[':      depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    found_end = j
                    break
        if found_end:
            arr_raw = chunk[:found_end + 1]
            try:
                items = json.loads(arr_raw)
                print(f"  Распаршено {len(items)} элементов!")
                if items and isinstance(items[0], dict):
                    print(f"  Ключи первого элемента: {list(items[0].keys())[:20]}")
                    # Проверяем есть ли нужные поля
                    first = items[0]
                    has_title  = "title" in first or "name" in first
                    has_price  = "price" in first or "priceDetailed" in first
                    has_seller = "seller" in first or "user" in first
                    has_desc   = "description" in first or "body" in first
                    print(f"  title={has_title}  price={has_price}  seller={has_seller}  description={has_desc}")
                    if has_title and has_price:
                        out = Path("recon_results/found_items.json")
                        out.write_text(json.dumps(items[:3], ensure_ascii=False, indent=2), encoding="utf-8")
                        print(f"  ✓ Первые 3 элемента сохранены в recon_results/found_items.json")
            except json.JSONDecodeError as e:
                print(f"  JSON parse error: {e}")
                print(f"  Первые 200 символов массива: {arr_raw[:200]!r}")
    else:
        print("  'items' массив НЕ найден")

    # 2b. Ищем window.VARNAME = {
    var_matches = re.findall(r'(?:window\.|self\.)(\w+)\s*=\s*\{', content[:5000])
    if var_matches:
        print(f"\n  window.* присваивания (первые 5000 char): {var_matches}")

    # 2c. Ищем JSON-like начала — любой { с "id": внутри рядом
    json_starts = [(m.start(), content[m.start():m.start()+100])
                   for m in re.finditer(r'\{"id"\s*:', content)]
    if json_starts:
        print(f"\n  Объекты начинающиеся с {{\"id\": — найдено {len(json_starts)} штук")
        print(f"  Первый: {json_starts[0][1]!r}")
    else:
        # Пробуем itemId
        json_starts2 = [(m.start(), content[m.start():m.start()+100])
                        for m in re.finditer(r'\{"itemId"\s*:', content)]
        if json_starts2:
            print(f"\n  Объекты с {{\"itemId\": — найдено {len(json_starts2)} штук")
            print(f"  Первый: {json_starts2[0][1]!r}")

    # 2d. Сохраняем первые 20KB и последние 5KB для ручного просмотра
    out_head = Path("recon_results/big_script_head.txt")
    out_head.write_text(content[:20000], encoding="utf-8", errors="ignore")
    out_tail = Path("recon_results/big_script_tail.txt")
    out_tail.write_text(content[-5000:], encoding="utf-8", errors="ignore")
    print(f"\n  Первые 20KB → recon_results/big_script_head.txt")
    print(f"  Последние 5KB → recon_results/big_script_tail.txt")

# ─── 3. <script type="application/json"> кроме __NEXT_DATA__ ──────────────────
print(f"\n{'='*60}")
print("3. <SCRIPT TYPE=application/json>")
json_scripts = soup.find_all("script", attrs={"type": "application/json"})
print(f"   Найдено: {len(json_scripts)}")
for sc in json_scripts:
    sc_id = sc.get("id", "")
    content = sc.string or ""
    print(f"   id={sc_id!r}: {len(content):,} chars")
    if len(content) > 100:
        try:
            data = json.loads(content)
            keys = list(data.keys())[:10] if isinstance(data, dict) else f"list[{len(data)}]"
            print(f"   keys: {keys}")
        except Exception as e:
            print(f"   parse error: {e}")

# ─── 4. data-state / data-initial-state атрибуты ──────────────────────────────
print(f"\n{'='*60}")
print("4. HTML-АТРИБУТЫ С ДАННЫМИ (data-state, data-initial-state и т.д.)")
for attr in ("data-state", "data-initial-state", "data-redux-state", "data-props", "data-app-state"):
    els = soup.find_all(attrs={attr: True})
    if els:
        for el in els[:2]:
            val = el.get(attr, "")
            print(f"   {attr} на <{el.name}>: {len(val)} chars")
            try:
                data = json.loads(val)
                print(f"   keys: {list(data.keys())[:10]}")
            except Exception:
                print(f"   первые 100 chars: {val[:100]!r}")
    else:
        print(f"   {attr}: не найден")

# ─── 5. Нестандартные window.* в HTML (ищем в всём тексте) ───────────────────
print(f"\n{'='*60}")
print("5. НЕСТАНДАРТНЫЕ window.* ПРИСВАИВАНИЯ (поиск по всему HTML)")
patterns = re.findall(r'window\.([A-Za-z_]\w{3,})\s*=\s*[\{\[]', html)
unique = list(dict.fromkeys(patterns))
if unique:
    print(f"   Найдены: {unique[:30]}")
    for name in unique:
        idx_html = html.find(f"window.{name}")
        snippet = html[idx_html:idx_html+80]
        print(f"   window.{name}: {snippet!r}")
else:
    print("   Не найдено")

print(f"\n{'='*60}")
print("ГОТОВО. Проверь recon_results/")
