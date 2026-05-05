import logging
import re
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import database as db

logger = logging.getLogger(__name__)

AVITO_URL_RE = re.compile(r"https?://(www\.)?avito\.ru/.+")
MAX_WATCHES_PER_USER = 10


class AddWatch(StatesGroup):
    waiting_for_url = State()
    waiting_for_label = State()


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
    dp.message.register(_handle_url, AddWatch.waiting_for_url)
    dp.message.register(_handle_label, AddWatch.waiting_for_label)
    dp.callback_query.register(_cb_delete, F.data.startswith("del:"))


async def _cmd_start(msg: Message):
    await msg.answer(
        "👋 <b>Авито Рингер</b>\n\n"
        "Мониторю новые объявления и сразу тебе сообщаю.\n\n"
        "/add — добавить поиск\n"
        "/list — мои поиски\n"
        "/stop — остановить поиск\n"
        "/help — помощь",
        parse_mode="HTML",
    )


async def _cmd_help(msg: Message):
    await msg.answer(
        "<b>Как пользоваться:</b>\n\n"
        "1. Зайди на avito.ru, настрой поиск (фильтры, регион, цена)\n"
        "2. Скопируй URL из адресной строки\n"
        "3. Отправь мне /add и вставь ссылку\n\n"
        "<b>Команды:</b>\n"
        "/add — добавить новый поиск\n"
        "/list — список активных поисков\n"
        "/stop — удалить поиск\n\n"
        "<b>Интервал проверки:</b> каждую минуту",
        parse_mode="HTML",
    )


async def _cmd_add(msg: Message, state: FSMContext):
    watches = await db.get_user_watches(msg.from_user.id)
    if len(watches) >= MAX_WATCHES_PER_USER:
        await msg.answer(f"❌ Максимум {MAX_WATCHES_PER_USER} поисков. Удали старый через /stop")
        return
    await state.set_state(AddWatch.waiting_for_url)
    await msg.answer(
        "🔗 Отправь ссылку на поиск Авито.\n\n"
        "<i>Пример: https://www.avito.ru/moskva/avtomobili?q=bmw&pmin=500000</i>",
        parse_mode="HTML",
    )


async def _handle_url(msg: Message, state: FSMContext):
    url = msg.text.strip()
    if not AVITO_URL_RE.match(url):
        await msg.answer("❌ Это не похоже на ссылку Авито. Попробуй ещё раз.")
        return

    # Normalize: sort query params for dedup
    await state.update_data(url=url)
    await state.set_state(AddWatch.waiting_for_label)
    await msg.answer(
        "📝 Как назвать этот поиск? (например: <i>BMW дешёвые</i>)\n\n"
        "Или отправь <b>-</b> чтобы пропустить.",
        parse_mode="HTML",
    )


async def _handle_label(msg: Message, state: FSMContext):
    label = msg.text.strip()
    if label == "-":
        label = ""
    data = await state.get_data()
    url = data["url"]
    watch_id = await db.add_watch(msg.from_user.id, url, label)
    await state.clear()
    name = label or f"Поиск #{watch_id}"
    await msg.answer(
        f"✅ <b>{name}</b> добавлен!\n\n"
        f"Буду проверять каждую минуту и сразу сообщать о новых объявлениях.",
        parse_mode="HTML",
    )


async def _cmd_list(msg: Message):
    watches = await db.get_user_watches(msg.from_user.id)
    if not watches:
        await msg.answer("У тебя пока нет активных поисков. Добавь через /add")
        return

    text = "📋 <b>Твои поиски:</b>\n\n"
    for w in watches:
        name = w["label"] or f"Поиск #{w['id']}"
        text += f"<b>#{w['id']}</b> — {name}\n<code>{w['url'][:60]}...</code>\n\n"

    await msg.answer(text, parse_mode="HTML")


async def _cmd_stop(msg: Message):
    watches = await db.get_user_watches(msg.from_user.id)
    if not watches:
        await msg.answer("У тебя нет активных поисков.")
        return

    buttons = []
    for w in watches:
        name = w["label"] or f"Поиск #{w['id']}"
        buttons.append([InlineKeyboardButton(text=f"❌ {name}", callback_data=f"del:{w['id']}")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await msg.answer("Какой поиск остановить?", reply_markup=kb)


async def _cb_delete(cb: CallbackQuery):
    watch_id = int(cb.data.split(":")[1])
    removed = await db.remove_watch(watch_id, cb.from_user.id)
    if removed:
        await cb.message.edit_text(f"✅ Поиск #{watch_id} удалён.")
    else:
        await cb.answer("Не найдено или нет прав.", show_alert=True)


def _build_listing_text(listing: dict, watch_label: str) -> str:
    label = watch_label or "Новое объявление"
    lines = [f"🔔 <b>{label}</b>\n"]
    lines.append(f"<b>{listing['title']}</b>")
    lines.append(f"💰 <b>{listing['price']}</b>")

    # Характеристики
    params: dict = listing.get("params", {})
    if params:
        lines.append("")
        for k, v in list(params.items())[:8]:
            lines.append(f"▪️ {k}: {v}")

    # Описание
    desc = listing.get("description", "").strip()
    if desc:
        short = desc[:300] + ("..." if len(desc) > 300 else "")
        lines.append(f"\n📝 {short}")

    lines.append("")
    if listing.get("location"):
        lines.append(f"📍 {listing['location']}")
    if listing.get("date"):
        lines.append(f"🕐 {listing['date']}")

    # Продавец
    seller_parts = []
    if listing.get("seller_name"):
        seller_parts.append(listing["seller_name"])
    if listing.get("seller_type"):
        seller_parts.append(listing["seller_type"])
    if seller_parts:
        lines.append(f"👤 {' · '.join(seller_parts)}")

    lines.append(f"\n<a href='{listing['link']}'>🔗 Открыть объявление</a>")
    return "\n".join(lines)


async def send_listing(bot: Bot, user_id: int, listing: dict, watch_label: str):
    from aiogram.types import InputMediaPhoto

    text = _build_listing_text(listing, watch_label)
    images: list = listing.get("images", [])
    # Filter out empty/broken image urls
    images = [img for img in images if img and img.startswith("http")]

    try:
        if len(images) >= 2:
            # Send as album (max 10 photos), caption on first
            media = [InputMediaPhoto(media=images[0], caption=text, parse_mode="HTML")]
            for img in images[1:10]:
                media.append(InputMediaPhoto(media=img))
            await bot.send_media_group(chat_id=user_id, media=media)

        elif len(images) == 1:
            await bot.send_photo(
                chat_id=user_id,
                photo=images[0],
                caption=text,
                parse_mode="HTML",
            )
        else:
            await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")

    except Exception as e:
        logger.warning(f"Failed to send listing to {user_id}: {e}")
        try:
            await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
        except Exception as e2:
            logger.error(f"Failed fallback send to {user_id}: {e2}")
