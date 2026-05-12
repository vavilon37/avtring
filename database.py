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
        await db.commit()
    logger.info("Database initialized")


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
