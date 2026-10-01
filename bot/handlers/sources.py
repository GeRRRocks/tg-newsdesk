"""Управление источниками новостей: список/добавление/переключение/удаление
через кнопки. Добавление URL — единственный шаг, где всё равно нужен текст
(адрес нельзя выбрать кнопкой), дальше это диалог на FSM с кнопками."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import NewsStatus, PostedNews, Source, SourceType
from bot.db.session import async_session_factory
from bot.filters.admin import IsAdmin
from bot.handlers.base import MenuCallback, main_menu_keyboard

router = Router(name="sources")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AddSourceStates(StatesGroup):
    waiting_url = State()
    waiting_type = State()
    waiting_name = State()


class SourceCallback(CallbackData, prefix="src"):
    action: str  # "view" | "toggle" | "remove"
    source_id: int


class AddSourceTypeCallback(CallbackData, prefix="srctype"):
    source_type: str  # "rss" | "html"


class CancelWizardCallback(CallbackData, prefix="srccancel"):
    pass


class SkipNameCallback(CallbackData, prefix="srcskipname"):
    pass


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=CancelWizardCallback().pack())]
        ]
    )


def _type_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="RSS",
                    callback_data=AddSourceTypeCallback(source_type=SourceType.RSS.value).pack(),
                ),
                InlineKeyboardButton(
                    text="HTML",
                    callback_data=AddSourceTypeCallback(source_type=SourceType.HTML.value).pack(),
                ),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=CancelWizardCallback().pack())],
        ]
    )


def _skip_name_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏭ Пропустить", callback_data=SkipNameCallback().pack()),
                InlineKeyboardButton(text="❌ Отмена", callback_data=CancelWizardCallback().pack()),
            ]
        ]
    )


def _source_button_label(idx: int, source: Source) -> str:
    label = source.name or source.url
    if len(label) > 28:
        label = label[:27] + "…"
    suffix = " (выкл)" if not source.is_active else ""
    return f"{idx}. {label}{suffix}"


def _sources_list_keyboard(sources: list[Source]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=_source_button_label(idx, source),
                callback_data=SourceCallback(action="view", source_id=source.id).pack(),
            )
        ]
        for idx, source in enumerate(sources, start=1)
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Добавить", callback_data=MenuCallback(action="add_source").pack()
            ),
            InlineKeyboardButton(text="⬅️ Меню", callback_data=MenuCallback(action="main").pack()),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _stats_line(counts: dict[NewsStatus, int]) -> str:
    line = (
        f"Опубликовано {counts.get(NewsStatus.POSTED, 0)} · "
        f"Отклонено {counts.get(NewsStatus.REJECTED, 0)} · "
        f"Ждут {counts.get(NewsStatus.PENDING, 0)}"
    )
    expired = counts.get(NewsStatus.EXPIRED, 0)
    return f"{line} · Истекло {expired}" if expired else line


async def _status_counts(session: AsyncSession, source_id: int | None = None) -> dict[NewsStatus, int]:
    """Сколько черновиков в каждом статусе: по одному источнику или по всем
    (включая новости уже удалённых источников)."""
    stmt = select(PostedNews.status, func.count()).group_by(PostedNews.status)
    if source_id is not None:
        stmt = stmt.where(PostedNews.source_id == source_id)
    return {status: count for status, count in (await session.execute(stmt)).all()}


def _source_detail_text(source: Source, counts: dict[NewsStatus, int]) -> str:
    status = "включён ▶️" if source.is_active else "выключен ⏸"
    # Название и URL вводит админ — экранируем, т.к. у бота parse_mode=HTML
    name_line = f"\nНазвание: {html.escape(source.name, quote=False)}" if source.name else ""
    type_label = "RSS-фид" if source.source_type == SourceType.RSS else "HTML-страница"
    return (
        f"Тип: {type_label}{name_line}\nURL: {html.escape(source.url, quote=False)}\n"
        f"Статус: {status}\n\n📊 Черновики: {_stats_line(counts)}"
    )


def _source_detail_keyboard(source: Source) -> InlineKeyboardMarkup:
    toggle_text = "⏸ Выключить" if source.is_active else "▶️ Включить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=toggle_text,
                    callback_data=SourceCallback(action="toggle", source_id=source.id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=SourceCallback(action="remove", source_id=source.id).pack(),
                )
            ],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data=MenuCallback(action="sources").pack())],
        ]
    )


async def _render_sources_list(query: CallbackQuery) -> None:
    async with async_session_factory() as session:
        sources = (await session.execute(select(Source).order_by(Source.id))).scalars().all()
        counts = await _status_counts(session)

    if query.message is None:
        return
    if sources:
        await query.message.edit_text(
            f"📋 Источники (нажми, чтобы открыть):\n\n📊 Всего черновиков: {_stats_line(counts)}",
            reply_markup=_sources_list_keyboard(sources),
        )
    else:
        await query.message.edit_text("Источников пока нет.", reply_markup=main_menu_keyboard())


@router.callback_query(MenuCallback.filter(F.action == "sources"))
async def cb_list_sources(query: CallbackQuery) -> None:
    await _render_sources_list(query)
    await query.answer()


@router.callback_query(SourceCallback.filter(F.action == "view"))
async def cb_view_source(query: CallbackQuery, callback_data: SourceCallback) -> None:
    async with async_session_factory() as session:
        source = await session.get(Source, callback_data.source_id)
        counts = await _status_counts(session, callback_data.source_id)

    if source is None:
        await query.answer("Источник не найден — возможно, уже удалён.", show_alert=True)
        await _render_sources_list(query)
        return

    if query.message is not None:
        await query.message.edit_text(
            _source_detail_text(source, counts), reply_markup=_source_detail_keyboard(source)
        )
    await query.answer()


@router.callback_query(SourceCallback.filter(F.action == "toggle"))
async def cb_toggle_source(query: CallbackQuery, callback_data: SourceCallback) -> None:
    async with async_session_factory() as session:
        source = await session.get(Source, callback_data.source_id)
        if source is None:
            await query.answer("Источник уже удалён.", show_alert=True)
            await _render_sources_list(query)
            return
        source.is_active = not source.is_active
        await session.commit()
        await session.refresh(source)
        is_active = source.is_active
        text = _source_detail_text(source, await _status_counts(session, source.id))
        keyboard = _source_detail_keyboard(source)

    if query.message is not None:
        await query.message.edit_text(text, reply_markup=keyboard)
    await query.answer("Включён ▶️" if is_active else "Выключен ⏸")


@router.callback_query(SourceCallback.filter(F.action == "remove"))
async def cb_remove_source(query: CallbackQuery, callback_data: SourceCallback) -> None:
    async with async_session_factory() as session:
        source = await session.get(Source, callback_data.source_id)
        if source is None:
            await query.answer("Источник уже удалён.", show_alert=True)
            await _render_sources_list(query)
            return
        await session.delete(source)
        await session.commit()

    await query.answer("Удалено")
    await _render_sources_list(query)


@router.callback_query(MenuCallback.filter(F.action == "add_source"))
async def cb_start_add_source(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddSourceStates.waiting_url)
    if query.message is not None:
        await query.message.answer(
            "Пришли URL источника (должен начинаться с http:// или https://).",
            reply_markup=_cancel_keyboard(),
        )
    await query.answer()


@router.message(AddSourceStates.waiting_url)
async def add_source_got_url(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not url.startswith(("http://", "https://")):
        await message.answer(
            "URL должен начинаться с http:// или https://. Пришли ещё раз.",
            reply_markup=_cancel_keyboard(),
        )
        return

    await state.update_data(url=url)
    await state.set_state(AddSourceStates.waiting_type)
    await message.answer("Тип источника?", reply_markup=_type_choice_keyboard())


@router.callback_query(AddSourceStates.waiting_type, AddSourceTypeCallback.filter())
async def add_source_got_type(
    query: CallbackQuery, callback_data: AddSourceTypeCallback, state: FSMContext
) -> None:
    await state.update_data(source_type=callback_data.source_type)
    await state.set_state(AddSourceStates.waiting_name)
    if query.message is not None:
        await query.message.edit_text(
            "Название источника? Пришли текстом или нажми «Без названия».",
            reply_markup=_skip_name_keyboard(),
        )
    await query.answer()


async def _create_source(target: Message, url: str, source_type: str, name: str | None) -> None:
    async with async_session_factory() as session:
        source = Source(url=url, source_type=SourceType(source_type), name=name)
        session.add(source)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            await target.answer("⚠️ Такой источник уже добавлен.", reply_markup=main_menu_keyboard())
            return

    type_label = "RSS-фид" if source_type == SourceType.RSS.value else "HTML-страница"
    name_line = f"\nНазвание: {html.escape(name, quote=False)}" if name else ""
    await target.answer(
        f"✅ Источник добавлен (id={source.id})\nТип: {type_label}\n"
        f"URL: {html.escape(url, quote=False)}{name_line}",
        reply_markup=main_menu_keyboard(),
    )


@router.message(AddSourceStates.waiting_name)
async def add_source_got_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    name = (message.text or "").strip() or None
    await _create_source(message, url=data["url"], source_type=data["source_type"], name=name)


@router.callback_query(AddSourceStates.waiting_name, SkipNameCallback.filter())
async def add_source_skip_name(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if query.message is not None:
        await _create_source(query.message, url=data["url"], source_type=data["source_type"], name=None)
    await query.answer()


@router.callback_query(CancelWizardCallback.filter())
async def cb_cancel_wizard(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if query.message is not None:
        await query.message.edit_text("Отменено.")
        await query.message.answer("Выбери действие:", reply_markup=main_menu_keyboard())
    await query.answer()
