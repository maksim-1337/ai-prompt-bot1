from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from .sources import Story, canonical_url

MAX_AGE = timedelta(hours=18)
CATEGORIES = {
    "sports": r"\b(спорт\w*|футбол\w*|хокке\w*|теннис\w*|олимп\w*|чемпион\w*|гран при|формул\w* 1|матч\w*|трансфер\w*)\b",
    "politics": r"\b(политик\w*|президент\w*|парламент\w*|министр\w*|выбор\w*|санкци\w*|правительств\w*|госдум\w*|нато|оон|мирн\w* соглашен\w*)\b",
    "games": r"\b(игр[аыуе]\w*|игров\w*|геймер\w*|steam|playstation|xbox|nintendo|roblox|minecraft|cs2|gta)\b",
    "social": r"\b(соцсет\w*|telegram|tiktok|youtube|instagram|вконтакте|блогер\w*|стример\w*)\b",
    "series": r"\b(сериал\w*|сезон\w*|netflix|hbo)\b",
    "cinema": r"\b(кино\w*|фильм\w*|актер\w*|актрис\w*|оскар\w*|режиссер\w*|прокат\w*)\b",
    "science": r"\b(наук\w*|учен\w*|исследован\w*|космос\w*|nasa|spacex|телескоп\w*|экзопланет\w*|физик\w*|биолог\w*)\b",
    "technology": r"\b(технолог\w*|ии|нейросет\w*|искусственн\w* интеллект\w*|openai|chatgpt|claude|gemini|apple|google|microsoft|iphone|android|смартфон\w*|робот\w*|процессор\w*|видеокарт\w*)\b",
    "internet": r"\b(интернет\w*|браузер\w*|сайт\w*|взлом\w*|утечк\w*|кибератак\w*)\b",
}
LABELS = dict(zip(CATEGORIES, ("Спорт", "Политика", "Игры", "Соцсети", "Сериалы", "Кино", "Наука", "Технологии", "Интернет")))
HARD_REJECT = (
    r"\b(гороскоп\w*|промокод\w*|котировк\w*|дивиденд\w*|облигаци\w*|лотере\w*|казино|букмекер\w*)\b",
    r"курс (?:доллара|евро|валют)|индекс мосбиржи|акции .* (?:выросли|упали)",
    r"\b(?:я|мы) (?:написал\w*|сделал\w*|создал\w*|разработал\w*|попробовал\w*)\b",
    r"пресс[ -]релиз|партнерский материал|на правах рекламы|рекламирует|рекламная кампания",
    r"вы не поверите|шокирующ\w*|взорвал\w* интернет|все в шоке|срочно смотрите|сенсацион\w*",
    r"провел\w* заседание|провел\w* совещание|рабочая встреча|поздравил\w* с|выразил\w* обеспокоенность",
    r"обзор\w*|распаковк\w*|топ[ -]?\d+|лучшие .* (?:купить|выбрать)|скидк\w*|распродаж\w*",
)
IMPACT = (
    r"\bвпервые\b", r"\bрекорд\w*", r"\bмиллион\w*", r"\bмиллиард\w*",
    r"\bзапрет\w*", r"\bзаблок\w*", r"\bмасштабн\w*", r"\bглобальн\w*",
    r"\bоткрыли\b", r"\bобнаружили\b", r"\bдоказали\b", r"\bнобел\w*",
    r"\bрелиз\w*", r"\bвыпустил\w*", r"\bзапустил\w*", r"\bпремьер\w*",
    r"\bфинал\w*", r"\bчемпион\w*", r"\bвыбор\w*", r"\bпринял\w* закон",
    r"\bвступил\w* в силу", r"\bотменил\w*", r"\bотставк\w*", r"мирн\w* соглашен\w*",
)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^а-яa-z0-9 ]", " ", text.lower().replace("ё", "е"))).strip()


def same_event(left: str, right: str) -> bool:
    a, b = norm(left), norm(right)
    aw, bw = set(a.split()), set(b.split())
    # Distinct publisher headlines often share little wording around named events.
    for event in ("ipo", "diablo v", "starcraft"):
        if event in a and event in b:
            brands = {"openai", "blizzard", "anthropic", "apple", "google"}
            if (aw & bw & brands) or event != "ipo":
                return True
    return bool(a and b) and (SequenceMatcher(None, a, b).ratio() >= .72 or len(aw & bw) / max(1, min(len(aw), len(bw))) >= .72)


def story_id(story: Story) -> str:
    return hashlib.sha256(canonical_url(story.url).encode()).hexdigest()[:24]


def category(story: Story) -> str | None:
    # Prefer the headline's topic. "Выиграл" is not a gaming keyword.
    for text in (norm(story.title), norm(story.title + " " + story.summary)):
        for name, pattern in CATEGORIES.items():
            if re.search(pattern, text):
                return name
    return None


def assess(story: Story, now: datetime) -> tuple[float, str, str]:
    try:
        canonical_url(story.url)
    except ValueError:
        return 0, "", "не редакционный источник"
    if not story.published.tzinfo or not -timedelta(minutes=5) <= now - story.published <= MAX_AGE:
        return 0, "", "дата отсутствует, в будущем или старше 18 часов"
    text = norm(story.title + " " + story.summary)
    title = norm(story.title)
    if any(re.search(p, text) for p in HARD_REJECT):
        return 0, "", "реклама, рутина, авторская статья или кликбейт"
    topic = category(story)
    if not topic:
        return 0, "", "вне разрешенных тематик"
    if topic == "sports" and not re.search(r"миров\w* рекорд|рекорд\w* трансфер|финал\w* (?:чемпионата мира|лиги чемпионов)|олимп\w*|чемпион\w* мира", text):
        return 0, topic, "проходной спорт: нужен крупный финал, мировой рекорд или громкий трансфер"
    if topic == "politics" and not re.search(r"выбор\w*|отставк\w*|принял\w* закон|вступил\w* в силу|санкци\w*|мирн\w* соглашен\w*|ратифиц\w*|импичмент\w*", title):
        return 0, topic, "политическое заявление без значимого решения"
    if len(story.summary) < 100 or norm(story.summary) == title:
        return 0, topic, "недостаточно фактов"
    hits = sum(bool(re.search(pattern, text)) for pattern in IMPACT)
    if not hits:
        return 0, topic, "нет выраженного новостного события"
    age = max(0, (now - story.published).total_seconds() / 3600)
    score = 5 + min(3, hits) + (1.5 if age <= 6 else .5)
    return score, topic, f"Свежее событие; значимость {hits}; возраст {age:.1f} ч. Требуется проверка редактором."


def validate_post(text: str) -> None:
    blocks = text.strip().split("\n\n")
    if not 3 <= len(blocks) <= 5 or any(not b.strip() for b in blocks):
        raise ValueError("Нужны первая строка, 1–3 абзаца и короткая концовка")
    if "\n" in blocks[0] or len(blocks[0]) > 240 or len(blocks[-1]) > 140:
        raise ValueError("Слишком длинная первая строка или концовка")
    if any(len(b) > 650 for b in blocks[1:-1]) or len(text.encode("utf-16-le")) // 2 > 1800:
        raise ValueError("Текст слишком длинный")
    if re.search(r"https?://|www\.|t\.me/|<[^>]+>|&(?:quot|amp|lt|gt|#\d+);|ТРЕБУЕТСЯ|\[ДОПИШИ", text, re.I):
        raise ValueError("В тексте ссылка, HTML или незаполненный шаблон")
    if any(b.rstrip().endswith(("…", "...")) for b in blocks):
        raise ValueError("Оборванное предложение")
    if blocks[0].startswith(("#", "**", "Источник:")):
        raise ValueError("Первая строка должна сразу сообщать факт")
