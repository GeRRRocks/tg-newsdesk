"""Просмотр, редактирование и сброс системного промпта нейросети из меню — без
перезапуска бота. Промпт — свободный текст, поэтому этим же способом можно
управлять длиной поста, упоминанием фото, тоном и чем угодно ещё.

Промпт можно прислать готовым текстом или попросить нейросеть составить его
по описанию; составленный вариант применяется только после «Сохранить»."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.filters.admin import IsAdmin
from bot.handlers.base import MenuCallback, main_menu_keyboard
from bot.i18n import t
from bot.services.ai import (
    default_system_prompt,
    generate_system_prompt,
    get_system_prompt,
    set_system_prompt,
)

router = Router(name="prompt")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

# Запас под текст-обвязку, чтобы не упереться в лимит Telegram на сообщение (4096)
_PROMPT_PREVIEW_LIMIT = 3500
_MAX_DESCRIPTION_LEN = 1000
# Админы, для которых нейросеть прямо сейчас составляет промпт: повторное
# нажатие не должно запускать второй платный запрос
_generating: set[int] = set()


class PromptStates(StatesGroup):
    waiting_new_prompt = State()
    waiting_description = State()


class EditPromptCallback(CallbackData, prefix="promptedit"):
    pass


class ResetPromptCallback(CallbackData, prefix="promptreset"):
    pass


class GeneratePromptCallback(CallbackData, prefix="promptgen"):
    pass


class RegeneratePromptCallback(CallbackData, prefix="promptregen"):
    pass


class SavePromptCallback(CallbackData, prefix="promptsave"):
    pass


class CancelPromptCallback(CallbackData, prefix="promptcancel"):
    pass


def _prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("prompt.manual_btn"), callback_data=EditPromptCallback().pack())],
            [
                InlineKeyboardButton(
                    text=t("prompt.generate_btn"), callback_data=GeneratePromptCallback().pack()
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("prompt.reset_btn"), callback_data=ResetPromptCallback().pack()
                )
            ],
            [InlineKeyboardButton(text=t("menu.back"), callback_data=MenuCallback(action="main").pack())],
        ]
    )


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("common.cancel"), callback_data=CancelPromptCallback().pack())]
        ]
    )


def _proposal_keyboard(can_save: bool) -> InlineKeyboardMarkup:
    """Кнопки под составленным промптом; can_save=False — нейросеть не
    ответила и сохранять нечего."""
    rows = []
    if can_save:
        rows.append(
            [InlineKeyboardButton(text=t("prompt.save_btn"), callback_data=SavePromptCallback().pack())]
        )
    rows.append(
        [InlineKeyboardButton(text=t("prompt.regen_btn"), callback_data=RegeneratePromptCallback().pack())]
    )
    rows.append(
        [InlineKeyboardButton(text=t("common.cancel"), callback_data=CancelPromptCallback().pack())]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _preview(prompt: str) -> str:
    """Обрезает промпт под лимит сообщения и экранирует его: у бота
    parse_mode=HTML, и символ < в тексте промпта иначе ломал бы отправку."""
    if len(prompt) > _PROMPT_PREVIEW_LIMIT:
        prompt = prompt[: _PROMPT_PREVIEW_LIMIT - 1] + "…"
    return html.escape(prompt, quote=False)


@router.callback_query(MenuCallback.filter(F.action == "prompt"))
async def cb_open_prompt(query: CallbackQuery) -> None:
    current = await get_system_prompt()
    if query.message is not None:
        await query.message.edit_text(
            t("prompt.current", prompt=_preview(current)), reply_markup=_prompt_keyboard()
        )
    await query.answer()


@router.callback_query(EditPromptCallback.filter())
async def cb_edit_prompt_start(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromptStates.waiting_new_prompt)
    if query.message is not None:
        await query.message.answer(t("prompt.ask"))
    await query.answer()


@router.message(PromptStates.waiting_new_prompt)
async def cb_edit_prompt_got(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer(t("prompt.empty"))
        return

    await state.clear()
    await set_system_prompt(text)
    await message.answer(
        t("prompt.updated"),
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(ResetPromptCallback.filter())
async def cb_reset_prompt(query: CallbackQuery) -> None:
    await set_system_prompt(None)
    default_prompt = default_system_prompt()
    if query.message is not None:
        await query.message.edit_text(
            t("prompt.reset_done", prompt=_preview(default_prompt)),
            reply_markup=_prompt_keyboard(),
        )
    await query.answer(t("prompt.reset_toast"))


@router.callback_query(GeneratePromptCallback.filter())
async def cb_generate_prompt_start(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(PromptStates.waiting_description)
    if query.message is not None:
        await query.message.answer(t("prompt.describe"), reply_markup=_cancel_keyboard())
    await query.answer()


async def _propose(status: Message, state: FSMContext, user_id: int) -> None:
    """Составляет промпт по описанию из состояния и показывает его в сообщении
    status. Вариант хранится в состоянии до «Сохранить» или «Отмена»."""
    data = await state.get_data()
    _generating.add(user_id)
    try:
        generated = await generate_system_prompt(data["description"], previous=data.get("generated"))
    finally:
        _generating.discard(user_id)

    if generated is None:
        await status.edit_text(
            t("prompt.generate_failed"),
            reply_markup=_proposal_keyboard(can_save=bool(data.get("generated"))),
        )
        return
    await state.update_data(generated=generated)
    await status.edit_text(
        t("prompt.generated", prompt=_preview(generated)),
        reply_markup=_proposal_keyboard(can_save=True),
    )


@router.message(PromptStates.waiting_description)
async def generate_prompt_got_description(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()
    if not description:
        await message.answer(t("prompt.empty"), reply_markup=_cancel_keyboard())
        return
    if len(description) > _MAX_DESCRIPTION_LEN:
        await message.answer(
            t("prompt.description_too_long", length=len(description), limit=_MAX_DESCRIPTION_LEN),
            reply_markup=_cancel_keyboard(),
        )
        return
    if message.from_user is None or message.from_user.id in _generating:
        await message.answer(t("prompt.busy"))
        return

    await state.set_state(None)
    await state.update_data(description=description, generated=None)
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
    generated = (await state.get_data()).get("generated")
    if not generated:
        await query.answer(t("prompt.generated_expired"), show_alert=True)
        return
    await state.clear()
    await set_system_prompt(generated)
    if query.message is not None:
        await query.message.edit_text(
            t("prompt.current", prompt=_preview(generated)), reply_markup=_prompt_keyboard()
        )
    await query.answer(t("prompt.saved_toast"))


@router.callback_query(CancelPromptCallback.filter())
async def cb_cancel_prompt(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if query.message is not None:
        await query.message.edit_text(t("prompt.cancelled"), reply_markup=_prompt_keyboard())
    await query.answer()
