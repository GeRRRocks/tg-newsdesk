from aiogram import Router

from bot.handlers.ai import router as ai_router
from bot.handlers.base import router as base_router
from bot.handlers.generate import router as generate_router
from bot.handlers.moderation import router as moderation_router
from bot.handlers.prompt import router as prompt_router
from bot.handlers.schedule import router as schedule_router
from bot.handlers.sources import router as sources_router


def get_routers() -> list[Router]:
    """Список роутеров для подключения к диспетчеру. Пополняется по мере добавления модулей."""
    return [
        base_router,
        sources_router,
        generate_router,
        schedule_router,
        prompt_router,
        ai_router,
        moderation_router,
    ]
