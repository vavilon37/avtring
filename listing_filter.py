import re
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Accessories in title — skip these entirely
_ACCESSORY_RE = re.compile(
    r"(кабел\w*|зарядн\w*|зарядк[аи]?|чехол\w*|наушник\w*|гарнитур\w*"
    r"|держател\w*|адаптер\w*|переходник\w*|павербанк|пауэрбанк|power[\s\-]?bank"
    r"|powerbank|ремешок|запчаст\w*|дисплей\s+для|защитн\w+\s+стекл\w*"
    r"|стекл\w*\s+защитн\w*|плёнк[аи]\s+для|аксессуар\w*|usb[\s\-]hub"
    r"|коробк[аи]\s+от|только\s+коробк|корпус\s+для|аккумулятор\s+для|батаре[яи]\s+для)",
    re.IGNORECASE,
)

# Store/reseller keywords in title — strong signal
_TITLE_STORE_RE = re.compile(
    r"\b(магазин|салон|официальн|trade[\s\-]?in|трейд[\s\-]?ин|оптом|опт\b"
    r"|доставка|гарантия\s+\d|ломбард|скупк\w*)\b",
    re.IGNORECASE,
)

# Emoji unicode ranges
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000FE00-\U0000FE0F"
    "\U00002300-\U000023FF"
    "\U00002B00-\U00002BFF"
    "\U0001F004-\U0001F0CF]",
    re.UNICODE,
)

# Phrases in description that signal a store/reseller
_DESC_STORE_PHRASES = re.compile(
    r"(наш\s+магазин|наш\s+салон|доставка\s+по\s+(всей\s+)?рф|доставка\s+по\s+(всей\s+)?росси"
    r"|гарантия\s+\d+\s*(месяц|год|лет)|звоните|пишите\s+в\s+whatsapp|пишите\s+в\s+вотсап"
    r"|пишите\s+в\s+telegram|обращайтесь|наш\s+адрес|приходите\s+к\s+нам"
    r"|в\s+наличии\s+и\s+под\s+заказ|под\s+заказ|trade[\s\-]?in|трейд[\s\-]?ин"
    r"|оптовые|ломбард|мы\s+официальн|интернет[\s\-]магазин)",
    re.IGNORECASE,
)

# seller_type values that mean it's NOT a private person
_STORE_SELLER_TYPES = re.compile(
    r"(компани|магазин|дилер|официальн|салон|ломбард|бизнес)",
    re.IGNORECASE,
)

# Russian months for date parsing
_MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def _parse_avito_date(date_str: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    s = date_str.strip().lower()

    if "только что" in s:
        return now

    m = re.match(r"(\d+)\s*минут", s)
    if m:
        return now - timedelta(minutes=int(m.group(1)))

    m = re.match(r"(\d+)\s*час", s)
    if m:
        return now - timedelta(hours=int(m.group(1)))

    m = re.match(r"(\d+)\s*дн", s)  # "7 дней назад", "2 дня назад"
    if m:
        return now - timedelta(days=int(m.group(1)))

    # RSS pubDate: "Wed, 13 May 2026 21:00:00 +0300"
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_str).astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    except Exception:
        pass

    m = re.search(r"сегодня.*?(\d{1,2})[:\.](\d{2})", s)
    if m:
        t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if t > now:
            t -= timedelta(days=1)
        return t

    m = re.search(r"вчера.*?(\d{1,2})[:\.](\d{2})", s)
    if m:
        t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        t -= timedelta(days=1)
        return t

    if "вчера" in s:
        return now - timedelta(days=1)

    m = re.search(r"(\d{1,2})\s+(\w+)(?:\s+\d{4})?\s+в\s+(\d{1,2})[:\.](\d{2})", s)
    if m:
        day, month_str, hour, minute = m.group(1), m.group(2), m.group(3), m.group(4)
        month = _MONTHS_RU.get(month_str)
        if month:
            year = now.year
            try:
                t = datetime(year, month, int(day), int(hour), int(minute), tzinfo=timezone.utc)
                if t > now + timedelta(hours=1):
                    t = t.replace(year=year - 1)
                return t
            except ValueError:
                pass

    # "11 мая" or "11 мая 2025" — date without time (old listing shown on list page)
    m = re.search(r"(\d{1,2})\s+(\w+)(?:\s+(\d{4}))?", s)
    if m:
        month = _MONTHS_RU.get(m.group(2))
        if month:
            year = int(m.group(3)) if m.group(3) else now.year
            try:
                t = datetime(year, month, int(m.group(1)), 0, 0, tzinfo=timezone.utc)
                if t > now + timedelta(hours=1):
                    t = t.replace(year=year - 1)
                return t
            except ValueError:
                pass

    return None


def _reseller_score(listing: dict) -> tuple[bool, str]:
    """
    Returns (is_reseller, reason).
    Uses multiple signals — any one strong signal = reject.
    """
    title = listing.get("title", "") or ""
    desc = listing.get("description", "") or ""
    seller_type = listing.get("seller_type", "") or ""
    seller_name = listing.get("seller_name", "") or ""

    # 1. Avito explicitly marks as company/store
    if _STORE_SELLER_TYPES.search(seller_type):
        return True, f"seller_type={seller_type!r}"

    # 2. Many emojis in description — single emoji is fine for private sellers
    emoji_count = len(_EMOJI_RE.findall(desc))
    if emoji_count >= 3:
        return True, f"emoji in desc ({emoji_count})"

    # 3. Store phrases in description
    m = _DESC_STORE_PHRASES.search(desc)
    if m:
        return True, f"store phrase in desc: {m.group()!r}"

    # 4. Store keywords in title
    m = _TITLE_STORE_RE.search(title)
    if m:
        return True, f"store keyword in title: {m.group()!r}"

    # 5. Very structured description: many lines / bullet-point style
    if desc:
        lines = [l.strip() for l in desc.splitlines() if l.strip()]
        # More than 10 short lines = template/structured ad
        short_lines = [l for l in lines if len(l) < 60]
        if len(short_lines) >= 8:
            return True, f"structured desc ({len(short_lines)} short lines)"

    # 6. Seller name looks like a business
    if seller_name:
        if re.search(r"\b(ооо|ип\b|зао|пао|ltd|llc|corp)\b", seller_name, re.IGNORECASE):
            return True, f"seller_name is company: {seller_name!r}"
        if re.search(r"(маркет|market|shop\b|store\b|трейд|trade\b|ломбард|салон|студи[яи])", seller_name, re.IGNORECASE):
            return True, f"seller_name contains store keyword: {seller_name!r}"

    return False, ""


def is_accessory(listing: dict) -> bool:
    return bool(_ACCESSORY_RE.search(listing.get("title", "")))


def is_too_old(listing: dict, max_age_minutes: int = 1440) -> bool:
    date_str = listing.get("date", "")
    if not date_str:
        return False
    dt = _parse_avito_date(date_str)
    if dt is None:
        return False  # не можем определить возраст — пропускаем дальше
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60 > max_age_minutes


def listing_datetime(listing: dict) -> datetime:
    """Parse listing date for sorting. Returns epoch on failure."""
    dt = _parse_avito_date(listing.get("date", ""))
    return dt if dt else datetime.fromtimestamp(0, tz=timezone.utc)


def filter_listings(listings: list[dict]) -> list[dict]:
    """Pre-filter (list page data): accessories + age + title store keywords."""
    result = []
    unparsed_dates = []
    for lst in listings:
        title = lst.get("title", "")

        date_str = lst.get("date", "")
        if date_str and _parse_avito_date(date_str) is None:
            unparsed_dates.append(date_str)

        if is_accessory(lst):
            logger.debug(f"[filter] accessory: {title!r}")
            continue

        if is_too_old(lst):
            logger.debug(f"[filter] too old: {title!r} [{lst.get('date')}]")
            continue

        m = _TITLE_STORE_RE.search(title)
        if m:
            logger.debug(f"[filter] store title: {title!r} [{m.group()!r}]")
            continue

        result.append(lst)

    if unparsed_dates:
        logger.warning(
            f"[date] {len(unparsed_dates)} listings with unrecognized date format — "
            f"age filter could not drop them. Samples: {unparsed_dates[:3]}"
        )

    skipped = len(listings) - len(result)
    if skipped:
        logger.info(f"[filter] pre: {skipped} skipped, {len(result)} remain")
    return result


def filter_after_detail(listings: list[dict]) -> list[dict]:
    """Post-filter (detail page data): seller_type, emoji, store phrases, structure."""
    result = []
    for lst in listings:
        rejected, reason = _reseller_score(lst)
        if rejected:
            logger.info(f"[filter] reseller: {lst.get('title')!r} — {reason}")
            continue
        result.append(lst)

    skipped = len(listings) - len(result)
    if skipped:
        logger.info(f"[filter] post: {skipped} resellers removed, {len(result)} remain")
    return result


_KNOWN_GB = [64, 128, 256, 512]
_TB_RE = re.compile(r'\b1\s*(ТБ|тб|TB|tb)\b', re.IGNORECASE)


def storage_matches(listing: dict, target_gb: int) -> bool:
    """
    Returns False only when a DIFFERENT storage value is positively detected.
    When storage is absent from the listing data, returns True (don't miss real listings).
    target_gb=0 means "any" — always True.
    """
    if not target_gb:
        return True

    parts = [listing.get("title") or ""]
    for v in (listing.get("params") or {}).values():
        parts.append(str(v))
    text = " ".join(parts)

    if target_gb == 1000:
        if _TB_RE.search(text):
            return True
        for gb in _KNOWN_GB:
            if re.search(rf'\b{gb}\s*(ГБ|гб|GB|gb)', text, re.IGNORECASE):
                return False
        return True

    target_re = re.compile(rf'\b{target_gb}\s*(ГБ|гб|GB|gb)', re.IGNORECASE)
    if target_re.search(text):
        return True

    for gb in _KNOWN_GB:
        if gb != target_gb and re.search(rf'\b{gb}\s*(ГБ|гб|GB|gb)', text, re.IGNORECASE):
            return False
    if _TB_RE.search(text):
        return False

    return True  # storage not mentioned — include
