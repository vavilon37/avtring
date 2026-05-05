import asyncio
import logging
from aiogram import Bot

import database as db
from parser import AvitoParser
from bot import send_listing

logger = logging.getLogger(__name__)


class Monitor:
    def __init__(self, bot: Bot, interval: int = 60):
        self.bot = bot
        self.interval = interval
        self._parser = AvitoParser()
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self):
        await self._parser.start()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"Monitor started, interval={self.interval}s")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._parser.stop()
        logger.info("Monitor stopped")

    async def _loop(self):
        # Stagger first run: wait a few seconds after startup
        await asyncio.sleep(3)
        while self._running:
            await self._tick()
            await asyncio.sleep(self.interval)

    async def _tick(self):
        watches = await db.get_all_watches()
        if not watches:
            return

        logger.info(f"Checking {len(watches)} watches")

        # Process concurrently but with a semaphore to avoid hammering Avito
        sem = asyncio.Semaphore(3)

        async def check_one(watch: dict):
            async with sem:
                await self._check_watch(watch)

        await asyncio.gather(*[check_one(w) for w in watches], return_exceptions=True)

    async def _check_watch(self, watch: dict):
        watch_id = watch["id"]
        user_id = watch["user_id"]
        url = watch["url"]
        label = watch["label"] or f"Поиск #{watch_id}"

        try:
            listings = await self._parser.fetch_listings(url)
            new_listings = await db.filter_new_listings(watch_id, listings)

            if new_listings:
                logger.info(f"Watch {watch_id}: {len(new_listings)} new listings for user {user_id}")
                for listing in new_listings:
                    await send_listing(self.bot, user_id, listing, label)
                    # Small delay between messages to avoid Telegram flood limits
                    await asyncio.sleep(0.3)
            else:
                logger.debug(f"Watch {watch_id}: no new listings")

        except Exception as e:
            logger.error(f"Watch {watch_id} error: {e}")
