"""Служебные сообщения админам о сбоях — чтобы о проблеме узнавали из
Telegram, а не из логов сервера."""

from __future__ import annotations

import html
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from bot.config import get_settings
from bot.i18n import t

logger = logging.getLogger(__name__)


async def notify_admins(bot: Bot, text: str) -> None:
    """text — готовый HTML (у бота parse_mode=HTML): внешние строки
    экранирует вызывающий код."""
    for admin_chat_id in get_settings().admin_ids:
        try:
            await bot.send_message(chat_id=admin_chat_id, text=text)
        except TelegramAPIError as exc:
            logger.warning("Не удалось отправить уведомление админу %s: %s", admin_chat_id, exc)


# Не чаще одного сообщения на один вид ошибки за этот срок: сломанная кнопка
# или упавший job иначе засыпали бы админов одинаковыми сообщениями.
ERROR_ALERT_INTERVAL = 600
_last_error_alert: dict[str, float] = {}


async def report_error(bot: Bot, exc: BaseException, where: str) -> None:
    """Сообщает админам о неожиданной ошибке (полный traceback — в логах)."""
    key = f"{where}:{type(exc).__name__}"
    now = time.monotonic()
    last = _last_error_alert.get(key)
    if last is not None and now - last < ERROR_ALERT_INTERVAL:
        return
    _last_error_alert[key] = now
    detail = html.escape(f"{type(exc).__name__}: {exc}"[:300], quote=False)
    await notify_admins(
        bot, t("err.report", where=html.escape(where, quote=False), detail=detail)
    )
