"""Определение типа источника по адресу, который прислал админ.

Админ может прислать как адрес фида, так и главную страницу сайта. Порядок:
сам адрес — фид? → фид, объявленный на странице через <link rel="alternate">
→ фид по одному из типовых путей → обычная страница со ссылками на статьи.
RSS предпочтительнее HTML: в нём есть даты и не нужно угадывать вёрстку.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from bot.db.models import SourceType
from bot.services.news import _fetch_bytes
from bot.services.scraper import _extract_article_links

_FEED_MIME_TYPES = ("application/rss+xml", "application/atom+xml")
# Больше фидов на странице не проверяем: у некоторых сайтов их десятки (по рубрикам)
_MAX_DECLARED_FEEDS = 5
# Типовые адреса фидов — на случай, когда страница не открылась или фид на ней не объявлен
_COMMON_FEED_PATHS = ("/rss", "/rss.xml", "/feed", "/feed/", "/rss/", "/atom.xml", "/index.xml", "/exports/rss")


@dataclass
class DetectedSource:
    source_type: SourceType
    url: str
    # сколько новостей (RSS) или ссылок на статьи (HTML) нашлось при проверке
    items: int


def _feed_entries(raw: bytes) -> int:
    parsed = feedparser.parse(raw)
    return sum(1 for entry in parsed.entries if entry.get("title") and entry.get("link"))


def _declared_feeds(page_url: str, html: bytes) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    feeds: list[str] = []
    for link in soup.find_all("link", href=True):
        rel = link.get("rel") or []
        if "alternate" not in rel or str(link.get("type", "")).lower() not in _FEED_MIME_TYPES:
            continue
        url = urljoin(page_url, link["href"].strip())
        if url not in feeds:
            feeds.append(url)
    return feeds[:_MAX_DECLARED_FEEDS]


async def _first_working_feed(client: httpx.AsyncClient, urls: list[str]) -> DetectedSource | None:
    """Первый по порядку адрес из urls, по которому отдаётся непустой фид."""

    async def check(url: str) -> int:
        raw = await _fetch_bytes(client, url)
        return await asyncio.to_thread(_feed_entries, raw) if raw is not None else 0

    counts = await asyncio.gather(*(check(url) for url in urls))
    for url, count in zip(urls, counts):
        if count:
            return DetectedSource(SourceType.RSS, url, count)
    return None


async def detect_source(url: str) -> DetectedSource | None:
    """Определяет, как читать источник по адресу url. None — ни фид, ни
    страницу со ссылками на статьи найти не удалось."""
    async with httpx.AsyncClient() as client:
        raw = await _fetch_bytes(client, url)

        if raw is not None:
            count = await asyncio.to_thread(_feed_entries, raw)
            if count:
                return DetectedSource(SourceType.RSS, url, count)
            declared = await asyncio.to_thread(_declared_feeds, url, raw)
            found = await _first_working_feed(client, declared)
            if found is not None:
                return found

        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        found = await _first_working_feed(client, [root + path for path in _COMMON_FEED_PATHS])
        if found is not None:
            return found

        if raw is not None:
            links = await asyncio.to_thread(_extract_article_links, url, raw)
            if links:
                return DetectedSource(SourceType.HTML, url, len(links))
    return None
