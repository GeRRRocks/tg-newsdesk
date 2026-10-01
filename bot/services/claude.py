"""Обёртка над Anthropic API: рерайт новости в текст поста для Telegram."""

from __future__ import annotations

import logging

import anthropic

from bot.config import get_settings
from bot.db.models import BotSetting
from bot.db.session import async_session_factory
from bot.services.news import NewsItem

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5"
MAX_TOKENS = 2048


def build_default_system_prompt(topic: str) -> str:
    return (
        f"Ты — редактор Telegram-канала на тему «{topic}». Перепиши присланную "
        "новость в короткий пост для Telegram: 3-6 предложений, живым языком, "
        "без канцелярита и без вступлений вроде «Вот пост:». Используй только "
        "факты из присланного текста, ничего не придумывай и не добавляй. Не "
        "используй markdown-разметку и хэштеги; эмодзи — умеренно и только по "
        "смыслу. Не добавляй ссылку на источник — она будет прикреплена "
        "отдельно. В ответе — только текст поста."
    )


async def get_system_prompt() -> str:
    """Текущий системный промпт: кастомный (если админ поменял его через меню
    «🏷 Промпт»), иначе дефолтный, собранный из BOT_TOPIC."""
    async with async_session_factory() as session:
        setting = await session.get(BotSetting, 1)
        if setting is not None and setting.system_prompt:
            return setting.system_prompt
    return build_default_system_prompt(get_settings().bot_topic)


async def set_system_prompt(prompt: str | None) -> None:
    """Сохраняет кастомный промпт. prompt=None — сброс на дефолтный из BOT_TOPIC."""
    async with async_session_factory() as session:
        setting = await session.get(BotSetting, 1)
        if setting is None:
            session.add(BotSetting(id=1, system_prompt=prompt))
        else:
            setting.system_prompt = prompt
        await session.commit()


_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=get_settings().anthropic_api_key)
    return _client


async def generate_post_text(item: NewsItem) -> str | None:
    """Переписывает новость в стиль Telegram-поста через Claude. Считается
    некритичной операцией: при отказе API, лимите запросов или сетевой ошибке
    возвращает None, не роняя цикл планировщика — эту новость подхватит
    следующий запуск."""
    client = _get_client()
    user_content = (
        f"Заголовок: {item.title}\n\nТекст анонса: {item.summary or '(отсутствует)'}"
    )

    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=await get_system_prompt(),
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.RateLimitError as exc:
        logger.warning("Claude API: превышен лимит запросов (%s)", exc.message)
        return None
    except anthropic.APIStatusError as exc:
        logger.warning("Claude API вернул ошибку (status=%s): %s", exc.status_code, exc.message)
        return None
    except anthropic.APIConnectionError as exc:
        logger.warning("Claude API недоступен: %s", exc)
        return None

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text or None
