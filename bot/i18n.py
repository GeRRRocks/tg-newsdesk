"""Тексты, которые бот показывает людям, на языке из BOT_LANGUAGE.

Все строки лежат в словарях bot/locales/ru.py и en.py под одинаковыми
ключами; код берёт строку через t("ключ", имя=значение). Язык один на всю
установку и читается из настроек при каждом вызове. Логи на язык не
переводятся.
"""

from __future__ import annotations

from bot.config import get_settings
from bot.locales import en, ru

_CATALOGS: dict[str, dict[str, str]] = {"ru": ru.TEXTS, "en": en.TEXTS}


def language() -> str:
    return get_settings().bot_language


def t(key: str, **params: object) -> str:
    """Строка по ключу на текущем языке. Подстановки — как в str.format;
    без параметров строка возвращается как есть (фигурные скобки не трогаются)."""
    text = _CATALOGS[language()][key]
    return text.format(**params) if params else text
