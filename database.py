import aiosqlite
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
DB_PATH = "avito_ringer.db"

FREE_MAX_WATCHES = 1
PAID_MAX_WATCHES = 3
TRIAL_DAYS = 3
FREE_INTERVAL = 300   # 5 минут
PAID_INTERVAL = 15    # 15 секунд
SUBSCRIPTION_DAYS = 5

OWNER_ID = 8501271486  # @yodealer
OWNER_IDS = {OWNER_ID}


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
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
        await db.commit()
    logger.info("Database initialized")


async def ensure_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, trial_started_at) VALUES (?, ?)",
            (user_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


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
    delta = datetime.now(timezone.utc) - started
    return delta.days < TRIAL_DAYS


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


async def add_watch(user_id: int, url: str, label: str = "") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO watches (user_id, url, label) VALUES (?, ?, ?)",
            (user_id, url, label),
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


async def clean_old_seen_listings(days: int = 30):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM seen_listings WHERE seen_at < ?", (cutoff,)
        )
        await db.commit()
        if cursor.rowcount:
            logger.info(f"Cleaned {cursor.rowcount} old seen_listings entries")


async def filter_new_listings(watch_id: int, listings: list[dict]) -> list[dict]:
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

        if new_listings:
            await db.executemany(
                "INSERT OR IGNORE INTO seen_listings (watch_id, listing_id) VALUES (?, ?)",
                [(watch_id, l["id"]) for l in new_listings],
            )
            await db.commit()

        return new_listings
