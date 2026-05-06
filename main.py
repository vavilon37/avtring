import asyncio
import logging
import os
from dotenv import load_dotenv

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
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


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан в .env файле")

    await init_db()

    session = AiohttpSession()
    session._connector_init["ssl"] = False
    bot, dp = make_bot(token, session=session)
    monitor = Monitor(bot=bot)

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
                logger.info("Жду 15 секунд и переподключаюсь (проверь что Happ запущен)...")
                await asyncio.sleep(15)
    finally:
        await monitor.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
