"""Учёт находок и атрибуция 5%.

Роли:
  • Реселлер (друг) — единственный оператор: заносит закупы (ссылка + цена +
    байер + описание/фото), отмечает продажи, ведёт свою статистику, запрашивает
    закрытие долга по 5%. Атрибуция «через бота / сам» считается автоматически по
    журналу присланного (database.sent_items), реселлер её не трогает.
  • Владелец — получает уведомление о каждой ботовской продаже, сводку 5% и
    подтверждает закрытие долга.

Подключается из main.py: register_deal_handlers(dp).
"""
import html
import logging
import re

from aiogram import F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db
from database import OWNER_ID, FEE_RATE

logger = logging.getLogger(__name__)

# ── Кнопки меню реселлера ────────────────────────────────────────────────────
BTN_DEAL_BUY    = "➕ Закуп"
BTN_DEAL_SELL   = "✅ Продать"
BTN_DEAL_CARDS  = "📂 Сделки"
BTN_DEAL_STATS  = "📊 Моя статистика"
BTN_DEAL_SETTLE = "💰 Закрыть долг"
BTN_DEAL_DROP   = "❌ Отменить сделку"
BTN_DEAL_CANCEL = "◀️ Отмена"
BTN_DONE        = "✅ Готово"

RESELLER_WELCOME = (
    "🧾 <b>Учёт сделок</b>\n\n"
    "• <b>➕ Закуп</b> — ссылка → цена → байер → описание/фото. "
    "Бот сам определит, из бота находка или нет.\n"
    "• <b>✅ Продать</b> — отметь проданное и цену.\n"
    "• <b>📂 Сделки</b> — карточки открытых сделок (фото, описание, действия).\n"
    "• <b>📊 Моя статистика</b> — сводка, разбивка по байерам, долг по 5%.\n"
    "• <b>💰 Закрыть долг</b> — запросить у владельца подтверждение оплаты 5%.\n"
    "• <b>❌ Отменить сделку</b> — убрать ошибочный закуп."
)


def reseller_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_DEAL_BUY), KeyboardButton(text=BTN_DEAL_SELL)],
            [KeyboardButton(text=BTN_DEAL_CARDS), KeyboardButton(text=BTN_DEAL_STATS)],
            [KeyboardButton(text=BTN_DEAL_SETTLE), KeyboardButton(text=BTN_DEAL_DROP)],
        ],
        resize_keyboard=True,
        persistent=True,
    )


def _cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_DEAL_CANCEL)]], resize_keyboard=True)


def _done_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_DONE)]], resize_keyboard=True)


# ── FSM ──────────────────────────────────────────────────────────────────────
class DealFSM(StatesGroup):
    buy_link       = State()
    buy_price      = State()
    buy_buyer      = State()   # ждём выбор кнопкой
    buy_buyer_text = State()   # ждём ввод имени вручную
    describe       = State()   # описание/фото к сделке
    sell_price     = State()


# ── Хелперы ──────────────────────────────────────────────────────────────────
def _parse_price(text: str) -> int | None:
    """'35000' / '35 000' / '35к' / '35.5к' → int рублей, либо None."""
    t = (text or "").strip().lower().replace(" ", "").replace(" ", "")
    mult = 1
    if re.search(r"(к|k|тыс\.?|т)$", t):
        mult = 1000
        t = re.sub(r"(к|k|тыс\.?|т)$", "", t)
    t = t.replace(",", ".")
    t = re.sub(r"[^\d.]", "", t)
    if not t:
        return None
    try:
        val = int(round(float(t) * mult))
    except ValueError:
        return None
    return val if val > 0 else None


def _money(n) -> str:
    try:
        return f"{int(n):,}".replace(",", " ") + " ₽"
    except (ValueError, TypeError):
        return "—"


def _short(deal: dict) -> str:
    return deal.get("item_id") or (deal.get("url") or "")[-24:] or f"#{deal['id']}"


def _deal_label(deal: dict) -> str:
    return deal.get("title") or _short(deal)


def _esc(s) -> str:
    return html.escape(str(s or ""))


async def _is_operator(user_id: int) -> bool:
    return user_id == OWNER_ID or await db.is_reseller(user_id)


async def _buyers_kb() -> InlineKeyboardMarkup:
    buyers = await db.get_buyers()
    rows = [[InlineKeyboardButton(text=b["name"] or str(b["user_id"]),
                                  callback_data=f"bsel:{b['user_id']}")] for b in buyers]
    rows.append([
        InlineKeyboardButton(text="✍️ Другой", callback_data="bsel:other"),
        InlineKeyboardButton(text="⏭ Пропустить", callback_data="bsel:skip"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Закуп ────────────────────────────────────────────────────────────────────
async def _start_buy(msg: Message, state: FSMContext):
    if not await _is_operator(msg.from_user.id):
        return
    await state.set_state(DealFSM.buy_link)
    await msg.answer(
        "🔗 Пришли <b>ссылку</b> на объявление (Авито или любую другую).",
        parse_mode="HTML", reply_markup=_cancel_menu(),
    )


async def _got_link(msg: Message, state: FSMContext):
    url = (msg.text or "").strip()
    if not url:
        await msg.answer("Пришли ссылку текстом.")
        return
    await state.update_data(url=url)
    await state.set_state(DealFSM.buy_price)
    item_id = db.extract_item_id(url)
    hint = f"\n(item_id: <code>{item_id}</code>)" if item_id else ""
    await msg.answer(
        f"💰 Цена <b>закупа</b>? (например 35000 или 35к){hint}",
        parse_mode="HTML", reply_markup=_cancel_menu(),
    )


async def _got_buy_price(msg: Message, state: FSMContext):
    price = _parse_price(msg.text or "")
    if price is None:
        await msg.answer("Не понял цену. Пришли число, например 35000 или 35к.")
        return
    await state.update_data(buy_price=price)
    await state.set_state(DealFSM.buy_buyer)
    await msg.answer("👤 От кого закуп?", reply_markup=await _buyers_kb())


async def _cb_buyer(cb: CallbackQuery, state: FSMContext):
    val = cb.data.split(":", 1)[1]
    if val == "skip":
        await cb.answer()
        await _finalize_buy(cb.message, cb.from_user.id, state, "", None)
    elif val == "other":
        await state.set_state(DealFSM.buy_buyer_text)
        await cb.message.answer("👤 Впиши имя байера:", reply_markup=_cancel_menu())
        await cb.answer()
    else:
        bid = int(val)
        name = await db.buyer_name(bid) or str(bid)
        await cb.answer()
        await _finalize_buy(cb.message, cb.from_user.id, state, name, bid)


async def _got_buyer_text(msg: Message, state: FSMContext):
    name = (msg.text or "").strip()
    if name in ("-", "—"):
        name = ""
    await _finalize_buy(msg, msg.from_user.id, state, name, None)


async def _finalize_buy(dst: Message, reseller_id: int, state: FSMContext,
                        buyer_hint: str, buyer_id: int | None):
    data = await state.get_data()
    url = data.get("url", "")
    price = data.get("buy_price", 0)
    deal = await db.add_deal(url, price, reseller_id=reseller_id,
                             buyer_hint=buyer_hint, buyer_id=buyer_id)
    await state.set_state(DealFSM.describe)
    await state.update_data(deal_id=deal["id"], note="")

    if deal["attributed"]:
        head = f"✅ <b>Через бота</b> — при продаже засчитаю {int(FEE_RATE*100)}%\n<i>{_deal_label(deal)}</i>"
    else:
        head = "➖ <b>Не через бота</b> — 5% не берётся"
    who = f" · от {buyer_hint}" if buyer_hint else ""
    await dst.answer(
        f"{head}\n\nСделка <b>#{deal['id']}</b> · закуп {_money(price)}{who}\n\n"
        f"📝 Опиши товар (текст) и/или пришли фото — для себя. "
        f"Когда закончишь — жми <b>{BTN_DONE}</b>.",
        parse_mode="HTML", reply_markup=_done_menu(),
    )


# ── Описание / фото ──────────────────────────────────────────────────────────
async def _describe_photo(msg: Message, state: FSMContext):
    data = await state.get_data()
    deal_id = data.get("deal_id")
    if not deal_id:
        return
    await db.add_deal_photo(deal_id, msg.photo[-1].file_id)
    await msg.answer(f"📷 Фото добавлено. Ещё текст/фото или <b>{BTN_DONE}</b>.", parse_mode="HTML")


async def _describe_text(msg: Message, state: FSMContext):
    data = await state.get_data()
    deal_id = data.get("deal_id")
    if not deal_id:
        return
    prev = data.get("note") or ""
    note = (prev + "\n" + (msg.text or "").strip()).strip()
    await state.update_data(note=note)
    await db.set_deal_note(deal_id, note)
    await msg.answer(f"📝 Описание сохранено. Ещё текст/фото или <b>{BTN_DONE}</b>.", parse_mode="HTML")


async def _describe_done(msg: Message, state: FSMContext):
    data = await state.get_data()
    deal_id = data.get("deal_id")
    await state.clear()
    await msg.answer(
        f"Готово по сделке <b>#{deal_id}</b>." if deal_id else "Готово.",
        parse_mode="HTML", reply_markup=reseller_menu(),
    )


# ── Продажа ──────────────────────────────────────────────────────────────────
async def _start_sell(msg: Message, state: FSMContext):
    if not await _is_operator(msg.from_user.id):
        return
    opens = await db.get_open_deals(msg.from_user.id)
    if not opens:
        await msg.answer("Открытых сделок нет.", reply_markup=reseller_menu())
        return
    rows = []
    for d in opens[:30]:
        mark = "🤖" if d["attributed"] else "➖"
        label = f"#{d['id']} {mark} {_money(d['buy_price'])} · {_deal_label(d)}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"sell:{d['id']}")])
    await msg.answer("Выбери проданную сделку:",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _cb_pick_sell(cb: CallbackQuery, state: FSMContext):
    deal_id = int(cb.data.split(":")[1])
    deal = await db.get_deal(deal_id)
    if not deal or deal["status"] != "open":
        await cb.answer("Сделка уже закрыта.", show_alert=True)
        return
    await state.update_data(deal_id=deal_id)
    await state.set_state(DealFSM.sell_price)
    await cb.message.answer(
        f"💵 Цена <b>продажи</b> для сделки #{deal_id} (закуп {_money(deal['buy_price'])})?",
        parse_mode="HTML", reply_markup=_cancel_menu(),
    )
    await cb.answer()


async def _got_sell_price(msg: Message, state: FSMContext):
    price = _parse_price(msg.text or "")
    if price is None:
        await msg.answer("Не понял цену. Пришли число, например 42000 или 42к.")
        return
    data = await state.get_data()
    deal_id = data.get("deal_id")
    await state.clear()
    sold = await db.mark_deal_sold(deal_id, price)
    if sold is None:
        await msg.answer("Сделка не найдена или уже закрыта.", reply_markup=reseller_menu())
        return

    margin, fee = sold["margin"], sold["fee"]
    src = "🤖 через бота" if sold["attributed"] else "➖ сам"
    await msg.answer(
        f"✅ Продано <b>#{deal_id}</b> ({src})\n"
        f"Закуп {_money(sold['buy_price'])} → продажа {_money(price)}\n"
        f"Маржа <b>{_money(margin)}</b>"
        + (f" · доля владельца {_money(fee)}" if sold["attributed"] else ""),
        parse_mode="HTML", reply_markup=reseller_menu(),
    )

    if sold["attributed"]:
        try:
            await msg.bot.send_message(
                OWNER_ID,
                f"🟢 <b>Продажа через бота</b> · сделка #{deal_id}\n"
                f"{_deal_label(sold)}\n"
                f"Закуп {_money(sold['buy_price'])} → продажа {_money(price)}\n"
                f"Маржа {_money(margin)} · <b>твои {int(FEE_RATE*100)}%: {_money(fee)}</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"owner notify failed for deal {deal_id}: {e}")


# ── Карточки сделок ──────────────────────────────────────────────────────────
async def _start_cards(msg: Message, state: FSMContext):
    if not await _is_operator(msg.from_user.id):
        return
    opens = await db.get_open_deals(msg.from_user.id)
    if not opens:
        await msg.answer("Открытых сделок нет.", reply_markup=reseller_menu())
        return
    rows = []
    for d in opens[:30]:
        mark = "🤖" if d["attributed"] else "➖"
        label = f"#{d['id']} {mark} {_money(d['buy_price'])} · {_deal_label(d)}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"card:{d['id']}")])
    await msg.answer("Твои открытые сделки:",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _cb_card(cb: CallbackQuery, state: FSMContext):
    deal_id = int(cb.data.split(":")[1])
    d = await db.get_deal(deal_id)
    if not d:
        await cb.answer("Сделка не найдена.", show_alert=True)
        return
    photos = await db.get_deal_photos(deal_id)
    src = "🤖 через бота" if d["attributed"] else "➖ сам"
    txt = f"<b>Сделка #{d['id']}</b> · {src}\n{_esc(_deal_label(d))}\nЗакуп: {_money(d['buy_price'])}"
    if d["status"] == "sold":
        txt += f" → продажа {_money(d['sell_price'])} · 5% {_money(d.get('fee') or 0)}"
    if d.get("buyer_hint"):
        txt += f"\nОт кого: {_esc(d['buyer_hint'])}"
    if d.get("note"):
        txt += f"\n📝 {_esc(d['note'])}"
    if photos:
        txt += f"\n📷 фото: {len(photos)}"

    actions = []
    if d["status"] == "open":
        actions.append([
            InlineKeyboardButton(text="✅ Продать", callback_data=f"sell:{d['id']}"),
            InlineKeyboardButton(text="❌ Удалить", callback_data=f"dcancel:{d['id']}"),
        ])
    actions.append([InlineKeyboardButton(text="📝 Дописать / фото", callback_data=f"dedit:{d['id']}")])
    await cb.message.answer(txt, parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=actions))
    if photos:
        try:
            await cb.bot.send_media_group(
                cb.from_user.id, [InputMediaPhoto(media=f) for f in photos[:10]]
            )
        except Exception as e:
            logger.warning(f"send photos failed deal {deal_id}: {e}")
    await cb.answer()


async def _cb_dedit(cb: CallbackQuery, state: FSMContext):
    deal_id = int(cb.data.split(":")[1])
    d = await db.get_deal(deal_id)
    if not d:
        await cb.answer("Сделка не найдена.", show_alert=True)
        return
    await state.set_state(DealFSM.describe)
    await state.update_data(deal_id=deal_id, note=d.get("note") or "")
    await cb.message.answer(
        f"📝 Дополни сделку #{deal_id}: пришли текст и/или фото, потом <b>{BTN_DONE}</b>.",
        parse_mode="HTML", reply_markup=_done_menu(),
    )
    await cb.answer()


# ── Отмена (удаление) сделки ──────────────────────────────────────────────────
async def _start_drop(msg: Message, state: FSMContext):
    if not await _is_operator(msg.from_user.id):
        return
    opens = await db.get_open_deals(msg.from_user.id)
    if not opens:
        await msg.answer("Открытых сделок нет.", reply_markup=reseller_menu())
        return
    rows = []
    for d in opens[:30]:
        mark = "🤖" if d["attributed"] else "➖"
        label = f"#{d['id']} {mark} {_money(d['buy_price'])} · {_deal_label(d)}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"dcancel:{d['id']}")])
    await msg.answer("Какую сделку удалить?",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _cb_drop(cb: CallbackQuery, state: FSMContext):
    deal_id = int(cb.data.split(":")[1])
    ok = await db.delete_deal(deal_id, reseller_id=cb.from_user.id)
    if ok:
        await cb.message.edit_text(f"🗑 Сделка #{deal_id} удалена.")
        await cb.answer()
    else:
        await cb.answer("Не найдена или не твоя.", show_alert=True)


# ── Закрытие долга по 5% ──────────────────────────────────────────────────────
async def _start_settle(msg: Message, state: FSMContext):
    if not await _is_operator(msg.from_user.id):
        return
    rid = msg.from_user.id
    out = await db.get_outstanding_fee(rid)
    if out <= 0:
        await msg.answer("Долга по 5% нет 👍", reply_markup=reseller_menu())
        return
    await msg.answer(
        f"Отправил владельцу запрос на закрытие долга <b>{_money(out)}</b>. Жди подтверждения.",
        parse_mode="HTML", reply_markup=reseller_menu(),
    )
    try:
        await msg.bot.send_message(
            OWNER_ID,
            f"💰 <b>Запрос на закрытие долга</b>\n"
            f"Реселлер <code>{rid}</code> просит подтвердить оплату <b>{_money(out)}</b>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Принять оплату", callback_data=f"setlok:{rid}"),
                InlineKeyboardButton(text="❌ Позже", callback_data=f"setlno:{rid}"),
            ]]),
        )
    except Exception as e:
        logger.warning(f"settle request to owner failed: {e}")


async def _cb_settle_ok(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("Только владелец.", show_alert=True)
        return
    rid = int(cb.data.split(":")[1])
    amount = await db.settle_debt(rid)
    await cb.message.edit_text(f"✅ Долг {_money(amount)} закрыт (реселлер {rid}).")
    try:
        await cb.bot.send_message(rid, f"✅ Владелец подтвердил оплату. Долг {_money(amount)} закрыт. Спасибо!")
    except Exception:
        pass
    await cb.answer()


async def _cb_settle_no(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("Только владелец.", show_alert=True)
        return
    rid = int(cb.data.split(":")[1])
    await cb.message.edit_text("❌ Закрытие долга отклонено.")
    try:
        await cb.bot.send_message(rid, "Владелец пока не подтвердил закрытие долга.")
    except Exception:
        pass
    await cb.answer()


# ── Статистика реселлера ──────────────────────────────────────────────────────
async def _reseller_stats(msg: Message, state: FSMContext):
    if not await _is_operator(msg.from_user.id):
        return
    r = await db.get_reseller_report(msg.from_user.id)
    out = await db.get_outstanding_fee(msg.from_user.id)
    tot = await db.get_total_fee(msg.from_user.id)
    opens = await db.get_open_deals(msg.from_user.id)

    lines = ["📊 <b>Твоя статистика</b>\n", f"<b>Открытые сделки: {len(opens)}</b>"]
    for d in opens[:20]:
        mark = "🤖" if d["attributed"] else "➖"
        row = f"#{d['id']} {mark} {_esc(_deal_label(d))} · закуп {_money(d['buy_price'])}"
        if d.get("buyer_hint"):
            row += f" · от {_esc(d['buyer_hint'])}"
        lines.append(row)
        if d.get("note"):
            note = _esc(d["note"].replace("\n", " "))
            lines.append(f"   📝 {note[:140]}")
    if len(opens) > 20:
        lines.append(f"…и ещё {len(opens) - 20}")

    lines += [
        f"\nПродано: <b>{r.get('sold_cnt', 0)}</b> (из них через бота: <b>{r.get('bot_sold', 0)}</b>)",
        f"Суммарная маржа: <b>{_money(r.get('margin', 0))}</b>",
        f"\n💰 Долг владельцу сейчас: <b>{_money(out)}</b>",
        f"Всего начислено 5%: <b>{_money(tot)}</b>",
    ]
    breakdown = await db.get_buyer_breakdown(msg.from_user.id)
    if breakdown:
        lines.append("\n<b>По байерам:</b>")
        for b in breakdown[:10]:
            lines.append(f"• {_esc(b['buyer'])}: {b['cnt']} сделок · маржа {_money(b['margin'])}")
    await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=reseller_menu())


# ── Отмена ввода ──────────────────────────────────────────────────────────────
async def _cancel(msg: Message, state: FSMContext):
    await state.clear()
    if await db.is_reseller(msg.from_user.id) and msg.from_user.id != OWNER_ID:
        await msg.answer("Отменил.", reply_markup=reseller_menu())
    else:
        await msg.answer("Отменил.")


# ── Команды владельца ─────────────────────────────────────────────────────────
async def _cmd_report(msg: Message):
    if msg.from_user.id != OWNER_ID:
        return
    parts = (msg.text or "").split()
    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    rep = await db.get_owner_report(days)
    out = await db.get_outstanding_fee()
    tot = await db.get_total_fee()
    period = f"за {days} дн." if days else "за всё время"
    lines = [
        f"💼 <b>Твои 5% {period}</b>\n",
        f"Ботовских продаж: <b>{rep['count']}</b>",
        f"Маржа по ним: <b>{_money(rep['margin'])}</b>",
        f"\n💰 К оплате сейчас (долг): <b>{_money(out)}</b>",
        f"Всего начислено за всё время: <b>{_money(tot)}</b>",
    ]
    if rep["deals"]:
        lines.append("\n<b>Последние:</b>")
        for d in rep["deals"][:10]:
            paid = "✓" if d.get("settled") else "•"
            lines.append(
                f"{paid} #{d['id']} {_deal_label(d)[:20]} · "
                f"{_money(d['buy_price'])}→{_money(d['sell_price'])} · 5% {_money(d['fee'])}"
            )
    await msg.answer("\n".join(lines), parse_mode="HTML")


async def _cmd_deal_del(msg: Message):
    if msg.from_user.id != OWNER_ID:
        return
    parts = (msg.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.answer("Использование: <code>/deal_del &lt;номер&gt;</code>", parse_mode="HTML")
        return
    ok = await db.delete_deal(int(parts[1]))
    await msg.answer(f"🗑 Сделка #{parts[1]} удалена." if ok else "Сделка не найдена.")


async def _cmd_buyer_add(msg: Message):
    if msg.from_user.id != OWNER_ID:
        return
    parts = (msg.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await msg.answer("Использование: <code>/buyer_add &lt;telegram_id&gt; &lt;имя&gt;</code>", parse_mode="HTML")
        return
    await db.add_buyer(int(parts[1]), parts[2].strip())
    await msg.answer(f"✅ Байер <code>{parts[1]}</code> → «{parts[2].strip()}»", parse_mode="HTML")


async def _cmd_buyers(msg: Message):
    if msg.from_user.id != OWNER_ID:
        return
    buyers = await db.get_buyers()
    if not buyers:
        await msg.answer("Байеров нет. Добавь: /buyer_add &lt;id&gt; &lt;имя&gt;", parse_mode="HTML")
        return
    await msg.answer(
        "👥 Байеры:\n" + "\n".join(f"• {b['name']} — <code>{b['user_id']}</code>" for b in buyers),
        parse_mode="HTML",
    )


async def _cmd_reseller_add(msg: Message):
    if msg.from_user.id != OWNER_ID:
        return
    target = None
    if msg.reply_to_message and msg.reply_to_message.forward_from:
        target = msg.reply_to_message.forward_from.id
    else:
        parts = (msg.text or "").split()
        if len(parts) > 1 and parts[1].lstrip("-").isdigit():
            target = int(parts[1])
    if target is None:
        await msg.answer(
            "Использование: <code>/reseller_add &lt;telegram_id&gt;</code>\n"
            "или ответь этой командой на пересланное от него сообщение.",
            parse_mode="HTML",
        )
        return
    await db.add_reseller(target)
    await msg.answer(f"✅ Реселлер <code>{target}</code> добавлен.", parse_mode="HTML")
    try:
        await msg.bot.send_message(target, "🧾 Тебе выдан доступ к учёту сделок. Нажми /start.")
    except Exception:
        pass


async def _cmd_reseller_del(msg: Message):
    if msg.from_user.id != OWNER_ID:
        return
    parts = (msg.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await msg.answer("Использование: <code>/reseller_del &lt;telegram_id&gt;</code>", parse_mode="HTML")
        return
    ok = await db.remove_reseller(int(parts[1]))
    await msg.answer("✅ Удалён." if ok else "Не найден.")


async def _cmd_resellers(msg: Message):
    if msg.from_user.id != OWNER_ID:
        return
    ids = await db.get_resellers()
    if not ids:
        await msg.answer("Реселлеров нет. Добавь: /reseller_add &lt;id&gt;", parse_mode="HTML")
        return
    await msg.answer("👥 Реселлеры:\n" + "\n".join(f"• <code>{i}</code>" for i in ids), parse_mode="HTML")


# ── Регистрация ───────────────────────────────────────────────────────────────
def register_deal_handlers(dp):
    # Отмена и «Готово» — до state-хэндлеров.
    dp.message.register(_cancel, F.text == BTN_DEAL_CANCEL)
    dp.message.register(_cancel, Command("cancel"))
    dp.message.register(_describe_done, F.text == BTN_DONE, DealFSM.describe)

    # Кнопки меню (до state-хэндлеров).
    dp.message.register(_start_buy,      F.text == BTN_DEAL_BUY)
    dp.message.register(_start_sell,     F.text == BTN_DEAL_SELL)
    dp.message.register(_start_cards,    F.text == BTN_DEAL_CARDS)
    dp.message.register(_reseller_stats, F.text == BTN_DEAL_STATS)
    dp.message.register(_start_settle,   F.text == BTN_DEAL_SETTLE)
    dp.message.register(_start_drop,     F.text == BTN_DEAL_DROP)

    # Команды владельца.
    dp.message.register(_cmd_report,       Command("report"))
    dp.message.register(_cmd_deal_del,     Command("deal_del"))
    dp.message.register(_cmd_buyer_add,    Command("buyer_add"))
    dp.message.register(_cmd_buyers,       Command("buyers"))
    dp.message.register(_cmd_reseller_add, Command("reseller_add"))
    dp.message.register(_cmd_reseller_del, Command("reseller_del"))
    dp.message.register(_cmd_resellers,    Command("resellers"))

    # Инлайн-коллбэки.
    dp.callback_query.register(_cb_buyer,     F.data.startswith("bsel:"), DealFSM.buy_buyer)
    dp.callback_query.register(_cb_pick_sell, F.data.startswith("sell:"))
    dp.callback_query.register(_cb_drop,      F.data.startswith("dcancel:"))
    dp.callback_query.register(_cb_card,      F.data.startswith("card:"))
    dp.callback_query.register(_cb_dedit,     F.data.startswith("dedit:"))
    dp.callback_query.register(_cb_settle_ok, F.data.startswith("setlok:"))
    dp.callback_query.register(_cb_settle_no, F.data.startswith("setlno:"))

    # State-хэндлеры (после кнопок).
    dp.message.register(_got_link,        DealFSM.buy_link)
    dp.message.register(_got_buy_price,   DealFSM.buy_price)
    dp.message.register(_got_buyer_text,  DealFSM.buy_buyer_text)
    dp.message.register(_describe_photo,  F.photo, DealFSM.describe)
    dp.message.register(_describe_text,   DealFSM.describe)
    dp.message.register(_got_sell_price,  DealFSM.sell_price)
