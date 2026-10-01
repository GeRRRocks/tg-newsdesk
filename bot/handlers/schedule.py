"""Настройка расписания автогенерации черновиков из меню — без правки .env
и перезапуска бота. Два режима: по интервалу (раз в N минут) или по дням
недели + фиксированному времени (одно и то же время для всех выбранных
дней, можно несколько времён в сутки, например 09:00 и 18:00)."""

from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.filters.admin import IsAdmin
from bot.handlers.base import MenuCallback, main_menu_keyboard
from bot.services.scheduler import (
    ScheduleConfig,
    WEEKDAY_CODES,
    WEEKDAY_LABELS,
    add_weekly_time,
    get_schedule_config,
    remove_weekly_time,
    set_draft_interval_minutes,
    set_schedule_mode,
    toggle_weekly_day,
)

router = Router(name="schedule")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

_PRESETS_MINUTES = [30, 60, 180, 360, 720, 1440]
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ScheduleStates(StatesGroup):
    waiting_custom_minutes = State()
    waiting_weekly_time = State()


class IntervalCallback(CallbackData, prefix="interval"):
    minutes: int


class CustomIntervalCallback(CallbackData, prefix="intervalcustom"):
    pass


class ModeCallback(CallbackData, prefix="schedmode"):
    mode: str  # "interval" | "weekly"


class WeekdayCallback(CallbackData, prefix="schedday"):
    day: str


class AddTimeCallback(CallbackData, prefix="schedtimeadd"):
    pass


class RemoveTimeCallback(CallbackData, prefix="schedtimedel"):
    hhmm: str  # "0900" — без ":" (запрещён в callback_data), см. _pack/_unpack_time


def _pack_time(time_str: str) -> str:
    return time_str.replace(":", "")


def _unpack_time(hhmm: str) -> str:
    return f"{hhmm[:2]}:{hhmm[2:]}"


def _format_interval(minutes: int) -> str:
    if minutes % 1440 == 0:
        days = minutes // 1440
        return "1 день" if days == 1 else f"{days} дн"
    if minutes % 60 == 0:
        return f"{minutes // 60} ч"
    return f"{minutes} мин"


def _mode_row(current_mode: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            text=("✅ " if current_mode == "interval" else "") + "⏱ Интервал",
            callback_data=ModeCallback(mode="interval").pack(),
        ),
        InlineKeyboardButton(
            text=("✅ " if current_mode == "weekly" else "") + "📅 Дни и время",
            callback_data=ModeCallback(mode="weekly").pack(),
        ),
    ]


def _interval_text(current_minutes: int) -> str:
    return f"⏱ Автогенерация черновика: раз в {_format_interval(current_minutes)}.\nВыбери новый интервал:"


def _interval_keyboard(current_minutes: int) -> InlineKeyboardMarkup:
    rows = [_mode_row("interval")]
    for minutes in _PRESETS_MINUTES:
        mark = "✅ " if minutes == current_minutes else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{_format_interval(minutes)}",
                    callback_data=IntervalCallback(minutes=minutes).pack(),
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="✏️ Своё значение", callback_data=CustomIntervalCallback().pack())]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Меню", callback_data=MenuCallback(action="main").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _weekly_text(days: list[str], times: list[str]) -> str:
    days_label = " ".join(WEEKDAY_LABELS[d] for d in days) if days else "— не выбраны —"
    times_label = ", ".join(times) if times else "— не выбрано —"
    text = f"📅 Публикация по дням недели.\nДни: {days_label}\nВремя: {times_label}"
    if not days or not times:
        text += "\n⚠️ Нужен хотя бы один день и время — пока не выбраны, работает интервал."
    return text


def _weekly_keyboard(days: list[str], times: list[str]) -> InlineKeyboardMarkup:
    rows = [_mode_row("weekly")]

    day_buttons = [
        InlineKeyboardButton(
            text=("✅ " if code in days else "") + WEEKDAY_LABELS[code],
            callback_data=WeekdayCallback(day=code).pack(),
        )
        for code in WEEKDAY_CODES
    ]
    rows.append(day_buttons[:4])
    rows.append(day_buttons[4:])

    for time_str in times:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✕ {time_str}",
                    callback_data=RemoveTimeCallback(hhmm=_pack_time(time_str)).pack(),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Добавить время", callback_data=AddTimeCallback().pack())])

    rows.append([InlineKeyboardButton(text="⬅️ Меню", callback_data=MenuCallback(action="main").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render(query: CallbackQuery, config: ScheduleConfig) -> None:
    if config.mode == "weekly":
        text = _weekly_text(config.weekly_days, config.weekly_times)
        keyboard = _weekly_keyboard(config.weekly_days, config.weekly_times)
    else:
        text = _interval_text(config.interval_minutes)
        keyboard = _interval_keyboard(config.interval_minutes)
    if query.message is not None:
        await query.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(MenuCallback.filter(F.action == "schedule"))
async def cb_open_schedule(query: CallbackQuery) -> None:
    config = await get_schedule_config()
    await _render(query, config)
    await query.answer()


@router.callback_query(ModeCallback.filter())
async def cb_set_mode(
    query: CallbackQuery, callback_data: ModeCallback, scheduler: AsyncIOScheduler, bot: Bot
) -> None:
    await set_schedule_mode(callback_data.mode, scheduler, bot)
    config = await get_schedule_config()
    await _render(query, config)
    await query.answer()


@router.callback_query(IntervalCallback.filter())
async def cb_set_interval(
    query: CallbackQuery, callback_data: IntervalCallback, scheduler: AsyncIOScheduler, bot: Bot
) -> None:
    await set_draft_interval_minutes(callback_data.minutes, scheduler, bot)
    config = await get_schedule_config()
    await _render(query, config)
    await query.answer("Сохранено ✅")


@router.callback_query(CustomIntervalCallback.filter())
async def cb_custom_interval_start(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ScheduleStates.waiting_custom_minutes)
    if query.message is not None:
        await query.message.answer("Пришли интервал в минутах (целое число, например 90).")
    await query.answer()


@router.message(ScheduleStates.waiting_custom_minutes)
async def cb_custom_interval_got(
    message: Message, state: FSMContext, scheduler: AsyncIOScheduler, bot: Bot
) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        await message.answer("Нужно целое число минут, больше 0. Пришли ещё раз.")
        return

    minutes = int(raw)
    await state.clear()
    await set_draft_interval_minutes(minutes, scheduler, bot)
    await message.answer(f"⏱ Готово: раз в {_format_interval(minutes)}.", reply_markup=main_menu_keyboard())


@router.callback_query(WeekdayCallback.filter())
async def cb_toggle_day(
    query: CallbackQuery, callback_data: WeekdayCallback, scheduler: AsyncIOScheduler, bot: Bot
) -> None:
    await toggle_weekly_day(callback_data.day, scheduler, bot)
    config = await get_schedule_config()
    await _render(query, config)
    await query.answer()


@router.callback_query(AddTimeCallback.filter())
async def cb_add_time_start(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ScheduleStates.waiting_weekly_time)
    if query.message is not None:
        await query.message.answer("Пришли время в формате ЧЧ:ММ, например 09:00.")
    await query.answer()


@router.message(ScheduleStates.waiting_weekly_time)
async def cb_add_time_got(
    message: Message, state: FSMContext, scheduler: AsyncIOScheduler, bot: Bot
) -> None:
    raw = (message.text or "").strip()
    if not _TIME_RE.match(raw):
        await message.answer("Формат должен быть ЧЧ:ММ, например 18:00. Пришли ещё раз.")
        return

    await state.clear()
    await add_weekly_time(raw, scheduler, bot)
    config = await get_schedule_config()
    await message.answer(
        _weekly_text(config.weekly_days, config.weekly_times),
        reply_markup=_weekly_keyboard(config.weekly_days, config.weekly_times),
    )


@router.callback_query(RemoveTimeCallback.filter())
async def cb_remove_time(
    query: CallbackQuery, callback_data: RemoveTimeCallback, scheduler: AsyncIOScheduler, bot: Bot
) -> None:
    await remove_weekly_time(_unpack_time(callback_data.hhmm), scheduler, bot)
    config = await get_schedule_config()
    await _render(query, config)
    await query.answer("Удалено")
