"""Меню «🚫 Фильтр»: стоп-слова и обязательные слова для новостей."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.filters.admin import IsAdmin
from bot.handlers.base import MenuCallback
from bot.services.news_filter import MAX_WORDS, get_word_filter, parse_words, set_words

router = Router(name="news_filter")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

# kind -> (колонка BotSetting, название списка в сообщениях)
_KINDS = {
    "stop": ("filter_stop_words", "Стоп-слова"),
    "req": ("filter_required_words", "Обязательные слова"),
}


class FilterStates(StatesGroup):
    waiting_words = State()


class FilterCallback(CallbackData, prefix="nfilter"):
    action: str  # "edit" | "clear"
    kind: str  # "stop" | "req"


def _words_line(words: list[str]) -> str:
    return html.escape(", ".join(words), quote=False) if words else "не заданы"


async def _menu() -> tuple[str, InlineKeyboardMarkup]:
    word_filter = await get_word_filter()
    text = (
        "🚫 Фильтр новостей\n\n"
        f"Стоп-слова: {_words_line(word_filter.stop_words)}\n"
        "Новость с любым из них пропускается.\n\n"
        f"Обязательные слова: {_words_line(word_filter.required_words)}\n"
        "Если заданы, берутся только новости, где есть хотя бы одно.\n\n"
        "Слова ищутся в заголовке и анонсе, регистр не важен."
    )

    def button(label: str, action: str, kind: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=label, callback_data=FilterCallback(action=action, kind=kind).pack()
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [button("✏️ Стоп-слова", "edit", "stop"), button("🗑 Очистить", "clear", "stop")],
            [button("✏️ Обязательные", "edit", "req"), button("🗑 Очистить", "clear", "req")],
            [InlineKeyboardButton(text="⬅️ Меню", callback_data=MenuCallback(action="main").pack())],
        ]
    )
    return text, keyboard


@router.callback_query(MenuCallback.filter(F.action == "filter"))
async def cb_open_filter(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, keyboard = await _menu()
    if query.message is not None:
        await query.message.edit_text(text, reply_markup=keyboard)
    await query.answer()


@router.callback_query(FilterCallback.filter(F.action == "edit"))
async def cb_edit_words(query: CallbackQuery, callback_data: FilterCallback, state: FSMContext) -> None:
    if callback_data.kind not in _KINDS:
        await query.answer()
        return
    await state.set_state(FilterStates.waiting_words)
    await state.update_data(kind=callback_data.kind)
    if query.message is not None:
        await query.message.answer(
            f"{_KINDS[callback_data.kind][1]}: пришли слова или фразы одним сообщением через "
            "запятую — они заменят текущий список. Например: такси, каршеринг, штраф",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="❌ Отмена", callback_data=MenuCallback(action="filter").pack()
                        )
                    ]
                ]
            ),
        )
    await query.answer()


@router.message(FilterStates.waiting_words)
async def msg_words(message: Message, state: FSMContext) -> None:
    words = parse_words(message.text)
    if not words:
        await message.answer("Не вижу слов. Пришли их через запятую или нажми «Отмена».")
        return

    kind = (await state.get_data()).get("kind")
    await state.clear()
    if kind not in _KINDS:
        return
    await set_words(_KINDS[kind][0], words)
    text, keyboard = await _menu()
    note = f" (сохранены первые {MAX_WORDS})" if len(words) == MAX_WORDS else ""
    await message.answer(f"✅ Сохранено{note}.\n\n{text}", reply_markup=keyboard)


@router.callback_query(FilterCallback.filter(F.action == "clear"))
async def cb_clear_words(query: CallbackQuery, callback_data: FilterCallback) -> None:
    if callback_data.kind not in _KINDS:
        await query.answer()
        return
    word_filter = await get_word_filter()
    current = word_filter.stop_words if callback_data.kind == "stop" else word_filter.required_words
    if not current:
        await query.answer("Список и так пуст")
        return
    await set_words(_KINDS[callback_data.kind][0], [])
    text, keyboard = await _menu()
    if query.message is not None:
        await query.message.edit_text(text, reply_markup=keyboard)
    await query.answer("Очищено")
