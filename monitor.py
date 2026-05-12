import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from aiogram import Bot
from aiogram.filters import Command
from aiogram.types import Message

import database as db
from database import FREE_INTERVAL, PAID_INTERVAL, OWNER_ID, SUBSCRIPTION_DAYS, mark_listings_seen
from parser import AvitoParser, BLOCK_COOLDOWNS
from bot import send_listing
from listing_filter import filter_listings, filter_after_detail, listing_datetime, storage_matches

MAX_DETAIL_FETCH = 5    # max detail pages per cycle
DETAIL_CONCURRENCY = 1  # sequential detail fetches — less suspicious
MIN_PAID_URL_INTERVAL = 30.0  # seconds between re-checks of the same URL

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
        self._url_last_checked: dict[str, float] = {}  # url -> last check timestamp
        self._consecutive_failures: int = 0
        self._last_break: float = 0.0
        self._last_expiry_check: float = 0.0
        self._expiry_notified: set[int] = set()

    @property
    def _delay_mult(self) -> float:
        if self._blocks_today >= 5:
            return 2.0
        if self._blocks_today >= 2:
            return 1.5
        return 1.0

    async def _check_expiring_subscriptions(self):
        expiring = await db.get_expiring_soon(hours=24)
        for user in expiring:
            uid = user["user_id"]
            if uid in self._expiry_notified:
                continue
            self._expiry_notified.add(uid)
            try:
                await self.bot.send_message(
                    chat_id=uid,
                    text=(
                        "⏰ <b>Подписка заканчивается завтра</b>\n\n"
                        f"Продли чтобы не прерывать мониторинг — "
                        f"нажми 💎 Подписка в меню."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

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
            real_now = time.time()

            if now - self._last_cleanup > 86400:
                self._last_cleanup = now
                await db.clean_old_seen_listings()

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != self._stats_date:
                self._stats_date = today
                self._blocks_today = 0
                self._sent_today = 0
                self._expiry_notified.clear()

            if self._last_break == 0:
                self._last_break = real_now

            # Subscription expiry notifications (once per hour)
            if real_now - self._last_expiry_check > 3600:
                self._last_expiry_check = real_now
                try:
                    await self._check_expiring_subscriptions()
                except Exception:
                    pass

            try:
                found_any = await self._tick()
                self._consecutive_failures = 0
            except Exception as e:
                logger.error(f"Monitor tick crashed: {e}", exc_info=True)
                found_any = False
                self._consecutive_failures += 1
                if self._consecutive_failures >= 3:
                    logger.warning("3 consecutive failures — restarting parser")
                    try:
                        await self._parser.stop()
                        await self._parser.start()
                        self._parser_started = True
                        self._consecutive_failures = 0
                    except Exception as re:
                        logger.error(f"Parser restart failed: {re}")

            if found_any:
                no_result_streak = 0
                delay = random.uniform(8, 18)
            else:
                # Don't grow streak while Avito cooldown is active — parser skips anyway
                parser_blocked = time.time() < self._parser._blocked_until
                if not parser_blocked:
                    no_result_streak = min(no_result_streak + 1, 5)
                base = min(15 * (1.5 ** no_result_streak), 30)
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

        # Clean up _url_last_checked for removed watches
        active_urls = {w["url"] for w in watches}
        for url in list(self._url_last_checked):
            if url not in active_urls:
                del self._url_last_checked[url]

        paid_watches = []
        free_watches = []
        for watch in watches:
            if await db.is_user_paused(watch["user_id"]):
                continue
            plan = await db.get_user_plan(watch["user_id"])
            if plan in ("paid", "trial"):
                paid_watches.append(watch)
            else:
                free_watches.append(watch)

        if paid_watches:
            url_groups: dict[str, list] = {}
            for w in paid_watches:
                url_groups.setdefault(w["url"], []).append(w)
            shuffled = list(url_groups.items())
            random.shuffle(shuffled)
            logger.info(
                f"Checking {len(paid_watches)} paid watches "
                f"→ {len(url_groups)} unique URLs"
            )
            processed = 0
            for url, watchers in shuffled:
                last_checked = self._url_last_checked.get(url, 0)
                if now - last_checked < MIN_PAID_URL_INTERVAL:
                    logger.debug(f"Skip {url[:50]} — checked {now - last_checked:.0f}s ago")
                    continue
                if processed > 0:
                    await asyncio.sleep(random.uniform(4.0, 10.0) * self._delay_mult)
                self._url_last_checked[url] = now
                processed += 1
                if await self._check_url_group(url, watchers):
                    found_any = True

        if free_watches:
            last = getattr(self, "_last_free_check", 0)
            if now - last >= FREE_INTERVAL:
                self._last_free_check = now
                url_groups = {}
                for w in free_watches:
                    url_groups.setdefault(w["url"], []).append(w)
                shuffled = list(url_groups.items())
                random.shuffle(shuffled)
                logger.info(
                    f"Checking {len(free_watches)} free watches "
                    f"→ {len(url_groups)} unique URLs"
                )
                processed = 0
                for url, watchers in shuffled:
                    if processed > 0:
                        await asyncio.sleep(random.uniform(4.0, 10.0) * self._delay_mult)
                    processed += 1
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
                new = await db.filter_new_listings(watch["id"], listings, mark=False)
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
                    await asyncio.sleep(random.uniform(0.8, 2.0) * self._delay_mult)
                    try:
                        enriched = await self._parser.fetch_listing_detail(lst)
                        # Tag as failed if detail page returned no data
                        if not enriched.get("description") and not enriched.get("params") and not enriched.get("seller_name"):
                            enriched["_detail_failed"] = True
                        return enriched
                    except Exception as e:
                        logger.warning(f"Detail fetch failed {lst.get('id')}: {e}")
                        return {**lst, "_detail_failed": True}

            detailed = await asyncio.gather(*[_enrich(l) for l in to_enrich.values()])
            detailed_by_id = {l["id"]: l for l in detailed}

            found_any = False
            for watch in watchers:
                label = watch["label"] or f"Поиск #{watch['id']}"
                watch_listings = [
                    detailed_by_id.get(l["id"], l) for l in per_watch[watch["id"]]
                ]
                watch_listings = filter_after_detail(watch_listings)

                target_gb = watch.get("storage_gb", 0)
                if target_gb:
                    before = len(watch_listings)
                    watch_listings = [l for l in watch_listings if storage_matches(l, target_gb)]
                    skipped = before - len(watch_listings)
                    if skipped:
                        logger.info(f"[filter] storage={target_gb}GB: {skipped} excluded")

                for listing in watch_listings:
                    if listing.get("_detail_failed"):
                        logger.info(f"[skip] detail failed for {listing.get('id')} — will retry next cycle")
                        continue

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
                    await mark_listings_seen(watch["id"], [listing["id"]])
                    await send_listing(self.bot, watch["user_id"], listing, label)
                    self._sent_today += 1
                    await asyncio.sleep(0.4)

                if watch_listings:
                    found_any = True

            return found_any

        except Exception as e:
            logger.error(f"URL group error ({url[:60]}): {e}")
            return False
