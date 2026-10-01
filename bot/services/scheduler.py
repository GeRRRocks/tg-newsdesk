"""Планировщик публикаций на APScheduler.

Два режима, хранятся в BotSetting.schedule_mode:
- "interval" — раз в N минут (IntervalTrigger), N = draft_interval_minutes.
- "weekly" — по выбранным дням недели, в одно и то же время для всех этих
  дней (CronTrigger), может быть несколько времён в сутки (например 09:00 и
  18:00) — под каждое своя cron-задача с одинаковым day_of_week.

apply_schedule() — единая точка входа: перечитывает BotSetting и
пересобирает job'ы с нуля, поэтому вызывается и при старте бота, и после
любого изменения настроек из меню (не только смены режима)."""

from __future__ import annotations

import logging
from typing import NamedTuple

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from bot.config import get_settings
from bot.db.models import BotSetting, Draft, PostedNews, Source
from bot.db.session import async_session_factory
from bot.handlers.moderation import send_draft_for_moderation
from bot.services.claude import generate_post_text
from bot.services.news import NewsItem, collect_latest_unseen

logger = logging.getLogger(__name__)

DRAFT_JOB_ID = "generate_draft"
WEEKLY_JOB_PREFIX = "generate_draft_weekly_"

# Порядок фиксирован — используется и для сортировки дней в UI/cron.
WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_LABELS = {
    "mon": "Пн", "tue": "Вт", "wed": "Ср", "thu": "Чт",
    "fri": "Пт", "sat": "Сб", "sun": "Вс",
}


class ScheduleConfig(NamedTuple):
    mode: str
    interval_minutes: int
    weekly_days: list[str]
    weekly_times: list[str]


def _parse_csv(value: str | None) -> list[str]:
    return [item for item in (value or "").split(",") if item]


async def get_schedule_config() -> ScheduleConfig:
    async with async_session_factory() as session:
        setting = await session.get(BotSetting, 1)
        if setting is None:
            return ScheduleConfig("interval", get_settings().draft_interval_minutes, list(WEEKDAY_CODES), [])
        return ScheduleConfig(
            setting.schedule_mode,
            setting.draft_interval_minutes,
            _parse_csv(setting.weekly_days),
            _parse_csv(setting.weekly_times),
        )


async def _update_setting(**fields: object) -> None:
    async with async_session_factory() as session:
        setting = await session.get(BotSetting, 1)
        if setting is None:
            # Строка ещё не существует — сидируем интервал из .env
            # (DRAFT_INTERVAL_MINUTES), а не из дефолта колонки, иначе первое
            # же изменение любого другого поля (день/время) молча подменит
            # ранее действовавший интервал на 60.
            setting = BotSetting(id=1, draft_interval_minutes=get_settings().draft_interval_minutes)
            session.add(setting)
        for name, value in fields.items():
            setattr(setting, name, value)
        await session.commit()


def _clear_draft_jobs(scheduler: AsyncIOScheduler) -> None:
    for job in scheduler.get_jobs():
        if job.id == DRAFT_JOB_ID or job.id.startswith(WEEKLY_JOB_PREFIX):
            job.remove()


def _add_interval_job(scheduler: AsyncIOScheduler, minutes: int, bot: Bot) -> None:
    scheduler.add_job(
        generate_draft_job,
        trigger=IntervalTrigger(minutes=minutes),
        args=[bot],
        id=DRAFT_JOB_ID,
        max_instances=1,
        coalesce=True,
    )


def _add_weekly_jobs(scheduler: AsyncIOScheduler, days: list[str], times: list[str], bot: Bot) -> None:
    day_of_week = ",".join(days)
    for index, time_str in enumerate(times):
        hour, minute = time_str.split(":")
        scheduler.add_job(
            generate_draft_job,
            trigger=CronTrigger(
                day_of_week=day_of_week,
                hour=int(hour),
                minute=int(minute),
                # Без явного timezone CronTrigger берёт локальную tz сервера
                # (UTC), а не scheduler.timezone (Europe/Moscow) — 09:00
                # реально срабатывал в полдень по Москве.
                timezone=scheduler.timezone,
            ),
            args=[bot],
            id=f"{WEEKLY_JOB_PREFIX}{index}",
            max_instances=1,
            coalesce=True,
        )


async def apply_schedule(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    """Пересобирает job'ы по текущим настройкам в БД. Если режим "weekly", но
    дни или время ещё не выбраны, тихо остаётся на интервале — иначе
    публикация вообще перестала бы запускаться."""
    config = await get_schedule_config()
    _clear_draft_jobs(scheduler)
    if config.mode == "weekly" and config.weekly_days and config.weekly_times:
        _add_weekly_jobs(scheduler, config.weekly_days, config.weekly_times, bot)
    else:
        _add_interval_job(scheduler, config.interval_minutes, bot)


async def set_schedule_mode(mode: str, scheduler: AsyncIOScheduler, bot: Bot) -> None:
    await _update_setting(schedule_mode=mode)
    await apply_schedule(scheduler, bot)


async def set_draft_interval_minutes(minutes: int, scheduler: AsyncIOScheduler, bot: Bot) -> None:
    await _update_setting(draft_interval_minutes=minutes)
    await apply_schedule(scheduler, bot)


async def toggle_weekly_day(day: str, scheduler: AsyncIOScheduler, bot: Bot) -> None:
    config = await get_schedule_config()
    days = set(config.weekly_days)
    days.symmetric_difference_update({day})
    ordered = [code for code in WEEKDAY_CODES if code in days]
    await _update_setting(weekly_days=",".join(ordered))
    await apply_schedule(scheduler, bot)


async def add_weekly_time(time_str: str, scheduler: AsyncIOScheduler, bot: Bot) -> None:
    config = await get_schedule_config()
    times = set(config.weekly_times)
    times.add(time_str)
    await _update_setting(weekly_times=",".join(sorted(times)))
    await apply_schedule(scheduler, bot)


async def remove_weekly_time(time_str: str, scheduler: AsyncIOScheduler, bot: Bot) -> None:
    config = await get_schedule_config()
    times = [t for t in config.weekly_times if t != time_str]
    await _update_setting(weekly_times=",".join(sorted(times)))
    await apply_schedule(scheduler, bot)


async def collect_next_draft_candidate() -> NewsItem | None:
    """Опрашивает только включённые источники (Source.is_active) и возвращает
    самую свежую новость, которой ещё нет среди уже увиденных — опубликованных
    или отклонённых (posted_news)."""
    async with async_session_factory() as session:
        sources = (
            await session.execute(select(Source).where(Source.is_active.is_(True)))
        ).scalars().all()
        seen_urls = set(
            (await session.execute(select(PostedNews.url))).scalars().all()
        )

    return await collect_latest_unseen(sources, seen_urls)


async def generate_draft_job(bot: Bot) -> str:
    """Один цикл: собрать новость -> сгенерировать черновик через Claude ->
    отправить админам на модерацию. Если генерация не удалась, запись о
    новости откатывается — её подхватит следующий запуск.

    Используется и планировщиком (по расписанию), и командой /generate_now
    (по требованию) — возвращаемый статус нужен второму, чтобы сразу
    ответить админу, что произошло."""
    item = await collect_next_draft_candidate()
    if item is None:
        return "no_news"

    async with async_session_factory() as session:
        news = PostedNews(
            source_id=item.source_id,
            url=item.url,
            guid=item.guid,
            title=item.title,
            content_hash=item.content_hash,
            published_at=item.published_at,
        )
        session.add(news)
        await session.flush()

        text = await generate_post_text(item)
        if text is None:
            logger.warning(
                "Не удалось сгенерировать черновик для %s, попробую в следующий раз", item.url
            )
            await session.rollback()
            return "generation_failed"

        session.add(
            Draft(news_id=news.id, text=text, photo_url=item.image_url, source_link=item.url)
        )
        await session.commit()
        news_id = news.id

    await send_draft_for_moderation(bot, news_id)
    return "sent"


async def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Создаёт и запускает планировщик, job'ы собирает apply_schedule() по
    текущим настройкам из БД (интервал или дни недели + время)."""
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.start()
    await apply_schedule(scheduler, bot)
    return scheduler
