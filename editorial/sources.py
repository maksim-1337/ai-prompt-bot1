from __future__ import annotations

import calendar
import html
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests
from bs4 import BeautifulSoup

# Only editorial news feeds. Habr articles/blogs and aggregators are not sources.
FEEDS = (
    ("3DNews", "https://3dnews.ru/news/rss/"),
    ("iXBT", "https://www.ixbt.com/export/news.rss"),
    ("Lenta.ru", "https://lenta.ru/rss"),
    ("РИА Новости", "https://ria.ru/export/rss2/archive/index.xml"),
    ("Коммерсантъ", "https://www.kommersant.ru/RSS/news.xml"),
)
HOSTS = {"3dnews.ru", "ixbt.com", "lenta.ru", "ria.ru", "kommersant.ru"}
HEADERS = {"User-Agent": "NewsLightGG-Editorial/3.0 (RSS reader)"}


def clean(value: str) -> str:
    value = html.unescape(value or "")
    return re.sub(r"\s+", " ", BeautifulSoup(value, "html.parser").get_text(" ")).strip()


def canonical_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Source URL must be public HTTPS")
    host = parts.hostname.lower().removeprefix("www.")
    if host not in HOSTS or parts.port not in (None, 443):
        raise ValueError("Source is outside the editorial allowlist")
    return urlunsplit(("https", host, parts.path.rstrip("/"), "", ""))


@dataclass
class Story:
    title: str
    summary: str
    source: str
    url: str
    published: datetime


def from_entry(source: str, entry) -> Story | None:
    # An undated item must not become fresh just because it was fetched now.
    date = entry.get("published_parsed")
    if not date:
        return None
    try:
        published = datetime.fromtimestamp(calendar.timegm(date), timezone.utc)
        url = canonical_url(entry.get("link", ""))
    except (ValueError, TypeError, OverflowError):
        return None
    title = clean(entry.get("title", ""))
    summary = clean(entry.get("summary", entry.get("description", "")))
    summary = re.split(r"Читать (?:далее|полностью)|Read more", summary, flags=re.I)[0].strip()
    if len(title) < 25 or len(title) > 250:
        return None
    return Story(title, summary[:2200], source, url, published)


def fetch_feed(feed) -> tuple[list[Story], str]:
    source, url = feed
    try:
        response = requests.get(url, headers=HEADERS, timeout=(8, 18))
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        if not parsed.entries:
            return [], f"{source}: empty/invalid RSS"
        stories = [story for entry in parsed.entries[:80] if (story := from_entry(source, entry))]
        return stories, f"{source}: {len(stories)} dated news items"
    except requests.RequestException as exc:
        return [], f"{source}: unavailable ({type(exc).__name__})"


def fetch_stories() -> tuple[list[Story], list[str]]:
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(fetch_feed, FEEDS))
    return [s for stories, _ in results for s in stories], [note for _, note in results]


def enrich(story: Story) -> Story:
    """Fetch text only; follow redirects only within the news allowlist."""
    url = story.url
    try:
        for _ in range(4):
            response = requests.get(url, headers=HEADERS, timeout=(6, 12), allow_redirects=False)
            if response.is_redirect:
                from urllib.parse import urljoin
                url = canonical_url(urljoin(url, response.headers["Location"]))
                continue
            response.raise_for_status()
            soup = BeautifulSoup(response.text[:700000], "html.parser")
            # Article text only: avoid menus, comments, recommendations and ads.
            article = soup.select_one('[itemprop="articleBody"], .article__text, .article_text, .article__body, .b-article__text')
            if article:
                paragraphs = [clean(p.get_text(" ")) for p in article.select("p")]
                text = " ".join(p for p in paragraphs[:8] if len(p) >= 45)
                if len(text) > len(story.summary):
                    story.summary = text[:3200]
            return story
    except (requests.RequestException, ValueError, KeyError):
        pass
    return story
