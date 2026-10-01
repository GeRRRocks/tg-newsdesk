"""Стартовое меню и разовая команда настройки топика.

Бот полностью приватный: и сообщения, и колбэки от кнопок принимаются
только от админов (ADMIN_CHAT_IDS) — router.message.filter(IsAdmin())/
router.callback_query.filter(IsAdmin()) ниже отсеивают всех остальных
до попадания в любой хендлер."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.filters.admin import IsAdmin

router = Router(name="base")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class MenuCallback(CallbackData, prefix="menu"):
    action: str  # "sources" | "add_source" | "generate_now" | "ping" | "main"


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Источники", callback_data=MenuCallback(action="sources").pack()
                ),
                InlineKeyboardButton(
                    text="➕ Добавить",
                    callback_data=MenuCallback(action="add_source").pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚡ Сгенерировать",
                    callback_data=MenuCallback(action="generate_now").pack(),
                ),
                InlineKeyboardButton(
                    text="⏱ Расписание", callback_data=MenuCallback(action="schedule").pack()
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🏷 Промт", callback_data=MenuCallback(action="prompt").pack()
                ),
                InlineKeyboardButton(
                    text="🤖 Нейросеть", callback_data=MenuCallback(action="ai").pack()
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🚫 Фильтр", callback_data=MenuCallback(action="filter").pack()
                ),
                InlineKeyboardButton(
                    text="🏓 Пинг", callback_data=MenuCallback(action="ping").pack()
                ),
            ],
        ]
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Бот автопостинга новостей запущен.\nВыбери действие:",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(MenuCallback.filter(F.action == "main"))
async def cb_main_menu(query: CallbackQuery) -> None:
    if query.message is not None:
        await query.message.edit_text("Выбери действие:", reply_markup=main_menu_keyboard())
    await query.answer()


@router.callback_query(MenuCallback.filter(F.action == "ping"))
async def cb_ping(query: CallbackQuery) -> None:
    await query.answer("pong 🏓")


@router.message(Command("get_topic_id"))
async def cmd_get_topic_id(message: Message) -> None:
    """Единственная команда, которую нельзя заменить кнопкой в личке — её
    нужно отправить прямо внутри нужного топика целевой группы, чтобы бот
    подсказал chat_id и message_thread_id для .env."""
    if message.message_thread_id is None:
        await message.answer(
            "Эта команда должна быть отправлена внутри нужного топика группы "
            "(не в общий чат General и не в личку) — тогда бот подскажет id."
        )
        return
    await message.answer(
        f"TARGET_GROUP_CHAT_ID={message.chat.id}\nTARGET_TOPIC_ID={message.message_thread_id}"
    )
