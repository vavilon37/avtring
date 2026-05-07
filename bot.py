import logging
import httpx
from datetime import datetime, timezone, timedelta
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
from database import FREE_MAX_WATCHES, TRIAL_MAX_WATCHES, PAID_MAX_WATCHES, OWNER_ID, OWNER_IDS, TRIAL_DAYS
from filters import (
    PHONE_MODELS, CONDITIONS, SELLER_TYPES, CITIES,
    build_avito_url, label_from_filters,
)
from payments import create_invoice, check_invoice, PRICE_RUB, SUBSCRIPTION_DAYS

logger = logging.getLogger(__name__)

BTN_ADD  = "➕ Добавить поиск"
BTN_LIST = "📋 Мои поиски"
BTN_STOP = "🗑 Удалить поиск"
BTN_SUB  = "💎 Подписка"
BTN_REF  = "🔗 Пригласить друга"
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


def make_bot(token: str, session=None) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=token, session=session)
    dp  = Dispatcher(storage=MemoryStorage())
    _register_handlers(dp)
    return bot, dp


def _register_handlers(dp: Dispatcher):
    dp.message.register(_cmd_start, CommandStart())
    dp.message.register(_cmd_help,  Command("help"))
    dp.message.register(_cmd_add,   Command("add"))
    dp.message.register(_cmd_list,  Command("list"))
    dp.message.register(_cmd_stop,  Command("stop"))
    dp.message.register(_cmd_sub,   Command("sub"))
    dp.message.register(_cmd_ref,   Command("ref"))

    dp.message.register(_cmd_add,  F.text == BTN_ADD)
    dp.message.register(_cmd_list, F.text == BTN_LIST)
    dp.message.register(_cmd_stop, F.text == BTN_STOP)
    dp.message.register(_cmd_sub,  F.text == BTN_SUB)
    dp.message.register(_cmd_ref,  F.text == BTN_REF)
    dp.message.register(_cmd_help, F.text == BTN_HELP)

    # FSM — мультивыбор моделей
    dp.callback_query.register(_cb_model_toggle, F.data.startswith("mt:"),   AddWatch.model)
    dp.callback_query.register(_cb_model_done,   F.data == "model_done",     AddWatch.model)
    dp.message.register(_handle_price_min, AddWatch.price_min)
    dp.message.register(_handle_price_max, AddWatch.price_max)
    dp.callback_query.register(_cb_condition, F.data.startswith("cond:"),   AddWatch.condition)
    dp.callback_query.register(_cb_seller,    F.data.startswith("seller:"), AddWatch.seller)
    dp.callback_query.register(_cb_city,      F.data.startswith("city:"),   AddWatch.city)

    dp.callback_query.register(_cb_delete,      F.data.startswith("del:"))
    dp.callback_query.register(_cb_check_payment, F.data.startswith("chkpay:"))


# ── Keyboards ────────────────────────────────────────────────────────────────

def _main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD),  KeyboardButton(text=BTN_LIST)],
            [KeyboardButton(text=BTN_STOP), KeyboardButton(text=BTN_SUB)],
            [KeyboardButton(text=BTN_REF),  KeyboardButton(text=BTN_HELP)],
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


def _kb_pay(pay_url: str, invoice_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {PRICE_RUB}₽", url=pay_url)],
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"chkpay:{invoice_id}")],
    ])


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _plan_text(user_id: int) -> str:
    plan = await db.get_user_plan(user_id)
    if plan == "paid":
        user = await db.get_user(user_id)
        expires = user["sub_expires_at"][:10] if user["sub_expires_at"] else "∞"
        return f"💎 <b>Подписка активна</b> до {expires}"
    if plan == "trial":
        user = await db.get_user(user_id)
        started = datetime.fromisoformat(user["trial_started_at"]).replace(tzinfo=timezone.utc)
        bonus = user["trial_bonus_days"] if user["trial_bonus_days"] else 0
        trial_end = started + timedelta(days=TRIAL_DAYS + bonus)
        hours_left = max(0, int((trial_end - datetime.now(timezone.utc)).total_seconds() / 3600))
        return f"🎁 <b>Пробный период</b> — осталось ~{hours_left}ч"
    return "🔒 <b>Нет подписки</b>"


async def _max_watches(user_id: int) -> int:
    if user_id == OWNER_ID:
        return 999
    plan = await db.get_user_plan(user_id)
    if plan == "paid":
        return PAID_MAX_WATCHES
    if plan == "trial":
        return TRIAL_MAX_WATCHES
    return FREE_MAX_WATCHES


# ── Commands ──────────────────────────────────────────────────────────────────

async def _cmd_start(msg: Message):
    is_new = await db.ensure_user(msg.from_user.id)

    # Parse referral payload: /start ref1234567890
    referrer_id = None
    text = msg.text or ""
    if " " in text:
        payload = text.split(" ", 1)[1]
        if payload.startswith("ref") and payload[3:].isdigit():
            referrer_id = int(payload[3:])

    if is_new and referrer_id:
        applied = await db.apply_referral(msg.from_user.id, referrer_id)
        if applied:
            try:
                await msg.bot.send_message(
                    referrer_id,
                    "🎉 <b>По твоей ссылке зарегистрировался новый пользователь!</b>\n"
                    "+1 день пробного периода добавлен.",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    welcome = (
        "📱 <b>Avito Ringer</b>\n\n"
        "Слежу за новыми объявлениями о продаже телефонов на Авито "
        "и сразу присылаю карточку с фото и всеми характеристиками.\n\n"
        f"🎁 <b>{TRIAL_DAYS} день бесплатно</b> — можешь попробовать прямо сейчас!"
    )
    await msg.answer(welcome, parse_mode="HTML", reply_markup=_main_menu())


async def _cmd_help(msg: Message):
    await msg.answer(
        "<b>Как работает:</b>\n\n"
        "1. Нажми <b>➕ Добавить поиск</b>\n"
        "2. Выбери модели, цену, состояние, продавца и город\n"
        "3. Бот мониторит Авито и присылает новые объявления\n"
        "4. При новом объявлении — сразу пришлю карточку с фото, "
        "характеристиками, описанием и ценой\n\n"
        "<b>Подписка:</b>\n"
        f"💎 {PRICE_RUB}₽/{SUBSCRIPTION_DAYS} дней — до {PAID_MAX_WATCHES} поисков, проверка каждые 15 сек\n\n"
        "Удалить поиск — <b>🗑 Удалить поиск</b>.",
        parse_mode="HTML",
        reply_markup=_main_menu(),
    )


async def _cmd_sub(msg: Message):
    await db.ensure_user(msg.from_user.id)
    plan_str = await _plan_text(msg.from_user.id)

    if await db.is_subscribed(msg.from_user.id):
        await msg.answer(
            f"{plan_str}\n\n"
            "Хочешь продлить подписку ещё на 7 дней?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"💳 Продлить за {PRICE_RUB}₽", callback_data="new_invoice")]
            ]),
        )
        return

    invoice = await create_invoice(msg.from_user.id)
    if not invoice:
        await msg.answer("❌ Ошибка при создании счёта. Попробуй позже.")
        return

    await db.save_invoice(invoice["invoice_id"], msg.from_user.id)
    await msg.answer(
        f"{plan_str}\n\n"
        f"💎 <b>Подписка Avito Ringer</b>\n\n"
        f"• Проверка каждые <b>15 секунд</b>\n"
        f"• До <b>3 поисков</b> одновременно\n"
        f"• Срок: <b>{SUBSCRIPTION_DAYS} дней</b>\n"
        f"• Цена: <b>{PRICE_RUB}₽</b>\n\n"
        f"Оплата через @CryptoBot — безопасно и мгновенно.",
        parse_mode="HTML",
        reply_markup=_kb_pay(invoice["pay_url"], invoice["invoice_id"]),
    )


async def _cb_check_payment(cb: CallbackQuery):
    invoice_id = cb.data.split(":", 1)[1]
    await cb.answer("Проверяю оплату...", show_alert=False)

    paid = await check_invoice(invoice_id)
    if paid:
        user_id = cb.from_user.id
        expires = await db.activate_subscription(user_id)
        expires_str = expires.strftime("%d.%m.%Y")
        await cb.message.edit_text(
            f"✅ <b>Подписка активирована!</b>\n\n"
            f"Действует до <b>{expires_str}</b>\n"
            f"Проверка каждые 15 секунд, до 3 поисков.",
            parse_mode="HTML",
        )
    else:
        await cb.answer("❌ Оплата не найдена. Попробуй через минуту.", show_alert=True)


# ── Add watch FSM ─────────────────────────────────────────────────────────────

async def _cmd_add(msg: Message, state: FSMContext):
    await db.ensure_user(msg.from_user.id)
    plan = await db.get_user_plan(msg.from_user.id)

    if plan == "free":
        await msg.answer(
            f"🔒 <b>Пробный период закончился</b>\n\n"
            f"Оформи подписку 💎 за {PRICE_RUB}₽/{SUBSCRIPTION_DAYS} дней — до {PAID_MAX_WATCHES} поисков, проверка каждые 15 сек.\n\n"
            f"Или пригласи друга — получи <b>+1 день</b> бесплатно 🔗",
            parse_mode="HTML",
            reply_markup=_main_menu(),
        )
        return

    watches = await db.get_user_watches(msg.from_user.id)
    max_w = await _max_watches(msg.from_user.id)

    if len(watches) >= max_w:
        await msg.answer(
            f"❌ Максимум {max_w} поисков. Удали старый.",
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
    try:
        await cb.message.edit_reply_markup(reply_markup=_kb_models(set(selected)))
    except Exception:
        pass
    await cb.answer()


async def _cb_model_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected: list = data.get("selected_models", [])
    query = " ".join(selected) if selected else ""
    await state.update_data(query=query)
    try:
        await cb.message.edit_reply_markup()
    except Exception:
        pass
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
    try:
        await cb.message.edit_reply_markup()
    except Exception:
        pass
    await cb.message.answer(
        "👤 <b>Шаг 5/6 — Тип продавца</b>",
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

    plan = await db.get_user_plan(cb.from_user.id)
    interval_note = "каждые 15 секунд" if plan in ("paid", "trial") else "раз в 5 минут"

    try:
        await cb.message.edit_reply_markup()
    except Exception:
        pass
    await cb.message.answer(
        f"✅ <b>Поиск создан!</b>\n\n"
        f"🔍 {label}\n\n"
        f"Мониторю Авито {interval_note}. Как появится новое объявление — сразу пришлю.",
        parse_mode="HTML",
        reply_markup=_main_menu(),
    )


async def _cmd_ref(msg: Message):
    await db.ensure_user(msg.from_user.id)
    me = await msg.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref{msg.from_user.id}"
    count = await db.get_referral_count(msg.from_user.id)
    await msg.answer(
        "🔗 <b>Пригласи друга — получи +1 день</b>\n\n"
        f"За каждого друга, который зайдёт по твоей ссылке, "
        f"тебе начисляется <b>+1 день</b> подписки.\n"
        f"Другу — стандартный пробный день.\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено друзей: <b>{count}</b>",
        parse_mode="HTML",
        reply_markup=_main_menu(),
    )


# ── List & Stop ───────────────────────────────────────────────────────────────

async def _cmd_list(msg: Message):
    watches = await db.get_user_watches(msg.from_user.id)
    plan_str = await _plan_text(msg.from_user.id)
    if not watches:
        await msg.answer(
            f"{plan_str}\n\nУ тебя пока нет поисков. Нажми ➕ Добавить поиск.",
            parse_mode="HTML",
            reply_markup=_main_menu(),
        )
        return
    text = f"{plan_str}\n\n📋 <b>Твои поиски:</b>\n\n"
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
            if v and str(v).strip():
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


async def _download_image(url: str) -> bytes | None:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url, headers={"Referer": "https://www.avito.ru/"})
            if r.status_code == 200:
                return r.content
    except Exception:
        pass
    return None


async def send_listing(bot: Bot, user_id: int, listing: dict, watch_label: str):
    text = _build_listing_text(listing, watch_label)
    images = [img for img in listing.get("images", []) if img and img.startswith("http")]

    try:
        if images:
            # Download first image and send as file (Telegram can't fetch Avito images directly)
            photo_bytes = await _download_image(images[0])
            if photo_bytes:
                from aiogram.types import BufferedInputFile
                photo_file = BufferedInputFile(photo_bytes, filename="photo.jpg")
                await bot.send_photo(chat_id=user_id, photo=photo_file, caption=text, parse_mode="HTML")
            else:
                await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Send failed for {user_id}: {e}")
        try:
            await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
        except Exception as e2:
            logger.error(f"Fallback send failed for {user_id}: {e2}")
