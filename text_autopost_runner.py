from __future__ import annotations

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

# Use the resilient multi-source collector. No image generation at all.
news.fetch_stories = fetch_stories_resilient


def format_text_post(story: news.Story) -> str:
    title = story.title.strip().rstrip(".!?")
    emoji = app.CATEGORY_EMOJI.get(story.category, "⚡")
    if not re.match(r"^[^\w\s]", title):
        title = f"{emoji} {title}"

    paragraphs = app.caption_paragraphs(story)
    if not paragraphs:
        raise RuntimeError("Story has no usable factual summary")

    # Telegram sendMessage allows much more than a photo caption, so keep up to
    # three useful paragraphs while still staying concise and readable.
    body = "\n\n".join(html.escape(p) for p in paragraphs[:3])
    result = f"<b>{html.escape(title)}</b>\n\n{body}"
    if re.search(r"https?://|www\.|t\.me/", result, re.I):
        raise RuntimeError("Public post unexpectedly contains a link")
    if len(result.encode("utf-16-le")) // 2 > 3900:
        result = result[:3600].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
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
    candidates.sort(key=lambda s: (s.score, s.published), reverse=True)

    for story in candidates[:16]:
        news.enrich_story(story)
        if not app.caption_paragraphs(story):
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
            "mode": "text-only",
        }
        state["published"] = dict(list(state["published"].items())[-500:])
        app.save_state(state)
        print(f"PUBLISHED TEXT https://t.me/newsLightGG/{message['message_id']}")
        return

    print("No suitable fresh story; skipped this cycle")


if __name__ == "__main__":
    main()
