from __future__ import annotations

import hashlib
import html
import os
import re
from datetime import datetime, timezone

import requests

import autopost_news as app
import main as news
from news_sources import fetch_stories_resilient

CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@newsLightGG")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Multi-source collector; this mode deliberately publishes TEXT ONLY.
news.fetch_stories = fetch_stories_resilient

# Extra editorial ranking inspired by fast, conversational entertainment/news feeds:
# consumer tech, social platforms, games, culture, unusual/viral stories rank higher.
YEP_LIKE_BOOSTS = {
    "iphone": 2.1,
    "apple": 1.4,
    "android": 1.2,
    "смартфон": 1.5,
    "telegram": 1.8,
    "tiktok": 1.8,
    "youtube": 1.5,
    "instagram": 1.3,
    "openai": 1.6,
    "chatgpt": 1.8,
    "нейросет": 1.6,
    "ии ": 1.1,
    "roblox": 2.0,
    "minecraft": 1.7,
    "steam": 1.6,
    "playstation": 1.5,
    "xbox": 1.4,
    "nintendo": 1.5,
    "игр": 1.0,
    "фильм": 1.2,
    "сериал": 1.1,
    "актер": 1.0,
    "актрис": 1.0,
    "блогер": 1.1,
    "стример": 1.1,
    "вирус": 1.5,
    "рекорд": 1.5,
    "необыч": 1.4,
    "впервые": 1.3,
    "подорож": 1.3,
    "дешев": 1.1,
    "запрет": 1.4,
    "заблок": 1.4,
    "штраф": 0.8,
    "космос": 1.2,
    "робот": 1.3,
}

DRY_NEWS_PENALTIES = {
    "заседание": -1.5,
    "совет директоров": -1.6,
    "облигац": -1.5,
    "котировк": -1.4,
    "индекс мосбиржи": -1.7,
    "дивиденд": -1.4,
    "валютн": -1.1,
    "протокол заседания": -1.8,
    "заместитель министра": -0.9,
}

CLOSERS_BY_CATEGORY = {
    "ai": [
        "Будущее опять решило не ждать. 🤖",
        "Нейросети снова ускорили календарь. 🙂",
        "Кажется, слово «фантастика» пора обновлять. 🤖",
    ],
    "tech": [
        "Техника снова нашла повод для обновления. 📱",
        "Прогресс идёт, кошелёк наблюдает. 🙂",
        "Ещё вчера это было бы просто концептом. 📱",
    ],
    "games": [
        "Геймеры это точно заметят. 🎮",
        "У игр снова появился новый способ съесть время. 🙂",
        "Список причин открыть игру стал длиннее. 🎮",
    ],
    "internet": [
        "Интернет снова ускорил события. 🫠",
        "Скучно в сети опять не будет. 🙂",
        "Ещё один обычный день в интернете. 🫠",
    ],
    "science": [
        "Наука снова забрала идею у фантастов. 🔬",
        "Звучит странно, но уже вполне реально. 🙂",
        "Учёные опять усложнили сценаристам работу. 🔬",
    ],
    "culture": [
        "Интернет это точно не пропустит. 🎬",
        "Обсуждения можно считать открытыми. 🙂",
        "Попкорн пока не убираем. 🍿",
    ],
    "world": [
        "Вот такой поворот. 🙂",
        "События опять решили не быть скучными. 🫠",
        "На сегодня уровень неожиданности повышен. 🙂",
    ],
}


def editorial_score(story: news.Story) -> float:
    text = news.normalize(story.title + " " + story.summary)
    score = float(story.score)
    for marker, bonus in YEP_LIKE_BOOSTS.items():
        if marker in text:
            score += bonus
    for marker, penalty in DRY_NEWS_PENALTIES.items():
        if marker in text:
            score += penalty
    # Slight preference for the most conversational categories.
    if story.category in {"games", "internet", "culture", "tech", "ai"}:
        score += 0.8
    return score


def _sentences(text: str) -> list[str]:
    text = news.clean_html(text)
    chunks = re.split(r"(?<=[.!?])\s+", text)
    result = []
    for chunk in chunks:
        chunk = re.sub(r"\s+", " ", chunk).strip(" —-\n\t")
        if len(chunk) < 25:
            continue
        result.append(chunk)
    return result


def _trim_sentence(text: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + "…"


def conversational_facts(story: news.Story) -> list[str]:
    title_norm = news.normalize(story.title)
    facts: list[str] = []

    for sentence in _sentences(story.summary):
        if news.normalize(sentence) == title_norm:
            continue
        sentence = _trim_sentence(sentence)
        if sentence not in facts:
            facts.append(sentence)
        if len(facts) >= 3:
            break

    # If the RSS summary is short, enrich_story may have fetched a better description.
    if not facts and story.summary.strip():
        facts = [_trim_sentence(story.summary, 300)]

    return facts[:3]


def smart_closer(story: news.Story) -> str:
    text = news.normalize(story.title + " " + story.summary)

    # Context-sensitive endings first. These are commentary, never presented as facts.
    if any(x in text for x in ["подорож", "цена", "стоимост", "дороже"]):
        options = ["Кошелёк уже сделал вид, что не видел эту новость. 🙂", "Цены снова выбрали сложность «хард». 😶"]
    elif any(x in text for x in ["дешев", "снизят", "скидк"]):
        options = ["Редкий случай, когда кошелёк может выдохнуть. 🙂", "Вот такие новости уже приятнее читать. 😌"]
    elif any(x in text for x in ["запрет", "заблок", "огранич"]):
        options = ["Интернет опять стал чуть сложнее. 🫠", "Свободного места в списке ограничений всё меньше. 🙂"]
    elif any(x in text for x in ["рекорд", "миллион просмот", "вирусн"]):
        options = ["Интернет снова выбрал себе героя дня. 🙂", "Алгоритмы сегодня явно не скучали. 📈"]
    elif any(x in text for x in ["робот", "нейросет", "искусственн интеллект", "openai", "chatgpt"]):
        options = CLOSERS_BY_CATEGORY["ai"]
    else:
        options = CLOSERS_BY_CATEGORY.get(story.category, CLOSERS_BY_CATEGORY["world"])

    # Stable choice for a given headline, so reruns don't randomly change tone.
    digest = hashlib.sha1(news.normalize(story.title).encode("utf-8")).digest()
    return options[digest[0] % len(options)]


def format_text_post(story: news.Story) -> str:
    # YEP-like structure: strong factual first line, compact context, human closer.
    title = _trim_sentence(story.title.strip().rstrip(".!?"), 190)
    facts = conversational_facts(story)
    if not facts:
        raise RuntimeError("Story has no usable factual summary")

    blocks = [html.escape(title) + "."]

    # Keep paragraphs compact rather than one long press-release-style wall.
    if facts:
        blocks.append(html.escape(facts[0]))
    if len(facts) >= 2:
        remaining = " ".join(facts[1:])
        blocks.append(html.escape(_trim_sentence(remaining, 360)))

    blocks.append(html.escape(smart_closer(story)))
    result = "\n\n".join(blocks)

    # Public posts intentionally contain no source links/channel branding.
    if re.search(r"https?://|www\.|t\.me/", result, re.I):
        raise RuntimeError("Public post unexpectedly contains a link")
    if len(result.encode("utf-16-le")) // 2 > 1500:
        result = result[:1350].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return result


def telegram_send_text(text: str) -> dict:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHANNEL,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(f"Telegram HTTP {response.status_code}")
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram rejected request")
    return data["result"]


def main() -> None:
    state = app.load_state()
    seen = set(state["published"])

    stories = news.fetch_stories()
    news.add_confirmations(stories)
    for story in stories:
        story.score = news.score_story(story)

    candidates = [
        s for s in stories
        if s.score >= news.MIN_SCORE and app.story_id(s) not in seen
    ]
    candidates.sort(key=lambda s: (editorial_score(s), s.published), reverse=True)

    for story in candidates[:20]:
        news.enrich_story(story)
        if not conversational_facts(story):
            continue

        message = telegram_send_text(format_text_post(story))
        if not message.get("message_id"):
            raise RuntimeError("Telegram returned no message_id")

        key = app.story_id(story)
        state["published"][key] = {
            "title": story.title,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "message_id": message["message_id"],
            "source": story.source,
            "source_url": story.link,
            "mode": "text-only-yep-like",
            "editorial_score": round(editorial_score(story), 2),
        }
        state["published"] = dict(list(state["published"].items())[-500:])
        app.save_state(state)
        print(f"PUBLISHED TEXT https://t.me/newsLightGG/{message['message_id']}")
        return

    print("No suitable fresh story; skipped this cycle")


if __name__ == "__main__":
    main()
