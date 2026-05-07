import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from aiogram import Bot
from aiogram.filters import Command
from aiogram.types import Message

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
        self._start_time: float = 0.0
        self._blocks_today: int = 0
        self._sent_today: int = 0
        self._stats_date: str = ""
        self._tick_sent: dict[int, set] = {}  # user_id -> listing_ids sent this tick

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

    async def _notify_recovered(self):
        try:
            await self.bot.send_message(
                chat_id=OWNER_ID,
                text="✅ <b>Парсер восстановился</b>\n\nАвито снова доступен, мониторинг продолжается.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Failed to send recovery notification: {e}")

    async def _notify_blocked(self):
        if self._block_alerted:
            return
        self._block_alerted = True
        self._blocks_today += 1
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

    async def fetch_preview(self, url: str, limit: int = 2) -> list[dict]:
        if not self._parser_started:
            return []
        try:
            listings = await asyncio.wait_for(
                self._parser.fetch_listings(url), timeout=30.0
            )
            return filter_listings(listings)[:limit]
        except Exception as e:
            logger.warning(f"Preview fetch failed: {e}")
            return []

    def register_handlers(self, dp):
        monitor = self

        async def _cmd_stats(msg: Message):
            if msg.from_user.id != OWNER_ID:
                return
            stats = await db.get_stats()
            uptime_sec = int(time.time() - monitor._start_time)
            h, rem = divmod(uptime_sec, 3600)
            m, s = divmod(rem, 60)
            await msg.answer(
                "📊 <b>Статистика</b>\n\n"
                f"👥 Пользователей: <b>{stats['total_users']}</b> "
                f"(с подпиской: <b>{stats['paid_users']}</b>)\n"
                f"🔍 Активных поисков: <b>{stats['active_watches']}</b>\n"
                f"📨 Отправлено сегодня: <b>{monitor._sent_today}</b>\n"
                f"🔎 Найдено сегодня (включая фильтр): <b>{stats['seen_today']}</b>\n"
                f"🚫 Блокировок сегодня: <b>{monitor._blocks_today}</b>\n"
                f"⏱ Аптайм: <b>{h}ч {m}м {s}с</b>",
                parse_mode="HTML",
            )

        dp.message.register(_cmd_stats, Command("stats"))

    async def start(self):
        self._start_time = time.time()
        self._stats_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
            try:
                await asyncio.wait_for(self._parser.stop(), timeout=15)
            except asyncio.TimeoutError:
                logger.warning("Parser did not stop within 15s — forcing exit")
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

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != self._stats_date:
                self._stats_date = today
                self._blocks_today = 0
                self._sent_today = 0

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
        self._tick_sent.clear()
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
            url_groups: dict[str, list] = {}
            for w in paid_watches:
                url_groups.setdefault(w["url"], []).append(w)
            logger.info(
                f"Checking {len(paid_watches)} paid watches "
                f"→ {len(url_groups)} unique URLs"
            )
            for i, (url, watchers) in enumerate(url_groups.items()):
                if i > 0:
                    await asyncio.sleep(random.uniform(2.0, 5.0))
                if await self._check_url_group(url, watchers):
                    found_any = True

        if free_watches:
            last = getattr(self, "_last_free_check", 0)
            if now - last >= FREE_INTERVAL:
                self._last_free_check = now
                url_groups = {}
                for w in free_watches:
                    url_groups.setdefault(w["url"], []).append(w)
                logger.info(
                    f"Checking {len(free_watches)} free watches "
                    f"→ {len(url_groups)} unique URLs"
                )
                for i, (url, watchers) in enumerate(url_groups.items()):
                    if i > 0:
                        await asyncio.sleep(random.uniform(2.0, 5.0))
                    if await self._check_url_group(url, watchers):
                        found_any = True

        return found_any

    async def _check_url_group(self, url: str, watchers: list[dict]) -> bool:
        try:
            listings = await self._parser.fetch_listings(url)
            if listings and self._block_alerted:
                self._block_alerted = False
                await self._notify_recovered()
            elif listings:
                self._block_alerted = False

            if not listings:
                return False

            # Per-watch: find new listings and pre-filter; deduplicate for detail fetch
            per_watch: dict[int, list[dict]] = {}
            to_enrich: dict[str, dict] = {}  # listing_id -> listing (unique across watches)

            for watch in watchers:
                new = await db.filter_new_listings(watch["id"], listings)
                new = filter_listings(new)
                new.sort(key=listing_datetime, reverse=True)
                top = new[:MAX_DETAIL_FETCH]
                per_watch[watch["id"]] = top
                for lst in top:
                    to_enrich.setdefault(lst["id"], lst)

            if not to_enrich:
                return False

            logger.info(
                f"URL group ({len(watchers)} watchers): "
                f"{len(to_enrich)} unique listings to enrich"
            )

            # Fetch each detail page once, even if multiple watchers need it
            sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

            async def _enrich(lst):
                async with sem:
                    try:
                        return await self._parser.fetch_listing_detail(lst)
                    except Exception as e:
                        logger.warning(f"Detail fetch failed {lst.get('id')}: {e}")
                        return lst

            detailed = await asyncio.gather(*[_enrich(l) for l in to_enrich.values()])
            detailed_by_id = {l["id"]: l for l in detailed}

            found_any = False
            for watch in watchers:
                label = watch["label"] or f"Поиск #{watch['id']}"
                watch_listings = [
                    detailed_by_id.get(l["id"], l) for l in per_watch[watch["id"]]
                ]
                watch_listings = filter_after_detail(watch_listings)

                for listing in watch_listings:
                    if not listing.get("params"):
                        self._empty_params_streak += 1
                        if (self._empty_params_streak >= EMPTY_PARAMS_THRESHOLD
                                and not self._empty_params_alerted):
                            self._empty_params_alerted = True
                            await self._notify_empty_params()
                    else:
                        self._empty_params_streak = 0
                        self._empty_params_alerted = False

                    user_sent = self._tick_sent.setdefault(watch["user_id"], set())
                    if listing["id"] in user_sent:
                        continue
                    user_sent.add(listing["id"])
                    await send_listing(self.bot, watch["user_id"], listing, label)
                    self._sent_today += 1
                    await asyncio.sleep(0.4)

                if watch_listings:
                    found_any = True

            return found_any

        except Exception as e:
            logger.error(f"URL group error ({url[:60]}): {e}")
            return False
