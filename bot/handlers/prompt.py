"""Просмотр, редактирование и сброс системного промпта нейросети из меню — без
перезапуска бота. Промпт — свободный текст, поэтому этим же способом можно
управлять длиной поста, упоминанием фото, тоном и чем угодно ещё."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import get_settings
from bot.filters.admin import IsAdmin
from bot.handlers.base import MenuCallback, main_menu_keyboard
from bot.services.ai import build_default_system_prompt, get_system_prompt, set_system_prompt

router = Router(name="prompt")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

# Запас под текст-обвязку, чтобы не упереться в лимит Telegram на сообщение (4096)
_PROMPT_PREVIEW_LIMIT = 3500


class PromptStates(StatesGroup):
    waiting_new_prompt = State()


class EditPromptCallback(CallbackData, prefix="promptedit"):
    pass


class ResetPromptCallback(CallbackData, prefix="promptreset"):
    pass


def _prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить", callback_data=EditPromptCallback().pack())],
            [
                InlineKeyboardButton(
                    text="♻️ Сбросить на умолчание", callback_data=ResetPromptCallback().pack()
                )
            ],
            [InlineKeyboardButton(text="⬅️ Меню", callback_data=MenuCallback(action="main").pack())],
        ]
    )


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
            f"🏷 Текущий промпт для нейросети:\n\n{_preview(current)}", reply_markup=_prompt_keyboard()
        )
    await query.answer()


@router.callback_query(EditPromptCallback.filter())
async def cb_edit_prompt_start(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromptStates.waiting_new_prompt)
    if query.message is not None:
        await query.message.answer(
            "Пришли новый текст промпта одним сообщением — он полностью заменит "
            "текущий. В нём можно словами задать длину поста, упоминание фото, "
            "тон, формат и что угодно ещё."
        )
    await query.answer()


@router.message(PromptStates.waiting_new_prompt)
async def cb_edit_prompt_got(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Промпт не может быть пустым. Пришли текст ещё раз.")
        return

    await state.clear()
    await set_system_prompt(text)
    await message.answer(
        "✅ Промпт обновлён — новые черновики будут генерироваться уже с ним.",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(ResetPromptCallback.filter())
async def cb_reset_prompt(query: CallbackQuery) -> None:
    await set_system_prompt(None)
    default_prompt = build_default_system_prompt(get_settings().bot_topic)
    if query.message is not None:
        await query.message.edit_text(
            f"♻️ Сброшено на умолчание:\n\n{_preview(default_prompt)}",
            reply_markup=_prompt_keyboard(),
        )
    await query.answer("Сброшено")
