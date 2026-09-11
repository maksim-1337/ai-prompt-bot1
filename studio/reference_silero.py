from __future__ import annotations

import os
import wave
from pathlib import Path

import torch

from .core import now, plan_from_story
from .direct_run import _install_production_audio_stage
from . import render as render_module

SAMPLE_RATE = 48000
SPEAKER = os.getenv("SILERO_SPEAKER", "aidar")
MODEL_URL = "https://models.silero.ai/models/tts/ru/v5_ru.pt"
_MODEL = None


def load_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    cache = Path.home() / ".cache" / "newslight"
    cache.mkdir(parents=True, exist_ok=True)
    local = cache / "silero_v5_ru.pt"
    if not local.exists():
        torch.hub.download_url_to_file(MODEL_URL, str(local), progress=True)
    model = torch.package.PackageImporter(str(local)).load_pickle("tts_models", "model")
    model.to(torch.device("cpu"))
    torch.set_num_threads(4)
    _MODEL = model
    return model


def save_pcm_wav(audio_tensor, path: Path):
    data = audio_tensor.detach().cpu().flatten().clamp(-1, 1).mul(32767).to(torch.int16).tolist()
    import array
    pcm = array.array("h", data)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


def male_voice(text, folder, demo=False):
    model = load_model()
    audio = folder / "voice.wav"
    waveform = model.apply_tts(
        text=text,
        speaker=SPEAKER,
        sample_rate=SAMPLE_RATE,
        put_accent=True,
        put_yo=True,
    )
    save_pcm_wav(waveform, audio)
    duration = len(waveform) / SAMPLE_RATE + 0.18
    words = text.split()
    timings = [
        {"text": w, "start": i * duration / max(1, len(words)), "end": (i + 1) * duration / max(1, len(words))}
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
    return audio, duration, groups, f"silero-{SPEAKER}"


def main():
    _install_production_audio_stage()
    render_module.voice = male_voice
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
    video, manifest = render_module.render(plan, Path("output/male-news"), variant=7, demo=False)
    print("READY", video, manifest["duration"], manifest["audio"])


if __name__ == "__main__":
    main()
