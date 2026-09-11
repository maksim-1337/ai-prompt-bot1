import html
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
bot.telegram_send_video = styled_send_video

if __name__ == "__main__":
    bot.main()
