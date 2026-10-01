"""Служебные сообщения админам о сбоях — чтобы о проблеме узнавали из
Telegram, а не из логов сервера."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from bot.config import get_settings

logger = logging.getLogger(__name__)


async def notify_admins(bot: Bot, text: str) -> None:
    """text — готовый HTML (у бота parse_mode=HTML): внешние строки
    экранирует вызывающий код."""
    for admin_chat_id in get_settings().admin_ids:
        try:
            await bot.send_message(chat_id=admin_chat_id, text=text)
        except TelegramAPIError as exc:
            logger.warning("Не удалось отправить уведомление админу %s: %s", admin_chat_id, exc)
