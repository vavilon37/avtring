"""
Avito URL builder for phones category.
Category: /rossiya/telefony (ID 15)
"""
from urllib.parse import urlencode

AVITO_BASE = "https://www.avito.ru"

# Модели телефонов для кнопок
PHONE_MODELS = {
    "iPhone 17 Pro Max": "iPhone+17+Pro+Max",
    "iPhone 17 Pro": "iPhone+17+Pro",
    "iPhone 17 Plus": "iPhone+17+Plus",
    "iPhone 17": "iPhone+17",
    "iPhone 16 Pro Max": "iPhone+16+Pro+Max",
    "iPhone 16 Pro": "iPhone+16+Pro",
    "iPhone 16 Plus": "iPhone+16+Plus",
    "iPhone 16 mini": "iPhone+16+mini",
    "iPhone 16": "iPhone+16",
    "iPhone 15 Pro Max": "iPhone+15+Pro+Max",
    "iPhone 15 Pro": "iPhone+15+Pro",
    "iPhone 15 Plus": "iPhone+15+Plus",
    "iPhone 15": "iPhone+15",
    "iPhone 14 Pro Max": "iPhone+14+Pro+Max",
    "iPhone 14 Pro": "iPhone+14+Pro",
    "iPhone 14 Plus": "iPhone+14+Plus",
    "iPhone 14": "iPhone+14",
    "iPhone 13 Pro Max": "iPhone+13+Pro+Max",
    "iPhone 13 Pro": "iPhone+13+Pro",
    "iPhone 13 mini": "iPhone+13+mini",
    "iPhone 13": "iPhone+13",
    "iPhone 12 Pro Max": "iPhone+12+Pro+Max",
    "iPhone 12 Pro": "iPhone+12+Pro",
    "iPhone 12 mini": "iPhone+12+mini",
    "iPhone 12": "iPhone+12",
    "Samsung Galaxy": "Samsung+Galaxy",
    "Xiaomi": "Xiaomi",
    "Любой телефон": "",
}

# Нативные Авито-слаги для iPhone-моделей: query_key -> (brand_slug, model_slug)
_MODEL_SLUGS: dict[str, tuple[str, str]] = {
    "iPhone+17+Pro+Max": ("apple", "iphone_17_pro_max"),
    "iPhone+17+Pro":     ("apple", "iphone_17_pro"),
    "iPhone+17+Plus":    ("apple", "iphone_17_plus"),
    "iPhone+17":         ("apple", "iphone_17"),
    "iPhone+16+Pro+Max": ("apple", "iphone_16_pro_max"),
    "iPhone+16+Pro":     ("apple", "iphone_16_pro"),
    "iPhone+16+Plus":    ("apple", "iphone_16_plus"),
    "iPhone+16+mini":    ("apple", "iphone_16_mini"),
    "iPhone+16":         ("apple", "iphone_16"),
    "iPhone+15+Pro+Max": ("apple", "iphone_15_pro_max"),
    "iPhone+15+Pro":     ("apple", "iphone_15_pro"),
    "iPhone+15+Plus":    ("apple", "iphone_15_plus"),
    "iPhone+15":         ("apple", "iphone_15"),
    "iPhone+14+Pro+Max": ("apple", "iphone_14_pro_max"),
    "iPhone+14+Pro":     ("apple", "iphone_14_pro"),
    "iPhone+14+Plus":    ("apple", "iphone_14_plus"),
    "iPhone+14":         ("apple", "iphone_14"),
    "iPhone+13+Pro+Max": ("apple", "iphone_13_pro_max"),
    "iPhone+13+Pro":     ("apple", "iphone_13_pro"),
    "iPhone+13+mini":    ("apple", "iphone_13_mini"),
    "iPhone+13":         ("apple", "iphone_13"),
    "iPhone+12+Pro+Max": ("apple", "iphone_12_pro_max"),
    "iPhone+12+Pro":     ("apple", "iphone_12_pro"),
    "iPhone+12+mini":    ("apple", "iphone_12_mini"),
    "iPhone+12":         ("apple", "iphone_12"),
}

# Авито-слаги для объёма памяти
_STORAGE_SLUGS: dict[int, str] = {
    64:   "64_gb",
    128:  "128_gb",
    256:  "256_gb",
    512:  "512_gb",
    1000: "1_tb",
}

# Состояние
CONDITIONS = {
    "Новый": "1",
    "Отличное": "2",
    "Хорошее": "3",
    "Удовлетворительное": "4",
    "Любое": "",
}

# Тип продавца
SELLER_TYPES = {
    "Частное лицо": "private",
    "Магазин": "company",
    "Все": "",
}

# Объём встроенной памяти
STORAGE_OPTIONS = {
    "64 ГБ":  64,
    "128 ГБ": 128,
    "256 ГБ": 256,
    "512 ГБ": 512,
    "1 ТБ":   1000,
    "Любой":  0,
}

# Города (топ)
CITIES = {
    "Вся Россия": "rossiya",
    "Москва": "moskva",
    "Санкт-Петербург": "sankt-peterburg",
    "Новосибирск": "novosibirsk",
    "Екатеринбург": "ekaterinburg",
    "Казань": "kazan",
    "Краснодар": "krasnodar",
}


def build_avito_url(filters: dict) -> str:
    city = filters.get("city", "rossiya")
    query = filters.get("query", "")
    pmin = filters.get("pmin", "")
    pmax = filters.get("pmax", "")
    condition = filters.get("condition", "")
    seller_type = filters.get("seller_type", "")
    storage_gb = filters.get("storage_gb", 0)

    slug_info = _MODEL_SLUGS.get(query)
    storage_slug = _STORAGE_SLUGS.get(storage_gb) if storage_gb else None

    params = {}
    if pmin:
        params["pmin"] = pmin
    if pmax:
        params["pmax"] = pmax
    if condition:
        params["cnd"] = condition
    if seller_type == "private":
        params["seller_type"] = "private"
    elif seller_type == "company":
        params["seller_type"] = "shop"
    params["sort"] = "date"
    params["s"] = "104"

    if slug_info:
        brand, model_slug = slug_info
        base = f"{AVITO_BASE}/{city}/telefony/mobilnye_telefony/{brand}/{model_slug}"
        if storage_slug:
            base += f"/{storage_slug}"
    else:
        # Для Samsung/Xiaomi/любой — текстовый поиск с GB в запросе
        q_parts = []
        if query:
            q_parts.append(query.replace("+", " "))
        if storage_gb:
            q_parts.append("1 ТБ" if storage_gb == 1000 else str(storage_gb))
        if q_parts:
            params["q"] = " ".join(q_parts)
        base = f"{AVITO_BASE}/{city}/telefony"

    if params:
        return f"{base}?{urlencode(params, encoding='utf-8')}"
    return base


def label_from_filters(filters: dict) -> str:
    parts = []
    query = filters.get("query", "")
    if query:
        parts.append(query.replace("+", " "))
    storage_gb = filters.get("storage_gb", 0)
    if storage_gb:
        parts.append("1 ТБ" if storage_gb == 1000 else f"{storage_gb} ГБ")
    if filters.get("pmin") or filters.get("pmax"):
        pmin = filters.get("pmin", "0")
        pmax = filters.get("pmax", "∞")
        parts.append(f"{pmin}–{pmax} ₽")
    city_key = filters.get("city", "rossiya")
    city_name = next((k for k, v in CITIES.items() if v == city_key), city_key)
    parts.append(city_name)
    return " · ".join(parts) if parts else "Телефоны"
