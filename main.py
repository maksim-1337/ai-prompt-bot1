from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@newsLightGG")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
MAX_AGE_HOURS = int(os.getenv("MAX_POST_AGE_HOURS", "10"))
MIN_SCORE = float(os.getenv("MIN_NEWS_SCORE", "4.5"))
STATE_PATH = Path("data/seen.json")
OUT_DIR = Path("output")
OUT_DIR.mkdir(exist_ok=True)

QUERIES = [
    "технологии искусственный интеллект нейросети сегодня",
    "Telegram TikTok YouTube Instagram интернет сегодня",
    "Apple Google Microsoft Meta OpenAI сегодня",
    "игры Steam PlayStation Xbox Nintendo сегодня",
    "наука космос необычное открытие сегодня",
    "кино знаменитости блогеры интернет сегодня",
    "необычная история вирусное видео мир сегодня",
]

VIRAL_WORDS = {
    "впервые": 2.0,
    "запуст": 1.4,
    "представ": 1.3,
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
    "видео": 0.6,
    "удив": 0.7,
}

STOP_HINTS = [
    "гороскоп",
    "ставки на спорт",
    "курс валют",
    "погода на",
    "реклама",
    "промокод",
]

CATEGORY_LABELS = {
    "ai": "ИИ",
    "tech": "ТЕХНОЛОГИИ",
    "games": "ИГРЫ",
    "internet": "ИНТЕРНЕТ",
    "science": "НАУКА",
    "culture": "КУЛЬТУРА",
    "world": "СЕЙЧАС",
}


@dataclass
class Story:
    title: str
    link: str
    source: str
    summary: str
    published: datetime
    score: float = 0.0
    confirmations: int = 1
    category: str = "world"

    @property
    def key(self) -> str:
        # Deduplicate by headline instead of publisher so the same story from
        # several outlets is not posted more than once.
        return hashlib.sha1(normalize(self.title).encode("utf-8")).hexdigest()


def clean_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def normalize(value: str) -> str:
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9 ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_title(value: str) -> str:
    value = clean_html(value)
    parts = re.split(r"\s+[—-]\s+", value)
    if len(parts) > 1 and len(parts[-1]) < 55:
        value = " — ".join(parts[:-1])
    return value.strip()


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


def classify(text: str) -> str:
    t = normalize(text)
    if any(x in t for x in ["openai", "нейросет", "искусственн интеллект", "chatgpt", "gemini", "claude"]):
        return "ai"
    if any(x in t for x in ["steam", "playstation", "xbox", "nintendo", "игр", "геймер"]):
        return "games"
    if any(x in t for x in ["telegram", "tiktok", "youtube", "instagram", "соцсет", "блогер", "интернет"]):
        return "internet"
    if any(x in t for x in ["космос", "nasa", "учен", "исследован", "открыт"]):
        return "science"
    if any(x in t for x in ["фильм", "кино", "актер", "актрис", "пев", "рэпер", "сериал"]):
        return "culture"
    if any(x in t for x in ["apple", "google", "microsoft", "смартфон", "iphone", "android", "технолог"]):
        return "tech"
    return "world"


def clean_summary(raw: str, title: str, source: str) -> str:
    text = clean_html(raw)
    text = re.sub(r"\bЧитать далее\b.*$", "", text, flags=re.I)
    text = text.replace(source, " ")
    text = re.sub(r"\s+", " ", text).strip(" —-|")
    if len(text) < 45:
        return ""
    if normalize(text) == normalize(title):
        return ""
    return text[:520]


def fetch_meta_description(url: str) -> str:
    try:
        response = requests.get(
            url,
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0 NewsLightBot/1.0"},
            allow_redirects=True,
        )
        if not response.ok or "news.google.com" in response.url:
            return ""
        soup = BeautifulSoup(response.text[:700000], "html.parser")
        for attrs in [
            {"property": "og:description"},
            {"name": "description"},
            {"name": "twitter:description"},
        ]:
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                return clean_html(tag["content"])[:520]
    except Exception:
        return ""
    return ""


def fetch_stories() -> list[Story]:
    stories: list[Story] = []
    headers = {"User-Agent": "Mozilla/5.0 NewsLightBot/1.0"}
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
            summary = clean_summary(getattr(entry, "summary", ""), title, source)
            stories.append(
                Story(
                    title=title[:220],
                    link=link,
                    source=source[:80] or "Источник",
                    summary=summary,
                    published=parse_published(entry),
                    category=classify(title + " " + summary),
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
            if SequenceMatcher(None, title, other_title).ratio() >= 0.62:
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
    STATE_PATH.write_text(json.dumps(seen[-800:], ensure_ascii=False, indent=2) + "\n", "utf-8")


def choose_story(stories: list[Story], seen: set[str]) -> Story | None:
    add_confirmations(stories)
    unique: dict[str, Story] = {}
    for story in stories:
        story.score = score_story(story)
        key = normalize(story.title)
        previous = unique.get(key)
        if previous is None or story.score > previous.score:
            unique[key] = story

    candidates = [s for s in unique.values() if s.key not in seen and s.score >= MIN_SCORE]
    candidates.sort(key=lambda s: (s.score, s.published), reverse=True)
    for story in candidates[:8]:
        print(f"candidate score={story.score:.1f} confirmations={story.confirmations}: {story.title}")
    return candidates[0] if candidates else None


def enrich_story(story: Story) -> None:
    if len(story.summary) >= 100:
        return
    meta = fetch_meta_description(story.link)
    if meta and normalize(story.title) not in normalize(meta):
        story.summary = meta


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", clean_html(text))
    return [part.strip() for part in parts if len(part.strip()) >= 25]


def punchline(story: Story) -> str:
    text = normalize(story.title + " " + story.summary)
    pools = {
        "ai": [
            "Будущее опять пришло без предупреждения 🤖",
            "Нейросети ускоряют сюжет 🫠",
            "Ещё один обычный день в 2026-м 🤖",
        ],
        "tech": [
            "Кошелёк уже напрягся 💸",
            "Технологии не дают расслабиться 📱",
            "Обновление, которого никто не просил 😶",
        ],
        "games": [
            "Геймеры, держимся 🎮",
            "Планы на вечер сами себя нашли 🎮",
            "Игровая индустрия снова в деле 👀",
        ],
        "internet": [
            "Интернет снова победил 🫥",
            "Лента на сегодня готова 📲",
            "Соцсети опять придумали новый сюжет 😵‍💫",
        ],
        "science": [
            "Наука снова делает красиво 🔬",
            "Учёные продолжают удивлять 🧪",
            "Вселенная подкинула ещё один факт 🌌",
        ],
        "culture": [
            "Интернет это точно обсудит 🎬",
            "Шоу продолжается 🍿",
            "Культурная повестка не подвела 🎭",
        ],
        "world": [
            "Сюжет дня найден 👀",
            "И это всё реально происходит 😶",
            "День только начался, а уже интересно 🫠",
        ],
    }

    if any(x in text for x in ["подорож", "цена", "стоить", "доллар", "рубл", "тысяч"]):
        pool = [
            "Кошелёк просит не читать дальше 💸",
            "Цены снова решили отличиться 💸",
            "Экономим морально 🫠",
        ]
    elif any(x in text for x in ["запрет", "заблок", "штраф", "суд"]):
        pool = [
            "Вот это уже серьёзно 👀",
            "Интернет запомнит 🫥",
            "Продолжаем следить 👀",
        ]
    else:
        pool = pools.get(story.category, pools["world"])

    index = int(hashlib.sha1(story.title.encode("utf-8")).hexdigest(), 16) % len(pool)
    return pool[index]


def build_post(story: Story) -> str:
    # YEP-like rhythm without copying any wording: headline, two factual lines,
    # then a short original reaction. No links and no source block in the post.
    title = story.title.rstrip(".!?") + "."
    facts = sentences(story.summary)
    body_parts: list[str] = []
    for fact in facts:
        if normalize(story.title) in normalize(fact):
            continue
        body_parts.append(fact)
        if len(body_parts) == 2:
            break

    if not body_parts and story.confirmations >= 2:
        body_parts.append("Эту же историю подхватили сразу несколько независимых источников.")
    elif not body_parts:
        body_parts.append("Подробности пока появляются — новость только начала расходиться по медиа.")

    body = " ".join(body_parts)
    return f"{title}\n\n{body}\n\n{punchline(story)}"[:980]


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


def draw_gradient(image: Image.Image, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> None:
    draw = ImageDraw.Draw(image)
    width, height = image.size
    for y in range(height):
        amount = y / max(1, height - 1)
        color = tuple(int(top[i] * (1 - amount) + bottom[i] * amount) for i in range(3))
        draw.line((0, y, width, y), fill=color)


def create_slide(path: Path, story: Story, mode: str, text: str, index: int) -> None:
    width, height = 1080, 1920
    palettes = [
        ((12, 12, 16), (28, 17, 50)),
        ((8, 13, 22), (9, 44, 58)),
        ((15, 11, 20), (55, 21, 35)),
        ((10, 12, 14), (34, 34, 22)),
    ]
    top, bottom = palettes[(index - 1) % len(palettes)]
    image = Image.new("RGB", (width, height), top)
    draw_gradient(image, top, bottom)
    draw = ImageDraw.Draw(image)

    # Clean editorial motion-card design: large type, lots of whitespace,
    # restrained branding. It is deliberately original rather than a copy of
    # another channel's graphics.
    draw.rounded_rectangle((54, 62, 1026, 1858), radius=48, outline=(255, 255, 255), width=2)
    draw.rounded_rectangle((92, 105, 352, 176), radius=28, fill=(245, 245, 245))
    label = CATEGORY_LABELS.get(story.category, "СЕЙЧАС")
    draw.text((122, 120), label, font=font(30, True), fill=(12, 12, 16))
    draw.text((760, 122), "NEWS LIGHT", font=font(28, True), fill=(238, 238, 242))

    if mode == "headline":
        draw.text((92, 260), "ГЛАВНОЕ ЗА МИНУТУ", font=font(34, True), fill=(185, 190, 205))
        title_font = font(72, True)
        y = 390
        for line in wrap_text(draw, text, title_font, 880)[:9]:
            draw.text((92, y), line, font=title_font, fill=(255, 255, 255))
            y += 92
        draw.text((92, 1680), "СМОТРИ СО ЗВУКОМ", font=font(32, True), fill=(210, 210, 218))
    elif mode == "fact":
        draw.text((92, 275), f"0{index}", font=font(54, True), fill=(185, 190, 205))
        fact_font = font(58, True)
        y = 460
        for line in wrap_text(draw, text, fact_font, 860)[:10]:
            draw.text((92, y), line, font=fact_font, fill=(255, 255, 255))
            y += 78
    else:
        draw.text((92, 310), "ИТОГ", font=font(36, True), fill=(185, 190, 205))
        ending_font = font(66, True)
        y = 520
        for line in wrap_text(draw, text, ending_font, 860)[:8]:
            draw.text((92, y), line, font=ending_font, fill=(255, 255, 255))
            y += 88
        draw.text((92, 1680), "@newsLightGG", font=font(36, True), fill=(240, 240, 245))

    image.save(path, quality=95)


def run(cmd: list[str]) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True)


def make_voice(text: str) -> Path:
    mp3 = OUT_DIR / "voice.mp3"
    try:
        run(
            [
                "edge-tts",
                "--voice",
                "ru-RU-DmitryNeural",
                "--rate=+7%",
                "--text",
                text,
                "--write-media",
                str(mp3),
            ]
        )
        if mp3.exists() and mp3.stat().st_size > 1000:
            return mp3
    except Exception as exc:
        print("edge-tts failed:", exc)

    # Fully local fallback if the free neural voice endpoint is unavailable.
    wav = OUT_DIR / "voice.wav"
    try:
        run(["espeak-ng", "-v", "ru", "-s", "162", "-w", str(wav), text])
        return wav
    except Exception:
        run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=mono",
                "-t",
                "18",
                str(wav),
            ]
        )
        return wav


def make_video(story: Story) -> Path:
    facts = sentences(story.summary)
    fact1 = facts[0] if facts else "Подробности появляются прямо сейчас."
    fact2 = facts[1] if len(facts) > 1 else (
        "Новость подтверждают несколько источников."
        if story.confirmations >= 2
        else "Следим за развитием истории."
    )
    ending = punchline(story)

    slides = [OUT_DIR / f"slide_{i}.jpg" for i in range(1, 5)]
    create_slide(slides[0], story, "headline", story.title, 1)
    create_slide(slides[1], story, "fact", fact1, 2)
    create_slide(slides[2], story, "fact", fact2, 3)
    create_slide(slides[3], story, "ending", ending, 4)

    script = f"{story.title}. {fact1} {fact2} {ending}"
    voice = make_voice(script)
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(voice),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    duration = max(16.0, float(probe.stdout.strip() or "18"))
    per = duration / 4.0
    out = OUT_DIR / "news_short.mp4"

    cmd = ["ffmpeg", "-y"]
    for slide in slides:
        cmd += ["-loop", "1", "-framerate", "30", "-t", f"{per:.2f}", "-i", str(slide)]
    cmd += ["-i", str(voice)]

    filters: list[str] = []
    for index in range(4):
        fade_out = max(0.1, per - 0.35)
        filters.append(
            f"[{index}:v]scale=1080:1920,"
            f"zoompan=z='min(zoom+0.00045,1.045)':x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps=30,"
            f"fade=t=in:st=0:d=0.35,fade=t=out:st={fade_out:.2f}:d=0.35,"
            f"trim=duration={per:.2f},setpts=PTS-STARTPTS[v{index}]"
        )
    filters.append("[v0][v1][v2][v3]concat=n=4:v=1:a=0[v]")

    cmd += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[v]",
        "-map",
        "4:a",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        "-movflags",
        "+faststart",
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
            data={
                "chat_id": CHANNEL,
                "caption": caption,
                "supports_streaming": "true",
            },
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
    stories = fetch_stories()
    print(f"Fetched {len(stories)} candidate stories")
    story = choose_story(stories, set(seen_list))
    if story is None:
        print("No fresh story passed the quality threshold.")
        return

    enrich_story(story)
    print(
        f"Selected: {story.title} | score={story.score:.1f} | "
        f"confirmations={story.confirmations}"
    )
    video = make_video(story)
    telegram_send_video(video, build_post(story))
    seen_list.append(story.key)
    save_seen(seen_list)
    print("Published successfully.")


if __name__ == "__main__":
    main()
