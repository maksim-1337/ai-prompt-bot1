from pathlib import Path

from gtts import gTTS

from .core import now, plan_from_story
from .direct_run import _install_production_audio_stage
from . import render as render_module


def clear_voice(text, folder, demo=False):
    audio = folder / "voice.mp3"
    gTTS(text=text, lang="ru", slow=False).save(str(audio))
    if not audio.exists() or audio.stat().st_size < 1000:
        raise RuntimeError("gTTS returned empty audio")
    words = text.split()
    # Use a deliberately conservative scene length so the final words are never cut.
    duration = max(4.8, len(words) / 1.75 + 0.7)
    timings = [
        {"text": w, "start": i * duration / len(words), "end": (i + 1) * duration / len(words)}
        for i, w in enumerate(words)
    ]
    groups = []
    for i in range(0, len(timings), 5):
        chunk = timings[i:i+5]
        groups.append({
            "text": " ".join(w["text"] for w in chunk),
            "start": chunk[0]["start"],
            "end": chunk[-1]["end"],
        })
    return audio, duration, groups, "gtts-clear"


def main():
    _install_production_audio_stage()
    render_module.voice = clear_voice
    story = {
        "title": "NASA и IBM обучили ИИ анализировать поверхность Луны",
        "summary": (
            "Новая открытая модель объединяет данные нескольких лунных миссий и помогает распознавать особенности поверхности. "
            "Система предназначена для поиска объектов вроде кратеров и потенциальных районов с водяным льдом. "
            "Такие инструменты могут помочь исследователям быстрее выбирать интересные и безопасные зоны для будущих миссий."
        ),
        "source": "NASA / IBM",
        "link": "https://www.nasa.gov/",
        "category": "science",
        "published": now(),
    }
    plan = plan_from_story(story)
    plan["label"] = "КОСМОС / AI"
    video, manifest = render_module.render(plan, Path("output/neural-news"), variant=7, demo=False)
    print("READY", video, manifest["duration"], manifest["audio"])


if __name__ == "__main__":
    main()
