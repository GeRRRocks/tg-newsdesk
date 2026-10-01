from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from bot.db.base import Base


class NewsStatus(str, enum.Enum):
    PENDING = "pending"
    POSTED = "posted"
    REJECTED = "rejected"
    # черновик закрыт автоматически: на него не ответили за DRAFT_EXPIRE_HOURS
    EXPIRED = "expired"


class SourceType(str, enum.Enum):
    RSS = "rss"
    # обычная страница со списком новостей — для сайтов без RSS-фида
    HTML = "html"
    # публичный Telegram-канал, читается через веб-версию t.me/s/<имя>
    TELEGRAM = "telegram"


class Source(Base):
    """Источник новостей (RSS-фид, HTML-страница или Telegram-канал), добавляемый/
    удаляемый через команды бота."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    # SQLAlchemy хранит Python-enum по .name ("RSS"/"HTML"), а не .value —
    # server_default должен совпадать с тем, что реально создаётся как метка
    # нативного enum-типа в Postgres, иначе CREATE TABLE падает с
    # InvalidTextRepresentationError.
    source_type: Mapped[SourceType] = mapped_column(
        default=SourceType.RSS, server_default=SourceType.RSS.name
    )
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    news_items: Mapped[list["PostedNews"]] = relationship(back_populates="source")

    def __repr__(self) -> str:
        return f"<Source id={self.id} url={self.url!r}>"


class PostedNews(Base):
    """Новость, уже увиденная ботом — используется для дедупликации по URL/хэшу."""

    __tablename__ = "posted_news"
    __table_args__ = (UniqueConstraint("url", name="uq_posted_news_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    guid: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Анонс из источника — нужен, чтобы сгенерировать другой вариант текста
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[NewsStatus] = mapped_column(default=NewsStatus.PENDING)
    # timezone=True: published_at/posted_at всегда приходят как aware UTC-datetime
    # (datetime.now(timezone.utc) / распарсенные из RSS-HTML даты) — naive-колонка
    # отказывается их принимать (asyncpg роняет INSERT с DataError).
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    source: Mapped["Source | None"] = relationship(back_populates="news_items")
    draft: Mapped["Draft | None"] = relationship(back_populates="news", uselist=False)

    def __repr__(self) -> str:
        return f"<PostedNews id={self.id} status={self.status} url={self.url!r}>"


class Draft(Base):
    """Черновик поста, отправленный админу на модерацию."""

    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    news_id: Mapped[int] = mapped_column(
        ForeignKey("posted_news.id", ondelete="CASCADE"), unique=True
    )
    text: Mapped[str] = mapped_column(Text)
    photo_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_link: Mapped[str] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    news: Mapped["PostedNews"] = relationship(back_populates="draft")
    notifications: Mapped[list["DraftNotification"]] = relationship(
        back_populates="draft", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Draft id={self.id} news_id={self.news_id}>"


class DraftNotification(Base):
    """Одна из копий черновика, разосланных админам (по одной на каждого из
    ADMIN_CHAT_IDS) — нужна, чтобы после approve/reject убрать кнопки у всех,
    а не только у того, кто нажал."""

    __tablename__ = "draft_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id", ondelete="CASCADE"))
    admin_chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)

    draft: Mapped["Draft"] = relationship(back_populates="notifications")

    def __repr__(self) -> str:
        return f"<DraftNotification draft_id={self.draft_id} admin_chat_id={self.admin_chat_id}>"


class BotSetting(Base):
    """Единственная строка (id=1) рантайм-настроек, изменяемых из меню бота
    без правки .env и перезапуска: интервал автогенерации и системный промпт
    нейросети (тема, длина поста, упоминание фото и т.п. — всё это просто текст
    промпта)."""

    __tablename__ = "bot_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_interval_minutes: Mapped[int] = mapped_column(default=60)
    # NULL = использовать дефолтный промпт, собранный из BOT_TOPIC
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "interval" (раз в draft_interval_minutes) или "weekly" (по дням недели
    # и фиксированному времени, см. weekly_days/weekly_times) — переключается
    # кнопками в "⏱ Расписание", см. bot/services/scheduler.py::apply_schedule.
    schedule_mode: Mapped[str] = mapped_column(
        String(16), default="interval", server_default="interval"
    )
    # Comma-separated: дни недели ("mon,tue,wed,...") и время ("09:00,18:00").
    # Одно и то же время применяется ко всем выбранным дням.
    weekly_days: Mapped[str] = mapped_column(
        String(32),
        default="mon,tue,wed,thu,fri,sat,sun",
        server_default="mon,tue,wed,thu,fri,sat,sun",
    )
    weekly_times: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # Нейросеть, выбранная в меню «🤖 Нейросеть». NULL = AI_PROVIDER из .env
    ai_provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Фильтр новостей из меню «🚫 Фильтр»: слова через запятую, в нижнем
    # регистре. NULL/пусто = фильтр не задан. См. bot/services/news_filter.py
    filter_stop_words: Mapped[str | None] = mapped_column(Text, nullable=True)
    filter_required_words: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<BotSetting draft_interval_minutes={self.draft_interval_minutes}>"


class Topic(Base):
    """Маппинг «название темы форума» -> message_thread_id в целевой группе."""

    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    thread_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<Topic id={self.id} name={self.name!r} thread_id={self.thread_id}>"


class QaUsage(Base):
    """Счётчик вопросов боту в группе за текущее суточное окно. Одна строка на
    участника (user_id из Telegram) и одна общая на всех — с user_id=0. Окно
    начинается с первого вопроса; см. bot/services/qa.py."""

    __tablename__ = "qa_usage"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int] = mapped_column(Integer, default=0)
    # об исчерпанном лимите уже сказали — до конца окна молчим
    limit_notified: Mapped[bool] = mapped_column(Boolean, default=False)

    def __repr__(self) -> str:
        return f"<QaUsage user_id={self.user_id} count={self.count}>"
