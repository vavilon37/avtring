import aiosqlite
import logging

logger = logging.getLogger(__name__)
DB_PATH = "avito_ringer.db"


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
        await db.commit()
    logger.info("Database initialized")


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
