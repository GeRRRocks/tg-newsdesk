from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import get_settings
from bot.db.base import Base
from bot.db import models as _models  # noqa: F401 — регистрирует модели в Base.metadata до create_all

settings = get_settings()

engine = create_async_engine(settings.database_url, echo=False)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def init_models() -> None:
    """Создаёт таблицы, если их ещё нет. На следующих этапах заменим на Alembic-миграции."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all не добавляет колонки в уже существующие таблицы, а
        # миграций нет — новые колонки дописываются здесь, идемпотентно.
        for statement in (
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS ai_provider VARCHAR(16)",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS filter_stop_words TEXT",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS filter_required_words TEXT",
            "ALTER TABLE posted_news ADD COLUMN IF NOT EXISTS summary TEXT",
            # Новое значение нативного enum; метка — .name, см. models.py
            "ALTER TYPE newsstatus ADD VALUE IF NOT EXISTS 'EXPIRED'",
        ):
            await conn.execute(text(statement))


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        yield session
