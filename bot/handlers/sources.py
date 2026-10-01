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
from bot.i18n import t
from bot.services.source_detect import detect_source
from bot.services.telegram_channel import channel_url, channel_username

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
    source_type: str  # "rss" | "html"; Telegram-канал определяется по адресу сам


class CancelWizardCallback(CallbackData, prefix="srccancel"):
    pass


class SkipNameCallback(CallbackData, prefix="srcskipname"):
    pass


# Лимиты колонок: более длинное значение база отклонит
_MAX_URL_LEN = Source.__table__.c.url.type.length
_MAX_NAME_LEN = Source.__table__.c.name.type.length

# тип источника -> ключ его названия
_TYPE_LABELS = {
    SourceType.RSS: "src.type_rss",
    SourceType.HTML: "src.type_html",
    SourceType.TELEGRAM: "src.type_telegram",
}


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("common.cancel"), callback_data=CancelWizardCallback().pack())]
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
            [InlineKeyboardButton(text=t("common.cancel"), callback_data=CancelWizardCallback().pack())],
        ]
    )


def _skip_name_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("src.skip_btn"), callback_data=SkipNameCallback().pack()),
                InlineKeyboardButton(text=t("common.cancel"), callback_data=CancelWizardCallback().pack()),
            ]
        ]
    )


def _source_button_label(idx: int, source: Source) -> str:
    label = source.name or source.url
    if len(label) > 28:
        label = label[:27] + "…"
    suffix = t("src.off_suffix") if not source.is_active else ""
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
                text=t("menu.add"), callback_data=MenuCallback(action="add_source").pack()
            ),
            InlineKeyboardButton(text=t("menu.back"), callback_data=MenuCallback(action="main").pack()),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _stats_line(counts: dict[NewsStatus, int]) -> str:
    line = t(
        "src.stats",
        posted=counts.get(NewsStatus.POSTED, 0),
        rejected=counts.get(NewsStatus.REJECTED, 0),
        pending=counts.get(NewsStatus.PENDING, 0),
    )
    expired = counts.get(NewsStatus.EXPIRED, 0)
    return line + t("src.stats_expired", expired=expired) if expired else line


async def _status_counts(session: AsyncSession, source_id: int | None = None) -> dict[NewsStatus, int]:
    """Сколько черновиков в каждом статусе: по одному источнику или по всем
    (включая новости уже удалённых источников)."""
    stmt = select(PostedNews.status, func.count()).group_by(PostedNews.status)
    if source_id is not None:
        stmt = stmt.where(PostedNews.source_id == source_id)
    return {status: count for status, count in (await session.execute(stmt)).all()}


def _source_detail_text(source: Source, counts: dict[NewsStatus, int]) -> str:
    # Название и URL вводит админ — экранируем, т.к. у бота parse_mode=HTML
    name_line = (
        t("src.name_line", name=html.escape(source.name, quote=False)) if source.name else ""
    )
    return t(
        "src.detail",
        type=t(_TYPE_LABELS[source.source_type]),
        name_line=name_line,
        url=html.escape(source.url, quote=False),
        status=t("src.status_on") if source.is_active else t("src.status_off"),
        stats=_stats_line(counts),
    )


def _source_detail_keyboard(source: Source) -> InlineKeyboardMarkup:
    toggle_text = t("src.disable_btn") if source.is_active else t("src.enable_btn")
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
                    text=t("src.delete_btn"),
                    callback_data=SourceCallback(action="remove", source_id=source.id).pack(),
                )
            ],
            [InlineKeyboardButton(text=t("src.back_btn"), callback_data=MenuCallback(action="sources").pack())],
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
            t("src.list", stats=_stats_line(counts)),
            reply_markup=_sources_list_keyboard(sources),
        )
    else:
        await query.message.edit_text(t("src.empty"), reply_markup=main_menu_keyboard())


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
        await query.answer(t("src.not_found"), show_alert=True)
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
            await query.answer(t("src.already_deleted"), show_alert=True)
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
    await query.answer(t("src.enabled_toast") if is_active else t("src.disabled_toast"))


@router.callback_query(SourceCallback.filter(F.action == "remove"))
async def cb_remove_source(query: CallbackQuery, callback_data: SourceCallback) -> None:
    async with async_session_factory() as session:
        source = await session.get(Source, callback_data.source_id)
        if source is None:
            await query.answer(t("src.already_deleted"), show_alert=True)
            await _render_sources_list(query)
            return
        await session.delete(source)
        await session.commit()

    await query.answer(t("src.deleted_toast"))
    await _render_sources_list(query)


@router.callback_query(MenuCallback.filter(F.action == "add_source"))
async def cb_start_add_source(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddSourceStates.waiting_url)
    if query.message is not None:
        await query.message.answer(
            t("src.ask_url"),
            reply_markup=_cancel_keyboard(),
        )
    await query.answer()


@router.message(AddSourceStates.waiting_url)
async def add_source_got_url(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    username = channel_username(url)
    if username is not None:
        # Канал хранится адресом своей веб-версии; тип спрашивать незачем
        await state.update_data(url=channel_url(username), source_type=SourceType.TELEGRAM.value)
        await state.set_state(AddSourceStates.waiting_name)
        await message.answer(
            t("src.tg_detected", username=username, ask_name=t("src.ask_name")),
            reply_markup=_skip_name_keyboard(),
        )
        return
    if len(url) > _MAX_URL_LEN:
        await message.answer(
            t("src.url_too_long", length=len(url), limit=_MAX_URL_LEN),
            reply_markup=_cancel_keyboard(),
        )
        return
    if not url.startswith(("http://", "https://")):
        await message.answer(
            t("src.bad_url"),
            reply_markup=_cancel_keyboard(),
        )
        return

    # Проверка ходит на сайт и занимает несколько секунд — показываем статус
    status = await message.answer(t("src.checking"))
    detected = await detect_source(url)
    if detected is None or len(detected.url) > _MAX_URL_LEN:
        await state.update_data(url=url)
        await state.set_state(AddSourceStates.waiting_type)
        await status.edit_text(
            t("src.nothing_found"),
            reply_markup=_type_choice_keyboard(),
        )
        return

    if detected.source_type == SourceType.RSS:
        found = t(
            "src.found_rss", url=html.escape(detected.url, quote=False), items=detected.items
        )
    else:
        found = t("src.found_html", items=detected.items)
    await state.update_data(url=detected.url, source_type=detected.source_type.value)
    await state.set_state(AddSourceStates.waiting_name)
    await status.edit_text(
        f"{found}\n\n{t('src.ask_name')}",
        reply_markup=_skip_name_keyboard(),
    )


@router.callback_query(AddSourceStates.waiting_type, AddSourceTypeCallback.filter())
async def add_source_got_type(
    query: CallbackQuery, callback_data: AddSourceTypeCallback, state: FSMContext
) -> None:
    await state.update_data(source_type=callback_data.source_type)
    await state.set_state(AddSourceStates.waiting_name)
    if query.message is not None:
        await query.message.edit_text(
            t("src.ask_name"),
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
            await target.answer(t("src.duplicate"), reply_markup=main_menu_keyboard())
            return

    name_line = t("src.name_line", name=html.escape(name, quote=False)) if name else ""
    await target.answer(
        t(
            "src.added",
            id=source.id,
            type=t(_TYPE_LABELS[SourceType(source_type)]),
            url=html.escape(url, quote=False),
            name_line=name_line,
        ),
        reply_markup=main_menu_keyboard(),
    )


@router.message(AddSourceStates.waiting_name)
async def add_source_got_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip() or None
    if name is not None and len(name) > _MAX_NAME_LEN:
        # состояние не сбрасываем: админ пришлёт название короче
        await message.answer(
            t("src.name_too_long", length=len(name), limit=_MAX_NAME_LEN),
            reply_markup=_skip_name_keyboard(),
        )
        return
    data = await state.get_data()
    await state.clear()
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
        await query.message.edit_text(t("src.cancelled"))
        await query.message.answer(t("base.choose"), reply_markup=main_menu_keyboard())
    await query.answer()
