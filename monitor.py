import asyncio
import logging
import random
from aiogram import Bot

import database as db
from database import FREE_INTERVAL, PAID_INTERVAL
from parser import AvitoParser
from bot import send_listing

logger = logging.getLogger(__name__)


class Monitor:
    def __init__(self, bot: Bot, interval: int = PAID_INTERVAL):
        self.bot = bot
        self.interval = interval
        self._parser = AvitoParser()
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self):
        await self._parser.start()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"Monitor started")

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
        await asyncio.sleep(3)
        while self._running:
            await self._tick()
            jitter = random.uniform(-3, 3)
            await asyncio.sleep(max(5, PAID_INTERVAL + jitter))

    async def _tick(self):
        watches = await db.get_all_watches()
        if not watches:
            return

        now = asyncio.get_event_loop().time()

        paid_watches = []
        free_watches = []

        for watch in watches:
            plan = await db.get_user_plan(watch["user_id"])
            if plan in ("paid", "trial"):
                paid_watches.append(watch)
            else:
                free_watches.append(watch)

        # Paid: check every tick (~15s)
        if paid_watches:
            logger.info(f"Checking {len(paid_watches)} paid watches")
            sem = asyncio.Semaphore(3)

            async def check_paid(watch):
                async with sem:
                    await self._check_watch(watch)

            await asyncio.gather(*[check_paid(w) for w in paid_watches], return_exceptions=True)

        # Free: check only if enough time passed (~5 min)
        if free_watches:
            last = getattr(self, "_last_free_check", 0)
            if now - last >= FREE_INTERVAL:
                self._last_free_check = now
                logger.info(f"Checking {len(free_watches)} free watches")
                sem = asyncio.Semaphore(2)

                async def check_free(watch):
                    async with sem:
                        await self._check_watch(watch)

                await asyncio.gather(*[check_free(w) for w in free_watches], return_exceptions=True)

    async def _check_watch(self, watch: dict):
        watch_id = watch["id"]
        user_id = watch["user_id"]
        url = watch["url"]
        label = watch["label"] or f"Поиск #{watch_id}"

        try:
            listings = await self._parser.fetch_listings(url)
            new_listings = await db.filter_new_listings(watch_id, listings)

            if not new_listings:
                logger.debug(f"Watch {watch_id}: no new listings")
                return

            logger.info(f"Watch {watch_id}: {len(new_listings)} new listings for user {user_id}")

            for listing in new_listings:
                detailed = await self._parser.fetch_listing_detail(listing)
                await send_listing(self.bot, user_id, detailed, label)
                await asyncio.sleep(0.5)

        except Exception as e:
            logger.error(f"Watch {watch_id} error: {e}")
