from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

import main as news

CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@newsLightGG")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
STATE = Path("data/autopost-state.json")
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

CATEGORY_EMOJI = {
    "ai": "🤖",
    "tech": "📱",
    "games": "🎮",
    "internet": "🌐",
    "science": "🔬",
    "culture": "🎬",
    "world": "⚡",
}

STOP_WORDS = {
    "что", "как", "для", "это", "его", "ее", "её", "или", "при", "после", "через",
    "новый", "новая", "новые", "сегодня", "заявил", "заявила", "стало", "будет", "может",
    "the", "and", "with", "from", "into", "about", "news",
}

ALLOWED_LICENSE_MARKERS = (
    "public domain", "cc0", "cc by", "cc-by", "cc by-sa", "cc-by-sa",
    "creative commons attribution", "creative commons zero",
)


def load_state() -> dict:
    if not STATE.exists():
        return {"schema_version": 1, "published": {}}
    data = json.loads(STATE.read_text("utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("published"), dict):
        raise RuntimeError("Invalid autopost state")
    return data


def save_state(data: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    subprocess.run(["git", "add", str(STATE)], check=True)
    commit = subprocess.run(
        ["git", "commit", "-m", "Record NewsLightGG autopost"],
        text=True,
        capture_output=True,
    )
    if commit.returncode not in (0, 1):
        raise RuntimeError("Could not commit autopost state")
    if commit.returncode == 0:
        subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)


def story_id(story: news.Story) -> str:
    return hashlib.sha256(news.normalize(story.title).encode("utf-8")).hexdigest()[:24]


def clean_fact(text: str) -> str:
    text = news.clean_html(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip(".!?") + "." if text else ""


def format_caption(story: news.Story) -> str:
    title = story.title.strip().rstrip(".!?")
    emoji = CATEGORY_EMOJI.get(story.category, "⚡")
    if not re.match(r"^[^\w\s]", title):
        title = f"{emoji} {title}"

    facts = []
    for sentence in news.sentences(story.summary):
        sentence = clean_fact(sentence)
        if not sentence:
            continue
        if news.normalize(story.title) in news.normalize(sentence):
            continue
        facts.append(sentence)
        if len(facts) == 3:
            break

    if not facts:
        raise RuntimeError("Story has no usable factual summary")

    quote = "\n".join("— " + html.escape(item) for item in facts)
    reaction = html.escape(news.punchline(story))
    result = (
        f"<b>{html.escape(title)}</b>\n\n"
        f"<b>Что известно:</b>\n"
        f"<blockquote>{quote}</blockquote>\n\n"
        f"{reaction}"
    )

    if re.search(r"https?://|www\.|t\.me/", result, re.I):
        raise RuntimeError("Public caption unexpectedly contains a link")
    if len(result.encode("utf-16-le")) // 2 > 1024:
        # Keep the strongest first two facts if Telegram photo caption gets too long.
        quote = "\n".join("— " + html.escape(item) for item in facts[:2])
        result = (
            f"<b>{html.escape(title)}</b>\n\n"
            f"<b>Что известно:</b>\n"
            f"<blockquote>{quote}</blockquote>\n\n"
            f"{reaction}"
        )
    if len(result.encode("utf-16-le")) // 2 > 1024:
        raise RuntimeError("Caption is too long")
    return result


def image_query(story: news.Story) -> str:
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", story.title)
    useful = [w for w in words if len(w) >= 4 and w.lower() not in STOP_WORDS]
    return " ".join(useful[:6]) or story.title[:80]


def commons_image(story: news.Story) -> dict | None:
    query = image_query(story)
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}",
        "gsrnamespace": 6,
        "gsrlimit": 12,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata",
        "iiurlwidth": 1280,
        "format": "json",
        "origin": "*",
    }
    try:
        response = requests.get(COMMONS_API, params=params, timeout=25, headers={"User-Agent": "NewsLightGG/1.0"})
        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", {})
    except Exception as exc:
        print("Commons lookup failed:", exc)
        return None

    for page in pages.values():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        meta = info.get("extmetadata") or {}
        license_name = (meta.get("LicenseShortName") or {}).get("value", "")
        usage = (meta.get("UsageTerms") or {}).get("value", "")
        license_text = f"{license_name} {usage}".lower()
        if not any(marker in license_text for marker in ALLOWED_LICENSE_MARKERS):
            continue
        url = info.get("thumburl") or info.get("url")
        page_url = info.get("descriptionurl")
        if not url or not page_url or not url.startswith("https://"):
            continue
        artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", "")).strip()
        return {
            "url": url,
            "page": page_url,
            "license": license_name or usage or "Wikimedia Commons",
            "credit": artist[:120],
        }
    return None


def telegram(method: str, payload: dict) -> dict:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        json=payload,
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(f"Telegram HTTP {response.status_code}")
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram rejected request")
    return data["result"]


def send_post(story: news.Story, image: dict) -> dict:
    caption = format_caption(story)
    # Every Commons image is treated as an illustration, not as documentary footage of the event.
    caption += "\n\n<i>Иллюстрация: Wikimedia Commons"
    if image.get("credit"):
        caption += ", " + html.escape(image["credit"])
    caption += "</i>"
    if len(caption.encode("utf-16-le")) // 2 > 1024:
        caption = format_caption(story) + "\n\n<i>Иллюстрация: Wikimedia Commons</i>"

    return telegram(
        "sendPhoto",
        {
            "chat_id": CHANNEL,
            "photo": image["url"],
            "caption": caption,
            "parse_mode": "HTML",
        },
    )


def main() -> None:
    state = load_state()
    seen = set(state["published"])

    stories = news.fetch_stories()
    news.add_confirmations(stories)
    for story in stories:
        story.score = news.score_story(story)
    candidates = [s for s in stories if s.score >= news.MIN_SCORE and story_id(s) not in seen]
    candidates.sort(key=lambda s: (s.score, s.published), reverse=True)

    for story in candidates[:12]:
        news.enrich_story(story)
        if len(news.sentences(story.summary)) < 1:
            continue
        image = commons_image(story)
        if not image:
            print("No safe Commons image for:", story.title)
            continue

        message = send_post(story, image)
        if not message.get("message_id"):
            raise RuntimeError("Telegram returned no message_id")

        key = story_id(story)
        state["published"][key] = {
            "title": story.title,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "message_id": message["message_id"],
            "source": story.source,
            "source_url": story.link,
            "image_page": image["page"],
            "image_license": image["license"],
        }
        # Keep state bounded while retaining recent duplicate protection.
        items = list(state["published"].items())[-500:]
        state["published"] = dict(items)
        save_state(state)
        print(f"PUBLISHED https://t.me/newsLightGG/{message['message_id']}")
        return

    print("No suitable fresh story + safe illustration; skipped this cycle")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Stopped:", exc, file=sys.stderr)
        sys.exit(1)
