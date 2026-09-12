from __future__ import annotations

import argparse
import base64
import io
import os
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

import main as news

OUT_DIR = Path("output/autopost")
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2.5-flare")
OPENAI_IMAGE_QUALITY = os.getenv("OPENAI_IMAGE_QUALITY", "low")

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


def clean_fact(text: str) -> str:
    text = news.clean_html(text)
    return " ".join(text.split()).strip().rstrip(".!?")


def facts(story: news.Story) -> list[str]:
    result: list[str] = []
    for sentence in news.sentences(story.summary):
        item = clean_fact(sentence)
        if not item:
            continue
        if news.normalize(story.title) in news.normalize(item):
            continue
        result.append(item)
        if len(result) == 2:
            break
    if not result and story.summary.strip():
        item = clean_fact(story.summary)
        if item:
            result.append(item)
    return result


def visual_prompt(story: news.Story) -> str:
    key_facts = facts(story)
    fact_text = " ".join(key_facts)[:900]
    return (
        "Create a premium vertical editorial news illustration for a modern technology/news Telegram post. "
        "The image must look like a professionally art-directed social-media news poster: cinematic lighting, "
        "high contrast, vivid but tasteful colors, strong central subject, depth, polished 3D/photoreal editorial feel. "
        "Visually represent the real topic described below without inventing specific documentary events or people that are not stated. "
        "Do NOT include any words, letters, captions, logos, watermarks, channel names, UI text, brand signatures, or fake screenshots. "
        "Leave useful darker negative space near the top and lower third so accurate Russian text can be overlaid later. "
        "Avoid generic stock-photo composition. Make it feel custom-made for this exact story.\n\n"
        f"NEWS HEADLINE: {story.title}\n"
        f"KNOWN FACTS: {fact_text}\n"
        f"CATEGORY: {story.category}"
    )


def generate_base_image(story: news.Story) -> Image.Image:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is missing. AI-only image mode refuses to fall back to stock/template covers."
        )

    response = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENAI_IMAGE_MODEL,
            "prompt": visual_prompt(story),
            "size": "1024x1536",
            "quality": OPENAI_IMAGE_QUALITY,
            "n": 1,
        },
        timeout=180,
    )
    if not response.ok:
        # Never log the API key or full provider response.
        raise RuntimeError(f"OpenAI image generation failed with HTTP {response.status_code}")

    payload = response.json()
    data = payload.get("data") or []
    if not data:
        raise RuntimeError("OpenAI image generation returned no image")

    item = data[0]
    raw: bytes
    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        image_response = requests.get(item["url"], timeout=90)
        image_response.raise_for_status()
        raw = image_response.content
    else:
        raise RuntimeError("OpenAI image response has no supported image field")

    image = Image.open(io.BytesIO(raw))
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
        used = sum(len(line.split()) for line in lines)
        remaining = words[used:]
        last = " ".join(remaining)
        while last:
            bbox = draw.textbbox((0, 0), last, font=fnt)
            if bbox[2] - bbox[0] <= max_width:
                break
            parts = last.split()
            if len(parts) <= 1:
                break
            last = " ".join(parts[:-1])
        if used + len(last.split()) < len(words):
            last = last.rstrip(" .,!?:;-") + "…"
        lines.append(last or current)
    return lines[:max_lines]


def short_headline(story: news.Story) -> str:
    value = news.clean_html(story.title).strip().rstrip(".!?")
    if len(value) <= 88:
        return value
    return value[:88].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"


def short_badges(story: news.Story) -> list[str]:
    result: list[str] = []
    for fact in facts(story):
        words = fact.split()
        text = " ".join(words[:8]).rstrip(".,;:-")
        if len(words) > 8:
            text += "…"
        if text:
            result.append(text)
    return result[:2]


def compose_poster(story: news.Story, generated: Image.Image, out: Path) -> Path:
    width, height = 1080, 1350
    image = ImageOps.fit(generated, (width, height), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)).convert("RGBA")

    # Darken only the text zones, preserving the AI visual in the center.
    shade = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shade_draw = ImageDraw.Draw(shade)
    for y in range(0, 520):
        alpha = int(185 - (y / 520) * 75)
        shade_draw.line((0, y, width, y), fill=(3, 8, 20, max(70, alpha)))
    for y in range(950, height):
        alpha = int(85 + ((y - 950) / 400) * 120)
        shade_draw.line((0, y, width, y), fill=(3, 8, 20, min(215, alpha)))
    shade = shade.filter(ImageFilter.GaussianBlur(8))
    canvas = Image.alpha_composite(image, shade)
    draw = ImageDraw.Draw(canvas)

    headline = short_headline(story)
    title_size = 68 if len(headline) < 52 else 58
    title_font = font(title_size, True)
    title_lines = wrap_lines(draw, headline, title_font, 930, 3)

    y = 72
    for line in title_lines:
        # soft shadow + crisp white type
        draw.text((77, y + 4), line, font=title_font, fill=(0, 0, 0, 165), stroke_width=2, stroke_fill=(0, 0, 0, 130))
        draw.text((72, y), line, font=title_font, fill=(255, 255, 255), stroke_width=1, stroke_fill=(0, 0, 0, 90))
        y += title_size + 12

    badges = short_badges(story)
    badge_y = 1040
    badge_width = 440 if len(badges) > 1 else 940
    for index, text in enumerate(badges):
        x0 = 70 + index * 470
        x1 = min(1010, x0 + badge_width)
        draw.rounded_rectangle((x0, badge_y, x1, 1240), radius=34, fill=(8, 18, 38, 210), outline=(255, 255, 255, 70), width=2)
        badge_font = font(29, True)
        lines = wrap_lines(draw, text, badge_font, x1 - x0 - 56, 4)
        ty = badge_y + 28
        for line in lines:
            draw.text((x0 + 28, ty), line, font=badge_font, fill=(255, 255, 255))
            ty += 38

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out, "JPEG", quality=93, optimize=True, progressive=True)
    return out


def generate_cover(story: news.Story, _meta: dict | None = None) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw = generate_base_image(story)
    digest = news.hashlib.sha1(news.normalize(story.title).encode("utf-8")).hexdigest()[:16]
    out = OUT_DIR / f"ai-{digest}.jpg"
    return compose_poster(story, raw, out)


def self_test() -> None:
    sample = news.Story(
        title="Roblox готовит игры без установки приложения",
        link="https://example.com",
        source="Demo",
        summary=(
            "Компания планирует запускать игры прямо в браузере без обязательной установки приложения. "
            "Отдельные проекты также смогут выходить как самостоятельные приложения для разных устройств."
        ),
        published=news.datetime.now(news.timezone.utc),
        category="games",
    )
    # Offline composition test: no paid API call.
    base = Image.new("RGB", (1024, 1536), (22, 55, 118))
    d = ImageDraw.Draw(base)
    d.ellipse((220, 360, 820, 960), fill=(58, 126, 235))
    d.rectangle((300, 690, 760, 1140), fill=(28, 38, 78))
    out = OUT_DIR / "ai-cover-self-test.jpg"
    compose_poster(sample, base, out)
    probe = Image.open(out)
    if probe.size != (1080, 1350) or probe.format != "JPEG" or out.stat().st_size < 20_000:
        raise RuntimeError("AI cover composition self-test failed")
    print(f"AI COVER SELF-TEST OK: {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        raise SystemExit("Use this module through autopost_runner.py")
