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

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from bot.config import get_settings
from bot.db.models import BotSetting, Draft, PostedNews, Source
from bot.db.session import async_session_factory
from bot.handlers.moderation import expire_stale_drafts, send_draft_for_moderation
from bot.services.ai import PROVIDER_LABELS, generate_post_text, get_active_provider
from bot.services.alerts import notify_admins
from bot.services.news import NewsItem, collect_latest_unseen
from bot.services.news_filter import get_word_filter

logger = logging.getLogger(__name__)

DRAFT_JOB_ID = "generate_draft"
WEEKLY_JOB_PREFIX = "generate_draft_weekly_"

# Один цикл генерации за раз: параллельные запуски (кнопка + расписание или
# два нажатия подряд) выбирали бы одну и ту же новость и дважды платили за генерацию.
_generation_lock = asyncio.Lock()

EXPIRE_JOB_ID = "expire_stale_drafts"

# Состояние для уведомлений о сбоях. Живёт в памяти процесса: после
# перезапуска отсчёт начинается заново.
SOURCE_FAIL_ALERT_AFTER = 3
_source_fail_streak: dict[int, int] = {}
_ai_failing = False

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
        args=[bot, True],
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
            args=[bot, True],
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


async def _track_source_failures(bot: Bot, sources: list[Source], failed: list[Source]) -> None:
    """Считает запуски подряд, в которых источник не открылся или не отдал ни
    одной новости, и на SOURCE_FAIL_ALERT_AFTER-м один раз пишет админам."""
    failed_ids = {source.id for source in failed}
    for source in sources:
        if source.id not in failed_ids:
            _source_fail_streak.pop(source.id, None)
            continue
        streak = _source_fail_streak.get(source.id, 0) + 1
        _source_fail_streak[source.id] = streak
        if streak == SOURCE_FAIL_ALERT_AFTER:
            name = html.escape(source.name or source.url, quote=False)
            await notify_admins(
                bot,
                f"⚠️ Источник «{name}» не отдаёт новости уже {streak} запуска подряд. "
                "Проверь, открывается ли он, или выключи его в «📋 Источники».",
            )


async def collect_next_draft_candidate(bot: Bot) -> NewsItem | None:
    """Опрашивает только включённые источники (Source.is_active) и возвращает
    самую свежую новость, которой ещё нет среди уже увиденных — опубликованных
    или отклонённых (posted_news) — и которая проходит фильтр по словам."""
    async with async_session_factory() as session:
        sources = (
            await session.execute(select(Source).where(Source.is_active.is_(True)))
        ).scalars().all()
        seen_urls = set(
            (await session.execute(select(PostedNews.url))).scalars().all()
        )

    result = await collect_latest_unseen(sources, seen_urls, await get_word_filter())
    await _track_source_failures(bot, sources, result.failed_sources)
    return result.item


async def generate_draft_job(bot: Bot, scheduled: bool = False) -> str:
    """Один цикл: собрать новость -> сгенерировать черновик через нейросеть ->
    отправить админам на модерацию. Если генерация не удалась, запись о
    новости откатывается — её подхватит следующий запуск.

    Используется и планировщиком (scheduled=True), и кнопкой «⚡ Сгенерировать»
    — возвращаемый статус нужен второй, чтобы сразу ответить админу, что
    произошло. О сбое нейросети при запуске по расписанию админы узнают из
    отдельного сообщения: при ручном запуске ответ и так виден сразу."""
    if _generation_lock.locked():
        logger.info("Генерация уже идёт, пропускаю параллельный запуск")
        return "busy"
    async with _generation_lock:
        status = await _generate_draft(bot)
    await _track_ai_failure(bot, status, scheduled)
    return status


async def _track_ai_failure(bot: Bot, status: str, scheduled: bool) -> None:
    """Одно сообщение на серию сбоев и одно — когда генерация восстановилась."""
    global _ai_failing
    if status == "generation_failed":
        if scheduled and not _ai_failing:
            _ai_failing = True
            label = PROVIDER_LABELS[await get_active_provider()]
            await notify_admins(
                bot,
                f"⚠️ Нейросеть {label} не смогла написать черновик по расписанию. "
                "Попробую снова в следующий запуск; сменить нейросеть можно в «🤖 Нейросеть».",
            )
    elif status == "sent" and _ai_failing:
        _ai_failing = False
        await notify_admins(bot, "✅ Генерация черновиков снова работает.")


async def _generate_draft(bot: Bot) -> str:
    item = await collect_next_draft_candidate(bot)
    if item is None:
        return "no_news"

    async with async_session_factory() as session:
        news = PostedNews(
            source_id=item.source_id,
            url=item.url,
            guid=item.guid,
            title=item.title,
            summary=item.summary or None,
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
    if settings.draft_expire_hours > 0:
        scheduler.add_job(
            expire_stale_drafts,
            trigger=IntervalTrigger(hours=1),
            args=[bot, settings.draft_expire_hours],
            id=EXPIRE_JOB_ID,
            max_instances=1,
            coalesce=True,
            # первый проход — вскоре после старта, а не через час
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
        )
    return scheduler
