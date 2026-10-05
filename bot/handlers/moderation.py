"""Модерация черновиков: карточка админу с кнопками, публикация в группу,
замена текста (другой вариант от нейросети или свой) и автозакрытие
черновиков, на которые никто не ответил."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.db.models import Draft, DraftNotification, NewsStatus, PostedNews
from bot.db.session import async_session_factory
from bot.filters.admin import IsAdmin
from bot.i18n import t
from bot.services.ai import generate_text

logger = logging.getLogger(__name__)

router = Router(name="moderation")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

# Лимит Telegram на длину caption у фото-сообщения
_CAPTION_LIMIT = 1024
# Свой текст черновика: запас до лимита сообщения (4096) под ссылку на источник
_EDIT_TEXT_LIMIT = 3500

# Новости, для которых прямо сейчас пишется другой вариант — чтобы повторное
# нажатие (в том числе вторым админом) не запускало второй платный запрос.
_regenerating: set[int] = set()


class DraftCallback(CallbackData, prefix="draft"):
    action: str  # "approve" | "reject" | "regen" | "edit"
    news_id: int


def _keyboard(news_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("draft.publish_btn"),
                    callback_data=DraftCallback(action="approve", news_id=news_id).pack(),
                ),
                InlineKeyboardButton(
                    text=t("draft.reject_btn"),
                    callback_data=DraftCallback(action="reject", news_id=news_id).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("draft.regen_btn"),
                    callback_data=DraftCallback(action="regen", news_id=news_id).pack(),
                ),
                InlineKeyboardButton(
                    text=t("draft.edit_btn"),
                    callback_data=DraftCallback(action="edit", news_id=news_id).pack(),
                ),
            ],
        ]
    )


class EditDraftStates(StatesGroup):
    waiting_text = State()


class CancelEditCallback(CallbackData, prefix="draftedit_cancel"):
    pass


def _format_admin_text(draft: Draft) -> str:
    body = html.escape(draft.text, quote=False)
    link = html.escape(draft.source_link, quote=False)
    return t("draft.card", body=body, link=link)


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
    name = query.from_user.full_name if query.from_user else t("common.unknown")
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
            await query.answer(t("draft.not_found"), show_alert=True)
            return
        if news.status != NewsStatus.PENDING:
            await query.answer(t("draft.already_done"), show_alert=True)
            return

        try:
            await _publish_to_group(bot, draft)
        except TelegramBadRequest as exc:
            logger.error("Публикация в группу не удалась (news_id=%s): %s", news.id, exc.message)
            await query.answer(t("draft.publish_failed", error=exc.message), show_alert=True)
            return

        news.status = NewsStatus.POSTED
        news.posted_at = datetime.now(timezone.utc)
        draft_id = draft.id
        await session.commit()

    # Сначала карточки у админов: ответ на нажатие может не дойти (опоздал),
    # и тогда исключение не должно оставить старые кнопки висеть.
    await _resolve_notifications(bot, draft_id, t("draft.published_note", name=_actor_name(query)))
    await query.answer(t("draft.published_toast"))


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
            await query.answer(t("draft.not_found"), show_alert=True)
            return
        if news.status != NewsStatus.PENDING:
            await query.answer(t("draft.already_done"), show_alert=True)
            return

        news.status = NewsStatus.REJECTED
        draft_id = draft.id
        await session.commit()

    # Сначала карточки у админов: ответ на нажатие может не дойти (опоздал),
    # и тогда исключение не должно оставить старые кнопки висеть.
    await _resolve_notifications(bot, draft_id, t("draft.rejected_note", name=_actor_name(query)))
    await query.answer(t("draft.rejected_toast"))


async def _replace_cards(bot: Bot, news_id: int, draft_id: int, note: str) -> None:
    """После замены текста черновика: закрывает старые карточки у всех админов
    и рассылает новые. Править старые на месте нельзя надёжно — карточка могла
    уйти фото с подписью, а новый текст в лимит подписи может не влезть."""
    await _resolve_notifications(bot, draft_id, note)
    async with async_session_factory() as session:
        await session.execute(
            delete(DraftNotification).where(DraftNotification.draft_id == draft_id)
        )
        await session.commit()
    await send_draft_for_moderation(bot, news_id)


async def _set_draft_text(news_id: int, text: str) -> int | None:
    """Под блокировкой строки заменяет текст черновика, если он всё ещё ждёт
    решения. Возвращает id черновика или None, если его уже обработали."""
    async with async_session_factory() as session:
        news = await _lock_news(session, news_id)
        draft = (
            await session.execute(select(Draft).where(Draft.news_id == news_id))
        ).scalar_one_or_none()
        if news is None or draft is None or news.status != NewsStatus.PENDING:
            return None
        draft.text = text
        await session.commit()
        return draft.id


async def _pending_draft(news_id: int) -> tuple[PostedNews, Draft] | None:
    async with async_session_factory() as session:
        news = await session.get(PostedNews, news_id)
        draft = (
            await session.execute(select(Draft).where(Draft.news_id == news_id))
        ).scalar_one_or_none()
    if news is None or draft is None or news.status != NewsStatus.PENDING:
        return None
    return news, draft


@router.callback_query(DraftCallback.filter(F.action == "regen"))
async def cb_regenerate(query: CallbackQuery, callback_data: DraftCallback, bot: Bot) -> None:
    news_id = callback_data.news_id
    if news_id in _regenerating:
        await query.answer(t("draft.regen_busy"), show_alert=True)
        return
    pending = await _pending_draft(news_id)
    if pending is None:
        await query.answer(t("draft.already_done"), show_alert=True)
        return
    news, draft = pending

    _regenerating.add(news_id)
    try:
        await query.answer(t("draft.regen_started"))
        # Запрос к нейросети идёт без блокировки строки: он длится секунды,
        # и держать на это время FOR UPDATE значило бы подвесить остальные кнопки.
        text = await generate_text(news.title, news.summary, previous=draft.text)
        if text is None:
            await bot.send_message(
                chat_id=query.from_user.id,
                text=t("draft.regen_failed"),
            )
            return
        draft_id = await _set_draft_text(news_id, text)
        if draft_id is None:
            # пока шла генерация, черновик опубликовали или отклонили
            return
        await _replace_cards(
            bot, news_id, draft_id, t("draft.regen_note", name=_actor_name(query))
        )
    finally:
        _regenerating.discard(news_id)


@router.callback_query(DraftCallback.filter(F.action == "edit"))
async def cb_edit_start(
    query: CallbackQuery, callback_data: DraftCallback, state: FSMContext
) -> None:
    if await _pending_draft(callback_data.news_id) is None:
        await query.answer(t("draft.already_done"), show_alert=True)
        return
    await state.set_state(EditDraftStates.waiting_text)
    await state.update_data(news_id=callback_data.news_id)
    if query.message is not None:
        await query.message.answer(
            t("draft.edit_ask"),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=t("common.cancel"), callback_data=CancelEditCallback().pack()
                        )
                    ]
                ]
            ),
        )
    await query.answer()


@router.callback_query(CancelEditCallback.filter())
async def cb_edit_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if query.message is not None:
        await query.message.edit_text(t("draft.edit_cancelled"))
    await query.answer()


@router.message(EditDraftStates.waiting_text)
async def msg_edit_text(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer(t("draft.edit_need_text"))
        return
    if len(text) > _EDIT_TEXT_LIMIT:
        await message.answer(t("draft.edit_too_long", length=len(text), limit=_EDIT_TEXT_LIMIT))
        return

    news_id = (await state.get_data()).get("news_id")
    await state.clear()
    draft_id = await _set_draft_text(news_id, text) if news_id is not None else None
    if draft_id is None:
        await message.answer(t("draft.edit_gone"))
        return

    name = (
        html.escape(message.from_user.full_name, quote=False)
        if message.from_user
        else t("common.unknown")
    )
    await _replace_cards(bot, news_id, draft_id, t("draft.edit_note", name=name))


async def expire_stale_drafts(bot: Bot, hours: int) -> int:
    """Закрывает черновики, на которые не ответили за hours часов: статус
    EXPIRED (а не REJECTED — это не решение админа и не должно портить
    статистику источников) и пометка под карточками. Возвращает их число."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with async_session_factory() as session:
        stale_ids = (
            await session.execute(
                select(PostedNews.id).where(
                    PostedNews.status == NewsStatus.PENDING, PostedNews.created_at < cutoff
                )
            )
        ).scalars().all()

    expired = 0
    for news_id in stale_ids:
        async with async_session_factory() as session:
            news = await _lock_news(session, news_id)
            if news is None or news.status != NewsStatus.PENDING:
                continue
            draft_id = (
                await session.execute(select(Draft.id).where(Draft.news_id == news_id))
            ).scalar_one_or_none()
            news.status = NewsStatus.EXPIRED
            await session.commit()
        expired += 1
        if draft_id is not None:
            await _resolve_notifications(bot, draft_id, t("draft.expired_note", hours=hours))
    if expired:
        logger.info("Автозакрытие: закрыто черновиков — %s", expired)
    return expired
