"""Запуск сбора и генерации черновика по кнопке, а не только по расписанию."""

from __future__ import annotations

import time

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from bot.filters.admin import IsAdmin
from bot.handlers.base import MenuCallback, main_menu_keyboard
from bot.i18n import t
from bot.services.scheduler import generate_draft_job

router = Router(name="generate")
router.callback_query.filter(IsAdmin())

# статус generate_draft_job -> ключ текста
_STATUS_MESSAGES = {
    "sent": "gen.sent",
    "no_news": "gen.no_news",
    "generation_failed": "gen.failed",
    "busy": "gen.busy",
}

# Пауза между ручными запусками, общая для всех админов: каждый запуск — это
# платный запрос к нейросети, а случайные повторные нажатия плодят черновики.
_COOLDOWN_SECONDS = 60
_last_manual_run = 0.0


@router.callback_query(MenuCallback.filter(F.action == "generate_now"))
async def cb_generate_now(query: CallbackQuery, bot: Bot) -> None:
    global _last_manual_run
    wait = _COOLDOWN_SECONDS - (time.monotonic() - _last_manual_run)
    if wait > 0:
        await query.answer(t("gen.wait", seconds=int(wait) + 1), show_alert=True)
        return
    _last_manual_run = time.monotonic()

    await query.answer()
    status_message = None
    if query.message is not None:
        # Сбор новости + вызов нейросети может занять до десятка секунд — без
        # видимого статуса выглядит так, будто бот завис.
        status_message = await query.message.answer(t("gen.working"))

    status = await generate_draft_job(bot)

    if status_message is not None:
        await status_message.edit_text(
            t(_STATUS_MESSAGES.get(status, "gen.done")), reply_markup=main_menu_keyboard()
        )
