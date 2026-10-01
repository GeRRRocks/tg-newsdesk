import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import get_settings
from bot.db.session import init_models
from bot.handlers import get_routers
from bot.services.ai import describe_provider
from bot.services.scheduler import setup_scheduler

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    settings = get_settings()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    for router in get_routers():
        dp.include_router(router)

    await init_models()
    logger.info("Тексты генерирует: %s", await describe_provider())

    scheduler = await setup_scheduler(bot)
    dp["scheduler"] = scheduler

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot starting polling...")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Bot stopped")
