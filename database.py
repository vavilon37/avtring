import aiosqlite
import logging
import re
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
DB_PATH = "avito_ringer.db"

FREE_INTERVAL = 300   # 5 минут — интервал мониторинга для не-байеров
PAID_INTERVAL = 15    # 15 секунд — интервал для байеров/владельца

OWNER_ID = 8501271486  # @yodealer
OWNER_IDS = {OWNER_ID}

FEE_RATE = 0.05  # доля владельца парсера с маржи по «ботовским» сделкам


def extract_item_id(url: str) -> str | None:
    """Достаёт Avito item_id из ссылки объявления.

    Ссылка вида .../telefony/apple_iphone_13_128_gb_4567890123?context=...
    → item_id = последнее число в пути. Совпадает с listing["id"] парсера.
    """
    if not url:
        return None
    path = urlparse(url.strip()).path
    m = re.search(r"_(\d+)/?$", path)
    if m:
        return m.group(1)
    nums = re.findall(r"\d{6,}", path)
    return nums[-1] if nums else None


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                label TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS seen_listings (
                watch_id INTEGER NOT NULL,
                listing_id TEXT NOT NULL,
                seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (watch_id, listing_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                trial_started_at TIMESTAMP,
                sub_expires_at TIMESTAMP
            )
        """)
        # --- Учёт находок и атрибуция (перепродажа телефонов) ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS resellers (
                user_id INTEGER PRIMARY KEY,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sent_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id TEXT NOT NULL,
                buyer_id INTEGER NOT NULL,
                watch_id INTEGER,
                price TEXT,
                title TEXT,
                link TEXT,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT,
                url TEXT,
                buy_price INTEGER,
                sell_price INTEGER,
                status TEXT NOT NULL DEFAULT 'open',
                attributed INTEGER NOT NULL DEFAULT 0,
                buyer_hint TEXT,
                note TEXT,
                fee INTEGER,
                reseller_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sold_at TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS buyers (
                user_id INTEGER PRIMARY KEY,
                name TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS deal_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_seen_watch ON seen_listings(watch_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dealphotos ON deal_photos(deal_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sent_listing ON sent_items(listing_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_deals_status ON deals(status)"
        )
        try:
            await db.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN trial_bonus_days INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN is_paused INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE watches ADD COLUMN storage_gb INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE watches ADD COLUMN initialized INTEGER DEFAULT 0")
            # Existing watches are already silenced; mark them so a restart won't re-silence.
            await db.execute("UPDATE watches SET initialized = 1")
        except Exception:
            pass
        for ddl in (
            "ALTER TABLE deals ADD COLUMN title TEXT",
            "ALTER TABLE deals ADD COLUMN settled INTEGER DEFAULT 0",
            "ALTER TABLE deals ADD COLUMN settled_at TIMESTAMP",
            "ALTER TABLE deals ADD COLUMN buyer_id INTEGER",
        ):
            try:
                await db.execute(ddl)
            except Exception:
                pass
        try:
            await db.execute("ALTER TABLE sent_items ADD COLUMN photo_file_id TEXT")
        except Exception:
            pass
        # Сид известных участников (id даны владельцем).
        await db.execute("INSERT OR IGNORE INTO resellers (user_id) VALUES (?)", (1295870874,))
        for uid, nm in ((1963364335, "Байер 1"), (1421447029, "Байер 2")):
            await db.execute("INSERT OR IGNORE INTO buyers (user_id, name) VALUES (?, ?)", (uid, nm))
        await db.commit()
    logger.info("Database initialized")
    await _migrate_slug_urls()


async def _migrate_slug_urls():
    """Convert broken path-slug watch URLs back to text-search URLs (one-time, idempotent)."""
    from urllib.parse import urlparse, parse_qs, urlencode

    _SLUG_TO_QUERY = {
        "iphone_17_pro_max": "iPhone+17+Pro+Max", "iphone_17_pro": "iPhone+17+Pro",
        "iphone_17_plus": "iPhone+17+Plus",        "iphone_17": "iPhone+17",
        "iphone_16_pro_max": "iPhone+16+Pro+Max",  "iphone_16_pro": "iPhone+16+Pro",
        "iphone_16_plus": "iPhone+16+Plus",        "iphone_16_mini": "iPhone+16+mini",
        "iphone_16": "iPhone+16",
        "iphone_15_pro_max": "iPhone+15+Pro+Max",  "iphone_15_pro": "iPhone+15+Pro",
        "iphone_15_plus": "iPhone+15+Plus",        "iphone_15": "iPhone+15",
        "iphone_14_pro_max": "iPhone+14+Pro+Max",  "iphone_14_pro": "iPhone+14+Pro",
        "iphone_14_plus": "iPhone+14+Plus",        "iphone_14": "iPhone+14",
        "iphone_13_pro_max": "iPhone+13+Pro+Max",  "iphone_13_pro": "iPhone+13+Pro",
        "iphone_13_mini": "iPhone+13+mini",        "iphone_13": "iPhone+13",
        "iphone_12_pro_max": "iPhone+12+Pro+Max",  "iphone_12_pro": "iPhone+12+Pro",
        "iphone_12_mini": "iPhone+12+mini",        "iphone_12": "iPhone+12",
    }
    _STORAGE_SLUG = {
        "64_gb": 64, "128_gb": 128, "256_gb": 256, "512_gb": 512, "1_tb": 1000,
    }

    def _fix(url: str):
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 3 or parts[1] != "telefony" or parts[2] != "mobilnye_telefony":
            return None
        city         = parts[0]
        model_slug   = parts[4] if len(parts) > 4 else ""
        storage_slug = parts[5] if len(parts) > 5 else ""
        qp           = parse_qs(parsed.query)
        query   = _SLUG_TO_QUERY.get(model_slug, "")
        stor_gb = _STORAGE_SLUG.get(storage_slug, 0)
        pmin    = qp.get("pmin",        [""])[0]
        pmax    = qp.get("pmax",        [""])[0]
        cnd     = qp.get("cnd",         [""])[0]
        sel_raw = qp.get("seller_type", [""])[0]
        seller  = "private" if sel_raw == "private" else ("shop" if sel_raw == "shop" else "")
        q_parts = []
        if query:
            q_parts.append(query.replace("+", " "))
        if stor_gb:
            q_parts.append("1 ТБ" if stor_gb == 1000 else str(stor_gb))
        params: dict = {}
        if q_parts:
            params["q"] = " ".join(q_parts)
        if pmin: params["pmin"] = pmin
        if pmax: params["pmax"] = pmax
        if cnd:  params["cnd"]  = cnd
        if seller == "private": params["seller_type"] = "private"
        elif seller == "shop":  params["seller_type"] = "shop"
        params["sort"] = "date"
        params["s"]    = "104"
        return f"https://www.avito.ru/{city}/telefony?{urlencode(params, encoding='utf-8')}"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, url FROM watches") as cur:
            watches = await cur.fetchall()
        fixed = 0
        for w in watches:
            new_url = _fix(w["url"])
            if new_url:
                await db.execute("UPDATE watches SET url = ? WHERE id = ?", (new_url, w["id"]))
                logger.info(f"Migrated watch #{w['id']}: {w['url'][:60]} → {new_url[:60]}")
                fixed += 1
        if fixed:
            await db.commit()
            logger.info(f"URL migration: fixed {fixed} watch(es)")


async def ensure_user(user_id: int) -> bool:
    """Returns True if user was just created (first start)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO users (user_id, trial_started_at) VALUES (?, ?)",
            (user_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_user_plan(user_id: int) -> str:
    """'paid' для владельца и байеров (быстрый интервал мониторинга), иначе 'free'."""
    if user_id in OWNER_IDS or await is_buyer(user_id):
        return "paid"
    return "free"


async def add_watch(user_id: int, url: str, label: str = "", storage_gb: int = 0) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO watches (user_id, url, label, storage_gb) VALUES (?, ?, ?, ?)",
            (user_id, url, label, storage_gb),
        )
        await db.commit()
        return cursor.lastrowid


async def mark_watch_initialized(watch_id: int) -> None:
    """Mark a watch as initialized so its backlog is silenced only once, ever."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE watches SET initialized = 1 WHERE id = ?", (watch_id,))
        await db.commit()


async def remove_watch(watch_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM watches WHERE id = ? AND user_id = ?",
            (watch_id, user_id),
        )
        await db.execute(
            "DELETE FROM seen_listings WHERE watch_id = ?",
            (watch_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def remove_watch_by_id(watch_id: int) -> bool:
    """Админское удаление поиска без проверки владельца."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        await db.execute("DELETE FROM seen_listings WHERE watch_id = ?", (watch_id,))
        await db.commit()
        return cursor.rowcount > 0


async def get_user_watches(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM watches WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_all_watches() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM watches") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            total_users = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM watches") as cur:
            active_watches = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM seen_listings WHERE date(seen_at) = date('now')"
        ) as cur:
            seen_today = (await cur.fetchone())[0]
    return {
        "total_users": total_users,
        "active_watches": active_watches,
        "seen_today": seen_today,
    }


async def clean_old_seen_listings(days: int = 30):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM seen_listings WHERE seen_at < ?", (cutoff,)
        )
        await db.commit()
        if cursor.rowcount:
            logger.info(f"Cleaned {cursor.rowcount} old seen_listings entries")


async def clear_seen_cache() -> int:
    """Полностью очищает кэш виденных объявлений и переинициализирует поиски.

    Спама не будет: на следующем цикле бэклог каждого поиска снова молча
    пометится как виденный (см. ветку `initialized` в monitor._check_url_group).
    Возвращает число удалённых записей.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM seen_listings")
        await db.execute("UPDATE watches SET initialized = 0")
        await db.commit()
        logger.info(f"Seen cache cleared: {cursor.rowcount} entries removed")
        return cursor.rowcount


async def backup_database() -> str:
    """Делает живую консистентную копию БД и возвращает путь к файлу."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = f"/tmp/avito_ringer_backup_{ts}.db"
    async with aiosqlite.connect(DB_PATH) as src, aiosqlite.connect(backup_path) as dst:
        await src.backup(dst)
    return backup_path


async def pause_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_paused = 1 WHERE user_id = ?", (user_id,))
        await db.commit()


async def resume_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_paused = 0 WHERE user_id = ?", (user_id,))
        await db.commit()


async def is_user_paused(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_paused FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return bool(row and row[0])


async def get_all_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT u.user_id, u.is_paused, COUNT(w.id) as watch_count "
            "FROM users u LEFT JOIN watches w ON u.user_id = w.user_id "
            "GROUP BY u.user_id ORDER BY u.user_id DESC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def filter_new_listings(watch_id: int, listings: list[dict], mark: bool = True) -> list[dict]:
    if not listings:
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        listing_ids = [l["id"] for l in listings]
        placeholders = ",".join("?" * len(listing_ids))
        async with db.execute(
            f"SELECT listing_id FROM seen_listings WHERE watch_id = ? AND listing_id IN ({placeholders})",
            [watch_id, *listing_ids],
        ) as cursor:
            seen = {row[0] for row in await cursor.fetchall()}

        new_listings = [l for l in listings if l["id"] not in seen]

        if mark and new_listings:
            await db.executemany(
                "INSERT OR IGNORE INTO seen_listings (watch_id, listing_id) VALUES (?, ?)",
                [(watch_id, l["id"]) for l in new_listings],
            )
            await db.commit()

        return new_listings


async def mark_listings_seen(watch_id: int, listing_ids: list[str]):
    if not listing_ids:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT OR IGNORE INTO seen_listings (watch_id, listing_id) VALUES (?, ?)",
            [(watch_id, lid) for lid in listing_ids],
        )
        await db.commit()


# ── Роль реселлера ──────────────────────────────────────────────────────────

async def add_reseller(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO resellers (user_id) VALUES (?)", (user_id,))
        await db.commit()


async def remove_reseller(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM resellers WHERE user_id = ?", (user_id,))
        await db.commit()
        return cur.rowcount > 0


async def is_reseller(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM resellers WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone() is not None


async def get_resellers() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM resellers") as cur:
            return [r[0] for r in await cur.fetchall()]


# ── Журнал присланного байерам (основа атрибуции) ───────────────────────────

async def log_sent_item(listing: dict, buyer_id: int, watch_id: int | None = None,
                        photo_file_id: str | None = None):
    """Фиксирует объявление, реально отправленное байеру. Вызывать в точке отправки.

    `photo_file_id` — Telegram file_id уже отправленного фото (для автофото сделки).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sent_items (listing_id, buyer_id, watch_id, price, title, link, photo_file_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(listing.get("id")), buyer_id, watch_id,
             listing.get("price"), listing.get("title"), listing.get("link"), photo_file_id),
        )
        await db.commit()


async def find_sent(item_id: str) -> dict | None:
    """Самая ранняя отправка этого item_id (любому байеру), либо None.

    Правило атрибуции: если item_id есть в присланном — сделка «через бота».
    """
    if not item_id:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sent_items WHERE listing_id = ? ORDER BY sent_at ASC LIMIT 1",
            (str(item_id),),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ── Сделки (закуп → продажа → 5%) ───────────────────────────────────────────

async def add_deal(url: str, buy_price: int, reseller_id: int,
                   buyer_hint: str = "", buyer_id: int | None = None, note: str = "") -> dict:
    """Заводит закуп. Атрибуция считается автоматически по журналу присланного."""
    item_id = extract_item_id(url)
    sent = await find_sent(item_id) if item_id else None
    attributed = 1 if sent else 0
    title = (sent or {}).get("title")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO deals (item_id, url, buy_price, reseller_id, buyer_hint, buyer_id, note, attributed, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, url, buy_price, reseller_id, buyer_hint, buyer_id, note, attributed, title),
        )
        await db.commit()
        deal_id = cur.lastrowid
    # Автофото: если объявление слал бот и есть его фото — цепляем к сделке.
    if sent and sent.get("photo_file_id"):
        await add_deal_photo(deal_id, sent["photo_file_id"])
    return {
        "id": deal_id, "item_id": item_id, "url": url, "buy_price": buy_price,
        "attributed": attributed, "sent": sent, "title": title,
        "buyer_hint": buyer_hint, "buyer_id": buyer_id,
    }


async def mark_deal_sold(deal_id: int, sell_price: int) -> dict | None:
    """Закрывает сделку продажей, считает маржу и 5% (только для attributed)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        deal = dict(row)
        if deal["status"] == "sold":
            return None
        margin = (sell_price or 0) - (deal["buy_price"] or 0)
        fee = round(margin * FEE_RATE) if deal["attributed"] and margin > 0 else 0
        await db.execute(
            "UPDATE deals SET sell_price = ?, status = 'sold', fee = ?, sold_at = ? WHERE id = ?",
            (sell_price, fee, datetime.now(timezone.utc).isoformat(), deal_id),
        )
        await db.commit()
    deal.update(sell_price=sell_price, status="sold", fee=fee, margin=margin)
    return deal


async def get_deal(deal_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_open_deals(reseller_id: int | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if reseller_id is not None:
            q = ("SELECT * FROM deals WHERE status = 'open' AND reseller_id = ? "
                 "ORDER BY created_at DESC")
            args: tuple = (reseller_id,)
        else:
            q = "SELECT * FROM deals WHERE status = 'open' ORDER BY created_at DESC"
            args = ()
        async with db.execute(q, args) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_owner_report(days: int | None = None) -> dict:
    """Сводка по твоим 5%: только проданные ботовские сделки."""
    where = "status = 'sold' AND attributed = 1"
    args: list = []
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        where += " AND sold_at >= ?"
        args.append(cutoff)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT COUNT(*) AS n, "
            f"COALESCE(SUM(sell_price - buy_price), 0) AS margin, "
            f"COALESCE(SUM(fee), 0) AS fee FROM deals WHERE {where}",
            args,
        ) as cur:
            agg = dict(await cur.fetchone())
        async with db.execute(
            f"SELECT * FROM deals WHERE {where} ORDER BY sold_at DESC LIMIT 20", args
        ) as cur:
            deals = [dict(r) for r in await cur.fetchall()]
    return {"count": agg["n"], "margin": agg["margin"], "fee": agg["fee"], "deals": deals}


async def delete_deal(deal_id: int, reseller_id: int | None = None) -> bool:
    """Удаляет сделку. Если задан reseller_id — только свою (для реселлера)."""
    async with aiosqlite.connect(DB_PATH) as db:
        if reseller_id is not None:
            cur = await db.execute(
                "DELETE FROM deals WHERE id = ? AND reseller_id = ?", (deal_id, reseller_id)
            )
        else:
            cur = await db.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_buyer_breakdown(reseller_id: int) -> list[dict]:
    """Разбивка сделок реселлера по байерам: кто сколько принёс."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT COALESCE(NULLIF(TRIM(buyer_hint), ''), '—') AS buyer, "
            "  COUNT(*) AS cnt, "
            "  COALESCE(SUM(CASE WHEN status='sold' THEN sell_price - buy_price END), 0) AS margin "
            "FROM deals WHERE reseller_id = ? "
            "GROUP BY buyer ORDER BY cnt DESC",
            (reseller_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── Реестр байеров ───────────────────────────────────────────────────────────

async def add_buyer(user_id: int, name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO buyers (user_id, name) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name",
            (user_id, name),
        )
        await db.commit()


async def get_buyers() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id, name FROM buyers ORDER BY name") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def buyer_name(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name FROM buyers WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def is_buyer(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM buyers WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone() is not None


# ── Фото и описание сделки ───────────────────────────────────────────────────

async def add_deal_photo(deal_id: int, file_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO deal_photos (deal_id, file_id) VALUES (?, ?)", (deal_id, file_id)
        )
        await db.commit()


async def get_deal_photos(deal_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT file_id FROM deal_photos WHERE deal_id = ? ORDER BY id", (deal_id,)
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def set_deal_note(deal_id: int, note: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE deals SET note = ? WHERE id = ?", (note, deal_id))
        await db.commit()


# ── Расчёты по долгу 5% ──────────────────────────────────────────────────────

def _fee_where(reseller_id, settled_only_unpaid):
    where = "attributed = 1 AND status = 'sold' AND fee > 0"
    args = []
    if settled_only_unpaid:
        where += " AND settled = 0"
    if reseller_id is not None:
        where += " AND reseller_id = ?"
        args.append(reseller_id)
    return where, args


async def get_outstanding_fee(reseller_id: int | None = None) -> int:
    """Непогашенный долг по 5% (ещё не подтверждён владельцем как оплаченный)."""
    where, args = _fee_where(reseller_id, settled_only_unpaid=True)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(f"SELECT COALESCE(SUM(fee), 0) FROM deals WHERE {where}", args) as cur:
            return (await cur.fetchone())[0]


async def get_total_fee(reseller_id: int | None = None) -> int:
    """Всего начислено 5% за всё время (независимо от оплаты)."""
    where, args = _fee_where(reseller_id, settled_only_unpaid=False)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(f"SELECT COALESCE(SUM(fee), 0) FROM deals WHERE {where}", args) as cur:
            return (await cur.fetchone())[0]


async def settle_debt(reseller_id: int) -> int:
    """Помечает текущий долг реселлера оплаченным. Возвращает погашенную сумму.

    Сделки не удаляются — общая статистика (get_total_fee) сохраняется.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(fee), 0) FROM deals "
            "WHERE attributed = 1 AND status = 'sold' AND fee > 0 AND settled = 0 AND reseller_id = ?",
            (reseller_id,),
        ) as cur:
            amount = (await cur.fetchone())[0]
        await db.execute(
            "UPDATE deals SET settled = 1, settled_at = ? "
            "WHERE attributed = 1 AND status = 'sold' AND settled = 0 AND reseller_id = ?",
            (datetime.now(timezone.utc).isoformat(), reseller_id),
        )
        await db.commit()
    return amount


async def get_reseller_report(reseller_id: int) -> dict:
    """Личная сводка реселлера по всем его сделкам (и ботовским, и своим)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT "
            "  COALESCE(SUM(status = 'open'), 0)  AS open_cnt, "
            "  COALESCE(SUM(status = 'sold'), 0)  AS sold_cnt, "
            "  COALESCE(SUM(CASE WHEN status='sold' THEN sell_price - buy_price END), 0) AS margin, "
            "  COALESCE(SUM(CASE WHEN status='sold' AND attributed=1 THEN 1 ELSE 0 END), 0) AS bot_sold, "
            "  COALESCE(SUM(CASE WHEN status='sold' AND attributed=1 THEN fee END), 0)     AS fee "
            "FROM deals WHERE reseller_id = ?",
            (reseller_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else {}
