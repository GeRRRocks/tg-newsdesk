"""Сбор новостей из публичных Telegram-каналов через их веб-версию.

У публичного канала есть страница https://t.me/s/<имя> с последними постами
(около двадцати), которая открывается без входа в Telegram. Бота в чужой
канал добавить нельзя, а отдельный пользовательский аккаунт для этого не
нужен. Закрытые каналы и каналы с отключённым веб-просмотром так не читаются:
для них страница отвечает редиректом, и источник считается неоткрывшимся.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup, Tag

from bot.db.models import Source
from bot.services.news import NewsItem, _content_hash, _fetch_bytes

# Имя канала: латиница, цифры и подчёркивание, начинается с буквы
_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_LINK_RE = re.compile(
    r"(?:https?://)?(?:t|telegram)\.me/(?:s/)?(?P<name>[A-Za-z][A-Za-z0-9_]{3,31})(?:[/?#].*)?",
    re.IGNORECASE,
)
_STYLE_URL_RE = re.compile(r"url\('([^']+)'\)")

# Пост короче этого — подпись к картинке или репост без текста, а не новость
_MIN_TEXT_LEN = 80
_MAX_TITLE_LEN = 200
_MAX_SUMMARY_LEN = 4000


def channel_username(text: str) -> str | None:
    """Имя канала из того, что прислал админ: @name, t.me/name, t.me/s/name
    или ссылка на отдельный пост. None — если это не похоже на канал."""
    text = text.strip()
    if text.startswith("@"):
        name = text[1:]
        return name if _USERNAME_RE.fullmatch(name) else None
    match = _LINK_RE.fullmatch(text)
    return match.group("name") if match else None


def channel_url(username: str) -> str:
    return f"https://t.me/s/{username}"


def _post_text(node: Tag) -> str:
    # get_text с разделителем рвал бы строку на каждой ссылке и выделении,
    # поэтому переносы оставляем только там, где в посте стоит <br>.
    for br in node.find_all("br"):
        br.replace_with("\n")
    lines = (" ".join(line.split()) for line in node.get_text().split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _post_title(text: str) -> str | None:
    """Заголовком считаем первую строку, в которой есть слова: у поста нет
    отдельного заголовка, а первая строка бывает из одних эмодзи."""
    for line in text.split("\n"):
        if sum(ch.isalnum() for ch in line) >= 10:
            # убираем эмодзи и знаки в начале строки
            line = re.sub(r"^[^\w«\"(]+", "", line)
            if len(line) > _MAX_TITLE_LEN:
                line = line[: _MAX_TITLE_LEN - 1].rstrip() + "…"
            return line
    return None


def _post_image(message: Tag) -> str | None:
    for selector in (".tgme_widget_message_photo_wrap", ".tgme_widget_message_video_thumb"):
        node = message.select_one(selector)
        match = _STYLE_URL_RE.search(node.get("style", "")) if node else None
        if match:
            url = match.group(1)
            return "https:" + url if url.startswith("//") else url
    return None


def _post_published(message: Tag) -> datetime | None:
    node = message.select_one(".tgme_widget_message_date time[datetime]")
    if node is None:
        return None
    try:
        return datetime.fromisoformat(node["datetime"])
    except ValueError:
        return None


def parse_channel_page(source_id: int, html: bytes) -> list[NewsItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[NewsItem] = []
    for message in soup.select("div.tgme_widget_message[data-post]"):
        post = message["data-post"]  # "<канал>/<номер поста>"
        text_node = message.select_one(".tgme_widget_message_text")
        if text_node is None or not re.fullmatch(r"[A-Za-z0-9_]+/\d+", post):
            continue
        text = _post_text(text_node)
        title = _post_title(text)
        if len(text) < _MIN_TEXT_LEN or title is None:
            continue
        url = f"https://t.me/{post}"
        items.append(
            NewsItem(
                source_id=source_id,
                title=title,
                url=url,
                guid=post,
                summary=text[:_MAX_SUMMARY_LEN],
                published_at=_post_published(message),
                image_url=_post_image(message),
                content_hash=_content_hash(title, url),
            )
        )
    return items


async def fetch_telegram_source_items(
    client: httpx.AsyncClient, source: Source
) -> list[NewsItem] | None:
    """Скачивает веб-версию канала source.url и разбирает посты. Если страница
    не открылась — как и для остальных типов — возвращает None."""
    raw = await _fetch_bytes(client, source.url)
    if raw is None:
        return None
    return await asyncio.to_thread(parse_channel_page, source.id, raw)
