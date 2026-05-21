import asyncio
import logging
import os
from dotenv import load_dotenv

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
import bot as bot_module
import admin as admin_module
from bot import make_bot
from database import init_db
from monitor import Monitor

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Кольцевой буфер логов для просмотра из админки (после basicConfig).
admin_module.setup_log_buffer()


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан в .env файле")

    await init_db()

    tg_proxy = os.getenv("TG_PROXY")  # socks5://user:pass@host:port
    session = AiohttpSession(proxy=tg_proxy) if tg_proxy else AiohttpSession()
    bot, dp = make_bot(token, session=session)
    monitor = Monitor(bot=bot)

    monitor.register_handlers(dp)
    bot_module.set_monitor(monitor)
    admin_module.set_monitor(monitor)
    admin_module.register_admin_handlers(dp)
    await monitor.start()
    logger.info("Bot started. Press Ctrl+C to stop.")

    try:
        while True:
            try:
                await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
                break  # чистый выход (Ctrl+C)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.warning(f"Polling упал: {e}")
                logger.info("Жду 15 секунд и переподключаюсь...")
                await asyncio.sleep(15)
    finally:
        await monitor.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
