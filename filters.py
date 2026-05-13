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

    q_parts = []
    if query:
        q_parts.append(query.replace("+", " "))
    if storage_gb:
        q_parts.append("1 ТБ" if storage_gb == 1000 else str(storage_gb))

    params = {}
    if q_parts:
        params["q"] = " ".join(q_parts)
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
