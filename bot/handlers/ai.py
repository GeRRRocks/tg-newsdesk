"""Выбор нейросети из меню — без перезапуска бота. Ключи остаются в .env:
переключиться можно только на провайдера, для которого ключ там задан."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.filters.admin import IsAdmin
from bot.handlers.base import MenuCallback
from bot.services.ai import (
    PROVIDER_LABELS,
    available_providers,
    get_active_provider,
    model_for,
    set_active_provider,
)

router = Router(name="ai")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class ProviderCallback(CallbackData, prefix="aiprov"):
    name: str


def _menu(active: str) -> tuple[str, InlineKeyboardMarkup]:
    available = available_providers()
    rows = []
    for name, label in PROVIDER_LABELS.items():
        if name == active:
            text = f"✅ {label}"
        elif name in available:
            text = label
        else:
            text = f"🔒 {label} — нет ключа"
        rows.append(
            [InlineKeyboardButton(text=text, callback_data=ProviderCallback(name=name).pack())]
        )
    rows.append(
        [InlineKeyboardButton(text="⬅️ Меню", callback_data=MenuCallback(action="main").pack())]
    )
    text = (
        f"🤖 Посты пишет: {PROVIDER_LABELS[active]}\n"
        f"Модель: {model_for(active)}\n\n"
        "Выбери нейросеть — следующий черновик будет сгенерирован уже ею."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(MenuCallback.filter(F.action == "ai"))
async def cb_open_ai(query: CallbackQuery) -> None:
    text, keyboard = _menu(await get_active_provider())
    if query.message is not None:
        await query.message.edit_text(text, reply_markup=keyboard)
    await query.answer()


@router.callback_query(ProviderCallback.filter())
async def cb_choose_provider(query: CallbackQuery, callback_data: ProviderCallback) -> None:
    name = callback_data.name
    if name not in PROVIDER_LABELS:
        await query.answer()
        return
    if name not in available_providers():
        await query.answer(
            f"Нет ключа. Добавь {name.upper()}_API_KEY в .env и перезапусти бота.",
            show_alert=True,
        )
        return
    if name == await get_active_provider():
        await query.answer("Уже выбрана")
        return

    await set_active_provider(name)
    text, keyboard = _menu(name)
    if query.message is not None:
        await query.message.edit_text(text, reply_markup=keyboard)
    await query.answer(f"Теперь посты пишет {PROVIDER_LABELS[name]}")
