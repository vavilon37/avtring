"""Учёт находок и атрибуция 5%.

Роли:
  • Реселлер (друг) — единственный оператор: заносит закупы (ссылка + цена),
    отмечает продажи. Атрибуция «через бота / сам» считается автоматически по
    журналу присланного (database.sent_items), реселлер её не трогает.
  • Владелец — получает уведомление о каждой ботовской продаже и сводку 5%.

Подключается из main.py: register_deal_handlers(dp).
"""
import logging
import re

from aiogram import F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db
from database import OWNER_ID, FEE_RATE

logger = logging.getLogger(__name__)

# ── Кнопки меню реселлера ────────────────────────────────────────────────────
BTN_DEAL_BUY    = "➕ Закуп"
BTN_DEAL_SELL   = "✅ Продать"
BTN_DEAL_STATS  = "📊 Моя статистика"
BTN_DEAL_CANCEL = "◀️ Отмена"

RESELLER_WELCOME = (
    "🧾 <b>Учёт сделок</b>\n\n"
    "• <b>➕ Закуп</b> — пришли ссылку на телефон и цену закупа. "
    "Я сам определю, из бота находка или нет.\n"
    "• <b>✅ Продать</b> — отметь проданное и укажи цену продажи.\n"
    "• <b>📊 Моя статистика</b> — сводка по всем сделкам."
)


def reseller_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_DEAL_BUY), KeyboardButton(text=BTN_DEAL_SELL)],
            [KeyboardButton(text=BTN_DEAL_STATS)],
        ],
        resize_keyboard=True,
        persistent=True,
    )


def _cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_DEAL_CANCEL)]],
        resize_keyboard=True,
    )


# ── FSM ──────────────────────────────────────────────────────────────────────
class DealFSM(StatesGroup):
    buy_link   = State()
    buy_price  = State()
    sell_price = State()


# ── Хелперы ──────────────────────────────────────────────────────────────────
def _parse_price(text: str) -> int | None:
    """'35000' / '35 000' / '35к' / '35.5к' → int рублей, либо None."""
    t = (text or "").strip().lower().replace(" ", "").replace(" ", "")
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


async def _is_operator(user_id: int) -> bool:
    return user_id == OWNER_ID or await db.is_reseller(user_id)


async def _back_menu(msg: Message):
    """Меню реселлера — либо ничего для владельца (он на командах)."""
    if await db.is_reseller(msg.from_user.id) and msg.from_user.id != OWNER_ID:
        await msg.answer("Готово.", reply_markup=reseller_menu())


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
    data = await state.get_data()
    url = data.get("url", "")
    await state.clear()
    deal = await db.add_deal(url, price, reseller_id=msg.from_user.id)

    if deal["attributed"]:
        title = deal.get("title") or _short(deal)
        head = f"✅ <b>Через бота</b> — при продаже засчитаю {int(FEE_RATE*100)}%\n<i>{title}</i>"
    else:
        head = "➖ <b>Не через бота</b> — 5% не берётся"
    await msg.answer(
        f"{head}\n\nСделка <b>#{deal['id']}</b> · закуп {_money(price)}\n"
        f"Как продашь — жми <b>{BTN_DEAL_SELL}</b>.",
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
        label = f"#{d['id']} {mark} {_money(d['buy_price'])} · {_short(d)}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"sell:{d['id']}")])
    await msg.answer(
        "Выбери проданную сделку:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


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

    # Уведомление владельцу по каждой ботовской продаже.
    if sold["attributed"]:
        try:
            await msg.bot.send_message(
                OWNER_ID,
                f"🟢 <b>Продажа через бота</b> · сделка #{deal_id}\n"
                f"{sold.get('url') or _short(sold)}\n"
                f"Закуп {_money(sold['buy_price'])} → продажа {_money(price)}\n"
                f"Маржа {_money(margin)} · <b>твои {int(FEE_RATE*100)}%: {_money(fee)}</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"owner notify failed for deal {deal_id}: {e}")


# ── Статистика реселлера ──────────────────────────────────────────────────────
async def _reseller_stats(msg: Message, state: FSMContext):
    if not await _is_operator(msg.from_user.id):
        return
    r = await db.get_reseller_report(msg.from_user.id)
    await msg.answer(
        "📊 <b>Твоя статистика</b>\n\n"
        f"Открыто: <b>{r.get('open_cnt', 0)}</b>\n"
        f"Продано: <b>{r.get('sold_cnt', 0)}</b> "
        f"(из них через бота: <b>{r.get('bot_sold', 0)}</b>)\n"
        f"Суммарная маржа: <b>{_money(r.get('margin', 0))}</b>\n"
        f"Доля владельца (5%): <b>{_money(r.get('fee', 0))}</b>",
        parse_mode="HTML", reply_markup=reseller_menu(),
    )


# ── Отмена ────────────────────────────────────────────────────────────────────
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
    period = f"за {days} дн." if days else "за всё время"
    lines = [
        f"💼 <b>Твои 5% {period}</b>\n",
        f"Ботовских продаж: <b>{rep['count']}</b>",
        f"Суммарная маржа: <b>{_money(rep['margin'])}</b>",
        f"Твоя доля: <b>{_money(rep['fee'])}</b>",
    ]
    if rep["deals"]:
        lines.append("\n<b>Последние:</b>")
        for d in rep["deals"][:10]:
            lines.append(
                f"#{d['id']} · {_money(d['buy_price'])}→{_money(d['sell_price'])} "
                f"· 5% {_money(d['fee'])}"
            )
    await msg.answer("\n".join(lines), parse_mode="HTML")


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
        await msg.bot.send_message(
            target,
            "🧾 Тебе выдан доступ к учёту сделок. Нажми /start.",
        )
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
    # Отмена и кнопки меню — до state-хэндлеров, чтобы работали в любой момент.
    dp.message.register(_cancel, F.text == BTN_DEAL_CANCEL)
    dp.message.register(_cancel, Command("cancel"))

    dp.message.register(_start_buy,      F.text == BTN_DEAL_BUY)
    dp.message.register(_start_sell,     F.text == BTN_DEAL_SELL)
    dp.message.register(_reseller_stats, F.text == BTN_DEAL_STATS)

    # Команды владельца
    dp.message.register(_cmd_report,       Command("report"))
    dp.message.register(_cmd_reseller_add, Command("reseller_add"))
    dp.message.register(_cmd_reseller_del, Command("reseller_del"))
    dp.message.register(_cmd_resellers,    Command("resellers"))

    # Выбор сделки для продажи
    dp.callback_query.register(_cb_pick_sell, F.data.startswith("sell:"))

    # State-хэндлеры (после кнопок)
    dp.message.register(_got_link,       DealFSM.buy_link)
    dp.message.register(_got_buy_price,  DealFSM.buy_price)
    dp.message.register(_got_sell_price, DealFSM.sell_price)
