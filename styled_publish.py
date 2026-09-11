import html

import imageio_ffmpeg
from mutagen import File as MutagenFile

import main as bot


def styled_build_post(story: bot.Story) -> str:
    title = html.escape(story.title.rstrip('.!?') + '.')
    facts = bot.sentences(story.summary)
    body_parts = []
    for fact in facts:
        if bot.normalize(story.title) in bot.normalize(fact):
            continue
        cleaned = fact.strip()
        if cleaned:
            body_parts.append(cleaned)
        if len(body_parts) == 3:
            break

    if not body_parts and story.confirmations >= 2:
        body_parts.append("Эту же историю подхватили сразу несколько независимых источников.")
    elif not body_parts:
        body_parts.append("Подробности пока появляются — новость только начала расходиться по медиа.")

    quote_lines = "\n".join(
        f"— {html.escape(item.rstrip('.!?') + '.')}" for item in body_parts
    )
    reaction = html.escape(bot.punchline(story))

    text = (
        f"<b>{title}</b>\n\n"
        f"<b>Что известно:</b>\n"
        f"<blockquote>{quote_lines}</blockquote>\n\n"
        f"{reaction}"
    )
    return text[:1000]


def audio_duration(path) -> float:
    try:
        media = MutagenFile(str(path))
        if media and getattr(media, "info", None) and getattr(media.info, "length", None):
            return float(media.info.length)
    except Exception as exc:
        print("Could not read audio duration:", exc)
    return 18.0


def styled_make_video(story: bot.Story):
    facts = bot.sentences(story.summary)
    fact1 = facts[0] if facts else "Подробности появляются прямо сейчас."
    fact2 = facts[1] if len(facts) > 1 else (
        "Новость подтверждают несколько источников."
        if story.confirmations >= 2
        else "Следим за развитием истории."
    )
    ending = bot.punchline(story)

    slides = [bot.OUT_DIR / f"slide_{i}.jpg" for i in range(1, 5)]
    bot.create_slide(slides[0], story, "headline", story.title, 1)
    bot.create_slide(slides[1], story, "fact", fact1, 2)
    bot.create_slide(slides[2], story, "fact", fact2, 3)
    bot.create_slide(slides[3], story, "ending", ending, 4)

    script = f"{story.title}. {fact1} {fact2} {ending}"
    voice = bot.make_voice(script)
    duration = max(16.0, audio_duration(voice))
    per = duration / 4.0
    out = bot.OUT_DIR / "news_short.mp4"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    cmd = [ffmpeg, "-y"]
    for slide in slides:
        cmd += ["-loop", "1", "-framerate", "30", "-t", f"{per:.2f}", "-i", str(slide)]
    cmd += ["-i", str(voice)]

    filters = []
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
        "-filter_complex", ";".join(filters),
        "-map", "[v]",
        "-map", "4:a",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        "-movflags", "+faststart",
        str(out),
    ]
    bot.run(cmd)
    return out


def styled_send_video(video, caption: str) -> None:
    if not bot.BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    url = f"https://api.telegram.org/bot{bot.BOT_TOKEN}/sendVideo"
    with video.open("rb") as handle:
        response = bot.requests.post(
            url,
            data={
                "chat_id": bot.CHANNEL,
                "caption": caption,
                "supports_streaming": "true",
                "parse_mode": "HTML",
            },
            files={"video": (video.name, handle, "video/mp4")},
            timeout=120,
        )
    if not response.ok:
        raise RuntimeError(f"Telegram error {response.status_code}: {response.text[:500]}")
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API rejected request: {payload}")


bot.build_post = styled_build_post
bot.make_video = styled_make_video
bot.telegram_send_video = styled_send_video

if __name__ == "__main__":
    bot.main()
