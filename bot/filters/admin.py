from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

from bot.config import get_settings


class IsAdmin(Filter):
    """Пропускает события от любого из администраторов (ADMIN_CHAT_IDS) —
    личные сообщения боту, команды внутри группы или колбэк от кнопки."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        settings = get_settings()
        return event.from_user is not None and event.from_user.id in settings.admin_ids
