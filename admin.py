import asyncio
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db
from database import OWNER_IDS, TRIAL_DAYS, SUBSCRIPTION_DAYS

logger = logging.getLogger(__name__)

_monitor = None


def set_monitor(monitor) -> None:
    global _monitor
    _monitor = monitor


class AdminState(StatesGroup):
    broadcast = State()
    give = State()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compute_plan(user: dict) -> str:
    now = datetime.now(timezone.utc)
    if user.get("sub_expires_at"):
        exp = datetime.fromisoformat(user["sub_expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp > now:
            return "paid"
    if user.get("trial_started_at"):
        started = datetime.fromisoformat(user["trial_started_at"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        bonus = user.get("trial_bonus_days") or 0
        if (now - started).days < (TRIAL_DAYS + bonus):
            return "trial"
    return "free"


async def _stats_text() -> str:
    stats = await db.get_stats()
    uptime_sec = int(time.time() - (_monitor._start_time if _monitor else 0))
    h, rem = divmod(uptime_sec, 3600)
    m, s = divmod(rem, 60)
    blocks = _monitor._blocks_today if _monitor else 0
    sent = _monitor._sent_today if _monitor else 0
    mult = _monitor._delay_mult if _monitor else 1.0
    parser_ok = (any(_monitor._parsers_started) if _monitor else False)
    return (
        "🔧 <b>Панель администратора</b>\n\n"
        f"👥 Пользователей: <b>{stats['total_users']}</b>  (платных: <b>{stats['paid_users']}</b>)\n"
        f"🔍 Активных поисков: <b>{stats['active_watches']}</b>\n"
        f"📨 Отправлено сегодня: <b>{sent}</b>\n"
        f"🔎 Найдено сегодня: <b>{stats['seen_today']}</b>\n"
        f"🚫 Блокировок сегодня: <b>{blocks}</b>  "
        f"{'(задержки ×' + str(mult) + ')' if mult > 1 else ''}\n"
        f"⚙️ Парсер: <b>{'✅ работает' if parser_ok else '❌ остановлен'}</b>\n"
        f"⏱ Аптайм: <b>{h}ч {m}м {s}с</b>"
    )


def _admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:stats"),
            InlineKeyboardButton(text="👥 Юзеры", callback_data="adm:users:0"),
        ],
        [
            InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:broadcast"),
            InlineKeyboardButton(text="🎁 Выдать дни", callback_data="adm:give"),
        ],
        [InlineKeyboardButton(text="🔄 Перезапустить парсер", callback_data="adm:restart")],
        [InlineKeyboardButton(text="⬇️ Git Pull + перезапуск", callback_data="adm:gitpull")],
    ])


# ── /admin ────────────────────────────────────────────────────────────────────

async def _cmd_admin(msg: Message):
    if msg.from_user.id not in OWNER_IDS:
        return
    await msg.answer(await _stats_text(), parse_mode="HTML", reply_markup=_admin_kb())


# ── Callbacks ─────────────────────────────────────────────────────────────────

async def _cb_stats(cb: CallbackQuery):
    if cb.from_user.id not in OWNER_IDS:
        return await cb.answer()
    try:
        await cb.message.edit_text(await _stats_text(), parse_mode="HTML", reply_markup=_admin_kb())
    except Exception:
        pass
    await cb.answer("Обновлено")


async def _cb_users(cb: CallbackQuery):
    if cb.from_user.id not in OWNER_IDS:
        return await cb.answer()

    page = int(cb.data.split(":")[-1])
    users = await db.get_all_users()
    per_page = 12
    total_pages = max(1, (len(users) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = users[page * per_page: (page + 1) * per_page]

    plan_icon = {"paid": "💎", "trial": "🎁", "free": "🔒"}
    lines = [f"👥 <b>Пользователи ({len(users)})</b>  стр. {page + 1}/{total_pages}\n"]
    for u in chunk:
        plan = _compute_plan(u)
        icon = plan_icon.get(plan, "?")
        paused = " ⏸" if u.get("is_paused") else ""
        watches = u.get("watch_count", 0)
        lines.append(f"{icon}{paused} <code>{u['user_id']}</code> — {watches} пос.")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm:users:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm:users:{page + 1}"))

    rows = []
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm:stats")])

    try:
        await cb.message.edit_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except Exception:
        pass
    await cb.answer()


async def _cb_broadcast(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in OWNER_IDS:
        return await cb.answer()
    await state.set_state(AdminState.broadcast)
    try:
        await cb.message.edit_reply_markup()
    except Exception:
        pass
    await cb.message.answer(
        "📢 <b>Рассылка</b>\n\nНапиши текст (HTML поддерживается).\n/cancel — отмена",
        parse_mode="HTML",
    )
    await cb.answer()


async def _handle_broadcast(msg: Message, state: FSMContext):
    if msg.from_user.id not in OWNER_IDS:
        return
    if msg.text and msg.text.strip() == "/cancel":
        await state.clear()
        await msg.answer("Отменено.")
        return
    if not msg.text:
        await msg.answer("Рассылка — только текстом. Пришли текст или /cancel.")
        return
    await state.clear()
    users = await db.get_all_users()
    ok, fail = 0, 0
    status = await msg.answer(f"📤 Рассылаю {len(users)} пользователям...")
    for u in users:
        try:
            await msg.bot.send_message(chat_id=u["user_id"], text=msg.text, parse_mode="HTML")
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    try:
        await status.edit_text(f"✅ Доставлено: {ok}\n❌ Не доставлено: {fail}")
    except Exception:
        await msg.answer(f"✅ Доставлено: {ok}\n❌ Не доставлено: {fail}")


async def _cb_give(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in OWNER_IDS:
        return await cb.answer()
    await state.set_state(AdminState.give)
    try:
        await cb.message.edit_reply_markup()
    except Exception:
        pass
    await cb.message.answer(
        "🎁 <b>Выдать подписку</b>\n\n"
        "Введи <code>user_id количество_дней</code>\n"
        "Пример: <code>123456789 7</code>\n\n/cancel — отмена",
        parse_mode="HTML",
    )
    await cb.answer()


async def _handle_give(msg: Message, state: FSMContext):
    if msg.from_user.id not in OWNER_IDS:
        return
    if msg.text and msg.text.strip() == "/cancel":
        await state.clear()
        await msg.answer("Отменено.")
        return
    parts = (msg.text or "").strip().split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await msg.answer("❌ Формат: <code>user_id дни</code>", parse_mode="HTML")
        return
    user_id, days = int(parts[0]), int(parts[1])
    await state.clear()
    await db.ensure_user(user_id)
    expires = await db.activate_subscription(user_id, days)
    expires_str = expires.strftime("%d.%m.%Y")
    try:
        await msg.bot.send_message(
            chat_id=user_id,
            text=f"🎁 <b>Тебе выдана подписка на {days} дней!</b>\nДействует до {expires_str}.",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await msg.answer(f"✅ Пользователю <code>{user_id}</code> выдано {days} дней. Истекает {expires_str}.", parse_mode="HTML")


async def _cb_restart(cb: CallbackQuery):
    if cb.from_user.id not in OWNER_IDS:
        return await cb.answer()
    await cb.answer("Перезапускаю...", show_alert=False)
    if _monitor:
        try:
            for i, parser in enumerate(_monitor._parsers):
                if _monitor._parsers_started[i]:
                    await parser.stop()
                    _monitor._parsers_started[i] = False
            for i, parser in enumerate(_monitor._parsers):
                await parser.start()
                _monitor._parsers_started[i] = True
            await cb.message.answer("✅ Парсеры перезапущены.")
        except Exception as e:
            await cb.message.answer(f"❌ Ошибка: {e}")
    else:
        await cb.message.answer("❌ Монитор не найден.")


async def _cb_gitpull(cb: CallbackQuery):
    if cb.from_user.id not in OWNER_IDS:
        return await cb.answer()
    await cb.answer("Подтягиваю...", show_alert=False)
    await cb.message.answer("⬇️ Запускаю git pull...")
    try:
        cwd = os.path.dirname(os.path.abspath(__file__))
        result = subprocess.run(
            ["git", "fetch", "origin"],
            capture_output=True, text=True, cwd=cwd,
        )
        result2 = subprocess.run(
            ["git", "reset", "--hard", "origin/main"],
            capture_output=True, text=True, cwd=cwd,
        )
        out = (result.stdout + result.stderr + result2.stdout + result2.stderr).strip()
        await cb.message.answer(f"✅ Готово:\n<code>{out}</code>\n\nПерезапускаюсь...", parse_mode="HTML")
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка: {e}")
        return
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ── Register ──────────────────────────────────────────────────────────────────

def register_admin_handlers(dp: Dispatcher):
    dp.message.register(_cmd_admin, Command("admin"))
    dp.callback_query.register(_cb_stats,     F.data == "adm:stats")
    dp.callback_query.register(_cb_users,     F.data.startswith("adm:users:"))
    dp.callback_query.register(_cb_broadcast, F.data == "adm:broadcast")
    dp.callback_query.register(_cb_give,      F.data == "adm:give")
    dp.callback_query.register(_cb_restart,  F.data == "adm:restart")
    dp.callback_query.register(_cb_gitpull, F.data == "adm:gitpull")
    dp.message.register(_handle_broadcast, AdminState.broadcast)
    dp.message.register(_handle_give,      AdminState.give)
