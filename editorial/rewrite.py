"""CPU paraphrasing, no paid inference API, no images, no remote model code."""
from __future__ import annotations

import re

from .quality import norm, validate_post
from .sources import Story, clean

MODEL = "cointegrated/rut5-base-paraphraser"
CLOSERS = {
    "sports": ("Такой результат ещё будут пересматривать.", "Вот теперь есть что обсуждать после финального свистка."),
    "politics": ("Теперь главное — что изменится на практике.", "За формулировками стоит следить не меньше, чем за заголовками."),
    "games": ("Планы на свободный вечер снова под угрозой.", "В списке «поиграть потом» опять пополнение."),
    "social": ("Лента сама себя не обсудит.", "У групповых чатов появилась новая тема."),
    "internet": ("Ещё один повод проверить свои настройки.", "Кажется, в сети намечается длинное обсуждение."),
    "cinema": ("Осталось договориться, кто берёт попкорн.", "Список «посмотреть на выходных» снова растёт."),
    "series": ("Сон снова проигрывает сериалам.", "Ещё одна причина не верить себе на словах «одна серия»."),
    "science": ("У фантастов появился новый материал.", "К школьному учебнику просится дополнение."),
    "technology": ("Ещё одна новость, которую вчера пришлось бы объяснять дольше.", "Будет интересно проверить это в деле."),
}


def closer(story: Story, topic: str, used: set[str]) -> str:
    if re.search(r"погиб|жертв|смерт|теракт|войн|трагед|ранен", norm(story.title + " " + story.summary)):
        return "Здесь важнее дождаться проверенных подробностей."
    options = CLOSERS[topic]
    return next((text for text in options if text not in used), options[0])


def facts(story: Story) -> list[str]:
    result = []
    for sentence in re.split(r"(?<=[.!?])\s+", clean(story.summary)):
        if not 55 <= len(sentence) <= 500 or sentence.endswith(("...", "…")):
            continue
        if not sentence.endswith((".", "!", "?", "»")):
            continue
        if norm(sentence) == norm(story.title) or re.search(r"читайте|подписывайтесь|ria\.ru|©", sentence, re.I):
            continue
        if sentence not in result:
            result.append(sentence)
    return result[:2]


def check_rewrite(original: str, rewritten: str) -> None:
    """Cheap observable checks, not a claim of semantic fact verification."""
    if len(rewritten) < 25 or len(rewritten) > 650 or norm(rewritten) == norm(original):
        raise ValueError("Модель не дала самостоятельный пересказ")
    numbers = lambda text: set(re.findall(r"\d+(?:[.,]\d+)?", text))
    if numbers(original) != numbers(rewritten):
        raise ValueError("При пересказе изменились числа")
    names = lambda text: set(re.findall(r"\b[A-Za-z][A-Za-z0-9-]{2,}\b", text))
    if names(original) != names(rewritten):
        raise ValueError("При пересказе изменились названия")
    if bool(re.search(r"\bне\b", original)) != bool(re.search(r"\bне\b", rewritten)):
        raise ValueError("При пересказе изменилось отрицание")
    # Keep announcements/rumours attributed; do not turn plans into completed facts.
    markers = r"планир|обеща|предполож|возмож|ожида|по данным|сообщ|заяв|рассказ|предлож|может|могут"
    if re.search(markers, original, re.I) and not re.search(markers, rewritten, re.I):
        raise ValueError("При пересказе пропала оговорка или атрибуция")
    words = norm(original).split()
    target = " " + norm(rewritten) + " "
    if any(" " + " ".join(words[i:i + 10]) + " " in target for i in range(len(words) - 9)):
        raise ValueError("Слишком длинное дословное совпадение")


class LocalEditor:
    def __init__(self):
        self.model = self.tokenizer = None

    def paraphrase(self, text: str) -> str:
        import torch
        from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

        if self.model is None:
            torch.set_num_threads(2)
            self.tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=False, use_fast=False)
            self.model = AutoModelForSeq2SeqLM.from_pretrained(
                MODEL, trust_remote_code=False, use_safetensors=True,
            ).eval()
        encoded = self.tokenizer(text, return_tensors="pt", truncation=True, max_length=256)
        with torch.inference_mode():
            output = self.model.generate(
                **encoded, num_beams=3, do_sample=False, max_new_tokens=200,
                no_repeat_ngram_size=3, encoder_no_repeat_ngram_size=5,
            )
        rewritten = self.tokenizer.decode(output[0], skip_special_tokens=True).strip()
        check_rewrite(text, rewritten)
        return rewritten.rstrip(".!?") + "."

    def compose(self, story: Story, topic: str, used: set[str]) -> str:
        detail = facts(story)
        if not detail:
            raise ValueError("Источник не содержит законченных подробностей")
        blocks = [self.paraphrase(story.title)]
        blocks.extend(self.paraphrase(sentence) for sentence in detail)
        blocks.append(closer(story, topic, used))
        text = "\n\n".join(blocks)
        validate_post(text)
        return text


if __name__ == "__main__":
    # Exercise tokenizer, model loading AND inference; import-only checks missed protobuf.
    editor = LocalEditor()
    editor.paraphrase("Компания представила новое приложение для пользователей.")
    print("Local text model inference: OK (no paid API)")
