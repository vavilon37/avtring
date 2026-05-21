import aiosqlite
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
DB_PATH = "avito_ringer.db"

FREE_MAX_WATCHES = 1
TRIAL_MAX_WATCHES = 1
PAID_MAX_WATCHES = 3
TRIAL_DAYS = 1
FREE_INTERVAL = 300   # 5 минут
PAID_INTERVAL = 15    # 15 секунд
SUBSCRIPTION_DAYS = 5

OWNER_ID = 8501271486  # @yodealer
OWNER_IDS = {OWNER_ID}


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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                invoice_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_seen_watch ON seen_listings(watch_id)"
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


async def is_subscribed(user_id: int) -> bool:
    user = await get_user(user_id)
    if not user:
        return False
    if user["sub_expires_at"]:
        expires = datetime.fromisoformat(user["sub_expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > datetime.now(timezone.utc)
    return False


async def is_trial_active(user_id: int) -> bool:
    user = await get_user(user_id)
    if not user or not user["trial_started_at"]:
        return False
    started = datetime.fromisoformat(user["trial_started_at"])
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    bonus = user["trial_bonus_days"] if user["trial_bonus_days"] else 0
    delta = datetime.now(timezone.utc) - started
    return delta.days < (TRIAL_DAYS + bonus)


async def get_user_plan(user_id: int) -> str:
    """Returns 'paid', 'trial', or 'free'"""
    if user_id in OWNER_IDS:
        return "paid"
    if await is_subscribed(user_id):
        return "paid"
    if await is_trial_active(user_id):
        return "trial"
    return "free"


async def activate_subscription(user_id: int, days: int = SUBSCRIPTION_DAYS):
    now = datetime.now(timezone.utc)
    user = await get_user(user_id)
    if user and user["sub_expires_at"]:
        current = datetime.fromisoformat(user["sub_expires_at"])
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        expires = (current if current > now else now) + timedelta(days=days)
    else:
        expires = now + timedelta(days=days)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET sub_expires_at = ? WHERE user_id = ?",
            (expires.isoformat(), user_id),
        )
        await db.commit()
    return expires


async def save_invoice(invoice_id: str, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO invoices (invoice_id, user_id) VALUES (?, ?)",
            (invoice_id, user_id),
        )
        await db.commit()


async def get_invoice_user(invoice_id: str) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM invoices WHERE invoice_id = ?", (invoice_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


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
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            total_users = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE sub_expires_at > ?", (now_iso,)
        ) as cur:
            paid_users = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM watches") as cur:
            active_watches = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM seen_listings WHERE date(seen_at) = date('now')"
        ) as cur:
            seen_today = (await cur.fetchone())[0]
    return {
        "total_users": total_users,
        "paid_users": paid_users,
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


async def get_expiring_soon(hours: int = 24) -> list[dict]:
    now_iso = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, sub_expires_at FROM users "
            "WHERE sub_expires_at > ? AND sub_expires_at <= ?",
            (now_iso, cutoff),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_all_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT u.user_id, u.sub_expires_at, u.trial_started_at, "
            "u.trial_bonus_days, u.is_paused, COUNT(w.id) as watch_count "
            "FROM users u LEFT JOIN watches w ON u.user_id = w.user_id "
            "GROUP BY u.user_id ORDER BY u.user_id DESC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def apply_referral(new_user_id: int, referrer_id: int) -> bool:
    """
    Links new_user to referrer and gives referrer +1 trial day (1 watch limit).
    Returns True if referral was applied (only once per new user).
    """
    if new_user_id == referrer_id:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT referred_by FROM users WHERE user_id = ?", (new_user_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row or row[0] is not None:
            return False  # user doesn't exist or was already referred
        await db.execute(
            "UPDATE users SET referred_by = ? WHERE user_id = ?",
            (referrer_id, new_user_id),
        )
        await db.execute(
            "UPDATE users SET trial_bonus_days = COALESCE(trial_bonus_days, 0) + 1 WHERE user_id = ?",
            (referrer_id,),
        )
        await db.commit()
    return True


async def get_referral_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)
        ) as cur:
            return (await cur.fetchone())[0]


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
