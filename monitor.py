import asyncio
import logging
import random
from aiogram import Bot

import database as db
from database import FREE_INTERVAL, PAID_INTERVAL, OWNER_ID
from parser import AvitoParser, BLOCK_COOLDOWNS
from bot import send_listing
from listing_filter import filter_listings, filter_after_detail, listing_datetime

MAX_DETAIL_FETCH = 10   # max detail pages per cycle
DETAIL_CONCURRENCY = 2  # simultaneous browser tabs for detail pages

logger = logging.getLogger(__name__)


EMPTY_PARAMS_THRESHOLD = 5  # alert after this many consecutive listings with no params

class Monitor:
    def __init__(self, bot: Bot):
        self.bot = bot
        self._parser = AvitoParser(on_blocked=self._notify_blocked)
        self._parser_started = False
        self._running = False
        self._task: asyncio.Task | None = None
        self._empty_params_streak: int = 0
        self._empty_params_alerted: bool = False
        self._block_alerted: bool = False
        self._last_cleanup: float = 0

    async def _notify_empty_params(self):
        try:
            await self.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    "⚠️ <b>Авито изменил разметку страницы</b>\n\n"
                    f"Последние {EMPTY_PARAMS_THRESHOLD} объявлений пришли без характеристик "
                    "(экран, корпус, память и т.д.).\n\n"
                    "Скорее всего Авито обновил HTML — нужно проверить парсер."
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Failed to send empty params notification: {e}")

    async def _notify_blocked(self):
        if self._block_alerted:
            return
        self._block_alerted = True
        try:
            await self.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    "⚠️ <b>Авито заблокировал парсер</b>\n\n"
                    "Обнаружена капча или IP-блокировка.\n"
                    f"Пауза {BLOCK_COOLDOWNS[0] // 60}→{BLOCK_COOLDOWNS[1] // 60}→{BLOCK_COOLDOWNS[2] // 60} мин (нарастает), потом попробую снова.\n\n"
                    "Если не восстановится — прогрей профиль:\n"
                    "<code>python warmup.py</code>\n"
                    "→ зайди на avito.ru → реши капчу → нажми Enter в терминале.\n"
                    "Watcher перезапустит бота автоматически."
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Failed to send block notification: {e}")

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Monitor started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._parser_started:
            await self._parser.stop()
            self._parser_started = False
        logger.info("Monitor stopped")

    async def _ensure_parser(self, has_watches: bool):
        if has_watches and not self._parser_started:
            await self._parser.start()
            self._parser_started = True
        elif not has_watches and self._parser_started:
            await self._parser.stop()
            self._parser_started = False

    async def _loop(self):
        await asyncio.sleep(3)
        no_result_streak = 0
        while self._running:
            now = asyncio.get_event_loop().time()
            if now - self._last_cleanup > 86400:
                self._last_cleanup = now
                await db.clean_old_seen_listings()

            try:
                found_any = await self._tick()
            except Exception as e:
                logger.error(f"Monitor tick crashed: {e}", exc_info=True)
                found_any = False

            if found_any:
                no_result_streak = 0
                delay = random.uniform(8, 18)
            else:
                no_result_streak = min(no_result_streak + 1, 5)
                base = min(15 * (1.5 ** no_result_streak), 120)
                delay = random.uniform(base * 0.7, base * 1.3)

            logger.debug(f"Next check in {delay:.1f}s (streak={no_result_streak})")
            await asyncio.sleep(delay)

    async def _tick(self) -> bool:
        logger.info("Monitor tick")
        watches = await db.get_all_watches()
        await self._ensure_parser(bool(watches))

        if not watches:
            logger.info("No watches in DB")
            return False

        now = asyncio.get_event_loop().time()
        found_any = False

        paid_watches = []
        free_watches = []

        for watch in watches:
            plan = await db.get_user_plan(watch["user_id"])
            if plan in ("paid", "trial"):
                paid_watches.append(watch)
            else:
                free_watches.append(watch)

        if paid_watches:
            logger.info(f"Checking {len(paid_watches)} paid watches")
            sem = asyncio.Semaphore(3)

            async def check_paid(watch):
                async with sem:
                    return await self._check_watch(watch)

            results = await asyncio.gather(*[check_paid(w) for w in paid_watches], return_exceptions=True)
            if any(r is True for r in results):
                found_any = True

        if free_watches:
            last = getattr(self, "_last_free_check", 0)
            if now - last >= FREE_INTERVAL:
                self._last_free_check = now
                logger.info(f"Checking {len(free_watches)} free watches")
                sem = asyncio.Semaphore(2)

                async def check_free(watch):
                    async with sem:
                        return await self._check_watch(watch)

                results = await asyncio.gather(*[check_free(w) for w in free_watches], return_exceptions=True)
                if any(r is True for r in results):
                    found_any = True

        return found_any

    async def _check_watch(self, watch: dict) -> bool:
        watch_id = watch["id"]
        user_id = watch["user_id"]
        url = watch["url"]
        label = watch["label"] or f"Поиск #{watch_id}"

        try:
            listings = await self._parser.fetch_listings(url)
            if listings:
                self._block_alerted = False
            new_listings = await db.filter_new_listings(watch_id, listings)
            new_listings = filter_listings(new_listings)

            if not new_listings:
                logger.debug(f"Watch {watch_id}: no new after pre-filter")
                return False

            new_listings.sort(key=listing_datetime, reverse=True)
            to_enrich = new_listings[:MAX_DETAIL_FETCH]

            logger.info(f"Watch {watch_id}: {len(new_listings)} new → enriching top {len(to_enrich)}")

            sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

            async def _enrich(lst):
                async with sem:
                    try:
                        return await self._parser.fetch_listing_detail(lst)
                    except Exception as e:
                        logger.warning(f"Detail fetch failed {lst.get('id')}: {e}")
                        return lst

            detailed_listings = list(await asyncio.gather(*[_enrich(l) for l in to_enrich]))
            detailed_listings = filter_after_detail(detailed_listings)

            for listing in detailed_listings:
                # Track empty params streak to detect Avito HTML changes
                if not listing.get("params"):
                    self._empty_params_streak += 1
                    if self._empty_params_streak >= EMPTY_PARAMS_THRESHOLD and not self._empty_params_alerted:
                        self._empty_params_alerted = True
                        await self._notify_empty_params()
                else:
                    self._empty_params_streak = 0
                    self._empty_params_alerted = False

                await send_listing(self.bot, user_id, listing, label)
                await asyncio.sleep(0.4)

            return bool(detailed_listings)

        except Exception as e:
            logger.error(f"Watch {watch_id} error: {e}")
            return False
