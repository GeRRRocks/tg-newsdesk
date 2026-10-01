"""Рерайт новости в текст поста для Telegram через выбранную нейросеть.

Провайдер по умолчанию задаётся в .env (AI_PROVIDER), а переключается на
лету из меню «🤖 Нейросеть» — между теми, чьи ключи есть в .env. Claude вызывается через Anthropic
SDK, остальные — OpenAI, Gemini и DeepSeek — через один и тот же
OpenAI-совместимый эндпоинт chat/completions, отличаются только адрес,
модель по умолчанию и имя параметра с лимитом токенов."""

from __future__ import annotations

import logging
from typing import NamedTuple

import anthropic
import httpx

from bot.config import get_settings
from bot.db.models import BotSetting
from bot.db.session import async_session_factory
from bot.i18n import t
from bot.services.news import NewsItem

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL = "claude-opus-5"
ANTHROPIC_MAX_TOKENS = 2048


class _OpenAICompatible(NamedTuple):
    label: str
    url: str
    default_model: str
    # У моделей с рассуждением лимит включает и скрытые токены рассуждения,
    # поэтому он заметно больше, чем нужно самому посту.
    max_tokens_param: str


_OPENAI_COMPATIBLE = {
    "openai": _OpenAICompatible(
        "OpenAI",
        "https://api.openai.com/v1/chat/completions",
        "gpt-5.4-mini",
        "max_completion_tokens",
    ),
    "gemini": _OpenAICompatible(
        "Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "gemini-3.8-flash",
        "max_tokens",
    ),
    "deepseek": _OpenAICompatible(
        "DeepSeek",
        "https://api.deepseek.com/chat/completions",
        "deepseek-flash",
        "max_tokens",
    ),
}
_OPENAI_COMPATIBLE_MAX_TOKENS = 4096
_HTTP_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


# Порядок — как в меню
PROVIDER_LABELS = {
    "anthropic": "Claude",
    "openai": "GPT",
    "gemini": "Gemini",
    "deepseek": "DeepSeek",
}


def available_providers() -> list[str]:
    """Провайдеры, для которых в .env есть ключ."""
    settings = get_settings()
    return [name for name in PROVIDER_LABELS if settings.api_key_for(name)]


def model_for(provider: str) -> str:
    """AI_MODEL относится только к провайдеру из .env: у остальных названия
    моделей другие, поэтому для них берётся модель по умолчанию."""
    settings = get_settings()
    if settings.ai_model and provider == settings.ai_provider:
        return settings.ai_model
    if provider == "anthropic":
        return ANTHROPIC_MODEL
    return _OPENAI_COMPATIBLE[provider].default_model


async def get_active_provider() -> str:
    """Провайдер, выбранный в меню; если выбора нет или его ключ убрали из
    .env — провайдер из AI_PROVIDER (его ключ проверяется при старте)."""
    async with async_session_factory() as session:
        setting = await session.get(BotSetting, 1)
    chosen = setting.ai_provider if setting is not None else None
    if chosen in PROVIDER_LABELS and get_settings().api_key_for(chosen):
        return chosen
    return get_settings().ai_provider


async def set_active_provider(provider: str) -> None:
    async with async_session_factory() as session:
        setting = await session.get(BotSetting, 1)
        if setting is None:
            session.add(BotSetting(id=1, ai_provider=provider))
        else:
            setting.ai_provider = provider
        await session.commit()


async def describe_provider() -> str:
    """Активный провайдер и модель одной строкой — для лога при старте."""
    provider = await get_active_provider()
    return f"{PROVIDER_LABELS[provider]}, модель {model_for(provider)}"


def build_default_system_prompt(topic: str) -> str:
    return t("ai.default_prompt", topic=topic)


def default_system_prompt() -> str:
    """Промпт по умолчанию на языке бота: тема — BOT_TOPIC, а если она не
    задана, тема по умолчанию для этого языка."""
    return build_default_system_prompt(get_settings().bot_topic or t("default_topic"))


async def get_system_prompt() -> str:
    """Текущий системный промпт: кастомный (если админ поменял его через меню
    «🏷 Промт»), иначе дефолтный, собранный из BOT_TOPIC."""
    async with async_session_factory() as session:
        setting = await session.get(BotSetting, 1)
        if setting is not None and setting.system_prompt:
            return setting.system_prompt
    return default_system_prompt()


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


async def _generate_anthropic(system_prompt: str, user_content: str) -> str | None:
    try:
        response = await _get_client().messages.create(
            model=model_for("anthropic"),
            max_tokens=ANTHROPIC_MAX_TOKENS,
            system=system_prompt,
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


_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    return _http_client


async def _generate_openai_compatible(
    name: str, system_prompt: str, user_content: str
) -> str | None:
    provider = _OPENAI_COMPATIBLE[name]
    api_key = get_settings().api_key_for(name)
    payload = {
        "model": model_for(name),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        provider.max_tokens_param: _OPENAI_COMPATIBLE_MAX_TOKENS,
    }
    try:
        response = await _get_http_client().post(
            provider.url, json=payload, headers={"Authorization": f"Bearer {api_key}"}
        )
    except httpx.HTTPError as exc:
        logger.warning("%s API недоступен: %s", provider.label, exc)
        return None

    if response.status_code != 200:
        # Тело ответа — описание ошибки от провайдера (неверная модель, лимит,
        # баланс); ключ в нём не возвращается.
        logger.warning(
            "%s API вернул ошибку (status=%s): %s",
            provider.label,
            response.status_code,
            response.text[:500],
        )
        return None

    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, LookupError, TypeError):
        logger.warning("%s API: неожиданный формат ответа: %s", provider.label, response.text[:500])
        return None
    if not isinstance(content, str):
        return None
    return content.strip() or None


async def _complete(system_prompt: str, user_content: str) -> str | None:
    """Один запрос к выбранной нейросети; None — отказ API, лимит или сеть."""
    provider = await get_active_provider()
    if provider == "anthropic":
        return await _generate_anthropic(system_prompt, user_content)
    return await _generate_openai_compatible(provider, system_prompt, user_content)


async def generate_text(title: str, summary: str | None, previous: str | None = None) -> str | None:
    """Переписывает новость в стиль Telegram-поста через выбранную нейросеть.
    previous — уже показанный админу вариант: нейросеть просят написать иначе.
    Считается некритичной операцией: при отказе API, лимите запросов или
    сетевой ошибке возвращает None, не роняя цикл планировщика."""
    system_prompt = await get_system_prompt()
    user_content = t("ai.user_content", title=title, summary=summary or t("ai.no_summary"))
    if previous:
        user_content += t("ai.previous", previous=previous)
    return await _complete(system_prompt, user_content)


async def generate_system_prompt(
    description: str, previous: str | None = None, kind: str = "post"
) -> str | None:
    """Составляет по описанию админа промпт для постов (kind="post") или
    инструкцию для ответов на вопросы (kind="qa") через выбранную нейросеть.
    previous — уже показанный вариант: нейросеть просят написать иначе.
    None — нейросеть не ответила."""
    user_content = t("ai.meta_user", description=description)
    if previous:
        user_content += t("ai.meta_previous", previous=previous)
    return await _complete(t("ai.meta_qa_prompt" if kind == "qa" else "ai.meta_prompt"), user_content)


def default_qa_prompt() -> str:
    return t("ai.qa_default", topic=get_settings().bot_topic or t("default_topic"))


def qa_rules() -> str:
    """Защитные правила ответов на вопросы. Дописываются к инструкции при
    каждом запросе и из меню не меняются: вопросы задают не админы."""
    return t("ai.qa_rules")


async def get_qa_prompt() -> str:
    """Редактируемая часть инструкции для ответов на вопросы: заданная через
    меню «🏷 Промт», иначе стандартная, собранная из BOT_TOPIC."""
    async with async_session_factory() as session:
        setting = await session.get(BotSetting, 1)
        if setting is not None and setting.qa_prompt:
            return setting.qa_prompt
    return default_qa_prompt()


async def set_qa_prompt(prompt: str | None) -> None:
    """Сохраняет инструкцию для ответов. prompt=None — сброс на стандартную."""
    async with async_session_factory() as session:
        setting = await session.get(BotSetting, 1)
        if setting is None:
            session.add(BotSetting(id=1, qa_prompt=prompt))
        else:
            setting.qa_prompt = prompt
        await session.commit()


async def answer_question(question: str) -> str | None:
    """Ответ на вопрос участника группы. Правила идут после инструкции админа,
    чтобы её текст не мог их отменить. None — нейросеть не ответила."""
    system_prompt = f"{await get_qa_prompt()}\n\n{qa_rules()}"
    return await _complete(system_prompt, question)


async def generate_post_text(item: NewsItem) -> str | None:
    """Текст поста для свежей новости; None — эту новость подхватит следующий запуск."""
    return await generate_text(item.title, item.summary)
