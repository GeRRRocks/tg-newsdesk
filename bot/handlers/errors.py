"""Последний рубеж: исключение, которое не поймал ни один обработчик.

Без этого нажатие на кнопку просто «не срабатывало», а ошибка оставалась
только в логах сервера. Теперь админ, нажавший кнопку, сразу видит, что
что-то сломалось, и все админы получают сообщение с сутью ошибки."""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import ErrorEvent

from bot.i18n import t
from bot.services.alerts import report_error

logger = logging.getLogger(__name__)

router = Router(name="errors")


@router.errors()
async def on_unhandled_error(event: ErrorEvent, bot: Bot) -> bool:
    exc = event.exception
    query = event.update.callback_query

    # Повторное нажатие той же кнопки: Telegram отказывается «менять»
    # сообщение на такое же. Это не поломка — молча закрываем нажатие.
    if isinstance(exc, TelegramBadRequest) and "message is not modified" in str(exc):
        if query is not None:
            try:
                await query.answer()
            except TelegramAPIError:
                pass
        return True

    # Нажатие пришло с опозданием (обычно после обрыва связи с Telegram):
    # ответить на него уже нельзя, но само действие выполнено. Не поломка.
    if isinstance(exc, TelegramBadRequest) and "query is too old" in str(exc):
        logger.warning("Ответ на нажатие опоздал: %s", exc.message)
        return True

    logger.error("Необработанная ошибка в обработчике", exc_info=exc)
    if query is not None:
        try:
            await query.answer(t("err.query"), show_alert=True)
        except TelegramAPIError:
            pass
    await report_error(bot, exc, t("err.where_handler"))
    return True
