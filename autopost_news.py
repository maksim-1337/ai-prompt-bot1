from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

import main as news

CHANNEL = os.getenv("TELEGRAM_CHANNEL", "@newsLightGG")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
STATE = Path("data/autopost-state.json")
OUT_DIR = Path("output/autopost")
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

CATEGORY_LABELS = {
    "ai": "AI / НЕЙРОСЕТИ",
    "tech": "ТЕХНОЛОГИИ",
    "games": "ИГРЫ",
    "internet": "ИНТЕРНЕТ",
    "science": "НАУКА",
    "culture": "КУЛЬТУРА",
    "world": "ГЛАВНОЕ",
}
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
FONT_CANDIDATES = {
    "bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ],
    "regular": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ],
}


def font(size: int, bold: bool = False):
    key = "bold" if bold else "regular"
    for path in FONT_CANDIDATES[key]:
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


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


def caption_paragraphs(story: news.Story) -> list[str]:
    raw = [clean_fact(s) for s in news.sentences(story.summary)]
    facts = [s for s in raw if s and news.normalize(story.title) not in news.normalize(s)]
    if not facts and story.summary.strip():
        summary = clean_fact(story.summary)
        if summary:
            facts = [summary]
    if not facts:
        return []

    paragraphs: list[str] = []
    current = ""
    for fact in facts[:5]:
        candidate = (current + " " + fact).strip()
        if current and len(candidate) > 330:
            paragraphs.append(current)
            current = fact
        else:
            current = candidate
        if len(paragraphs) == 2:
            break
    if current and len(paragraphs) < 3:
        paragraphs.append(current)
    return [p for p in paragraphs if len(p) >= 40][:3]


def format_caption(story: news.Story) -> str:
    title = story.title.strip().rstrip(".!?")
    emoji = CATEGORY_EMOJI.get(story.category, "⚡")
    if not re.match(r"^[^\w\s]", title):
        title = f"{emoji} {title}"

    paragraphs = caption_paragraphs(story)
    if not paragraphs:
        raise RuntimeError("Story has no usable factual summary")

    result = f"<b>{html.escape(title)}</b>\n\n" + "\n\n".join(html.escape(p) for p in paragraphs)
    if re.search(r"https?://|www\.|t\.me/", result, re.I):
        raise RuntimeError("Public caption unexpectedly contains a link")

    while len(result.encode("utf-16-le")) // 2 > 1024 and len(paragraphs) > 1:
        paragraphs.pop()
        result = f"<b>{html.escape(title)}</b>\n\n" + "\n\n".join(html.escape(p) for p in paragraphs)
    if len(result.encode("utf-16-le")) // 2 > 1024:
        body = paragraphs[0][:820].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
        result = f"<b>{html.escape(title)}</b>\n\n{html.escape(body)}"
    return result


def image_query(story: news.Story) -> str:
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", story.title)
    useful = [w for w in words if len(w) >= 4 and w.lower() not in STOP_WORDS]
    return " ".join(useful[:6]) or story.title[:80]


def commons_image(story: news.Story) -> dict | None:
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"filetype:bitmap {image_query(story)}",
        "gsrnamespace": 6,
        "gsrlimit": 14,
        "prop": "imageinfo",
        "iiprop": "url|size|extmetadata",
        "iiurlwidth": 1600,
        "format": "json",
        "origin": "*",
    }
    try:
        response = requests.get(
            COMMONS_API,
            params=params,
            timeout=25,
            headers={"User-Agent": "NewsLightGG/2.0 (Telegram news cover generator)"},
        )
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
        width, height = int(info.get("width") or 0), int(info.get("height") or 0)
        if width < 640 or height < 360:
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
            "credit": re.sub(r"\s+", " ", html.unescape(artist))[:100],
        }
    return None


def download_image(url: str) -> Image.Image:
    response = requests.get(url, timeout=30, headers={"User-Agent": "NewsLightGG/2.0"})
    response.raise_for_status()
    if len(response.content) > 12 * 1024 * 1024:
        raise RuntimeError("Source image too large")
    image = Image.open(io.BytesIO(response.content))
    image.load()
    return image.convert("RGB")


def wrap_lines(draw: ImageDraw.ImageDraw, text: str, fnt, max_width: int, max_lines: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        bbox = draw.textbbox((0, 0), candidate, font=fnt)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) >= max_lines - 1:
            break
    if current and len(lines) < max_lines:
        rest_index = sum(len(x.split()) for x in lines)
        remaining = words[rest_index:]
        last = " ".join(remaining)
        while last:
            bbox = draw.textbbox((0, 0), last, font=fnt)
            if bbox[2] - bbox[0] <= max_width:
                break
            last_words = last.split()
            if len(last_words) <= 1:
                break
            last = " ".join(last_words[:-1])
        if rest_index + len(last.split()) < len(words):
            last = last.rstrip(" .,!?:;-") + "…"
        lines.append(last or current)
    return lines[:max_lines]


def cover_headline(story: news.Story) -> str:
    value = news.clean_html(story.title).strip().rstrip(".!?")
    if len(value) <= 92:
        return value
    return value[:92].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"


def badge_texts(story: news.Story) -> list[str]:
    facts = caption_paragraphs(story)
    badges = []
    for paragraph in facts[:2]:
        words = paragraph.replace("—", " ").split()
        short = " ".join(words[:8]).rstrip(".,;:-")
        if len(words) > 8:
            short += "…"
        if short:
            badges.append(short)
    if len(badges) == 1:
        badges.append(CATEGORY_LABELS.get(story.category, "СВЕЖАЯ НОВОСТЬ"))
    return badges[:2]


def vertical_gradient(size: tuple[int, int], top_alpha: int, bottom_alpha: int) -> Image.Image:
    width, height = size
    layer = Image.new("RGBA", size, (4, 12, 28, 0))
    draw = ImageDraw.Draw(layer)
    for y in range(height):
        t = y / max(1, height - 1)
        alpha = int(top_alpha * (1 - t) + bottom_alpha * t)
        draw.line((0, y, width, y), fill=(4, 12, 28, alpha))
    return layer


def rounded_crop(image: Image.Image, size: tuple[int, int], radius: int) -> Image.Image:
    fitted = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.45))
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.paste(fitted.convert("RGBA"), (0, 0), mask)
    return out


def draw_cover(story: news.Story, source: Image.Image, image_meta: dict, out: Path) -> Path:
    width, height = 1080, 1350
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    bg = ImageOps.fit(source, (width, height), method=Image.Resampling.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(20)).convert("RGBA")
    bg = Image.blend(bg, Image.new("RGBA", (width, height), (5, 14, 34, 255)), 0.42)
    canvas = Image.alpha_composite(bg, vertical_gradient((width, height), 205, 230))

    hero = rounded_crop(source, (920, 690), 42)
    hero_shadow = Image.new("RGBA", (960, 730), (0, 0, 0, 0))
    ImageDraw.Draw(hero_shadow).rounded_rectangle((18, 22, 942, 712), radius=48, fill=(0, 0, 0, 105))
    hero_shadow = hero_shadow.filter(ImageFilter.GaussianBlur(13))
    canvas.alpha_composite(hero_shadow, (60, 350))
    canvas.alpha_composite(hero, (80, 350))

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((70, 55, 330, 118), radius=28, fill=(255, 255, 255, 238))
    draw.text((99, 70), "NEWS LIGHT", font=font(30, True), fill=(9, 20, 44))

    label = CATEGORY_LABELS.get(story.category, "ГЛАВНОЕ")
    label_font = font(24, True)
    label_box = draw.textbbox((0, 0), label, font=label_font)
    label_w = min(500, label_box[2] - label_box[0] + 50)
    draw.rounded_rectangle((width - 70 - label_w, 55, width - 70, 118), radius=28, fill=(42, 113, 255, 232))
    draw.text((width - 70 - label_w + 25, 73), label, font=label_font, fill="white")

    headline = cover_headline(story)
    size = 62 if len(headline) < 55 else 54
    title_font = font(size, True)
    lines = wrap_lines(draw, headline, title_font, 920, 3)
    panel_h = 60 + len(lines) * (size + 10)
    draw.rounded_rectangle((60, 145, 1020, 145 + panel_h), radius=34, fill=(6, 17, 40, 205), outline=(255, 255, 255, 55), width=2)
    y = 176
    for line in lines:
        draw.text((92, y), line, font=title_font, fill=(255, 255, 255))
        y += size + 10

    badges = badge_texts(story)
    badge_y = 1085
    badge_colors = [(40, 122, 255, 235), (169, 81, 255, 235)]
    for index, text in enumerate(badges):
        x0 = 70 + index * 475
        x1 = x0 + 445
        draw.rounded_rectangle((x0, badge_y, x1, 1230), radius=32, fill=badge_colors[index])
        badge_font = font(27, True)
        badge_lines = wrap_lines(draw, text, badge_font, 385, 3)
        ty = badge_y + 24
        for line in badge_lines:
            draw.text((x0 + 30, ty), line, font=badge_font, fill="white")
            ty += 35

    draw.text((70, 1290), "@newsLightGG", font=font(24, True), fill=(236, 242, 255))
    credit = image_meta.get("credit") or "Wikimedia Commons"
    credit_text = f"Иллюстрация: {credit} / Wikimedia Commons"
    footer_font = font(22, False)
    bbox = draw.textbbox((0, 0), credit_text, font=footer_font)
    if bbox[2] - bbox[0] > 660:
        credit_text = "Иллюстрация: Wikimedia Commons"
        bbox = draw.textbbox((0, 0), credit_text, font=footer_font)
    draw.text((1010 - (bbox[2] - bbox[0]), 1292), credit_text, font=footer_font, fill=(190, 200, 220))

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out, "JPEG", quality=92, optimize=True, progressive=True)
    return out


def telegram_send_photo(photo: Path, caption: str) -> dict:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    with photo.open("rb") as handle:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={"chat_id": CHANNEL, "caption": caption, "parse_mode": "HTML"},
            files={"photo": (photo.name, handle, "image/jpeg")},
            timeout=90,
        )
    if not response.ok:
        raise RuntimeError(f"Telegram HTTP {response.status_code}")
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram rejected request")
    return data["result"]


def build_cover(story: news.Story, image_meta: dict) -> Path:
    source = download_image(image_meta["url"])
    output = OUT_DIR / f"{story_id(story)}.jpg"
    return draw_cover(story, source, image_meta, output)


def self_test() -> None:
    sample = news.Story(
        title="Roblox готовит игры без установки приложения",
        link="https://example.com",
        source="Demo",
        summary=(
            "Компания планирует запускать игры прямо в браузере без обязательной установки приложения. "
            "Отдельные проекты также смогут выходить как самостоятельные приложения для разных устройств."
        ),
        published=datetime.now(timezone.utc),
        category="games",
    )
    source = Image.new("RGB", (1600, 900), (25, 71, 145))
    src_draw = ImageDraw.Draw(source)
    for x in range(0, 1600, 120):
        src_draw.rectangle((x, 0, x + 60, 900), fill=(30 + (x // 120) % 4 * 18, 80, 165))
    output = OUT_DIR / "self-test-cover.jpg"
    draw_cover(sample, source, {"credit": "Self-test"}, output)
    probe = Image.open(output)
    if probe.size != (1080, 1350) or probe.format != "JPEG" or output.stat().st_size < 20_000:
        raise RuntimeError("Cover self-test failed")
    caption = format_caption(sample)
    if "https://" in caption or len(caption.encode("utf-16-le")) // 2 > 1024:
        raise RuntimeError("Caption self-test failed")
    print(f"SELF-TEST OK: {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return

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
        if not caption_paragraphs(story):
            continue

        image_meta = commons_image(story)
        if not image_meta:
            print("No safe illustration for:", story.title)
            continue

        try:
            cover = build_cover(story, image_meta)
        except Exception as exc:
            print("Cover failed for:", story.title, "-", exc)
            continue

        message = telegram_send_photo(cover, format_caption(story))
        if not message.get("message_id"):
            raise RuntimeError("Telegram returned no message_id")

        key = story_id(story)
        state["published"][key] = {
            "title": story.title,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "message_id": message["message_id"],
            "source": story.source,
            "source_url": story.link,
            "image_page": image_meta["page"],
            "image_license": image_meta["license"],
            "cover": str(cover),
        }
        state["published"] = dict(list(state["published"].items())[-500:])
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
