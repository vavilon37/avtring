import logging
import re
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto,
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


# ── FSM ──────────────────────────────────────────────────────────────────────

class AddWatch(StatesGroup):
    model = State()
    price_min = State()
    price_max = State()
    condition = State()
    seller = State()
    city = State()


# ── Bot factory ──────────────────────────────────────────────────────────────

def make_bot(token: str) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=token)
    dp = Dispatcher(storage=MemoryStorage())
    _register_handlers(dp)
    return bot, dp


def _register_handlers(dp: Dispatcher):
    dp.message.register(_cmd_start, CommandStart())
    dp.message.register(_cmd_help, Command("help"))
    dp.message.register(_cmd_add, Command("add"))
    dp.message.register(_cmd_list, Command("list"))
    dp.message.register(_cmd_stop, Command("stop"))

    # FSM
    dp.callback_query.register(_cb_model, F.data.startswith("model:"), AddWatch.model)
    dp.callback_query.register(_cb_condition, F.data.startswith("cond:"), AddWatch.condition)
    dp.callback_query.register(_cb_seller, F.data.startswith("seller:"), AddWatch.seller)
    dp.callback_query.register(_cb_city, F.data.startswith("city:"), AddWatch.city)
    dp.message.register(_handle_price_min, AddWatch.price_min)
    dp.message.register(_handle_price_max, AddWatch.price_max)

    # Delete watch
    dp.callback_query.register(_cb_delete, F.data.startswith("del:"))


# ── Keyboards ────────────────────────────────────────────────────────────────

def _kb_models() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"model:{q}")]
        for name, q in PHONE_MODELS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _kb_conditions() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"cond:{val}")]
        for name, val in CONDITIONS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _kb_sellers() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"seller:{val}")]
        for name, val in SELLER_TYPES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _kb_cities() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"city:{val}")]
        for name, val in CITIES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Commands ─────────────────────────────────────────────────────────────────

async def _cmd_start(msg: Message):
    await msg.answer(
        "📱 <b>iPhone &amp; Phone Ringer</b>\n\n"
        "Слежу за новыми объявлениями о продаже телефонов на Авито "
        "и сразу присылаю карточку с фото и всеми характеристиками.\n\n"
        "/add — настроить новый поиск\n"
        "/list — мои поиски\n"
        "/stop — удалить поиск\n"
        "/help — помощь",
        parse_mode="HTML",
    )


async def _cmd_help(msg: Message):
    await msg.answer(
        "<b>Как работает:</b>\n\n"
        "1. Нажми /add\n"
        "2. Выбери модель, цену, состояние, продавца и город\n"
        "3. Бот начнёт мониторить Авито каждую минуту\n"
        "4. При новом объявлении — сразу пришлю карточку с фото, "
        "характеристиками, описанием и ценой\n\n"
        "До 10 поисков одновременно.",
        parse_mode="HTML",
    )


# ── Add watch FSM ─────────────────────────────────────────────────────────────

async def _cmd_add(msg: Message, state: FSMContext):
    watches = await db.get_user_watches(msg.from_user.id)
    if len(watches) >= MAX_WATCHES_PER_USER:
        await msg.answer(f"❌ Максимум {MAX_WATCHES_PER_USER} поисков. Удали старый через /stop")
        return
    await state.set_state(AddWatch.model)
    await msg.answer("📱 <b>Шаг 1/5.</b> Выбери модель:", parse_mode="HTML", reply_markup=_kb_models())


async def _cb_model(cb: CallbackQuery, state: FSMContext):
    query = cb.data.split(":", 1)[1]
    await state.update_data(query=query)
    await cb.message.edit_text(
        "💰 <b>Шаг 2/5.</b> Минимальная цена (₽)?\n\n"
        "Напиши число или отправь <b>0</b> чтобы пропустить.",
        parse_mode="HTML",
    )
    await state.set_state(AddWatch.price_min)


async def _handle_price_min(msg: Message, state: FSMContext):
    val = msg.text.strip().replace(" ", "")
    pmin = val if val.isdigit() and int(val) > 0 else ""
    await state.update_data(pmin=pmin)
    await msg.answer(
        "💰 <b>Шаг 3/5.</b> Максимальная цена (₽)?\n\n"
        "Напиши число или отправь <b>0</b> чтобы пропустить.",
        parse_mode="HTML",
    )
    await state.set_state(AddWatch.price_max)


async def _handle_price_max(msg: Message, state: FSMContext):
    val = msg.text.strip().replace(" ", "")
    pmax = val if val.isdigit() and int(val) > 0 else ""
    await state.update_data(pmax=pmax)
    await msg.answer(
        "📦 <b>Шаг 4/5.</b> Состояние телефона:",
        parse_mode="HTML",
        reply_markup=_kb_conditions(),
    )
    await state.set_state(AddWatch.condition)


async def _cb_condition(cb: CallbackQuery, state: FSMContext):
    val = cb.data.split(":", 1)[1]
    await state.update_data(condition=val)
    await cb.message.edit_text(
        "👤 <b>Шаг 5/5 (a).</b> Тип продавца:",
        parse_mode="HTML",
        reply_markup=_kb_sellers(),
    )
    await state.set_state(AddWatch.seller)


async def _cb_seller(cb: CallbackQuery, state: FSMContext):
    val = cb.data.split(":", 1)[1]
    await state.update_data(seller_type=val)
    await cb.message.edit_text(
        "🏙 <b>Шаг 5/5 (b).</b> Город:",
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
    watch_id = await db.add_watch(cb.from_user.id, url, label)

    await cb.message.edit_text(
        f"✅ <b>Поиск создан!</b>\n\n"
        f"🔍 <b>{label}</b>\n"
        f"<code>{url}</code>\n\n"
        f"Буду проверять каждую минуту и сразу присылать новые объявления.",
        parse_mode="HTML",
    )


# ── List & Stop ───────────────────────────────────────────────────────────────

async def _cmd_list(msg: Message):
    watches = await db.get_user_watches(msg.from_user.id)
    if not watches:
        await msg.answer("У тебя пока нет поисков. Добавь через /add")
        return
    text = "📋 <b>Твои поиски:</b>\n\n"
    for w in watches:
        name = w["label"] or f"Поиск #{w['id']}"
        text += f"<b>#{w['id']}</b> {name}\n<code>{w['url'][:70]}</code>\n\n"
    await msg.answer(text, parse_mode="HTML")


async def _cmd_stop(msg: Message):
    watches = await db.get_user_watches(msg.from_user.id)
    if not watches:
        await msg.answer("У тебя нет активных поисков.")
        return
    buttons = [
        [InlineKeyboardButton(
            text=f"❌ {w['label'] or f'Поиск #{w[\"id\"]}'}",
            callback_data=f"del:{w['id']}"
        )]
        for w in watches
    ]
    await msg.answer("Какой поиск остановить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


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
