from __future__ import annotations

import time
from datetime import datetime, timezone

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import main as news

DIRECT_FEEDS = [
    ("Habr", "https://habr.com/ru/rss/articles/?fl=ru"),
    ("iXBT", "https://www.ixbt.com/export/news.rss"),
    ("3DNews", "https://3dnews.ru/news/rss/"),
    ("Lenta.ru", "https://lenta.ru/rss"),
    ("РИА Новости", "https://ria.ru/export/rss2/archive/index.xml"),
    ("Коммерсантъ", "https://www.kommersant.ru/RSS/news.xml"),
]

INTEREST_MARKERS = (
    "ии", "нейросет", "искусственн интеллект", "chatgpt", "openai", "gemini", "claude",
    "apple", "google", "microsoft", "meta", "telegram", "youtube", "tiktok", "instagram",
    "смартфон", "iphone", "android", "робот", "технолог", "интернет", "браузер",
    "steam", "playstation", "xbox", "nintendo", "игр", "roblox", "minecraft",
    "космос", "nasa", "spacex", "учен", "исследован", "открыт", "ракета",
    "фильм", "кино", "сериал", "актер", "актрис", "блогер", "стример",
    "запуст", "представ", "впервые", "рекорд", "запрет", "заблок",
)


def _session() -> requests.Session:
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        status=2,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36 NewsLightGG/2.1",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
    })
    return s


def _published(entry) -> datetime:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed:
        try:
            return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _entry_source(default_name: str, entry) -> str:
    source_obj = getattr(entry, "source", None)
    value = news.clean_html(getattr(source_obj, "title", "")) if source_obj else ""
    return (value or default_name)[:80]


def _relevant(title: str, summary: str) -> bool:
    normalized = news.normalize(title + " " + summary)
    category = news.classify(normalized)
    if category != "world":
        return True
    return any(marker in normalized for marker in INTEREST_MARKERS)


def _story_from_entry(default_source: str, entry) -> news.Story | None:
    title = news.clean_title(getattr(entry, "title", ""))
    link = getattr(entry, "link", "")
    if not title or not link:
        return None
    source = _entry_source(default_source, entry)
    raw_summary = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
    summary = news.clean_summary(raw_summary, title, source)
    if not _relevant(title, summary):
        return None
    return news.Story(
        title=title[:220],
        link=link,
        source=source,
        summary=summary,
        published=_published(entry),
        category=news.classify(title + " " + summary),
    )


def fetch_direct_feeds() -> list[news.Story]:
    s = _session()
    stories: list[news.Story] = []
    successes = 0
    for source_name, url in DIRECT_FEEDS:
        try:
            response = s.get(url, timeout=14)
            if response.status_code >= 400:
                print(f"RSS {source_name} HTTP {response.status_code}")
                continue
            parsed = feedparser.parse(response.content)
            if getattr(parsed, "bozo", False) and not parsed.entries:
                print(f"RSS {source_name} parse error")
                continue
            successes += 1
            accepted = 0
            for entry in parsed.entries[:45]:
                story = _story_from_entry(source_name, entry)
                if story is None:
                    continue
                stories.append(story)
                accepted += 1
                if accepted >= 16:
                    break
            print(f"RSS {source_name}: {accepted} relevant")
        except Exception as exc:
            print(f"RSS {source_name} failed: {type(exc).__name__}")
    print(f"Direct feeds online: {successes}/{len(DIRECT_FEEDS)}; stories={len(stories)}")
    return stories


def fetch_google_top() -> list[news.Story]:
    # General top feed is less likely to be rate-limited than seven search queries.
    url = "https://news.google.com/rss?hl=ru&gl=RU&ceid=RU:ru"
    try:
        response = _session().get(url, timeout=12)
        if response.status_code >= 400:
            print(f"Google top RSS HTTP {response.status_code}")
            return []
        parsed = feedparser.parse(response.content)
        stories = []
        for entry in parsed.entries[:60]:
            story = _story_from_entry("Google News", entry)
            if story is not None:
                stories.append(story)
        print(f"Google top RSS: {len(stories)} relevant")
        return stories
    except Exception as exc:
        print(f"Google top RSS failed: {type(exc).__name__}")
        return []


def fetch_stories_resilient() -> list[news.Story]:
    stories = fetch_direct_feeds()
    if len(stories) < 12:
        stories.extend(fetch_google_top())

    # Deduplicate exact normalized titles before downstream confirmation scoring.
    unique: dict[str, news.Story] = {}
    for story in stories:
        key = news.normalize(story.title)
        if not key:
            continue
        previous = unique.get(key)
        if previous is None or story.published > previous.published:
            unique[key] = story
    result = list(unique.values())
    print(f"Resilient source total: {len(result)} unique relevant stories")
    return result
