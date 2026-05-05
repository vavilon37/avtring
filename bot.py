import logging
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import database as db
from filters import (
    PHONE_MODELS, CONDITIONS, SELLER_TYPES, CITIES,
    build_avito_url, label_from_filters,
)

logger = logging.getLogger(__name__)
MAX_WATCHES_PER_USER = 10

BTN_ADD  = "➕ Добавить поиск"
BTN_LIST = "📋 Мои поиски"
BTN_STOP = "🗑 Удалить поиск"
BTN_HELP = "❓ Помощь"


# ── FSM ──────────────────────────────────────────────────────────────────────

class AddWatch(StatesGroup):
    model     = State()
    price_min = State()
    price_max = State()
    condition = State()
    seller    = State()
    city      = State()


# ── Bot factory ──────────────────────────────────────────────────────────────

def make_bot(token: str) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=token)
    dp  = Dispatcher(storage=MemoryStorage())
    _register_handlers(dp)
    return bot, dp


def _register_handlers(dp: Dispatcher):
    dp.message.register(_cmd_start, CommandStart())
    dp.message.register(_cmd_help,  Command("help"))
    dp.message.register(_cmd_add,   Command("add"))
    dp.message.register(_cmd_list,  Command("list"))
    dp.message.register(_cmd_stop,  Command("stop"))

    dp.message.register(_cmd_add,  F.text == BTN_ADD)
    dp.message.register(_cmd_list, F.text == BTN_LIST)
    dp.message.register(_cmd_stop, F.text == BTN_STOP)
    dp.message.register(_cmd_help, F.text == BTN_HELP)

    # FSM — мультивыбор моделей
    dp.callback_query.register(_cb_model_toggle, F.data.startswith("mt:"),   AddWatch.model)
    dp.callback_query.register(_cb_model_done,   F.data == "model_done",     AddWatch.model)
    dp.message.register(_handle_price_min, AddWatch.price_min)
    dp.message.register(_handle_price_max, AddWatch.price_max)
    dp.callback_query.register(_cb_condition, F.data.startswith("cond:"),   AddWatch.condition)
    dp.callback_query.register(_cb_seller,    F.data.startswith("seller:"), AddWatch.seller)
    dp.callback_query.register(_cb_city,      F.data.startswith("city:"),   AddWatch.city)

    dp.callback_query.register(_cb_delete, F.data.startswith("del:"))


# ── Keyboards ────────────────────────────────────────────────────────────────

def _main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD),  KeyboardButton(text=BTN_LIST)],
            [KeyboardButton(text=BTN_STOP), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        persistent=True,
    )


def _kb_models(selected: set[str]) -> InlineKeyboardMarkup:
    buttons = []
    for name, q in PHONE_MODELS.items():
        check = "✅ " if q in selected else ""
        buttons.append([InlineKeyboardButton(
            text=f"{check}{name}",
            callback_data=f"mt:{q}",
        )])
    buttons.append([InlineKeyboardButton(
        text=f"➡️ Готово ({len(selected)} выбрано)" if selected else "➡️ Готово (все модели)",
        callback_data="model_done",
    )])
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


# ── Commands ──────────────────────────────────────────────────────────────────

async def _cmd_start(msg: Message):
    await msg.answer(
        "📱 <b>Avito Ringer</b>\n\n"
        "Слежу за новыми объявлениями о продаже телефонов на Авито "
        "и сразу присылаю карточку с фото и всеми характеристиками.\n\n"
        "До 10 поисков одновременно. Проверка каждые 15 секунд.",
        parse_mode="HTML",
        reply_markup=_main_menu(),
    )


async def _cmd_help(msg: Message):
    await msg.answer(
        "<b>Как работает:</b>\n\n"
        "1. Нажми <b>➕ Добавить поиск</b>\n"
        "2. Выбери модели, цену, состояние, продавца и город\n"
        "3. Бот мониторит Авито каждые 15 секунд\n"
        "4. При новом объявлении — сразу пришлю карточку с фото, "
        "характеристиками, описанием и ценой\n\n"
        "До 10 поисков одновременно.\n"
        "Удалить поиск — <b>🗑 Удалить поиск</b>.",
        parse_mode="HTML",
        reply_markup=_main_menu(),
    )


# ── Add watch FSM ─────────────────────────────────────────────────────────────

async def _cmd_add(msg: Message, state: FSMContext):
    watches = await db.get_user_watches(msg.from_user.id)
    if len(watches) >= MAX_WATCHES_PER_USER:
        await msg.answer(
            f"❌ Максимум {MAX_WATCHES_PER_USER} поисков. Удали старый.",
            reply_markup=_main_menu(),
        )
        return
    await state.set_state(AddWatch.model)
    await state.update_data(selected_models=[])
    await msg.answer(
        "📱 <b>Шаг 1/6 — Модели</b>\n\n"
        "Выбери одну или несколько моделей.\n"
        "Нажми ➡️ Готово когда закончишь.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    await msg.answer("👇", reply_markup=_kb_models(set()))


async def _cb_model_toggle(cb: CallbackQuery, state: FSMContext):
    q = cb.data.split(":", 1)[1]
    data = await state.get_data()
    selected: list = data.get("selected_models", [])

    if q in selected:
        selected.remove(q)
    else:
        selected.append(q)

    await state.update_data(selected_models=selected)
    await cb.message.edit_reply_markup(reply_markup=_kb_models(set(selected)))
    await cb.answer()


async def _cb_model_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected: list = data.get("selected_models", [])
    # Собираем строку запроса: несколько моделей через OR (пробел в Авито)
    query = " ".join(selected) if selected else ""
    await state.update_data(query=query)
    await cb.message.edit_reply_markup()
    await cb.message.answer(
        "💰 <b>Шаг 2/6 — Минимальная цена (₽)</b>\n\n"
        "Введи число или <b>0</b> чтобы пропустить:",
        parse_mode="HTML",
    )
    await state.set_state(AddWatch.price_min)


async def _handle_price_min(msg: Message, state: FSMContext):
    val = msg.text.strip().replace(" ", "")
    pmin = val if val.isdigit() and int(val) > 0 else ""
    await state.update_data(pmin=pmin)
    await msg.answer(
        "💰 <b>Шаг 3/6 — Максимальная цена (₽)</b>\n\n"
        "Введи число или <b>0</b> чтобы пропустить:",
        parse_mode="HTML",
    )
    await state.set_state(AddWatch.price_max)


async def _handle_price_max(msg: Message, state: FSMContext):
    val = msg.text.strip().replace(" ", "")
    pmax = val if val.isdigit() and int(val) > 0 else ""
    await state.update_data(pmax=pmax)
    await msg.answer(
        "📦 <b>Шаг 4/6 — Состояние</b>",
        parse_mode="HTML",
        reply_markup=_kb_conditions(),
    )
    await state.set_state(AddWatch.condition)


async def _cb_condition(cb: CallbackQuery, state: FSMContext):
    val = cb.data.split(":", 1)[1]
    await state.update_data(condition=val)
    await cb.message.edit_reply_markup()
    await cb.message.answer(
        "👤 <b>Шаг 5/6 — Тип продавца</b>",
        parse_mode="HTML",
        reply_markup=_kb_sellers(),
    )
    await state.set_state(AddWatch.seller)


async def _cb_seller(cb: CallbackQuery, state: FSMContext):
    val = cb.data.split(":", 1)[1]
    await state.update_data(seller_type=val)
    await cb.message.edit_reply_markup()
    await cb.message.answer(
        "🏙 <b>Шаг 6/6 — Город</b>",
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
    await db.add_watch(cb.from_user.id, url, label)

    await cb.message.edit_reply_markup()
    await cb.message.answer(
        f"✅ <b>Поиск создан!</b>\n\n"
        f"🔍 {label}\n\n"
        f"Мониторю Авито каждые 15 секунд. Как появится новое объявление — сразу пришлю.",
        parse_mode="HTML",
        reply_markup=_main_menu(),
    )


# ── List & Stop ───────────────────────────────────────────────────────────────

async def _cmd_list(msg: Message):
    watches = await db.get_user_watches(msg.from_user.id)
    if not watches:
        await msg.answer(
            "У тебя пока нет поисков. Нажми ➕ Добавить поиск.",
            reply_markup=_main_menu(),
        )
        return
    text = "📋 <b>Твои поиски:</b>\n\n"
    for w in watches:
        name = w["label"] or f"Поиск #{w['id']}"
        text += f"<b>#{w['id']}</b> {name}\n"
    await msg.answer(text, parse_mode="HTML", reply_markup=_main_menu())


async def _cmd_stop(msg: Message):
    watches = await db.get_user_watches(msg.from_user.id)
    if not watches:
        await msg.answer("У тебя нет активных поисков.", reply_markup=_main_menu())
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

def _build_listing_text(listing: dict, watch_label: str) -> str:
    lines = [f"🔔 <b>{watch_label}</b>\n"]
    lines.append(f"📱 <b>{listing['title']}</b>")
    lines.append(f"💰 <b>{listing['price']}</b>")

    params: dict = listing.get("params", {})
    if params:
        lines.append("")
        for k, v in list(params.items())[:10]:
            lines.append(f"▫️ {k}: <b>{v}</b>")

    desc = (listing.get("description") or "").strip()
    if desc:
        short = desc[:400] + ("..." if len(desc) > 400 else "")
        lines.append(f"\n📝 {short}")

    lines.append("")
    if listing.get("location"):
        lines.append(f"📍 {listing['location']}")
    if listing.get("date"):
        lines.append(f"🕐 {listing['date']}")

    seller_parts = []
    if listing.get("seller_name"):
        seller_parts.append(listing["seller_name"])
    if listing.get("seller_type"):
        seller_parts.append(listing["seller_type"])
    if seller_parts:
        lines.append(f"👤 {' · '.join(seller_parts)}")

    lines.append(f"\n<a href='{listing['link']}'>🔗 Открыть на Авито</a>")
    return "\n".join(lines)


async def send_listing(bot: Bot, user_id: int, listing: dict, watch_label: str):
    text = _build_listing_text(listing, watch_label)
    images = [img for img in listing.get("images", []) if img and img.startswith("http")]

    try:
        if len(images) >= 2:
            media = [InputMediaPhoto(media=images[0], caption=text, parse_mode="HTML")]
            for img in images[1:10]:
                media.append(InputMediaPhoto(media=img))
            await bot.send_media_group(chat_id=user_id, media=media)
        elif len(images) == 1:
            await bot.send_photo(chat_id=user_id, photo=images[0], caption=text, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Send failed for {user_id}: {e}")
        try:
            await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
        except Exception as e2:
            logger.error(f"Fallback send failed for {user_id}: {e2}")
