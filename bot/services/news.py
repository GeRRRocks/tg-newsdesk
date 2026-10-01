"""Парсинг источников новостей (RSS и обычных HTML-страниц) и отбор свежих."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NamedTuple
from urllib.parse import urlparse, urlunparse

import feedparser
import httpx

from bot.db.models import Draft, PostedNews, Source, SourceType
from bot.services.news_filter import WordFilter

logger = logging.getLogger(__name__)

USER_AGENT = "tg-news-bot/1.0 (+https://github.com/GeRRRocks/TG_NEWS_BOT)"

# Потолок на размер одного ответа (после распаковки gzip): страниц качается до
# пары десятков параллельно, и без лимита один тяжёлый ответ съедает память.
_MAX_RESPONSE_BYTES = 3 * 1024 * 1024

# Лимиты колонок, в которые ложатся поля новости — см. _fit_to_columns.
_MAX_URL_LEN = PostedNews.__table__.c.url.type.length
_MAX_TITLE_LEN = PostedNews.__table__.c.title.type.length
_MAX_GUID_LEN = PostedNews.__table__.c.guid.type.length
_MAX_IMAGE_URL_LEN = Draft.__table__.c.photo_url.type.length


@dataclass
class NewsItem:
    source_id: int
    title: str
    url: str
    guid: str | None
    summary: str
    published_at: datetime | None
    image_url: str | None
    content_hash: str


def _entry_image(entry: feedparser.FeedParserDict) -> str | None:
    media_content = entry.get("media_content")
    if media_content:
        url = media_content[0].get("url")
        if url:
            return url

    media_thumbnail = entry.get("media_thumbnail")
    if media_thumbnail:
        url = media_thumbnail[0].get("url")
        if url:
            return url

    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image/"):
            return link.get("href")

    return None


def _entry_published(entry: feedparser.FeedParserDict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc)
    return None


def normalize_article_url(url: str) -> str:
    """Убирает query-string и fragment. Некоторые сайты (например, autonews.ru)
    отдают разную ссылку на одну и ту же статью в зависимости от того, с какой
    страницы на неё сослались (?from=newsfeed и т.п.) — без нормализации это
    выглядело бы как две разные новости и дедупликация по URL не срабатывала."""
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query="", fragment=""))


def _content_hash(title: str, url: str) -> str:
    return hashlib.sha256(f"{title}|{url}".encode("utf-8")).hexdigest()


async def _is_public_host(host: str) -> bool:
    """True, только если все адреса, в которые резолвится host, публичные.
    Отсекает localhost, приватные сети, link-local (метаданные облака
    169.254.169.254) и имена сервисов внутренней docker-сети."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


async def _fetch_bytes(client: httpx.AsyncClient, url: str) -> bytes | None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    # URL источника задаёт админ, а ссылки на статьи — сам сайт-источник, так
    # что без этой проверки бот можно направить на внутренние адреса (SSRF).
    # Редиректы httpx по умолчанию не проходит — обойти проверку через них нельзя.
    if not await _is_public_host(parsed.hostname):
        logger.warning("Пропускаю %s: адрес не резолвится или не публичный", url)
        return None

    try:
        async with client.stream(
            "GET", url, timeout=15.0, headers={"User-Agent": USER_AGENT}
        ) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _MAX_RESPONSE_BYTES:
                    logger.warning("Пропускаю %s: ответ больше %s байт", url, _MAX_RESPONSE_BYTES)
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError:
        return None


def _fit_to_columns(item: NewsItem) -> NewsItem | None:
    """Подгоняет поля под лимиты колонок БД. Без этого новость с аномально
    длинным заголовком или адресом картинки роняла бы вставку, а следующий
    запуск выбирал бы её же снова — генерация вставала бы намертво. URL
    обрезать нельзя (по нему идёт дедупликация), поэтому такую новость
    пропускаем целиком."""
    if len(item.url) > _MAX_URL_LEN:
        logger.warning("Пропускаю новость со слишком длинным URL (%s символов)", len(item.url))
        return None
    item.title = item.title[:_MAX_TITLE_LEN]
    if item.guid is not None:
        item.guid = item.guid[:_MAX_GUID_LEN]
    if item.image_url is not None and len(item.image_url) > _MAX_IMAGE_URL_LEN:
        item.image_url = None
    return item


async def fetch_source_items(
    client: httpx.AsyncClient, source: Source
) -> list[NewsItem] | None:
    """Скачивает и парсит один источник. Для RSS — сам фид, для обычной страницы
    (source.source_type == HTML) — делегирует в bot.services.scraper, который
    находит ссылки на статьи и вытаскивает их og-теги. Если источник не
    открылся, возвращает None, не роняя весь цикл сбора новостей."""
    if source.source_type == SourceType.HTML:
        from bot.services.scraper import fetch_html_source_items

        return await fetch_html_source_items(client, source)

    raw = await _fetch_bytes(client, source.url)
    if raw is None:
        return None

    parsed = await asyncio.to_thread(feedparser.parse, raw)

    items: list[NewsItem] = []
    for entry in parsed.entries:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        if not title or not link:
            continue
        link = normalize_article_url(link)
        items.append(
            NewsItem(
                source_id=source.id,
                title=title,
                url=link,
                guid=entry.get("id") or entry.get("guid"),
                summary=entry.get("summary", "").strip(),
                published_at=_entry_published(entry),
                image_url=_entry_image(entry),
                content_hash=_content_hash(title, link),
            )
        )
    return items


class CollectResult(NamedTuple):
    item: NewsItem | None
    # Источники, которые не открылись или не отдали ни одной новости
    failed_sources: list[Source]


async def collect_latest_unseen(
    sources: list[Source], seen_urls: set[str], word_filter: WordFilter | None = None
) -> CollectResult:
    """Опрашивает все активные источники параллельно и возвращает самую свежую
    новость, которой ещё нет в seen_urls (история публикаций/отклонений) и
    которая проходит фильтр по словам."""
    if not sources:
        return CollectResult(None, [])

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(fetch_source_items(client, s) for s in sources), return_exceptions=True
        )

    failed: list[Source] = []
    all_items: list[NewsItem] = []
    for source, result in zip(sources, results):
        if isinstance(result, BaseException):
            logger.warning("Источник %s: ошибка при сборе: %r", source.url, result)
            failed.append(source)
        elif not result:
            failed.append(source)
        else:
            all_items.extend(result)

    fitted = (_fit_to_columns(item) for item in all_items)
    candidates = [
        item
        for item in fitted
        if item is not None
        and item.url not in seen_urls
        and (word_filter is None or word_filter.allows(item.title, item.summary))
    ]
    if not candidates:
        return CollectResult(None, failed)

    # Перемешиваем до сортировки: у большинства HTML-источников published_at
    # неизвестен (None), и без шаффла стабильная сортировка каждый раз
    # выбирала бы источник, который просто раньше в списке — из-за этого не
    # было разнообразия. Реальные даты (когда есть) по-прежнему в приоритете.
    random.shuffle(candidates)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    candidates.sort(key=lambda item: item.published_at or epoch, reverse=True)
    return CollectResult(candidates[0], failed)
