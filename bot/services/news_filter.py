"""Фильтр новостей по словам, настраиваемый из меню «🚫 Фильтр».

Применяется до генерации, поэтому отсеянная новость не стоит запроса к
нейросети. В историю (posted_news) она не попадает — просто пропускается при
каждом сборе, и после смены фильтра снова может быть выбрана."""

from __future__ import annotations

from typing import NamedTuple

from bot.db.models import BotSetting
from bot.db.session import async_session_factory

MAX_WORDS = 100
MAX_WORD_LEN = 64


class WordFilter(NamedTuple):
    stop_words: list[str]
    required_words: list[str]

    def allows(self, title: str, summary: str | None) -> bool:
        """Нет ни одного стоп-слова и, если заданы обязательные слова, есть
        хотя бы одно из них. Ищется вхождение без учёта регистра, так что
        «кроссовер» ловит и «кроссоверы»."""
        text = f"{title}\n{summary or ''}".lower()
        if any(word in text for word in self.stop_words):
            return False
        if self.required_words and not any(word in text for word in self.required_words):
            return False
        return True


def parse_words(raw: str | None) -> list[str]:
    """Слова или фразы через запятую либо с новой строки -> список без
    повторов, в нижнем регистре."""
    words: list[str] = []
    for chunk in (raw or "").replace("\n", ",").split(","):
        word = " ".join(chunk.lower().split())[:MAX_WORD_LEN]
        if word and word not in words:
            words.append(word)
    return words[:MAX_WORDS]


async def get_word_filter() -> WordFilter:
    async with async_session_factory() as session:
        setting = await session.get(BotSetting, 1)
    if setting is None:
        return WordFilter([], [])
    return WordFilter(
        parse_words(setting.filter_stop_words), parse_words(setting.filter_required_words)
    )


async def set_words(field: str, words: list[str]) -> None:
    """field — "filter_stop_words" или "filter_required_words"."""
    value = ",".join(words) or None
    async with async_session_factory() as session:
        setting = await session.get(BotSetting, 1)
        if setting is None:
            session.add(BotSetting(id=1, **{field: value}))
        else:
            setattr(setting, field, value)
        await session.commit()
