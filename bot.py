import html
import logging
import httpx
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    LinkPreviewOptions,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import database as db
from database import OWNER_ID, OWNER_IDS
from database import pause_user, resume_user, is_user_paused
from filters import (
    PHONE_MODELS, CONDITIONS, SELLER_TYPES, CITIES, STORAGE_OPTIONS,
    build_avito_url, label_from_filters,
)
from admin import BTN_ADMIN

logger = logging.getLogger(__name__)

_monitor = None

def set_monitor(monitor) -> None:
    global _monitor
    _monitor = monitor


BTN_ADD  = "➕ Добавить поиск"
BTN_LIST = "📋 Мои поиски"
BTN_STOP = "🗑 Удалить поиск"
BTN_HELP = "❓ Помощь"


# ── FSM ──────────────────────────────────────────────────────────────────────

class AddWatch(StatesGroup):
    model     = State()
    storage   = State()
    price_min = State()
    price_max = State()
    condition = State()
    seller    = State()
    city      = State()


# ── Admin-only lock (temporary) ──────────────────────────────────────────────

class AdminOnlyMiddleware(BaseMiddleware):
    """Доступ только у владельца, реселлеров и байеров из реестра."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is not None and user.id != OWNER_ID:
            if not (await db.is_reseller(user.id) or await db.is_buyer(user.id)):
                if isinstance(event, CallbackQuery):
                    await event.answer("🚧 Бот временно недоступен", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("🚧 Бот временно недоступен. Загляни позже.")
                return None
        return await handler(event, data)


# ── Bot factory ──────────────────────────────────────────────────────────────


def make_bot(token: str, session=None) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=token, session=session)
    dp  = Dispatcher(storage=MemoryStorage())
    _register_handlers(dp)
    return bot, dp


def _register_handlers(dp: Dispatcher):
    # Временная блокировка: пускаем в бот только администратора.
    dp.message.outer_middleware(AdminOnlyMiddleware())
    dp.callback_query.outer_middleware(AdminOnlyMiddleware())

    dp.message.register(_cmd_start, CommandStart())
    dp.message.register(_cmd_help,  Command("help"))
    dp.message.register(_cmd_add,   Command("add"))
    dp.message.register(_cmd_list,  Command("list"))
    dp.message.register(_cmd_stop,  Command("stop"))
    dp.message.register(_cmd_pause,  Command("pause"))
    dp.message.register(_cmd_resume, Command("resume"))

    dp.message.register(_cmd_add,  F.text == BTN_ADD)
    dp.message.register(_cmd_list, F.text == BTN_LIST)
    dp.message.register(_cmd_stop, F.text == BTN_STOP)
    dp.message.register(_cmd_help, F.text == BTN_HELP)

    # FSM
    dp.callback_query.register(_cb_model_select, F.data.startswith("mt:"),    AddWatch.model)
    dp.callback_query.register(_cb_storage,      F.data.startswith("stor:"),  AddWatch.storage)
    dp.message.register(_handle_price_min, AddWatch.price_min)
    dp.message.register(_handle_price_max, AddWatch.price_max)
    dp.callback_query.register(_cb_condition, F.data.startswith("cond:"),   AddWatch.condition)
    dp.callback_query.register(_cb_seller,    F.data.startswith("seller:"), AddWatch.seller)
    dp.callback_query.register(_cb_city,      F.data.startswith("city:"),   AddWatch.city)

    dp.callback_query.register(_cb_delete,      F.data.startswith("del:"))


# ── Keyboards ────────────────────────────────────────────────────────────────

def _main_menu(user_id: int) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text=BTN_ADD),  KeyboardButton(text=BTN_LIST)],
        [KeyboardButton(text=BTN_STOP), KeyboardButton(text=BTN_HELP)],
    ]
    if user_id == OWNER_ID:
        keyboard.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        persistent=True,
    )


def _kb_models() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"mt:{q}")]
        for name, q in PHONE_MODELS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _kb_storage() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=label, callback_data=f"stor:{gb}")]
        for label, gb in STORAGE_OPTIONS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _kb_conditions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"cond:{val}")]
        for name, val in CONDITIONS.items()
    ])


def _kb_sellers() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"seller:{val}")]
        for name, val in SELLER_TYPES.items()
    ])


def _kb_cities() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"city:{val}")]
        for name, val in CITIES.items()
    ])


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _max_watches(user_id: int) -> int:
    if user_id == OWNER_ID:
        return 999
    if user_id == 6833886572:
        return 8  # Иван — расширенный лимит
    if await db.is_buyer(user_id):
        return 2  # байер — до 2 поисков
    return 0


# ── Commands ──────────────────────────────────────────────────────────────────

async def _cmd_start(msg: Message):
    await db.ensure_user(msg.from_user.id)

    # Реселлер (не владелец) видит своё меню учёта сделок, а не байерский экран.
    if msg.from_user.id != OWNER_ID and await db.is_reseller(msg.from_user.id):
        from deals import reseller_menu, RESELLER_WELCOME
        await msg.answer(RESELLER_WELCOME, parse_mode="HTML", reply_markup=reseller_menu())
        return

    welcome = (
        "📱 <b>Мониторинг Авито</b>\n\n"
        "Слежу за новыми объявлениями о продаже телефонов и сразу присылаю "
        "карточку с фото и характеристиками.\n\n"
        "Нажми <b>➕ Добавить поиск</b>, чтобы начать."
    )
    await msg.answer(welcome, parse_mode="HTML", reply_markup=_main_menu(msg.from_user.id))


async def _cmd_help(msg: Message):
    await msg.answer(
        "<b>Как работает:</b>\n\n"
        "1. Нажми <b>➕ Добавить поиск</b>\n"
        "2. Выбери модели, цену, состояние, продавца и город\n"
        "3. Бот мониторит Авито и присылает новые объявления\n"
        "4. При новом объявлении — сразу пришлю карточку с фото, "
        "характеристиками, описанием и ценой\n\n"
        "Удалить поиск — <b>🗑 Удалить поиск</b>.",
        parse_mode="HTML",
        reply_markup=_main_menu(msg.from_user.id),
    )


# ── Add watch FSM ─────────────────────────────────────────────────────────────

async def _cmd_add(msg: Message, state: FSMContext):
    await db.ensure_user(msg.from_user.id)

    # Поиски создают только владелец и байеры. Реселлер — только учёт сделок.
    if msg.from_user.id != OWNER_ID and not await db.is_buyer(msg.from_user.id):
        await msg.answer("🧾 У тебя только учёт сделок — поиски не создаются.")
        return

    watches = await db.get_user_watches(msg.from_user.id)
    max_w = await _max_watches(msg.from_user.id)

    if len(watches) >= max_w:
        await msg.answer(
            f"❌ Максимум {max_w} поисков. Удали старый.",
            reply_markup=_main_menu(msg.from_user.id),
        )
        return

    await state.set_state(AddWatch.model)
    await msg.answer(
        "📱 <b>Шаг 1/7 — Модель</b>\n\nНажми на модель чтобы выбрать её:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    await msg.answer("👇", reply_markup=_kb_models())


async def _cb_model_select(cb: CallbackQuery, state: FSMContext):
    q = cb.data.split(":", 1)[1]
    await state.update_data(query=q)
    try:
        await cb.message.edit_reply_markup()
    except Exception:
        pass
    await cb.message.answer(
        "💾 <b>Шаг 2/7 — Объём памяти</b>",
        parse_mode="HTML",
        reply_markup=_kb_storage(),
    )
    await state.set_state(AddWatch.storage)
    await cb.answer()


async def _cb_storage(cb: CallbackQuery, state: FSMContext):
    gb = int(cb.data.split(":", 1)[1])
    await state.update_data(storage_gb=gb)
    try:
        await cb.message.edit_reply_markup()
    except Exception:
        pass
    await cb.message.answer(
        "💰 <b>Шаг 3/7 — Минимальная цена (₽)</b>\n\n"
        "Введи число или <b>0</b> чтобы пропустить:",
        parse_mode="HTML",
    )
    await state.set_state(AddWatch.price_min)
    await cb.answer()


async def _handle_price_min(msg: Message, state: FSMContext):
    if not msg.text:
        await msg.answer("Введи число цифрами — или 0, чтобы пропустить.")
        return
    val = msg.text.strip().replace(" ", "")
    pmin = val if val.isdigit() and int(val) > 0 else ""
    await state.update_data(pmin=pmin)
    await msg.answer(
        "💰 <b>Шаг 4/7 — Максимальная цена (₽)</b>\n\n"
        "Введи число или <b>0</b> чтобы пропустить:",
        parse_mode="HTML",
    )
    await state.set_state(AddWatch.price_max)


async def _handle_price_max(msg: Message, state: FSMContext):
    if not msg.text:
        await msg.answer("Введи число цифрами — или 0, чтобы пропустить.")
        return
    val = msg.text.strip().replace(" ", "")
    pmax = val if val.isdigit() and int(val) > 0 else ""
    await state.update_data(pmax=pmax)
    await msg.answer(
        "📦 <b>Шаг 5/7 — Состояние</b>",
        parse_mode="HTML",
        reply_markup=_kb_conditions(),
    )
    await state.set_state(AddWatch.condition)


async def _cb_condition(cb: CallbackQuery, state: FSMContext):
    val = cb.data.split(":", 1)[1]
    await state.update_data(condition=val)
    try:
        await cb.message.edit_reply_markup()
    except Exception:
        pass
    await cb.message.answer(
        "👤 <b>Шаг 6/7 — Тип продавца</b>",
        parse_mode="HTML",
        reply_markup=_kb_sellers(),
    )
    await state.set_state(AddWatch.seller)


async def _cb_seller(cb: CallbackQuery, state: FSMContext):
    val = cb.data.split(":", 1)[1]
    await state.update_data(seller_type=val)
    try:
        await cb.message.edit_reply_markup()
    except Exception:
        pass
    await cb.message.answer(
        "🏙 <b>Шаг 7/7 — Город</b>",
        parse_mode="HTML",
        reply_markup=_kb_cities(),
    )
    await state.set_state(AddWatch.city)


async def _cb_city(cb: CallbackQuery, state: FSMContext):
    city = cb.data.split(":", 1)[1]
    await state.update_data(city=city)
    data = await state.get_data()
    await state.clear()

    url = build_avito_url(data)
    label = label_from_filters(data)
    storage_gb = data.get("storage_gb", 0)
    await db.add_watch(cb.from_user.id, url, label, storage_gb)

    try:
        await cb.message.edit_reply_markup()
    except Exception:
        pass
    await cb.message.answer(
        f"✅ <b>Поиск создан!</b>\n\n"
        f"🔍 {label}\n\n"
        f"Мониторю Авито. Как появится новое объявление — сразу пришлю.",
        parse_mode="HTML",
        reply_markup=_main_menu(cb.from_user.id),
    )

    if _monitor:
        await cb.message.answer("🔍 Ищу свежие объявления для превью...")
        previews = await _monitor.fetch_preview(url)
        if previews:
            await cb.message.answer("📍 <b>Пара свежих объявлений по твоему запросу:</b>", parse_mode="HTML")
            for p in previews:
                ok, fid = await send_listing(cb.bot, cb.from_user.id, p, f"Превью · {label}")
                # Превью — тоже «присланное ботом», логируем для атрибуции + автофото.
                # Только реально доставленное: недошедшее в журнал не попадает.
                if not ok:
                    continue
                try:
                    await db.log_sent_item(p, cb.from_user.id, photo_file_id=fid)
                except Exception as e:
                    logger.warning(f"log_sent_item (preview) failed for {p.get('id')}: {e}")
        else:
            await cb.message.answer("ℹ️ Сейчас свежих объявлений нет — как появятся, сразу пришлю.")


async def _cmd_pause(msg: Message):
    await db.ensure_user(msg.from_user.id)
    if await is_user_paused(msg.from_user.id):
        await msg.answer("⏸ Мониторинг уже на паузе. Чтобы возобновить — /resume")
        return
    await pause_user(msg.from_user.id)
    await msg.answer(
        "⏸ <b>Мониторинг поставлен на паузу</b>\n\n"
        "Уведомления не будут приходить. Чтобы возобновить — /resume",
        parse_mode="HTML",
        reply_markup=_main_menu(msg.from_user.id),
    )


async def _cmd_resume(msg: Message):
    await db.ensure_user(msg.from_user.id)
    if not await is_user_paused(msg.from_user.id):
        await msg.answer("▶️ Мониторинг уже активен.")
        return
    await resume_user(msg.from_user.id)
    await msg.answer(
        "▶️ <b>Мониторинг возобновлён</b>\n\nСнова слежу за объявлениями.",
        parse_mode="HTML",
        reply_markup=_main_menu(msg.from_user.id),
    )


# ── List & Stop ───────────────────────────────────────────────────────────────

async def _cmd_list(msg: Message):
    watches = await db.get_user_watches(msg.from_user.id)
    if not watches:
        await msg.answer(
            "У тебя пока нет поисков. Нажми ➕ Добавить поиск.",
            reply_markup=_main_menu(msg.from_user.id),
        )
        return
    text = "📋 <b>Твои поиски:</b>\n\n"
    for w in watches:
        name = w["label"] or f"Поиск #{w['id']}"
        text += f"<b>#{w['id']}</b> {name}\n"
    await msg.answer(text, parse_mode="HTML", reply_markup=_main_menu(msg.from_user.id))


async def _cmd_stop(msg: Message):
    watches = await db.get_user_watches(msg.from_user.id)
    if not watches:
        await msg.answer("У тебя нет активных поисков.", reply_markup=_main_menu(msg.from_user.id))
        return
    kb_buttons = []
    for w in watches:
        name = w["label"] or f"Поиск #{w['id']}"
        kb_buttons.append([InlineKeyboardButton(text=f"❌ {name}", callback_data=f"del:{w['id']}")])
    await msg.answer(
        "Выбери поиск для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons),
    )


async def _cb_delete(cb: CallbackQuery):
    watch_id = int(cb.data.split(":")[1])
    removed = await db.remove_watch(watch_id, cb.from_user.id)
    if removed:
        await cb.message.edit_text(f"✅ Поиск #{watch_id} удалён.")
    else:
        await cb.answer("Не найдено или нет прав.", show_alert=True)


# ── Listing card ──────────────────────────────────────────────────────────────

def _esc(s) -> str:
    """Экранирует текст с Авито перед вставкой в HTML parse_mode —
    иначе '<' в заголовке/описании ломает отправку целиком."""
    return html.escape(str(s or ""))


def _build_listing_text(listing: dict, watch_label: str) -> str:
    lines = [f"🔔 <b>{_esc(watch_label)}</b>"]
    lines.append(f"📱 <b>{_esc(listing['title'])}</b> — <b>{_esc(listing['price'])}</b>")

    params: dict = listing.get("params", {})
    if params:
        lines.append("")
        for k, v in list(params.items())[:10]:
            if v and str(v).strip():
                lines.append(f"▫️ {_esc(k)}: <b>{_esc(v)}</b>")

    desc = (listing.get("description") or "").strip()
    if desc:
        short = desc[:400] + ("..." if len(desc) > 400 else "")
        lines.append(f"\n📝 {_esc(short)}")

    lines.append("")
    if listing.get("location"):
        lines.append(f"📍 {_esc(listing['location'])}")
    if listing.get("date"):
        lines.append(f"🕐 {_esc(listing['date'])}")

    seller_parts = []
    if listing.get("seller_name"):
        seller_parts.append(listing["seller_name"])
    if listing.get("seller_type"):
        seller_parts.append(listing["seller_type"])
    if seller_parts:
        lines.append(f"👤 {_esc(' · '.join(seller_parts))}")

    lines.append(f"\n<a href='{_esc(listing['link'])}'>🔗 Открыть на Авито</a>")
    return "\n".join(lines)


async def _download_image(url: str) -> bytes | None:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url, headers={"Referer": "https://www.avito.ru/"})
            if r.status_code == 200:
                return r.content
    except Exception:
        pass
    return None


async def send_listing(bot: Bot, user_id: int, listing: dict, watch_label: str) -> tuple[bool, str | None]:
    """Отправляет карточку байеру.

    Возвращает (доставлено, file_id отправленного фото или None). По флагу
    доставки monitor решает: писать в журнал атрибуции или повторить на
    следующем цикле.
    """
    text = _build_listing_text(listing, watch_label)
    images = [img for img in listing.get("images", []) if img and img.startswith("http")]
    photo_file_id = None
    delivered = False

    try:
        _no_preview = LinkPreviewOptions(is_disabled=True)
        if images:
            sent_msg = None
            try:
                sent_msg = await bot.send_photo(chat_id=user_id, photo=images[0], caption=text, parse_mode="HTML")
            except Exception:
                sent_msg = None
            if sent_msg is None:
                # Avito CDN requires Referer — Telegram doesn't send it; download and re-upload
                photo_bytes = await _download_image(images[0])
                if photo_bytes:
                    from aiogram.types import BufferedInputFile
                    sent_msg = await bot.send_photo(
                        chat_id=user_id,
                        photo=BufferedInputFile(photo_bytes, filename="photo.jpg"),
                        caption=text, parse_mode="HTML",
                    )
                else:
                    await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML",
                                           link_preview_options=_no_preview)
                    delivered = True
            if sent_msg:
                delivered = True
                if sent_msg.photo:
                    photo_file_id = sent_msg.photo[-1].file_id
        else:
            await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML",
                                   link_preview_options=_no_preview)
            delivered = True
    except Exception as e:
        logger.warning(f"Send failed for {user_id}: {e}")
        try:
            await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML",
                                   link_preview_options=LinkPreviewOptions(is_disabled=True))
            delivered = True
        except Exception as e2:
            logger.error(f"Fallback send failed for {user_id}: {e2}")
    return delivered, photo_file_id
