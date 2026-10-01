"""Лимиты на вопросы боту в группе.

Каждый ответ — платный запрос к нейросети, а задавать вопросы может любой
участник группы, поэтому попытки считаются: на одного участника и на всех
вместе. Окно — 24 часа с первого вопроса; когда оно истекло, счётчик
обнуляется и новое окно начинается со следующего вопроса."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from bot.config import get_settings
from bot.db.models import QaUsage
from bot.db.session import async_session_factory

WINDOW = timedelta(hours=24)
# user_id строки с общим счётчиком: у настоящих пользователей Telegram id > 0
GLOBAL_ID = 0
# Слово, с которого должен начинаться вопрос («Вопрос: …»). Без него
# упоминание бота в разговоре участников вопросом не считается. Оба варианта
# принимаются при любом языке бота.
QUESTION_MARKERS = ("вопрос", "question")


@dataclass
class Reservation:
    allowed: bool
    # Для отказа: чей лимит исчерпан ("user" / "global"), когда он обнулится и
    # нужно ли об этом сказать (говорим один раз за окно)
    scope: str | None = None
    resets_at: datetime | None = None
    notify: bool = False


def _refresh(row: QaUsage, now: datetime) -> None:
    if row.window_start + WINDOW <= now:
        row.window_start = now
        row.count = 0
        row.limit_notified = False


def _refuse(row: QaUsage, scope: str) -> Reservation:
    notify = not row.limit_notified
    row.limit_notified = True
    return Reservation(False, scope, row.window_start + WINDOW, notify)


async def reserve(user_id: int, now: datetime | None = None) -> Reservation:
    """Занимает одну попытку участника и одну общую — до запроса к нейросети,
    чтобы одновременные вопросы не обошли лимит. Если ответа не получилось,
    попытку возвращает release()."""
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    async with async_session_factory() as session:
        # Счётчики тех, у кого окно давно истекло, не храним
        await session.execute(
            delete(QaUsage).where(
                QaUsage.window_start < now - WINDOW, QaUsage.user_id.not_in([user_id, GLOBAL_ID])
            )
        )
        ids = [GLOBAL_ID, user_id]
        await session.execute(
            insert(QaUsage)
            .values([{"user_id": i, "window_start": now, "count": 0, "limit_notified": False} for i in ids])
            .on_conflict_do_nothing()
        )
        # Блокировка строк в одном порядке — без взаимных блокировок
        rows = await session.scalars(
            select(QaUsage).where(QaUsage.user_id.in_(ids)).order_by(QaUsage.user_id).with_for_update()
        )
        total, own = rows.all()
        _refresh(total, now)
        _refresh(own, now)

        if own.count >= settings.qa_user_daily_limit:
            result = _refuse(own, "user")
        elif total.count >= settings.qa_global_daily_limit:
            result = _refuse(total, "global")
        else:
            own.count += 1
            total.count += 1
            result = Reservation(True)
        await session.commit()
    return result


async def release(user_id: int) -> None:
    """Возвращает попытку, занятую reserve(): нейросеть не ответила."""
    async with async_session_factory() as session:
        rows = await session.scalars(
            select(QaUsage)
            .where(QaUsage.user_id.in_([GLOBAL_ID, user_id]))
            .order_by(QaUsage.user_id)
            .with_for_update()
        )
        for row in rows:
            row.count = max(row.count - 1, 0)
        await session.commit()
