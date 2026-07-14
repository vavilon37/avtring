import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from aiogram import Bot
from aiogram.filters import Command
from aiogram.types import Message

import database as db
from database import FREE_INTERVAL, PAID_INTERVAL, OWNER_ID, mark_listings_seen
from parser import AvitoParser, BLOCK_COOLDOWNS, PROFILE_DIRS
from bot import send_listing
from listing_filter import filter_listings, filter_after_detail, listing_datetime, storage_matches

MAX_DETAIL_FETCH = 1    # max detail pages per cycle (usually 0 when XHR data present)
DETAIL_CONCURRENCY = 1  # sequential detail fetches — less suspicious
MIN_PAID_URL_INTERVAL = 10.0  # seconds between re-checks of the same URL

logger = logging.getLogger(__name__)


class Monitor:
    def __init__(self, bot: Bot):
        self.bot = bot
        # Один парсер (полный пул профилей). Монитор и _tick универсальны по
        # числу парсеров: чтобы снова распараллелить — добавь второй элемент
        # с непересекающимся пулом, напр. profiles=PROFILE_DIRS[3:].
        self._parsers = [
            AvitoParser(on_blocked=self._notify_blocked),
        ]
        self._parsers_started = [False] * len(self._parsers)
        self._running = False
        self._paused = False  # парсер остановлен админом из панели
        self._task: asyncio.Task | None = None
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

    @property
    def _delay_mult(self) -> float:
        if self._blocks_today >= 5:
            return 2.0
        if self._blocks_today >= 2:
            return 1.5
        return 1.0

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
        if not self._parsers_started[0]:
            return []
        try:
            listings = await asyncio.wait_for(
                self._parsers[0].fetch_listings(url), timeout=30.0
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
                f"👥 Пользователей: <b>{stats['total_users']}</b>\n"
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
        for i, parser in enumerate(self._parsers):
            if self._parsers_started[i]:
                try:
                    await asyncio.wait_for(parser.stop(), timeout=15)
                except asyncio.TimeoutError:
                    logger.warning(f"Parser {i} did not stop within 15s — forcing exit")
                self._parsers_started[i] = False
        logger.info("Monitor stopped")

    async def pause_parser(self):
        """Остановить парсер по команде админа — цикл не будет проверять Авито."""
        self._paused = True
        for i, parser in enumerate(self._parsers):
            if self._parsers_started[i]:
                try:
                    await asyncio.wait_for(parser.stop(), timeout=15)
                except asyncio.TimeoutError:
                    logger.warning(f"Parser {i} did not stop within 15s")
                self._parsers_started[i] = False
        logger.info("Parser paused by admin")

    async def resume_parser(self):
        """Возобновить парсер — цикл сам перезапустит парсеры на ближайшем тике."""
        self._paused = False
        logger.info("Parser resumed by admin")

    async def _ensure_parsers(self, has_watches: bool):
        for i, parser in enumerate(self._parsers):
            if has_watches and not self._parsers_started[i]:
                await parser.start()
                self._parsers_started[i] = True
            elif not has_watches and self._parsers_started[i]:
                await parser.stop()
                self._parsers_started[i] = False

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

            if self._last_break == 0:
                self._last_break = real_now

            if self._paused:
                await asyncio.sleep(5)
                continue

            try:
                found_any = await self._tick()
                self._consecutive_failures = 0
            except Exception as e:
                logger.error(f"Monitor tick crashed: {e}", exc_info=True)
                found_any = False
                self._consecutive_failures += 1
                if self._consecutive_failures >= 3:
                    logger.warning("3 consecutive failures — restarting parsers")
                    for i, parser in enumerate(self._parsers):
                        try:
                            await parser.stop()
                            await parser.start()
                            self._parsers_started[i] = True
                        except Exception as re:
                            logger.error(f"Parser {i} restart failed: {re}")
                    self._consecutive_failures = 0

            if found_any:
                no_result_streak = 0
                delay = random.uniform(2, 5)
            else:
                # Don't grow streak while Avito cooldown is active — parser skips anyway
                parser_blocked = any(time.time() < p._blocked_until for p in self._parsers)
                if not parser_blocked:
                    no_result_streak = min(no_result_streak + 1, 5)
                base = min(5 * (1.4 ** no_result_streak), 12)
                delay = random.uniform(base * 0.7, base * 1.3)

            logger.debug(f"Next check in {delay:.1f}s (streak={no_result_streak})")
            await asyncio.sleep(delay)

    async def _tick(self) -> bool:
        logger.info("Monitor tick")
        self._tick_sent.clear()
        watches = await db.get_all_watches()
        await self._ensure_parsers(bool(watches))

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
            if plan == "paid":
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
                f"→ {len(url_groups)} unique URLs on {len(self._parsers)} parsers"
            )
            # Раскидываем URL по парсерам (каждый — свой профиль Chrome) и гоняем
            # бакеты параллельно; внутри бакета — последовательно с паузой.
            buckets: list[list] = [[] for _ in self._parsers]
            bi = 0
            for url, watchers in shuffled:
                last_checked = self._url_last_checked.get(url, 0)
                if now - last_checked < MIN_PAID_URL_INTERVAL:
                    logger.debug(f"Skip {url[:50]} — checked {now - last_checked:.0f}s ago")
                    continue
                self._url_last_checked[url] = now
                buckets[bi % len(self._parsers)].append((url, watchers))
                bi += 1

            async def _run_bucket(parser, bucket):
                found = False
                for j, (url, watchers) in enumerate(bucket):
                    if j > 0:
                        await asyncio.sleep(random.uniform(1.0, 3.0) * self._delay_mult)
                    if await self._check_url_group(url, watchers, parser):
                        found = True
                return found

            results = await asyncio.gather(
                *(_run_bucket(p, b) for p, b in zip(self._parsers, buckets) if b)
            )
            if any(results):
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
                        await asyncio.sleep(random.uniform(2.0, 5.0) * self._delay_mult)
                    processed += 1
                    if await self._check_url_group(url, watchers, self._parsers[0]):
                        found_any = True

        return found_any

    async def _check_url_group(self, url: str, watchers: list[dict], parser: AvitoParser) -> bool:
        try:
            listings = await parser.fetch_listings(url)
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
                if not watch.get("initialized"):
                    # First time ever for this watch — silence its backlog once.
                    backlog = await db.filter_new_listings(watch["id"], listings, mark=False)
                    if backlog:
                        await mark_listings_seen(watch["id"], [l["id"] for l in backlog])
                        logger.info(
                            f"[init] watch {watch['id']}: silenced {len(backlog)} backlog listings"
                        )
                    await db.mark_watch_initialized(watch["id"])
                    per_watch[watch["id"]] = []
                    continue

                new = await db.filter_new_listings(watch["id"], listings, mark=False)
                new = filter_listings(new)
                if len(new) > 15:
                    logger.warning(
                        f"[many-new] watch {watch['id']}: {len(new)} new listings in one cycle "
                        f"(restart catch-up or seen_listings desync)"
                    )
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
                    # Skip detail fetch entirely for confirmed companies — filtered anyway
                    if lst.get("seller_type") == "Компания":
                        return lst
                    # Sleep only when list page gave no seller info at all;
                    # if seller_name is known, Playwright list load already acts as delay
                    if not lst.get("seller_name"):
                        await asyncio.sleep(random.uniform(1.5, 3.0) * self._delay_mult)
                    try:
                        enriched = await parser.fetch_listing_detail(lst)
                        # Only skip if HTTP fetch itself failed (not if parsing gave partial data)
                        if enriched.get("_fetch_failed"):
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
                enriched_listings = [
                    detailed_by_id.get(l["id"], l) for l in per_watch[watch["id"]]
                ]
                watch_listings = filter_after_detail(enriched_listings)

                # Mark reseller-rejected listings as seen so they aren't re-fetched next cycle
                passed_ids = {l["id"] for l in watch_listings}
                rejected = [l for l in enriched_listings
                            if l["id"] not in passed_ids and not l.get("_detail_failed")]
                if rejected:
                    await mark_listings_seen(watch["id"], [l["id"] for l in rejected])

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

                    user_sent = self._tick_sent.setdefault(watch["user_id"], set())
                    if listing["id"] in user_sent:
                        # Уже отправлено этому юзеру в этом цикле через другой поиск.
                        # Помечаем виденным и для текущего поиска, иначе на следующем
                        # цикле он распознает объявление как новое и пришлёт дубль.
                        await mark_listings_seen(watch["id"], [listing["id"]])
                        continue
                    ok, fid = await send_listing(self.bot, watch["user_id"], listing, label)
                    if not ok:
                        # Не помечаем seen — уйдёт повторно на следующем цикле.
                        # (mark только ПОСЛЕ успешной доставки: падение процесса
                        # между отправкой и mark даст максимум дубль, а не потерю.)
                        logger.warning(f"[send-fail] {listing.get('id')} → retry next cycle")
                        continue
                    await mark_listings_seen(watch["id"], [listing["id"]])
                    user_sent.add(listing["id"])
                    # Журнал реально присланного байеру — основа атрибуции 5% + автофото.
                    try:
                        await db.log_sent_item(listing, watch["user_id"], watch["id"], photo_file_id=fid)
                    except Exception as e:
                        logger.warning(f"log_sent_item failed for {listing.get('id')}: {e}")
                    self._sent_today += 1
                    await asyncio.sleep(0.4)

                if watch_listings:
                    found_any = True

            return found_any

        except Exception as e:
            logger.error(f"URL group error ({url[:60]}): {e}")
            return False
