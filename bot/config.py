from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    # Какой нейросетью переписывать новости. Нужен ключ только выбранного
    # провайдера; ai_model — необязательная замена модели по умолчанию.
    ai_provider: Literal["anthropic", "openai", "gemini", "deepseek"] = "anthropic"
    ai_model: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    deepseek_api_key: str | None = None
    # список chat_id админов через запятую (например "111,222") — у каждого
    # свои права на команды, черновики на модерацию рассылаются всем сразу
    admin_chat_ids: str
    target_group_chat_id: int
    target_topic_id: int | None = None
    database_url: str
    timezone: str = "Europe/Moscow"
    draft_interval_minutes: int = 60
    # Через сколько часов без ответа черновик закрывается сам. 0 — никогда.
    draft_expire_hours: int = 24
    # Тематика канала — вставляется в системный промпт нейросети. Источники
    # (RSS/HTML) уже тематически нейтральны, так что смена темы + источников
    # достаточна, чтобы превратить бота в канал про что угодно.
    # Не задана — берётся тема по умолчанию на языке бота (см. bot/locales).
    bot_topic: str | None = None
    # Язык меню, сообщений админам и промпта по умолчанию.
    bot_language: Literal["ru", "en"] = "ru"
    # Ответы нейросети на вопросы участников целевой группы (упоминание бота).
    # Лимиты — за 24 часа с первого вопроса: на одного участника и на всех.
    qa_enabled: bool = False
    # Топик группы, в котором бот отвечает на вопросы. Не задан — в любом.
    qa_topic_id: int | None = None
    qa_user_daily_limit: int = 3
    qa_global_daily_limit: int = 50

    @field_validator(
        "ai_model",
        "bot_topic",
        "qa_topic_id",
        "anthropic_api_key",
        "openai_api_key",
        "gemini_api_key",
        "deepseek_api_key",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # В .env строки вида "OPENAI_API_KEY=" остаются пустыми для
        # неиспользуемых провайдеров — считаем их незаданными.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _require_provider_key(self) -> "Settings":
        if not self.ai_api_key:
            raise ValueError(
                f"AI_PROVIDER={self.ai_provider}, но {self.ai_provider.upper()}_API_KEY не задан в .env"
            )
        return self

    def api_key_for(self, provider: str) -> str | None:
        return getattr(self, f"{provider}_api_key")

    @property
    def ai_api_key(self) -> str | None:
        """Ключ провайдера, выбранного в .env."""
        return self.api_key_for(self.ai_provider)

    @property
    def admin_ids(self) -> set[int]:
        return {int(chat_id.strip()) for chat_id in self.admin_chat_ids.split(",") if chat_id.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
