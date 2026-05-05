import asyncio
import logging
import os
from dotenv import load_dotenv

from aiogram import Bot
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

    interval = int(os.getenv("CHECK_INTERVAL", "15"))

    await init_db()

    bot, dp = make_bot(token)
    monitor = Monitor(bot=bot, interval=interval)

    await monitor.start()
    logger.info("Bot started. Press Ctrl+C to stop.")

    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await monitor.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
