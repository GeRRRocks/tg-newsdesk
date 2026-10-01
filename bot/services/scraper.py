"""Сбор новостей со страниц-«витрин» без RSS.

Подход генерический: на странице-списке (source.url) ищем ссылки, похожие на
статьи, затем у каждой кандидатной страницы читаем og-теги (title/description/
image) — так же, как это делают мессенджеры при превью ссылок. Специфичной под
конкретный сайт вёрстки не требует, но и не гарантирует идеальный результат на
любом сайте.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from bot.db.models import Source
from bot.services.news import NewsItem, _content_hash, _fetch_bytes, normalize_article_url

# Фрагменты пути, которые почти никогда не ведут на статью, а на служебные
# разделы сайта (теги, категории, пагинация, авторы, поиск и т.п.)
_SKIP_PATH_PARTS = (
    "/tag/", "/tags/", "/category/", "/categories/", "/author/", "/authors/",
    "/page/", "/search", "/login", "/register", "/rss", "/feed",
)

_MIN_LINK_TEXT_LEN = 15
_MAX_CANDIDATES = 20


def _is_article_candidate(base_netloc: str, href: str, text: str) -> bool:
    if not href or len(text.strip()) < _MIN_LINK_TEXT_LEN:
        return False
    if href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return False
    parsed = urlparse(href)
    if parsed.scheme not in ("http", "https") or parsed.netloc != base_netloc:
        return False
    path = parsed.path.lower()
    if path in ("", "/") or path.endswith("/"):
        # путь-«директория» (без имени файла/слага) почти всегда ведёт на
        # раздел/категорию/бренд, а не на конкретную статью
        return False
    return not any(part in path for part in _SKIP_PATH_PARTS)


def _extract_article_links(listing_url: str, html: bytes) -> list[str]:
    """Возвращает до _MAX_CANDIDATES уникальных ссылок на статьи в порядке
    появления на странице (обычно — от новых к старым)."""
    soup = BeautifulSoup(html, "html.parser")
    base_netloc = urlparse(listing_url).netloc

    seen: set[str] = set()
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(listing_url, a["href"].strip()).split("#", 1)[0]
        text = a.get_text(strip=True)
        if not _is_article_candidate(base_netloc, href, text):
            continue
        # Сравниваем по нормализованному URL — иначе одна и та же статья,
        # сославшаяся на себя дважды с разными трекинг-параметрами
        # (?from=newsfeed и т.п.), фетчится и обрабатывается дважды.
        dedup_key = normalize_article_url(href)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        links.append(href)
        if len(links) >= _MAX_CANDIDATES:
            break
    return links


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        content = tag.get("content") if tag else None
        if content:
            return content.strip()
    return None


def _article_published(soup: BeautifulSoup) -> datetime | None:
    value = _meta(soup, "article:published_time", "og:updated_time")
    if not value:
        time_tag = soup.find("time")
        value = time_tag.get("datetime") if time_tag else None
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _parse_article(client: httpx.AsyncClient, source: Source, url: str) -> NewsItem | None:
    raw = await _fetch_bytes(client, url)
    if raw is None:
        return None

    soup = await asyncio.to_thread(BeautifulSoup, raw, "html.parser")

    title = _meta(soup, "og:title")
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    if not title:
        return None

    # Фетчим по исходной ссылке (мало ли трекинг-параметр на что-то влияет на
    # сервере), но как идентификатор новости используем нормализованный URL —
    # см. normalize_article_url.
    canonical_url = normalize_article_url(url)
    return NewsItem(
        source_id=source.id,
        title=title,
        url=canonical_url,
        guid=None,
        summary=_meta(soup, "og:description", "description") or "",
        published_at=_article_published(soup),
        image_url=_meta(soup, "og:image"),
        content_hash=_content_hash(title, canonical_url),
    )


async def fetch_html_source_items(client: httpx.AsyncClient, source: Source) -> list[NewsItem]:
    """Скачивает страницу-список source.url, находит ссылки на статьи и
    парсит og-теги каждой из них. При любой сетевой ошибке — как и для RSS —
    возвращает пустой список, не роняя весь цикл сбора."""
    raw = await _fetch_bytes(client, source.url)
    if raw is None:
        return []

    links = await asyncio.to_thread(_extract_article_links, source.url, raw)
    if not links:
        return []

    articles = await asyncio.gather(*(_parse_article(client, source, url) for url in links))
    return [item for item in articles if item is not None]
