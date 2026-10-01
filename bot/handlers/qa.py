"""Ответы на вопросы участников группы: сообщение с упоминанием бота и меткой
«Вопрос:» / «Question:» уходит в нейросеть, ответ публикуется реплаем.

Единственный обработчик, доступный не только админам. Поэтому он работает
лишь в целевой группе (при заданном QA_TOPIC_ID — в одном её топике), выключен по умолчанию (QA_ENABLED) и считает попытки —
см. bot/services/qa.py. Админы под лимит не попадают."""

from __future__ import annotations

import html
import logging
import re
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from bot.config import get_settings
from bot.i18n import t
from bot.services import qa
from bot.services.ai import answer_question

logger = logging.getLogger(__name__)

router = Router(name="qa")

_MAX_QUESTION_LEN = 500
# Запас до лимита Telegram на сообщение (4096)
_MAX_ANSWER_LEN = 3500
_LIMIT_MESSAGES = {"user": "qa.limit_user", "global": "qa.limit_global"}


_MARKER = re.compile(rf"(?:{'|'.join(qa.QUESTION_MARKERS)})\s*:\s*", re.IGNORECASE)


def extract_question(text: str, username: str) -> str | None:
    """Текст вопроса без упоминания бота и метки «Вопрос:». None — сообщение
    не вопрос боту: его не упомянули или текст не начинается с метки."""
    mention = re.compile(rf"(?<![\w@])@{re.escape(username)}(?!\w)", re.IGNORECASE)
    if not mention.search(text):
        return None
    rest = " ".join(mention.sub(" ", text).split())
    marker = _MARKER.match(rest)
    if marker is None:
        return None
    return rest[marker.end() :]


async def _reply(message: Message, text: str) -> None:
    try:
        await message.reply(text)
    except TelegramAPIError as exc:
        # Например, вопрос успели удалить — отвечать уже некому
        logger.warning("Не удалось ответить на вопрос в группе: %s", exc)


@router.message(F.chat.id == get_settings().target_group_chat_id, F.text | F.caption)
async def on_group_message(message: Message, bot: Bot) -> None:
    settings = get_settings()
    user = message.from_user
    if not settings.qa_enabled or user is None or user.is_bot:
        return
    if settings.qa_topic_id is not None:
        # message_thread_id бывает и у обычных реплаев вне топиков
        thread_id = message.message_thread_id if message.is_topic_message else None
        if thread_id != settings.qa_topic_id:
            return
    me = await bot.me()
    question = extract_question(message.text or message.caption or "", me.username or "")
    if not question:
        return
    if len(question) > _MAX_QUESTION_LEN:
        await _reply(message, t("qa.too_long", limit=_MAX_QUESTION_LEN))
        return

    limited = user.id not in settings.admin_ids
    if limited:
        reservation = await qa.reserve(user.id)
        if not reservation.allowed:
            if reservation.notify:
                resets = reservation.resets_at.astimezone(ZoneInfo(settings.timezone))
                await _reply(
                    message,
                    t(_LIMIT_MESSAGES[reservation.scope], time=resets.strftime("%d.%m %H:%M")),
                )
            return

    try:
        answer = await answer_question(question)
    except Exception:
        if limited:
            await qa.release(user.id)
        raise
    if answer is None:
        if limited:
            await qa.release(user.id)
        await _reply(message, t("qa.failed"))
        return
    if len(answer) > _MAX_ANSWER_LEN:
        answer = answer[: _MAX_ANSWER_LEN - 1] + "…"
    await _reply(message, html.escape(answer, quote=False))
