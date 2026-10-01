"""Запуск сбора и генерации черновика по кнопке, а не только по расписанию."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from bot.filters.admin import IsAdmin
from bot.handlers.base import MenuCallback, main_menu_keyboard
from bot.services.scheduler import generate_draft_job

router = Router(name="generate")
router.callback_query.filter(IsAdmin())

_STATUS_MESSAGES = {
    "sent": "✅ Черновик готов и отправлен на модерацию выше.",
    "no_news": "Нет новых новостей ни в одном включённом источнике.",
    "generation_failed": "⚠️ Claude не смог сгенерировать текст — попробуй ещё раз позже.",
}


@router.callback_query(MenuCallback.filter(F.action == "generate_now"))
async def cb_generate_now(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    status_message = None
    if query.message is not None:
        # Сбор новости + вызов Claude может занять до десятка секунд — без
        # видимого статуса выглядит так, будто бот завис.
        status_message = await query.message.answer("🔄 Собираю новость и генерирую черновик…")

    status = await generate_draft_job(bot)

    if status_message is not None:
        await status_message.edit_text(
            _STATUS_MESSAGES.get(status, "Готово."), reply_markup=main_menu_keyboard()
        )
