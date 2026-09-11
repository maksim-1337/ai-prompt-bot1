from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import subprocess
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests
from PIL import Image, ImageDraw, ImageFont

CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@historyXYT")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
MAX_AGE_HOURS = int(os.getenv("MAX_POST_AGE_HOURS", "12"))
MIN_SCORE = float(os.getenv("MIN_NEWS_SCORE", "4.0"))
STATE_PATH = Path("data/seen.json")
OUT_DIR = Path("output")
OUT_DIR.mkdir(exist_ok=True)

QUERIES = [
    "искусственный интеллект нейросети технологии",
    "Telegram TikTok YouTube Instagram интернет",
    "Apple Google Microsoft Meta OpenAI",
    "игры Steam PlayStation Xbox Nintendo",
    "наука космос необычное открытие",
    "знаменитости блогеры интернет",
    "громкая новость мир сегодня",
]

VIRAL_WORDS = {
    "впервые": 2.0,
    "запустил": 1.4,
    "запустила": 1.4,
    "представил": 1.3,
    "представила": 1.3,
    "запрет": 1.8,
    "заблок": 1.8,
    "рекорд": 1.8,
    "скандал": 1.8,
    "миллиард": 1.2,
    "искусственн": 1.3,
    "нейросет": 1.3,
    "telegram": 1.2,
    "youtube": 1.0,
    "tiktok": 1.0,
    "apple": 1.0,
    "google": 1.0,
    "openai": 1.2,
    "игр": 0.8,
    "космос": 1.0,
}

STOP_HINTS = [
    "гороскоп",
    "ставки на спорт",
    "курс валют",
    "погода на",
]


@dataclass
class Story:
    title: str
    link: str
    source: str
    summary: str
    published: datetime
    score: float = 0.0
    confirmations: int = 1

    @property
    def key(self) -> str:
        return hashlib.sha1((self.title.lower() + self.source.lower()).encode("utf-8")).hexdigest()


def clean_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def clean_title(value: str) -> str:
    value = clean_html(value)
    # Google News often appends the publisher after a dash.
    parts = re.split(r"\s+[—-]\s+", value)
    if len(parts) > 1 and len(parts[-1]) < 45:
        value = " — ".join(parts[:-1])
    return value.strip()


def normalize(value: str) -> str:
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9 ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_published(entry) -> datetime:
    tm = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if tm:
        return datetime.fromtimestamp(time.mktime(tm), tz=timezone.utc)
    return datetime.now(timezone.utc)


def google_news_url(query: str) -> str:
    return (
        "https://news.google.com/rss/search?q="
        + quote_plus(query + " when:1d")
        + "&hl=ru&gl=RU&ceid=RU:ru"
    )


def fetch_stories() -> list[Story]:
    stories: list[Story] = []
    headers = {"User-Agent": "Mozilla/5.0 HistoryXYTNewsBot/1.0"}
    for query in QUERIES:
        try:
            response = requests.get(google_news_url(query), headers=headers, timeout=20)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
        except Exception as exc:
            print(f"Feed error for {query!r}: {exc}")
            continue

        for entry in feed.entries[:25]:
            title = clean_title(getattr(entry, "title", ""))
            link = getattr(entry, "link", "")
            if not title or not link:
                continue
            source_obj = getattr(entry, "source", None)
            source = clean_html(getattr(source_obj, "title", "")) if source_obj else "Источник"
            summary = clean_html(getattr(entry, "summary", ""))
            # Avoid repeating the full title/source blob from Google News summaries.
            if len(summary) < 40 or normalize(title) in normalize(summary):
                summary = ""
            stories.append(
                Story(
                    title=title[:220],
                    link=link,
                    source=source[:80] or "Источник",
                    summary=summary[:450],
                    published=parse_published(entry),
                )
            )
    return stories


def add_confirmations(stories: list[Story]) -> None:
    normalized = [(story, normalize(story.title)) for story in stories]
    for story, title in normalized:
        sources = {story.source.lower()}
        for other, other_title in normalized:
            if other is story or other.source.lower() in sources:
                continue
            similarity = SequenceMatcher(None, title, other_title).ratio()
            if similarity >= 0.62:
                sources.add(other.source.lower())
        story.confirmations = len(sources)


def score_story(story: Story) -> float:
    now = datetime.now(timezone.utc)
    age_h = max(0.0, (now - story.published).total_seconds() / 3600)
    if age_h > MAX_AGE_HOURS:
        return -999

    text = normalize(story.title + " " + story.summary)
    if any(stop in text for stop in STOP_HINTS):
        return -999

    score = 0.0
    if age_h <= 1:
        score += 5.0
    elif age_h <= 3:
        score += 4.0
    elif age_h <= 6:
        score += 2.5
    else:
        score += 1.0

    for word, weight in VIRAL_WORDS.items():
        if word in text:
            score += weight

    if story.confirmations >= 3:
        score += 3.0
    elif story.confirmations == 2:
        score += 1.5

    if 45 <= len(story.title) <= 160:
        score += 0.5
    return score


def load_seen() -> list[str]:
    try:
        return json.loads(STATE_PATH.read_text("utf-8"))
    except Exception:
        return []


def save_seen(seen: list[str]) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(seen[-600:], ensure_ascii=False, indent=2) + "\n", "utf-8")


def choose_story(stories: list[Story], seen: set[str]) -> Story | None:
    add_confirmations(stories)
    unique: dict[str, Story] = {}
    for story in stories:
        story.score = score_story(story)
        title_key = normalize(story.title)
        previous = unique.get(title_key)
        if previous is None or story.score > previous.score:
            unique[title_key] = story

    candidates = [s for s in unique.values() if s.key not in seen and s.score >= MIN_SCORE]
    candidates.sort(key=lambda s: (s.score, s.published), reverse=True)
    for story in candidates[:10]:
        print(f"candidate score={story.score:.1f} confirmations={story.confirmations}: {story.title}")
    return candidates[0] if candidates else None


def sentence_summary(story: Story) -> str:
    if story.summary:
        raw = story.summary
        raw = re.sub(r"\s+[—-]\s+[^.]{1,50}$", "", raw)
        if len(raw) > 320:
            raw = raw[:317].rsplit(" ", 1)[0] + "…"
        return raw
    return (
        f"Новость появилась у {story.source}. Сейчас она набирает внимание в новостной выдаче. "
        "Следим за обновлениями и оставляем ссылку на первоисточник ниже."
    )


def build_caption(story: Story) -> str:
    summary = sentence_summary(story)
    check = "Подтверждается несколькими СМИ" if story.confirmations >= 2 else "Источник указан ниже"
    caption = (
        f"🔥 {story.title}\n\n"
        f"{summary}\n\n"
        f"🔎 {check}.\n"
        f"📰 {story.source}\n"
        f"🔗 {story.link}\n\n"
        "@historyXYT"
    )
    return caption[:1000]


def font(size: int, bold: bool = False):
    path = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    )
    return ImageFont.truetype(path, size)


def wrap_text(draw: ImageDraw.ImageDraw, text: str, fnt, width_px: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        box = draw.textbbox((0, 0), test, font=fnt)
        if box[2] - box[0] <= width_px:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def create_slide(path: Path, kicker: str, title: str, body: str, index: int) -> None:
    w, h = 1080, 1920
    image = Image.new("RGB", (w, h), (10 + index * 5, 13 + index * 4, 24 + index * 8))
    draw = ImageDraw.Draw(image)

    # Original abstract background: no copyrighted source image is required.
    for i in range(14):
        x = (i * 173 + index * 97) % 1200 - 100
        y = (i * 251 + index * 149) % 2100 - 100
        r = 130 + (i % 4) * 55
        shade = 28 + (i * 7) % 60
        draw.ellipse((x-r, y-r, x+r, y+r), fill=(shade, 35 + index * 10, 80 + (i * 6) % 70))

    draw.rounded_rectangle((70, 90, 1010, 1830), radius=42, fill=(7, 9, 17))
    draw.text((120, 155), kicker.upper(), font=font(46, True), fill=(255, 210, 70))

    title_font = font(64, True)
    y = 320
    for line in wrap_text(draw, title, title_font, 820)[:8]:
        draw.text((120, y), line, font=title_font, fill=(248, 248, 252))
        y += 82

    body_font = font(40)
    y = max(y + 85, 1060)
    for line in wrap_text(draw, body, body_font, 820)[:9]:
        draw.text((120, y), line, font=body_font, fill=(205, 210, 224))
        y += 56

    draw.text((120, 1710), "@historyXYT", font=font(44, True), fill=(255, 255, 255))
    image.save(path, quality=94)


def run(cmd: list[str]) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True)


def make_video(story: Story) -> Path:
    summary = sentence_summary(story)
    slides = [OUT_DIR / f"slide_{i}.png" for i in range(1, 4)]
    create_slide(slides[0], "СВЕЖАЯ НОВОСТЬ", story.title, "Самое важное — за несколько секунд.", 1)
    create_slide(slides[1], "ЧТО ИЗВЕСТНО", story.title, summary, 2)
    create_slide(
        slides[2],
        "ПОДРОБНОСТИ",
        story.title,
        f"Источник: {story.source}. Больше свежих новостей — в Telegram.",
        3,
    )

    script = f"{story.title}. {summary}. Источник: {story.source}. Больше свежих новостей в Телеграм канале История икс вай ти."
    wav = OUT_DIR / "voice.wav"
    try:
        run(["espeak-ng", "-v", "ru", "-s", "158", "-w", str(wav), script])
    except Exception:
        # Fallback: silent audio, so video creation still succeeds.
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "18", str(wav)])

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(wav)],
        capture_output=True,
        text=True,
        check=True,
    )
    duration = max(15.0, float(probe.stdout.strip() or "18"))
    per = duration / 3.0
    out = OUT_DIR / "news_short.mp4"

    cmd = ["ffmpeg", "-y"]
    for slide in slides:
        cmd += ["-loop", "1", "-framerate", "30", "-t", f"{per:.2f}", "-i", str(slide)]
    cmd += ["-i", str(wav)]
    cmd += [
        "-filter_complex",
        "[0:v]scale=1080:1920,format=yuv420p[v0];"
        "[1:v]scale=1080:1920,format=yuv420p[v1];"
        "[2:v]scale=1080:1920,format=yuv420p[v2];"
        "[v0][v1][v2]concat=n=3:v=1:a=0[v]",
        "-map", "[v]",
        "-map", "3:a",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "24",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        "-movflags", "+faststart",
        str(out),
    ]
    run(cmd)
    return out


def telegram_send_video(video: Path, caption: str) -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
    with video.open("rb") as handle:
        response = requests.post(
            url,
            data={"chat_id": CHANNEL, "caption": caption, "supports_streaming": "true"},
            files={"video": (video.name, handle, "video/mp4")},
            timeout=120,
        )
    if not response.ok:
        raise RuntimeError(f"Telegram error {response.status_code}: {response.text[:500]}")
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API rejected request: {payload}")


def main() -> None:
    seen_list = load_seen()
    seen = set(seen_list)
    stories = fetch_stories()
    print(f"Fetched {len(stories)} candidate stories")
    story = choose_story(stories, seen)
    if story is None:
        print("No fresh story passed the quality threshold.")
        return

    print(f"Selected: {story.title} | score={story.score:.1f} | confirmations={story.confirmations}")
    video = make_video(story)
    telegram_send_video(video, build_caption(story))
    seen_list.append(story.key)
    save_seen(seen_list)
    print("Published successfully.")


if __name__ == "__main__":
    main()
