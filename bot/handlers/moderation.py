"""Модерация черновиков: карточка админу с кнопками ✅/❌ и публикация в группу."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.db.models import Draft, DraftNotification, NewsStatus, PostedNews
from bot.db.session import async_session_factory
from bot.filters.admin import IsAdmin

logger = logging.getLogger(__name__)

router = Router(name="moderation")
router.callback_query.filter(IsAdmin())

# Лимит Telegram на длину caption у фото-сообщения
_CAPTION_LIMIT = 1024


class DraftCallback(CallbackData, prefix="draft"):
    action: str  # "approve" | "reject"
    news_id: int


def _keyboard(news_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Опубликовать",
                    callback_data=DraftCallback(action="approve", news_id=news_id).pack(),
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=DraftCallback(action="reject", news_id=news_id).pack(),
                ),
            ]
        ]
    )


def _format_admin_text(draft: Draft) -> str:
    body = html.escape(draft.text, quote=False)
    link = html.escape(draft.source_link, quote=False)
    return f"{body}\n\n🔗 Источник: {link}"


async def send_draft_for_moderation(bot: Bot, news_id: int) -> None:
    """Рассылает карточку черновика с кнопками модерации всем админам
    (ADMIN_CHAT_IDS) и запоминает id каждого отправленного сообщения — это
    нужно, чтобы после approve/reject убрать кнопки у всех копий сразу."""
    settings = get_settings()
    async with async_session_factory() as session:
        draft = (
            await session.execute(select(Draft).where(Draft.news_id == news_id))
        ).scalar_one()

        text = _format_admin_text(draft)
        keyboard = _keyboard(news_id)
        use_photo = bool(draft.photo_url) and len(text) <= _CAPTION_LIMIT

        for admin_chat_id in settings.admin_ids:
            message = None
            if use_photo:
                try:
                    message = await bot.send_photo(
                        chat_id=admin_chat_id,
                        photo=draft.photo_url,
                        caption=text,
                        reply_markup=keyboard,
                    )
                except TelegramBadRequest:
                    logger.warning(
                        "Не удалось отправить фото черновика админу %s (news_id=%s), отправляю текстом",
                        admin_chat_id,
                        news_id,
                    )

            if message is None:
                try:
                    message = await bot.send_message(
                        chat_id=admin_chat_id,
                        text=text,
                        reply_markup=keyboard,
                    )
                except TelegramBadRequest:
                    # Например, админ ни разу не писал боту в личку ("chat not
                    # found") — пропускаем его, чтобы не терять уведомления
                    # остальных админов и не срывать commit ниже.
                    logger.error(
                        "Не удалось отправить черновик админу %s (news_id=%s) — "
                        "ни фото, ни текстом. Возможно, он не открывал чат с ботом.",
                        admin_chat_id,
                        news_id,
                    )
                    continue

            session.add(
                DraftNotification(
                    draft_id=draft.id, admin_chat_id=admin_chat_id, message_id=message.message_id
                )
            )

        await session.commit()


async def _publish_to_group(bot: Bot, draft: Draft) -> None:
    settings = get_settings()
    # У бота parse_mode=HTML по умолчанию, а текст приходит от нейросети по
    # материалам сторонних сайтов — без экранирования символы <, >, & ломали
    # бы отправку или превращались в разметку, которой админ в карточке не видел.
    text = html.escape(draft.text, quote=False)
    if draft.photo_url:
        try:
            await bot.send_photo(
                chat_id=settings.target_group_chat_id,
                message_thread_id=settings.target_topic_id,
                photo=draft.photo_url,
                caption=text,
            )
            return
        except TelegramBadRequest:
            logger.warning(
                "Не удалось опубликовать фото (news_id=%s), публикую текстом", draft.news_id
            )

    await bot.send_message(
        chat_id=settings.target_group_chat_id,
        message_thread_id=settings.target_topic_id,
        text=text,
    )


async def _resolve_notifications(bot: Bot, draft_id: int, status_note: str) -> None:
    """Убирает кнопки со всех разосланных админам копий черновика и оставляет
    под каждой короткую пометку о результате (важно при нескольких админах —
    остальные видят, что черновик уже обработан и кем)."""
    async with async_session_factory() as session:
        notifications = (
            await session.execute(
                select(DraftNotification).where(DraftNotification.draft_id == draft_id)
            )
        ).scalars().all()

    for notif in notifications:
        try:
            await bot.edit_message_reply_markup(
                chat_id=notif.admin_chat_id, message_id=notif.message_id, reply_markup=None
            )
        except TelegramBadRequest:
            pass
        try:
            await bot.send_message(
                chat_id=notif.admin_chat_id,
                text=status_note,
                reply_to_message_id=notif.message_id,
            )
        except TelegramBadRequest:
            pass


async def _lock_news(session: AsyncSession, news_id: int) -> PostedNews | None:
    """Читает новость с блокировкой строки (SELECT ... FOR UPDATE) до конца
    транзакции. Без неё два админа, нажавшие кнопку одновременно, оба видели
    статус PENDING и черновик публиковался дважды. Теперь второй ждёт, пока
    первый закончит, и получает уже обновлённый статус."""
    return await session.get(PostedNews, news_id, with_for_update=True)


def _actor_name(query: CallbackQuery) -> str:
    name = query.from_user.full_name if query.from_user else "неизвестно"
    return html.escape(name, quote=False)


@router.callback_query(DraftCallback.filter(F.action == "approve"))
async def cb_approve(query: CallbackQuery, callback_data: DraftCallback, bot: Bot) -> None:
    async with async_session_factory() as session:
        news = await _lock_news(session, callback_data.news_id)
        draft = (
            await session.execute(
                select(Draft).where(Draft.news_id == callback_data.news_id)
            )
        ).scalar_one_or_none()

        if news is None or draft is None:
            await query.answer("Черновик не найден.", show_alert=True)
            return
        if news.status != NewsStatus.PENDING:
            await query.answer("Уже обработано.", show_alert=True)
            return

        try:
            await _publish_to_group(bot, draft)
        except TelegramBadRequest as exc:
            logger.error("Публикация в группу не удалась (news_id=%s): %s", news.id, exc.message)
            await query.answer(f"⚠️ Не удалось опубликовать: {exc.message}", show_alert=True)
            return

        news.status = NewsStatus.POSTED
        news.posted_at = datetime.now(timezone.utc)
        draft_id = draft.id
        await session.commit()

    await query.answer("Опубликовано ✅")
    await _resolve_notifications(bot, draft_id, f"✅ Опубликовано в группу ({_actor_name(query)}).")


@router.callback_query(DraftCallback.filter(F.action == "reject"))
async def cb_reject(query: CallbackQuery, callback_data: DraftCallback, bot: Bot) -> None:
    async with async_session_factory() as session:
        news = await _lock_news(session, callback_data.news_id)
        draft = (
            await session.execute(
                select(Draft).where(Draft.news_id == callback_data.news_id)
            )
        ).scalar_one_or_none()
        if news is None or draft is None:
            await query.answer("Черновик не найден.", show_alert=True)
            return
        if news.status != NewsStatus.PENDING:
            await query.answer("Уже обработано.", show_alert=True)
            return

        news.status = NewsStatus.REJECTED
        draft_id = draft.id
        await session.commit()

    await query.answer("Отклонено ❌")
    await _resolve_notifications(bot, draft_id, f"❌ Черновик отклонён ({_actor_name(query)}).")
