"""Просмотр, редактирование и сброс текстов для нейросети из меню — без
перезапуска бота. Раздела два: промпт для постов и инструкция для ответов на
вопросы в группе. Оба — свободный текст, поэтому этим же способом можно
управлять длиной, тоном, тематикой и чем угодно ещё.

Текст можно прислать готовым или попросить нейросеть составить его по
описанию; составленный вариант применяется только после «Сохранить».

У инструкции для ответов редактируется только её часть: защитные правила
дописываются в коде (bot/services/ai.py::qa_rules) и здесь лишь показываются."""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import get_settings
from bot.filters.admin import IsAdmin
from bot.handlers.base import MenuCallback, main_menu_keyboard
from bot.i18n import t
from bot.services.ai import (
    default_qa_prompt,
    default_system_prompt,
    generate_system_prompt,
    get_qa_prompt,
    get_system_prompt,
    qa_rules,
    set_qa_prompt,
    set_system_prompt,
)

router = Router(name="prompt")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

# Запас под текст-обвязку, чтобы не упереться в лимит Telegram на сообщение (4096)
_PROMPT_PREVIEW_LIMIT = 3500
# У инструкции для ответов в том же сообщении показываются ещё и правила
_QA_PREVIEW_LIMIT = 2500
_MAX_DESCRIPTION_LEN = 1000
# Админы, для которых нейросеть прямо сейчас составляет промпт: повторное
# нажатие не должно запускать второй платный запрос
_generating: set[int] = set()


class _Section(NamedTuple):
    """Раздел меню: что редактируется и какими текстами это описывается."""

    get: Callable[[], Awaitable[str]]
    save: Callable[[str | None], Awaitable[None]]
    default: Callable[[], str]
    other: str
    other_btn: str
    current: str
    reset_done: str
    ask: str
    describe: str
    updated: str


_SECTIONS = {
    "post": _Section(
        get_system_prompt,
        set_system_prompt,
        default_system_prompt,
        "qa",
        "prompt.to_qa_btn",
        "prompt.current",
        "prompt.reset_done",
        "prompt.ask",
        "prompt.describe",
        "prompt.updated",
    ),
    "qa": _Section(
        get_qa_prompt,
        set_qa_prompt,
        default_qa_prompt,
        "post",
        "prompt.to_post_btn",
        "prompt.qa_current",
        "prompt.qa_reset_done",
        "prompt.qa_ask",
        "prompt.qa_describe",
        "prompt.qa_updated",
    ),
}


class PromptStates(StatesGroup):
    waiting_new_prompt = State()
    waiting_description = State()


class PromptSectionCallback(CallbackData, prefix="promptsec"):
    kind: str


class EditPromptCallback(CallbackData, prefix="promptedit"):
    kind: str


class ResetPromptCallback(CallbackData, prefix="promptreset"):
    kind: str


class GeneratePromptCallback(CallbackData, prefix="promptgen"):
    kind: str


class RegeneratePromptCallback(CallbackData, prefix="promptregen"):
    pass


class SavePromptCallback(CallbackData, prefix="promptsave"):
    pass


class CancelPromptCallback(CallbackData, prefix="promptcancel"):
    kind: str


def _button(text_key: str, callback: CallbackData) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=t(text_key), callback_data=callback.pack())]


def _prompt_keyboard(kind: str) -> InlineKeyboardMarkup:
    section = _SECTIONS[kind]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _button("prompt.manual_btn", EditPromptCallback(kind=kind)),
            _button("prompt.generate_btn", GeneratePromptCallback(kind=kind)),
            _button("prompt.reset_btn", ResetPromptCallback(kind=kind)),
            _button(section.other_btn, PromptSectionCallback(kind=section.other)),
            _button("menu.back", MenuCallback(action="main")),
        ]
    )


def _cancel_keyboard(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_button("common.cancel", CancelPromptCallback(kind=kind))])


def _proposal_keyboard(kind: str, can_save: bool) -> InlineKeyboardMarkup:
    """Кнопки под составленным промптом; can_save=False — нейросеть не
    ответила и сохранять нечего."""
    rows = []
    if can_save:
        rows.append(_button("prompt.save_btn", SavePromptCallback()))
    rows.append(_button("prompt.regen_btn", RegeneratePromptCallback()))
    rows.append(_button("common.cancel", CancelPromptCallback(kind=kind)))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _preview(prompt: str, limit: int = _PROMPT_PREVIEW_LIMIT) -> str:
    """Обрезает промпт под лимит сообщения и экранирует его: у бота
    parse_mode=HTML, и символ < в тексте промпта иначе ломал бы отправку."""
    if len(prompt) > limit:
        prompt = prompt[: limit - 1] + "…"
    return html.escape(prompt, quote=False)


def _screen(kind: str, text_key: str, prompt: str) -> str:
    """Текст экрана раздела: сам промпт, а для ответов — ещё и неизменяемые
    правила и пометка, если ответы выключены."""
    if kind != "qa":
        return t(text_key, prompt=_preview(prompt))
    text = t(
        text_key,
        prompt=_preview(prompt, _QA_PREVIEW_LIMIT),
        rules=html.escape(qa_rules(), quote=False),
    )
    if not get_settings().qa_enabled:
        text += t("prompt.qa_disabled_note")
    return text


async def _show_section(query: CallbackQuery, kind: str) -> None:
    section = _SECTIONS[kind]
    if query.message is not None:
        await query.message.edit_text(
            _screen(kind, section.current, await section.get()), reply_markup=_prompt_keyboard(kind)
        )


@router.callback_query(MenuCallback.filter(F.action == "prompt"))
async def cb_open_prompt(query: CallbackQuery) -> None:
    await _show_section(query, "post")
    await query.answer()


@router.callback_query(PromptSectionCallback.filter(F.kind.in_(_SECTIONS)))
async def cb_open_section(query: CallbackQuery, callback_data: PromptSectionCallback) -> None:
    await _show_section(query, callback_data.kind)
    await query.answer()


@router.callback_query(EditPromptCallback.filter(F.kind.in_(_SECTIONS)))
async def cb_edit_prompt_start(
    query: CallbackQuery, callback_data: EditPromptCallback, state: FSMContext
) -> None:
    await state.clear()
    await state.set_state(PromptStates.waiting_new_prompt)
    await state.update_data(kind=callback_data.kind)
    if query.message is not None:
        await query.message.answer(
            t(_SECTIONS[callback_data.kind].ask), reply_markup=_cancel_keyboard(callback_data.kind)
        )
    await query.answer()


@router.message(PromptStates.waiting_new_prompt)
async def cb_edit_prompt_got(message: Message, state: FSMContext) -> None:
    kind = (await state.get_data()).get("kind", "post")
    text = (message.text or "").strip()
    if not text:
        await message.answer(t("prompt.empty"), reply_markup=_cancel_keyboard(kind))
        return

    await state.clear()
    await _SECTIONS[kind].save(text)
    await message.answer(t(_SECTIONS[kind].updated), reply_markup=main_menu_keyboard())


@router.callback_query(ResetPromptCallback.filter(F.kind.in_(_SECTIONS)))
async def cb_reset_prompt(query: CallbackQuery, callback_data: ResetPromptCallback) -> None:
    kind = callback_data.kind
    section = _SECTIONS[kind]
    await section.save(None)
    if query.message is not None:
        await query.message.edit_text(
            _screen(kind, section.reset_done, section.default()), reply_markup=_prompt_keyboard(kind)
        )
    await query.answer(t("prompt.reset_toast"))


@router.callback_query(GeneratePromptCallback.filter(F.kind.in_(_SECTIONS)))
async def cb_generate_prompt_start(
    query: CallbackQuery, callback_data: GeneratePromptCallback, state: FSMContext
) -> None:
    await state.clear()
    await state.set_state(PromptStates.waiting_description)
    await state.update_data(kind=callback_data.kind)
    if query.message is not None:
        await query.message.answer(
            t(_SECTIONS[callback_data.kind].describe), reply_markup=_cancel_keyboard(callback_data.kind)
        )
    await query.answer()


async def _propose(status: Message, state: FSMContext, user_id: int) -> None:
    """Составляет промпт по описанию из состояния и показывает его в сообщении
    status. Вариант хранится в состоянии до «Сохранить» или «Отмена»."""
    data = await state.get_data()
    kind = data["kind"]
    _generating.add(user_id)
    try:
        generated = await generate_system_prompt(
            data["description"], previous=data.get("generated"), kind=kind
        )
    finally:
        _generating.discard(user_id)

    if generated is None:
        await status.edit_text(
            t("prompt.generate_failed"),
            reply_markup=_proposal_keyboard(kind, can_save=bool(data.get("generated"))),
        )
        return
    await state.update_data(generated=generated)
    await status.edit_text(
        t("prompt.generated", prompt=_preview(generated)),
        reply_markup=_proposal_keyboard(kind, can_save=True),
    )


@router.message(PromptStates.waiting_description)
async def generate_prompt_got_description(message: Message, state: FSMContext) -> None:
    kind = (await state.get_data()).get("kind", "post")
    description = (message.text or "").strip()
    if not description:
        await message.answer(t("prompt.empty"), reply_markup=_cancel_keyboard(kind))
        return
    if len(description) > _MAX_DESCRIPTION_LEN:
        await message.answer(
            t("prompt.description_too_long", length=len(description), limit=_MAX_DESCRIPTION_LEN),
            reply_markup=_cancel_keyboard(kind),
        )
        return
    if message.from_user is None or message.from_user.id in _generating:
        await message.answer(t("prompt.busy"))
        return

    await state.set_state(None)
    await state.update_data(kind=kind, description=description, generated=None)
    status = await message.answer(t("prompt.generating"))
    await _propose(status, state, message.from_user.id)


@router.callback_query(RegeneratePromptCallback.filter())
async def cb_regenerate_prompt(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("description"):
        await query.answer(t("prompt.generated_expired"), show_alert=True)
        return
    if query.from_user.id in _generating:
        await query.answer(t("prompt.busy"))
        return
    await query.answer()
    if query.message is not None:
        await query.message.edit_text(t("prompt.generating"))
        await _propose(query.message, state, query.from_user.id)


@router.callback_query(SavePromptCallback.filter())
async def cb_save_generated_prompt(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    generated = data.get("generated")
    if not generated:
        await query.answer(t("prompt.generated_expired"), show_alert=True)
        return
    kind = data["kind"]
    await state.clear()
    await _SECTIONS[kind].save(generated)
    await _show_section(query, kind)
    await query.answer(t("prompt.saved_toast"))


@router.callback_query(CancelPromptCallback.filter(F.kind.in_(_SECTIONS)))
async def cb_cancel_prompt(
    query: CallbackQuery, callback_data: CancelPromptCallback, state: FSMContext
) -> None:
    await state.clear()
    if query.message is not None:
        await query.message.edit_text(
            t("prompt.cancelled"), reply_markup=_prompt_keyboard(callback_data.kind)
        )
    await query.answer()
